"""Quality gates: a gate must be able to fail, and must never fake a pass."""

from __future__ import annotations

import pytest

from forge.agents._common import publish
from forge.core.paths import paths as forge_paths
from forge.validation import evaluate, overall_pass, summarize
from tests.conftest import make_node


def _gate(results, name):
    return next(r for r in results if r.gate == name)


@pytest.fixture
def wf(make_workflow):
    return make_workflow([make_node("producer")])


def _publish(wf, key, content):
    publish(wf, wf.tasks["producer"], key, content, bill=False)


VALID_SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "API", "version": "1.0.0"},
    "paths": {"/v1/items": {"post": {"responses": {"201": {"description": "ok"}}}}},
}


class TestNoGateIsUnfailable:
    """Every blocking gate must have an input that makes it FAIL."""

    def test_empty_workflow_fails_the_stage(self, wf):
        results = evaluate(wf, stage="pre_release")

        assert overall_pass(results) is False
        assert summarize(results)["blocking_failures"]

    def test_capacity_gate_can_fail(self, wf):
        assert _gate(evaluate(wf), "arch.capacity_present").status == "FAIL"

        _publish(wf, "capacity_estimate", {"write_qps": 76})

        assert _gate(evaluate(wf), "arch.capacity_present").status == "PASS"

    def test_adr_gate_can_fail(self, wf):
        assert _gate(evaluate(wf), "arch.adr_complete").status == "FAIL"

        _publish(wf, "hld", {"tenets": []})
        _publish(wf, "adrs", [{"id": "ADR-1"}])

        assert _gate(evaluate(wf), "arch.adr_complete").status == "PASS"


class TestDeferredGatesSkipRatherThanPass:
    """A gate whose producing agent has not run yet reports SKIP, never PASS."""

    def test_docs_gate_skips_at_the_quality_stage(self, wf):
        gate = _gate(evaluate(wf, stage="quality_gate"), "docs.runbook_exists")

        assert gate.status == "SKIP"
        assert gate.blocking is False

    def test_docs_gate_passes_once_documentation_exists(self, wf):
        _publish(wf, "documentation", {"readme": "..."})

        assert _gate(evaluate(wf, stage="quality_gate"), "docs.runbook_exists").status == "PASS"

    def test_docs_gate_can_fail_at_the_release_stage(self, wf):
        gate = _gate(evaluate(wf, stage="pre_release"), "docs.runbook_exists")

        assert gate.status == "FAIL"
        assert gate.blocking is True

    def test_observability_gate_skips_at_the_quality_stage(self, wf):
        gate = _gate(evaluate(wf, stage="quality_gate"), "o11y.dashboards")

        assert gate.status == "SKIP"

    def test_security_scan_gate_skips_on_brownfield_only(self, wf):
        assert _gate(evaluate(wf, stage="brownfield"), "sec.scan").status == "SKIP"
        assert _gate(evaluate(wf, stage="pre_release"), "sec.scan").status == "FAIL"

    def test_skipped_gates_do_not_block_the_stage(self, wf):
        results = evaluate(wf, stage="quality_gate")
        skipped = [r for r in results if r.status == "SKIP"]

        assert skipped
        assert all(r.blocking is False for r in skipped)


class TestSecurityVerdictIsEvidenceBased:
    """The verdict is read from the scan artifact, not from a mutable fact."""

    def test_failing_scan_fails_the_gate(self, wf):
        _publish(
            wf,
            "security_scan",
            {"verdict": "FAIL", "critical_open": [{"finding": "open redirect"}]},
        )

        gate = _gate(evaluate(wf), "sec.validation_passed")

        assert gate.status == "FAIL"
        assert "unresolved critical/high=1" in gate.detail

    def test_a_fact_cannot_override_a_failing_scan(self, wf):
        """Regression: a re-plan used to set this fact and self-heal the gate."""
        _publish(wf, "security_scan", {"verdict": "FAIL", "critical_open": [{"f": "x"}]})
        wf.facts["security_validation_passed"] = True

        assert _gate(evaluate(wf), "sec.validation_passed").status == "FAIL"

    def test_passing_scan_with_no_open_findings_passes(self, wf):
        _publish(wf, "security_scan", {"verdict": "PASS", "critical_open": []})

        assert _gate(evaluate(wf), "sec.validation_passed").status == "PASS"

    def test_missing_verdict_fails_closed(self, wf):
        _publish(wf, "security_scan", {"findings": []})

        assert _gate(evaluate(wf), "sec.validation_passed").status == "FAIL"

    def test_absent_scan_is_skipped_not_passed(self, wf):
        assert _gate(evaluate(wf), "sec.validation_passed").status == "SKIP"


