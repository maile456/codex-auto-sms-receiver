from __future__ import annotations

import pytest

from manager.sms_retry_policy import (
    RetryAction,
    SmsFailure,
    classify_provider_failure,
    classify_send_failure,
    decide_retry,
)


@pytest.mark.parametrize(
    ("body", "status", "upstream_reason", "expected"),
    [
        (
            "invalid_request_error fraud_guard suspicious behavior",
            400,
            "send_rejected",
            SmsFailure.FRAUD_GUARD,
        ),
        (
            "invalid_request_error rate_limit_exceeded",
            429,
            "send_limited",
            SmsFailure.PHONE_RATE_LIMITED,
        ),
        (
            "phone_number_in_use",
            400,
            "phone_used_or_max",
            SmsFailure.NUMBER_IN_USE,
        ),
        ("", 400, "invalid_phone", SmsFailure.NUMBER_REJECTED),
        ("", 503, "server_error", SmsFailure.TRANSIENT_SERVER),
        ("", 204, "", None),
    ],
)
def test_classify_send_failure(
    body: str,
    status: int,
    upstream_reason: str,
    expected: SmsFailure | None,
) -> None:
    assert classify_send_failure(body, status, upstream_reason) is expected


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Hero-SMS NO_NUMBERS: Numbers Not Found", SmsFailure.NO_NUMBERS),
        ("Hero-SMS NO_BALANCE", SmsFailure.NO_BALANCE),
        ("Hero-SMS BAD_KEY", SmsFailure.BAD_KEY),
        ("Hero-SMS temporary provider failure", SmsFailure.UNKNOWN_SEND),
    ],
)
def test_classify_provider_failure(message: str, expected: SmsFailure) -> None:
    assert classify_provider_failure(message) is expected


@pytest.mark.parametrize(
    "failure",
    [
        SmsFailure.FRAUD_GUARD,
        SmsFailure.PHONE_RATE_LIMITED,
        SmsFailure.NO_NUMBERS,
        SmsFailure.NO_BALANCE,
        SmsFailure.BAD_KEY,
    ],
)
def test_terminal_failures_stop_on_first_attempt(failure: SmsFailure) -> None:
    decision = decide_retry(
        failure,
        occurrence=1,
        total_attempts=1,
        max_attempts=10,
    )

    assert decision.action is RetryAction.STOP
    assert decision.code == failure.value


@pytest.mark.parametrize(
    "failure",
    [
        SmsFailure.NUMBER_IN_USE,
        SmsFailure.NUMBER_REJECTED,
        SmsFailure.SMS_TIMEOUT,
        SmsFailure.OTP_REJECTED,
        SmsFailure.TRANSIENT_SERVER,
        SmsFailure.UNKNOWN_SEND,
    ],
)
def test_retryable_failures_allow_only_one_replacement(failure: SmsFailure) -> None:
    first = decide_retry(
        failure,
        occurrence=1,
        total_attempts=1,
        max_attempts=10,
    )
    second = decide_retry(
        failure,
        occurrence=2,
        total_attempts=2,
        max_attempts=10,
    )

    assert first.action is RetryAction.RETRY
    assert second.action is RetryAction.STOP


def test_global_retry_limit_wins_over_reason_limit() -> None:
    decision = decide_retry(
        SmsFailure.NUMBER_IN_USE,
        occurrence=1,
        total_attempts=1,
        max_attempts=1,
    )

    assert decision.action is RetryAction.STOP


def test_retry_decision_message_does_not_contain_sensitive_input() -> None:
    decision = decide_retry(
        SmsFailure.FRAUD_GUARD,
        occurrence=1,
        total_attempts=1,
        max_attempts=10,
    )

    assert decision.message == "风控熔断：停止当前账号任务"
