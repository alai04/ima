#!/usr/bin/env python3
"""从百度网盘分享链接下载整个文件夹（支持提取码）。

用法示例::

    python baidu_pan_download.py https://pan.baidu.com/s/1lpUp14K-1CXccXny5RlYmg --pwd 0203

只依赖 ``requests``，登录态通过 Cookie 提供（BDUSS 是必须项）：::

    export BAIDU_COOKIE='BDUSS=xxxx; STOKEN=yyyy; ...'   # 或写进 .env
    python baidu_pan_download.py <分享链接> --pwd 0203

两种工作模式（``--mode``）：

* ``transfer``（默认）：先把分享内容「转存」到自己的网盘，再从自己的网盘取直链下载。
  只用到了最稳定的接口（``/share/verify`` + ``/share/transfer`` + ``/api/list`` + ``/api/download``），
  文件夹结构会被完整保留；下载完可用 ``--cleanup`` 删掉网盘里的临时副本。
* ``share``: 不转存，直接遍历分享目录并用 ``/api/sharedownload`` 取每个文件直链下载。
  不占用网盘空间，但接口较老、风控更严，适合小规模下载。

Cookie 获取方式：浏览器登录 https://pan.baidu.com/ ，F12 → Network → 任意请求 →
复制请求头里的 ``Cookie`` 整串。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import posixpath
import queue
import re
import sqlite3
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import requests

try:  # 项目已依赖 python-dotenv，读 .env 里的 BAIDU_COOKIE
    from dotenv import load_dotenv

    _HERE = os.path.dirname(os.path.abspath(__file__))
    # 先读脚本目录，再读当前工作目录（已有的环境变量不会被覆盖）
    load_dotenv(os.path.join(_HERE, ".env"))
    if os.path.abspath(os.getcwd()) != _HERE:
        load_dotenv(os.path.join(os.getcwd(), ".env"))
except Exception:  # pragma: no cover - 没有该依赖时静默跳过
    pass

BASE_URL = "https://pan.baidu.com"
APP_ID = "250528"
PROJECT_DIR = Path(__file__).resolve().parent

# 转存时默认使用的网盘临时目录前缀，下载完可安全删除
DEFAULT_PAN_ROOT = "/ima_download"

# 注意：不要手动设置 Host 头（虽然值看着一样），百度反爬会因此把分享页 302 跳转到 passport 登录页。
WEB_HEADERS = {
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    # 百度偶尔会「声明 gzip 但内容不是 gzip」，关掉压缩最稳（页面/JSON 都很小）
    "Accept-Encoding": "identity",
    "Referer": "https://pan.baidu.com/disk/home",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}
# 解析 dlink 必须用这个 UA（百度自己校验的）
RESOLVE_UA = "LogStatistic"
# 下载真实文件时可用的 UA，逐个尝试
DOWNLOAD_UAS = (
    "netdisk;P2SP;3.0.0.8;android-android;",
    "pan.baidu.com",
    "netdisk;2.2.51.6;netdisk;10.0.63;PC;android-android",
)

CHUNK_SIZE = 1 << 17  # 128 KiB

# /share/verify 错误码 → 人话
VERIFY_ERRNO_MSG = {
    -9: "提取码错误",
    -12: "提取码错误",
    -62: "访问过于频繁，请稍后再试",
    105: "分享链接不存在或已失效",
    2: "分享链接已失效",
}

TRANSFER_ERRNO_MSG = {
    -1: "链接错误、失效或缺少提取码",
    -4: "无效登录，请重新获取 Cookie",
    -6: "请用浏览器无痕模式重新获取 Cookie",
    -7: "目标目录名含非法字符（< > | * ? \\ :）",
    -8: "目标目录中已存在同名文件",
    -9: "提取码错误",
    -10: "网盘容量不足",
    -62: "链接访问次数过多，请手动转存或稍后再试",
    2: "目标目录不存在",
    4: "目录中存在同名文件",
    12: "转存文件数超过限制（会员限制）",
    20: "网盘容量不足",
    105: "所访问的页面不存在",
    404: "秒传无效",
}

_lock = threading.Lock()


class PanError(RuntimeError):
    """网盘操作失败。"""


# ────────────────────────────── 基础工具 ──────────────────────────────


def log(msg: str) -> None:
    """线程安全的日志输出。"""
    with _lock:
        print(msg, flush=True)


def human_size(num: float) -> str:
    """字节数转可读字符串。"""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024 or unit == "TB":
            return f"{num:.1f}{unit}" if unit != "B" else f"{int(num)}B"
        num /= 1024
    return f"{num:.1f}TB"


def extract_surl(url: str) -> str:
    """从分享链接中取出 surl。

    分享链接形如 ``https://pan.baidu.com/s/1lpUp14K-1CXccXny5RlYmg``，
    其中 ``/s/`` 后面那个前导 ``1`` 是固定前缀，真正的 surl 是
    ``lpUp14K-1CXccXny5RlYmg``（``/share/init?surl=xxx`` 形式则不含前导 1）。
    """
    url = url.strip()
    m = re.search(r"[?&]surl=([A-Za-z0-9_\-]+)", url)
    if m:
        surl = m.group(1)
        return surl[1:] if len(surl) > 23 and surl.startswith("1") else surl
    m = re.search(r"/s/([A-Za-z0-9_\-]+)", url)
    if m:
        seg = m.group(1)
        return seg[1:] if seg.startswith("1") else seg
    m = re.search(r"([A-Za-z0-9_\-]{20,})", url)
    if m:
        seg = m.group(1)
        return seg[1:] if seg.startswith("1") else seg
    raise PanError(f"无法从链接中解析出 surl：{url}")


def share_page_url(surl: str) -> str:
    """surl（不含前导 1）→ 分享页面地址。"""
    return f"{BASE_URL}/s/1{surl}"


# Cookie 字段名允许的字符；用「name=value」token 扫描整个输入，兼容分号/空格/制表符/换行分隔
_COOKIE_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
_PAIR_FIND_RE = re.compile(r"([A-Za-z0-9_.\-]+)\s*=\s*([^;\s]*)")


def parse_cookie_string(raw: str) -> dict[str, str]:
    """把各种粘贴形态的 Cookie 解析成 ``{name: value}``。

    兼容：

    * 浏览器请求头整段粘贴（带 ``Cookie:`` 前缀、含换行/续行符）
    * ``BDUSS=xxx; STOKEN=yyy`` 形式
    * 只有裸 BDUSS 值
    * EditThisCookie / Cookie-Editor 导出的 JSON
    """
    text = (raw or "").strip()
    if not text:
        return {}

    if text[:1] in "[{":  # JSON 导出
        pairs = _parse_cookie_json(text)
        if pairs:
            return pairs

    # 行尾续行符（复制请求头时常见）：先拼回一行
    text = re.sub(r"\\\s*\r?\n\s*", " ", text)

    # 多行粘贴（整段请求头）：只保留含 BDUSS/STOKEN 的那些行
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) > 1:
        keep = [ln for ln in lines if re.search(r"BDUSS|STOKEN", ln)]
        text = " ".join(keep or lines)

    # 去掉 "Cookie:" / "Set-Cookie:" 前缀与反引号
    text = re.sub(r"^[`\s]*(?:set-)?cookie\s*:\s*", "", text, flags=re.I).strip()
    text = text.strip("`").strip()

    pairs: dict[str, str] = {}
    for name, value in _PAIR_FIND_RE.findall(text):
        if not _COOKIE_NAME_RE.match(name):
            continue
        pairs[name] = value.strip().strip("'\"`")
    return pairs


def _parse_cookie_json(text: str) -> dict[str, str]:
    """解析 Cookie 导出插件生成的 JSON（数组或对象形式）。"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    items: Any = data
    if isinstance(data, dict):
        inner = data.get("cookies") or data.get("Cookies") or data
        if isinstance(inner, dict):
            items = [{"name": k, "value": v} for k, v in inner.items()]
        else:
            items = inner
    pairs: dict[str, str] = {}
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict) and item.get("name"):
                pairs[str(item["name"])] = str(item.get("value", ""))
    return pairs


def normalize_cookie(raw: str) -> str:
    """把用户输入整理成可直接放进请求头的 Cookie 串。

    除了清洗格式，还会把新版百度只下发的 ``BDUSS_BFESS`` / ``STOKEN_BFESS``
    回填到经典的 ``BDUSS`` / ``STOKEN`` 字段上（否则接口会返回 errno=-6）。
    """
    pairs = parse_cookie_string(raw)
    if not pairs:
        value = (raw or "").strip().strip("'\"`").strip()
        if not value:
            return ""
        # 既无 "=" 也无 ";"，视为「裸 BDUSS 值」
        if "=" not in value and ";" not in value and " " not in value:
            return f"BDUSS={value}"
        return ""
    for base in ("BDUSS", "STOKEN"):
        if base not in pairs and f"{base}_BFESS" in pairs:
            pairs[base] = pairs[f"{base}_BFESS"]
    return "; ".join(f"{name}={value}" for name, value in pairs.items())


