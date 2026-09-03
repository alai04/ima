# 研报 SharePoint Team Site 共享方案设计

> 版本：v1.0　状态：待评审　关联项目：`~/projects/python/ima`
> 目标：在现有 IMA 研报自动下载流水线之上，增加「上传 SharePoint Team Site 并写入元数据」环节，使团队可在一个统一站点内**搜索、筛选、阅读**研报。

---

## 1. 背景与目标

### 1.1 现状

现有 `ima` 项目已实现一条自动化流水线：

```
IMA 知识库"环球研报直通车"
   │  search_knowledge（最近 3 天）
   ▼
SQLite reports 表（去重入库）
   │  get_media_info → 下载 PDF
   ▼
本地 downloaded_reports/
   │  DeepSeek LLM 两级分类
   ▼
categorized_reports/{一级}/{二级}/
   │  O365 (Graph) 邮件附件
   ▼
团队成员邮箱
```

现有 `reports` 表字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `media_id` | TEXT PK | IMA 唯一标识 |
| `title` | TEXT | 研报标题（文件名） |
| `downloaded_ts` | INTEGER | 下载时间戳，0=未下载 |
| `sendmail_ts` | INTEGER | 发信时间戳，0=未发送 |
| `created_ts` | INTEGER | 入库时间戳 |
| `path` | TEXT | 本地保存目录（缺省 `downloaded_reports`） |

### 1.2 痛点

- 研报以**邮件附件**分发，团队成员各自收件，无统一检索入口。
- 分类结果只体现在本地目录结构（`path`），未结构化成可检索的元数据。
- 无优先级概念，成员无法区分「当日必读」与「例行更新」。

### 1.3 目标

1. 建立 SharePoint **Team Site** + **文档库** 作为统一共享、检索、阅读入口。
2. 定期（随现有下载流水线）将新研报自动上传，并写入结构化元数据。
3. 元数据至少覆盖：**撰写方、撰写日期、一级/二级/三级分类、优先级**，且可扩展。
4. 团队成员通过 SharePoint 原生搜索 + 列筛选视图快速定位研报。

---

## 2. 目标架构

```
┌─────────────────────────── 现有（保持不变） ───────────────────────────┐
│  IMA 搜索 → SQLite 入库 → 下载 PDF → DeepSeek 元数据抽取 → 本地归档     │
└──────────────────────────────────┬────────────────────────────────────┘
                                   │ 新增：sharepoint_ts = 0 的研报
                                   ▼
┌─────────────────────────── 新增模块 upload_to_sharepoint ──────────────┐
│  读 DB 元数据 → Graph 上传 PDF → PATCH listItem/fields 写元数据          │
│  → 记录 sharepoint_item_id / sharepoint_url / sharepoint_ts             │
└──────────────────────────────────┬────────────────────────────────────┘
                                   ▼
                 SharePoint Team Site 文档库（含元数据列 + 视图）
                                   │
                                   ▼
                   团队成员：搜索 / 筛选 / 在线阅读 / 下载
```

**设计原则**

- 邮件分发**保留为可选项**（新研报提醒），SharePoint 成为主存储与检索入口，二者不冲突。
- 元数据在**本地 DB 完整落库**，上传模块只做「读 DB → 上传 → 写远端」，不重复调用 LLM。
- 上传与元数据写入**幂等**，可安全重跑。

---

## 3. SharePoint 侧设计（一次性配置）

### 3.1 Team Site 与文档库

| 项 | 建议值 |
|----|--------|
| Site 类型 | Communication Site 或 Team Site（无 Microsoft 365 组亦可，用 Communication Site 最简单） |
| Site 名称 | `Research Reports`（或团队自定义） |
| Site 路径 | `/sites/ResearchReports` |
| 文档库 | 默认 `Documents`，建议重命名为「研报库」并保留库名英文，例如 `Research Library` |
| 目录结构 | **不分文件夹**，所有研报直接平铺上传至文档库根目录；全部分类/券商/日期/优先级用列承载 |

> 采用「纯元数据」方案：所有研报平铺在文档库根目录，分类 / 券商 / 日期 / 优先级全部用列承载，靠视图与搜索筛选。这符合 SharePoint「元数据优于文件夹」的最佳实践，也避免了浏览时多进一层目录。

