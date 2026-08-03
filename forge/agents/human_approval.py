from __future__ import annotations

from typing import Any

from ..models import TaskNode, Workflow


def human_approval_present(wf: Workflow, task: TaskNode) -> dict[str, Any]:
    """Human Approval Agent — present gate; decision captured by orchestrator."""
    return {"summary": f"Awaiting human decision for {task.id}"}


def noop_barrier(wf: Workflow, task: TaskNode) -> dict[str, Any]:
    """Synchronization barrier — no compute, deps already satisfied."""
    return {"summary": "Barrier satisfied"}
