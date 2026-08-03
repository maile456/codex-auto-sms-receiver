import shutil
import uuid
import io
import json
import base64
import re
import zipfile
from pathlib import Path

import pytest
from dotenv import dotenv_values

from src.mailbox_store import MailboxStore
from src.artifact_store import ArtifactStore
from src.settings import Settings
from src.sms_config import SmsConfigStore
from src.webapp import create_app


def _unsigned_jwt(claims: dict) -> str:
    def encode(value: dict) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode(claims)}.signature"


class FakeCodexManager:
    def __init__(self):
        self.jobs = []
        self.pipeline = {
            "id": "",
            "status": "idle",
            "active": False,
            "concurrency": 1,
            "retry_limit": 0,
            "total": 0,
            "completed": 0,
            "progress": 0.0,
            "counts": {},
        }

    def availability(self):
        return {"available": True, "reason": ""}

    def runtime_config(self):
        return {"driver": "protocol", "auth_source": "local", "sms_provider": "hero", "outlook_fetch_mode": "direct"}

    def list_jobs(self):
        return list(self.jobs)

    def start(self, email):
        job = {"id": "job-1", "email": email, "status": "queued"}
        self.jobs.append(job)
        return job

    def stop(self, job_id):
        return False

    def pipeline_overview(self):
        return dict(self.pipeline)

    def start_batch(self, emails, *, concurrency, retry_limit, retry_backoff_seconds):
        self.pipeline = {
            "id": "pipeline-1",
            "status": "running",
            "active": True,
            "concurrency": int(concurrency),
            "retry_limit": int(retry_limit),
            "retry_backoff_seconds": int(retry_backoff_seconds),
            "total": len(emails),
            "completed": 0,
            "progress": 0.0,
            "counts": {"queued": len(emails)},
            "emails": list(emails),
        }
        return dict(self.pipeline)

    def stop_pipeline(self, pipeline_id):
        if pipeline_id != self.pipeline.get("id") or not self.pipeline.get("active"):
            return False
        self.pipeline.update(status="stopping", active=True)
        return True

    def pause_pipeline(self, pipeline_id):
        if pipeline_id != self.pipeline.get("id") or self.pipeline.get("status") not in {"queued", "running"}:
            return False
        self.pipeline.update(status="paused", active=True)
        return True

    def force_pause_pipeline(self, pipeline_id):
        if pipeline_id != self.pipeline.get("id") or not self.pipeline.get("active"):
            return None
        running = int((self.pipeline.get("counts") or {}).get("running") or 0)
        counts = dict(self.pipeline.get("counts") or {})
        counts["running"] = 0
        counts["failed"] = int(counts.get("failed") or 0) + running
        self.pipeline.update(
            status="paused",
            active=True,
            counts=counts,
            force_paused_count=running,
        )
        return dict(self.pipeline)

    def set_pipeline_concurrency(self, pipeline_id, concurrency):
        if pipeline_id != self.pipeline.get("id") or not self.pipeline.get("active"):
            return None
        concurrency = int(concurrency)
        if concurrency < 1 or concurrency > 10:
            raise ValueError("任务并发必须在 1 - 10 之间")
        self.pipeline["concurrency"] = concurrency
        return dict(self.pipeline)

    def resume_pipeline(self, pipeline_id):
        if pipeline_id != self.pipeline.get("id") or self.pipeline.get("status") != "paused":
            return False
        self.pipeline.update(status="running", active=True)
        return True

    def is_account_active(self, email):
        return any(
            str(job.get("email") or "").casefold() == str(email or "").casefold()
            and job.get("status") in {"queued", "running", "retry_wait"}
            for job in self.jobs
        )


@pytest.fixture
def workspace_path():
    path = Path(__file__).resolve().parent / f"runtime-web-{uuid.uuid4().hex}"
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _settings(path: Path) -> Settings:
    return Settings(
        project_root=Path(__file__).resolve().parents[1],
        data_dir=path / "data",
        log_dir=path / "logs",
        browser_executable=None,
        browser_timeout_seconds=120,
        host="127.0.0.1",
        port=5015,
    )


def _client(workspace_path: Path):
    mailbox = MailboxStore(workspace_path / "data")
    codex = FakeCodexManager()
    app = create_app(_settings(workspace_path), mailbox_store=mailbox, codex_manager=codex)
    client = app.test_client()
    return client, mailbox, codex


