"""
End-to-end playbook runs in heuristic mode (no LLM).

These are the tests that would catch a regression in the thing the platform
actually claims to do: take a requirement, run the full SDLC DAG under governance,
and emit output that passes real quality gates.
"""

from __future__ import annotations

import json

import pytest

from forge.core.paths import paths as forge_paths
from forge.graph.runtime import LangGraphRuntime, langgraph_available
from forge.models import TaskStatus, WorkflowStatus

pytestmark = pytest.mark.skipif(
    not langgraph_available(), reason="langgraph is not installed"
)

MAX_GATES = 15


def drive_to_completion(runtime, wf):
    """Approve every gate the workflow opens; return the gates it stopped at."""
    result = runtime.start(wf)
    gates: list[tuple[str, str]] = []
    while result.get("interrupted"):
        assert len(gates) < MAX_GATES, f"gate loop did not terminate: {gates}"
        pending = result["pending_approval"]
        options = [o["id"] for o in pending.get("options") or []]
        decision = next((o for o in options if o not in ("reject", "nogo")), "approve")
        gates.append((pending["task_id"], decision))
        result = runtime.resume_approval(
            wf, decision=decision, rationale="approved by integration test"
        )
    return result, gates


@pytest.fixture
def runtime():
    return LangGraphRuntime(auto_approve=True, max_workers=4, cli_demo_mode=True)


class TestGreenfieldRun:
    @pytest.fixture(scope="class")
    def completed(self):
        rt = LangGraphRuntime(auto_approve=True, max_workers=4, cli_demo_mode=True)
        wf = rt.engine.plan("greenfield")
        _, gates = drive_to_completion(rt, wf)
        return wf, gates

    def test_workflow_reaches_a_successful_terminal_state(self, completed):
        wf, _ = completed

        assert wf.status == WorkflowStatus.SUCCEEDED
        assert not [
            t.id for t in wf.tasks.values() if t.status == TaskStatus.FAILED
        ]

    def test_the_figma_gate_stops_for_a_human(self, completed):
        """A hard gate must interrupt even in unattended demo mode."""
        _, gates = completed

        assert ("approval.figma", "agent_design") in gates

    def test_every_sdlc_stage_produced_its_artifact(self, completed):
        wf, _ = completed

        for key in (
            "reqspec",
            "execution_plan",
            "risk_register",
            "hld",
            "adrs",
            "capacity_estimate",
            "schema_ddl",
            "openapi",
            "perf_budget",
            "backend_source",
            "test_plan",
            "documentation",
            "security_review",
            "security_scan",
            "validation_report",
        ):
            assert key in wf.artifacts, f"missing artifact: {key}"

    def test_all_blocking_gates_pass_on_generated_output(self, completed):
        wf, _ = completed
        report = wf.artifacts["validation_report"].content

        assert report["overall"] == "PASS"
        assert report["summary"]["blocking_failures"] == []

    def test_the_contract_gate_validated_a_real_spec(self, completed):
        wf, _ = completed
        report = wf.artifacts["validation_report"].content
        gate = next(r for r in report["results"] if r["gate"] == "api.openapi_valid")

        assert gate["status"] == "PASS"
        assert "operations" in gate["detail"]

    def test_generated_python_actually_compiles(self, completed):
        wf, _ = completed
        report = wf.artifacts["validation_report"].content
        gate = next(r for r in report["results"] if r["gate"] == "code.compiles")

        assert gate["status"] == "PASS"
        assert (forge_paths().workspaces / wf.id / "backend").exists()

    def test_coverage_is_measured_against_the_contract(self, completed):
        wf, _ = completed
        coverage = wf.artifacts["test_plan"].content["coverage_report"]

        assert coverage["declared_operations"] > 0
        assert coverage["covered_operations"] == coverage["declared_operations"]
        assert "OpenAPI operations" in coverage["method"]

    def test_the_run_is_fully_audited(self, completed):
        wf, _ = completed
        trace = forge_paths().state / "audit" / "traces" / f"{wf.id}.jsonl"

        events = [json.loads(line) for line in trace.read_text().splitlines() if line.strip()]
        types = {e["type"] for e in events}

        assert {"WORKFLOW_STARTED", "TASK_STARTED", "TASK_SUCCEEDED"} <= types
        assert "APPROVAL_APPROVED" in types
        assert "VALIDATION_PASSED" in types
        assert "WORKFLOW_FINISHED" in types
        assert all(e["trace_id"] == wf.id for e in events)

    def test_reliability_metrics_are_recorded(self, completed):
        wf, _ = completed

        assert wf.metrics["success_rate"] == 1.0
        assert wf.metrics["e2e_latency_ms"] > 0
        assert wf.metrics["parallel_max_width"] >= 2, "no parallel fan-out occurred"

    def test_state_is_durable_on_disk(self, completed):
        wf, _ = completed
        saved = forge_paths().state / "workflows" / f"{wf.id}.json"

        data = json.loads(saved.read_text())
        assert data["status"] == "SUCCEEDED"
        assert data["checkpoint_seq"] > 0


class TestAmbiguousRun:
    """The clarification gate must re-plan the graph from the human's answer."""

    def test_clarification_choice_grafts_new_work(self, runtime):
        wf = runtime.engine.plan("ambiguous", facts={"ambiguous_brief": True})
        before = set(wf.tasks)

        _, gates = drive_to_completion(runtime, wf)

        assert any(g[0] == "approval.clarify" for g in gates)
        assert wf.status == WorkflowStatus.SUCCEEDED
        added = set(wf.tasks) - before
        assert "analytics.pipeline" in added, "clarification did not expand the DAG"
        assert any(e["type"] == "REPLAN_EXPAND" for e in wf.events)


