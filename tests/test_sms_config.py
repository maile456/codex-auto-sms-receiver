import os
from pathlib import Path

import pytest
from dotenv import dotenv_values

from src.sms_config import SmsConfigStore


SMS_ENV_KEYS = (
    "SMS_PROVIDER",
    "SMS_PROVIDER_ORDER",
    "SMS_API_KEY",
    "SMS_COUNTRY",
    "SMS_SERVICE",
    "SMS_MAX_PRICE",
    "SMS_MAX_RETRIES",
    "SMS_CODE_WAIT",
    "GENERIC_API_OTP_MAX_WAIT",
    "GENERIC_API_OTP_POLL_INTERVAL",
    "GENERIC_API_OTP_ATTEMPTS",
    "HERO_SMS_API_KEY",
    "HERO_SMS_COUNTRIES",
    "HERO_SMS_MIN_PRICE",
    "HERO_SMS_MAX_PRICE",
    "HERO_SMS_PREFERRED_PRICE",
    "HERO_SMS_ACQUIRE_PRIORITY",
    "HERO_SMS_REUSE_ENABLED",
    "L_API_BASE",
    "L_ADMIN_AUTH_CODE",
    "L_PHONE_PREFIX",
    "H_API_BASE",
    "H_ADMIN_AUTH_CODE",
    "H_PHONE_PREFIX",
    "H_PHONE_ACQUIRE_MODE",
)


@pytest.fixture(autouse=True)
def clean_sms_environment(monkeypatch):
    for key in SMS_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _payload(**overrides):
    value = {
        "provider": "hero",
        "countries": ["33"],
        "service": "dr",
        "min_price": "",
        "max_price": "",
        "preferred_price": "",
        "acquire_priority": "country",
        "max_retries": 8,
        "code_wait": 150,
        "credential": "hero-secret",
        "clear_credential": False,
    }
    value.update(overrides)
    return value