def test_console_is_available_without_login(workspace_path: Path):
    app = create_app(
        _settings(workspace_path),
        mailbox_store=MailboxStore(workspace_path / "data"),
        codex_manager=FakeCodexManager(),
    )
    client = app.test_client()
    index = client.get("/")
    assert index.status_code == 200
    assert "账号与任务" in index.get_data(as_text=True)
    assert client.get("/api/overview").status_code == 200
    for path, method in (("/login", "get"), ("/login", "post"), ("/logout", "get"), ("/logout", "post")):
        response = getattr(client, method)(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["Location"].endswith("/")


def test_original_account_material_requires_explicit_reveal(workspace_path: Path):
    client, mailbox, _ = _client(workspace_path)
    material = "owner@example.com----https://mail.test/code?id=fixture"
    mailbox.import_text("code_url", material)
    account = client.get("/api/accounts").get_json()["accounts"][0]
    assert "material" not in account

    denied = client.post(f"/api/accounts/{account['id']}/material/reveal", json={})
    assert denied.status_code == 400

    revealed = client.post(
        f"/api/accounts/{account['id']}/material/reveal", json={"confirmed": True}
    )
    assert revealed.status_code == 200
    assert revealed.get_json()["material"] == material

    html = client.get("/").get_data(as_text=True)
    assert 'id="materialDialog"' in html
    assert 'data-account-material="${esc(id)}"' in html
    assert "原始素材可能包含邮箱密码" not in html


def test_code_url_can_be_opened_directly_from_local_inventory(workspace_path: Path):
    client, mailbox, _ = _client(workspace_path)
    target = "https://mail.test/code?id=fixture"
    mailbox.import_text("code_url", f"owner@example.com----{target}")
    account = mailbox.list_accounts()[0]

    response = client.get(f"/api/accounts/{account['id']}/code-url/open")

    assert response.status_code == 302
    assert response.headers["Location"] == target
    html = client.get("/").get_data(as_text=True)
    assert 'data-account-url="${esc(id)}"' in html
    assert "function accountMaterialUrl" in html
    assert 'data-copy-email="${esc(row.email)}"' in html
    assert 'data-copy-email="${esc(account.email)}"' in html
    assert "toast('邮箱已复制')" in html


def test_phone_status_is_exposed_and_unverified_accounts_export_original_format(
    workspace_path: Path,
):
    client, mailbox, _ = _client(workspace_path)
    unverified = "waiting@example.com----https://mail.test/waiting"
    verified = "done@example.com----https://mail.test/done"
    mailbox.import_text("code_url", f"{unverified}\n{verified}")
    mailbox.update_codex(
        "done@example.com",
        status="success",
        phone_verified=True,
        phone_number="+15550000001",
    )

    accounts = client.get("/api/accounts").get_json()["accounts"]
    by_email = {row["email"]: row for row in accounts}
    assert by_email["waiting@example.com"]["phone_verified"] is False
    assert by_email["done@example.com"]["phone_verified"] is True

    denied = client.post("/api/accounts/phone-unverified/export", json={"count": 1})
    assert denied.status_code == 400
    exported = client.post(
        "/api/accounts/phone-unverified/export",
        json={"count": 1, "confirmed": True},
    )
    assert exported.status_code == 200
    assert exported.headers["X-Exported-Account-Count"] == "1"
    assert exported.headers["X-Account-Phone-Status"] == "unverified"
    assert exported.headers["X-Account-Scope"] == "all"
    with zipfile.ZipFile(io.BytesIO(exported.data)) as archive:
        content = archive.read("code_url.txt").decode("utf-8")
        assert unverified in content
        assert verified not in content

    html = client.get("/").get_data(as_text=True)
    assert '<option value="phone_unverified">未接码</option>' in html
    assert '<option value="phone_verified">已接码</option>' in html
    assert 'id="sellerExportSelected"' in html


def test_seller_inventory_export_status_and_quota(monkeypatch, workspace_path: Path):
    data_dir = workspace_path / "data"
    credential_dir = data_dir / "codex_accounts"
    credential_dir.mkdir(parents=True)
    mailbox = MailboxStore(data_dir)
    mailbox.import_text(
        "code_url",
        "first@example.com----https://mail.test/first\n"
        "second@example.com----https://mail.test/second\n"
        "third-no-credential@example.com----https://mail.test/third",
    )
    for email in ("first@example.com", "second@example.com"):
        (credential_dir / f"codex-{email}.json").write_text(
            json.dumps(
                {
                    "type": "plus",
                    "email": email,
                    "access_token": f"access-{email}",
                    "refresh_token": f"refresh-{email}",
                    "account_id": "chatgpt-account-1234567890",
                    "id_token": _unsigned_jwt(
                        {
                            "https://api.openai.com/auth": {
                                "chatgpt_account_id": "chatgpt-account-1234567890",
                                "chatgpt_plan_type": "plus",
                                "chatgpt_subscription_active_until": "2026-09-02T05:00:00+00:00",
                            }
                        }
                    ),
                }
            ),
            encoding="utf-8",
        )
    def fake_refresh_metadata(path, *, include_quota):
        credential = json.loads(Path(path).read_text(encoding="utf-8"))
        return {
            "credential": credential,
            "subscription": {
                "status": "ok",
                "checked_at": "2026-08-02T00:00:00+00:00",
                "plan_type": "plus",
                "subscription_active_until": "2026-09-02T05:00:00+00:00",
                "source": "accounts_check",
            },
            "subscription_error": None,
            "quota": (
                {
                    "status": "ok",
                    "checked_at": "2026-08-02T00:00:00+00:00",
                    "plan_type": "plus",
                    "primary": {"remaining_percent": 80},
                    "secondary": {"remaining_percent": 60},
                }
                if include_quota
                else None
            ),
            "quota_error": None,
            "token_refreshed": False,
        }

    monkeypatch.setattr("src.webapp.refresh_codex_credential_metadata", fake_refresh_metadata)
    app = create_app(
        _settings(workspace_path), mailbox_store=mailbox, codex_manager=FakeCodexManager()
    )
    client = app.test_client()

    inventory = client.get("/api/seller/inventory").get_json()
    credential_account = next(row for row in inventory["accounts"] if row["has_credential"])
    assert credential_account["subscription_plan_type"] == "plus"
    assert (
        credential_account["subscription_active_until"]
        == "2026-09-02T05:00:00+00:00"
    )
    assert credential_account["credential_account_hint"] == "chatgpt-…7890"
    assert inventory["summary"] == {
        "total": 3,
        "credential_total": 2,
        "unexported": 3,
        "exported": 0,
        "export_count": 0,
        "quota_ok": 0,
        "phone_verified": 0,
        "phone_unverified": 3,
    }
    account_ids = [row["id"] for row in inventory["accounts"] if row["has_credential"]]
    refreshed = client.post(
        "/api/seller/quota/refresh", json={"account_ids": account_ids}
    ).get_json()
    assert refreshed["success"] == 2
    refreshed_all = client.post(
        "/api/seller/quota/refresh", json={"all": True}
    ).get_json()
    assert refreshed_all["total"] == 2
    assert refreshed_all["success"] == 2
    assert refreshed_all["failed"] == 0
    refreshed_subscriptions = client.post(
        "/api/seller/subscription/refresh", json={"account_ids": account_ids}
    ).get_json()
    assert refreshed_subscriptions["total"] == 2
    assert refreshed_subscriptions["success"] == 2
    assert refreshed_subscriptions["failed"] == 0

    no_credential = next(row for row in inventory["accounts"] if not row["has_credential"])
    blocked_selected = client.post(
        "/api/seller/export",
        json={
            "confirmed": True,
            "account_ids": [no_credential["id"]],
            "format": "sub2api",
        },
    )
    assert blocked_selected.status_code == 409
    selected_original = client.post(
        "/api/seller/export",
        json={
            "confirmed": True,
            "account_ids": [no_credential["id"]],
            "format": "original",
        },
    )
    assert selected_original.status_code == 200
    assert selected_original.headers["X-Exported-Account-Count"] == "1"
    assert selected_original.headers["X-Export-Mode"] == "selected"
    with zipfile.ZipFile(io.BytesIO(selected_original.data)) as archive:
        assert "third-no-credential@example.com" in archive.read("code_url.txt").decode("utf-8")

    exported = client.post(
        "/api/seller/export",
        json={
            "confirmed": True,
            "count": 1,
            "format": "sub2api",
            "export_state": "unexported",
            "phone_state": "all",
        },
    )
    assert exported.status_code == 200
    assert json.loads(exported.data)["accounts"][0]["platform"] == "openai"
    after = client.get("/api/seller/inventory").get_json()
    assert after["summary"]["exported"] == 2
    assert after["summary"]["unexported"] == 1
    assert after["summary"]["export_count"] == 2
    exported_row = next(row for row in after["accounts"] if row["export_count"] == 1)
    assert exported_row["last_exported_at"]

    never_exported = client.post(
        "/api/seller/export",
        json={
            "confirmed": True,
            "all_unexported": True,
            "format": "original",
        },
    )
    assert never_exported.status_code == 200
    assert never_exported.headers["X-Exported-Account-Count"] == "1"
    assert never_exported.headers["X-Export-Mode"] == "all_unexported"
    after_all = client.get("/api/seller/inventory").get_json()
    assert after_all["summary"]["unexported"] == 0
    assert after_all["summary"]["exported"] == 3
    assert after_all["summary"]["export_count"] == 3
    assert (
        client.post(
            "/api/seller/export",
            json={"confirmed": True, "all_unexported": True, "format": "original"},
        ).status_code
        == 409
    )

    multi = client.post(
        "/api/seller/export",
        json={
            "confirmed": True,
            "account_ids": account_ids,
            "formats": ["codex_json", "sub2api"],
        },
    )
    assert multi.status_code == 200
    history_id = multi.headers["X-Export-History-ID"]
    with zipfile.ZipFile(io.BytesIO(multi.data)) as archive:
        assert set(archive.namelist()) == {"codex-json.zip", "sub2api.json"}
        assert json.loads(archive.read("sub2api.json"))["accounts"][0]["platform"] == "openai"
    history = client.get("/api/exports").get_json()["exports"]
    assert history[0]["id"] == history_id
    assert history[0]["formats"] == ["codex_json", "sub2api"]
    assert history[0]["account_count"] == 2
    downloaded = client.get(f"/api/exports/{history_id}/download")
    assert downloaded.status_code == 200
    assert downloaded.data == multi.data

    assert client.post("/api/seller/status", json={}).status_code == 404
    assert not (_settings(workspace_path).project_root / "templates" / "login.html").exists()


def test_seller_export_filters_by_phone_verification_time(workspace_path: Path):
    client, mailbox, _ = _client(workspace_path)
    mailbox.import_text(
        "code_url",
        "early@example.com----https://mail.test/early\n"
        "late@example.com----https://mail.test/late\n"
        "unverified@example.com----https://mail.test/unverified",
    )
    records = json.loads(mailbox.path.read_text(encoding="utf-8"))
    by_email = {row["email"]: row for row in records.values()}
    by_email["early@example.com"].update(
        phone_verified=True,
        phone_verified_at="2026-08-01T02:00:00+00:00",
        phone_number="+10000000001",
    )
    by_email["late@example.com"].update(
        phone_verified=True,
        phone_verified_at="2026-08-03T02:00:00+00:00",
        phone_number="+10000000002",
    )
    mailbox.path.write_text(json.dumps(records), encoding="utf-8")

    inventory = client.get("/api/seller/inventory").get_json()["accounts"]
    assert [row["email"] for row in inventory] == [
        "early@example.com",
        "late@example.com",
        "unverified@example.com",
    ]

    late = client.post(
        "/api/seller/export",
        json={
            "confirmed": True,
            "count": 1,
            "format": "original",
            "export_state": "all",
            "phone_state": "verified",
            "phone_verified_from": "2026-08-03T01:59:00Z",
            "phone_verified_to": "2026-08-03T02:01:00Z",
        },
    )
    assert late.status_code == 200
    with zipfile.ZipFile(io.BytesIO(late.data)) as archive:
        material = archive.read("code_url.txt").decode("utf-8")
        assert "late@example.com" in material
        assert "early@example.com" not in material

    early = client.post(
        "/api/seller/export",
        json={
            "confirmed": True,
            "all_unexported": True,
            "format": "original",
            "phone_state": "verified",
            "phone_verified_from": "2026-08-01T00:00:00Z",
            "phone_verified_to": "2026-08-02T00:00:00Z",
        },
    )
    assert early.status_code == 200
    assert early.headers["X-Exported-Account-Count"] == "1"
    with zipfile.ZipFile(io.BytesIO(early.data)) as archive:
        material = archive.read("code_url.txt").decode("utf-8")
        assert "early@example.com" in material
        assert "unverified@example.com" not in material

    invalid = client.post(
        "/api/seller/export",
        json={
            "confirmed": True,
            "count": 1,
            "format": "original",
            "phone_verified_from": "2026-08-04T00:00:00Z",
            "phone_verified_to": "2026-08-03T00:00:00Z",
        },
    )
    assert invalid.status_code == 400
    assert "开始时间" in invalid.get_json()["error"]


def test_seller_export_filters_by_subscription_plan(workspace_path: Path):
    client, mailbox, _ = _client(workspace_path)
    mailbox.import_text(
        "code_url",
        "plus@example.com----https://mail.test/plus\n"
        "free@example.com----https://mail.test/free",
    )
    credential_dir = workspace_path / "data" / "codex_accounts"
    credential_dir.mkdir(parents=True, exist_ok=True)
    for email, plan in (
        ("plus@example.com", "chatgptplusplan"),
        ("free@example.com", "chatgptfreeplan"),
    ):
        (credential_dir / f"codex-{email}.json").write_text(
            json.dumps(
                {
                    "type": "codex",
                    "email": email,
                    "access_token": f"fixture-{plan}-access",
                    "refresh_token": f"fixture-{plan}-refresh",
                    "plan_type": plan,
                }
            ),
            encoding="utf-8",
        )

    plus = client.post(
        "/api/seller/export",
        json={
            "confirmed": True,
            "count": 1,
            "format": "original",
            "export_state": "all",
            "plan_state": "plus",
        },
    )
    assert plus.status_code == 200
    with zipfile.ZipFile(io.BytesIO(plus.data)) as archive:
        material = archive.read("code_url.txt").decode("utf-8")
        assert "plus@example.com" in material
        assert "free@example.com" not in material

    free = client.post(
        "/api/seller/export",
        json={
            "confirmed": True,
            "count": 1,
            "format": "original",
            "export_state": "all",
            "plan_state": "free",
        },
    )
    assert free.status_code == 200
    with zipfile.ZipFile(io.BytesIO(free.data)) as archive:
        material = archive.read("code_url.txt").decode("utf-8")
        assert "free@example.com" in material
        assert "plus@example.com" not in material

    invalid = client.post(
        "/api/seller/export",
        json={"confirmed": True, "count": 1, "plan_state": "premium"},
    )
    assert invalid.status_code == 400
    assert "套餐筛选" in invalid.get_json()["error"]


def test_timeline_maps_account_email_and_accounts_include_safe_recent_task(
    workspace_path: Path,
):
    client, mailbox, codex = _client(workspace_path)
    mailbox.import_text("generic_api", "owner@example.com----https://mail.test/code")
    mailbox.update_codex(
        "owner@example.com",
        status="failed",
        message="失败：owner@example.com password=mailbox-secret",
    )
    account = mailbox.list_accounts()[0]
    account_id = account["id"]
    log_dir = workspace_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / f"codex-{account_id}-task.log").write_text(
        "2026-07-29 01:00:00,100 [WARNING] [Codex] "
        "add-phone/send 未成功 reason=send_rejected, api_key=secret-key\n",
        encoding="utf-8",
    )
    codex.jobs = [
        {
            "id": "job-safe",
            "email": "owner@example.com",
            "status": "failed",
            "stage": "执行失败",
            "message": "失败：owner@example.com api_key=message-secret",
            "created_at": "2026-07-29T01:00:00+08:00",
            "log_path": "C:\\private\\task.log",
            "credential_path": "C:\\private\\credential.json",
            "callback_url": "http://localhost/callback?code=secret",
        }
    ]

    timeline = client.get("/api/logs/timeline?level=warn&limit=20")
    assert timeline.status_code == 200
    timeline_body = timeline.get_json()
    assert timeline_body["events"][0]["account_id"] == account_id
    assert timeline_body["events"][0]["account_email"] == "owner@example.com"
    assert timeline_body["recent_task"]["id"] == "job-safe"
    assert timeline_body["recent_task"]["email"] == "owner@example.com"
    assert timeline_body["recent_task"]["updated_at"] == "2026-07-29T01:00:00+08:00"
    assert "log_path" not in timeline_body["recent_task"]
    assert "secret-key" not in json.dumps(timeline_body, ensure_ascii=False)

    searched = client.get("/api/logs/timeline?query=OWNER%40example.com")
    assert searched.status_code == 200
    assert searched.get_json()["filtered_events"] == 1

    account_row = client.get("/api/accounts").get_json()["accounts"][0]
    recent = account_row["recent_task"]
    assert recent["id"] == "job-safe"
    assert recent["email"] == "owner@example.com"
    assert recent["updated_at"] == "2026-07-29T01:00:00+08:00"
    assert recent["message"] == "失败：o***@example.com api_key=[REDACTED]"
    assert "log_path" not in recent
    assert "credential_path" not in recent
    assert "callback_url" not in recent
    assert account_row["codex_message"] == "失败：o***@example.com password=[REDACTED]"

    overview = client.get("/api/overview").get_json()
    overview_job = overview["codex_jobs"][0]
    assert overview_job["message"] == "失败：o***@example.com api_key=[REDACTED]"
    assert "log_path" not in overview_job
    assert "credential_path" not in overview_job
    assert "callback_url" not in overview_job


