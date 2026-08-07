from __future__ import annotations

import json
import urllib.error
from pathlib import Path

import pytest

from manager.upstream_sync import (
    FileEntry,
    GitHubClient,
    LockFile,
    RemoteCommit,
    RemoteTree,
    SyncError,
    UpstreamSpec,
    UpdateStatus,
    bootstrap_lock,
    check_updates,
    git_blob_sha,
    load_lock,
    main,
    resolve_inside,
    safe_repo_path,
    validate_upstream_paths,
    verify_extracted_tree,
    verify_local_manifest,
)


def entry(path: str, body: bytes) -> FileEntry:
    return FileEntry(path=path, type="blob", sha=git_blob_sha(body), size=len(body))


def upstream_spec(
    *, files: list[FileEntry], target: str = ".", mode: str = "overlay"
) -> UpstreamSpec:
    return UpstreamSpec(
        key="receiver",
        repository="owner/project",
        branch="main",
        commit="1" * 40,
        target=target,
        mode=mode,
        files=tuple(files),
    )


def write_lock(path: Path, *, upstreams: list[dict], protected: list[str] | None = None) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "upstreams": upstreams,
                "protected_prefixes": protected or ["manager/", ".env"],
            }
        ),
        encoding="utf-8",
    )


def lock_row(*, key: str = "receiver", target: str = ".", files: list[dict] | None = None) -> dict:
    return {
        "key": key,
        "repository": "owner/project",
        "branch": "main",
        "commit": "1" * 40,
        "target": target,
        "mode": "overlay",
        "files": files or [],
    }


