"""Human-in-the-loop governance: which gates may auto-decide, and what a decision unlocks."""

from __future__ import annotations

import pytest

from forge.approval import auto_decide, build_request
from forge.approval_gates import force_human_gate, is_approval_task
from forge.engine import OrchestrationEngine
from forge.models import NodeType, RiskTier, TaskStatus, WorkflowStatus
from tests.conftest import make_node


class TestForceHumanGate:
    """Gates that may never be silently auto-approved are the core autonomy boundary."""

    @pytest.mark.parametrize(
        "task_id", ["approval.clarify", "approval.coding", "approval.figma"]
    )
    def test_hard_gates_require_a_human_even_in_cli_demo(self, task_id):
        assert force_human_gate(task_id, cli_demo_mode=True) is True
        assert force_human_gate(task_id, cli_demo_mode=False) is True

    @pytest.mark.parametrize("task_id", ["approval.plan", "approval.arch"])
    def test_studio_gates_require_a_human_but_cli_demo_may_auto(self, task_id):
        assert force_human_gate(task_id, cli_demo_mode=False) is True
        assert force_human_gate(task_id, cli_demo_mode=True) is False

    @pytest.mark.parametrize("task_id", ["approval.db", "approval.api", "approval.release"])
    def test_remaining_gates_follow_the_auto_approve_setting(self, task_id):
        assert force_human_gate(task_id, cli_demo_mode=False) is False

    def test_approval_task_detection(self):
        assert is_approval_task("approval.arch") is True
        assert is_approval_task("arch.design") is False


class TestApprovalEffects:
    """Approving a gate must freeze the right decision and unlock the next stage."""

    def _gate(self, engine, make_workflow, gate_id):
        wf = make_workflow(
            [make_node(gate_id, agent="human_approval", node_type=NodeType.APPROVAL)]
        )
        node = wf.tasks[gate_id]
        req = build_request(wf, node)
        wf.approvals.append(req)
        return wf, node, req

    def test_db_approval_unlocks_the_api_stage(self, engine, make_workflow):
        wf, node, req = self._gate(engine, make_workflow, "approval.db")

        engine._apply_approval_decision(wf, node, req, "approve", "looks good")

        assert req.status == "APPROVED"
        assert node.status == TaskStatus.SUCCEEDED
        assert wf.facts["frozen_database"] is True
        assert wf.facts["api_unlocked"] is True

    def test_api_approval_unlocks_codegen(self, engine, make_workflow):
        wf, node, req = self._gate(engine, make_workflow, "approval.api")

        engine._apply_approval_decision(wf, node, req, "approve", "contract frozen")

        assert wf.facts["frozen_api"] is True
        assert wf.facts["code_unlocked"] is True

    def test_arch_approval_freezes_architecture(self, engine, make_workflow):
        wf, node, req = self._gate(engine, make_workflow, "approval.arch")

        engine._apply_approval_decision(wf, node, req, "approve", "frozen")

        assert wf.facts["frozen_architecture"] is True

    def test_clarify_choice_is_recorded_and_expands_the_graph(self, engine, make_workflow):
        wf, node, req = self._gate(engine, make_workflow, "approval.clarify")

        engine._apply_approval_decision(wf, node, req, "B", "product analytics scope")

        assert wf.facts["analytics_option"] == "B"
        assert wf.facts["needs_clarification"] is False
        assert "analytics.pipeline" in wf.tasks, "clarification did not re-plan the DAG"
        assert "analytics.api" in wf.tasks

    def test_an_invented_option_id_would_fail_the_run(self, engine, make_workflow):
        """
        Why sanitize_options exists: the engine reads any unknown id as a
        rejection, so a gate button the model named "proceed" is destructive.
        """
        wf, node, req = self._gate(engine, make_workflow, "approval.clarify")

        engine._apply_approval_decision(wf, node, req, "proceed", "looks fine")

        assert wf.status == WorkflowStatus.FAILED, "unknown ids must stay fail-closed"

    def test_llm_authored_options_are_constrained_to_known_ids(self):
        from forge.approval_gates import sanitize_options

        # Model invents its own vocabulary — drop it and use the gate defaults.
        assert sanitize_options([{"id": "proceed", "label": "Proceed"}]) is None
        # A menu with no way forward is not a usable menu.
        assert sanitize_options([{"id": "reject", "label": "Stop"}]) is None
        # Known ids survive intact.
        assert sanitize_options(
            [{"id": "A", "label": "Essentials"}, {"id": "reject", "label": "Stop"}]
        ) == [{"id": "A", "label": "Essentials"}, {"id": "reject", "label": "Stop"}]

    def test_rejection_fails_the_workflow(self, engine, make_workflow):
        wf, node, req = self._gate(engine, make_workflow, "approval.arch")

        engine._apply_approval_decision(wf, node, req, "reject", "capacity model unsound")

        assert req.status == "REJECTED"
        assert node.status == TaskStatus.FAILED
        assert wf.status == WorkflowStatus.FAILED
        assert "capacity model unsound" in (node.error or "")

    def test_rejection_is_audited(self, engine, make_workflow):
        wf, node, req = self._gate(engine, make_workflow, "approval.release")

        engine._apply_approval_decision(wf, node, req, "nogo", "perf regression")

        assert any(e["type"] == "APPROVAL_REJECTED" for e in wf.events)


