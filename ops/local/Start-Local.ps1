[CmdletBinding()]
param(
    [switch]$NoBrowser
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$script:ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$script:PythonPath = Join-Path $script:ProjectRoot ".venv\Scripts\python.exe"
$script:AppPath = Join-Path $script:ProjectRoot "manager_app.py"
$script:EnvPath = Join-Path $script:ProjectRoot ".env"
$script:DataDirectory = Join-Path $script:ProjectRoot "data"
$script:LogDirectory = Join-Path $script:ProjectRoot "logs"
$script:PidPath = Join-Path $script:DataDirectory "server.pid"
$script:StdoutPath = Join-Path $script:LogDirectory "server.stdout.log"
$script:StderrPath = Join-Path $script:LogDirectory "server.stderr.log"
$script:HealthUrl = "http://127.0.0.1:5015/health"
$script:WebUrl = "http://127.0.0.1:5015/manager"

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