def test_timeline_validates_query_and_pagination(workspace_path: Path):
    client, _, _ = _client(workspace_path)

    assert client.get("/api/logs/timeline?limit=201").status_code == 400
    assert client.get("/api/logs/timeline?offset=bad").status_code == 400
    assert client.get("/api/logs/timeline?level=error").status_code == 400
    assert client.get("/api/logs/timeline?query=" + "x" * 201).status_code == 400


def test_import_then_start_codex(workspace_path: Path):
    client, _, codex = _client(workspace_path)
    imported = client.post(
        "/api/accounts/import",
        json={"source": "generic_api", "text": "owner@example.com----https://mail.test/code"},
    )
    assert imported.status_code == 200
    started = client.post(
        "/api/codex-jobs",
        json={"email": "owner@example.com", "confirmed": True},
    )
    assert started.status_code == 202
    assert codex.jobs[0]["email"] == "owner@example.com"


def test_import_code_url_then_start_codex(workspace_path: Path):
    client, mailbox, codex = _client(workspace_path)
    imported = client.post(
        "/api/accounts/import",
        json={
            "source": "code_url",
            "text": "owner@example.com----https://mail.test/inbox/token",
        },
    )
    assert imported.status_code == 200
    account = client.get("/api/accounts").get_json()["accounts"][0]
    assert account["source"] == "code_url"
    assert account["otp_ready"] is True
    assert "code_url" not in account
    assert mailbox.get_secret(email="owner@example.com")["code_url"].endswith("/token")

    started = client.post(
        "/api/codex-jobs",
        json={"email": "owner@example.com", "confirmed": True},
    )
    assert started.status_code == 202
    assert codex.jobs[0]["email"] == "owner@example.com"


