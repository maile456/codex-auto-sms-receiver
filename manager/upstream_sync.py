from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
GITHUB_JSON_LIMIT = 2 * 1024 * 1024
GITHUB_TIMEOUT_SECONDS = 10
USER_AGENT = "Codex-Unified-Local-Manager"


class SyncError(RuntimeError):
    """A non-sensitive error that stops an upstream operation safely."""


@dataclass(frozen=True)
class FileEntry:
    path: str
    type: str
    sha: str
    size: int

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "type": self.type,
            "sha": self.sha,
            "size": self.size,
        }


@dataclass(frozen=True)
class UpstreamSpec:
    key: str
    repository: str
    branch: str
    commit: str
    target: str
    mode: str
    files: tuple[FileEntry, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "repository": self.repository,
            "branch": self.branch,
            "commit": self.commit,
            "target": self.target,
            "mode": self.mode,
            "files": [entry.as_dict() for entry in self.files],
        }


@dataclass(frozen=True)
class LockFile:
    schema_version: int
    upstreams: tuple[UpstreamSpec, ...]
    protected_prefixes: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "upstreams": [spec.as_dict() for spec in self.upstreams],
            "protected_prefixes": list(self.protected_prefixes),
        }


@dataclass(frozen=True)
class RemoteTree:
    rows: tuple[dict[str, Any], ...]
    truncated: bool


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


class GitHubClient:
    def __init__(self, *, opener=None):
        self._opener = opener or urllib.request.urlopen

    def _get_json(self, url: str) -> dict[str, Any]:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with self._opener(request, timeout=GITHUB_TIMEOUT_SECONDS) as response:
                status = int(getattr(response, "status", 200))
                if status != 200:
                    raise SyncError(f"GitHub API 请求失败: HTTP {status}")
                body = response.read(GITHUB_JSON_LIMIT + 1)
        except urllib.error.HTTPError as exc:
            raise SyncError(f"GitHub API 请求失败: HTTP {exc.code}") from exc
        except SyncError:
            raise
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            raise SyncError("GitHub 请求失败") from exc
        if len(body) > GITHUB_JSON_LIMIT:
            raise SyncError("GitHub 响应超过 2 MiB")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise SyncError("GitHub 响应不是有效 JSON") from exc
        if not isinstance(payload, dict):
            raise SyncError("GitHub 响应结构无效")
        return payload

    def latest_commit(self, repository: str, branch: str) -> RemoteCommit:
        if not REPOSITORY_PATTERN.fullmatch(repository):
            raise SyncError(f"仓库名无效: {repository}")
        encoded_branch = urllib.parse.quote(branch, safe="")
        payload = self._get_json(
            f"https://api.github.com/repos/{repository}/commits/{encoded_branch}"
        )
        sha = payload.get("sha")
        commit = payload.get("commit")
        if not isinstance(sha, str) or not SHA_PATTERN.fullmatch(sha):
            raise SyncError("GitHub 提交响应缺少有效 SHA")
        if not isinstance(commit, dict):
            raise SyncError("GitHub 提交响应缺少 commit")
        message = commit.get("message")
        committer = commit.get("committer")
        if not isinstance(message, str) or not isinstance(committer, dict):
            raise SyncError("GitHub 提交响应结构无效")
        committed_at = committer.get("date")
        if not isinstance(committed_at, str):
            raise SyncError("GitHub 提交响应缺少时间")
        return RemoteCommit(
            sha=sha,
            title=message.splitlines()[0][:300],
            committed_at=committed_at,
        )

    def commit_tree(self, repository: str, commit: str) -> RemoteTree:
        if not REPOSITORY_PATTERN.fullmatch(repository) or not SHA_PATTERN.fullmatch(commit):
            raise SyncError("GitHub tree 请求参数无效")
        payload = self._get_json(
            f"https://api.github.com/repos/{repository}/git/trees/{commit}?recursive=1"
        )
        rows = payload.get("tree")
        truncated = payload.get("truncated")
        if not isinstance(rows, list) or not isinstance(truncated, bool):
            raise SyncError("GitHub tree 响应结构无效")
        if not all(isinstance(row, dict) for row in rows):
            raise SyncError("GitHub tree 项必须是对象")
        return RemoteTree(rows=tuple(rows), truncated=truncated)


