这是用python编写的一个CLI脚本，用于定时检查“环球研报直通车”知识库中新增的研报，进行下载并发送邮件到指定地址。

## 数据层设计
使用 Sqlite DB 保存数据，建立一张 reports 表格，包含以下字段：

｜ Field name | type | sample |
|-------------|-------|---------|
| media_id | str, unique key | 'pdf_19ee3075e13eb6fc6b0a6821afb4b957_cc59a42f00c496787c6ca45b9d9b234b7442602265681522' |
| title | str, not null | '高盛-中国经济活动与政策追踪-260724.pdf' |
| downloaded_ts | int, default=0 | 1784979069 |
| sendmail_ts | int, default=0 | 1784979069 |
| created_ts | int, default=0 | 1784979069 |

## 执行逻辑
1. 取最近3天的日期，以'yymmdd'格式生成搜索关键字，从“环球研报直通车”知识库中搜索相应内容。
2. 对每条搜索结果，将 media_id, title 保存到sqlite db中，注意不要重复保存。
3. 对于搜索结果中的新增内容，获取媒体信息，根据取得的 url 进行下载，下载成功则更新 downloaded_ts 字段为下载时间；下载的文件以 title 命名，保存在 downloaded_reports 目录下。
4. 对于新下载未发送的研报，逐个发送邮件到指定目标邮箱地址；发送成功则更新 sendmail_ts 字段为发送时间


## 环境
1. 知识库的API接口，参照 https://skillhub.cn/skills/ima-skills 
2. 调用API接口所需的 API_KEY, CLIENT_ID, 以及发送邮件所需的 SMTP 配置、目标邮箱地址等保存在 .env 文件中。
3. 邮件发送使用 resend API。