class TestGatePausing:
    def test_workflow_pauses_and_persists_at_a_gate(self, make_workflow):
        engine = OrchestrationEngine(auto_approve=False, max_workers=1)
        wf = make_workflow(
            [
                make_node("approval.arch", agent="human_approval", node_type=NodeType.APPROVAL),
                make_node("next", node_type=NodeType.BARRIER, deps=["approval.arch"]),
            ]
        )

        engine.run(wf)

        assert wf.status == WorkflowStatus.WAITING_APPROVAL
        assert wf.tasks["next"].status == TaskStatus.PENDING
        assert [a.status for a in wf.approvals] == ["REQUESTED"]
        assert engine.store.load_workflow(wf.id) is not None, "pause was not persisted"

    def test_resume_after_approval_continues_the_dag(self, make_workflow):
        engine = OrchestrationEngine(auto_approve=False, max_workers=1)
        wf = make_workflow(
            [
                make_node("approval.arch", agent="human_approval", node_type=NodeType.APPROVAL),
                make_node("next", node_type=NodeType.BARRIER, deps=["approval.arch"]),
            ]
        )
        engine.run(wf)

        engine.submit_approval(wf, decision="approve", rationale="ship it")

        assert wf.status == WorkflowStatus.SUCCEEDED
        assert wf.tasks["next"].status == TaskStatus.SUCCEEDED

    def test_submitting_without_a_pending_request_is_rejected(self, engine, make_workflow):
        wf = make_workflow([make_node("a", node_type=NodeType.BARRIER)])

        with pytest.raises(ValueError, match="No pending approval"):
            engine.submit_approval(wf, decision="approve")

    def test_auto_approve_does_not_bypass_a_hard_gate(self, make_workflow):
        """auto_approve=True must still stop at approval.coding."""
        engine = OrchestrationEngine(
            auto_approve=True, max_workers=1, cli_demo_mode=True
        )
        wf = make_workflow(
            [make_node("approval.coding", agent="human_approval", node_type=NodeType.APPROVAL)]
        )

        engine.run(wf)

        assert wf.status == WorkflowStatus.WAITING_APPROVAL


def test_auto_decide_defaults_are_conservative():
    from forge.models import ApprovalRequest

    def req(task_id):
        return ApprovalRequest(
            id="a1", task_id=task_id, risk_tier=RiskTier.HIGH, title="t",
            summary="s", options=[],
        )

    assert auto_decide(req("approval.clarify"))[0] == "A"
    assert auto_decide(req("approval.release"))[0] == "go"
    assert auto_decide(req("approval.figma"))[0] == "agent_design"
    assert auto_decide(req("approval.db"))[0] == "approve"
