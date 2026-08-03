from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import threading
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit


_MAX_METADATA_JSON_BYTES = 8 * 1024 * 1024
_MAX_VIEW_LOG_BYTES = 32 * 1024 * 1024
_MAX_LOG_PAGE_SIZE = 500
_TIMELINE_TAIL_BYTES = 512 * 1024
_TIMELINE_CACHE_FILES = 24
_TIMELINE_EVENTS_PER_FILE = 300
_MAX_TIMELINE_PAGE_SIZE = 200
_PLAN_ALIASES = {
    "chatgptfreeplan": "free",
    "freeplan": "free",
    "chatgptplusplan": "plus",
    "plusplan": "plus",
    "chatgptproplan": "pro",
    "proplan": "pro",
    "chatgptteamplan": "team",
    "teamplan": "team",
    "chatgptbusinessplan": "business",
    "businessplan": "business",
    "chatgptenterpriseplan": "enterprise",
    "enterpriseplan": "enterprise",
}


def _normalized_plan_type(value: object) -> str:
    raw = str(value or "").strip().lower()
    compact = "".join(character for character in raw if character.isalnum())
    return _PLAN_ALIASES.get(compact, raw)

_ANSI_ESCAPE = re.compile(r"\x1b(?:[@-_][0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_LOG_RECORD = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:,\d{3,6})?)\s+"
    r"\[(?P<level>TRACE|DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|FATAL)\]\s*(?P<body>.*)$",
    re.IGNORECASE,
)
_LOCAL_WERKZEUG_API_ACCESS = re.compile(
    r'^(?:127\.0\.0\.1|::1|\[::1\]|localhost)\s+-\s+-\s+\[[^\]\r\n]+\]\s+'
    r'"(?:GET|HEAD|POST|PUT|PATCH|DELETE|OPTIONS)\s+/api(?:[/?][^"\s]*)?\s+'
    r'HTTP/\d(?:\.\d)?"\s+\d{3}(?:\s|$)',
    re.IGNORECASE,
)
_COMPONENT = re.compile(r"^\[([^\]\r\n]{1,80})\]\s*")
_SECRET_KEY = (
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|"
    r"client[_-]?secret|authorization[_ -]?code|callback[_ -]?code|oauth[_ -]?code|"
    r"activation[_-]?id|auth[_-]?state|state|nonce|session[_-]?id|"
    r"auth[_-]?url|callback[_-]?url|redirect[_-]?url|code[_-]?url|"
    r"code[_-]?verifier|code[_-]?challenge|authorization|bearer|"
    r"password|passwd|totp(?:[_-]?secret)?|2fa(?:[_-]?secret)?|cookie|set-cookie|"
    r"webui[_-]?auth[_-]?code|webui[_-]?session[_-]?secret|"
    r"openai-sentinel(?:-so)?-token)"
)
_JSON_SECRET = re.compile(
    rf'(?i)(["\']{_SECRET_KEY}["\']\s*:\s*["\'])(.*?)(["\'])'
)
_ASSIGNMENT_SECRET = re.compile(
    rf"(?i)(\b{_SECRET_KEY}\b\s*[=:：]\s*)([^\s,;&]+)"
)
_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|"
    r"token|key|code|state|nonce|session_id|code_verifier|code_challenge|client_secret)=)([^&#\s]+)"
)
_DICT_CODE_SECRET = re.compile(
    r'(?i)(["\'](?:code|authorization[_-]?code|callback[_-]?code|oauth[_-]?code)'
    r'["\']\s*:\s*["\'])(.*?)(["\'])'
)
_CONTEXT_CODE_SECRET = re.compile(
    r"(?i)((?:authorization|callback|oauth)[_ -]?code\s*[=:：]\s*)([^\s,;&]+)"
)
_BEARER_SECRET = re.compile(r"(?i)(\bBearer\s+)([A-Za-z0-9._~+/=-]{8,})")
_JWT_SECRET = re.compile(r"\beyJ[A-Za-z0-9_-]{16,}(?:\.[A-Za-z0-9_-]{8,}){1,2}\b")
_AUTH_CODE_SECRET = re.compile(r"\bac_[A-Za-z0-9_-]{8,}\b")
_OTP_SECRET = re.compile(
    r"(?i)((?:OTP|one[- ]time code|verification code|\bcode\b|\u9a8c\u8bc1\u7801|\u9a8c\u8bc1\u7801\u662f|\u6536\u5230\uff1a)[^\d\r\n]{0,16})(\d{4,8})"
)
_OLD_OTP_SECRET = re.compile(
    r"(?i)((?:\u66ff\u6362\u4e4b\u524d\u7684|previous OTP)[^\S\r\n]*)(\d{4,8})"
)
_PASSWORD_TOTP_MATERIAL = re.compile(
    r"(?i)([A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,63})"
    r"\|([^\r\n]+)\|([A-Z2-7][A-Z2-7=\s-]{7,})"
)
_GENERIC_API_KEY_MATERIAL = re.compile(
    r"(?i)([A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,63})"
    r"((?:----|====))(?!https?://)([^\s\r\n]+)"
)
_OTPAUTH_URI = re.compile(r"(?i)\botpauth://[^\s<>'\"(){}\uff0c\u3002\uff1b]+")
_EMAIL_ADDRESS = re.compile(
    r"(?i)(?<![A-Z0-9.!#$%&'*+/=?^_`{|}~-])"
    r"([A-Z0-9.!#$%&'*+/=?^_`{|}~-]+)@"
    r"([A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?"
    r"(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+)"
    r"(?![A-Z0-9-])"
)
_PHONE_SECRET = re.compile(
    r"(?i)((?:phone(?:_?number|[_-]?e164)?|e\.?164|actualvisible|visiblevalue|hiddenvalue|"
    r"visible|hidden|actual|raw_phone|normalized|\u5df2\u53d6\u53f7|"
    r"\u624b\u673a\u53f7(?:\s*E\.?164|\u8f93\u5165\u503c)?|\u53f7\u7801)"
    r"\s*[=:：]?\s*[\"']?\+?)(\d{7,15})"
)
_E164_PHONE = re.compile(r"(?<![\w])\+(\d{7,15})\b")
_HTTP_URL = re.compile(r"https?://[^\s<>'\"(){}，。；]+", re.IGNORECASE)
_SENSITIVE_MAILBOX_PATH = re.compile(
    r"(?i)(/(?:message|messages)/)[^\s<>'\"(){}，。；]+"
)
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BIDI_CONTROLS = re.compile("[\u202a-\u202e\u2066-\u2069]")
_USER_HOME = re.compile(r"(?i)(?:[A-Z]:\\Users\\[^\\\s]+|/home/[^/\s]+)")

