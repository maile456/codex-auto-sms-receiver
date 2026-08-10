from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import importlib
import logging
import re
import sys
import threading
from typing import Any

from .sms_retry_policy import SmsFailure, classify_provider_failure
from .sms_runtime_overlay import (
    TerminalSmsProviderAbort,
    install_codex_sms_overlay,
    terminal_provider_abort_enabled,
)


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
        redacted = _RECEIVED_OTP.sub(
            "手机 OTP 收到 [REDACTED]",
            message,
        )
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


class _VerifiedWithoutPhoneNumber:
    def __bool__(self) -> bool:
        return True

    def __str__(self) -> str:
        return ""


_VERIFIED_WITHOUT_PHONE = _VerifiedWithoutPhoneNumber()


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
    artifact_store_class: type
    original_phone_lookup: Any
    patched_phone_lookup: Any
    hero_adapter_class: type
    original_hero_request: Any
    patched_hero_request: Any
    original_hero_query: Any
    patched_hero_query: Any
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
                self.artifact_store_class.phone_verification_for_account
                is self.patched_phone_lookup
            ):
                self.artifact_store_class.phone_verification_for_account = (
                    self.original_phone_lookup
                )
            if self.hero_adapter_class.request is self.patched_hero_request:
                self.hero_adapter_class.request = self.original_hero_request
            if self.hero_adapter_class.query is self.patched_hero_query:
                self.hero_adapter_class.query = self.original_hero_query
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


def _terminal_failure_from_provider_result(result: Any) -> SmsFailure | None:
    if not isinstance(result, Mapping):
        return None
    failure = classify_provider_failure(str(result.get("result") or ""))
    if failure in {SmsFailure.BAD_KEY, SmsFailure.NO_BALANCE}:
        return failure
    return None


def install_managed_sms_runtime() -> ManagedSmsInstallation:
    global _INSTALLATION
    from src import artifact_store, hero_sms, upstream_bridge
    from src.codex_service import CodexJobManager

    with _INSTALL_LOCK:
        if _INSTALLATION is not None and not _INSTALLATION.restored:
            return _INSTALLATION

        original_bridge_run = upstream_bridge.run_codex_only
        original_failure_info = CodexJobManager._failure_info
        original_handle_result = CodexJobManager._handle_result_locked
        original_status_parser = artifact_store._sms_rejection_status
        original_phone_lookup = (
            artifact_store.ArtifactStore.phone_verification_for_account
        )
        original_hero_request = hero_sms.HeroSmsAdapter.request
        original_hero_query = hero_sms.HeroSmsAdapter.query
        worker_was_loaded = "src.codex_worker" in sys.modules
        worker_module = importlib.import_module("src.codex_worker")
        worker_missing = [
            name
            for name in ("run_codex_only", "_safe_result")
            if not callable(getattr(worker_module, name, None))
        ]
        if worker_missing:
            raise RuntimeError(
                "智能接码管理器不兼容，缺少 worker 接口: "
                + ", ".join(worker_missing)
            )
        original_worker_run = (
            worker_module.run_codex_only
            if worker_was_loaded
            else original_bridge_run
        )
        original_worker_safe_result = worker_module._safe_result

        def managed_run(settings, mailbox, *, reauth=False):
            patch = None
            codex_oauth = None
            try:
                if not reauth:
                    codex_oauth = upstream_bridge._ensure_upstream_imports(settings)
                    setattr(codex_oauth, "_manager_phone_verified", False)
                    patch = install_codex_sms_overlay(codex_oauth)
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

        def managed_phone_lookup(store, account_id):
            verified = original_phone_lookup(store, account_id)
            if verified:
                return verified
            from src.mailbox_store import MailboxStore

            account = MailboxStore(store.data_dir).get_secret(
                account_id=str(account_id or "")
            )
            if (
                account
                and bool(account.get("phone_verified"))
                and not str(account.get("phone_number") or "")
            ):
                return {
                    "phone_number": _VERIFIED_WITHOUT_PHONE,
                    "phone_verified_at": account.get("phone_verified_at"),
                }
            return verified

        def managed_hero_query(adapter, *args, **kwargs):
            try:
                result = original_hero_query(adapter, *args, **kwargs)
            except Exception as exc:
                failure = classify_provider_failure(str(exc))
                if (
                    terminal_provider_abort_enabled()
                    and failure in {SmsFailure.BAD_KEY, SmsFailure.NO_BALANCE}
                ):
                    raise TerminalSmsProviderAbort(failure) from exc
                raise
            failure = _terminal_failure_from_provider_result(result)
            if terminal_provider_abort_enabled() and failure is not None:
                raise TerminalSmsProviderAbort(failure)
            return result

        def managed_hero_request(adapter, *args, **kwargs):
            try:
                return original_hero_request(adapter, *args, **kwargs)
            except Exception as exc:
                failure = classify_provider_failure(str(exc))
                if (
                    terminal_provider_abort_enabled()
                    and failure in {SmsFailure.BAD_KEY, SmsFailure.NO_BALANCE}
                ):
                    raise TerminalSmsProviderAbort(failure) from exc
                raise

        upstream_bridge.run_codex_only = managed_run
        CodexJobManager._failure_info = staticmethod(managed_failure_info)
        CodexJobManager._handle_result_locked = managed_handle_result
        artifact_store._sms_rejection_status = managed_status_parser
        artifact_store.ArtifactStore.phone_verification_for_account = (
            managed_phone_lookup
        )
        hero_sms.HeroSmsAdapter.request = managed_hero_request
        hero_sms.HeroSmsAdapter.query = managed_hero_query

        worker_module.run_codex_only = managed_run

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
            artifact_store_class=artifact_store.ArtifactStore,
            original_phone_lookup=original_phone_lookup,
            patched_phone_lookup=managed_phone_lookup,
            hero_adapter_class=hero_sms.HeroSmsAdapter,
            original_hero_request=original_hero_request,
            patched_hero_request=managed_hero_request,
            original_hero_query=original_hero_query,
            patched_hero_query=managed_hero_query,
            worker_module=worker_module,
            original_worker_run=original_worker_run,
            original_worker_safe_result=original_worker_safe_result,
            patched_worker_safe_result=managed_worker_safe_result,
            otp_logger=otp_logger,
            otp_filter=otp_filter,
        )
        return _INSTALLATION


__all__ = ["ManagedSmsInstallation", "install_managed_sms_runtime"]
