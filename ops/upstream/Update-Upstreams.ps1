[CmdletBinding()]
param(
    [switch]$ConfirmUpdate,
    [ValidateSet("all", "receiver", "converter")]
    [string]$Project = "all",
    [Parameter(DontShow = $true)]
    [string]$TestProjectRoot = "",
    [Parameter(DontShow = $true)]
    [string]$TestPythonPath = "",
    [Parameter(DontShow = $true)]
    [string]$TestBasePythonPath = "",
    [Parameter(DontShow = $true)]
    [string]$TestNodePath = "",
    [Parameter(DontShow = $true)]
    [string]$TestHealthBaseUrl = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Assert-PathInsideProject {
    param(
        [string]$ProjectRoot,
        [string]$Candidate,
        [switch]$AllowRoot
    )
    $root = [IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\')
    $resolved = [IO.Path]::GetFullPath($Candidate)
    if ($AllowRoot -and [string]::Equals($root, $resolved, [StringComparison]::OrdinalIgnoreCase)) {
        return $resolved
    }
    $prefix = $root + [IO.Path]::DirectorySeparatorChar
    if (-not $resolved.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "路径不在项目目录内：$resolved"
    }
    return $resolved
}

function Invoke-LocalControl {
    param([string]$ScriptPath, [string[]]$Arguments = @())
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $ScriptPath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "本地控制脚本失败：$ScriptPath"
    }
}

function Test-ManagerHealth {
    param([string]$BaseUrl)
    try {
        $health = Invoke-WebRequest -UseBasicParsing -Uri ($BaseUrl + "/health") -TimeoutSec 2
        $manager = Invoke-WebRequest -UseBasicParsing -Uri ($BaseUrl + "/manager") -TimeoutSec 2
        return $health.StatusCode -eq 200 -and $manager.StatusCode -eq 200
    }
    catch {
        return $false
    }
}

function Wait-ManagerHealth {
    param([string]$BaseUrl, [int]$Seconds = 30)
    $deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-ManagerHealth -BaseUrl $BaseUrl) {
            return
        }
        Start-Sleep -Milliseconds 500
    }
    throw "服务未能通过 /health 和 /manager 检查。"
}

$transactionPath = $null
$serviceStarted = $false
$environmentSwitched = $false
$previousVenv = $null
$basePython = $null
$python = $null
$projectRoot = $null
$healthBaseUrl = $null
$stopScript = $null
$startScript = $null

