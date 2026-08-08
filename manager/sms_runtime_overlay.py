from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
import threading
from typing import Any

from .sms_retry_policy import (
    RetryAction,
    SmsFailure,
    classify_provider_failure,
    classify_send_failure,
    decide_retry,
)


_PATCH_ATTRIBUTE = "_manager_smart_sms_patch"
_PATCH_LOCK = threading.RLock()
_PROVIDER_ABORT_STATE = threading.local()


class SmartSmsStop(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = str(code)
        super().__init__(f"[smart_sms:{self.code}] {message}")


class TerminalSmsProviderAbort(BaseException):
    def __init__(self, failure: SmsFailure):
        self.failure = failure
        super().__init__(failure.value)


def terminal_provider_abort_enabled() -> bool:
    return bool(getattr(_PROVIDER_ABORT_STATE, "enabled", False))


@contextmanager
def _terminal_provider_abort_scope():
    previous = terminal_provider_abort_enabled()
    _PROVIDER_ABORT_STATE.enabled = True
    try:
        yield
    finally:
        _PROVIDER_ABORT_STATE.enabled = previous


@dataclass
class CodexSmsPatch:
    module: Any
    original: Any
    patched: Any
    restored: bool = False

    def __enter__(self) -> "CodexSmsPatch":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.restore()

    def restore(self) -> None:
        with _PATCH_LOCK:
            if self.restored:
                return
            if getattr(self.module, "_do_phone_verification", None) is self.patched:
                setattr(self.module, "_do_phone_verification", self.original)
            if getattr(self.module, _PATCH_ATTRIBUTE, None) is self:
                delattr(self.module, _PATCH_ATTRIBUTE)
            self.restored = True


def _cancel_without_masking(
    provider: Any,
    activation_id: str | None,
    http: Any,
    logger: Any,
) -> None:
    if not activation_id:
        return
    try:
        provider.cancel(activation_id, http)
    except Exception as exc:
        logger.warning(
            "[Codex] smart_sms cancel failed (%s)",
            type(exc).__name__,
        )


def _retry_or_stop(
    module: Any,
    failure: SmsFailure,
    counters: Counter[SmsFailure],
    total_attempts: int,
    max_attempts: int,
) -> None:
    counters[failure] += 1
    decision = decide_retry(
        failure,
        occurrence=counters[failure],
        total_attempts=total_attempts,
        max_attempts=max_attempts,
    )
    module.logger.warning(
        "[Codex] add-phone/send 未成功 reason=smart_sms smart_sms_code=%s",
        failure.value,
    )
    module.logger.warning(
        "[Codex] smart_sms reason=%s action=%s message=%s",
        failure.value,
        decision.action.value,
        decision.message,
    )
    if decision.action is RetryAction.STOP:
        raise SmartSmsStop(decision.code, decision.message)
    module._sleep_before_phone_retry(total_attempts, max_attempts)


def _stop_for_provider_failure(failure: SmsFailure) -> SmartSmsStop:
    messages = {
        SmsFailure.NO_NUMBERS: "HeroSMS 无可用号码：已扫描配置队列，停止任务",
        SmsFailure.NO_BALANCE: "HeroSMS 余额不足：停止当前账号任务",
        SmsFailure.BAD_KEY: "HeroSMS API Key 无效：停止当前账号任务",
    }
    return SmartSmsStop(failure.value, messages[failure])


def _managed_phone_verification(module: Any, session: Any) -> None:
    provider = module.sms_provider
    http = provider._http()
    try:
        max_attempts = max(
            1,
            int(getattr(module._cfg, "SMS_MAX_RETRIES", 1) or 1),
        )
    except (TypeError, ValueError):
        max_attempts = 1
    counters: Counter[SmsFailure] = Counter()

    try:
        for attempt in range(1, max_attempts + 1):
            activation_id: str | None = None
            try:
                with _terminal_provider_abort_scope():
                    activation_id, phone = provider.acquire_number(http)
                module.logger.info(
                    "[Codex] 手机号验证尝试 %s/%s provider=%s activation_id=%s",
                    attempt,
                    max_attempts,
                    module._sms_provider_name(),
                    activation_id,
                )

                send_response = module._post_json(
                    session,
                    "https://auth.openai.com/api/accounts/add-phone/send",
                    {"phone_number": f"+{phone}", "channel": "sms"},
                    referer="https://auth.openai.com/add-phone",
                )
                send_text = module._response_text(send_response)
                upstream_reason = module._phone_failure_reason(
                    send_text,
                    send_response.status_code,
                )
                failure = classify_send_failure(
                    send_text,
                    send_response.status_code,
                    upstream_reason,
                )
                if failure is not None:
                    _cancel_without_masking(
                        provider,
                        activation_id,
                        http,
                        module.logger,
                    )
                    activation_id = None
                    _retry_or_stop(
                        module,
                        failure,
                        counters,
                        attempt,
                        max_attempts,
                    )
                    continue

                provider.set_status(activation_id, 1, http=http)
                module.logger.info(
                    "[Codex] 短信已发送，开始轮询验证码 activation_id=%s",
                    activation_id,
                )
                sms_code = provider.wait_for_sms_code(activation_id, http)

                validate_response = module._post_json(
                    session,
                    "https://auth.openai.com/api/accounts/phone-otp/validate",
                    {"code": sms_code},
                    referer="https://auth.openai.com/phone-verification",
                )
                validate_text = module._response_text(validate_response)
                validate_upstream_reason = module._phone_failure_reason(
                    validate_text,
                    validate_response.status_code,
                )
                validate_failure = classify_send_failure(
                    validate_text,
                    validate_response.status_code,
                    validate_upstream_reason,
                )
                if validate_response.status_code != 200 or validate_failure is not None:
                    terminal_validation_failure = (
                        validate_failure
                        if validate_failure
                        in {
                            SmsFailure.FRAUD_GUARD,
                            SmsFailure.PHONE_RATE_LIMITED,
                        }
                        else SmsFailure.OTP_REJECTED
                    )
                    _cancel_without_masking(
                        provider,
                        activation_id,
                        http,
                        module.logger,
                    )
                    activation_id = None
                    _retry_or_stop(
                        module,
                        terminal_validation_failure,
                        counters,
                        attempt,
                        max_attempts,
                    )
                    continue

                provider.complete(activation_id, http)
                activation_id = None
                setattr(module, "_manager_phone_verified", True)
                module.logger.info("[Codex] 手机号验证通过")
                return

            except SmartSmsStop:
                raise
            except TerminalSmsProviderAbort as exc:
                _cancel_without_masking(
                    provider,
                    activation_id,
                    http,
                    module.logger,
                )
                raise _stop_for_provider_failure(exc.failure)
            except provider.SmsNoNumbersError:
                _cancel_without_masking(
                    provider,
                    activation_id,
                    http,
                    module.logger,
                )
                raise _stop_for_provider_failure(SmsFailure.NO_NUMBERS)
            except provider.SmsNoBalanceError:
                _cancel_without_masking(
                    provider,
                    activation_id,
                    http,
                    module.logger,
                )
                raise _stop_for_provider_failure(SmsFailure.NO_BALANCE)
            except provider.SmsCodeTimeout:
                _cancel_without_masking(
                    provider,
                    activation_id,
                    http,
                    module.logger,
                )
                activation_id = None
                _retry_or_stop(
                    module,
                    SmsFailure.SMS_TIMEOUT,
                    counters,
                    attempt,
                    max_attempts,
                )
                continue
            except provider.SmsProviderError as exc:
                _cancel_without_masking(
                    provider,
                    activation_id,
                    http,
                    module.logger,
                )
                activation_id = None
                failure = classify_provider_failure(str(exc))
                if failure in {
                    SmsFailure.NO_NUMBERS,
                    SmsFailure.NO_BALANCE,
                    SmsFailure.BAD_KEY,
                }:
                    raise _stop_for_provider_failure(failure)
                _retry_or_stop(
                    module,
                    failure,
                    counters,
                    attempt,
                    max_attempts,
                )
                continue
            except Exception:
                _cancel_without_masking(
                    provider,
                    activation_id,
                    http,
                    module.logger,
                )
                raise

        raise SmartSmsStop(
            SmsFailure.UNKNOWN_SEND.value,
            "短信验证达到配置总上限：停止当前账号任务",
        )
    finally:
        http.close()


def install_codex_sms_overlay(codex_oauth: Any) -> CodexSmsPatch:
    with _PATCH_LOCK:
        existing = getattr(codex_oauth, _PATCH_ATTRIBUTE, None)
        if isinstance(existing, CodexSmsPatch) and not existing.restored:
            return existing

        required = (
            "_do_phone_verification",
            "_post_json",
            "_response_text",
            "_phone_failure_reason",
            "_sleep_before_phone_retry",
            "_sms_provider_name",
            "_cfg",
            "sms_provider",
            "logger",
        )
        missing = [name for name in required if not hasattr(codex_oauth, name)]
        if missing:
            raise RuntimeError(
                "智能接码覆盖不兼容，缺少上游接口: " + ", ".join(missing)
            )

        provider_required = (
            "_http",
            "acquire_number",
            "cancel",
            "set_status",
            "wait_for_sms_code",
            "complete",
            "SmsProviderError",
            "SmsNoNumbersError",
            "SmsNoBalanceError",
            "SmsCodeTimeout",
        )
        provider_missing = [
            name
            for name in provider_required
            if not hasattr(codex_oauth.sms_provider, name)
        ]
        if provider_missing:
            raise RuntimeError(
                "智能接码覆盖不兼容，缺少短信接口: "
                + ", ".join(provider_missing)
            )

        original = codex_oauth._do_phone_verification

        def patched(session: Any) -> None:
            return _managed_phone_verification(codex_oauth, session)

        patch = CodexSmsPatch(
            module=codex_oauth,
            original=original,
            patched=patched,
        )
        codex_oauth._do_phone_verification = patched
        setattr(codex_oauth, _PATCH_ATTRIBUTE, patch)
        return patch


__all__ = [
    "CodexSmsPatch",
    "SmartSmsStop",
    "TerminalSmsProviderAbort",
    "install_codex_sms_overlay",
    "terminal_provider_abort_enabled",
]
