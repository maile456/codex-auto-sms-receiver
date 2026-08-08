# 本机统一管理部署说明

这份说明只描述本机集成层。两个上游项目仍保持独立：接码项目位于仓库根目录，格式转换器的固定快照位于 `vendor/GPTSession2CPAandSub2API/`。

## 一次性准备

需要 Windows 10/11、Python 3.12 和 Node.js 22。首次部署在 PowerShell 中执行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

然后编辑 `.env`，至少配置 HeroSMS API Key。也可以启动后在接码工作台内完成 HeroSMS 国家、最高价格和重试参数配置。

## 启动和关闭开关

- 双击 `启动.cmd`：后台运行 `manager_app.py`，等待健康检查成功，然后打开统一管理页。
- 双击 `关闭.cmd`：核对 PID、绝对入口路径和项目虚拟环境后，只结束本项目的进程树；重复关闭安全。

自动化或不希望打开浏览器时，可以运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ops\local\Start-Local.ps1 -NoBrowser
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ops\local\Stop-Local.ps1
```

## 智能接码熔断

通过 `启动.cmd` 或 `ops/local/Start-Local.ps1` 启动时，统一管理器会启用本地智能接码覆盖层：OpenAI 返回风控拒绝或手机号接口限流时立即停止当前账号；号码已使用、短信收码超时和验证码被拒最多换号一次；HeroSMS 无号码、余额不足或 API Key 错误时立即停止。现有“最大换号次数”仍是绝对总上限，智能规则可以更早停止，但不能被调大参数绕过。

直接运行 `python app.py` 不启用该覆盖层。覆盖层只位于受保护的 `manager/` 集成层，不修改 `src/` 或 `vendor/` 上游文件，也不会自动调整国家、最低价、最高价或指定价格档。

上游更新后会运行 Python 测试验证 `core.codex_oauth` 兼容性。如果上游接口变化导致智能覆盖无法安全安装，更新测试必须失败，现有更新脚本会恢复旧文件、依赖和服务。

## 本机入口

- 统一管理：<http://127.0.0.1:5015/manager>
- 接码与 OAuth：<http://127.0.0.1:5015/>
- Session / Token 转换：<http://127.0.0.1:5015/tools/session-converter/>
- 健康检查：<http://127.0.0.1:5015/health>

转换器默认只在浏览器内处理内容。只有点击“保存到本地凭证库”并确认后，标准 Codex 凭证才会写入 `data/codex_accounts/`。缺少邮箱的凭证只归档，不会自动创建接码账号或任务。

## 更新两个上游项目

- 双击 `检查更新.cmd`：只读取 GitHub 最新提交并显示差异，不修改文件、不停服务。
- 双击 `更新两个项目.cmd`：先预览；只有明确确认后才下载固定 SHA、校验全部 Git blob、备份、停服、应用、测试并重启。

更新失败会恢复旧上游文件、锁文件、vendor 快照和发生依赖切换时的虚拟环境，然后重启旧版本。管理网页不提供修改源码的接口。

## 日志、测试与排错

启动输出位于：

- `logs/server.stdout.log`
- `logs/server.stderr.log`

完整自动化验证：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
node vendor\GPTSession2CPAandSub2API\tests\convert-session.test.js
node tests\manager-bridge.test.js
```

控制台没有登录密码，只允许监听 `127.0.0.1`。不要通过反向代理、端口转发或其他方式暴露到局域网或公网；`.env`、`data/` 和 `logs/` 都只应保留在本机。