_SEVERITY = {
    "TRACE": ("TRACE", 1),
    "DEBUG": ("DEBUG", 5),
    "INFO": ("INFO", 9),
    "WARNING": ("WARN", 13),
    "WARN": ("WARN", 13),
    "ERROR": ("ERROR", 17),
    "CRITICAL": ("FATAL", 21),
    "FATAL": ("FATAL", 21),
}
_LEVEL_FILTERS = {"all", "problem", "trace", "debug", "info", "warn", "error", "fatal", "unknown"}
_TIMELINE_LEVEL_FILTERS = {"important", "all", "problem", "warn", "info"}
_ACCOUNT_LOG_NAME = re.compile(r"^codex-(?P<account_id>[0-9a-f]{24})-", re.IGNORECASE)

_SMS_ACQUIRED = re.compile(
    r"^\[SMS:Hero\]\s+acquired\s+country=(?P<country>\d{1,5})\s+"
    r"price=(?P<price>auto|\d{1,12}(?:\.\d{1,8})?)\s+"
    r"action=(?P<action>[A-Za-z0-9_]{1,32})\s+activation_id=(?P<activation>[^,\s]{1,256})",
    re.IGNORECASE,
)
_SMS_PHONE_ATTEMPT = re.compile(
    r"^\[Codex\]\s+手机验证尝试\s+(?P<attempt>\d+)\s*/\s*(?P<maximum>\d+)"
    r".*?activation_id=(?P<activation>[^,\s]{1,256}).*?号码=\+?(?P<phone>\d{7,15})",
    re.IGNORECASE,
)
_SMS_SENT = re.compile(
    r"^\[Codex\]\s+短信已发送.*?activation_id=(?P<activation>[^,\s]{1,256})",
    re.IGNORECASE,
)
_SMS_TIMEOUT = re.compile(
    r"^\[Codex\]\s+号码\s+\+?(?P<phone>\d{7,15})\s+在\s+\d+s\s+内未收到短信",
    re.IGNORECASE,
)
_SMS_CODE_RECEIVED = re.compile(
    r"(?:第\s*\d+\s*轮收到验证码\s*[：:]\s*\d{4,8}\b|手机\s*OTP\s*收到)",
    re.IGNORECASE,
)
_SMS_VALIDATE_FAILED = re.compile(r"^\[Codex\]\s+phone-otp/validate\s+失败\b", re.IGNORECASE)
_SMS_VERIFIED = re.compile(r"^\[Codex\]\s+手机号验证通过\b", re.IGNORECASE)

_SMS_STATUS: dict[str, tuple[str, str]] = {
    "acquired": ("已取号", "号码已获取"),
    "waiting_code": ("等待验证码", "短信请求已发送"),
    "code_received": ("已收到验证码", "接码平台已收到验证码"),
    "verified": ("验证成功", "手机号验证通过"),
    "fraud_guard": ("风控拒绝", "OpenAI 风控拒绝"),
    "number_in_use": ("号码已使用", "号码已被其他账号使用"),
    "rate_limited": ("请求过多", "OpenAI 限制了验证请求"),
    "send_rejected": ("发送被拒", "OpenAI 未接受短信发送请求"),
    "sms_timeout": ("收码超时", "等待短信验证码超时"),
    "code_rejected": ("验证码被拒", "OpenAI 未接受收到的验证码"),
    "replaced": ("已换号", "当前号码已被新号码替换"),
}
_SMS_PENDING_STATUSES = {"acquired", "waiting_code", "code_received"}
_SMS_FAILED_STATUSES = {
    "fraud_guard",
    "number_in_use",
    "rate_limited",
    "send_rejected",
    "sms_timeout",
    "code_rejected",
    "replaced",
}


def _iso_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def _artifact_id(kind: str, relative_name: str) -> str:
    value = f"{kind}:{relative_name}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()[:24]


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _safe_oauth_metadata(payload: dict[str, Any]) -> dict[str, str]:
    """Extract display-only OAuth metadata without returning any token value."""

    auth: dict[str, Any] = {}
    for token_key in ("id_token", "access_token"):
        token = str(payload.get(token_key) or "")
        parts = token.split(".")
        if len(parts) < 2 or len(parts[1]) > 128 * 1024:
            continue
        try:
            encoded = parts[1] + "=" * (-len(parts[1]) % 4)
            claims = json.loads(base64.urlsafe_b64decode(encoded).decode("utf-8"))
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
        if not isinstance(claims, dict):
            continue
        candidate = claims.get("https://api.openai.com/auth")
        if isinstance(candidate, dict):
            auth = candidate
            break

    plan_type = _normalized_plan_type(
        payload.get("plan_type") or auth.get("chatgpt_plan_type") or ""
    )
    active_until = str(
        payload.get("subscription_active_until")
        or auth.get("chatgpt_subscription_active_until")
        or ""
    ).strip()
    if active_until:
        try:
            parsed = datetime.fromisoformat(active_until.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                active_until = ""
        except ValueError:
            active_until = ""
    account_id = str(
        payload.get("account_id") or auth.get("chatgpt_account_id") or ""
    ).strip()
    if len(account_id) > 14:
        account_hint = f"{account_id[:8]}…{account_id[-4:]}"
    else:
        account_hint = account_id
    return {
        "plan_type": plan_type[:40],
        "subscription_active_until": active_until[:80],
        "subscription_checked_at": str(payload.get("subscription_checked_at") or "")[:80],
        "subscription_source": str(payload.get("subscription_source") or "")[:40],
        "subscription_error": str(payload.get("subscription_error") or "")[:160],
        "account_hint": account_hint[:40],
    }


def _redact_log_text(value: str) -> str:
    """Remove credentials and account identifiers from WebUI log details."""

    text = _ANSI_ESCAPE.sub("", str(value or ""))
    text = _CONTROL_CHARACTERS.sub("", text)
    text = _BIDI_CONTROLS.sub("", text)
    # Imported password/TOTP material can otherwise evade the labelled-secret
    # rules because the separators carry the only context.  Keep the address
    # until the final email pass so it receives the same stable masking.
    text = _PASSWORD_TOTP_MATERIAL.sub(r"\1|[REDACTED]|[REDACTED]", text)
    text = _GENERIC_API_KEY_MATERIAL.sub(r"\1\2[REDACTED]", text)
    # An otpauth URI contains both the Base32 seed and often the account name.
    # Redact the whole URI instead of trying to preserve its non-secret query.
    text = _OTPAUTH_URI.sub("[REDACTED-OTPAUTH]", text)
    text = _JSON_SECRET.sub(r"\1[REDACTED]\3", text)
    text = _DICT_CODE_SECRET.sub(r"\1[REDACTED]\3", text)
    text = _QUERY_SECRET.sub(r"\1[REDACTED]", text)
    text = _BEARER_SECRET.sub(r"\1[REDACTED]", text)
    text = _ASSIGNMENT_SECRET.sub(r"\1[REDACTED]", text)
    text = _CONTEXT_CODE_SECRET.sub(r"\1[REDACTED]", text)
    text = _JWT_SECRET.sub("[REDACTED-JWT]", text)
    text = _AUTH_CODE_SECRET.sub("[REDACTED-AUTH-CODE]", text)
    text = _OTP_SECRET.sub(r"\1[REDACTED]", text)
    text = _OLD_OTP_SECRET.sub(r"\1[REDACTED]", text)
    # Generic mailbox URLs commonly carry the access token and address in the
    # path rather than in a query string.  This also catches urllib3's relative
    # request target, which has no scheme/host for _HTTP_URL to match.
    text = _SENSITIVE_MAILBOX_PATH.sub(r"\1[REDACTED]", text)

    def mask_phone(match: re.Match) -> str:
        digits = match.group(2)
        return f"{match.group(1)}***{digits[-4:]}"

    text = _PHONE_SECRET.sub(mask_phone, text)
    text = _E164_PHONE.sub(lambda match: f"+***{match.group(1)[-4:]}", text)

    def remove_url_query(match: re.Match) -> str:
        raw = match.group(0)
        try:
            parts = urlsplit(raw)
        except ValueError:
            return "[REDACTED-URL]"
        if not parts.query and not parts.fragment:
            return raw
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "[REDACTED]", ""))

    text = _HTTP_URL.sub(remove_url_query, text)
    text = _USER_HOME.sub("[USER_HOME]", text)

    def mask_email(match: re.Match) -> str:
        local = match.group(1)
        return f"{local[0]}***@{match.group(2)}"

    return _EMAIL_ADDRESS.sub(mask_email, text)


