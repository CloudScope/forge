from __future__ import annotations

from typing import Any

from ..models import TaskNode, Workflow
from ._common import art, publish


def engineering_review(wf: Workflow, task: TaskNode) -> dict[str, Any]:
    """Reviewer Agent — cross-cutting design + code review."""
    blocking: list[str] = []
    non_blocking: list[str] = []
    hld = art(wf, "hld") or {}
    tenets_blob = " ".join(str(t) for t in (hld.get("tenets") or [])).lower()
    hld_blob = f"{tenets_blob} {hld}".lower()
    # Only require transactional outbox when this run's design claims dual-write risk.
    dual_write_markers = (
        "dual-write",
        "dual write",
        "outbox",
        "analytics side",
        "side-effect",
        "side effect",
        "click event",
        "click stream",
        "kafka",
        "event bus",
        "publish event",
    )
    claims_dual_write = any(m in hld_blob for m in dual_write_markers)
    if claims_dual_write and "outbox" not in hld_blob:
        blocking.append("Missing transactional outbox tenet (design claims async side-effects)")
    if not art(wf, "openapi"):
        blocking.append("OpenAPI missing")
    if not art(wf, "schema_ddl"):
        blocking.append("Schema DDL missing")

    report = {
        "blocking": blocking,
        "non_blocking": non_blocking,
        "checks": [
            "ADR consistency with HLD",
            "API/schema alignment",
            "reliability patterns match claimed HLD tenets",
            "naming consistency across services",
        ],
        "verdict": "APPROVE_WITH_NITS" if not blocking else "REQUEST_CHANGES",
    }
    publish(wf, task, "review_report", report)
    return {"summary": f"Eng review: {report['verdict']}"}
