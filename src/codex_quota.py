from __future__ import annotations

import json
import os
import secrets
import threading
import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import requests


USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
ACCOUNT_CHECK_URL = "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27"
SUBSCRIPTIONS_URL = "https://chatgpt.com/backend-api/subscriptions"
TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_PLAN_TYPES = {"free", "plus", "pro", "team", "business", "enterprise"}
_PLAN_ALIASES = {
    "chatgptfreeplan": "free",
    "freeplan": "free",
    "chatgptplusplan": "plus",
    "plusplan": "plus",
    "chatgptproplan": "pro",
    "proplan": "pro",
    "chatgptteamplan": "team",
    "teamplan": "team",
    "chatgptbusinessplan": "business",
    "businessplan": "business",
    "chatgptenterpriseplan": "enterprise",
    "enterpriseplan": "enterprise",
}


def _normalize_plan_type(value: object) -> str:
    raw = str(value or "").strip().lower()
    compact = "".join(character for character in raw if character.isalnum())
    return _PLAN_ALIASES.get(compact, raw)


class CodexHttpError(RuntimeError):
    def __init__(self, label: str, status_code: int):
        self.status_code = int(status_code or 0)
        super().__init__(f"{label} HTTP {self.status_code or 'unknown'}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _remaining(window: object) -> dict | None:
    if not isinstance(window, Mapping):
        return None
    try:
        used = int(window.get("used_percent"))
    except (TypeError, ValueError):
        return None
    if used < 0 or used > 100:
        return None
    try:
        seconds = int(window.get("limit_window_seconds"))
        minutes = (seconds + 59) // 60 if seconds > 0 else None
    except (TypeError, ValueError):
        minutes = None
    try:
        reset_at = int(window.get("reset_at"))
    except (TypeError, ValueError):
        reset_at = None
    if reset_at is None:
        try:
            reset_after = int(window.get("reset_after_seconds"))
        except (TypeError, ValueError):
            reset_after = None
        if reset_after is not None and reset_after >= 0:
            reset_at = int(datetime.now(timezone.utc).timestamp()) + reset_after
    return {
        "remaining_percent": 100 - used,
        "used_percent": used,
        "window_minutes": minutes,
        "reset_at": reset_at,
    }


def _jwt_claims(token: object) -> dict[str, Any]:
    parts = str(token or "").split(".")
    if len(parts) < 2 or len(parts[1]) > 128 * 1024:
        return {}
    try:
        encoded = parts[1] + "=" * (-len(parts[1]) % 4)
        value = json.loads(base64.urlsafe_b64decode(encoded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _access_token_needs_refresh(credential: Mapping, *, leeway_seconds: int = 60) -> bool:
    claims = _jwt_claims(credential.get("access_token"))
    try:
        expires_at = int(claims.get("exp"))
    except (TypeError, ValueError):
        return False
    return expires_at <= int(datetime.now(timezone.utc).timestamp()) + max(0, leeway_seconds)


def refresh_codex_tokens(credential: Mapping, *, timeout: int = 20) -> dict[str, Any]:
    refresh_token = str(credential.get("refresh_token") or "").strip()
    if not refresh_token:
        raise ValueError("凭证缺少 refresh_token")
    response = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": CODEX_CLIENT_ID,
            "refresh_token": refresh_token,
        },
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        timeout=timeout,
    )
    if response.status_code != 200:
        raise CodexHttpError("Token 刷新接口", response.status_code)
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("Token 刷新接口未返回 JSON") from exc
    if not isinstance(payload, Mapping) or not str(payload.get("access_token") or "").strip():
        raise RuntimeError("Token 刷新接口缺少 access_token")
    updated = dict(credential)
    for key in ("access_token", "refresh_token", "id_token"):
        value = str(payload.get(key) or "").strip()
        if value:
            updated[key] = value
    claims = _jwt_claims(updated.get("id_token")) or _jwt_claims(updated.get("access_token"))
    auth = claims.get("https://api.openai.com/auth") if isinstance(claims, dict) else {}
    if isinstance(auth, Mapping):
        account_id = str(auth.get("chatgpt_account_id") or "").strip()
        if account_id:
            updated["account_id"] = account_id
    try:
        expires_in = max(0, int(payload.get("expires_in") or 0))
    except (TypeError, ValueError):
        expires_in = 0
    updated["last_refresh"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if expires_in:
        updated["expired"] = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    return updated


def _chatgpt_headers(credential: Mapping, target_path: str, *, include_account: bool = False) -> dict:
    access_token = str(credential.get("access_token") or "").strip()
    if not access_token:
        raise ValueError("凭证缺少 access_token")
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Referer": "https://chatgpt.com/",
        "User-Agent": "Mozilla/5.0 Codex-Seller-Console/1.0",
        "x-openai-target-path": target_path,
        "x-openai-target-route": target_path,
    }
    account_id = str(
        credential.get("account_id") or credential.get("chatgpt_account_id") or ""
    ).strip()
    if include_account and account_id:
        headers["ChatGPT-Account-Id"] = account_id
    return headers


def _response_json(response: object, label: str) -> dict:
    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code != 200:
        raise CodexHttpError(label, status_code)
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"{label}未返回 JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{label}响应格式无效")
    return payload