def cookie_field_names(cookie: str) -> list[str]:
    """列出 Cookie 里含有的字段名（用于诊断，不泄露值）。"""
    return sorted(parse_cookie_string(cookie))


def cookie_value(cookie: str, name: str) -> str:
    """从 Cookie 串里取某个键的值。"""
    m = re.search(rf"(?:^|;\s*){re.escape(name)}=([^;]*)", cookie)
    return m.group(1) if m else ""


def _first_nonzero(*values: Any) -> str:
    """返回第一个非空且不为 "0" 的值（百度用 ""/0 表示未登录或无值）。"""
    for value in values:
        if value is None:
            continue
        text = str(value)
        if text and text != "0":
            return text
    return ""


def _extract_json_object(text: str, start: int) -> str | None:
    """从 ``text[start]`` 处（应是 ``{``）扫描出配平的 JSON 对象字符串。"""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def extract_page_json(html: str) -> dict[str, Any] | None:
    """从分享页 HTML 中抽出 yunData/locals.mset 的配置对象。

    页面里可能是标准 JSON（``locals.mset({...})``），也可能是 JS 对象字面量
    （``window.yunData={skinName:'white', neglect:1}``），后者需先粗转成 JSON。
    """
    for pattern in (
        r"locals\.mset\(",
        r"yunData\.setData\(",
        r"window\.yunData\s*=\s*",
        r"yunData\s*=\s*",
    ):
        m = re.search(pattern, html)
        if not m:
            continue
        brace = html.find("{", m.end())
        if brace < 0:
            continue
        blob = _extract_json_object(html, brace)
        if not blob:
            continue
        for candidate in (blob, js_object_to_json(blob)):
            try:
                data = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return data
    return None


_JSON_ESC = re.compile(r"\\u([0-9a-fA-F]{4})|\\/")


def _unescape_json_str(text: str) -> str:
    """把 JSON 字符串里的 \\uXXXX 与 \\/ 还原成普通文本。"""
    return _JSON_ESC.sub(
        lambda m: chr(int(m.group(1), 16)) if m.group(1) else "/", text
    )


def js_object_to_json(text: str) -> str:
    """把 JS 对象字面量粗转成 JSON（补 key 引号、单引号转双引号）。"""
    text = re.sub(r"([{,[]\s*)([A-Za-z_$][\w$]*)\s*:", r'\1"\2":', text)
    text = re.sub(r"'((?:[^'\\]|\\.)*)'", lambda m: json.dumps(m.group(1)), text)
    return text


def extract_entries_fallback(html: str) -> list["ShareEntry"]:
    """整页兜底：先找 file_list 对象，再退回按 fs_id 分段的正则抓取。"""
    entries = _entries_from_file_list_blob(html)
    if entries:
        return entries

    entries = []
    seen: set[str] = set()
    # key 可能带引号也可能不带（JS 对象字面量）
    for m in re.finditer(r'\bfs_id"?\s*[:=]\s*"?(\d+)"?', html):
        fs_id = m.group(1)
        if fs_id in seen:
            continue
        seg = html[m.end() : m.end() + 800]
        name_m = re.search(r'\bserver_filename"?\s*[:=]\s*["\'](.*?)["\']', seg)
        if not name_m:
            continue
        isdir_m = re.search(r'\bisdir"?\s*[:=]\s*"?(\d)', seg)
        size_m = re.search(r'\bsize"?\s*[:=]\s*(\d+)', seg)
        path_m = re.search(r'\bpath"?\s*[:=]\s*["\'](.*?)["\']', seg)
        seen.add(fs_id)
        entries.append(
            ShareEntry(
                fs_id=fs_id,
                name=_unescape_json_str(name_m.group(1)),
                isdir=bool(isdir_m and isdir_m.group(1) == "1"),
                size=int(size_m.group(1)) if size_m else 0,
                path=_unescape_json_str(path_m.group(1)) if path_m else "",
            )
        )
    return entries


def _entries_from_file_list_blob(html: str) -> list["ShareEntry"]:
    """定位页面里的 file_list 对象并解析成条目列表。"""
    m = re.search(r'file_list"?\s*[:=]\s*', html)
    if not m:
        return []
    brace = html.find("{", m.end())
    if brace < 0:
        return []
    blob = _extract_json_object(html, brace)
    if not blob:
        return []
    for candidate in (blob, js_object_to_json(blob)):
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        items = obj.get("list") if isinstance(obj, dict) else None
        if isinstance(items, list) and items:
            return [_entry_from_dict(it) for it in items if isinstance(it, dict)]
    return []


def _entry_from_dict(item: dict[str, Any]) -> "ShareEntry":
    """把 /share/list 或页面 file_list 中的一个条目转成 ShareEntry。"""
    return ShareEntry(
        fs_id=str(item.get("fs_id")),
        name=str(item.get("server_filename") or ""),
        isdir=bool(int(item.get("isdir") or 0)),
        size=int(item.get("size") or 0),
        path=str(item.get("path") or ""),
    )


# ────────────────────────────── 数据模型 ──────────────────────────────


@dataclass
class ShareEntry:
    """分享目录中的一个条目。"""

    fs_id: str
    name: str
    isdir: bool
    size: int = 0
    path: str = ""


@dataclass
class PanFile:
    """自己网盘中的一个文件（用于下载）。"""

    fs_id: str
    path: str
    size: int = 0
    md5: str = ""

    @property
    def name(self) -> str:
        return posixpath.basename(self.path)


@dataclass
class ShareMeta:
    """分享页解析结果。"""

    shareid: str
    uk: str
    entries: list[ShareEntry] = field(default_factory=list)
    total: int = 0
    bdstoken: str = ""
    sign: str = ""
    timestamp: int = 0


# ────────────────────────────── 网盘客户端 ──────────────────────────────