### 3.2 元数据列（文档库自定义列）

列在文档库级别创建。**内部名（`name`）一旦创建不可更改**（显示名 `displayName` 可随时改），务必按下方英文内部名一次建对。

| 内部名 (name) | 显示名 (displayName) | 列类型 | 取值/说明 |
|---------------|----------------------|--------|-----------|
| `Title` | 标题 | 文本（内置） | 直接存研报标题 |
| `ReportAuthor` | 撰写方 | 文本 | 券商名：高盛 / 伯恩斯坦 / 摩根士丹利… |
| `ReportDate` | 撰写日期 | 日期和时间（仅日期） | `YYYY-MM-DD`，从文件名末尾 `-yymmdd` 解析 |
| `Category1` | 一级分类 | 选项(Choice) | Equity Research / Macro & Strategy / Industry & Thematic / Others |
| `Category2` | 二级分类 | 文本 | 行业（Equity Research / Industry & Thematic）/ 地区（Macro & Strategy） |
| `Category3` | 三级分类 | 文本 | 股票代码（仅 Equity Research 使用） |
| `Priority` | 优先级 | 选项(Choice) | High / Medium / Low（默认 Medium） |
| `SourceKB` | 来源知识库 | 文本 | 固定 `环球研报直通车` |
| `MediaId` | 媒体 ID | 文本 | IMA `media_id`，用于幂等去重 |

**列创建方式（二选一，推荐 A）**

- **A. SharePoint Web UI 手动创建**（推荐，一次性、直观、团队成员可见）：文档库 → 「+ 添加列」逐列创建；选项列填 choices。**此方式脚本只需最小权限**（见 §4）。
- **B. Graph 编程创建**（可版本化、可重复部署，需 `Sites.Manage.All` 权限）：

```http
POST https://graph.microsoft.com/v1.0/sites/{site-id}/lists/{list-id}/columns
Content-Type: application/json

# 文本列
{ "name": "ReportAuthor", "displayName": "撰写方", "text": {} }

# 日期列
{ "name": "ReportDate", "displayName": "撰写日期", "dateTime": { "displayAs": "dateOnly" } }

# 选项列
{ "name": "Priority", "displayName": "优先级",
  "choice": { "choices": ["High", "Medium", "Low"], "allowTextEntry": false } }
```

### 3.3 视图（提升检索体验）

在文档库中创建以下**预设视图**，成员零成本筛选：

| 视图名 | 用途 | 筛选/分组/排序 |
|--------|------|----------------|
| 全部研报 | 默认列表 | 按 ReportDate 降序 |
| 高优先级 | 当日必读 | `Priority = High`，按 ReportDate 降序 |
| 按券商分组 | 快速定位某家 | 按 ReportAuthor 分组 |
| 按一级分类分组 | 与研究领域对齐 | 按 Category1 分组 |

> SharePoint 文档库的列默认会进入搜索索引；搜索时可用 KQL（如 `ReportAuthor:高盛 Priority:High`）精确过滤。

---

## 4. 认证与权限设计

沿用现有 Azure AD 应用（已用于发邮件），**新增 SharePoint 相关 Graph 权限**。

### 4.1 权限选型

| 方案 | 权限 | 说明 | 推荐度 |
|------|------|------|--------|
| 最小权限（细粒度） | `Sites.Selected`（Application） | 仅授予对**指定 site** 的访问，需管理员额外执行一次 site 授权；最安全 | ★★★ 生产推荐 |
| 简单权限 | `Sites.ReadWrite.All`（Application） | 全租户所有 site 读写；配置简单，权限面大 | ★★ 快速起步 |
| 建库/建列 | 额外 `Sites.Manage.All` | 仅当用 Graph 创建列/库时需要；用 Web UI 则不需要 | 按需 |

**推荐路径**：起步用 `Sites.ReadWrite.All` 跑通全流程；生产切换 `Sites.Selected`（把应用授权到单个 site，`POST /sites/{siteId}/permissions` 给 `write` 角色）。

### 4.2 Azure AD 应用注册变更

在现有应用（`O365_CLIENT_ID` / `O365_CLIENT_SECRET`）上，由管理员授予以下 **Application** 类型权限，并**代表全员同意（Admin Consent）**：

