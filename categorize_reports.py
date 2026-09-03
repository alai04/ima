"""使用 LLM（DeepSeek）对研报进行分类，并移动到分类目录。

流程：
  1. 遍历 reports 表中 path 为缺省值 'downloaded_reports' 的记录
  2. 阅读研报内容，用 LLM 输出分类（Equity Research 为三级，其余两级）
  3. 按分类生成新路径 categorized_reports/{一级}/{二级}/[{三级}/]，移动文件并更新 path 字段

用法: python categorize_reports.py [--dry-run] [-n N]
"""

import argparse
import sqlite3
import time
from typing import Any

import check_reports
from classifier import DEEPSEEK_API_KEY, classify_one_report

DEFAULT_PATH = "downloaded_reports"
CATEGORIZED_ROOT = check_reports.PROJECT_DIR / "categorized_reports"


def get_default_reports() -> list[dict[str, Any]]:
    """返回 path 为缺省值且已下载的记录，按创建时间从近到远排序。"""
    with sqlite3.connect(str(check_reports.DB_PATH)) as conn:
        rows = conn.execute(
            "SELECT media_id, title, path FROM reports "
            "WHERE path = ? AND downloaded_ts > 0 "
            "ORDER BY created_ts DESC",
            (DEFAULT_PATH,),
        ).fetchall()
    return [{"media_id": r[0], "title": r[1], "path": r[2]} for r in rows]


def main() -> None:
    parser = argparse.ArgumentParser(description="使用 LLM 对研报进行分类")
    parser.add_argument("--dry-run", action="store_true", help="只打印分类结果，不移动文件")
    parser.add_argument("-n", "--limit", type=int, default=0, help="最多处理 N 条（0 表示全部）")
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

        print(f"[{i}/{len(reports)}] {title}")

        ok, _ = classify_one_report(
            check_reports.DB_PATH, CATEGORIZED_ROOT, media_id, title, src_dir,
            dry_run=args.dry_run,
        )
        if ok:
            classified += 1
        else:
            failed += 1

        time.sleep(0.2)  # 温和限速

    print("\n—— 完成 ——")
    print(f"  成功分类: {classified}")
    print(f"  失败/跳过: {failed}")


if __name__ == "__main__":
    main()