class BaiduPan:
    """封装分享校验、目录遍历、转存、取直链、下载等操作。"""

    def __init__(
        self, cookie: str, timeout: int = 30, retries: int = 3, user_agent: str = ""
    ) -> None:
        self.cookie = normalize_cookie(cookie)
        if not self.cookie:
            raise PanError("缺少 Cookie，请用 --cookie / --cookie-file 或 BAIDU_COOKIE 提供")
        self.timeout = timeout
        self.retries = retries
        self.session = requests.Session()
        self.session.headers.update(WEB_HEADERS)
        if user_agent:
            # 百度的登录态可能与 UA 绑定，允许用「复制 Cookie 的那个浏览器」的 UA
            self.session.headers["User-Agent"] = user_agent
        self.session.headers["Cookie"] = self.cookie
        self.bdstoken = ""
        self.username = ""
        self.uk = ""
        self.web_api_ok = True
        self.share_referer = f"{BASE_URL}/disk/home"
        self.debug = False
        self._sign = ""
        self._sign_ts = 0.0

    # ── 请求底层 ──────────────────────────────────────────────

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        last_exc: Exception | None = None
        attempt = 0
        while attempt < self.retries:
            attempt += 1
            try:
                resp = self.session.request(method, url, **kwargs)
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    time.sleep(1.5 * attempt)
                    continue
                return resp
            except requests.exceptions.ContentDecodingError as exc:
                # 「声明 gzip 但内容不是 gzip」：改成不压缩后重试
                last_exc = exc
                headers = dict(kwargs.get("headers") or {})
                headers["Accept-Encoding"] = "identity"
                kwargs["headers"] = headers
                time.sleep(0.5)
            except requests.RequestException as exc:  # 网络抖动重试
                last_exc = exc
                if attempt < self.retries:
                    time.sleep(1.5 * attempt)
        raise PanError(f"请求失败：{method} {url}（{last_exc}）")

    def _json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        resp = self._request(method, url, **kwargs)
        text = resp.text.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                pass
        # 兼容 jsonp：xxx({"errno":0})
        brace = text.find("{")
        if brace >= 0:
            blob = _extract_json_object(text, brace)
            if blob:
                try:
                    return json.loads(blob)
                except json.JSONDecodeError:
                    pass
        raise PanError(f"返回内容无法解析为 JSON：{text[:200]}")

    def _set_bdclnd(self, randsk: str) -> None:
        """把 randsk 写进 Cookie 的 BDCLND 字段（后续分享接口都靠它鉴权）。"""
        cookie = re.sub(r"(?:^|;\s*)BDCLND=[^;]*", "", self.cookie).strip("; ")
        self.cookie = f"{cookie}; BDCLND={randsk}" if cookie else f"BDCLND={randsk}"
        self.session.headers["Cookie"] = self.cookie

    # ── 登录态 / 签名 ─────────────────────────────────────────

    def _template_vars(self, fields: str) -> tuple[dict[str, Any], int]:
        last_errno = -1
        for app_id, extra in (
            ("38824127", {"web": "1", "clienttype": "0"}),
            (APP_ID, {"channel": "chunlei", "web": "1", "clienttype": "0"}),
        ):
            params = {"app_id": app_id, "fields": fields, **extra}
            data = self._json(
                "GET", f"{BASE_URL}/api/gettemplatevariable", params=params
            )
            errno = int(data.get("errno") or 0)
            if self.debug:
                log(f"    [debug] gettemplatevariable app_id={app_id} errno={errno}")
            if errno == 0 and isinstance(data.get("result"), dict):
                return data["result"], 0
            last_errno = errno
        return {}, last_errno

    def login_hint(self, errno: int) -> str:
        """errno=-6 等登录失败的排查提示（打印解析到的 Cookie 字段名）。"""
        fields = cookie_field_names(self.cookie)
        tips = []
        if "BDUSS" not in fields:
            tips.append(
                "解析后没有 BDUSS 字段（必需）：确认复制的是完整 Cookie；"
                "若只复制了 BDUSS_BFESS，本脚本已自动回填"
            )
        if not fields:
            tips.append("没有从输入里解析出任何 Cookie 字段，检查 .env 的 BAIDU_COOKIE 是否写完整")
        tips.append("BDUSS 可能已过期：重新登录 pan.baidu.com 后再复制")
        tips.append(
            "粘贴时不要带 'Cookie:' 前缀；多行 Cookie 建议去掉换行或加引号包成一行"
        )
        tips.append(
            "登录态可能与浏览器绑定：用同一个浏览器的 UA 重试（--user-agent / BAIDU_UA）"
        )
        head = f"Cookie 校验失败（errno={errno}）"
        return head + "\n  - " + "\n  - ".join(tips) + \
            "\n  已解析到的字段：" + (", ".join(fields) if fields else "（无）")

    def login_status(self) -> dict[str, Any]:
        """用 /api/loginStatus 校验登录态。

        这个接口只需要 BDUSS（不需要 STOKEN），所以它能单独验证「账号是否登录」；
        它还会直接返回 bdstoken / uk / username。
        """
        data = self._json(
            "GET",
            f"{BASE_URL}/api/loginStatus",
            params={"clienttype": "0", "app_id": APP_ID, "web": "1"},
        )
        if int(data.get("errno") or 0) != 0:
            return {}
        info = data.get("login_info")
        return info if isinstance(info, dict) else {}

    def check_login(self) -> str:
        """校验 Cookie 并取得 bdstoken。

        先走 /api/loginStatus（只需 BDUSS，最可靠）；拿不到再退回 gettemplatevariable
        （需要完整的网盘会话）。失败时给出可操作的排查提示。
        """
        info = self.login_status()
        if info:
            self.username = str(info.get("username") or "")
            self.uk = str(info.get("uk") or "")
            self.bdstoken = str(info.get("bdstoken") or "")
        if not self.bdstoken:
            result, errno = self._template_vars('["bdstoken","uk","servertime"]')
            self.bdstoken = str(result.get("bdstoken") or "")
            if not self.bdstoken:
                raise PanError(self.login_hint(errno))
        self.web_api_ok = self.check_web_api()
        return self.bdstoken

    def check_web_api(self) -> bool:
        """探测 /api/* 这组网盘接口是否可用（它们需要网盘会话字段，尤其是 STOKEN）。"""
        data = self._json(
            "GET",
            f"{BASE_URL}/api/list",
            params={
                "dir": "/",
                "page": "1",
                "num": "1",
                "web": "1",
                "channel": "chunlei",
                "app_id": APP_ID,
                "clienttype": "0",
                "bdstoken": self.bdstoken,
            },
        )
        return int(data.get("errno") or 0) == 0

    def session_hint(self) -> str:
        """网盘接口返回 errno=-6 时的根因说明（Cookie 缺网盘会话字段）。"""
        names = cookie_field_names(self.cookie)
        missing = [n for n in ("STOKEN", "PANWEB") if n not in names]
        who = f"（账号：{self.username}）" if self.username else ""
        return (
            f"Cookie 本身有效{who}，但网盘接口 /api/* 返回 errno=-6。\n"
            f"  原因：这份 Cookie 缺少网盘会话字段：{'、'.join(missing) or '（无）'}。\n"
            "  BDUSS 只能证明「百度账号已登录」；网盘接口还需要 STOKEN —— 它只存在于登录\n"
            "  pan.baidu.com 后的会话里（只从 www.baidu.com 或未登录的分享页复制是拿不到的）。\n"
            "  正确做法：\n"
            "    1. 浏览器打开 https://pan.baidu.com/disk/main，确认能看到文件列表\n"
            "       （若跳到登录页/提示重新登录，先完成登录再继续）\n"
            "    2. F12 → Network → 刷新 → 点任意一个 pan.baidu.com 的请求（例如 api/list）\n"
            "    3. 右键 → Copy → Copy request headers → 复制其中的 Cookie 整串，\n"
            "       应包含 STOKEN=... 和 PANWEB=1（可能还有 PSTM/PANPSC）——只复制 BDUSS 不够\n"
            "    4. 粘到 .env 的 BAIDU_COOKIE（建议用引号包成一行）\n"
            f"  当前 Cookie 字段：{', '.join(names) if names else '（无）'}"
        )

    def web_sign(self) -> tuple[str, int]:
        """取 /api/download 所需的 sign/timestamp（解析自网盘页面模板变量）。"""
        if self._sign and self._sign_ts + 3600 > time.time():
            return self._sign, int(self._sign_ts)

        result, errno = self._template_vars('["sign1","sign2","sign3","timestamp"]')
        if errno != 0:
            raise PanError(
                f"获取下载签名失败（errno={errno}），Cookie 可能已失效：\n  "
                + self.login_hint(errno)
            )
        chars = _sign2(result["sign3"], result["sign1"])
        self._sign = _sign_base64("".join(chars))
        self._sign_ts = float(result["timestamp"])
        return self._sign, int(self._sign_ts)

    # ── 分享相关 ─────────────────────────────────────────────

    def verify_share(self, surl: str, pwd: str) -> str:
        """提交提取码，返回 randsk（等价于页面 Cookie 里的 BDCLND）。"""
        data = self._json(
            "POST",
            f"{BASE_URL}/share/verify",
            params={
                "surl": surl,
                "bdstoken": self.bdstoken,
                "t": str(int(time.time() * 1000)),
                "channel": "chunlei",
                "web": "1",
                "app_id": APP_ID,
                "clienttype": "0",
            },
            data={"pwd": pwd.strip(), "vcode": "", "vcode_str": ""},
            headers={"Referer": f"{BASE_URL}/disk/home", "User-Agent": "netdisk"},
        )
        errno = data.get("errno")
        if errno != 0:
            msg = VERIFY_ERRNO_MSG.get(errno, "提取码校验失败")
            raise PanError(f"{msg}（errno={errno}）")
        randsk = data.get("randsk") or ""
        if not randsk:
            raise PanError("提取码校验通过但未返回 randsk")
        self._set_bdclnd(randsk)
        return randsk

    def parse_share_page(self, surl: str) -> ShareMeta:
        """访问分享页面并解析 shareid / share_uk / 根目录文件列表。"""
        url = share_page_url(surl)
        resp = self._request("GET", url, headers={"Referer": f"{BASE_URL}/disk/home"})
        # 无提取码的分享：服务端直接下发 BDCLND，需要手动接管（我们不走 CookieJar）
        bdclnd = resp.cookies.get("BDCLND")
        if bdclnd:
            self._set_bdclnd(bdclnd)
        html = resp.text
        self.share_referer = share_page_url(surl)
        data = extract_page_json(html)

        meta = ShareMeta(shareid="", uk="")
        if data:
            meta.bdstoken = str(data.get("bdstoken") or "")
            meta.sign = str(data.get("sign") or "")
            try:
                meta.timestamp = int(data.get("timestamp") or 0)
            except (TypeError, ValueError):
                meta.timestamp = 0
            file_list = data.get("file_list")
            raw_items: list[Any] = []
            total: Any = None
            if isinstance(file_list, dict):
                raw_items = file_list.get("list") or []
                total = file_list.get("total")
            elif isinstance(file_list, list):  # 已验证的分享页把 file_list 直接给成数组
                raw_items = file_list
            for item in raw_items:
                if isinstance(item, dict):
                    meta.entries.append(_entry_from_dict(item))
            try:
                meta.total = int(total or len(meta.entries))
            except (TypeError, ValueError):
                meta.total = len(meta.entries)
            meta.shareid = _first_nonzero(
                data.get("shareid"), data.get("share_id")
            )
            meta.uk = _first_nonzero(data.get("share_uk"), data.get("uk"))

        # 页面结构变化时用正则兜底（key 可能带引号也可能不带）
        if not meta.shareid:
            m = re.search(r'shareid"?\s*[:=]\s*"?(\d+)"?', html)
            if m:
                meta.shareid = m.group(1)
        if not meta.uk:
            m = re.search(r'share_uk"?\s*[:=]\s*"?(\d+)"?', html)
            if m:
                meta.uk = m.group(1)
        if not meta.entries:
            meta.entries = extract_entries_fallback(html)
            meta.total = len(meta.entries)

        if not meta.shareid or not meta.uk:
            raise PanError(
                "分享页解析失败：可能是提取码未通过、链接失效，或百度调整了页面结构。"
                "请先用浏览器打开该链接确认能正常看到文件。"
            )

        # 新版分享页是不含文件列表的 SPA，列表统一走 /share/list（这个接口不需要 STOKEN）
        if not meta.entries:
            try:
                entries, total = self.list_share_dir(meta.shareid, meta.uk, None)
            except PanError as exc:
                raise PanError(f"无法获取分享文件列表：{exc}") from exc
            meta.entries = entries
            meta.total = total or len(entries)
        if not meta.entries:
            raise PanError("分享里没有文件，链接可能已失效或需要重新获取提取码")
        return meta

    def list_share_dir(
        self,
        shareid: str,
        uk: str,
        dir_path: str | None = None,
        page: int = 1,
        num: int = 100,
    ) -> tuple[list[ShareEntry], int]:
        """列分享目录，返回 (条目, 总数)。``dir_path`` 为空表示分享根目录。"""
        params = {
            "app_id": APP_ID,
            "channel": "chunlei",
            "clienttype": "0",
            "desc": "0",
            "num": str(num),
            "order": "name",
            "page": str(page),
            "shareid": shareid,
            "showempty": "0",
            "uk": uk,
            "web": "1",
        }
        if dir_path:
            params["dir"] = dir_path
            params["root"] = "0"
        else:
            params["root"] = "1"
        data = self._json(
            "GET",
            f"{BASE_URL}/share/list",
            params=params,
            headers={"Referer": self.share_referer},
        )
        if data.get("errno") != 0:
            raise PanError(f"列举分享目录失败（errno={data.get('errno')}）")
        entries = [
            ShareEntry(
                fs_id=str(it.get("fs_id")),
                name=str(it.get("server_filename") or ""),
                isdir=bool(int(it.get("isdir") or 0)),
                size=int(it.get("size") or 0),
                path=str(it.get("path") or ""),
            )
            for it in (data.get("list") or [])
        ]
        return entries, int(data.get("total") or len(entries))

    def walk_share(self, meta: ShareMeta) -> list[ShareEntry]:
        """递归列出分享中的所有文件（带分享内绝对路径）。

        根目录用首页解析结果，子目录走 ``/share/list``；每层按 total 自动翻页。
        """
        files: list[ShareEntry] = []
        # 根目录优先走 /share/list（路径规范、能翻页），失败再用页面解析结果
        try:
            root_entries, root_total = self.list_share_dir(meta.shareid, meta.uk, None)
        except PanError as exc:
            log(f"  ! 列举分享根目录失败，改用页面解析结果：{exc}")
            root_entries = list(meta.entries)
            root_total = meta.total or len(root_entries)
        queue: list[tuple[str | None, list[ShareEntry], int]] = [
            (None, root_entries, root_total or len(root_entries))
        ]
        while queue:
            dir_path, first_page, total = queue.pop(0)
            entries = list(first_page)
            page = 2
            while len(entries) < total:
                try:
                    more, _ = self.list_share_dir(
                        meta.shareid, meta.uk, dir_path, page=page
                    )
                except PanError as exc:
                    log(f"  ! 翻页失败（{dir_path or '根目录'}）：{exc}")
                    break
                if not more:
                    break
                entries.extend(more)
                page += 1
            for entry in entries:
                if entry.isdir:
                    sub_dir = entry.path or posixpath.join(dir_path or "/", entry.name)
                    try:
                        child, child_total = self.list_share_dir(
                            meta.shareid, meta.uk, sub_dir
                        )
                    except PanError as exc:
                        log(f"  ! 无法进入目录 {sub_dir}：{exc}")
                        continue
                    queue.append((sub_dir, child, child_total))
                else:
                    files.append(entry)
        return files

    def share_sign(self, surl: str, meta: ShareMeta) -> tuple[str, int]:
        """分享下载所需的 sign/timestamp。

        首选 gettemplatevariable 里的 sign1/sign3（稳定可靠）；
        页面/``share/tplconfig`` 作为兼容傅底。
        """
        if meta.sign and meta.timestamp:
            return meta.sign, meta.timestamp
        try:
            return self.web_sign()
        except PanError:
            pass
        try:
            data = self._json(
                "GET",
                f"{BASE_URL}/share/tplconfig",
                params={
                    "surl": f"1{surl}",
                    "fields": "sign,timestamp",
                    "channel": "chunlei",
                    "web": "1",
                    "app_id": APP_ID,
                    "clienttype": "0",
                },
                headers={"Referer": share_page_url(surl)},
            )
            info = data.get("data") or {}
            sign = str(info.get("sign") or "")
            ts = int(info.get("timestamp") or 0)
            if sign and ts:
                return sign, ts
        except (PanError, TypeError, ValueError):
            pass
        raise PanError(
            "未能获取分享文件的下载签名（sign/timestamp）：\n  " + self.session_hint()
        )

    def share_file_dlink(
        self, entry: ShareEntry, meta: ShareMeta, randsk: str, surl: str
    ) -> str:
        """直接取分享中某个文件的 dlink。"""
        sign, timestamp = self.share_sign(surl, meta)
        data = self._json(
            "POST",
            f"{BASE_URL}/api/sharedownload",
            params={
                "app_id": APP_ID,
                "channel": "chunlei",
                "clienttype": "12",
                "sign": sign,
                "timestamp": str(timestamp),
                "web": "1",
            },
            data={
                "encrypt": "0",
                "extra": json.dumps({"sekey": urllib.parse.unquote(randsk)}),
                "fid_list": f"[{entry.fs_id}]",
                "primaryid": meta.shareid,
                "uk": meta.uk,
                "product": "share",
                "type": "nolimit",
            },
            headers={"Referer": share_page_url(surl)},
        )
        errno = data.get("errno")
        if errno != 0:
            if errno in (9019, -20):  # 9019 = need verify，-20 = 需要验证码
                raise PanError(
                    f"分享直下接口要求风控校验（errno={errno}）：{entry.name}；"
                    "请改用默认的 --mode transfer（转存到自己网盘再下载）"
                )
            raise PanError(
                f"获取分享直链失败：{entry.name}（errno={errno}）"
            )
        try:
            return str(data["list"][0]["dlink"])
        except (KeyError, IndexError, TypeError) as exc:
            raise PanError(f"分享直链响应异常：{entry.name}") from exc

    # ── 自己网盘 ─────────────────────────────────────────────

    def list_own_dir(self, dir_path: str) -> list[dict[str, Any]]:
        """列出自己网盘某个目录（自动翻页）。"""
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self._json(
                "GET",
                f"{BASE_URL}/api/list",
                params={
                    "order": "name",
                    "desc": "0",
                    "showempty": "0",
                    "web": "1",
                    "page": str(page),
                    "num": "1000",
                    "dir": dir_path,
                    "bdstoken": self.bdstoken,
                    "channel": "chunlei",
                    "app_id": APP_ID,
                    "clienttype": "0",
                },
            )
            if data.get("errno") != 0:
                raise PanError(
                    f"列举网盘目录失败：{dir_path}（errno={data.get('errno')}）"
                )
            items = data.get("list") or []
            out.extend(items)
            if len(items) < 1000:
                return out
            page += 1

    def _dir_exists(self, dir_path: str) -> bool:
        """判断网盘目录是否存在（区分「不存在」与其它错误）。"""
        data = self._json(
            "GET",
            f"{BASE_URL}/api/list",
            params={
                "order": "name",
                "desc": "0",
                "showempty": "0",
                "web": "1",
                "page": "1",
                "num": "1",
                "dir": dir_path,
                "bdstoken": self.bdstoken,
                "channel": "chunlei",
                "app_id": APP_ID,
                "clienttype": "0",
            },
        )
        errno = data.get("errno")
        if errno == 0:
            return True
        if errno in (-8, -9):
            return False
        if errno == -6:
            raise PanError("Cookie 登录态失效（errno=-6），请重新获取")
        raise PanError(f"列举网盘目录失败：{dir_path}（errno={errno}）")

    def ensure_dir(self, dir_path: str) -> None:
        """确保网盘目录存在，不存在则逐级创建。"""
        dir_path = "/" + dir_path.strip("/")
        if dir_path == "/":
            return
        if self._dir_exists(dir_path):
            return
        self.ensure_dir(posixpath.dirname(dir_path))
        data = self._json(
            "POST",
            f"{BASE_URL}/api/create",
            params={"a": "commit", "bdstoken": self.bdstoken},
            data={"path": dir_path, "isdir": "1", "block_list": "[]"},
        )
        if data.get("errno") not in (0, -8):  # -8: 已存在
            raise PanError(f"创建网盘目录失败：{dir_path}（errno={data.get('errno')}）")

    def transfer(self, meta: ShareMeta, fs_ids: Sequence[str], to_path: str) -> None:
        """把分享的根条目转存到自己的网盘目录。"""
        data = self._json(
            "POST",
            f"{BASE_URL}/share/transfer",
            params={
                "shareid": meta.shareid,
                "from": meta.uk,
                "bdstoken": self.bdstoken,
                "channel": "chunlei",
                "web": "1",
                "app_id": APP_ID,
                "clienttype": "0",
            },
            data={"fsidlist": f"[{','.join(fs_ids)}]", "path": to_path},
        )
        errno = data.get("errno")
        if errno != 0:
            msg = TRANSFER_ERRNO_MSG.get(errno, "转存失败")
            raise PanError(f"{msg}（errno={errno}）")

    def walk_own_dir(self, root_dir: str) -> list[PanFile]:
        """递归列出自己网盘目录下的全部文件。"""
        files: list[PanFile] = []
        stack = [root_dir]
        while stack:
            current = stack.pop()
            for item in self.list_own_dir(current):
                if int(item.get("isdir") or 0) == 1:
                    stack.append(str(item["path"]))
                else:
                    files.append(
                        PanFile(
                            fs_id=str(item.get("fs_id")),
                            path=str(item.get("path")),
                            size=int(item.get("size") or 0),
                            md5=str(item.get("md5") or ""),
                        )
                    )
        return files

    def dlinks(self, fs_ids: Sequence[str]) -> dict[str, str]:
        """批量取自己网盘文件的 dlink，返回 {fs_id: dlink}。"""
        if not fs_ids:
            return {}
        sign, timestamp = self.web_sign()
        fidlist = urllib.parse.quote(f"[{','.join(fs_ids)}]", safe=",")
        url = (
            f"{BASE_URL}/api/download?type=dlink&channel=chunlei&web=1&app_id={APP_ID}"
            f"&clienttype=0&sign={urllib.parse.quote(sign)}&timestamp={timestamp}"
            f"&fidlist={fidlist}"
        )
        data = self._json("GET", url, headers={"Referer": f"{BASE_URL}/disk/home"})
        if data.get("errno") != 0:
            raise PanError(f"获取下载直链失败（errno={data.get('errno')}）")
        out: dict[str, str] = {}
        for idx, item in enumerate(data.get("dlink") or []):
            if isinstance(item, dict):
                fid = str(
                    item.get("fs_id") or (fs_ids[idx] if idx < len(fs_ids) else "")
                )
                link = item.get("dlink")
            else:  # 少数情况下返回的是纯字符串列表
                fid = fs_ids[idx] if idx < len(fs_ids) else ""
                link = item
            if fid and link:
                out[fid] = str(link)
        return out

    def delete_paths(self, paths: Sequence[str]) -> None:
        """删除自己网盘中的若干路径（用于清理转存副本）。"""
        if not paths:
            return
        self._json(
            "POST",
            f"{BASE_URL}/api/filemanager",
            params={
                "opera": "delete",
                "async": "2",
                "onnest": "fail",
                "channel": "chunlei",
                "web": "1",
                "app_id": APP_ID,
                "clienttype": "0",
                "bdstoken": self.bdstoken,
            },
            data={"filelist": json.dumps(list(paths), ensure_ascii=False)},
        )

    # ── 直链解析 & 下载 ──────────────────────────────────────

    def resolve_dlink(self, dlink: str) -> str | None:
        """把 dlink 解析成带签名的真实 CDN 地址。"""
        bduss = cookie_value(self.cookie, "BDUSS")
        try:
            resp = self.session.get(
                dlink,
                headers={"User-Agent": RESOLVE_UA, "Cookie": f"BDUSS={bduss}"},
                allow_redirects=False,
                timeout=self.timeout,
            )
        except requests.RequestException:
            return None
        if resp.status_code in (301, 302, 303, 307, 308):
            return resp.headers.get("Location")
        if resp.status_code == 200:
            return dlink
        return None

    def download(
        self,
        url: str,
        dest: Path,
        expected_size: int | None = None,
        expected_md5: str = "",
        verify_md5: bool = False,
    ) -> None:
        """流式下载（支持断点续传），失败自动换 UA 重试。"""
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and expected_size and dest.stat().st_size == expected_size:
            if not verify_md5 or not expected_md5 or _md5(dest) == expected_md5:
                return

        tmp = dest.with_name(dest.name + ".part")
        bduss = cookie_value(self.cookie, "BDUSS")
        last_err = ""
        for ua in DOWNLOAD_UAS:
            try:
                self._stream(url, tmp, ua, bduss, expected_size)
                os.replace(tmp, dest)
                if expected_size and dest.stat().st_size != expected_size:
                    raise PanError(
                        f"大小不符：期望 {expected_size}，实际 {dest.stat().st_size}"
                    )
                if verify_md5 and expected_md5 and _md5(dest) != expected_md5:
                    raise PanError("md5 校验失败")
                return
            except PanError as exc:
                last_err = str(exc)
                continue
        raise PanError(last_err or "下载失败")

    def _stream(
        self, url: str, tmp: Path, ua: str, bduss: str, expected_size: int | None
    ) -> None:
        """单次下载尝试，写入 tmp（带 Range 续传）。"""
        pos = tmp.stat().st_size if tmp.exists() else 0
        if expected_size and pos > expected_size:
            tmp.unlink(missing_ok=True)
            pos = 0
        headers = {
            "User-Agent": ua,
            "Cookie": f"BDUSS={bduss}",
            "Referer": f"{BASE_URL}/disk/home",
        }
        if pos:
            headers["Range"] = f"bytes={pos}-"
        resp = self.session.get(
            url, headers=headers, stream=True, timeout=(15, 120), allow_redirects=True
        )
        if resp.status_code == 416 and expected_size and pos == expected_size:
            resp.close()
            return
        if resp.status_code not in (200, 206):
            resp.close()
            raise PanError(f"HTTP {resp.status_code}")
        if resp.status_code == 200 and pos:
            pos = 0  # 服务端不支持续传，重头写
        mode = "ab" if (pos and resp.status_code == 206) else "wb"
        with open(tmp, mode) as fh:
            for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                if chunk:
                    fh.write(chunk)
        resp.close()


