"""从 zip 压缩包中提取 PDF 研报，标记为已下载并立即分类。

流程（逐个文件）：
  1. 按标题在 DB 中定位记录（容忍空格/中英文标点差异与文件名截断）
  2. 解压写入该记录 path 字段指定的目录（不走 zf.extract，避免深层路径 ENAMETOOLONG）
  3. 标记 downloaded_ts
  4. 调用 LLM 分类：落库 author/report_date/level1/level2/level3/priority，
     移动文件到 categorized_reports/... 并更新 path 字段

用法: python download_from_zip.py <zip文件路径>
"""

import re
import sqlite3
import sys
import unicodedata
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

import check_reports
from classifier import DEEPSEEK_API_KEY, classify_one_report

load_dotenv()

CATEGORIZED_ROOT = check_reports.PROJECT_DIR / "categorized_reports"

# ── 标题匹配参数 ──────────────────────────────────────────────────────
INDEX_GRAM = 8            # 头索引片段长度（归一化后）
MIN_PREFIX_LEN = 20       # 判定"截断"关系所需的最短归一化长度

# 匹配类型（越靠前越精确）
MATCH_EXACT = "精确"
MATCH_NORMALIZED = "归一化"
MATCH_AFFIX = "前缀截断"

# 处理结果计数键（统计输出顺序即此顺序）
OUTCOME_EXTRACTED = "解压并标记"
OUTCOME_CLASSIFIED = "分类成功"
OUTCOME_CLASSIFY_FAILED = "分类失败"
OUTCOME_FUZZY_MATCH = "其中模糊匹配命中"
OUTCOME_NO_RECORD = "跳过(无记录)"
OUTCOME_DOWNLOADED = "跳过(已下载)"
OUTCOME_EXTRACT_FAILED = "跳过(解压失败)"

OUTCOME_ORDER = (
    OUTCOME_EXTRACTED,
    OUTCOME_CLASSIFIED,
    OUTCOME_CLASSIFY_FAILED,
    OUTCOME_FUZZY_MATCH,
    OUTCOME_NO_RECORD,
    OUTCOME_DOWNLOADED,
    OUTCOME_EXTRACT_FAILED,
)


@dataclass(frozen=True)
class ReportRecord:
    """reports 表中与解压匹配相关的字段。"""

    media_id: str
    title: str
    downloaded_ts: int
    path: str


_EXT_RE = re.compile(r"\.(pdf|zip)$", re.IGNORECASE)


def _normalize_title(title: str) -> str:
    """标题归一化：去扩展名 → NFKC（全角→半角）→ casefold → 只保留字母/数字/汉字。

    借此抹平空格、半角/全角括号与冒号、中英文标点、连字符等差异，
    例如「高盛-英伟达(NVDA.US)：要点」与「高盛-英伟达（NVDA.US）: 要点」归一化后一致。
    去掉扩展名是为了让「名字被截断后补 .pdf」的文件名也能与其完整标题对齐前缀。
    """
    folded = unicodedata.normalize("NFKC", _EXT_RE.sub("", title)).casefold()
    return "".join(ch for ch in folded if unicodedata.category(ch)[0] in ("L", "N"))


def _is_truncation(left: str, right: str) -> bool:
    """判断两个归一化标题是否存在截断关系：任一方是另一方的前缀，且前缀足够长。

    只用前缀、不用尾段——同券商同日的研报尾部（日期 + 「维持买入评级」等套话）
    高度雷同，尾段匹配会把不同研报相互错配（实测错配率 >10%）。
    """
    if len(left) >= MIN_PREFIX_LEN and right.startswith(left):
        return True
    return len(right) >= MIN_PREFIX_LEN and left.startswith(right)


