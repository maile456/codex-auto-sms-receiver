# Safe Upstream Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Check and safely apply updates from both pinned GitHub projects without overwriting local integration code, configuration, business data, or a known-good runtime.

**Architecture:** A standard-library Python module owns GitHub metadata, Git blob verification, manifests, update transactions, backups, application, and rollback. PowerShell/CMD wrappers own interactive confirmation, service lifecycle, optional virtualenv backup, test execution, final health verification, and recovery orchestration.

**Tech Stack:** Python 3.12 standard library, GitHub REST/codeload, ZIP, SHA-1 Git blob hashing, Windows PowerShell 5.1, CMD, pytest, Node.js 22.

## Global Constraints

- Check automatically, but never install updates automatically on service start or page load.
- Mutating updates require a local CMD/PowerShell confirmation; no browser POST endpoint may update source.
- Only access `api.github.com`, `codeload.github.com`, and the configured Python package index when requirements change.
- Never read, back up, modify, or log `.env`, `data/codex_accounts/`, account materials, tokens, or logs.
- Reject local drift in managed upstream files and collisions with protected integration paths.
- Validate every extracted regular file against GitHub tree blob SHA before applying it.
- Back up source, lock state, vendor snapshot, and `.venv` when dependencies change.
- Run full pytest, upstream converter Node tests, bridge Node tests, and live manager health before finalizing.
- Roll back source, lock state, vendor snapshot, and virtualenv after any failed gate.

---

### Task 1: Build manifest and Git-content validation primitives

**Files:**
- Create: `manager/upstream_sync.py`
- Create: `tests/test_upstream_sync.py`
- Modify: `manager/upstreams.lock.json`

**Interfaces:**
- Produces: `git_blob_sha(data: bytes) -> str`, `safe_repo_path(value: str) -> PurePosixPath`, `resolve_inside(workspace: Path, value: str, *, allow_root: bool = True) -> Path`, `load_lock(path: Path) -> LockFile`, `verify_local_manifest(workspace: Path, spec: UpstreamSpec) -> None`, and `verify_extracted_tree(root: Path, tree_rows: list[dict]) -> list[FileEntry]`.

- [ ] **Step 1: Write failing primitive tests**

Create the test file with these imports and deterministic helpers before the tests:

```python
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from manager.upstream_sync import (
    FileEntry,
    LockFile,
    SyncError,
    UpstreamSpec,
    git_blob_sha,
    load_lock,
    resolve_inside,
    safe_repo_path,
    verify_extracted_tree,
    verify_local_manifest,
)


def entry(path: str, body: bytes) -> FileEntry:
    return FileEntry(path=path, type="blob", sha=git_blob_sha(body), size=len(body))


def upstream_spec(*, files: list[FileEntry], target: str = ".") -> UpstreamSpec:
    return UpstreamSpec(
        key="receiver",
        repository="owner/project",
        branch="main",
        commit="1" * 40,
        target=target,
        mode="overlay",
        files=tuple(files),
    )


def snapshot_tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
```

Then add these tests with literal expected values:

```python
def test_git_blob_sha_matches_github_blob_algorithm():
    assert git_blob_sha(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"


@pytest.mark.parametrize("value", ["../escape", "/absolute", "C:/drive", "a/../../b", "a\\b"])
def test_safe_repo_path_rejects_unsafe_names(value):
    with pytest.raises(SyncError):
        safe_repo_path(value)


def test_verify_extracted_tree_rejects_blob_mismatch(tmp_path):
    (tmp_path / "README.md").write_text("changed", encoding="utf-8")
    with pytest.raises(SyncError, match="blob SHA 不匹配"):
        verify_extracted_tree(
            tmp_path,
            [{"path": "README.md", "type": "blob", "size": 8, "sha": "0" * 40}],
        )


def test_verify_local_manifest_detects_drift(tmp_path):
    (tmp_path / "owned.txt").write_text("current\n", encoding="utf-8")
    spec = upstream_spec(files=[entry("owned.txt", b"expected\n")])
    with pytest.raises(SyncError, match="本地上游文件已修改: owned.txt"):
        verify_local_manifest(tmp_path, spec)
```

