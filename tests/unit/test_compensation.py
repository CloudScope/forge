"""Saga rollback: ordering, durable side-effect removal, and artifact retention."""

from __future__ import annotations

from forge.compensation import (
    is_side_effecting,
    reverse_topo_succeeded,
    run_compensation_saga,
)
from forge.core.paths import paths as forge_paths
from forge.models import NodeType, TaskStatus
from tests.conftest import make_node


def _succeeded(*nodes):
    for n in nodes:
        n.status = TaskStatus.SUCCEEDED
    return list(nodes)


class TestSideEffectClassification:
    def test_codegen_agents_are_side_effecting(self):
        for agent in ("backend", "frontend", "database", "devops", "deployment", "api"):
            assert is_side_effecting(make_node("n", agent=agent)) is True

    def test_analysis_agents_are_not_side_effecting(self):
        for agent in ("requirement", "planner", "architecture", "testing"):
            assert is_side_effecting(make_node("n", agent=agent)) is False

    def test_barriers_and_gates_are_never_compensated(self):
        assert is_side_effecting(make_node("b", node_type=NodeType.BARRIER)) is False
        assert (
            is_side_effecting(make_node("g", agent="backend", node_type=NodeType.APPROVAL))
            is False
        )

    def test_playbook_can_opt_a_node_in_by_description(self):
        node = make_node("custom", agent="planner")
        node.description = "publishes to registry (side_effect)"
        assert is_side_effecting(node) is True


class TestCompensationOrdering:
    def test_dependents_are_compensated_before_dependencies(self, make_workflow):
        """Undo newest-first: frontend before backend before api."""
        wf = make_workflow(
            _succeeded(
                make_node("api", agent="api"),
                make_node("backend", agent="backend", deps=["api"]),
                make_node("frontend", agent="frontend", deps=["backend"]),
            )
        )

        order = [n.id for n in reverse_topo_succeeded(wf)]

        assert order.index("frontend") < order.index("backend") < order.index("api")

    def test_unsucceeded_nodes_are_not_compensated(self, make_workflow):
        wf = make_workflow([make_node("backend", agent="backend")])
        wf.tasks["backend"].status = TaskStatus.FAILED

        assert reverse_topo_succeeded(wf) == []

    def test_saga_marks_nodes_compensated_and_audits(self, make_workflow):
        wf = make_workflow(_succeeded(make_node("backend", agent="backend")))
        events = []

        results = run_compensation_saga(
            wf,
            reason="task_failed",
            audit=lambda w, t, **kw: events.append(t),
        )

        assert [r["task_id"] for r in results] == ["backend"]
        assert wf.tasks["backend"].status == TaskStatus.COMPENSATED
        assert "COMPENSATION_STARTED" in events
        assert "COMPENSATION_COMPLETED" in events
        assert "ROLLBACK_SAGA_FINISHED" in events

    def test_stop_before_halts_the_chain(self, make_workflow):
        wf = make_workflow(
            _succeeded(
                make_node("api", agent="api"),
                make_node("backend", agent="backend", deps=["api"]),
            )
        )

        results = run_compensation_saga(
            wf, reason="halt", stop_before={"backend"}
        )

        assert results == []
        assert wf.tasks["backend"].status == TaskStatus.SUCCEEDED


class TestDurableSideEffects:
    def test_generated_backend_directory_is_removed(self, make_workflow):
        wf = make_workflow(_succeeded(make_node("backend.implement", agent="backend")))
        ws = forge_paths().workspaces / wf.id / "backend"
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "main.py").write_text("print('generated')", encoding="utf-8")

        run_compensation_saga(wf, reason="rollback")

        assert not ws.exists(), "generated backend survived rollback"

    def test_design_artifacts_are_retained_for_diagnosis(self, make_workflow):
        """Rollback must not destroy the evidence explaining why it happened."""
        from forge.agents._common import publish

        wf = make_workflow(_succeeded(make_node("backend", agent="backend")))
        node = wf.tasks["backend"]
        publish(wf, node, "hld", {"tenets": ["cache-first"]})
        publish(wf, node, "security_review", {"findings": []})
        publish(wf, node, "backend_source", {"app/main.py": "..."})

        run_compensation_saga(wf, reason="rollback")

        assert "hld" in wf.artifacts
        assert "security_review" in wf.artifacts
        assert "backend_source" not in wf.artifacts, "implementation output kept"

    def test_backend_failure_publishes_a_root_cause_verdict(self, make_workflow):
        wf = make_workflow(_succeeded(make_node("backend", agent="backend")))

        run_compensation_saga(wf, reason="task_failed:boom")

        assert "backend_verdict" in wf.artifacts


def test_engine_rolls_back_after_a_terminal_failure(engine, make_workflow):
    """A failing node downstream of codegen triggers the saga."""
    wf = make_workflow(
        [
            make_node("backend", agent="backend"),
            make_node("boom", agent="no-such-agent", deps=["backend"]),
        ]
    )
    wf.tasks["backend"].status = TaskStatus.SUCCEEDED
    wf.tasks["boom"].max_attempts = 1

    engine.run(wf)

    assert wf.tasks["backend"].status == TaskStatus.COMPENSATED
    assert wf.metrics["rollbacks"] >= 1
