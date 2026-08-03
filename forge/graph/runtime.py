"""LangGraphRuntime — Studio/CLI façade over the Forge StateGraph."""

from __future__ import annotations

import os
import threading
from typing import Any, Optional

from ..engine import OrchestrationEngine
from ..models import Workflow, WorkflowStatus
from .build import build_forge_graph
from .checkpointing import build_checkpointer
from .tracing import configure_langsmith, run_config


def langgraph_available() -> bool:
    try:
        import langgraph  # noqa: F401
        from langgraph.types import Command, interrupt  # noqa: F401

        return True
    except Exception:
        return False


def use_langgraph() -> bool:
    """
    Prefer LangGraph when installed unless FORGE_ORCHESTRATOR=legacy.

    Default: langgraph (when available).
    """
    choice = (os.environ.get("FORGE_ORCHESTRATOR") or "langgraph").strip().lower()
    if choice in {"legacy", "engine", "classic"}:
        return False
    return langgraph_available()


class LangGraphRuntime:
    """
    Drives a Workflow through LangGraph with LangSmith tracing.

    Human gates use interrupt(); Studio resumes via resume_approval().
    Agents remain the existing REGISTRY callables on OrchestrationEngine.
    """

    def __init__(
        self,
        engine: Optional[OrchestrationEngine] = None,
        *,
        auto_approve: bool = True,
        max_workers: int = 4,
        cli_demo_mode: bool = False,
        allow_stdin_prompt: bool = False,
    ):
        if not langgraph_available():
            raise RuntimeError(
                "langgraph is not installed. pip install -r requirements.txt"
            )
        self.engine = engine or OrchestrationEngine(
            auto_approve=auto_approve,
            max_workers=max_workers,
            cli_demo_mode=cli_demo_mode,
            allow_stdin_prompt=allow_stdin_prompt,
        )
        self.auto_approve = self.engine.auto_approve
        self.max_workers = self.engine.max_workers
        self._sessions: dict[str, dict[str, Any]] = {}
        self.checkpointer, self.checkpointer_info = build_checkpointer()
        self._graph = build_forge_graph(self._sessions, self.checkpointer)
        self._lock = threading.RLock()
        self.langsmith = configure_langsmith()

    def register(self, wf: Workflow) -> None:
        self._sessions[wf.id] = {"engine": self.engine, "wf": wf}

    def prepare_from_document(self, **kwargs: Any) -> Workflow:
        wf = self.engine.prepare_from_document(**kwargs)
        self.register(wf)
        return wf

    def run_from_document_bytes(self, *args: Any, **kwargs: Any) -> Workflow:
        wf = self.engine.run_from_document_bytes(*args, **kwargs)
        # Legacy helper already calls engine.run — for LangGraph prefer prepare+run_graph
        return wf

    def start(self, wf: Workflow) -> dict[str, Any]:
        """Invoke the graph until completion or human interrupt."""
        with self._lock:
            self.register(wf)
            cfg = run_config(wf.id)
            result = self._graph.invoke(
                {
                    "workflow_id": wf.id,
                    "playbook_id": wf.playbook_id,
                    "status": wf.status.value,
                    "step": 0,
                    "done": False,
                    "runtime": "langgraph",
                },
                cfg,
            )
            return self._normalize_result(wf, result)

    def resume_approval(
        self,
        wf: Workflow,
        *,
        decision: str,
        rationale: str = "",
        approval_id: str | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """Resume a paused interrupt gate (Studio Approve / Reject)."""
        from langgraph.types import Command

        payload = {
            "decision": decision,
            "rationale": rationale or "Approved in Forge Studio (LangGraph)",
            "approval_id": approval_id,
            "task_id": task_id,
        }
        with self._lock:
            self.register(wf)
            cfg = run_config(wf.id)
            # Prefer LangGraph interrupt resume when the checkpointer still has it.
            if self.get_interrupt(wf.id) is not None:
                result = self._graph.invoke(Command(resume=payload), cfg)
                return self._normalize_result(wf, result)

            # No live interrupt (checkpoint pruned, or an in-memory saver lost it on
            # restart): apply the decision to the Workflow directly and drive on.
            req = None
            if approval_id:
                req = next((a for a in wf.approvals if a.id == approval_id), None)
            if req is None and task_id:
                req = next(
                    (
                        a
                        for a in reversed(wf.approvals)
                        if a.task_id == task_id and a.status == "REQUESTED"
                    ),
                    None,
                )
            if req is None:
                req = next(
                    (a for a in reversed(wf.approvals) if a.status == "REQUESTED"), None
                )
            if req is None:
                raise ValueError("No pending approval request")
            node = wf.tasks.get(req.task_id)
            if node is None:
                raise ValueError(f"Unknown approval task {req.task_id}")
            self.engine._apply_approval_decision(  # noqa: SLF001
                wf, node, req, decision, rationale
            )
            self.engine.store.checkpoint(wf)
            self.engine.store.save_workflow(wf)
            if wf.status == WorkflowStatus.FAILED:
                self.engine._rollback_on_failure(  # noqa: SLF001
                    wf, f"approval_rejected:{node.id}"
                )
                return self._normalize_result(
                    wf, {"status": wf.status.value, "done": True, "error": node.error}
                )
            result = self._graph.invoke(
                {
                    "workflow_id": wf.id,
                    "playbook_id": wf.playbook_id,
                    "status": wf.status.value,
                    "step": 0,
                    "done": False,
                    "runtime": "langgraph",
                },
                cfg,
            )
            return self._normalize_result(wf, result)

    def request_safe_stop(self) -> None:
        self.engine.request_safe_stop()

    def rehydrate(self, workflow_id: str) -> Workflow | None:
        wf = self.engine.rehydrate(workflow_id)
        if wf is not None:
            self.register(wf)
        return wf

    def get_interrupt(self, workflow_id: str) -> dict[str, Any] | None:
        cfg = run_config(workflow_id)
        try:
            st = self._graph.get_state(cfg)
        except Exception:
            return None
        tasks = getattr(st, "tasks", None) or ()
        for task in tasks:
            interrupts = getattr(task, "interrupts", None) or ()
            for intr in interrupts:
                val = getattr(intr, "value", None)
                if isinstance(val, dict):
                    return val
        return None

    def _normalize_result(self, wf: Workflow, result: dict[str, Any]) -> dict[str, Any]:
        interrupted = bool(result.get("__interrupt__"))
        if interrupted:
            wf.status = WorkflowStatus.WAITING_APPROVAL
            self.engine.store.save_workflow(wf)
            intr = None
            raw = result.get("__interrupt__") or []
            if raw:
                first = raw[0]
                intr = getattr(first, "value", None) or first
            return {
                "workflow_id": wf.id,
                "status": wf.status.value,
                "interrupted": True,
                "pending_approval": intr if isinstance(intr, dict) else None,
                "runtime": "langgraph",
                "langsmith": self.langsmith,
            }
        status = result.get("status") or wf.status.value
        return {
            "workflow_id": wf.id,
            "status": status,
            "interrupted": False,
            "done": bool(result.get("done")),
            "step": result.get("step"),
            "last_batch": result.get("last_batch"),
            "error": result.get("error"),
            "runtime": "langgraph",
            "langsmith": self.langsmith,
        }
