from __future__ import annotations

import json
import os
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

import requests


USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"


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
        raise RuntimeError(f"额度接口 HTTP {response.status_code}")
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
        "plan_type": str(payload.get("plan_type") or credential.get("type") or ""),
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
            rows[str(account_id)] = safe
            self._write(rows)
        return safe

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