Add one named test for each of these breakages using literal one-file fixtures: a missing manifest file; an unexpected extracted regular file; a tree row with `type == "commit"`; a tree row with mode `120000`; a path matching `manager/`; duplicate lock keys in JSON; and `resolve_inside(tmp_path, "../outside")`. Each must assert `SyncError` and an error message naming the rejected path or key. ZIP member-mode validation belongs to Task 3, where an actual in-memory ZIP is available.

- [ ] **Step 2: Run RED verification**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_upstream_sync.py -q
```

Expected: import FAILS because `manager.upstream_sync` does not exist.

- [ ] **Step 3: Implement immutable data contracts and validators**

Create these public contracts:

```python
class SyncError(RuntimeError):
    pass


@dataclass(frozen=True)
class FileEntry:
    path: str
    type: str
    sha: str
    size: int


@dataclass(frozen=True)
class UpstreamSpec:
    key: str
    repository: str
    branch: str
    commit: str
    target: str
    mode: str
    files: tuple[FileEntry, ...]


@dataclass(frozen=True)
class LockFile:
    schema_version: int
    upstreams: tuple[UpstreamSpec, ...]
    protected_prefixes: tuple[str, ...]
```

`safe_repo_path` must accept only non-empty forward-slash relative paths whose parts exclude `""`, `"."`, and `".."`. `resolve_inside` must resolve a relative value and reject anything outside the resolved workspace. `git_blob_sha` must hash `b"blob " + ascii(size) + b"\0" + content`. `load_lock` must validate schema version 1, unique keys, `type == "blob"`, 40 lowercase hex commit/blob SHAs, repository `owner/name`, and modes `overlay|replace`; callers resolve every target through `resolve_inside`. `verify_extracted_tree` ignores ordinary `tree` directory rows, rejects `commit`, symlink mode `120000`, unknown types/modes, and a truncated GitHub tree response before requiring an exact set match between extracted regular files and blob rows.

- [ ] **Step 4: Bootstrap complete manifests from the two pinned GitHub trees**

Add CLI command:

```text
python -m manager.upstream_sync bootstrap-lock --workspace <root> --lock manager/upstreams.lock.json
```

It reads each pinned commit tree from `https://api.github.com/repos/<repo>/git/trees/<sha>?recursive=1`, verifies current root upstream files and the vendor tree, fills sorted `files`, and atomically replaces the lock. For the receiver, only paths returned by its pinned tree are managed; integration additions are not inserted. Run it once, then rerun local manifest verification.

- [ ] **Step 5: Run GREEN verification and commit**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_upstream_sync.py -q
.\.venv\Scripts\python.exe -m manager.upstream_sync bootstrap-lock --workspace . --lock manager/upstreams.lock.json
.\.venv\Scripts\python.exe -m manager.upstream_sync verify-local --workspace . --lock manager/upstreams.lock.json
git add manager/upstream_sync.py manager/upstreams.lock.json tests/test_upstream_sync.py
git commit -m "feat: validate pinned upstream manifests"
```

### Task 2: Implement read-only GitHub update checks

**Files:**
- Modify: `manager/upstream_sync.py`
- Modify: `manager/blueprint.py`
- Modify: `manager/templates/manager.html`
- Modify: `tests/test_upstream_sync.py`
- Modify: `tests/test_unified_manager.py`

**Interfaces:**
- Produces: `GitHubClient.latest_commit(repository, branch) -> RemoteCommit`, `check_updates(lock, client) -> tuple[UpdateStatus, ...]`, and CLI `check --json|--human`.

- [ ] **Step 1: Add failing no-write update-check tests**

Add these exact data contracts and fake-client helper to the test plan:

```python
@dataclass(frozen=True)
class RemoteCommit:
    sha: str
    title: str
    committed_at: str


@dataclass(frozen=True)
class UpdateStatus:
    key: str
    repository: str
    current_sha: str
    latest_sha: str | None
    update_available: bool | None
    title: str | None
    committed_at: str | None
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "key": self.key,
            "repository": self.repository,
            "current_sha": self.current_sha,
            "latest_sha": self.latest_sha,
            "update_available": self.update_available,
            "title": self.title,
            "committed_at": self.committed_at,
        }
        if self.error is not None:
            result["error"] = self.error
        return result