def git_blob_sha(data: bytes) -> str:
    header = b"blob " + str(len(data)).encode("ascii") + b"\0"
    return hashlib.sha1(header + data).hexdigest()


def safe_repo_path(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise SyncError(f"不安全的仓库路径: {value}")
    if value.startswith("/") or value.endswith("/") or "//" in value:
        raise SyncError(f"不安全的仓库路径: {value}")
    result = PurePosixPath(value)
    if result.is_absolute() or any(part in {"", ".", ".."} for part in result.parts):
        raise SyncError(f"不安全的仓库路径: {value}")
    return result


def resolve_inside(workspace: Path, value: str, *, allow_root: bool = True) -> Path:
    root = Path(workspace).resolve()
    if allow_root and value in {"", "."}:
        return root
    try:
        relative = safe_repo_path(value)
    except SyncError as exc:
        raise SyncError(f"路径位于项目目录之外: {value}") from exc
    candidate = (root / Path(*relative.parts)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise SyncError(f"路径位于项目目录之外: {value}") from exc
    if not allow_root and candidate == root:
        raise SyncError(f"路径必须位于项目目录内部: {value}")
    return candidate


def _require_string(row: dict[str, Any], name: str, *, context: str) -> str:
    value = row.get(name)
    if not isinstance(value, str) or not value:
        raise SyncError(f"{context} 缺少字段: {name}")
    return value


def _file_entry(row: object, *, context: str) -> FileEntry:
    if not isinstance(row, dict):
        raise SyncError(f"{context} 的文件项必须是对象")
    path = _require_string(row, "path", context=context)
    safe_repo_path(path)
    file_type = _require_string(row, "type", context=context)
    if file_type != "blob":
        raise SyncError(f"{context} 的文件类型无效: {path}")
    sha = _require_string(row, "sha", context=context)
    if not SHA_PATTERN.fullmatch(sha):
        raise SyncError(f"{context} 的 blob SHA 无效: {path}")
    size = row.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise SyncError(f"{context} 的文件大小无效: {path}")
    return FileEntry(path=path, type=file_type, sha=sha, size=size)


def load_lock(path: Path) -> LockFile:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SyncError(f"无法读取上游锁文件: {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise SyncError("上游锁文件 schema_version 必须为 1")
    raw_upstreams = payload.get("upstreams")
    raw_protected = payload.get("protected_prefixes")
    if not isinstance(raw_upstreams, list) or not raw_upstreams:
        raise SyncError("上游锁文件缺少 upstreams")
    if not isinstance(raw_protected, list):
        raise SyncError("上游锁文件缺少 protected_prefixes")

    upstreams: list[UpstreamSpec] = []
    keys: set[str] = set()
    for index, row in enumerate(raw_upstreams, start=1):
        context = f"第 {index} 个上游"
        if not isinstance(row, dict):
            raise SyncError(f"{context} 必须是对象")
        key = _require_string(row, "key", context=context)
        if key in keys:
            raise SyncError(f"重复的上游键: {key}")
        keys.add(key)
        repository = _require_string(row, "repository", context=context)
        if not REPOSITORY_PATTERN.fullmatch(repository):
            raise SyncError(f"{context} 的仓库名无效: {repository}")
        branch = _require_string(row, "branch", context=context)
        commit = _require_string(row, "commit", context=context)
        if not SHA_PATTERN.fullmatch(commit):
            raise SyncError(f"{context} 的提交 SHA 无效")
        target = _require_string(row, "target", context=context)
        if target != ".":
            safe_repo_path(target)
        mode = _require_string(row, "mode", context=context)
        if mode not in {"overlay", "replace"}:
            raise SyncError(f"{context} 的更新模式无效: {mode}")
        raw_files = row.get("files")
        if not isinstance(raw_files, list):
            raise SyncError(f"{context} 缺少 files")
        files = tuple(_file_entry(item, context=context) for item in raw_files)
        if len({item.path for item in files}) != len(files):
            raise SyncError(f"{context} 包含重复文件路径")
        upstreams.append(
            UpstreamSpec(
                key=key,
                repository=repository,
                branch=branch,
                commit=commit,
                target=target,
                mode=mode,
                files=files,
            )
        )

    protected: list[str] = []
    for value in raw_protected:
        if not isinstance(value, str) or not value:
            raise SyncError("受保护路径必须是非空字符串")
        is_directory = value.endswith("/")
        normalized = value[:-1] if is_directory else value
        safe_repo_path(normalized)
        protected.append(normalized + ("/" if is_directory else ""))
    lock = LockFile(1, tuple(upstreams), tuple(protected))
    validate_upstream_paths(lock)
    return lock


def _deployed_path(spec: UpstreamSpec, entry_path: str) -> str:
    if spec.target == ".":
        return entry_path
    return f"{spec.target.rstrip('/')}/{entry_path}"


def _matches_protected(path: str, prefix: str) -> bool:
    if prefix.endswith("/"):
        return path.startswith(prefix)
    return path == prefix


def validate_upstream_paths(lock: LockFile) -> None:
    for spec in lock.upstreams:
        for file_entry in spec.files:
            deployed = _deployed_path(spec, file_entry.path)
            for prefix in lock.protected_prefixes:
                if _matches_protected(deployed, prefix):
                    raise SyncError(f"受保护路径冲突: {deployed}")


def _manifest_from_tree_rows(tree_rows: list[dict] | tuple[dict[str, Any], ...]) -> dict[str, FileEntry]:
    expected: dict[str, FileEntry] = {}
    for raw in tree_rows:
        if not isinstance(raw, dict):
            raise SyncError("Git tree 项必须是对象")
        path = str(raw.get("path") or "")
        safe_repo_path(path)
        item_type = raw.get("type")
        mode = str(raw.get("mode") or "")
        if item_type == "tree" and mode == "040000":
            continue
        if item_type == "commit":
            raise SyncError(f"不支持的 Git tree 项: {path}")
        if item_type != "blob":
            raise SyncError(f"不支持的 Git tree 项: {path}")
        if mode == "120000":
            raise SyncError(f"禁止符号链接: {path}")
        if mode not in {"100644", "100755"}:
            raise SyncError(f"不支持的 Git 文件模式: {path}")
        if path in expected:
            raise SyncError(f"Git tree 包含重复路径: {path}")
        expected[path] = _file_entry(raw, context="Git tree")
    return expected


def verify_extracted_tree(root: Path, tree_rows: list[dict]) -> list[FileEntry]:
    expected = _manifest_from_tree_rows(tree_rows)

    root = Path(root).resolve()
    actual_paths: dict[str, Path] = {}
    if root.is_dir():
        for path in root.rglob("*"):
            if path.is_symlink():
                raise SyncError(f"归档包含符号链接: {path.relative_to(root).as_posix()}")
            if path.is_file():
                actual_paths[path.relative_to(root).as_posix()] = path
    missing = sorted(set(expected) - set(actual_paths))
    unexpected = sorted(set(actual_paths) - set(expected))
    if missing or unexpected:
        details = ", ".join(missing + unexpected)
        raise SyncError(f"归档文件清单不匹配: {details}")
    for name, item in expected.items():
        body = actual_paths[name].read_bytes()
        if len(body) != item.size or git_blob_sha(body) != item.sha:
            raise SyncError(f"blob SHA 不匹配: {name}")
    return [expected[name] for name in sorted(expected)]


def verify_local_manifest(workspace: Path, spec: UpstreamSpec) -> None:
    target = resolve_inside(workspace, spec.target)
    expected = {entry.path: entry for entry in spec.files}
    for name, item in expected.items():
        path = resolve_inside(target, name, allow_root=False)
        if not path.is_file() or path.is_symlink():
            raise SyncError(f"缺少本地上游文件: {name}")
        body = path.read_bytes()
        if len(body) != item.size or git_blob_sha(body) != item.sha:
            raise SyncError(f"本地上游文件已修改: {name}")
    if spec.mode == "replace" and target.is_dir():
        actual = {
            path.relative_to(target).as_posix()
            for path in target.rglob("*")
            if path.is_file()
        }
        unexpected = sorted(actual - set(expected))
        if unexpected:
            raise SyncError(f"本地目标包含清单外文件: {', '.join(unexpected)}")


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path = Path(path)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def bootstrap_lock(workspace: Path, lock_path: Path, client: Any) -> LockFile:
    current = load_lock(lock_path)
    populated: list[UpstreamSpec] = []
    for spec in current.upstreams:
        remote = client.commit_tree(spec.repository, spec.commit)
        if not isinstance(remote, RemoteTree):
            raise SyncError(f"{spec.key} 的 Git tree 响应无效")
        if remote.truncated:
            raise SyncError(f"Git tree 响应被截断: {spec.key}")
        manifest = _manifest_from_tree_rows(remote.rows)
        populated.append(
            UpstreamSpec(
                key=spec.key,
                repository=spec.repository,
                branch=spec.branch,
                commit=spec.commit,
                target=spec.target,
                mode=spec.mode,
                files=tuple(manifest[name] for name in sorted(manifest)),
            )
        )
    result = LockFile(
        schema_version=current.schema_version,
        upstreams=tuple(populated),
        protected_prefixes=current.protected_prefixes,
    )
    validate_upstream_paths(result)
    for spec in result.upstreams:
        resolve_inside(workspace, spec.target)
        verify_local_manifest(workspace, spec)
    _atomic_write_json(Path(lock_path), result.as_dict())
    return result


def check_updates(lock: LockFile, client: Any) -> tuple[UpdateStatus, ...]:
    statuses: list[UpdateStatus] = []
    for spec in lock.upstreams:
        try:
            latest = client.latest_commit(spec.repository, spec.branch)
            statuses.append(
                UpdateStatus(
                    key=spec.key,
                    repository=spec.repository,
                    current_sha=spec.commit,
                    latest_sha=latest.sha,
                    update_available=latest.sha != spec.commit,
                    title=latest.title,
                    committed_at=latest.committed_at,
                )
            )
        except SyncError as exc:
            statuses.append(
                UpdateStatus(
                    key=spec.key,
                    repository=spec.repository,
                    current_sha=spec.commit,
                    latest_sha=None,
                    update_available=None,
                    title=None,
                    committed_at=None,
                    error=str(exc)[:300],
                )
            )
        except Exception as exc:
            statuses.append(
                UpdateStatus(
                    key=spec.key,
                    repository=spec.repository,
                    current_sha=spec.commit,
                    latest_sha=None,
                    update_available=None,
                    title=None,
                    committed_at=None,
                    error=f"GitHub 检查失败 ({type(exc).__name__})",
                )
            )
    return tuple(statuses)


def _cli_workspace_and_lock(workspace_value: str, lock_value: str) -> tuple[Path, Path]:
    workspace = Path(workspace_value).resolve()
    raw_lock = Path(lock_value)
    lock_path = raw_lock.resolve() if raw_lock.is_absolute() else (workspace / raw_lock).resolve()
    try:
        lock_path.relative_to(workspace)
    except ValueError as exc:
        raise SyncError(f"锁文件位于项目目录之外: {lock_path}") from exc
    return workspace, lock_path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="安全检查和同步两个 GitHub 上游项目")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("bootstrap-lock", "verify-local", "check"):
        command = subparsers.add_parser(name)
        command.add_argument("--workspace", required=True)
        command.add_argument("--lock", required=True)
        if name == "check":
            output = command.add_mutually_exclusive_group()
            output.add_argument("--json", action="store_true")
            output.add_argument("--human", action="store_true")
    return parser


def main(argv: list[str] | None = None, *, client: Any | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        workspace, lock_path = _cli_workspace_and_lock(args.workspace, args.lock)
        github = client or GitHubClient()
        if args.command == "bootstrap-lock":
            lock = bootstrap_lock(workspace, lock_path, github)
            print(f"已写入 {len(lock.upstreams)} 个上游的完整清单: {lock_path}")
            return 0
        lock = load_lock(lock_path)
        for spec in lock.upstreams:
            resolve_inside(workspace, spec.target)
        if args.command == "verify-local":
            for spec in lock.upstreams:
                verify_local_manifest(workspace, spec)
            print("本地上游文件与锁定清单一致。")
            return 0
        statuses = check_updates(lock, github)
        if args.json:
            print(
                json.dumps(
                    {"projects": [status.as_dict() for status in statuses]},
                    ensure_ascii=False,
                )
            )
        else:
            for status in statuses:
                if status.update_available is True:
                    state = "有更新"
                elif status.update_available is False:
                    state = "已是最新"
                else:
                    state = "无法检查"
                latest = status.latest_sha or "-"
                suffix = f" | {status.title}" if status.title else ""
                if status.error:
                    suffix += f" | {status.error}"
                print(
                    f"[{state}] {status.repository} | 当前 {status.current_sha} | "
                    f"最新 {latest}{suffix}"
                )
        return 2 if statuses and all(status.error is not None for status in statuses) else 0
    except SyncError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
