from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MAX_DOCUMENTS = 100
MAX_TOKEN_BYTES = 2 * 1024 * 1024
MAX_EXISTING_FILE_BYTES = 5 * 1024 * 1024
JWT_PAYLOAD_LIMIT = 128 * 1024


class CredentialImportError(ValueError):
    """A safe, non-secret validation error for a credential batch."""


@dataclass(frozen=True)
class ImportResult:
    imported: int
    duplicates: int

    @property
    def total(self) -> int:
        return self.imported + self.duplicates

    def as_dict(self) -> dict[str, int]:
        return {
            "imported": self.imported,
            "duplicates": self.duplicates,
            "total": self.total,
        }


def import_codex_documents(
    documents: object,
    credential_dir: Path,
    *,
    now: datetime | None = None,
) -> ImportResult:
    if isinstance(documents, dict):
        rows = [documents]
    elif isinstance(documents, list):
        rows = list(documents)
    else:
        rows = []
    if not rows:
        raise CredentialImportError("请提供至少一个 Codex 凭证")
    if len(rows) > MAX_DOCUMENTS:
        raise CredentialImportError("每次最多导入 100 个凭证")

    observed = now or datetime.now(timezone.utc)
    normalized = [
        _normalize_document(row, index + 1, observed)
        for index, row in enumerate(rows)
    ]

    target = Path(credential_dir)
    fingerprints = _existing_fingerprints(target)
    pending: list[tuple[dict[str, Any], str]] = []
    duplicates = 0
    for payload in normalized:
        fingerprint = _fingerprint(payload)
        if fingerprint in fingerprints:
            duplicates += 1
            continue
        fingerprints.add(fingerprint)
        pending.append((payload, fingerprint))

    if pending:
        target.mkdir(parents=True, exist_ok=True)
    for payload, fingerprint in pending:
        _atomic_write(target, payload, fingerprint, observed)
    return ImportResult(imported=len(pending), duplicates=duplicates)


def _token(value: Any, *, index: int, name: str, required: bool = False) -> str:
    if value is None:
        result = ""
    elif not isinstance(value, str):
        raise CredentialImportError(f"第 {index} 项的 {name} 必须是字符串")
    else:
        result = value.strip()
    if required and not result:
        raise CredentialImportError(f"第 {index} 项缺少 {name}")
    if len(result.encode("utf-8")) > MAX_TOKEN_BYTES:
        raise CredentialImportError(f"第 {index} 项的 {name} 过大")
    return result


def _jwt_claims(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2 or len(parts[1]) > JWT_PAYLOAD_LIMIT * 2:
        return {}
    try:
        encoded = parts[1] + "=" * (-len(parts[1]) % 4)
        raw = base64.urlsafe_b64decode(encoded)
        if len(raw) > JWT_PAYLOAD_LIMIT:
            return {}
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _normalize_document(row: object, index: int, observed: datetime) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise CredentialImportError(f"第 {index} 项必须是 JSON 对象")
    tokens = row.get("tokens")
    if not isinstance(tokens, dict):
        raise CredentialImportError(f"第 {index} 项缺少 tokens 对象")

    access = _token(tokens.get("access_token"), index=index, name="access_token", required=True)
    refresh = _token(tokens.get("refresh_token"), index=index, name="refresh_token")
    identity = _token(tokens.get("id_token"), index=index, name="id_token")
    identity_claims = _jwt_claims(identity)
    access_claims = _jwt_claims(access)
    auth = access_claims.get("https://api.openai.com/auth") or identity_claims.get(
        "https://api.openai.com/auth"
    )
    auth = auth if isinstance(auth, dict) else {}

    email = str(
        identity_claims.get("email") or access_claims.get("email") or ""
    ).strip()[:320]
    account_id = str(
        tokens.get("account_id") or auth.get("chatgpt_account_id") or ""
    ).strip()[:320]
    expired = ""
    exp = access_claims.get("exp")
    if not isinstance(exp, (int, float)):
        exp = identity_claims.get("exp")
    if isinstance(exp, (int, float)):
        try:
            expired = datetime.fromtimestamp(exp, tz=timezone.utc).isoformat().replace(
                "+00:00", "Z"
            )
        except (OSError, OverflowError, ValueError):
            expired = ""

    last_refresh = row.get("last_refresh")
    if not isinstance(last_refresh, str) or not last_refresh.strip():
        last_refresh = observed.isoformat().replace("+00:00", "Z")
    return {
        "type": "codex",
        "email": email,
        "account_id": account_id,
        "id_token": identity,
        "access_token": access,
        "refresh_token": refresh,
        "last_refresh": last_refresh.strip()[:80],
        "expired": expired,
        "source": "GPTSession2CPAandSub2API",
    }


def _fingerprint(payload: dict[str, Any]) -> str:
    body = "\0".join(
        str(payload.get(key) or "")
        for key in ("id_token", "access_token", "refresh_token")
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _existing_fingerprints(target: Path) -> set[str]:
    results: set[str] = set()
    if not target.is_dir():
        return results
    for path in target.glob("*.json"):
        try:
            if not path.is_file() or path.stat().st_size > MAX_EXISTING_FILE_BYTES:
                continue
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(value, dict):
            results.add(_fingerprint(value))
    return results


def _safe_identity(payload: dict[str, Any]) -> str:
    value = str(
        payload.get("email") or payload.get("account_id") or "credential"
    ).casefold()
    value = re.sub(r"[^a-z0-9._-]+", "-", value).strip("-._")
    return (value or "credential")[:80]


def _atomic_write(
    target: Path,
    payload: dict[str, Any],
    fingerprint: str,
    observed: datetime,
) -> None:
    stamp = observed.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = target / f"{_safe_identity(payload)}-{stamp}-{fingerprint[:12]}.json"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target,
            prefix=".credential-import-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except Exception:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise
