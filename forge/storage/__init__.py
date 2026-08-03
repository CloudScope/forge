"""
Storage factory.

`FORGE_STORAGE=local` (default) writes under `FORGE_VAR_ROOT`, exactly as before.
`FORGE_STORAGE=aws` uses S3 + DynamoDB and requires:

    FORGE_S3_BUCKET        artifacts, workspaces, uploads, deliverables
    FORGE_DDB_TABLE_PREFIX workflows / checkpoints / memory / events tables

Stores are process-level singletons; `reset()` exists for tests.
"""

from __future__ import annotations

import os
import threading
from typing import Any

from ..core.paths import paths as forge_paths
from .base import (
    CHECKPOINTS,
    COLLECTIONS,
    EVENTS,
    MEMORY,
    WORKFLOWS,
    DocumentStore,
    ObjectStore,
    join_key,
    latest_version,
)
from .local import LocalDocumentStore, LocalObjectStore

_lock = threading.Lock()
_objects: ObjectStore | None = None
_documents: DocumentStore | None = None


def backend_name() -> str:
    return (os.environ.get("FORGE_STORAGE") or "local").strip().lower()


def using_aws() -> bool:
    return backend_name() in {"aws", "s3", "cloud"}


def _build() -> tuple[ObjectStore, DocumentStore]:
    if not using_aws():
        p = forge_paths()
        return LocalObjectStore(p.var), LocalDocumentStore(p.state)

    from .aws import DynamoDBDocumentStore, S3ObjectStore

    bucket = (os.environ.get("FORGE_S3_BUCKET") or "").strip()
    prefix = (os.environ.get("FORGE_DDB_TABLE_PREFIX") or "").strip()
    if not bucket:
        raise RuntimeError("FORGE_STORAGE=aws requires FORGE_S3_BUCKET")
    if not prefix:
        raise RuntimeError("FORGE_STORAGE=aws requires FORGE_DDB_TABLE_PREFIX")

    objects = S3ObjectStore(bucket, prefix=os.environ.get("FORGE_S3_PREFIX", ""))
    documents = DynamoDBDocumentStore(prefix, overflow=objects)
    return objects, documents


def _ensure() -> tuple[ObjectStore, DocumentStore]:
    global _objects, _documents
    if _objects is None or _documents is None:
        with _lock:
            if _objects is None or _documents is None:
                _objects, _documents = _build()
    return _objects, _documents


def object_store() -> ObjectStore:
    return _ensure()[0]


def document_store() -> DocumentStore:
    return _ensure()[1]


def reset() -> None:
    """Drop cached stores so the next call re-reads the environment."""
    global _objects, _documents
    with _lock:
        _objects = None
        _documents = None


def workspace_prefix(workflow_id: str) -> str:
    return join_key("workspaces", workflow_id)


def artifact_prefix(workflow_id: str) -> str:
    return join_key("artifacts", workflow_id)


def sync_workspace_up(workflow_id: str) -> int:
    """
    Mirror a generated workspace from local disk into the object store.

    Codegen writes real files (a FastAPI tree, a React app, Terraform) because
    that is the deliverable. On AWS each execution segment runs on fresh ephemeral
    storage, so the tree is pushed at the end of a segment and pulled at the start
    of the next. Locally the object store *is* the filesystem and this is a no-op.
    """
    if not using_aws():
        return 0
    from ..core.paths import paths

    root = paths().workspaces / workflow_id
    if not root.is_dir():
        return 0
    store = object_store()
    count = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        store.put_bytes(join_key(workspace_prefix(workflow_id), rel), path.read_bytes())
        count += 1
    return count


def sync_workspace_down(workflow_id: str) -> int:
    """Restore a workspace from the object store onto local disk."""
    if not using_aws():
        return 0
    from ..core.paths import paths

    root = paths().workspaces / workflow_id
    prefix = workspace_prefix(workflow_id)
    store = object_store()
    count = 0
    for key in store.list_keys(prefix):
        rel = key[len(prefix) :].lstrip("/")
        if not rel:
            continue
        data = store.get_bytes(key)
        if data is None:
            continue
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        count += 1
    return count


def health() -> dict[str, Any]:
    """Reported by /api/health so the active backend is never a guess."""
    if using_aws():
        return {
            "backend": "aws",
            "bucket": os.environ.get("FORGE_S3_BUCKET"),
            "table_prefix": os.environ.get("FORGE_DDB_TABLE_PREFIX"),
        }
    return {"backend": "local", "var_root": str(forge_paths().var)}


__all__ = [
    "CHECKPOINTS",
    "COLLECTIONS",
    "EVENTS",
    "MEMORY",
    "WORKFLOWS",
    "DocumentStore",
    "ObjectStore",
    "backend_name",
    "document_store",
    "health",
    "join_key",
    "latest_version",
    "object_store",
    "reset",
    "using_aws",
]