class FakeClient:
    def __init__(self, results: dict[str, RemoteCommit | Exception]):
        self.results = results

    def latest_commit(self, repository: str, branch: str) -> RemoteCommit:
        result = self.results[repository]
        if isinstance(result, Exception):
            raise result
        return result
```

The `UpdateStatus.as_dict()` implementation must return literal keys `key`, `repository`, `current_sha`, `latest_sha`, `update_available`, `title`, and `committed_at`, plus a sanitized `error` only when non-null. Build a one-project `LockFile` whose receiver commit is `"1" * 40`, set `workspace = tmp_path`, record `before = snapshot_tree(workspace)`, and instantiate this exact fake before asserting:

```python
fake_client = FakeClient(
    {
        "maile456/codex-auto-sms-receiver": RemoteCommit(
            sha="2" * 40,
            title="upstream change",
            committed_at="2026-08-08T00:00:00Z",
        )
    }
)
```

```python
statuses = check_updates(lock, fake_client)
assert statuses[0].as_dict() == {
    "key": "receiver",
    "repository": "maile456/codex-auto-sms-receiver",
    "current_sha": "1" * 40,
    "latest_sha": "2" * 40,
    "update_available": True,
    "title": "upstream change",
    "committed_at": "2026-08-08T00:00:00Z",
}
assert snapshot_tree(workspace) == before
```

Test HTTP timeout, rate-limit response, malformed JSON, and one-repository failure without hiding the other result.

- [ ] **Step 2: Run RED verification**

Expected: FAIL because `GitHubClient`, `RemoteCommit`, `UpdateStatus`, and `check_updates` are absent.

- [ ] **Step 3: Implement the read-only client and 10-minute manager cache**

Use `urllib.request` with `User-Agent: Codex-Unified-Local-Manager`, 10-second timeout, and JSON size cap 2 MiB. Do not send credentials. The manager blueprint caches successful or failed status tuples under a lock for 600 seconds and exposes them only through `GET /api/manager/status`; the rest of the status payload remains available when GitHub is offline.

Update `manager.html` to show per-project current short SHA, latest short SHA, `已是最新` / `有更新` / `无法检查`, commit title, and instructions to use `检查更新.cmd` or `更新两个项目.cmd`. Do not add a mutating fetch call.

- [ ] **Step 4: Add and verify CLI output**

`python -m manager.upstream_sync check --workspace . --lock manager/upstreams.lock.json --json` emits one JSON object containing a `projects` array and exits 0 when the API call itself was valid, even if an update exists. `--human` prints one line per project and exits 2 only when every project check failed.

- [ ] **Step 5: Run GREEN tests and commit**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_upstream_sync.py tests/test_unified_manager.py -q
git add manager/upstream_sync.py manager/blueprint.py manager/templates/manager.html tests/test_upstream_sync.py tests/test_unified_manager.py
git commit -m "feat: report upstream update availability"
```

### Task 3: Build staged download, archive verification, backup, apply, and rollback

**Files:**
- Modify: `manager/upstream_sync.py`
- Modify: `tests/test_upstream_sync.py`

**Interfaces:**
- Produces `plan_update(workspace: Path, lock_path: Path, project: str, client: GitHubClient) -> Path`, `apply_transaction(workspace: Path, transaction_path: Path) -> None`, `finalize_transaction(workspace: Path, transaction_path: Path) -> None`, and `rollback_transaction(workspace: Path, transaction_path: Path) -> None`, plus matching CLI phases. The transaction JSON lives under `data/upstream-staging/<id>/transaction.json`; durable backups live under `data/upstream-backups/<timestamp>-<id>/`.

- [ ] **Step 1: Write failing transaction tests against temporary workspaces**

Create in-memory ZIP fixtures with a single GitHub-style root directory and fake tree rows. Assert:

