from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .artifact_store import _redact_log_text
from .upstream_bridge import run_codex_only


_SENSITIVE_TRANSPORT_LOGGERS = (
    "urllib3",
    "requests.packages.urllib3",
    "httpcore",
    "httpx",
)


@dataclass(frozen=True)
class WorkerSettings:
    """Only the two paths needed by the isolated upstream worker."""

    project_root: Path
    data_dir: Path


def _safe_result(result: Any) -> dict[str, Any]:
    """Return only scheduler fields; OAuth callback data never crosses the worker queue."""

    value = result if isinstance(result, dict) else {}
    return {
        "ok": bool(value.get("ok")),
        "status": str(value.get("status") or "failed")[:80],
        "message": _redact_log_text(str(value.get("message") or ""))[:500],
        "file_path": str(value.get("file_path") or "")[:4096],
        "http_status": value.get("http_status"),
    }


def worker_main(settings, task_queue, result_queue) -> None:
    """Long-lived, single-task worker process isolating upstream module globals."""

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    # Transport DEBUG records include full request targets.  Generic mailbox
    # access tokens may live in the URL path, so never persist these records.
    for logger_name in _SENSITIVE_TRANSPORT_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.WARNING)
    while True:
        task = task_queue.get()
        if task is None:
            return
        dispatch_id = str(task.get("dispatch_id") or "")
        job_id = str(task.get("job_id") or "")
        attempt = int(task.get("attempt") or 1)
        mailbox = task.get("mailbox") if isinstance(task.get("mailbox"), dict) else {}
        log_path = Path(str(task.get("log_path") or ""))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        worker_thread = threading.current_thread().name
        handler.addFilter(lambda record, name=worker_thread: record.threadName == name)
        root_logger.addHandler(handler)
        try:
            # Report ownership before entering upstream network code.  The
            # scheduler uses this PID to terminate only the worker whose
            # current attempt exceeds the hard deadline, then provisions a
            # replacement without disturbing other concurrent accounts.
            result_queue.put(
                {
                    "kind": "started",
                    "dispatch_id": dispatch_id,
                    "job_id": job_id,
                    "attempt": attempt,
                    "worker_pid": os.getpid(),
                }
            )
            logging.getLogger(__name__).info(
                "Pipeline worker start: source=%s attempt=%s",
                mailbox.get("source"),
                attempt,
            )
            result_queue.put(
                {
                    "dispatch_id": dispatch_id,
                    "job_id": job_id,
                    "attempt": attempt,
                    "result": _safe_result(run_codex_only(settings, mailbox)),
                }
            )
        except Exception as exc:
            logging.getLogger(__name__).exception("Pipeline worker job failed")
            result_queue.put(
                {
                    "dispatch_id": dispatch_id,
                    "job_id": job_id,
                    "attempt": attempt,
                    "error_type": type(exc).__name__,
                    "error": _redact_log_text(str(exc))[:500],
                }
            )
        finally:
            root_logger.removeHandler(handler)
            handler.close()


__all__ = ["WorkerSettings", "worker_main"]
