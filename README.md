# Codex Auto SMS Receiver

基于 HeroSMS 的 Codex 自动接码与 OAuth 登录工具，支持邮箱 OTP、批量任务处理、凭证管理和运行日志查看。

> [!IMPORTANT]
> 本项目只处理由使用者本人持有或已获得明确授权的现有 ChatGPT 账号，不创建账号，也不包含任何注册入口。请遵守相关服务条款及所在地区的法律法规。

## 界面预览

### 账号导入与任务工作区

![账号导入与任务工作区](docs/images/dashboard-import.png)

### 接码统计与号码明细

![接码统计与号码明细](docs/images/sms-statistics-redacted.png)

> 预览图中的账号素材均为占位内容，号码明细已经匿名化处理。

## 功能特性

- 通过 HeroSMS 自动获取手机号并轮询短信验证码
- 自动读取 Outlook 或通用取码接口中的邮箱 OTP
- 支持账号密码 + TOTP 2FA 登录，验证码在本机生成，无需读取邮箱
- 完成现有 ChatGPT 账号的 Codex OAuth 授权流程
- 支持批量任务、1～3 路并发和可恢复错误重试
- 支持最多 10 个 HeroSMS 国家组成的优先队列
- 本地查看、单独下载、多选打包或全部打包 Codex OAuth 凭证
- 成功凭证可导出为 Codex 原始 JSON（ZIP）或 Sub2API 可导入 JSON
- 账号素材可按最初导入类型原样分组导出，并支持多选删除与独立凭证清理
- 卖家库存采用账号横向总览：集中展示套餐、Plus 剩余天数/到期日、动态额度窗口、进度条、重置倒计时、接码与销售状态；记录逐账号与累计导出次数，支持按数量导出或一次导出全部从未导出过的可售账号，并可查询所选或一次查询全部账号额度
- 账号清单和卖家库存会明确区分“未接码/已经接码”；未接码账号可按最初导入的邮箱素材格式分组导出
- 验证码 URL 的单轮等待时间、轮询间隔与邮箱验证码重试轮数可在接码配置页设置
- 提供脱敏日志、接码统计和失败原因分析
- 流水线状态持久化，服务重启后不会自动重复购买号码

## 工作流程

```text
导入已有账号的登录素材
        ↓
通过邮箱 OTP 或密码 + TOTP 登录 OpenAI 授权页
        ↓
按需通过 HeroSMS 完成手机短信验证
        ↓
完成 Codex OAuth 回调
        ↓
在本地保存凭证、任务状态与日志
```

## 环境要求

- Python 3.10 或更高版本
- 可正常访问 OpenAI、Microsoft Graph 和 HeroSMS 的网络环境
- HeroSMS 账号及 API Key
- 一个或多个由你本人持有或已获授权的现有 ChatGPT 账号

## 快速开始

### 1. 安装依赖

```powershell
python -m pip install -r requirements.txt
```

建议使用虚拟环境：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

### 2. 创建本地配置

PowerShell：

```powershell
Copy-Item .env.example .env
```

Linux / macOS：

```bash
cp .env.example .env
```

可以直接编辑 `.env`，也可以在 WebUI 中配置 HeroSMS。最少需要设置：

```dotenv
HERO_SMS_API_KEY=your_api_key
HERO_SMS_COUNTRIES=33
HERO_SMS_MAX_PRICE=0.11
```

国家使用 HeroSMS 的数字国家 ID；多个国家以英文逗号分隔，并按照从左到右的顺序尝试。

### 3. 启动服务

```powershell
python app.py --host 127.0.0.1 --port 5015
```

浏览器访问：<http://127.0.0.1:5015>

WebUI 不设登录密码，因此程序只允许监听本机回环地址。请勿通过反向代理、端口转发或其他方式将控制台暴露到局域网或公网。

## 账号导入

### Outlook

每行一个账号：

```text
email----邮箱密码----clientId----refreshToken
```

默认通过 Microsoft Graph 直连读取验证码，避免将 Refresh Token 交给第三方取件服务。

