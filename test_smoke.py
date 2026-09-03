"""冒烟测试：import + 元数据解析 + DB 迁移 + build_fields（不触发 Graph API）。"""

import sqlite3

import check_reports
import metadata as md
import sharepoint
import upload_to_sharepoint as up


def main() -> None:
    # 1. 元数据解析（确定性函数）
    print("=== parse_author / parse_date ===")
    samples = [
        "高盛-欧洲市场周报：仅数据更新-260828.pdf",
        "伯恩斯坦-比亚迪（002594.SZ）二季度业绩实则强于表象-260828.pdf",
        "摩根士丹利-测试研报-260829.pdf",
        "中金-测试研报-260801.pdf",
        "未知券商-无日期研报.pdf",
    ]
    for t in samples:
        print(f"  {t}\n    author={md.parse_author(t)!r}  date={md.parse_date(t)!r}")

    # 2. DB 迁移
    print("\n=== init_db 迁移 ===")
    check_reports.init_db()
    with sqlite3.connect(str(check_reports.DB_PATH)) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(reports)").fetchall()]
    required = [
        "author", "report_date", "level1", "level2", "level3", "priority",
        "source_kb", "sharepoint_ts", "sharepoint_item_id", "sharepoint_url",
    ]
    missing = [c for c in required if c not in cols]
    print(f"  reports 表列数: {len(cols)}")
    print("  缺失列:", missing or "无")

    # 3. build_fields
    print("\n=== build_fields ===")
    item = {
        "media_id": "pdf_test", "title": "高盛-测试-260828.pdf", "path": "downloaded_reports",
        "author": "Goldman Sachs", "report_date": "2026-08-28",
        "level1": "Macro & Strategy", "level2": "Europe", "level3": "", "priority": "Low",
        "source_kb": "环球研报直通车", "sharepoint_ts": 0, "sharepoint_item_id": "",
    }
    print("  ", up.build_fields(item))

    # 4. SharePoint 配置（只打印是否配置，不打印值）
    print("\n=== sharepoint 配置 ===")
    print("  enabled      =", sharepoint.SHAREPOINT_ENABLED)
    print("  tenant       =", "已配置" if sharepoint.SHAREPOINT_TENANT else "(未配置)")
    print("  site_path    =", "已配置" if sharepoint.SHAREPOINT_SITE_PATH else "(未配置)")
    print("  drive_name   =", sharepoint.SHAREPOINT_DRIVE_NAME)

    print("\n=== 冒烟测试通过 ===")


if __name__ == "__main__":
    main()
