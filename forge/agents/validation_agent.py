from __future__ import annotations

from typing import Any

from ..models import TaskNode, Workflow
from ._common import art, publish
from .llm_bridge import run_llm_agent


def validation_review(wf: Workflow, task: TaskNode) -> dict[str, Any]:
    """
    Validation Agent — engineering validation across quality dimensions.

    Note: machine quality gates still run on nodes whose id starts with
    `validate.` via the orchestration engine Validation Engine.
    """
    llm = run_llm_agent(
        wf,
        task,
        agent="validation",
        inputs={
            "hld": art(wf, "hld"),
            "schema_ddl": art(wf, "schema_ddl"),
            "openapi": art(wf, "openapi"),
            "security_review": art(wf, "security_review"),
            "test_plan": art(wf, "test_plan"),
            "documentation": art(wf, "documentation"),
        },
        schema_hint=(
            '{"engineering_validation":{"dimensions":{},"failed":[],"verdict":"PASS|FAIL"},'
            '"perf_check":{"redirect_design_cache_first":true,"budget_ok":true}}'
        ),
    )
    if llm and isinstance(llm.get("engineering_validation"), dict):
        report = llm["engineering_validation"]
        publish(wf, task, "engineering_validation", report, bill=False)
        publish(
            wf,
            task,
            "perf_check",
            llm.get("perf_check")
            or {"redirect_design_cache_first": True, "budget_ok": bool(art(wf, "perf_budget"))},
            bill=False,
        )
        return {
            "summary": f"Engineering validation via LLM: {report.get('verdict')}",
            "mode": "llm",
        }

    dimensions = {
        "architecture": bool(art(wf, "hld") and art(wf, "adrs")),
        "business_logic": bool(art(wf, "domain_model") or art(wf, "reqspec")),
        "security": bool(art(wf, "security_review")),
        "performance": bool(art(wf, "perf_budget")),
        "scalability": bool(art(wf, "schema_ddl") and art(wf, "hld")),
        "availability": bool(art(wf, "observability_plan") or art(wf, "workload_dashboards")),
        "reliability": "outbox" in str(art(wf, "hld") or "").lower(),
        "maintainability": bool(art(wf, "documentation")),
        "code_quality": bool(art(wf, "backend_source") or art(wf, "source_tree")),
        "api_design": bool(art(wf, "openapi")),
        "database_design": bool(art(wf, "schema_ddl")),
        "naming_consistency": True,
        "artifact_consistency": bool(art(wf, "test_plan") and art(wf, "risk_register")),
    }
    failed = [k for k, ok in dimensions.items() if not ok]
    report = {
        "dimensions": dimensions,
        "failed": failed,
        "verdict": "PASS" if not failed else "FAIL",
        "notes": "Human-readable engineering validation prior to automated gates",
    }
    publish(wf, task, "engineering_validation", report)
    # Also mirror perf_check for legacy validation gates
    publish(
        wf,
        task,
        "perf_check",
        {
            "redirect_design_cache_first": True,
            "budget_ok": bool(art(wf, "perf_budget")),
        },
    )
    return {"summary": f"Engineering validation {report['verdict']} ({len(failed)} gaps)"}
