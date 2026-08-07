from __future__ import annotations

import re
import threading
import time
from pathlib import Path

from flask import Blueprint, Response, jsonify, render_template, request, send_from_directory

from src.settings import Settings

from .credential_import import CredentialImportError, import_codex_documents
from .upstream_sync import GitHubClient, SyncError, check_updates, load_lock


MAX_MANAGER_IMPORT_BYTES = 5 * 1024 * 1024
BRIDGE_TAG = '<script src="/manager-static/converter_bridge.js"></script>'


def create_manager_blueprint(settings: Settings, *, github_client=None) -> Blueprint:
    blueprint = Blueprint(
        "manager",
        __name__,
        template_folder="templates",
        static_folder="static",
        static_url_path="/manager-static",
    )
    project_root = settings.project_root.resolve()
    converter_docs = (
        project_root / "vendor" / "GPTSession2CPAandSub2API" / "docs"
    ).resolve()
    lock_path = project_root / "manager" / "upstreams.lock.json"
    credential_dir = settings.data_dir / "codex_accounts"
    update_client = github_client or GitHubClient()
    update_cache_lock = threading.Lock()
    update_cache: dict[str, object] = {
        "key": None,
        "expires_at": 0.0,
        "projects": (),
    }

    @blueprint.get("/manager")
    def manager_home():
        return render_template("manager.html")

    @blueprint.get("/api/manager/status")
    def manager_status():
        try:
            lock = load_lock(lock_path)
        except SyncError:
            return jsonify({"ok": False, "error": "上游锁文件不可用"}), 500
        cache_key = tuple((spec.key, spec.commit) for spec in lock.upstreams)
        now = time.monotonic()
        with update_cache_lock:
            if update_cache["key"] != cache_key or now >= float(update_cache["expires_at"]):
                update_cache["projects"] = check_updates(lock, update_client)
                update_cache["key"] = cache_key
                update_cache["expires_at"] = now + 600
            projects = [status.as_dict() for status in update_cache["projects"]]
        credential_count = 0
        if credential_dir.is_dir():
            credential_count = sum(1 for path in credential_dir.glob("*.json") if path.is_file())
        return jsonify(
            {
                "ok": True,
                "service": "codex-unified-local-manager",
                "credential_count": credential_count,
                "projects": projects,
            }
        )

    @blueprint.get("/tools/session-converter/")
    def converter_index():
        index_path = converter_docs / "index.html"
        try:
            source = index_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return Response("本地转换器快照不可用", status=500, content_type="text/plain; charset=utf-8")
        markers = list(re.finditer(r"</body\s*>", source, flags=re.IGNORECASE))
        if len(markers) != 1:
            return Response("本地转换器页面缺少唯一注入点", status=500, content_type="text/plain; charset=utf-8")
        marker = markers[0]
        body = source[: marker.start()] + BRIDGE_TAG + source[marker.start() :]
        return Response(body, content_type="text/html; charset=utf-8")

    @blueprint.get("/tools/session-converter/<path:asset_path>")
    def converter_asset(asset_path: str):
        return send_from_directory(converter_docs, asset_path)

    @blueprint.post("/api/manager/credentials/import")
    def import_credentials():
        request.max_content_length = MAX_MANAGER_IMPORT_BYTES
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or payload.get("confirmed") is not True:
            return jsonify({"ok": False, "error": "保存前必须明确确认"}), 400
        try:
            result = import_codex_documents(payload.get("documents"), credential_dir)
        except CredentialImportError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except OSError:
            return jsonify({"ok": False, "error": "凭证写入失败，请检查本地目录权限"}), 500
        return jsonify({"ok": True, **result.as_dict()})

    return blueprint
