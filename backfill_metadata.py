"""回填历史已分类研报的元数据字段。

旧版 categorize_reports.py 只把分类结果写进 path（目录结构），未落库
level1/level2/level3 字段。本脚本从 path 反推分类，并从 title 解析
author/report_date，回填 reports 表，供 upload_to_sharepoint.py 使用。

priority 历史数据无法从 path 判断，默认 'Medium'；如需精确可人工调整，
或用 upload_to_sharepoint.py --force 重写元数据。

用法: python backfill_metadata.py [--dry-run]
"""

from __future__ import annotations

import argparse
import sqlite3
from typing import Any

import check_reports
import categorize_reports as cr

CATEGORIZED_PREFIX = "categorized_reports/"
DEFAULT_PRIORITY = "Medium"


def get_categorized_unfilled() -> list[dict[str, Any]]:
    """返回已分类（path 含 categorized_reports/）但 level1 为空的记录。"""
    with sqlite3.connect(str(check_reports.DB_PATH)) as conn:
        rows = conn.execute(
            "SELECT media_id, title, path FROM reports "
            "WHERE path LIKE 'categorized_reports/%' AND level1 = '' "
            "ORDER BY created_ts DESC"
        ).fetchall()
    return [{"media_id": r[0], "title": r[1], "path": r[2]} for r in rows]


def parse_levels_from_path(path: str) -> tuple[str, str, str]:
    """从 categorized_reports/{level1}/{level2}/{level3} 反推分类。"""
    rel = path[len(CATEGORIZED_PREFIX):]
    parts = [p for p in rel.split("/") if p]
    level1 = parts[0] if parts else ""
    level2 = parts[1] if len(parts) > 1 else ""
    level3 = parts[2] if len(parts) > 2 else ""
    return level1, level2, level3


def main() -> None:
    parser = argparse.ArgumentParser(description="回填历史已分类研报的元数据字段")
    parser.add_argument("--dry-run", action="store_true", help="只打印将回填的字段，不写库")
    args = parser.parse_args()

    check_reports.init_db()
    items = get_categorized_unfilled()
    if not items:
        print("没有需要回填的记录（已分类研报的 level1 均已填充）")
        return

    print(f"待回填: {len(items)} 条\n")
    filled = 0

    for i, item in enumerate(items, start=1):
        level1, level2, level3 = parse_levels_from_path(item["path"])
        author = cr.parse_author(item["title"])
        report_date = cr.parse_date(item["title"])
        priority = DEFAULT_PRIORITY

        label = f"{level1}" + (f" / {level2}" if level2 else "") + (f" / {level3}" if level3 else "")
        print(f"[{i}/{len(items)}] {item['title']}")
        print(f"  → {label} ({priority}) [{author or '?'} | {report_date or '?'}]")

        if args.dry_run:
            filled += 1
            continue

        cr.update_metadata(item["media_id"], author, report_date, level1, level2, level3, priority)
        filled += 1

    print(f"\n—— 完成 —— 回填 {filled} 条")


if __name__ == "__main__":
    main()