def _md5(path: Path) -> str:
    """计算文件 md5（大文件分块读取）。"""
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# ── 百度页面里的 sign 算法（sign2 + 自定义 base64），照搬前端逻辑 ──


def _sign2(j: str, r: str) -> list[str]:
    a = [0] * 256
    p = list(range(256))
    v = len(j)
    for q in range(256):
        a[q] = ord(j[q % v])
        p[q] = q
    u = 0
    for q in range(256):
        u = (u + p[q] + a[q]) % 256
        p[q], p[u] = p[u], p[q]
    i = u = 0
    out: list[str] = []
    for ch in r:
        i = (i + 1) % 256
        u = (u + p[i]) % 256
        p[i], p[u] = p[u], p[i]
        k = p[(p[i] + p[u]) % 256]
        out.append(chr(ord(ch) ^ k))
    return out


def _sign_base64(t: str) -> str:
    table = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    a = len(t)
    r = 0
    e = ""
    while a > r:
        o = ord(t[r]) & 255
        r += 1
        if r == a:
            e += table[o >> 2] + table[(3 & o) << 4] + "=="
            break
        n = ord(t[r])
        r += 1
        if r == a:
            e += table[o >> 2] + table[((3 & o) << 4) | ((240 & n) >> 4)] + table[(15 & n) << 2] + "="
            break
        i = ord(t[r])
        r += 1
        e += (
            table[o >> 2]
            + table[((3 & o) << 4) | ((240 & n) >> 4)]
            + table[((15 & n) << 2) | ((192 & i) >> 6)]
            + table[63 & i]
        )
    return e


