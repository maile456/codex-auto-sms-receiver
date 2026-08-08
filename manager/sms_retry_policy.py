from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SmsFailure(str, Enum):
    FRAUD_GUARD = "fraud_guard"
    PHONE_RATE_LIMITED = "phone_rate_limited"
    NUMBER_IN_USE = "number_in_use"
    NUMBER_REJECTED = "number_rejected"
    SMS_TIMEOUT = "sms_timeout"
    OTP_REJECTED = "otp_rejected"
    NO_NUMBERS = "sms_no_numbers"
    NO_BALANCE = "sms_no_balance"
    BAD_KEY = "sms_bad_key"
    TRANSIENT_SERVER = "phone_transient_server"
    UNKNOWN_SEND = "phone_unknown_send"


class RetryAction(str, Enum):
    STOP = "stop"
    RETRY = "retry"


@dataclass(frozen=True)
class RetryDecision:
    action: RetryAction
    code: str
    message: str


_ATTEMPT_LIMITS = {
    SmsFailure.FRAUD_GUARD: 1,
    SmsFailure.PHONE_RATE_LIMITED: 1,
    SmsFailure.NO_NUMBERS: 1,
    SmsFailure.NO_BALANCE: 1,
    SmsFailure.BAD_KEY: 1,
    SmsFailure.NUMBER_IN_USE: 2,
    SmsFailure.NUMBER_REJECTED: 2,
    SmsFailure.SMS_TIMEOUT: 2,
    SmsFailure.OTP_REJECTED: 2,
    SmsFailure.TRANSIENT_SERVER: 2,
    SmsFailure.UNKNOWN_SEND: 2,
}

_MESSAGES = {
    SmsFailure.FRAUD_GUARD: (
        "风控熔断：停止当前账号任务",
        "风控熔断：停止当前账号任务",
    ),
    SmsFailure.PHONE_RATE_LIMITED: (
        "手机号接口限流：停止当前账号任务，不自动重试",
        "手机号接口限流：停止当前账号任务，不自动重试",
    ),
    SmsFailure.NUMBER_IN_USE: (
        "号码已使用：允许换号 1/1",
        "号码已使用：停止当前账号任务",
    ),
    SmsFailure.NUMBER_REJECTED: (
        "号码被拒绝：允许换号 1/1",
        "号码被拒绝：停止当前账号任务",
    ),
    SmsFailure.SMS_TIMEOUT: (
        "短信收码超时：允许换号 1/1",
        "短信收码超时：停止当前账号任务",
    ),
    SmsFailure.OTP_REJECTED: (
        "短信验证码被拒：允许换号 1/1",
        "短信验证码被拒：停止当前账号任务",
    ),
    SmsFailure.NO_NUMBERS: (
        "HeroSMS 无可用号码：停止当前账号任务",
        "HeroSMS 无可用号码：停止当前账号任务",
    ),
    SmsFailure.NO_BALANCE: (
        "HeroSMS 余额不足：停止当前账号任务",
        "HeroSMS 余额不足：停止当前账号任务",
    ),
    SmsFailure.BAD_KEY: (
        "HeroSMS API Key 无效：停止当前账号任务",
        "HeroSMS API Key 无效：停止当前账号任务",
    ),
    SmsFailure.TRANSIENT_SERVER: (
        "手机号服务临时异常：允许换号 1/1",
        "手机号服务临时异常：停止当前账号任务",
    ),
    SmsFailure.UNKNOWN_SEND: (
        "短信发送失败：允许换号 1/1",
        "短信发送失败：停止当前账号任务",
    ),
}


def classify_send_failure(
    text: str,
    status_code: int,
    upstream_reason: str = "",
) -> SmsFailure | None:
    body = f"{text} {upstream_reason}".casefold()
    if "fraud_guard" in body or "suspicious behavior" in body:
        return SmsFailure.FRAUD_GUARD
    if (
        status_code == 429
        or any(
            marker in body
            for marker in (
                "rate_limit",
                "rate limit",
                "too many",
                "send_limited",
                "throttle",
            )
        )
    ):
        return SmsFailure.PHONE_RATE_LIMITED
    if any(
        marker in body
        for marker in (
            "phone_number_in_use",
            "phone number already in use",
            "already used",
            "phone_used_or_max",
        )
    ):
        return SmsFailure.NUMBER_IN_USE
    if upstream_reason in {
        "invalid_phone",
        "delivery_refused",
        "whatsapp_channel",
    }:
        return SmsFailure.NUMBER_REJECTED
    if status_code >= 500 or upstream_reason == "server_error":
        return SmsFailure.TRANSIENT_SERVER
    if status_code in {200, 204} and not upstream_reason:
        return None
    return SmsFailure.UNKNOWN_SEND


def classify_provider_failure(message: str) -> SmsFailure:
    value = str(message or "").casefold()
    if "no_numbers" in value or "numbers not found" in value:
        return SmsFailure.NO_NUMBERS
    if "no_balance" in value:
        return SmsFailure.NO_BALANCE
    if any(
        marker in value
        for marker in (
            "bad_key",
            "bad key",
            "invalid_key",
            "wrong_key",
            "api key is empty",
            "empty api key",
            "missing api key",
            "invalid api key",
        )
    ):
        return SmsFailure.BAD_KEY
    return SmsFailure.UNKNOWN_SEND


def decide_retry(
    failure: SmsFailure,
    *,
    occurrence: int,
    total_attempts: int,
    max_attempts: int,
) -> RetryDecision:
    configured_limit = max(1, int(max_attempts))
    reason_limit = _ATTEMPT_LIMITS[failure]
    retry = (
        max(1, int(occurrence)) < min(reason_limit, configured_limit)
        and max(1, int(total_attempts)) < configured_limit
    )
    action = RetryAction.RETRY if retry else RetryAction.STOP
    retry_message, stop_message = _MESSAGES[failure]
    return RetryDecision(
        action=action,
        code=failure.value,
        message=retry_message if retry else stop_message,
    )


__all__ = [
    "RetryAction",
    "RetryDecision",
    "SmsFailure",
    "classify_provider_failure",
    "classify_send_failure",
    "decide_retry",
]
