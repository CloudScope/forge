"""Compile the Forge SDLC StateGraph (scheduler + interrupt gates + compensate)."""

from __future__ import annotations

from typing import Any, Literal

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ..approval import build_request
from ..models import TaskStatus, WorkflowStatus
from ..reliability import mark_started
from .checkpointing import build_checkpointer
from .state import ForgeGraphState


def _pending_request(wf: Any, task_id: str) -> Any:
    return next(
        (
            a
            for a in reversed(wf.approvals)
            if a.task_id == task_id and a.status == "REQUESTED"
        ),
        None,
    )


def _approval_payload(req: Any) -> dict[str, Any]:
    return {
        "approval_id": req.id,
        "task_id": req.task_id,
        "title": req.title,
        "summary": req.summary,
        "options": req.options,
        "risk_tier": req.risk_tier.value
        if hasattr(req.risk_tier, "value")
        else str(req.risk_tier),
        "status": req.status,
    }


def build_forge_graph(
    sessions: dict[str, dict[str, Any]], checkpointer: Any = None
):
    """
    Build a compiled LangGraph that drives Forge's ready-set scheduler.

    Sessions map: workflow_id → {"engine": OrchestrationEngine, "wf": Workflow}
    Human gates use langgraph.types.interrupt (LangSmith shows the pause).
    `checkpointer` defaults to the durable SQLite saver.
    """

    def _session(workflow_id: str) -> tuple[Any, Any]:
        live = sessions.get(workflow_id)
        if not live or not live.get("engine") or not live.get("wf"):
            raise RuntimeError(f"No live LangGraph session for {workflow_id}")
        return live["engine"], live["wf"]

    def start_node(state: ForgeGraphState) -> dict[str, Any]:
        engine, wf = _session(state["workflow_id"])
        # NOTE: never clear engine._stop_requested here. `start` also runs on every
        # approval resume, so clearing would let a resume silently cancel an
        # operator safe-stop. Stops are lifted only by engine.clear_safe_stop().
        # Idempotent: rehydrate / resume after process restart skips re-mark.
        # Only skip banner when this is a mid-run resume (tasks already progressed).
        progressed = any(
            t.status
            not in (TaskStatus.PENDING, TaskStatus.READY)
            for t in wf.tasks.values()
        )
        if not progressed:
            mark_started(wf)
            engine.audit.append(
                wf,
                "WORKFLOW_STARTED",
                payload={
                    "playbook": wf.playbook_id,
                    "max_workers": engine.max_workers,
                    "runtime": "langgraph",
                },
            )
            print(
                f"\n▶ [LangGraph] Workflow {wf.id} playbook={wf.playbook_id} "
                f"workers={engine.max_workers}"
            )
        else:
            print(
                f"\n▶ [LangGraph] Resume {wf.id} status={wf.status.value} "
                f"checkpoints={wf.checkpoint_seq}"
            )
        if wf.status in (
            WorkflowStatus.CREATED,
            WorkflowStatus.PLANNING,
            WorkflowStatus.WAITING_APPROVAL,
        ):
            wf.status = WorkflowStatus.RUNNING
        return {
            "status": WorkflowStatus.RUNNING.value,
            "step": int(state.get("step") or 0),
            "done": False,
            "error": None,
            "pending_approval": None,
            "runtime": "langgraph",
            "playbook_id": wf.playbook_id,
        }

    def step_node(state: ForgeGraphState) -> dict[str, Any]:
        """
        One scheduler step, delegated to `OrchestrationEngine.tick`.

        The engine owns scheduling, gating and failure semantics for both runtimes;
        this node only translates the outcome into graph channels, and turns an open
        human gate into a LangGraph interrupt().
        """
        engine, wf = _session(state["workflow_id"])
        step = int(state.get("step") or 0) + 1

        outcome = engine.tick(wf, step)

        if outcome.action == "await_approval":
            node = wf.tasks[outcome.gate_task_id]
            req = _pending_request(wf, node.id)
            if req is None:
                req = build_request(wf, node)
                wf.approvals.append(req)
            engine._write_artifacts(wf)  # noqa: SLF001
            engine.store.checkpoint(wf)
            engine.store.save_workflow(wf)
            print(f"⏸ [LangGraph] interrupt at {node.id} — awaiting human approval")
            decision = interrupt(_approval_payload(req))
            return _apply_interrupt_decision(engine, wf, decision, step, node.id)

        if outcome.action == "done":
            return {
                "status": wf.status.value,
                "step": step,
                "done": True,
                "last_batch": outcome.batch,
                "error": outcome.error,
            }

        return {
            "status": WorkflowStatus.RUNNING.value,
            "step": step,
            "done": False,
            "last_batch": outcome.batch,
            "pending_approval": None,
        }

    def _apply_interrupt_decision(
        engine: Any,
        wf: Any,
        decision: Any,
        step: int,
        task_id: str | None,
    ) -> dict[str, Any]:
        if isinstance(decision, dict):
            dec = str(decision.get("decision") or "").strip()
            rationale = str(
                decision.get("rationale") or "Approved via LangGraph interrupt"
            )
            approval_id = decision.get("approval_id")
            task_id = decision.get("task_id") or task_id
        else:
            dec = str(decision or "").strip()
            rationale = "Approved via LangGraph interrupt"
            approval_id = None

        if not dec:
            raise ValueError("Approval resume missing decision")

        req = None
        if approval_id:
            req = next((a for a in wf.approvals if a.id == approval_id), None)
        if req is None and task_id:
            req = _pending_request(wf, task_id)
        if req is None:
            req = next(
                (a for a in reversed(wf.approvals) if a.status == "REQUESTED"), None
            )
        if req is None:
            raise ValueError("No pending approval request to resume")
        node = wf.tasks.get(req.task_id)
        if node is None:
            raise ValueError(f"Unknown approval task {req.task_id}")

        engine._apply_approval_decision(wf, node, req, dec, rationale)  # noqa: SLF001
        engine.store.checkpoint(wf)
        engine.store.save_workflow(wf)

        if wf.status == WorkflowStatus.FAILED:
            engine._rollback_on_failure(wf, f"approval_rejected:{node.id}")  # noqa: SLF001
            return {
                "status": wf.status.value,
                "step": step,
                "done": True,
                "last_batch": [node.id],
                "pending_approval": None,
                "error": node.error or "rejected",
            }

        wf.status = WorkflowStatus.RUNNING
        return {
            "status": WorkflowStatus.RUNNING.value,
            "step": step,
            "done": False,
            "last_batch": [node.id],
            "pending_approval": None,
        }

    def compensate_node(state: ForgeGraphState) -> dict[str, Any]:
        engine, wf = _session(state["workflow_id"])
        if wf.status != WorkflowStatus.FAILED:
            wf.status = WorkflowStatus.FAILED
        reason = state.get("error") or "langgraph_failed"
        engine._rollback_on_failure(wf, reason)  # noqa: SLF001
        return {"status": wf.status.value, "done": True}

    def finalize_node(state: ForgeGraphState) -> dict[str, Any]:
        engine, wf = _session(state["workflow_id"])
        engine.finalize_run(wf)
        engine.audit.append(wf, "RUNTIME_FINALIZED", payload={"runtime": "langgraph"})
        print(
            f"\n■ [LangGraph] Workflow {wf.status.value} | "
            f"checkpoints={wf.checkpoint_seq} | "
            f"cost=${wf.budgets['usd_spent']:.2f} | "
            f"tokens={int(wf.budgets['tokens'])}"
        )
        return {"status": wf.status.value, "done": True}

    def route_after_step(
        state: ForgeGraphState,
    ) -> Literal["step", "compensate", "finalize"]:
        if state.get("done"):
            if state.get("status") == WorkflowStatus.FAILED.value:
                # Compensation already ran in step on most failure paths;
                # still finalize metrics/artifacts.
                return "finalize"
            return "finalize"
        if state.get("status") == WorkflowStatus.FAILED.value:
            return "compensate"
        return "step"

    g = StateGraph(ForgeGraphState)
    g.add_node("start", start_node)
    g.add_node("step", step_node)
    g.add_node("compensate", compensate_node)
    g.add_node("finalize", finalize_node)
    g.add_edge(START, "start")
    g.add_edge("start", "step")
    g.add_conditional_edges(
        "step",
        route_after_step,
        {"step": "step", "compensate": "compensate", "finalize": "finalize"},
    )
    g.add_edge("compensate", "finalize")
    g.add_edge("finalize", END)

    if checkpointer is None:
        checkpointer, _ = build_checkpointer()
    return g.compile(checkpointer=checkpointer)