def test_import_icloud_api_key_never_exposes_key_in_public_responses(
    workspace_path: Path,
):
    client, mailbox, _ = _client(workspace_path)
    api_key = "fictional-icloud-api-key-secret"

    imported = client.post(
        "/api/accounts/import",
        json={
            "source": "generic_api",
            "text": f"owner@example.com----{api_key}",
        },
    )

    assert imported.status_code == 200
    public_payloads = [
        imported.get_json(),
        client.get("/api/accounts").get_json(),
        client.get("/api/overview").get_json(),
    ]
    assert all(api_key not in json.dumps(payload) for payload in public_payloads)
    assert api_key in mailbox.get_secret(email="owner@example.com")["code_url"]


def test_import_password_totp_account_without_exposing_secrets(workspace_path: Path):
    client, _, _ = _client(workspace_path)
    response = client.post(
        "/api/accounts/import",
        json={
            "source": "password_totp",
            "text": "owner@example.com|chatgpt-pass|JBSWY3DPEHPK3PXP",
        },
    )

    assert response.status_code == 200
    accounts_response = client.get("/api/accounts")
    serialized = json.dumps(accounts_response.get_json(), ensure_ascii=False)
    assert "password_totp" in serialized
    assert "chatgpt-pass" not in serialized
    assert "JBSWY3DPEHPK3PXP" not in serialized
    assert accounts_response.get_json()["accounts"][0]["otp_ready"] is True


def test_codex_requires_confirmation(workspace_path: Path):
    client, mailbox, _ = _client(workspace_path)
    mailbox.import_text("generic_api", "owner@example.com----https://mail.test/code")
    response = client.post("/api/codex-jobs", json={"email": "owner@example.com"})
    assert response.status_code == 400


def test_pipeline_starts_selected_accounts_with_concurrency_and_retry_settings(workspace_path: Path):
    client, mailbox, codex = _client(workspace_path)
    mailbox.import_text(
        "generic_api",
        "one@example.com----https://mail.test/one\n"
        "two@example.com----https://mail.test/two",
    )
    assert client.post(
        "/api/codex-pipeline",
        json={"emails": ["one@example.com"], "concurrency": 2, "retry_limit": 1},
    ).status_code == 400

    response = client.post(
        "/api/codex-pipeline",
        json={
            "emails": ["one@example.com", "two@example.com"],
            "concurrency": 2,
            "retry_limit": 1,
            "retry_backoff_seconds": 30,
            "confirmed": True,
        },
    )

    assert response.status_code == 202
    body = response.get_json()
    assert body["pipeline"]["total"] == 2
    assert body["pipeline"]["concurrency"] == 2
    assert codex.pipeline["emails"] == ["one@example.com", "two@example.com"]
    resized = client.post("/api/codex-pipeline/pipeline-1/concurrency", json={"concurrency": 6})
    assert resized.status_code == 200
    assert resized.get_json()["pipeline"]["concurrency"] == 6
    assert client.post("/api/codex-pipeline/pipeline-1/concurrency", json={"concurrency": 11}).status_code == 400
    paused = client.post("/api/codex-pipeline/pipeline-1/pause", json={})
    assert paused.status_code == 200
    assert paused.get_json()["pipeline"]["status"] == "paused"
    assert client.post("/api/codex-pipeline/pipeline-1/pause", json={}).status_code == 409
    resumed = client.post("/api/codex-pipeline/pipeline-1/resume", json={})
    assert resumed.status_code == 200
    assert resumed.get_json()["pipeline"]["status"] == "running"
    stopped = client.post("/api/codex-pipeline/pipeline-1/stop", json={})
    assert stopped.status_code == 200
    assert stopped.get_json()["pipeline"]["status"] == "stopping"


def test_active_pipeline_account_cannot_be_deleted(workspace_path: Path):
    client, mailbox, codex = _client(workspace_path)
    mailbox.import_text("generic_api", "owner@example.com----https://mail.test/code")
    account = mailbox.list_accounts()[0]
    codex.jobs.append({"id": "job-1", "email": account["email"], "status": "running"})

    response = client.delete(f"/api/accounts/{account['id']}")

    assert response.status_code == 409
    assert mailbox.get_secret(account_id=account["id"]) is not None


def test_force_pause_endpoint_marks_running_count_failed(workspace_path: Path):
    client, _, codex = _client(workspace_path)
    codex.pipeline.update(
        id="pipeline-1",
        status="running",
        active=True,
        total=2,
        counts={"running": 1, "queued": 1},
    )

    response = client.post("/api/codex-pipeline/pipeline-1/force-pause", json={})

    assert response.status_code == 200
    pipeline = response.get_json()["pipeline"]
    assert pipeline["status"] == "paused"
    assert pipeline["force_paused_count"] == 1
    assert pipeline["counts"] == {"running": 0, "queued": 1, "failed": 1}


def test_health_is_public(workspace_path: Path):
    app = create_app(
        _settings(workspace_path),
        mailbox_store=MailboxStore(workspace_path / "data"),
        codex_manager=FakeCodexManager(),
    )
    response = app.test_client().get("/health")
    assert response.status_code == 200
    assert response.get_json() == {
        "ok": True,
        "service": "codex-auto-sms-receiver",
    }