def _event_timestamp(value: str) -> str:
    if not value:
        return ""
    for pattern in ("%Y-%m-%d %H:%M:%S,%f", "%Y-%m-%d %H:%M:%S"):
        try:
            # Source logs use local wall-clock time. astimezone() attaches the
            # host timezone so the API returns a valid ISO-8601 timestamp.
            return datetime.strptime(value, pattern).astimezone().isoformat()
        except ValueError:
            continue
    return value


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _success_rate(verified: int, acquired: int) -> float:
    return round((verified / acquired) * 100, 1) if acquired else 0.0


def _set_sms_status(record: dict[str, Any], status: str, timestamp: str = "") -> None:
    label, detail = _SMS_STATUS[status]
    record["status"] = status
    record["status_label"] = label
    record["detail"] = detail
    if status == "verified":
        record["verified"] = True
    if status in _SMS_FAILED_STATUSES or status == "verified":
        record["completed_at"] = timestamp


def _sms_rejection_status(body: str) -> str:
    lowered = body.casefold()
    if "phone_number_in_use" in lowered or "phone number already in use" in lowered:
        return "number_in_use"
    if "rate_limit_exceeded" in lowered or "reason=send_limited" in lowered:
        return "rate_limited"
    if "fraud_guard" in lowered or "suspicious behavior" in lowered:
        return "fraud_guard"
    return "send_rejected"


def _parse_sms_log(text: str, *, relative: str) -> tuple[list[dict[str, Any]], int]:
    """Parse one dedicated OAuth log while keeping sensitive join keys internal."""

    rows: list[dict[str, Any]] = []
    by_activation: dict[str, dict[str, Any]] = {}
    active: dict[str, Any] | None = None
    cancel_errors = 0

    for line in text.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        if len(line) > 65536:
            continue
        matched_log = _LOG_RECORD.match(line)
        if not matched_log:
            continue
        timestamp = _event_timestamp(matched_log.group("timestamp"))
        body = matched_log.group("body")

        matched = _SMS_ACQUIRED.match(body)
        if matched:
            if active is not None and str(active.get("status")) in _SMS_PENDING_STATUSES:
                _set_sms_status(active, "replaced", timestamp)
            activation = matched.group("activation")[:256]
            raw_price = matched.group("price").lower()
            price: Decimal | None = None
            if raw_price != "auto":
                try:
                    parsed_price = Decimal(raw_price)
                    if parsed_price.is_finite() and parsed_price >= 0:
                        price = parsed_price
                except InvalidOperation:
                    price = None
            raw_action = matched.group("action").lower()
            action = {
                "getnumber": "getNumber",
                "getnumberv2": "getNumberV2",
            }.get(raw_action, "other")
            status_label, detail = _SMS_STATUS["acquired"]
            active = {
                "id": hashlib.sha256(
                    f"sms:{relative}\0{activation}".encode("utf-8", errors="replace")
                ).hexdigest()[:20],
                "acquired_at": timestamp,
                "completed_at": "",
                "country_id": int(matched.group("country")),
                "phone_number": "",
                "price": _decimal_text(price) if price is not None else None,
                "action": action,
                "attempt": None,
                "max_attempts": None,
                "status": "acquired",
                "status_label": status_label,
                "detail": detail,
                "sms_sent": False,
                "code_received": False,
                "verified": False,
                "_activation": activation,
                "_phone": "",
                "_price_decimal": price,
            }
            rows.append(active)
            by_activation[activation] = active
            continue

        matched = _SMS_PHONE_ATTEMPT.match(body)
        if matched:
            activation = matched.group("activation")[:256]
            record = by_activation.get(activation)
            if record is None:
                continue
            phone = matched.group("phone")
            record["attempt"] = int(matched.group("attempt"))
            record["max_attempts"] = int(matched.group("maximum"))
            record["phone_number"] = f"+{phone}"
            record["_phone"] = phone
            active = record
            continue

        matched = _SMS_SENT.match(body)
        if matched:
            activation = matched.group("activation")[:256]
            record = by_activation.get(activation)
            if record is None:
                continue
            record["sms_sent"] = True
            if str(record.get("status")) in _SMS_PENDING_STATUSES:
                _set_sms_status(record, "waiting_code")
            active = record
            continue

        if "add-phone/send" in body.casefold() and "reason=" in body.casefold():
            if active is not None and str(active.get("status")) in _SMS_PENDING_STATUSES:
                _set_sms_status(active, _sms_rejection_status(body), timestamp)
            continue

        matched = _SMS_TIMEOUT.match(body)
        if matched:
            phone = matched.group("phone")
            record = next(
                (item for item in reversed(rows) if item.get("_phone") == phone),
                active,
            )
            if record is not None and str(record.get("status")) in _SMS_PENDING_STATUSES:
                _set_sms_status(record, "sms_timeout", timestamp)
            continue

        if _SMS_CODE_RECEIVED.search(body):
            if active is not None and str(active.get("status")) in _SMS_PENDING_STATUSES:
                active["code_received"] = True
                _set_sms_status(active, "code_received")
            continue

        if _SMS_VALIDATE_FAILED.match(body):
            if active is not None and str(active.get("status")) in _SMS_PENDING_STATUSES:
                _set_sms_status(active, "code_rejected", timestamp)
            continue

        if _SMS_VERIFIED.match(body):
            if active is not None and str(active.get("status")) in _SMS_PENDING_STATUSES:
                _set_sms_status(active, "verified", timestamp)
            continue

        if body.casefold().startswith("[sms:hero] cancel failed"):
            cancel_errors += 1

    return rows, cancel_errors


