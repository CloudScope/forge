"""
Filesystem backends.

Deliberately byte-compatible with the layout Forge has always written under
`FORGE_VAR_ROOT`, so an existing `var/` directory keeps working and the whole
test suite runs with no AWS dependency.
"""

from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path
from typing import Any

from .base import CHECKPOINTS, COLLECTIONS, EVENTS, MEMORY, WORKFLOWS

# Where each document collection lives, relative to the state root.
_COLLECTION_DIRS = {
    WORKFLOWS: ("workflows",),
    CHECKPOINTS: ("checkpoints",),
    MEMORY: ("memory",),
    EVENTS: ("audit", "traces"),
}


class LocalObjectStore:
    """Objects as files under a root directory."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        root = self.root.resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"Key escapes storage root: {key!r}")
        return path

    def put_bytes(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def put_text(self, key: str, text: str) -> None:
        self.put_bytes(key, text.encode("utf-8"))

    def get_bytes(self, key: str) -> bytes | None:
        path = self._path(key)
        return path.read_bytes() if path.is_file() else None

    def get_text(self, key: str) -> str | None:
        raw = self.get_bytes(key)
        return raw.decode("utf-8") if raw is not None else None

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def list_keys(self, prefix: str) -> list[str]:
        base = self._path(prefix)
        if base.is_file():
            return [prefix]
        if not base.is_dir():
            return []
        root = self.root.resolve()
        return sorted(
            str(p.resolve().relative_to(root)).replace("\\", "/")
            for p in base.rglob("*")
            if p.is_file()
        )

    def delete(self, key: str) -> bool:
        path = self._path(key)
        if path.is_file():
            path.unlink()
            return True
        return False

    def delete_prefix(self, prefix: str) -> int:
        base = self._path(prefix)
        if base.is_dir():
            count = sum(1 for p in base.rglob("*") if p.is_file())
            shutil.rmtree(base, ignore_errors=True)
            return count
        return 1 if self.delete(prefix) else 0


class LocalDocumentStore:
    """JSON documents as files; event streams as append-only JSONL."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._lock = threading.Lock()
        for parts in _COLLECTION_DIRS.values():
            self.root.joinpath(*parts).mkdir(parents=True, exist_ok=True)
        # Memory is namespaced by scope ("agent#x" / "workflow#y") in one directory.
        self.index_path = self.root / "audit" / "index.jsonl"

    def _dir(self, collection: str) -> Path:
        try:
            parts = _COLLECTION_DIRS[collection]
        except KeyError:
            raise ValueError(f"Unknown collection: {collection!r}") from None
        path = self.root.joinpath(*parts)
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _filename(key: str) -> str:
        # Scoped memory keys ("agent#architecture") become nested paths.
        return key.replace("#", "/")

    def _path(self, collection: str, key: str) -> Path:
        path = (self._dir(collection) / f"{self._filename(key)}.json").resolve()
        base = self._dir(collection).resolve()
        if not path.is_relative_to(base):
            raise ValueError(f"Key escapes collection root: {key!r}")
        return path

    def put(self, collection: str, key: str, doc: dict[str, Any]) -> None:
        path = self._path(collection, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")

    def get(self, collection: str, key: str) -> dict[str, Any] | None:
        path = self._path(collection, key)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def list_keys(self, collection: str) -> list[str]:
        base = self._dir(collection)
        keys = []
        for path in base.rglob("*.json"):
            rel = path.relative_to(base).with_suffix("")
            keys.append(str(rel).replace("\\", "/").replace("/", "#"))
        return sorted(keys)

    def list_docs(
        self, collection: str, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        base = self._dir(collection)
        paths = sorted(
            (p for p in base.rglob("*.json")),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if limit is not None:
            paths = paths[:limit]
        docs = []
        for path in paths:
            try:
                docs.append(json.loads(path.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                continue
        return docs

    def delete(self, collection: str, key: str) -> bool:
        path = self._path(collection, key)
        if path.is_file():
            path.unlink()
            return True
        return False

    def delete_all(self, collection: str) -> int:
        base = self._dir(collection)
        paths = list(base.rglob("*.json"))
        for path in paths:
            path.unlink(missing_ok=True)
        return len(paths)

    # ── Event streams ─────────────────────────────────────────────────────────

    def _trace_path(self, key: str) -> Path:
        return self._dir(EVENTS) / f"{key}.jsonl"

    def append_event(self, key: str, seq: int, event: dict[str, Any]) -> None:
        line = json.dumps({**event, "seq": seq}, default=str)
        with self._lock:
            with self._trace_path(key).open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            self.index_path.parent.mkdir(parents=True, exist_ok=True)
            with self.index_path.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "ts": event.get("ts"),
                            "workflow_id": key,
                            "type": event.get("type"),
                            "task_id": event.get("task_id"),
                        },
                        default=str,
                    )
                    + "\n"
                )

    def read_events(
        self, key: str, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        path = self._trace_path(key)
        if not path.is_file():
            return []
        events = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events[-limit:] if limit else events

    def delete_events(self, key: str) -> bool:
        path = self._trace_path(key)
        if path.is_file():
            path.unlink()
            return True
        return False

    def clear_index(self) -> None:
        self.index_path.unlink(missing_ok=True)


__all__ = ["LocalObjectStore", "LocalDocumentStore", "COLLECTIONS"]
