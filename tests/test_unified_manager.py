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
