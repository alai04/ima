"""研报分类：PDF 文本提取 + DeepSeek LLM 分类 + 落库 + 移动文件。

不 import 项目内其它模块（DB 路径与分类根目录通过参数传入），供
categorize_reports / check_reports / migrate_equity_research / backfill_metadata
共用，避免循环 import。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pymupdf
from dotenv import load_dotenv

from metadata import parse_author, parse_date

load_dotenv()

# ── 配置 ──────────────────────────────────────────────────────────────
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

LEVEL1_CATEGORIES = ["Equity Research", "Macro & Strategy", "Industry & Thematic", "Others"]
PRIORITY_CATEGORIES = ["High", "Medium", "Low"]

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

_SYSTEM_PROMPT = """你是一名卖方研究（sell-side research）研报分类专家。根据给定的研报文件名和正文摘要，输出分类。

一级分类只能是以下四类之一：
- "Equity Research"：针对具体上市公司的个股研究（含评级、目标价、盈利预测）
- "Macro & Strategy"：宏观经济、外汇、利率、策略、市场展望等
- "Industry & Thematic"：行业研究、产业链、主题投资（半导体、新能源、医药等），不聚焦单一公司
- "Others"：无法归入上述三类

二级分类规则：
- Equity Research：输出该公司所属行业英文名（与 Industry & Thematic 的二级分类保持一致的行业名，如 "Semiconductors"、"Software"、"Autos"、"Internet"、"Pharmaceuticals" 等）
- Macro & Strategy：输出地区（如 "China"、"US"、"Global"、"Asia ex-Japan"、"Japan"、"Europe"）
- Industry & Thematic：输出行业英文名（如 "Semiconductors"、"Energy Storage"、"Software"、"Autos"、"Biotech"）
- Others：输出空字符串 ""

三级分类规则：
- Equity Research：输出该公司股票代码（如 "AAPL"、"0700.HK"、"TCOM.US"、"688825.SH"）；若无法确定代码则输出公司英文名
- 其他三类：输出空字符串 ""

额外判断优先级 priority，只能是以下三档之一：
- "High"：软件行业股票、所有宏观研报、首次覆盖、业绩大幅超/低于预期
- "Medium"：常规业绩点评、行业/策略例行更新
- "Low"：数据更新、例行周报、会议纪要
无法判断时输出 "Medium"。

只输出 JSON，格式：
{"level1": "...", "level2": "...", "level3": "...", "priority": "..."}
不要输出任何其他文字。"""


def classify_report(title: str, text: str) -> tuple[str, str, str, str] | None:
    """调用 DeepSeek 分类，返回 (level1, level2, level3, priority)；失败返回 None。"""
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
    level3 = result.get("level3", "").strip()
    priority = result.get("priority", "").strip() or "Medium"

    if level1 not in LEVEL1_CATEGORIES:
        print(f"[classify] 非法一级分类 ({title}): {level1}")
        return None

    if priority not in PRIORITY_CATEGORIES:
        print(f"[classify] 非法优先级 ({title}): {priority}，回退 Medium")
        priority = "Medium"

    if level1 == "Others":
        level2 = ""
        level3 = ""
    if level1 != "Others" and not level2:
        print(f"[classify] 缺少二级分类 ({title}): {level1}")
        return None
    if level1 == "Equity Research" and not level3:
        print(f"[classify] 缺少三级分类（股票代码）({title})")
        return None
    if level1 != "Equity Research":
        level3 = ""  # 仅 Equity Research 有三级分类

    return level1, level2, level3, priority


def sanitize_name(name: str) -> str:
    """清理文件系统不安全的字符。"""
    name = name.strip()
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = name.strip(". ")
    return name or "_"


# ═══════════════════════════════════════════════════════════════════════
# 落库与移动
# ═══════════════════════════════════════════════════════════════════════


def update_metadata(db_path: Path, media_id: str, author: str, report_date: str,
                    level1: str, level2: str, level3: str, priority: str) -> None:
    """落库元数据字段（撰写方/撰写日期/一级/二级/三级分类/优先级）。"""
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "UPDATE reports SET author = ?, report_date = ?, level1 = ?, level2 = ?, level3 = ?, priority = ? "
            "WHERE media_id = ?",
            (author, report_date, level1, level2, level3, priority, media_id),
        )
        conn.commit()


def update_path(db_path: Path, media_id: str, new_path: str) -> None:
    """更新记录的 path 字段。"""
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("UPDATE reports SET path = ? WHERE media_id = ?", (new_path, media_id))
        conn.commit()


def move_report(categorized_root: Path, media_id: str, title: str, src_dir: Path,
                level1: str, level2: str, level3: str) -> str | None:
    """将文件移动到分类目录，返回新目录的相对路径；失败返回 None。"""
    level1 = sanitize_name(level1)
    level2 = sanitize_name(level2) if level2 else ""
    level3 = sanitize_name(level3) if level3 else ""
    if level3:
        dest_dir = categorized_root / level1 / level2 / level3
    elif level2:
        dest_dir = categorized_root / level1 / level2
    else:
        dest_dir = categorized_root / level1
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

    # 返回相对于项目根目录（categorized_root 的父目录）的路径
    return str(dest_dir.relative_to(categorized_root.parent))


def classify_one_report(db_path: Path, categorized_root: Path, media_id: str, title: str,
                        src_dir: Path, dry_run: bool = False) -> tuple[bool, str]:
    """对单条研报分类、落库元数据、移动文件。

    返回 (ok, new_rel_path)：
      - ok=True 且 new_rel_path 非空：分类成功且已移动
      - ok=True 且 new_rel_path 空：dry_run 分类成功（未落库未移动）
      - ok=False：失败（文件不存在/分类失败/移动失败），文件保持原位置
    """
    src_file = src_dir / title
    if not src_file.exists():
        print("  ✗ 文件不存在，跳过")
        return False, ""

    text = extract_text(src_file)
    if not text.strip():
        print("  ⚠ 无文本层，仅凭文件名分类")

    author = parse_author(title)
    report_date = parse_date(title)

    result = classify_report(title, text)
    if result is None:
        print("  ✗ 分类失败，跳过")
        return False, ""

    level1, level2, level3, priority = result
    label = f"{level1}" + (f" / {level2}" if level2 else "") + (f" / {level3}" if level3 else "") + f" ({priority})"
    print(f"  → {label} [{author or '?'} | {report_date or '?'}]")

    if dry_run:
        return True, ""

    update_metadata(db_path, media_id, author, report_date, level1, level2, level3, priority)

    new_rel_path = move_report(categorized_root, media_id, title, src_dir, level1, level2, level3)
    if new_rel_path is None:
        print("  ✗ 移动失败")
        return False, ""

    update_path(db_path, media_id, new_rel_path)
    print(f"  ✓ 已移动 → {new_rel_path}")
    return True, new_rel_path
