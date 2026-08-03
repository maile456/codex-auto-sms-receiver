import base64
import json
from pathlib import Path

from datetime import datetime, timezone

from src.codex_quota import (
    ACCOUNT_CHECK_URL,
    SUBSCRIPTIONS_URL,
    TOKEN_URL,
    USAGE_URL,
    CodexQuotaStore,
    _remaining,
    query_codex_quota,
    query_codex_subscription,
    refresh_codex_credential_metadata,
)


class _Response:
    status_code = 200

    @staticmethod
    def json():
        return {
            "plan_type": "plus",
            "rate_limit": {
                "allowed": True,
                "limit_reached": False,
                "primary_window": {
                    "used_percent": 25,
                    "limit_window_seconds": 18000,
                    "reset_at": 123,
                },
                "secondary_window": {
                    "used_percent": 60,
                    "limit_window_seconds": 604800,
                    "reset_at": 456,
                },
            },
        }


class _JsonResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


def _jwt(payload: dict) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"fixture.{encoded}.signature"


def _account_check(plan="chatgptplusplan", expires_at="2099-09-02T05:00:00+00:00"):
    return {
        "accounts": {
            "account-key": {
                "account": {"account_id": "acct-1"},
                "entitlement": {
                    "subscription_plan": plan,
                    "expires_at": expires_at,
                },
            }
        },
        "account_ordering": ["account-key"],
    }


def test_query_codex_quota_uses_account_header_and_returns_remaining(monkeypatch):
    observed = {}

    def fake_get(url, *, headers, timeout):
        observed.update(url=url, headers=headers, timeout=timeout)
        return _Response()

    monkeypatch.setattr("src.codex_quota.requests.get", fake_get)
    result = query_codex_quota(
        {"access_token": "fixture-token", "account_id": "acct-1", "type": "plus"}
    )

    assert observed["headers"]["Authorization"] == "Bearer fixture-token"
    assert observed["headers"]["ChatGPT-Account-Id"] == "acct-1"
    assert result["primary"]["remaining_percent"] == 75
    assert result["primary"]["window_minutes"] == 300
    assert result["secondary"]["remaining_percent"] == 40


def test_quota_store_persists_no_tokens(tmp_path: Path):
    store = CodexQuotaStore(tmp_path)
    store.put(
        "account-1",
        {"status": "ok", "checked_at": "now", "plan_type": "plus", "access_token": "secret"},
    )
    assert store.list()["account-1"]["plan_type"] == "plus"
    assert "secret" not in (tmp_path / "codex-quota.json").read_text(encoding="utf-8")


def test_quota_window_derives_reset_at_from_reset_after_seconds():
    before = int(datetime.now(timezone.utc).timestamp())
    result = _remaining(
        {
            "used_percent": 0,
            "limit_window_seconds": 604800,
            "reset_after_seconds": 90,
        }
    )
    after = int(datetime.now(timezone.utc).timestamp())

    assert result["window_minutes"] == 10080
    assert before + 90 <= result["reset_at"] <= after + 90


def test_subscription_uses_account_check_plan_and_expiry(monkeypatch):
    observed = {}

    def fake_get(url, *, params, headers, timeout):
        observed.update(url=url, params=params, headers=headers, timeout=timeout)
        return _JsonResponse(_account_check())

    monkeypatch.setattr("src.codex_quota.requests.get", fake_get)
    result = query_codex_subscription(
        {"access_token": "fixture-token", "account_id": "acct-1"}
    )

    assert observed["url"] == ACCOUNT_CHECK_URL
    assert "timezone_offset_min" in observed["params"]
    assert result["plan_type"] == "plus"
    assert result["subscription_active_until"] == "2099-09-02T05:00:00+00:00"
    assert result["source"] == "accounts_check"


def test_subscription_fills_missing_expiry_from_subscriptions(monkeypatch):
    calls = []

    def fake_get(url, *, params, headers, timeout):
        calls.append((url, params))
        if url == ACCOUNT_CHECK_URL:
            return _JsonResponse(_account_check(expires_at=""))
        assert url == SUBSCRIPTIONS_URL
        return _JsonResponse(
            {
                "data": {
                    "subscription_plan": "plus",
                    "active_until": "2099-10-03T06:00:00+00:00",
                }
            }
        )

    monkeypatch.setattr("src.codex_quota.requests.get", fake_get)
    result = query_codex_subscription(
        {"access_token": "fixture-token", "account_id": "acct-1"}
    )

    assert calls[1] == (SUBSCRIPTIONS_URL, {"account_id": "acct-1"})
    assert result["subscription_active_until"] == "2099-10-03T06:00:00+00:00"
    assert result["source"] == "subscriptions"


def test_free_subscription_404_keeps_account_check_result(monkeypatch):
    calls = []

    def fake_get(url, *, params, headers, timeout):
        calls.append(url)
        if url == ACCOUNT_CHECK_URL:
            return _JsonResponse(_account_check(plan="chatgptfreeplan", expires_at=""))
        assert url == SUBSCRIPTIONS_URL
        return _JsonResponse({}, status_code=404)

    monkeypatch.setattr("src.codex_quota.requests.get", fake_get)
    result = query_codex_subscription(
        {"access_token": "fixture-token", "account_id": "acct-1"}
    )

    assert calls == [ACCOUNT_CHECK_URL, SUBSCRIPTIONS_URL]
    assert result["status"] == "ok"
    assert result["plan_type"] == "free"
    assert result["subscription_active_until"] == ""
    assert result["source"] == "accounts_check"


