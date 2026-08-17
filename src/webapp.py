from __future__ import annotations

import ipaddress
import io
import json
import base64
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, jsonify, redirect, render_template, request, send_file, url_for

from .artifact_store import ArtifactStore, _redact_log_text
from .codex_quota import (
    CodexQuotaStore,
    query_codex_quota,
    refresh_codex_credential_metadata,
)
from .export_history import ExportHistoryStore
from .hero_catalog import HeroCatalog
from .hero_pricing import HeroPricingClient, HeroPricingError, filter_price_tiers
from .mailbox_store import MailboxStore
from .settings import Settings
from .sms_config import SmsConfigStore, normalize_hero_countries, normalize_price


def _is_loopback_host(host: str) -> bool:
    value = str(host or "").strip().lower().strip("[]")
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _oauth_subscription_metadata(payload: dict) -> dict:
    """Read plan/subscription dates embedded in a locally stored OAuth JWT."""

    result = {}
    for token_key in ("id_token", "access_token"):
        token = str(payload.get(token_key) or "")
        parts = token.split(".")
        if len(parts) < 2 or len(parts[1]) > 128 * 1024:
            continue
        try:
            encoded = parts[1] + "=" * (-len(parts[1]) % 4)
            claims = json.loads(base64.urlsafe_b64decode(encoded).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(claims, dict):
            continue
        auth = claims.get("https://api.openai.com/auth")
        if not isinstance(auth, dict):
            auth = {}
        plan_type = str(auth.get("chatgpt_plan_type") or "").strip().lower()
        if plan_type and "plan_type" not in result:
            result["plan_type"] = plan_type
        active_until = str(auth.get("chatgpt_subscription_active_until") or "").strip()
        if active_until:
            try:
                parsed = datetime.fromisoformat(active_until.replace("Z", "+00:00"))
                if parsed.tzinfo is not None:
                    result["subscription_active_until"] = active_until
            except ValueError:
                pass
    return result


def create_app(
    settings: Settings,
    *,
    mailbox_store=None,
    codex_manager=None,
    sms_config_store=None,
    hero_catalog=None,
    hero_pricing=None,
    artifact_store=None,
    quota_store=None,
    **_legacy,
) -> Flask:
    if not _is_loopback_host(settings.host):
        raise ValueError("未启用控制台登录，WEBUI_HOST 必须是本机回环地址")
    app = Flask(
        __name__,
        template_folder=str(settings.project_root / "templates"),
        static_folder=None,
    )
    app.config.update(
        MAX_CONTENT_LENGTH=32 * 1024,
    )
    mailbox_store = mailbox_store or MailboxStore(settings.data_dir)
    sms_config_store = sms_config_store or SmsConfigStore(settings.project_root / ".env")
    hero_catalog = hero_catalog or HeroCatalog()
    artifact_store = artifact_store or ArtifactStore(settings.data_dir, settings.log_dir)
    quota_store = quota_store or CodexQuotaStore(settings.data_dir)
    export_history = ExportHistoryStore(settings.data_dir)
    if codex_manager is None:
        from .codex_service import CodexJobManager

        codex_manager = CodexJobManager(settings, mailbox_store)

    def _task_observed(job):
        if not isinstance(job, dict):
            return ""
        return str(
            job.get("updated_at")
            or job.get("finished_at")
            or job.get("started_at")
            or job.get("created_at")
            or ""
        )

    def _safe_recent_task(job):
        """Expose task progress without leaking worker paths or protocol secrets."""

        if not isinstance(job, dict):
            return None
        allowed = (
            "id",
            "pipeline_id",
            "email",
            "status",
            "stage",
            "attempt",
            "max_attempts",
            "failure_code",
            "retryable",
            "next_retry_at",
            "created_at",
            "started_at",
            "attempt_started_at",
            "attempt_deadline_at",
            "attempt_finished_at",
            "finished_at",
            "has_log",
            "log_count",
            "has_credential",
        )
        public = {key: job.get(key) for key in allowed if key in job}
        public["updated_at"] = _task_observed(job)
        public["message"] = _redact_log_text(str(job.get("message") or ""))[:1000]
        return public

    def _account_rows(jobs=None):
        rows = mailbox_store.list_accounts()
        jobs = list(codex_manager.list_jobs() if jobs is None else jobs)
        latest_jobs = {}
        for job in jobs:
            if not isinstance(job, dict):
                continue
            key = str(job.get("email") or "").strip().casefold()
            if not key:
                continue
            observed = _task_observed(job)
            previous = latest_jobs.get(key)
            previous_observed = str((previous or {}).get("_observed") or "")
            if previous is None or observed >= previous_observed:
                latest_jobs[key] = {"_observed": observed, "job": job}
        credential_rows = (
            artifact_store.list_credentials()
            if callable(getattr(artifact_store, "list_credentials", None))
            else []
        )
        credentials_by_email = {}
        for item in credential_rows:
            key = str(item.get("email") or "").strip().casefold()
            if key and item.get("exportable") and key not in credentials_by_email:
                credentials_by_email[key] = item
        phone_lookup = getattr(artifact_store, "phone_verification_for_account", None)
        for row in rows:
            row["codex_message"] = _redact_log_text(
                str(row.get("codex_message") or "")
            )[:1000]
            email_key = str(row.get("email") or "").strip().casefold()
            credential = credentials_by_email.get(email_key)
            row["has_credential"] = bool(credential)
            row["credential_id"] = str((credential or {}).get("id") or "")
            row["credential_modified_at"] = (credential or {}).get("modified_at")
            row["credential_expired"] = (credential or {}).get("expired")
            row["subscription_active_until"] = (credential or {}).get(
                "subscription_active_until"
            )
            row["subscription_plan_type"] = (credential or {}).get("plan_type")
            row["subscription_checked_at"] = (credential or {}).get(
                "subscription_checked_at"
            )
            row["subscription_source"] = (credential or {}).get("subscription_source")
            row["subscription_error"] = (credential or {}).get("subscription_error")
            row["credential_account_hint"] = (credential or {}).get("account_hint")
            if credential and not row.get("phone_number") and callable(phone_lookup):
                verified = phone_lookup(str(row.get("id") or "")) or {}
                row["phone_verified"] = bool(verified.get("phone_number"))
                row["phone_number"] = str(verified.get("phone_number") or "")
                row["phone_verified_at"] = verified.get("phone_verified_at")
            recent = latest_jobs.get(email_key)
            row["recent_task"] = _safe_recent_task((recent or {}).get("job"))
        return rows

    def _integration_account_status(email: str) -> dict | None:
        account = mailbox_store.get_secret(email=email)
        if account is None:
            return None
        latest_getter = getattr(codex_manager, "latest_job_for_email", None)
        if callable(latest_getter):
            recent = latest_getter(str(account.get("email") or email))
        else:
            target = str(account.get("email") or email).strip().casefold()
            candidates = [
                item
                for item in codex_manager.list_jobs()
                if str(item.get("email") or "").strip().casefold() == target
            ]
            recent = max(candidates, key=_task_observed) if candidates else None
        recent = _safe_recent_task(recent) if isinstance(recent, dict) else None
        credential_ready = bool(account.get("credential_path"))
        raw_state = str(
            (recent or {}).get("status") or account.get("codex_status") or "idle"
        ).strip().lower()
        state_aliases = {
            "success": "ready" if credential_ready else "completed",
            "completed": "ready" if credential_ready else "completed",
            "pending": "queued",
        }
        state = state_aliases.get(raw_state, raw_state)
        if credential_ready:
            state = "ready"
        elif state not in {
            "idle",
            "queued",
            "running",
            "retry_wait",
            "paused",
            "completed",
            "failed",
            "stopped",
        }:
            state = "idle"
        task = None
        if recent:
            task = {
                key: recent.get(key)
                for key in ("id", "status", "stage", "message", "updated_at")
                if recent.get(key) not in (None, "")
            }
        return {
            "email": str(account.get("email") or email),
            "state": state,
            "credential_ready": credential_ready,
            "phone_verified": bool(account.get("phone_verified")),
            "updated_at": str(
                (recent or {}).get("updated_at") or account.get("updated_at") or ""
            ),
            "task": task,
        }

    def _pipeline_overview():
        getter = getattr(codex_manager, "pipeline_overview", None)
        if callable(getter):
            return getter()
        return {
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

    @app.after_request
    def _security_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        return response

    @app.route("/login", methods=["GET", "POST"])
    @app.route("/logout", methods=["GET", "POST"])
    def legacy_auth_redirect():
        return redirect(url_for("index"), code=303)

    @app.get("/health")
    def health():
        return jsonify({"ok": True, "service": "codex-auto-sms-receiver"})

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/overview")
    def overview():
        jobs = codex_manager.list_jobs()
        return jsonify(
            {
                "ok": True,
                "browser_available": bool(settings.browser_executable),
                "browser_executable": str(settings.browser_executable or ""),
                "accounts": _account_rows(jobs),
                "codex": codex_manager.availability(),
                "runtime_config": codex_manager.runtime_config(),
                "codex_jobs": [
                    safe for job in jobs if (safe := _safe_recent_task(job)) is not None
                ],
                "pipeline": _pipeline_overview(),
            }
        )

    @app.post("/api/accounts/import")
    def import_accounts():
        data = request.get_json(silent=True) or {}
        try:
            result = mailbox_store.import_text(
                source=str(data.get("source") or ""),
                text=str(data.get("text") or ""),
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400
        return jsonify({"ok": True, **result})

    @app.get("/api/accounts")
    def list_accounts():
        return jsonify({"ok": True, "accounts": _account_rows()})

    @app.post("/api/v1/integration/accounts/submit")
    def integration_submit_account():
        data = request.get_json(silent=True)
        email = str((data or {}).get("email") or "").strip()
        if not isinstance(data, dict) or not email or len(email) > 320 or "@" not in email:
            return jsonify({"ok": False, "error": "invalid_email"}), 400
        status = _integration_account_status(email)
        if status is None:
            return jsonify({"ok": False, "error": "account_not_found"}), 404
        if status["credential_ready"] or status["state"] in {
            "queued",
            "running",
            "retry_wait",
            "paused",
        }:
            return jsonify({"ok": True, "started": False, "account": status})
        try:
            codex_manager.start(status["email"])
        except Exception as exc:
            return jsonify(
                {
                    "ok": False,
                    "error": "job_start_failed",
                    "error_type": type(exc).__name__,
                }
            ), 409
        return jsonify(
            {
                "ok": True,
                "started": True,
                "account": _integration_account_status(status["email"]),
            }
        ), 202

    @app.get("/api/v1/integration/accounts/status")
    def integration_account_status():
        email = str(request.args.get("email") or "").strip()
        if not email or len(email) > 320 or "@" not in email:
            return jsonify({"ok": False, "error": "invalid_email"}), 400
        status = _integration_account_status(email)
        if status is None:
            return jsonify({"ok": False, "error": "account_not_found"}), 404
        return jsonify({"ok": True, "account": status})

    @app.post("/api/accounts/<account_id>/material/reveal")
    def reveal_account_material(account_id: str):
        if not _is_loopback_host(settings.host):
            return jsonify({"ok": False, "error": "原始账号素材仅允许从本机 WebUI 查看"}), 403
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict) or data.get("confirmed") is not True:
            return jsonify({"ok": False, "error": "查看原始素材前必须明确确认"}), 400
        try:
            material = mailbox_store.reveal_original(account_id)
        except (ValueError, KeyError) as exc:
            return _selection_error(exc)
        return jsonify({"ok": True, **material})

    @app.get("/api/accounts/<account_id>/code-url/open")
    def open_account_code_url(account_id: str):
        if not _is_loopback_host(settings.host):
            return jsonify({"ok": False, "error": "取码 URL 仅允许从本机 WebUI 打开"}), 403
        account = mailbox_store.get_secret(account_id=account_id)
        if account is None:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        target = str(account.get("code_url") or "").strip()
        try:
            parsed = urlsplit(target)
            _ = parsed.port
        except ValueError:
            parsed = None
        if (
            parsed is None
            or parsed.scheme.casefold() not in {"http", "https"}
            or not parsed.hostname
        ):
            return jsonify({"ok": False, "error": "该账号没有可打开的取码 URL"}), 404
        return redirect(target, code=302)

    @app.get("/api/sms-config")
    def get_sms_config():
        return jsonify({"ok": True, "config": sms_config_store.snapshot()})

    @app.get("/api/hero-sms/catalog")
    def get_hero_sms_catalog():
        catalog = hero_catalog.catalog()
        return jsonify({"ok": True, **catalog})

    def _hero_pricing_client(api_key: str):
        if hero_pricing is None:
            return HeroPricingClient(api_key)
        if hasattr(hero_pricing, "for_api_key"):
            return hero_pricing.for_api_key(api_key)
        if callable(hero_pricing) and not hasattr(hero_pricing, "prices"):
            return hero_pricing(api_key)
        return hero_pricing

    def _saved_hero_key() -> str:
        # This is deliberately read from the backend store. Request bodies and
        # query strings can never override or receive the saved API key.
        return sms_config_store.reveal_credential("hero")

    @app.get("/api/hero-sms/balance")
    def get_hero_sms_balance():
        api_key = _saved_hero_key()
        if not api_key:
            return jsonify({"ok": False, "error": "Hero SMS API Key 尚未配置"}), 409
        try:
            balance = _hero_pricing_client(api_key).balance()
        except HeroPricingError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 502
        except Exception as exc:
            return jsonify(
                {
                    "ok": False,
                    "error": f"Hero SMS 余额查询失败（{type(exc).__name__}）",
                }
            ), 502
        return jsonify({"ok": True, "provider": "hero", "balance": balance})

    @app.route("/api/hero-sms/prices", methods=["GET", "POST"])
    def get_hero_sms_prices():
        api_key = _saved_hero_key()
        if not api_key:
            return jsonify({"ok": False, "error": "Hero SMS API Key 尚未配置"}), 409

        data = request.get_json(silent=True) if request.method == "POST" else None
        data = data if isinstance(data, dict) else {}
        requested: object = data.get("countries", data.get("country"))
        if requested is None and request.method == "GET":
            repeated = request.args.getlist("country")
            requested = repeated or request.args.get("countries") or request.args.get("country")
        config = sms_config_store.snapshot()
        try:
            countries = normalize_hero_countries(
                requested,
                fallback=config.get("countries") or (config.get("country") or "10",),
            )
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        if not countries:
            return jsonify({"ok": False, "error": "至少需要 1 个 Hero SMS 国家"}), 400

        try:
            rows = _hero_pricing_client(api_key).prices(countries)
        except HeroPricingError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 502
        except Exception as exc:
            return jsonify(
                {
                    "ok": False,
                    "error": f"Hero SMS 价格查询失败（{type(exc).__name__}）",
                }
            ), 502

        try:
            directory = hero_catalog.catalog()
            country_names = {
                str(item.get("id") or ""): {
                    "name": str(item.get("name") or ""),
                    "name_en": str(item.get("name_en") or ""),
                    "flag": str(item.get("flag") or "🌐"),
                }
                for item in directory.get("countries", [])
                if isinstance(item, dict)
            }
        except Exception:
            country_names = {}

        try:
            minimum = normalize_price(
                data.get("min_price", config.get("min_price") or ""),
                field="最低购买价",
            )
            maximum = normalize_price(
                data.get("max_price", config.get("max_price") or ""),
                field="价格上限",
            )
            preferred = normalize_price(
                data.get("preferred_price", config.get("preferred_price") or ""),
                field="指定价格档位",
            )
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        if minimum and maximum and Decimal(minimum) > Decimal(maximum):
            return jsonify({"ok": False, "error": "最低购买价不能高于价格上限"}), 400
        if preferred and minimum and Decimal(preferred) < Decimal(minimum):
            return jsonify({"ok": False, "error": "指定价格档位不能低于最低购买价"}), 400
        if preferred and maximum and Decimal(preferred) > Decimal(maximum):
            return jsonify({"ok": False, "error": "指定价格档位不能高于价格上限"}), 400
        acquire_priority = str(
            data.get("acquire_priority", config.get("acquire_priority") or "country")
        ).strip().lower()
        if acquire_priority not in {"country", "price", "price_high"}:
            return jsonify({"ok": False, "error": "拿号优先级格式不正确"}), 400
        for row in rows:
            eligible = filter_price_tiers(
                row.get("tiers") or [],
                min_price=minimum,
                max_price=maximum,
            )
            eligible_prices = {
                str(item.get("price") or "")
                for item in eligible
                if item.get("available")
            }
            for tier in row.get("tiers") or []:
                tier["eligible"] = str(tier.get("price") or "") in eligible_prices
            available_prices = [
                str(item.get("price") or "")
                for item in eligible
                if item.get("available") and str(item.get("price") or "")
            ]
            row["available_in_range"] = bool(available_prices)
            row["lowest_available_price"] = available_prices[0] if available_prices else None
            row.update(country_names.get(str(row.get("country") or ""), {}))

        return jsonify(
            {
                "ok": True,
                "provider": "hero",
                "service": {"code": "dr", "name": "OpenAI"},
                "filters": {
                    "min_price": minimum,
                    "max_price": maximum,
                    "preferred_price": preferred,
                    "acquire_priority": acquire_priority,
                },
                "countries": rows,
            }
        )

    @app.get("/api/artifacts")
    def list_artifacts():
        return jsonify({"ok": True, **artifact_store.overview()})

    @app.get("/api/artifacts/sms-stats")
    def sms_statistics():
        if not _is_loopback_host(settings.host):
            return jsonify({"ok": False, "error": "接码统计仅允许从本机监听的 WebUI 查看"}), 403
        try:
            result = artifact_store.sms_statistics()
        except OSError as exc:
            return jsonify(
                {"ok": False, "error": f"无法读取接码日志（{type(exc).__name__}）"}
            ), 500

        try:
            directory = hero_catalog.catalog()
            country_names = {
                str(item.get("id") or ""): {
                    "name": str(item.get("name") or ""),
                    "name_en": str(item.get("name_en") or ""),
                    "flag": str(item.get("flag") or "🌐"),
                }
                for item in directory.get("countries", [])
                if isinstance(item, dict)
            }
        except Exception:
            country_names = {}

        def with_country_name(row):
            enriched = dict(row) if isinstance(row, dict) else {}
            country_id = str(enriched.get("country_id") or "")
            names = country_names.get(country_id, {})
            enriched.update(
                {
                    "name": names.get("name") or (f"国家 {country_id}" if country_id else "未知国家"),
                    "name_en": names.get("name_en") or "",
                    "flag": names.get("flag") or "🌐",
                }
            )
            return enriched

        payload = dict(result) if isinstance(result, dict) else {}
        payload["countries"] = [
            with_country_name(row) for row in payload.get("countries", [])
        ]
        payload["records"] = [
            with_country_name(row) for row in payload.get("records", [])
        ]
        return jsonify({"ok": True, **payload})

    def _download_guard():
        if not _is_loopback_host(settings.host):
            return jsonify({"ok": False, "error": "敏感文件仅允许从本机监听的 WebUI 下载"}), 403
        if str(request.args.get("confirmed") or "").lower() not in {"1", "true"}:
            return jsonify({"ok": False, "error": "下载前必须明确确认"}), 400
        return None

    @app.get("/api/artifacts/credentials/<artifact_id>/download")
    def download_credential(artifact_id: str):
        blocked = _download_guard()
        if blocked is not None:
            return blocked
        path = artifact_store.credential_file(artifact_id)
        if path is None:
            return jsonify({"ok": False, "error": "凭证文件不存在"}), 404
        return send_file(
            path,
            mimetype="application/json",
            as_attachment=True,
            download_name=path.name,
            conditional=False,
            etag=False,
            max_age=0,
        )

    @app.get("/api/accounts/<account_id>/credential/download")
    def download_account_credential(account_id: str):
        blocked = _download_guard()
        if blocked is not None:
            return blocked
        account = mailbox_store.get_secret(account_id=account_id)
        if account is None:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        credential = artifact_store.exportable_credential_for_email(
            str(account.get("email") or "")
        )
        if not credential:
            return jsonify({"ok": False, "error": "该账号没有可导出的 OAuth 凭证"}), 404
        path = artifact_store.exportable_credential_file(
            str(credential.get("id") or ""), expected_email=str(account.get("email") or "")
        )
        if path is None:
            return jsonify({"ok": False, "error": "该账号的 OAuth 凭证不可用"}), 404
        return send_file(
            path,
            mimetype="application/json",
            as_attachment=True,
            download_name=path.name,
            conditional=False,
            etag=False,
            max_age=0,
        )

    @app.get("/api/artifacts/logs/<artifact_id>/download")
    def download_log(artifact_id: str):
        blocked = _download_guard()
        if blocked is not None:
            return blocked
        path = artifact_store.log_file(artifact_id)
        if path is None:
            return jsonify({"ok": False, "error": "日志文件不存在"}), 404
        return send_file(
            path,
            mimetype="text/plain; charset=utf-8",
            as_attachment=True,
            download_name=path.name,
            conditional=False,
            etag=False,
            max_age=0,
        )

    @app.get("/api/artifacts/logs/<artifact_id>/content")
    def view_log_content(artifact_id: str):
        if not _is_loopback_host(settings.host):
            return jsonify({"ok": False, "error": "日志内容仅允许从本机监听的 WebUI 查看"}), 403
        try:
            offset = int(request.args.get("offset", "0"))
            limit = int(request.args.get("limit", "200"))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "日志分页参数必须是整数"}), 400
        try:
            result = artifact_store.read_log_events(
                artifact_id,
                offset=offset,
                limit=limit,
                level=str(request.args.get("level") or "all"),
                query=str(request.args.get("q") or ""),
                order=str(request.args.get("order") or "desc"),
            )
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except OSError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500
        if result is None:
            return jsonify({"ok": False, "error": "日志文件不存在"}), 404
        return jsonify({"ok": True, **result})

    @app.get("/api/logs/timeline")
    def view_log_timeline():
        if not _is_loopback_host(settings.host):
            return jsonify({"ok": False, "error": "日志时间线仅允许从本机监听的 WebUI 查看"}), 403
        try:
            offset = int(request.args.get("offset", "0"))
            limit = int(request.args.get("limit", "100"))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "日志分页参数必须是整数"}), 400
        reader = getattr(artifact_store, "read_log_timeline", None)
        if not callable(reader):
            return jsonify({"ok": False, "error": "当前日志存储不支持聚合时间线"}), 501
        account_emails = {
            str(row.get("id") or "").strip().lower(): str(row.get("email") or "")
            for row in mailbox_store.list_accounts()
            if isinstance(row, dict) and row.get("id")
        }
        try:
            result = reader(
                offset=offset,
                limit=limit,
                level=str(request.args.get("level") or "important"),
                query=str(request.args.get("q") or request.args.get("query") or ""),
                account_emails=account_emails,
            )
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except OSError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500

        jobs = [job for job in codex_manager.list_jobs() if isinstance(job, dict)]
        latest_job = max(jobs, key=_task_observed, default=None)
        result["recent_task"] = _safe_recent_task(latest_job)
        return jsonify({"ok": True, **result})

    def _zip_bytes(rows) -> bytes:
        if not rows:
            raise ValueError("没有可打包的文件")
        total_size = 0
        for path, _ in rows:
            try:
                total_size += path.stat().st_size
            except OSError:
                continue
        if total_size > 128 * 1024 * 1024:
            raise OverflowError("归档文件总量超过 128MB，请单独下载")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path, relative in rows:
                try:
                    archive.write(path, arcname=relative)
                except OSError:
                    continue
        return buffer.getvalue()

    def _bytes_download(content: bytes, *, filename: str, mimetype: str):
        return send_file(
            io.BytesIO(content),
            mimetype=mimetype,
            as_attachment=True,
            download_name=filename,
            conditional=False,
            etag=False,
            max_age=0,
        )

    def _remembered_download(
        content: bytes,
        *,
        filename: str,
        mimetype: str,
        kind: str,
        formats: list[str],
        account_count: int,
    ):
        history = export_history.save(
            content,
            filename=filename,
            kind=kind,
            formats=formats,
            account_count=account_count,
        )
        response = _bytes_download(content, filename=filename, mimetype=mimetype)
        response.headers["X-Export-History-ID"] = str(history["id"])
        return response

    def _zip_download(rows, *, filename: str):
        try:
            return _bytes_download(
                _zip_bytes(rows), filename=filename, mimetype="application/zip"
            )
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        except OverflowError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 413

    def _selected_credential_files(data, *, max_items: int = 100) -> list[tuple[Path, str]]:
        if not isinstance(data, dict):
            raise ValueError("请提交 JSON 对象")
        if data.get("confirmed") is not True:
            raise ValueError("导出前必须明确确认")
        credential_ids = data.get("credential_ids", [])
        account_ids = data.get("account_ids", [])
        if not isinstance(credential_ids, list) or not isinstance(account_ids, list):
            raise ValueError("凭证和账号 ID 必须使用数组")
        if not credential_ids and not account_ids:
            raise ValueError("请至少选择一个凭证")
        if len(credential_ids) + len(account_ids) > max_items:
            raise OverflowError(f"每次最多导出 {max_items} 个凭证")
        if any(not isinstance(value, str) or not value.strip() for value in credential_ids):
            raise ValueError("凭证 ID 格式无效")
        if any(not isinstance(value, str) or not value.strip() for value in account_ids):
            raise ValueError("账号 ID 格式无效")

        exportable = {
            str(item.get("id") or "").strip().lower(): item
            for item in artifact_store.list_credentials()
            if item.get("exportable") and item.get("id")
        }
        selected: dict[str, tuple[Path, str]] = {}
        for raw_id in credential_ids:
            artifact_id = raw_id.strip().lower()
            if artifact_id not in exportable:
                raise KeyError("所选凭证不存在或不可导出")
            path = artifact_store.exportable_credential_file(artifact_id)
            if path is None:
                raise KeyError("所选凭证不存在或不可导出")
            selected[str(path.resolve())] = (path, path.name)
        for raw_id in account_ids:
            account = mailbox_store.get_secret(account_id=raw_id.strip())
            if account is None:
                raise KeyError("所选账号不存在")
            email = str(account.get("email") or "")
            credential = artifact_store.exportable_credential_for_email(email)
            if not credential:
                raise KeyError("所选账号没有可导出的 OAuth 凭证")
            path = artifact_store.exportable_credential_file(
                str(credential.get("id") or ""), expected_email=email
            )
            if path is None:
                raise KeyError("所选账号的 OAuth 凭证不可用")
            selected[str(path.resolve())] = (path, path.name)
        return list(selected.values())

    def _selection_error(exc: Exception):
        if isinstance(exc, OverflowError):
            return jsonify({"ok": False, "error": str(exc)}), 413
        if isinstance(exc, KeyError):
            return jsonify({"ok": False, "error": str(exc).strip("'")}), 404
        return jsonify({"ok": False, "error": str(exc)}), 400

    def _sub2api_payload(rows: list[tuple[Path, str]]) -> dict:
        accounts = []
        for path, _ in rows:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ValueError(f"凭证文件读取失败：{path.name}") from exc
            if not isinstance(raw, dict):
                raise ValueError(f"凭证文件格式无效：{path.name}")
            subscription = _oauth_subscription_metadata(raw)
            credentials = {}
            for source_key, target_key in (
                ("access_token", "access_token"),
                ("refresh_token", "refresh_token"),
                ("id_token", "id_token"),
                ("account_id", "chatgpt_account_id"),
                ("email", "email"),
                ("plan_type", "plan_type"),
            ):
                value = raw.get(source_key)
                if value is not None and str(value).strip():
                    credentials[target_key] = value
            expires_at = subscription.get("subscription_active_until") or raw.get(
                "expired"
            ) or raw.get("expires_at")
            if expires_at:
                credentials["expires_at"] = expires_at
            if not credentials.get("plan_type") and subscription.get("plan_type"):
                credentials["plan_type"] = subscription["plan_type"]
            raw_type = str(raw.get("type") or "").strip().lower()
            if "plan_type" not in credentials and raw_type in {
                "free",
                "plus",
                "pro",
                "team",
                "business",
                "enterprise",
            }:
                credentials["plan_type"] = raw_type
            if not credentials.get("access_token") and not credentials.get("refresh_token"):
                raise ValueError(f"凭证缺少可用 OAuth Token：{path.name}")
            email = str(raw.get("email") or path.stem).strip()
            accounts.append(
                {
                    "name": email,
                    "platform": "openai",
                    "type": "oauth",
                    "credentials": credentials,
                    "extra": {},
                    "concurrency": 1,
                    "priority": 50,
                }
            )
        return {
            "type": "sub2api-data",
            "version": 1,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "proxies": [],
            "accounts": accounts,
        }

    def _original_accounts_bytes(account_ids: list[str]) -> bytes:
        grouped = mailbox_store.export_original(account_ids)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for source, lines in grouped.items():
                archive.writestr(f"{source}.txt", ("\n".join(lines) + "\n").encode("utf-8"))
            archive.writestr(
                "README.txt",
                "每个 TXT 文件均保持对应的原导入格式；在 WebUI 选择同名登录素材类型后可直接重新导入。\n".encode(
                    "utf-8"
                ),
            )
        return buffer.getvalue()

    def _original_accounts_download(account_ids: list[str], filename: str):
        return _bytes_download(
            _original_accounts_bytes(account_ids),
            filename=filename,
            mimetype="application/zip",
        )

    def _normalize_export_formats(data: dict, *, default: str) -> list[str]:
        requested = data.get("formats")
        if requested is None:
            requested = [data.get("format") or default]
        if not isinstance(requested, list) or not requested:
            raise ValueError("请至少选择一种导出格式")
        formats = list(dict.fromkeys(str(value or "").strip().lower() for value in requested))
        if any(value not in {"original", "codex_json", "sub2api"} for value in formats):
            raise ValueError("不支持的账号导出格式")
        return formats

    def _account_export_content(
        account_ids: list[str], formats: list[str], *, scope: str
    ) -> tuple[bytes, str, str]:
        parts: dict[str, bytes] = {}
        if "original" in formats:
            parts["original-format.zip"] = _original_accounts_bytes(account_ids)
        credential_formats = [value for value in formats if value != "original"]
        if credential_formats:
            rows = _selected_credential_files(
                {"account_ids": account_ids, "credential_ids": [], "confirmed": True},
                max_items=500,
            )
            if "codex_json" in credential_formats:
                parts["codex-json.zip"] = _zip_bytes(rows)
            if "sub2api" in credential_formats:
                parts["sub2api.json"] = (
                    json.dumps(_sub2api_payload(rows), ensure_ascii=False, indent=2).encode("utf-8")
                    + b"\n"
                )
        if len(parts) == 1:
            part_name, content = next(iter(parts.items()))
            return content, f"account-inventory-{scope}-{part_name}", (
                "application/json" if part_name.endswith(".json") else "application/zip"
            )
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for part_name, content in parts.items():
                archive.writestr(part_name, content)
        return buffer.getvalue(), f"account-inventory-{scope}-multi-format.zip", "application/zip"

    def _credential_export_content(
        rows: list[tuple[Path, str]], formats: list[str], *, scope: str
    ) -> tuple[bytes, str, str]:
        parts: dict[str, bytes] = {}
        if "codex_json" in formats:
            parts["codex-json.zip"] = _zip_bytes(rows)
        if "sub2api" in formats:
            parts["sub2api.json"] = (
                json.dumps(_sub2api_payload(rows), ensure_ascii=False, indent=2).encode("utf-8")
                + b"\n"
            )
        if len(parts) == 1:
            part_name, content = next(iter(parts.items()))
            return content, f"{scope}-{part_name}", (
                "application/json" if part_name.endswith(".json") else "application/zip"
            )
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for part_name, content in parts.items():
                archive.writestr(part_name, content)
        return buffer.getvalue(), f"{scope}-multi-format.zip", "application/zip"

    @app.get("/api/exports")
    def list_export_history():
        return jsonify({"ok": True, "exports": export_history.list()})

    @app.get("/api/exports/<export_id>/download")
    def download_export_history(export_id: str):
        resolved = export_history.file(export_id)
        if resolved is None:
            return jsonify({"ok": False, "error": "导出记录不存在或文件已清理"}), 404
        path, metadata = resolved
        return send_file(
            path,
            mimetype="application/json" if str(metadata.get("filename") or "").endswith(".json") else "application/zip",
            as_attachment=True,
            download_name=str(metadata.get("filename") or path.name),
            conditional=False,
            etag=False,
            max_age=0,
        )

    def _credential_record_for_account(account_id: str) -> tuple[dict, Path, dict]:
        account = mailbox_store.get_secret(account_id=account_id)
        if account is None:
            raise KeyError("所选账号不存在")
        email = str(account.get("email") or "")
        credential = artifact_store.exportable_credential_for_email(email)
        if not credential:
            raise KeyError("所选账号没有可用 OAuth 凭证")
        path = artifact_store.exportable_credential_file(
            str(credential.get("id") or ""), expected_email=email
        )
        if path is None:
            raise KeyError("所选账号的 OAuth 凭证不可用")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError("OAuth 凭证文件读取失败") from exc
        if not isinstance(payload, dict):
            raise ValueError("OAuth 凭证格式无效")
        return account, path, payload

    def _credential_payload_for_account(account_id: str) -> dict:
        return _credential_record_for_account(account_id)[2]

    @app.post("/api/accounts/<account_id>/formats/reveal")
    def reveal_account_formats(account_id: str):
        """Return on-demand previews for every format available to one account."""
        if not _is_loopback_host(settings.host):
            return jsonify({"ok": False, "error": "账号格式仅允许从本机 WebUI 查看"}), 403
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict) or data.get("confirmed") is not True:
            return jsonify({"ok": False, "error": "查看账号格式前必须明确确认"}), 400
        try:
            material = mailbox_store.reveal_original(account_id)
        except (ValueError, KeyError) as exc:
            return _selection_error(exc)

        email = str(material.get("email") or "")
        formats = [
            {
                "id": "original",
                "label": "原始素材",
                "filename": "account-original.txt",
                "content": str(material.get("material") or ""),
            }
        ]
        try:
            _, credential_path, credential = _credential_record_for_account(account_id)
        except KeyError:
            credential_path = None
            credential = None
        except ValueError as exc:
            return _selection_error(exc)
        if credential_path is not None and credential is not None:
            formats.extend(
                [
                    {
                        "id": "codex_json",
                        "label": "Codex JSON",
                        "filename": credential_path.name,
                        "content": json.dumps(
                            credential, ensure_ascii=False, indent=2
                        )
                        + "\n",
                    },
                    {
                        "id": "sub2api",
                        "label": "Sub2API",
                        "filename": "sub2api.json",
                        "content": json.dumps(
                            _sub2api_payload([(credential_path, credential_path.name)]),
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n",
                    },
                ]
            )
        return jsonify(
            {
                "ok": True,
                "email": email,
                "source": material.get("source") or "unknown",
                "has_credential": credential is not None,
                "formats": formats,
            }
        )

    def _refresh_account_metadata(account_id: str, *, include_quota: bool) -> dict:
        _, credential_path, _ = _credential_record_for_account(account_id)
        refreshed = refresh_codex_credential_metadata(
            credential_path, include_quota=include_quota
        )
        subscription = refreshed.get("subscription")
        subscription_error = refreshed.get("subscription_error")
        if isinstance(subscription, dict):
            stored = quota_store.put_subscription(account_id, subscription)
        else:
            stored = quota_store.record_subscription_error(
                account_id, subscription_error or "套餐查询失败"
            )
        quota = refreshed.get("quota")
        quota_error = refreshed.get("quota_error")
        if include_quota:
            if isinstance(quota, dict):
                stored = quota_store.put(account_id, quota)
            else:
                stored = quota_store.record_error(account_id, quota_error or "额度查询失败")
        return {
            **stored,
            "token_refreshed": bool(refreshed.get("token_refreshed")),
        }

    metadata_executor = ThreadPoolExecutor(
        max_workers=3, thread_name_prefix="codex-metadata"
    )
    metadata_schedule_lock = threading.Lock()
    metadata_scheduled: set[str] = set()

    def _schedule_account_metadata_refresh(
        account_id: str, *, attempt: int = 0, delay: float = 0
    ) -> None:
        key = f"{account_id}:{attempt}"
        with metadata_schedule_lock:
            if key in metadata_scheduled:
                return
            metadata_scheduled.add(key)

        def run() -> None:
            try:
                result = _refresh_account_metadata(account_id, include_quota=True)
                plan_type = str(
                    result.get("subscription_plan_type") or result.get("plan_type") or ""
                ).strip().lower()
                subscription_status = str(result.get("subscription_status") or "")
                if attempt < 2 and (
                    plan_type in {"", "free"} or subscription_status != "ok"
                ):
                    _schedule_account_metadata_refresh(
                        account_id,
                        attempt=attempt + 1,
                        delay=(30, 120)[attempt],
                    )
            except Exception:
                if attempt < 2:
                    _schedule_account_metadata_refresh(
                        account_id,
                        attempt=attempt + 1,
                        delay=(30, 120)[attempt],
                    )
            finally:
                with metadata_schedule_lock:
                    metadata_scheduled.discard(key)

        def submit() -> None:
            metadata_executor.submit(run)

        if delay > 0:
            timer = threading.Timer(delay, submit)
            timer.daemon = True
            timer.start()
        else:
            submit()

    def _queue_success_metadata_refresh(email: str, _credential_path: str | None) -> None:
        account = mailbox_store.get_secret(email=str(email or ""))
        if account is not None:
            _schedule_account_metadata_refresh(str(account.get("id") or ""))

    success_callback_setter = getattr(codex_manager, "set_success_callback", None)
    if callable(success_callback_setter):
        success_callback_setter(_queue_success_metadata_refresh)

    def _requested_inventory_account_ids(data: dict, noun: str) -> list[str]:
        if data.get("all") is True:
            account_ids = [
                str(row.get("id") or "")
                for row in _account_rows()
                if row.get("has_credential")
            ]
        else:
            account_ids = data.get("account_ids")
        if not isinstance(account_ids, list) or not account_ids:
            raise ValueError(f"请至少选择一个账号{noun}")
        normalized = list(dict.fromkeys(str(value or "").strip() for value in account_ids))
        if any(not value for value in normalized):
            raise ValueError("账号 ID 格式无效")
        return normalized

    @staticmethod
    def _metadata_result_requires_relogin(result: object) -> bool:
        if not isinstance(result, dict):
            return False
        text = " ".join(
            str(result.get(key) or "")
            for key in ("subscription_error", "error")
        ).casefold()
        return "token 刷新接口 http 401" in text

    def _start_credential_relogin(account_ids: list[str]) -> dict:
        emails = []
        for account_id in account_ids:
            account = mailbox_store.get_secret(account_id=account_id)
            if account is None:
                raise KeyError("所选账号不存在")
            emails.append(str(account.get("email") or ""))
        starter = getattr(codex_manager, "start_batch", None)
        if not callable(starter):
            raise RuntimeError("当前任务管理器不支持重新登录")
        return starter(
            emails,
            concurrency=max(1, min(5, len(emails))),
            retry_limit=1,
            retry_backoff_seconds=30,
            reauth=True,
        )

    def _queue_failed_token_relogins(
        account_ids: list[str], results: dict[str, dict]
    ) -> tuple[list[str], dict | None, str]:
        required = [
            account_id
            for account_id in account_ids
            if _metadata_result_requires_relogin(results.get(account_id))
        ]
        if not required:
            return [], None, ""
        try:
            return required, _start_credential_relogin(required), ""
        except Exception as exc:
            return required, None, f"{type(exc).__name__}: {exc}"

    @app.get("/api/seller/inventory")
    def seller_inventory():
        # Inventory is account-centric: imported accounts remain manageable
        # before phone verification or OAuth credential creation.
        accounts = _account_rows()
        def phone_order(row: dict) -> tuple[float, str]:
            try:
                verified_at = datetime.fromisoformat(
                    str(row.get("phone_verified_at") or "").replace("Z", "+00:00")
                ).timestamp()
            except (TypeError, ValueError):
                verified_at = float("inf")
            return verified_at, str(row.get("email") or "").casefold()

        accounts.sort(key=phone_order)
        quotas = quota_store.list()
        for row in accounts:
            row["quota"] = quotas.get(str(row.get("id") or ""))
        summary = {
            "total": len(accounts),
            "credential_total": sum(bool(row.get("has_credential")) for row in accounts),
            "unexported": sum(int(row.get("export_count") or 0) == 0 for row in accounts),
            "exported": sum(int(row.get("export_count") or 0) > 0 for row in accounts),
            "export_count": sum(int(row.get("export_count") or 0) for row in accounts),
            "quota_ok": sum((row.get("quota") or {}).get("status") == "ok" for row in accounts),
            "phone_verified": sum(bool(row.get("phone_verified")) for row in accounts),
            "phone_unverified": sum(not row.get("phone_verified") for row in accounts),
        }
        return jsonify({"ok": True, "accounts": accounts, "summary": summary})

    @app.post("/api/seller/quota/refresh")
    def refresh_seller_quota():
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "请提交 JSON 对象"}), 400
        try:
            normalized = _requested_inventory_account_ids(data, "刷新套餐和额度")
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        def refresh_one(account_id: str) -> tuple[str, dict]:
            try:
                return account_id, _refresh_account_metadata(account_id, include_quota=True)
            except Exception as exc:
                quota_store.record_subscription_error(account_id, exc)
                return account_id, quota_store.record_error(account_id, exc)

        results = {}
        with ThreadPoolExecutor(max_workers=min(5, len(normalized))) as executor:
            futures = [executor.submit(refresh_one, account_id) for account_id in normalized]
            for future in as_completed(futures):
                account_id, result = future.result()
                results[account_id] = result
        relogin_ids, relogin_pipeline, relogin_error = _queue_failed_token_relogins(
            normalized, results
        )
        success = sum(
            item.get("status") == "ok"
            and item.get("subscription_status") == "ok"
            for item in results.values()
        )
        return jsonify(
            {
                "ok": True,
                "total": len(results),
                "results": results,
                "success": success,
                "failed": len(results) - success,
                "relogin_required": len(relogin_ids),
                "relogin_queued": len(relogin_ids) if relogin_pipeline else 0,
                "relogin_pipeline": relogin_pipeline,
                "relogin_error": relogin_error,
            }
        )

    @app.post("/api/seller/subscription/refresh")
    def refresh_seller_subscription():
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "请提交 JSON 对象"}), 400
        try:
            normalized = _requested_inventory_account_ids(data, "刷新套餐")
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        def refresh_one(account_id: str) -> tuple[str, dict]:
            try:
                return account_id, _refresh_account_metadata(account_id, include_quota=False)
            except Exception as exc:
                return account_id, quota_store.record_subscription_error(account_id, exc)

        results = {}
        with ThreadPoolExecutor(max_workers=min(5, len(normalized))) as executor:
            futures = [executor.submit(refresh_one, account_id) for account_id in normalized]
            for future in as_completed(futures):
                account_id, result = future.result()
                results[account_id] = result
        relogin_ids, relogin_pipeline, relogin_error = _queue_failed_token_relogins(
            normalized, results
        )
        success = sum(
            item.get("subscription_status") == "ok" for item in results.values()
        )
        return jsonify(
            {
                "ok": True,
                "total": len(results),
                "results": results,
                "success": success,
                "failed": len(results) - success,
                "relogin_required": len(relogin_ids),
                "relogin_queued": len(relogin_ids) if relogin_pipeline else 0,
                "relogin_pipeline": relogin_pipeline,
                "relogin_error": relogin_error,
            }
        )

    @app.post("/api/seller/credentials/relogin")
    def relogin_seller_credentials():
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "请提交 JSON 对象"}), 400
        try:
            normalized = _requested_inventory_account_ids(data, "重新登录刷新凭证")
            pipeline = _start_credential_relogin(normalized)
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400
        return jsonify(
            {"ok": True, "queued": len(normalized), "pipeline": pipeline}
        ), 202

    @app.post("/api/seller/export")
    def export_inventory_accounts():
        if not _is_loopback_host(settings.host):
            return jsonify({"ok": False, "error": "账号库存仅允许从本机 WebUI 导出"}), 403
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict) or data.get("confirmed") is not True:
            return jsonify({"ok": False, "error": "导出前必须明确确认"}), 400
        try:
            export_formats = _normalize_export_formats(data, default="original")
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        needs_credential = any(value != "original" for value in export_formats)
        phone_state = str(data.get("phone_state") or "all").strip().lower()
        if phone_state not in {"all", "verified", "unverified"}:
            return jsonify({"ok": False, "error": "接码状态筛选无效"}), 400
        plan_state = str(data.get("plan_state") or "all").strip().lower()
        if plan_state not in {"all", "plus", "free", "other", "unknown"}:
            return jsonify({"ok": False, "error": "套餐筛选无效"}), 400

        def parse_phone_time(key: str) -> datetime | None:
            value = str(data.get(key) or "").strip()
            if not value:
                return None
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("接码时间筛选格式无效") from exc
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)

        try:
            phone_verified_from = parse_phone_time("phone_verified_from")
            phone_verified_to = parse_phone_time("phone_verified_to")
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        if phone_verified_from and phone_verified_to and phone_verified_from > phone_verified_to:
            return jsonify({"ok": False, "error": "接码开始时间不能晚于结束时间"}), 400

        def matches_phone_filters(row: dict) -> bool:
            if phone_state == "verified" and not row.get("phone_verified"):
                return False
            if phone_state == "unverified" and row.get("phone_verified"):
                return False
            plan_type = str(row.get("subscription_plan_type") or "").strip().lower()
            if plan_state == "plus" and plan_type != "plus":
                return False
            if plan_state == "free" and plan_type != "free":
                return False
            if plan_state == "unknown" and plan_type:
                return False
            if plan_state == "other" and plan_type in {"", "plus", "free"}:
                return False
            if phone_verified_from is None and phone_verified_to is None:
                return True
            try:
                verified_at = datetime.fromisoformat(
                    str(row.get("phone_verified_at") or "").replace("Z", "+00:00")
                )
            except ValueError:
                return False
            if verified_at.tzinfo is None:
                verified_at = verified_at.replace(tzinfo=timezone.utc)
            return (
                (phone_verified_from is None or verified_at >= phone_verified_from)
                and (phone_verified_to is None or verified_at <= phone_verified_to)
            )

        inventory = _account_rows()
        inventory.sort(
            key=lambda row: str(
                row.get("credential_modified_at")
                or row.get("created_at")
                or row.get("updated_at")
                or ""
            )
        )
        inventory_by_id = {str(row.get("id") or ""): row for row in inventory}
        requested_ids = data.get("account_ids")
        all_unexported = data.get("all_unexported") is True

        if requested_ids is not None:
            if not isinstance(requested_ids, list) or not requested_ids or len(requested_ids) > 500:
                return jsonify({"ok": False, "error": "请选择 1 - 500 个库存账号"}), 400
            normalized = list(dict.fromkeys(str(value or "").strip() for value in requested_ids))
            if any(not value for value in normalized):
                return jsonify({"ok": False, "error": "账号 ID 格式无效"}), 400
            if any(value not in inventory_by_id for value in normalized):
                return jsonify({"ok": False, "error": "所选库存账号不存在"}), 404
            selected = [inventory_by_id[value] for value in normalized]
            name_scope = "selected"
            export_mode = "selected"
        elif all_unexported:
            selected = [
                row
                for row in inventory
                if int(row.get("export_count") or 0) == 0 and matches_phone_filters(row)
            ]
            if needs_credential:
                selected = [row for row in selected if row.get("has_credential")]
            if not selected:
                return jsonify({"ok": False, "error": "当前没有符合该格式的未导出账号"}), 409
            if len(selected) > 500:
                return jsonify({"ok": False, "error": "未导出账号超过 500 个，请分批导出"}), 413
            name_scope = "all-unexported"
            export_mode = "all_unexported"
        else:
            export_state = str(data.get("export_state") or "unexported").strip().lower()
            if export_state not in {"all", "unexported", "exported"}:
                return jsonify({"ok": False, "error": "导出状态筛选无效"}), 400
            candidates = [
                row
                for row in inventory
                if (
                    export_state == "all"
                    or (export_state == "unexported" and int(row.get("export_count") or 0) == 0)
                    or (export_state == "exported" and int(row.get("export_count") or 0) > 0)
                )
                and matches_phone_filters(row)
            ]
            if needs_credential:
                candidates = [row for row in candidates if row.get("has_credential")]
            try:
                count = int(data.get("count", 1))
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "导出数量必须是整数"}), 400
            if count < 1 or count > 100:
                return jsonify({"ok": False, "error": "每次可导出 1 - 100 个账号"}), 400
            selected = candidates[:count]
            if len(selected) < count:
                return jsonify(
                    {"ok": False, "error": f"当前筛选及格式下只有 {len(selected)} 个可导出账号"}
                ), 409
            name_scope = export_state
            export_mode = "count"

        if needs_credential:
            missing_credentials = [row for row in selected if not row.get("has_credential")]
            if missing_credentials:
                return jsonify(
                    {
                        "ok": False,
                        "error": (
                            f"所选账号中有 {len(missing_credentials)} 个尚无 OAuth 凭证；"
                            "请改用原导入格式，或只选择已有凭证账号"
                        ),
                    }
                ), 409

        account_ids = [str(row.get("id") or "") for row in selected]
        try:
            content, filename, mimetype = _account_export_content(
                account_ids, export_formats, scope=name_scope
            )
            response = _remembered_download(
                content,
                filename=filename,
                mimetype=mimetype,
                kind="seller_inventory",
                formats=export_formats,
                account_count=len(account_ids),
            )
            mailbox_store.record_exports(account_ids)
            response.headers["X-Exported-Account-Count"] = str(len(account_ids))
            response.headers["X-Export-Mode"] = export_mode
            return response
        except (ValueError, KeyError, OverflowError) as exc:
            return _selection_error(exc)

    @app.get("/api/artifacts/credentials.zip")
    def download_all_credentials():
        blocked = _download_guard()
        if blocked is not None:
            return blocked
        return _zip_download(
            artifact_store.exportable_credential_files(),
            filename="codex-credentials.zip",
        )

    @app.post("/api/artifacts/credentials/selected.zip")
    def download_selected_credentials():
        if not _is_loopback_host(settings.host):
            return jsonify({"ok": False, "error": "敏感文件仅允许从本机监听的 WebUI 下载"}), 403
        try:
            rows = _selected_credential_files(request.get_json(silent=True))
        except (ValueError, KeyError, OverflowError) as exc:
            return _selection_error(exc)
        return _zip_download(rows, filename="codex-selected-credentials.zip")

    @app.post("/api/artifacts/credentials/selected/export")
    def export_selected_credentials():
        if not _is_loopback_host(settings.host):
            return jsonify({"ok": False, "error": "敏感文件仅允许从本机监听的 WebUI 下载"}), 403
        data = request.get_json(silent=True)
        try:
            rows = _selected_credential_files(data)
        except (ValueError, KeyError, OverflowError) as exc:
            return _selection_error(exc)
        try:
            formats = _normalize_export_formats(data or {}, default="codex_json")
            if "original" in formats:
                raise ValueError("凭证导出仅支持 Codex JSON 和 Sub2API")
            content, filename, mimetype = _credential_export_content(
                rows, formats, scope="selected-credentials"
            )
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return _remembered_download(
            content,
            filename=filename,
            mimetype=mimetype,
            kind="credentials",
            formats=formats,
            account_count=len(rows),
        )

    @app.delete("/api/artifacts/credentials/selected")
    def delete_selected_credentials():
        if not _is_loopback_host(settings.host):
            return jsonify({"ok": False, "error": "敏感文件仅允许从本机 WebUI 管理"}), 403
        data = request.get_json(silent=True)
        try:
            rows = _selected_credential_files(data)
            ids_by_path = {}
            for item in artifact_store.list_credentials():
                if not item.get("exportable") or not item.get("id"):
                    continue
                credential_path = artifact_store.credential_file(str(item.get("id") or ""))
                if credential_path is not None:
                    ids_by_path[str(credential_path.resolve())] = str(item.get("id") or "")
            artifact_ids = [ids_by_path[str(path.resolve())] for path, _ in rows]
            deleted = artifact_store.delete_credentials(artifact_ids)
        except (ValueError, KeyError, OverflowError, OSError) as exc:
            return _selection_error(exc)
        return jsonify({"ok": True, "deleted": deleted})

    @app.get("/api/artifacts/logs.zip")
    def download_all_logs():
        blocked = _download_guard()
        if blocked is not None:
            return blocked
        return _zip_download(artifact_store.log_files(), filename="codex-logs.zip")

    @app.post("/api/sms-config")
    def save_sms_config():
        if not _is_loopback_host(settings.host):
            return jsonify({"ok": False, "error": "敏感短信凭证仅允许在本机监听模式下保存"}), 403
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "请提交 JSON 对象"}), 400
        if any(
            str(job.get("status") or "") in {"queued", "running", "retry_wait"}
            for job in codex_manager.list_jobs()
        ):
            return jsonify({"ok": False, "error": "Codex OAuth 任务运行中，请结束后再修改短信配置"}), 409
        try:
            config = sms_config_store.save(data)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except OSError as exc:
            return jsonify({"ok": False, "error": f"无法写入 .env：{exc}"}), 500
        return jsonify(
            {
                "ok": True,
                "config": config,
                "codex": codex_manager.availability(),
                "runtime_config": codex_manager.runtime_config(),
            }
        )

    @app.post("/api/sms-config/reveal")
    def reveal_sms_credential():
        if not _is_loopback_host(settings.host):
            return jsonify({"ok": False, "error": "敏感短信凭证仅允许在本机监听模式下显示"}), 403
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict) or data.get("confirmed") is not True:
            return jsonify({"ok": False, "error": "必须明确确认显示凭证"}), 400
        try:
            credential = sms_config_store.reveal_credential("hero")
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        if not credential:
            return jsonify({"ok": False, "error": "Hero SMS 尚未保存 API Key"}), 404
        return jsonify({"ok": True, "credential": credential})

    @app.post("/api/accounts/selected/export")
    def export_selected_accounts():
        if not _is_loopback_host(settings.host):
            return jsonify({"ok": False, "error": "账号素材仅允许从本机监听的 WebUI 下载"}), 403
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or data.get("confirmed") is not True:
            return jsonify({"ok": False, "error": "导出前必须明确确认"}), 400
        account_ids = data.get("account_ids", [])
        if not isinstance(account_ids, list) or not account_ids:
            return jsonify({"ok": False, "error": "请至少选择一个账号"}), 400
        if len(account_ids) > 500:
            return jsonify({"ok": False, "error": "每次最多导出 500 个账号"}), 413
        if any(not isinstance(value, str) or not value.strip() for value in account_ids):
            return jsonify({"ok": False, "error": "账号 ID 格式无效"}), 400
        try:
            content = _original_accounts_bytes(account_ids)
            return _remembered_download(
                content,
                filename="account-materials-original-format.zip",
                mimetype="application/zip",
                kind="account_materials",
                formats=["original"],
                account_count=len(account_ids),
            )
        except (ValueError, KeyError) as exc:
            return _selection_error(exc)

    @app.post("/api/accounts/phone-unverified/export")
    def export_phone_unverified_accounts():
        if not _is_loopback_host(settings.host):
            return jsonify({"ok": False, "error": "未接码账号素材仅允许从本机 WebUI 导出"}), 403
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict) or data.get("confirmed") is not True:
            return jsonify({"ok": False, "error": "导出未接码账号前必须明确确认"}), 400
        credential_only = data.get("credential_only") is True
        rows = [
            row
            for row in _account_rows()
            if not row.get("phone_verified")
            and (not credential_only or row.get("has_credential"))
        ]
        rows.sort(key=lambda row: str(row.get("created_at") or row.get("updated_at") or ""))
        raw_count = data.get("count")
        if raw_count not in (None, ""):
            try:
                count = int(raw_count)
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "导出数量必须是整数"}), 400
            if count < 1 or count > 500:
                return jsonify({"ok": False, "error": "每次可导出 1 - 500 个未接码账号"}), 400
            if len(rows) < count:
                return jsonify(
                    {"ok": False, "error": f"当前未接码账号只有 {len(rows)} 个"}
                ), 409
            rows = rows[:count]
        if not rows:
            return jsonify({"ok": False, "error": "当前没有未接码账号"}), 409
        account_ids = [str(row.get("id") or "") for row in rows]
        try:
            response = _original_accounts_download(
                account_ids, "phone-unverified-original-format.zip"
            )
        except (ValueError, KeyError) as exc:
            return _selection_error(exc)
        response.headers["X-Exported-Account-Count"] = str(len(account_ids))
        response.headers["X-Account-Phone-Status"] = "unverified"
        response.headers["X-Account-Scope"] = "seller" if credential_only else "all"
        if credential_only:
            try:
                mailbox_store.record_exports(account_ids)
            except (ValueError, KeyError) as exc:
                return _selection_error(exc)
        return response

    @app.delete("/api/accounts/selected")
    def delete_selected_accounts():
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or data.get("confirmed") is not True:
            return jsonify({"ok": False, "error": "删除前必须明确确认"}), 400
        account_ids = data.get("account_ids", [])
        if not isinstance(account_ids, list) or not account_ids:
            return jsonify({"ok": False, "error": "请至少选择一个账号"}), 400
        if len(account_ids) > 500:
            return jsonify({"ok": False, "error": "每次最多删除 500 个账号"}), 413
        accounts = []
        for account_id in account_ids:
            if not isinstance(account_id, str) or not account_id.strip():
                return jsonify({"ok": False, "error": "账号 ID 格式无效"}), 400
            account = mailbox_store.get_secret(account_id=account_id.strip())
            if account is None:
                return jsonify({"ok": False, "error": "所选账号不存在"}), 404
            accounts.append(account)
        active = getattr(codex_manager, "is_account_active", None)
        if callable(active) and any(active(str(account.get("email") or "")) for account in accounts):
            return jsonify({"ok": False, "error": "所选账号中有正在运行的任务"}), 409
        try:
            deleted = mailbox_store.delete_many(account_ids)
        except (ValueError, KeyError) as exc:
            return _selection_error(exc)
        return jsonify({"ok": True, "deleted": deleted})

    @app.delete("/api/accounts/<account_id>")
    def delete_account(account_id: str):
        account = mailbox_store.get_secret(account_id=account_id)
        if account is None:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        active = getattr(codex_manager, "is_account_active", None)
        if callable(active) and active(str(account.get("email") or "")):
            return jsonify({"ok": False, "error": "账号正在流水线中，暂时不能删除"}), 409
        if not mailbox_store.delete(account_id):
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        return jsonify({"ok": True})

    @app.post("/api/codex-pipeline")
    def start_codex_pipeline():
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict) or data.get("confirmed") is not True:
            return jsonify({"ok": False, "error": "必须确认流水线可能消耗邮箱 OTP 和短信号码"}), 400
        emails = data.get("emails")
        if not isinstance(emails, list):
            return jsonify({"ok": False, "error": "流水线账号列表格式不正确"}), 400
        starter = getattr(codex_manager, "start_batch", None)
        if not callable(starter):
            return jsonify({"ok": False, "error": "当前任务管理器不支持流水线"}), 501
        try:
            pipeline = starter(
                emails,
                concurrency=data.get("concurrency", 1),
                retry_limit=data.get("retry_limit", 0),
                retry_backoff_seconds=data.get("retry_backoff_seconds", 30),
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400
        return jsonify({"ok": True, "pipeline": pipeline}), 202

    @app.post("/api/codex-pipeline/<pipeline_id>/stop")
    def stop_codex_pipeline(pipeline_id: str):
        stopper = getattr(codex_manager, "stop_pipeline", None)
        if not callable(stopper) or not stopper(pipeline_id):
            return jsonify({"ok": False, "error": "流水线不存在或已经结束"}), 404
        return jsonify({"ok": True, "pipeline": _pipeline_overview()})

    @app.post("/api/codex-pipeline/<pipeline_id>/pause")
    def pause_codex_pipeline(pipeline_id: str):
        pauser = getattr(codex_manager, "pause_pipeline", None)
        if not callable(pauser):
            return jsonify({"ok": False, "error": "当前任务管理器不支持暂停"}), 501
        if not pauser(pipeline_id):
            return jsonify({"ok": False, "error": "流水线不存在、已暂停或已结束"}), 409
        return jsonify({"ok": True, "pipeline": _pipeline_overview()})

    @app.post("/api/codex-pipeline/<pipeline_id>/force-pause")
    def force_pause_codex_pipeline(pipeline_id: str):
        pauser = getattr(codex_manager, "force_pause_pipeline", None)
        if not callable(pauser):
            return jsonify({"ok": False, "error": "当前任务管理器不支持强制暂停"}), 501
        pipeline = pauser(pipeline_id)
        if pipeline is None:
            return jsonify({"ok": False, "error": "流水线不存在或已经结束"}), 409
        return jsonify({"ok": True, "pipeline": pipeline})

    @app.post("/api/codex-pipeline/<pipeline_id>/concurrency")
    def set_codex_pipeline_concurrency(pipeline_id: str):
        data = request.get_json(silent=True) or {}
        setter = getattr(codex_manager, "set_pipeline_concurrency", None)
        if not callable(setter):
            return jsonify({"ok": False, "error": "当前任务管理器不支持动态调整并发"}), 501
        try:
            pipeline = setter(pipeline_id, data.get("concurrency"))
        except (TypeError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        if pipeline is None:
            return jsonify({"ok": False, "error": "流水线不存在或已经结束"}), 409
        return jsonify({"ok": True, "pipeline": pipeline})

    @app.post("/api/codex-pipeline/<pipeline_id>/resume")
    def resume_codex_pipeline(pipeline_id: str):
        resumer = getattr(codex_manager, "resume_pipeline", None)
        if not callable(resumer):
            return jsonify({"ok": False, "error": "当前任务管理器不支持继续"}), 501
        if not resumer(pipeline_id):
            return jsonify({"ok": False, "error": "流水线不存在、未暂停或已结束"}), 409
        return jsonify({"ok": True, "pipeline": _pipeline_overview()})

    @app.post("/api/codex-jobs")
    def start_codex_job():
        data = request.get_json(silent=True) or {}
        email = str(data.get("email") or "").strip()
        if data.get("confirmed") is not True:
            return jsonify({"ok": False, "error": "必须确认该操作可能消耗邮箱 OTP 和短信号码"}), 400
        if not any(str(item.get("email") or "").lower() == email.lower() for item in mailbox_store.list_accounts()):
            return jsonify({"ok": False, "error": "请先导入该已有账号的登录素材"}), 400
        try:
            job = codex_manager.start(email)
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400
        return jsonify({"ok": True, "job": job}), 202

    @app.post("/api/codex-jobs/<job_id>/stop")
    def stop_codex_job(job_id: str):
        if not codex_manager.stop(job_id):
            return jsonify({"ok": False, "error": "任务不存在或已经结束"}), 404
        return jsonify({"ok": True})

    app.extensions["codex_manager"] = codex_manager
    app.extensions["mailbox_store"] = mailbox_store
    app.extensions["sms_config_store"] = sms_config_store
    app.extensions["hero_catalog"] = hero_catalog
    app.extensions["hero_pricing"] = hero_pricing
    app.extensions["artifact_store"] = artifact_store
    app.extensions["quota_store"] = quota_store
    return app