- `Sites.ReadWrite.All`（上传文件 + 写 list item 字段）
- （可选，细粒度替代）`Sites.Selected`

> 注意区分 Application 与 Delegated：脚本以 `client_credentials` 无用户身份运行，必须授予 **Application** 类型。

### 4.3 Token 获取（独立于邮件模块）

项目已依赖 `O365`，其传递依赖含 `msal`。为解耦 SharePoint 与邮件，**显式声明并直接使用 `msal`** 获取 token：

```python
# uv add msal   （显式声明传递依赖）
from msal import ConfidentialClientApplication

GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]  # 一次性拿全已授应用权限

def get_graph_token() -> str:
    app = ConfidentialClientApplication(
        client_id=O365_CLIENT_ID,
        client_credential=O365_CLIENT_SECRET,
        authority=f"https://login.microsoftonline.com/{O365_TENANT_ID}",
    )
    result = app.acquire_token_for_client(scopes=GRAPH_SCOPE)
    if "access_token" not in result:
        raise RuntimeError(f"获取 token 失败: {result.get('error_description', result)}")
    return result["access_token"]
```

> 复用 `.env` 中已有的 `O365_CLIENT_ID` / `O365_CLIENT_SECRET` / `O365_TENANT_ID`，不新增密钥。

---

## 5. 元数据设计

### 5.1 字段定义与来源

| 元数据 | 本地 DB 字段 | SharePoint 列 | 来源 | 抽取方式 |
|--------|-------------|---------------|------|----------|
| 撰写方 | `author` | `ReportAuthor` | 文件名前缀（中文） | 正则提取后经券商中英文映射表转英文（附录 C） |
| 撰写日期 | `report_date` | `ReportDate` | 文件名后缀 | 正则 `-(\d{6})\.pdf$` → `20yy-mm-dd` |
| 一级分类 | `level1` | `Category1` | LLM | 复用现有 DeepSeek 分类 |
| 二级分类 | `level2` | `Category2` | LLM | Equity Research/Industry & Thematic=行业；Macro & Strategy=地区 |
| 三级分类 | `level3` | `Category3` | LLM | 仅 Equity Research=股票代码 |
| 优先级 | `priority` | `Priority` | LLM | 并入分类 prompt 一次输出 |
| 来源知识库 | `source_kb` | `SourceKB` | 常量 | `环球研报直通车` |
| 媒体 ID | `media_id` | `MediaId` | IMA | 已有字段直接映射 |

### 5.2 文件名解析规则（确定性、零成本）

文件名高度规整，示例：

```
高盛-欧洲市场周报：仅数据更新-260828.pdf      → author=高盛, date=2026-08-28
伯恩斯坦-比亚迪（002594.SZ）二季度业绩…-260828.pdf → author=伯恩斯坦, date=2026-08-28
```

```python
import re

BROKER_NAME_MAP: dict[str, str] = {
    "高盛": "Goldman Sachs",
    "伯恩斯坦": "Bernstein",
    "摩根士丹利": "Morgan Stanley",
    # ... 完整对照见附录 C
}

def parse_author(title: str) -> str:
    m = re.match(r"^([^-—]+)[-—]", title)
    if not m:
        return ""
    zh = m.group(1).strip()
    en = BROKER_NAME_MAP.get(zh)
    if en is None:
        print(f"[meta] 券商名未命中映射表，保留原文待补充: {zh}")
        return zh
    return en

def parse_date(title: str) -> str | None:
    m = re.search(r"-(\d{6})\.pdf$", title)
    if not m:
        return None
    yy, mm, dd = m.group(1)[:2], m.group(1)[2:4], m.group(1)[4:6]
    try:
        return f"20{yy}-{mm}-{dd}"
    except ValueError:
        return None
```

> 解析失败时回退：券商名未命中映射表 → 保留中文原文并告警（待补充附录 C）；无日期 → date 用 `created_ts` 对应日期；文件名无券商前缀 → author 留空。

### 5.3 优先级判定（并入分类 LLM prompt）

扩展 `categorize_reports.py` 的 system prompt，令 LLM 一次输出 `{level1, level2, level3, priority}`：