class TitleIndex:
    """标题 → DB 记录索引，容忍标点/空白差异与文件名截断。

    匹配优先级：原始标题 → 归一化标题 → 前缀截断。
    非精确匹配会在调用处打印 DB 标题以便人工核对。

    不做相似度（模糊比率）匹配：实测同一券商同股同日的近似标题之间相似度可达
    0.98，任何可用阈值都会把 zip 名静默绑定到错误记录，代价远高于匹配不上时
    回落到「跳过(无记录)」。
    """

    def __init__(self, records: list[ReportRecord]) -> None:
        self._exact: dict[str, ReportRecord] = {}
        self._normalized: dict[str, ReportRecord] = {}
        self._by_head: dict[str, list[str]] = {}
        for record in records:
            self._exact.setdefault(record.title, record)
            key = _normalize_title(record.title)
            if not key:
                continue
            self._normalized.setdefault(key, record)
            self._by_head.setdefault(key[:INDEX_GRAM], []).append(key)

    def find(self, zip_title: str) -> tuple[ReportRecord, str] | None:
        """定位记录，返回 (记录, 匹配类型)；无命中返回 None。"""
        record = self._exact.get(zip_title)
        if record is not None:
            return record, MATCH_EXACT

        key = _normalize_title(zip_title)
        if not key:
            return None

        record = self._normalized.get(key)
        if record is not None:
            return record, MATCH_NORMALIZED

        # 按头片段取候选，避免全表两两比较
        candidates = [c for c in self._by_head.get(key[:INDEX_GRAM], []) if _is_truncation(key, c)]
        if candidates:
            # 多个候选共享同一前缀时为截断歧义，取最短者（多余内容最少），保证结果确定
            return self._normalized[min(candidates, key=len)], MATCH_AFFIX

        return None


def _load_records() -> list[ReportRecord]:
    """读取全部记录；未下载的排在前面，同名重复记录优先命中待下载条目。"""
    with sqlite3.connect(str(check_reports.DB_PATH)) as conn:
        rows = conn.execute(
            "SELECT media_id, title, downloaded_ts, path FROM reports"
        ).fetchall()

    records = [
        ReportRecord(media_id=r[0], title=r[1], downloaded_ts=r[2], path=r[3])
        for r in rows
    ]
    records.sort(key=lambda r: r.downloaded_ts > 0)
    return records


def _fix_filename(name: str) -> str:
    """还原被 cp437 误解码的 UTF-8 zip 文件名；无法还原时返回原名。"""
    try:
        return name.encode('cp437').decode('utf-8')
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


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

    records = _load_records()
    index = TitleIndex(records)
    downloaded_ids = {r.media_id for r in records if r.downloaded_ts > 0}

    stats: Counter[str] = Counter()

    with zipfile.ZipFile(zip_path) as zf:
        pdf_members = [
            info for info in zf.infolist()
            if info.filename.lower().endswith(".pdf") and not info.is_dir()
        ]
        if not pdf_members:
            print("zip 中没有 PDF 文件")
            return

        print(f"zip 中共 {len(pdf_members)} 个 PDF 文件（DB 记录 {len(records)} 条）\n")

        for info in pdf_members:
            title = Path(_fix_filename(info.filename)).name  # 去掉路径前缀，只取文件名，处理编码
            print(f"  → {title} ...", end=" ")

            match = index.find(title)
            if match is None:
                print("跳过（DB 中无记录）")
                stats[OUTCOME_NO_RECORD] += 1
                continue

            record, match_kind = match
            if match_kind != MATCH_EXACT:
                stats[OUTCOME_FUZZY_MATCH] += 1
                note = f"（{match_kind}匹配 → {record.title}）"
            else:
                note = ""

            if record.media_id in downloaded_ids:
                print(f"跳过（已下载）{note}")
                stats[OUTCOME_DOWNLOADED] += 1
                continue

            # 用 DB 中的 path 字段指定保存目录，文件名用 DB 标题
            out_dir = check_reports._resolve_path(record.path)
            out_dir.mkdir(parents=True, exist_ok=True)

            dest = out_dir / record.title
            if not _extract_member(zf, info, dest):
                stats[OUTCOME_EXTRACT_FAILED] += 1
                continue

            check_reports.mark_downloaded(record.media_id)
            downloaded_ids.add(record.media_id)  # 同一 zip 内的重复条目不再重复处理
            print(f"✓ 已解压并标记为已下载{note}")
            stats[OUTCOME_EXTRACTED] += 1

            # 解压完成即分类（失败不中断后续文件，文件保留在 downloaded_reports）
            if _classify_extracted(record.media_id, record.title, out_dir):
                stats[OUTCOME_CLASSIFIED] += 1
            else:
                stats[OUTCOME_CLASSIFY_FAILED] += 1

    print("\n—— 完成 ——")
    for outcome in OUTCOME_ORDER:
        if outcome == OUTCOME_EXTRACT_FAILED and not stats[outcome]:
            continue
        print(f"  {outcome}: {stats[outcome]}")


if __name__ == "__main__":
    main()