def _subscription_records(payload: Mapping) -> list[tuple[str, dict]]:
    accounts = payload.get("accounts")
    if isinstance(accounts, list):
        return [("", item) for item in accounts if isinstance(item, dict)]
    if isinstance(accounts, Mapping):
        return [(str(key), item) for key, item in accounts.items() if isinstance(item, dict)]
    return [("", dict(payload))]


def _record_account(record: Mapping) -> Mapping:
    value = record.get("account")
    return value if isinstance(value, Mapping) else record


def _record_account_id(record: Mapping) -> str:
    account = _record_account(record)
    return str(
        account.get("account_id")
        or account.get("id")
        or account.get("chatgpt_account_id")
        or account.get("workspace_id")
        or ""
    ).strip()


def _select_subscription_record(payload: Mapping, account_id: str) -> Mapping:
    records = _subscription_records(payload)
    if not records:
        raise RuntimeError("订阅账号接口没有可用账号")
    if account_id:
        matched = next((record for _, record in records if _record_account_id(record) == account_id), None)
        if matched is not None:
            return matched
    ordering = payload.get("account_ordering")
    first_key = str(ordering[0]) if isinstance(ordering, list) and ordering else ""
    if first_key:
        matched = next((record for key, record in records if key == first_key), None)
        if matched is not None:
            return matched
    non_free = next(
        (
            record
            for _, record in records
            if str(
                (record.get("entitlement") or {}).get("subscription_plan")
                if isinstance(record.get("entitlement"), Mapping)
                else ""
            ).strip().lower()
            not in {"", "free"}
        ),
        None,
    )
    return non_free or records[0][1]


def _subscription_values(payload: Mapping, account_id: str) -> dict[str, str]:
    record = _select_subscription_record(payload, account_id)
    account = _record_account(record)
    entitlement = record.get("entitlement") if isinstance(record.get("entitlement"), Mapping) else {}
    return {
        "account_id": _record_account_id(record) or account_id,
        "plan_type": _normalize_plan_type(
            entitlement.get("subscription_plan")
            or account.get("plan_type")
            or account.get("planType")
            or ""
        ),
        "subscription_active_until": str(
            entitlement.get("expires_at") or account.get("expires_at") or ""
        ).strip(),
    }


def _subscription_missing_or_expired(value: object) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return True
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return True
        return parsed <= datetime.now(timezone.utc)
    except ValueError:
        try:
            return int(raw) <= int(datetime.now(timezone.utc).timestamp())
        except ValueError:
            return True


def query_codex_subscription(credential: Mapping, *, timeout: int = 20) -> dict:
    account_id = str(
        credential.get("account_id") or credential.get("chatgpt_account_id") or ""
    ).strip()
    offset = datetime.now().astimezone().utcoffset()
    timezone_offset_min = -int((offset.total_seconds() if offset else 0) / 60)
    checked = _response_json(
        requests.get(
            ACCOUNT_CHECK_URL,
            params={"timezone_offset_min": timezone_offset_min},
            headers=_chatgpt_headers(
                credential, "/backend-api/accounts/check/v4-2023-04-27"
            ),
            timeout=timeout,
        ),
        "订阅账号接口",
    )
    result = _subscription_values(checked, account_id)
    source = "accounts_check"
    if _subscription_missing_or_expired(result.get("subscription_active_until")):
        resolved_id = str(result.get("account_id") or account_id).strip()
        if resolved_id:
            try:
                payload = _response_json(
                    requests.get(
                        SUBSCRIPTIONS_URL,
                        params={"account_id": resolved_id},
                        headers=_chatgpt_headers(credential, "/backend-api/subscriptions"),
                        timeout=timeout,
                    ),
                    "订阅信息接口",
                )
            except CodexHttpError as exc:
                if exc.status_code != 404:
                    raise
                # Free accounts commonly have no paid subscription resource.
                # The account-check result remains authoritative; a missing
                # optional expiry record is not a refresh failure.
                payload = None
            if payload is not None:
                root = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
                plan_type = _normalize_plan_type(
                    root.get("subscription_plan") or root.get("plan_type") or ""
                )
                active_until = str(root.get("active_until") or root.get("expires_at") or "").strip()
                if plan_type:
                    result["plan_type"] = plan_type
                if active_until:
                    result["subscription_active_until"] = active_until
                source = "subscriptions"
    return {
        "status": "ok",
        "checked_at": _now(),
        "plan_type": _normalize_plan_type(result.get("plan_type")),
        "subscription_active_until": str(result.get("subscription_active_until") or "").strip(),
        "account_id": str(result.get("account_id") or account_id).strip(),
        "source": source,
    }


