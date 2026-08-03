"""Storage contracts. The local backend is the reference implementation."""

from __future__ import annotations

import pytest

from forge.storage import (
    CHECKPOINTS,
    MEMORY,
    WORKFLOWS,
    backend_name,
    join_key,
    latest_version,
    using_aws,
)
from forge.storage.local import LocalDocumentStore, LocalObjectStore


@pytest.fixture
def objects(tmp_path):
    return LocalObjectStore(tmp_path / "objects")


@pytest.fixture
def docs(tmp_path):
    return LocalDocumentStore(tmp_path / "state")


class TestKeys:
    def test_segments_are_joined_posix_style(self):
        assert join_key("artifacts", "wf_1", "hld.v1.json") == "artifacts/wf_1/hld.v1.json"

    def test_empty_segments_are_dropped(self):
        assert join_key("a", "", "b") == "a/b"

    def test_traversal_is_rejected(self):
        with pytest.raises(ValueError):
            join_key("artifacts", "../../etc/passwd")

    def test_latest_version_picks_the_highest(self):
        keys = ["a/hld.v1.json", "a/hld.v10.json", "a/hld.v2.json"]
        assert latest_version(keys, "hld") == "a/hld.v10.json"

    def test_latest_version_ignores_other_artifacts(self):
        assert latest_version(["a/lld.v3.json"], "hld") is None


class TestObjectStore:
    def test_round_trips_text(self, objects):
        objects.put_text("artifacts/wf_1/hld.v1.json", '{"a": 1}')

        assert objects.get_text("artifacts/wf_1/hld.v1.json") == '{"a": 1}'

    def test_round_trips_bytes(self, objects):
        objects.put_bytes("uploads/spec.pdf", b"\x25PDF-1.7")

        assert objects.get_bytes("uploads/spec.pdf") == b"\x25PDF-1.7"

    def test_missing_key_reads_as_none(self, objects):
        assert objects.get_text("nope") is None
        assert objects.exists("nope") is False

    def test_lists_keys_under_a_prefix(self, objects):
        objects.put_text("artifacts/wf_1/a.json", "{}")
        objects.put_text("artifacts/wf_1/nested/b.json", "{}")
        objects.put_text("artifacts/wf_2/c.json", "{}")

        assert objects.list_keys("artifacts/wf_1") == [
            "artifacts/wf_1/a.json",
            "artifacts/wf_1/nested/b.json",
        ]

    def test_deletes_a_single_key(self, objects):
        objects.put_text("a/b.json", "{}")

        assert objects.delete("a/b.json") is True
        assert objects.delete("a/b.json") is False

    def test_deletes_a_whole_prefix(self, objects):
        for name in ("a", "b", "c"):
            objects.put_text(f"workspaces/wf_1/{name}.py", "x")

        assert objects.delete_prefix("workspaces/wf_1") == 3
        assert objects.list_keys("workspaces/wf_1") == []

    def test_keys_cannot_escape_the_root(self, objects):
        with pytest.raises(ValueError):
            objects.put_text("../escape.json", "{}")


class TestDocumentStore:
    def test_round_trips_a_document(self, docs):
        docs.put(WORKFLOWS, "wf_1", {"id": "wf_1", "status": "RUNNING"})

        assert docs.get(WORKFLOWS, "wf_1")["status"] == "RUNNING"

    def test_missing_document_reads_as_none(self, docs):
        assert docs.get(WORKFLOWS, "absent") is None

    def test_overwrites_in_place(self, docs):
        docs.put(WORKFLOWS, "wf_1", {"n": 1})
        docs.put(WORKFLOWS, "wf_1", {"n": 2})

        assert docs.get(WORKFLOWS, "wf_1") == {"n": 2}

    def test_lists_keys_and_documents(self, docs):
        docs.put(CHECKPOINTS, "wf_1_seq0001", {"seq": 1})
        docs.put(CHECKPOINTS, "wf_1_seq0002", {"seq": 2})

        assert docs.list_keys(CHECKPOINTS) == ["wf_1_seq0001", "wf_1_seq0002"]
        assert len(docs.list_docs(CHECKPOINTS)) == 2

    def test_scoped_keys_do_not_collide(self, docs):
        docs.put(MEMORY, "agent#architecture", {"scope": "agent"})
        docs.put(MEMORY, "workflow#wf_1", {"scope": "workflow"})

        assert docs.get(MEMORY, "agent#architecture")["scope"] == "agent"
        assert docs.get(MEMORY, "workflow#wf_1")["scope"] == "workflow"
        assert set(docs.list_keys(MEMORY)) == {"agent#architecture", "workflow#wf_1"}

    def test_delete_reports_whether_it_removed_anything(self, docs):
        docs.put(WORKFLOWS, "wf_1", {})

        assert docs.delete(WORKFLOWS, "wf_1") is True
        assert docs.delete(WORKFLOWS, "wf_1") is False

    def test_delete_all_empties_a_collection(self, docs):
        for i in range(3):
            docs.put(WORKFLOWS, f"wf_{i}", {})

        assert docs.delete_all(WORKFLOWS) == 3
        assert docs.list_keys(WORKFLOWS) == []

    def test_unknown_collection_is_rejected(self, docs):
        with pytest.raises(ValueError):
            docs.put("not-a-collection", "k", {})


class TestEventStreams:
    def test_events_read_back_in_order(self, docs):
        for seq in (1, 2, 3):
            docs.append_event("wf_1", seq, {"type": f"E{seq}"})

        events = docs.read_events("wf_1")

        assert [e["type"] for e in events] == ["E1", "E2", "E3"]
        assert [e["seq"] for e in events] == [1, 2, 3]

    def test_limit_returns_the_newest(self, docs):
        for seq in range(1, 6):
            docs.append_event("wf_1", seq, {"type": f"E{seq}"})

        assert [e["type"] for e in docs.read_events("wf_1", limit=2)] == ["E4", "E5"]

    def test_streams_are_isolated_per_workflow(self, docs):
        docs.append_event("wf_1", 1, {"type": "A"})
        docs.append_event("wf_2", 1, {"type": "B"})

        assert [e["type"] for e in docs.read_events("wf_1")] == ["A"]
        assert [e["type"] for e in docs.read_events("wf_2")] == ["B"]

    def test_missing_stream_is_empty(self, docs):
        assert docs.read_events("absent") == []

    def test_delete_reports_whether_a_stream_existed(self, docs):
        docs.append_event("wf_1", 1, {"type": "A"})

        assert docs.delete_events("wf_1") is True
        assert docs.delete_events("wf_1") is False


class TestFactory:
    def test_local_is_the_default(self):
        assert backend_name() == "local"
        assert using_aws() is False

    def test_aws_backend_requires_configuration(self, monkeypatch):
        import forge.storage as storage

        monkeypatch.setenv("FORGE_STORAGE", "aws")
        monkeypatch.delenv("FORGE_S3_BUCKET", raising=False)
        storage.reset()

        with pytest.raises(RuntimeError, match="FORGE_S3_BUCKET"):
            storage.object_store()

        storage.reset()

    def test_health_reports_the_active_backend(self):
        from forge.storage import health

        assert health()["backend"] == "local"