- `plan_update` retries a transient download, validates blobs, records added/changed/deleted paths, backs up only managed source and lock files, and does not modify the workspace.
- path traversal, symlink mode, submodule row, blob mismatch, local drift, and protected collision fail before a backup or source mutation.
- `apply_transaction` writes added/changed files atomically, removes only previously managed deleted files, replaces the vendor file set, updates the lock, and never touches `.env`, `data/codex_accounts`, `manager/`, or `ops/`.
- `rollback_transaction` restores old bytes, removes transaction-added files, restores the old lock and vendor contents, and is idempotent.

- [ ] **Step 2: Run RED verification**

Expected: FAIL because transaction functions and CLI commands are absent.

- [ ] **Step 3: Implement `plan-update`**

For each selected upstream: call latest commit; fetch a non-truncated recursive tree; download `https://codeload.github.com/<owner>/<repo>/zip/<sha>` with three attempts and exponential delays 1, 2, 4 seconds; cap archive at 100 MiB; reject ZIP members with absolute/parent paths, encrypted flags, symlink Unix modes, or more than 10,000 files; extract into its transaction staging directory; verify exact blob rows; compute the update diff. Copy the old lock and all old managed files to `data/upstream-backups/<timestamp>-<id>/source/<key>/` and write transaction JSON atomically. Keep the running service and workspace source unchanged.

- [ ] **Step 4: Implement `apply`, `finalize`, and `rollback`**

`apply` requires `state == "planned"` and a caller-created `service-stopped.marker`; it writes with sibling temp files and `os.replace`, removes deleted files only after resolving them inside the managed target, prunes empty managed directories, and atomically writes the new lock before setting `state = "applied"`.

`finalize` requires caller-created `tests-passed.marker` and `health-passed.marker`, sets state `complete`, removes staging archives/extracted directories, retains the separate durable backup, and prunes `data/upstream-backups/` beyond the newest three.

`rollback` accepts planned or applied transactions, restores the backup lock and every backed-up file, removes files listed as added by this transaction, restores state `rolled_back`, and leaves logs/backup intact. It never starts or stops processes itself.

