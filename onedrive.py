"""OneDrive（SharePoint 文档库）上传封装：按撰写方分子文件夹 + 同名覆盖。

目标位置（缺省）：

    https://ikariacapital.sharepoint.com/sites/IkariaEquity
      /Shared Documents/环球研报/{撰写方机构名}/{研报文件名}

与 ``sharepoint.py`` 的区别：

  * ``sharepoint.py`` 把文件**平铺**上传到「研报库」根目录，并 PATCH 元数据列；
  * 本模块按**撰写方机构名**建子文件夹（不存在则新建），**不写元数据列**，
    目标位置已存在同名文件时直接覆盖。

复用现有 Azure AD 应用（client_credentials）的 Graph token，无需新账号。

环境变量（``O365_CLIENT_ID`` / ``O365_CLIENT_SECRET`` 与邮件、SharePoint 共用）：

  ``ONEDRIVE_ENABLED``        是否启用（缺省 true）
  ``ONEDRIVE_TENANT``         租户（缺省复用 ``SHAREPOINT_TENANT``）
  ``ONEDRIVE_SITE_PATH``      站点路径（缺省 /sites/IkariaEquity）
  ``ONEDRIVE_DRIVE_NAME``     文档库名称（缺省空 = 站点默认文档库 Shared Documents）
  ``ONEDRIVE_ROOT_FOLDER``    目标根文件夹（缺省 环球研报）
  ``ONEDRIVE_DEFAULT_FOLDER`` 无法判定撰写方时的子文件夹（缺省 Others）
"""

from __future__ import annotations

import os
import re
import time
from typing import Any
from urllib.parse import quote

import httpx
from dotenv import load_dotenv

import sharepoint
from metadata import parse_author

load_dotenv()

# ── 环境变量 ──────────────────────────────────────────────────────────
ONEDRIVE_ENABLED = os.getenv("ONEDRIVE_ENABLED", "true").strip().lower() in (
    "1", "true", "yes", "on",
)
ONEDRIVE_TENANT = os.getenv("ONEDRIVE_TENANT", "").strip() or sharepoint.SHAREPOINT_TENANT
ONEDRIVE_SITE_PATH = os.getenv("ONEDRIVE_SITE_PATH", "/sites/IkariaEquity").strip() or "/sites/IkariaEquity"
ONEDRIVE_DRIVE_NAME = os.getenv("ONEDRIVE_DRIVE_NAME", "").strip()
ONEDRIVE_ROOT_FOLDER = os.getenv("ONEDRIVE_ROOT_FOLDER", "环球研报").strip().strip("/") or "环球研报"
ONEDRIVE_DEFAULT_FOLDER = os.getenv("ONEDRIVE_DEFAULT_FOLDER", "Others").strip() or "Others"

GRAPH_BASE = sharepoint.GRAPH_BASE
SIMPLE_UPLOAD_LIMIT = sharepoint.SIMPLE_UPLOAD_LIMIT
SESSION_CHUNK_SIZE = sharepoint.SESSION_CHUNK_SIZE

OneDriveError = sharepoint.SharePointError

# ── 进程内缓存 ─────────────────────────────────────────────────────────
_site_id: str | None = None
_drive_id: str | None = None
_ensured_folders: set[str] = set()

_INVALID_FOLDER_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


# ═══════════════════════════════════════════════════════════════════════
# 路径工具（纯函数）
# ═══════════════════════════════════════════════════════════════════════


def join_path(*parts: str) -> str:
    """拼接文档库内的相对路径（去掉多余斜杠）。"""
    return "/".join(p.strip("/") for p in parts if p and p.strip("/"))


def sanitize_folder_name(name: str, max_len: int = 80) -> str:
    """把机构名清洗成合法文件夹名（去掉 ``\\ / : * ? " < > |`` 等非法字符）。"""
    cleaned = _INVALID_FOLDER_CHARS.sub("_", name or "")
    cleaned = cleaned.strip().strip(".").strip()
    return cleaned[:max_len].strip()


def author_folder(author: str, title: str = "") -> str:
    """按撰写方机构名决定子文件夹名。

    优先用 DB 里的 ``author``；为空时从文件名前缀解析（中文券商名映射为英文）；
    仍无法判定则落到 ``ONEDRIVE_DEFAULT_FOLDER``（缺省 Others）。
    """
    name = (author or "").strip()
    if not name and title:
        name = parse_author(title)
    return sanitize_folder_name(name) or ONEDRIVE_DEFAULT_FOLDER