def test_index_contains_sms_credential_visibility_toggle(workspace_path: Path):
    client, _, _ = _client(workspace_path)
    response = client.get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'id="onboardingGuide"' not in html
    assert 'id="jobsCard"' not in html
    assert 'id="jobs"' not in html
    assert 'id="importLineCount"' in html
    assert 'id="importPanel" data-workspace="accounts" open' in html
    assert 'id="importFeedback"' in html
    assert 'id="openImportFromAccounts"' in html
    assert 'id="pipelineSettings" open' in html
    assert "导入已有账号" in html
    assert '<option value="password_totp">账号密码 + TOTP 2FA</option>' in html
    assert "email|密码|2FA密钥" in html
    assert "不是 iCloud 等邮箱的密码" in html
    assert "动态验证码在本机生成，不读取邮箱" in html
    assert "Hero SMS 接码" in html
    assert "批量处理账号" in html
    assert "账号与任务" in html
    assert "凭证与统计" in html
    assert "运行日志" in html
    assert 'id="credentialSelectedCount"' in html
    assert 'id="sellerRefreshAllQuota"' in html
    assert 'id="sellerRefreshSubscription"' in html
    assert 'id="sellerRefreshAllSubscriptions"' in html
    assert "/api/seller/subscription/refresh" in html
    assert "刷新所选套餐和额度" in html
    assert "刷新全部套餐和额度" in html
    assert 'id="sellerExportAllUnexported"' in html
    assert 'id="sellerUnexportedCount"' in html
    assert 'id="sellerExportedCount"' in html
    assert 'id="sellerCredentialCount"' in html
    assert 'id="sellerExportSelected"' in html
    assert 'id="sellerExportCountTotal"' in html
    assert "已导出 ${exportCount} 次" in html
    assert "每次最多查询 20 个账号额度" not in html
    assert 'class="seller-inventory-table"' in html
    assert "function accountImportTimestamp" in html
    assert "accountImportTimestamp(right)-accountImportTimestamp(left)" in html
    assert '<th>导入时间</th><th>最近任务</th>' in html
    assert 'data-label="导入时间"' in html
    assert "function sellerSubscription" in html
    assert "function sellerDisplayPlan" in html
    assert "credential_expired||''" not in html
    assert "已保留原套餐" in html
    assert "待确认" not in html
    assert "刷新失败" not in html
    assert html.count('class="workspace-icon"') == 5
    assert "function sellerQuotaLabel" in html
    assert "subscription_active_until" in html
    assert "账号库存" in html
    assert "卖家库存" not in html
    assert "已售" not in html
    assert "未售" not in html
    assert 'id="selectAllCredentials"' in html
    assert 'id="clearCredentialSelection"' in html
    assert 'id="downloadSelectedCredentials"' in html
    assert "/api/artifacts/credentials/selected/export" in html
    assert 'data-credential-export-format value="sub2api"' in html
    assert 'data-seller-export-format value="sub2api"' in html
    assert 'id="selectFilteredAccounts"' in html
    assert 'id="selectAllManageAccounts"' in html
    assert 'id="accountPhoneVerifiedFrom"' in html
    assert 'id="accountPhoneVerifiedTo"' in html
    assert 'id="clearAccountTimeFilter"' in html
    assert "function accountMatchesPhoneTime" in html
    assert 'id="sellerPhoneVerifiedFrom"' in html
    assert 'id="sellerPhoneVerifiedTo"' in html
    assert 'id="clearSellerPhoneTime"' in html
    assert "function sellerMatchesPhoneTime" in html
    assert 'data-seller-time-preset="today"' in html
    assert 'data-seller-time-preset="7d"' in html
    assert 'data-seller-time-preset="custom">自定义' in html
    assert '<option value="verified" selected>已经接码</option>' in html
    assert "setSellerTimePreset('all');switchWorkspace('accounts')" in html
    assert 'class="active" data-seller-time-preset="all"' in html
    assert "sellerTimePreset='all'" in html
    assert "if(preset==='custom'){start=new Date(today);end=new Date(now)}" in html
    assert '<strong id="sellerDateRangeLabel">全部接码时间</strong>' in html
    assert "结束时间，默认当前时间" in html
    assert 'data-seller-time-preset="30m"' in html
    assert 'data-seller-time-preset="1h"' in html
    assert 'data-seller-time-preset="6h"' in html
    assert 'data-seller-time-preset="24h"' in html
    assert 'id="sellerPhoneVerifiedFrom" type="datetime-local" step="60"' in html
    assert 'id="sellerPhoneVerifiedTo" type="datetime-local" step="60"' in html
    assert "function sellerLocalMinuteValue" in html
    assert 'id="sellerPlanFilter"' in html
    assert '<option value="plus">Plus</option>' in html
    assert "function sellerPlanType" in html
    assert "function normalizeSellerPlan" in html
    assert "chatgptplusplan:'plus'" in html
    assert "plan_state:planState" in html
    assert "seller-phone-time-cell" in html
    assert "phoneHeader.textContent='接码时间'" in html
    assert 'id="sellerRangeSelect"' in html
    assert 'id="sellerRangeHint"' in html
    assert "function applySellerVisibleRange" in html
    assert "event.shiftKey" in html
    assert 'id="sellerOpenDateRange"' in html
    assert 'id="sellerDateRangeDialog"' in html
    assert 'id="sellerCalendarDays"' in html
    assert "function renderSellerCalendar" in html
    assert "showModal()" in html
    assert 'data-seller-quick-select="10"' in html
    assert 'data-seller-quick-select="20"' in html
    assert 'data-seller-quick-select="50"' in html
    assert "function quickSelectSellerAccounts" in html
    assert 'id="sellerSelectFiltered"' in html
    assert 'id="sellerSelectAll"' in html
    assert 'id="exportHistory"' in html
    assert 'id="forcePausePipeline"' in html
    assert 'id="exportSelectedAccounts"' in html
    assert 'id="deleteSelectedAccounts"' in html
    assert 'id="deleteSelectedCredentials"' in html
    assert 'id="selectFailedForPipeline"' in html
    assert '<option value="otp_failed">取码失败</option>' in html
    assert '<option value="expiring">凭证即将到期</option>' in html
    assert "managedAccountIds=new Set()" in html
    assert "data-account-manage" in html
    assert "data-pipeline-toggle" in html
    assert "function pipelineTiming" in html
    assert "勾选起点后按住 Shift 勾选终点，可连续选择多个账号" in html
    assert "function applyAccountVisibleRange" in html
    assert "accountManageShiftPressed" in html
    assert "event.shiftKey" in html
    assert 'value="phone_unverified"' in html
    assert 'value="filtered"' in html
    assert 'value="batch"' in html
    assert 'id="pipelineScopePreview"' in html
    assert 'data-pipeline-scope="selected"' in html
    assert 'id="sellerSort"' in html
    assert "function sellerSortTime" in html
    assert "不执行注册" not in html
    assert html.index('id="importPanel"') < html.index('id="pipelineCard"')
    assert html.index('id="pipelineCard"') < html.index('id="accounts"')
    assert html.count('data-workspace="accounts"') == 3
    assert response.data.count(b'id="materials"') == 1
    assert response.data.count(b'id="startPipeline"') == 1
    element_ids = re.findall(r'\bid="([^"]+)"', html)
    assert len(element_ids) == len(set(element_ids))
    assert b'id="toggleSmsCredential"' in response.data
    assert "Hero SMS API Key" in response.get_data(as_text=True)
    assert ".hero-provider .hero-service-icon{color:#05251d" in html
    assert b'id="heroCountrySearch"' in response.data
    assert b'id="heroCountryOptions"' in response.data
    assert b'id="heroCountrySelect"' not in response.data
    assert b'role="combobox"' in response.data
    assert b'aria-autocomplete="list"' in response.data
    assert b'aria-haspopup="listbox"' in response.data
    assert "输入中文或英文国名" in response.get_data(as_text=True)
    assert b'matchingHeroCountries' in response.data
    assert b'handleHeroCountrySearchKey' in response.data
    assert b'id="providerOrder"' not in response.data
    assert b'GrizzlySMS' not in response.data
    assert b'id="lApiBase"' not in response.data
    assert b'id="hApiBase"' not in response.data
    assert b'id="heroMinPrice"' not in response.data
    assert b'id="heroMaxPrice"' in response.data
    assert b'id="heroPreferredPrice"' not in response.data
    assert b'id="heroAcquirePriority"' not in response.data
    assert b'id="queryHeroBalance"' in response.data
    assert b'id="queryHeroOffers"' in response.data
    assert b'/api/hero-sms/prices' in response.data
    assert b'id="heroQueuePreview"' in response.data
    assert b'data-country-drag=' in response.data
    assert b'draggable="true"' in response.data
    assert b'reorderHeroCountry' in response.data
    assert "拖动整条卡片可排序" in html
    assert "data-drag-label=\"正在拖动：${esc(name)}\"" in html
    assert "松开放到这里" in html
    assert "const row=event.target.closest('[data-country-row]')" in html
    assert ".hero-selected-actions,button,input,select,textarea,a" in html
    assert b'<details class="hero-advanced" open>' in response.data
    assert b'id="heroMaxPrice" type="number" min="0" step="0.0001" inputmode="decimal" value="0.11"' in response.data
    assert b'id="smsMaxRetries" type="number" min="1" max="50" value="10"' in response.data
    assert b'id="smsCodeWait" type="number" min="30" max="600" value="30"' in response.data
    assert "国家优先队列" in response.get_data(as_text=True)
    assert "前一个国家无号时，会自动尝试下一个" in response.get_data(as_text=True)
    assert "保存并使用此优先队列" in response.get_data(as_text=True)
    assert "选择服务" not in response.get_data(as_text=True)
    assert b'id="runtimeConfig"' not in response.data
    assert b"acquire_priority:'country'" in response.data
    assert b'data-view="accounts"' in response.data
    assert b'data-view="sms"' in response.data
    assert b'data-view="results"' in response.data
    assert b'data-view="logs"' in response.data
    assert b'data-view="accounts" aria-current="page"' in response.data
    assert "<span>⌂</span>" not in html
    assert "<span>◆</span>" not in html
    assert "<span>↓</span>" not in html
    assert "<span>≡</span>" not in html
    assert b'id="pipelineScope"' in response.data
    assert b'id="pipelineConcurrency"' in response.data
    assert b'id="pipelineConcurrencyHint"' in response.data
    assert b'id="pipelineAccountList"' in response.data
    assert b'id="excludeRiskyAccounts"' in response.data
    assert b'id="restoreExcludedAccounts"' in response.data
    assert b'max="10" value="3"' in response.data
    assert b"function accountRiskProfile" in response.data
    assert b"pipelineExcludedAccountIds" in response.data
    assert b'/concurrency`' in response.data
    assert b"PIPELINE_SETTINGS_KEY='codex-pipeline-settings-v1'" in response.data
    assert b"loadPipelineSettings();updateImportHelper()" in response.data
    assert b"pipelineState.concurrency)||1)));$('#pipelineRetryLimit').value" not in response.data
    assert b'id="pipelineRetryLimit"' in response.data
    assert b'id="startPipeline"' in response.data
    assert b'id="pausePipeline"' in response.data
    assert b'id="resumePipeline"' in response.data
    assert b'id="stopPipeline"' in response.data
    assert b'/api/codex-pipeline' in response.data
    assert b'id="refreshAll"' not in response.data
    assert b'action="/logout"' not in response.data
    assert b'data-codex=' not in response.data
    assert b'id="downloadAllCredentials"' in response.data
    assert b'id="downloadAllLogs"' in response.data
    assert b'id="logFileSelect"' in response.data
    assert b'id="timelineRecords"' in response.data
    assert b'id="timelineCountImportant"' in response.data
    assert b'data-timeline-level="important"' in response.data
    assert "失败/错误" in response.get_data(as_text=True)
    assert b"/api/logs/timeline" in response.data
    assert b"limit:'30'" in response.data
    assert b'id="rawLogExplorer"' in response.data
    assert b'id="logAutoRefresh"' not in response.data
    assert b'id="logFileSearch"' not in response.data
    assert b'id="logSearch"' in response.data
    assert b'id="logLevel"' in response.data
    assert b'<option value="problem">\xe5\x8f\xaa\xe7\x9c\x8b\xe9\x94\x99\xe8\xaf\xaf</option>' in response.data
    assert b'id="logOrder"' not in response.data
    assert b'id="logPageSize"' not in response.data
    assert b'id="logDisplayMode"' not in response.data
    assert b'id="clearLogSearch"' in response.data
    assert b'id="logRecords"' in response.data
    assert b'id="previousLogPage"' in response.data
    assert b'id="nextLogPage"' in response.data
    assert b'id="downloadSelectedLog"' in response.data
    assert b'data-view-log=' not in response.data
    assert b'/api/artifacts/logs/' in response.data
    assert b'id="smsStatsCard"' in response.data
    assert b'id="smsStatsOverview"' in response.data
    assert b'id="smsCountryStats"' in response.data
    assert b'id="smsNumberRecords"' in response.data
    assert b'id="refreshSmsStats"' not in response.data
    assert b'/api/artifacts/sms-stats' in response.data
    assert "接码统计" in response.get_data(as_text=True)
    assert "按国家统计" in response.get_data(as_text=True)
    assert "完整号码" in response.get_data(as_text=True)
    assert b'scope="col"' in response.data
    assert "时间、状态和内容" in response.get_data(as_text=True)
    assert "OpenTelemetry" not in response.get_data(as_text=True)
    assert "SeverityNumber" not in response.get_data(as_text=True)
    assert b"limit:'100'" in response.data
    assert b"order:'desc'" in response.data