def _decode_log_bytes(payload: bytes) -> str:
    """Decode current UTF-8 logs while retaining compatibility with Windows CP936 logs."""

    try:
        return payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        try:
            return payload.decode("gb18030", errors="strict")
        except UnicodeDecodeError:
            # A damaged or mixed-encoding log should remain viewable. This final
            # fallback is intentionally reached only after both strict decoders.
            return payload.decode("utf-8", errors="replace")


def _read_log_text(path: Path) -> str:
    """Read a complete log for the backwards-compatible single-file API."""

    return _decode_log_bytes(path.read_bytes())


def _read_log_tail_text(path: Path, *, size: int, max_bytes: int) -> tuple[str, bool]:
    """Read at most the complete lines in the last ``max_bytes`` of a log."""

    truncated = size > max_bytes
    with path.open("rb") as handle:
        if truncated:
            handle.seek(max(0, size - max_bytes))
        payload = handle.read(max_bytes)
    if truncated:
        # The first bytes usually start in the middle of a UTF-8 character or
        # log record. Dropping that partial line also prevents it being joined
        # to the following record as a misleading continuation.
        newline = payload.find(b"\n")
        payload = payload[newline + 1 :] if newline >= 0 else b""
    return _decode_log_bytes(payload), truncated


def _timeline_stage(body: str, category: str) -> str:
    lowered = str(body or "").casefold()
    if category == "system":
        return "系统"
    if "[sms:hero]" in lowered or any(
        marker in lowered
        for marker in ("手机验证", "手机号验证", "短信已发送", "add-phone", "phone-otp")
    ):
        return "接码"
    if "[outlook]" in lowered or "邮箱 otp" in lowered or "邮箱验证码" in lowered:
        return "邮箱验证"
    if any(marker in lowered for marker in ("sentinel", "turnstile", "captcha")):
        return "安全验证"
    if any(
        marker in lowered
        for marker in ("authorization code", "换 token", "oauth", "开始授权", "授权地址")
    ):
        return "OAuth"
    if "成功：" in body or "失败：" in body:
        return "任务结果"
    return "登录"


def _timeline_summary(body: str) -> str:
    """Turn a technical record into a short operator-facing Chinese summary."""

    detail = re.sub(r"^\[[^\]\r\n]{1,80}\]\s*", "", str(body or "").strip())
    lowered = detail.casefold()
    if "phone_number_in_use" in lowered or "phone number already in use" in lowered:
        return "号码已被使用，准备换号"
    if "fraud_guard" in lowered or "suspicious behavior" in lowered:
        return "号码被风控拒绝，准备换号"
    if "rate_limit_exceeded" in lowered or "reason=send_limited" in lowered:
        return "手机号验证请求过多"
    if "tls connect error" in lowered or "sslerror" in lowered:
        return "TLS 网络连接失败"
    if "短信已发送" in detail:
        return "短信已发送，正在等待验证码"
    if "内未收到短信" in detail:
        return "等待短信验证码超时，准备换号"
    if "手机号验证通过" in detail:
        return "手机号验证成功"
    if "手机验证尝试" in detail:
        matched = re.search(r"手机验证尝试\s*(\d+\s*/\s*\d+)", detail)
        return f"正在验证手机号（第 {matched.group(1)} 次）" if matched else "正在验证手机号"
    if "[REDACTED]" in detail and "邮箱 otp 收到" in lowered:
        return "已收到邮箱验证码"
    if "邮箱 otp 验证通过" in lowered:
        return "邮箱验证码验证成功"
    if "等待邮箱 otp" in lowered or ("已提交邮箱" in detail and "otp" in lowered):
        return "等待邮箱验证码"
    if "acquired country=" in lowered:
        matched = re.search(r"country=(\d+).*?price=([^\s]+)", detail, re.IGNORECASE)
        if matched:
            return f"已获取号码（国家 {matched.group(1)}，价格 {matched.group(2)}）"
        return "已获取 Hero SMS 号码"
    if "trying country=" in lowered:
        return "正在购买 Hero SMS 号码"
    if "成功：" in detail:
        return "OAuth 登录成功，凭证已保存"
    if "失败：" in detail:
        reason = detail.split("失败：", 1)[-1].strip()
        return ("任务失败：" + reason)[:240]
    compact = " ".join(detail.split())
    return compact[:240] if compact else "日志事件"


def _timeline_is_important(*, body: str, stage: str, summary: str, severity: str) -> bool:
    """Keep the default operator timeline focused on decisions and milestones."""

    if str(severity or "").strip().lower() in {"warn", "error", "fatal"}:
        return True
    if stage == "任务结果":
        return True
    lowered = str(body or "").casefold()
    markers = (
        "codex-only start",
        "开始授权",
        "邮箱 otp 收到",
        "邮箱 otp 验证通过",
        "已收到邮箱验证码",
        "邮箱验证码验证成功",
        "acquired country=",
        "手机验证尝试",
        "正在验证手机号",
        "短信已发送",
        "内未收到短信",
        "手机号验证通过",
        "手机号验证成功",
        "phone_number_in_use",
        "fraud_guard",
        "rate_limit_exceeded",
        "换 token 成功",
        "oauth 登录成功",
        "凭证已保存",
    )
    return any(marker in lowered for marker in markers) or summary.startswith("任务失败")


def _timeline_is_problem(*, body: str, summary: str, severity: str) -> bool:
    """Classify final task failures as problems even when logged as WARNING."""

    if str(severity or "").strip().lower() in {"error", "fatal"}:
        return True
    original = str(body or "").strip()
    final_detail = re.sub(r"^\[[^\]\r\n]{1,80}\]\s*", "", original)
    if final_detail.startswith("失败："):
        return True
    lowered = original.casefold()
    return summary.startswith("任务失败") or any(
        marker in lowered
        for marker in (
            "pipeline worker job failed",
            "codex oauth 失败",
            "服务重启中断了上一次任务",
        )
    )


def _component_name(body: str, category: str) -> str:
    matched = _COMPONENT.match(body or "")
    if matched:
        return matched.group(1).strip()
    return "Codex OAuth" if category == "oauth" else "WebUI"


