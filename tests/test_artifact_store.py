from __future__ import annotations

import base64
import json
import hashlib
from pathlib import Path

import pytest

from src.artifact_store import ArtifactStore


def _unsigned_jwt(claims: dict) -> str:
    encoded = base64.urlsafe_b64encode(
        json.dumps(claims, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"header.{encoded}.signature"


def test_artifact_index_masks_tokens_and_classifies_files(tmp_path: Path):
    data_dir = tmp_path / "data"
    credential_dir = data_dir / "codex_accounts"
    log_dir = tmp_path / "logs"
    credential_dir.mkdir(parents=True)
    log_dir.mkdir()
    secret = "refresh-secret-that-must-not-be-listed"
    (credential_dir / "codex-owner@example.com.json").write_text(
        json.dumps(
            {
                "type": "codex",
                "email": "owner@example.com",
                "access_token": "access-secret",
                "refresh_token": secret,
                "id_token": _unsigned_jwt(
                    {
                        "https://api.openai.com/auth": {
                            "chatgpt_account_id": "chatgpt-account-1234567890",
                            "chatgpt_plan_type": "plus",
                            "chatgpt_subscription_active_until": "2026-09-02T05:00:00+00:00",
                        }
                    }
                ),
                "expired": "2026-08-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    (credential_dir / "codex-receipt.json").write_text(
        json.dumps({"type": "codex_cpa_callback", "email": "receipt@example.com"}),
        encoding="utf-8",
    )
    (credential_dir / "broken.json").write_text("{broken", encoding="utf-8")
    (log_dir / "codex-run.log").write_text("hello", encoding="utf-8")

    store = ArtifactStore(data_dir, log_dir)
    overview = store.overview()
    serialized = repr(overview)

    assert secret not in serialized
    assert "access-secret" not in serialized
    assert overview["counts"] == {
        "credentials": 1,
        "receipts": 1,
        "logs": 1,
        "oauth_logs": 1,
    }
    by_name = {item["name"]: item for item in overview["credentials"]}
    assert by_name["codex-owner@example.com.json"]["exportable"] is True
    assert by_name["codex-owner@example.com.json"]["has_refresh_token"] is True
    assert by_name["codex-owner@example.com.json"]["plan_type"] == "plus"
    assert (
        by_name["codex-owner@example.com.json"]["subscription_active_until"]
        == "2026-09-02T05:00:00+00:00"
    )
    assert by_name["codex-owner@example.com.json"]["account_hint"] == "chatgpt-…7890"
    assert by_name["codex-receipt.json"]["kind"] == "receipt"
    assert by_name["broken.json"]["kind"] == "invalid"
    assert "path" not in by_name["codex-owner@example.com.json"]


def test_artifact_ids_resolve_only_known_files(tmp_path: Path):
    data_dir = tmp_path / "data"
    credential_dir = data_dir / "codex_accounts"
    log_dir = tmp_path / "logs"
    credential_dir.mkdir(parents=True)
    log_dir.mkdir()
    credential = credential_dir / "codex-owner.json"
    credential.write_text(
        json.dumps({"type": "codex", "email": "owner@example.com", "refresh_token": "x"}),
        encoding="utf-8",
    )
    log = log_dir / "server.stderr.log"
    log.write_text("log", encoding="utf-8")
    store = ArtifactStore(data_dir, log_dir)

    credential_id = store.list_credentials()[0]["id"]
    log_id = store.list_logs()[0]["id"]
    assert store.credential_file(credential_id) == credential.resolve()
    assert store.log_file(log_id) == log.resolve()
    assert store.credential_file("../../mailboxes.json") is None
    assert store.log_file("0" * 24) is None
    assert store.exportable_credential_files() == [(credential.resolve(), "codex-owner.json")]
    matched = store.exportable_credential_for_email("OWNER@example.com")
    assert matched is not None
    assert matched["id"] == credential_id
    assert store.exportable_credential_file(
        credential_id, expected_email="owner@example.com"
    ) == credential.resolve()
    assert store.exportable_credential_file(
        credential_id, expected_email="different@example.com"
    ) is None


def test_delete_credentials_only_removes_resolved_exportable_files(tmp_path: Path):
    data_dir = tmp_path / "data"
    credential_dir = data_dir / "codex_accounts"
    credential_dir.mkdir(parents=True)
    credential = credential_dir / "codex-owner.json"
    credential.write_text(
        json.dumps({"type": "codex", "email": "owner@example.com", "access_token": "secret"}),
        encoding="utf-8",
    )
    store = ArtifactStore(data_dir, tmp_path / "logs")
    artifact_id = store.list_credentials()[0]["id"]

    assert store.delete_credentials([artifact_id]) == 1
    assert not credential.exists()
    with pytest.raises(KeyError):
        store.delete_credentials([artifact_id])


def test_log_view_maps_severity_groups_continuations_and_pagination(tmp_path: Path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "codex-run.log").write_text(
        "2026-07-28 21:00:00,100 [DEBUG] [Sentinel] frame ready\n"
        "2026-07-28 21:00:01,200 [INFO] [Codex] authorize started\n"
        "2026-07-28 21:00:02,300 [WARNING] [Outlook] retrying\n"
        "traceback continuation\n"
        "2026-07-28 21:00:03,400 [ERROR] [Codex] failed\n",
        encoding="utf-8",
    )
    store = ArtifactStore(tmp_path / "data", log_dir)
    log_id = store.list_logs()[0]["id"]

    first = store.read_log_events(log_id, offset=0, limit=2, order="asc")
    assert first is not None
    assert first["schema"]["name"] == "OpenTelemetry LogRecord"
    assert first["total_events"] == 4
    assert first["filtered_events"] == 4
    assert first["has_more"] is True
    assert [row["severity_number"] for row in first["events"]] == [5, 9]
    assert first["events"][0]["instrumentation_scope"]["name"] == "Sentinel"

    warnings = store.read_log_events(log_id, level="warn", query="traceback", order="asc")
    assert warnings is not None
    assert warnings["filtered_events"] == 1
    assert warnings["events"][0]["severity_text"] == "WARN"
    assert "traceback continuation" in warnings["events"][0]["body"]
    assert warnings["level_counts"]["error"] == 1


def test_log_view_problem_filter_combines_error_and_fatal(tmp_path: Path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "codex-problems.log").write_text(
        "2026-07-28 21:00:00,100 [INFO] normal\n"
        "2026-07-28 21:00:01,100 [ERROR] failed\n"
        "2026-07-28 21:00:02,100 [CRITICAL] stopped\n",
        encoding="utf-8",
    )
    store = ArtifactStore(tmp_path / "data", log_dir)
    result = store.read_log_events(store.list_logs()[0]["id"], level="problem", order="asc")

    assert result is not None
    assert [event["severity_text"] for event in result["events"]] == ["ERROR", "FATAL"]


def test_log_view_falls_back_to_gb18030_and_filters_only_local_api_access(tmp_path: Path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    content = (
        "2026-07-29 00:00:00,100 [INFO] [Codex] 邮箱 OTP 验证通过\n"
        '2026-07-29 00:00:01,100 [INFO] 127.0.0.1 - - [29/Jul/2026 00:00:01] "GET /api/overview HTTP/1.1" 200 -\n'
        '2026-07-29 00:00:02,100 [INFO] 127.0.0.1 - - [29/Jul/2026 00:00:02] "GET / HTTP/1.1" 200 -\n'
        '2026-07-29 00:00:03,100 [INFO] 192.0.2.10 - - [29/Jul/2026 00:00:03] "GET /api/overview HTTP/1.1" 200 -\n'
    )
    (log_dir / "server.stderr.log").write_bytes(content.encode("gb18030"))
    store = ArtifactStore(tmp_path / "data", log_dir)

    result = store.read_log_events(store.list_logs()[0]["id"], order="asc")

    assert result is not None
    bodies = [event["body"] for event in result["events"]]
    assert result["total_events"] == 3
    assert bodies[0] == "[Codex] 邮箱 OTP 验证通过"
    assert "[REDACTED]-07-29" not in repr(result)
    assert not any("127.0.0.1" in body and "/api/overview" in body for body in bodies)
    assert any('127.0.0.1 - -' in body and '"GET / HTTP/1.1"' in body for body in bodies)
    assert any("192.0.2.10" in body and "/api/overview" in body for body in bodies)


def test_log_view_redacts_secrets_otp_phone_paths_and_control_codes(tmp_path: Path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    secrets = {
        "api": "hero-api-secret",
        "token": "refresh-token-secret",
        "bearer": "bearer-secret-value",
        "code": "ac_authorization_secret",
        "plain_code": "generic-oauth-code-secret",
        "dict_code": "payload-code-secret",
        "state": "oauth-state-secret",
        "activation": "hero-activation-secret",
        "old_otp": "135790",
        "otp": "246810",
        "phone": "15551234567",
        "e164": "447700900123",
        "url_ticket": "unknown-url-ticket-secret",
        "payload_url": "payload-url-path-secret",
        "mailbox_path_token": "mailbox-path-token-secret",
        "icloud_api_key": "icloud-api-key-secret",
        "totp_secret": "JBSWY3DPEHPK3PXP",
        "two_factor_secret": "GEZDGNBVGY3TQOJQ",
    }
    (log_dir / "server.stdout.log").write_text(
        "2026-07-28 21:00:00,100 [INFO] WEBUI_AUTH_CODE=local-console-secret\n"
        f"2026-07-28 21:00:01,100 [INFO] api_key={secrets['api']} refresh_token={secrets['token']}\n"
        f"2026-07-28 21:00:02,100 [INFO] Authorization: Bearer {secrets['bearer']}\n"
        f"2026-07-28 21:00:03,100 [INFO] callback?code={secrets['code']}&state=state-secret\n"
        f"2026-07-28 21:00:04,100 [INFO] \u90ae\u7bb1 OTP \u6536\u5230\uff1a{secrets['otp']} phone=+{secrets['phone']}\n"
        "2026-07-28 21:00:05,100 [INFO] C:\\Users\\private-user\\project\\app.py\x1b[31m\u202e\n"
        f"2026-07-28 21:00:06,100 [INFO] state={secrets['state']} activation_id={secrets['activation']}\n"
        f"2026-07-28 21:00:07,100 [INFO] authorization code\uff1a{secrets['plain_code']} phone +{secrets['e164']}\n"
        f"2026-07-28 21:00:08,100 [INFO] \u53d1\u73b0\u66f4\u65b0 OTP={secrets['otp']}\uff0c\u66ff\u6362\u4e4b\u524d\u7684 {secrets['old_otp']}\n"
        f"2026-07-28 21:00:09,100 [INFO] \u5b8c\u6574\u6388\u6743\u5730\u5740: https://auth.openai.com/oauth/authorize?code={secrets['code']}&unknown_ticket={secrets['url_ticket']}\n"
        f"2026-07-28 21:00:10,100 [INFO] \u5df2\u53d6\u53f7\uff1a{secrets['phone']} visible={secrets['e164']} hidden={secrets['phone']}\n"
        f"2026-07-28 21:00:11,100 [INFO] \u624b\u673a\u53f7 E.164\uff1a'+{secrets['e164']}' \u624b\u673a\u53f7\u8f93\u5165\u503c\uff1a'{secrets['phone']}'\n"
        f"2026-07-28 21:00:12,100 [ERROR] payload={{'auth_url': 'https://mail.test/{secrets['payload_url']}', 'code': '{secrets['dict_code']}'}}\n"
        f"2026-07-28 21:00:13,100 [INFO] totp_secret={secrets['totp_secret']} 2fa_secret={secrets['two_factor_secret']}\n"
        f"2026-07-28 21:00:14,100 [DEBUG] http://mail.example.test:80 \"GET /message/481902/{secrets['mailbox_path_token']}/owner@example.test HTTP/1.1\" 200\n"
        f"2026-07-28 21:00:15,100 [DEBUG] GET /api/v1/code?email=owner@example.test&key={secrets['icloud_api_key']} HTTP/1.1\n"
        f"2026-07-28 21:00:16,100 [INFO] imported=owner@example.test----{secrets['icloud_api_key']}\n",
        encoding="utf-8",
    )
    store = ArtifactStore(tmp_path / "data", log_dir)
    result = store.read_log_events(store.list_logs()[0]["id"], order="asc")
    serialized = json.dumps(result, ensure_ascii=False)

    for secret in (*secrets.values(), "local-console-secret", "state-secret", "private-user"):
        assert secret not in serialized
    assert "[REDACTED]" in serialized
    assert "***4567" in serialized
    assert "[USER_HOME]" in serialized
    assert "\x1b" not in serialized
    assert "\u202e" not in serialized


def test_log_view_masks_email_pipe_totp_material_and_otpauth_uri(tmp_path: Path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    email = "demo.operator@example.test"
    password = "fictional|password-for-test"
    pipe_secret = "JBSWY3DPEHPK3PXP"
    uri_secret = "GEZDGNBVGY3TQOJQ"
    (log_dir / "codex-redaction.log").write_text(
        f"2026-07-31 03:00:00,100 [WARNING] [Codex] \u5931\u8d25\uff1a{email}\uff0cRuntimeError\n"
        f"2026-07-31 03:00:01,100 [ERROR] imported={email}|{password}|{pipe_secret}\n"
        "2026-07-31 03:00:02,100 [ERROR] authenticator="
        f"otpauth://totp/Demo:{email}?secret={uri_secret}&issuer=Demo\n",
        encoding="utf-8",
    )

    store = ArtifactStore(tmp_path / "data", log_dir)
    result = store.read_log_events(store.list_logs()[0]["id"], order="asc")
    serialized = json.dumps(result, ensure_ascii=False)

    assert email not in serialized
    assert password not in serialized
    assert pipe_secret not in serialized
    assert uri_secret not in serialized
    assert "otpauth://" not in serialized
    assert "d***@example.test" in serialized
    assert "[REDACTED]|[REDACTED]" in serialized
    assert "[REDACTED-OTPAUTH]" in serialized


def test_log_view_rejects_invalid_filters(tmp_path: Path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "server.log").write_text("one line", encoding="utf-8")
    store = ArtifactStore(tmp_path / "data", log_dir)
    log_id = store.list_logs()[0]["id"]

    for kwargs in (
        {"offset": -1},
        {"limit": 501},
        {"level": "verbose"},
        {"order": "sideways"},
        {"query": "x" * 201},
    ):
        try:
            store.read_log_events(log_id, **kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {kwargs}")


def test_log_timeline_aggregates_recent_events_and_excludes_archive(tmp_path: Path):
    log_dir = tmp_path / "logs"
    archive_dir = log_dir / "archive"
    archive_dir.mkdir(parents=True)
    account_id = "a" * 24
    (log_dir / f"codex-{account_id}-new.log").write_text(
        "2026-07-29 01:00:00,100 [INFO] [Codex] 邮箱 OTP 验证通过\n"
        "2026-07-29 01:00:02,100 [WARNING] [Codex] add-phone/send 未成功 "
        "Phone number already in use. api_key=hero-secret\n",
        encoding="utf-8",
    )
    (log_dir / "server.stderr.log").write_text(
        "2026-07-29 01:00:01,100 [ERROR] [Worker] TLS connect error\n",
        encoding="utf-8",
    )
    (archive_dir / "old.log").write_text(
        "2026-07-30 01:00:00,100 [ERROR] archived failure must stay hidden\n",
        encoding="utf-8",
    )

    store = ArtifactStore(tmp_path / "data", log_dir)
    result = store.read_log_timeline(
        limit=20,
        account_emails={account_id: "owner@example.com"},
    )

    assert result["redacted"] is True
    assert result["files_total"] == 2
    assert result["files_scanned"] == 2
    assert [event["timestamp"] for event in result["events"]] == sorted(
        [event["timestamp"] for event in result["events"]], reverse=True
    )
    assert all("archive" not in event["source"]["name"] for event in result["events"])
    warning = next(event for event in result["events"] if event["severity_text"] == "WARN")
    assert warning["account_id"] == account_id
    assert warning["account_email"] == "owner@example.com"
    assert warning["stage"] == "接码"
    assert warning["summary"] == "号码已被使用，准备换号"
    assert "hero-secret" not in json.dumps(warning, ensure_ascii=False)
    assert "[REDACTED]" in warning["detail"]

    problems = store.read_log_timeline(level="problem", query="TLS")
    assert problems["filtered_events"] == 1
    assert problems["events"][0]["summary"] == "TLS 网络连接失败"
    by_email = store.read_log_timeline(
        query="OWNER@example.com",
        account_emails={account_id.upper(): "owner@example.com"},
    )
    assert by_email["filtered_events"] == 2
    important = store.read_log_timeline(level="important")
    assert important["level_counts"]["important"] >= 1
    assert all(event["important"] for event in important["events"])


def test_log_timeline_treats_warning_task_failure_as_problem(tmp_path: Path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "codex-task.log").write_text(
        "2026-07-29 01:00:00,100 [WARNING] [Codex] add-phone/send 未成功 "
        "Phone number already in use.\n"
        "2026-07-29 01:00:01,100 [WARNING] [Codex] 失败：owner@example.com，"
        "RuntimeError: [Codex] 手机号验证重试 2 次仍失败（provider=hero）\n"
        "2026-07-29 01:00:02,100 [WARNING] [Codex] 失败：tls@example.com，"
        "SSLError: TLS connect error\n"
        "2026-07-29 01:00:03,100 [WARNING] [Codex] 失败：guard@example.com，"
        "invalid_request_error fraud_guard suspicious behavior\n"
        "2026-07-29 01:00:04,100 [WARNING] [Codex] 失败：used@example.com，"
        "invalid_request_error phone_number_in_use\n",
        encoding="utf-8",
    )

    result = ArtifactStore(tmp_path / "data", log_dir).read_log_timeline(
        level="problem"
    )

    assert result["level_counts"]["problem"] == 4
    assert result["filtered_events"] == 4
    assert all(event["problem"] is True for event in result["events"])
    assert any(event["summary"].startswith("任务失败：") for event in result["events"])
    assert {event["summary"] for event in result["events"]} >= {
        "TLS 网络连接失败",
        "号码被风控拒绝，准备换号",
        "号码已被使用，准备换号",
    }


def test_log_timeline_reads_only_tail_and_reuses_bounded_cache(tmp_path: Path, monkeypatch):
    import src.artifact_store as artifact_module

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    log_path = log_dir / "server.log"
    old = "2026-07-28 00:00:00,100 [ERROR] old-prefix-event\n"
    padding = "x" * (512 * 1024 + 64)
    log_path.write_text(
        old + padding + "\n2026-07-29 00:00:00,100 [INFO] recent-tail-event\n",
        encoding="utf-8",
    )
    original_reader = artifact_module._read_log_tail_text
    calls = []

    def counted_reader(*args, **kwargs):
        calls.append(1)
        return original_reader(*args, **kwargs)

    monkeypatch.setattr(artifact_module, "_read_log_tail_text", counted_reader)
    store = ArtifactStore(tmp_path / "data", log_dir)
    first = store.read_log_timeline()
    second = store.read_log_timeline()

    assert len(calls) == 1
    assert first["events"] == second["events"]
    assert first["events"][0]["summary"] == "recent-tail-event"
    assert all("old-prefix-event" not in event["detail"] for event in first["events"])
    assert first["events"][0]["source"]["tail_truncated"] is True

    with log_path.open("a", encoding="utf-8") as handle:
        handle.write("2026-07-29 00:00:01,100 [WARNING] changed\n")
    changed = store.read_log_timeline()
    assert len(calls) == 2
    assert changed["events"][0]["summary"] == "changed"


def test_log_timeline_rejects_invalid_filters(tmp_path: Path):
    store = ArtifactStore(tmp_path / "data", tmp_path / "logs")
    for kwargs in (
        {"offset": -1},
        {"limit": 201},
        {"level": "error"},
        {"query": "x" * 201},
    ):
        with __import__("pytest").raises(ValueError):
            store.read_log_timeline(**kwargs)


def test_sms_statistics_reuses_cache_until_source_log_changes(tmp_path: Path, monkeypatch):
    import src.artifact_store as artifact_module

    data_dir = tmp_path / "data"
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    log = log_dir / "codex-0123456789abcdef01234567-run.log"
    log.write_text(
        "2026-07-29 01:00:00,000 [INFO] [SMS:Hero] acquired country=33 price=0.1 "
        "action=getNumber activation_id=x\n",
        encoding="utf-8",
    )
    store = ArtifactStore(data_dir, log_dir)
    calls = 0
    original = artifact_module._read_log_text

    def counted(path):
        nonlocal calls
        calls += 1
        return original(path)

    monkeypatch.setattr(artifact_module, "_read_log_text", counted)

    first = store.sms_statistics()
    second = store.sms_statistics()
    assert first == second
    assert calls == 1

    with log.open("a", encoding="utf-8") as handle:
        handle.write("2026-07-29 01:00:01,000 [INFO] [Codex] 手机号验证通过\n")
    changed = store.sms_statistics()
    assert calls == 2
    assert changed["summary"]["verified"] == 1


def test_log_timeline_caps_cached_events_per_file(tmp_path: Path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "server.log").write_text(
        "".join(
            f"2026-07-29 01:{index // 60:02d}:{index % 60:02d},100 [INFO] event-{index}\n"
            for index in range(350)
        ),
        encoding="utf-8",
    )

    result = ArtifactStore(tmp_path / "data", log_dir).read_log_timeline(limit=200)

    assert result["events_per_file"] == 300
    assert result["total_events"] == 300
    assert all(event["summary"] != "event-0" for event in result["events"])


def test_sms_statistics_tracks_country_price_outcomes_and_keeps_auth_secrets_private(tmp_path: Path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    activation_ids = [
        "act-fraud-secret",
        "act-used-secret",
        "act-limited-secret",
        "act-timeout-secret",
        "act-code-secret",
        "act-success-secret",
    ]
    phones = [
        "84111111111",
        "84222222222",
        "523333333333",
        "56444444444",
        "525555555555",
        "526666666666",
    ]
    otp_values = ["731942", "846205"]
    (log_dir / "codex-run.log").write_text(
        "2026-07-29 00:00:00,000 [INFO] [SMS:Hero] acquired country=10 price=0.275 action=getNumber activation_id=act-fraud-secret\n"
        "2026-07-29 00:00:01,000 [INFO] [Codex] 手机验证尝试 1/10，provider=hero, activation_id=act-fraud-secret, 号码=+84111111111\n"
        "2026-07-29 00:00:02,000 [WARNING] [Codex] add-phone/send 未成功 reason=send_rejected, status=400: suspicious behavior invalid_request_error fraud_guard，换号重试\n"
        "2026-07-29 00:00:03,000 [INFO] [SMS:Hero] acquired country=10 price=0.275 action=getNumber activation_id=act-used-secret\n"
        "2026-07-29 00:00:04,000 [INFO] [Codex] 手机验证尝试 2/10，provider=hero, activation_id=act-used-secret, 号码=+84222222222\n"
        "2026-07-29 00:00:05,000 [WARNING] [Codex] add-phone/send 未成功 reason=send_rejected, status=400: Phone number already in use. invalid_request_error phone_number_in_use，换号重试\n"
        "2026-07-29 00:00:06,000 [INFO] [SMS:Hero] acquired country=54 price=0.11 action=getNumberV2 activation_id=act-limited-secret\n"
        "2026-07-29 00:00:07,000 [INFO] [Codex] 手机验证尝试 3/10，provider=hero, activation_id=act-limited-secret, 号码=+523333333333\n"
        "2026-07-29 00:00:08,000 [WARNING] [Codex] add-phone/send 未成功 reason=send_limited, status=400: invalid_request_error rate_limit_exceeded，换号重试\n"
        "2026-07-29 00:00:09,000 [INFO] [SMS:Hero] acquired country=151 price=auto action=getNumber activation_id=act-timeout-secret\n"
        "2026-07-29 00:00:10,000 [INFO] [Codex] 手机验证尝试 4/10，provider=hero, activation_id=act-timeout-secret, 号码=+56444444444\n"
        "2026-07-29 00:00:11,000 [INFO] [Codex] 短信已发送，开始轮询验证码 activation_id=act-timeout-secret, wait=30s, interval=5s\n"
        "2026-07-29 00:00:41,000 [WARNING] [Codex] 号码 +56444444444 在 30s 内未收到短信，取消换号\n"
        "2026-07-29 00:00:42,000 [INFO] [SMS:Hero] acquired country=54 price=0.11 action=getNumber activation_id=act-code-secret\n"
        "2026-07-29 00:00:43,000 [INFO] [Codex] 手机验证尝试 5/10，provider=hero, activation_id=act-code-secret, 号码=+525555555555\n"
        "2026-07-29 00:00:44,000 [INFO] [Codex] 短信已发送，开始轮询验证码 activation_id=act-code-secret, wait=30s, interval=5s\n"
        "2026-07-29 00:00:45,000 [INFO] [SMS] 第 1 轮收到验证码：731942\n"
        "2026-07-29 00:00:46,000 [WARNING] [Codex] phone-otp/validate 失败 reason=invalid, status=400: secret raw response\n"
        "2026-07-29 00:00:47,000 [INFO] [SMS:Hero] acquired country=54 price=0.11 action=getNumber activation_id=act-success-secret\n"
        "2026-07-29 00:00:48,000 [INFO] [Codex] 手机验证尝试 6/10，provider=hero, activation_id=act-success-secret, 号码=+526666666666\n"
        "2026-07-29 00:00:49,000 [INFO] [Codex] 短信已发送，开始轮询验证码 activation_id=act-success-secret, wait=30s, interval=5s\n"
        "2026-07-29 00:00:50,000 [INFO] [SMS] 第 2 轮收到验证码：846205\n"
        "2026-07-29 00:00:51,000 [INFO] [Codex] 手机号验证通过\n"
        "2026-07-29 00:00:52,000 [INFO] [Codex] 成功：private-owner@example.com\n",
        encoding="utf-8",
    )
    (log_dir / "server.stderr.log").write_text(
        "2026-07-29 00:01:00,000 [INFO] [SMS:Hero] acquired country=99 price=9.99 action=getNumber activation_id=server-secret\n",
        encoding="utf-8",
    )

    result = ArtifactStore(tmp_path / "data", log_dir).sms_statistics()
    serialized = json.dumps(result, ensure_ascii=False)

    assert result["summary"] == {
        "numbers_acquired": 6,
        "sms_sent": 3,
        "codes_received": 2,
        "verified": 1,
        "failed": 5,
        "pending": 0,
        "success_rate": 16.7,
        "priced_numbers": 5,
        "quoted_total": "0.88",
        "quoted_average": "0.176",
        "cancel_errors": 0,
    }
    assert result["logs_scanned"] == 1
    assert result["records_total"] == 6
    assert result["records_truncated"] is False
    assert "验证成功号码数 / 取号数" in result["success_rate_note"]
    assert "不代表最终实际扣费" in result["price_note"]
    countries = {row["country_id"]: row for row in result["countries"]}
    assert countries[10]["failed"] == 2
    assert countries[10]["quoted_average"] == "0.275"
    assert countries[54]["numbers_acquired"] == 3
    assert countries[54]["verified"] == 1
    assert countries[54]["success_rate"] == 33.3
    assert countries[151]["priced_numbers"] == 0
    assert countries[151]["quoted_total"] == "0"

    by_status = {row["status"]: row for row in result["records"]}
    assert set(by_status) == {
        "fraud_guard",
        "number_in_use",
        "rate_limited",
        "sms_timeout",
        "code_rejected",
        "verified",
    }
    assert by_status["verified"]["phone_number"] == "+526666666666"
    assert by_status["verified"]["action"] == "getNumber"
    assert by_status["verified"]["code_received"] is True
    assert by_status["rate_limited"]["action"] == "getNumberV2"
    assert by_status["sms_timeout"]["price"] is None
    assert all(len(row["id"]) == 20 for row in result["records"])

    for phone in phones:
        assert f"+{phone}" in serialized
    for secret in (*activation_ids, *otp_values, "private-owner@example.com", "secret raw response"):
        assert secret not in serialized
    assert "server-secret" not in serialized


def test_sms_statistics_distinguishes_pending_from_terminal_failure(tmp_path: Path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "codex-pending.log").write_text(
        "2026-07-29 01:00:00,000 [INFO] [SMS:Hero] acquired country=10 price=0.1 action=getNumber activation_id=first\n"
        "2026-07-29 01:00:01,000 [INFO] [Codex] 手机验证尝试 1/2，provider=hero, activation_id=first, 号码=+84123450001\n"
        "2026-07-29 01:00:02,000 [INFO] [SMS:Hero] acquired country=54 price=0.2 action=getNumber activation_id=second\n"
        "2026-07-29 01:00:03,000 [INFO] [Codex] 手机验证尝试 2/2，provider=hero, activation_id=second, 号码=+52123450002\n"
        "2026-07-29 01:00:04,000 [INFO] [Codex] 短信已发送，开始轮询验证码 activation_id=second, wait=30s, interval=5s\n",
        encoding="utf-8",
    )

    result = ArtifactStore(tmp_path / "data", log_dir).sms_statistics()

    assert result["summary"]["failed"] == 1
    assert result["summary"]["pending"] == 1
    assert {row["status"] for row in result["records"]} == {"replaced", "waiting_code"}
    assert "尚未进入" in result["definitions"]["pending"]


def test_phone_verification_can_be_recovered_for_existing_account(tmp_path: Path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    account_id = hashlib.sha256("owner@example.com".encode("utf-8")).hexdigest()[:24]
    (log_dir / f"codex-{account_id}-old-a1.log").write_text(
        "2026-07-29 01:00:00,000 [INFO] [Codex] 手机验证尝试 1/2，provider=hero, activation_id=secret, 号码=+84123456789\n"
        "2026-07-29 01:00:01,000 [INFO] [Codex] 手机号验证通过\n",
        encoding="utf-8",
    )

    result = ArtifactStore(tmp_path / "data", log_dir).phone_verification_for_account(account_id)

    assert result is not None
    assert result["phone_number"] == "+84123456789"
    assert "2026-07-29T01:00:01" in result["phone_verified_at"]
    assert ArtifactStore(tmp_path / "data", log_dir).phone_verification_for_account("../bad") is None