| 优先级 | 判定标准（给 LLM 的提示） |
|--------|--------------------------|
| High | 评级/目标价变动、首次覆盖、业绩大幅超/低于预期、重大宏观事件 |
| Medium | 常规业绩点评、行业/策略例行更新 |
| Low | 数据更新、例行周报、会议纪要 |

> 优先级字段类型为 Choice，若后续想人工调整，成员直接在 SharePoint 列表编辑列值即可，脚本重跑不会覆盖（见 §8.4 幂等策略）。

---

## 6. 数据库变更（reports 表扩展）

在 `init_db()` 中新增以下列（沿用现有 try/except `ALTER TABLE` 迁移模式）：

| 列名 | 类型 | 缺省 | 说明 |
|------|------|------|------|
| `author` | TEXT | `''` | 撰写方 |
| `report_date` | TEXT | `''` | `YYYY-MM-DD` |
| `level1` | TEXT | `''` | 一级分类 |
| `level2` | TEXT | `''` | 二级分类（行业/地区） |
| `level3` | TEXT | `''` | 三级分类（仅 Equity Research 股票代码） |
| `priority` | TEXT | `'Medium'` | High/Medium/Low |
| `source_kb` | TEXT | `'环球研报直通车'` | 来源 |
| `sharepoint_ts` | INTEGER | `0` | 上传时间戳，0=未上传 |
| `sharepoint_item_id` | TEXT | `''` | 远端 listItem id（溯源） |
| `sharepoint_url` | TEXT | `''` | 远端文件 webUrl（溯源） |

**变更影响**：

- `categorize_reports.py`：分类结果除移动目录外，**同步写 `level1`/`level2`/`level3`/`priority`/`author`/`report_date` 字段**（`author`/`report_date` 用 §5.2 正则，`priority` 用 LLM）。
- 其余脚本（`check_reports.py` 下载/发信）不改逻辑，仅 `init_db` 增加迁移。

---

## 7. 上传流程设计

### 7.1 流程

```
查询 reports 表：downloaded_ts > 0 AND sharepoint_ts = 0 AND level1 != ''
   （只上传已分类、已下载、未上传的研报，按 created_ts DESC）
   │
   ├─ 1. 解析远端定位：get_site_id() → get_drive_id("Research Library")
   ├─ 2. 幂等检查：远端是否已存在同名文件？已存在且 MediaId 匹配 → 标记跳过
   ├─ 3. 上传：≤250MB 简单 PUT；>250MB createUploadSession 分片
   ├─ 4. 写元数据：PATCH .../items/{itemId}/listItem/fields
   └─ 5. 回写 DB：sharepoint_ts / sharepoint_item_id / sharepoint_url
```

### 7.2 Graph API 端点（v1.0）

**（1）解析 site**

```http
GET https://graph.microsoft.com/v1.0/sites/{tenant}.sharepoint.com:/sites/ResearchReports
# 或按名称搜索：GET /sites?search=ResearchReports
```

**（2）解析文档库 drive**

```http
GET https://graph.microsoft.com/v1.0/sites/{site-id}/drives
# 找到 name == "Research Library" 的 drive id
```

**（3）上传文件（≤250MB，简单 PUT）**

```http
PUT https://graph.microsoft.com/v1.0/sites/{site-id}/drives/{drive-id}/root:/{filename}:/content
Content-Type: application/pdf
# Body = PDF 二进制
```

- `{filename}` 含中文，**必须 URL 编码**（httpx 中用 `quote(filename, safe="")`）。
- 返回 `driveItem`，含 `id`（drive 命名空间）与 `webUrl`。

**（4）上传大文件（>250MB，上传会话）**

```http
POST https://graph.microsoft.com/v1.0/sites/{site-id}/drives/{drive-id}/root:/{filename}:/createUploadSession
Content-Type: application/json
{ "item": { "@microsoft.graph.conflictBehavior": "replace" } }
# 返回 uploadUrl，按字节范围 PUT 分片上传
```

**（5）写入元数据（关键步骤）**

```http
PATCH https://graph.microsoft.com/v1.0/sites/{site-id}/drives/{drive-id}/items/{itemId}/listItem/fields
Content-Type: application/json
{
  "Title": "高盛-欧洲市场周报：仅数据更新-260828",
  "ReportAuthor": "Goldman Sachs",
  "ReportDate": "2026-08-28T00:00:00",
  "Category1": "Macro & Strategy",
  "Category2": "Europe",
  "Category3": "",        // 仅 Equity Research 填股票代码（如 "AAPL"）
  "Priority": "Low",
  "SourceKB": "环球研报直通车",
  "MediaId": "pdf_19ee…"
}
```

