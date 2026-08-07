from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
WINDOWS_DEVICE_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}
GITHUB_JSON_LIMIT = 2 * 1024 * 1024
ARCHIVE_LIMIT = 100 * 1024 * 1024
EXTRACTED_LIMIT = 512 * 1024 * 1024
ARCHIVE_FILE_LIMIT = 10_000
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

    def download_archive(self, repository: str, commit: str) -> bytes:
        if not REPOSITORY_PATTERN.fullmatch(repository) or not SHA_PATTERN.fullmatch(commit):
            raise SyncError("codeload 请求参数无效")
        request = urllib.request.Request(
            f"https://codeload.github.com/{repository}/zip/{commit}",
            headers={"User-Agent": USER_AGENT},
        )
        try:
            with self._opener(request, timeout=GITHUB_TIMEOUT_SECONDS) as response:
                status = int(getattr(response, "status", 200))
                if status != 200:
                    raise SyncError(f"codeload 请求失败: HTTP {status}")
                body = response.read(ARCHIVE_LIMIT + 1)
        except urllib.error.HTTPError as exc:
            raise SyncError(f"codeload 请求失败: HTTP {exc.code}") from exc
        except SyncError:
            raise
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            raise SyncError("codeload 下载失败") from exc
        if len(body) > ARCHIVE_LIMIT:
            raise SyncError("codeload 归档超过 100 MiB")
        return body


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
    for part in result.parts:
        device_name = part.split(".", 1)[0].casefold()
        if (
            part.endswith((".", " "))
            or any(ord(character) < 32 for character in part)
            or device_name in WINDOWS_DEVICE_NAMES
        ):
            raise SyncError(f"Windows 不支持的仓库路径: {value}")
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


def _spec_from_dict(row: object, *, context: str) -> UpstreamSpec:
    if not isinstance(row, dict):
        raise SyncError(f"{context} 必须是对象")
    key = _require_string(row, "key", context=context)
    if not re.fullmatch(r"[a-z0-9_-]+", key):
        raise SyncError(f"{context} 的 key 无效")
    repository = _require_string(row, "repository", context=context)
    if not REPOSITORY_PATTERN.fullmatch(repository):
        raise SyncError(f"{context} 的仓库名无效")
    branch = _require_string(row, "branch", context=context)
    commit = _require_string(row, "commit", context=context)
    if not SHA_PATTERN.fullmatch(commit):
        raise SyncError(f"{context} 的提交 SHA 无效")
    target = _require_string(row, "target", context=context)
    if target != ".":
        safe_repo_path(target)
    mode = _require_string(row, "mode", context=context)
    if mode not in {"overlay", "replace"}:
        raise SyncError(f"{context} 的更新模式无效")
    raw_files = row.get("files")
    if not isinstance(raw_files, list):
        raise SyncError(f"{context} 缺少 files")
    files = tuple(_file_entry(item, context=context) for item in raw_files)
    if len({item.path for item in files}) != len(files):
        raise SyncError(f"{context} 包含重复文件路径")
    return UpstreamSpec(key, repository, branch, commit, target, mode, files)


def _lock_from_transaction(row: object) -> LockFile:
    if not isinstance(row, dict) or row.get("schema_version") != 1:
        raise SyncError("事务中的锁文件无效")
    upstream_rows = row.get("upstreams")
    protected_rows = row.get("protected_prefixes")
    if not isinstance(upstream_rows, list) or not isinstance(protected_rows, list):
        raise SyncError("事务中的锁文件结构无效")
    upstreams = tuple(
        _spec_from_dict(item, context=f"事务上游 {index}")
        for index, item in enumerate(upstream_rows, start=1)
    )
    if len({item.key for item in upstreams}) != len(upstreams):
        raise SyncError("事务中的锁文件包含重复上游键")
    protected: list[str] = []
    for value in protected_rows:
        if not isinstance(value, str) or not value:
            raise SyncError("事务中的受保护路径无效")
        directory = value.endswith("/")
        safe_repo_path(value[:-1] if directory else value)
        protected.append(value)
    lock = LockFile(1, upstreams, tuple(protected))
    validate_upstream_paths(lock)
    return lock


