from __future__ import annotations

import hashlib
import json
import base64
from datetime import datetime, timezone
from pathlib import Path

import pytest

from manager.credential_import import CredentialImportError, import_codex_documents


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
