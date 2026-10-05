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
1. 按日期文件夹扫描：知识库目录结构为 根目录 /「{年份}年国际顶级投行研报」/「9月」/「9.11」；取最近 3 天（按北京时间）的日期文件夹，列出其中全部条目并取文件名。
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
6. 研报分类所需的 DEEPSEEK_API_KEY 保存在 .env 文件中；分类默认使用 deepseek-flash 模型（可用 DEEPSEEK_MODEL 覆盖）。

## 脚本文件

| 文件 | 用途 |
|------|------|
| `check_reports.py` | 主脚本：搜索 → 入库 → 下载 → 发邮件 |
| `list_undownloaded.py` | 辅助脚本：列出所有未下载研报并发送清单邮件 |
| `download_from_zip.py` | 辅助脚本：从 zip 压缩包中提取 PDF 研报，标记为已下载并即时 LLM 分类 |
| `categorize_reports.py` | 辅助脚本：使用 LLM 对已下载研报分类并移动至分类目录 |
| `baidu_pan_download.py` | 辅助脚本：从百度网盘分享链接（带提取码）下载整个文件夹 |
| `test_baidu_pan_download.py` | 冒烟测试：`baidu_pan_download` 的纯函数（不联网、不需要 Cookie） |

## zip 补下载的文件名匹配（download_from_zip.py）

zip 内文件名与 DB `title` 常有细微差别，匹配分三级，按序尝试：

1. **精确**：原始标题完全相同。
2. **归一化**：去扩展名 → NFKC（全角转半角）→ 小写 → 只保留字母/数字/汉字，
   从而抹平空格、半角/全角括号与冒号、中英文标点、连字符等差异。
3. **前缀截断**：任一方归一化后是另一方的完整前缀（前缀 ≥ 20 字符），
   用于文件名被截断的场景（Windows 路径长度限制）。

匹配到后一律用 DB 的 `title` 命名落盘，并打印 DB 标题（`（归一化匹配 → …）`）便于人工核对；
三级都不命中则回落到「跳过（无记录）」，**不做相似度模糊绑定**——同一券商同日研报的近似标题
相似度可达 0.98，模糊绑定会把 zip 名错配到别的记录（实测尾段匹配错配率 >10%）。

## 百度网盘分享下载（baidu_pan_download.py）

用于把别人分享的研报文件夹（带提取码）整包拉到本地：

```bash
export BAIDU_COOKIE='BDUSS=...; STOKEN=...'    # 也可以写进 .env，或用 --cookie / --cookie-file
uv run python baidu_pan_download.py https://pan.baidu.com/s/1lpUp14K-1CXccXny5RlYmg --pwd 0203
```

Cookie 获取：浏览器登录 https://pan.baidu.com/ ，F12 → Network → 任意请求 → 复制请求头里的 `Cookie` 整串
（`BDUSS` 是必需项；只给 `BDUSS` 的值也可以）。

```bash
# 先在 .env 里写一行（推荐，脚本会自动读取脚本目录与当前目录的 .env）：
# BAIDU_COOKIE=BDUSS=xxx; STOKEN=yyy; ...
uv run python baidu_pan_download.py <链接> --pwd 0203 --check   # 先快速校验 Cookie/提取码
```

### 排查 `[错误] Cookie 校验失败（errno=-6）`

脚本会先打印「Cookie 字段（共 N 个）」，对照下表排查：

| 现象 | 原因 | 处理 |
|------|------|------|
| 有 `BDUSS` 但**没有 `STOKEN`**（最常见） | 这份 Cookie 不是从**已登录的 pan.baidu.com** 复制的。`BDUSS` 只能证明「百度账号已登录」，而 `/api/*`（列表、转存、取直链）还需要 `STOKEN`——它只存在于登录网盘后的会话里 | 看下面的「正确复制 Cookie」步骤。注意：此时 `--mode list` / `--check` 仍可用（它们只走 `/share/*` 接口） |
| 字段里没有 `BDUSS` | 只复制了 `BDUSS_BFESS`，或没复制到 Cookie 行 | 脚本会自动把 `BDUSS_BFESS` 回填为 `BDUSS`；否则重新复制 |
| 字段数很少/为 0 | `.env` 里值被截断，或换行导致只读到了一半 | 用引号把整串包成一行：`BAIDU_COOKIE="BDUSS=...; STOKEN=..."` |
| 字段名里出现 `Host`、`Accept` 等 | 把整段请求头粘进来了 | 现在能自动只取 `Cookie:` 行（带前缀也可），但建议直接粘值 |
| `STOKEN` 也在，仍报 -6 | BDUSS 已过期或登录态与浏览器 UA 绑定 | 重新登录后复制；再用 `--user-agent`/`BAIDU_UA` 填「复制 Cookie 那个浏览器」的 UA |

**正确复制 Cookie（必读）**

Cookie 里必须同时有 `BDUSS` 和 `STOKEN`，否则网盘接口会返回 errno=-6。步骤：

1. 浏览器打开 https://pan.baidu.com/disk/main ，确认能看到文件列表；
   若跳到登录页或提示重新登录，先完成登录（`newlogin` 会话需要重新登录才能用于脚本）。
2. F12 → Network → 刷新页面 → 点任意一个 `pan.baidu.com` 的请求（例如 `api/list` / `api/loginStatus`）。
3. 右键 → Copy → **Copy request headers** → 复制其中的 `Cookie` 整串。
   应类似：`BAIDUID=...; PANWEB=1; PSTM=...; BDUSS=...; STOKEN=...; PANPSC=...`
   —— 只复制 `BDUSS`（或从 `www.baidu.com`、未登录的分享页复制的 Cookie）是不够的。
