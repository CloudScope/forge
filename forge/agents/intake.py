from __future__ import annotations

from typing import Any

from ..models import TaskNode, Workflow
from ._common import publish
from .doc_context import doc_summary, requirement_text
from .llm_bridge import run_llm_agent


def intake_capture(wf: Workflow, task: TaskNode) -> dict[str, Any]:
    """1. Requirement Intake — capture PRD, stakeholders, constraints."""
    text = requirement_text(wf)
    summary = doc_summary(wf)
    llm = run_llm_agent(
        wf,
        task,
        agent="intake",
        inputs={"document_summary": summary},
        schema_hint=(
            '{"intake_record":{"stakeholders":[],"constraints":[],"compliance_hints":[],'
            '"source_systems":[],"success_signals":[]}}'
        ),
    )
    if llm and isinstance(llm.get("intake_record"), dict):
        record = llm["intake_record"]
        mode = "llm"
    else:
        record = {
            "filename": wf.facts.get("requirement_filename"),
            "product_name": summary.get("product_name") or wf.facts.get("product_name"),
            "title": summary.get("title"),
            "stakeholders": ["PM", "Platform Eng", "SRE", "Security", "Compliance"],
            "constraints": summary.get("goals")[:3] if summary.get("goals") else [],
            "compliance_hints": ["access_logging", "encryption_at_rest", "retention"],
            "source_systems": ["uploaded_prd"] if text else ["demo_brief"],
            "success_signals": [
                f"redirect_p99_ms<={summary.get('redirect_p99_ms', 50)}",
                f"availability>={summary.get('availability', '99.99%')}",
            ],
            "char_count": summary.get("char_count") or len(text),
            "features_detected": summary.get("features") or [],
        }
        mode = "heuristic"

    record["workflow_id"] = wf.id
    record["playbook_id"] = wf.playbook_id
    publish(wf, task, "intake_record", record, bill=(mode != "llm"))
    if record.get("product_name"):
        wf.facts["product_name"] = record["product_name"]
    return {
        "summary": f"Intake captured for {record.get('product_name') or 'product'}",
        "mode": mode,
    }
