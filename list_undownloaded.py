"""列出所有未下载研报并通过 O365 邮件发送清单。"""

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from O365 import Account

import check_reports

load_dotenv()

LIST_EMAIL_TO = os.getenv("LIST_EMAIL_TO", "")


def _get_account() -> Account | None:
    """复用 check_reports 中的 O365 配置进行认证。"""
    client_id = check_reports.O365_CLIENT_ID
    client_secret = check_reports.O365_CLIENT_SECRET
    tenant_id = check_reports.O365_TENANT_ID

    if not client_id or not client_secret:
        print("[o365] 缺少 O365 配置")
        return None

    account = Account(
        (client_id, client_secret),
        auth_flow_type="credentials",
        tenant_id=tenant_id,
    )
    if account.authenticate():
        print("[o365] ✓ 认证成功")
        return account
    else:
        print("[o365] ✗ 认证失败")
        return None


def get_undownloaded() -> list[tuple[str, int]]:
    """返回 (title, created_ts) 列表，按 created_ts DESC 排序。"""
    with sqlite3.connect(str(check_reports.DB_PATH)) as conn:
        rows = conn.execute(
            "SELECT title, created_ts FROM reports "
            "WHERE downloaded_ts = 0 "
            "ORDER BY created_ts DESC"
        ).fetchall()
    return [(r[0], r[1]) for r in rows]


def build_table(rows: list[tuple[str, int]]) -> str:
    """生成 HTML 表格字符串。"""
    if not rows:
        return "<p>There are currently no undownloaded research reports.</p>"

    lines = [
        "<p>Due to limitations on the Tencent IMA platform, only a portion of the research reports can be automatically downloaded and sent each day.</p>",
        "<p>Below is a list of reports that were not downloaded; if you need any of them, please reply to this email listing the specific titles, and I will manually download and send them to you.</p>",
        "<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse; width:100%'>",
        "<thead>",
        "<tr style='background:#f5f5f5'>"
        "<th style='width:40px; text-align:center'>#</th>"
        "<th>Title of Report</th>"
        "<th style='width:160px; text-align:center'>Created time (UTC)</th>"
        "</tr>",
        "</thead>",
        "<tbody>",
    ]
    for i, (title, ts) in enumerate(rows, start=1):
        ts_str = (
            datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            if ts > 0
            else "N/A"
        )
        lines.append(
            f"<tr>"
            f"<td style='text-align:center; vertical-align:top'>{i}</td>"
            f"<td>{title}</td>"
            f"<td style='text-align:center; vertical-align:top; white-space:nowrap'>{ts_str}</td>"
            f"</tr>"
        )
    lines.append("</tbody></table>")
    lines.append(f"<p style='color:#888; font-size:12px'>Total: {len(rows)} undownloaded reports</p>")
    return "\n".join(lines)


def main() -> None:
    if not LIST_EMAIL_TO:
        print("错误: 缺少 LIST_EMAIL_TO 环境变量")
        return

    email_from = check_reports.EMAIL_FROM
    if not email_from:
        print("错误: 缺少 EMAIL_FROM 环境变量")
        return

    account = _get_account()
    if not account:
        return

    rows = get_undownloaded()
    print(f"未下载研报: {len(rows)} 条")
    for title, ts in rows:
        ts_str = (
            datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            if ts > 0
            else "N/A"
        )
        print(f"  {ts_str}  {title}")

    html_body = build_table(rows)

    try:
        m = account.new_message(resource=email_from)
        m.to.add(LIST_EMAIL_TO)
        m.subject = f"List of Undownloaded Research Reports ({len(rows)} reports)"
        m.body = html_body
        m.send()
        print(f"[sendmail] ✓ 已发送至 {LIST_EMAIL_TO}")
    except Exception as e:
        print(f"[sendmail] ✗ 发送失败: {e}")


if __name__ == "__main__":
    main()
