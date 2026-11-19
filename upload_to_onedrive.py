"""把已下载研报上传到 OneDrive（SharePoint 文档库），按撰写方分子文件夹。

流程：
  1. 查询 reports 表中已下载（downloaded_ts > 0）的研报，**默认只看最近 7 天**；
  2. 幂等检查（onedrive_ts > 0 视为已上传）；
  3. 按撰写方机构名确定子文件夹（如 ``环球研报/Barclays/``），
     不存在则新建；同名文件已存在则**覆盖**；
  4. 回写 DB：onedrive_ts / onedrive_item_id / onedrive_url。

追溯规则（与需求一致）：
  * 7 天内的研报可以补上传（``--days`` 可调，``--days 0`` 表示不限）；
  * 早于 7 天的研报不再追溯。

用法:
  python upload_to_onedrive.py                 # 补传最近 7 天
  python upload_to_onedrive.py --dry-run       # 只打印目标路径
  python upload_to_onedrive.py -n 5 --days 3   # 只处理最近 3 天中的 5 条
  python upload_to_onedrive.py --force         # 已上传的也重新上传（覆盖）
  python upload_to_onedrive.py --probe         # 连通性自检（列出目标文件夹）
"""

from __future__ import annotations

import argparse
import sqlite3
import time
from typing import Any

import check_reports
import onedrive
from metadata import parse_author


# ═══════════════════════════════════════════════════════════════════════
# Database
# ═══════════════════════════════════════════════════════════════════════


def get_reports_to_upload(days: int = 7, include_uploaded: bool = False) -> list[dict[str, Any]]:
    """返回待上传研报，按下载时间从近到远排序。

    ``days > 0`` 时只取 ``downloaded_ts`` 在最近 days 天内的记录；
    ``days <= 0`` 表示不限时间。``include_uploaded`` 为 True 时连已上传的
    也返回（供 --force 覆盖重传）。
    """
    where = "downloaded_ts > 0"
    params: list[Any] = []
    if days > 0:
        where += " AND downloaded_ts > ?"
        params.append(int(time.time()) - days * 24 * 3600)
    if not include_uploaded:
        where += " AND onedrive_ts = 0"

    with sqlite3.connect(str(check_reports.DB_PATH)) as conn:
        rows = conn.execute(
            "SELECT media_id, title, path, author, report_date, level1, onedrive_ts "
            f"FROM reports WHERE {where} ORDER BY downloaded_ts DESC",
            params,
        ).fetchall()

    cols = ["media_id", "title", "path", "author", "report_date", "level1", "onedrive_ts"]
    return [dict(zip(cols, r)) for r in rows]


def record_uploaded(media_id: str, result: dict[str, Any], ts: int | None = None) -> None:
    """回写上传结果（item id / webUrl / 时间戳）。"""
    ts = ts or int(time.time())
    with sqlite3.connect(str(check_reports.DB_PATH)) as conn:
        conn.execute(
            "UPDATE reports SET onedrive_ts = ?, onedrive_item_id = ?, onedrive_url = ? "
            "WHERE media_id = ?",
            (ts, result.get("id", ""), result.get("webUrl", ""), media_id),
        )
        conn.commit()


# ═══════════════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════════════


def main() -> None:
    parser = argparse.ArgumentParser(description="上传已下载研报到 OneDrive（按撰写方分文件夹）")
    parser.add_argument("--dry-run", action="store_true", help="只打印目标路径，不真正上传")
    parser.add_argument("-n", "--limit", type=int, default=0, help="最多处理 N 条（0=全部）")
    parser.add_argument(
        "--days", type=int, default=7, help="只补传最近 N 天下载的研报（0=不限，缺省 7）"
    )
    parser.add_argument("--force", action="store_true", help="已上传的记录也重新上传（覆盖）")
    parser.add_argument("--probe", action="store_true", help="只做连通性自检，不传文件")
    args = parser.parse_args()

    if not onedrive.ONEDRIVE_ENABLED:
        print("ONEDRIVE_ENABLED=false，跳过上传")
        return

    check_reports.init_db()

    if args.probe:
        info = onedrive.probe()
        print(f"站点: {info['site_id']}")
        print(f"文档库: {info['drive_id']}")
        print(f"根文件夹: {info['root']}（存在={info['root_exists']}）")
        print(f"现有子文件夹: {', '.join(info['folders']) or '(无)'}")
        return

    reports = get_reports_to_upload(days=args.days, include_uploaded=args.force)
    if args.limit > 0:
        reports = reports[: args.limit]

    window = "不限时间" if args.days <= 0 else f"最近 {args.days} 天"
    if not reports:
        print(f"没有需要上传的研报（范围：{window}）")
        return

    print(f"待上传研报: {len(reports)} 条（范围：{window}）")
    print(f"目标根文件夹: {onedrive.ONEDRIVE_ROOT_FOLDER}\n")

    uploaded = 0
    failed = 0

    for i, item in enumerate(reports, start=1):
        media_id = item["media_id"]
        title = item["title"]
        author = item["author"] or parse_author(title)
        folder = onedrive.target_folder(author, title)

        print(f"[{i}/{len(reports)}] {title}")
        print(f"  → {folder}/{title}")

        if args.dry_run:
            uploaded += 1
            continue

        src_file = check_reports._resolve_path(item["path"]) / title
        if not src_file.exists():
            print("  ✗ 本地文件不存在，跳过")
            failed += 1
            continue

        try:
            result = onedrive.upload_report(title, author, src_file.read_bytes())
            record_uploaded(media_id, result)
            print(f"  ✓ 已上传 ({result.get('size', 0) // 1024} KB)")
            uploaded += 1
        except onedrive.OneDriveError as e:
            print(f"  ✗ OneDrive 错误: {e}")
            failed += 1
        except Exception as e:
            print(f"  ✗ 失败: {e}")
            failed += 1

    print("\n—— 完成 ——")
    print(f"  成功: {uploaded}")
    print(f"  失败: {failed}")


if __name__ == "__main__":
    main()
