from pathlib import Path
from types import SimpleNamespace

from src import upstream_bridge


def test_bridge_calls_only_codex_entrypoint():
    source = (Path(__file__).resolve().parents[1] / "src" / "upstream_bridge.py").read_text(encoding="utf-8")
    assert "run_codex_oauth(" in source
    assert "run_registration(" not in source
    assert "create_account(" not in source


def test_upstream_protocol_contains_password_and_totp_verification_steps():
    source = (
        Path(__file__).resolve().parents[1]
        / "vendor"
        / "turb-gpt-free-register"
        / "core"
        / "codex_oauth.py"
    ).read_text(encoding="utf-8")

    assert '"https://auth.openai.com/api/accounts/password/verify"' in source
    assert '{"password": password}' in source
    assert '"https://auth.openai.com/api/accounts/mfa/verify"' in source
    assert '{"id": normalized_factor_id, "type": "totp", "code": normalized}' in source
    assert "_extract_totp_factor_id(password_payload, _auth_session_payload(session))" in source
    assert 'request_sentinel_token(session, "password_verify")' in source
    assert 'request_sentinel_token(session, "mfa_verify")' in source
    assert "password=password" in source
    assert "totp_provider=totp_provider" in source


def test_submit_mfa_totp_uses_current_factor_schema(monkeypatch):
    project_root = Path(__file__).resolve().parents[1]
    settings = SimpleNamespace(project_root=project_root, data_dir=project_root / "data")
    codex_oauth = upstream_bridge._ensure_upstream_imports(settings)
    captured = {}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": True}

    def fake_post_json(session, url, payload, **kwargs):
        captured.update(url=url, payload=payload, kwargs=kwargs)
        return FakeResponse()

    monkeypatch.setattr(codex_oauth, "request_sentinel_token", lambda *_: object())
    monkeypatch.setattr(
        codex_oauth,
        "build_sentinel_header",
        lambda *_: ("sentinel-test", "sentinel-so-test"),
    )
    monkeypatch.setattr(codex_oauth, "_post_json", fake_post_json)

    result = codex_oauth._submit_mfa_totp(object(), "123456", "factor-test")

    assert result == {"ok": True}
    assert captured["url"] == "https://auth.openai.com/api/accounts/mfa/verify"
    assert captured["payload"] == {
        "id": "factor-test",
        "type": "totp",
        "code": "123456",
    }
    assert captured["kwargs"] == {
        "referer": "https://auth.openai.com/mfa",
        "sentinel_header": "sentinel-test",
        "so_header": "sentinel-so-test",
    }


def test_run_codex_only_passes_custom_email_otp_provider(monkeypatch):
    calls = {}
    cleaned = []
    restored = []

    class FakeCodex:
        sms_provider = object()

        @staticmethod
        def run_codex_oauth(email, otp_provider, proxy, force):
            calls.update(email=email, proxy=proxy, force=force)
            calls["otp"] = otp_provider(email, 123.0)
            return {"ok": True, "status": "success", "file_path": "credential.json"}

    monkeypatch.setattr(upstream_bridge, "_ensure_upstream_imports", lambda settings: FakeCodex())
    monkeypatch.setattr(
        upstream_bridge,
        "install_hero_sms_patch",
        lambda provider: SimpleNamespace(restore=lambda: restored.append(provider)),
    )
    monkeypatch.setattr(
        upstream_bridge,
        "_outlook_otp_provider",
        lambda mailbox: (lambda email, after_ts, **kwargs: "654321", lambda: cleaned.append(True)),
    )
    project_root = Path(__file__).resolve().parents[1]
    settings = SimpleNamespace(project_root=project_root, data_dir=project_root / "data")
    result = upstream_bridge.run_codex_only(
        settings,
        {
            "source": "outlook",
            "email": "owner@example.com",
            "password": "mail-pass",
            "client_id": "client",
            "refresh_token": "refresh",
        },
    )

    assert result["ok"] is True
    assert calls == {"email": "owner@example.com", "proxy": None, "force": True, "otp": "654321"}
    assert cleaned == [True]
    assert restored == [FakeCodex.sms_provider]


