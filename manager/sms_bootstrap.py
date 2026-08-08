from __future__ import annotations

from dataclasses import dataclass
import importlib
import logging
import re
import sys
import threading
from typing import Any

from .sms_runtime_overlay import install_codex_sms_overlay


_SMART_CODE = re.compile(r"\bsmart_sms_code=([a-z_]{1,64})\b", re.IGNORECASE)
_RECEIVED_OTP = re.compile(
    r"((?:收到验证码|OTP\s*收到)\s*[:：]?\s*)\d{4,8}\b",
    re.IGNORECASE,
)
_INSTALL_LOCK = threading.RLock()
_INSTALLATION: "ManagedSmsInstallation | None" = None


class _OtpRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = _RECEIVED_OTP.sub(r"\1[REDACTED]", message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


@dataclass
class ManagedSmsInstallation:
    bridge: Any
    original_bridge_run: Any
    patched_bridge_run: Any
    job_manager_class: type
    original_failure_info: Any
    patched_failure_info: Any
    original_handle_result: Any
    patched_handle_result: Any
    artifact_module: Any
    original_status_parser: Any
    patched_status_parser: Any
    worker_module: Any | None
    original_worker_run: Any | None
    original_worker_safe_result: Any | None
    patched_worker_safe_result: Any | None
    otp_logger: logging.Logger
    otp_filter: logging.Filter
    restored: bool = False

    def restore(self) -> None:
        global _INSTALLATION
        with _INSTALL_LOCK:
            if self.restored:
                return
            if self.bridge.run_codex_only is self.patched_bridge_run:
                self.bridge.run_codex_only = self.original_bridge_run
            if (
                self.worker_module is not None
                and self.worker_module.run_codex_only is self.patched_bridge_run
            ):
                self.worker_module.run_codex_only = self.original_worker_run
            if self.job_manager_class._failure_info is self.patched_failure_info:
                self.job_manager_class._failure_info = staticmethod(
                    self.original_failure_info
                )
            if (
                self.job_manager_class._handle_result_locked
                is self.patched_handle_result
            ):
                self.job_manager_class._handle_result_locked = (
                    self.original_handle_result
                )
            if (
                self.artifact_module._sms_rejection_status
                is self.patched_status_parser
            ):
                self.artifact_module._sms_rejection_status = (
                    self.original_status_parser
                )
            if (
                self.worker_module is not None
                and self.patched_worker_safe_result is not None
                and self.worker_module._safe_result
                is self.patched_worker_safe_result
            ):
                self.worker_module._safe_result = self.original_worker_safe_result
            self.otp_logger.removeFilter(self.otp_filter)
            self.restored = True
            if _INSTALLATION is self:
                _INSTALLATION = None


def _smart_failure_mapping(text: str) -> tuple[str, bool, int] | None:
    mappings = {
        "fraud_guard": ("fraud_guard", False, 0),
        "phone_rate_limited": ("phone_rate_limited", False, 0),
        "number_in_use": ("number_in_use", False, 0),
        "number_rejected": ("number_rejected", False, 0),
        "sms_timeout": ("sms_timeout", False, 0),
        "otp_rejected": ("otp_rejected", False, 0),
        "sms_no_numbers": ("sms_no_numbers", False, 0),
        "sms_no_balance": ("sms_no_balance", False, 0),
        "sms_bad_key": ("sms_bad_key", False, 0),
        "phone_transient_server": ("phone_transient_server", False, 0),
        "phone_unknown_send": ("phone_unknown_send", False, 0),
    }
    lowered = text.casefold()
    for code, result in mappings.items():
        if f"[smart_sms:{code}]" in lowered:
            return result
    return None


def _smart_statistics_status(body: str) -> str | None:
    matched = _SMART_CODE.search(str(body or ""))
    if not matched:
        return None
    return {
        "fraud_guard": "fraud_guard",
        "phone_rate_limited": "rate_limited",
        "number_in_use": "number_in_use",
        "number_rejected": "send_rejected",
        "sms_timeout": "sms_timeout",
        "otp_rejected": "code_rejected",
        "phone_transient_server": "send_rejected",
        "phone_unknown_send": "send_rejected",
    }.get(matched.group(1).casefold(), "send_rejected")


def install_managed_sms_runtime() -> ManagedSmsInstallation:
    global _INSTALLATION
    from src import artifact_store, upstream_bridge
    from src.codex_service import CodexJobManager

    with _INSTALL_LOCK:
        if _INSTALLATION is not None and not _INSTALLATION.restored:
            return _INSTALLATION

        original_bridge_run = upstream_bridge.run_codex_only
        original_failure_info = CodexJobManager._failure_info
        original_handle_result = CodexJobManager._handle_result_locked
        original_status_parser = artifact_store._sms_rejection_status

        def managed_run(settings, mailbox, *, reauth=False):
            patch = None
            codex_oauth = None
            if not reauth:
                codex_oauth = upstream_bridge._ensure_upstream_imports(settings)
                setattr(codex_oauth, "_manager_phone_verified", False)
                patch = install_codex_sms_overlay(codex_oauth)
            try:
                result = original_bridge_run(
                    settings,
                    mailbox,
                    reauth=reauth,
                )
                if (
                    isinstance(result, dict)
                    and codex_oauth is not None
                    and bool(getattr(codex_oauth, "_manager_phone_verified", False))
                ):
                    result = dict(result)
                    result["phone_verified"] = True
                return result
            finally:
                if patch is not None:
                    patch.restore()
                if codex_oauth is not None and hasattr(
                    codex_oauth,
                    "_manager_phone_verified",
                ):
                    delattr(codex_oauth, "_manager_phone_verified")

        def managed_failure_info(message, status="", http_status=None):
            smart = _smart_failure_mapping(f"{status} {message}")
            if smart is not None:
                return smart
            return original_failure_info(message, status, http_status)

        def managed_handle_result(self, job, response, pipeline):
            original_handle_result(self, job, response, pipeline)
            result = (
                response.get("result")
                if isinstance(response.get("result"), dict)
                else {}
            )
            if (
                job.get("status") == "success"
                and not bool(job.get("reauth"))
                and bool(result.get("phone_verified"))
            ):
                job["phone_verified"] = True
                self.mailbox_store.update_codex(
                    str(job.get("email") or ""),
                    status="success",
                    message=str(job.get("message") or "")[:500],
                    credential_path=job.get("credential_path"),
                    phone_verified=True,
                )

        def managed_status_parser(body):
            smart = _smart_statistics_status(body)
            if smart is not None:
                return smart
            return original_status_parser(body)

        upstream_bridge.run_codex_only = managed_run
        CodexJobManager._failure_info = staticmethod(managed_failure_info)
        CodexJobManager._handle_result_locked = managed_handle_result
        artifact_store._sms_rejection_status = managed_status_parser

        worker_was_loaded = "src.codex_worker" in sys.modules
        worker_module = importlib.import_module("src.codex_worker")
        original_worker_run = (
            worker_module.run_codex_only
            if worker_was_loaded
            else original_bridge_run
        )
        worker_module.run_codex_only = managed_run
        original_worker_safe_result = worker_module._safe_result

        def managed_worker_safe_result(result):
            safe = original_worker_safe_result(result)
            if isinstance(result, dict) and bool(result.get("phone_verified")):
                safe["phone_verified"] = True
            return safe

        worker_module._safe_result = managed_worker_safe_result

        otp_logger = logging.getLogger("core.sms_provider")
        otp_filter = _OtpRedactionFilter()
        otp_logger.addFilter(otp_filter)

        _INSTALLATION = ManagedSmsInstallation(
            bridge=upstream_bridge,
            original_bridge_run=original_bridge_run,
            patched_bridge_run=managed_run,
            job_manager_class=CodexJobManager,
            original_failure_info=original_failure_info,
            patched_failure_info=managed_failure_info,
            original_handle_result=original_handle_result,
            patched_handle_result=managed_handle_result,
            artifact_module=artifact_store,
            original_status_parser=original_status_parser,
            patched_status_parser=managed_status_parser,
            worker_module=worker_module,
            original_worker_run=original_worker_run,
            original_worker_safe_result=original_worker_safe_result,
            patched_worker_safe_result=managed_worker_safe_result,
            otp_logger=otp_logger,
            otp_filter=otp_filter,
        )
        return _INSTALLATION


__all__ = ["ManagedSmsInstallation", "install_managed_sms_runtime"]
