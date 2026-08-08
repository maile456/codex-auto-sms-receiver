[CmdletBinding()]
param(
    [Parameter(DontShow = $true)]
    [string]$TestProjectRoot = "",
    [Parameter(DontShow = $true)]
    [string]$TestPythonPath = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-TestOverrideAllowed {
    return $env:RUN_UPSTREAM_CONTROL_INTEGRATION -eq "1"
}

try {
    if (
        (-not [string]::IsNullOrWhiteSpace($TestProjectRoot) -or
         -not [string]::IsNullOrWhiteSpace($TestPythonPath)) -and
        -not (Get-TestOverrideAllowed)
    ) {
        throw "测试路径参数只能在 RUN_UPSTREAM_CONTROL_INTEGRATION=1 时使用。"
    }
    $projectRoot = if ([string]::IsNullOrWhiteSpace($TestProjectRoot)) {
        [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
    } else {
        [IO.Path]::GetFullPath($TestProjectRoot)
    }
    $python = if ([string]::IsNullOrWhiteSpace($TestPythonPath)) {
        Join-Path $projectRoot ".venv\Scripts\python.exe"
    } else {
        [IO.Path]::GetFullPath($TestPythonPath)
    }
    $lockPath = Join-Path $projectRoot "manager\upstreams.lock.json"
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw "未找到项目 Python：$python"
    }
    if (-not (Test-Path -LiteralPath $lockPath -PathType Leaf)) {
        throw "未找到上游锁文件：$lockPath"
    }
    Push-Location $projectRoot
    try {
        & $python -m manager.upstream_sync check --workspace $projectRoot --lock $lockPath --human
        $exitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
    exit $exitCode
}
catch {
    Write-Host "[错误] $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