def target_folder(author: str, title: str = "") -> str:
    """返回完整的目标文件夹相对路径：``环球研报/{撰写方}``。"""
    return join_path(ONEDRIVE_ROOT_FOLDER, author_folder(author, title))


# ═══════════════════════════════════════════════════════════════════════
# 站点 / 文档库解析（含缓存）
# ═══════════════════════════════════════════════════════════════════════


def resolve_site_id() -> str:
    """解析站点 ID（优先按路径精确解析，失败按站点名搜索）。"""
    global _site_id
    if _site_id:
        return _site_id

    if not ONEDRIVE_TENANT:
        raise OneDriveError("缺少 ONEDRIVE_TENANT（或 SHAREPOINT_TENANT）环境变量")

    hostname = f"{ONEDRIVE_TENANT}.sharepoint.com"
    site_path = ONEDRIVE_SITE_PATH.strip("/")
    site_ref = quote(f"{hostname}:/{site_path}", safe="")

    try:
        data = sharepoint.graph_request("GET", f"{GRAPH_BASE}/sites/{site_ref}").json()
        _site_id = data.get("id", "")
    except OneDriveError:
        _site_id = ""

    if not _site_id:
        site_name = site_path.split("/")[-1] or site_path
        data = sharepoint.graph_request(
            "GET", f"{GRAPH_BASE}/sites?search={quote(site_name)}"
        ).json()
        sites = data.get("value", [])
        for site in sites:
            if site.get("name", "").lower() == site_name.lower():
                _site_id = site.get("id", "")
                break
        if not _site_id and sites:
            _site_id = sites[0].get("id", "")

    if not _site_id:
        raise OneDriveError(f"未解析到 OneDrive 站点: {ONEDRIVE_SITE_PATH}")
    return _site_id


def resolve_drive_id() -> str:
    """解析文档库 drive ID（缺省用站点默认文档库）。"""
    global _drive_id
    if _drive_id:
        return _drive_id

    site_id = resolve_site_id()
    if ONEDRIVE_DRIVE_NAME:
        data = sharepoint.graph_request("GET", f"{GRAPH_BASE}/sites/{site_id}/drives").json()
        for drive in data.get("value", []):
            if drive.get("name", "").lower() == ONEDRIVE_DRIVE_NAME.lower():
                _drive_id = drive.get("id", "")
                break
        if not _drive_id:
            raise OneDriveError(f"未找到文档库: {ONEDRIVE_DRIVE_NAME}")
    else:
        data = sharepoint.graph_request("GET", f"{GRAPH_BASE}/sites/{site_id}/drive").json()
        _drive_id = data.get("id", "")
        if not _drive_id:
            raise OneDriveError(f"未解析到站点默认文档库: {ONEDRIVE_SITE_PATH}")
    return _drive_id


def site_url() -> str:
    """站点 URL（用于日志/邮件链接）。"""
    if not ONEDRIVE_TENANT:
        return ""
    return f"https://{ONEDRIVE_TENANT}.sharepoint.com{ONEDRIVE_SITE_PATH}"


def root_folder_url(encoded: bool = True) -> str:
    """研报根文件夹的浏览 URL（含文档库路径）。

    形如 ``{site}/Shared Documents/{ONEDRIVE_ROOT_FOLDER}``；未配置
    ``ONEDRIVE_DRIVE_NAME`` 时按站点默认文档库名 ``Shared Documents``。

    ``encoded=True`` 返回百分号编码后的链接（适合放进 href）；
    ``encoded=False`` 返回原始中文路径（适合作为显示文本）。
    """
    base = site_url()
    if not base:
        return ""
    drive_name = ONEDRIVE_DRIVE_NAME or "Shared Documents"
    raw = f"{base}/{drive_name}/{ONEDRIVE_ROOT_FOLDER}"
    return quote(raw, safe=":/") if encoded else raw


# ═══════════════════════════════════════════════════════════════════════
# Graph 路径封装
# ═══════════════════════════════════════════════════════════════════════


def _drive_base() -> str:
    return f"{GRAPH_BASE}/sites/{resolve_site_id()}/drive"


def item_ref(rel_path: str) -> str:
    """构造 ``root:/{path}:`` 引用（整体 URL 编码，与 sharepoint.py 一致）。"""
    return quote(f"root:/{rel_path.strip('/')}:", safe="")


def get_item(rel_path: str) -> dict[str, Any] | None:
    """按相对路径取 driveItem；不存在返回 None。"""
    url = f"{_drive_base()}/{item_ref(rel_path)}"
    resp = sharepoint.graph_request("GET", url, raise_on_error=False)
    if resp.status_code == 404:
        return None
    sharepoint.raise_for_graph(resp)
    return resp.json()