### iCloud 取码 API Key

对于 `icloud.xbovo.online` 邮箱凭证，每行填写：

```text
email----API_KEY
```

程序会在本机自动构造 HTTPS 查询地址。

### 验证码 URL

在界面选择“验证码 URL（HTML / JSON / 文本）”，每行填写：

```text
email----https://example.com/code
```

响应可以是纯文本、JSON 或 HTML。结构化 JSON 支持 `ok`、`code`、`mail`
和 `fetched_at` 字段：空 `code` 表示尚未收到；有验证码时会以 `mail` 中的
邮件时间过滤旧码，`fetched_at` 仅视为查询时间。也支持“邮件列表 HTML →
同源 JSON 详情 → data URI 正文”这类网页收件箱。

API Key 和完整取码地址都属于敏感信息，仅保存在本机，不会出现在账号列表；
请勿写入日志、截图或公开仓库。

通用取码 URL 默认最长等待 90 秒、每 3 秒轮询一次。可在 `.env` 中通过
`GENERIC_API_OTP_MAX_WAIT` 和 `GENERIC_API_OTP_POLL_INTERVAL` 调整，范围分别为
30～300 秒和 1～30 秒。取码超时或代理连接失败会被标记为可重试错误，
当流水线的“失败后自动重试”大于 0 时会重新执行登录流程。

其他本机项目可只提交邮箱并查询任务状态，验证码和 OAuth 凭证仍保存在本项目中，
详见 [邮箱任务对接 README](docs/INTEGRATION_README.md)。

### 账号密码 + TOTP 2FA

适用于已启用验证器 2FA 的现有 ChatGPT 账号，每行格式如下：

```text
email|ChatGPT密码|Base32格式的2FA密钥
```

程序按照 TOTP 标准在本机生成动态验证码，不连接 iCloud 或其他邮箱。此方式仅支持默认的 `protocol` 驱动。

## 批量任务

导入账号后，可以按“待处理”“上次失败”“全部可运行”或手动选择确定任务范围，再启动整批任务。

- **并发数**：可设置为 1～3；每个并发槽使用独立进程，通常建议设置为 1～2
- **任务重试**：可设置为 0～3；仅重试网络超时、HTTP 429、服务端 5xx 等临时错误
- **换号次数**：控制单个 OAuth 任务内部的 HeroSMS 号码轮换，与任务级重试相互独立
- **暂停/继续**：暂停后不再派发新任务，已运行的网络步骤正常收尾；继续后恢复剩余队列
- **停止任务**：停止领取后续任务，已经开始的任务会自然结束并保留实际结果
- **异常恢复**：未完成批次在服务重启后标记为已中断，不会静默重复执行或购买号码

## HeroSMS 配置

WebUI 支持搜索 HeroSMS 国家目录，并将最多 10 个国家加入优先队列。运行时严格按照队列顺序取号；当前国家无库存时自动尝试下一个国家。

| 配置项 | 说明 | 默认值 |
| --- | --- | --- |
| `HERO_SMS_API_KEY` | HeroSMS API Key | 空 |
| `HERO_SMS_COUNTRIES` | 国家 ID 优先队列，逗号分隔 | `33` |
| `HERO_SMS_MAX_PRICE` | 单个号码的最高价格 | `0.11` |
| `SMS_MAX_RETRIES` | 单任务最大换号次数 | `10` |
| `SMS_CODE_WAIT` | 短信验证码等待参数 | `30` |
| `SMS_SERVICE` | HeroSMS 服务代码，运行时固定为 OpenAI 对应的 `dr` | `dr` |

程序会通过 HeroSMS 官方接口查询余额、国家目录、库存和价格。已购号码如果遇到平台拒绝、发送失败或收码超时，下一次换号会从队列中的下一个国家继续。

## 凭证、日志与统计

