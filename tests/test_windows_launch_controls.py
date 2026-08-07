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