def _parse_log_events(text: str, *, relative: str, category: str) -> list[dict[str, Any]]:
    """Map Python text logs to an OpenTelemetry-shaped event collection."""

    redacted = _redact_log_text(text).replace("\r\n", "\n").replace("\r", "\n")
    rows: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    def finish() -> None:
        nonlocal current
        if current is None:
            return
        current["body"] = "\n".join(current.pop("_body_lines"))
        current["raw"] = "\n".join(current.pop("_raw_lines"))
        current["line_end"] = current.pop("_line_end")
        current["instrumentation_scope"] = {
            "name": _component_name(current["body"], category)
        }
        rows.append(current)
        current = None

    for line_number, line in enumerate(redacted.splitlines(), start=1):
        if len(line) > 65536:
            line = "[超长日志行已省略]"
        matched = _LOG_RECORD.match(line)
        if matched:
            finish()
            original_level = matched.group("level").upper()
            severity_text, severity_number = _SEVERITY.get(original_level, (original_level, 0))
            body = matched.group("body")
            current = {
                "timestamp": _event_timestamp(matched.group("timestamp")),
                "severity_text": severity_text,
                "severity_number": severity_number,
                "severity_original": original_level,
                "event_name": "log.record",
                "resource": {"service.name": "codex-auto-sms-receiver"},
                "attributes": {
                    "event.dataset": category,
                    "log.file.name": relative,
                },
                "line_start": line_number,
                "_line_end": line_number,
                "_body_lines": [body],
                "_raw_lines": [line],
            }
            continue

        if not line and current is None:
            continue
        if (
            current is not None
            and current.get("severity_text") == "UNKNOWN"
            and line
            and not line[:1].isspace()
        ):
            finish()
        if current is None:
            current = {
                "timestamp": "",
                "severity_text": "UNKNOWN",
                "severity_number": 0,
                "severity_original": "",
                "event_name": "log.record",
                "resource": {"service.name": "codex-auto-sms-receiver"},
                "attributes": {
                    "event.dataset": category,
                    "log.file.name": relative,
                },
                "line_start": line_number,
                "_line_end": line_number,
                "_body_lines": [line],
                "_raw_lines": [line],
            }
        else:
            current["_line_end"] = line_number
            current["_body_lines"].append(line)
            current["_raw_lines"].append(line)
    finish()

    # The browser polls local API endpoints. Werkzeug writes every poll to the
    # same stderr log, so showing those records creates a noisy feedback loop.
    # Match only loopback request lines under /api; other access and app logs stay.
    rows = [
        item
        for item in rows
        if not _LOCAL_WERKZEUG_API_ACCESS.match(str(item.get("body") or "").strip())
    ]
    for index, item in enumerate(rows):
        item["event_index"] = index
    return rows


