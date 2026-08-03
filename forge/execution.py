"""
How a workflow run is executed.

Locally a run is a background thread inside the API process. That model cannot
survive a serverless control plane: a Lambda returns as soon as the response is
written, and any scale-in kills in-flight threads. On AWS a run is instead a Step
Functions execution that alternates between two things:

    RunTask (Fargate)  →  the engine ticks until a gate or a terminal state
    waitForTaskToken   →  the execution parks, free, until a human decides

Step Functions holds a task token for up to a year, which is the only primitive
that matches a workflow paused at `approval.coding` overnight.

Both launchers satisfy the same interface, so `dashboard.py` never branches on
the deployment target.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Callable, Protocol

from .storage import WORKFLOWS, document_store

logger = logging.getLogger("forge.execution")

# Where a pending Step Functions task token is parked between segments.
TOKEN_KEY_SUFFIX = "#approval-token"


def token_key(workflow_id: str) -> str:
    return f"{workflow_id}{TOKEN_KEY_SUFFIX}"


class WorkflowLauncher(Protocol):
    """Starts and resumes workflow runs."""

    name: str

    def start(self, workflow_id: str, *, runner: Callable[[], Any] | None = None) -> dict[str, Any]: ...

    def resume(self, workflow_id: str, decision: dict[str, Any]) -> dict[str, Any]: ...


class ThreadLauncher:
    """In-process execution: a daemon thread per run. Local development and CLI."""

    name = "thread"

    def __init__(self, on_state: Callable[[str, str], None] | None = None):
        self._on_state = on_state or (lambda _wf, _state: None)

    def start(
        self, workflow_id: str, *, runner: Callable[[], Any] | None = None
    ) -> dict[str, Any]:
        if runner is None:
            raise ValueError("ThreadLauncher.start requires a runner callable")

        def _target() -> None:
            self._on_state(workflow_id, "RUNNING")
            try:
                runner()
                self._on_state(workflow_id, "FINISHED")
            except Exception as exc:  # noqa: BLE001 — surfaced via run state
                logger.exception("Workflow %s failed", workflow_id)
                self._on_state(workflow_id, f"FAILED:{exc}")

        threading.Thread(
            target=_target, daemon=True, name=f"forge-{workflow_id}"
        ).start()
        return {"mode": self.name, "workflow_id": workflow_id, "started": True}

    def resume(self, workflow_id: str, decision: dict[str, Any]) -> dict[str, Any]:
        # The caller resumes in-process (LangGraph interrupt or engine.submit_approval).
        return {"mode": self.name, "workflow_id": workflow_id, "handled": False}


class StepFunctionsLauncher:
    """
    Step Functions execution, one Fargate task per segment.

    `start` begins an execution. `resume` hands the human decision back to the
    parked `waitForTaskToken` state, which releases the next segment.
    """

    name = "stepfunctions"

    def __init__(
        self,
        state_machine_arn: str | None = None,
        *,
        client: Any = None,
        docs: Any = None,
    ):
        self.state_machine_arn = state_machine_arn or os.environ.get(
            "FORGE_STATE_MACHINE_ARN", ""
        )
        if not self.state_machine_arn:
            raise RuntimeError(
                "FORGE_EXECUTION=stepfunctions requires FORGE_STATE_MACHINE_ARN"
            )
        self._client = client
        self.docs = docs or document_store()

    @property
    def client(self):
        if self._client is None:
            import boto3

            self._client = boto3.client("stepfunctions")
        return self._client

    def start(
        self, workflow_id: str, *, runner: Callable[[], Any] | None = None
    ) -> dict[str, Any]:
        # `runner` is ignored: execution happens in a Fargate task, not in this process.
        response = self.client.start_execution(
            stateMachineArn=self.state_machine_arn,
            name=_execution_name(workflow_id),
            input=json.dumps({"workflow_id": workflow_id, "segment": 1}),
        )
        arn = response["executionArn"]
        logger.info("Started execution %s for %s", arn, workflow_id)
        return {
            "mode": self.name,
            "workflow_id": workflow_id,
            "started": True,
            "execution_arn": arn,
        }

    def register_token(self, workflow_id: str, task_token: str) -> None:
        """Called from the state machine when a run parks at a human gate."""
        self.docs.put(
            WORKFLOWS,
            token_key(workflow_id),
            {"workflow_id": workflow_id, "task_token": task_token},
        )

    def pending_token(self, workflow_id: str) -> str | None:
        doc = self.docs.get(WORKFLOWS, token_key(workflow_id))
        return (doc or {}).get("task_token")

    def resume(self, workflow_id: str, decision: dict[str, Any]) -> dict[str, Any]:
        token = self.pending_token(workflow_id)
        if not token:
            # No parked execution — the caller falls back to resuming in-process.
            return {"mode": self.name, "workflow_id": workflow_id, "handled": False}
        self.client.send_task_success(taskToken=token, output=json.dumps(decision))
        self.docs.delete(WORKFLOWS, token_key(workflow_id))
        logger.info("Released gate for %s via task token", workflow_id)
        return {"mode": self.name, "workflow_id": workflow_id, "handled": True}


def _execution_name(workflow_id: str) -> str:
    """Execution names must be unique per state machine and ≤80 chars."""
    import time

    return f"{workflow_id}-{int(time.time())}"[:80]


_launcher: WorkflowLauncher | None = None
_lock = threading.Lock()


def execution_mode() -> str:
    return (os.environ.get("FORGE_EXECUTION") or "thread").strip().lower()


def using_step_functions() -> bool:
    return execution_mode() in {"stepfunctions", "sfn", "aws"}


def get_launcher(on_state: Callable[[str, str], None] | None = None) -> WorkflowLauncher:
    global _launcher
    if _launcher is None:
        with _lock:
            if _launcher is None:
                _launcher = (
                    StepFunctionsLauncher()
                    if using_step_functions()
                    else ThreadLauncher(on_state)
                )
    return _launcher


def reset() -> None:
    global _launcher
    with _lock:
        _launcher = None


def health() -> dict[str, Any]:
    if using_step_functions():
        return {
            "mode": "stepfunctions",
            "state_machine_arn": os.environ.get("FORGE_STATE_MACHINE_ARN"),
        }
    return {"mode": "thread"}
