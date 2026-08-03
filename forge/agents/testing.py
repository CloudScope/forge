from __future__ import annotations

from typing import Any

from ..codegen_validation import derive_operation_coverage, operations
from ..models import TaskNode, Workflow
from ._common import art, publish
from .doc_context import has_feature, product_name
from .llm_bridge import run_llm_agent


def _contract_cases(openapi: Any) -> list[str]:
    """One named API case per documented operation, so coverage is real work."""
    cases: list[str] = []
    for op in operations(openapi):
        method, _, path = op.partition(" ")
        cases.append(f"{method} {path} — contract conformance (status, schema, errors)")
    return cases


def _finalize_plan(wf: Workflow, task: TaskNode, plan: dict[str, Any], mode: str) -> dict[str, Any]:
    """
    Attach measured operation coverage to the plan.

    The number is derived from the frozen OpenAPI contract versus the named test
    cases — it is never asserted by this agent about itself.
    """
    openapi = art(wf, "openapi")
    declared = operations(openapi)
    if declared:
        existing = " ".join(
            str(v) for value in plan.values() if isinstance(value, list) for v in value
        ).lower()
        missing = [
            case
            for case in _contract_cases(openapi)
            if case.split(" — ")[0].partition(" ")[2].lower() not in existing
        ]
        if missing:
            plan["api"] = list(plan.get("api") or []) + missing

    coverage = derive_operation_coverage(openapi, plan)
    plan["coverage_report"] = coverage
    plan["critical_coverage_pct"] = coverage["coverage_pct"]
    plan["coverage_method"] = coverage["method"]
    publish(wf, task, "test_plan", plan, bill=(mode != "llm"))

    pct = coverage["coverage_pct"]
    measured = f"{pct}% of {coverage['declared_operations']} API operations" if pct is not None else "no API operations to measure"
    return {
        "summary": f"Test plan for {product_name(wf)} — {measured}",
        "mode": mode,
        "coverage": coverage,
    }


def test_generate(wf: Workflow, task: TaskNode) -> dict[str, Any]:
    """9. Testing Agent — suites mapped to PRD acceptance criteria."""
    llm = run_llm_agent(
        wf,
        task,
        agent="testing",
        inputs={
            "reqspec": art(wf, "reqspec"),
            "openapi": art(wf, "openapi"),
            "backend_source": art(wf, "backend_source"),
        },
        schema_hint=(
            '{"test_plan":{"unit":[],"integration":[],"api":[],"load":[],"security":[]}}'
        ),
        system_extra=(
            "Name one API case per documented OpenAPI operation, quoting the exact "
            "path template. Coverage is measured against the contract — do not "
            "report a coverage percentage yourself."
        ),
    )
    if llm and isinstance(llm.get("test_plan"), dict):
        return _finalize_plan(wf, task, dict(llm["test_plan"]), "llm")

    # Heuristic fallback: derive suites from this run's own artifacts, not from a
    # hardcoded product. Domain-specific cases come from the security/perf agents.
    name = product_name(wf)
    entities = [
        str(e.get("name") if isinstance(e, dict) else e)
        for e in ((art(wf, "domain_model") or {}).get("entities") or [])
    ]
    controls = (art(wf, "backend_notes") or {}).get("security_needs") or []
    perf = art(wf, "perf_budget") or {}

    plan: dict[str, Any] = {
        "product": name,
        "unit": [f"{e} model invariants and validation" for e in entities[:8]]
        or ["request/response schema validation", "error envelope mapping"],
        "integration": ["persistence round-trip per repository", "transaction rollback on error"],
        "api": [],
        "load": [
            f"{k} budget: {v}" for k, v in list(perf.items())[:4] if isinstance(v, (int, float, str))
        ]
        or ["baseline throughput smoke"],
        "security": [f"negative test for control: {c}" for c in controls]
        or ["input validation rejects malformed payloads"],
        "chaos": ["dependency timeout during a mutating request"],
        "edge_cases": ["empty result set", "duplicate create", "unauthorized access"],
    }
    if has_feature(wf, "custom_alias"):
        plan["unit"].append("alias uniqueness constraint")
    if has_feature(wf, "bulk"):
        plan["integration"].append("bulk partial failure semantics")
    if has_feature(wf, "qr_code") or wf.facts.get("feature_qr"):
        plan["unit"].append("QR render png/svg")
    if wf.facts.get("fix_open_redirect"):
        plan["security"].append("reject non-http(s) redirect targets")

    return _finalize_plan(wf, task, plan, "heuristic")
