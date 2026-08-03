"""
Segmented execution — the AWS model, exercised locally.

On AWS each segment is a separate Fargate task with no shared memory. This test
drives the same path by calling `run_segment` repeatedly against durable storage,
which is exactly what the Step Functions loop does. If a run cannot be advanced by
a process that did not start it, the deployment does not work.
"""

from __future__ import annotations

import pytest

from forge.core.paths import paths as forge_paths
from forge.graph.runtime import langgraph_available
from forge.worker import run_segment

pytestmark = pytest.mark.skipif(
    not langgraph_available(), reason="langgraph is not installed"
)

MAX_SEGMENTS = 12


def _plan(playbook: str = "greenfield"):
    """Create a run without executing it, as the API does before handing off."""
    from forge.engine import OrchestrationEngine

    return OrchestrationEngine(auto_approve=True, cli_demo_mode=True).plan(playbook)


def drive_segments(workflow_id: str) -> tuple[dict, list[str]]:
    """Run segment → approve → run segment, exactly like the state machine."""
    verdict = run_segment(workflow_id)
    gates: list[str] = []
    while verdict["status"] == "PAUSED":
        assert len(gates) < MAX_SEGMENTS, f"segment loop did not terminate: {gates}"
        pending = verdict["pending_approval"]
        options = [o["id"] for o in pending.get("options") or []]
        decision = next((o for o in options if o not in ("reject", "nogo")), "approve")
        gates.append(verdict["gate"])
        verdict = run_segment(
            workflow_id,
            decision=decision,
            rationale="approved by the segment test",
            approval_id=pending.get("approval_id"),
            task_id=pending.get("task_id"),
        )
    return verdict, gates


class TestSegmentedRun:
    @pytest.fixture(scope="class")
    def completed(self):
        wf = _plan("greenfield")
        verdict, gates = drive_segments(wf.id)
        return wf.id, verdict, gates

    def test_the_run_completes_across_segments(self, completed):
        _, verdict, gates = completed

        assert verdict["status"] == "SUCCEEDED"
        assert gates, "no gate was ever surfaced"

    def test_each_pause_reports_the_gate_and_its_options(self, completed):
        """Step Functions needs a machine-readable verdict, not a log line."""
        workflow_id = _plan("greenfield").id
        verdict = run_segment(workflow_id)

        assert verdict["status"] == "PAUSED"
        assert verdict["gate"]
        assert verdict["pending_approval"]["options"]
        assert verdict["workflow_id"] == workflow_id

    def test_state_survives_between_segments(self, completed):
        workflow_id, _, _ = completed
        from forge.state_store import StateStore

        stored = StateStore().load_workflow(workflow_id)

        assert stored is not None
        assert stored.status.value == "SUCCEEDED"
        assert stored.checkpoint_seq > 0

    def test_artifacts_accumulate_across_segments(self, completed):
        """Work done before a gate must still be there after it."""
        workflow_id, _, _ = completed
        from forge.engine import OrchestrationEngine

        engine = OrchestrationEngine(auto_approve=True, cli_demo_mode=True)
        wf = engine.rehydrate(workflow_id)

        assert wf is not None
        for key in ("hld", "openapi", "backend_source", "validation_report"):
            assert key in wf.artifacts, f"{key} lost across a segment boundary"

    def test_the_audit_trace_is_one_continuous_stream(self, completed):
        """Sequence numbers must not restart when a new segment picks the run up."""
        workflow_id, _, _ = completed
        from forge.audit import AuditTraceStore

        events = AuditTraceStore().read(workflow_id)
        seqs = [int(e["seq"]) for e in events]

        assert seqs == sorted(seqs), "audit events are out of order"
        assert len(seqs) == len(set(seqs)), "duplicate sequence numbers across segments"

    def test_the_workspace_is_available_to_later_segments(self, completed):
        workflow_id, _, _ = completed

        assert (forge_paths().workspaces / workflow_id / "backend").exists()


class TestSegmentFailure:
    def test_an_unknown_workflow_is_rejected(self):
        from forge.worker import WorkflowNotFound

        with pytest.raises(WorkflowNotFound):
            run_segment("wf_does_not_exist")

    def test_a_rejected_gate_ends_the_run(self):
        wf = _plan("greenfield")
        verdict = run_segment(wf.id)
        assert verdict["status"] == "PAUSED"

        verdict = run_segment(
            wf.id,
            decision="reject",
            rationale="not viable",
            task_id=verdict["gate"],
        )

        assert verdict["status"] == "FAILED"


class TestWorkerCli:
    def test_the_cli_emits_a_single_json_verdict(self, capsys):
        import json

        from forge.worker import main

        wf = _plan("greenfield")
        exit_code = main(["--workflow-id", wf.id, "--workers", "2"])

        verdict = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert exit_code == 0
        assert verdict["workflow_id"] == wf.id
        assert verdict["status"] in {"PAUSED", "SUCCEEDED", "FAILED", "PARTIAL"}

    def test_a_failed_segment_exits_nonzero(self, capsys):
        from forge.worker import main

        exit_code = main(["--workflow-id", "wf_missing"])

        assert exit_code == 1
        assert "FAILED" in capsys.readouterr().out
