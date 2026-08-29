"""使用 LLM（DeepSeek）对研报进行分类，并移动到分类目录。

流程：
  1. 遍历 reports 表中 path 为缺省值 'downloaded_reports' 的记录
  2. 阅读研报内容，用 LLM 输出两级分类
  3. 按分类生成新路径 categorized_reports/{一级}/{二级}/，移动文件并更新 path 字段

用法: python categorize_reports.py [--dry-run] [--limit N]
"""

import argparse
import json
import os
import re
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

import pymupdf  # fitz API
import httpx
from dotenv import load_dotenv

import check_reports

load_dotenv()

# ── 配置 ──────────────────────────────────────────────────────────────
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

CATEGORIZED_ROOT = check_reports.PROJECT_DIR / "categorized_reports"
DEFAULT_PATH = "downloaded_reports"

LEVEL1_CATEGORIES = ["Equity Research", "Macro & Strategy", "Industry & Thematic", "Others"]

# 每次分类抽取的最大字符数（首页通常含标题/公司/摘要）
MAX_TEXT_CHARS = 6000


# ═══════════════════════════════════════════════════════════════════════
# PDF 文本提取
# ═══════════════════════════════════════════════════════════════════════


def extract_text(filepath: Path, max_chars: int = MAX_TEXT_CHARS) -> str:
    """提取 PDF 前几页文本。无文本层时返回空串。"""
    try:
        doc = pymupdf.open(str(filepath))
    except Exception:
        return ""
    text = ""
    for page in doc[:5]:
        text += str(page.get_text())
    doc.close()
    return text[:max_chars]


# ═══════════════════════════════════════════════════════════════════════
# LLM 分类
# ═══════════════════════════════════════════════════════════════════════

_SYSTEM_PROMPT = """你是一名卖方研究（sell-side research）研报分类专家。根据给定的研报文件名和正文摘要，输出两级分类。

一级分类只能是以下四类之一：
- "Equity Research"：针对具体上市公司的个股研究（含评级、目标价、盈利预测）
- "Macro & Strategy"：宏观经济、外汇、利率、策略、市场展望等
- "Industry & Thematic"：行业研究、产业链、主题投资（半导体、新能源、医药等），不聚焦单一公司
- "Others"：无法归入上述三类

二级分类规则：
- Equity Research：输出该公司股票代码（如 "AAPL"、"0700.HK"、"TCOM.US"、"688825.SH"）；若无法确定代码则输出公司英文名
- Macro & Strategy：输出地区（如 "China"、"US"、"Global"、"Asia ex-Japan"、"Japan"、"Europe"）
- Industry & Thematic：输出行业英文名（如 "Semiconductors"、"Energy Storage"、"Software"、"Autos"、"Biotech"）
- Others：输出空字符串 ""

只输出 JSON，格式：
{"level1": "...", "level2": "..."}
不要输出任何其他文字。"""


