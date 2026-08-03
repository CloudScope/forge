from __future__ import annotations

from typing import Any, Optional

from ..llm import try_complete_json
from ..models import TaskNode, Workflow
from .doc_context import doc_summary, requirement_text


def prd_context(wf: Workflow) -> dict[str, Any]:
    return {
        "requirement_filename": wf.facts.get("requirement_filename"),
        "requirement_text": requirement_text(wf)[:20000],
        "document_summary": doc_summary(wf),
    }


def run_llm_agent(
    wf: Workflow,
    task: TaskNode,
    *,
    agent: str,
    inputs: dict[str, Any],
    schema_hint: str,
    system_extra: str = "",
) -> Optional[dict[str, Any]]:
    """
    Attempt LLM generation for an agent.

    Returns parsed JSON dict or None (use heuristics).
    Includes PRD text automatically when present.
    """
    payload = {"prd": prd_context(wf), **inputs}
    return try_complete_json(
        wf,
        task,
        agent=agent,
        user_payload=payload,
        schema_hint=schema_hint,
        system_extra=system_extra,
    )