# ────────────────────────────── 任务编排 ──────────────────────────────


@dataclass
class Plan:
    """待下载任务：直链 + 本地落盘路径。"""

    name: str
    rel_path: str
    url: str
    size: int
    md5: str = ""
    fs_id: str = ""


@dataclass
class PendingFile:
    """本地已存在的研报文件，等待后处理（入库/分类/上传/发信）。"""

    fs_id: str
    title: str
    filepath: Path


def _db_path_value(directory: Path) -> str:
    """DB 的 path 字段存「目录」：项目目录下用相对路径，否则用绝对路径。"""
    resolved = directory.resolve()
    try:
        return str(resolved.relative_to(PROJECT_DIR))
    except ValueError:
        return str(resolved)


class PostProcessor:
    """下载完成后的串行流水线：入库 → 分类 → SharePoint → 发邮件。

    设计要点：

    * 在独立线程里串行消费队列，下载线程不阻塞（下载和后处理并行）；
    * 每一步单独 try/except，**任何一步失败都不影响其它研报、也不影响下载**；
    * 已在库且已发过邮件的同名研报直接跳过，避免重复发信；
    * 依赖（pymupdf / o365 / DeepSeek 等）在构造时按需导入，
      纯下载场景（``--no-post-process``）不依赖这些包。
    """

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.queue: "queue.Queue[PendingFile | None]" = queue.Queue()
        self.thread: threading.Thread | None = None
        self.available = False
        self.cr: Any = None
        self.classifier: Any = None
        self.stats: dict[str, int] = {
            "db": 0,
            "classify": 0,
            "sharepoint": 0,
            "mail": 0,
            "skipped": 0,
            "failed": 0,
        }
        try:
            import check_reports as cr  # noqa: PLC0415 - 按需导入，避免纯下载依赖
            import classifier as cl  # noqa: PLC0415

            cr.init_db()
            self.cr, self.classifier = cr, cl
            self.available = True
        except Exception as exc:  # noqa: BLE001 - 缺依赖时降级为「只下载」
            log(f"  ! 后处理模块不可用（分类/入库/上传/发信将跳过）：{exc}")

    # ── 对外接口 ──────────────────────────────────────────────

    def start(self) -> None:
        if not self.available or self.thread is not None:
            return
        self.thread = threading.Thread(target=self._loop, name="bdpan-post", daemon=True)
        self.thread.start()

    def submit(self, item: PendingFile) -> None:
        """投递一份刚下载（或本地已存在）的研报，交给后处理线程。"""
        if self.available:
            self.queue.put(item)

    def close(self) -> None:
        """等待队列清空后返回。"""
        if not self.available or self.thread is None:
            return
        self.queue.put(None)
        self.thread.join()
        self.thread = None

    def summary(self) -> str:
        s = self.stats
        return (
            f"入库 {s['db']}，分类 {s['classify']}，上传 SharePoint {s['sharepoint']}，"
            f"发邮件 {s['mail']}，跳过 {s['skipped']}，失败 {s['failed']}"
        )

    # ── 内部实现 ──────────────────────────────────────────────

    def _loop(self) -> None:
        while True:
            item = self.queue.get()
            if item is None:
                self.queue.task_done()
                return
            try:
                self._process(item)
            except Exception as exc:  # noqa: BLE001 - 单份失败不中断队列
                self.stats["failed"] += 1
                log(f"  ! 后处理异常（{item.title}）：{exc}")
            finally:
                self.queue.task_done()

    def _process(self, item: PendingFile) -> None:
        title, filepath = item.title, item.filepath
        if not filepath.exists():
            self.stats["failed"] += 1
            log(f"  ! 后处理跳过（文件不存在）：{filepath}")
            return

        media_id, already_sent = self._ensure_record(item)
        if already_sent:
            self.stats["skipped"] += 1
            log(f"  · 已入库且已发过邮件，跳过后处理：{title}")
            return

        # 1) 入库：标记已下载 + 落 path
        self._record_downloaded(media_id, title, filepath)

        # 2) 分类（LLM）：写元数据并把文件移到分类目录
        filepath = self._classify(media_id, title, filepath)

        # 3) 上传 SharePoint（失败不影响后续）
        if self.args.sharepoint:
            self._upload(media_id, title, filepath)

        # 4) 发邮件（失败不影响后续研报）
        if self.args.mail:
            self._send_mail(media_id, title, filepath)

    def _fail(self, message: str) -> None:
        """记录一次失败（不抛出，保证后续研报继续处理）。"""
        self.stats["failed"] += 1
        log(f"  ! {message}")

    def _record_downloaded(self, media_id: str, title: str, filepath: Path) -> None:
        """标记已下载并记录文件所在目录（失败不阻断后续步骤）。"""
        try:
            cr = self.cr
            cr.mark_downloaded(media_id)
            path_value = _db_path_value(filepath.parent)
            with sqlite3.connect(str(cr.DB_PATH)) as conn:
                conn.execute(
                    "UPDATE reports SET path = ? WHERE media_id = ?",
                    (path_value, media_id),
                )
                conn.commit()
            self.stats["db"] += 1
            log(f"  [db] ✓ {title} → {path_value}")
        except Exception as exc:  # noqa: BLE001
            self._fail(f"入库失败（{title}）：{exc}")

    def _ensure_record(self, item: PendingFile) -> tuple[str, bool]:
        """确保 DB 中有该研报的记录，返回 (media_id, 是否已发过邮件)。

        按标题去重：同名研报（无论来自 IMA 知识库还是网盘）复用同一条记录，
        这样已经发过邮件的研报不会被重复发送。
        """
        cr = self.cr
        with sqlite3.connect(str(cr.DB_PATH)) as conn:
            row = conn.execute(
                "SELECT media_id, sendmail_ts FROM reports WHERE title = ? "
                "ORDER BY created_ts DESC LIMIT 1",
                (item.title,),
            ).fetchone()
        if row:
            return str(row[0]), int(row[1] or 0) > 0
        media_id = f"bdpan_{item.fs_id or int(time.time() * 1000)}"
        try:
            cr.insert_report(media_id, item.title)
        except Exception as exc:  # noqa: BLE001
            log(f"  [db] 新增记录失败（{item.title}）：{exc}")
        return media_id, False

    def _classify(self, media_id: str, title: str, filepath: Path) -> Path:
        """调用 LLM 分类；成功时返回移动后的新路径，失败/未开启时返回原路径。"""
        if not self.args.classify:
            return filepath
        cr, cl = self.cr, self.classifier
        try:
            ok, new_rel = cl.classify_one_report(
                cr.DB_PATH, cr.CATEGORIZED_ROOT, media_id, title, filepath.parent
            )
        except Exception as exc:  # noqa: BLE001
            self._fail(f"分类异常（{title}）：{exc}")
            return filepath
        if ok and new_rel:
            self.stats["classify"] += 1
            return cr._resolve_path(new_rel) / title
        self._fail(f"分类失败（{title}），文件保持原位置")
        return filepath

    def _upload(self, media_id: str, title: str, filepath: Path) -> None:
        """上传到 SharePoint（幂等：已上传过会直接返回成功）。"""
        try:
            if self.cr.upload_report_to_sharepoint(media_id, title, filepath):
                self.stats["sharepoint"] += 1
            else:
                self._fail(f"上传 SharePoint 失败（{title}）")
        except Exception as exc:  # noqa: BLE001
            self._fail(f"上传 SharePoint 异常（{title}）：{exc}")

    def _send_mail(self, media_id: str, title: str, filepath: Path) -> None:
        """发送带附件的邮件，成功则标记 sendmail_ts。"""
        try:
            cr = self.cr
            result = cr.send_email(title, filepath)
        except Exception as exc:  # noqa: BLE001
            self._fail(f"发邮件异常（{title}）：{exc}")
            return
        if result == cr.SEND_OK:
            try:
                cr.mark_sent(media_id)
            except Exception as exc:  # noqa: BLE001
                self._fail(f"标记已发送失败（{title}）：{exc}")
                return
            self.stats["mail"] += 1
        elif result == cr.SEND_CLIENT_ERROR:
            self._fail(f"发邮件被拒（400，{title}）")
        else:
            self._fail(f"发邮件失败（{title}）")


