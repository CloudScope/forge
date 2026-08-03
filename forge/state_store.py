from __future__ import annotations

import time
from typing import Any

from .models import (
    ApprovalRequest,
    NodeType,
    RiskTier,
    TaskNode,
    TaskStatus,
    Workflow,
    WorkflowStatus,
)
from .storage import CHECKPOINTS, WORKFLOWS, document_store


def workflow_document(wf: Workflow) -> dict[str, Any]:
    """Serialise a Workflow for durable storage (no engine objects)."""
    return {
        "id": wf.id,
        "playbook_id": wf.playbook_id,
        "status": wf.status.value,
        "facts": wf.facts,
        "budgets": wf.budgets,
        "metrics": wf.metrics,
        "checkpoint_seq": wf.checkpoint_seq,
        "tasks": {
            tid: {
                "id": t.id,
                "agent": t.agent,
                "type": t.type.value,
                "deps": t.deps,
                "risk_tier": t.risk_tier.value,
                "description": t.description,
                "status": t.status.value,
                "attempt": t.attempt,
                "error": t.error,
                "outputs": t.outputs,
                "started_at": t.started_at,
                "finished_at": t.finished_at,
            }
            for tid, t in wf.tasks.items()
        },
        "approvals": [
            {
                "id": a.id,
                "task_id": a.task_id,
                "risk_tier": a.risk_tier.value,
                "title": a.title,
                "summary": a.summary,
                "options": a.options,
                "status": a.status,
                "decision": a.decision,
                "rationale": a.rationale,
            }
            for a in wf.approvals
        ],
        "events": wf.events[-200:],
        "updated_at": time.time(),
    }


def workflow_from_document(data: dict[str, Any]) -> Workflow:
    """Rehydrate a Workflow from durable storage."""
    wf = Workflow(
        id=data["id"],
        playbook_id=data.get("playbook_id") or "production_sdlc",
        status=WorkflowStatus(data.get("status") or "CREATED"),
        facts=dict(data.get("facts") or {}),
        budgets=dict(data.get("budgets") or {"usd_spent": 0.0, "tokens": 0.0}),
        metrics=dict(data.get("metrics") or {}),
        checkpoint_seq=int(data.get("checkpoint_seq") or 0),
        events=list(data.get("events") or []),
    )
    for tid, t in (data.get("tasks") or {}).items():
        wf.tasks[tid] = TaskNode(
            id=t.get("id") or tid,
            agent=t.get("agent") or "barrier",
            type=NodeType(t.get("type") or "COMPUTE"),
            deps=list(t.get("deps") or []),
            risk_tier=RiskTier(t.get("risk_tier") or "LOW"),
            description=t.get("description") or "",
            status=TaskStatus(t.get("status") or "PENDING"),
            attempt=int(t.get("attempt") or 0),
            error=t.get("error"),
            outputs=dict(t.get("outputs") or {}),
            started_at=t.get("started_at"),
            finished_at=t.get("finished_at"),
        )
    for a in data.get("approvals") or []:
        wf.approvals.append(
            ApprovalRequest(
                id=a.get("id") or f"appr_{(a.get('task_id') or 'x')[:8]}",
                task_id=a.get("task_id") or "",
                risk_tier=RiskTier(a.get("risk_tier") or "HIGH"),
                title=a.get("title") or "",
                summary=a.get("summary") or "",
                options=list(a.get("options") or []),
                status=a.get("status") or "REQUESTED",
                decision=a.get("decision"),
                rationale=a.get("rationale") or "",
            )
        )
    return wf


class StateStore:
    """
    Durable workflow state and checkpoints.

    Backed by the configured storage layer: JSON files under `FORGE_VAR_ROOT`
    locally, DynamoDB in AWS. The engine never sees the difference.
    """

    def __init__(self, root: Any = None):
        # `root` is retained for call-site compatibility; the storage factory owns
        # placement now and passing it does not override the configured backend.
        self.root = root
        self.docs = document_store()

    def emit(self, wf: Workflow, event_type: str, payload: dict[str, Any] | None = None) -> None:
        wf.events.append(
            {
                "ts": time.time(),
                "type": event_type,
                "workflow_id": wf.id,
                "payload": payload or {},
            }
        )

    def checkpoint(self, wf: Workflow) -> str:
        wf.checkpoint_seq += 1
        snapshot = {
            "workflow_id": wf.id,
            "seq": wf.checkpoint_seq,
            "status": wf.status.value,
            "facts": wf.facts,
            "budgets": wf.budgets,
            "metrics": wf.metrics,
            "tasks": {
                tid: {
                    "status": t.status.value,
                    "attempt": t.attempt,
                    "error": t.error,
                    "outputs_keys": list(t.outputs.keys()),
                }
                for tid, t in wf.tasks.items()
            },
            "artifacts": {
                k: {"version": a.version, "task_id": a.task_id, "hash": a.content_hash}
                for k, a in wf.artifacts.items()
            },
        }
        key = f"{wf.id}_seq{wf.checkpoint_seq:04d}"
        self.docs.put(CHECKPOINTS, key, snapshot)
        self.emit(wf, "CHECKPOINT", {"seq": wf.checkpoint_seq, "key": key})
        self.save_workflow(wf)
        return key

    def save_workflow(self, wf: Workflow) -> str:
        self.docs.put(WORKFLOWS, wf.id, workflow_document(wf))
        return wf.id

    def load_workflow(self, workflow_id: str) -> Workflow | None:
        data = self.docs.get(WORKFLOWS, workflow_id)
        return workflow_from_document(data) if data else None

    def load_document(self, workflow_id: str) -> dict[str, Any] | None:
        """Raw stored document — for read-only API responses that never mutate."""
        return self.docs.get(WORKFLOWS, workflow_id)

    def list_workflow_documents(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        return self.docs.list_docs(WORKFLOWS, limit=limit)

    def delete_workflow(self, workflow_id: str) -> bool:
        removed = self.docs.delete(WORKFLOWS, workflow_id)
        for key in self.docs.list_keys(CHECKPOINTS):
            if key.startswith(f"{workflow_id}_seq"):
                self.docs.delete(CHECKPOINTS, key)
        return removed

    def delete_everything(self) -> dict[str, int]:
        return {
            "workflows": self.docs.delete_all(WORKFLOWS),
            "checkpoints": self.docs.delete_all(CHECKPOINTS),
        }
