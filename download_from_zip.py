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

    out_dir = check_reports.DOWNLOAD_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    skipped_no_record = 0
    skipped_downloaded = 0
    extracted = 0

    with zipfile.ZipFile(zip_path) as zf:
        pdf_entries = [
            name for name in zf.namelist()
            if name.lower().endswith(".pdf") and not name.endswith("/")
        ]
        if not pdf_entries:
            print("zip 中没有 PDF 文件")
            return

        print(f"zip 中共 {len(pdf_entries)} 个 PDF 文件\n")

        for name in pdf_entries:
            title = Path(name).name.encode('cp437').decode('utf8')  # 去掉路径前缀，只取文件名，转换编码
            print(f"  → {title} ...", end=" ")

            # 查 DB
            with sqlite3.connect(str(check_reports.DB_PATH)) as conn:
                row = conn.execute(
                    "SELECT media_id, downloaded_ts FROM reports WHERE title = ?", (title,)
                ).fetchone()

            if row is None:
                print("跳过（DB 中无记录）")
                skipped_no_record += 1
                continue

            media_id, downloaded_ts = row
            if downloaded_ts > 0:
                print("跳过（已下载）")
                skipped_downloaded += 1
                continue

            # 解压
            dest = out_dir / title
            try:
                zf.extract(name, path=out_dir)
                # extract 会保留原始目录结构，需要移动到目标位置
                extracted_path = out_dir / name
                if extracted_path != dest:
                    extracted_path.rename(dest)
                    # 清理可能残留的空目录
                    parent = extracted_path.parent
                    while parent != out_dir and parent != parent.parent:
                        try:
                            parent.rmdir()
                        except OSError:
                            break
                        parent = parent.parent
            except Exception as e:
                print(f"解压失败: {e}")
                continue

            # 标记已下载
            check_reports.mark_downloaded(media_id)
            print("✓")
            extracted += 1

    print(f"\n—— 完成 ——")
    print(f"  解压并标记: {extracted}")
    print(f"  跳过(无记录): {skipped_no_record}")
    print(f"  跳过(已下载): {skipped_downloaded}")


if __name__ == "__main__":
    main()