def test_hero_catalog_api_returns_named_countries(workspace_path: Path):
    class FakeCatalog:
        def catalog(self):
            return {
                "source": "live",
                "updated_at": "2026-07-28T00:00:00+00:00",
                "service": {"code": "dr", "name": "OpenAI"},
                "countries": [
                    {
                        "id": "52",
                        "name": "泰国",
                        "name_en": "Thailand",
                        "flag": "🇹🇭",
                        "popular": True,
                    }
                ],
            }

    mailbox = MailboxStore(workspace_path / "data")
    app = create_app(
        _settings(workspace_path),
        mailbox_store=mailbox,
        codex_manager=FakeCodexManager(),
        hero_catalog=FakeCatalog(),
    )
    client = app.test_client()

    response = client.get("/api/hero-sms/catalog")
    assert response.status_code == 200
    body = response.get_json()
    assert body["countries"][0]["name"] == "泰国"
    assert body["countries"][0]["id"] == "52"


def test_hero_balance_and_filtered_price_endpoints_use_saved_backend_key(workspace_path: Path):
    class FakeSmsStore:
        def snapshot(self):
            return {
                "countries": ["33", "187"],
                "country": "33",
                "min_price": "0.05",
                "max_price": "0.10",
                "preferred_price": "0.075",
                "acquire_priority": "country",
            }

        def reveal_credential(self, provider):
            assert provider == "hero"
            return "backend-only-key"

    class FakePricing:
        def balance(self):
            return {"amount": "9.5"}

        def prices(self, countries):
            assert countries == ["33", "187"]
            return [
                {
                    "country": "33",
                    "tiers": [
                        {"price": "0.04", "stock": 3, "available": True},
                        {"price": "0.08", "stock": 2, "available": True},
                    ],
                },
                {
                    "country": "187",
                    "tiers": [{"price": "0.12", "stock": 4, "available": True}],
                },
            ]

    class FakeCatalog:
        def catalog(self):
            return {
                "source": "live",
                "countries": [
                    {"id": "33", "name": "哥伦比亚", "name_en": "Colombia", "flag": "🇨🇴"},
                    {"id": "187", "name": "美国", "name_en": "United States", "flag": "🇺🇸"},
                ],
            }

    app = create_app(
        _settings(workspace_path),
        mailbox_store=MailboxStore(workspace_path / "data"),
        codex_manager=FakeCodexManager(),
        sms_config_store=FakeSmsStore(),
        hero_catalog=FakeCatalog(),
        hero_pricing=FakePricing(),
    )
    client = app.test_client()

    balance = client.get("/api/hero-sms/balance")
    assert balance.status_code == 200
    assert balance.get_json()["balance"] == {"amount": "9.5"}
    assert "backend-only-key" not in balance.get_data(as_text=True)

    prices = client.post(
        "/api/hero-sms/prices",
        json={
            "countries": ["33", "187"],
            "min_price": "0.06",
            "max_price": "0.09",
            "preferred_price": "0.075",
            "acquire_priority": "price",
        },
    )
    assert prices.status_code == 200
    body = prices.get_json()
    assert body["filters"]["min_price"] == "0.06"
    assert body["filters"]["max_price"] == "0.09"
    assert body["filters"]["acquire_priority"] == "price"
    assert body["countries"][0]["tiers"][0]["eligible"] is False
    assert body["countries"][0]["tiers"][1]["eligible"] is True
    assert body["countries"][1]["available_in_range"] is False
    assert "backend-only-key" not in prices.get_data(as_text=True)


