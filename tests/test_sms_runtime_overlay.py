from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import logging
from types import SimpleNamespace

import pytest

from manager.sms_runtime_overlay import SmartSmsStop, install_codex_sms_overlay


class FakeSmsProviderError(RuntimeError):
    pass


class FakeSmsNoNumbersError(FakeSmsProviderError):
    pass


class FakeSmsNoBalanceError(FakeSmsProviderError):
    pass


class FakeSmsCodeTimeout(FakeSmsProviderError):
    pass


class FakeResponse:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text


class FakeHttp:
    def __init__(self, lifecycle: "Lifecycle"):
        self.lifecycle = lifecycle

    def close(self) -> None:
        self.lifecycle.http_closed += 1


@dataclass
class Lifecycle:
    acquired: int = 0
    cancelled: list[str] = field(default_factory=list)
    completed: list[str] = field(default_factory=list)
    statuses: list[tuple[str, int]] = field(default_factory=list)
    requests: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    sleeps: list[tuple[int, int]] = field(default_factory=list)
    http_closed: int = 0


def fake_codex_module(
    *,
    send: list[tuple[int, str]] | None = None,
    validate: list[tuple[int, str]] | None = None,
    sms_codes: list[str] | None = None,
    wait_errors: list[str] | None = None,
    acquire_errors: list[str] | None = None,
    cancel_error: bool = False,
    phone: str = "15551234567",
    max_retries: int = 10,
):
    lifecycle = Lifecycle()
    send_queue = deque(send or [(204, "")])
    validate_queue = deque(validate or [(200, "")])
    code_queue = deque(sms_codes or ["123456"])
    wait_queue = deque(wait_errors or [])
    acquire_queue = deque(acquire_errors or [])

    class FakeProvider:
        SmsProviderError = FakeSmsProviderError
        SmsNoNumbersError = FakeSmsNoNumbersError
        SmsNoBalanceError = FakeSmsNoBalanceError
        SmsCodeTimeout = FakeSmsCodeTimeout

        @staticmethod
        def _http():
            return FakeHttp(lifecycle)

        @staticmethod
        def acquire_number(http):
            lifecycle.acquired += 1
            error = acquire_queue.popleft() if acquire_queue else ""
            if error == "NO_NUMBERS":
                raise FakeSmsNoNumbersError("Hero-SMS NO_NUMBERS")
            if error == "NO_BALANCE":
                raise FakeSmsNoBalanceError("Hero-SMS NO_BALANCE")
            if error:
                raise FakeSmsProviderError(error)
            return f"activation-{lifecycle.acquired}", phone

        @staticmethod
        def cancel(activation_id, http):
            if cancel_error:
                raise RuntimeError("cancel failed with secret-response")
            lifecycle.cancelled.append(activation_id)

        @staticmethod
        def set_status(activation_id, status, *, http):
            lifecycle.statuses.append((activation_id, status))

        @staticmethod
        def wait_for_sms_code(activation_id, http):
            error = wait_queue.popleft() if wait_queue else ""
            if error == "timeout":
                raise FakeSmsCodeTimeout("timeout")
            return code_queue.popleft()

        @staticmethod
        def complete(activation_id, http):
            lifecycle.completed.append(activation_id)

    def post_json(session, url, payload, *, referer):
        lifecycle.requests.append((url, dict(payload)))
        queue = validate_queue if "phone-otp/validate" in url else send_queue
        status, body = queue.popleft()
        return FakeResponse(status, body)

    def phone_failure_reason(text, status_code):
        body = text.casefold()
        if "rate_limit" in body:
            return "send_limited"
        if "phone_number_in_use" in body:
            return "phone_used_or_max"
        if status_code >= 500:
            return "server_error"
        if status_code >= 400:
            return "send_rejected"
        return ""

    module = SimpleNamespace(
        _cfg=SimpleNamespace(SMS_MAX_RETRIES=max_retries),
        _do_phone_verification=lambda session: None,
        _post_json=post_json,
        _response_text=lambda response: response.text,
        _phone_failure_reason=phone_failure_reason,
        _sleep_before_phone_retry=lambda attempt, maximum: lifecycle.sleeps.append(
            (attempt, maximum)
        ),
        _sms_provider_name=lambda: "hero",
        sms_provider=FakeProvider,
        logger=logging.getLogger(f"test-managed-sms-{id(lifecycle)}"),
    )
    return module, lifecycle


@pytest.mark.parametrize(
    ("response_text", "status", "expected_code"),
    [
        (
            "invalid_request_error fraud_guard suspicious behavior",
            400,
            "fraud_guard",
        ),
        (
            "invalid_request_error rate_limit_exceeded",
            429,
            "phone_rate_limited",
        ),
    ],
)
def test_terminal_send_failure_cancels_once_and_stops(
    response_text: str,
    status: int,
    expected_code: str,
) -> None:
    module, lifecycle = fake_codex_module(send=[(status, response_text)])
    patch = install_codex_sms_overlay(module)

    try:
        with pytest.raises(SmartSmsStop) as caught:
            module._do_phone_verification(object())
    finally:
        patch.restore()

    assert caught.value.code == expected_code
    assert lifecycle.acquired == 1
    assert lifecycle.cancelled == ["activation-1"]
    assert lifecycle.completed == []
    assert lifecycle.sleeps == []
    assert lifecycle.http_closed == 1


