# Unified Project Manager Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Host the pinned GPTSession2CPAandSub2API frontend beside the existing receiver, add one local manager entry point, and persist explicitly confirmed Codex conversions into the existing credential archive.

**Architecture:** `manager_app.py` wraps the upstream `app.py` factory at runtime and registers a focused blueprint from `manager/`; upstream receiver files and the vendored converter snapshot remain unmodified. A separately served bridge script converts the current browser output to Codex auth JSON and calls a loopback-only import endpoint after confirmation.

**Tech Stack:** Python 3.12, Flask 3.1, Windows PowerShell 5.1, vanilla browser JavaScript, Node.js 22 tests, pytest.

## Global Constraints

- Continue listening only on `127.0.0.1:5015`.
- Preserve the receiver's `/`, `/health`, existing `/api/*`, `.env`, `data/`, and `logs/` behavior.
- Keep `vendor/GPTSession2CPAandSub2API/` byte-for-byte equal to upstream commit `a097eb155bb7bdf6cbbc26f1e4e75e120ab3163c`.
- Do not modify upstream `app.py`, `src/webapp.py`, `templates/index.html`, or any converter file.
- Never persist converter input automatically; import requires an explicit browser confirmation and `confirmed: true` in the request.
- Never return or log access, refresh, ID, or session tokens.
- Accept at most 100 Codex documents and 5 MiB per manager import request.
- Use UTF-8 with BOM for Chinese PowerShell files and CRLF for CMD launchers.

---

### Task 1: Pin and verify the independent converter snapshot

**Files:**
- Create: `vendor/GPTSession2CPAandSub2API/**`
- Create: `manager/__init__.py`
- Create: `manager/upstreams.lock.json`
- Create: `tests/test_unified_manager.py`

**Interfaces:**
- Consumes: GitHub commit `a097eb155bb7bdf6cbbc26f1e4e75e120ab3163c` and its codeload archive.
- Produces: an unmodified vendor tree and `load_upstream_lock(project_root: Path) -> dict` data contract for the manager and later sync plan.

- [ ] **Step 1: Write the failing pinned-snapshot test**

Add `tests/test_unified_manager.py` with these initial tests and helpers:

```python
from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "vendor" / "GPTSession2CPAandSub2API"
LOCK = ROOT / "manager" / "upstreams.lock.json"


def git_blob_sha(path: Path) -> str:
    body = path.read_bytes()
    return hashlib.sha1(f"blob {len(body)}\0".encode() + body).hexdigest()


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
```

- [ ] **Step 2: Run RED verification**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_unified_manager.py -q
```

Expected: FAIL because the converter vendor directory and lock file do not exist.

- [ ] **Step 3: Download and extract the exact official snapshot**

Run:

```powershell
New-Item -ItemType Directory -Force -Path '.tmp-converter','vendor\GPTSession2CPAandSub2API','manager' | Out-Null
Invoke-WebRequest -Headers @{ 'User-Agent'='Codex' } -Uri 'https://codeload.github.com/gtxx3600/GPTSession2CPAandSub2API/zip/a097eb155bb7bdf6cbbc26f1e4e75e120ab3163c' -OutFile '.tmp-converter\converter.zip'
tar -xf '.tmp-converter\converter.zip' --strip-components=1 -C 'vendor\GPTSession2CPAandSub2API'
Remove-Item -Recurse -Force '.tmp-converter'
```

Create `manager/__init__.py` as an empty package marker. Create `manager/upstreams.lock.json` with:

```json
{
  "schema_version": 1,
  "upstreams": [
    {
      "key": "receiver",
      "repository": "maile456/codex-auto-sms-receiver",
      "branch": "main",
      "commit": "269bf3cd088b075f164ad2fe8e674b8b72a9fd26",
      "target": ".",
      "mode": "overlay",
      "files": []
    },
    {
      "key": "converter",
      "repository": "gtxx3600/GPTSession2CPAandSub2API",
      "branch": "main",
      "commit": "a097eb155bb7bdf6cbbc26f1e4e75e120ab3163c",
      "target": "vendor/GPTSession2CPAandSub2API",
      "mode": "replace",
      "files": []
    }
  ],
  "protected_prefixes": [
    ".env",
    ".venv/",
    "data/",
    "logs/",
    "manager/",
    "manager_app.py",
    "ops/",
    "docs/superpowers/",
    "tests/test_unified_manager.py",
    "tests/manager-bridge.test.js",
    "启动.cmd",
    "关闭.cmd",
    "检查更新.cmd",
    "更新两个项目.cmd",
    "LOCAL-DEPLOYMENT.md"
  ]
}
```

- [ ] **Step 4: Verify snapshot and upstream Node tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_unified_manager.py -q
node vendor\GPTSession2CPAandSub2API\tests\convert-session.test.js
```

