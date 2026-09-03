"""SharePoint Graph 认证 + 上传 + 元数据写入封装。

复用现有 Azure AD 应用（client_credentials），通过 Microsoft Graph API：
  1. 上传研报 PDF 到 SharePoint 文档库
  2. 写入 list item 字段（元数据列）

核心注意：Graph 的 ``PUT .../content`` 只写文件内容，**不写文档库的元数据列**；
必须紧跟一次 ``PATCH .../listItem/fields`` 才能落元数据。

本模块无副作用、纯函数式封装，供 ``upload_to_sharepoint.py`` 调用。
"""

from __future__ import annotations

import os
import time
from typing import Any
from urllib.parse import quote

import httpx
from dotenv import load_dotenv
from msal import ConfidentialClientApplication

load_dotenv()

# ── 环境变量 ──────────────────────────────────────────────────────────
O365_CLIENT_ID = os.getenv("O365_CLIENT_ID", "")
O365_CLIENT_SECRET = os.getenv("O365_CLIENT_SECRET", "")
O365_TENANT_ID = os.getenv("O365_TENANT_ID", "common")
SHAREPOINT_TENANT = os.getenv("SHAREPOINT_TENANT", "")
SHAREPOINT_SITE_PATH = os.getenv("SHAREPOINT_SITE_PATH", "")
SHAREPOINT_DRIVE_NAME = os.getenv("SHAREPOINT_DRIVE_NAME", "Documents")
SHAREPOINT_ENABLED = os.getenv("SHAREPOINT_ENABLED", "true").strip().lower() in (
    "1", "true", "yes", "on",
)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]
SIMPLE_UPLOAD_LIMIT = 250 * 1024 * 1024  # 简单 PUT 上限 250MB
SESSION_CHUNK_SIZE = 10 * 1024 * 1024   # upload session 分片 10MB

# ── 进程内缓存 ─────────────────────────────────────────────────────────
_app: ConfidentialClientApplication | None = None
_site_id: str | None = None
_drive_id: str | None = None
_list_id: str | None = None


class SharePointError(RuntimeError):
    """SharePoint 操作失败（认证/权限/网络/冲突等）。"""


# ═══════════════════════════════════════════════════════════════════════
# 认证
# ═══════════════════════════════════════════════════════════════════════


def _get_app() -> ConfidentialClientApplication:
    global _app
    if _app is None:
        if not O365_CLIENT_ID or not O365_CLIENT_SECRET:
            raise SharePointError("缺少 O365_CLIENT_ID / O365_CLIENT_SECRET 环境变量")
        _app = ConfidentialClientApplication(
            client_id=O365_CLIENT_ID,
            client_credential=O365_CLIENT_SECRET,
            authority=f"https://login.microsoftonline.com/{O365_TENANT_ID}",
        )
    return _app


def get_graph_token() -> str:
    """获取 client_credentials 的 Graph access token（msal 内部缓存刷新）。"""
    result = _get_app().acquire_token_for_client(scopes=GRAPH_SCOPE)
    if not result or "access_token" not in result:
        err = (result or {}).get("error_description") or (result or {}).get("error") or str(result)
        raise SharePointError(f"获取 Graph token 失败: {err}")
    return result["access_token"]


# ═══════════════════════════════════════════════════════════════════════
# HTTP 封装（统一 429 退避 + 错误分类）
# ═══════════════════════════════════════════════════════════════════════


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {get_graph_token()}"}


def _retry_after(resp: httpx.Response, default: float = 5.0) -> float:
    raw = resp.headers.get("Retry-After", "")
    try:
        return max(1.0, float(raw))
    except ValueError:
        return default


def _raise_for_graph(resp: httpx.Response) -> None:
    if resp.is_success:
        return
    detail = ""
    try:
        detail = resp.json().get("error", {}).get("message", "")
    except Exception:
        pass
    method = resp.request.method
    url = resp.request.url
    raise SharePointError(
        f"Graph {method} {url} -> {resp.status_code}: {detail or resp.text[:200]}"
    )