- [ ] **Step 5: Run GREEN transaction tests and commit**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_upstream_sync.py -q
git add manager/upstream_sync.py tests/test_upstream_sync.py
git commit -m "feat: apply upstream updates transactionally"
```

### Task 4: Add safe Windows check/update orchestration

**Files:**
- Create: `ops/upstream/Check-Upstreams.ps1`
- Create: `ops/upstream/Update-Upstreams.ps1`
- Create: `检查更新.cmd`
- Create: `更新两个项目.cmd`
- Create: `tests/test_windows_upstream_controls.py`

**Interfaces:**
- Consumes: upstream-sync CLI, existing Start/Stop scripts, `.venv`, Node, transaction state.
- Produces: read-only check and confirmed update buttons with deterministic exit codes and rollback.

- [ ] **Step 1: Write failing Windows behavior tests**

Tests invoke scripts from an unrelated current directory with a temporary fixture workspace and a fake Python CLI. Assert:

- check passes `check --human`, returns the fake exit code, and creates no files;
- update without `-ConfirmUpdate` invokes only `check --human` and exits nonzero without `plan-update`;
- confirmed success calls phases in order `plan-update`, Stop-Local, `apply`, pytest, both Node tests, Start-Local, health check, `finalize`;
- a pytest failure calls `rollback`, restores a fixture virtualenv backup when present, restarts the old service, and exits nonzero;
- all resolved recursive copy/remove targets remain inside the fixture root.

- [ ] **Step 2: Run RED verification**

Set `RUN_UPSTREAM_CONTROL_INTEGRATION=1` and run the test file. Expected: FAIL because scripts do not exist.

- [ ] **Step 3: Implement read-only wrappers**

`Check-Upstreams.ps1` resolves project root from `$PSScriptRoot`, requires `.venv\Scripts\python.exe` and the lock, then runs:

```powershell
& $python -m manager.upstream_sync check --workspace $projectRoot --lock $lockPath --human
exit $LASTEXITCODE
```

`检查更新.cmd` uses `%~dp0`, CRLF, UTF-8, passes `%*`, and pauses only on nonzero exit.

- [ ] **Step 4: Implement confirmed update orchestration**

`Update-Upstreams.ps1` accepts `[switch]$ConfirmUpdate` and `[ValidateSet('all','receiver','converter')]$Project='all'`. Without confirmation it runs check, prints the exact files that may change, and exits 3. With confirmation it:

1. calls `plan-update --project` and reads the emitted transaction path;
2. if requirements changed, reads the base interpreter from `$projectRoot\.venv\pyvenv.cfg`, resolves `$projectRoot\.venv-next` and the transaction-specific previous-environment path strictly inside the project, removes only a stale verified `.venv-next`, creates `.venv-next` from that base interpreter, and installs the staged new `requirements.txt` plus pytest before stopping the service;
3. calls existing Stop-Local and writes `service-stopped.marker` only after success;
4. calls `apply`;
5. when requirements changed, renames `$projectRoot\.venv` to the transaction-specific previous path and `$projectRoot\.venv-next` to `$projectRoot\.venv`, then runs all gates with the new environment; otherwise it reuses the existing environment;
6. runs full pytest and both Node test files;
7. writes `tests-passed.marker`, calls Start-Local with `-NoBrowser`, polls `/health` and `/manager`, then writes `health-passed.marker` and calls `finalize`;
8. on any terminating error, stops a partially started service, calls rollback with a verified Python interpreter, removes the failed new environment only after resolving it inside the project, renames the transaction-specific previous environment back to `$projectRoot\.venv` when present, starts the old service, verifies `/health`, and exits 1.

Every recursive copy/remove first resolves both root and target and rejects a target not strictly inside the project. Save PS1 files with UTF-8 BOM. `更新两个项目.cmd` uses CRLF and calls `Update-Upstreams.ps1`; on exit code 3 it explains that the user must rerun with `-ConfirmUpdate` or double-click and type the literal confirmation requested by the script.

- [ ] **Step 5: Run GREEN wrapper tests and commit**

```powershell
$env:RUN_UPSTREAM_CONTROL_INTEGRATION='1'
.\.venv\Scripts\python.exe -m pytest tests/test_windows_upstream_controls.py -q
Remove-Item Env:RUN_UPSTREAM_CONTROL_INTEGRATION
git add ops/upstream/Check-Upstreams.ps1 ops/upstream/Update-Upstreams.ps1 tests/test_windows_upstream_controls.py '检查更新.cmd' '更新两个项目.cmd'
git commit -m "feat: add safe upstream update controls"
```

### Task 5: Verify current pins, no-op updates, and rollback evidence

**Files:**
- Verify all sync files and existing manager files.

**Interfaces:**
- Produces: fresh evidence that both pins are current and check operations do not mutate source or runtime.

- [ ] **Step 1: Run all automated tests**

```powershell
.\.venv\Scripts\python.exe -m pytest -q
node vendor\GPTSession2CPAandSub2API\tests\convert-session.test.js
node tests\manager-bridge.test.js
```

- [ ] **Step 2: Record a pre-check snapshot**

Record `git status --porcelain=v1`, manager PID, `/health`, lock-file SHA-256, credential-directory file names/lengths/last-write times, and a recursive source manifest excluding ignored runtime directories.

- [ ] **Step 3: Run the real read-only GitHub check**

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ops\upstream\Check-Upstreams.ps1
```

Expected: both projects report their full pinned commits as current and no update is applied.

- [ ] **Step 4: Prove the check was non-mutating**

Recompute every Step 2 snapshot and assert exact equality, including unchanged service PID and credential metadata.

- [ ] **Step 5: Exercise rollback in a temporary fixture only**

Run the transaction rollback test with a fixture update that changes, adds, and deletes files and forces the test gate to fail. Confirm old bytes and lock return exactly; do not run a fake update against the production workspace.

- [ ] **Step 6: Final smoke and handoff**

Open `/manager`, confirm both module cards and update states, verify `/tools/session-converter/`, `/health`, and `/` return 200, run `git diff --check`, show the final commits, and leave `manager_app.py` running.
