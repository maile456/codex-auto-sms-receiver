# Windows Local Launch Controls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy the application into a repository-local Python environment and provide safe, double-clickable Windows start and stop controls.

**Architecture:** Root-level CMD launchers provide the user-facing buttons and delegate to PowerShell 5.1 scripts in `ops/local`. The scripts identify the server with both its repository-local Python executable and absolute `app.py` command-line path, use the existing PID file and `/health` endpoint for state, and fail closed whenever ownership is uncertain.

**Tech Stack:** Windows CMD, Windows PowerShell 5.1, Python 3.10+, pytest, Flask health endpoint.

## Global Constraints

- The WebUI is fixed to `http://127.0.0.1:5015`; do not add LAN or public listeners.
- Use the repository-local `.venv`; do not install packages into the system Python environment.
- Never overwrite an existing `.env`, and never put HeroSMS keys or account material into scripts, logs, tests, or Git.
- `启动.cmd` and `关闭.cmd` must work when double-clicked from Explorer and when the caller's current directory is elsewhere.
- Save CMD launchers with CRLF line endings and PowerShell scripts as UTF-8 with BOM for Windows PowerShell 5.1 compatibility.
- Stop only a process whose command line contains the absolute repository `app.py` path and whose executable or direct parent executable is `.venv\Scripts\python.exe`.
- Refuse to terminate a reused or mismatched PID.
- Do not add Windows service registration, startup tasks, a reverse proxy, or public exposure.

---

### Task 1: Prepare and verify the local runtime

**Files:**
- Create locally, Git-ignored: `.venv/`
- Create locally when absent, Git-ignored: `.env`

**Interfaces:**
- Consumes: system `python` or `py` launcher with Python 3.10 or newer; `requirements.txt`; `.env.example`.
- Produces: `.venv\Scripts\python.exe` with project dependencies and a local `.env` preserving all documented defaults.

- [ ] **Step 1: Select a supported Python interpreter**

Run:

```powershell
$candidate = Get-Command python -ErrorAction SilentlyContinue
if (-not $candidate) { throw '未找到 Python，请先安装 Python 3.10 或更高版本。' }
python -c "import sys; assert sys.version_info >= (3, 10), sys.version"
python --version
```

Expected: Python reports version 3.10 or newer and the assertion exits with code 0.

- [ ] **Step 2: Create the repository-local virtual environment**

Run:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip --version
```

Expected: both commands exit with code 0 and the pip path is inside this repository's `.venv`.

- [ ] **Step 3: Install dependencies**

Run:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Expected: dependency installation exits with code 0.

- [ ] **Step 4: Create local configuration without overwriting user data**

Run:

```powershell
if (-not (Test-Path -LiteralPath '.env')) {
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
}
```

Expected: `.env` exists, remains Git-ignored, and contains no real HeroSMS API key.

- [ ] **Step 5: Verify the untouched application baseline**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: the existing test suite passes before launch-control code is added.

### Task 2: Write failing launch-control tests

**Files:**
- Create: `tests/test_windows_launch_controls.py`

**Interfaces:**
- Consumes: root project path, `cmd.exe`, `powershell.exe`, `.venv`, `.env`, `/health`, and `data/server.pid`.
- Produces: opt-in Windows behavior tests activated by `RUN_LOCAL_CONTROL_INTEGRATION=1`.

- [ ] **Step 1: Add tests for wrappers, idempotent start/stop, and mismatched PID safety**

Create `tests/test_windows_launch_controls.py` with:

```python
from __future__ import annotations

import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
START_CMD = ROOT / "启动.cmd"
STOP_CMD = ROOT / "关闭.cmd"
START_SCRIPT = ROOT / "ops" / "local" / "Start-Local.ps1"
STOP_SCRIPT = ROOT / "ops" / "local" / "Stop-Local.ps1"
PID_PATH = ROOT / "data" / "server.pid"
HEALTH_URL = "http://127.0.0.1:5015/health"
RUN_INTEGRATION = (
    sys.platform == "win32"
    and os.environ.get("RUN_LOCAL_CONTROL_INTEGRATION") == "1"
)


def _run_control(script: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            *arguments,
        ],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=45,
        check=False,
    )


def _run_button(
    button: Path,
    *arguments: str,
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["cmd.exe", "/d", "/c", str(button), *arguments],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=45,
        check=False,
    )


