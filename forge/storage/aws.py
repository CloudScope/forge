"""
AWS backends: S3 for objects, DynamoDB for documents and event streams.

Chosen so the control plane needs no VPC — Lambda reaches S3, DynamoDB and Step
Functions over AWS public endpoints, which removes the NAT gateway that would
otherwise dominate the monthly bill.

Documents are stored as a single JSON string attribute rather than marshalled
DynamoDB types: the engine's artifacts contain arbitrary nested JSON with floats,
and round-tripping those through DynamoDB's Decimal type loses fidelity. Items
larger than the DynamoDB limit spill to S3 transparently.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .base import CHECKPOINTS, EVENTS, MEMORY, WORKFLOWS

logger = logging.getLogger("forge.storage")

# DynamoDB hard limit is 400 KB per item; leave headroom for keys and metadata.
MAX_INLINE_BYTES = 350_000
OVERFLOW_PREFIX = "overflow"

_TABLE_SUFFIX = {
    WORKFLOWS: "workflows",
    CHECKPOINTS: "checkpoints",
    MEMORY: "memory",
    EVENTS: "events",
}


def _boto3():
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise RuntimeError(
            "AWS storage requires boto3. Install with: pip install boto3"
        ) from exc
    return boto3


class S3ObjectStore:
    """Objects in a single bucket, keyed exactly as the local layout."""

    def __init__(self, bucket: str, *, prefix: str = "", client: Any = None):
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._client = client or _boto3().client("s3")

    def _key(self, key: str) -> str:
        key = key.strip("/")
        return f"{self.prefix}/{key}" if self.prefix else key

    def _strip(self, key: str) -> str:
        if self.prefix and key.startswith(f"{self.prefix}/"):
            return key[len(self.prefix) + 1 :]
        return key

    def put_bytes(self, key: str, data: bytes) -> None:
        self._client.put_object(Bucket=self.bucket, Key=self._key(key), Body=data)

    def put_text(self, key: str, text: str) -> None:
        self.put_bytes(key, text.encode("utf-8"))

    def get_bytes(self, key: str) -> bytes | None:
        try:
            resp = self._client.get_object(Bucket=self.bucket, Key=self._key(key))
        except self._client.exceptions.NoSuchKey:
            return None
        except Exception as exc:  # noqa: BLE001 — 404 shapes vary by client config
            if _is_not_found(exc):
                return None
            raise
        return resp["Body"].read()

    def get_text(self, key: str) -> str | None:
        raw = self.get_bytes(key)
        return raw.decode("utf-8") if raw is not None else None

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=self._key(key))
            return True
        except Exception as exc:  # noqa: BLE001
            if _is_not_found(exc):
                return False
            raise

    def list_keys(self, prefix: str) -> list[str]:
        paginator = self._client.get_paginator("list_objects_v2")
        keys: list[str] = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self._key(prefix)):
            for item in page.get("Contents") or []:
                keys.append(self._strip(item["Key"]))
        return sorted(keys)

    def delete(self, key: str) -> bool:
        if not self.exists(key):
            return False
        self._client.delete_object(Bucket=self.bucket, Key=self._key(key))
        return True

    def delete_prefix(self, prefix: str) -> int:
        keys = self.list_keys(prefix)
        for batch_start in range(0, len(keys), 1000):
            batch = keys[batch_start : batch_start + 1000]
            self._client.delete_objects(
                Bucket=self.bucket,
                Delete={"Objects": [{"Key": self._key(k)} for k in batch]},
            )
        return len(keys)


def _is_not_found(exc: Exception) -> bool:
    response = getattr(exc, "response", None) or {}
    code = str((response.get("Error") or {}).get("Code") or "")
    return code in {"404", "NoSuchKey", "NotFound"}


class DynamoDBDocumentStore:
    """
    Documents and event streams in DynamoDB.

    Table layout (all on-demand billing, no capacity planning):

      {prefix}-workflows    pk=key
      {prefix}-checkpoints  pk=key
      {prefix}-memory       pk=key
      {prefix}-events       pk=key, sk=seq   ← audit trace, queried in order
    """

    def __init__(
        self,
        table_prefix: str,
        *,
        overflow: Any = None,
        resource: Any = None,
    ):
        self.table_prefix = table_prefix.rstrip("-")
        self._resource = resource or _boto3().resource("dynamodb")
        self._overflow = overflow
        self._tables: dict[str, Any] = {}

    def _table(self, collection: str):
        try:
            suffix = _TABLE_SUFFIX[collection]
        except KeyError:
            raise ValueError(f"Unknown collection: {collection!r}") from None
        if collection not in self._tables:
            self._tables[collection] = self._resource.Table(
                f"{self.table_prefix}-{suffix}"
            )
        return self._tables[collection]

    # ── Overflow to S3 for oversized documents ────────────────────────────────

    def _encode(self, collection: str, key: str, doc: dict[str, Any]) -> str:
        payload = json.dumps(doc, default=str)
        if len(payload.encode("utf-8")) <= MAX_INLINE_BYTES:
            return payload
        if self._overflow is None:
            raise ValueError(
                f"Document {collection}/{key} exceeds the DynamoDB item limit and "
                "no overflow object store is configured"
            )
        overflow_key = f"{OVERFLOW_PREFIX}/{collection}/{key}.json"
        self._overflow.put_text(overflow_key, payload)
        logger.info("Spilled oversized document %s/%s to %s", collection, key, overflow_key)
        return json.dumps({"__overflow__": overflow_key})

    def _decode(self, payload: str | None) -> dict[str, Any] | None:
        if not payload:
            return None
        try:
            doc = json.loads(payload)
        except json.JSONDecodeError:
            return None
        if isinstance(doc, dict) and "__overflow__" in doc:
            if self._overflow is None:
                return None
            raw = self._overflow.get_text(doc["__overflow__"])
            return json.loads(raw) if raw else None
        return doc

    # ── Documents ─────────────────────────────────────────────────────────────

    def put(self, collection: str, key: str, doc: dict[str, Any]) -> None:
        self._table(collection).put_item(
            Item={"key": key, "doc": self._encode(collection, key, doc)}
        )

    def get(self, collection: str, key: str) -> dict[str, Any] | None:
        item = (self._table(collection).get_item(Key={"key": key}) or {}).get("Item")
        return self._decode(item.get("doc")) if item else None

    def list_keys(self, collection: str) -> list[str]:
        return sorted(item["key"] for item in self._scan(collection, ["key"]))

    def list_docs(
        self, collection: str, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        docs = []
        for item in self._scan(collection, ["key", "doc"]):
            decoded = self._decode(item.get("doc"))
            if decoded is not None:
                docs.append(decoded)
            if limit is not None and len(docs) >= limit:
                break
        return docs

    def _scan(self, collection: str, attributes: list[str]) -> list[dict[str, Any]]:
        table = self._table(collection)
        items: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {"ProjectionExpression": ", ".join(f"#{a}" for a in attributes),
                                  "ExpressionAttributeNames": {f"#{a}": a for a in attributes}}
        while True:
            resp = table.scan(**kwargs)
            items.extend(resp.get("Items") or [])
            token = resp.get("LastEvaluatedKey")
            if not token:
                return items
            kwargs["ExclusiveStartKey"] = token

    def delete(self, collection: str, key: str) -> bool:
        existing = self.get(collection, key)
        self._table(collection).delete_item(Key={"key": key})
        if self._overflow is not None:
            self._overflow.delete(f"{OVERFLOW_PREFIX}/{collection}/{key}.json")
        return existing is not None

    def delete_all(self, collection: str) -> int:
        table = self._table(collection)
        keys = self.list_keys(collection)
        with table.batch_writer() as batch:
            for key in keys:
                batch.delete_item(Key={"key": key})
        return len(keys)

    # ── Event streams ─────────────────────────────────────────────────────────

    def append_event(self, key: str, seq: int, event: dict[str, Any]) -> None:
        self._table(EVENTS).put_item(
            Item={
                "key": key,
                "seq": seq,
                "doc": self._encode(EVENTS, f"{key}#{seq}", event),
            }
        )

    def read_events(
        self, key: str, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        from boto3.dynamodb.conditions import Key as DdbKey

        table = self._table(EVENTS)
        kwargs: dict[str, Any] = {"KeyConditionExpression": DdbKey("key").eq(key)}
        if limit:
            # Newest N: read backwards, then restore chronological order.
            kwargs["ScanIndexForward"] = False
            kwargs["Limit"] = limit
        items: list[dict[str, Any]] = []
        while True:
            resp = table.query(**kwargs)
            items.extend(resp.get("Items") or [])
            token = resp.get("LastEvaluatedKey")
            if not token or (limit and len(items) >= limit):
                break
            kwargs["ExclusiveStartKey"] = token
        if limit:
            items = list(reversed(items[:limit]))
        events = [self._decode(i.get("doc")) for i in items]
        return [e for e in events if e is not None]

    def delete_events(self, key: str) -> bool:
        from boto3.dynamodb.conditions import Key as DdbKey

        table = self._table(EVENTS)
        resp = table.query(
            KeyConditionExpression=DdbKey("key").eq(key),
            ProjectionExpression="#k, #s",
            ExpressionAttributeNames={"#k": "key", "#s": "seq"},
        )
        items = resp.get("Items") or []
        if not items:
            return False
        with table.batch_writer() as batch:
            for item in items:
                batch.delete_item(Key={"key": item["key"], "seq": item["seq"]})
        return True

    def clear_index(self) -> None:  # parity with the local backend
        return None


__all__ = ["S3ObjectStore", "DynamoDBDocumentStore"]
