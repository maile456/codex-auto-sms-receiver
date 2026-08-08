from __future__ import annotations

import json
import logging
import multiprocessing
from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest

import manager.sms_bootstrap as sms_bootstrap
from manager.sms_bootstrap import install_managed_sms_runtime
from manager.sms_runtime_overlay import SmartSmsStop, install_codex_sms_overlay
from src import artifact_store, codex_worker, upstream_bridge
from src.codex_service import CodexJobManager
from src.hero_sms import install_hero_sms_patch
from src.mailbox_store import MailboxStore
from src.settings import Settings
from manager_app import create_managed_app


ROOT = Path(__file__).resolve().parents[1]


class _HeroTerminalResponse:
    def __init__(self, title: str):
        self.status_code = 200
        self.text = title
        self._title = title

    def json(self):
        return {"title": self._title}


class _CountingHeroHttp:
    def __init__(self, title: str):
        self.title = title
        self.calls: list[dict] = []
        self.closed = 0

    def get(self, url, params):
        self.calls.append(
            {
                key: "[SET]" if key == "api_key" else value
                for key, value in dict(params).items()
            }
        )
        return _HeroTerminalResponse(self.title)

    def close(self) -> None:
        self.closed += 1


class _AccountApiManager:
    @staticmethod
    def list_jobs():
        return []

    @staticmethod
    def availability():
        return {"available": True, "reason": ""}

    @staticmethod
    def runtime_config():
        return {}


def _spawn_runtime_probe(result_queue, root: str) -> None:
    runpy.run_path(str(Path(root) / "manager_app.py"), run_name="__mp_main__")
    from src import codex_worker as spawned_worker
    from src import upstream_bridge as spawned_bridge

    result_queue.put(
        {
            "same_run": (
                spawned_worker.run_codex_only
                is spawned_bridge.run_codex_only
            ),
            "run_module": spawned_worker.run_codex_only.__module__,
            "safe_result_module": spawned_worker._safe_result.__module__,
        }
    )


def test_managed_runtime_wraps_worker_phone_jobs_and_restores(monkeypatch) -> None:
    events: list[str] = []
    codex = SimpleNamespace()

    def fake_original_run(settings, mailbox, *, reauth=False):
        events.append("run")
        codex._manager_phone_verified = True
        return {"ok": True}

    monkeypatch.setattr(
        upstream_bridge,
        "_ensure_upstream_imports",
        lambda settings: codex,
    )
    monkeypatch.setattr(upstream_bridge, "run_codex_only", fake_original_run)
    monkeypatch.setattr(codex_worker, "run_codex_only", fake_original_run)
    monkeypatch.setattr(
        "manager.sms_bootstrap.install_codex_sms_overlay",
        lambda module: SimpleNamespace(
            restore=lambda: events.append("restore"),
        ),
    )

    installation = install_managed_sms_runtime()
    try:
        assert upstream_bridge.run_codex_only is not fake_original_run
        assert codex_worker.run_codex_only is upstream_bridge.run_codex_only
        result = codex_worker.run_codex_only(
            object(),
            {"email": "owner@example.com"},
        )
    finally:
        installation.restore()

    assert result == {"ok": True, "phone_verified": True}
    assert events == ["run", "restore"]
    assert upstream_bridge.run_codex_only is fake_original_run
    assert codex_worker.run_codex_only is fake_original_run


def test_reauth_job_skips_phone_overlay(monkeypatch) -> None:
    calls: list[str] = []

    def fake_original_run(settings, mailbox, *, reauth=False):
        calls.append(f"run:{reauth}")
        return {"ok": True}

    monkeypatch.setattr(upstream_bridge, "run_codex_only", fake_original_run)
    monkeypatch.setattr(codex_worker, "run_codex_only", fake_original_run)
    monkeypatch.setattr(
        upstream_bridge,
        "_ensure_upstream_imports",
        lambda settings: pytest.fail("reauth must not load the phone overlay"),
    )

    installation = install_managed_sms_runtime()
    try:
        result = upstream_bridge.run_codex_only(
            object(),
            {"email": "owner@example.com"},
            reauth=True,
        )
    finally:
        installation.restore()

    assert result == {"ok": True}
    assert calls == ["run:True"]


def test_phone_terminal_codes_are_not_scheduler_retryable() -> None:
    installation = install_managed_sms_runtime()
    try:
        assert CodexJobManager._failure_info(
            "[smart_sms:fraud_guard] stopped"
        ) == ("fraud_guard", False, 0)
        assert CodexJobManager._failure_info(
            "[smart_sms:phone_rate_limited] stopped"
        ) == ("phone_rate_limited", False, 0)
        assert CodexJobManager._failure_info(
            "[smart_sms:sms_no_numbers] stopped"
        ) == ("sms_no_numbers", False, 0)
        assert CodexJobManager._failure_info(
            "invalid_request_error rate_limit_exceeded"
        ) == ("rate_limited", True, 180)
    finally:
        installation.restore()


