from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_EXPORT_ID = re.compile(r"^[0-9a-f]{24}$")
_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ExportHistoryStore:
    """Persist downloadable export results and a small local history index."""

    def __init__(self, data_dir: Path, *, max_items: int = 100):
        self.data_dir = Path(data_dir)
        self.export_dir = self.data_dir / "exports"
        self.index_path = self.data_dir / "export-history.json"
        self.max_items = max(10, int(max_items))
        self._lock = threading.RLock()

    def _read(self) -> list[dict[str, Any]]:
        if not self.index_path.is_file():
            return []
        try:
            payload = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        rows = payload.get("exports") if isinstance(payload, dict) else None
        return [dict(row) for row in rows or [] if isinstance(row, dict)]

    def _write(self, rows: list[dict[str, Any]]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.data_dir / f".{self.index_path.name}.{secrets.token_hex(8)}.tmp"
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump({"version": 1, "exports": rows}, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.index_path)

    @staticmethod
    def _filename(value: str) -> str:
        cleaned = _SAFE_FILENAME.sub("-", Path(str(value or "export.zip")).name).strip(".-")
        return (cleaned or "export.zip")[:160]

    def save(
        self,
        content: bytes,
        *,
        filename: str,
        kind: str,
        formats: list[str],
        account_count: int,
    ) -> dict[str, Any]:
        payload = bytes(content)
        if not payload:
            raise ValueError("导出文件内容为空")
        export_id = secrets.token_hex(12)
        download_name = self._filename(filename)
        stored_name = f"{export_id}-{download_name}"
        now = _now()
        row = {
            "id": export_id,
            "filename": download_name,
            "stored_name": stored_name,
            "kind": str(kind or "accounts")[:40],
            "formats": list(dict.fromkeys(str(value)[:40] for value in formats if str(value))),
            "account_count": max(0, int(account_count)),
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "created_at": now,
        }
        with self._lock:
            self.export_dir.mkdir(parents=True, exist_ok=True)
            target = self.export_dir / stored_name
            temporary = self.export_dir / f".{stored_name}.{secrets.token_hex(8)}.tmp"
            with temporary.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            rows = [row, *self._read()]
            discarded = rows[self.max_items :]
            rows = rows[: self.max_items]
            self._write(rows)
            for old in discarded:
                old_path = self.export_dir / str(old.get("stored_name") or "")
                try:
                    if old_path.is_file() and old_path.parent == self.export_dir:
                        old_path.unlink()
                except OSError:
                    pass
        return self._public(row)

    @staticmethod
    def _public(row: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in row.items() if key != "stored_name"}

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = []
            for row in self._read():
                path = self.export_dir / str(row.get("stored_name") or "")
                if path.is_file():
                    rows.append(self._public(row))
            return rows

    def file(self, export_id: str) -> tuple[Path, dict[str, Any]] | None:
        normalized = str(export_id or "").strip().lower()
        if not _EXPORT_ID.fullmatch(normalized):
            return None
        with self._lock:
            row = next((item for item in self._read() if item.get("id") == normalized), None)
            if row is None:
                return None
            path = self.export_dir / str(row.get("stored_name") or "")
            if not path.is_file() or path.parent != self.export_dir:
                return None
            return path, self._public(row)


__all__ = ["ExportHistoryStore"]
