import shutil
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from src.mailbox_store import MailboxStore


@pytest.fixture
def workspace_path():
    path = Path(__file__).resolve().parent / f"runtime-mailbox-{uuid.uuid4().hex}"
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_import_outlook_masks_secrets(workspace_path: Path):
    store = MailboxStore(workspace_path)
    result = store.import_text(
        "outlook",
        "owner@example.com----mail-pass----client-id----refresh-secret",
    )
    assert result == {"parsed": 1, "inserted": 1, "updated": 0, "invalid": 0}
    public = store.list_accounts()[0]
    assert public["email"] == "owner@example.com"
    assert public["otp_ready"] is True
    assert "refresh_token" not in public
    assert "password" not in public
    secret = store.get_secret(email="owner@example.com")
    assert secret["refresh_token"] == "refresh-secret"


def test_import_generic_api_updates_existing(workspace_path: Path):
    store = MailboxStore(workspace_path)
    store.import_text("generic_api", "owner@example.com----https://mail.test/first")
    result = store.import_text("generic_api", "owner@example.com----https://mail.test/second")
    assert result["updated"] == 1
    assert len(store.list_accounts()) == 1
    assert store.get_secret(email="owner@example.com")["code_url"].endswith("/second")


def test_import_generic_api_key_builds_icloud_endpoint_without_public_leak(
    workspace_path: Path,
):
    store = MailboxStore(workspace_path)
    api_key = "fictional:key+with&equals=?#slash/----tail"

    result = store.import_text(
        "generic_api",
        f"owner+alias@example.com----{api_key}",
    )

    assert result == {"parsed": 1, "inserted": 1, "updated": 0, "invalid": 0}
    public = store.list_accounts()[0]
    assert public["otp_ready"] is True
    assert "code_url" not in public
    assert api_key not in str(public)

    secret = store.get_secret(email="owner+alias@example.com")
    parsed = urlsplit(secret["code_url"])
    assert parsed.scheme == "https"
    assert parsed.netloc == "icloud.xbovo.online"
    assert parsed.path == "/api/v1/code"
    assert parse_qs(parsed.query) == {
        "email": ["owner+alias@example.com"],
        "key": [api_key],
    }


def test_import_generic_api_rejects_non_http_url(workspace_path: Path):
    store = MailboxStore(workspace_path)

    with pytest.raises(ValueError, match="没有解析到"):
        store.import_text(
            "generic_api",
            "owner@example.com----ftp://mail.example.test/code",
        )


@pytest.mark.parametrize(
    "value",
    [
        "https://@/code",
        "https://mail.example.test:bad/code",
        "https://mail.example.test/code\x00injected",
    ],
)
def test_import_generic_api_rejects_invalid_http_url(
    workspace_path: Path,
    value: str,
):
    store = MailboxStore(workspace_path)

    with pytest.raises(ValueError, match="没有解析到"):
        store.import_text("generic_api", f"owner@example.com----{value}")


def test_import_password_totp_normalizes_and_masks_secrets(workspace_path: Path):
    store = MailboxStore(workspace_path)
    result = store.import_text(
        "password_totp",
        "owner@example.com|chatgpt-pass|jbsw y3dp-ehpk3pxp jbswy3dpehpk3pxp",
    )

    assert result == {"parsed": 1, "inserted": 1, "updated": 0, "invalid": 0}
    public = store.list_accounts()[0]
    assert public["source"] == "password_totp"
    assert public["otp_ready"] is True
    assert "password" not in public
    assert "totp_secret" not in public
    secret = store.get_secret(email="owner@example.com")
    assert secret["password"] == "chatgpt-pass"
    assert secret["totp_secret"] == "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"


def test_import_password_totp_allows_pipe_characters_in_password(workspace_path: Path):
    store = MailboxStore(workspace_path)
    result = store.import_text(
        "password_totp",
        "owner@icloud.example|fictional|chatgpt|password|JBSWY3DPEHPK3PXP",
    )

    assert result == {"parsed": 1, "inserted": 1, "updated": 0, "invalid": 0}
    secret = store.get_secret(email="owner@icloud.example")
    assert secret["password"] == "fictional|chatgpt|password"
    assert secret["totp_secret"] == "JBSWY3DPEHPK3PXP"