def test_worker_and_scheduler_preserve_non_sensitive_verified_flag(tmp_path) -> None:
    store = MailboxStore(tmp_path / "data")
    store.import_text(
        "generic_api",
        "owner@example.com----https://mail.test/code",
    )
    settings = SimpleNamespace(
        project_root=ROOT,
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
    )
    manager = CodexJobManager(settings, store)
    job = {
        "email": "owner@example.com",
        "log_path": str(tmp_path / "logs" / "missing.log"),
        "reauth": False,
    }
    response = {
        "result": {
            "ok": True,
            "status": "success",
            "message": "done",
            "file_path": str(tmp_path / "credential.json"),
            "phone_verified": True,
        }
    }

    installation = install_managed_sms_runtime()
    try:
        safe = codex_worker._safe_result(response["result"])
        manager._handle_result_locked(job, {"result": safe}, {})
    finally:
        installation.restore()

    account = store.list_accounts()[0]
    assert safe["phone_verified"] is True
    assert job["phone_verified"] is True
    assert job["phone_number"] == ""
    assert account["phone_verified"] is True
    assert account["phone_number"] == ""


def test_account_apis_preserve_verified_state_without_phone_number(tmp_path) -> None:
    data_dir = tmp_path / "data"
    log_dir = tmp_path / "logs"
    credential_dir = data_dir / "codex_accounts"
    credential_dir.mkdir(parents=True)
    log_dir.mkdir(parents=True)
    store = MailboxStore(data_dir)
    store.import_text(
        "generic_api",
        "owner@example.com----https://mail.test/code",
    )
    store.update_codex(
        "owner@example.com",
        status="success",
        phone_verified=True,
    )
    (credential_dir / "codex-owner.json").write_text(
        json.dumps(
            {
                "type": "codex",
                "email": "owner@example.com",
                "refresh_token": "fixture-not-a-real-token",
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(
        project_root=ROOT,
        data_dir=data_dir,
        log_dir=log_dir,
        browser_executable=None,
        browser_timeout_seconds=120,
        host="127.0.0.1",
        port=5015,
    )

    installation = install_managed_sms_runtime()
    try:
        app = create_managed_app(settings, codex_manager=_AccountApiManager())
        app.config["TESTING"] = True
        client = app.test_client()
        account = client.get("/api/accounts").get_json()["accounts"][0]
        inventory = client.get("/api/seller/inventory").get_json()
    finally:
        installation.restore()

    assert account["has_credential"] is True
    assert account["phone_verified"] is True
    assert account["phone_number"] == ""
    assert inventory["accounts"][0]["phone_verified"] is True
    assert inventory["summary"]["phone_verified"] == 1
    assert inventory["summary"]["phone_unverified"] == 0


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("fraud_guard", "fraud_guard"),
        ("phone_rate_limited", "rate_limited"),
        ("number_in_use", "number_in_use"),
        ("sms_timeout", "sms_timeout"),
        ("otp_rejected", "code_rejected"),
    ],
)
def test_safe_statistics_marker_maps_to_existing_statuses(
    code: str,
    expected: str,
) -> None:
    installation = install_managed_sms_runtime()
    try:
        status = artifact_store._sms_rejection_status(
            "[Codex] add-phone/send 未成功 "
            f"reason=smart_sms smart_sms_code={code}"
        )
    finally:
        installation.restore()

    assert status == expected


def test_safe_timeout_marker_completes_active_statistics_record() -> None:
    text = "\n".join(
        [
            "2026-08-08 10:00:00,000 [INFO] "
            "[SMS:Hero] acquired country=4 price=0.0275 "
            "action=getNumberV2 activation_id=activation-1",
            "2026-08-08 10:00:01,000 [INFO] "
            "[Codex] 短信已发送，开始轮询验证码 activation_id=activation-1",
            "2026-08-08 10:00:31,000 [WARNING] "
            "[Codex] add-phone/send 未成功 "
            "reason=smart_sms smart_sms_code=sms_timeout",
        ]
    )
    installation = install_managed_sms_runtime()
    try:
        records, _ = artifact_store._parse_sms_log(text, relative="safe.log")
    finally:
        installation.restore()

    assert len(records) == 1
    assert records[0]["status"] == "sms_timeout"
    assert records[0]["phone_number"] == ""


def test_installation_is_idempotent() -> None:
    first = install_managed_sms_runtime()
    second = install_managed_sms_runtime()
    try:
        assert second is first
    finally:
        first.restore()


def test_install_preflight_failure_leaves_runtime_unmodified(monkeypatch) -> None:
    from src import hero_sms

    original_bridge_run = upstream_bridge.run_codex_only
    original_failure_info = CodexJobManager._failure_info
    original_handle_result = CodexJobManager._handle_result_locked
    original_status_parser = artifact_store._sms_rejection_status
    original_phone_lookup = (
        artifact_store.ArtifactStore.phone_verification_for_account
    )
    original_hero_query = hero_sms.HeroSmsAdapter.query
    real_import = sms_bootstrap.importlib.import_module

    def incompatible_import(name: str):
        if name == "src.codex_worker":
            return SimpleNamespace(run_codex_only=original_bridge_run)
        return real_import(name)

    monkeypatch.setattr(
        sms_bootstrap.importlib,
        "import_module",
        incompatible_import,
    )

    with pytest.raises(RuntimeError, match="_safe_result"):
        install_managed_sms_runtime()

    assert upstream_bridge.run_codex_only is original_bridge_run
    assert CodexJobManager._failure_info is original_failure_info
    assert CodexJobManager._handle_result_locked is original_handle_result
    assert artifact_store._sms_rejection_status is original_status_parser
    assert (
        artifact_store.ArtifactStore.phone_verification_for_account
        is original_phone_lookup
    )
    assert hero_sms.HeroSmsAdapter.query is original_hero_query


def test_real_upstream_sms_poller_redacts_received_otp(
    monkeypatch,
    caplog,
) -> None:
    settings = SimpleNamespace(project_root=ROOT, data_dir=ROOT / "data")
    codex_oauth = upstream_bridge._ensure_upstream_imports(settings)
    provider = codex_oauth.sms_provider
    caplog.set_level(logging.INFO, logger=provider.logger.name)
    monkeypatch.setattr(
        provider,
        "_request_grizzly",
        lambda http, params: "STATUS_OK:654321",
    )

    installation = install_managed_sms_runtime()
    try:
        code = provider.wait_for_sms_code("activation-safe-test", http=object())
    finally:
        installation.restore()

    assert code == "654321"
    assert "654321" not in caplog.text
    assert "[REDACTED]" in caplog.text
    received_message = next(
        record.getMessage()
        for record in caplog.records
        if "[REDACTED]" in record.getMessage()
    )
    parsed_text = "\n".join(
        [
            "2026-08-08 10:00:00,000 [INFO] "
            "[SMS:Hero] acquired country=4 price=0.0275 "
            "action=getNumberV2 activation_id=activation-safe-test",
            "2026-08-08 10:00:01,000 [INFO] "
            "[Codex] 短信已发送，开始轮询验证码 "
            "activation_id=activation-safe-test",
            f"2026-08-08 10:00:02,000 [INFO] {received_message}",
        ]
    )
    records, _ = artifact_store._parse_sms_log(
        parsed_text,
        relative="redacted-otp.log",
    )
    assert records[0]["code_received"] is True


def test_checked_in_codex_oauth_accepts_and_restores_overlay() -> None:
    settings = SimpleNamespace(project_root=ROOT, data_dir=ROOT / "data")
    codex_oauth = upstream_bridge._ensure_upstream_imports(settings)
    original = codex_oauth._do_phone_verification

    patch = install_codex_sms_overlay(codex_oauth)
    try:
        assert codex_oauth._do_phone_verification is patch.patched
    finally:
        patch.restore()

    assert codex_oauth._do_phone_verification is original


@pytest.mark.parametrize(
    ("title", "expected_code"),
    [
        ("BAD_KEY", "sms_bad_key"),
        ("INVALID_KEY", "sms_bad_key"),
        ("WRONG_KEY", "sms_bad_key"),
        ("NO_BALANCE", "sms_no_balance"),
    ],
)
def test_real_hero_coordinator_aborts_terminal_error_after_one_request(
    monkeypatch,
    title: str,
    expected_code: str,
) -> None:
    settings = SimpleNamespace(project_root=ROOT, data_dir=ROOT / "data")
    codex_oauth = upstream_bridge._ensure_upstream_imports(settings)
    monkeypatch.setenv("HERO_SMS_API_KEY", "fixture-key")
    monkeypatch.setenv("HERO_SMS_COUNTRIES", "4,6")
    provider = codex_oauth.sms_provider
    http = _CountingHeroHttp(title)
    monkeypatch.setattr(provider, "_http", lambda: http)

    installation = install_managed_sms_runtime()
    hero_patch = install_hero_sms_patch(provider)
    codex_patch = install_codex_sms_overlay(codex_oauth)
    try:
        with pytest.raises(SmartSmsStop) as caught:
            codex_oauth._do_phone_verification(object())
    finally:
        codex_patch.restore()
        hero_patch.restore()
        installation.restore()

    assert caught.value.code == expected_code
    assert len(http.calls) == 1


def test_real_spawn_import_installs_worker_runtime_wrapper() -> None:
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    process = context.Process(
        target=_spawn_runtime_probe,
        args=(result_queue, str(ROOT)),
    )
    process.start()
    process.join(timeout=20)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)

    assert process.exitcode == 0
    result = result_queue.get(timeout=5)
    assert result == {
        "same_run": True,
        "run_module": "manager.sms_bootstrap",
        "safe_result_module": "manager.sms_bootstrap",
    }


def test_manager_entry_installs_runtime_when_spawn_imports(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "manager.sms_bootstrap.install_managed_sms_runtime",
        lambda: calls.append("installed"),
    )

    runpy.run_path("manager_app.py", run_name="__mp_main__")

    assert calls == ["installed"]