class TestContractGate:
    def test_valid_contract_passes(self, wf):
        _publish(wf, "openapi", VALID_SPEC)

        gate = _gate(evaluate(wf), "api.openapi_valid")

        assert gate.status == "PASS"
        assert "1 operations" in gate.detail

    def test_structurally_broken_contract_fails(self, wf):
        _publish(wf, "openapi", {"openapi": "3.0.3", "info": {}, "paths": {}})

        gate = _gate(evaluate(wf), "api.openapi_valid")

        assert gate.status == "FAIL"
        assert "contract error" in gate.detail

    def test_presence_alone_is_not_enough(self, wf):
        """The old gate passed on any non-empty artifact."""
        _publish(wf, "openapi", {"paths": {"/a": {}}})

        assert _gate(evaluate(wf), "api.openapi_valid").status == "FAIL"


class TestCompileGate:
    def test_skipped_when_no_workspace_was_generated(self, wf):
        assert _gate(evaluate(wf), "code.compiles").status == "SKIP"

    def test_passes_on_syntactically_valid_generated_code(self, wf):
        root = forge_paths().workspaces / wf.id / "backend"
        root.mkdir(parents=True, exist_ok=True)
        (root / "main.py").write_text("def app():\n    return None\n", encoding="utf-8")

        assert _gate(evaluate(wf), "code.compiles").status == "PASS"

    def test_fails_on_broken_generated_code(self, wf):
        root = forge_paths().workspaces / wf.id / "backend"
        root.mkdir(parents=True, exist_ok=True)
        (root / "main.py").write_text("def app(:\n", encoding="utf-8")

        gate = _gate(evaluate(wf), "code.compiles")

        assert gate.status == "FAIL"
        assert "syntax error" in gate.detail


class TestCoverageGate:
    def test_coverage_is_measured_against_the_contract(self, wf):
        _publish(wf, "openapi", VALID_SPEC)
        _publish(wf, "test_plan", {"api": ["POST /v1/items contract conformance"]})

        gate = _gate(evaluate(wf), "test.coverage_critical")

        assert gate.status == "PASS"
        assert "1/1 API operations" in gate.detail

    def test_self_asserted_percentage_is_ignored(self, wf):
        """Regression: the gate used to read a number the agent wrote about itself."""
        _publish(wf, "openapi", VALID_SPEC)
        _publish(wf, "test_plan", {"critical_coverage_pct": 99, "api": ["vague case"]})

        gate = _gate(evaluate(wf), "test.coverage_critical")

        assert gate.status == "FAIL"
        assert "0/1 API operations" in gate.detail

    def test_non_dict_test_plan_does_not_crash_the_gate(self, wf):
        """Regression: an LLM returning a list used to raise AttributeError."""
        _publish(wf, "openapi", VALID_SPEC)
        _publish(wf, "test_plan", ["case one", "case two"])

        assert _gate(evaluate(wf), "test.coverage_critical").status == "FAIL"

    def test_skipped_when_there_is_no_contract_to_measure(self, wf):
        _publish(wf, "test_plan", {"unit": ["x"]})

        assert _gate(evaluate(wf), "test.coverage_critical").status == "SKIP"


class TestFailureInjection:
    def test_named_gate_is_forced_to_fail(self, wf, monkeypatch):
        _publish(wf, "capacity_estimate", {"qps": 1})
        monkeypatch.setenv("FORGE_INJECT_FAIL", "arch.capacity_present")

        gate = _gate(evaluate(wf), "arch.capacity_present")

        assert gate.status == "FAIL"
        assert gate.blocking is True
        assert "Injected failure" in gate.detail

    def test_injection_can_override_a_skip(self, wf, monkeypatch):
        monkeypatch.setenv("FORGE_INJECT_FAIL", "docs.runbook_exists")

        results = evaluate(wf, stage="quality_gate")

        assert _gate(results, "docs.runbook_exists").status == "FAIL"
        assert overall_pass(results) is False


def test_summarize_reports_gate_counts(wf):
    results = evaluate(wf, stage="quality_gate")
    summary = summarize(results)

    assert summary["passed"] + summary["failed"] + summary["skipped"] == len(results)
    assert set(summary["blocking_failures"]) <= {r.gate for r in results}