def _write_bytes_atomic(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def _download_with_retries(client: Any, repository: str, commit: str, sleeper) -> bytes:
    last_error: SyncError | None = None
    for attempt in range(3):
        try:
            body = client.download_archive(repository, commit)
            if not isinstance(body, bytes):
                raise SyncError("codeload 客户端未返回字节")
            if len(body) > ARCHIVE_LIMIT:
                raise SyncError("codeload 归档超过 100 MiB")
            return body
        except SyncError as exc:
            last_error = exc
            if attempt < 2:
                sleeper(2**attempt)
    raise SyncError(f"下载重试失败: {last_error}")


def _validated_zip_members(
    archive: zipfile.ZipFile,
    expected: dict[str, FileEntry],
) -> dict[str, zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) > ARCHIVE_FILE_LIMIT + 1_000:
        raise SyncError("ZIP 项目数超过限制")
    roots: set[str] = set()
    files: dict[str, zipfile.ZipInfo] = {}
    total_size = 0
    for info in infos:
        name = info.filename
        if not name or "\\" in name or name.startswith("/"):
            raise SyncError(f"ZIP 路径不安全: {name}")
        parts = PurePosixPath(name).parts
        if any(part in {"", ".", ".."} for part in parts) or not parts:
            raise SyncError(f"ZIP 路径不安全: {name}")
        roots.add(parts[0])
        if info.flag_bits & 0x1:
            raise SyncError(f"ZIP 包含加密项: {name}")
        unix_type = (info.external_attr >> 16) & 0o170000
        if unix_type == 0o120000:
            raise SyncError(f"ZIP 包含符号链接: {name}")
        if info.is_dir():
            continue
        if unix_type not in {0, 0o100000}:
            raise SyncError(f"ZIP 文件类型不受支持: {name}")
        if len(parts) < 2:
            raise SyncError(f"ZIP 缺少单一根目录: {name}")
        relative = PurePosixPath(*parts[1:]).as_posix()
        safe_repo_path(relative)
        if relative in files:
            raise SyncError(f"ZIP 包含重复文件: {relative}")
        files[relative] = info
        total_size += info.file_size
        if total_size > EXTRACTED_LIMIT:
            raise SyncError("ZIP 解压后大小超过 512 MiB")
    if len(roots) != 1:
        raise SyncError("ZIP 必须只有一个根目录")
    if len(files) > ARCHIVE_FILE_LIMIT:
        raise SyncError("ZIP 文件数超过 10000")
    missing = sorted(set(expected) - set(files))
    unexpected = sorted(set(files) - set(expected))
    if missing or unexpected:
        raise SyncError(f"归档文件清单不匹配: {', '.join(missing + unexpected)}")
    for name, info in files.items():
        if info.file_size != expected[name].size:
            raise SyncError(f"归档文件大小不匹配: {name}")
    return files


def _extract_verified_archive(
    archive_body: bytes,
    destination: Path,
    remote: RemoteTree,
) -> tuple[FileEntry, ...]:
    if remote.truncated:
        raise SyncError("Git tree 响应被截断")
    expected = _manifest_from_tree_rows(remote.rows)
    try:
        with zipfile.ZipFile(io.BytesIO(archive_body)) as archive:
            members = _validated_zip_members(archive, expected)
            destination.mkdir(parents=True, exist_ok=True)
            for name in sorted(members):
                with archive.open(members[name], "r") as source:
                    body = source.read(expected[name].size + 1)
                if len(body) != expected[name].size:
                    raise SyncError(f"归档文件大小不匹配: {name}")
                _write_bytes_atomic(resolve_inside(destination, name, allow_root=False), body)
    except zipfile.BadZipFile as exc:
        raise SyncError("codeload 归档不是有效 ZIP") from exc
    return tuple(verify_extracted_tree(destination, list(remote.rows)))


def _diff_specs(old: UpstreamSpec, new: UpstreamSpec) -> tuple[list[str], list[str], list[str]]:
    old_files = {item.path: item for item in old.files}
    new_files = {item.path: item for item in new.files}
    added = sorted(set(new_files) - set(old_files))
    deleted = sorted(set(old_files) - set(new_files))
    changed = sorted(
        name
        for name in set(old_files) & set(new_files)
        if old_files[name].sha != new_files[name].sha
        or old_files[name].size != new_files[name].size
    )
    return added, changed, deleted


def plan_update(
    workspace: Path,
    lock_path: Path,
    project: str,
    client: Any,
    *,
    sleeper=time.sleep,
) -> Path:
    root = Path(workspace).resolve()
    lock_file = Path(lock_path).resolve()
    try:
        lock_file.relative_to(root)
    except ValueError as exc:
        raise SyncError("锁文件位于项目目录之外") from exc
    current = load_lock(lock_file)
    known_keys = {spec.key for spec in current.upstreams}
    if project != "all" and project not in known_keys:
        raise SyncError(f"未知上游项目: {project}")
    selected = [
        spec for spec in current.upstreams if project == "all" or spec.key == project
    ]
    for spec in current.upstreams:
        resolve_inside(root, spec.target)
        verify_local_manifest(root, spec)

    now = datetime.now(timezone.utc)
    transaction_id = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:12]}"
    staging_root = resolve_inside(
        root, f"data/upstream-staging/{transaction_id}", allow_root=False
    )
    backup_root = resolve_inside(
        root, f"data/upstream-backups/{transaction_id}", allow_root=False
    )
    staging_root.mkdir(parents=True, exist_ok=False)

    replacements: dict[str, UpstreamSpec] = {}
    updates: list[dict[str, object]] = []
    for old_spec in selected:
        latest = client.latest_commit(old_spec.repository, old_spec.branch)
        if latest.sha == old_spec.commit:
            continue
        remote = client.commit_tree(old_spec.repository, latest.sha)
        if not isinstance(remote, RemoteTree):
            raise SyncError(f"{old_spec.key} 的 Git tree 响应无效")
        if remote.truncated:
            raise SyncError(f"Git tree 响应被截断: {old_spec.key}")
        manifest = _manifest_from_tree_rows(remote.rows)
        new_spec = UpstreamSpec(
            key=old_spec.key,
            repository=old_spec.repository,
            branch=old_spec.branch,
            commit=latest.sha,
            target=old_spec.target,
            mode=old_spec.mode,
            files=tuple(manifest[name] for name in sorted(manifest)),
        )
        replacements[old_spec.key] = new_spec
        candidate_lock = LockFile(
            1,
            tuple(replacements.get(spec.key, spec) for spec in current.upstreams),
            current.protected_prefixes,
        )
        validate_upstream_paths(candidate_lock)

        archive_body = _download_with_retries(
            client, old_spec.repository, latest.sha, sleeper
        )
        archive_path = staging_root / f"{old_spec.key}.zip"
        _write_bytes_atomic(archive_path, archive_body)
        extracted = staging_root / "extracted" / old_spec.key
        verified = _extract_verified_archive(archive_body, extracted, remote)
        if verified != new_spec.files:
            raise SyncError(f"{old_spec.key} 的归档清单与 Git tree 不一致")
        added, changed, deleted = _diff_specs(old_spec, new_spec)
        target = resolve_inside(root, old_spec.target)
        for name in added:
            if resolve_inside(target, name, allow_root=False).exists():
                raise SyncError(f"本地清单外路径冲突: {name}")
        updates.append(
            {
                "key": old_spec.key,
                "old_spec": old_spec.as_dict(),
                "new_spec": new_spec.as_dict(),
                "added": added,
                "changed": changed,
                "deleted": deleted,
                "requirements_changed": "requirements.txt"
                in set(added + changed + deleted),
            }
        )

    new_lock = LockFile(
        1,
        tuple(replacements.get(spec.key, spec) for spec in current.upstreams),
        current.protected_prefixes,
    )
    validate_upstream_paths(new_lock)

    backup_root.mkdir(parents=True, exist_ok=False)
    old_lock_body = lock_file.read_bytes()
    _write_bytes_atomic(backup_root / "lock.json", old_lock_body)
    for update in updates:
        old_spec = _spec_from_dict(update["old_spec"], context="旧上游")
        source_root = resolve_inside(root, old_spec.target)
        backup_source = backup_root / "source" / old_spec.key
        for item in old_spec.files:
            source = resolve_inside(source_root, item.path, allow_root=False)
            body = source.read_bytes()
            if len(body) != item.size or git_blob_sha(body) != item.sha:
                raise SyncError(f"备份前本地上游文件已改变: {item.path}")
            _write_bytes_atomic(
                resolve_inside(backup_source, item.path, allow_root=False), body
            )

    transaction = {
        "schema_version": 1,
        "id": transaction_id,
        "state": "planned",
        "created_at": now.isoformat().replace("+00:00", "Z"),
        "workspace": str(root),
        "lock_path": str(lock_file),
        "backup_dir": str(backup_root),
        "backup_lock_sha256": hashlib.sha256(old_lock_body).hexdigest(),
        "selected": [spec.key for spec in selected],
        "update_count": len(updates),
        "requirements_changed": any(
            bool(update["requirements_changed"]) for update in updates
        ),
        "updates": updates,
        "new_lock": new_lock.as_dict(),
    }
    transaction_path = staging_root / "transaction.json"
    _atomic_write_json(transaction_path, transaction)
    return transaction_path