Expected: pytest passes both tests and the Node process exits 0.

- [ ] **Step 5: Commit the pinned upstream boundary**

```powershell
git add manager/__init__.py manager/upstreams.lock.json tests/test_unified_manager.py vendor/GPTSession2CPAandSub2API
git commit -m "chore: pin session converter upstream snapshot"
```

### Task 2: Build the credential import engine with TDD

**Files:**
- Create: `manager/credential_import.py`
- Modify: `tests/test_unified_manager.py`

**Interfaces:**
- Consumes: `documents: dict | list[dict]`, `credential_dir: Path`, optional `now: datetime`.
- Produces: `import_codex_documents(documents, credential_dir, now=None) -> ImportResult` where `ImportResult.as_dict()` contains only `imported`, `duplicates`, and `total`.

- [ ] **Step 1: Add failing behavior tests**

Append these imports and helpers before the tests:

```python
import base64
from datetime import datetime, timezone

import pytest

from manager.credential_import import (
    CredentialImportError,
    import_codex_documents,
)


fixed_now = datetime(2026, 8, 8, 0, 0, 0, tzinfo=timezone.utc)


def jwt(payload: dict) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{header}.{body}.sig"


def codex_document(email: str | None, *, access: str | None = None) -> dict:
    claims = {"exp": 1780000000}
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
```

Append tests that construct only these non-secret fixtures and assert:

```python
def test_import_codex_document_writes_flat_local_credential(tmp_path):
    document = {
        "auth_mode": "chatgpt",
        "tokens": {
            "access_token": jwt({"email": "owner@example.com", "exp": 1780000000}),
            "refresh_token": "refresh-secret",
            "id_token": jwt({"email": "owner@example.com"}),
            "account_id": "acct-1",
        },
        "last_refresh": "2026-08-08T00:00:00Z",
    }
    result = import_codex_documents(document, tmp_path, now=fixed_now)
    saved = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    assert result.as_dict() == {"imported": 1, "duplicates": 0, "total": 1}
    assert saved == {
        "type": "codex",
        "email": "owner@example.com",
        "account_id": "acct-1",
        "id_token": document["tokens"]["id_token"],
        "access_token": document["tokens"]["access_token"],
        "refresh_token": "refresh-secret",
        "last_refresh": "2026-08-08T00:00:00Z",
        "expired": "2026-05-28T20:26:40Z",
        "source": "GPTSession2CPAandSub2API"
    }


def test_import_codex_documents_skips_exact_duplicate_and_versions_new_token(tmp_path):
    first = codex_document("same@example.com", access="access-one")
    assert import_codex_documents(first, tmp_path, now=fixed_now).imported == 1
    assert import_codex_documents(first, tmp_path, now=fixed_now).duplicates == 1
    second = codex_document("same@example.com", access="access-two")
    assert import_codex_documents(second, tmp_path, now=fixed_now).imported == 1
    assert len(list(tmp_path.glob("*.json"))) == 2


def test_import_validates_entire_batch_before_writing(tmp_path):
    with pytest.raises(CredentialImportError, match="第 2 项缺少 access_token"):
        import_codex_documents([codex_document("ok@example.com"), {"tokens": {}}], tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_import_allows_missing_email_but_never_uses_client_path(tmp_path):
    document = codex_document(None)
    document["filename"] = "../../outside.json"
    result = import_codex_documents(document, tmp_path, now=fixed_now)
    assert result.imported == 1
    saved = next(tmp_path.glob("*.json"))
    assert saved.parent == tmp_path
    assert ".." not in saved.name


def test_import_rejects_more_than_100_documents(tmp_path):
    with pytest.raises(CredentialImportError, match="最多导入 100 个凭证"):
        import_codex_documents([codex_document(f"user-{i}@example.com") for i in range(101)], tmp_path)
```

