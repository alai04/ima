"""上传研报到 SharePoint 文档库并写入元数据。

流程：
  1. 查询 reports 表中已下载、已分类（level1 非空）的研报
  2. 幂等检查（远端 MediaId 是否已存在）
  3. 上传 PDF 到文档库根目录
  4. PATCH listItem/fields 写元数据
  5. 回写 DB：sharepoint_item_id / sharepoint_url / sharepoint_ts

幂等设计（安全重跑）：
  - 上传成功后先落 sharepoint_item_id（sharepoint_ts 仍为 0），再写元数据；
    若元数据写入失败，下次运行直接走「补写元数据」分支，不重复上传文件。
  - 远端已存在同 MediaId 文件时补标记跳过，不重复上传。

用法:
  python upload_to_sharepoint.py [--dry-run] [-n N] [--force]
"""

from __future__ import annotations

import argparse
import sqlite3
import time
from typing import Any

import check_reports
import sharepoint
from metadata import build_fields


# ═══════════════════════════════════════════════════════════════════════
# Database
# ═══════════════════════════════════════════════════════════════════════


def get_reports_to_upload(include_uploaded: bool = False) -> list[dict[str, Any]]:
    """返回待处理研报，按 created_ts 降序。

    include_uploaded=False 只取未上传（sharepoint_ts=0）；
    True 时取全部已分类记录（供 --force 重写元数据）。
    """
    where = "downloaded_ts > 0 AND level1 != ''"
    if not include_uploaded:
        where += " AND sharepoint_ts = 0"
    with sqlite3.connect(str(check_reports.DB_PATH)) as conn:
        rows = conn.execute(
            "SELECT media_id, title, path, author, report_date, level1, level2, level3, priority, "
            "source_kb, sharepoint_ts, sharepoint_item_id "
            f"FROM reports WHERE {where} ORDER BY created_ts DESC"
        ).fetchall()
    cols = [
        "media_id", "title", "path", "author", "report_date", "level1",
        "level2", "level3", "priority", "source_kb", "sharepoint_ts", "sharepoint_item_id",
    ]
    return [dict(zip(cols, r)) for r in rows]


def record_item_id(media_id: str, item_id: str, web_url: str) -> None:
    """落远端 item 标识（文件已上传，元数据待写）。"""
    with sqlite3.connect(str(check_reports.DB_PATH)) as conn:
        conn.execute(
            "UPDATE reports SET sharepoint_item_id = ?, sharepoint_url = ? WHERE media_id = ?",
            (item_id, web_url, media_id),
        )
        conn.commit()


def mark_uploaded(media_id: str, ts: int | None = None) -> None:
    """标记上传完成（写 sharepoint_ts）。"""
    ts = ts or int(time.time())
    with sqlite3.connect(str(check_reports.DB_PATH)) as conn:
        conn.execute("UPDATE reports SET sharepoint_ts = ? WHERE media_id = ?", (ts, media_id))
        conn.commit()


# ═══════════════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════════════


def main() -> None:
    parser = argparse.ArgumentParser(description="上传研报到 SharePoint 文档库")
    parser.add_argument("--dry-run", action="store_true", help="只打印将上传/写入的字段，不真正上传")
    parser.add_argument("-n", "--limit", type=int, default=0, help="最多处理 N 条（0=全部）")
    parser.add_argument("--force", action="store_true", help="重写已上传记录的元数据")
    args = parser.parse_args()

    if not sharepoint.SHAREPOINT_ENABLED:
        print("SHAREPOINT_ENABLED=false，跳过上传")
        return

    check_reports.init_db()

    reports = get_reports_to_upload(include_uploaded=args.force)
    if args.limit > 0:
        reports = reports[: args.limit]

    if not reports:
        print("没有需要上传的研报")
        return

    print(f"待处理研报: {len(reports)} 条\n")

    uploaded = 0
    skipped = 0
    failed = 0

    for i, item in enumerate(reports, start=1):
        media_id = item["media_id"]
        title = item["title"]
        fields = build_fields(item)

        print(f"[{i}/{len(reports)}] {title}")

        if args.dry_run:
            print(f"  → 字段: {fields}")
            uploaded += 1
            continue

        try:
            # 分支1：文件已在远端（上次上传成功），仅补写元数据
            if item["sharepoint_item_id"]:
                sharepoint.set_fields(item["sharepoint_item_id"], fields)
                mark_uploaded(media_id)
                print(f"  ✓ 已补写元数据 (item={item['sharepoint_item_id']})")
                uploaded += 1
                continue

            # 分支2：远端已存在同 MediaId → 补标记跳过 / force 时重写元数据
            existing = sharepoint.find_by_media_id(media_id)
            if existing:
                drive_item_id = existing.get("drive_item_id", "")
                web_url = existing.get("webUrl", "")
                if not drive_item_id:
                    print("  ✗ 无法定位远端 drive item，跳过")
                    failed += 1
                    continue
                if args.force:
                    sharepoint.set_fields(drive_item_id, fields)
                    record_item_id(media_id, drive_item_id, web_url)
                    mark_uploaded(media_id)
                    print("  ✓ 已重写元数据")
                    uploaded += 1
                else:
                    record_item_id(media_id, drive_item_id, web_url)
                    mark_uploaded(media_id)
                    print("  ⏭ 远端已存在，补标记跳过")
                    skipped += 1
                continue

            # 分支3：全新上传 + 写元数据
            src_file = check_reports._resolve_path(item["path"]) / title
            if not src_file.exists():
                print("  ✗ 本地文件不存在，跳过")
                failed += 1
                continue

            result = sharepoint.upload_file(title, src_file.read_bytes())
            drive_item_id = result.get("id", "")
            web_url = result.get("webUrl", "")
            if not drive_item_id:
                print(f"  ✗ 上传响应缺少 id: {result}")
                failed += 1
                continue

            record_item_id(media_id, drive_item_id, web_url)
            sharepoint.set_fields(drive_item_id, fields)
            mark_uploaded(media_id)
            print("  ✓ 已上传")
            uploaded += 1

        except sharepoint.SharePointError as e:
            print(f"  ✗ SharePoint 错误: {e}")
            failed += 1
        except Exception as e:
            print(f"  ✗ 失败: {e}")
            failed += 1

    print("\n—— 完成 ——")
    print(f"  成功: {uploaded}")
    print(f"  跳过: {skipped}")
    print(f"  失败: {failed}")


if __name__ == "__main__":
    main()
