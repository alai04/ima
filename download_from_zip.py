"""从 zip 压缩包中提取 PDF 研报，标记为已下载并立即分类。

流程（逐个文件）：
  1. 按标题在 DB 中定位记录
  2. 解压写入该记录 path 字段指定的目录（不走 zf.extract，避免深层路径 ENAMETOOLONG）
  3. 标记 downloaded_ts
  4. 调用 LLM 分类：落库 author/report_date/level1/level2/level3/priority，
     移动文件到 categorized_reports/... 并更新 path 字段

用法: python download_from_zip.py <zip文件路径>
"""

import sqlite3
import sys
import zipfile
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv

import check_reports
from classifier import DEEPSEEK_API_KEY, classify_one_report

load_dotenv()

CATEGORIZED_ROOT = check_reports.PROJECT_DIR / "categorized_reports"

# 处理结果计数键（统计输出顺序即此顺序）
OUTCOME_EXTRACTED = "解压并标记"
OUTCOME_CLASSIFIED = "分类成功"
OUTCOME_CLASSIFY_FAILED = "分类失败"
OUTCOME_NO_RECORD = "跳过(无记录)"
OUTCOME_DOWNLOADED = "跳过(已下载)"
OUTCOME_EXTRACT_FAILED = "跳过(解压失败)"

OUTCOME_ORDER = (
    OUTCOME_EXTRACTED,
    OUTCOME_CLASSIFIED,
    OUTCOME_CLASSIFY_FAILED,
    OUTCOME_NO_RECORD,
    OUTCOME_DOWNLOADED,
    OUTCOME_EXTRACT_FAILED,
)


def _fix_filename(name: str) -> str:
    """还原被 cp437 误解码的 UTF-8 zip 文件名；无法还原时返回原名。"""
    try:
        return name.encode('cp437').decode('utf-8')
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


def _find_db_record(title: str) -> tuple[str, str, int, str] | None:
    """按文件名定位 DB 记录（media_id, title, downloaded_ts, path）。

    zip 内文件名可能被截断，长文件名用 LIKE 首尾片段模糊匹配。
    """
    pattern = title if len(title) < 25 else f"{title[:15]}%{title[-10:]}"
    with sqlite3.connect(str(check_reports.DB_PATH)) as conn:
        row = conn.execute(
            "SELECT media_id, title, downloaded_ts, path FROM reports WHERE title LIKE ?",
            (pattern,),
        ).fetchone()
    return row


def _extract_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo, dest: Path) -> bool:
    """把 zip 内部条目写出到 dest。成功返回 True。"""
    try:
        with zf.open(info) as source, open(dest, "wb") as target:
            target.write(source.read())
    except (OSError, zipfile.BadZipFile, RuntimeError) as e:
        print(f"✗ 解压失败: {e}")
        return False
    return True


def _classify_extracted(media_id: str, title: str, src_dir: Path) -> bool:
    """解压完成后立即分类：落库元数据、移动文件并更新 path 字段。"""
    ok, _ = classify_one_report(
        check_reports.DB_PATH, CATEGORIZED_ROOT, media_id, title, src_dir,
    )
    return ok


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

    if not DEEPSEEK_API_KEY:
        print("[warn] 缺少 DEEPSEEK_API_KEY，解压后无法分类（文件将保留在 downloaded_reports）")

    stats: Counter[str] = Counter()

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
            title = Path(_fix_filename(info.filename)).name  # 去掉路径前缀，只取文件名，处理编码
            print(f"  → {title} ...", end=" ")

            record = _find_db_record(title)
            if record is None:
                print("跳过（DB 中无记录）")
                stats[OUTCOME_NO_RECORD] += 1
                continue

            media_id, db_title, downloaded_ts, db_path = record
            if downloaded_ts > 0:
                print("跳过（已下载）")
                stats[OUTCOME_DOWNLOADED] += 1
                continue

            # 用 DB 中的 path 字段指定保存目录
            out_dir = check_reports._resolve_path(db_path)
            out_dir.mkdir(parents=True, exist_ok=True)

            dest = out_dir / db_title
            if not _extract_member(zf, info, dest):
                stats[OUTCOME_EXTRACT_FAILED] += 1
                continue

            check_reports.mark_downloaded(media_id)
            print("✓ 已解压并标记为已下载")
            stats[OUTCOME_EXTRACTED] += 1

            # 解压完成即分类（失败不中断后续文件，文件保留在 downloaded_reports）
            if _classify_extracted(media_id, db_title, out_dir):
                stats[OUTCOME_CLASSIFIED] += 1
            else:
                stats[OUTCOME_CLASSIFY_FAILED] += 1

    print("\n—— 完成 ——")
    for outcome in OUTCOME_ORDER:
        if outcome in (OUTCOME_EXTRACT_FAILED,) and not stats[outcome]:
            continue
        print(f"  {outcome}: {stats[outcome]}")


if __name__ == "__main__":
    main()