def _health_is_available() -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=2) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def _details(result: subprocess.CompletedProcess[str]) -> str:
    return f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"


@pytest.mark.skipif(
    not RUN_INTEGRATION,
    reason="set RUN_LOCAL_CONTROL_INTEGRATION=1 on Windows",
)
def test_cmd_buttons_work_from_an_unrelated_current_directory(tmp_path: Path):
    baseline = _run_control(STOP_SCRIPT)
    assert baseline.returncode == 0, _details(baseline)

    try:
        started = _run_button(START_CMD, "-NoBrowser", cwd=tmp_path)
        assert started.returncode == 0, _details(started)
        assert _health_is_available()
    finally:
        stopped = _run_button(STOP_CMD, cwd=tmp_path)
        assert stopped.returncode == 0, _details(stopped)

    assert not _health_is_available()


@pytest.mark.skipif(
    not RUN_INTEGRATION,
    reason="set RUN_LOCAL_CONTROL_INTEGRATION=1 on Windows",
)
def test_start_is_idempotent_and_stop_is_repeatable():
    baseline = _run_control(STOP_SCRIPT)
    assert baseline.returncode == 0, _details(baseline)

    try:
        first = _run_control(START_SCRIPT, "-NoBrowser")
        assert first.returncode == 0, _details(first)
        assert _health_is_available()
        first_process_id = int(PID_PATH.read_text(encoding="utf-8").strip())

        second = _run_control(START_SCRIPT, "-NoBrowser")
        assert second.returncode == 0, _details(second)
        second_process_id = int(PID_PATH.read_text(encoding="utf-8").strip())

        assert second_process_id == first_process_id
    finally:
        stopped = _run_control(STOP_SCRIPT)
        assert stopped.returncode == 0, _details(stopped)

    assert not _health_is_available()
    repeated = _run_control(STOP_SCRIPT)
    assert repeated.returncode == 0, _details(repeated)


@pytest.mark.skipif(
    not RUN_INTEGRATION,
    reason="set RUN_LOCAL_CONTROL_INTEGRATION=1 on Windows",
)
def test_stop_refuses_to_terminate_a_mismatched_pid():
    baseline = _run_control(STOP_SCRIPT)
    assert baseline.returncode == 0, _details(baseline)
    PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    sleeper = subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-Command", "Start-Sleep -Seconds 60"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        PID_PATH.write_text(f"{sleeper.pid}\n", encoding="utf-8")
        result = _run_control(STOP_SCRIPT)

        assert result.returncode != 0, _details(result)
        assert sleeper.poll() is None
    finally:
        sleeper.terminate()
        sleeper.wait(timeout=10)
        PID_PATH.unlink(missing_ok=True)