- [ ] **Step 2: Run RED verification**

Run the five named tests. Expected: collection FAILS because `manager.credential_import` does not exist.

- [ ] **Step 3: Implement normalization, validation, deduplication, and atomic writes**

Create these exact public types in `manager/credential_import.py`:

```python
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
    pass


@dataclass(frozen=True)
class ImportResult:
    imported: int
    duplicates: int

    @property
    def total(self) -> int:
        return self.imported + self.duplicates

    def as_dict(self) -> dict[str, int]:
        return {"imported": self.imported, "duplicates": self.duplicates, "total": self.total}


def import_codex_documents(
    documents: object,
    credential_dir: Path,
    *,
    now: datetime | None = None,
) -> ImportResult:
    rows = [documents] if isinstance(documents, dict) else list(documents) if isinstance(documents, list) else []
    if not rows:
        raise CredentialImportError("请提供至少一个 Codex 凭证")
    if len(rows) > MAX_DOCUMENTS:
        raise CredentialImportError("每次最多导入 100 个凭证")
    observed = now or datetime.now(timezone.utc)
    normalized = [_normalize_document(row, index + 1, observed) for index, row in enumerate(rows)]

    target = Path(credential_dir)
    existing = _existing_fingerprints(target)
    pending = []
    duplicates = 0
    for payload in normalized:
        fingerprint = _fingerprint(payload)
        if fingerprint in existing:
            duplicates += 1
            continue
        existing.add(fingerprint)
        pending.append((payload, fingerprint))

    if pending:
        target.mkdir(parents=True, exist_ok=True)
    imported = 0
    for payload, fingerprint in pending:
        _atomic_write(target, payload, fingerprint, observed)
        imported += 1
    return ImportResult(imported=imported, duplicates=duplicates)


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
    email = str(identity_claims.get("email") or access_claims.get("email") or "").strip()[:320]
    account_id = str(tokens.get("account_id") or auth.get("chatgpt_account_id") or "").strip()[:320]
    expired = ""
    exp = access_claims.get("exp")
    if not isinstance(exp, (int, float)):
        exp = identity_claims.get("exp")
    if isinstance(exp, (int, float)):
        expired = datetime.fromtimestamp(exp, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "type": "codex",
        "email": email,
        "account_id": account_id,
        "id_token": identity,
        "access_token": access,
        "refresh_token": refresh,
        "last_refresh": str(row.get("last_refresh") or observed.isoformat().replace("+00:00", "Z"))[:80],
        "expired": expired,
        "source": "GPTSession2CPAandSub2API",
    }


def _fingerprint(payload: dict[str, Any]) -> str:
    body = "\0".join(str(payload.get(key) or "") for key in ("id_token", "access_token", "refresh_token"))
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
    value = str(payload.get("email") or payload.get("account_id") or "credential").casefold()
    value = re.sub(r"[^a-z0-9._-]+", "-", value).strip("-._")
    return (value or "credential")[:80]


def _atomic_write(target: Path, payload: dict[str, Any], fingerprint: str, observed: datetime) -> None:
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
```

Implementation requirements are exact: normalize a dict to one item and a list to a copy; reject empty and over-100 batches; validate every item before creating the directory; require every supplied token to be a string and cap it at 2 MiB; derive identity fields from the ID token first, while deriving expiry and account metadata from the access token first, with a 128 KiB JWT payload limit; compute a SHA-256 fingerprint over `id_token`, `access_token`, and `refresh_token` separated by NUL; scan existing JSON files no larger than 5 MiB for exact fingerprints; create names as `<sanitized-identity>-<UTC timestamp>-<fingerprint[0:12]>.json`; write with `tempfile.NamedTemporaryFile(delete=False, dir=credential_dir)` followed by `os.replace`; delete a temp file on write failure. Do not include token values in exception text or `ImportResult`.