def _load_transaction(workspace: Path, transaction_path: Path) -> tuple[dict[str, Any], Path, Path, Path]:
    root = Path(workspace).resolve()
    path = Path(transaction_path).resolve()
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise SyncError("事务文件位于项目目录之外") from exc
    if len(relative.parts) < 4 or relative.parts[:2] != ("data", "upstream-staging"):
        raise SyncError("事务文件不在 data/upstream-staging 内")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SyncError("无法读取更新事务") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise SyncError("更新事务结构无效")
    if str(payload.get("workspace") or "").casefold() != str(root).casefold():
        raise SyncError("更新事务不属于当前工作区")
    if payload.get("id") != path.parent.name:
        raise SyncError("更新事务 ID 与目录不匹配")
    lock_path = Path(str(payload.get("lock_path") or "")).resolve()
    backup_dir = Path(str(payload.get("backup_dir") or "")).resolve()
    try:
        lock_path.relative_to(root)
        backup_relative = backup_dir.relative_to(root)
    except ValueError as exc:
        raise SyncError("事务路径位于项目目录之外") from exc
    if backup_relative.parts[:2] != ("data", "upstream-backups"):
        raise SyncError("事务备份目录无效")
    if backup_dir.name != payload.get("id"):
        raise SyncError("事务备份目录与 ID 不匹配")
    updates = payload.get("updates")
    if not isinstance(updates, list) or payload.get("update_count") != len(updates):
        raise SyncError("事务更新清单无效")
    _lock_from_transaction(payload.get("new_lock"))
    return payload, path, lock_path, backup_dir


