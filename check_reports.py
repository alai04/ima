"""IMA 研报自动下载与邮件分发 CLI。

功能：
  1. 从"环球研报直通车"知识库搜索最近 3 天的研报
  2. 保存到 SQLite DB（去重）
  3. 下载新增研报并立即邮件发送（下载失败则终止）
"""

import argparse
import html
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from O365 import Account
from requests.exceptions import HTTPError as RequestsHTTPError

load_dotenv()

# ── 环境变量 ──────────────────────────────────────────────────────────
IMA_API_KEY = os.getenv("IMA_API_KEY", "")
IMA_CLIENT_ID = os.getenv("IMA_CLIENT_ID", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", "")
EMAIL_TO = os.getenv("EMAIL_TO", "")
KEYWORD_IGNORE = os.getenv("KEYWORD_IGNORE", "")
STATUS_EMAIL_TO = os.getenv("STATUS_EMAIL_TO", "")
O365_CLIENT_ID = os.getenv("O365_CLIENT_ID", "")
O365_CLIENT_SECRET = os.getenv("O365_CLIENT_SECRET", "")
O365_TENANT_ID = os.getenv("O365_TENANT_ID", "common")
BASE_URL = "https://ima.qq.com/openapi/wiki/v1"
ROOT_KB_NAME = "环球研报直通车"
ROOT_KB_ID = ""

# ── 路径 ──────────────────────────────────────────────────────────────
PROJECT_DIR = Path(__file__).resolve().parent
DB_PATH = PROJECT_DIR / "reports.db"
DOWNLOAD_DIR = PROJECT_DIR / "downloaded_reports"

IMA_HEADERS = {
    "ima-openapi-clientid": IMA_CLIENT_ID,
    "ima-openapi-apikey": IMA_API_KEY,
    "Content-Type": "application/json",
}


# ═══════════════════════════════════════════════════════════════════════
# Database
# ═══════════════════════════════════════════════════════════════════════


def init_db() -> None:
    """建表并确保 schema 版本最新。"""
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reports (
                media_id   TEXT PRIMARY KEY,
                title      TEXT NOT NULL,
                downloaded_ts INTEGER DEFAULT 0,
                sendmail_ts   INTEGER DEFAULT 0,
                created_ts    INTEGER DEFAULT 0,
                path          TEXT DEFAULT 'downloaded_reports'
            )
            """
        )
        # 兼容旧表：补上新增列
        migrations = [
            ("created_ts", "INTEGER DEFAULT 0"),
            ("path", "TEXT DEFAULT 'downloaded_reports'"),
        ]
        for column, definition in migrations:
            try:
                conn.execute(f"ALTER TABLE reports ADD COLUMN {column} {definition}")
            except sqlite3.OperationalError:
                pass  # 列已存在
        conn.commit()


def insert_report(media_id: str, title: str) -> bool:
    """插入新研报记录，返回 True 表示新增，False 表示已存在。"""
    now = int(time.time())
    with sqlite3.connect(str(DB_PATH)) as conn:
        try:
            conn.execute(
                "INSERT INTO reports (media_id, title, created_ts) VALUES (?, ?, ?)",
                (media_id, title, now),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def _resolve_path(path_value: str) -> Path:
    """将 path 字段值解析为绝对路径。相对路径以 PROJECT_DIR 为基准。"""
    p = Path(path_value)
    if p.is_absolute():
        return p
    return PROJECT_DIR / p


def get_reports_to_download() -> list[dict[str, Any]]:
    """获取尚未下载的研报（仅限 48 小时内入库的），按插入时间从近到远排序。"""
    cutoff = int(time.time()) - 48 * 3600
    with sqlite3.connect(str(DB_PATH)) as conn:
        rows = conn.execute(
            "SELECT media_id, title, path FROM reports "
            "WHERE downloaded_ts = 0 AND created_ts > ? "
            "ORDER BY created_ts DESC",
            (cutoff,),
        ).fetchall()
    return [{"media_id": r[0], "title": r[1], "path": r[2]} for r in rows]


def get_reports_to_send() -> list[dict[str, Any]]:
    """获取已下载但未发送的研报，按插入时间从近到远排序。"""
    with sqlite3.connect(str(DB_PATH)) as conn:
        rows = conn.execute(
            "SELECT media_id, title, path FROM reports WHERE downloaded_ts > 0 AND sendmail_ts = 0 ORDER BY created_ts DESC"
        ).fetchall()
    return [{"media_id": r[0], "title": r[1], "path": r[2]} for r in rows]


def mark_downloaded(media_id: str, ts: int | None = None) -> None:
    ts = ts or int(time.time())
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.execute("UPDATE reports SET downloaded_ts = ? WHERE media_id = ?", (ts, media_id))
        conn.commit()


def mark_sent(media_id: str, ts: int | None = None) -> None:
    ts = ts or int(time.time())
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.execute("UPDATE reports SET sendmail_ts = ? WHERE media_id = ?", (ts, media_id))
        conn.commit()


# ═══════════════════════════════════════════════════════════════════════
# IMA API
# ═══════════════════════════════════════════════════════════════════════


def call_ima_api(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
    """调用 IMA OpenAPI，统一 POST + JSON Body。"""
    url = f"{BASE_URL}/{endpoint}"
    cleaned: dict[str, Any] = {}
    for k, v in payload.items():
        cleaned[k] = v.encode("utf-8", errors="ignore").decode("utf-8") if isinstance(v, str) else v

    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(url, headers=IMA_HEADERS, json=cleaned)
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as e:
        print(f"[IMA API] HTTP error on {endpoint}: {e}")
        return {"code": -1, "msg": str(e)}
    except Exception as e:
        print(f"[IMA API] error on {endpoint}: {e}")
        return {"code": -1, "msg": str(e)}


def get_knowledge_base_id() -> str:
    """获取知识库 ID。"""
    global ROOT_KB_ID
    if not ROOT_KB_ID:
        # 如果未设置，尝试通过名称搜索
        result = call_ima_api("search_knowledge_base", {"query": ROOT_KB_NAME, "cursor": "", "limit": 20})
        if result.get("code") == 0:
            data = result.get("data", {})
            kb_list = data.get("info_list", [])
            if kb_list:
                ROOT_KB_ID = kb_list[0].get("kb_id", "")
    return ROOT_KB_ID


def search_knowledge(query: str) -> list[dict[str, Any]]:
    """分页搜索知识库，返回全部命中的 info_list 条目。"""
    all_items: list[dict[str, Any]] = []
    cursor = ""
    while True:
        result = call_ima_api(
            "search_knowledge",
            {"query": query, "cursor": cursor, "knowledge_base_id": ROOT_KB_ID},
        )
        if result.get("code") != 0:
            print(f"[search_knowledge] 失败: {result.get('msg')}")
            break

        data = result.get("data", {})
        info_list = data.get("info_list", [])
        all_items.extend(info_list)

        if data.get("is_end", True):
            break
        cursor = data.get("next_cursor", "")
        if not cursor:
            break

    return all_items


def get_media_download_url(media_id: str) -> str | None:
    """获取媒体文件的强制下载 URL。"""
    result = call_ima_api("get_media_info", {"media_id": media_id})
    if result.get("code") != 0:
        print(f"[get_media_info] 失败 (media_id={media_id}): {result.get('msg')}")
        return None

    data = result.get("data", {})
    url_info = data.get("url_info", {})
    raw_url = url_info.get("url")
    if not raw_url:
        print(f"[get_media_info] 无 url (media_id={media_id})")
        return None

    return raw_url


# ═══════════════════════════════════════════════════════════════════════
# Download
# ═══════════════════════════════════════════════════════════════════════


def download_report(media_id: str, title: str, save_dir: Path) -> bool:
    """下载研报到 save_dir 目录。成功返回 True。"""
    download_url = get_media_download_url(media_id)
    if not download_url:
        return False

    # 追加 COS 强制下载参数
    separator = "&" if "?" in download_url else "?"
    dl = f"{download_url}{separator}response-content-type=application%2Foctet-stream&response-content-disposition=attachment%3Bfilename%3D%22{title}%22"

    save_dir.mkdir(parents=True, exist_ok=True)
    filepath = save_dir / title

    try:
        with httpx.Client(timeout=120.0, follow_redirects=True) as client:
            resp = client.get(dl)
            resp.raise_for_status()
            filepath.write_bytes(resp.content)
    except httpx.HTTPError as e:
        print(f"[download] 失败 ({title}): {e}")
        return False
    except OSError as e:
        print(f"[download] I/O 错误 ({title}): {e}")
        return False

    print(f"[download] ✓ {title}")
    return True


# ═══════════════════════════════════════════════════════════════════════
# Email (O365)
# ═══════════════════════════════════════════════════════════════════════

_account: Account | None = None

# 邮件发送结果状态
SEND_OK = "ok"
SEND_CLIENT_ERROR = "client_error"  # 400 Client Error
SEND_FAILED = "failed"              # 其它错误


def _get_account() -> Account | None:
    """懒初始化 O365 Account，认证后全局复用。"""
    global _account
    if _account is not None:
        return _account

    if not O365_CLIENT_ID or not O365_CLIENT_SECRET:
        print("[o365] 缺少 O365_CLIENT_ID / O365_CLIENT_SECRET 配置")
        return None

    credentials = (O365_CLIENT_ID, O365_CLIENT_SECRET)
    _account = Account(credentials, auth_flow_type="credentials", tenant_id=O365_TENANT_ID)

    if _account.authenticate():
        print("[o365] 认证成功")
        return _account
    else:
        print("[o365] 认证失败")
        _account = None
        return None


def send_email(title: str, filepath: Path) -> str:
    """通过 O365 发送带附件的邮件。

    返回 SEND_OK / SEND_CLIENT_ERROR / SEND_FAILED。
    """
    if not EMAIL_FROM or not EMAIL_TO:
        print("[sendmail] 缺少 EMAIL_FROM / EMAIL_TO 配置，跳过")
        return SEND_FAILED

    account = _get_account()
    if not account:
        print("[sendmail] O365 未认证，跳过")
        return SEND_FAILED

    try:
        m = account.new_message(resource=EMAIL_FROM)
        m.to.add(EMAIL_TO)
        m.subject = f"环球研报: {title}"
        m.body = f"<p>附件为最新研报：<strong>{title}</strong></p>"
        m.attachments.add(str(filepath))
        m.send()
        print(f"[sendmail] ✓ {title}")
        return SEND_OK
    except RequestsHTTPError as e:
        status_code = getattr(getattr(e, "response", None), "status_code", None)
        if status_code == 400:
            print(f"[sendmail] 400 Client Error，跳过 ({title}): {e}")
            return SEND_CLIENT_ERROR
        print(f"[sendmail] 失败 ({title}): {e}")
        return SEND_FAILED
    except Exception as e:
        print(f"[sendmail] 失败 ({title}): {e}")
        return SEND_FAILED


def _format_ts_range(ts_list: list[int]) -> str:
    """格式化创建时间范围字符串（UTC）。"""
    if not ts_list:
        return "—"
    fmt = lambda ts: datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    mn, mx = min(ts_list), max(ts_list)
    if mn == mx:
        return fmt(mn)
    return f"{fmt(mn)} ~ {fmt(mx)}"


def send_status_report() -> None:
    """汇总研报下载/发送状态并发送邮件到 STATUS_EMAIL_TO。"""
    if not STATUS_EMAIL_TO:
        print("错误: 缺少 STATUS_EMAIL_TO 环境变量")
        return

    account = _get_account()
    if not account:
        return

    if not EMAIL_FROM:
        print("错误: 缺少 EMAIL_FROM 环境变量")
        return

    # ── 查询各状态数量与创建时间范围 ──
    with sqlite3.connect(str(DB_PATH)) as conn:
        sections = []
        queries = [
            ("未下载", "downloaded_ts = 0"),
            ("已下载未发送", "downloaded_ts > 0 AND sendmail_ts = 0"),
            ("已下载未分类", "downloaded_ts > 0 AND path = 'downloaded_reports'"),
            ("已发送", "sendmail_ts > 0"),
        ]
        for label, where in queries:
            rows = conn.execute(f"SELECT created_ts FROM reports WHERE {where}").fetchall()
            ts_list = [r[0] for r in rows if r[0] > 0]
            sections.append({"label": label, "count": len(rows), "ts_list": ts_list})

        total = conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
        categorized_total = conn.execute(
            "SELECT COUNT(*) FROM reports WHERE path LIKE 'categorized_reports/%'"
        ).fetchone()[0]

        # 已分类研报的一级分类分布
        cat_rows = conn.execute(
            """
            SELECT
                CASE
                    WHEN path LIKE 'categorized_reports/Equity Research/%' THEN 'Equity Research'
                    WHEN path LIKE 'categorized_reports/Macro & Strategy/%' THEN 'Macro & Strategy'
                    WHEN path LIKE 'categorized_reports/Industry & Thematic/%' THEN 'Industry & Thematic'
                    WHEN path LIKE 'categorized_reports/Others/%' THEN 'Others'
                    ELSE 'Others'
                END AS category, COUNT(*)
            FROM reports
            WHERE path LIKE 'categorized_reports/%'
            GROUP BY category
            ORDER BY COUNT(*) DESC
            """
        ).fetchall()

        # 已下载研报（用于文件存在性检查）
        downloaded_rows = conn.execute(
            "SELECT title, path FROM reports WHERE downloaded_ts > 0 ORDER BY created_ts DESC"
        ).fetchall()

    # ── 检查已下载研报文件是否存在 ──
    missing_files: list[dict[str, str]] = []
    for title, path in downloaded_rows:
        if not (_resolve_path(path) / title).exists():
            missing_files.append({"title": title, "path": path})

    # ── 构建 HTML ──
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    rows_html = "\n".join(
        f"<tr>"
        f"<td>{s['label']}</td>"
        f"<td style='text-align:right'>{s['count']}</td>"
        f"<td>{_format_ts_range(s['ts_list'])}</td>"
        f"</tr>"
        for s in sections
    )

    cat_html = ""
    if cat_rows:
        cat_items = "".join(f"<li>{c} — {n} 条</li>" for c, n in cat_rows)
        cat_html = f"<h3>已分类研报分布</h3><ul>{cat_items}</ul>"

    missing_html = ""
    if missing_files:
        missing_items = "".join(
            f"<tr>"
            f"<td>{html.escape(m['title'])}</td>"
            f"<td>{html.escape(m['path'])}</td>"
            f"</tr>"
            for m in missing_files
        )
        missing_html = (
            f"<h3>⚠️ 文件缺失异常（{len(missing_files)} 条）</h3>"
            f"<p>以下已下载研报在文件系统中未找到对应文件：</p>"
            f"<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse'>"
            f"<thead><tr style='background:#fff0f0'>"
            f"<th>研报标题</th><th>期望路径</th>"
            f"</tr></thead>"
            f"<tbody>{missing_items}</tbody>"
            f"</table>"
        )
    else:
        missing_html = "<p>✅ 已下载研报文件均存在，无异常。</p>"

    html_body = (
        f"<h2>研报状态汇总</h2>"
        f"<p>统计时间：{now_str} (UTC)</p>"
        f"<p>总计 {total} 条研报，其中已分类 {categorized_total} 条。</p>"
        f"<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse'>"
        f"<thead><tr style='background:#f5f5f5'>"
        f"<th>状态</th><th>数量</th><th>创建时间范围 (UTC)</th>"
        f"</tr></thead>"
        f"<tbody>{rows_html}</tbody>"
        f"</table>"
        f"{cat_html}"
        f"{missing_html}"
    )

    try:
        m = account.new_message(resource=EMAIL_FROM)
        m.to.add(STATUS_EMAIL_TO)
        m.subject = f"研报状态汇总 ({now_str})"
        m.body = html_body
        m.send()
        print(f"[status] ✓ 已发送至 {STATUS_EMAIL_TO}")
    except Exception as e:
        print(f"[status] ✗ 发送失败: {e}")


def _should_ignore(title: str) -> bool:
    """检查标题是否命中 KEYWORD_IGNORE 过滤词（逗号分隔，大小写不敏感）。"""
    if not KEYWORD_IGNORE:
        return False
    keywords = [kw.strip().lower() for kw in KEYWORD_IGNORE.split(",") if kw.strip()]
    if not keywords:
        return False
    title_lower = title.lower()
    return any(kw in title_lower for kw in keywords)


def collect_reports() -> int:
    """搜索最近 3 天研报并入库，返回新增数量。"""
    today = datetime.now(timezone.utc)
    new_count = 0
    ignored_count = 0
    for offset in reversed(range(3)):
        dt = today - timedelta(days=offset)
        date_str = dt.strftime("%y%m%d")
        print(f"[search] 搜索日期: {date_str}")

        items = search_knowledge(date_str)
        for item in items:
            media_id = item.get("media_id", "")
            title = item.get("title", "")
            if not media_id or not title:
                continue
            if _should_ignore(title):
                ignored_count += 1
                print(f"[ignore] 跳过: {title}")
                continue
            if insert_report(media_id, title):
                new_count += 1
                print(f"[db] 新增: {title}")

    print(f"[collect] 合计新增 {new_count} 条，忽略 {ignored_count} 条")
    return new_count


def download_and_send_new_reports() -> tuple[int, int]:
    """下载并发送研报。

    1. 先将之前已下载但未发送的研报发送出去
    2. 再逐个处理未下载的研报：下载 → 立即发送
       如果下载失败，立即退出不再继续。

    返回 (downloaded_count, sent_count)。
    """
    download_count = 0
    sent_count = 0

    # ── Phase 1: 发送已下载但未发送的存量 ──
    pending_sends = get_reports_to_send()
    if pending_sends:
        print(f"[sendmail] 处理 {len(pending_sends)} 条已下载未发送的存量")
        for item in pending_sends:
            title = item["title"]
            filepath = _resolve_path(item["path"]) / title
            if not filepath.exists():
                print(f"[sendmail] 文件不存在，跳过: {title}")
                continue
            result = send_email(title, filepath)
            if result == SEND_OK:
                mark_sent(item["media_id"])
                sent_count += 1
            elif result == SEND_CLIENT_ERROR:
                continue  # 400 错误，继续处理下一条
            else:
                break  # 其它发送错误，终止后续处理

    # ── Phase 2: 逐个下载并立即发送 ──
    to_download = get_reports_to_download()
    if not to_download:
        print("[download] 没有需要下载的新研报")
        return download_count, sent_count

    print(f"[download] 开始处理 {len(to_download)} 条新研报...")
    for item in to_download:
        media_id = item["media_id"]
        title = item["title"]
        save_dir = _resolve_path(item["path"])

        # 下载
        if not download_report(media_id, title, save_dir):
            print(f"[download] ✗ 下载失败 ({title})，终止后续处理")
            break
        mark_downloaded(media_id)
        download_count += 1

        # 下载成功立即发送
        filepath = save_dir / title
        if send_email(title, filepath) == SEND_OK:
            mark_sent(media_id)
            sent_count += 1

    print(f"[summary] 本次下载 {download_count}，发送 {sent_count}")
    return download_count, sent_count


def main() -> None:
    parser = argparse.ArgumentParser(description="IMA 研报自动下载与邮件分发 CLI")
    parser.add_argument(
        "--status",
        action="store_true",
        help="汇总研报下载/发送状态并发送邮件，不执行下载/发送",
    )
    args = parser.parse_args()

    if args.status:
        init_db()
        send_status_report()
        return

    if not IMA_API_KEY or not IMA_CLIENT_ID:
        print("错误: 缺少 IMA_API_KEY / IMA_CLIENT_ID 环境变量")
        return

    init_db()
    get_knowledge_base_id()
    collect_reports()
    download_and_send_new_reports()


if __name__ == "__main__":
    main()