def load_cookie(args: argparse.Namespace) -> str:
    """按优先级取 Cookie：--cookie > --cookie-file > BAIDU_COOKIE > BAIDU_BDUSS。"""
    if args.cookie:
        return args.cookie
    if args.cookie_file:
        path = Path(args.cookie_file).expanduser()
        if not path.exists():
            raise PanError(f"Cookie 文件不存在：{path}")
        return path.read_text(encoding="utf-8").strip()
    for env_name in ("BAIDU_COOKIE", "BAIDU_COOKIES", "BAIDU_BDUSS"):
        value = os.environ.get(env_name)
        if value:
            return value
    raise PanError(
        "缺少 Cookie。请用 --cookie 传入（或 --cookie-file / 环境变量 BAIDU_COOKIE）。"
    )


def load_user_agent(args: argparse.Namespace) -> str:
    """取可选的 User-Agent 覆盖：--user-agent > BAIDU_UA > BAIDU_USER_AGENT。"""
    return (
        args.user_agent
        or os.environ.get("BAIDU_UA")
        or os.environ.get("BAIDU_USER_AGENT")
        or ""
    ).strip()


def _safe_rel_path(rel: str) -> str:
    """清理路径中的非法/危险字符，避免落盘失败或目录穿越。"""
    parts = []
    for part in rel.replace("\\", "/").split("/"):
        part = part.strip()
        if part in ("", ".", ".."):
            continue
        part = re.sub(r'[<>:"|?*\x00-\x1f]', "_", part).rstrip(". ")
        if part:
            parts.append(part)
    return "/".join(parts) or "unnamed"


