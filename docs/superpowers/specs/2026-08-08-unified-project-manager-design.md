# 两项目统一管理设计

## 目标

在保持两个上游项目代码边界清晰的前提下，为本机提供一个统一入口：继续使用 Codex Auto SMS Receiver 的接码、OAuth、账号和凭证管理能力，同时内嵌 GPTSession2CPAandSub2API 的浏览器端转换工具，并允许使用者主动确认后将转换结果保存到本机凭证库。

## 已核验的上游状态

- `maile456/codex-auto-sms-receiver`：主分支提交 `269bf3cd088b075f164ad2fe8e674b8b72a9fd26`。
- `gtxx3600/GPTSession2CPAandSub2API`：主分支提交 `a097eb155bb7bdf6cbbc26f1e4e75e120ab3163c`。
- 前者是 Python Flask 服务，当前只监听 `127.0.0.1:5015`。
- 后者是无构建步骤、无服务器依赖的纯前端单页工具，原始入口为 `docs/index.html`，上游测试为 `node tests/convert-session.test.js`。

## 架构边界

统一管理层使用本地新增的 `manager/` Python 包和根目录 `manager_app.py`。`manager_app.py` 在运行时包装上游 `app.py` 的应用工厂并注册管理蓝图，不直接修改上游 `app.py`、`src/webapp.py` 或 `templates/index.html`。上游启动、日志保留、PID 文件和 Flask 配置仍由原项目入口执行。

GPTSession2CPAandSub2API 的完整上游快照保存在 `vendor/GPTSession2CPAandSub2API/`，不直接修改其中的 HTML、JavaScript、测试或许可证。统一管理层在返回转换器首页时只在响应内容中注入本地桥接脚本；磁盘上的上游文件保持原样，方便以后整目录替换。

主要目录职责如下：

```text
manager_app.py                         本地组合入口
manager/
  blueprint.py                        管理页、转换器托管和本地 API
  credential_import.py                凭证校验、去重和原子落盘
  templates/manager.html              统一管理首页
  static/converter_bridge.js          转换器与本机凭证库的显式桥接
  upstreams.lock.json                 两个上游的提交和文件归属清单
vendor/GPTSession2CPAandSub2API/       第二个项目的未修改上游快照
data/codex_accounts/                   用户主动导入后的本机凭证
```

## 路由与用户体验

- `/manager`：统一管理首页，展示两个模块、当前运行状态、上游版本和更新状态。
- `/`：保留原接码与 OAuth 工作台，不改变现有 API 或页面行为。
- `/tools/session-converter/`：托管第二个项目的单页工具。
- `/api/manager/status`：只返回服务状态、上游 SHA 和非敏感计数。
- `/api/manager/credentials/import`：接收桥接脚本生成的 Codex 原生凭证文档。

`启动.cmd` 和 `ops/local/Start-Local.ps1` 改为运行 `manager_app.py` 并在健康检查成功后打开 `/manager`。`关闭.cmd` 继续使用 PID、绝对入口路径和虚拟环境父子进程校验，只终止本项目进程树。

统一管理首页提供两个清晰卡片：“接码与 OAuth 管理”和“Session / Token 格式转换”。页面不复制两边的业务界面，也不把两个项目的数据模型混在一起。

## 转换与保存的数据流

默认转换流程保持上游语义：输入内容只存在于浏览器内存，不自动发送或保存。桥接脚本新增“保存到本地凭证库”动作，并明确说明这一步会改变原来的临时处理边界。

保存时执行以下流程：

1. 使用者点击保存并确认本机持久化提示。
2. 桥接脚本临时调用转换器自身的 Codex 输出模式，读取标准 `auth.json` 结果，然后恢复使用者原先选择的输出格式。
3. 浏览器只向同源 `127.0.0.1:5015` 提交标准化凭证，不访问外部服务器。
4. 后端验证整个批次后，逐个使用临时文件和原子替换写入 `data/codex_accounts/`。
5. 响应只返回新增数、重复跳过数和不含 Token 的记录标识；页面跳转或链接回原凭证管理区。

## 凭证校验和冲突规则

- 请求体上限为 5 MiB，单批最多 100 个凭证。
- 接受单个 Codex `auth.json` 或这类文档的数组。
- `tokens` 必须是对象，且 `access_token` 必须是非空字符串；`refresh_token`、`id_token` 和 `account_id` 可为空。
- 文件名只使用经过清理的邮箱、账号 ID 或内容指纹，不使用客户端传入的路径和文件名。
- 后端可以从 JWT claims 补充邮箱、账号 ID、套餐和过期时间，但不能把解码失败当成可绕过结构验证的理由。
- 与现有文件 Token 指纹完全相同的凭证记为重复并跳过。
- 相同邮箱或账号 ID 但 Token 不同的凭证保存为新的时间戳版本，不静默覆盖；现有 ArtifactStore 按更新时间展示和选择最新版本。
- 缺少邮箱的有效凭证允许保存为独立凭证，但不会自动创建邮箱素材、接码账号或流水线任务。
- 过期 Token 可以归档，但页面必须标记过期；不会自动运行或刷新。

## 安全边界

- 统一服务继续固定监听 `127.0.0.1`，不新增反向代理、公网或局域网入口。
- Token、账号素材和完整取码地址不写入应用日志、API 响应、更新记录或测试夹具。
- 凭证导入必须由明确点击和确认触发；页面加载、转换、复制和下载都不能隐式落盘。
- 上游转换器在磁盘上保持未修改；直接打开其 `docs/index.html` 时仍然是纯浏览器、无持久化模式。
- 所有静态文件路由使用受控根目录和安全路径解析，拒绝目录穿越。

## 错误处理

- 转换结果无法生成 Codex 格式时，不发送请求并在转换器内显示原因。
- 后端批次结构存在无效项时，整个批次拒绝写入并返回无敏感数据的索引错误。
- 单个原子写入失败时停止后续写入，报告已完成数量；不删除此前已有凭证。
- 上游快照缺失、桥接注入点缺失或静态资源路径异常时，转换器路由返回明确的本地配置错误，不回退到外部在线页面。

## 测试与验收

- 运行原项目完整 pytest 测试。
- 为组合入口、管理页、静态文件安全、桥接注入和凭证导入新增 pytest 测试。
- 测试请求大小、批次数量、路径输入、无 Token、完全重复、同账号新 Token、缺少邮箱和过期 Token。
- 运行未修改上游的 `node vendor/GPTSession2CPAandSub2API/tests/convert-session.test.js`。
- 实际启动后确认 `/manager`、`/`、`/tools/session-converter/` 和 `/health` 均返回成功。
- 使用无真实 Token 的测试夹具完成“转换 → 确认保存 → 凭证列表可见”，随后清理测试夹具。

## 不在本阶段范围内

- 不自动把导入凭证加入接码账号或批量任务。
- 不在两个项目之间共享 `.env`、邮箱素材、日志或临时浏览器状态。
- 不把 Token 上传到 GitHub、在线转换页或任何新增第三方服务。
- 不改变上游转换格式算法；格式问题由独立上游快照和桥接兼容测试处理。
