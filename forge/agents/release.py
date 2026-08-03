from __future__ import annotations

from typing import Any

from ..models import TaskNode, Workflow
from ._common import art, publish
from .doc_context import product_name
from .llm_bridge import run_llm_agent


def release_readiness(wf: Workflow, task: TaskNode) -> dict[str, Any]:
    """14. Release Readiness Agent — checklist, notes, deployment recommendation."""
    val = art(wf, "validation_report") or {}
    eng = art(wf, "engineering_validation") or {}
    approved = any(a.status == "APPROVED" for a in wf.approvals)
    name = product_name(wf)

    llm = run_llm_agent(
        wf,
        task,
        agent="release",
        inputs={
            "validation_report": val,
            "engineering_validation": eng,
            "security_review": art(wf, "security_review"),
            "documentation": art(wf, "documentation"),
            "approvals": [
                {"title": a.title, "status": a.status, "decision": a.decision}
                for a in wf.approvals
            ],
        },
        schema_hint=(
            '{"release_notes":{"version":"","highlights":[],"checklist":{},"go_no_go":"GO|PENDING"},'
            '"deployment_recommendation":{}}'
        ),
    )
    if llm and isinstance(llm.get("release_notes"), dict):
        notes = llm["release_notes"]
        notes.setdefault("product", name)
        notes.setdefault("go_no_go", "GO" if approved else "PENDING")
        publish(wf, task, "release_notes", notes, bill=False)
        publish(
            wf,
            task,
            "deployment_recommendation",
            llm.get("deployment_recommendation") or notes.get("deployment_recommendation") or {},
            bill=False,
        )
        return {
            "summary": f"Release readiness via LLM — {notes.get('go_no_go')}",
            "mode": "llm",
        }

    notes = {
        "product": name,
        "version": "0.1.0",
        "highlights": [
            f"{name} MVP from uploaded requirements",
            "shorten + redirect",
            "analytics / API keys as specified in PRD",
        ],
        "rollback": "redeploy previous redirect-api image; cache warm optional",
        "checklist": {
            "validation_gates": val.get("overall", "unknown"),
            "engineering_validation": eng.get("verdict", "unknown"),
            "security_review": "complete" if art(wf, "security_review") else "missing",
            "runbooks": "published" if art(wf, "documentation") else "missing",
            "canary_plan": "5% → 25% → 50% → 100%",
            "compliance": "mapped"
            if art(wf, "compliance_mapping")
            else ("n/a" if wf.facts.get("from_document") else "missing"),
        },
        "deployment_recommendation": {
            "environment": "staging → prod canary",
            "pre_reqs": ["migrations applied", "dashboards imported", "alerts armed"],
            "post_deploy": ["watch redirect_p99", "watch error_rate", "verify create path"],
        },
        "go_no_go": "GO" if approved else "PENDING",
        "source_document": wf.facts.get("requirement_filename"),
    }
    if wf.facts.get("feature_qr"):
        notes["highlights"].append("QR code generation")
    if wf.facts.get("fix_open_redirect"):
        notes["highlights"].append("Open-redirect hardening")
    publish(wf, task, "release_notes", notes)
    publish(wf, task, "deployment_recommendation", notes["deployment_recommendation"])
    return {"summary": f"Release readiness — {notes['go_no_go']}"}