def _size_matches(path: Path, size: int) -> bool:
    """本地文件存在且大小符合预期（size<=0 时只要求非空）。"""
    if not path.exists():
        return False
    actual = path.stat().st_size
    if size <= 0:
        return actual > 0
    return actual == size


_known_paths_cache: dict[str, str] = {}
_known_paths_loaded = False


def _load_known_locations() -> dict[str, str]:
    """从 reports.db 读出「标题 → 本地已存在文件路径」的映射。

    分类会把下载好的文件移入 ``categorized_reports/...``，因此重跑时不能只看
    ``--out`` 目录，否则已处理过的研报会被重新下载一遍。
    """
    db_path = PROJECT_DIR / "reports.db"
    if not db_path.exists():
        return {}
    try:
        with sqlite3.connect(str(db_path)) as conn:
            rows = conn.execute("SELECT title, path FROM reports").fetchall()
    except sqlite3.Error:
        return {}
    known: dict[str, str] = {}
    for title, path_value in rows:
        if not title or not path_value:
            continue
        directory = Path(str(path_value))
        if not directory.is_absolute():
            directory = PROJECT_DIR / directory
        candidate = directory / str(title)
        if candidate.exists():
            known[str(title)] = str(candidate)
    return known


def _known_locations(force: bool = False) -> dict[str, str]:
    """带缓存的 :func:`_load_known_locations`（每轮运行开始时 force 刷新）。"""
    global _known_paths_cache, _known_paths_loaded
    if force or not _known_paths_loaded:
        _known_paths_cache = _load_known_locations()
        _known_paths_loaded = True
    return _known_paths_cache


def _existing_local(out_dir: Path, rel: str, size: int) -> Path | None:
    """本地是否已有完整副本：优先 ``--out``，其次 DB 记录指向的位置（分类后的目录）。"""
    dest = out_dir / rel
    if _size_matches(dest, size):
        return dest
    known = _known_locations().get(Path(rel).name)
    if known and _size_matches(Path(known), size):
        return Path(known)
    return None


def build_plan_transfer(
    pan: BaiduPan,
    meta: ShareMeta,
    args: argparse.Namespace,
    out_dir: Path,
) -> tuple[list[Plan], str, list[PendingFile]]:
    """转存模式：转存 → 递归列举 → 生成下载计划。"""
    fs_ids = [e.fs_id for e in meta.entries]
    to_path = args.pan_dir or f"{DEFAULT_PAN_ROOT}/{int(time.time())}"
    log(f"→ 转存 {len(fs_ids)} 个条目到网盘目录 {to_path}")
    pan.ensure_dir(to_path)
    pan.transfer(meta, fs_ids, to_path)
    log("  转存成功，正在列举网盘目录…")

    files = pan.walk_own_dir(to_path)
    if args.limit:
        files = files[: args.limit]
    log(f"  网盘目录下共 {len(files)} 个文件")

    plans: list[Plan] = []
    skipped: list[PendingFile] = []
    batch = 50
    for start in range(0, len(files), batch):
        chunk = files[start : start + batch]
        pending: list[tuple[PanFile, str]] = []
        for item in chunk:
            rel = _safe_rel_path(item.path[len(to_path) :].lstrip("/"))
            existing = _existing_local(out_dir, rel, item.size)
            if existing is not None:
                log(f"  = 已存在，跳过下载：{rel}")
                skipped.append(
                    PendingFile(fs_id=item.fs_id, title=Path(rel).name, filepath=existing)
                )
                continue
            pending.append((item, rel))
        if not pending:
            continue
        link_map = pan.dlinks([item.fs_id for item, _ in pending])
        for item, rel in pending:
            dlink = link_map.get(item.fs_id)
            if not dlink:
                log(f"  ! 跳过（无直链）：{item.path}")
                continue
            url = pan.resolve_dlink(dlink) or dlink
            plans.append(
                Plan(
                    name=item.name,
                    rel_path=rel,
                    url=url,
                    size=item.size,
                    md5=item.md5,
                    fs_id=item.fs_id,
                )
            )
    return plans, to_path, skipped


def build_plan_share(
    pan: BaiduPan,
    meta: ShareMeta,
    surl: str,
    randsk: str,
    args: argparse.Namespace,
    out_dir: Path,
) -> tuple[list[Plan], str, list[PendingFile]]:
    """分享直下模式：递归列举分享 → 逐个取直链 → 生成下载计划。"""
    log("→ 递归列举分享目录…")
    entries = pan.walk_share(meta)
    if args.limit:
        entries = entries[: args.limit]
    log(f"  分享内共 {len(entries)} 个文件")

    plans: list[Plan] = []
    skipped: list[PendingFile] = []
    risk_blocked = False
    for idx, entry in enumerate(entries, 1):
        # 保留分享内原始目录结构（单层根目录不额外剥离，与 transfer 模式一致）
        rel = _safe_rel_path((entry.path or entry.name).lstrip("/"))
        existing = _existing_local(out_dir, rel, entry.size)
        if existing is not None:
            log(f"  = 已存在，跳过下载：{rel}")
            skipped.append(
                PendingFile(fs_id=entry.fs_id, title=Path(rel).name, filepath=existing)
            )
            continue
        try:
            dlink = pan.share_file_dlink(entry, meta, randsk, surl)
        except PanError as exc:
            log(f"  ! {exc}")
            if "9019" in str(exc) or "风控" in str(exc):
                risk_blocked = True
            continue
        url = pan.resolve_dlink(dlink) or dlink
        plans.append(
            Plan(name=entry.name, rel_path=rel, url=url, size=entry.size, fs_id=entry.fs_id)
        )
        if idx % 20 == 0:
            log(f"  已解析 {idx}/{len(entries)} 个直链")
        time.sleep(0.2)  # 轻量限速，避免风控
    if risk_blocked and not plans:
        log("  ! 百度对分享直下接口做了风控校验，请改用默认的 --mode transfer 模式（转存后下载）")
    return plans, "", skipped


def run_downloads(
    plans: Sequence[Plan],
    out_dir: Path,
    args: argparse.Namespace,
    post: "PostProcessor | None" = None,
) -> int:
    """并发下载，返回失败条数。

    每份研报下载（或已存在）后会立即投递给 ``post`` 做入库/分类/上传/发信，
    单份后处理失败不影响其它研报，也不影响下载线程。
    """
    if not plans:
        log("没有需要下载的文件")
        return 0
    total = len(plans)
    total_bytes = sum(p.size for p in plans)
    log(f"→ 开始下载 {total} 个文件，共 {human_size(total_bytes)} → {out_dir}")
    done = 0
    failed: list[str] = []
    counter_lock = threading.Lock()

    def worker(plan: Plan) -> tuple[Plan, str]:
        dest = out_dir / plan.rel_path
        # 每个线程独立的客户端，避免共享 Session 的并发问题
        client = BaiduPan(
            args.cookie_effective,
            timeout=args.timeout,
            user_agent=getattr(args, "user_agent_effective", ""),
        )
        started = time.time()
        try:
            client.download(
                plan.url,
                dest,
                expected_size=plan.size or None,
                expected_md5=plan.md5,
                verify_md5=args.verify_md5,
            )
        except Exception as exc:  # noqa: BLE001 - 单文件失败不影响整体
            return plan, f"失败：{exc}"
        if post is not None:
            post.submit(
                PendingFile(fs_id=plan.fs_id, title=Path(plan.rel_path).name, filepath=dest)
            )
        spend = max(time.time() - started, 0.001)
        speed = human_size((plan.size or dest.stat().st_size) / spend) + "/s"
        return plan, f"完成（{human_size(dest.stat().st_size)}，{spend:.1f}s，{speed}）"

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = {pool.submit(worker, plan): plan for plan in plans}
        for future in concurrent.futures.as_completed(futures):
            plan, status = future.result()
            with counter_lock:
                done += 1
                idx = done
            if status.startswith("失败"):
                failed.append(plan.rel_path)
            log(f"  [{idx}/{total}] {plan.rel_path} … {status}")

    if failed:
        log(f"→ 完成，失败 {len(failed)} 个：")
        for name in failed:
            log(f"    - {name}")
    else:
        log("→ 全部下载完成")
    return len(failed)