def test_artifact_listing_and_confirmed_downloads(workspace_path: Path):
    data_dir = workspace_path / "data"
    credential_dir = data_dir / "codex_accounts"
    log_dir = workspace_path / "logs"
    credential_dir.mkdir(parents=True)
    log_dir.mkdir(parents=True)
    credential = credential_dir / "codex-owner@example.com.json"
    credential.write_text(
        json.dumps(
            {
                "type": "codex",
                "email": "owner@example.com",
                "refresh_token": "download-secret",
            }
        ),
        encoding="utf-8",
    )
    (log_dir / "codex-run.log").write_text("oauth log", encoding="utf-8")
    store = ArtifactStore(data_dir, log_dir)
    mailbox = MailboxStore(data_dir)
    mailbox.import_text("generic_api", "owner@example.com----https://mail.test/code")
    app = create_app(
        _settings(workspace_path),
        mailbox_store=mailbox,
        codex_manager=FakeCodexManager(),
        artifact_store=store,
    )
    client = app.test_client()

    listing = client.get("/api/artifacts")
    assert listing.status_code == 200
    body = listing.get_json()
    assert body["counts"]["credentials"] == 1
    assert "download-secret" not in listing.get_data(as_text=True)
    credential_id = body["credentials"][0]["id"]
    log_id = body["logs"][0]["id"]

    account = client.get("/api/accounts").get_json()["accounts"][0]
    assert account["has_credential"] is True
    assert account["credential_id"] == credential_id
    assert "download-secret" not in json.dumps(account)
    account_download_path = f"/api/accounts/{account['id']}/credential/download"
    assert client.get(account_download_path).status_code == 400
    account_download = client.get(account_download_path + "?confirmed=1")
    assert account_download.status_code == 200
    assert b"download-secret" in account_download.data

    assert client.get(f"/api/artifacts/credentials/{credential_id}/download").status_code == 400
    downloaded = client.get(
        f"/api/artifacts/credentials/{credential_id}/download?confirmed=1"
    )
    assert downloaded.status_code == 200
    assert b"download-secret" in downloaded.data
    assert "attachment" in downloaded.headers["Content-Disposition"]
    assert client.get("/api/artifacts/credentials/../../mailboxes.json/download?confirmed=1").status_code == 404

    assert client.get(f"/api/artifacts/logs/{log_id}/download").status_code == 400
    log_download = client.get(f"/api/artifacts/logs/{log_id}/download?confirmed=1")
    assert log_download.status_code == 200
    assert log_download.data == b"oauth log"

    archive = client.get("/api/artifacts/credentials.zip?confirmed=1")
    assert archive.status_code == 200
    with zipfile.ZipFile(io.BytesIO(archive.data)) as zipped:
        assert zipped.namelist() == ["codex-owner@example.com.json"]
        assert b"download-secret" in zipped.read("codex-owner@example.com.json")


def test_selected_credential_archive_accepts_only_exportable_ids(workspace_path: Path):
    data_dir = workspace_path / "data"
    credential_dir = data_dir / "codex_accounts"
    credential_dir.mkdir(parents=True)
    credentials = {
        "codex-first@example.com.json": {
            "type": "codex",
            "email": "first@example.com",
            "access_token": "first-secret",
            "id_token": _unsigned_jwt(
                {
                    "https://api.openai.com/auth": {
                        "chatgpt_plan_type": "plus",
                        "chatgpt_subscription_active_until": "2026-09-01T20:56:30+00:00",
                    }
                }
            ),
            "expired": "2026-08-11T21:06:39Z",
        },
        "codex-second@example.com.json": {
            "type": "codex",
            "email": "second@example.com",
            "refresh_token": "second-secret",
        },
        "codex-receipt.json": {
            "type": "codex_cpa_callback",
            "email": "receipt@example.com",
            "code": "not-an-oauth-credential",
        },
    }
    for name, payload in credentials.items():
        (credential_dir / name).write_text(json.dumps(payload), encoding="utf-8")

    mailbox = MailboxStore(data_dir)
    mailbox.import_text("generic_api", "first@example.com----https://mail.test/first")
    mailbox.import_text("generic_api", "second@example.com----https://mail.test/second")
    store = ArtifactStore(data_dir, workspace_path / "logs")
    app = create_app(
        _settings(workspace_path),
        mailbox_store=mailbox,
        codex_manager=FakeCodexManager(),
        artifact_store=store,
    )
    client = app.test_client()
    listed = client.get("/api/artifacts").get_json()["credentials"]
    by_email = {item["email"]: item for item in listed}
    account_by_email = {
        item["email"]: item for item in client.get("/api/accounts").get_json()["accounts"]
    }
    endpoint = "/api/artifacts/credentials/selected.zip"

    assert client.post(endpoint, json={}).status_code == 400
    assert (
        client.post(
            endpoint,
            json={
                "confirmed": True,
                "credential_ids": [by_email["first@example.com"]["id"]],
            },
        ).status_code
        == 200
    )

    selected = client.post(
        endpoint,
        json={
            "confirmed": True,
            "credential_ids": [by_email["first@example.com"]["id"]],
            "account_ids": [
                account_by_email["first@example.com"]["id"],
                account_by_email["second@example.com"]["id"],
            ],
        },
    )
    assert selected.status_code == 200
    assert "codex-selected-credentials.zip" in selected.headers["Content-Disposition"]
    with zipfile.ZipFile(io.BytesIO(selected.data)) as zipped:
        assert sorted(zipped.namelist()) == [
            "codex-first@example.com.json",
            "codex-second@example.com.json",
        ]
        assert b"first-secret" in zipped.read("codex-first@example.com.json")
        assert b"second-secret" in zipped.read("codex-second@example.com.json")

    assert (
        client.post(
            endpoint,
            json={"confirmed": True, "credential_ids": [by_email["receipt@example.com"]["id"]]},
        ).status_code
        == 404
    )
    assert (
        client.post(
            endpoint,
            json={"confirmed": True, "credential_ids": ["../../mailboxes.json"]},
        ).status_code
        == 404
    )
    assert (
        client.post(
            endpoint,
            json={
                "confirmed": True,
                "credential_ids": [by_email["first@example.com"]["id"]] * 101,
            },
        ).status_code
        == 413
    )

    sub2api = client.post(
        "/api/artifacts/credentials/selected/export",
        json={
            "confirmed": True,
            "format": "sub2api",
            "credential_ids": [by_email["first@example.com"]["id"]],
        },
    )
    assert sub2api.status_code == 200
    payload = json.loads(sub2api.data)
    assert payload["type"] == "sub2api-data"
    assert payload["version"] == 1
    assert payload["proxies"] == []
    assert payload["accounts"][0]["platform"] == "openai"
    assert payload["accounts"][0]["type"] == "oauth"
    assert payload["accounts"][0]["credentials"]["access_token"] == "first-secret"
    assert payload["accounts"][0]["credentials"]["plan_type"] == "plus"
    assert (
        payload["accounts"][0]["credentials"]["expires_at"]
        == "2026-09-01T20:56:30+00:00"
    )

    removed = client.delete(
        "/api/artifacts/credentials/selected",
        json={
            "confirmed": True,
            "credential_ids": [by_email["first@example.com"]["id"]],
        },
    )
    assert removed.status_code == 200
    assert removed.get_json()["deleted"] == 1
    assert by_email["first@example.com"]["id"] not in {
        item["id"] for item in client.get("/api/artifacts").get_json()["credentials"]
    }


def test_account_management_exports_original_formats_and_batch_deletes(workspace_path: Path):
    client, mailbox, _ = _client(workspace_path)
    outlook = "mail@example.com====mail-pass====client-id====refresh-token"
    code_url = "code@example.com----https://mail.test/code"
    mailbox.import_text("outlook", outlook)
    mailbox.import_text("code_url", code_url)
    accounts = client.get("/api/accounts").get_json()["accounts"]
    account_ids = [item["id"] for item in accounts]

    exported = client.post(
        "/api/accounts/selected/export",
        json={"confirmed": True, "account_ids": account_ids},
    )
    assert exported.status_code == 200
    with zipfile.ZipFile(io.BytesIO(exported.data)) as archive:
        assert set(archive.namelist()) == {"outlook.txt", "code_url.txt", "README.txt"}
        assert archive.read("outlook.txt").decode("utf-8").strip() == outlook
        assert archive.read("code_url.txt").decode("utf-8").strip() == code_url

    deleted = client.delete(
        "/api/accounts/selected",
        json={"confirmed": True, "account_ids": account_ids},
    )
    assert deleted.status_code == 200
    assert deleted.get_json()["deleted"] == 2
    assert client.get("/api/accounts").get_json()["accounts"] == []