```

- [ ] **Step 2: Run the static test and confirm it fails before implementation**

Run:

```powershell
$env:RUN_LOCAL_CONTROL_INTEGRATION = '1'
.\.venv\Scripts\python.exe -m pytest tests/test_windows_launch_controls.py::test_cmd_buttons_work_from_an_unrelated_current_directory -q
Remove-Item Env:RUN_LOCAL_CONTROL_INTEGRATION
```

Expected: FAIL with `FileNotFoundError` because `ops/local/Stop-Local.ps1` does not exist yet.

### Task 3: Implement the safe Windows start and stop controls

**Files:**
- Create: `启动.cmd`
- Create: `关闭.cmd`
- Create: `ops/local/Start-Local.ps1`
- Create: `ops/local/Stop-Local.ps1`
- Test: `tests/test_windows_launch_controls.py`

**Interfaces:**
- Consumes: `.venv\Scripts\python.exe`, absolute `app.py`, `.env`, `data/server.pid`, `GET /health`.
- Produces: exit code 0 for successful/already-satisfied operations; exit code 1 for conflicts, ownership uncertainty, or health-check failure; `logs/server.stdout.log` and `logs/server.stderr.log` for server startup output.

- [ ] **Step 1: Add the two CMD buttons**

Create `启动.cmd`:

```bat
@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0ops\local\Start-Local.ps1" %*
if errorlevel 1 (
    echo.
    echo 启动失败，请查看上面的错误信息。
    pause
    exit /b 1
)
exit /b 0
```

Create `关闭.cmd`:

```bat
@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0ops\local\Stop-Local.ps1" %*
if errorlevel 1 (
    echo.
    echo 关闭失败，请查看上面的错误信息。
    pause
    exit /b 1
)
exit /b 0
```

Save both CMD files with CRLF line endings.

- [ ] **Step 2: Implement `ops/local/Start-Local.ps1`**

Create `ops/local/Start-Local.ps1` as UTF-8 with BOM using:

```powershell
[CmdletBinding()]
param(
    [switch]$NoBrowser
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$script:ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$script:PythonPath = Join-Path $script:ProjectRoot ".venv\Scripts\python.exe"
$script:AppPath = Join-Path $script:ProjectRoot "app.py"
$script:EnvPath = Join-Path $script:ProjectRoot ".env"
$script:DataDirectory = Join-Path $script:ProjectRoot "data"
$script:LogDirectory = Join-Path $script:ProjectRoot "logs"
$script:PidPath = Join-Path $script:DataDirectory "server.pid"
$script:StdoutPath = Join-Path $script:LogDirectory "server.stdout.log"
$script:StderrPath = Join-Path $script:LogDirectory "server.stderr.log"
$script:HealthUrl = "http://127.0.0.1:5015/health"
$script:WebUrl = "http://127.0.0.1:5015"

function Get-CandidateProcess {
    param([int]$CandidateId)

    try {
        return Get-CimInstance Win32_Process -Filter "ProcessId = $CandidateId" -ErrorAction Stop
    }
    catch {
        return $null
    }
}

function Get-ProjectProcess {
    param([int]$CandidateId)

    $candidate = Get-CandidateProcess -CandidateId $CandidateId
    if ($null -eq $candidate -or [string]::IsNullOrWhiteSpace([string]$candidate.CommandLine)) {
        return $null
    }
    if ($candidate.CommandLine.IndexOf(
        $script:AppPath,
        [StringComparison]::OrdinalIgnoreCase
    ) -lt 0) {
        return $null
    }
    $expectedExecutable = [IO.Path]::GetFullPath($script:PythonPath)
    if (-not [string]::IsNullOrWhiteSpace([string]$candidate.ExecutablePath)) {
        $actualExecutable = [IO.Path]::GetFullPath([string]$candidate.ExecutablePath)
        if ([string]::Equals(
            $actualExecutable,
            $expectedExecutable,
            [StringComparison]::OrdinalIgnoreCase
        )) {
            return $candidate
        }
    }
    $parent = Get-CandidateProcess -CandidateId $candidate.ParentProcessId
    if ($null -eq $parent -or [string]::IsNullOrWhiteSpace([string]$parent.ExecutablePath)) {
        return $null
    }
    $parentExecutable = [IO.Path]::GetFullPath([string]$parent.ExecutablePath)
    if (-not [string]::Equals(
        $parentExecutable,
        $expectedExecutable,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        return $null
    }
    if ([string]::IsNullOrWhiteSpace([string]$parent.CommandLine)) {
        return $null
    }
    if ($parent.CommandLine.IndexOf(
        $script:AppPath,
        [StringComparison]::OrdinalIgnoreCase
    ) -lt 0) {
        return $null
    }
    return $candidate
}

function Get-ServerProcessId {
    if (-not (Test-Path -LiteralPath $script:PidPath)) {
        return $null
    }
    $raw = (Get-Content -LiteralPath $script:PidPath -Raw -Encoding UTF8).Trim()
    $parsed = 0
    if (-not [int]::TryParse($raw, [ref]$parsed) -or $parsed -le 0) {
        throw "PID 文件内容无效：$script:PidPath"
    }
    return $parsed
}

function Test-ServiceHealth {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $script:HealthUrl -TimeoutSec 2
        return $response.StatusCode -eq 200
    }
    catch {
        return $false
    }
}

function Test-ServicePortInUse {
    $listeners = @(Get-NetTCPConnection -State Listen -LocalPort 5015 -ErrorAction SilentlyContinue)
    return $listeners.Count -gt 0
}

function Open-LocalWebUi {
    if (-not $NoBrowser) {
        Start-Process -FilePath $script:WebUrl | Out-Null
    }
}

function Invoke-StartLocal {
    if (-not (Test-Path -LiteralPath $script:PythonPath -PathType Leaf)) {
        throw "未找到项目虚拟环境：$script:PythonPath。请先完成本地部署。"
    }
    if (-not (Test-Path -LiteralPath $script:EnvPath -PathType Leaf)) {
        throw "未找到本地配置：$script:EnvPath。请先从 .env.example 创建 .env。"
    }
    New-Item -ItemType Directory -Force -Path $script:DataDirectory | Out-Null
    New-Item -ItemType Directory -Force -Path $script:LogDirectory | Out-Null

    $serverProcessId = Get-ServerProcessId
    if ($null -ne $serverProcessId) {
        $candidate = Get-CandidateProcess -CandidateId $serverProcessId
        if ($null -eq $candidate) {
            if (Test-ServiceHealth) {
                throw "WebUI 正在运行，但 PID 文件指向的进程不存在；为避免重复启动，已停止操作。"
            }
            Remove-Item -LiteralPath $script:PidPath -Force
        }
        else {
            $owned = Get-ProjectProcess -CandidateId $serverProcessId
            if ($null -eq $owned) {
                throw "PID $serverProcessId 不属于本项目，拒绝启动或结束该进程。"
            }
            if (Test-ServiceHealth) {
                Write-Host "[就绪] 服务已经运行：$script:WebUrl" -ForegroundColor Green
                Open-LocalWebUi
                return
            }
            throw "本项目进程 $serverProcessId 存在，但健康检查失败。请先使用关闭按钮清理后重试。"
        }
    }

    if (Test-ServiceHealth) {
        throw "端口 5015 上已有健康服务，但无法证明它属于本项目，拒绝重复启动。"
    }
    if (Test-ServicePortInUse) {
        throw "端口 5015 已被其他程序占用；不会自动结束占用者。"
    }

    Remove-Item -LiteralPath $script:StdoutPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $script:StderrPath -Force -ErrorAction SilentlyContinue
    $arguments = @(
        ('"{0}"' -f $script:AppPath),
        "--host",
        "127.0.0.1",
        "--port",
        "5015"
    )
    $startParameters = @{
        FilePath = $script:PythonPath
        ArgumentList = $arguments
        WorkingDirectory = $script:ProjectRoot
        WindowStyle = "Hidden"
        RedirectStandardOutput = $script:StdoutPath
        RedirectStandardError = $script:StderrPath
        PassThru = $true
    }
    $started = Start-Process @startParameters

    $ready = $false
    try {
        $deadline = [DateTime]::UtcNow.AddSeconds(30)
        while ([DateTime]::UtcNow -lt $deadline) {
            if (Test-ServiceHealth) {
                $writtenProcessId = Get-ServerProcessId
                $owned = Get-ProjectProcess -CandidateId $writtenProcessId
                if ($null -eq $owned) {
                    throw "服务已响应，但无法确认进程属于本项目。"
                }
                if (
                    $writtenProcessId -ne $started.Id -and
                    $owned.ParentProcessId -ne $started.Id
                ) {
                    throw "服务已响应，但 PID 不是本次启动进程或其直接子进程。"
                }
                $ready = $true
                Write-Host "[成功] 服务已启动：$script:WebUrl" -ForegroundColor Green
                Open-LocalWebUi
                return
            }
            if ($started.HasExited) {
                break
            }
            Start-Sleep -Milliseconds 500
        }
        throw "服务未能在 30 秒内启动。请查看 $script:StdoutPath 和 $script:StderrPath"
    }
    finally {
        if (-not $ready) {
            $owned = Get-ProjectProcess -CandidateId $started.Id
            if ($null -ne $owned) {
                & taskkill.exe /PID $started.Id /T /F *> $null
            }
            if (Test-Path -LiteralPath $script:PidPath) {
                $writtenProcessId = Get-ServerProcessId
                if ($writtenProcessId -eq $started.Id) {
                    Remove-Item -LiteralPath $script:PidPath -Force
                }
            }
        }
    }
}

try {
    Invoke-StartLocal
    exit 0
}
catch {
    Write-Host "[错误] $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
```

- [ ] **Step 3: Implement `ops/local/Stop-Local.ps1`**

Create `ops/local/Stop-Local.ps1` as UTF-8 with BOM using:

```powershell
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$script:ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$script:PythonPath = Join-Path $script:ProjectRoot ".venv\Scripts\python.exe"
$script:AppPath = Join-Path $script:ProjectRoot "app.py"
$script:PidPath = Join-Path $script:ProjectRoot "data\server.pid"
$script:HealthUrl = "http://127.0.0.1:5015/health"

function Get-CandidateProcess {
    param([int]$CandidateId)

    try {
        return Get-CimInstance Win32_Process -Filter "ProcessId = $CandidateId" -ErrorAction Stop
    }
    catch {
        return $null
    }
}

function Get-ProjectProcess {
    param([int]$CandidateId)

    $candidate = Get-CandidateProcess -CandidateId $CandidateId
    if ($null -eq $candidate -or [string]::IsNullOrWhiteSpace([string]$candidate.CommandLine)) {
        return $null
    }
    if ($candidate.CommandLine.IndexOf(
        $script:AppPath,
        [StringComparison]::OrdinalIgnoreCase
    ) -lt 0) {
        return $null
    }
    $expectedExecutable = [IO.Path]::GetFullPath($script:PythonPath)
    if (-not [string]::IsNullOrWhiteSpace([string]$candidate.ExecutablePath)) {
        $actualExecutable = [IO.Path]::GetFullPath([string]$candidate.ExecutablePath)
        if ([string]::Equals(
            $actualExecutable,
            $expectedExecutable,
            [StringComparison]::OrdinalIgnoreCase
        )) {
            return $candidate
        }
    }
    $parent = Get-CandidateProcess -CandidateId $candidate.ParentProcessId
    if ($null -eq $parent -or [string]::IsNullOrWhiteSpace([string]$parent.ExecutablePath)) {
        return $null
    }
    $parentExecutable = [IO.Path]::GetFullPath([string]$parent.ExecutablePath)
    if (-not [string]::Equals(
        $parentExecutable,
        $expectedExecutable,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        return $null
    }
    if ([string]::IsNullOrWhiteSpace([string]$parent.CommandLine)) {
        return $null
    }
    if ($parent.CommandLine.IndexOf(
        $script:AppPath,
        [StringComparison]::OrdinalIgnoreCase
    ) -lt 0) {
        return $null
    }
    return $candidate
}

function Get-ServerProcessId {
    if (-not (Test-Path -LiteralPath $script:PidPath)) {
        return $null
    }
    $raw = (Get-Content -LiteralPath $script:PidPath -Raw -Encoding UTF8).Trim()
    $parsed = 0
    if (-not [int]::TryParse($raw, [ref]$parsed) -or $parsed -le 0) {
        throw "PID 文件内容无效：$script:PidPath"
    }
    return $parsed
}

function Test-ServiceHealth {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $script:HealthUrl -TimeoutSec 1
        return $response.StatusCode -eq 200
    }
    catch {
        return $false
    }
}

function Invoke-StopLocal {
    $serverProcessId = Get-ServerProcessId
    if ($null -eq $serverProcessId) {
        if (Test-ServiceHealth) {
            throw "WebUI 仍在运行，但缺少项目 PID 文件；为避免误杀，拒绝自动关闭。"
        }
        Write-Host "[完成] 服务已经关闭。" -ForegroundColor Green
        return
    }

    $candidate = Get-CandidateProcess -CandidateId $serverProcessId
    if ($null -eq $candidate) {
        if (Test-ServiceHealth) {
            throw "WebUI 仍在运行，但 PID $serverProcessId 不存在；为避免误杀，拒绝自动关闭。"
        }
        Remove-Item -LiteralPath $script:PidPath -Force
        Write-Host "[完成] 已清理过期 PID，服务已经关闭。" -ForegroundColor Green
        return
    }

    $owned = Get-ProjectProcess -CandidateId $serverProcessId
    if ($null -eq $owned) {
        throw "PID $serverProcessId 不属于本项目，拒绝结束该进程。"
    }

    & taskkill.exe /PID $serverProcessId /T /F *> $null
    if ($LASTEXITCODE -ne 0 -and $null -ne (Get-CandidateProcess -CandidateId $serverProcessId)) {
        throw "无法结束项目进程 $serverProcessId。"
    }

    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    while ([DateTime]::UtcNow -lt $deadline) {
        $processGone = $null -eq (Get-CandidateProcess -CandidateId $serverProcessId)
        if ($processGone -and -not (Test-ServiceHealth)) {
            break
        }
        Start-Sleep -Milliseconds 500
    }

    if ($null -ne (Get-CandidateProcess -CandidateId $serverProcessId)) {
        throw "项目进程 $serverProcessId 在 10 秒后仍未退出。"
    }
    if (Test-ServiceHealth) {
        throw "项目进程已退出，但端口 5015 上的健康服务仍然可达。"
    }
    if (Test-Path -LiteralPath $script:PidPath) {
        $writtenProcessId = Get-ServerProcessId
        if ($writtenProcessId -eq $serverProcessId) {
            Remove-Item -LiteralPath $script:PidPath -Force
        }
    }
    Write-Host "[成功] 服务已关闭。" -ForegroundColor Green
}

try {
    Invoke-StopLocal
    exit 0
}
catch {
    Write-Host "[错误] $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
```

- [ ] **Step 4: Run launch-control tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_windows_launch_controls.py -q
$env:RUN_LOCAL_CONTROL_INTEGRATION = '1'
.\.venv\Scripts\python.exe -m pytest tests/test_windows_launch_controls.py -q
Remove-Item Env:RUN_LOCAL_CONTROL_INTEGRATION
```

Expected: the first run reports three skips; the opt-in run reports three passes. The live service is stopped when the test exits.

- [ ] **Step 5: Commit the controls and tests**

Run:

```powershell
git add -- '启动.cmd' '关闭.cmd' ops/local/Start-Local.ps1 ops/local/Stop-Local.ps1 tests/test_windows_launch_controls.py
git commit -m "feat: add safe Windows local launch controls"
```

Expected: one commit containing only the launch controls and their tests.

### Task 4: Document and accept the local deployment

**Files:**
- Modify: `README.md:98`
- Test: all tracked tests plus live endpoint and process checks.

**Interfaces:**
- Consumes: the four launch-control files from Task 3.
- Produces: beginner-facing Windows usage instructions and final evidence for start, idempotence, health, stop, port release, and Git cleanliness.

- [ ] **Step 1: Add a Windows double-click section after the existing startup command**

Add this text after the WebUI URL paragraph:

```markdown
### Windows 双击启动与关闭

首次完成依赖安装和 `.env` 创建后，可以直接在资源管理器中使用：

- 双击 `启动.cmd`：检查项目进程，后台启动服务，健康检查成功后打开 WebUI。
- 双击 `关闭.cmd`：核验项目 PID 后关闭服务；重复关闭不会报错。

启动失败时查看 `logs/server.stdout.log` 和 `logs/server.stderr.log`。关闭服务会中断正在运行的批量任务；流水线状态仍会保存在本机，并在下次启动时按项目的异常恢复规则处理。
```

- [ ] **Step 2: Run the complete automated suite**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: all normal tests pass; opt-in live-control tests remain skipped unless their environment variable is set.

- [ ] **Step 3: Perform the actual user-facing acceptance flow**

Run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ops\local\Start-Local.ps1 -NoBrowser
$firstProcessId = [int](Get-Content -Raw .\data\server.pid)
(Invoke-WebRequest -UseBasicParsing -Uri http://127.0.0.1:5015/health -TimeoutSec 5).StatusCode
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ops\local\Start-Local.ps1 -NoBrowser
$secondProcessId = [int](Get-Content -Raw .\data\server.pid)
if ($firstProcessId -ne $secondProcessId) { throw '重复启动产生了第二个进程。' }
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ops\local\Stop-Local.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ops\local\Stop-Local.ps1
```

Expected: health returns `200`, both PID values are equal, and both stop calls exit with code 0.

- [ ] **Step 4: Verify the port is released and runtime secrets are untracked**

Run:

```powershell
$listener = @(Get-NetTCPConnection -State Listen -LocalPort 5015 -ErrorAction SilentlyContinue)
if ($listener.Count -ne 0) { throw '端口 5015 仍被占用。' }
git status --short
git check-ignore -v .env .venv data logs
```

Expected: no listener remains on port 5015; `.env`, `.venv`, `data`, and `logs` are ignored; Git status lists only the intended README and plan change before the final commit.

- [ ] **Step 5: Commit documentation**

Run:

```powershell
git add README.md
git commit -m "docs: explain Windows local controls"
```

Expected: the README documentation is committed and the worktree is clean.