def _request_json(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    """执行返回 JSON 的请求，429 自动退避。"""
    with httpx.Client(timeout=30.0) as client:
        while True:
            resp = client.request(method, url, headers=_auth_headers(), **kwargs)
            if resp.status_code == 429:
                time.sleep(_retry_after(resp))
                continue
            _raise_for_graph(resp)
            return resp.json()


def graph_get(path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return _request_json("GET", f"{GRAPH_BASE}{path}", params=params or {})


def graph_patch_json(path: str, body: dict[str, Any]) -> dict[str, Any]:
    return _request_json("PATCH", f"{GRAPH_BASE}{path}", json=body)


# ═══════════════════════════════════════════════════════════════════════
# 站点 / 文档库解析（含缓存）
# ═══════════════════════════════════════════════════════════════════════


def resolve_site() -> str:
    """解析站点 ID。优先按路径精确解析，失败则按名称搜索。"""
    global _site_id
    if _site_id:
        return _site_id

    if not SHAREPOINT_TENANT:
        raise SharePointError("缺少 SHAREPOINT_TENANT 环境变量")

    hostname = f"{SHAREPOINT_TENANT}.sharepoint.com"
    site_ref = quote(f"{hostname}:/{SHAREPOINT_SITE_PATH.strip('/')}", safe="")

    try:
        data = graph_get(f"/sites/{site_ref}")
        _site_id = data.get("id", "")
    except SharePointError:
        _site_id = ""

    if not _site_id:
        site_name = SHAREPOINT_SITE_PATH.strip("/").split("/")[-1]
        data = graph_get("/sites", params={"search": site_name})
        sites = data.get("value", [])
        for s in sites:
            if s.get("webUrl", "").lower().find(hostname.lower()) >= 0 and s.get("name", "").lower() == site_name.lower():
                _site_id = s.get("id", "")
                break
        if not _site_id and sites:
            _site_id = sites[0].get("id", "")

    if not _site_id:
        raise SharePointError(f"未解析到 site: {SHAREPOINT_SITE_PATH}")
    return _site_id


def resolve_drive() -> str:
    """解析文档库 drive ID（含缓存）。"""
    global _drive_id
    if _drive_id:
        return _drive_id

    site_id = resolve_site()
    data = graph_get(f"/sites/{site_id}/drives")
    drives = data.get("value", [])

    for d in drives:
        if d.get("name") == SHAREPOINT_DRIVE_NAME:
            _drive_id = d.get("id", "")
            break
    if not _drive_id:  # 回退到默认 Documents 库
        for d in drives:
            if d.get("name") == "Documents":
                _drive_id = d.get("id", "")
                break
    if not _drive_id:
        raise SharePointError(f"未找到文档库: {SHAREPOINT_DRIVE_NAME}")
    return _drive_id


def resolve_list() -> str:
    """解析文档库对应的 list ID（用于 listItem 查询，含缓存）。"""
    global _list_id
    if _list_id:
        return _list_id

    site_id = resolve_site()
    data = graph_get(f"/sites/{site_id}/lists")
    lists = data.get("value", [])

    for lst in lists:
        if lst.get("displayName") == SHAREPOINT_DRIVE_NAME or lst.get("name") == SHAREPOINT_DRIVE_NAME:
            _list_id = lst.get("id", "")
            break
    if not _list_id:
        raise SharePointError(f"未找到列表: {SHAREPOINT_DRIVE_NAME}")
    return _list_id


def site_url() -> str:
    """返回 SharePoint 站点 URL（用于邮件链接）。未配置 tenant 时返回空串。"""
    if not SHAREPOINT_TENANT:
        return ""
    return f"https://ikariacapital.sharepoint.com{SHAREPOINT_SITE_PATH}"


# ═══════════════════════════════════════════════════════════════════════
# 上传
# ═══════════════════════════════════════════════════════════════════════


def _target_path(filename: str) -> str:
    """构造 Graph 的 root: 路径引用（含冒号，整体 URL 编码）。"""
    return quote(f"root:/{filename}:", safe="")


def _simple_upload(filename: str, data: bytes) -> dict[str, Any]:
    site_id = resolve_site()
    drive_id = resolve_drive()
    target = _target_path(filename)
    url = f"{GRAPH_BASE}/sites/{site_id}/drives/{drive_id}/{target}/content"

    headers = _auth_headers()
    headers["Content-Type"] = "application/pdf"

    with httpx.Client(timeout=120.0) as client:
        while True:
            resp = client.put(url, headers=headers, content=data)
            if resp.status_code == 429:
                time.sleep(_retry_after(resp))
                continue
            _raise_for_graph(resp)
            return resp.json()


def _session_upload(filename: str, data: bytes) -> dict[str, Any]:
    """大文件（>250MB）走 createUploadSession 分片上传。"""
    site_id = resolve_site()
    drive_id = resolve_drive()
    target = _target_path(filename)
    create_url = f"{GRAPH_BASE}/sites/{site_id}/drives/{drive_id}/{target}/createUploadSession"

    body = {"item": {"@microsoft.graph.conflictBehavior": "replace"}}
    session = _request_json("POST", create_url, json=body)
    upload_url = session.get("uploadUrl", "")
    if not upload_url:
        raise SharePointError(f"createUploadSession 未返回 uploadUrl: {session}")

    total = len(data)
    offset = 0
    while offset < total:
        chunk = data[offset : offset + SESSION_CHUNK_SIZE]
        end = offset + len(chunk) - 1
        with httpx.Client(timeout=120.0) as client:
            resp = client.put(
                upload_url,
                headers={"Content-Length": str(len(chunk)), "Content-Range": f"bytes {offset}-{end}/{total}"},
                content=chunk,
            )
            if resp.status_code in (200, 201):
                return resp.json()
            if resp.status_code not in (202,):
                _raise_for_graph(resp)
        offset = end + 1
    raise SharePointError(f"上传会话未完成: {filename}")


def upload_file(filename: str, data: bytes) -> dict[str, Any]:
    """上传文件到文档库根目录。返回 driveItem（含 id 与 webUrl）。"""
    if len(data) <= SIMPLE_UPLOAD_LIMIT:
        return _simple_upload(filename, data)
    return _session_upload(filename, data)


# ═══════════════════════════════════════════════════════════════════════
# 元数据
# ═══════════════════════════════════════════════════════════════════════


def set_fields(item_id: str, fields: dict[str, Any]) -> None:
    """PATCH listItem/fields 写入元数据。``item_id`` 为 drive item id。"""
    site_id = resolve_site()
    drive_id = resolve_drive()
    graph_patch_json(f"/sites/{site_id}/drives/{drive_id}/items/{item_id}/listItem/fields", fields)


def find_by_media_id(media_id: str) -> dict[str, Any] | None:
    """按 MediaId 字段查询远端是否已存在。

    返回 {"drive_item_id": str, "webUrl": str} 或 None。drive_item_id 用于
    set_fields（drive 端点）重写元数据。
    """
    site_id = resolve_site()
    list_id = resolve_list()
    data = graph_get(
        f"/sites/{site_id}/lists/{list_id}/items",
        params={"expand": "driveItem", "filter": f"fields/MediaId eq '{media_id}'"},
    )
    items = data.get("value", [])
    if not items:
        return None
    item = items[0]
    drive_item = item.get("driveItem") or {}
    return {
        "drive_item_id": str(drive_item.get("id", "")),
        "webUrl": item.get("webUrl", ""),
    }
