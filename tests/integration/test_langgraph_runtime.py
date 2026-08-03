"""LangGraph orchestration: interrupts at gates, durable checkpoints, resume."""

from __future__ import annotations

import pytest

from forge.graph.checkpointing import build_checkpointer, checkpoint_db_path
from forge.graph.runtime import LangGraphRuntime, langgraph_available
from forge.models import NodeType, TaskStatus, WorkflowStatus
from tests.conftest import make_node

pytestmark = pytest.mark.skipif(
    not langgraph_available(), reason="langgraph is not installed"
)


class TestCheckpointer:
    def test_sqlite_is_the_default_and_is_durable(self):
        saver, info = build_checkpointer()

        assert info["type"] == "sqlite"
        assert info["durable"] is True
        assert checkpoint_db_path().exists()

    def test_memory_can_be_selected_explicitly(self, monkeypatch):
        monkeypatch.setenv("FORGE_CHECKPOINTER", "memory")

        _, info = build_checkpointer()

        assert info["type"] == "memory"
        assert info["durable"] is False

    def test_checkpoint_survives_a_new_saver_instance(self):
        """A restart builds a fresh saver against the same file."""
        saver_a, _ = build_checkpointer()
        cfg = {"configurable": {"thread_id": "durability-probe", "checkpoint_ns": ""}}
        saver_a.put(
            cfg,
            {"v": 1, "id": "chk-1", "ts": "2026-01-01T00:00:00+00:00", "channel_values": {}},
            {"source": "input", "step": 0},
            {},
        )

        saver_b, _ = build_checkpointer()

        assert saver_b.get(cfg) is not None


@pytest.fixture
def runtime():
    return LangGraphRuntime(auto_approve=True, max_workers=2, cli_demo_mode=True)


class TestGraphExecution:
    def test_a_simple_graph_runs_to_completion(self, runtime, make_workflow):
        wf = make_workflow(
            [
                make_node("a", node_type=NodeType.BARRIER),
                make_node("b", node_type=NodeType.BARRIER, deps=["a"]),
            ]
        )

        result = runtime.start(wf)

        assert result["interrupted"] is False
        assert wf.status == WorkflowStatus.SUCCEEDED
        assert result["runtime"] == "langgraph"

    def test_parallel_peers_all_execute(self, runtime, make_workflow):
        wf = make_workflow(
            [
                make_node("entry", node_type=NodeType.BARRIER),
                make_node("p1", node_type=NodeType.BARRIER, deps=["entry"]),
                make_node("p2", node_type=NodeType.BARRIER, deps=["entry"]),
                make_node("p3", node_type=NodeType.BARRIER, deps=["entry"]),
                make_node("sync", node_type=NodeType.BARRIER, deps=["p1", "p2", "p3"]),
            ]
        )

        runtime.start(wf)

        assert wf.status == WorkflowStatus.SUCCEEDED
        assert all(t.status == TaskStatus.SUCCEEDED for t in wf.tasks.values())


class TestHumanInterrupt:
    def test_hard_gate_interrupts_the_graph(self, runtime, make_workflow):
        wf = make_workflow(
            [
                make_node(
                    "approval.coding", agent="human_approval", node_type=NodeType.APPROVAL
                ),
                make_node("after", node_type=NodeType.BARRIER, deps=["approval.coding"]),
            ]
        )

        result = runtime.start(wf)

        assert result["interrupted"] is True
        assert result["pending_approval"]["task_id"] == "approval.coding"
        assert wf.status == WorkflowStatus.WAITING_APPROVAL
        assert wf.tasks["after"].status == TaskStatus.PENDING

    def test_resume_applies_the_decision_and_continues(self, runtime, make_workflow):
        wf = make_workflow(
            [
                make_node(
                    "approval.coding", agent="human_approval", node_type=NodeType.APPROVAL
                ),
                make_node("after", node_type=NodeType.BARRIER, deps=["approval.coding"]),
            ]
        )
        runtime.start(wf)

        result = runtime.resume_approval(
            wf, decision="approve", rationale="workspace reviewed"
        )

        assert result["interrupted"] is False
        assert wf.status == WorkflowStatus.SUCCEEDED
        assert wf.tasks["after"].status == TaskStatus.SUCCEEDED
        assert wf.facts["coding_approved"] is True

    def test_rejection_fails_the_workflow(self, runtime, make_workflow):
        wf = make_workflow(
            [
                make_node(
                    "approval.coding", agent="human_approval", node_type=NodeType.APPROVAL
                ),
                make_node("after", node_type=NodeType.BARRIER, deps=["approval.coding"]),
            ]
        )
        runtime.start(wf)

        runtime.resume_approval(wf, decision="reject", rationale="codegen unusable")

        assert wf.status == WorkflowStatus.FAILED
        assert wf.tasks["after"].status == TaskStatus.PENDING

    def test_pending_interrupt_is_readable_before_resuming(self, runtime, make_workflow):
        wf = make_workflow(
            [make_node("approval.coding", agent="human_approval", node_type=NodeType.APPROVAL)]
        )
        runtime.start(wf)

        pending = runtime.get_interrupt(wf.id)

        assert pending is not None
        assert pending["task_id"] == "approval.coding"

    def test_a_paused_gate_is_recoverable_after_a_process_restart(self, make_workflow):
        """The whole point of durable checkpointing: a new runtime picks the gate up."""
        first = LangGraphRuntime(auto_approve=True, max_workers=1, cli_demo_mode=True)
        wf = make_workflow(
            [
                make_node(
                    "approval.coding", agent="human_approval", node_type=NodeType.APPROVAL
                ),
                make_node("after", node_type=NodeType.BARRIER, deps=["approval.coding"]),
            ]
        )
        first.start(wf)
        assert wf.status == WorkflowStatus.WAITING_APPROVAL

        # Simulate a restart: brand-new runtime, workflow rehydrated from disk.
        second = LangGraphRuntime(auto_approve=True, max_workers=1, cli_demo_mode=True)
        recovered = second.rehydrate(wf.id)

        assert recovered is not None
        assert recovered.status == WorkflowStatus.WAITING_APPROVAL
        assert second.get_interrupt(wf.id) is not None, "interrupt lost across restart"

        second.resume_approval(recovered, decision="approve", rationale="after restart")

        assert recovered.status == WorkflowStatus.SUCCEEDED


class TestSafeStop:
    def test_safe_stop_prevents_further_scheduling(self, runtime, make_workflow):
        wf = make_workflow(
            [
                make_node("a", node_type=NodeType.BARRIER),
                make_node("b", node_type=NodeType.BARRIER, deps=["a"]),
            ]
        )
        runtime.request_safe_stop()

        runtime.start(wf)

        assert wf.status == WorkflowStatus.PARTIAL
        assert wf.metrics["safe_stops"] == 1
