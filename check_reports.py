"""IMA 研报自动下载与邮件分发 CLI。

功能：
  1. 从"环球研报直通车"知识库搜索最近 3 天的研报
  2. 保存到 SQLite DB（去重）
  3. 下载新增研报并立即邮件发送（下载失败则终止）
"""

import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()

# ── 环境变量 ──────────────────────────────────────────────────────────
IMA_API_KEY = os.getenv("IMA_API_KEY", "")
IMA_CLIENT_ID = os.getenv("IMA_CLIENT_ID", "")
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", "")
EMAIL_TO = os.getenv("EMAIL_TO", "")
KEYWORD_IGNORE = os.getenv("KEYWORD_IGNORE", "")
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
                created_ts    INTEGER DEFAULT 0
            )
            """
        )
        # 兼容旧表：补上 created_ts 列
        try:
            conn.execute("ALTER TABLE reports ADD COLUMN created_ts INTEGER DEFAULT 0")
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


def get_reports_to_download() -> list[dict[str, Any]]:
    """获取尚未下载的研报，按插入时间从近到远排序。"""
    with sqlite3.connect(str(DB_PATH)) as conn:
        rows = conn.execute(
            "SELECT media_id, title FROM reports WHERE downloaded_ts = 0 ORDER BY created_ts DESC"
        ).fetchall()
    return [{"media_id": r[0], "title": r[1]} for r in rows]


def get_reports_to_send() -> list[dict[str, Any]]:
    """获取已下载但未发送的研报，按插入时间从近到远排序。"""
    with sqlite3.connect(str(DB_PATH)) as conn:
        rows = conn.execute(
            "SELECT media_id, title FROM reports WHERE downloaded_ts > 0 AND sendmail_ts = 0 ORDER BY created_ts DESC"
        ).fetchall()
    return [{"media_id": r[0], "title": r[1]} for r in rows]


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


def download_report(media_id: str, title: str) -> bool:
    """下载研报到 downloaded_reports/ 目录。成功返回 True。"""
    download_url = get_media_download_url(media_id)
    if not download_url:
        return False

    # 追加 COS 强制下载参数
    separator = "&" if "?" in download_url else "?"
    dl = f"{download_url}{separator}response-content-type=application%2Foctet-stream&response-content-disposition=attachment%3Bfilename%3D%22{title}%22"

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    filepath = DOWNLOAD_DIR / title

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
# Email (Resend)
# ═══════════════════════════════════════════════════════════════════════


def send_email(title: str, filepath: Path) -> bool:
    """通过 Resend API 发送带附件的邮件。成功返回 True。"""
    if not RESEND_API_KEY or not EMAIL_FROM or not EMAIL_TO:
        print("[sendmail] 缺少 RESEND_API_KEY / EMAIL_FROM / EMAIL_TO 配置，跳过")
        return False

    content_bytes = filepath.read_bytes()

    payload: dict[str, Any] = {
        "from": EMAIL_FROM,
        "to": [EMAIL_TO],
        "subject": f"环球研报: {title}",
        "html": f"<p>附件为最新研报：<strong>{title}</strong></p>",
        "attachments": [
            {
                "filename": title,
                "content": list(content_bytes),
            }
        ],
    }

    try:
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {RESEND_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            print(f"[sendmail] ✓ {title} (email id: {data.get('id', '?' )})")
            return True
    except httpx.HTTPStatusError as e:
        print(f"[sendmail] 失败 ({title}): {e}")
        print(f"  Response: {e.response.text[:500]}")
        return False
    except httpx.HTTPError as e:
        print(f"[sendmail] 失败 ({title}): {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════
# Orchestrator
# ═══════════════════════════════════════════════════════════════════════


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
            filepath = DOWNLOAD_DIR / title
            if not filepath.exists():
                print(f"[sendmail] 文件不存在，跳过: {title}")
                continue
            if send_email(title, filepath):
                mark_sent(item["media_id"])
                sent_count += 1

    # ── Phase 2: 逐个下载并立即发送 ──
    to_download = get_reports_to_download()
    if not to_download:
        print("[download] 没有需要下载的新研报")
        return download_count, sent_count

    print(f"[download] 开始处理 {len(to_download)} 条新研报...")
    for item in to_download:
        media_id = item["media_id"]
        title = item["title"]

        # 下载
        if not download_report(media_id, title):
            print(f"[download] ✗ 下载失败 ({title})，终止后续处理")
            break
        mark_downloaded(media_id)
        download_count += 1

        # 下载成功立即发送
        filepath = DOWNLOAD_DIR / title
        if send_email(title, filepath):
            mark_sent(media_id)
            sent_count += 1

    print(f"[summary] 本次下载 {download_count}，发送 {sent_count}")
    return download_count, sent_count


def main() -> None:
    if not IMA_API_KEY or not IMA_CLIENT_ID:
        print("错误: 缺少 IMA_API_KEY / IMA_CLIENT_ID 环境变量")
        return

    init_db()
    get_knowledge_base_id()
    collect_reports()
    download_and_send_new_reports()


if __name__ == "__main__":
    main()
