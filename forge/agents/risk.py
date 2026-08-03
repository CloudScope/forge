from __future__ import annotations

from typing import Any

from ..models import TaskNode, Workflow
from ._common import art, publish


def risk_assess(wf: Workflow, task: TaskNode) -> dict[str, Any]:
    """Risk Agent — living risk register + stage scores."""
    plan = art(wf, "execution_plan") or {}
    existing = art(wf, "risk_register") or plan.get("risks") or []
    register = list(existing)
    # Enrich with stage scoring
    stage_scores = {
        "requirements": 4,
        "architecture": 12,
        "implementation": 8,
        "security": 16,
        "release": 10,
    }
    for r in register:
        r.setdefault("status", "OPEN")
        r.setdefault("owner", "risk")
    publish(wf, task, "risk_register", register)
    publish(
        wf,
        task,
        "risk_assessment",
        {
            "stage_scores": stage_scores,
            "top_risks": sorted(register, key=lambda x: x.get("score", 0), reverse=True)[:3],
            "gate_policy": "score>=15 → human approval required",
        },
    )
    return {"summary": f"Risk register: {len(register)} risks tracked"}
