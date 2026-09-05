这是用python编写的一个CLI脚本，用于定时检查"环球研报直通车"知识库中新增的研报，进行下载并发送邮件到指定地址。

## 数据层设计
使用 Sqlite DB 保存数据，建立一张 reports 表格，包含以下字段：

| Field name | type | sample |
|-------------|-------|---------|
| media_id | str, unique key | 'pdf_19ee3075e13eb6fc6b0a6821afb4b957_cc59a42f00c496787c6ca45b9d9b234b7442602265681522' |
| title | str, not null | '高盛-中国经济活动与政策追踪-260724.pdf' |
| downloaded_ts | int, default=0 | 1784979069 |
| sendmail_ts | int, default=0 | 1784979069 |
| created_ts | int, default=0 | 1784979069 |
| path | str, default='downloaded_reports' | 'downloaded_reports' |

## 执行逻辑
1. 取最近7天的日期，以'yymmdd'格式生成搜索关键字，从"环球研报直通车"知识库中搜索相应内容。
2. 对每条搜索结果，将 media_id, title 保存到sqlite db中，注意不要重复保存。
3. 对于搜索结果中的新增内容（仅限 48 小时内入库的），获取媒体信息，根据取得的 url 进行下载，下载成功则更新 downloaded_ts 字段为下载时间；下载的文件以 title 命名，保存目录由数据库中该记录的 path 字段指定。
4. 对于新下载未发送的研报，逐个发送邮件到指定目标邮箱地址；发送成功则更新 sendmail_ts 字段为发送时间。
5. 发送邮件前，先将已下载但未发送的存量研报发完；再逐个下载新研报，每下载成功一个立即发送；若下载失败则终止后续所有处理。
6. 下载和发送均按 created_ts 从近到远排序，最近入库的研报优先处理。
7. 搜索阶段可通过 KEYWORD_IGNORE 环境变量过滤标题，逗号分隔多关键词，大小写不敏感。
8. 研报的下载与发送路径均从数据库 path 字段动态读取（相对路径以项目目录为基准）。

## 研报分类（LLM）
对已下载的研报使用 DeepSeek LLM 进行分类，并将文件移动到分类目录：

1. 仅处理 path 为缺省值（'downloaded_reports'）且已下载（downloaded_ts > 0）的研报。
2. 一级分类四类：Equity Research、Macro & Strategy、Industry & Thematic、Others。
3. 二级分类：Equity Research 按行业（与 Industry & Thematic 二级一致）；Macro & Strategy 按地区；Industry & Thematic 按行业；Others 不再细分。
4. 三级分类：仅 Equity Research 按公司股票代码（如 AAPL、0700.HK）。
5. 分类后按分类生成新路径 categorized_reports/{一级}/{二级}/[{三级}/]，移动文件并更新 path 字段。

## 环境
1. 知识库的API接口，参照 https://skillhub.cn/skills/ima-skills
2. 调用API接口所需的 IMA_API_KEY, IMA_CLIENT_ID 保存在 .env 文件中。
3. 邮件发送使用 O365 (Microsoft Graph API)，所需配置 O365_CLIENT_ID, O365_CLIENT_SECRET, O365_TENANT_ID 保存在 .env 文件中。
4. 收件人 EMAIL_TO 保存在 .env 文件中。
5. 未下载清单邮件的收件人 LIST_EMAIL_TO 保存在 .env 文件中。
6. 研报分类所需的 DEEPSEEK_API_KEY 保存在 .env 文件中。

## 脚本文件

| 文件 | 用途 |
|------|------|
| `check_reports.py` | 主脚本：搜索 → 入库 → 下载 → 发邮件 |
| `list_undownloaded.py` | 辅助脚本：列出所有未下载研报并发送清单邮件 |
| `download_from_zip.py` | 辅助脚本：从 zip 压缩包中提取 PDF 研报并标记为已下载 |
| `categorize_reports.py` | 辅助脚本：使用 LLM 对已下载研报分类并移动至分类目录 |
