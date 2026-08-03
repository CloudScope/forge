"""
Lambda handlers used by the Step Functions state machine.

Two tiny actions, deliberately kept out of the worker container so the state
machine can inspect and park a run without paying for a Fargate task:

    read_state  → is this run paused at a gate, finished, or failed?
    register    → park the task token so `POST /approve` can release the gate

The token is stored next to the workflow in DynamoDB, which is why an approval
submitted through the API can resume an execution the API never started.
"""

from __future__ import annotations

import logging
from typing import Any

from .execution import token_key
from .models import TaskStatus, WorkflowStatus
from .storage import WORKFLOWS, document_store

logger = logging.getLogger("forge.aws")
logging.getLogger().setLevel(logging.INFO)


def _open_gate(doc: dict[str, Any]) -> str | None:
    for task_id, task in (doc.get("tasks") or {}).items():
        if task.get("status") == TaskStatus.WAITING_APPROVAL.value:
            return task_id
    return None


def _pending_approval(doc: dict[str, Any]) -> dict[str, Any] | None:
    for approval in reversed(doc.get("approvals") or []):
        if approval.get("status") == "REQUESTED":
            return {
                "approval_id": approval.get("id"),
                "task_id": approval.get("task_id"),
                "title": approval.get("title"),
                "options": approval.get("options"),
            }
    return None


def read_state(workflow_id: str) -> dict[str, Any]:
    """Classify a run for the state machine's Choice state."""
    doc = document_store().get(WORKFLOWS, workflow_id)
    if doc is None:
        return {"workflow_id": workflow_id, "status": "FAILED", "reason": "not_found"}

    status = str(doc.get("status") or "")
    gate = _open_gate(doc)
    if status == WorkflowStatus.WAITING_APPROVAL.value or gate:
        return {
            "workflow_id": workflow_id,
            "status": "PAUSED",
            "gate": gate,
            "pending_approval": _pending_approval(doc),
        }
    if status in (WorkflowStatus.FAILED.value,):
        return {"workflow_id": workflow_id, "status": "FAILED"}
    return {"workflow_id": workflow_id, "status": status or "SUCCEEDED"}


def register_token(workflow_id: str, task_token: str, gate: str | None = None) -> dict[str, Any]:
    """Park the task token so the API can release this gate later."""
    document_store().put(
        WORKFLOWS,
        token_key(workflow_id),
        {"workflow_id": workflow_id, "task_token": task_token, "gate": gate},
    )
    logger.info("Registered approval token for %s at gate %s", workflow_id, gate)
    return {"workflow_id": workflow_id, "registered": True, "gate": gate}


def register_token_handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    """Single entry point; `action` selects the behaviour."""
    action = str(event.get("action") or "").strip()
    workflow_id = str(event.get("workflow_id") or "").strip()
    if not workflow_id:
        raise ValueError("workflow_id is required")

    if action == "read_state":
        return read_state(workflow_id)
    if action == "register":
        token = str(event.get("task_token") or "").strip()
        if not token:
            raise ValueError("task_token is required to register a gate")
        return register_token(workflow_id, token, event.get("gate"))
    raise ValueError(f"Unknown action: {action!r}")
