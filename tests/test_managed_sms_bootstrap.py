from __future__ import annotations

import runpy
from types import SimpleNamespace

import pytest

from manager.sms_bootstrap import install_managed_sms_runtime
from src import artifact_store, codex_worker, upstream_bridge
from src.codex_service import CodexJobManager


def test_managed_runtime_wraps_worker_phone_jobs_and_restores(monkeypatch) -> None:
    events: list[str] = []
    codex = SimpleNamespace()

    def fake_original_run(settings, mailbox, *, reauth=False):
        events.append("run")
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

    assert result == {"ok": True}
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


def test_manager_entry_installs_runtime_when_spawn_imports(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "manager.sms_bootstrap.install_managed_sms_runtime",
        lambda: calls.append("installed"),
    )

    runpy.run_path("manager_app.py", run_name="__mp_main__")

    assert calls == ["installed"]