> **核心坑**：Graph 的 `PUT …/content` 上传只写文件内容，**不会写库的元数据列**。必须紧跟一次 `PATCH listItem/fields` 才能落元数据。这是本方案最关键的一步，也是方案正确性的基石。

### 7.3 冲突处理

- 上传时 `@microsoft.graph.conflictBehavior`：
  - 默认 `fail`（同名冲突返回 409）——稳妥，用于幂等检测。
  - 需要覆盖更新时用 `replace`（会生成新版本，旧版本保留在版本历史）。

### 7.4 幂等与重试

| 策略 | 说明 |
|------|------|
| 本地去重 | `sharepoint_ts > 0` 跳过，重跑安全 |
| 远端去重 | 上传前用 `filter=fields/MediaId eq '{media_id}'` 查询 list 是否已存在；存在则只补 PATCH 字段、不重复上传 |
| 失败重试 | 上传/写字段抛异常时**不写 `sharepoint_ts`**，下次运行自动重试 |
| 部分成功 | 文件已上传但字段写入失败 → 记录 `sharepoint_item_id` 但 `sharepoint_ts` 仍为 0 的分支，下轮只补 PATCH 字段 |

---

## 8. 模块与脚本设计

### 8.1 目录结构（在 `ima` 项目内新增）

```
ima/
├── check_reports.py            # 现有：搜索/下载/分类/发信（可选内联上传 SharePoint + 清单邮件）
├── categorize_reports.py       # 改造：批量分类 CLI（复用 classifier）
├── classifier.py               # 新增：PDF 提取 + DeepSeek 分类 + 落库 + 移动（共享，无项目内依赖）
├── metadata.py                 # 新增：券商映射 / 文件名解析 / SharePoint 字段构建（共享）
├── backfill_metadata.py        # 新增：历史已分类研报从 path 反推并回填元数据
├── migrate_equity_research.py  # 新增：历史 Equity Research 迁移到三级分类
├── sharepoint.py               # 新增：Graph 认证 + 上传 + 元数据写入 + site_url 的封装
├── upload_to_sharepoint.py     # 新增：上传 CLI（读 DB → 上传 → 写字段 → 回写）
└── docs/
    └── sharepoint-team-site-plan.md   # 本文档
```

### 8.2 `sharepoint.py`（职责单一）

封装为可复用函数，全部返回结果对象而非抛裸异常：

| 函数 | 职责 |
|------|------|
| `get_graph_token()` | msal client_credentials 取 token |
| `graph_get(path)` / `graph_patch(path, body)` / `graph_put_binary(path, data)` | 带 Authorization 头的 httpx 封装，统一 429 退避与错误分类 |
| `resolve_site(site_path)` | 返回 site id（含内存缓存） |
| `resolve_drive(site_id, drive_name)` | 返回 drive id（含缓存） |
| `upload_file(site_id, drive_id, filename, data)` | ≤250MB 简单 PUT；返回 itemId/webUrl |
| `upload_file_large(...)` | createUploadSession 分片 |
| `set_fields(site_id, drive_id, item_id, fields)` | PATCH listItem/fields |
| `find_by_media_id(site_id, drive_id, media_id)` | 幂等查询 |

### 8.3 `upload_to_sharepoint.py`（CLI）

```bash
# 上传全部未上传研报（默认）
PYTHONPATH=src .venv/bin/python upload_to_sharepoint.py

# 干跑：只打印将上传/写入的字段，不真正上传
PYTHONPATH=src .venv/bin/python upload_to_sharepoint.py --dry-run

# 仅处理 N 条
PYTHONPATH=src .venv/bin/python upload_to_sharepoint.py -n 10

# 强制重新上传（覆盖远端，用于修正历史元数据）
PYTHONPATH=src .venv/bin/python upload_to_sharepoint.py --force
```

输出为结构化日志：每行 `[upload] ✓ title → {category1}/{category2} (High)` 或 `[upload] ✗ title: 原因`；结束打印成功/失败/跳过计数。