class ArtifactStore:
    """Index and resolve OAuth credential/log artifacts without exposing paths or tokens."""

    def __init__(self, data_dir: Path, log_dir: Path):
        self.data_dir = Path(data_dir)
        self.log_dir = Path(log_dir)
        self.credential_dir = self.data_dir / "codex_accounts"
        self._timeline_cache: OrderedDict[
            tuple[str, int, int], tuple[tuple[dict[str, Any], ...], bool]
        ] = OrderedDict()
        self._timeline_cache_lock = threading.RLock()
        self._sms_statistics_cache_key: tuple[tuple[str, int, int], ...] | None = None
        self._sms_statistics_cache_value: dict[str, Any] | None = None
        self._sms_statistics_cache_lock = threading.RLock()

    @staticmethod
    def _files(root: Path, pattern: str, *, recursive: bool) -> Iterable[tuple[Path, str]]:
        if not root.is_dir():
            return []
        resolved_root = root.resolve()
        iterator = root.rglob(pattern) if recursive else root.glob(pattern)
        rows: list[tuple[Path, str]] = []
        for path in iterator:
            try:
                resolved = path.resolve(strict=True)
                if not resolved.is_file() or not _inside(resolved_root, resolved):
                    continue
                relative = resolved.relative_to(resolved_root).as_posix()
            except (OSError, ValueError):
                continue
            rows.append((resolved, relative))
        return rows

    @staticmethod
    def _credential_metadata(path: Path) -> dict[str, Any]:
        try:
            if path.stat().st_size > _MAX_METADATA_JSON_BYTES:
                raise ValueError("file too large")
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {
                "kind": "invalid",
                "email": "",
                "expired": "",
                "has_access_token": False,
                "has_refresh_token": False,
                "has_id_token": False,
                "exportable": False,
                "plan_type": "",
                "subscription_active_until": "",
                "subscription_checked_at": "",
                "subscription_source": "",
                "subscription_error": "",
                "account_hint": "",
            }
        if not isinstance(payload, dict):
            payload = {}
        type_name = str(payload.get("type") or "").strip().lower()
        access = bool(str(payload.get("access_token") or "").strip())
        refresh = bool(str(payload.get("refresh_token") or "").strip())
        id_token = bool(str(payload.get("id_token") or "").strip())
        has_token = access or refresh or id_token
        if type_name in {"codex_cpa_callback", "codex_sub2_callback"}:
            kind = "receipt"
        elif type_name == "codex" and has_token:
            kind = "credential"
        elif has_token:
            kind = "credential"
        else:
            kind = "record"
        email = str(payload.get("email") or "").strip()[:320]
        expired = str(payload.get("expired") or payload.get("expires_at") or "").strip()[:80]
        oauth_metadata = _safe_oauth_metadata(payload)
        return {
            "kind": kind,
            "email": email,
            "expired": expired,
            "has_access_token": access,
            "has_refresh_token": refresh,
            "has_id_token": id_token,
            "exportable": kind == "credential" and has_token,
            **oauth_metadata,
        }

    def list_credentials(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for path, relative in self._files(self.credential_dir, "*.json", recursive=False):
            try:
                stat = path.stat()
            except OSError:
                continue
            metadata = self._credential_metadata(path)
            rows.append(
                {
                    "id": _artifact_id("credential", relative),
                    "name": path.name,
                    "size": stat.st_size,
                    "modified_at": _iso_timestamp(stat.st_mtime),
                    **metadata,
                }
            )
        rows.sort(key=lambda row: str(row["modified_at"]), reverse=True)
        return rows

    def list_logs(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for path, relative in self._files(self.log_dir, "*.log", recursive=True):
            try:
                stat = path.stat()
            except OSError:
                continue
            name_lower = path.name.lower()
            rows.append(
                {
                    "id": _artifact_id("log", relative),
                    "name": relative,
                    "category": "oauth" if name_lower.startswith("codex-") else "system",
                    "size": stat.st_size,
                    "modified_at": _iso_timestamp(stat.st_mtime),
                    "downloadable": True,
                }
            )
        rows.sort(key=lambda row: str(row["modified_at"]), reverse=True)
        return rows

    def overview(self) -> dict[str, Any]:
        credentials = self.list_credentials()
        logs = self.list_logs()
        return {
            "credentials": credentials,
            "logs": logs,
            "counts": {
                "credentials": sum(1 for item in credentials if item["exportable"]),
                "receipts": sum(1 for item in credentials if item["kind"] == "receipt"),
                "logs": len(logs),
                "oauth_logs": sum(1 for item in logs if item["category"] == "oauth"),
            },
        }

    def _resolve(self, kind: str, artifact_id: str) -> tuple[Path, str] | None:
        artifact_id = str(artifact_id or "").strip().lower()
        if len(artifact_id) != 24 or any(character not in "0123456789abcdef" for character in artifact_id):
            return None
        if kind == "credential":
            root, pattern, recursive = self.credential_dir, "*.json", False
        elif kind == "log":
            root, pattern, recursive = self.log_dir, "*.log", True
        else:
            return None
        for path, relative in self._files(root, pattern, recursive=recursive):
            if _artifact_id(kind, relative) == artifact_id:
                return path, relative
        return None

    def credential_file(self, artifact_id: str) -> Path | None:
        value = self._resolve("credential", artifact_id)
        return value[0] if value else None

    def exportable_credential_for_email(self, email: str) -> dict[str, Any] | None:
        """Return the newest exportable credential metadata matching an account email."""

        target = str(email or "").strip().casefold()
        if not target:
            return None
        return next(
            (
                item
                for item in self.list_credentials()
                if item.get("exportable")
                and str(item.get("email") or "").strip().casefold() == target
            ),
            None,
        )

    def exportable_credential_file(
        self, artifact_id: str, *, expected_email: str | None = None
    ) -> Path | None:
        resolved = self._resolve("credential", artifact_id)
        if resolved is None:
            return None
        path, _ = resolved
        metadata = self._credential_metadata(path)
        if not metadata.get("exportable"):
            return None
        if expected_email and str(metadata.get("email") or "").strip().casefold() != str(
            expected_email
        ).strip().casefold():
            return None
        return path

    def delete_credentials(self, artifact_ids: list[str]) -> int:
        """Delete selected credential artifacts after resolving every ID first."""

        normalized = list(dict.fromkeys(str(value or "").strip().lower() for value in artifact_ids))
        if not normalized or any(not value for value in normalized):
            raise ValueError("请至少选择一个凭证")
        paths: list[Path] = []
        for artifact_id in normalized:
            path = self.exportable_credential_file(artifact_id)
            if path is None:
                raise KeyError("所选凭证不存在或不可删除")
            paths.append(path)
        for path in paths:
            path.unlink()
        return len(paths)

    def phone_verification_for_account(self, account_id: str) -> dict[str, str] | None:
        """Recover the newest successful phone verification from this account's OAuth logs."""

        normalized = str(account_id or "").strip().lower()
        if len(normalized) != 24 or any(char not in "0123456789abcdef" for char in normalized):
            return None
        candidates: list[tuple[float, Path]] = []
        for path, _ in self._files(self.log_dir, "*.log", recursive=True):
            if not path.name.lower().startswith(f"codex-{normalized}-"):
                continue
            try:
                candidates.append((path.stat().st_mtime, path))
            except OSError:
                continue
        candidates.sort(key=lambda item: item[0], reverse=True)
        for _, path in candidates:
            try:
                if path.stat().st_size > _MAX_VIEW_LOG_BYTES:
                    continue
                content = _read_log_text(path)
            except OSError:
                continue
            phone = ""
            verified_at = ""
            for line in content.replace("\r\n", "\n").replace("\r", "\n").splitlines():
                matched_log = _LOG_RECORD.match(line)
                if not matched_log:
                    continue
                body = matched_log.group("body")
                attempt = _SMS_PHONE_ATTEMPT.match(body)
                if attempt:
                    phone = f"+{attempt.group('phone')}"
                if _SMS_VERIFIED.match(body) and phone:
                    verified_at = _event_timestamp(matched_log.group("timestamp"))
            if phone and verified_at:
                return {"phone_number": phone, "phone_verified_at": verified_at}
        return None

    def log_file(self, artifact_id: str) -> Path | None:
        value = self._resolve("log", artifact_id)
        return value[0] if value else None

    def read_log_events(
        self,
        artifact_id: str,
        *,
        offset: int = 0,
        limit: int = 200,
        level: str = "all",
        query: str = "",
        order: str = "desc",
    ) -> dict[str, Any] | None:
        """Return a redacted, filtered page of OpenTelemetry-shaped log records."""

        resolved = self._resolve("log", artifact_id)
        if resolved is None:
            return None
        path, relative = resolved
        try:
            stat = path.stat()
        except OSError:
            return None
        if stat.st_size > _MAX_VIEW_LOG_BYTES:
            raise ValueError("日志文件超过前端查看上限（32 MB），请下载后查看")
        if offset < 0:
            raise ValueError("日志偏移量不能为负数")
        if limit < 1 or limit > _MAX_LOG_PAGE_SIZE:
            raise ValueError(f"每页日志数量必须在 1 - {_MAX_LOG_PAGE_SIZE} 之间")
        normalized_level = str(level or "all").strip().lower()
        if normalized_level not in _LEVEL_FILTERS:
            raise ValueError("日志级别筛选格式不正确")
        normalized_order = str(order or "desc").strip().lower()
        if normalized_order not in {"asc", "desc"}:
            raise ValueError("日志排序仅支持 asc / desc")
        normalized_query = str(query or "").strip()
        if len(normalized_query) > 200 or any(ord(char) < 32 for char in normalized_query):
            raise ValueError("日志搜索关键词过长或包含控制字符")

        try:
            content = _read_log_text(path)
        except OSError as exc:
            raise OSError(f"无法读取日志文件: {type(exc).__name__}") from exc
        category = "oauth" if path.name.lower().startswith("codex-") else "system"
        events = _parse_log_events(content, relative=relative, category=category)

        level_counts = {name: 0 for name in ("trace", "debug", "info", "warn", "error", "fatal", "unknown")}
        for event in events:
            key = str(event.get("severity_text") or "unknown").lower()
            key = key if key in level_counts else "unknown"
            level_counts[key] += 1

        filtered = events
        if normalized_level == "problem":
            filtered = [
                event
                for event in filtered
                if str(event.get("severity_text") or "unknown").lower() in {"error", "fatal"}
            ]
        elif normalized_level != "all":
            filtered = [
                event
                for event in filtered
                if str(event.get("severity_text") or "unknown").lower() == normalized_level
            ]
        if normalized_query:
            needle = normalized_query.casefold()
            filtered = [
                event
                for event in filtered
                if needle
                in "\n".join(
                    (
                        str(event.get("timestamp") or ""),
                        str(event.get("severity_text") or ""),
                        str((event.get("instrumentation_scope") or {}).get("name") or ""),
                        str(event.get("body") or ""),
                    )
                ).casefold()
            ]
        if normalized_order == "desc":
            filtered = list(reversed(filtered))

        page = filtered[offset : offset + limit]
        observed_at = _iso_timestamp(stat.st_mtime)
        return {
            "schema": {
                "name": "OpenTelemetry LogRecord",
                "field_mapping": {
                    "timestamp": "@timestamp",
                    "severity_text": "log.level",
                    "body": "message",
                    "resource.service.name": "service.name",
                    "instrumentation_scope.name": "log.logger",
                    "attributes.event.dataset": "event.dataset",
                    "attributes.log.file.name": "log.file.name",
                },
            },
            "log": {
                "id": str(artifact_id),
                "name": relative,
                "category": category,
                "size": stat.st_size,
                "modified_at": observed_at,
                "redacted": True,
            },
            "total_events": len(events),
            "filtered_events": len(filtered),
            "level_counts": level_counts,
            "offset": offset,
            "limit": limit,
            "order": normalized_order,
            "has_more": offset + len(page) < len(filtered),
            "next_offset": offset + len(page),
            "events": page,
        }

    def _timeline_events_for_file(
        self,
        path: Path,
        relative: str,
        *,
        size: int,
        mtime_ns: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Return cached redacted events parsed from a bounded file tail."""

        cache_key = (str(path), int(mtime_ns), int(size))
        with self._timeline_cache_lock:
            cached = self._timeline_cache.get(cache_key)
            if cached is not None:
                self._timeline_cache.move_to_end(cache_key)
                events, truncated = cached
                return [dict(event) for event in events], truncated

        content, truncated = _read_log_tail_text(
            path,
            size=size,
            max_bytes=_TIMELINE_TAIL_BYTES,
        )
        category = "oauth" if path.name.lower().startswith("codex-") else "system"
        parsed = _parse_log_events(content, relative=relative, category=category)[
            -_TIMELINE_EVENTS_PER_FILE:
        ]
        immutable = tuple(dict(event) for event in parsed)
        with self._timeline_cache_lock:
            # A growing file creates a new key. Remove old versions for the
            # same path so frequently updated server logs cannot fill the LRU.
            stale = [key for key in self._timeline_cache if key[0] == str(path)]
            for key in stale:
                self._timeline_cache.pop(key, None)
            self._timeline_cache[cache_key] = (immutable, truncated)
            self._timeline_cache.move_to_end(cache_key)
            while len(self._timeline_cache) > _TIMELINE_CACHE_FILES:
                self._timeline_cache.popitem(last=False)
        return [dict(event) for event in immutable], truncated

    def read_log_timeline(
        self,
        *,
        offset: int = 0,
        limit: int = 100,
        level: str = "all",
        query: str = "",
        account_emails: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Aggregate recent, redacted events without loading complete log files."""

        if offset < 0:
            raise ValueError("日志偏移量不能为负数")
        if limit < 1 or limit > _MAX_TIMELINE_PAGE_SIZE:
            raise ValueError(f"每页时间线数量必须在 1 - {_MAX_TIMELINE_PAGE_SIZE} 之间")
        normalized_level = str(level or "all").strip().lower()
        if normalized_level not in _TIMELINE_LEVEL_FILTERS:
            raise ValueError("时间线级别仅支持 important / all / problem / warn / info")
        normalized_query = str(query or "").strip()
        if len(normalized_query) > 200 or any(ord(char) < 32 for char in normalized_query):
            raise ValueError("日志搜索关键词过长或包含控制字符")
        normalized_account_emails = {
            str(account_id or "").strip().lower(): str(email or "").strip()
            for account_id, email in (account_emails or {}).items()
            if str(account_id or "").strip()
        }

        files: list[tuple[int, int, Path, str]] = []
        for path, relative in self._files(self.log_dir, "*.log", recursive=True):
            if any(part.casefold() == "archive" for part in relative.split("/")):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            files.append((stat.st_mtime_ns, stat.st_size, path, relative))
        files.sort(key=lambda item: (item[0], item[3]), reverse=True)
        files_total = len(files)
        files = files[:_TIMELINE_CACHE_FILES]

        candidates: list[dict[str, Any]] = []
        skipped_files = 0
        for mtime_ns, size, path, relative in files:
            try:
                parsed, tail_truncated = self._timeline_events_for_file(
                    path,
                    relative,
                    size=size,
                    mtime_ns=mtime_ns,
                )
            except OSError:
                skipped_files += 1
                continue
            category = "oauth" if path.name.lower().startswith("codex-") else "system"
            matched_account = _ACCOUNT_LOG_NAME.match(path.name)
            account_id = matched_account.group("account_id").lower() if matched_account else ""
            modified_at = _iso_timestamp(mtime_ns / 1_000_000_000)
            source = {
                "id": _artifact_id("log", relative),
                "name": relative,
                "modified_at": modified_at,
                "tail_truncated": tail_truncated,
            }
            for event in parsed:
                detail = str(event.get("body") or "")
                raw = str(event.get("raw") or detail)
                event_index = int(event.get("event_index") or 0)
                stage = _timeline_stage(detail, category)
                summary = _timeline_summary(detail)
                severity = str(event.get("severity_text") or "")
                event.update(
                    {
                        "id": hashlib.sha256(
                            f"{relative}:{mtime_ns}:{event_index}".encode("utf-8")
                        ).hexdigest()[:24],
                        "account_id": account_id,
                        "account_email": normalized_account_emails.get(account_id, ""),
                        "category": category,
                        "stage": stage,
                        "summary": summary,
                        "problem": _timeline_is_problem(
                            body=detail,
                            summary=summary,
                            severity=severity,
                        ),
                        "important": _timeline_is_important(
                            body=detail,
                            stage=stage,
                            summary=summary,
                            severity=severity,
                        ),
                        "detail": detail,
                        "raw": raw,
                        "source": source,
                        "_sort_timestamp": str(event.get("timestamp") or modified_at),
                        "_source_mtime_ns": mtime_ns,
                    }
                )
                candidates.append(event)

        # Dedicated per-account files intentionally duplicate worker output
        # also written to server stderr. Keep one copy, preferring the event
        # that carries an account id so the timeline can identify its owner.
        deduplicated: dict[tuple[str, str, str], dict[str, Any]] = {}
        for event in candidates:
            key = (
                str(event.get("timestamp") or ""),
                str(event.get("severity_text") or ""),
                str(event.get("detail") or ""),
            )
            previous = deduplicated.get(key)
            if previous is None or (event.get("account_id") and not previous.get("account_id")):
                deduplicated[key] = event
        events = list(deduplicated.values())
        events.sort(
            key=lambda event: (
                str(event.get("_sort_timestamp") or ""),
                int(event.get("_source_mtime_ns") or 0),
                int(event.get("event_index") or 0),
            ),
            reverse=True,
        )

        level_counts = {
            name: 0 for name in ("trace", "debug", "info", "warn", "error", "fatal", "unknown")
        }
        for event in events:
            key = str(event.get("severity_text") or "unknown").lower()
            level_counts[key if key in level_counts else "unknown"] += 1
        level_counts["problem"] = sum(1 for event in events if event.get("problem"))
        level_counts["important"] = sum(1 for event in events if event.get("important"))

        filtered = events
        if normalized_level == "important":
            filtered = [event for event in filtered if event.get("important")]
        elif normalized_level == "problem":
            filtered = [event for event in filtered if event.get("problem")]
        elif normalized_level != "all":
            filtered = [
                event
                for event in filtered
                if str(event.get("severity_text") or "").lower() == normalized_level
            ]
        if normalized_query:
            needle = normalized_query.casefold()
            filtered = [
                event
                for event in filtered
                if needle
                in "\n".join(
                    (
                        str(event.get("timestamp") or ""),
                        str(event.get("stage") or ""),
                        str(event.get("summary") or ""),
                        str(event.get("detail") or ""),
                        str(event.get("account_id") or ""),
                        str(event.get("account_email") or ""),
                        str((event.get("source") or {}).get("name") or ""),
                    )
                ).casefold()
            ]

        page = filtered[offset : offset + limit]
        for event in page:
            event.pop("_sort_timestamp", None)
            event.pop("_source_mtime_ns", None)
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "events": page,
            "total_events": len(events),
            "filtered_events": len(filtered),
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(page) < len(filtered),
            "next_offset": offset + len(page),
            "level_counts": level_counts,
            "files_total": files_total,
            "files_scanned": len(files) - skipped_files,
            "files_skipped": skipped_files,
            "tail_bytes": _TIMELINE_TAIL_BYTES,
            "events_per_file": _TIMELINE_EVENTS_PER_FILE,
            "redacted": True,
        }

    def sms_statistics(self) -> dict[str, Any]:
        """Return Hero SMS statistics, recomputing only after a log changes."""

        candidates: list[tuple[Path, str, int]] = []
        cache_parts: list[tuple[str, int, int]] = []
        for path, relative in self._files(self.log_dir, "*.log", recursive=True):
            if not path.name.lower().startswith("codex-"):
                continue
            # Archived raw files remain downloadable but are outside the live
            # statistics scan; this also prevents accidental double counting.
            if any(part.casefold() == "archive" for part in relative.split("/")):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            candidates.append((path, relative, int(stat.st_size)))
            cache_parts.append((str(path), int(stat.st_mtime_ns), int(stat.st_size)))
        cache_key = tuple(sorted(cache_parts))

        with self._sms_statistics_cache_lock:
            if (
                cache_key == self._sms_statistics_cache_key
                and self._sms_statistics_cache_value is not None
            ):
                return deepcopy(self._sms_statistics_cache_value)
            result = self._build_sms_statistics(candidates)
            self._sms_statistics_cache_key = cache_key
            self._sms_statistics_cache_value = deepcopy(result)
            return result

    def _build_sms_statistics(
        self, candidates: Iterable[tuple[Path, str, int]]
    ) -> dict[str, Any]:
        """Build redacted Hero SMS statistics from a stable file snapshot."""

        records: list[dict[str, Any]] = []
        cancel_errors = 0
        logs_scanned = 0
        logs_skipped = 0
        for path, relative, size in candidates:
            try:
                if size > _MAX_VIEW_LOG_BYTES:
                    logs_skipped += 1
                    continue
                content = _read_log_text(path)
            except OSError:
                logs_skipped += 1
                continue
            parsed, parsed_cancel_errors = _parse_sms_log(content, relative=relative)
            records.extend(parsed)
            cancel_errors += parsed_cancel_errors
            logs_scanned += 1

        acquired = len(records)
        sms_sent = sum(1 for item in records if item["sms_sent"])
        codes_received = sum(1 for item in records if item["code_received"])
        verified = sum(1 for item in records if item["verified"])
        failed = sum(1 for item in records if item["status"] in _SMS_FAILED_STATUSES)
        pending = sum(1 for item in records if item["status"] in _SMS_PENDING_STATUSES)
        priced = [item["_price_decimal"] for item in records if item["_price_decimal"] is not None]
        quoted_total = sum(priced, Decimal("0"))
        quoted_average = quoted_total / Decimal(len(priced)) if priced else Decimal("0")

        country_rows: dict[int, dict[str, Any]] = {}
        for record in records:
            country_id = int(record["country_id"])
            country = country_rows.setdefault(
                country_id,
                {
                    "country_id": country_id,
                    "numbers_acquired": 0,
                    "sms_sent": 0,
                    "codes_received": 0,
                    "verified": 0,
                    "failed": 0,
                    "pending": 0,
                    "priced_numbers": 0,
                    "_quoted_total": Decimal("0"),
                },
            )
            country["numbers_acquired"] += 1
            country["sms_sent"] += int(bool(record["sms_sent"]))
            country["codes_received"] += int(bool(record["code_received"]))
            country["verified"] += int(bool(record["verified"]))
            country["failed"] += int(record["status"] in _SMS_FAILED_STATUSES)
            country["pending"] += int(record["status"] in _SMS_PENDING_STATUSES)
            if record["_price_decimal"] is not None:
                country["priced_numbers"] += 1
                country["_quoted_total"] += record["_price_decimal"]

        countries: list[dict[str, Any]] = []
        for country in country_rows.values():
            country_priced = int(country["priced_numbers"])
            country_total = country.pop("_quoted_total")
            country_average = (
                country_total / Decimal(country_priced) if country_priced else Decimal("0")
            )
            country["success_rate"] = _success_rate(
                int(country["verified"]), int(country["numbers_acquired"])
            )
            country["quoted_total"] = _decimal_text(country_total)
            country["quoted_average"] = _decimal_text(country_average)
            countries.append(country)
        countries.sort(
            key=lambda item: (-int(item["numbers_acquired"]), int(item["country_id"]))
        )

        records.sort(key=lambda item: (str(item["acquired_at"]), str(item["id"])), reverse=True)
        public_records = [
            {
                "id": item["id"],
                "acquired_at": item["acquired_at"],
                "completed_at": item["completed_at"],
                "country_id": item["country_id"],
                "phone_number": item["phone_number"],
                "price": item["price"],
                "action": item["action"],
                "attempt": item["attempt"],
                "max_attempts": item["max_attempts"],
                "status": item["status"],
                "status_label": item["status_label"],
                "detail": item["detail"],
                "sms_sent": item["sms_sent"],
                "code_received": item["code_received"],
                "verified": item["verified"],
            }
            for item in records[:200]
        ]

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "logs_scanned": logs_scanned,
            "logs_skipped": logs_skipped,
            "summary": {
                "numbers_acquired": acquired,
                "sms_sent": sms_sent,
                "codes_received": codes_received,
                "verified": verified,
                "failed": failed,
                "pending": pending,
                "success_rate": _success_rate(verified, acquired),
                "priced_numbers": len(priced),
                "quoted_total": _decimal_text(quoted_total),
                "quoted_average": _decimal_text(quoted_average),
                "cancel_errors": cancel_errors,
            },
            "countries": countries,
            "records": public_records,
            "records_total": acquired,
            "records_truncated": acquired > len(public_records),
            "success_rate_note": "成功率 = 验证成功号码数 / 取号数",
            "price_note": "价格为 Hero SMS 取号时报价，不代表最终实际扣费",
            "definitions": {
                "success_rate": "验证成功号码数 / 取号数",
                "failed": "已进入风控拒绝、号码已使用、限流、收码超时或验证码被拒等明确失败终态",
                "pending": "尚未进入验证成功或明确失败终态",
                "price": "Hero SMS 取号时的报价，不代表最终实际扣费",
            },
        }

    def exportable_credential_files(self) -> list[tuple[Path, str]]:
        metadata_by_name = {item["name"]: item for item in self.list_credentials()}
        rows: list[tuple[Path, str]] = []
        for path, relative in self._files(self.credential_dir, "*.json", recursive=False):
            metadata = metadata_by_name.get(path.name)
            if metadata and metadata.get("exportable"):
                rows.append((path, relative))
        return rows

    def log_files(self) -> list[tuple[Path, str]]:
        return list(self._files(self.log_dir, "*.log", recursive=True))


__all__ = ["ArtifactStore"]