- **凭证管理**：展示凭证类型、Token 字段状态、到期时间和文件大小，不在列表中返回 Token 内容；“取消选择”和“删除所选”是两个独立操作
- **多格式导出**：批量凭证支持 Codex 原始 JSON 压缩包和 Sub2API `sub2api-data` JSON；Sub2API 的 `expires_at` 优先使用 Plus 订阅到期时间，缺少订阅信息时才回退到 OAuth Token 到期时间；账号清单支持按 Outlook、通用 API、验证码 URL、密码 + TOTP 的原导入格式分别导出 TXT
- **批量管理**：账号和凭证均可多选、取消选择和删除；删除凭证会保留账号登录素材，删除账号会保留已归档凭证
- **选择隔离**：账号“管理选择”只影响导出和删除，“本批选择”只影响流水线，二者互不覆盖
- **健康筛选**：账号清单可直接筛选取码失败、凭证即将到期和凭证已过期，并支持将失败账号加入重试批次
- **流水线反馈**：运行中展示账号当前阶段耗时、整批运行时间、处理速度和预计剩余时间；启动前显示账号来源与失败重试预检
- **安全下载**：导出账号素材或完整 Token 前均需要二次确认
- **日志查看**：支持搜索、级别筛选、分页和自动刷新；WebUI 会隐藏 API Key、Token、OTP、OAuth 参数及完整手机号
- **接码统计**：汇总取号、短信发送、收码、验证结果、国家、报价和失败原因
- **日志保留**：默认保留 30 天，最多 1000 个文件，总量不超过 200 MB

原始日志仍可能包含敏感上下文，导出后请妥善保管。

## 数据安全

以下内容只保存在本机，并已通过 `.gitignore` 排除：

```text
.env                         本地配置与 HeroSMS API Key
data/mailboxes.json          账号登录素材
data/pipeline-state.json     流水线状态
data/codex_accounts/         Codex OAuth 凭证
logs/                        OAuth 与服务日志
```

开源或提交代码前，请确认这些文件没有被加入版本控制。账号密码、TOTP 2FA 密钥、Refresh Token、取码地址、HeroSMS API Key 和 OAuth 凭证均属于高敏感信息，不应分享或提交到公开仓库。

## 关键运行配置

项目默认使用以下安全配置：

```dotenv
WEBUI_HOST=127.0.0.1
WEBUI_PORT=5015
CODEX_OAUTH_DRIVER=protocol
CODEX_AUTH_URL_SOURCE=local
OUTLOOK_FETCH_MODE=direct
SMS_PROVIDER=hero
```

运行时固定使用协议驱动和 HeroSMS，不会调用上游的账号注册入口。

## 项目结构

```text
app.py                         应用入口
src/                           WebUI、任务调度、邮箱与 HeroSMS 集成
templates/                     WebUI 页面
tests/                         自动化测试
vendor/turb-gpt-free-register/ OAuth、OTP 与 Sentinel 运行时快照
data/                          本地账号、状态与凭证（不入库）
logs/                          本地运行日志（不入库）
```

## 测试

```powershell
python -m pytest -q
```

测试覆盖账号导入、TOTP 生成、HeroSMS 配置与取码、批量流水线、日志脱敏、凭证导出以及“不包含注册入口”的运行边界。

## 上游说明

项目内置了 [`myfanhua/turb-gpt-free-register`](https://github.com/myfanhua/turb-gpt-free-register) 在提交 `9e00a7b0a8cf9e77edc265c1883f68f1a321b2da` 的运行快照及上游许可证。

本项目只调用 `core.codex_oauth.run_codex_oauth()`，不会调用账号注册入口。vendor 快照为保持上游导入兼容性保留了部分未使用模块，但这些模块没有接入本项目的 WebUI、任务调度或启动入口。具体信息参见 [`vendor/turb-gpt-free-register/UPSTREAM.md`](vendor/turb-gpt-free-register/UPSTREAM.md)。

## 免责声明

本项目是非官方工具，与 OpenAI、ChatGPT、Codex 或 HeroSMS 不存在隶属、授权或背书关系。使用者应对账号权限、服务费用、数据安全及使用行为承担全部责任。
