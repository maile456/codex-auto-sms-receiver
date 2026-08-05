from pathlib import Path
from types import SimpleNamespace

from src import upstream_bridge


def _codex_oauth():
    project_root = Path(__file__).resolve().parents[1]
    settings = SimpleNamespace(project_root=project_root, data_dir=project_root / "data")
    return upstream_bridge._ensure_upstream_imports(settings)


def test_flow_page_type_detects_password_and_email_otp_states():
    codex_oauth = _codex_oauth()

    assert codex_oauth._flow_page_type({"page": {"type": "login-password"}}) == "login_password"
    assert codex_oauth._flow_page_type({"continue_url": "/log-in/password"}) == "login_password"
    assert (
        codex_oauth._flow_page_type(
            {"data": {"page": {"payload": {"url": "/email-verification"}}}}
        )
        == "email_otp_verification"
    )


def test_send_passwordless_email_otp_uses_password_page_session():
    codex_oauth = _codex_oauth()
    captured = {}

    class FakeResponse:
        status_code = 200
        text = '{"page":{"type":"email_otp_verification"}}'

        @staticmethod
        def json():
            return {"page": {"type": "email_otp_verification"}}

    class FakeSession:
        @staticmethod
        def get_auth_headers(referer):
            captured["referer"] = referer
            return {"content-type": "application/json"}

        @staticmethod
        def post(url, **kwargs):
            captured.update(url=url, kwargs=kwargs)
            return FakeResponse()

    result = codex_oauth._send_passwordless_email_otp(FakeSession())

    assert result == {"page": {"type": "email_otp_verification"}}
    assert captured["url"] == "https://auth.openai.com/api/accounts/passwordless/send-otp"
    assert captured["referer"] == "https://auth.openai.com/log-in/password"
    assert captured["kwargs"]["data"] == ""
    assert captured["kwargs"]["allow_redirects"] is False


def test_no_password_login_switches_to_email_otp_before_polling(monkeypatch, tmp_path):
    codex_oauth = _codex_oauth()
    events = []

    class FakeSession:
        def __init__(self, proxy=None):
            self.proxy = proxy

    monkeypatch.setattr(codex_oauth, "BrowserSession", FakeSession)
    monkeypatch.setattr(codex_oauth, "_codex_auth_url_source", lambda: "local")
    monkeypatch.setattr(codex_oauth, "_generate_pkce", lambda: ("verifier", "challenge"))
    monkeypatch.setattr(codex_oauth, "_generate_state", lambda: "state-test")
    monkeypatch.setattr(codex_oauth, "network_preflight", lambda session: events.append("preflight"))
    monkeypatch.setattr(codex_oauth, "_bootstrap_authorize", lambda *args, **kwargs: events.append("bootstrap"))
    monkeypatch.setattr(codex_oauth, "human_delay", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        codex_oauth,
        "_submit_email",
        lambda session, email: events.append("submit_email") or {"page": {"type": "login_password"}},
    )
    monkeypatch.setattr(
        codex_oauth,
        "_send_passwordless_email_otp",
        lambda session: events.append("send_passwordless") or {"page": {"type": "email_otp_verification"}},
    )

    def otp_provider(email, after_ts):
        assert events[-1] == "send_passwordless"
        events.append("poll_code_url")
        return "123456"

    monkeypatch.setattr(
        codex_oauth,
        "_submit_email_otp",
        lambda session, code: events.append(f"validate_otp:{code}"),
    )
    monkeypatch.setattr(codex_oauth, "_do_phone_verification", lambda session: events.append("phone"))
    monkeypatch.setattr(
        codex_oauth,
        "_select_workspace_and_get_callback",
        lambda session, state: f"http://localhost:1455/auth/callback?code=ac_test&state={state}",
    )
    monkeypatch.setattr(
        codex_oauth,
        "exchange_codex_token",
        lambda session, code, verifier: {"access_token": "access", "id_token": "id"},
    )
    monkeypatch.setattr(
        codex_oauth,
        "_parse_id_token",
        lambda token: {"email": "owner@example.test", "plan_type": "plus"},
    )
    monkeypatch.setattr(codex_oauth, "build_codex_storage", lambda *args: {"type": "codex"})
    monkeypatch.setattr(
        codex_oauth,
        "save_codex_credential",
        lambda *args: tmp_path / "codex-owner.json",
    )

    result = codex_oauth.run_codex_oauth(
        "owner@example.test",
        otp_provider=otp_provider,
        force=True,
    )

    assert result["ok"] is True
    assert events[:5] == [
        "preflight",
        "bootstrap",
        "submit_email",
        "send_passwordless",
        "poll_code_url",
    ]
    assert "validate_otp:123456" in events
    assert events.count("phone") == 1

    refreshed = codex_oauth.run_codex_oauth(
        "owner@example.test",
        otp_provider=otp_provider,
        force=True,
        skip_phone_verification=True,
    )

    assert refreshed["ok"] is True
    assert events.count("phone") == 1
