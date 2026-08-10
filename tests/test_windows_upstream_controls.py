from __future__ import annotations

import http.server
import os
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
CHECK_SCRIPT = ROOT / "ops" / "upstream" / "Check-Upstreams.ps1"
UPDATE_SCRIPT = ROOT / "ops" / "upstream" / "Update-Upstreams.ps1"
RUN_INTEGRATION = sys.platform == "win32" and os.environ.get("RUN_UPSTREAM_CONTROL_INTEGRATION") == "1"


def write_ps1(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8-sig")


@pytest.fixture
def control_workspace(tmp_path: Path):
    (tmp_path / "manager").mkdir()
    (tmp_path / "manager" / "upstreams.lock.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "vendor" / "GPTSession2CPAandSub2API" / "tests").mkdir(parents=True)
    (tmp_path / "vendor" / "GPTSession2CPAandSub2API" / "tests" / "convert-session.test.js").write_text(
        "// fixture\n", encoding="utf-8"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "manager-bridge.test.js").write_text("// fixture\n", encoding="utf-8")
    log_path = tmp_path / "calls.log"
    fake_python = tmp_path / "fake-python.ps1"
    fake_node = tmp_path / "fake-node.ps1"
    write_ps1(
        fake_python,
        r'''
$Remaining = @($args)
$line = "python " + ($Remaining -join " ")
Add-Content -LiteralPath $env:UPSTREAM_TEST_LOG -Value $line -Encoding UTF8
if ($line -match " manager\.upstream_sync check ") {
    Write-Output "[已是最新] fixture/project"
    $checkExit = 0
    [void][int]::TryParse($env:UPSTREAM_TEST_CHECK_EXIT, [ref]$checkExit)
    exit $checkExit
}
if ($line -match " manager\.upstream_sync plan-update ") {
    $transactionDirectory = Join-Path $env:UPSTREAM_TEST_ROOT "data\upstream-staging\fixture-transaction"
    $backupDirectory = Join-Path $env:UPSTREAM_TEST_ROOT "data\upstream-backups\fixture-transaction"
    New-Item -ItemType Directory -Force -Path $transactionDirectory,$backupDirectory | Out-Null
    $requirementsChanged = $env:UPSTREAM_TEST_REQUIREMENTS -eq "1"
    [pscustomobject]@{backup_dir=$backupDirectory} | ConvertTo-Json -Compress | Set-Content -LiteralPath (Join-Path $transactionDirectory "transaction.json") -Encoding UTF8
    if ($requirementsChanged) {
        $staged = Join-Path $transactionDirectory "extracted\receiver"
        New-Item -ItemType Directory -Force -Path $staged | Out-Null
        Set-Content -LiteralPath (Join-Path $staged "requirements.txt") -Value "fixture==1" -Encoding UTF8
    }
    [pscustomobject]@{
        transaction = (Join-Path $transactionDirectory "transaction.json")
        update_count = 1
        requirements_changed = $requirementsChanged
        projects = @([pscustomobject]@{key="receiver";added=@("new.txt");changed=@("change.txt");deleted=@("old.txt")})
    } | ConvertTo-Json -Compress -Depth 5
    exit 0
}
if ($Remaining.Count -ge 3 -and $Remaining[0] -eq "-m" -and $Remaining[1] -eq "venv") {
    New-Item -ItemType Directory -Force -Path $Remaining[2] | Out-Null
    Set-Content -LiteralPath (Join-Path $Remaining[2] "new-environment.marker") -Value "new" -Encoding UTF8
    exit 0
}
if ($line -match " -m pytest " -and $env:UPSTREAM_TEST_FAIL_PYTEST -eq "1") { exit 7 }
exit 0
'''.strip()
        + "\n",
    )
    write_ps1(
        fake_node,
        r'''
$Remaining = @($args)
Add-Content -LiteralPath $env:UPSTREAM_TEST_LOG -Value ("node " + ($Remaining -join " ")) -Encoding UTF8
if ($env:UPSTREAM_TEST_FAIL_NODE -eq "1") { exit 8 }
exit 0
'''.strip()
        + "\n",
    )
    write_ps1(
        tmp_path / "ops" / "local" / "Stop-Local.ps1",
        'Add-Content -LiteralPath $env:UPSTREAM_TEST_LOG -Value "stop" -Encoding UTF8\nexit 0\n',
    )
    write_ps1(
        tmp_path / "ops" / "local" / "Start-Local.ps1",
        'param([switch]$NoBrowser)\nAdd-Content -LiteralPath $env:UPSTREAM_TEST_LOG -Value "start" -Encoding UTF8\nexit 0\n',
    )
    return tmp_path, fake_python, fake_node, log_path


def run_script(script: Path, arguments: list[str], env: dict[str, str]):
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
        cwd=Path(env["UPSTREAM_TEST_ROOT"]).parent,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=90,
        check=False,
    )


def control_env(workspace: Path, log_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "RUN_UPSTREAM_CONTROL_INTEGRATION": "1",
            "UPSTREAM_TEST_ROOT": str(workspace),
            "UPSTREAM_TEST_LOG": str(log_path),
            "UPSTREAM_TEST_CHECK_EXIT": "0",
            "UPSTREAM_TEST_REQUIREMENTS": "0",
            "UPSTREAM_TEST_FAIL_PYTEST": "0",
            "UPSTREAM_TEST_FAIL_NODE": "0",
        }
    )
    return env


