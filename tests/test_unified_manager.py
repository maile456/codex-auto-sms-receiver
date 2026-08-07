from __future__ import annotations

import hashlib
import json
import base64
from datetime import datetime, timezone
from pathlib import Path

import pytest

from manager.credential_import import CredentialImportError, import_codex_documents
from manager_app import create_managed_app
from src.settings import Settings


ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "vendor" / "GPTSession2CPAandSub2API"
LOCK = ROOT / "manager" / "upstreams.lock.json"
FIXED_NOW = datetime(2026, 8, 8, 0, 0, 0, tzinfo=timezone.utc)


def git_blob_sha(path: Path) -> str:
    body = path.read_bytes()
    return hashlib.sha1(f"blob {len(body)}\0".encode() + body).hexdigest()


def jwt(payload: dict) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{header}.{body}.fixture"


def codex_document(email: str | None, *, access: str | None = None) -> dict:
    claims: dict[str, object] = {"exp": 1780000000}
    if email:
        claims["email"] = email
    return {
        "auth_mode": "chatgpt",
        "tokens": {
            "access_token": access or jwt(claims),
            "refresh_token": "fixture-refresh-not-a-real-token",
            "id_token": jwt({"email": email}) if email else "fixture-id-not-a-real-token",
            "account_id": "fixture-account",
        },
        "last_refresh": "2026-08-08T00:00:00Z",
    }


def settings_for(path: Path) -> Settings:
    return Settings(
        project_root=ROOT,
        data_dir=path / "data",
        log_dir=path / "logs",
        browser_executable=None,
        browser_timeout_seconds=120,
        host="127.0.0.1",
        port=5015,
    )


def managed_client(path: Path):
    app = create_managed_app(settings_for(path), codex_manager=object())
    app.config["TESTING"] = True
    return app.test_client()


def test_converter_snapshot_matches_pinned_upstream_blobs():
    expected = {
        ".gitignore": "f82ca88940e9e1b2653b840c5980e0787d9a4b3a",
        "README.md": "72ea5156f8ba69eb9fb3aab0b1fcf23c6ed9998b",
        "docs/index.html": "8d853eae0022bcb965161f921c5afcacd1ad7166",
        "tests/convert-session.test.js": "bc05413da38573c102d19393c3dd148adbca96b1",
    }
    assert {name: git_blob_sha(VENDOR / name) for name in expected} == expected


def test_upstream_lock_names_both_pinned_repositories():
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    by_key = {row["key"]: row for row in lock["upstreams"]}
    assert lock["schema_version"] == 1
    assert by_key["receiver"]["repository"] == "maile456/codex-auto-sms-receiver"
    assert by_key["receiver"]["commit"] == "269bf3cd088b075f164ad2fe8e674b8b72a9fd26"
    assert by_key["converter"]["repository"] == "gtxx3600/GPTSession2CPAandSub2API"
    assert by_key["converter"]["commit"] == "a097eb155bb7bdf6cbbc26f1e4e75e120ab3163c"


def test_import_codex_document_writes_flat_local_credential(tmp_path):
    document = {
        "auth_mode": "chatgpt",
        "tokens": {
            "access_token": jwt({"email": "owner@example.com", "exp": 1780000000}),
            "refresh_token": "fixture-refresh-not-a-real-token",
            "id_token": jwt({"email": "owner@example.com"}),
            "account_id": "acct-1",
        },
        "last_refresh": "2026-08-08T00:00:00Z",
    }

    result = import_codex_documents(document, tmp_path, now=FIXED_NOW)

    saved = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    assert result.as_dict() == {"imported": 1, "duplicates": 0, "total": 1}
    assert saved == {
        "type": "codex",
        "email": "owner@example.com",
        "account_id": "acct-1",
        "id_token": document["tokens"]["id_token"],
        "access_token": document["tokens"]["access_token"],
        "refresh_token": "fixture-refresh-not-a-real-token",
        "last_refresh": "2026-08-08T00:00:00Z",
        "expired": "2026-05-28T20:26:40Z",
        "source": "GPTSession2CPAandSub2API",
    }


def test_import_codex_documents_skips_duplicate_and_versions_new_token(tmp_path):
    first = codex_document("same@example.com", access="fixture-access-one")
    assert import_codex_documents(first, tmp_path, now=FIXED_NOW).imported == 1
    assert import_codex_documents(first, tmp_path, now=FIXED_NOW).duplicates == 1

    second = codex_document("same@example.com", access="fixture-access-two")
    assert import_codex_documents(second, tmp_path, now=FIXED_NOW).imported == 1
    assert len(list(tmp_path.glob("*.json"))) == 2