- [ ] **Step 4: Run GREEN and regression tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_unified_manager.py -q
.\.venv\Scripts\python.exe -m pytest tests/test_artifact_store.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit the import engine**

```powershell
git add manager/credential_import.py tests/test_unified_manager.py
git commit -m "feat: import converted Codex credentials safely"
```

### Task 3: Compose the manager blueprint without editing upstream files

**Files:**
- Create: `manager/blueprint.py`
- Create: `manager_app.py`
- Create: `manager/templates/manager.html`
- Modify: `tests/test_unified_manager.py`

**Interfaces:**
- Consumes: `Settings`, the vendor `docs/` directory, `import_codex_documents`, and the lock file.
- Produces: `create_manager_blueprint(settings: Settings) -> Blueprint` and `create_managed_app(settings: Settings, **upstream_kwargs) -> Flask`.

- [ ] **Step 1: Add failing route and composition tests**

Add these test helpers:

```python
import shutil
import uuid

from manager_app import create_managed_app
from src.settings import Settings


@pytest.fixture
def workspace_path():
    path = ROOT / "tests" / f"runtime-manager-{uuid.uuid4().hex}"
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _settings(path: Path) -> Settings:
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
    app = create_managed_app(_settings(path), codex_manager=object())
    app.config["TESTING"] = True
    return app.test_client()
```

Add tests asserting:

```python
def test_managed_app_preserves_receiver_and_adds_manager_routes(workspace_path):
    app = create_managed_app(_settings(workspace_path), codex_manager=object())
    client = app.test_client()
    assert client.get("/").status_code == 200
    assert client.get("/health").get_json()["ok"] is True
    manager = client.get("/manager")
    assert manager.status_code == 200
    assert "接码与 OAuth 管理" in manager.get_data(as_text=True)
    assert "Session / Token 格式转换" in manager.get_data(as_text=True)


def test_converter_route_injects_bridge_without_modifying_vendor(workspace_path):
    before = git_blob_sha(VENDOR / "docs/index.html")
    client = managed_client(workspace_path)
    response = client.get("/tools/session-converter/")
    assert response.status_code == 200
    assert '/manager-static/converter_bridge.js' in response.get_data(as_text=True)
    assert git_blob_sha(VENDOR / "docs/index.html") == before
    assert client.get("/tools/session-converter/favicon.svg").status_code == 200
    assert client.get("/tools/session-converter/../../README.md").status_code in {404, 308}


def test_credential_import_route_requires_confirmation_and_hides_tokens(workspace_path):
    client = managed_client(workspace_path)
    document = codex_document("route@example.com", access="route-access-secret")
    denied = client.post("/api/manager/credentials/import", json={"documents": document})
    assert denied.status_code == 400
    response = client.post(
        "/api/manager/credentials/import",
        json={"confirmed": True, "documents": document},
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body == {"ok": True, "imported": 1, "duplicates": 0, "total": 1}
    assert "route-access-secret" not in response.get_data(as_text=True)
```

Also post a valid payload larger than the upstream 32 KiB default but smaller than 5 MiB and assert success; post a body larger than 5 MiB and assert 413.

- [ ] **Step 2: Run RED verification**

Expected: collection FAILS because `manager.blueprint` and `manager_app` do not exist.

- [ ] **Step 3: Implement the blueprint and upstream wrapper**

`manager/blueprint.py` must create:

```python
MAX_MANAGER_IMPORT_BYTES = 5 * 1024 * 1024


def create_manager_blueprint(settings: Settings) -> Blueprint:
    blueprint = Blueprint(
        "manager",
        __name__,
        template_folder="templates",
        static_folder="static",
        static_url_path="/manager-static",
    )
    # Register /manager, converter routes, status, and confirmed import.
    return blueprint
```