### 8.4 环境变量（`.env` 新增）

```
# SharePoint 站点与文档库
SHAREPOINT_TENANT=your-tenant          # {tenant}.sharepoint.com 的 {tenant}
SHAREPOINT_SITE_PATH=/sites/ResearchReports
SHAREPOINT_DRIVE_NAME=Research Library
# 上传脚本 upload_to_sharepoint.py 的开关（false 时静默跳过）
SHAREPOINT_ENABLED=true

# check_reports.py：下载后、发邮件前是否自动上传 SharePoint（缺省 false）
UPLOAD_TO_SHAREPOINT=false

# check_reports.py：邮件发送方式 attachment=附件逐个 / digest=清单汇总（缺省 digest）
SEND_MODE=digest
```

---

## 9. 搜索与阅读（团队使用侧）

1. **搜索**：SharePoint 顶部搜索框输入关键词即可命中标题与正文（文档库列默认进索引）；高级搜索可用 KQL：`ReportAuthor:高盛 Priority:High`。
2. **筛选**：文档库页面右侧筛选窗格按 `Category1` / `Priority` / `ReportAuthor` / `ReportDate` 组合筛选。
3. **阅读**：点击文件在线预览（PDF 浏览器内置预览），或下载。
4. **提醒**：`check_reports.py` 在 `SEND_MODE=digest` 下发送「最新研报清单」邮件，正文附 SharePoint 站点链接（`https://{tenant}.sharepoint.com{site_path}`），成员一键跳转检索；`SEND_MODE=attachment` 下则逐封发送附件。

---

## 10. 定时调度

调度链（cron 顺序执行）：

```
check_reports（搜索→下载→[可选上传]→发送邮件）
   → categorize_reports（LLM 分类 + 元数据落库）
   → upload_to_sharepoint（补传/补写元数据）
```

- `check_reports.py` 现在可配置：
  - `UPLOAD_TO_SHAREPOINT=true` 时，下载后、发邮件前自动上传 SharePoint；
  - `SEND_MODE=digest`（缺省）时汇总为一份清单邮件（含站点链接），`attachment` 时逐封发附件。
- 建议调度：**每个交易日早间一次**（覆盖前一日与隔夜研报）。
- 三个脚本独立、幂等，任一环节失败不影响其它；cron 按顺序执行并保留各自退出码。

---

## 11. 分阶段实施计划

| 阶段 | 任务 | 涉及 | 验收标准 |
|------|------|------|----------|
| 0. 站点准备 | 创建 Team Site + 文档库 + 元数据列 + 视图；Azure AD 授权 `Sites.ReadWrite.All` | 人工/管理员 | 站点可访问，列已建，应用已授权 |
| 1. 认证打通 | `uv add msal`；`sharepoint.py` 实现 `get_graph_token` + `resolve_site` + `resolve_drive` | sharepoint.py | 能打印出 site id 与 drive id |
| 2. 上传打通 | 实现 `upload_file` + `set_fields`；`upload_to_sharepoint.py` 骨架 | sharepoint.py / upload_to_sharepoint.py | 单文件上传成功且列值正确 |
| 3. 元数据落库 | 扩展 `init_db` 迁移；改造 `categorize_reports.py` 输出并落库 `author/report_date/level1/level2/level3/priority` | check_reports.py / categorize_reports.py | 分类后 DB 字段完整、正确 |
| 4. 全量上传 | `upload_to_sharepoint.py` 完整流程 + 幂等 + 重试 + 中文文件名编码 | upload_to_sharepoint.py | 存量研报全量上传，重跑零重复 |
| 5. 历史回填 | 用 `--force` 或一次性脚本回填存量 `path` 已分类研报的元数据 | upload_to_sharepoint.py | 历史研报元数据齐全 |
| 6. 调度上线 | 接入 cron，追加上传步骤 | cron | 每日自动上传，失败有告警（状态邮件） |

---

## 12. 风险与注意事项（Pitfalls）