def create_folder(parent_path: str, name: str) -> dict[str, Any]:
    """在 ``parent_path`` 下新建子文件夹（已存在则返回现有项）。"""
    body = {
        "name": name,
        "folder": {},
        "@microsoft.graph.conflictBehavior": "replace",
    }
    url = (
        f"{_drive_base()}/root/children"
        if not parent_path
        else f"{_drive_base()}/{item_ref(parent_path)}/children"
    )
    resp = sharepoint.graph_request("POST", url, json_body=body)
    return resp.json()


def ensure_folder(rel_path: str) -> str:
    """逐级确保文件夹存在（不存在则新建），返回原相对路径。"""
    rel_path = join_path(rel_path)
    if not rel_path or rel_path in _ensured_folders:
        return rel_path

    parent = ""
    for segment in rel_path.split("/"):
        child = join_path(parent, segment)
        if get_item(child) is None:
            create_folder(parent, segment)
        parent = child

    _ensured_folders.add(rel_path)
    return rel_path


# ═══════════════════════════════════════════════════════════════════════
# 上传
# ═══════════════════════════════════════════════════════════════════════


def _simple_upload(rel_path: str, data: bytes) -> dict[str, Any]:
    """<=250MB 走简单 PUT（``conflictBehavior=replace`` 覆盖同名文件）。"""
    url = f"{_drive_base()}/{item_ref(rel_path)}/content?@microsoft.graph.conflictBehavior=replace"
    resp = sharepoint.graph_request(
        "PUT",
        url,
        content=data,
        headers={"Content-Type": "application/pdf"},
        timeout=180.0,
    )
    return resp.json()


def _session_upload(rel_path: str, data: bytes) -> dict[str, Any]:
    """>250MB 走 createUploadSession 分片上传（同样覆盖同名文件）。"""
    create_url = f"{_drive_base()}/{item_ref(rel_path)}/createUploadSession"
    body = {"item": {"@microsoft.graph.conflictBehavior": "replace"}}
    session = sharepoint.graph_request("POST", create_url, json_body=body).json()
    upload_url = session.get("uploadUrl", "")
    if not upload_url:
        raise OneDriveError(f"createUploadSession 未返回 uploadUrl: {session}")

    total = len(data)
    offset = 0
    while offset < total:
        chunk = data[offset : offset + SESSION_CHUNK_SIZE]
        end = offset + len(chunk) - 1
        headers = {
            "Content-Length": str(len(chunk)),
            "Content-Range": f"bytes {offset}-{end}/{total}",
        }
        with httpx.Client(timeout=180.0) as client:
            resp = client.put(upload_url, headers=headers, content=chunk)
            if resp.status_code in (200, 201):
                return resp.json()
            if resp.status_code != 202:
                sharepoint.raise_for_graph(resp)
        offset = end + 1
    raise OneDriveError(f"上传会话未完成: {rel_path}")


def upload_file(folder: str, filename: str, data: bytes) -> dict[str, Any]:
    """上传文件到 ``{root}/{folder}``，同名覆盖。返回 driveItem（含 id/webUrl）。"""
    folder = ensure_folder(folder)
    rel_path = join_path(folder, filename)
    if len(data) <= SIMPLE_UPLOAD_LIMIT:
        return _simple_upload(rel_path, data)
    return _session_upload(rel_path, data)


def upload_report(title: str, author: str, data: bytes) -> dict[str, Any]:
    """按撰写方上传一份研报，返回 ``{id, webUrl, folder, size}``。"""
    folder = target_folder(author, title)
    item = upload_file(folder, title, data)
    return {
        "id": item.get("id", ""),
        "webUrl": item.get("webUrl", ""),
        "folder": folder,
        "size": item.get("size", len(data)),
    }


def probe() -> dict[str, Any]:
    """连通性自检：解析站点/文档库并列出目标根文件夹下的子文件夹。"""
    site_id = resolve_site_id()
    drive_id = resolve_drive_id()
    item = get_item(ONEDRIVE_ROOT_FOLDER)
    children: list[str] = []
    if item is not None:
        url = f"{_drive_base()}/{item_ref(ONEDRIVE_ROOT_FOLDER)}/children?$select=name,folder"
        data = sharepoint.graph_request("GET", url).json()
        children = [
            c.get("name", "") for c in data.get("value", []) if "folder" in c
        ]
    return {
        "site_id": site_id,
        "drive_id": drive_id,
        "root_exists": item is not None,
        "root": ONEDRIVE_ROOT_FOLDER,
        "folders": children,
        "checked_at": int(time.time()),
    }