4. 粘到 `.env` 的 `BAIDU_COOKIE`（建议用引号包成一行），然后跑
   `uv run python baidu_pan_download.py <链接> --pwd <提取码> --check` 验证。

其它细节：脚本按优先级读 `--cookie` > `--cookie-file` > `BAIDU_COOKIE`/`BAIDU_COOKIES`/`BAIDU_BDUSS`。
加 `--debug` 可看到各个 `app_id` 与接口的 `errno`，便于区分「登录态问题」和「接口变化」。

两种模式（`--mode`）：

| 模式 | 流程 | 特点 |
|------|------|------|
| `transfer`（默认） | 提取码校验 → 转存到网盘 `/ima_download/<时间戳>` → 递归列举 → 取直链下载 | 接口最稳，文件夹结构完整；占用网盘空间，配合 `--cleanup` 自动清理副本 |
| `share` | 提取码校验 → 递归遍历分享 → 逐文件取直链下载 | 不占网盘空间，但接口较老：百度现在常返回 `errno=9019 (need verify)` 风控，此时请用默认的 `transfer` |
| `list` | 只解析并打印文件树 | 先确认内容再下载 |

常用参数：

| 参数 | 说明 |
|------|------|
| `--pwd` | 提取码（如 `0203`）；不传则交互式输入 |
| `--out` | 本地保存目录，默认 `downloaded_reports` |
| `--jobs` | 并发下载数，默认 3（非会员限速，调高收益有限） |
| `--pan-dir` | 转存模式的目标网盘目录 |
| `--check` | 只校验 Cookie + 提取码并打印分享根目录（排查登录问题首选） |
| `--debug` | 打印接口调试信息（app_id / errno） |
| `--user-agent` | 自定义 UA（登录态与浏览器绑定时用；也可用 `BAIDU_UA`） |
| `--cleanup` | 下载完成后删除网盘里的转存副本 |
| `--verify-md5` | 下载完成后校验 md5 |
| `--dry-run` | 只打印文件树，不做转存/下载 |
| `--[no-]post-process` | 下载后逐份后处理（默认开启，见下节；`--no-post-process` 只下载） |
| `--[no-]classify` | LLM 分类并移入 `categorized_reports`（默认开启） |
| `--[no-]sharepoint` | 上传 SharePoint（默认开启，需 `SHAREPOINT_*` / `O365_*` 配置） |
| `--[no-]mail` | 逐份发送带附件的邮件（默认开启，需 `EMAIL_FROM` / `EMAIL_TO`） |

### 下载后处理（默认开启）

**每完成一份研报的下载，立即依次执行**：

```
下载完成 → ①入库(reports.db) → ②LLM 分类 → ③上传 SharePoint → ④发邮件
```

* **入库**：`reports` 表新增记录（`media_id = bdpan_<网盘fs_id>`）并标记 `downloaded_ts`，
  同时写入 `path`（文件所在目录；项目目录下用相对路径）。
* **分类**：调用 `classifier.classify_one_report()`，写入 `author / report_date / level1~3 / priority`，
  并把文件**移动到** `categorized_reports/{一级}/{二级}/[{三级}/]`，同步更新 `path`。
  未配置 `DEEPSEEK_API_KEY` 或分类失败时文件保持原位置。
* **上传**：`check_reports.upload_report_to_sharepoint()`（幂等，已上传则直接跳过）。
* **发邮件**：`check_reports.send_email()`，成功后标记 `sendmail_ts`。

失败隔离与幂等：

* 上述每一步都单独捕获异常，**任何一步失败都不会影响其它研报的下载与处理**；
* 后处理在**独立线程**里串行消费队列，与下载并行，不拖慢下载速度；
* **只处理 `created_ts` 在最近 7 天内入库的记录**（可用 `BAIDU_PAN_RECENT_DAYS` 调整）：
  更早的旧记录一律不处理 —— 不写库、不分类、不上传、不发信；`created_ts` 为 0 的存量数据同样按旧记录处理；
* **已分类的记录（`level1` 非空）不再重复调 LLM 分类**（文件本就已经在分类目录里），
  但仍会补上传（幂等）/补发邮件；
* 按标题去重：同名研报（无论是 IMA 知识库还是网盘来的）复用同一条记录，
  已发过邮件（`sendmail_ts > 0`）的直接跳过，**不会重复发信**；
* 已存在本地的文件（包括分类后已移入 `categorized_reports/` 的）也会补做后处理，
  重跑可以自愈上次失败的环节，且**不会重新下载**（会查 DB 里记录的 `path`，
  该查询不受 7 天限制，因为它只用于避免重复下载，不参与任何处理）；
* 结束时打印汇总：`入库 N，分类 N（已分类跳过 N），上传 SharePoint N，发邮件 N，跳过 N，失败 N`。

```bash
# 只下载不入库/不分类/不发信（纯下载）
uv run python baidu_pan_download.py <链接> --pwd 0203 --no-post-process

# 下载 + 入库 + 分类，但不上传、不发信
uv run python baidu_pan_download.py <链接> --pwd 0203 --no-sharepoint --no-mail
```

依赖说明：后处理需要项目已有的依赖（`pymupdf`、`o365`、`httpx` 等）与 `.env` 配置；
缺失时脚本会打印提示并**自动降级为只下载**，不会中断。

实现要点：分享页的 `yunData` 是 JS 对象字面量、`locals.mset(...)` 才是标准 JSON，解析时两条路都试；
下载直链用 `/api/download`（sign 由页面 `sign1/sign3/timestamp` 按前端算法算出），
再经 `LogStatistic` UA 解析 302 拿到真实 CDN 地址；下载支持断点续传与多 UA 回退。
