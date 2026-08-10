# Windows 本地部署与启停按钮设计

## 目标

在 Windows 上把项目部署到独立的 Python 虚拟环境，并在仓库根目录提供可双击的 `启动.cmd` 与 `关闭.cmd`。启动按钮必须确认 WebUI 真正可用后再打开浏览器；关闭按钮必须只终止本项目进程，不能按进程名批量结束其他 Python 程序。

## 已确认环境与边界

- 操作系统：Windows，入口兼容 Windows PowerShell 5.1。
- 根目录 CMD 按钮使用 CRLF 换行；包含中文消息的 PowerShell 脚本使用带 BOM 的 UTF-8，确保 Windows PowerShell 5.1 正确解析。
- Python：使用 Python 3.10 或更高版本创建根目录下的 `.venv`。
- 服务地址固定为 `http://127.0.0.1:5015`，不监听局域网或公网地址。
- 健康检查使用项目已有的 `GET /health`。
- HeroSMS API Key 不写入脚本或版本库；首次部署只从 `.env.example` 复制出被 Git 忽略的 `.env`，具体密钥由用户以后在本机 WebUI 或 `.env` 中填写。
- 当前源码来自 GitHub 主分支快照 `269bf3c`。由于本机无法通过 Git HTTPS 克隆，使用 GitHub 官方 codeload 源码包取得，并已用 Git blob SHA 核验关键文件。
- 不增加开机自启、Windows 服务、反向代理或公网访问能力。

## 方案选择

采用“根目录 CMD 入口 + `ops/local` PowerShell 控制脚本”。CMD 文件只负责提供稳定的双击体验，复杂的状态判断、进程校验、健康检查和错误信息由 PowerShell 实现。

没有选择纯 CMD，是因为它难以安全读取并验证进程信息；没有注册为 Windows 服务，是因为本项目包含交互式 WebUI，且用户只要求手动启停。

## 文件与职责

### `启动.cmd`

- 从文件自身所在目录启动，不依赖用户当前工作目录。
- 使用 `powershell.exe -NoProfile -ExecutionPolicy Bypass` 调用 `ops/local/Start-Local.ps1`。
- 成功后自动关闭命令窗口；失败时保留错误信息并等待用户按键。

### `关闭.cmd`

- 从文件自身所在目录调用 `ops/local/Stop-Local.ps1`。
- “已经关闭”也视为成功，方便重复双击。
- 真正发生错误时保留窗口供用户查看。

### `ops/local/Start-Local.ps1`

- 定位项目根目录、`.venv\Scripts\python.exe`、绝对路径 `app.py`、`data/server.pid` 和启动日志。
- 如果虚拟环境或 `.env` 不存在，给出明确的部署缺失提示并退出，不在每次启动时重新安装依赖。
- 读取 PID 文件并通过 CIM 校验：命令行必须包含本项目 `app.py` 的绝对路径，且该进程本身或其直接父进程必须是项目 `.venv` 中的 Python。此规则兼容 Windows Python 虚拟环境 redirector。
- 如果已确认是本项目且 `/health` 正常，则不重复创建进程，只打开 WebUI。
- 如果 `5015` 已被其他程序占用，拒绝启动并提示端口冲突，不尝试结束占用者。
- 使用隐藏窗口启动项目专属 Python，标准输出和标准错误分别写入 `logs/server.stdout.log` 与 `logs/server.stderr.log`。
- 最多等待 30 秒轮询 `/health`。成功后打开默认浏览器；超时则只清理本次创建且身份仍匹配的进程，并显示日志位置。

### `ops/local/Stop-Local.ps1`

- 优先读取项目自己的 `data/server.pid`。
- 在终止前进行与启动脚本相同的双重身份校验：本项目 `app.py` 绝对路径 + 当前进程或直接父进程属于项目虚拟环境 Python。
- 校验通过后终止该进程树，覆盖流水线可能创建的子进程；不使用 `taskkill /IM python.exe` 等按名称批量终止方式。
- 最多等待 10 秒确认 `/health` 不再可达，并清理属于已停止实例的 PID 文件。
- PID 文件缺失或进程已不存在且健康检查也不可达时，返回“服务已经关闭”。
- PID 已被系统复用、身份不匹配或 WebUI 仍在线却无法证明其属于本项目时，拒绝杀进程并提示人工检查。

### `tests/test_windows_launch_controls.py`

- 非 Windows 平台跳过集成部分。
- 验证根目录按钮正确引用对应 PowerShell 脚本。
- 验证启动脚本能启动服务并使 `/health` 返回成功。
- 验证重复启动不会创建第二个服务进程。
- 验证关闭脚本能停止服务，重复关闭仍成功。
- 验证身份不匹配的 PID 不会被关闭脚本终止。

### `README.md`

增加 Windows 本地启停说明，说明首次部署完成后可以双击两个按钮，并注明启动失败时的日志位置和关闭服务会中断正在进行的任务。

## 首次部署流程

1. 用系统中满足版本要求的 Python 创建 `.venv`。
2. 使用 `.venv\Scripts\python.exe -m pip install -r requirements.txt` 安装依赖。
3. 如果 `.env` 不存在，复制 `.env.example`；永不覆盖已有 `.env`。
4. 运行完整测试集，确认依赖和当前源码可用。
5. 使用 `启动.cmd` 启动，确认 `GET /health` 成功且浏览器页面可访问。
6. 再次启动，确认没有重复进程。
7. 使用 `关闭.cmd` 停止，确认健康检查不可达且端口释放。

## 状态流与安全策略

启动路径为：入口按钮 → 环境检查 → PID/端口检查 → 后台进程 → 健康检查 → 浏览器。任何一步失败都会停止后续步骤，并给出可操作的信息；只有本次启动的新进程会在启动超时后被回滚。

关闭路径为：入口按钮 → PID 读取 → 进程身份校验 → 终止进程树 → 健康检查确认。无法证明进程归属时，以不终止为默认行为。

`.env`、`data/`、`logs/` 和 `.venv/` 已由项目 `.gitignore` 排除。脚本不得输出 API Key、账号素材、Token 或完整取码地址。

## 验收标准

- 在资源管理器中双击 `启动.cmd`，30 秒内 `http://127.0.0.1:5015/health` 返回成功并打开 WebUI。
- 连续双击启动按钮不会产生多个 `app.py` 服务进程。
- 双击 `关闭.cmd` 后服务停止，重复双击不会报错。
- 关闭脚本不会结束其他 Python 进程，也不会结束身份不匹配的 PID。
- 项目测试通过，启动和关闭的实际验证结果有命令输出作为证据。
- 敏感配置只存在于本机被忽略的运行文件中。