def classify_report(title: str, text: str) -> tuple[str, str] | None:
    """调用 DeepSeek 分类，返回 (level1, level2)；失败返回 None。"""
    if not DEEPSEEK_API_KEY:
        print("[classify] 缺少 DEEPSEEK_API_KEY，跳过")
        return None

    user_content = f"文件名：{title}\n\n正文摘要（前 {MAX_TEXT_CHARS} 字符）：\n{text or '(无文本层)'}"

    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
    }

    try:
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(
                f"{DEEPSEEK_BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
    except (httpx.HTTPError, KeyError, IndexError) as e:
        print(f"[classify] API 调用失败 ({title}): {e}")
        return None

    # 解析 JSON
    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        # 尝试从文本中提取 JSON
        m = re.search(r"\{.*\}", content, re.DOTALL)
        if not m:
            print(f"[classify] 无法解析 LLM 输出 ({title}): {content[:200]}")
            return None
        try:
            result = json.loads(m.group(0))
        except json.JSONDecodeError:
            print(f"[classify] JSON 解析失败 ({title})")
            return None

    level1 = result.get("level1", "").strip()
    level2 = result.get("level2", "").strip()

    if level1 not in LEVEL1_CATEGORIES:
        print(f"[classify] 非法一级分类 ({title}): {level1}")
        return None

    if level1 == "Others":
        level2 = ""
    if level1 != "Others" and not level2:
        print(f"[classify] 缺少二级分类 ({title}): {level1}")
        return None

    return level1, level2


# ═══════════════════════════════════════════════════════════════════════
# 文件移动与 DB 更新
# ═══════════════════════════════════════════════════════════════════════


def sanitize_name(name: str) -> str:
    """清理文件系统不安全的字符。"""
    name = name.strip()
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = name.strip(". ")
    return name or "_"


def get_default_reports() -> list[dict[str, Any]]:
    """返回 path 为缺省值且已下载的记录。"""
    with sqlite3.connect(str(check_reports.DB_PATH)) as conn:
        rows = conn.execute(
            "SELECT media_id, title, path FROM reports WHERE path = ? AND downloaded_ts > 0",
            (DEFAULT_PATH,),
        ).fetchall()
    return [{"media_id": r[0], "title": r[1], "path": r[2]} for r in rows]


def update_path(media_id: str, new_path: str) -> None:
    """更新记录的 path 字段。"""
    with sqlite3.connect(str(check_reports.DB_PATH)) as conn:
        conn.execute("UPDATE reports SET path = ? WHERE media_id = ?", (new_path, media_id))
        conn.commit()


def move_report(media_id: str, title: str, src_dir: Path, level1: str, level2: str) -> str | None:
    """将文件移动到分类目录，返回新目录的相对路径；失败返回 None。"""
    level1 = sanitize_name(level1)
    level2 = sanitize_name(level2) if level2 else ""
    dest_dir = CATEGORIZED_ROOT / level1 / level2 if level2 else CATEGORIZED_ROOT / level1
    dest_dir.mkdir(parents=True, exist_ok=True)

    src = src_dir / title
    dest = dest_dir / title

    if not src.exists():
        print(f"[move] 源文件不存在: {src}")
        return None
    if dest.exists():
        print(f"[move] 目标已存在，跳过: {dest}")
        return None

    try:
        shutil.move(str(src), str(dest))
    except OSError as e:
        print(f"[move] 移动失败 ({title}): {e}")
        return None

    # 返回相对于项目根目录的路径
    return str(dest_dir.relative_to(check_reports.PROJECT_DIR))


# ═══════════════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════════════


def main() -> None:
    parser = argparse.ArgumentParser(description="使用 LLM 对研报进行分类")
    parser.add_argument("--dry-run", action="store_true", help="只打印分类结果，不移动文件")
    parser.add_argument("--limit", type=int, default=0, help="最多处理 N 条（0 表示全部）")
    args = parser.parse_args()

    if not DEEPSEEK_API_KEY:
        print("错误: 缺少 DEEPSEEK_API_KEY 环境变量")
        return

    reports = get_default_reports()
    if args.limit > 0:
        reports = reports[: args.limit]

    if not reports:
        print("没有需要分类的研报（path 均为非缺省值）")
        return

    print(f"待分类研报: {len(reports)} 条\n")

    classified = 0
    failed = 0

    for i, item in enumerate(reports, start=1):
        media_id = item["media_id"]
        title = item["title"]
        src_dir = check_reports._resolve_path(item["path"])
        src_file = src_dir / title

        print(f"[{i}/{len(reports)}] {title}")

        if not src_file.exists():
            print("  ✗ 文件不存在，跳过")
            failed += 1
            continue

        # 提取文本
        text = extract_text(src_file)
        if not text.strip():
            print("  ⚠ 无文本层，仅凭文件名分类")

        # LLM 分类
        result = classify_report(title, text)
        if result is None:
            print("  ✗ 分类失败，跳过")
            failed += 1
            continue

        level1, level2 = result
        label = f"{level1}" + (f" / {level2}" if level2 else "")
        print(f"  → {label}")

        if args.dry_run:
            classified += 1
            continue

        # 移动文件
        new_rel_path = move_report(media_id, title, src_dir, level1, level2)
        if new_rel_path is None:
            print("  ✗ 移动失败")
            failed += 1
            continue

        update_path(media_id, new_rel_path)
        print(f"  ✓ 已移动 → {new_rel_path}")
        classified += 1

        time.sleep(0.2)  # 温和限速

    print(f"\n—— 完成 ——")
    print(f"  成功分类: {classified}")
    print(f"  失败/跳过: {failed}")


if __name__ == "__main__":
    main()