class TestBrownfieldRun:
    """Impact analysis over a preserved baseline, then delta execution."""

    @pytest.fixture(scope="class")
    def completed(self):
        from forge.engine import OrchestrationEngine

        engine = OrchestrationEngine(
            auto_approve=True, max_workers=4, cli_demo_mode=True
        )
        return engine.run_brownfield_with_impact()

    def test_the_delta_run_succeeds(self, completed):
        assert completed.status == WorkflowStatus.SUCCEEDED

    def test_baseline_work_is_preserved_not_re_executed(self, completed):
        baseline = [t for t in completed.tasks.values() if t.id.startswith("baseline.")]

        assert baseline
        assert all(t.status == TaskStatus.SUCCEEDED for t in baseline)
        assert all(t.attempt == 0 for t in baseline), "baseline was re-run"

    def test_impact_analysis_is_audited(self, completed):
        event = next(e for e in completed.events if e["type"] == "IMPACT_ANALYSIS")

        assert event["payload"]["preserved"]
        assert event["payload"]["impact"]

    def test_the_preserved_baseline_passes_the_same_gates(self, completed):
        """A preserved contract is validated exactly like a newly generated one."""
        report = completed.artifacts["validation_report"].content

        assert report["overall"] == "PASS"
        contract = next(r for r in report["results"] if r["gate"] == "api.openapi_valid")
        assert contract["status"] == "PASS"

    def test_the_security_scan_gate_is_skipped_not_faked(self, completed):
        report = completed.artifacts["validation_report"].content
        gate = next(r for r in report["results"] if r["gate"] == "sec.scan")

        assert gate["status"] == "SKIP"
        assert gate["blocking"] is False


class TestDocumentIngestRun:
    """The primary path: upload a requirements document, run the production SDLC."""

    @pytest.fixture(scope="class")
    def completed(self):
        rt = LangGraphRuntime(auto_approve=True, max_workers=4, cli_demo_mode=True)
        doc = forge_paths().examples / "tinyurl-requirements.md"
        text = doc.read_text(encoding="utf-8")
        wf = rt.prepare_from_document(text=text, filename=doc.name)
        _, gates = drive_to_completion(rt, wf)
        return wf, gates

    def test_the_document_drives_the_workflow(self, completed):
        wf, _ = completed

        assert wf.facts["from_document"] is True
        assert wf.artifacts["raw_requirement"].content["filename"] == "tinyurl-requirements.md"
        assert wf.status == WorkflowStatus.SUCCEEDED

    def test_all_human_gates_were_presented(self, completed):
        """Controlled autonomy: the high-impact decisions stopped for a human."""
        _, gates = completed
        stopped_at = {g[0] for g in gates}

        assert {"approval.clarify", "approval.coding", "approval.figma"} <= stopped_at

    def test_quality_gates_pass_and_defer_honestly(self, completed):
        wf, _ = completed
        report = wf.artifacts["validation_report"].content

        assert report["overall"] == "PASS"
        assert report["summary"]["blocking_failures"] == []
        # Docs and observability run after this stage — reported SKIP, not PASS.
        deferred = {r["gate"] for r in report["results"] if r["status"] == "SKIP"}
        assert "docs.runbook_exists" in deferred

    def test_a_runnable_workspace_is_produced(self, completed):
        wf, _ = completed
        manifest = wf.artifacts["workspace_manifest"].content

        assert manifest["backend_files"]
        assert (forge_paths().workspaces / wf.id / "backend").exists()

    def test_the_engineering_summary_is_delivered(self, completed):
        wf, _ = completed

        assert "engineering_summary" in wf.artifacts


class TestFailurePaths:
    def test_an_injected_gate_failure_fails_the_run(self, runtime, monkeypatch):
        monkeypatch.setenv("FORGE_INJECT_FAIL", "test.coverage_critical")
        wf = runtime.engine.plan("greenfield")

        drive_to_completion(runtime, wf)

        assert wf.status == WorkflowStatus.FAILED
        report = wf.artifacts["validation_report"].content
        assert report["overall"] == "FAIL"
        assert "test.coverage_critical" in report["summary"]["blocking_failures"]

    def test_a_rejected_gate_stops_the_workflow(self, runtime):
        wf = runtime.engine.plan("greenfield")
        result = runtime.start(wf)

        assert result["interrupted"] is True
        runtime.resume_approval(wf, decision="reject", rationale="design not viable")

        assert wf.status == WorkflowStatus.FAILED
        assert any(e["type"] == "APPROVAL_REJECTED" for e in wf.events)

    def test_failure_after_codegen_rolls_back_the_workspace(self, runtime, monkeypatch):
        """Compensation must remove generated code when a later gate fails."""
        monkeypatch.setenv("FORGE_INJECT_FAIL", "api.openapi_valid")
        wf = runtime.engine.plan("greenfield")

        drive_to_completion(runtime, wf)

        assert wf.status == WorkflowStatus.FAILED
        compensated = [
            t.id for t in wf.tasks.values() if t.status == TaskStatus.COMPENSATED
        ]
        assert compensated, "no side-effecting node was compensated"
        assert wf.metrics["rollbacks"] >= 1
        assert any(e["type"] == "ROLLBACK_SAGA_FINISHED" for e in wf.events)
