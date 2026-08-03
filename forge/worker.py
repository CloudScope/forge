"""
Fargate worker: run one segment of a workflow.

A *segment* is the work between two human decisions. The task starts, rehydrates
the workflow from durable storage, ticks the engine until it either reaches a
human gate or a terminal state, syncs generated code to the object store, and
exits with a JSON verdict that Step Functions branches on:

    {"status": "PAUSED",  "gate": "approval.coding", ...}  → park on a task token
    {"status": "SUCCEEDED" | "FAILED" | "PARTIAL", ...}    → end the execution

Exiting between gates is what makes the deployment serverless: nothing is billed
while a workflow waits for a human, and no process holds state across the pause.

    python -m forge.worker --workflow-id wf_abc [--decision approve --rationale ok]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from .models import WorkflowStatus
from .storage import sync_workspace_down, sync_workspace_up

logger = logging.getLogger("forge.worker")

TERMINAL = {
    WorkflowStatus.SUCCEEDED,
    WorkflowStatus.FAILED,
    WorkflowStatus.PARTIAL,
}


class WorkflowNotFound(LookupError):
    """Raised when a segment is asked to advance a workflow that is not stored."""

    def __init__(self, workflow_id: str):
        super().__init__(f"Unknown workflow: {workflow_id}")
        self.workflow_id = workflow_id


def _open_gate(wf: Any) -> str | None:
    from .models import TaskStatus

    for task in wf.tasks.values():
        if task.status == TaskStatus.WAITING_APPROVAL:
            return task.id
    return None


def _pending_approval(wf: Any) -> dict[str, Any] | None:
    request = next((a for a in reversed(wf.approvals) if a.status == "REQUESTED"), None)
    if request is None:
        return None
    return {
        "approval_id": request.id,
        "task_id": request.task_id,
        "title": request.title,
        "summary": request.summary,
        "options": request.options,
        "risk_tier": request.risk_tier.value,
    }


def run_segment(
    workflow_id: str,
    *,
    decision: str | None = None,
    rationale: str = "",
    approval_id: str | None = None,
    task_id: str | None = None,
    max_workers: int = 4,
) -> dict[str, Any]:
    """Execute one segment and return the verdict for the state machine."""
    from .graph.runtime import LangGraphRuntime, use_langgraph

    # Bring any previously generated code back onto local disk: later agents
    # (security scan, tests, docs) read the workspace the build agents wrote.
    restored = sync_workspace_down(workflow_id)
    if restored:
        logger.info("Restored %s workspace files for %s", restored, workflow_id)

    if use_langgraph():
        runtime = LangGraphRuntime(
            auto_approve=True, max_workers=max_workers, cli_demo_mode=False
        )
        engine = runtime.engine
    else:
        from .engine import OrchestrationEngine

        runtime = None
        engine = OrchestrationEngine(
            auto_approve=True, max_workers=max_workers, cli_demo_mode=False
        )

    wf = (runtime.rehydrate(workflow_id) if runtime else engine.rehydrate(workflow_id))
    if wf is None:
        # A plain exception, never SystemExit: `main` must always be able to turn
        # a failure into the JSON verdict the state machine branches on.
        raise WorkflowNotFound(workflow_id)

    # A decision is only applied when a gate is actually open. The API records
    # the decision before releasing the token, so by the time this segment runs
    # the gate is usually already closed — re-applying would raise "No pending
    # approval request" and fail an otherwise healthy segment.
    if decision and _pending_approval(wf) is not None:
        # Resuming after a human decision released this segment.
        if runtime:
            runtime.resume_approval(
                wf,
                decision=decision,
                rationale=rationale,
                approval_id=approval_id,
                task_id=task_id,
            )
        else:
            engine.submit_approval(
                wf,
                decision=decision,
                rationale=rationale,
                approval_id=approval_id,
                task_id=task_id,
            )
    elif runtime:
        runtime.start(wf)
    else:
        engine.run(wf)

    uploaded = sync_workspace_up(workflow_id)
    if uploaded:
        logger.info("Synced %s workspace files for %s", uploaded, workflow_id)

    gate = _open_gate(wf)
    if wf.status == WorkflowStatus.WAITING_APPROVAL or gate:
        return {
            "workflow_id": workflow_id,
            "status": "PAUSED",
            "gate": gate,
            "pending_approval": _pending_approval(wf),
            "checkpoints": wf.checkpoint_seq,
        }

    return {
        "workflow_id": workflow_id,
        "status": wf.status.value,
        "gate": None,
        "checkpoints": wf.checkpoint_seq,
        "metrics": wf.metrics,
    }


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    from .secrets import hydrate

    hydrate()
    parser = argparse.ArgumentParser(prog="forge-worker")
    parser.add_argument("--workflow-id", required=True)
    parser.add_argument("--decision", default=None, help="Human decision resuming a gate")
    parser.add_argument("--rationale", default="")
    parser.add_argument("--approval-id", default=None)
    parser.add_argument("--task-id", default=None)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args(argv)

    try:
        verdict = run_segment(
            args.workflow_id,
            decision=args.decision,
            rationale=args.rationale,
            approval_id=args.approval_id,
            task_id=args.task_id,
            max_workers=args.workers,
        )
    except Exception as exc:  # noqa: BLE001 — the state machine needs a verdict
        logger.exception("Segment failed for %s", args.workflow_id)
        verdict = {
            "workflow_id": args.workflow_id,
            "status": "FAILED",
            "error": str(exc),
        }

    # Step Functions reads the last line of stdout via the container's log stream;
    # emitting a single JSON line keeps the contract explicit.
    print(json.dumps(verdict))
    return 0 if verdict.get("status") != "FAILED" else 1


if __name__ == "__main__":
    sys.exit(main())