def common_arguments(workspace: Path, fake_python: Path) -> list[str]:
    return ["-TestProjectRoot", str(workspace), "-TestPythonPath", str(fake_python)]


@contextmanager
def health_server():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def log_message(self, *_args):
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.mark.skipif(not RUN_INTEGRATION, reason="set RUN_UPSTREAM_CONTROL_INTEGRATION=1 on Windows")
def test_check_wrapper_is_read_only_and_returns_cli_exit(control_workspace):
    workspace, fake_python, _fake_node, log_path = control_workspace
    sentinel = workspace / "sentinel.txt"
    sentinel.write_bytes(b"unchanged\n")
    env = control_env(workspace, log_path)

    result = run_script(CHECK_SCRIPT, common_arguments(workspace, fake_python), env)

    assert result.returncode == 0, result.stderr
    assert sentinel.read_bytes() == b"unchanged\n"
    calls = log_path.read_text(encoding="utf-8-sig").splitlines()
    assert len(calls) == 1
    assert "manager.upstream_sync check" in calls[0]
    assert "--human" in calls[0]


@pytest.mark.skipif(not RUN_INTEGRATION, reason="set RUN_UPSTREAM_CONTROL_INTEGRATION=1 on Windows")
def test_update_without_confirmation_only_checks_and_exits_three(control_workspace):
    workspace, fake_python, fake_node, log_path = control_workspace
    env = control_env(workspace, log_path)
    arguments = common_arguments(workspace, fake_python) + ["-TestNodePath", str(fake_node)]

    result = run_script(UPDATE_SCRIPT, arguments, env)

    assert result.returncode == 3
    calls = log_path.read_text(encoding="utf-8-sig")
    assert "manager.upstream_sync check" in calls
    assert "plan-update" not in calls
    assert "stop" not in calls


@pytest.mark.skipif(not RUN_INTEGRATION, reason="set RUN_UPSTREAM_CONTROL_INTEGRATION=1 on Windows")
def test_confirmed_update_runs_all_gates_in_order(control_workspace):
    workspace, fake_python, fake_node, log_path = control_workspace
    env = control_env(workspace, log_path)
    with health_server() as base_url:
        arguments = common_arguments(workspace, fake_python) + [
            "-TestNodePath", str(fake_node), "-TestHealthBaseUrl", base_url,
            "-ConfirmUpdate", "-Project", "receiver",
        ]
        result = run_script(UPDATE_SCRIPT, arguments, env)

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    calls = log_path.read_text(encoding="utf-8-sig").splitlines()
    joined = "\n".join(calls)
    expected = ["plan-update", "stop", " manager.upstream_sync apply ", " -m pytest -q", "node ", "manager-bridge.test.js", "start", " manager.upstream_sync finalize "]
    positions = [joined.index(item) for item in expected]
    assert positions == sorted(positions)


@pytest.mark.skipif(not RUN_INTEGRATION, reason="set RUN_UPSTREAM_CONTROL_INTEGRATION=1 on Windows")
def test_failed_pytest_rolls_back_and_restarts_old_service(control_workspace):
    workspace, fake_python, fake_node, log_path = control_workspace
    env = control_env(workspace, log_path)
    env["UPSTREAM_TEST_FAIL_PYTEST"] = "1"
    with health_server() as base_url:
        arguments = common_arguments(workspace, fake_python) + [
            "-TestNodePath", str(fake_node), "-TestHealthBaseUrl", base_url,
            "-ConfirmUpdate",
        ]
        result = run_script(UPDATE_SCRIPT, arguments, env)

    assert result.returncode == 1
    calls = log_path.read_text(encoding="utf-8-sig")
    assert "manager.upstream_sync rollback" in calls
    assert calls.rstrip().endswith("start")


@pytest.mark.skipif(not RUN_INTEGRATION, reason="set RUN_UPSTREAM_CONTROL_INTEGRATION=1 on Windows")
def test_dependency_switch_failure_restores_previous_virtualenv(control_workspace):
    workspace, fake_python, fake_node, log_path = control_workspace
    old_venv = workspace / ".venv"
    old_venv.mkdir()
    (old_venv / "pyvenv.cfg").write_text("home = C:\\fixture-base\n", encoding="utf-8")
    (old_venv / "old-environment.marker").write_text("old\n", encoding="utf-8")
    env = control_env(workspace, log_path)
    env["UPSTREAM_TEST_REQUIREMENTS"] = "1"
    env["UPSTREAM_TEST_FAIL_PYTEST"] = "1"
    with health_server() as base_url:
        arguments = common_arguments(workspace, fake_python) + [
            "-TestNodePath", str(fake_node),
            "-TestBasePythonPath", str(fake_python),
            "-TestHealthBaseUrl", base_url,
            "-ConfirmUpdate",
        ]
        result = run_script(UPDATE_SCRIPT, arguments, env)

    assert result.returncode == 1
    calls = log_path.read_text(encoding="utf-8-sig")
    assert (workspace / ".venv" / "old-environment.marker").read_text(encoding="utf-8") == "old\n"
    assert not (workspace / ".venv" / "new-environment.marker").exists()
    assert not (workspace / ".venv-next").exists(), f"{result.stdout}\n{result.stderr}\n{calls}"
    assert " -m venv " in calls
    assert "manager.upstream_sync rollback" in calls
