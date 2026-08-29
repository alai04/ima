"""从 zip 压缩包中提取 PDF 研报并标记为已下载。

用法: python download_from_zip.py <zip文件路径>
"""

import sqlite3
import sys
import zipfile
from pathlib import Path

from dotenv import load_dotenv

import check_reports

load_dotenv()


def main() -> None:
    if len(sys.argv) < 2:
        print("用法: python download_from_zip.py <zip文件路径>")
        sys.exit(1)

    zip_path = Path(sys.argv[1])
    if not zip_path.exists():
        print(f"错误: 文件不存在 — {zip_path}")
        sys.exit(1)

    if not zipfile.is_zipfile(zip_path):
        print(f"错误: 不是有效的 zip 文件 — {zip_path}")
        sys.exit(1)

    skipped_no_record = 0
    skipped_downloaded = 0
    extracted = 0

    with zipfile.ZipFile(zip_path) as zf:
        pdf_members = [
            info for info in zf.infolist()
            if info.filename.lower().endswith(".pdf") and not info.is_dir()
        ]
        if not pdf_members:
            print("zip 中没有 PDF 文件")
            return

        print(f"zip 中共 {len(pdf_members)} 个 PDF 文件\n")

        for info in pdf_members:
            title = Path(info.filename).name.encode('cp437').decode('utf8')  # 去掉路径前缀，只取文件名，转换编码
            print(f"  → {title} ...", end=" ")

            # 查 DB（zip 内文件名可能被截断，对长文件名用 LIKE 模糊匹配）
            if len(title) < 25:
                title_pattern = title
            else:
                title_pattern = title[:15] + "%" + title[-10:]

            with sqlite3.connect(str(check_reports.DB_PATH)) as conn:
                row = conn.execute(
                    "SELECT media_id, title, downloaded_ts, path FROM reports WHERE title LIKE ?",
                    (title_pattern,),
                ).fetchone()

            if row is None:
                print("跳过（DB 中无记录）")
                skipped_no_record += 1
                continue

            media_id, db_title, downloaded_ts, db_path = row
            if downloaded_ts > 0:
                print("跳过（已下载）")
                skipped_downloaded += 1
                continue

            # 用 DB 中的 path 字段指定保存目录
            out_dir = check_reports._resolve_path(db_path)
            out_dir.mkdir(parents=True, exist_ok=True)

            # 直接读取 zip 内容写入目标文件（避免 zf.extract 创建深层路径导致 ENAMETOOLONG）
            dest = out_dir / db_title
            try:
                with zf.open(info) as source, open(dest, "wb") as target:
                    target.write(source.read())
            except Exception as e:
                print(f"解压失败: {e}")
                continue

            check_reports.mark_downloaded(media_id)
            print("✓")
            extracted += 1

    print(f"\n—— 完成 ——")
    print(f"  解压并标记: {extracted}")
    print(f"  跳过(无记录): {skipped_no_record}")
    print(f"  跳过(已下载): {skipped_downloaded}")


if __name__ == "__main__":
    main()