def test_credential_reauth_skips_sms_patch_and_phone_verification(monkeypatch):
    calls = {}
    cleaned = []

    class FakeCodex:
        sms_provider = object()

        @staticmethod
        def run_codex_oauth(
            email, otp_provider, proxy, force, skip_phone_verification=False
        ):
            calls.update(
                email=email,
                proxy=proxy,
                force=force,
                skip_phone_verification=skip_phone_verification,
                otp=otp_provider(email, 123.0),
            )
            return {"ok": True, "status": "success", "file_path": "credential.json"}

    monkeypatch.setattr(upstream_bridge, "_ensure_upstream_imports", lambda settings: FakeCodex())
    monkeypatch.setattr(
        upstream_bridge,
        "install_hero_sms_patch",
        lambda provider: (_ for _ in ()).throw(AssertionError("SMS patch must not run")),
    )
    monkeypatch.setattr(
        upstream_bridge,
        "_outlook_otp_provider",
        lambda mailbox: (
            lambda email, after_ts, **kwargs: "654321",
            lambda: cleaned.append(True),
        ),
    )
    project_root = Path(__file__).resolve().parents[1]
    settings = SimpleNamespace(project_root=project_root, data_dir=project_root / "data")

    result = upstream_bridge.run_codex_only(
        settings,
        {
            "source": "outlook",
            "email": "owner@example.com",
            "password": "mail-pass",
            "client_id": "client",
            "refresh_token": "refresh",
        },
        reauth=True,
    )

    assert result["ok"] is True
    assert calls == {
        "email": "owner@example.com",
        "proxy": None,
        "force": True,
        "skip_phone_verification": True,
        "otp": "654321",
    }
    assert cleaned == [True]


def test_generic_api_otp_provider_uses_extended_wait_and_allows_one_resend(monkeypatch):
    project_root = Path(__file__).resolve().parents[1]
    settings = SimpleNamespace(project_root=project_root, data_dir=project_root / "data")
    upstream_bridge._ensure_upstream_imports(settings)
    from core import generic_api_mail_client

    captured = {}

    def fake_fetch(email, after_ts, **kwargs):
        captured.update(email=email, after_ts=after_ts, kwargs=kwargs)
        return "654321"

    monkeypatch.setattr(generic_api_mail_client, "fetch_latest_otp", fake_fetch)
    monkeypatch.delenv("GENERIC_API_OTP_MAX_WAIT", raising=False)
    monkeypatch.delenv("GENERIC_API_OTP_POLL_INTERVAL", raising=False)
    provider, cleanup = upstream_bridge._generic_api_otp_provider(
        {
            "email": "owner@example.com",
            "code_url": "https://mail.example.test/code",
        }
    )
    try:
        assert provider("owner@example.com", 123.0, max_wait=999, poll_interval=2) == "654321"
        assert captured == {
            "email": "owner@example.com",
            "after_ts": 123.0,
            "kwargs": {"max_wait": 90, "poll_interval": 2},
        }
        assert provider.codex_max_email_otp_attempts == 2
    finally:
        cleanup()


def test_generic_api_otp_provider_allows_bounded_polling_overrides(monkeypatch):
    project_root = Path(__file__).resolve().parents[1]
    settings = SimpleNamespace(project_root=project_root, data_dir=project_root / "data")
    upstream_bridge._ensure_upstream_imports(settings)
    from core import generic_api_mail_client

    captured = {}
    monkeypatch.setenv("GENERIC_API_OTP_MAX_WAIT", "120")
    monkeypatch.setenv("GENERIC_API_OTP_POLL_INTERVAL", "2")
    monkeypatch.setattr(
        generic_api_mail_client,
        "fetch_latest_otp",
        lambda email, after_ts, **kwargs: captured.update(kwargs) or "654321",
    )
    provider, cleanup = upstream_bridge._generic_api_otp_provider(
        {"email": "owner@example.com", "code_url": "https://mail.example.test/code"}
    )
    try:
        assert provider("owner@example.com", 123.0) == "654321"
        assert captured == {"max_wait": 120, "poll_interval": 2}
    finally:
        cleanup()


def test_run_codex_only_passes_password_and_local_totp_without_mailbox(monkeypatch):
    calls = {}
    restored = []

    class FakeCodex:
        sms_provider = object()

        @staticmethod
        def run_codex_oauth(
            email,
            otp_provider,
            proxy,
            force,
            password=None,
            totp_provider=None,
        ):
            calls.update(
                email=email,
                otp_provider=otp_provider,
                proxy=proxy,
                force=force,
                password=password,
                totp=totp_provider(),
            )
            return {"ok": True, "status": "success", "file_path": "credential.json"}

    monkeypatch.setattr(upstream_bridge, "_ensure_upstream_imports", lambda settings: FakeCodex())
    monkeypatch.setattr(
        upstream_bridge,
        "install_hero_sms_patch",
        lambda provider: SimpleNamespace(restore=lambda: restored.append(provider)),
    )
    monkeypatch.setattr(upstream_bridge, "current_totp", lambda secret: "654321")
    project_root = Path(__file__).resolve().parents[1]
    settings = SimpleNamespace(project_root=project_root, data_dir=project_root / "data")

    result = upstream_bridge.run_codex_only(
        settings,
        {
            "source": "password_totp",
            "email": "owner@example.com",
            "password": "chatgpt-pass",
            "totp_secret": "JBSWY3DPEHPK3PXP",
        },
    )

    assert result["ok"] is True
    assert calls == {
        "email": "owner@example.com",
        "otp_provider": None,
        "proxy": None,
        "force": True,
        "password": "chatgpt-pass",
        "totp": "654321",
    }
    assert restored == [FakeCodex.sms_provider]