def test_metadata_refresh_updates_free_to_plus_atomically(monkeypatch, tmp_path: Path):
    path = tmp_path / "credential.json"
    path.write_text(
        json.dumps(
            {
                "access_token": "fixture-token",
                "refresh_token": "fixture-refresh",
                "account_id": "acct-1",
                "plan_type": "free",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "src.codex_quota.requests.get",
        lambda url, **kwargs: _JsonResponse(_account_check()),
    )

    result = refresh_codex_credential_metadata(path, include_quota=False)
    saved = json.loads(path.read_text(encoding="utf-8"))

    assert result["subscription"]["plan_type"] == "plus"
    assert saved["plan_type"] == "plus"
    assert saved["subscription_source"] == "accounts_check"
    assert not list(tmp_path.glob(".credential.json.*.tmp"))


def test_metadata_refresh_keeps_unexpired_plus_when_remote_temporarily_reports_free(
    monkeypatch, tmp_path: Path
):
    path = tmp_path / "credential.json"
    path.write_text(
        json.dumps(
            {
                "access_token": "fixture-token",
                "refresh_token": "fixture-refresh",
                "account_id": "acct-1",
                "plan_type": "plus",
                "subscription_active_until": "2099-09-02T05:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "src.codex_quota.query_codex_subscription",
        lambda credential, timeout=20: {
            "status": "ok",
            "checked_at": "2026-08-04T00:00:00+00:00",
            "plan_type": "free",
            "subscription_active_until": "",
            "account_id": "acct-1",
            "source": "accounts_check",
        },
    )

    result = refresh_codex_credential_metadata(path, include_quota=False)
    saved = json.loads(path.read_text(encoding="utf-8"))

    assert result["subscription"]["plan_type"] == "plus"
    assert result["subscription"]["preserved_active_plan"] is True
    assert saved["plan_type"] == "plus"
    assert saved["subscription_active_until"] == "2099-09-02T05:00:00+00:00"


def test_expired_access_token_is_refreshed_before_subscription_query(monkeypatch, tmp_path: Path):
    path = tmp_path / "credential.json"
    path.write_text(
        json.dumps(
            {
                "access_token": _jwt({"exp": 1}),
                "refresh_token": "old-refresh",
                "account_id": "acct-1",
            }
        ),
        encoding="utf-8",
    )
    posts = []

    def fake_post(url, **kwargs):
        posts.append((url, kwargs["data"]["refresh_token"]))
        return _JsonResponse(
            {
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 3600,
            }
        )

    def fake_get(url, **kwargs):
        assert kwargs["headers"]["Authorization"] == "Bearer new-access"
        return _JsonResponse(_account_check())

    monkeypatch.setattr("src.codex_quota.requests.post", fake_post)
    monkeypatch.setattr("src.codex_quota.requests.get", fake_get)
    result = refresh_codex_credential_metadata(path, include_quota=False)

    assert posts == [(TOKEN_URL, "old-refresh")]
    assert result["token_refreshed"] is True
    assert json.loads(path.read_text(encoding="utf-8"))["refresh_token"] == "new-refresh"


def test_subscription_401_refreshes_token_and_retries_once(monkeypatch, tmp_path: Path):
    path = tmp_path / "credential.json"
    path.write_text(
        json.dumps(
            {
                "access_token": "old-access",
                "refresh_token": "fixture-refresh",
                "account_id": "acct-1",
            }
        ),
        encoding="utf-8",
    )
    gets = []

    def fake_get(url, **kwargs):
        token = kwargs["headers"]["Authorization"]
        gets.append(token)
        if token == "Bearer old-access":
            return _JsonResponse({}, status_code=401)
        return _JsonResponse(_account_check())

    monkeypatch.setattr("src.codex_quota.requests.get", fake_get)
    monkeypatch.setattr(
        "src.codex_quota.requests.post",
        lambda url, **kwargs: _JsonResponse({"access_token": "new-access"}),
    )

    result = refresh_codex_credential_metadata(path, include_quota=False)

    assert gets == ["Bearer old-access", "Bearer new-access"]
    assert result["subscription"]["plan_type"] == "plus"
    assert json.loads(path.read_text(encoding="utf-8"))["access_token"] == "new-access"


def test_subscription_is_saved_when_quota_refresh_fails(monkeypatch, tmp_path: Path):
    path = tmp_path / "credential.json"
    path.write_text(
        json.dumps(
            {
                "access_token": "fixture-token",
                "refresh_token": "fixture-refresh",
                "account_id": "acct-1",
                "plan_type": "free",
            }
        ),
        encoding="utf-8",
    )

    def fake_get(url, **kwargs):
        if url == ACCOUNT_CHECK_URL:
            return _JsonResponse(_account_check())
        assert url == USAGE_URL
        return _JsonResponse({}, status_code=500)

    monkeypatch.setattr("src.codex_quota.requests.get", fake_get)
    result = refresh_codex_credential_metadata(path, include_quota=True)

    assert result["subscription"]["plan_type"] == "plus"
    assert result["quota"] is None
    assert "HTTP 500" in str(result["quota_error"])
    assert json.loads(path.read_text(encoding="utf-8"))["plan_type"] == "plus"


def test_quota_errors_preserve_confirmed_subscription(tmp_path: Path):
    store = CodexQuotaStore(tmp_path)
    store.put_subscription(
        "account-1",
        {
            "status": "ok",
            "checked_at": "now",
            "plan_type": "plus",
            "subscription_active_until": "2099-01-01T00:00:00+00:00",
            "source": "accounts_check",
        },
    )
    store.record_error("account-1", RuntimeError("network secret detail"))

    saved = store.list()["account-1"]
    assert saved["subscription_plan_type"] == "plus"
    assert saved["subscription_status"] == "ok"
    assert saved["status"] == "error"
