from __future__ import annotations

import json
import multiprocessing
import queue
import threading
import time
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.codex_service import CodexJobManager
from src.codex_worker import worker_main
from src.mailbox_store import MailboxStore


def _settings(tmp_path: Path):
    return SimpleNamespace(
        project_root=Path(__file__).resolve().parents[1],
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
    )


def _wait_for(predicate, timeout: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("condition did not become true")


def _mailboxes(tmp_path: Path, count: int = 3):
    store = MailboxStore(tmp_path / "data")
    lines = [f"user{index}@example.com----https://mail.test/{index}" for index in range(count)]
    store.import_text("generic_api", "\n".join(lines))
    return store, [f"user{index}@example.com" for index in range(count)]


def _install_fake_workers(manager, *, handler, worker_count: int):
    task_queue: queue.Queue = queue.Queue()
    result_queue: queue.Queue = queue.Queue()
    threads: list[threading.Thread] = []

    def worker():
        while True:
            task = task_queue.get()
            if task is None:
                return
            result_queue.put(handler(task))

    for index in range(worker_count):
        thread = threading.Thread(target=worker, name=f"test-worker-{index}", daemon=True)
        thread.start()
        threads.append(thread)

    def ensure_workers(_count):
        manager._task_queue = task_queue
        manager._result_queue = result_queue

    manager._ensure_workers = ensure_workers
    return task_queue, threads


def test_pipeline_honors_concurrency_and_retries_only_transient_failures(tmp_path: Path):
    mailbox_store, emails = _mailboxes(tmp_path)
    manager = CodexJobManager(_settings(tmp_path), mailbox_store)
    manager.availability = lambda: {"available": True, "reason": ""}
    attempts = defaultdict(int)
    active = 0
    maximum_active = 0
    state_lock = threading.Lock()

    def handler(task):
        nonlocal active, maximum_active
        email = task["mailbox"]["email"]
        attempts[email] += 1
        with state_lock:
            active += 1
            maximum_active = max(maximum_active, active)
        try:
            time.sleep(0.04)
            Path(task["log_path"]).parent.mkdir(parents=True, exist_ok=True)
            Path(task["log_path"]).write_text(
                "2026-07-29 01:00:00,000 [INFO] [Codex] 手机验证尝试 1/2，provider=hero, activation_id=x, 号码=+84123456789\n"
                "2026-07-29 01:00:01,000 [INFO] [Codex] 手机号验证通过\n",
                encoding="utf-8",
            )
            if email == emails[0] and attempts[email] == 1:
                return {
                    "dispatch_id": task["dispatch_id"],
                    "job_id": task["job_id"],
                    "attempt": task["attempt"],
                    "error_type": "SSLError",
                    "error": "TLS connect error curl: (35)",
                }
            return {
                "dispatch_id": task["dispatch_id"],
                "job_id": task["job_id"],
                "attempt": task["attempt"],
                "result": {
                    "ok": True,
                    "status": "success",
                    "message": "done",
                    "file_path": str(tmp_path / f"{email}.json"),
                },
            }
        finally:
            with state_lock:
                active -= 1

    _install_fake_workers(manager, handler=handler, worker_count=2)
    pipeline = manager.start_batch(
        emails,
        concurrency=2,
        retry_limit=1,
        retry_backoff_seconds=5,
    )

    def completed_pipeline():
        value = manager.pipeline_overview(pipeline["id"])
        return value if not value["active"] else None

    finished = _wait_for(completed_pipeline, timeout=8)

    assert finished["status"] == "completed"
    assert finished["counts"]["success"] == 3
    assert maximum_active == 2
    assert attempts[emails[0]] == 2
    jobs = {row["email"]: row for row in manager.list_jobs()}
    assert jobs[emails[0]]["attempt"] == 2
    assert jobs[emails[0]]["phone_number"] == "+84123456789"
    assert jobs[emails[0]]["has_credential"] is True
    state_text = (tmp_path / "data" / "pipeline-state.json").read_text(encoding="utf-8")
    assert "https://mail.test" not in state_text


def test_reauth_pipeline_skips_phone_mode_and_preserves_phone_state(tmp_path: Path):
    mailbox_store, emails = _mailboxes(tmp_path, count=1)
    mailbox_store.update_codex(
        emails[0],
        status="success",
        phone_verified=True,
        phone_number="+84123456789",
    )
    manager = CodexJobManager(_settings(tmp_path), mailbox_store)
    manager.availability = lambda **kwargs: {"available": True, "reason": ""}
    observed = {}

    def handler(task):
        observed.update(reauth=task.get("reauth"), email=task["mailbox"]["email"])
        Path(task["log_path"]).parent.mkdir(parents=True, exist_ok=True)
        Path(task["log_path"]).write_text("credential refreshed", encoding="utf-8")
        return {
            "dispatch_id": task["dispatch_id"],
            "job_id": task["job_id"],
            "attempt": task["attempt"],
            "result": {
                "ok": True,
                "status": "success",
                "message": "credential refreshed",
                "file_path": str(tmp_path / "credential.json"),
            },
        }

    _install_fake_workers(manager, handler=handler, worker_count=1)
    pipeline = manager.start_batch(
        emails,
        concurrency=1,
        retry_limit=1,
        reauth=True,
    )

    finished = _wait_for(
        lambda: (
            value
            if not (value := manager.pipeline_overview(pipeline["id"]))["active"]
            else None
        )
    )

    assert finished["status"] == "completed"
    assert finished["mode"] == "credential_reauth"
    assert observed == {"reauth": True, "email": emails[0]}
    job = manager.list_jobs()[0]
    assert job["stage"] == "凭证已刷新"
    saved = mailbox_store.get_secret(email=emails[0])
    assert saved["phone_verified"] is True
    assert saved["phone_number"] == "+84123456789"


def test_pipeline_stop_cancels_queued_but_preserves_running_result(tmp_path: Path):
    mailbox_store, emails = _mailboxes(tmp_path, count=2)
    manager = CodexJobManager(_settings(tmp_path), mailbox_store)
    manager.availability = lambda: {"available": True, "reason": ""}
    started = threading.Event()
    release = threading.Event()

    def handler(task):
        started.set()
        assert release.wait(timeout=3)
        Path(task["log_path"]).parent.mkdir(parents=True, exist_ok=True)
        Path(task["log_path"]).write_text("completed", encoding="utf-8")
        return {
            "dispatch_id": task["dispatch_id"],
            "job_id": task["job_id"],
            "attempt": task["attempt"],
            "result": {
                "ok": True,
                "status": "success",
                "message": "done",
                "file_path": str(tmp_path / "credential.json"),
            },
        }

    _install_fake_workers(manager, handler=handler, worker_count=1)
    pipeline = manager.start_batch(emails, concurrency=1, retry_limit=2)
    assert started.wait(timeout=3)
    assert manager.pause_pipeline(pipeline["id"]) is True
    assert manager.pipeline_overview(pipeline["id"])["status"] == "paused"
    assert manager.stop_pipeline(pipeline["id"]) is True
    release.set()

    def completed_pipeline():
        value = manager.pipeline_overview(pipeline["id"])
        return value if not value["active"] else None

    finished = _wait_for(completed_pipeline)
    assert finished["status"] == "stopped"
    assert finished["counts"]["success"] == 1
    assert finished["counts"]["stopped"] == 1
    assert {row["status"] for row in manager.list_jobs()} == {"success", "stopped"}


def test_pipeline_pause_finishes_inflight_and_resume_dispatches_queue(tmp_path: Path):
    mailbox_store, emails = _mailboxes(tmp_path, count=3)
    manager = CodexJobManager(_settings(tmp_path), mailbox_store)
    manager.availability = lambda: {"available": True, "reason": ""}
    first_started = threading.Event()
    release_first = threading.Event()
    dispatched: list[str] = []
    dispatch_lock = threading.Lock()

    def handler(task):
        email = task["mailbox"]["email"]
        with dispatch_lock:
            dispatched.append(email)
            position = len(dispatched)
        if position == 1:
            first_started.set()
            assert release_first.wait(timeout=3)
        return {
            "dispatch_id": task["dispatch_id"],
            "job_id": task["job_id"],
            "attempt": task["attempt"],
            "result": {
                "ok": True,
                "status": "success",
                "message": "done",
                "file_path": str(tmp_path / f"{email}.json"),
            },
        }

    _install_fake_workers(manager, handler=handler, worker_count=1)
    pipeline = manager.start_batch(emails, concurrency=1)
    assert first_started.wait(timeout=3)

    assert manager.pause_pipeline(pipeline["id"]) is True
    paused = manager.pipeline_overview(pipeline["id"])
    assert paused["status"] == "paused"
    assert paused["active"] is True
    assert paused["counts"]["running"] == 1
    assert paused["counts"]["queued"] == 2
    assert manager.pause_pipeline(pipeline["id"]) is False

    release_first.set()
    _wait_for(lambda: manager.pipeline_overview(pipeline["id"])["counts"]["success"] == 1)
    time.sleep(0.15)
    assert dispatched == [emails[0]]
    persisted = json.loads((tmp_path / "data" / "pipeline-state.json").read_text(encoding="utf-8"))
    assert persisted["pipelines"][pipeline["id"]]["status"] == "paused"

    assert manager.resume_pipeline(pipeline["id"]) is True
    finished = _wait_for(
        lambda: (
            value
            if not (value := manager.pipeline_overview(pipeline["id"]))["active"]
            else None
        )
    )
    assert finished["status"] == "completed"
    assert finished["counts"]["success"] == 3
    assert dispatched == emails
    assert manager.resume_pipeline(pipeline["id"]) is False


def test_active_pipeline_can_scale_concurrency_for_queued_jobs(tmp_path: Path):
    mailbox_store, emails = _mailboxes(tmp_path, count=3)
    manager = CodexJobManager(_settings(tmp_path), mailbox_store)
    manager.availability = lambda: {"available": True, "reason": ""}
    release = threading.Event()
    started: list[str] = []
    started_lock = threading.Lock()

    def handler(task):
        with started_lock:
            started.append(task["mailbox"]["email"])
        assert release.wait(timeout=3)
        return {
            "dispatch_id": task["dispatch_id"],
            "job_id": task["job_id"],
            "attempt": task["attempt"],
            "result": {"ok": True, "status": "success", "message": "done"},
        }

    _install_fake_workers(manager, handler=handler, worker_count=2)
    pipeline = manager.start_batch(emails, concurrency=1)
    _wait_for(lambda: len(started) == 1)

    resized = manager.set_pipeline_concurrency(pipeline["id"], 2)
    assert resized is not None
    assert resized["concurrency"] == 2
    _wait_for(lambda: len(started) == 2)
    assert manager.pipeline_overview(pipeline["id"])["counts"]["running"] == 2
    with pytest.raises(ValueError, match="并发"):
        manager.set_pipeline_concurrency(pipeline["id"], 11)

    release.set()
    _wait_for(lambda: not manager.pipeline_overview(pipeline["id"])["active"])


def test_force_pause_fails_inflight_and_ignores_late_success(tmp_path: Path):
    mailbox_store, emails = _mailboxes(tmp_path, count=2)
    manager = CodexJobManager(_settings(tmp_path), mailbox_store)
    manager.availability = lambda: {"available": True, "reason": ""}
    first_started = threading.Event()
    release_first = threading.Event()

    def handler(task):
        if task["mailbox"]["email"] == emails[0]:
            first_started.set()
            release_first.wait(timeout=3)
        return {
            "dispatch_id": task["dispatch_id"],
            "job_id": task["job_id"],
            "attempt": task["attempt"],
            "result": {"ok": True, "status": "success", "message": "late success"},
        }

    _install_fake_workers(manager, handler=handler, worker_count=1)
    pipeline = manager.start_batch(emails, concurrency=1, retry_limit=2)
    assert first_started.wait(timeout=3)

    paused = manager.force_pause_pipeline(pipeline["id"])
    assert paused is not None
    assert paused["status"] == "paused"
    assert paused["force_paused_count"] == 1
    assert paused["counts"]["failed"] == 1
    assert paused["counts"]["queued"] == 1
    failed_job = next(row for row in manager.list_jobs() if row["email"] == emails[0])
    assert failed_job["failure_code"] == "force_paused"
    assert failed_job["retryable"] is False

    release_first.set()
    time.sleep(0.1)
    failed_job = next(row for row in manager.list_jobs() if row["email"] == emails[0])
    assert failed_job["status"] == "failed"
    assert manager.resume_pipeline(pipeline["id"]) is True
    finished = _wait_for(
        lambda: (
            value
            if not (value := manager.pipeline_overview(pipeline["id"]))["active"]
            else None
        )
    )
    assert finished["counts"]["failed"] == 1
    assert finished["counts"]["success"] == 1


def test_pipeline_rejects_unsafe_limits_and_second_active_batch(tmp_path: Path):
    mailbox_store, emails = _mailboxes(tmp_path, count=1)
    manager = CodexJobManager(_settings(tmp_path), mailbox_store)
    manager.availability = lambda: {"available": True, "reason": ""}

    with pytest.raises(ValueError, match="并发"):
        manager.start_batch(emails, concurrency=11)
    with pytest.raises(ValueError, match="失败重试"):
        manager.start_batch(emails, retry_limit=4)

    blocker = threading.Event()

    def handler(task):
        blocker.wait(timeout=3)
        return {
            "dispatch_id": task["dispatch_id"],
            "job_id": task["job_id"],
            "attempt": task["attempt"],
            "result": {"ok": False, "status": "failed", "message": "permanent"},
        }

    _install_fake_workers(manager, handler=handler, worker_count=1)
    pipeline = manager.start_batch(emails)
    _wait_for(lambda: manager.pipeline_overview(pipeline["id"])["counts"].get("running"))
    with pytest.raises(RuntimeError, match="已有流水线"):
        manager.start_batch(emails)
    blocker.set()


def test_pipeline_concurrency_limit_defaults_to_ten_and_is_configurable(
    monkeypatch, tmp_path: Path
):
    mailbox_store, _ = _mailboxes(tmp_path, count=1)
    monkeypatch.delenv("CODEX_PIPELINE_MAX_CONCURRENCY", raising=False)
    manager = CodexJobManager(_settings(tmp_path), mailbox_store)

    assert manager.runtime_config()["pipeline_max_concurrency"] == 10
    monkeypatch.setenv("CODEX_PIPELINE_MAX_CONCURRENCY", "12")
    assert manager.runtime_config()["pipeline_max_concurrency"] == 12
    monkeypatch.setenv("CODEX_PIPELINE_MAX_CONCURRENCY", "99")
    assert manager.runtime_config()["pipeline_max_concurrency"] == 20


def test_success_callback_receives_email_and_credential_path_once(tmp_path: Path):
    mailbox_store, emails = _mailboxes(tmp_path, count=1)
    manager = CodexJobManager(_settings(tmp_path), mailbox_store)
    manager.availability = lambda: {"available": True, "reason": ""}
    credential_path = tmp_path / "credential.json"
    callbacks = []
    manager.set_success_callback(lambda email, path: callbacks.append((email, path)))

    def handler(task):
        credential_path.write_text("{}", encoding="utf-8")
        return {
            "dispatch_id": task["dispatch_id"],
            "job_id": task["job_id"],
            "attempt": task["attempt"],
            "result": {
                "ok": True,
                "status": "success",
                "message": "done",
                "file_path": str(credential_path),
            },
        }

    _install_fake_workers(manager, handler=handler, worker_count=1)
    pipeline = manager.start_batch(emails, concurrency=1)
    finished = _wait_for(
        lambda: (
            value
            if not (value := manager.pipeline_overview(pipeline["id"]))["active"]
            else None
        )
    )

    assert finished["counts"]["success"] == 1
    assert callbacks == [(emails[0], str(credential_path))]


def test_success_callback_error_does_not_change_job_success(tmp_path: Path):
    mailbox_store, emails = _mailboxes(tmp_path, count=1)
    manager = CodexJobManager(_settings(tmp_path), mailbox_store)
    manager.availability = lambda: {"available": True, "reason": ""}

    def broken_callback(_email, _path):
        raise RuntimeError("fixture callback failure")

    manager.set_success_callback(broken_callback)

    def handler(task):
        return {
            "dispatch_id": task["dispatch_id"],
            "job_id": task["job_id"],
            "attempt": task["attempt"],
            "result": {
                "ok": True,
                "status": "success",
                "message": "done",
                "file_path": str(tmp_path / "credential.json"),
            },
        }

    _install_fake_workers(manager, handler=handler, worker_count=1)
    pipeline = manager.start_batch(emails, concurrency=1)
    finished = _wait_for(
        lambda: (
            value
            if not (value := manager.pipeline_overview(pipeline["id"]))["active"]
            else None
        )
    )

    assert finished["counts"]["success"] == 1
    assert manager.list_jobs()[0]["status"] == "success"


def test_generic_mailbox_timeouts_and_proxy_errors_are_retryable(tmp_path: Path):
    mailbox_store, _ = _mailboxes(tmp_path, count=1)
    manager = CodexJobManager(_settings(tmp_path), mailbox_store)

    assert manager._failure_info(
        "GenericApiMailError: 等待通用 API 验证码超时；HTTP 200 但未提取到验证码"
    ) == ("mailbox_otp_timeout", True, 15)
    assert manager._failure_info(
        "GenericApiMailError: 网络请求失败（ProxyError）"
    ) == ("transient_network", True, 0)
    assert manager._failure_info(
        "任务执行超时：单次执行超过 600 秒，已终止本轮并释放执行槽位"
    ) == ("task_timeout", True, 15)


def test_pipeline_hard_timeout_finishes_stuck_attempt(tmp_path: Path):
    mailbox_store, emails = _mailboxes(tmp_path, count=1)
    manager = CodexJobManager(_settings(tmp_path), mailbox_store)
    manager.availability = lambda: {"available": True, "reason": ""}
    manager._attempt_timeout_seconds = lambda: 1
    release = threading.Event()

    def handler(task):
        release.wait(timeout=3)
        return {
            "dispatch_id": task["dispatch_id"],
            "job_id": task["job_id"],
            "attempt": task["attempt"],
            "result": {"ok": True, "status": "success", "message": "late result"},
        }

    _install_fake_workers(manager, handler=handler, worker_count=1)
    pipeline = manager.start_batch(emails, retry_limit=0)
    finished = _wait_for(
        lambda: (
            value
            if not (value := manager.pipeline_overview(pipeline["id"]))["active"]
            else None
        ),
        timeout=4,
    )
    release.set()

    assert finished["counts"]["failed"] == 1
    job = manager.list_jobs()[0]
    assert job["failure_code"] == "task_timeout"
    assert job["attempt_deadline_at"] is None
    assert job["attempt_finished_at"]


def test_isolated_worker_process_starts_and_stops_cleanly(tmp_path: Path):
    context = multiprocessing.get_context("spawn")
    task_queue = context.Queue()
    result_queue = context.Queue()
    process = context.Process(
        target=worker_main,
        args=(_settings(tmp_path), task_queue, result_queue),
    )
    process.start()
    task_queue.put(None)
    process.join(timeout=10)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
    assert process.exitcode == 0


def test_pipeline_state_recovers_interrupted_jobs_after_restart(tmp_path: Path):
    mailbox_store, emails = _mailboxes(tmp_path, count=1)
    state = {
        "version": 1,
        "pipelines": {
            "pipeline-old": {
                "id": "pipeline-old",
                "status": "running",
                "concurrency": 1,
                "retry_limit": 1,
                "job_ids": ["job-old"],
                "created_at": "2026-07-29T00:00:00+00:00",
            }
        },
        "jobs": {
            "job-old": {
                "id": "job-old",
                "pipeline_id": "pipeline-old",
                "email": emails[0],
                "status": "running",
                "created_at": "2026-07-29T00:00:00+00:00",
                "log_paths": [],
            }
        },
    }
    path = tmp_path / "data" / "pipeline-state.json"
    path.write_text(json.dumps(state), encoding="utf-8")

    manager = CodexJobManager(_settings(tmp_path), mailbox_store)

    assert manager.pipeline_overview("pipeline-old")["status"] == "interrupted"
    job = manager.list_jobs()[0]
    assert job["status"] == "failed"
    assert job["failure_code"] == "service_restarted"
    assert mailbox_store.list_accounts()[0]["codex_status"] == "failed"


def test_paused_pipeline_survives_restart_and_can_be_resumed(monkeypatch, tmp_path: Path):
    mailbox_store, emails = _mailboxes(tmp_path, count=1)
    state = {
        "version": 1,
        "pipelines": {
            "pipeline-paused": {
                "id": "pipeline-paused",
                "status": "paused",
                "pause_requested": True,
                "concurrency": 1,
                "retry_limit": 1,
                "job_ids": ["job-paused"],
                "created_at": "2026-07-29T00:00:00+00:00",
            }
        },
        "jobs": {
            "job-paused": {
                "id": "job-paused",
                "pipeline_id": "pipeline-paused",
                "email": emails[0],
                "status": "running",
                "attempt": 1,
                "max_attempts": 2,
                "created_at": "2026-07-29T00:00:00+00:00",
                "log_paths": [],
            }
        },
    }
    path = tmp_path / "data" / "pipeline-state.json"
    path.write_text(json.dumps(state), encoding="utf-8")
    launched = []
    monkeypatch.setattr(CodexJobManager, "_ensure_workers", lambda self, count: None)
    monkeypatch.setattr(
        CodexJobManager,
        "_launch_pipeline_thread",
        lambda self, pipeline_id, mailboxes: launched.append((pipeline_id, mailboxes)),
    )

    manager = CodexJobManager(_settings(tmp_path), mailbox_store)

    assert manager.pipeline_overview("pipeline-paused")["status"] == "paused"
    job = manager.list_jobs()[0]
    assert job["status"] == "retry_wait"
    assert job["failure_code"] == "service_restarted_paused"
    assert launched[0][0] == "pipeline-paused"
    assert launched[0][1]["job-paused"]["email"] == emails[0]


def test_public_job_ignores_deleted_or_outside_log_paths(tmp_path: Path):
    mailbox_store, _ = _mailboxes(tmp_path, count=1)
    manager = CodexJobManager(_settings(tmp_path), mailbox_store)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    existing = log_dir / "codex-account-task.log"
    existing.write_text("safe log", encoding="utf-8")
    outside = tmp_path / "outside.log"
    outside.write_text("outside", encoding="utf-8")

    public = manager._public_job(
        {
            "id": "job-old",
            "message": "done",
            "log_path": str(log_dir / "deleted.log"),
            "log_paths": [str(existing), str(existing), str(outside)],
        }
    )

    assert public["has_log"] is True
    assert public["log_count"] == 1
    assert "log_path" not in public
    assert "log_paths" not in public

    existing.unlink()
    public = manager._public_job(
        {
            "id": "job-old",
            "message": "done",
            "log_path": str(existing),
            "log_paths": [str(outside)],
        }
    )
    assert public["has_log"] is False
    assert public["log_count"] == 0