def test_no_numbers_stops_after_one_provider_scan() -> None:
    module, lifecycle = fake_codex_module(acquire_errors=["NO_NUMBERS"])

    with install_codex_sms_overlay(module):
        with pytest.raises(SmartSmsStop) as caught:
            module._do_phone_verification(object())

    assert caught.value.code == "sms_no_numbers"
    assert lifecycle.acquired == 1
    assert lifecycle.cancelled == []
    assert lifecycle.sleeps == []


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [("NO_BALANCE", "sms_no_balance"), ("Hero-SMS BAD_KEY", "sms_bad_key")],
)
def test_provider_configuration_failures_stop_immediately(
    error: str,
    expected_code: str,
) -> None:
    module, lifecycle = fake_codex_module(acquire_errors=[error])

    with install_codex_sms_overlay(module):
        with pytest.raises(SmartSmsStop) as caught:
            module._do_phone_verification(object())

    assert caught.value.code == expected_code
    assert lifecycle.acquired == 1
    assert lifecycle.sleeps == []


def test_number_in_use_retries_once_then_stops() -> None:
    module, lifecycle = fake_codex_module(
        send=[(400, "phone_number_in_use"), (400, "phone_number_in_use")]
    )

    with install_codex_sms_overlay(module):
        with pytest.raises(SmartSmsStop) as caught:
            module._do_phone_verification(object())

    assert caught.value.code == "number_in_use"
    assert lifecycle.acquired == 2
    assert lifecycle.cancelled == ["activation-1", "activation-2"]
    assert lifecycle.sleeps == [(1, 10)]


def test_sms_timeout_retries_once_then_stops() -> None:
    module, lifecycle = fake_codex_module(
        send=[(204, ""), (204, "")],
        wait_errors=["timeout", "timeout"],
    )

    with install_codex_sms_overlay(module):
        with pytest.raises(SmartSmsStop) as caught:
            module._do_phone_verification(object())

    assert caught.value.code == "sms_timeout"
    assert lifecycle.acquired == 2
    assert lifecycle.cancelled == ["activation-1", "activation-2"]
    assert lifecycle.statuses == [("activation-1", 1), ("activation-2", 1)]


def test_otp_rejection_retries_once_then_stops() -> None:
    module, lifecycle = fake_codex_module(
        send=[(204, ""), (204, "")],
        sms_codes=["111111", "222222"],
        validate=[(400, "invalid code"), (400, "invalid code")],
    )

    with install_codex_sms_overlay(module):
        with pytest.raises(SmartSmsStop) as caught:
            module._do_phone_verification(object())

    assert caught.value.code == "otp_rejected"
    assert lifecycle.acquired == 2
    assert lifecycle.cancelled == ["activation-1", "activation-2"]


def test_success_completes_without_cancel() -> None:
    module, lifecycle = fake_codex_module(
        send=[(204, "")],
        sms_codes=["123456"],
        validate=[(200, "")],
    )

    with install_codex_sms_overlay(module):
        module._do_phone_verification(object())

    assert lifecycle.statuses == [("activation-1", 1)]
    assert lifecycle.completed == ["activation-1"]
    assert lifecycle.cancelled == []
    assert lifecycle.requests == [
        (
            "https://auth.openai.com/api/accounts/add-phone/send",
            {"phone_number": "+15551234567", "channel": "sms"},
        ),
        (
            "https://auth.openai.com/api/accounts/phone-otp/validate",
            {"code": "123456"},
        ),
    ]


def test_cancel_failure_keeps_original_stop_code() -> None:
    module, lifecycle = fake_codex_module(
        send=[(400, "fraud_guard")],
        cancel_error=True,
    )

    with install_codex_sms_overlay(module):
        with pytest.raises(SmartSmsStop) as caught:
            module._do_phone_verification(object())

    assert caught.value.code == "fraud_guard"
    assert lifecycle.acquired == 1


def test_install_is_idempotent_and_restore_is_safe() -> None:
    module, _ = fake_codex_module()
    original = module._do_phone_verification

    first = install_codex_sms_overlay(module)
    second = install_codex_sms_overlay(module)

    assert second is first
    second.restore()
    first.restore()
    assert module._do_phone_verification is original


def test_missing_upstream_interface_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="缺少上游接口"):
        install_codex_sms_overlay(object())


def test_logs_do_not_include_phone_code_or_response_secret(caplog) -> None:
    caplog.set_level(logging.INFO)
    module, _ = fake_codex_module(
        send=[(400, "fraud_guard secret-response")],
        phone="15551234567",
    )

    with install_codex_sms_overlay(module):
        with pytest.raises(SmartSmsStop):
            module._do_phone_verification(object())

    assert "15551234567" not in caplog.text
    assert "123456" not in caplog.text
    assert "secret-response" not in caplog.text
    assert "reason=fraud_guard" in caplog.text
    assert "add-phone/send" in caplog.text
    assert "smart_sms_code=fraud_guard" in caplog.text


def test_sms_timeout_logs_safe_statistics_marker(caplog) -> None:
    caplog.set_level(logging.INFO)
    module, _ = fake_codex_module(
        send=[(204, ""), (204, "")],
        wait_errors=["timeout", "timeout"],
    )

    with install_codex_sms_overlay(module):
        with pytest.raises(SmartSmsStop):
            module._do_phone_verification(object())

    assert "smart_sms_code=sms_timeout" in caplog.text
    assert "15551234567" not in caplog.text