def test_git_blob_sha_matches_github_blob_algorithm():
    assert git_blob_sha(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"


@pytest.mark.parametrize(
    "value", ["../escape", "/absolute", "C:/drive", "a/../../b", "a\\b", "a//b"]
)
def test_safe_repo_path_rejects_unsafe_names(value):
    with pytest.raises(SyncError):
        safe_repo_path(value)


def test_resolve_inside_rejects_target_outside_workspace(tmp_path):
    with pytest.raises(SyncError, match="项目目录之外"):
        resolve_inside(tmp_path, "../outside")


def test_load_lock_rejects_duplicate_keys(tmp_path):
    lock_path = tmp_path / "lock.json"
    write_lock(lock_path, upstreams=[lock_row(), lock_row()])
    with pytest.raises(SyncError, match="重复的上游键: receiver"):
        load_lock(lock_path)


def test_validate_upstream_paths_rejects_protected_collision():
    lock = LockFile(
        schema_version=1,
        upstreams=(upstream_spec(files=[entry("manager/owned.py", b"x")]),),
        protected_prefixes=("manager/", ".env"),
    )
    with pytest.raises(SyncError, match="受保护路径冲突: manager/owned.py"):
        validate_upstream_paths(lock)


def test_verify_extracted_tree_accepts_directories_and_exact_blobs(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "index.html").write_bytes(b"hello\n")
    rows = [
        {"path": "docs", "type": "tree", "mode": "040000", "sha": "2" * 40},
        {
            "path": "docs/index.html",
            "type": "blob",
            "mode": "100644",
            "size": 6,
            "sha": "ce013625030ba8dba906f756967f9e9ca394464a",
        },
    ]
    assert verify_extracted_tree(tmp_path, rows) == [entry("docs/index.html", b"hello\n")]


def test_verify_extracted_tree_rejects_blob_mismatch(tmp_path):
    (tmp_path / "README.md").write_text("changed", encoding="utf-8")
    with pytest.raises(SyncError, match="blob SHA 不匹配: README.md"):
        verify_extracted_tree(
            tmp_path,
            [
                {
                    "path": "README.md",
                    "type": "blob",
                    "mode": "100644",
                    "size": 7,
                    "sha": "0" * 40,
                }
            ],
        )


def test_verify_extracted_tree_rejects_unexpected_file(tmp_path):
    (tmp_path / "extra.txt").write_bytes(b"extra")
    with pytest.raises(SyncError, match="归档文件清单不匹配: extra.txt"):
        verify_extracted_tree(tmp_path, [])


@pytest.mark.parametrize(
    ("row", "message"),
    [
        (
            {"path": "module", "type": "commit", "mode": "160000", "sha": "2" * 40},
            "不支持的 Git tree 项: module",
        ),
        (
            {"path": "link", "type": "blob", "mode": "120000", "size": 4, "sha": "2" * 40},
            "禁止符号链接: link",
        ),
    ],
)
def test_verify_extracted_tree_rejects_submodule_and_symlink(tmp_path, row, message):
    with pytest.raises(SyncError, match=message):
        verify_extracted_tree(tmp_path, [row])


def test_verify_local_manifest_detects_drift(tmp_path):
    (tmp_path / "owned.txt").write_text("current\n", encoding="utf-8")
    spec = upstream_spec(files=[entry("owned.txt", b"expected\n")])
    with pytest.raises(SyncError, match="本地上游文件已修改: owned.txt"):
        verify_local_manifest(tmp_path, spec)


def test_verify_local_manifest_detects_missing_file(tmp_path):
    spec = upstream_spec(files=[entry("missing.txt", b"expected\n")])
    with pytest.raises(SyncError, match="缺少本地上游文件: missing.txt"):
        verify_local_manifest(tmp_path, spec)


def test_replace_manifest_rejects_unexpected_local_file(tmp_path):
    target = tmp_path / "vendor"
    target.mkdir()
    (target / "owned.txt").write_bytes(b"expected\n")
    (target / "extra.txt").write_bytes(b"extra\n")
    spec = upstream_spec(
        files=[entry("owned.txt", b"expected\n")], target="vendor", mode="replace"
    )
    with pytest.raises(SyncError, match="本地目标包含清单外文件: extra.txt"):
        verify_local_manifest(tmp_path, spec)


class FakeTreeClient:
    def __init__(self, trees: dict[tuple[str, str], RemoteTree]):
        self.trees = trees

    def commit_tree(self, repository: str, commit: str) -> RemoteTree:
        return self.trees[(repository, commit)]


def tree_for(path: str, body: bytes) -> dict:
    return {
        "path": path,
        "type": "blob",
        "mode": "100644",
        "size": len(body),
        "sha": git_blob_sha(body),
    }


def test_bootstrap_lock_fills_manifests_without_owning_integration_files(tmp_path):
    (tmp_path / "owned.txt").write_bytes(b"receiver\n")
    (tmp_path / "manager").mkdir()
    (tmp_path / "manager" / "local.py").write_bytes(b"integration\n")
    vendor = tmp_path / "vendor" / "tool"
    vendor.mkdir(parents=True)
    (vendor / "tool.txt").write_bytes(b"converter\n")
    lock_path = tmp_path / "upstreams.lock.json"
    receiver = lock_row()
    converter = lock_row(key="converter", target="vendor/tool")
    converter["repository"] = "owner/converter"
    converter["mode"] = "replace"
    write_lock(lock_path, upstreams=[receiver, converter])
    client = FakeTreeClient(
        {
            ("owner/project", "1" * 40): RemoteTree(
                rows=(tree_for("owned.txt", b"receiver\n"),), truncated=False
            ),
            ("owner/converter", "1" * 40): RemoteTree(
                rows=(tree_for("tool.txt", b"converter\n"),), truncated=False
            ),
        }
    )

    lock = bootstrap_lock(tmp_path, lock_path, client)

    by_key = {item.key: item for item in lock.upstreams}
    assert [item.path for item in by_key["receiver"].files] == ["owned.txt"]
    assert [item.path for item in by_key["converter"].files] == ["tool.txt"]
    assert (tmp_path / "manager" / "local.py").read_bytes() == b"integration\n"
    assert load_lock(lock_path) == lock


def test_bootstrap_lock_rejects_truncated_tree_without_changing_lock(tmp_path):
    (tmp_path / "owned.txt").write_bytes(b"receiver\n")
    lock_path = tmp_path / "upstreams.lock.json"
    write_lock(lock_path, upstreams=[lock_row()])
    before = lock_path.read_bytes()
    client = FakeTreeClient(
        {
            ("owner/project", "1" * 40): RemoteTree(
                rows=(tree_for("owned.txt", b"receiver\n"),), truncated=True
            )
        }
    )

    with pytest.raises(SyncError, match="Git tree 响应被截断"):
        bootstrap_lock(tmp_path, lock_path, client)

    assert lock_path.read_bytes() == before


def snapshot_tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class FakeLatestClient:
    def __init__(self, results: dict[str, RemoteCommit | Exception]):
        self.results = results

    def latest_commit(self, repository: str, branch: str) -> RemoteCommit:
        result = self.results[repository]
        if isinstance(result, Exception):
            raise result
        return result


def test_check_updates_reports_literal_status_without_writing(tmp_path):
    (tmp_path / "sentinel.txt").write_bytes(b"unchanged\n")
    before = snapshot_tree(tmp_path)
    lock = LockFile(
        schema_version=1,
        upstreams=(upstream_spec(files=[]),),
        protected_prefixes=("manager/",),
    )
    client = FakeLatestClient(
        {
            "owner/project": RemoteCommit(
                sha="2" * 40,
                title="upstream change",
                committed_at="2026-08-08T00:00:00Z",
            )
        }
    )

    statuses = check_updates(lock, client)

    assert statuses == (
        UpdateStatus(
            key="receiver",
            repository="owner/project",
            current_sha="1" * 40,
            latest_sha="2" * 40,
            update_available=True,
            title="upstream change",
            committed_at="2026-08-08T00:00:00Z",
        ),
    )
    assert statuses[0].as_dict() == {
        "key": "receiver",
        "repository": "owner/project",
        "current_sha": "1" * 40,
        "latest_sha": "2" * 40,
        "update_available": True,
        "title": "upstream change",
        "committed_at": "2026-08-08T00:00:00Z",
    }
    assert snapshot_tree(tmp_path) == before


def test_check_updates_keeps_other_repository_when_one_fails():
    receiver = upstream_spec(files=[])
    converter = UpstreamSpec(
        key="converter",
        repository="owner/converter",
        branch="main",
        commit="3" * 40,
        target="vendor/tool",
        mode="replace",
        files=(),
    )
    lock = LockFile(1, (receiver, converter), ("manager/",))
    client = FakeLatestClient(
        {
            "owner/project": SyncError("GitHub 请求失败"),
            "owner/converter": RemoteCommit(
                sha="3" * 40,
                title="current",
                committed_at="2026-08-08T00:00:00Z",
            ),
        }
    )

    statuses = check_updates(lock, client)

    assert statuses[0].update_available is None
    assert statuses[0].error == "GitHub 请求失败"
    assert statuses[1].update_available is False
    assert statuses[1].error is None


class FakeResponse:
    def __init__(self, payload: bytes, *, status: int = 200):
        self.payload = payload
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, amount: int) -> bytes:
        return self.payload[:amount]