1. **上传不写元数据**：`PUT …/content` 仅写文件内容，元数据必须二次 `PATCH listItem/fields`（§7.2）。这是最高频的踩坑点。
2. **列内部名不可改**：`name` 用英文一次定死（`ReportAuthor` 等），`displayName` 中文可改；改名不会迁移内部名。
3. **中文文件名 URL 编码**：Graph 路径中的 `{filename}` 需 `urllib.parse.quote(filename, safe="")`，否则 400。
4. **简单上传大小上限 250MB**：PDF 研报通常远小于此，一般无需 upload session；但代码须保留大文件分支以防单文件超限。
5. **权限类型**：`client_credentials` 运行必须授 **Application** 权限并 Admin Consent，否则 `401/403`。
6. **429 限流**：Graph 对批量操作有节流，封装统一 `Retry-After` 退避；批量上传逐条串行并留少量间隔。
7. **日期列格式**：Graph 日期列**不要带 `Z`（UTC 后缀）**——带 `Z` 会被按站点时区错误解析，导致日期偏移一天（Graph 已知 bug，见微软 Q&A「Different time is returned when creating SharePoint List Item」）。应传无时区格式 `"2026-08-28T00:00:00"`，空值可传 `null`。
8. **Choice 列取值**：`fields` 中 `Priority`/`Category1` 必须精确匹配建列时的 choices，否则写入失败。
9. **幂等以 `media_id` 为准**：文件名可能重复，去重靠 `MediaId` 列而非文件名。
10. **`source_kb` 等静态列**：统一从常量落库，避免每条重复判断。

---

## 附录 A：Graph API 端点速查

| 操作 | 方法 + 路径 |
|------|------------|
| 解析站点 | `GET /sites/{tenant}.sharepoint.com:/sites/{path}` 或 `GET /sites?search={name}` |
| 列文档库 | `GET /sites/{site-id}/drives` |
| 列列表 | `GET /sites/{site-id}/lists`（文档库即 list） |
| 上传（≤250MB） | `PUT /sites/{site-id}/drives/{drive-id}/root:/{name}:/content` |
| 上传（大文件） | `POST /sites/{site-id}/drives/{drive-id}/root:/{name}:/createUploadSession` |
| 写元数据 | `PATCH /sites/{site-id}/drives/{drive-id}/items/{itemId}/listItem/fields` |
| 建列 | `POST /sites/{site-id}/lists/{list-id}/columns` |
| 幂等查询 | `GET /sites/{site-id}/lists/{list-id}/items?expand=fields&filter=fields/MediaId eq '{id}'` |

## 附录 B：权限 scope 速查

| Scope | 类型 | 用途 |
|-------|------|------|
| `Sites.ReadWrite.All` | Application | 上传文件 + 读写 list item（起步推荐） |
| `Sites.Selected` | Application | 仅授权指定 site（生产最小权限） |
| `Sites.Manage.All` | Application | 仅当用 Graph 建库/建列时 |
| `Files.ReadWrite.All` | Application | 可选，与 Sites 权限重叠，通常不必单独授 |

## 附录 C：券商中英文对照表（可扩展）

解析出的中文券商名经此表映射为**英文**后写入 `ReportAuthor`。**未命中的券商保留原文并告警，需人工补充此表。**

| 中文名 | 英文名 |
|--------|--------|
| 高盛 | Goldman Sachs |
| 伯恩斯坦 | Bernstein |
| 摩根士丹利 / 大摩 | Morgan Stanley |
| 摩根大通 | J.P. Morgan |
| 美银 / 美银美林 / 美国银行 | BofA Securities |
| 花旗 | Citi |
| 瑞银 | UBS |
| 瑞信 | Credit Suisse |
| 巴克莱 | Barclays |
| 德意志银行 / 德银 | Deutsche Bank |
| 汇丰 | HSBC |
| 野村 | Nomura |
| 大和 | Daiwa |
| 杰富瑞 | Jefferies |
| 麦格理 | Macquarie |
| 里昂 | CLSA |
| 中金 | CICC |
| 中信证券 | CITIC Securities |
| 华泰证券 | Huatai Securities |
| 国泰君安 | Guotai Junan |
| 申万宏源 | Shenwan Hongyuan |
| 海通证券 | Haitong Securities |
| 广发证券 | GF Securities |
| 招商证券 | China Merchants Securities |

> 别名处理：同一英文名的多个中文写法（如「美银 / 美银美林 / 美国银行」）作为多个 key 指向同一 value，在 `BROKER_NAME_MAP` 中并列即可。