try {
    $hasTestOverride = (
        -not [string]::IsNullOrWhiteSpace($TestProjectRoot) -or
        -not [string]::IsNullOrWhiteSpace($TestPythonPath) -or
        -not [string]::IsNullOrWhiteSpace($TestBasePythonPath) -or
        -not [string]::IsNullOrWhiteSpace($TestNodePath) -or
        -not [string]::IsNullOrWhiteSpace($TestHealthBaseUrl)
    )
    if ($hasTestOverride -and $env:RUN_UPSTREAM_CONTROL_INTEGRATION -ne "1") {
        throw "测试路径参数只能在 RUN_UPSTREAM_CONTROL_INTEGRATION=1 时使用。"
    }
    $projectRoot = if ([string]::IsNullOrWhiteSpace($TestProjectRoot)) {
        [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
    } else {
        [IO.Path]::GetFullPath($TestProjectRoot)
    }
    $projectRoot = Assert-PathInsideProject -ProjectRoot $projectRoot -Candidate $projectRoot -AllowRoot
    $python = if ([string]::IsNullOrWhiteSpace($TestPythonPath)) {
        Join-Path $projectRoot ".venv\Scripts\python.exe"
    } else {
        [IO.Path]::GetFullPath($TestPythonPath)
    }
    $node = if ([string]::IsNullOrWhiteSpace($TestNodePath)) {
        $nodeCommand = Get-Command node.exe -ErrorAction Stop
        $nodeCommand.Source
    } else {
        [IO.Path]::GetFullPath($TestNodePath)
    }
    $healthBaseUrl = if ([string]::IsNullOrWhiteSpace($TestHealthBaseUrl)) {
        "http://127.0.0.1:5015"
    } else {
        $TestHealthBaseUrl.TrimEnd('/')
    }
    $lockPath = Join-Path $projectRoot "manager\upstreams.lock.json"
    $stopScript = Join-Path $projectRoot "ops\local\Stop-Local.ps1"
    $startScript = Join-Path $projectRoot "ops\local\Start-Local.ps1"
    foreach ($required in @($python, $lockPath)) {
        if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
            throw "缺少更新所需文件：$required"
        }
    }

    Push-Location $projectRoot
    try {
        if (-not $ConfirmUpdate) {
            & $python -m manager.upstream_sync check --workspace $projectRoot --lock $lockPath --human
            $checkExit = $LASTEXITCODE
            if ($checkExit -ne 0) {
                exit $checkExit
            }
            Write-Host "[预览] 更新只会改动锁文件声明的上游路径；不会读取或修改 .env、data/codex_accounts、logs 或 manager 集成层。" -ForegroundColor Yellow
            Write-Host "请明确确认后使用 -ConfirmUpdate；本次未修改任何文件。" -ForegroundColor Yellow
            exit 3
        }

        $planLines = @(& $python -m manager.upstream_sync plan-update --workspace $projectRoot --lock $lockPath --project $Project)
        if ($LASTEXITCODE -ne 0) {
            throw "更新预检或归档校验失败。"
        }
        $plan = ($planLines -join [Environment]::NewLine) | ConvertFrom-Json
        $transactionPath = Assert-PathInsideProject -ProjectRoot $projectRoot -Candidate ([string]$plan.transaction)
        if (-not (Test-Path -LiteralPath $transactionPath -PathType Leaf)) {
            throw "更新事务文件不存在：$transactionPath"
        }
        Write-Host "[计划] 事务：$transactionPath" -ForegroundColor Cyan
        foreach ($item in @($plan.projects)) {
            Write-Host "  $($item.key)：新增 $(@($item.added).Count)，修改 $(@($item.changed).Count)，删除 $(@($item.deleted).Count)"
        }
        if ([int]$plan.update_count -eq 0) {
            & $python -m manager.upstream_sync finalize --workspace $projectRoot --transaction $transactionPath
            if ($LASTEXITCODE -ne 0) { throw "无法完成无变更事务。" }
            Write-Host "[完成] 两个项目已经是最新版本，服务未停止。" -ForegroundColor Green
            exit 0
        }

        foreach ($required in @($node, $stopScript, $startScript)) {
            if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
                throw "缺少更新所需文件：$required"
            }
        }
        $transactionDirectory = Split-Path -Parent $transactionPath

        if ([bool]$plan.requirements_changed) {
            $venv = Assert-PathInsideProject -ProjectRoot $projectRoot -Candidate (Join-Path $projectRoot ".venv")
            $venvNext = Assert-PathInsideProject -ProjectRoot $projectRoot -Candidate (Join-Path $projectRoot ".venv-next")
            $pyvenvConfig = Join-Path $venv "pyvenv.cfg"
            if (-not (Test-Path -LiteralPath $pyvenvConfig -PathType Leaf)) {
                throw "依赖变化，但无法读取现有 pyvenv.cfg。"
            }
            if (-not [string]::IsNullOrWhiteSpace($TestBasePythonPath)) {
                $basePython = [IO.Path]::GetFullPath($TestBasePythonPath)
            }
            else {
                $homeLine = Get-Content -LiteralPath $pyvenvConfig -Encoding UTF8 | Where-Object { $_ -match '^home\s*=' } | Select-Object -First 1
                if ([string]::IsNullOrWhiteSpace([string]$homeLine)) {
                    throw "pyvenv.cfg 缺少基础 Python home。"
                }
                $baseHome = (($homeLine -split '=', 2)[1]).Trim()
                $basePython = Join-Path $baseHome "python.exe"
            }
            if (-not (Test-Path -LiteralPath $basePython -PathType Leaf)) {
                throw "基础 Python 不存在：$basePython"
            }
            if (Test-Path -LiteralPath $venvNext) {
                $verifiedNext = Assert-PathInsideProject -ProjectRoot $projectRoot -Candidate $venvNext
                Remove-Item -LiteralPath $verifiedNext -Recurse -Force
            }
            & $basePython -m venv $venvNext
            if ($LASTEXITCODE -ne 0) { throw "无法创建 .venv-next。" }
            $nextPython = if (-not [string]::IsNullOrWhiteSpace($TestPythonPath)) {
                [IO.Path]::GetFullPath($TestPythonPath)
            } else {
                Join-Path $venvNext "Scripts\python.exe"
            }
            $stagedRequirements = Join-Path $transactionDirectory "extracted\receiver\requirements.txt"
            if (-not (Test-Path -LiteralPath $stagedRequirements -PathType Leaf)) {
                throw "依赖发生变化，但 staging 中缺少 requirements.txt。"
            }
            & $nextPython -m pip install -r $stagedRequirements
            if ($LASTEXITCODE -ne 0) { throw "新虚拟环境依赖安装失败。" }
            & $nextPython -m pip install pytest
            if ($LASTEXITCODE -ne 0) { throw "新虚拟环境 pytest 安装失败。" }
        }

        Invoke-LocalControl -ScriptPath $stopScript
        [IO.File]::WriteAllText((Join-Path $transactionDirectory "service-stopped.marker"), "ok`n", [Text.UTF8Encoding]::new($false))
        & $python -m manager.upstream_sync apply --workspace $projectRoot --transaction $transactionPath
        if ($LASTEXITCODE -ne 0) { throw "上游文件应用失败。" }

        if ([bool]$plan.requirements_changed) {
            $transaction = Get-Content -LiteralPath $transactionPath -Raw -Encoding UTF8 | ConvertFrom-Json
            $backupDirectory = Assert-PathInsideProject -ProjectRoot $projectRoot -Candidate ([string]$transaction.backup_dir)
            $previousVenv = Assert-PathInsideProject -ProjectRoot $projectRoot -Candidate (Join-Path $backupDirectory "venv")
            Move-Item -LiteralPath (Join-Path $projectRoot ".venv") -Destination $previousVenv
            Move-Item -LiteralPath (Join-Path $projectRoot ".venv-next") -Destination (Join-Path $projectRoot ".venv")
            $environmentSwitched = $true
            $python = if (-not [string]::IsNullOrWhiteSpace($TestPythonPath)) {
                [IO.Path]::GetFullPath($TestPythonPath)
            } else {
                Join-Path $projectRoot ".venv\Scripts\python.exe"
            }
        }

        & $python -m pytest -q
        if ($LASTEXITCODE -ne 0) { throw "Python 测试失败。" }
        & $node (Join-Path $projectRoot "vendor\GPTSession2CPAandSub2API\tests\convert-session.test.js")
        if ($LASTEXITCODE -ne 0) { throw "转换器上游 Node 测试失败。" }
        & $node (Join-Path $projectRoot "tests\manager-bridge.test.js")
        if ($LASTEXITCODE -ne 0) { throw "本地桥接 Node 测试失败。" }
        [IO.File]::WriteAllText((Join-Path $transactionDirectory "tests-passed.marker"), "ok`n", [Text.UTF8Encoding]::new($false))

        Invoke-LocalControl -ScriptPath $startScript -Arguments @("-NoBrowser")
        $serviceStarted = $true
        Wait-ManagerHealth -BaseUrl $healthBaseUrl
        [IO.File]::WriteAllText((Join-Path $transactionDirectory "health-passed.marker"), "ok`n", [Text.UTF8Encoding]::new($false))
        & $python -m manager.upstream_sync finalize --workspace $projectRoot --transaction $transactionPath
        if ($LASTEXITCODE -ne 0) { throw "事务完成标记失败。" }
        Write-Host "[成功] 上游更新、测试和重启验证全部通过。" -ForegroundColor Green
        exit 0
    }
    finally {
        Pop-Location
    }
}
catch {
    Write-Host "[错误] $($_.Exception.Message)" -ForegroundColor Red
    try {
        $recoveryLocationSet = $false
        if ($null -ne $projectRoot) {
            Push-Location $projectRoot
            $recoveryLocationSet = $true
        }
        try {
            if ($serviceStarted -and $null -ne $stopScript) {
                Invoke-LocalControl -ScriptPath $stopScript
                $serviceStarted = $false
            }
            if ($null -ne $transactionPath -and (Test-Path -LiteralPath $transactionPath -PathType Leaf)) {
                $rollbackPython = if ($null -ne $basePython) { $basePython } else { $python }
                & $rollbackPython -m manager.upstream_sync rollback --workspace $projectRoot --transaction $transactionPath
                if ($LASTEXITCODE -ne 0) { throw "源码回滚失败，备份已保留：$transactionPath" }
            }
            if ($environmentSwitched -and $null -ne $previousVenv) {
                $activeVenv = Assert-PathInsideProject -ProjectRoot $projectRoot -Candidate (Join-Path $projectRoot ".venv")
                if (Test-Path -LiteralPath $activeVenv) {
                    Remove-Item -LiteralPath $activeVenv -Recurse -Force
                }
                Move-Item -LiteralPath $previousVenv -Destination $activeVenv
            }
            if ($null -ne $startScript -and (Test-Path -LiteralPath $startScript -PathType Leaf)) {
                Invoke-LocalControl -ScriptPath $startScript -Arguments @("-NoBrowser")
                Wait-ManagerHealth -BaseUrl $healthBaseUrl
            }
        }
        finally {
            if ($recoveryLocationSet) {
                Pop-Location
            }
        }
    }
    catch {
        Write-Host "[严重] 回滚或旧服务重启失败：$($_.Exception.Message)" -ForegroundColor Red
        if ($null -ne $transactionPath) {
            Write-Host "请保留并检查事务：$transactionPath" -ForegroundColor Yellow
        }
    }
    exit 1
}