Use `request.max_content_length = MAX_MANAGER_IMPORT_BYTES` only for the manager import endpoint. Serve converter assets exclusively from `<project_root>/vendor/GPTSession2CPAandSub2API/docs` with `send_from_directory`. For the converter index, require exactly one case-insensitive `</body>` marker and inject `<script src="/manager-static/converter_bridge.js"></script>` immediately before it. Return no-store and `X-Content-Type-Options: nosniff` headers.

`manager_app.py` must preserve upstream startup by monkeypatching only the imported factory reference:

```python
from __future__ import annotations

import app as upstream_entry
from manager.blueprint import create_manager_blueprint
from src.settings import Settings
from src.webapp import create_app as create_upstream_app


def create_managed_app(settings: Settings, **upstream_kwargs):
    flask_app = create_upstream_app(settings, **upstream_kwargs)
    flask_app.register_blueprint(create_manager_blueprint(settings))
    return flask_app


def main() -> None:
    upstream_entry.create_app = create_managed_app
    upstream_entry.main()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Create the manager page**

Build `manager/templates/manager.html` as a dependency-free local page with two module cards, local-only notice, current pinned SHAs read from `/api/manager/status`, links to `/` and `/tools/session-converter/`, and update-state placeholders. Use semantic headings, visible keyboard focus, minimum 44 px controls, no external fonts/assets, and responsive single-column layout below 720 px.

- [ ] **Step 5: Run GREEN verification**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_unified_manager.py -q
.\.venv\Scripts\python.exe -m pytest tests/test_webapp.py -q
```

- [ ] **Step 6: Commit the composed local manager**

```powershell
git add manager/blueprint.py manager/templates/manager.html manager_app.py tests/test_unified_manager.py
git commit -m "feat: add unified local project manager"
```

### Task 4: Add the browser bridge with real JavaScript behavior tests

**Files:**
- Create: `manager/static/converter_bridge.js`
- Create: `tests/manager-bridge.test.js`
- Modify: `tests/test_unified_manager.py`

**Interfaces:**
- Consumes: converter selectors `[data-format="codex"]`, `[data-format][aria-pressed="true"]`, `#output`, `#output-status`, and `#download-output`.
- Produces: an injected `#save-local-credentials` button and one POST to `/api/manager/credentials/import` with `{confirmed: true, documents}`.

- [ ] **Step 1: Write the failing Node behavior test**

Create a fake DOM that exposes the five selectors, makes the Codex button synchronously replace `#output.value` with one literal auth document, records `fetch` calls, and returns `true` from `window.confirm`. Load the real bridge with `vm.runInNewContext`, click the injected button, await queued promises, then assert:

```javascript
assert.equal(fetchCalls.length, 1);
assert.equal(fetchCalls[0].url, "/api/manager/credentials/import");
assert.deepEqual(JSON.parse(fetchCalls[0].options.body), {
  confirmed: true,
  documents: codexDocument,
});
assert.equal(previousFormatButton.clickCount, 1, "restore prior output format");
assert.equal(outputStatus.textContent, "已保存 1 个凭证，跳过 0 个重复项。");
```

Add separate tests for user cancellation, empty output, invalid JSON, and an HTTP 400 response. No test may contain a real-looking JWT or secret.

- [ ] **Step 2: Run RED verification**

```powershell
node tests\manager-bridge.test.js
```

Expected: FAIL because `manager/static/converter_bridge.js` does not exist.

- [ ] **Step 3: Implement the bridge**

The bridge must execute in an IIFE, refuse duplicate installation, insert a secondary button after `#download-output`, and on click: require non-empty output; capture the pressed format button; click the Codex button; parse `#output.value`; immediately restore the prior button; show the exact persistence warning through `window.confirm`; POST JSON with same-origin credentials; handle non-2xx JSON errors; render only counts; disable itself while pending. Never read or write localStorage, sessionStorage, cookies, or external URLs.

- [ ] **Step 4: Run GREEN and route verification**

```powershell
node tests\manager-bridge.test.js
.\.venv\Scripts\python.exe -m pytest tests/test_unified_manager.py -q
```

- [ ] **Step 5: Commit the bridge**

```powershell
git add manager/static/converter_bridge.js tests/manager-bridge.test.js tests/test_unified_manager.py
git commit -m "feat: bridge converter output to local credentials"
```