def print_tree(pan: BaiduPan, meta: ShareMeta) -> None:
    """打印分享内的完整文件树（--mode list）。"""
    entries = pan.walk_share(meta)
    total = sum(e.size for e in entries)
    print(f"共 {len(entries)} 个文件，合计 {human_size(total)}：")
    for entry in entries:
        print(f"    [{human_size(entry.size)}] {entry.path or entry.name}")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="从百度网盘分享链接（带提取码）下载整个文件夹",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python baidu_pan_download.py https://pan.baidu.com/s/1lpUp14K-1CXccXny5RlYmg --pwd 0203\n"
            "  python baidu_pan_download.py <链接> --pwd 0203 --mode list\n"
            "  python baidu_pan_download.py <链接> --pwd 0203 --mode share --jobs 4\n"
        ),
    )
    parser.add_argument("url", help="分享链接，如 https://pan.baidu.com/s/1lpUp14K-1CXccXny5RlYmg")
    parser.add_argument("-p", "--pwd", default="", help="提取码，如 0203；不传则交互式输入")
    parser.add_argument("-o", "--out", default="downloaded_reports", help="本地保存目录（默认 downloaded_reports）")
    parser.add_argument(
        "--mode",
        choices=("transfer", "share", "list"),
        default="transfer",
        help="transfer=转存后下载（默认）；share=分享直下（不占网盘空间）；list=仅列出内容",
    )
    parser.add_argument("--cookie", default="", help="浏览器 Cookie 整串，或只给 BDUSS 的值")
    parser.add_argument("--cookie-file", default="", help="存放 Cookie 的文件路径（整个文件即 Cookie）")
    parser.add_argument(
        "--check",
        action="store_true",
        help="只校验 Cookie + 提取码并打印分享根目录（排查登录问题时最好用）",
    )
    parser.add_argument("--debug", action="store_true", help="打印接口调试信息（app_id / errno）")
    parser.add_argument(
        "--user-agent",
        default="",
        help="自定义 User-Agent（登录态与浏览器绑定时用；也可用 BAIDU_UA）",
    )
    parser.add_argument("--pan-dir", default="", help="转存模式的目标网盘目录（默认 /ima_download/<时间戳>）")
    parser.add_argument("--jobs", type=int, default=3, help="并发下载数（默认 3，过高易被限速）")
    parser.add_argument("--limit", type=int, default=0, help="最多下载前 N 个文件（调试用）")
    parser.add_argument("--timeout", type=int, default=30, help="单次请求超时秒数（默认 30）")
    parser.add_argument("--verify-md5", action="store_true", help="下载完成后校验 md5（转存模式）")
    parser.add_argument("--cleanup", action="store_true", help="下载完成后删除网盘里的转存副本")
    parser.add_argument("--dry-run", action="store_true", help="只解析并打印文件树，不做任何转存/下载")
    # ── 下载后处理（每完成一份研报立即依次执行）──
    parser.add_argument(
        "--post-process",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="下载后逐份执行：入库 → LLM 分类 → 上传 SharePoint → 发邮件（默认开启，--no-post-process 关闭）",
    )
    parser.add_argument(
        "--classify",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="用 LLM 对研报分类并移入 categorized_reports（默认开启）",
    )
    parser.add_argument(
        "--sharepoint",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="上传到 SharePoint（需 SHAREPOINT_* / O365_* 配置；默认开启）",
    )
    parser.add_argument(
        "--mail",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="逐份发送带附件的邮件（需 EMAIL_FROM/EMAIL_TO + O365 配置；默认开启）",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        surl = extract_surl(args.url)
    except PanError as exc:
        log(f"[错误] {exc}")
        return 2

    pwd = args.pwd.strip()
    if not pwd and sys.stdin.isatty():
        import getpass

        pwd = getpass.getpass("请输入提取码（无则直接回车）：").strip()

    try:
        cookie = load_cookie(args)
    except PanError as exc:
        log(f"[错误] {exc}")
        return 2
    args.cookie_effective = normalize_cookie(cookie)
    args.user_agent_effective = load_user_agent(args)

    log(f"分享链接：{share_page_url(surl)}")
    log(f"提取码：{pwd or '（无）'}    模式：{args.mode}")
    fields = cookie_field_names(args.cookie_effective)
    log(f"Cookie 字段（共 {len(fields)} 个）：{', '.join(fields) if fields else '（无）'}")
    if fields and "BDUSS" not in fields:
        log("  ! 未解析到 BDUSS，接口会返回 errno=-6；请确认复制的是完整 Cookie")
    if fields and "STOKEN" not in fields and args.mode in ("transfer", "share"):
        log("  ! Cookie 里没有 STOKEN（只复制了 BDUSS？），网盘下载接口会返回 errno=-6")
    if not args.cookie_effective:
        log("[错误] 传入的 Cookie 无法解析，请检查格式")
        return 2

    pan = BaiduPan(
        args.cookie_effective,
        timeout=args.timeout,
        user_agent=args.user_agent_effective,
    )
    pan.debug = args.debug
    try:
        pan.check_login()
        who = f"（{pan.username}）" if pan.username else ""
        log(f"✓ Cookie 已登录{who}，bdstoken 已获取")
        if pan.web_api_ok:
            log("✓ 网盘接口可用（STOKEN 等会话字段完整）")
        else:
            log("  ! 网盘接口 /api/* 不可用（通常因为 Cookie 缺 STOKEN）")
            need_api = args.mode in ("transfer", "share")
            if need_api:
                raise PanError(pan.session_hint())
            log("    仅供列出内容；下载前请按下面提示重新获取 Cookie：")
            for line in pan.session_hint().splitlines()[2:]:
                log("  " + line.strip() if line.strip() else line)

        randsk = ""
        if pwd:
            randsk = pan.verify_share(surl, pwd)
            log("✓ 提取码校验通过")
        meta = pan.parse_share_page(surl)
        log(
            f"✓ 分享解析成功：shareid={meta.shareid}，根条目 {len(meta.entries)} 个"
        )

        if args.mode == "list" or args.dry_run:
            print_tree(pan, meta)
            return 0

        if args.check:
            log("✓ 检查通过（Cookie 与提取码均可用）。分享根目录：")
            for entry in meta.entries:
                kind = "目录" if entry.isdir else human_size(entry.size)
                print(f"    [{kind}] {entry.name}")
            return 0

        out_dir = Path(args.out).expanduser()
        _known_locations(force=True)  # 刷新「已处理过的研报」缓存（分类后位置会变）

        # ── 下载后处理：入库 → 分类 → SharePoint → 邮件（逐份、失败隔离）──
        post: PostProcessor | None = None
        if args.post_process:
            post = PostProcessor(args)
            if post.available:
                post.start()
                log("→ 每完成一份研报将依次执行：入库 → 分类"
                    + (" → 上传 SharePoint" if args.sharepoint else "")
                    + (" → 发邮件" if args.mail else ""))
            else:
                post = None

        if args.mode == "transfer":
            plans, pan_dir, skipped = build_plan_transfer(pan, meta, args, out_dir)
        else:
            plans, pan_dir, skipped = build_plan_share(pan, meta, surl, randsk, args, out_dir)

        if not plans and not skipped:
            log("[错误] 没有可下载的文件")
            return 1
        if not plans:
            if post is not None:
                log(f"→ {len(skipped)} 个文件已存在于本地，只做后处理")
            else:
                log(f"→ {len(skipped)} 个文件已存在于本地，无需下载")

        failed = 0
        if plans:
            failed = run_downloads(plans, out_dir, args, post)

        # 本地已存在（本次未重新下载）的文件也补做后处理，保证重跑可自愈
        if post is not None:
            for pending in skipped:
                post.submit(pending)
            post.close()
            log(f"→ 后处理完成：{post.summary()}")

        if args.mode == "transfer" and pan_dir and args.cleanup:
            log(f"→ 清理网盘目录 {pan_dir}")
            try:
                pan.delete_paths([pan_dir])
            except PanError as exc:
                log(f"  ! 清理失败（不影响本地文件）：{exc}")

        return 1 if failed else 0
    except PanError as exc:
        log(f"[错误] {exc}")
        return 1
    except KeyboardInterrupt:
        log("\n已中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