def _write_credential(path: Path, payload: Mapping) -> None:
    temporary = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    with temporary.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(dict(payload), handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def refresh_codex_credential_metadata(
    path: Path,
    *,
    include_quota: bool = True,
    timeout: int = 20,
) -> dict:
    path = Path(path)
    try:
        credential = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("OAuth 凭证文件读取失败") from exc
    if not isinstance(credential, dict):
        raise ValueError("OAuth 凭证格式无效")
    token_refreshed = False
    if _access_token_needs_refresh(credential):
        credential = refresh_codex_tokens(credential, timeout=timeout)
        token_refreshed = True
    for attempt in range(2):
        subscription = quota = None
        subscription_error = quota_error = None
        try:
            subscription = query_codex_subscription(credential, timeout=timeout)
        except Exception as exc:
            subscription_error = exc
        if include_quota:
            try:
                quota = query_codex_quota(credential, timeout=timeout)
            except Exception as exc:
                quota_error = exc
        unauthorized = any(
            isinstance(error, CodexHttpError) and error.status_code == 401
            for error in (subscription_error, quota_error)
        )
        if unauthorized and not token_refreshed and attempt == 0:
            credential = refresh_codex_tokens(credential, timeout=timeout)
            token_refreshed = True
            continue
        break
    changed = token_refreshed
    if subscription:
        subscription = dict(subscription)
        current_plan = _normalize_plan_type(credential.get("plan_type"))
        current_active_until = str(
            credential.get("subscription_active_until") or ""
        ).strip()
        plan_type = _normalize_plan_type(subscription.get("plan_type"))
        active_until = str(subscription.get("subscription_active_until") or "").strip()
        current_paid_is_active = (
            current_plan in {"plus", "pro", "team", "business", "enterprise"}
            and not _subscription_missing_or_expired(current_active_until)
        )
        if current_paid_is_active and plan_type in {"", "free"}:
            # A still-valid paid entitlement is stronger than a transient Free
            # response. Keep it until its recorded expiry while still recording
            # that the remote check completed.
            plan_type = current_plan
            active_until = current_active_until
            subscription["plan_type"] = plan_type
            subscription["subscription_active_until"] = active_until
            subscription["preserved_active_plan"] = True
        elif current_paid_is_active and _subscription_missing_or_expired(active_until):
            active_until = current_active_until
            subscription["subscription_active_until"] = active_until
        if plan_type:
            credential["plan_type"] = plan_type
        if active_until:
            credential["subscription_active_until"] = active_until
        account_id = str(subscription.get("account_id") or "").strip()
        if account_id:
            credential["account_id"] = account_id
        credential["subscription_checked_at"] = subscription["checked_at"]
        credential["subscription_source"] = subscription["source"]
        credential.pop("subscription_error", None)
        changed = True
    elif subscription_error:
        credential["subscription_checked_at"] = _now()
        credential["subscription_error"] = str(subscription_error)[:160]
        changed = True
    if changed:
        _write_credential(path, credential)
    return {
        "credential": credential,
        "subscription": subscription,
        "subscription_error": subscription_error,
        "quota": quota,
        "quota_error": quota_error,
        "token_refreshed": token_refreshed,
    }


def query_codex_quota(credential: Mapping, *, timeout: int = 20) -> dict:
    """Query the Codex usage endpoint without retaining OAuth secrets."""

    access_token = str(credential.get("access_token") or "").strip()
    account_id = str(
        credential.get("account_id") or credential.get("chatgpt_account_id") or ""
    ).strip()
    if not access_token:
        raise ValueError("凭证缺少 access_token")
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Referer": "https://chatgpt.com/",
        "User-Agent": "Mozilla/5.0 Codex-Seller-Console/1.0",
        "OpenAI-Beta": "codex-1",
        "originator": "Codex Desktop",
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    response = requests.get(USAGE_URL, headers=headers, timeout=timeout)
    if response.status_code != 200:
        raise CodexHttpError("额度接口", response.status_code)
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("额度接口未返回 JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("额度接口响应格式无效")
    rate_limit = payload.get("rate_limit")
    if not isinstance(rate_limit, Mapping):
        rate_limit = {}
    return {
        "status": "ok",
        "checked_at": _now(),
        "plan_type": _normalize_plan_type(
            payload.get("plan_type")
            or credential.get("plan_type")
            or (
                credential.get("type")
                if str(credential.get("type") or "").strip().lower() in _PLAN_TYPES
                else ""
            )
            or ""
        ),
        "allowed": rate_limit.get("allowed"),
        "limit_reached": rate_limit.get("limit_reached"),
        "primary": _remaining(rate_limit.get("primary_window")),
        "secondary": _remaining(rate_limit.get("secondary_window")),
    }


class CodexQuotaStore:
    """Persist only quota summaries; OAuth tokens remain in credential artifacts."""

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "codex-quota.json"
        self._lock = threading.RLock()

    def _read(self) -> dict[str, dict]:
        if not self.path.is_file():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def _write(self, value: Mapping[str, Mapping]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.parent / f".{self.path.name}.{secrets.token_hex(8)}.tmp"
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)

    def list(self) -> dict[str, dict]:
        with self._lock:
            return self._read()

    def put(self, account_id: str, result: Mapping) -> dict:
        safe = {
            key: result.get(key)
            for key in (
                "status",
                "checked_at",
                "plan_type",
                "allowed",
                "limit_reached",
                "primary",
                "secondary",
                "error",
            )
            if key in result
        }
        with self._lock:
            rows = self._read()
            existing = rows.get(str(account_id))
            merged = dict(existing) if isinstance(existing, dict) else {}
            merged.update(safe)
            if str(safe.get("status") or "") == "ok":
                merged.pop("error", None)
            rows[str(account_id)] = merged
            self._write(rows)
        return merged

    def put_subscription(self, account_id: str, result: Mapping) -> dict:
        safe = {
            "subscription_status": str(result.get("status") or "unknown")[:20],
            "subscription_checked_at": result.get("checked_at"),
            "subscription_plan_type": str(result.get("plan_type") or "")[:40],
            "subscription_active_until": str(
                result.get("subscription_active_until") or ""
            )[:80],
            "subscription_source": str(result.get("source") or "")[:40],
        }
        with self._lock:
            rows = self._read()
            existing = rows.get(str(account_id))
            merged = dict(existing) if isinstance(existing, dict) else {}
            merged.update(safe)
            merged.pop("subscription_error", None)
            rows[str(account_id)] = merged
            self._write(rows)
        return merged

    def record_subscription_error(self, account_id: str, error: object) -> dict:
        message = str(error or "套餐查询失败")
        if "HTTP " not in message and message not in {
            "凭证缺少 access_token",
            "凭证缺少 refresh_token",
            "订阅账号接口未返回 JSON",
            "订阅账号接口响应格式无效",
            "订阅信息接口未返回 JSON",
            "订阅信息接口响应格式无效",
        }:
            message = "套餐查询请求失败"
        with self._lock:
            rows = self._read()
            existing = rows.get(str(account_id))
            merged = dict(existing) if isinstance(existing, dict) else {}
            merged.update(
                subscription_status="error",
                subscription_checked_at=_now(),
                subscription_error=message[:160],
            )
            rows[str(account_id)] = merged
            self._write(rows)
        return merged

    def record_error(self, account_id: str, error: object) -> dict:
        message = str(error or "额度查询失败")
        # Do not persist response bodies, headers, or tokens from exceptions.
        if "HTTP " not in message and message not in {
            "凭证缺少 access_token",
            "额度接口未返回 JSON",
            "额度接口响应格式无效",
        }:
            message = "额度查询请求失败"
        return self.put(
            account_id,
            {"status": "error", "checked_at": _now(), "error": message[:160]},
        )