def test_export_original_preserves_each_import_format_and_delete_many(workspace_path: Path):
    store = MailboxStore(workspace_path)
    outlook = "mail@example.com====mail-pass====client-id====refresh-token"
    generic = "code@example.com----fixture-api-key"
    totp = "totp@example.com|chatgpt|password|JBSWY3DPEHPK3PXP"
    store.import_text("outlook", outlook)
    store.import_text("generic_api", generic)
    store.import_text("password_totp", totp)

    rows = store.list_accounts()
    grouped = store.export_original([row["id"] for row in rows])

    assert grouped["outlook"] == [outlook]
    assert grouped["generic_api"] == [generic]
    assert grouped["password_totp"] == [totp]
    assert store.delete_many([row["id"] for row in rows[:2]]) == 2
    assert len(store.list_accounts()) == 1


def test_reveal_original_returns_one_explicit_reimportable_line(workspace_path: Path):
    store = MailboxStore(workspace_path)
    material = "owner@example.com----https://mail.test/code?id=fixture"
    store.import_text("code_url", material)
    account_id = store.list_accounts()[0]["id"]

    revealed = store.reveal_original(account_id)

    assert revealed == {
        "id": account_id,
        "email": "owner@example.com",
        "source": "code_url",
        "material": material,
    }
    assert "material" not in store.list_accounts()[0]


@pytest.mark.parametrize(
    "material",
    [
        "|fictional-password|JBSWY3DPEHPK3PXP",
        "owner@icloud.example||JBSWY3DPEHPK3PXP",
        "owner@icloud.example|fictional-password|",
        "owner@icloud.example|fictional-password",
    ],
)
def test_import_password_totp_rejects_missing_fields(workspace_path: Path, material: str):
    store = MailboxStore(workspace_path)

    with pytest.raises(ValueError, match="没有解析到"):
        store.import_text("password_totp", material)


def test_import_password_totp_rejects_invalid_secret(workspace_path: Path):
    store = MailboxStore(workspace_path)
    with pytest.raises(ValueError, match="没有解析到"):
        store.import_text("password_totp", "owner@example.com|chatgpt-pass|not-base32")


def test_invalid_material_is_rejected(workspace_path: Path):
    store = MailboxStore(workspace_path)
    with pytest.raises(ValueError, match="没有解析到"):
        store.import_text("outlook", "owner@example.com----too-short")


def test_public_account_exposes_credential_presence_not_path(workspace_path: Path):
    store = MailboxStore(workspace_path)
    store.import_text("generic_api", "owner@example.com----https://mail.test/code")
    store.update_codex(
        "owner@example.com",
        status="success",
        credential_path=r"F:\\private\\codex-owner.json",
        phone_verified=True,
        phone_number="+84123456789",
    )
    store.update_codex("owner@example.com", status="failed", message="later retry failed")

    public = store.list_accounts()[0]
    assert public["has_credential"] is True
    assert public["phone_verified"] is True
    assert public["phone_number"] == "+84123456789"
    assert public["codex_status"] == "failed"
    assert "credential_path" not in public


def test_sale_status_can_be_marked_and_restored(workspace_path: Path):
    store = MailboxStore(workspace_path)
    store.import_text("code_url", "owner@example.com----https://mail.test/code")
    account_id = store.list_accounts()[0]["id"]

    assert store.list_accounts()[0]["sale_status"] == "unsold"
    assert store.update_sale_status([account_id], status="sold", note="order-100") == 1
    sold = store.list_accounts()[0]
    assert sold["sale_status"] == "sold"
    assert sold["sold_at"]
    assert sold["sale_note"] == "order-100"

    store.update_sale_status([account_id], status="unsold")
    restored = store.list_accounts()[0]
    assert restored["sale_status"] == "unsold"
    assert restored["sold_at"] is None
    assert restored["sale_note"] == ""


def test_inventory_export_history_counts_each_successful_export(workspace_path: Path):
    store = MailboxStore(workspace_path)
    store.import_text("code_url", "owner@example.com----https://mail.test/code")
    account_id = store.list_accounts()[0]["id"]

    assert store.list_accounts()[0]["export_count"] == 0
    assert store.record_exports([account_id]) == 1
    first = store.list_accounts()[0]
    assert first["export_count"] == 1
    assert first["first_exported_at"]
    assert first["last_exported_at"] == first["first_exported_at"]
    assert first["sale_status"] == "unsold"

    assert store.record_exports([account_id], mark_sold=True) == 1
    second = store.list_accounts()[0]
    assert second["export_count"] == 2
    assert second["last_exported_at"]
    assert second["sale_status"] == "sold"
    assert second["sold_at"]
