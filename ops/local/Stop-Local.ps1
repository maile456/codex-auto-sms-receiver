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