def test_import_validates_entire_batch_before_writing(tmp_path):
    with pytest.raises(CredentialImportError, match="第 2 项缺少 access_token"):
        import_codex_documents([codex_document("ok@example.com"), {"tokens": {}}], tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_import_allows_missing_email_and_ignores_client_filename(tmp_path):
    document = codex_document(None)
    document["filename"] = "../../outside.json"

    result = import_codex_documents(document, tmp_path, now=FIXED_NOW)

    saved = next(tmp_path.glob("*.json"))
    assert result.imported == 1
    assert saved.parent == tmp_path
    assert ".." not in saved.name


def test_import_rejects_more_than_100_documents(tmp_path):
    with pytest.raises(CredentialImportError, match="每次最多导入 100 个凭证"):
        import_codex_documents(
            [codex_document(f"user-{index}@example.com") for index in range(101)],
            tmp_path,
        )


def test_import_rejects_non_string_access_token_without_leaking_value(tmp_path):
    document = codex_document("owner@example.com")
    document["tokens"]["access_token"] = 123456789

    with pytest.raises(CredentialImportError, match="access_token 必须是字符串") as raised:
        import_codex_documents(document, tmp_path)

    assert "123456789" not in str(raised.value)


def test_managed_app_preserves_receiver_and_adds_manager_routes(tmp_path):
    client = managed_client(tmp_path)

    assert client.get("/").status_code == 200
    assert client.get("/health").get_json()["ok"] is True
    manager = client.get("/manager")
    page = manager.get_data(as_text=True)
    assert manager.status_code == 200
    assert "接码与 OAuth 管理" in page
    assert "Session / Token 格式转换" in page
    assert 'href="/"' in page
    assert 'href="/tools/session-converter/"' in page


def test_manager_status_exposes_pins_without_credentials(tmp_path):
    credential_dir = tmp_path / "data" / "codex_accounts"
    credential_dir.mkdir(parents=True)
    (credential_dir / "fixture.json").write_text(
        json.dumps(codex_document("status@example.com")),
        encoding="utf-8",
    )

    response = managed_client(tmp_path).get("/api/manager/status")

    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert body["credential_count"] == 1
    assert {item["key"]: item["current_sha"] for item in body["projects"]} == {
        "receiver": "269bf3cd088b075f164ad2fe8e674b8b72a9fd26",
        "converter": "a097eb155bb7bdf6cbbc26f1e4e75e120ab3163c",
    }
    assert "fixture-refresh-not-a-real-token" not in response.get_data(as_text=True)


def test_converter_route_injects_bridge_without_modifying_vendor(tmp_path):
    before = git_blob_sha(VENDOR / "docs/index.html")
    client = managed_client(tmp_path)

    response = client.get("/tools/session-converter/")

    assert response.status_code == 200
    assert '/manager-static/converter_bridge.js' in response.get_data(as_text=True)
    assert response.headers["Cache-Control"] == "no-store"
    assert git_blob_sha(VENDOR / "docs/index.html") == before
    assert client.get("/tools/session-converter/favicon.svg").status_code == 200
    assert client.get("/tools/session-converter/%2e%2e/README.md").status_code == 404


def test_credential_import_route_requires_confirmation_and_hides_tokens(tmp_path):
    client = managed_client(tmp_path)
    document = codex_document("route@example.com", access="fixture-route-access")

    denied = client.post("/api/manager/credentials/import", json={"documents": document})
    response = client.post(
        "/api/manager/credentials/import",
        json={"confirmed": True, "documents": document},
    )

    assert denied.status_code == 400
    assert response.status_code == 200
    assert response.get_json() == {
        "ok": True,
        "imported": 1,
        "duplicates": 0,
        "total": 1,
    }
    assert "fixture-route-access" not in response.get_data(as_text=True)


def test_manager_import_has_its_own_five_mebibyte_request_limit(tmp_path):
    client = managed_client(tmp_path)
    medium = codex_document("medium@example.com", access="x" * (64 * 1024))

    accepted = client.post(
        "/api/manager/credentials/import",
        json={"confirmed": True, "documents": medium},
    )
    oversized = b'{"confirmed":true,"documents":"' + b"x" * (5 * 1024 * 1024) + b'"}'
    rejected = client.post(
        "/api/manager/credentials/import",
        data=oversized,
        content_type="application/json",
    )

    assert accepted.status_code == 200
    assert rejected.status_code == 413