### Task 5: Switch local launch controls to the composed entry point

**Files:**
- Modify: `ops/local/Start-Local.ps1`
- Modify: `ops/local/Stop-Local.ps1`
- Modify: `tests/test_windows_launch_controls.py`
- Restore upstream: `README.md`
- Create: `LOCAL-DEPLOYMENT.md`

**Interfaces:**
- Consumes: `manager_app.py`, `/health`, `/manager`, `data/server.pid`.
- Produces: existing double-click controls that identify `manager_app.py` and open `http://127.0.0.1:5015/manager`.

- [ ] **Step 1: Extend the live-control test and watch it fail**

After starting, assert the server command line contains the absolute `manager_app.py` path, `GET /manager` returns 200, and the PID stays equal on a repeated start. Run the opt-in test and expect FAIL because the scripts still launch `app.py`.

- [ ] **Step 2: Update both PowerShell scripts**

Replace their single expected application path with `manager_app.py`; keep the existing executable-or-parent virtualenv validation. Set Start-Local's browser URL to `/manager`. Keep health checks at `/health`, output logs unchanged, and failure rollback scoped to the newly launched process tree.

- [ ] **Step 3: Move local documentation outside upstream README**

Restore `README.md` exactly by removing the previously added `### Windows 双击启动与关闭` section (the nine added lines between the browser URL and the WebUI local-only warning) with `apply_patch`, then verify:

```powershell
$expected = git rev-parse bd661ff:README.md
$actual = git hash-object README.md
if ($actual -ne $expected) { throw "README.md 未恢复到上游快照" }
```

Create `LOCAL-DEPLOYMENT.md` containing the Windows `.venv` setup, `启动.cmd` / `关闭.cmd`, manager/converter URLs, local-only warning, HeroSMS configuration note, logs, tests, and upstream update-button summary. Save as UTF-8.

- [ ] **Step 4: Run live GREEN verification**

Stop the currently running old entry, set `RUN_LOCAL_CONTROL_INTEGRATION=1`, run `tests/test_windows_launch_controls.py`, then run it normally and expect all live tests pass and normal mode skips them.

- [ ] **Step 5: Commit launch migration and local docs**

```powershell
git add ops/local/Start-Local.ps1 ops/local/Stop-Local.ps1 tests/test_windows_launch_controls.py README.md LOCAL-DEPLOYMENT.md
git commit -m "feat: launch the unified manager locally"
```

### Task 6: Verify the unified manager end to end

**Files:**
- Verify all files from Tasks 1-5.

**Interfaces:**
- Consumes: local `.venv`, Node.js, pinned vendor snapshot, manager entry point.
- Produces: one running unified service and fresh evidence for both independent modules.

- [ ] **Step 1: Run every automated suite**

```powershell
.\.venv\Scripts\python.exe -m pytest -q
node vendor\GPTSession2CPAandSub2API\tests\convert-session.test.js
node tests\manager-bridge.test.js
```

Expected: zero failures.

- [ ] **Step 2: Start and smoke-test every route**

Start through `启动.cmd` without opening an extra browser during automation, then verify literal 200 responses for `/health`, `/manager`, `/`, `/tools/session-converter/`, `/manager-static/converter_bridge.js`, and `/tools/session-converter/favicon.svg`.

- [ ] **Step 3: Verify a disposable import through HTTP**

POST one fixture document containing tokens named `fixture-access-not-a-real-token` and `fixture-refresh-not-a-real-token`; assert it appears as an exportable credential without either fixture string in the response. Remove only the exact fixture file from `data/codex_accounts/` after resolving it inside that directory.

- [ ] **Step 4: Verify upstream isolation and Git state**

Recompute the four pinned converter Git blob SHAs, confirm `README.md` equals `bd661ff:README.md`, run `git diff --check`, and inspect `git status --short`. Runtime files must remain ignored.

- [ ] **Step 5: Leave the manager running**

Open `http://127.0.0.1:5015/manager`, recheck `/health`, record the PID, and leave that process running for handoff.
