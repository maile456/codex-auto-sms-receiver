from __future__ import annotations

import os
import re
import secrets
import threading
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Mapping

from dotenv import dotenv_values


_ENV_LINE = re.compile(r"^(?P<prefix>\s*(?:export\s+)?)(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=.*$")
_PRICE_VALUE = re.compile(r"^(?:0|[1-9]\d*)(?:\.\d{1,4})?$")

# These settings belonged to providers that are intentionally unsupported by
# this login-only project. Saving the Hero configuration also removes stale
# copies from the local .env so they cannot silently become active again.
_REMOVED_PROVIDER_KEYS = {
    "SMS_PROVIDER_ORDER",
    "SMS_API_KEY",
    "L_API_BASE",
    "L_ADMIN_AUTH_CODE",
    "L_PHONE_PREFIX",
    "H_API_BASE",
    "H_ADMIN_AUTH_CODE",
    "H_PHONE_PREFIX",
    "H_PHONE_ACQUIRE_MODE",
}


def _single_line(value: object, *, field: str, max_length: int = 4096) -> str:
    text = "" if value is None else str(value).strip()
    if len(text) > max_length:
        raise ValueError(f"{field}过长")
    if any(ord(char) < 32 for char in text):
        raise ValueError(f"{field}不能包含控制字符")
    return text


def _integer(value: object, *, field: str, minimum: int, maximum: int) -> str:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field}必须是整数") from exc
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{field}必须在 {minimum} - {maximum} 之间")
    return str(parsed)