def test_log_content_api_is_structured_paginated_and_redacted(workspace_path: Path):
    data_dir = workspace_path / "data"
    log_dir = workspace_path / "logs"
    log_dir.mkdir(parents=True)
    secret = "viewer-secret-token"
    (log_dir / "codex-view.log").write_text(
        "2026-07-28 21:00:00,100 [INFO] [Codex] started\n"
        f"2026-07-28 21:00:01,100 [ERROR] [Codex] access_token={secret}\n",
        encoding="utf-8",
    )
    store = ArtifactStore(data_dir, log_dir)
    app = create_app(
        _settings(workspace_path),
        mailbox_store=MailboxStore(data_dir),
        codex_manager=FakeCodexManager(),
        artifact_store=store,
    )
    client = app.test_client()
    log_id = client.get("/api/artifacts").get_json()["logs"][0]["id"]

    response = client.get(
        f"/api/artifacts/logs/{log_id}/content?limit=1&order=asc&level=info"
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["schema"]["name"] == "OpenTelemetry LogRecord"
    assert body["filtered_events"] == 1
    assert body["events"][0]["severity_text"] == "INFO"
    assert body["events"][0]["instrumentation_scope"]["name"] == "Codex"
    assert body["log"]["redacted"] is True
    assert secret not in response.get_data(as_text=True)

    first_page = client.get(
        f"/api/artifacts/logs/{log_id}/content?limit=1&order=asc&level=all"
    ).get_json()
    assert first_page["has_more"] is True
    assert first_page["next_offset"] == 1
    second_page = client.get(
        f"/api/artifacts/logs/{log_id}/content?offset=1&limit=1&order=asc&level=all"
    ).get_json()
    assert second_page["events"][0]["severity_text"] == "ERROR"

    searched = client.get(
        f"/api/artifacts/logs/{log_id}/content?q=access&level=error"
    ).get_json()
    assert searched["filtered_events"] == 1
    assert searched["events"][0]["severity_text"] == "ERROR"
    assert secret not in json.dumps(searched)
    assert client.get(f"/api/artifacts/logs/{log_id}/content?level=verbose").status_code == 400
    assert client.get("/api/artifacts/logs/" + "0" * 24 + "/content").status_code == 404


def test_webui_rejects_non_loopback_host(workspace_path: Path):
    settings = _settings(workspace_path)
    settings = Settings(
        project_root=settings.project_root,
        data_dir=settings.data_dir,
        log_dir=settings.log_dir,
        browser_executable=settings.browser_executable,
        browser_timeout_seconds=settings.browser_timeout_seconds,
        host="0.0.0.0",
        port=settings.port,
    )
    with pytest.raises(ValueError, match="回环地址"):
        create_app(
            settings,
            mailbox_store=MailboxStore(workspace_path / "data"),
            codex_manager=FakeCodexManager(),
        )


def test_sms_statistics_api_adds_country_names_and_keeps_auth_secrets_private(workspace_path: Path):
    class FakeArtifactStore:
        def sms_statistics(self):
            return {
                "generated_at": "2026-07-29T00:00:00+00:00",
                "price_note": "价格为 Hero SMS 取号报价，不代表最终实际扣费。",
                "success_rate_note": "成功率 = 验证通过号码数 / 已取号码数。",
                "summary": {
                    "numbers_acquired": 1,
                    "sms_sent": 1,
                    "codes_received": 0,
                    "verified": 0,
                    "failed": 1,
                    "pending": 0,
                    "success_rate": 0.0,
                    "priced_numbers": 1,
                    "quoted_total": "0.275",
                    "quoted_average": "0.275",
                },
                "countries": [{"country_id": "10", "numbers_acquired": 1}],
                "records": [
                    {
                        "id": "opaque-row-id",
                        "country_id": "10",
                        "phone_number": "+84987650644",
                        "price": "0.275",
                        "status": "rate_limited",
                        "status_label": "请求过多",
                    }
                ],
            }

        def overview(self):
            return {"credentials": [], "logs": [], "counts": {}}

    class FakeCatalog:
        def catalog(self):
            return {
                "countries": [
                    {"id": "10", "name": "越南", "name_en": "Vietnam", "flag": "🇻🇳"}
                ]
            }

    app = create_app(
        _settings(workspace_path),
        mailbox_store=MailboxStore(workspace_path / "data"),
        codex_manager=FakeCodexManager(),
        artifact_store=FakeArtifactStore(),
        hero_catalog=FakeCatalog(),
    )
    client = app.test_client()

    response = client.get("/api/artifacts/sms-stats")

    assert response.status_code == 200
    body = response.get_json()
    assert body["countries"][0]["name"] == "越南"
    assert body["records"][0]["flag"] == "🇻🇳"
    assert body["records"][0]["phone_number"] == "+84987650644"
    serialized = response.get_data(as_text=True)
    assert "activation_id" not in serialized
    assert "123456" not in serialized


def test_webui_allows_ipv6_loopback_host(workspace_path: Path):
    settings = _settings(workspace_path)
    settings = Settings(
        project_root=settings.project_root,
        data_dir=settings.data_dir,
        log_dir=settings.log_dir,
        browser_executable=settings.browser_executable,
        browser_timeout_seconds=settings.browser_timeout_seconds,
        host="::1",
        port=settings.port,
    )
    app = create_app(
        settings,
        mailbox_store=MailboxStore(workspace_path / "data"),
        codex_manager=FakeCodexManager(),
    )
    client = app.test_client()
    assert client.get("/").status_code == 200


def test_sms_config_can_be_saved_without_returning_secret(workspace_path: Path, monkeypatch):
    for key in (
        "SMS_PROVIDER",
        "SMS_API_KEY",
        "HERO_SMS_API_KEY",
        "SMS_COUNTRY",
        "SMS_SERVICE",
        "SMS_MAX_RETRIES",
        "SMS_CODE_WAIT",
        "L_API_BASE",
        "L_ADMIN_AUTH_CODE",
        "L_PHONE_PREFIX",
        "H_API_BASE",
        "H_ADMIN_AUTH_CODE",
        "H_PHONE_PREFIX",
        "H_PHONE_ACQUIRE_MODE",
        "HERO_SMS_COUNTRIES",
        "HERO_SMS_MIN_PRICE",
        "HERO_SMS_MAX_PRICE",
        "HERO_SMS_PREFERRED_PRICE",
        "HERO_SMS_ACQUIRE_PRIORITY",
        "HERO_SMS_REUSE_ENABLED",
    ):
        monkeypatch.delenv(key, raising=False)
    mailbox = MailboxStore(workspace_path / "data")
    codex = FakeCodexManager()
    sms_store = SmsConfigStore(workspace_path / ".env")
    app = create_app(
        _settings(workspace_path),
        mailbox_store=mailbox,
        codex_manager=codex,
        sms_config_store=sms_store,
    )
    client = app.test_client()

    response = client.post(
        "/api/sms-config",
        json={
            "provider": "hero",
            "countries": ["187", "33"],
            "service": "dr",
            "min_price": "0.05",
            "max_price": "0.10",
            "preferred_price": "0.075",
            "acquire_priority": "price",
            "max_retries": 10,
            "code_wait": 120,
            "credential": "hero-secret",
            "clear_credential": False,
        },
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["config"]["credentials_configured"] == {"hero": True}
    assert body["config"]["countries"] == ["187", "33"]
    assert "hero-secret" not in response.get_data(as_text=True)
    values = dotenv_values(workspace_path / ".env")
    assert values["HERO_SMS_API_KEY"] == "hero-secret"
    assert values["SMS_PROVIDER"] == "hero"
    assert values["SMS_SERVICE"] == "dr"

    hidden = client.get("/api/sms-config")
    assert "hero-secret" not in hidden.get_data(as_text=True)
    revealed = client.post(
        "/api/sms-config/reveal",
        json={"confirmed": True},
    )
    assert revealed.status_code == 200
    assert revealed.get_json()["credential"] == "hero-secret"


def test_sms_config_rejects_non_hero_provider(workspace_path: Path):
    client, _, _ = _client(workspace_path)
    response = client.post(
        "/api/sms-config",
        json={"provider": "unsupported", "countries": ["33"]},
    )
    assert response.status_code == 400
    assert "Hero SMS" in response.get_json()["error"]
