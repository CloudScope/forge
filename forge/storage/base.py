"""
Storage contracts.

Forge persists four kinds of thing:

* **documents** — workflows, checkpoints, agent memory: small, keyed, read-modify-write
* **event streams** — the audit trace: append-only, read in order
* **objects** — artifacts, generated workspaces, uploads, deliverables: opaque blobs
* **nothing else** — there is no relational access pattern anywhere in the engine

That shape is why the AWS backend is DynamoDB + S3 and needs no database, no VPC
and therefore no NAT gateway. The local backend writes the same layout the engine
has always used under `FORGE_VAR_ROOT`, so development and tests are unchanged.

Keys are POSIX-style relative paths (`artifacts/wf_123/hld.v1.json`). They map to
files under the var root locally and to S3 object keys in AWS — no backend may
interpret them further.
"""

from __future__ import annotations

from typing import Any, Iterable, Protocol, runtime_checkable

# Document collections. Local backends map these to directories; DynamoDB maps
# each to a table named f"{prefix}-{collection}".
WORKFLOWS = "workflows"
CHECKPOINTS = "checkpoints"
EVENTS = "events"
MEMORY = "memory"

COLLECTIONS = (WORKFLOWS, CHECKPOINTS, EVENTS, MEMORY)


@runtime_checkable
class ObjectStore(Protocol):
    """Blob storage for artifacts, workspaces, uploads and deliverables."""

    def put_bytes(self, key: str, data: bytes) -> None: ...

    def put_text(self, key: str, text: str) -> None: ...

    def get_bytes(self, key: str) -> bytes | None: ...

    def get_text(self, key: str) -> str | None: ...

    def exists(self, key: str) -> bool: ...

    def list_keys(self, prefix: str) -> list[str]: ...

    def delete(self, key: str) -> bool: ...

    def delete_prefix(self, prefix: str) -> int: ...


@runtime_checkable
class DocumentStore(Protocol):
    """Keyed JSON documents plus append-only per-key event streams."""

    def put(self, collection: str, key: str, doc: dict[str, Any]) -> None: ...

    def get(self, collection: str, key: str) -> dict[str, Any] | None: ...

    def list_keys(self, collection: str) -> list[str]: ...

    def list_docs(self, collection: str, *, limit: int | None = None) -> list[dict[str, Any]]: ...

    def delete(self, collection: str, key: str) -> bool: ...

    def delete_all(self, collection: str) -> int: ...

    # Event streams (audit trace). `seq` orders events within a stream.
    def append_event(self, key: str, seq: int, event: dict[str, Any]) -> None: ...

    def read_events(self, key: str, *, limit: int | None = None) -> list[dict[str, Any]]: ...

    def delete_events(self, key: str) -> bool: ...


def join_key(*parts: str) -> str:
    """Build a storage key from path segments, rejecting traversal."""
    cleaned: list[str] = []
    for part in parts:
        text = str(part).strip("/")
        if not text:
            continue
        if ".." in text.split("/"):
            raise ValueError(f"Illegal storage key segment: {part!r}")
        cleaned.append(text)
    return "/".join(cleaned)


def latest_version(keys: Iterable[str], artifact: str) -> str | None:
    """Pick the highest `name.vN.json` from a listing (artifacts are versioned)."""
    best: tuple[int, str] | None = None
    suffix = ".json"
    for key in keys:
        name = key.rsplit("/", 1)[-1]
        if not name.startswith(f"{artifact}.v") or not name.endswith(suffix):
            continue
        raw = name[len(artifact) + 2 : -len(suffix)]
        try:
            version = int(raw)
        except ValueError:
            continue
        if best is None or version > best[0]:
            best = (version, key)
    return best[1] if best else None
