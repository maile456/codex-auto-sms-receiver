from pathlib import Path

from datetime import datetime, timezone

from src.codex_quota import CodexQuotaStore, _remaining, query_codex_quota


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
