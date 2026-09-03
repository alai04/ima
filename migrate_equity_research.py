"""迁移历史 Equity Research 研报到三级分类。

旧标准：Equity Research 二级 = 股票代码
新标准：Equity Research 二级 = 行业（与 Industry & Thematic 一致），三级 = 股票代码

对 level1='Equity Research' 且 level3='' 的历史记录：
  1. 重新 LLM 分类得到 (Equity Research, 行业, 股票代码, priority)
  2. 更新 DB：level2=行业，level3=股票代码（author/report_date 沿用现有值）
  3. 移动文件到三级目录 Equity Research/{行业}/{股票代码}/

用法: python migrate_equity_research.py [--dry-run]
"""

from __future__ import annotations

import argparse
import sqlite3
import time
from typing import Any

import check_reports
from classifier import extract_text, classify_report, update_metadata, move_report, update_path


def get_equity_to_migrate() -> list[dict[str, Any]]:
    """返回 level1='Equity Research' 且 level3 为空（旧标准）的已下载记录。"""
    with sqlite3.connect(str(check_reports.DB_PATH)) as conn:
        rows = conn.execute(
            "SELECT media_id, title, path, author, report_date FROM reports "
            "WHERE level1 = 'Equity Research' AND level3 = '' AND downloaded_ts > 0 "
            "ORDER BY created_ts DESC"
        ).fetchall()
    cols = ["media_id", "title", "path", "author", "report_date"]
    return [dict(zip(cols, r)) for r in rows]


def main() -> None:
    parser = argparse.ArgumentParser(description="迁移历史 Equity Research 研报到三级分类")
    parser.add_argument("--dry-run", action="store_true", help="只打印分类结果，不写库/不移动文件")
    args = parser.parse_args()

    check_reports.init_db()
    items = get_equity_to_migrate()
    if not items:
        print("没有需要迁移的 Equity Research 研报（level3 均已填充）")
        return

    print(f"待迁移: {len(items)} 条\n")
    migrated = 0
    failed = 0

    for i, item in enumerate(items, start=1):
        media_id = item["media_id"]
        title = item["title"]
        src_dir = check_reports._resolve_path(item["path"])
        src_file = src_dir / title

        print(f"[{i}/{len(items)}] {title}")

        if not src_file.exists():
            print("  ✗ 文件不存在，跳过")
            failed += 1
            continue

        text = extract_text(src_file)
        result = classify_report(title, text)
        if result is None:
            print("  ✗ 分类失败，跳过")
            failed += 1
            continue

        level1, level2, level3, priority = result
        if level1 != "Equity Research":
            print(f"  ⚠ 分类结果非 Equity Research（{level1}），跳过")
            failed += 1
            continue

        print(f"  → {level1} / {level2} / {level3} ({priority})")

        if args.dry_run:
            migrated += 1
            continue

        # 更新 DB（author/report_date 沿用现有值）
        update_metadata(check_reports.DB_PATH, media_id, item["author"], item["report_date"], level1, level2, level3, priority)

        # 移动文件到三级目录
        new_rel_path = move_report(check_reports.CATEGORIZED_ROOT, media_id, title, src_dir, level1, level2, level3)
        if new_rel_path is None:
            print("  ✗ 移动失败")
            failed += 1
            continue

        update_path(check_reports.DB_PATH, media_id, new_rel_path)
        print(f"  ✓ 已迁移 → {new_rel_path}")
        migrated += 1

        time.sleep(0.2)  # 温和限速

    print(f"\n—— 完成 —— 迁移 {migrated} 条，失败/跳过 {failed}")


if __name__ == "__main__":
    main()
