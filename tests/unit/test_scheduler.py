"""Dependency-DAG scheduling: readiness, conditions, barriers, fan-out, deadlock."""

from __future__ import annotations

from forge.models import NodeType, RiskTier, TaskStatus, WorkflowStatus
from tests.conftest import make_node


def test_only_dependency_free_nodes_are_ready(engine, make_workflow):
    wf = make_workflow([make_node("a"), make_node("b", deps=["a"])])

    ready = engine.ready_nodes(wf)

    assert [n.id for n in ready] == ["a"]
    assert wf.tasks["b"].status == TaskStatus.PENDING


def test_dependent_becomes_ready_after_dependency_succeeds(engine, make_workflow):
    wf = make_workflow([make_node("a"), make_node("b", deps=["a"])])
    wf.tasks["a"].status = TaskStatus.SUCCEEDED

    assert [n.id for n in engine.ready_nodes(wf)] == ["b"]


def test_skipped_dependency_still_satisfies_dependents(engine, make_workflow):
    """A conditional node that skips must not deadlock everything behind it."""
    wf = make_workflow([make_node("a"), make_node("b", deps=["a"])])
    wf.tasks["a"].status = TaskStatus.SKIPPED

    assert [n.id for n in engine.ready_nodes(wf)] == ["b"]


def test_failed_dependency_blocks_dependents(engine, make_workflow):
    wf = make_workflow([make_node("a"), make_node("b", deps=["a"])])
    wf.tasks["a"].status = TaskStatus.FAILED

    assert engine.ready_nodes(wf) == []


def test_ready_set_orders_by_risk_tier(engine, make_workflow):
    wf = make_workflow(
        [
            make_node("low", risk=RiskTier.LOW),
            make_node("critical", risk=RiskTier.CRITICAL),
            make_node("medium", risk=RiskTier.MEDIUM),
            make_node("high", risk=RiskTier.HIGH),
        ]
    )

    assert [n.id for n in engine.ready_nodes(wf)] == [
        "critical",
        "high",
        "medium",
        "low",
    ]


def test_parallel_frontier_exposes_all_independent_nodes(engine, make_workflow):
    """Four peers behind one barrier must all be ready at once (fan-out width 4)."""
    wf = make_workflow(
        [
            make_node("entry", node_type=NodeType.BARRIER),
            make_node("be", deps=["entry"]),
            make_node("fe", deps=["entry"]),
            make_node("ops", deps=["entry"]),
            make_node("sec", deps=["entry"]),
            make_node("sync", node_type=NodeType.BARRIER, deps=["be", "fe", "ops", "sec"]),
        ]
    )
    wf.tasks["entry"].status = TaskStatus.SUCCEEDED

    ready = {n.id for n in engine.ready_nodes(wf)}

    assert ready == {"be", "fe", "ops", "sec"}
    assert wf.tasks["sync"].status == TaskStatus.PENDING


def test_barrier_waits_for_every_peer(engine, make_workflow):
    wf = make_workflow(
        [
            make_node("be"),
            make_node("fe"),
            make_node("sync", node_type=NodeType.BARRIER, deps=["be", "fe"]),
        ]
    )
    wf.tasks["be"].status = TaskStatus.SUCCEEDED

    assert [n.id for n in engine.ready_nodes(wf)] == ["fe"]

    wf.tasks["fe"].status = TaskStatus.SUCCEEDED
    assert [n.id for n in engine.ready_nodes(wf)] == ["sync"]


class TestConditions:
    def test_false_condition_skips_node(self, engine, make_workflow):
        wf = make_workflow([make_node("opt", condition="false")])

        assert engine.ready_nodes(wf) == []
        assert wf.tasks["opt"].status == TaskStatus.SKIPPED

    def test_fact_condition_gates_on_workflow_state(self, engine, make_workflow):
        wf = make_workflow(
            [make_node("qr", condition="fact:feature_qr==true")], feature_qr=False
        )

        assert engine.ready_nodes(wf) == []
        assert wf.tasks["qr"].status == TaskStatus.SKIPPED

    def test_fact_condition_admits_node_when_true(self, engine, make_workflow):
        wf = make_workflow(
            [make_node("qr", condition="fact:feature_qr==true")], feature_qr=True
        )

        assert [n.id for n in engine.ready_nodes(wf)] == ["qr"]

    def test_condition_is_evaluated_only_after_deps_resolve(self, engine, make_workflow):
        """Upstream nodes set the facts downstream conditions read."""
        wf = make_workflow(
            [
                make_node("clarify"),
                make_node("expand", deps=["clarify"], condition="fact:accepted==true"),
            ]
        )

        engine.ready_nodes(wf)
        assert wf.tasks["expand"].status == TaskStatus.PENDING, "skipped too early"

        wf.tasks["clarify"].status = TaskStatus.SUCCEEDED
        wf.facts["accepted"] = True
        assert [n.id for n in engine.ready_nodes(wf)] == ["expand"]