def test_save_is_atomic_masks_secret_and_forces_hero(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text("WEBUI_PORT=5015\n# keep me\nSMS_PROVIDER=grizzly\n", encoding="utf-8")
    store = SmsConfigStore(env_path)

    snapshot = store.save(_payload())

    values = dotenv_values(env_path)
    assert values["WEBUI_PORT"] == "5015"
    assert values["SMS_PROVIDER"] == "hero"
    assert values["SMS_SERVICE"] == "dr"
    assert values["HERO_SMS_API_KEY"] == "hero-secret"
    assert snapshot["provider"] == "hero"
    assert snapshot["credential_configured"] is True
    assert snapshot["credentials_configured"] == {"hero": True}
    assert "hero-secret" not in repr(snapshot)
    assert list(tmp_path.glob(".*.tmp")) == []


def test_email_otp_timing_and_attempts_are_persisted(tmp_path: Path):
    env_path = tmp_path / ".env"
    snapshot = SmsConfigStore(env_path).save(
        _payload(email_otp_wait=180, email_otp_poll_interval=5, email_otp_attempts=3)
    )
    values = dotenv_values(env_path)
    assert snapshot["email_otp_wait"] == 180
    assert snapshot["email_otp_poll_interval"] == 5
    assert snapshot["email_otp_attempts"] == 3
    assert values["GENERIC_API_OTP_MAX_WAIT"] == "180"
    assert values["GENERIC_API_OTP_POLL_INTERVAL"] == "5"
    assert values["GENERIC_API_OTP_ATTEMPTS"] == "3"


def test_operator_defaults_are_applied_to_empty_hero_settings(tmp_path: Path):
    store = SmsConfigStore(tmp_path / ".env")

    initial = store.snapshot()
    saved = store.save(_payload(max_price="", max_retries=10, code_wait=30))

    assert initial["max_price"] == "0.11"
    assert initial["max_retries"] == 10
    assert initial["code_wait"] == 30
    assert saved["max_price"] == "0.11"
    assert saved["max_retries"] == 10
    assert saved["code_wait"] == 30
    values = dotenv_values(tmp_path / ".env")
    assert values["HERO_SMS_MAX_PRICE"] == "0.11"
    assert values["SMS_MAX_RETRIES"] == "10"
    assert values["SMS_CODE_WAIT"] == "30"


def test_duplicate_keys_are_deduplicated_and_legacy_provider_keys_removed(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "SMS_PROVIDER=grizzly\nSMS_PROVIDER=h\n"
        "SMS_PROVIDER_ORDER=hero,grizzly,l\n"
        "SMS_API_KEY=old\nL_ADMIN_AUTH_CODE=old\nH_ADMIN_AUTH_CODE=old\n",
        encoding="utf-8",
    )
    SmsConfigStore(env_path).save(_payload(credential="new-secret"))

    text = env_path.read_text(encoding="utf-8")
    keys = {line.split("=", 1)[0] for line in text.splitlines() if "=" in line}
    assert text.count("SMS_PROVIDER=") == 1
    assert "SMS_PROVIDER_ORDER" not in keys
    assert "SMS_API_KEY" not in keys
    assert "L_ADMIN_AUTH_CODE" not in keys
    assert "H_ADMIN_AUTH_CODE" not in keys
    assert dotenv_values(env_path)["HERO_SMS_API_KEY"] == "new-secret"


def test_blank_credential_preserves_existing_and_clear_is_explicit(tmp_path: Path):
    store = SmsConfigStore(tmp_path / ".env")
    store.save(_payload(credential="first-secret"))
    store.save(_payload(credential=""))
    assert os.environ["HERO_SMS_API_KEY"] == "first-secret"

    snapshot = store.save(_payload(credential="ignored", clear_credential=True))
    assert os.environ["HERO_SMS_API_KEY"] == ""
    assert snapshot["credential_configured"] is False


def test_saved_hero_credential_can_be_revealed_explicitly(tmp_path: Path):
    store = SmsConfigStore(tmp_path / ".env")
    store.save(_payload(credential="visible-on-demand"))
    assert store.reveal_credential() == "visible-on-demand"
    assert store.reveal_credential("hero") == "visible-on-demand"
    with pytest.raises(ValueError, match="仅支持 Hero SMS"):
        store.reveal_credential("grizzly")


def test_openai_service_alias_is_normalized_to_dr(tmp_path: Path):
    store = SmsConfigStore(tmp_path / ".env")
    snapshot = store.save(_payload(service="openai"))
    assert snapshot["service"] == "dr"
    assert dotenv_values(tmp_path / ".env")["SMS_SERVICE"] == "dr"


def test_hero_saves_country_and_price_strategy(tmp_path: Path):
    store = SmsConfigStore(tmp_path / ".env")
    snapshot = store.save(
        _payload(
            countries=["33", "187", "52", "33"],
            service="openai",
            min_price="0.0500",
            max_price="0.12",
            preferred_price="0.075",
            acquire_priority="price",
            credential="hero-key",
        )
    )

    values = dotenv_values(tmp_path / ".env")
    assert "provider_order" not in snapshot
    assert snapshot["countries"] == ["33", "187", "52"]
    assert snapshot["min_price"] == "0.05"
    assert snapshot["max_price"] == "0.12"
    assert snapshot["preferred_price"] == "0.075"
    assert snapshot["acquire_priority"] == "price"
    assert values["HERO_SMS_COUNTRIES"] == "33,187,52"
    assert values["HERO_SMS_MAX_PRICE"] == "0.12"
    assert values["SMS_MAX_PRICE"] == "0.12"


@pytest.mark.parametrize(
    "overrides",
    [
        {"countries": [str(item) for item in range(11)]},
        {"min_price": "0.2", "max_price": "0.1"},
        {"min_price": "0.05", "max_price": "0.1", "preferred_price": "0.2"},
        {"acquire_priority": "random"},
    ],
)
def test_invalid_hero_strategy_is_rejected(tmp_path: Path, overrides: dict):
    with pytest.raises(ValueError):
        SmsConfigStore(tmp_path / ".env").save(_payload(**overrides))


@pytest.mark.parametrize(
    "overrides",
    [
        {"provider": "grizzly"},
        {"provider_order": ["hero", "l"]},
        {"countries": ["us"]},
        {"service": "telegram"},
        {"max_retries": "0"},
        {"credential": "secret\nINJECTED=yes"},
    ],
)
def test_non_hero_or_invalid_values_are_rejected(tmp_path: Path, overrides: dict):
    with pytest.raises(ValueError):
        SmsConfigStore(tmp_path / ".env").save(_payload(**overrides))


def test_save_clears_stale_provider_environment(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("SMS_PROVIDER_ORDER", "hero,grizzly,l,h")
    monkeypatch.setenv("SMS_API_KEY", "old-key")
    monkeypatch.setenv("L_ADMIN_AUTH_CODE", "old-l")
    monkeypatch.setenv("H_ADMIN_AUTH_CODE", "old-h")

    SmsConfigStore(tmp_path / ".env").save(_payload())

    assert "SMS_PROVIDER_ORDER" not in os.environ
    assert "SMS_API_KEY" not in os.environ
    assert "L_ADMIN_AUTH_CODE" not in os.environ
    assert "H_ADMIN_AUTH_CODE" not in os.environ