def test_github_client_parses_commit_and_tree_responses():
    responses = iter(
        [
            FakeResponse(
                json.dumps(
                    {
                        "sha": "2" * 40,
                        "commit": {
                            "message": "Title line\n\nDetails",
                            "committer": {"date": "2026-08-08T00:00:00Z"},
                        },
                    }
                ).encode()
            ),
            FakeResponse(
                json.dumps(
                    {"truncated": False, "tree": [tree_for("a.txt", b"a\n")]}
                ).encode()
            ),
        ]
    )
    seen = []

    def opener(request, timeout):
        seen.append((request.full_url, request.headers, timeout))
        return next(responses)

    client = GitHubClient(opener=opener)

    commit = client.latest_commit("owner/project", "main")
    tree = client.commit_tree("owner/project", "2" * 40)

    assert commit == RemoteCommit("2" * 40, "Title line", "2026-08-08T00:00:00Z")
    assert tree.truncated is False
    assert tree.rows == (tree_for("a.txt", b"a\n"),)
    assert seen[0][2] == 10
    assert seen[0][1]["User-agent"] == "Codex-Unified-Local-Manager"


@pytest.mark.parametrize(
    ("opener", "message"),
    [
        (lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError()), "GitHub 请求失败"),
        (lambda *_args, **_kwargs: FakeResponse(b"not-json"), "GitHub 响应不是有效 JSON"),
        (
            lambda *_args, **_kwargs: FakeResponse(b"x" * (2 * 1024 * 1024 + 1)),
            "GitHub 响应超过 2 MiB",
        ),
    ],
)
def test_github_client_rejects_timeout_malformed_and_oversized(opener, message):
    with pytest.raises(SyncError, match=message):
        GitHubClient(opener=opener).latest_commit("owner/project", "main")


def test_github_client_reports_rate_limit_without_response_body():
    def opener(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "https://api.github.com/example", 403, "rate limited", {}, None
        )

    with pytest.raises(SyncError, match="GitHub API 请求失败: HTTP 403"):
        GitHubClient(opener=opener).latest_commit("owner/project", "main")


def test_check_cli_emits_json_and_returns_zero_when_update_exists(tmp_path, capsys):
    lock_path = tmp_path / "lock.json"
    write_lock(lock_path, upstreams=[lock_row()])
    client = FakeLatestClient(
        {
            "owner/project": RemoteCommit(
                sha="2" * 40,
                title="new release",
                committed_at="2026-08-08T00:00:00Z",
            )
        }
    )

    exit_code = main(
        ["check", "--workspace", str(tmp_path), "--lock", str(lock_path), "--json"],
        client=client,
    )

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["projects"][0]["update_available"] is True
    assert output["projects"][0]["latest_sha"] == "2" * 40


def test_check_cli_human_returns_two_only_when_every_project_failed(tmp_path, capsys):
    lock_path = tmp_path / "lock.json"
    write_lock(lock_path, upstreams=[lock_row()])
    client = FakeLatestClient({"owner/project": SyncError("offline")})

    exit_code = main(
        ["check", "--workspace", str(tmp_path), "--lock", str(lock_path), "--human"],
        client=client,
    )

    assert exit_code == 2
    output = capsys.readouterr().out
    assert "owner/project" in output
    assert "无法检查" in output
    assert "offline" in output


def test_bootstrap_lock_rejects_new_protected_collision_without_writing(tmp_path):
    manager = tmp_path / "manager"
    manager.mkdir()
    (manager / "hack.py").write_bytes(b"remote\n")
    lock_path = tmp_path / "upstreams.lock.json"
    write_lock(lock_path, upstreams=[lock_row()])
    before = lock_path.read_bytes()
    client = FakeTreeClient(
        {
            ("owner/project", "1" * 40): RemoteTree(
                rows=(tree_for("manager/hack.py", b"remote\n"),), truncated=False
            )
        }
    )

    with pytest.raises(SyncError, match="受保护路径冲突: manager/hack.py"):
        bootstrap_lock(tmp_path, lock_path, client)

    assert lock_path.read_bytes() == before