def _validated_transaction_update(update: object) -> tuple[UpstreamSpec, UpstreamSpec]:
    if not isinstance(update, dict):
        raise SyncError("事务更新项必须是对象")
    old_spec = _spec_from_dict(update.get("old_spec"), context="事务旧上游")
    new_spec = _spec_from_dict(update.get("new_spec"), context="事务新上游")
    if (
        old_spec.key != new_spec.key
        or old_spec.repository != new_spec.repository
        or old_spec.branch != new_spec.branch
        or old_spec.target != new_spec.target
        or old_spec.mode != new_spec.mode
        or update.get("key") != old_spec.key
    ):
        raise SyncError("事务前后上游边界不一致")
    added, changed, deleted = _diff_specs(old_spec, new_spec)
    if update.get("added") != added or update.get("changed") != changed or update.get("deleted") != deleted:
        raise SyncError(f"事务文件差异清单无效: {old_spec.key}")
    return old_spec, new_spec


def _stage_spec(spec: UpstreamSpec) -> UpstreamSpec:
    return UpstreamSpec(
        key=spec.key,
        repository=spec.repository,
        branch=spec.branch,
        commit=spec.commit,
        target=".",
        mode="replace",
        files=spec.files,
    )


def _remove_managed_file(target: Path, name: str) -> None:
    path = resolve_inside(target, name, allow_root=False)
    if path.is_symlink():
        raise SyncError(f"拒绝删除符号链接路径: {name}")
    if path.exists():
        if not path.is_file():
            raise SyncError(f"受管理路径不是普通文件: {name}")
        path.unlink()
    parent = path.parent
    while parent != target and parent.is_dir():
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def apply_transaction(workspace: Path, transaction_path: Path) -> None:
    payload, path, lock_path, _backup_dir = _load_transaction(workspace, transaction_path)
    if payload.get("state") != "planned":
        raise SyncError(f"事务状态不是 planned: {payload.get('state')}")
    if not (path.parent / "service-stopped.marker").is_file():
        raise SyncError("缺少 service-stopped.marker，拒绝修改源码")
    root = Path(workspace).resolve()
    new_lock = _lock_from_transaction(payload["new_lock"])
    updates: list[tuple[dict[str, Any], UpstreamSpec, UpstreamSpec]] = []
    for raw_update in payload["updates"]:
        old_spec, new_spec = _validated_transaction_update(raw_update)
        verify_local_manifest(root, old_spec)
        target = resolve_inside(root, old_spec.target)
        for name in raw_update["added"]:
            if resolve_inside(target, name, allow_root=False).exists():
                raise SyncError(f"本地清单外路径冲突: {name}")
        extracted = resolve_inside(
            path.parent, f"extracted/{old_spec.key}", allow_root=False
        )
        verify_local_manifest(extracted, _stage_spec(new_spec))
        updates.append((raw_update, old_spec, new_spec))

    payload["state"] = "applying"
    _atomic_write_json(path, payload)
    for raw_update, old_spec, new_spec in updates:
        target = resolve_inside(root, old_spec.target)
        extracted = resolve_inside(
            path.parent, f"extracted/{old_spec.key}", allow_root=False
        )
        for name in raw_update["added"] + raw_update["changed"]:
            source = resolve_inside(extracted, name, allow_root=False)
            destination = resolve_inside(target, name, allow_root=False)
            _write_bytes_atomic(destination, source.read_bytes())
        for name in raw_update["deleted"]:
            _remove_managed_file(target, name)
    _atomic_write_json(lock_path, new_lock.as_dict())
    payload["state"] = "applied"
    payload["applied_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _atomic_write_json(path, payload)


def _verify_backup(payload: dict[str, Any], backup_dir: Path) -> None:
    lock_backup = backup_dir / "lock.json"
    try:
        lock_body = lock_backup.read_bytes()
    except OSError as exc:
        raise SyncError("事务锁文件备份缺失") from exc
    if hashlib.sha256(lock_body).hexdigest() != payload.get("backup_lock_sha256"):
        raise SyncError("事务锁文件备份校验失败")
    for raw_update in payload["updates"]:
        old_spec, _new_spec = _validated_transaction_update(raw_update)
        backup_source = backup_dir / "source" / old_spec.key
        for item in old_spec.files:
            path = resolve_inside(backup_source, item.path, allow_root=False)
            try:
                body = path.read_bytes()
            except OSError as exc:
                raise SyncError(f"事务文件备份缺失: {old_spec.key}/{item.path}") from exc
            if len(body) != item.size or git_blob_sha(body) != item.sha:
                raise SyncError(f"事务文件备份校验失败: {old_spec.key}/{item.path}")


def rollback_transaction(workspace: Path, transaction_path: Path) -> None:
    payload, path, lock_path, backup_dir = _load_transaction(workspace, transaction_path)
    state = payload.get("state")
    if state == "rolled_back":
        return
    if state not in {"planned", "applying", "applied"}:
        raise SyncError(f"事务状态不可回滚: {state}")
    _verify_backup(payload, backup_dir)
    root = Path(workspace).resolve()
    if state in {"applying", "applied"}:
        for raw_update in payload["updates"]:
            old_spec, new_spec = _validated_transaction_update(raw_update)
            target = resolve_inside(root, old_spec.target)
            for item in new_spec.files:
                _remove_managed_file(target, item.path)
            backup_source = backup_dir / "source" / old_spec.key
            for item in old_spec.files:
                source = resolve_inside(backup_source, item.path, allow_root=False)
                destination = resolve_inside(target, item.path, allow_root=False)
                _write_bytes_atomic(destination, source.read_bytes())
        _write_bytes_atomic(lock_path, (backup_dir / "lock.json").read_bytes())
    payload["state"] = "rolled_back"
    payload["rolled_back_at"] = datetime.now(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    _atomic_write_json(path, payload)


def _prune_backups(root: Path, *, keep: int = 3) -> None:
    backup_root = resolve_inside(root, "data/upstream-backups", allow_root=False)
    if not backup_root.is_dir():
        return
    directories = sorted(
        (item for item in backup_root.iterdir() if item.is_dir()),
        key=lambda item: item.name,
        reverse=True,
    )
    for old in directories[keep:]:
        resolved = old.resolve()
        try:
            resolved.relative_to(backup_root)
        except ValueError as exc:
            raise SyncError(f"备份目录位于项目之外: {old}") from exc
        shutil.rmtree(resolved)


def _write_update_log(root: Path, payload: dict[str, Any]) -> Path:
    updates = []
    for raw_update in payload["updates"]:
        old_spec, new_spec = _validated_transaction_update(raw_update)
        updates.append(
            {
                "key": old_spec.key,
                "repository": old_spec.repository,
                "old_commit": old_spec.commit,
                "new_commit": new_spec.commit,
                "added": list(raw_update["added"]),
                "changed": list(raw_update["changed"]),
                "deleted": list(raw_update["deleted"]),
            }
        )
    log_dir = resolve_inside(root, "data/upstream-update-logs", allow_root=False)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = resolve_inside(
        log_dir, f"{payload['id']}.json", allow_root=False
    )
    _atomic_write_json(
        log_path,
        {
            "schema_version": 1,
            "id": payload["id"],
            "state": payload["state"],
            "created_at": payload["created_at"],
            "completed_at": payload.get("completed_at"),
            "rolled_back_at": payload.get("rolled_back_at"),
            "backup_dir": payload["backup_dir"],
            "update_count": payload["update_count"],
            "requirements_changed": payload["requirements_changed"],
            "updates": updates,
        },
    )
    return log_path


def finalize_transaction(workspace: Path, transaction_path: Path) -> None:
    payload, path, _lock_path, _backup_dir = _load_transaction(workspace, transaction_path)
    state = payload.get("state")
    if payload.get("update_count") == 0 and state == "planned":
        pass
    elif state != "applied":
        raise SyncError(f"事务状态不是 applied: {state}")
    else:
        for marker in ("tests-passed.marker", "health-passed.marker"):
            if not (path.parent / marker).is_file():
                raise SyncError(f"缺少 {marker}，拒绝完成事务")
    payload["state"] = "complete"
    payload["completed_at"] = datetime.now(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    _atomic_write_json(path, payload)
    root = Path(workspace).resolve()
    _write_update_log(root, payload)
    _prune_backups(root)
    shutil.rmtree(path.parent)


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
    plan = subparsers.add_parser("plan-update")
    plan.add_argument("--workspace", required=True)
    plan.add_argument("--lock", required=True)
    plan.add_argument("--project", default="all")
    for name in ("apply", "finalize", "rollback"):
        command = subparsers.add_parser(name)
        command.add_argument("--workspace", required=True)
        command.add_argument("--transaction", required=True)
    return parser


def main(
    argv: list[str] | None = None,
    *,
    client: Any | None = None,
    sleeper=time.sleep,
) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command in {"apply", "finalize", "rollback"}:
            workspace = Path(args.workspace).resolve()
            transaction = Path(args.transaction)
            if args.command == "apply":
                apply_transaction(workspace, transaction)
                state = "applied"
            elif args.command == "finalize":
                finalize_transaction(workspace, transaction)
                state = "complete"
            else:
                rollback_transaction(workspace, transaction)
                state = "rolled_back"
            print(json.dumps({"transaction": str(transaction.resolve()), "state": state}))
            return 0
        workspace, lock_path = _cli_workspace_and_lock(args.workspace, args.lock)
        github = client or GitHubClient()
        if args.command == "plan-update":
            transaction_path = plan_update(
                workspace,
                lock_path,
                args.project,
                github,
                sleeper=sleeper,
            )
            transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
            print(
                json.dumps(
                    {
                        "transaction": str(transaction_path),
                        "update_count": transaction["update_count"],
                        "requirements_changed": transaction["requirements_changed"],
                        "projects": [
                            {
                                "key": update["key"],
                                "added": update["added"],
                                "changed": update["changed"],
                                "deleted": update["deleted"],
                            }
                            for update in transaction["updates"]
                        ],
                    },
                    ensure_ascii=False,
                )
            )
            return 0
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