def _env_value(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _stored_integer(value: str, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _boolean(value: object, *, field: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError(f"{field}必须是布尔值")


def _stored_boolean(value: str, default: bool = False) -> bool:
    try:
        return _boolean(value, field="布尔配置")
    except ValueError:
        return default


def _sequence(value: object) -> list[object]:
    if isinstance(value, (list, tuple)):
        return list(value)
    if value is None:
        return []
    return [item for item in re.split(r"[,\s;，；]+", str(value).strip()) if item]


def normalize_hero_countries(value: object, *, fallback: Iterable[object] = ()) -> list[str]:
    source = _sequence(value) or list(fallback)
    result: list[str] = []
    for item in source:
        if isinstance(item, Mapping):
            item = item.get("id", item.get("country", item.get("value", "")))
        text = _single_line(item, field="Hero SMS 国家 ID", max_length=12)
        if not text or not text.isdigit():
            raise ValueError("Hero SMS 国家 ID 必须是数字")
        country_id = int(text)
        if country_id < 0 or country_id > 9999:
            raise ValueError("Hero SMS 国家 ID 必须在 0 - 9999 之间")
        normalized = str(country_id)
        if normalized not in result:
            result.append(normalized)
        if len(result) > 10:
            raise ValueError("Hero SMS 国家最多选择 10 个")
    return result


def normalize_price(value: object, *, field: str) -> str:
    text = _single_line(value, field=field, max_length=32)
    if not text:
        return ""
    if not _PRICE_VALUE.fullmatch(text):
        raise ValueError(f"{field}必须是正数，且最多 4 位小数")
    try:
        parsed = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"{field}格式不正确") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"{field}必须是正数，且最多 4 位小数")
    return format(parsed.quantize(Decimal("0.0001")).normalize(), "f")


def _validate_price_range(min_price: str, max_price: str, preferred_price: str) -> None:
    minimum = Decimal(min_price) if min_price else None
    maximum = Decimal(max_price) if max_price else None
    preferred = Decimal(preferred_price) if preferred_price else None
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError("最低购买价不能高于价格上限")
    if preferred is not None and minimum is not None and preferred < minimum:
        raise ValueError("指定价格档位不能低于最低购买价")
    if preferred is not None and maximum is not None and preferred > maximum:
        raise ValueError("指定价格档位不能高于价格上限")


class SmsConfigStore:
    """Persist the Hero SMS configuration without exposing its API key."""

    _KEYS = {
        # Upstream compatibility fields. They are always written as Hero/dr.
        "SMS_PROVIDER",
        "SMS_COUNTRY",
        "SMS_SERVICE",
        "SMS_MAX_PRICE",
        "SMS_MAX_RETRIES",
        "SMS_CODE_WAIT",
        "GENERIC_API_OTP_MAX_WAIT",
        "GENERIC_API_OTP_POLL_INTERVAL",
        "GENERIC_API_OTP_ATTEMPTS",
        # Hero-owned settings.
        "HERO_SMS_API_KEY",
        "HERO_SMS_COUNTRIES",
        "HERO_SMS_MIN_PRICE",
        "HERO_SMS_MAX_PRICE",
        "HERO_SMS_PREFERRED_PRICE",
        "HERO_SMS_ACQUIRE_PRIORITY",
        "HERO_SMS_REUSE_ENABLED",
    }

    def __init__(self, env_path: Path):
        self.env_path = Path(env_path)
        self._lock = threading.RLock()

    def _values(self) -> dict[str, str]:
        persisted = dotenv_values(self.env_path) if self.env_path.is_file() else {}
        return {
            key: str(os.getenv(key, persisted.get(key) or "") or "")
            for key in self._KEYS
        }

    def snapshot(self) -> dict:
        with self._lock:
            values = self._values()

        legacy_country = values["SMS_COUNTRY"].strip()
        fallback = (legacy_country,) if legacy_country.isdigit() else ()
        try:
            countries = normalize_hero_countries(
                values["HERO_SMS_COUNTRIES"],
                fallback=fallback,
            )
        except ValueError:
            countries = list(fallback)

        def stored_price(name: str, *, legacy: str = "", label: str) -> str:
            try:
                return normalize_price(values[name] or (values[legacy] if legacy else ""), field=label)
            except ValueError:
                return ""

        min_price = stored_price("HERO_SMS_MIN_PRICE", label="最低购买价")
        max_price = (
            stored_price("HERO_SMS_MAX_PRICE", legacy="SMS_MAX_PRICE", label="价格上限")
            or "0.11"
        )
        preferred_price = stored_price("HERO_SMS_PREFERRED_PRICE", label="指定价格档位")
        acquire_priority = values["HERO_SMS_ACQUIRE_PRIORITY"].strip().lower()
        if acquire_priority not in {"country", "price", "price_high"}:
            acquire_priority = "country"
        configured = bool(values["HERO_SMS_API_KEY"].strip())
        return {
            "provider": "hero",
            "country": countries[0] if countries else "",
            "countries": countries,
            "service": "dr",
            "min_price": min_price,
            "max_price": max_price,
            "preferred_price": preferred_price,
            "acquire_priority": acquire_priority,
            "reuse_enabled": _stored_boolean(values["HERO_SMS_REUSE_ENABLED"], False),
            "max_retries": _stored_integer(values["SMS_MAX_RETRIES"], 10),
            "code_wait": _stored_integer(values["SMS_CODE_WAIT"], 30),
            "email_otp_wait": _stored_integer(values["GENERIC_API_OTP_MAX_WAIT"], 90),
            "email_otp_poll_interval": _stored_integer(
                values["GENERIC_API_OTP_POLL_INTERVAL"], 3
            ),
            "email_otp_attempts": _stored_integer(values["GENERIC_API_OTP_ATTEMPTS"], 1),
            "credential_configured": configured,
            # Kept as a one-item object for older local UI/API consumers.
            "credentials_configured": {"hero": configured},
        }

    def reveal_credential(self, provider: str = "hero") -> str:
        requested = _single_line(provider or "hero", field="短信平台", max_length=20).lower()
        if requested != "hero":
            raise ValueError("本项目仅支持 Hero SMS")
        with self._lock:
            return self._values()["HERO_SMS_API_KEY"].strip()

    def save(self, payload: Mapping) -> dict:
        requested_provider = _single_line(
            payload.get("provider", "hero"),
            field="短信平台",
            max_length=20,
        ).lower()
        if requested_provider not in {"", "hero"}:
            raise ValueError("本项目仅支持 Hero SMS")
        if "provider_order" in payload:
            providers = {str(item or "").strip().lower() for item in _sequence(payload.get("provider_order"))}
            if providers - {"", "hero"}:
                raise ValueError("本项目不支持服务商回退，仅支持 Hero SMS")

        with self._lock:
            current = self._values()

        current_country = str(current["SMS_COUNTRY"] or "").strip()
        current_fallback = (current_country,) if current_country.isdigit() else ()
        try:
            current_countries = normalize_hero_countries(
                current["HERO_SMS_COUNTRIES"],
                fallback=current_fallback,
            )
        except ValueError:
            current_countries = list(current_fallback)

        if "countries" in payload or "hero_countries" in payload:
            countries = normalize_hero_countries(payload.get("countries", payload.get("hero_countries")))
        elif "country" in payload:
            first = _single_line(payload.get("country"), field="Hero SMS 国家 ID", max_length=12)
            countries = normalize_hero_countries([first, *current_countries[1:]])
        else:
            countries = current_countries
        if not countries:
            raise ValueError("Hero SMS 至少需要选择 1 个国家")

        service = _single_line(payload.get("service", "dr"), field="服务代码", max_length=64).lower()
        if service not in {"", "dr", "openai", "chatgpt"}:
            raise ValueError("Hero SMS 服务已固定为 OpenAI（dr）")

        min_price = normalize_price(
            payload.get("min_price", current["HERO_SMS_MIN_PRICE"]),
            field="最低购买价",
        )
        max_price = (
            normalize_price(
                payload.get(
                    "max_price",
                    current["HERO_SMS_MAX_PRICE"] or current["SMS_MAX_PRICE"],
                ),
                field="价格上限",
            )
            or "0.11"
        )
        preferred_price = normalize_price(
            payload.get("preferred_price", current["HERO_SMS_PREFERRED_PRICE"]),
            field="指定价格档位",
        )
        _validate_price_range(min_price, max_price, preferred_price)
        acquire_priority = _single_line(
            payload.get("acquire_priority", current["HERO_SMS_ACQUIRE_PRIORITY"] or "country"),
            field="拿号优先级",
            max_length=20,
        ).lower()
        if acquire_priority not in {"country", "price", "price_high"}:
            raise ValueError("拿号优先级仅支持 country / price / price_high")
        reuse_enabled = _boolean(
            payload.get("reuse_enabled", _stored_boolean(current["HERO_SMS_REUSE_ENABLED"], False)),
            field="号码复用",
        )

        updates = {
            "SMS_PROVIDER": "hero",
            "SMS_COUNTRY": countries[0],
            "SMS_SERVICE": "dr",
            "SMS_MAX_PRICE": max_price,
            "SMS_MAX_RETRIES": _integer(
                payload.get("max_retries", current["SMS_MAX_RETRIES"] or 10),
                field="换号重试次数",
                minimum=1,
                maximum=50,
            ),
            "SMS_CODE_WAIT": _integer(
                payload.get("code_wait", current["SMS_CODE_WAIT"] or 30),
                field="短信等待秒数",
                minimum=30,
                maximum=600,
            ),
            "GENERIC_API_OTP_MAX_WAIT": _integer(
                payload.get(
                    "email_otp_wait",
                    current["GENERIC_API_OTP_MAX_WAIT"] or 90,
                ),
                field="邮箱验证码单轮等待秒数",
                minimum=30,
                maximum=300,
            ),
            "GENERIC_API_OTP_POLL_INTERVAL": _integer(
                payload.get(
                    "email_otp_poll_interval",
                    current["GENERIC_API_OTP_POLL_INTERVAL"] or 3,
                ),
                field="邮箱验证码轮询间隔",
                minimum=1,
                maximum=30,
            ),
            "GENERIC_API_OTP_ATTEMPTS": _integer(
                payload.get(
                    "email_otp_attempts",
                    current["GENERIC_API_OTP_ATTEMPTS"] or 1,
                ),
                field="邮箱验证码重试轮数",
                minimum=1,
                maximum=5,
            ),
            "HERO_SMS_COUNTRIES": ",".join(countries),
            "HERO_SMS_MIN_PRICE": min_price,
            "HERO_SMS_MAX_PRICE": max_price,
            "HERO_SMS_PREFERRED_PRICE": preferred_price,
            "HERO_SMS_ACQUIRE_PRIORITY": acquire_priority,
            "HERO_SMS_REUSE_ENABLED": "true" if reuse_enabled else "false",
        }
        credential = _single_line(payload.get("credential"), field="Hero SMS API Key")
        if payload.get("clear_credential") is True:
            updates["HERO_SMS_API_KEY"] = ""
        elif credential:
            updates["HERO_SMS_API_KEY"] = credential

        with self._lock:
            self._write(updates)
            for key in _REMOVED_PROVIDER_KEYS:
                os.environ.pop(key, None)
            for key, value in updates.items():
                os.environ[key] = value
        return self.snapshot()

    def _write(self, updates: Mapping[str, str]) -> None:
        self.env_path.parent.mkdir(parents=True, exist_ok=True)
        original = self.env_path.read_text(encoding="utf-8") if self.env_path.is_file() else ""
        lines = original.splitlines()
        pending = dict(updates)
        update_keys = set(updates)
        written: set[str] = set()
        output: list[str] = []
        for line in lines:
            match = _ENV_LINE.match(line)
            key = match.group("key") if match else None
            if key in _REMOVED_PROVIDER_KEYS:
                continue
            if key in update_keys:
                if key in written:
                    continue
                prefix = match.group("prefix")
                output.append(f"{prefix}{key}={_env_value(updates[key])}")
                written.add(key)
                pending.pop(key, None)
            else:
                output.append(line)
        if pending:
            if output and output[-1].strip():
                output.append("")
            output.append("# ---- Hero SMS settings ----")
            output.extend(f"{key}={_env_value(value)}" for key, value in pending.items())

        temporary = self.env_path.parent / f".{self.env_path.name}.{secrets.token_hex(8)}.tmp"
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write("\n".join(output).rstrip("\n") + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.env_path)
            try:
                os.chmod(self.env_path, 0o600)
            except OSError:
                pass
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


__all__ = ["SmsConfigStore", "normalize_hero_countries", "normalize_price"]
