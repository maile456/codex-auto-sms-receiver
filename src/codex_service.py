from __future__ import annotations

import importlib.util
import json
import logging
import multiprocessing
import os
import queue
import re
import threading
import time
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .artifact_store import _redact_log_text
from .mailbox_store import MailboxStore
from .settings import Settings
from .sms_config import normalize_hero_countries
from .upstream_location import resolve_upstream_root


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _after(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=max(0, seconds))).isoformat()


def _parse_time(value: Any) -> float:
    try:
        return datetime.fromisoformat(str(value or "").replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


_PHONE_ATTEMPT = re.compile(r"手机验证尝试\s+\d+\s*/\s*\d+.*?号码=\+?(\d{7,15})")


def _project_env(project_root: Path, name: str, default: str = "") -> str:
    """Read a setting from the process first, then the receiver .env file."""
    value = os.getenv(name)
    if value not in (None, ""):
        return str(value)
    try:
        from dotenv import dotenv_values

        loaded = dotenv_values(project_root / ".env")
        raw = loaded.get(name)
        if raw not in (None, ""):
            return str(raw)
    except Exception:
        pass
    return default


class CodexJobManager:
    """Persistent batch scheduler for existing-account Codex OAuth jobs."""

    _ACTIVE_JOBS = {"queued", "running", "retry_wait"}
    _TERMINAL_JOBS = {"success", "failed", "stopped", "deactivated", "skipped"}
    _ACTIVE_PIPELINES = {"queued", "running", "paused", "stopping"}
    _MAX_CONCURRENCY = 10
    _MAX_RETRY_LIMIT = 3
    _MAX_BATCH_SIZE = 200
    _DEFAULT_ATTEMPT_TIMEOUT_SECONDS = 600

    def __init__(self, settings: Settings, mailbox_store: MailboxStore):
        self.settings = settings
        self.mailbox_store = mailbox_store
        self.upstream_root = resolve_upstream_root(settings.project_root)
        self._jobs: dict[str, dict[str, Any]] = {}
        self._pipelines: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._data_dir = Path(getattr(settings, "data_dir", mailbox_store.data_dir))
        self._log_dir = Path(getattr(settings, "log_dir", settings.project_root / "logs"))
        self._state_path = self._data_dir / "pipeline-state.json"
        self._mp_context = multiprocessing.get_context("spawn")
        self._task_queue = None
        self._result_queue = None
        self._workers: list[Any] = []
        self._active_dispatches: dict[str, dict[str, Any]] = {}
        self._pipeline_threads: dict[str, threading.Thread] = {}
        self._success_callback: Callable[[str, str | None], None] | None = None
        self._load_state()
        self._recover_interrupted()

    def set_success_callback(
        self, callback: Callable[[str, str | None], None] | None
    ) -> None:
        """Register a non-blocking callback for newly saved OAuth credentials."""

        self._success_callback = callback

    def availability(self, *, reauth: bool = False) -> dict:
        if not (self.upstream_root / "core" / "codex_oauth.py").is_file():
            return {"available": False, "reason": "未找到原项目 core/codex_oauth.py"}
        missing = [name for name in ("curl_cffi", "Crypto", "pyotp") if importlib.util.find_spec(name) is None]
        if missing:
            return {"available": False, "reason": "缺少依赖：" + ", ".join(missing)}
        driver = _project_env(self.settings.project_root, "CODEX_OAUTH_DRIVER", "protocol").strip().lower()
        auth_source = _project_env(self.settings.project_root, "CODEX_AUTH_URL_SOURCE", "local").strip().lower()
        if driver not in {"protocol", "api", "http", "roxy", "roxybrowser", "fingerprint", "browser"}:
            return {"available": False, "reason": f"不支持的 OAuth 驱动：{driver}"}
        if auth_source == "cpa" and not _project_env(self.settings.project_root, "CPA_MANAGEMENT_KEY").strip():
            return {"available": False, "reason": "CPA 模式缺少 CPA_MANAGEMENT_KEY"}
        if not reauth and not _project_env(self.settings.project_root, "HERO_SMS_API_KEY").strip():
            return {"available": False, "reason": "Hero SMS 缺少 HERO_SMS_API_KEY"}
        try:
            countries = normalize_hero_countries(
                _project_env(self.settings.project_root, "HERO_SMS_COUNTRIES")
            )
        except ValueError:
            countries = []
        if not reauth and not countries:
            return {"available": False, "reason": "Hero SMS 至少需要选择 1 个国家"}
        return {"available": True, "reason": ""}

    def runtime_config(self) -> dict:
        return {
            "driver": _project_env(self.settings.project_root, "CODEX_OAUTH_DRIVER", "protocol"),
            "auth_source": _project_env(self.settings.project_root, "CODEX_AUTH_URL_SOURCE", "local") or "local",
            "sms_provider": "hero",
            "outlook_fetch_mode": os.getenv("OUTLOOK_FETCH_MODE", "direct") or "direct",
            "pipeline_max_concurrency": self._max_concurrency(),
            "pipeline_max_retries": self._MAX_RETRY_LIMIT,
            "pipeline_attempt_timeout_seconds": self._attempt_timeout_seconds(),
        }

    def _attempt_timeout_seconds(self) -> int:
        """Return the hard deadline for one dispatched worker attempt."""

        try:
            value = int(os.getenv("CODEX_JOB_TIMEOUT_SECONDS", "") or self._DEFAULT_ATTEMPT_TIMEOUT_SECONDS)
        except (TypeError, ValueError):
            value = self._DEFAULT_ATTEMPT_TIMEOUT_SECONDS
        return max(120, min(3600, value))

    def _max_concurrency(self) -> int:
        """Return the configured worker ceiling while keeping resource use bounded."""

        try:
            value = int(os.getenv("CODEX_PIPELINE_MAX_CONCURRENCY", "") or self._MAX_CONCURRENCY)
        except (TypeError, ValueError):
            value = self._MAX_CONCURRENCY
        return max(1, min(20, value))

    @staticmethod
    def _otp_ready(mailbox: dict[str, Any]) -> bool:
        if mailbox.get("source") in {"generic_api", "code_url"}:
            return bool(str(mailbox.get("code_url") or "").strip())
        if mailbox.get("source") == "password_totp":
            return bool(
                str(mailbox.get("password") or "").strip()
                and str(mailbox.get("totp_secret") or "").strip()
            )
        return bool(str(mailbox.get("client_id") or "").strip() and str(mailbox.get("refresh_token") or "").strip())

    def _load_state(self) -> None:
        if not self._state_path.is_file():
            return
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        jobs = payload.get("jobs")
        pipelines = payload.get("pipelines")
        if isinstance(jobs, dict):
            self._jobs = {str(key): value for key, value in jobs.items() if isinstance(value, dict)}
        if isinstance(pipelines, dict):
            self._pipelines = {
                str(key): value for key, value in pipelines.items() if isinstance(value, dict)
            }

    def _persist_locked(self) -> None:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        temporary = self._data_dir / f".{self._state_path.name}.{uuid.uuid4().hex}.tmp"
        payload = {"version": 1, "pipelines": self._pipelines, "jobs": self._jobs}
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self._state_path)

    def _recover_interrupted(self) -> None:
        changed = False
        paused_to_restore: list[tuple[str, int, dict[str, dict[str, Any]]]] = []
        with self._lock:
            paused_pipeline_ids = {
                str(pipeline_id)
                for pipeline_id, pipeline in self._pipelines.items()
                if str(pipeline.get("status") or "") == "paused"
            }
            for job in self._jobs.values():
                status = str(job.get("status") or "")
                pipeline_id = str(job.get("pipeline_id") or "")
                if pipeline_id in paused_pipeline_ids and status in self._ACTIVE_JOBS:
                    if status == "running":
                        job.update(
                            status="retry_wait",
                            stage="暂停中",
                            message="服务重启后已放回暂停队列，继续流水线时会重新执行",
                            failure_code="service_restarted_paused",
                            retryable=True,
                            next_retry_at=_now(),
                            started_at=None,
                            finished_at=None,
                        )
                        changed = True
                    continue
                if status in self._ACTIVE_JOBS:
                    job.update(
                        status="failed",
                        stage="服务已重启",
                        message="服务重启中断了上一次任务，可重新加入流水线",
                        failure_code="service_restarted",
                        retryable=False,
                        next_retry_at=None,
                        finished_at=_now(),
                    )
                    changed = True
            for pipeline_id, pipeline in self._pipelines.items():
                if str(pipeline.get("status") or "") == "paused":
                    pipeline.update(pause_requested=True, finished_at=None)
                    mailboxes = {}
                    for job_id in pipeline.get("job_ids") or []:
                        job = self._jobs.get(str(job_id))
                        if not job or str(job.get("status") or "") not in self._ACTIVE_JOBS:
                            continue
                        mailbox = self.mailbox_store.get_secret(email=str(job.get("email") or ""))
                        if mailbox is not None:
                            mailboxes[str(job_id)] = mailbox
                        else:
                            job.update(
                                status="failed",
                                stage="登录素材不存在",
                                message="服务重启后未找到账号登录素材",
                                failure_code="mailbox_missing",
                                retryable=False,
                                next_retry_at=None,
                                finished_at=_now(),
                            )
                            changed = True
                    if mailboxes:
                        paused_to_restore.append(
                            (str(pipeline_id), int(pipeline.get("concurrency") or 1), mailboxes)
                        )
                    continue
                if str(pipeline.get("status") or "") in self._ACTIVE_PIPELINES:
                    pipeline.update(status="interrupted", finished_at=_now())
                    changed = True
            if changed:
                self._persist_locked()
        if changed:
            for job in self._jobs.values():
                if job.get("failure_code") == "service_restarted":
                    self.mailbox_store.update_codex(
                        str(job.get("email") or ""),
                        status="failed",
                        message="服务重启中断了上一次任务",
                    )
        for pipeline_id, concurrency, mailboxes in paused_to_restore:
            self._ensure_workers(concurrency)
            self._launch_pipeline_thread(pipeline_id, mailboxes)

    def _launch_pipeline_thread(
        self, pipeline_id: str, mailboxes: dict[str, dict[str, Any]]
    ) -> None:
        thread = threading.Thread(
            target=self._run_pipeline,
            args=(pipeline_id, mailboxes),
            name=f"codex-pipeline-{pipeline_id[:8]}",
            daemon=True,
        )
        self._pipeline_threads[pipeline_id] = thread
        thread.start()

    def _public_job(self, item: dict) -> dict:
        row = deepcopy(item)
        current_log = row.pop("log_path", None)
        log_paths = row.pop("log_paths", [])
        credential_path = row.pop("credential_path", None)
        candidates = list(log_paths) if isinstance(log_paths, list) else []
        if current_log:
            candidates.append(current_log)
        existing_logs: set[str] = set()
        try:
            log_root = self._log_dir.resolve()
        except OSError:
            log_root = self._log_dir
        for value in candidates:
            try:
                resolved = Path(str(value or "")).resolve(strict=True)
                resolved.relative_to(log_root)
            except (OSError, ValueError):
                continue
            if resolved.is_file():
                existing_logs.add(str(resolved))
        row["has_log"] = bool(existing_logs)
        row["log_count"] = len(existing_logs)
        row["has_credential"] = bool(credential_path)
        row["message"] = _redact_log_text(str(row.get("message") or ""))[:500]
        return row

    def list_jobs(self) -> list[dict]:
        with self._lock:
            rows = [self._public_job(item) for item in self._jobs.values()]
        rows.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return rows[:500]

    def latest_job_for_email(self, email: str) -> dict | None:
        """Return one public job without materializing every account's history."""

        target = str(email or "").strip().casefold()
        if not target:
            return None
        with self._lock:
            candidates = [
                item
                for item in self._jobs.values()
                if str(item.get("email") or "").strip().casefold() == target
            ]
            selected = max(
                candidates,
                key=lambda item: str(
                    item.get("updated_at")
                    or item.get("finished_at")
                    or item.get("started_at")
                    or item.get("created_at")
                    or ""
                ),
                default=None,
            )
            return self._public_job(selected) if selected is not None else None

    def _pipeline_public_locked(self, pipeline: dict[str, Any]) -> dict[str, Any]:
        job_ids = [str(value) for value in pipeline.get("job_ids") or []]
        jobs = [self._jobs[job_id] for job_id in job_ids if job_id in self._jobs]
        count_names = ("queued", "running", "retry_wait", "success", "failed", "stopped", "deactivated", "skipped")
        counts = {name: sum(1 for job in jobs if job.get("status") == name) for name in count_names}
        terminal = sum(counts[name] for name in self._TERMINAL_JOBS)
        public = {key: deepcopy(value) for key, value in pipeline.items() if key != "job_ids"}
        public.update(
            {
                "total": len(jobs),
                "counts": counts,
                "completed": terminal,
                "progress": round((terminal / len(jobs)) * 100, 1) if jobs else 0.0,
                "active": str(pipeline.get("status") or "") in self._ACTIVE_PIPELINES,
            }
        )
        return public

    def pipeline_overview(self, pipeline_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            selected = self._pipelines.get(str(pipeline_id or "")) if pipeline_id else None
            if selected is None:
                active = [
                    item
                    for item in self._pipelines.values()
                    if str(item.get("status") or "") in self._ACTIVE_PIPELINES
                ]
                candidates = active or list(self._pipelines.values())
                selected = max(candidates, key=lambda item: str(item.get("created_at") or "")) if candidates else None
            if selected is None:
                return {
                    "id": "",
                    "status": "idle",
                    "active": False,
                    "concurrency": 1,
                    "retry_limit": 0,
                    "total": 0,
                    "completed": 0,
                    "progress": 0.0,
                    "counts": {},
                }
            return self._pipeline_public_locked(selected)

    def _has_active_pipeline_locked(self) -> bool:
        return any(
            str(item.get("status") or "") in self._ACTIVE_PIPELINES
            for item in self._pipelines.values()
        )

    def start(self, email: str) -> dict:
        pipeline = self.start_batch([email], concurrency=1, retry_limit=0)
        pipeline_id = str(pipeline["id"])
        with self._lock:
            job = next(item for item in self._jobs.values() if item.get("pipeline_id") == pipeline_id)
            return self._public_job(job)

    def start_batch(
        self,
        emails: Iterable[str],
        *,
        concurrency: int = 1,
        retry_limit: int = 0,
        retry_backoff_seconds: int = 30,
        reauth: bool = False,
    ) -> dict[str, Any]:
        try:
            concurrency = int(concurrency)
            retry_limit = int(retry_limit)
            retry_backoff_seconds = int(retry_backoff_seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError("流水线并发和重试参数必须是整数") from exc
        max_concurrency = self._max_concurrency()
        if concurrency < 1 or concurrency > max_concurrency:
            raise ValueError(f"任务并发必须在 1 - {max_concurrency} 之间")
        if retry_limit < 0 or retry_limit > self._MAX_RETRY_LIMIT:
            raise ValueError(f"失败重试必须在 0 - {self._MAX_RETRY_LIMIT} 之间")
        if retry_backoff_seconds < 5 or retry_backoff_seconds > 600:
            raise ValueError("重试间隔必须在 5 - 600 秒之间")

        normalized: list[str] = []
        seen: set[str] = set()
        for email in emails:
            value = str(email or "").strip()
            key = value.casefold()
            if value and key not in seen:
                normalized.append(value)
                seen.add(key)
        if not normalized:
            raise ValueError("请至少选择 1 个账号")
        if len(normalized) > self._MAX_BATCH_SIZE:
            raise ValueError(f"单批最多处理 {self._MAX_BATCH_SIZE} 个账号")
        reauth = bool(reauth)
        availability = self.availability(reauth=True) if reauth else self.availability()
        if not availability["available"]:
            raise RuntimeError(availability["reason"])

        mailboxes: list[dict[str, Any]] = []
        for email in normalized:
            mailbox = self.mailbox_store.get_secret(email=email)
            if mailbox is None:
                raise ValueError(f"账号未导入：{email}")
            if not self._otp_ready(mailbox):
                raise ValueError(f"邮箱 OTP 配置未就绪：{email}")
            mailboxes.append(mailbox)

        self._ensure_workers(concurrency)
        pipeline_id = uuid.uuid4().hex
        created_at = _now()
        job_mailboxes: dict[str, dict[str, Any]] = {}
        with self._lock:
            if self._has_active_pipeline_locked():
                raise RuntimeError("已有流水线正在运行")
            job_ids: list[str] = []
            for mailbox in mailboxes:
                job_id = uuid.uuid4().hex
                job_ids.append(job_id)
                job_mailboxes[job_id] = mailbox
                self._jobs[job_id] = {
                    "id": job_id,
                    "pipeline_id": pipeline_id,
                    "account_id": mailbox.get("id"),
                    "email": mailbox.get("email"),
                    "source": mailbox.get("source"),
                    "status": "queued",
                    "stage": "等待执行",
                    "message": "已加入流水线",
                    "attempt": 0,
                    "max_attempts": 1 + retry_limit,
                    "failure_code": "",
                    "retryable": False,
                    "next_retry_at": None,
                    "stop_requested": False,
                    "created_at": created_at,
                    "started_at": None,
                    "finished_at": None,
                    "log_path": None,
                    "log_paths": [],
                    "credential_path": None,
                    "phone_verified": False,
                    "reauth": reauth,
                }
            self._pipelines[pipeline_id] = {
                "id": pipeline_id,
                "status": "queued",
                "concurrency": concurrency,
                "retry_limit": retry_limit,
                "retry_backoff_seconds": retry_backoff_seconds,
                "attempt_timeout_seconds": self._attempt_timeout_seconds(),
                "mode": "credential_reauth" if reauth else "login_and_verify",
                "pause_requested": False,
                "stop_requested": False,
                "created_at": created_at,
                "started_at": None,
                "paused_at": None,
                "resumed_at": None,
                "finished_at": None,
                "job_ids": job_ids,
            }
            self._persist_locked()
            public = self._pipeline_public_locked(self._pipelines[pipeline_id])

        self._launch_pipeline_thread(pipeline_id, job_mailboxes)
        return public

    def _ensure_workers(self, count: int) -> None:
        from .codex_worker import WorkerSettings, worker_main

        with self._lock:
            self._workers = [worker for worker in self._workers if worker.is_alive()]
            if self._task_queue is None:
                self._task_queue = self._mp_context.Queue()
                self._result_queue = self._mp_context.Queue()
            while len(self._workers) < count:
                worker = self._mp_context.Process(
                    target=worker_main,
                    args=(
                        WorkerSettings(
                            project_root=Path(self.settings.project_root),
                            data_dir=self._data_dir,
                        ),
                        self._task_queue,
                        self._result_queue,
                    ),
                    name=f"codex-worker-{len(self._workers) + 1}",
                    daemon=True,
                )
                worker.start()
                self._workers.append(worker)

    def _terminate_worker(self, worker_pid: int | None) -> bool:
        """Terminate one timed-out worker and remove it from the live pool."""

        if not worker_pid:
            return False
        terminated = False
        with self._lock:
            for worker in list(self._workers):
                if int(getattr(worker, "pid", 0) or 0) != int(worker_pid):
                    continue
                if worker.is_alive():
                    worker.terminate()
                    worker.join(timeout=2)
                terminated = True
                break
            self._workers = [worker for worker in self._workers if worker.is_alive()]
        return terminated

    def _dispatch_locked(
        self,
        job: dict[str, Any],
        mailbox: dict[str, Any],
        *,
        timeout_seconds: int,
    ) -> str:
        job["attempt"] = int(job.get("attempt") or 0) + 1
        attempt = int(job["attempt"])
        log_path = self._log_dir / (
            f"codex-{job.get('account_id')}-{job['id'][:8]}-a{attempt}.log"
        )
        dispatch_id = uuid.uuid4().hex
        attempt_started_at = _now()
        job.update(
            status="running",
            stage="重新登录刷新凭证" if job.get("reauth") else "登录与授权",
            message=(
                f"正在重新登录刷新凭证（第 {attempt}/{job['max_attempts']} 次）"
                if job.get("reauth")
                else f"正在执行第 {attempt}/{job['max_attempts']} 次"
            ),
            retryable=False,
            next_retry_at=None,
            started_at=job.get("started_at") or _now(),
            attempt_started_at=attempt_started_at,
            attempt_deadline_at=_after(timeout_seconds),
            finished_at=None,
            log_path=str(log_path),
        )
        job.setdefault("log_paths", []).append(str(log_path))
        self.mailbox_store.update_codex(
            str(job.get("email") or ""),
            status="running",
            message=(
                f"正在重新登录刷新凭证（第 {attempt}/{job['max_attempts']} 次）"
                if job.get("reauth")
                else f"流水线执行中（第 {attempt}/{job['max_attempts']} 次）"
            ),
        )
        self._task_queue.put(
            {
                "dispatch_id": dispatch_id,
                "job_id": job["id"],
                "attempt": attempt,
                "mailbox": mailbox,
                "log_path": str(log_path),
                "reauth": bool(job.get("reauth")),
            }
        )
        self._active_dispatches[dispatch_id] = {
            "pipeline_id": str(job.get("pipeline_id") or ""),
            "job_id": str(job.get("id") or ""),
            "worker_pid": None,
        }
        return dispatch_id

    @staticmethod
    def _failure_info(message: str, status: str = "", http_status: Any = None) -> tuple[str, bool, int]:
        text = f"{status} {message}".casefold()
        if "任务执行超时" in text or "单次执行超过" in text:
            return "task_timeout", True, 15
        if "等待通用 api 验证码超时" in text:
            return "mailbox_otp_timeout", True, 15
        if any(value in text for value in ("rate_limit", "too many", "请求过多")):
            return "rate_limited", True, 180
        if any(
            value in text
            for value in (
                "tls",
                "sslerror",
                "ssl connect",
                "timed out",
                "timeout",
                "connection reset",
                "connection aborted",
                "proxyerror",
                "temporary failure",
                "curl: (28)",
                "curl: (35)",
            )
        ):
            return "transient_network", True, 0
        try:
            code = int(http_status or 0)
        except (TypeError, ValueError):
            code = 0
        if code in {408, 425, 429, 500, 502, 503, 504}:
            return "transient_http", True, 180 if code == 429 else 0
        if "no_balance" in text or "余额" in text:
            return "sms_no_balance", False, 0
        if "bad_key" in text or "api key" in text:
            return "sms_bad_key", False, 0
        if "fraud_guard" in text or "suspicious behavior" in text:
            return "fraud_guard", False, 0
        if "phone_number_in_use" in text or "phone number already in use" in text:
            return "phone_number_in_use", False, 0
        if "邮箱" in text and any(value in text for value in ("凭证", "refresh", "未就绪")):
            return "mailbox_unavailable", False, 0
        return "task_failed", False, 0

    @staticmethod
    def _phone_from_log(path: str | None) -> str:
        if not path:
            return ""
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        phone_success = (
            "手机号验证通过" in text
            or (
                "手机 OTP 提交后状态：left_phone_flow" in text
                and "已标记完成 activation_id=" in text
            )
        )
        if not phone_success:
            return ""
        matches = _PHONE_ATTEMPT.findall(text)
        return f"+{matches[-1]}" if matches else ""

    def _handle_result_locked(self, job: dict[str, Any], response: dict[str, Any], pipeline: dict[str, Any]) -> None:
        result = response.get("result") if isinstance(response.get("result"), dict) else {}
        error = str(response.get("error") or "")
        error_type = str(response.get("error_type") or "")
        if error:
            status = "failed"
            message = f"{error_type}: {error}" if error_type else error
            credential_path = None
            http_status = None
        elif result.get("ok"):
            status = "success"
            message = str(result.get("message") or "Codex OAuth 完成")
            credential_path = str(result.get("file_path") or "") or None
            http_status = result.get("http_status")
        else:
            status = str(result.get("status") or "failed")
            message = str(result.get("message") or "Codex OAuth 失败")
            credential_path = None
            http_status = result.get("http_status")

        # Worker/vendor errors are untrusted text.  Sanitize once before any
        # scheduler or mailbox state is persisted so new state files never
        # retain credentials accidentally embedded in an exception message.
        message = _redact_log_text(message)[:500]

        if status == "success":
            phone_number = self._phone_from_log(job.get("log_path"))
            reauth = bool(job.get("reauth"))
            job.update(
                status="success",
                stage="凭证已刷新" if reauth else "已完成",
                message=message[:500],
                credential_path=credential_path,
                phone_verified=bool(phone_number),
                phone_number=phone_number,
                failure_code="",
                retryable=False,
                next_retry_at=None,
                attempt_deadline_at=None,
                attempt_finished_at=_now(),
                finished_at=_now(),
            )
            self.mailbox_store.update_codex(
                str(job.get("email") or ""),
                status="success",
                message=message[:500],
                credential_path=credential_path,
                phone_verified=None if reauth else bool(phone_number),
                phone_number=None if reauth else (phone_number or None),
            )
            if self._success_callback is not None:
                try:
                    self._success_callback(
                        str(job.get("email") or ""), credential_path
                    )
                except Exception as exc:
                    logging.getLogger(__name__).warning(
                        "套餐刷新任务入队失败: %s", type(exc).__name__
                    )
            return

        if status in {"deactivated", "skipped"}:
            job.update(
                status=status,
                stage="已结束",
                message=message[:500],
                failure_code=status,
                retryable=False,
                next_retry_at=None,
                attempt_deadline_at=None,
                attempt_finished_at=_now(),
                finished_at=_now(),
            )
            self.mailbox_store.update_codex(
                str(job.get("email") or ""), status=status, message=message[:500]
            )
            return

        failure_code, retryable, minimum_delay = self._failure_info(message, status, http_status)
        can_retry = (
            retryable
            and int(job.get("attempt") or 0) < int(job.get("max_attempts") or 1)
            and not bool(pipeline.get("stop_requested"))
            and not bool(job.get("stop_requested"))
        )
        if can_retry:
            base = int(pipeline.get("retry_backoff_seconds") or 30)
            exponent = max(0, int(job.get("attempt") or 1) - 1)
            delay = max(minimum_delay, min(600, base * (3**exponent)))
            job.update(
                status="retry_wait",
                stage="等待重试",
                message=f"临时失败，{delay} 秒后重试：{message[:360]}",
                failure_code=failure_code,
                retryable=True,
                next_retry_at=_after(delay),
                attempt_deadline_at=None,
                attempt_finished_at=_now(),
                finished_at=None,
            )
            self.mailbox_store.update_codex(
                str(job.get("email") or ""),
                status="retry_wait",
                message=f"临时失败，等待第 {int(job.get('attempt') or 0)+1}/{job['max_attempts']} 次执行",
            )
            return

        job.update(
            status="failed",
            stage="执行失败",
            message=message[:500],
            failure_code=failure_code,
            retryable=retryable,
            next_retry_at=None,
            attempt_deadline_at=None,
            attempt_finished_at=_now(),
            finished_at=_now(),
        )
        self.mailbox_store.update_codex(
            str(job.get("email") or ""), status="failed", message=message[:500]
        )

    def _run_pipeline(self, pipeline_id: str, mailboxes: dict[str, dict[str, Any]]) -> None:
        inflight: dict[str, dict[str, Any]] = {}
        with self._lock:
            pipeline = self._pipelines.get(pipeline_id)
            if pipeline is None:
                return
            pipeline.update(
                status="paused" if pipeline.get("pause_requested") else "running",
                started_at=pipeline.get("started_at") or _now(),
            )
            self._persist_locked()

        while True:
            timed_out_worker_pids: list[int] = []
            desired_workers = 1
            pipeline_finished = False
            with self._lock:
                pipeline = self._pipelines.get(pipeline_id)
                if pipeline is None:
                    return
                job_ids = [str(value) for value in pipeline.get("job_ids") or []]
                changed = False
                desired_workers = int(pipeline.get("concurrency") or 1)
                timeout_seconds = int(
                    pipeline.get("attempt_timeout_seconds") or self._attempt_timeout_seconds()
                )
                if pipeline.get("stop_requested"):
                    for job_id in job_ids:
                        job = self._jobs.get(job_id)
                        if job and job.get("status") in {"queued", "retry_wait"}:
                            job.update(
                                status="stopped",
                                stage="已停止",
                                message="流水线已停止，任务未再派发",
                                next_retry_at=None,
                                finished_at=_now(),
                            )
                            self.mailbox_store.update_codex(
                                str(job.get("email") or ""),
                                status="stopped",
                                message="流水线停止前尚未执行",
                            )
                            changed = True

                now_ts = time.time()
                for dispatch_id, active in list(inflight.items()):
                    active_job = self._jobs.get(str(active.get("job_id") or ""))
                    if active_job is not None and str(active_job.get("status") or "") == "running":
                        continue
                    inflight.pop(dispatch_id, None)
                    self._active_dispatches.pop(dispatch_id, None)
                    changed = True
                for dispatch_id, active in list(inflight.items()):
                    if now_ts - float(active.get("started_ts") or now_ts) < timeout_seconds:
                        continue
                    inflight.pop(dispatch_id, None)
                    self._active_dispatches.pop(dispatch_id, None)
                    job = self._jobs.get(str(active.get("job_id") or ""))
                    worker_pid = int(active.get("worker_pid") or 0)
                    if worker_pid:
                        timed_out_worker_pids.append(worker_pid)
                    if job is None or str(job.get("status") or "") != "running":
                        continue
                    self._handle_result_locked(
                        job,
                        {
                            "error_type": "TaskTimeoutError",
                            "error": (
                                f"任务执行超时：单次执行超过 {timeout_seconds} 秒，"
                                "已终止本轮并释放执行槽位"
                            ),
                        },
                        pipeline,
                    )
                    changed = True
                ready = [] if pipeline.get("pause_requested") else [
                    self._jobs[job_id]
                    for job_id in job_ids
                    if job_id in self._jobs
                    and self._jobs[job_id].get("status") in {"queued", "retry_wait"}
                    and (
                        self._jobs[job_id].get("status") == "queued"
                        or _parse_time(self._jobs[job_id].get("next_retry_at")) <= now_ts
                    )
                ]
                ready.sort(key=lambda item: (str(item.get("next_retry_at") or ""), str(item.get("created_at") or "")))
                slots = max(0, int(pipeline.get("concurrency") or 1) - len(inflight))
                for job in ready[:slots]:
                    if pipeline.get("stop_requested") or pipeline.get("pause_requested"):
                        break
                    dispatch_id = self._dispatch_locked(
                        job,
                        mailboxes[job["id"]],
                        timeout_seconds=timeout_seconds,
                    )
                    inflight[dispatch_id] = {
                        "job_id": job["id"],
                        "started_ts": time.time(),
                        "worker_pid": None,
                    }
                    changed = True
                jobs = [self._jobs[job_id] for job_id in job_ids if job_id in self._jobs]
                all_terminal = bool(jobs) and all(job.get("status") in self._TERMINAL_JOBS for job in jobs)
                if changed:
                    self._persist_locked()
                if all_terminal and not inflight:
                    # A stopped batch may still contain truthful success/failure
                    # results from work that was already in flight.  Keep those
                    # per-job results, while making the batch-level state reflect
                    # the user's stop request.
                    pipeline["status"] = "stopped" if pipeline.get("stop_requested") else "completed"
                    pipeline["finished_at"] = _now()
                    self._persist_locked()
                    pipeline_finished = True

            if timed_out_worker_pids:
                for worker_pid in set(timed_out_worker_pids):
                    self._terminate_worker(worker_pid)
                self._ensure_workers(desired_workers)
            if pipeline_finished:
                return

            try:
                response = self._result_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if not isinstance(response, dict):
                continue
            dispatch_id = str(response.get("dispatch_id") or "")
            active = inflight.get(dispatch_id)
            if active is None:
                continue
            if str(response.get("kind") or "") == "started":
                active["worker_pid"] = int(response.get("worker_pid") or 0) or None
                tracked = self._active_dispatches.get(dispatch_id)
                if tracked is not None:
                    tracked["worker_pid"] = active["worker_pid"]
                continue
            inflight.pop(dispatch_id, None)
            self._active_dispatches.pop(dispatch_id, None)
            job_id = str(active.get("job_id") or "")
            with self._lock:
                pipeline = self._pipelines.get(pipeline_id)
                job = self._jobs.get(job_id)
                if pipeline is None or job is None or str(job.get("status") or "") != "running":
                    continue
                self._handle_result_locked(job, response, pipeline)
                self._persist_locked()

    def pause_pipeline(self, pipeline_id: str) -> bool:
        """Pause future dispatches while allowing already-running jobs to finish."""

        with self._lock:
            pipeline = self._pipelines.get(str(pipeline_id or ""))
            if not pipeline or str(pipeline.get("status") or "") not in {"queued", "running"}:
                return False
            pipeline.update(
                pause_requested=True,
                status="paused",
                paused_at=_now(),
            )
            self._persist_locked()
            return True

    def force_pause_pipeline(self, pipeline_id: str) -> dict[str, Any] | None:
        """Pause dispatch and fail every attempt that is running right now."""

        worker_pids: list[int] = []
        desired_workers = 1
        with self._lock:
            pipeline = self._pipelines.get(str(pipeline_id or ""))
            if not pipeline or str(pipeline.get("status") or "") not in {"queued", "running", "paused"}:
                return None
            desired_workers = int(pipeline.get("concurrency") or 1)
            now = _now()
            running_job_ids = {
                str(job_id)
                for job_id in pipeline.get("job_ids") or []
                if (self._jobs.get(str(job_id)) or {}).get("status") == "running"
            }
            pipeline.update(
                pause_requested=True,
                status="paused",
                paused_at=now,
                force_paused_at=now,
                force_paused_count=len(running_job_ids),
            )
            for job_id in running_job_ids:
                job = self._jobs.get(job_id)
                if job is None:
                    continue
                job.update(
                    status="failed",
                    stage="强制暂停",
                    message="流水线已强制暂停，本轮运行被终止并判定失败",
                    failure_code="force_paused",
                    retryable=False,
                    next_retry_at=None,
                    attempt_deadline_at=None,
                    attempt_finished_at=now,
                    finished_at=now,
                )
                self.mailbox_store.update_codex(
                    str(job.get("email") or ""),
                    status="failed",
                    message="流水线强制暂停，运行中的账号已判定失败",
                )
            for dispatch_id, active in list(self._active_dispatches.items()):
                if str(active.get("job_id") or "") not in running_job_ids:
                    continue
                worker_pid = int(active.get("worker_pid") or 0)
                if worker_pid:
                    worker_pids.append(worker_pid)
                self._active_dispatches.pop(dispatch_id, None)
            # A worker may not have acknowledged its dispatch yet. Since only one
            # pipeline can be active, recycling the complete pool guarantees the
            # force-pause has no orphaned network attempt.
            if running_job_ids:
                worker_pids.extend(
                    int(getattr(worker, "pid", 0) or 0) for worker in self._workers
                )
            self._persist_locked()
            public = self._pipeline_public_locked(pipeline)

        for worker_pid in set(worker_pids):
            self._terminate_worker(worker_pid)
        if running_job_ids:
            self._ensure_workers(desired_workers)
        return public

    def set_pipeline_concurrency(self, pipeline_id: str, concurrency: int) -> dict[str, Any] | None:
        """Resize an active pipeline; already-running jobs are left untouched."""

        try:
            concurrency = int(concurrency)
        except (TypeError, ValueError) as exc:
            raise ValueError("任务并发必须是整数") from exc
        max_concurrency = self._max_concurrency()
        if concurrency < 1 or concurrency > max_concurrency:
            raise ValueError(f"任务并发必须在 1 - {max_concurrency} 之间")

        with self._lock:
            pipeline = self._pipelines.get(str(pipeline_id or ""))
            if not pipeline or str(pipeline.get("status") or "") not in self._ACTIVE_PIPELINES:
                return None

        # Scaling up must provision the extra worker before the scheduler sees
        # the larger slot count. Scaling down simply limits future dispatches.
        self._ensure_workers(concurrency)
        with self._lock:
            pipeline = self._pipelines.get(str(pipeline_id or ""))
            if not pipeline or str(pipeline.get("status") or "") not in self._ACTIVE_PIPELINES:
                return None
            pipeline["concurrency"] = concurrency
            self._persist_locked()
            return self._pipeline_public_locked(pipeline)

    def resume_pipeline(self, pipeline_id: str) -> bool:
        """Resume dispatching queued and retry-wait jobs in a paused pipeline."""

        with self._lock:
            pipeline = self._pipelines.get(str(pipeline_id or ""))
            if (
                not pipeline
                or str(pipeline.get("status") or "") != "paused"
                or pipeline.get("stop_requested")
            ):
                return False
            pipeline.update(
                pause_requested=False,
                status="running",
                paused_at=None,
                resumed_at=_now(),
            )
            self._persist_locked()
            return True

    def stop_pipeline(self, pipeline_id: str) -> bool:
        with self._lock:
            pipeline = self._pipelines.get(str(pipeline_id or ""))
            if not pipeline or str(pipeline.get("status") or "") not in self._ACTIVE_PIPELINES:
                return False
            pipeline["pause_requested"] = False
            pipeline["stop_requested"] = True
            pipeline["status"] = "stopping"
            for job_id in pipeline.get("job_ids") or []:
                job = self._jobs.get(str(job_id))
                if job and job.get("status") == "running":
                    job["stop_requested"] = True
                    job["message"] = "已停止派发后续任务；当前网络步骤将执行完"
            self._persist_locked()
            return True

    def stop(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(str(job_id or ""))
            if not job or job.get("status") not in self._ACTIVE_JOBS:
                return False
            if job.get("status") in {"queued", "retry_wait"}:
                job.update(
                    status="stopped",
                    stage="已停止",
                    message="任务在执行前被停止",
                    next_retry_at=None,
                    finished_at=_now(),
                )
            else:
                job["stop_requested"] = True
                job["message"] = "已请求停止重试；当前网络步骤将执行完"
            self._persist_locked()
            return True

    def is_account_active(self, email: str) -> bool:
        target = str(email or "").strip().casefold()
        with self._lock:
            return any(
                str(job.get("email") or "").casefold() == target
                and str(job.get("status") or "") in self._ACTIVE_JOBS
                for job in self._jobs.values()
            )


__all__ = ["CodexJobManager"]