def test_run_completes_a_linear_barrier_graph(engine, make_workflow):
    wf = make_workflow(
        [
            make_node("a", node_type=NodeType.BARRIER),
            make_node("b", node_type=NodeType.BARRIER, deps=["a"]),
            make_node("c", node_type=NodeType.BARRIER, deps=["b"]),
        ]
    )

    engine.run(wf)

    assert wf.status == WorkflowStatus.SUCCEEDED
    assert all(t.status == TaskStatus.SUCCEEDED for t in wf.tasks.values())
    assert wf.checkpoint_seq > 0


def test_unmet_dependency_is_a_recorded_terminal_failure(engine, make_workflow):
    """A playbook defect must fail the run with an audit trail, not raise."""
    wf = make_workflow([make_node("orphan", deps=["does-not-exist"])])

    engine.run(wf)

    assert wf.status == WorkflowStatus.FAILED
    event = next(e for e in wf.events if e["type"] == "DEADLOCK")
    assert event["payload"]["pending"] == ["orphan"]


def test_cyclic_dependencies_are_detected(engine, make_workflow):
    wf = make_workflow(
        [make_node("a", deps=["b"]), make_node("b", deps=["a"])]
    )

    engine.run(wf)

    assert wf.status == WorkflowStatus.FAILED
    assert any(e["type"] == "DEADLOCK" for e in wf.events)


def test_unknown_agent_fails_the_workflow(engine, make_workflow):
    wf = make_workflow([make_node("bad", agent="no-such-agent")])
    wf.tasks["bad"].max_attempts = 1

    engine.run(wf)

    assert wf.status == WorkflowStatus.FAILED
    assert "Unknown agent" in (wf.tasks["bad"].error or "")


def test_safe_stop_halts_scheduling_and_checkpoints(engine, make_workflow):
    """Operator requests safe-stop mid-run: in-flight work finishes, nothing new starts."""
    wf = make_workflow(
        [
            make_node("a", node_type=NodeType.BARRIER),
            make_node("b", node_type=NodeType.BARRIER, deps=["a"]),
            make_node("c", node_type=NodeType.BARRIER, deps=["b"]),
        ]
    )
    original = engine.ready_nodes

    def stop_after_first_batch(workflow):
        engine.request_safe_stop()
        return original(workflow)

    engine.ready_nodes = stop_after_first_batch

    engine.run(wf)

    assert wf.status == WorkflowStatus.PARTIAL
    assert wf.metrics["safe_stops"] == 1
    assert any(e["type"] == "SAFE_STOP" for e in wf.events)
    assert wf.tasks["c"].status == TaskStatus.PENDING, "scheduled work after safe-stop"


def test_safe_stop_survives_an_approval_pause(make_workflow):
    """A stop requested while paused at a gate must not be cleared by resuming."""
    from forge.engine import OrchestrationEngine

    engine = OrchestrationEngine(auto_approve=False, max_workers=1)
    wf = make_workflow(
        [
            make_node("gate", agent="human_approval", node_type=NodeType.APPROVAL),
            make_node("after", node_type=NodeType.BARRIER, deps=["gate"]),
        ]
    )

    engine.run(wf)
    assert wf.status == WorkflowStatus.WAITING_APPROVAL

    engine.request_safe_stop()
    engine.submit_approval(wf, decision="approve", rationale="ok")

    assert wf.status == WorkflowStatus.PARTIAL
    assert wf.tasks["after"].status == TaskStatus.PENDING
