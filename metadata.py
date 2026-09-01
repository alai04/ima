"""研报元数据：券商映射、文件名解析、SharePoint 字段构建。

纯函数 + 常量，不依赖项目内其它模块，供
categorize_reports / upload_to_sharepoint / check_reports 共用。
"""

from __future__ import annotations

import re
from typing import Any

SOURCE_KB_DEFAULT = "环球研报直通车"
PRIORITY_DEFAULT = "Medium"

# ── 券商中英文映射表（可扩展，未命中保留原文） ─────────────────────────
BROKER_NAME_MAP: dict[str, str] = {
    "高盛": "Goldman Sachs",
    "伯恩斯坦": "Bernstein",
    "摩根士丹利": "Morgan Stanley",
    "大摩": "Morgan Stanley",
    "摩根大通": "J.P. Morgan",
    "美银": "BofA Securities",
    "美银美林": "BofA Securities",
    "美国银行": "BofA Securities",
    "花旗": "Citi",
    "瑞银": "UBS",
    "瑞信": "Credit Suisse",
    "巴克莱": "Barclays",
    "德意志银行": "Deutsche Bank",
    "德银": "Deutsche Bank",
    "汇丰": "HSBC",
    "野村": "Nomura",
    "大和": "Daiwa",
    "杰富瑞": "Jefferies",
    "麦格理": "Macquarie",
    "里昂": "CLSA",
    "中金": "CICC",
    "中信证券": "CITIC Securities",
    "华泰证券": "Huatai Securities",
    "国泰君安": "Guotai Junan",
    "申万宏源": "Shenwan Hongyuan",
    "海通证券": "Haitong Securities",
    "广发证券": "GF Securities",
    "招商证券": "China Merchants Securities",
}


def parse_author(title: str) -> str:
    """从文件名前缀解析中文券商名并映射为英文。未命中保留原文并告警。"""
    m = re.match(r"^([^-—]+)[-—]", title)
    if not m:
        return ""
    zh = m.group(1).strip()
    en = BROKER_NAME_MAP.get(zh)
    if en is None:
        print(f"[meta] 券商名未命中映射表，保留原文待补充: {zh}")
        return zh
    return en


def parse_date(title: str) -> str:
    """从文件名末尾 -yymmdd 解析撰写日期，返回 'YYYY-MM-DD'；无则返回空串。"""
    m = re.search(r"-(\d{6})\.pdf$", title, re.IGNORECASE)
    if not m:
        return ""
    yy, mm, dd = m.group(1)[:2], m.group(1)[2:4], m.group(1)[4:6]
    if not (1 <= int(mm) <= 12 and 1 <= int(dd) <= 31):
        return ""
    return f"20{yy}-{mm}-{dd}"


def build_fields(item: dict[str, Any]) -> dict[str, Any]:
    """从记录 dict 构建 SharePoint 元数据字段。空值省略，避免覆盖远端已有值。

    item 需含键：media_id / title / author / report_date / level1 / level2 /
    priority / source_kb。level1 为空时省略 Category1（未分类研报仍可上传，
    分类字段由后续 categorize + upload 补齐）。
    """
    fields: dict[str, Any] = {
        "Title": item["title"],
        "MediaId": item["media_id"],
        "SourceKB": item["source_kb"] or SOURCE_KB_DEFAULT,
        "Priority": item["priority"] or PRIORITY_DEFAULT,
    }
    if item["level1"]:
        fields["Category1"] = item["level1"]
    if item["level2"]:
        fields["Category2"] = item["level2"]
    if item["author"]:
        fields["ReportAuthor"] = item["author"]
    if item["report_date"]:
        fields["ReportDate"] = f"{item['report_date']}T00:00:00Z"
    return fields
