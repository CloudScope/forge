from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from . import agents as agent_mod
from .agents._common import content_hash
from .approval import auto_decide, build_request
from .audit import AuditTraceStore
from .compensation import run_compensation_saga
from .doc_ingest import extract_text, save_upload, summarize_document
from .memory import MemoryContextStore
from .models import (
    Artifact,
    NodeType,
    RiskTier,
    TaskNode,
    TaskStatus,
    Workflow,
    WorkflowStatus,
    new_id,
)
from .reliability import (
    close_failure_window,
    finalize as finalize_metrics,
    mark_started,
    open_failure_window,
    record_compensation,
    record_parallel_batch,
    record_retry,
    record_safe_stop,
)
from .replan import graft_nodes, invalidate
from .state_store import StateStore
from .storage import artifact_prefix, object_store
from .validation import evaluate, overall_pass, summarize


from .core.paths import paths as forge_paths


@dataclass
class TickOutcome:
    """Result of one scheduler step, interpreted by whichever driver called `tick`."""

    action: str  # "continue" | "await_approval" | "done"
    step: int
    batch: list[str] = field(default_factory=list)
    error: Optional[str] = None
    gate_task_id: Optional[str] = None


_P = forge_paths()
ROOT = _P.root
PLAYBOOKS = _P.playbooks
ARTIFACTS = _P.artifacts
STATE = _P.state

# Per-agent consecutive failures before circuit opens (fail-fast / escalate).
CIRCUIT_FAILURE_THRESHOLD = 3

# Agent roles whose output is invalidated when the security gate fails.
SECURITY_REPLAN_AGENTS = frozenset(
    {"security", "security_scan", "backend", "testing"}
)


class OrchestrationEngine:
    """
    Orchestrator Agent — state machine + dependency DAG scheduler.

    Coordinates specialized agents, human gates, memory, and audit trails.
    Supports parallel ready-set workers, saga compensation, and reliability metrics.
    """

    def __init__(
        self,
        auto_approve: bool = True,
        max_workers: int = 4,
        *,
        cli_demo_mode: bool = False,
        allow_stdin_prompt: bool = False,
    ):
        self.auto_approve = auto_approve
        # CLI unattended demos may auto-decide plan/arch; Studio must not.
        self.cli_demo_mode = cli_demo_mode
        # Only CLI --interactive may block on stdin; Studio always uses the modal/API.
        self.allow_stdin_prompt = allow_stdin_prompt
        self.max_workers = max(1, max_workers)
        self.store = StateStore()
        self.memory = MemoryContextStore()
        self.audit = AuditTraceStore()
        self.objects = object_store()
        self._lock = threading.RLock()
        self._stop_requested = False
        self._agent_failures: dict[str, int] = {}
        ARTIFACTS.mkdir(parents=True, exist_ok=True)

    def request_safe_stop(self) -> None:
        """Operator safe-stop: finish in-flight work, skip new scheduling, checkpoint."""
        self._stop_requested = True

    def clear_safe_stop(self) -> None:
        """Explicitly lift a safe-stop. Never called implicitly by run/resume —
        a stop requested while paused at a gate must survive the resume."""
        self._stop_requested = False

    def load_playbook(self, name: str) -> dict[str, Any]:
        path = PLAYBOOKS / f"{name}.yaml"
        with path.open(encoding="utf-8") as f:
            return yaml.safe_load(f)

    def plan(self, playbook_name: str, facts: Optional[dict[str, Any]] = None) -> Workflow:
        pb = self.load_playbook(playbook_name)
        # A fresh plan is an explicit operator start — safe to lift a prior stop.
        self.clear_safe_stop()
        wf = Workflow(id=new_id("wf"), playbook_id=pb["id"], status=WorkflowStatus.PLANNING)
        if facts:
            wf.facts.update(facts)
        for n in pb["nodes"]:
            node = TaskNode(
                id=n["id"],
                agent=n.get("agent", "barrier"),
                type=NodeType(n.get("type", "COMPUTE")),
                deps=list(n.get("deps", [])),
                risk_tier=RiskTier(n.get("risk_tier", "LOW")),
                description=n.get("description", ""),
                condition=n.get("condition"),
            )
            if node.id.startswith("validate."):
                node.max_attempts = 1
            wf.tasks[node.id] = node
        self.audit.append(
            wf, "PLANNED", payload={"playbook": pb["id"], "nodes": len(wf.tasks)}
        )
        wf.status = WorkflowStatus.RUNNING
        self.store.checkpoint(wf)
        return wf

    def _seed_artifact(self, wf: Workflow, key: str, content: Any, task_id: str = "ingest") -> None:
        art = Artifact(
            key=key,
            version=1,
            task_id=task_id,
            content=content,
            content_hash=content_hash(content),
        )
        wf.artifacts[key] = art
        wf.artifact_history.append(art)

    def prepare_from_document(
        self,
        *,
        text: str,
        filename: str,
        summary: Optional[dict[str, Any]] = None,
    ) -> Workflow:
        """16. Workflow Orchestrator — plan DAG seeded from an uploaded PRD."""
        summary = summary or summarize_document(text)
        wf = self.plan(
            "production_sdlc",
            facts={
                "from_document": True,
                "requirement_text": text,
                "requirement_filename": filename,
                "document_summary": summary,
                "product_name": summary.get("product_name"),
                "feature_qr": "qr_code" in (summary.get("features") or []),
                "hld_aligned": True,
            },
        )
        self._seed_artifact(
            wf,
            "raw_requirement",
            {"filename": filename, "text": text, "char_count": len(text)},
        )
        self._seed_artifact(wf, "document_summary", summary)
        self.audit.append(
            wf,
            "DOCUMENT_INGESTED",
            payload={
                "filename": filename,
                "product_name": summary.get("product_name"),
                "features": summary.get("features"),
                "fr_lines": len(summary.get("fr_lines") or []),
            },
        )
        self.store.checkpoint(wf)
        return wf

    def run_from_document_bytes(
        self, filename: str, data: bytes, *, persist_upload: bool = True
    ) -> Workflow:
        path = save_upload(filename, data) if persist_upload else Path(filename)
        if persist_upload:
            text = extract_text(path, data)
        else:
            text = extract_text(Path(filename), data)
        summary = summarize_document(text)
        wf = self.prepare_from_document(text=text, filename=filename, summary=summary)
        return self.run(wf)

    def _deps_satisfied(self, wf: Workflow, node: TaskNode) -> bool:
        for d in node.deps:
            dep = wf.tasks.get(d)
            if dep is None:
                return False
            if dep.status == TaskStatus.SKIPPED:
                continue
            if dep.status != TaskStatus.SUCCEEDED:
                return False
        return True

    def _condition_ok(self, wf: Workflow, node: TaskNode) -> bool:
        if not node.condition:
            return True
        if node.condition == "true":
            return True
        if node.condition == "false":
            return False
        if node.condition.startswith("fact:"):
            expr = node.condition[5:]
            if "==" in expr:
                key, val = expr.split("==", 1)
                actual = wf.facts.get(key.strip())
                expected = val.strip().lower()
                if expected in ("true", "false"):
                    return bool(actual) == (expected == "true")
                return str(actual) == val.strip()
            return bool(wf.facts.get(expr.strip()))
        return True

    def ready_nodes(self, wf: Workflow) -> list[TaskNode]:
        ready = []
        for node in wf.tasks.values():
            if node.status not in (TaskStatus.PENDING, TaskStatus.READY, TaskStatus.INVALIDATED):
                continue
            if node.status == TaskStatus.INVALIDATED:
                node.status = TaskStatus.PENDING
            # Only evaluate conditions once deps are met — otherwise early skip
            # can erase gates that depend on upstream facts (e.g. ambiguity).
            if not self._deps_satisfied(wf, node):
                continue
            if not self._condition_ok(wf, node):
                node.status = TaskStatus.SKIPPED
                self.audit.append(
                    wf, "TASK_SKIPPED", task_id=node.id, payload={"reason": "condition"}
                )
                continue
            node.status = TaskStatus.READY
            ready.append(node)
        ready.sort(
            key=lambda n: (
                {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}[n.risk_tier.value],
                n.id,
            )
        )
        return ready

    def _run_compute(self, wf: Workflow, node: TaskNode) -> None:
        """Run a compute agent. Heavy work happens outside `_lock` for true parallelism."""
        with self._lock:
            node.status = TaskStatus.RUNNING
            node.attempt += 1
            node.started_at = time.time()
            bundle = self.memory.context_bundle(wf, node.id, node.agent)
            self.audit.append(
                wf,
                "TASK_STARTED",
                task_id=node.id,
                agent=node.agent,
                payload={"context_keys": list(bundle.keys())},
            )
            fn = agent_mod.REGISTRY.get(node.agent)
            if fn is None:
                raise RuntimeError(f"Unknown agent: {node.agent}")

        # Agent body (incl. LLM I/O) runs without holding the orchestrator lock.
        result = fn(wf, node)

        with self._lock:
            for evt in wf.events[-8:]:
                if evt.get("type") not in ("LLM_COMPLETED", "LLM_FALLBACK"):
                    continue
                payload = evt.get("payload") or {}
                if payload.get("task_id") and payload.get("task_id") != node.id:
                    continue
                self.audit.append(
                    wf,
                    evt["type"],
                    task_id=node.id,
                    agent=node.agent,
                    payload=payload,
                )
            self.memory.remember_agent(
                node.agent,
                kind="task_result",
                content={
                    "summary": result.get("summary"),
                    "mode": result.get("mode", "heuristic"),
                },
                citation=f"workflow://{wf.id}/task/{node.id}",
            )
            if result.get("escalate") == "clarify":
                node.status = TaskStatus.WAITING_INPUT
                node.finished_at = time.time()
                self.audit.append(
                    wf, "NEEDS_CLARIFICATION", task_id=node.id, agent=node.agent
                )
                return
            node.status = TaskStatus.SUCCEEDED
            node.finished_at = time.time()
            self.audit.append(
                wf,
                "TASK_SUCCEEDED",
                task_id=node.id,
                agent=node.agent,
                payload={
                    "summary": result.get("summary"),
                    "outputs": list(node.outputs),
                },
            )

    def _run_barrier(self, wf: Workflow, node: TaskNode) -> None:
        node.status = TaskStatus.RUNNING
        agent_mod.noop_barrier(wf, node)
        node.status = TaskStatus.SUCCEEDED
        self.audit.append(wf, "BARRIER_PASSED", task_id=node.id, agent=node.agent)

    def _apply_approval_decision(
        self,
        wf: Workflow,
        node: TaskNode,
        req: Any,
        decision: str,
        rationale: str,
    ) -> None:
        """Finalize an approval request (approve → continue, else fail)."""
        from .models import ApprovalRequest

        assert isinstance(req, ApprovalRequest)
        req.decision = decision
        req.rationale = rationale
        from .approval_gates import is_approve_decision

        if is_approve_decision(decision):
            req.status = "APPROVED"
            if node.id.startswith("approval.clarify"):
                wf.facts["needs_clarification"] = False
                wf.facts["ambiguous_brief"] = False
                wf.facts["clarification_accepted"] = True
                if decision in {"A", "B", "C"}:
                    wf.facts["analytics_option"] = decision
                    self._expand_analytics(wf, decision)
            if node.id.startswith("approval.coding"):
                wf.facts["coding_approved"] = True
                wf.facts["coding_complete"] = True
                if decision == "open_workspace":
                    wf.facts["open_workspace_after_coding"] = True
            if node.id.startswith("approval.figma"):
                if decision in {"figma_uploaded"} or wf.facts.get("figma_provided"):
                    wf.facts["figma_provided"] = bool(
                        wf.facts.get("figma_provided")
                        or wf.facts.get("figma_files")
                        or wf.facts.get("figma_url")
                    )
                    wf.facts["figma_mode"] = (
                        "figma" if wf.facts.get("figma_provided") else "agent_design"
                    )
                else:
                    wf.facts["figma_mode"] = "agent_design"
                    wf.facts.setdefault("figma_provided", False)
                wf.facts["figma_gate_done"] = True
            if node.id.startswith("approval.arch"):
                wf.facts["frozen_architecture"] = True
            if node.id.startswith("approval.plan"):
                wf.facts["frozen_plan"] = True
            if node.id.startswith("approval.db"):
                wf.facts["frozen_database"] = True
                wf.facts["api_unlocked"] = True
            if node.id.startswith("approval.api"):
                wf.facts["frozen_api"] = True
                wf.facts["code_unlocked"] = True
            node.status = TaskStatus.SUCCEEDED
            wf.status = WorkflowStatus.RUNNING
            self.audit.append(
                wf,
                "APPROVAL_APPROVED",
                task_id=node.id,
                agent="human_approval",
                payload={"id": req.id, "decision": decision, "rationale": rationale},
            )
            self.memory.remember_agent(
                "human_approval",
                kind="decision",
                content={"decision": decision, "title": req.title},
                citation=f"approval://{req.id}",
            )
        else:
            req.status = "REJECTED"
            node.status = TaskStatus.FAILED
            node.error = f"Rejected: {rationale}"
            wf.status = WorkflowStatus.FAILED
            self.audit.append(
                wf,
                "APPROVAL_REJECTED",
                task_id=node.id,
                agent="human_approval",
                payload={"id": req.id, "rationale": rationale},
            )

    def _run_approval(self, wf: Workflow, node: TaskNode) -> None:
        # Re-entry after pause: request already exists and is pending decision
        existing = next(
            (
                a
                for a in wf.approvals
                if a.task_id == node.id and a.status == "REQUESTED"
            ),
            None,
        )
        if existing is not None and existing.decision:
            self._apply_approval_decision(
                wf, node, existing, existing.decision, existing.rationale or ""
            )
            return

        node.status = TaskStatus.WAITING_APPROVAL
        wf.status = WorkflowStatus.WAITING_APPROVAL
        agent_mod.human_approval_present(wf, node)
        if existing is None:
            req = build_request(wf, node)
            wf.approvals.append(req)
        else:
            req = existing
        self.audit.append(
            wf,
            "APPROVAL_REQUESTED",
            task_id=node.id,
            agent="human_approval",
            payload={"id": req.id, "title": req.title, "options": req.options},
        )
        print(f"\n=== APPROVAL GATE [{req.risk_tier.value}] ===")
        print(f"Title: {req.title}")
        print(f"Summary: {req.summary}")
        for opt in req.options:
            print(f"  - {opt['id']}: {opt['label']}")

        # Plan/Arch (+ clarify/figma/coding) must pause in Studio — never silent-auto.
        from .approval_gates import force_human_gate

        force_human = force_human_gate(node.id, cli_demo_mode=self.cli_demo_mode)
        if self.auto_approve and not force_human:
            decision, rationale = auto_decide(req)
            print(f"→ Auto-decision: {decision} ({rationale})")
            self._apply_approval_decision(wf, node, req, decision, rationale)
            return

        # CLI --interactive only. Never block Studio/uvicorn threads on stdin —
        # that prevented LangGraph interrupt() and broke the approval modal.
        if (
            self.allow_stdin_prompt
            and sys.stdin.isatty()
            and (force_human or not self.auto_approve)
        ):
            decision = input("Decision id> ").strip()
            rationale = input("Rationale> ").strip()
            self._apply_approval_decision(wf, node, req, decision, rationale)
            return

        # Studio / non-interactive: pause until human submits decision via API
        print("→ Waiting for human approval (workflow paused)")
        self._write_artifacts(wf)
        self.store.checkpoint(wf)
        self.store.save_workflow(wf)

    def find_approval(
        self,
        wf: Workflow,
        *,
        approval_id: str | None = None,
        task_id: str | None = None,
    ) -> Any:
        """Resolve the request a decision refers to: by id, by task, else the latest."""
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
        return req

    def record_approval(
        self,
        wf: Workflow,
        *,
        decision: str,
        rationale: str = "",
        approval_id: str | None = None,
        task_id: str | None = None,
    ) -> Workflow:
        """
        Persist a human decision without executing any further work.

        Split out of `submit_approval` for the distributed path: the API records
        the decision durably, and a worker elsewhere continues the run. Recording
        and executing in one call is only safe in-process — an API Lambda cannot
        run a workflow, and handing the decision to a worker as the *only* record
        of it loses the decision whenever that handoff does not arrive.
        """
        req = self.find_approval(wf, approval_id=approval_id, task_id=task_id)
        node = wf.tasks.get(req.task_id)
        if node is None:
            raise ValueError(f"Unknown approval task {req.task_id}")
        self._apply_approval_decision(wf, node, req, decision, rationale)
        self.store.checkpoint(wf)
        self.store.save_workflow(wf)
        return wf

    def submit_approval(
        self,
        wf: Workflow,
        *,
        decision: str,
        rationale: str = "",
        approval_id: str | None = None,
        task_id: str | None = None,
    ) -> Workflow:
        """Human submits a decision for a paused WAITING_APPROVAL gate, then resume."""
        self.record_approval(
            wf,
            decision=decision,
            rationale=rationale,
            approval_id=approval_id,
            task_id=task_id,
        )
        if wf.status == WorkflowStatus.RUNNING:
            return self.run(wf)
        return wf

    def _expand_analytics(self, wf: Workflow, option: str) -> None:
        """Dynamic planning: graft analytics nodes after clarification."""
        wf.status = WorkflowStatus.REPLANNING
        new_nodes = [
            TaskNode(
                id="analytics.pipeline",
                agent="architecture",
                deps=["approval.clarify"],
                description=f"Analytics option {option}",
                risk_tier=RiskTier.MEDIUM,
            ),
            TaskNode(
                id="analytics.api",
                agent="backend",
                deps=["analytics.pipeline", "approval.arch", "barrier.design"],
                description="Analytics API implementation",
            ),
        ]
        grafted = [n for n in new_nodes if n.id not in wf.tasks]
        graft_nodes(wf, grafted)
        # Wire into implementation barrier / validation when present
        for barrier_id in ("barrier.impl", "barrier.sync"):
            if barrier_id in wf.tasks and "analytics.api" in wf.tasks:
                b = wf.tasks[barrier_id]
                if "analytics.api" not in b.deps:
                    b.deps.append("analytics.api")
        if "validate.pre_release" in wf.tasks and "analytics.api" in wf.tasks:
            v = wf.tasks["validate.pre_release"]
            if "analytics.api" not in v.deps:
                v.deps.append("analytics.api")
        self.audit.append(
            wf,
            "REPLAN_EXPAND",
            payload={"option": option, "added": [n.id for n in grafted]},
        )
        wf.status = WorkflowStatus.RUNNING

    def _replan_security_failure(self, wf: Workflow) -> None:
        """
        Security gate failed — invalidate the security/build cone and retry once.

        Roots are selected by *agent role*, not by node id, so this works across every
        playbook (`production_sdlc` names the node `security.threat`, `greenfield`
        names it `security.review`).
        """
        if wf.facts.get("security_replan_attempted"):
            return
        wf.facts["security_replan_attempted"] = True
        wf.status = WorkflowStatus.REPLANNING
        roots = [
            tid
            for tid, node in wf.tasks.items()
            if node.agent in SECURITY_REPLAN_AGENTS and node.type == NodeType.COMPUTE
        ]
        impacted, preserved = invalidate(
            wf, roots, protect=lambda n: n.type == NodeType.APPROVAL
        )
        self.audit.append(
            wf,
            "REPLAN_SECURITY",
            payload={
                "roots": sorted(roots),
                "impacted": sorted(impacted),
                "preserved": sorted(preserved)[:30],
                "reason": "security_validation_failed",
            },
        )
        wf.facts["needs_security_replan"] = False
        # Do NOT assert the gate's own verdict here. Clearing the stale verdict forces
        # the re-run security_scan agent to publish a fresh one; `sec.validation_passed`
        # is then re-derived from the security_scan artifact, not from this flag.
        wf.facts.pop("security_validation_passed", None)
        wf.status = WorkflowStatus.RUNNING

    def _run_validate(self, wf: Workflow, node: TaskNode) -> None:
        node.status = TaskStatus.RUNNING
        # Production HLD validates before docs/o11y agents run.
        if "brownfield" in wf.playbook_id:
            stage = "brownfield"
        elif "production_sdlc" in wf.playbook_id:
            stage = "quality_gate"
        else:
            stage = "pre_release"
        results = evaluate(wf, stage=stage)
        report = {
            "stage": stage,
            "results": [
                {
                    "gate": r.gate,
                    "status": r.status,
                    "blocking": r.blocking,
                    "detail": r.detail,
                }
                for r in results
            ],
            "overall": "PASS" if overall_pass(results) else "FAIL",
            "summary": summarize(results),
        }
        agent_mod.publish(wf, node, "validation_report", report)
        print("\n=== VALIDATION REPORT ===")
        marks = {"PASS": "✓", "FAIL": "✗", "SKIP": "–"}
        for r in results:
            print(f" {marks.get(r.status, '?')} {r.gate}: {r.status} — {r.detail}")
        if not overall_pass(results):
            # Dynamic replanning / retry path from HLD
            if wf.facts.get("needs_security_replan") and not wf.facts.get(
                "security_replan_attempted"
            ):
                print("→ Validation failed — triggering security replan/retry")
                self._replan_security_failure(wf)
                node.status = TaskStatus.PENDING
                node.attempt = max(0, node.attempt - 1)
                self.audit.append(
                    wf, "VALIDATION_REPLAN", task_id=node.id, payload=report
                )
                return
            node.status = TaskStatus.FAILED
            node.error = "Blocking validation gates failed"
            self.audit.append(wf, "VALIDATION_FAILED", task_id=node.id, payload=report)
            raise RuntimeError(node.error)
        node.status = TaskStatus.SUCCEEDED
        self.audit.append(wf, "VALIDATION_PASSED", task_id=node.id, payload=report)

    def _circuit_open(self, agent: str) -> bool:
        return self._agent_failures.get(agent, 0) >= CIRCUIT_FAILURE_THRESHOLD

    def _note_agent_success(self, agent: str) -> None:
        self._agent_failures[agent] = 0

    def _note_agent_failure(self, agent: str) -> None:
        self._agent_failures[agent] = self._agent_failures.get(agent, 0) + 1

    def execute_node(self, wf: Workflow, node: TaskNode) -> None:
        """Execute one node. Thread-safe for parallel compute workers via `_lock`."""
        with self._lock:
            if self._circuit_open(node.agent) and node.type == NodeType.COMPUTE:
                node.status = TaskStatus.FAILED
                node.error = f"Circuit open for agent '{node.agent}'"
                open_failure_window(wf, node.id, "circuit_open")
                self.audit.append(
                    wf,
                    "CIRCUIT_OPEN",
                    task_id=node.id,
                    agent=node.agent,
                    payload={"failures": self._agent_failures.get(node.agent)},
                )
                wf.status = WorkflowStatus.FAILED
                raise RuntimeError(node.error)

        try:
            if node.type == NodeType.BARRIER and not node.id.startswith("validate."):
                with self._lock:
                    self._run_barrier(wf, node)
            elif node.type == NodeType.APPROVAL:
                with self._lock:
                    self._run_approval(wf, node)
            elif node.id.startswith("validate."):
                with self._lock:
                    self._run_validate(wf, node)
            else:
                # _run_compute releases the lock during agent/LLM work
                self._run_compute(wf, node)
            with self._lock:
                if node.status == TaskStatus.SUCCEEDED:
                    self._note_agent_success(node.agent)
                    close_failure_window(wf, node.id)
        except Exception as e:
            with self._lock:
                node.error = str(e)
                retryable = (
                    node.type == NodeType.COMPUTE
                    and not node.id.startswith("validate.")
                    and node.attempt < node.max_attempts
                )
                if retryable:
                    node.status = TaskStatus.PENDING
                    record_retry(wf)
                    open_failure_window(wf, node.id, str(e))
                    self.audit.append(
                        wf,
                        "TASK_RETRY",
                        task_id=node.id,
                        agent=node.agent,
                        payload={
                            "error": str(e),
                            "attempt": node.attempt,
                            "max_attempts": node.max_attempts,
                        },
                    )
                else:
                    node.status = TaskStatus.FAILED
                    self._note_agent_failure(node.agent)
                    open_failure_window(wf, node.id, str(e))
                    wf.status = WorkflowStatus.FAILED
                    self.audit.append(
                        wf,
                        "TASK_FAILED",
                        task_id=node.id,
                        agent=node.agent,
                        payload={"error": str(e)},
                    )
                    raise

    def _execute_parallel(self, wf: Workflow, nodes: list[TaskNode]) -> None:
        """Run independent ready compute/barrier nodes on a worker pool."""
        if not nodes:
            return
        if len(nodes) == 1:
            print(f"  → {nodes[0].id} [{nodes[0].type.value}] via {nodes[0].agent}")
            self.execute_node(wf, nodes[0])
            return

        width = min(len(nodes), self.max_workers)
        record_parallel_batch(wf, width)
        self.audit.append(
            wf,
            "PARALLEL_BATCH",
            payload={
                "width": width,
                "nodes": [n.id for n in nodes[:width]],
                "max_workers": self.max_workers,
            },
        )
        print(
            "  ∥ parallel workers ("
            + str(width)
            + "): "
            + ", ".join(f"{n.id}/{n.agent}" for n in nodes[:width])
        )
        batch = nodes[:width]
        errors: list[BaseException] = []
        with ThreadPoolExecutor(max_workers=width, thread_name_prefix="forge-w") as pool:
            futures = {pool.submit(self.execute_node, wf, n): n for n in batch}
            for fut in as_completed(futures):
                node = futures[fut]
                try:
                    fut.result()
                    print(f"  ✓ {node.id} [{node.status.value}]")
                except BaseException as exc:  # noqa: BLE001 — surface worker failures
                    errors.append(exc)
                    print(f"  ✗ {node.id} failed: {exc}")
        if errors:
            raise errors[0]

    # ── Scheduler core ────────────────────────────────────────────────────────
    # `tick` is the single implementation of one scheduler step. Both drivers —
    # the legacy `run()` loop and the LangGraph `step` node — consume it, so
    # scheduling, gating and failure semantics can never drift between runtimes.
    # It never blocks on a human: an open gate is *returned*, and each driver
    # surfaces it in its own idiom (CLI pause vs. LangGraph interrupt()).

    def max_steps(self, wf: Workflow) -> int:
        return max(120, len(wf.tasks) * 5)

    def _resolve_waiting_input(self, wf: Workflow) -> None:
        """Ambiguity escalations are recorded, then released for the next stage."""
        for t in list(wf.tasks.values()):
            if t.status == TaskStatus.WAITING_INPUT:
                t.status = TaskStatus.SUCCEEDED
                self.audit.append(
                    wf,
                    "TASK_SUCCEEDED",
                    task_id=t.id,
                    agent=t.agent,
                    payload={"summary": "Ambiguity reported"},
                )

    def _open_human_gate(self, wf: Workflow) -> Optional[TaskNode]:
        """The gate awaiting a decision, if any. Never schedules sibling work past it."""
        waiting = [
            t for t in wf.tasks.values() if t.status == TaskStatus.WAITING_APPROVAL
        ]
        return waiting[0] if waiting else None

    def _settle(self, wf: Workflow, step: int) -> TickOutcome:
        """No node is ready — decide whether that is completion, waiting, or deadlock."""
        statuses = {t.status for t in wf.tasks.values()}
        if TaskStatus.FAILED in statuses:
            wf.status = WorkflowStatus.FAILED
            return TickOutcome("done", step, error="task_failed")

        running = [t.id for t in wf.tasks.values() if t.status == TaskStatus.RUNNING]
        if running:
            # Parallel workers still in flight — not a deadlock.
            self.store.checkpoint(wf)
            self.store.save_workflow(wf)
            time.sleep(0.05)
            return TickOutcome("continue", step, batch=running)

        terminal_ok = (
            TaskStatus.SUCCEEDED,
            TaskStatus.SKIPPED,
            TaskStatus.COMPENSATED,
        )
        if all(t.status in terminal_ok for t in wf.tasks.values()):
            wf.status = WorkflowStatus.SUCCEEDED
            return TickOutcome("done", step)

        pending = [
            t.id
            for t in wf.tasks.values()
            if t.status in (TaskStatus.PENDING, TaskStatus.READY)
        ]
        if pending:
            # A playbook defect (missing or cyclic dependency). Record it as a
            # terminal failure with an audit trail rather than an unhandled raise.
            wf.status = WorkflowStatus.FAILED
            error = f"Deadlock or unmet deps for: {pending}"
            self.audit.append(wf, "DEADLOCK", payload={"pending": pending})
            self._rollback_on_failure(wf, "deadlock")
            return TickOutcome("done", step, error=error)

        wf.status = WorkflowStatus.SUCCEEDED
        return TickOutcome("done", step)

    def tick(self, wf: Workflow, step: int) -> TickOutcome:
        """Advance the workflow by one scheduler step."""
        if self._stop_requested:
            record_safe_stop(wf)
            wf.status = WorkflowStatus.PARTIAL
            self.audit.append(
                wf, "SAFE_STOP", payload={"reason": "operator_requested", "step": step}
            )
            print("  ⏹ Safe-stop — no new tasks; checkpointing")
            self._rollback_on_failure(wf, "safe_stop")
            return TickOutcome("done", step, batch=["safe_stop"])

        if wf.status == WorkflowStatus.FAILED:
            return TickOutcome("done", step, error="task_failed")

        self._resolve_waiting_input(wf)

        # A gate opened on an earlier step and is still undecided. Running dep-ready
        # peers now would race the resume thread and false-deadlock barrier.sync.
        gate = self._open_human_gate(wf)
        if gate is not None:
            wf.status = WorkflowStatus.WAITING_APPROVAL
            return TickOutcome("await_approval", step, batch=[gate.id], gate_task_id=gate.id)

        ready = self.ready_nodes(wf)
        if not ready:
            return self._settle(wf, step)

        if step >= self.max_steps(wf):
            wf.status = WorkflowStatus.FAILED
            self._rollback_on_failure(wf, "max_steps")
            return TickOutcome(
                "done", step, error=f"Exceeded max steps ({self.max_steps(wf)})"
            )

        # Approvals stay serial; independent compute/barrier nodes fan out to workers.
        approvals = [n for n in ready if n.type == NodeType.APPROVAL]
        compute = [n for n in ready if n.type != NodeType.APPROVAL]
        try:
            if approvals:
                node = approvals[0]
                print(f"  → {node.id} [{node.type.value}] via {node.agent}")
                self.execute_node(wf, node)
                self.store.checkpoint(wf)
                self.store.save_workflow(wf)
                if node.status == TaskStatus.WAITING_APPROVAL:
                    return TickOutcome(
                        "await_approval", step, batch=[node.id], gate_task_id=node.id
                    )
                if wf.status == WorkflowStatus.FAILED:
                    return TickOutcome(
                        "done", step, batch=[node.id], error=node.error or "approval_failed"
                    )
                return TickOutcome("continue", step, batch=[node.id])

            self._execute_parallel(wf, compute)
            batch = [n.id for n in compute[: self.max_workers]]
            self.store.checkpoint(wf)
            self.store.save_workflow(wf)
            if wf.status == WorkflowStatus.FAILED:
                return TickOutcome("done", step, batch=batch, error="parallel_batch_failed")
            return TickOutcome("continue", step, batch=batch)
        except Exception as exc:  # noqa: BLE001 — terminal path: record and compensate
            wf.status = WorkflowStatus.FAILED
            print(f"  ✗ batch/node failed: {exc}")
            self._rollback_on_failure(wf, f"task_failed:{exc}")
            return TickOutcome("done", step, error=str(exc))

    def finalize_run(self, wf: Workflow) -> dict[str, Any]:
        """Terminal bookkeeping shared by both drivers."""
        metrics = finalize_metrics(wf)
        self._write_artifacts(wf)
        self._write_dag(wf)
        self.store.save_workflow(wf)
        self.audit.append(
            wf,
            "WORKFLOW_FINISHED",
            payload={
                "status": wf.status.value,
                "checkpoints": wf.checkpoint_seq,
                "metrics": metrics,
            },
        )
        return metrics

    def _rollback_on_failure(self, wf: Workflow, reason: str) -> None:
        """Saga compensation after terminal failure or safe-stop with side effects."""
        results = run_compensation_saga(
            wf, reason=reason, audit=self.audit.append
        )
        if results:
            record_compensation(wf, len(results))
            print(
                f"  ↺ Rollback saga: compensated {len(results)} side-effect node(s) "
                f"({', '.join(r['task_id'] for r in results[:6])})"
            )

    def run(self, wf: Workflow) -> Workflow:
        print(f"\n▶ Workflow {wf.id} playbook={wf.playbook_id} workers={self.max_workers}")
        mark_started(wf)
        self.audit.append(
            wf,
            "WORKFLOW_STARTED",
            payload={"playbook": wf.playbook_id, "max_workers": self.max_workers},
        )
        step = 0
        while step < self.max_steps(wf):
            step += 1
            outcome = self.tick(wf, step)

            if outcome.action == "await_approval":
                # CLI/Studio driver: persist and hand control back to the human.
                print(
                    f"⏸ Workflow paused at {outcome.gate_task_id} — awaiting human approval"
                )
                self._write_artifacts(wf)
                self.store.checkpoint(wf)
                self.store.save_workflow(wf)
                break

            if outcome.action == "done":
                if outcome.error:
                    print(f"  ✗ {outcome.error}")
                break

        metrics = self.finalize_run(wf)
        print(
            f"\n■ Workflow {wf.status.value} | checkpoints={wf.checkpoint_seq} "
            f"| cost=${wf.budgets['usd_spent']:.2f} | tokens={int(wf.budgets['tokens'])}"
        )
        sr = metrics.get("success_rate")
        print(
            f"  Reliability → success_rate={sr} "
            f"retries={metrics.get('task_retries')} "
            f"rollbacks={metrics.get('rollbacks')} "
            f"mttr_ms={metrics.get('mttr_ms')} "
            f"e2e_ms={metrics.get('e2e_latency_ms')} "
            f"parallel_max={metrics.get('parallel_max_width')}"
        )
        print(f"  Audit → {STATE / 'audit' / 'traces' / (wf.id + '.jsonl')}")
        print(f"  Memory → {STATE / 'memory' / 'workflow' / (wf.id + '.json')}")
        return wf

    def run_brownfield_with_impact(self) -> Workflow:
        """Scenario 2: start from preserved baseline, add delta nodes, show impact."""
        wf = self.plan(
            "brownfield",
            facts={
                "baseline": True,
                "existing_services": ["link-api", "redirect-api", "analytics-api"],
            },
        )
        pb = self.load_playbook("brownfield")
        for n in pb["nodes"]:
            if not n.get("preserve"):
                continue
            node = wf.tasks[n["id"]]
            node.status = TaskStatus.SUCCEEDED
            if n["id"] == "baseline.reqspec":
                agent_mod.publish(
                    wf,
                    node,
                    "reqspec",
                    {
                        "fr": [{"id": "FR-01", "text": "create"}],
                        "nfr": [{"id": "NFR-01", "text": "p99"}],
                        "domain": {"entities": ["Link"]},
                    },
                )
                agent_mod.publish(
                    wf,
                    node,
                    "product_brief",
                    {"name": "Snipr", "status": "baseline"},
                )
                agent_mod.publish(
                    wf,
                    node,
                    "domain_model",
                    {
                        "entities": [{"name": "Link"}],
                        "use_cases": [{"id": "UC-01", "name": "CreateLink"}],
                        "edge_cases": ["expired link", "open redirect"],
                    },
                )
            if n["id"] == "baseline.plan":
                agent_mod.publish(
                    wf,
                    node,
                    "execution_plan",
                    {"strategy": "dependency_dag", "stages": [], "preserved": True},
                )
                agent_mod.publish(
                    wf,
                    node,
                    "risk_register",
                    [{"id": "R-01", "text": "baseline risk", "score": 8}],
                )
                # The preserved plan carries its dependency graph — `plan.dag`
                # validates the baseline the same way it validates a new plan.
                agent_mod.publish(
                    wf,
                    node,
                    "dependency_graph",
                    {
                        "nodes": ["link-api", "redirect-api", "analytics-api"],
                        "edges": [
                            {"from": "link-api", "to": "analytics-api"},
                            {"from": "redirect-api", "to": "analytics-api"},
                        ],
                        "parallel_waves": [
                            {
                                "wave": "baseline-services",
                                "after": "baseline.hld",
                                "agents": ["backend", "frontend"],
                                "sync": "barrier.delta",
                            }
                        ],
                        "preserved": True,
                    },
                )
            if n["id"] == "baseline.hld":
                agent_mod.publish(
                    wf,
                    node,
                    "hld",
                    {
                        "tenets": [
                            "redirects never wait on DB as happy path",
                            "outbox for mutations",
                            "cache-first redirect path",
                            "regional-first",
                        ],
                        "components": ["link-api", "redirect-api", "analytics-api"],
                    },
                )
                agent_mod.publish(
                    wf, node, "adrs", [{"id": "ADR-SNIPR-001", "decision": "baseline"}]
                )
                agent_mod.publish(
                    wf,
                    node,
                    "schema_ddl",
                    {
                        "tables": {"links": ["id", "code", "target_url"]},
                        "sharding": "hash(code)",
                        "indexes": ["unique(code)"],
                    },
                )
                agent_mod.publish(
                    wf,
                    node,
                    "openapi",
                    # A preserved baseline must be a real contract: the brownfield
                    # run validates it exactly like a greenfield one.
                    {
                        "openapi": "3.0.3",
                        "info": {
                            "title": "Snipr Links API (baseline)",
                            "version": "1.0.0",
                        },
                        "paths": {
                            "/v1/links": {
                                "post": {
                                    "operationId": "createLink",
                                    "summary": "Create a short link",
                                    "responses": {
                                        "201": {"description": "created"},
                                        "400": {"description": "invalid target url"},
                                    },
                                }
                            },
                            "/{code}": {
                                "get": {
                                    "operationId": "redirectLink",
                                    "summary": "Redirect to the target URL",
                                    "parameters": [
                                        {
                                            "name": "code",
                                            "in": "path",
                                            "required": True,
                                            "schema": {"type": "string"},
                                        }
                                    ],
                                    "responses": {
                                        "302": {"description": "redirect"},
                                        "404": {"description": "unknown code"},
                                        "410": {"description": "expired"},
                                    },
                                }
                            },
                        },
                    },
                )
                agent_mod.publish(
                    wf,
                    node,
                    "perf_budget",
                    {"redirect_p99_ms": 50, "cache_hit_ratio_target": 0.98},
                )
                agent_mod.publish(
                    wf,
                    node,
                    "capacity_estimate",
                    {
                        "assumptions": {
                            "write_read_ratio": "1:100",
                            "new_links_per_month": 200_000_000,
                            "entry_bytes": 500,
                            "retention_years": 5,
                        },
                        "derived": {
                            "entries_5y": 12_000_000_000,
                            "storage_tb": 6,
                            "write_qps": 76,
                            "read_qps": 7600,
                            "cache_gb": 66,
                        },
                        "preserved": True,
                    },
                )

        changed = [
            "feature.qr.design",
            "analytics.refactor",
            "db.optimize",
            "bug.open_redirect",
        ]
        _, preserved = invalidate(wf, [])
        impact = set(changed)
        for tid, node in wf.tasks.items():
            if node.status != TaskStatus.SUCCEEDED:
                impact.add(tid)
            else:
                preserved.add(tid)

        print("\n=== BROWNFIELD IMPACT ANALYSIS ===")
        print(f"Preserved ({len(preserved)}): {sorted(preserved)}")
        print(f"To execute ({len(impact)}): {sorted(impact)}")
        self.audit.append(
            wf,
            "IMPACT_ANALYSIS",
            payload={"preserved": sorted(preserved), "impact": sorted(impact)},
        )
        return self.run(wf)

    def rehydrate(self, workflow_id: str) -> Workflow | None:
        """Load a paused workflow from disk and restore latest artifact contents."""
        wf = self.store.load_workflow(workflow_id)
        if wf is None:
            return None
        self._load_artifacts(wf)
        return wf

    def _load_artifacts(self, wf: Workflow) -> None:
        """Restore the latest version of every artifact from the object store."""
        prefix = artifact_prefix(wf.id)
        latest: dict[str, tuple[int, str]] = {}
        for key in self.objects.list_keys(prefix):
            name = key.rsplit("/", 1)[-1]
            if name.startswith("_") or not name.endswith(".json"):
                continue
            stem = name[: -len(".json")]
            if ".v" not in stem:
                continue
            artifact_key, _, ver_s = stem.rpartition(".v")
            try:
                ver = int(ver_s)
            except ValueError:
                continue
            prev = latest.get(artifact_key)
            if prev is None or ver > prev[0]:
                latest[artifact_key] = (ver, key)
        for artifact_key, (ver, key) in latest.items():
            raw = self.objects.get_text(key)
            if raw is None:
                continue
            try:
                content = json.loads(raw)
            except json.JSONDecodeError:
                continue
            art = Artifact(
                key=artifact_key,
                version=ver,
                task_id="rehydrate",
                content=content,
                content_hash=content_hash(content),
            )
            wf.artifacts[artifact_key] = art
            wf.artifact_history.append(art)

    def _write_artifacts(self, wf: Workflow) -> None:
        prefix = artifact_prefix(wf.id)
        for key, art in wf.artifacts.items():
            self.objects.put_text(
                f"{prefix}/{key}.v{art.version}.json",
                json.dumps(art.content, indent=2, default=str),
            )
        summary = {
            "workflow_id": wf.id,
            "status": wf.status.value,
            "playbook": wf.playbook_id,
            "artifacts": list(wf.artifacts.keys()),
            "approvals": [
                {"title": a.title, "status": a.status, "decision": a.decision}
                for a in wf.approvals
            ],
            "budgets": wf.budgets,
            "metrics": wf.metrics,
            "agents": sorted({t.agent for t in wf.tasks.values()}),
        }
        self.objects.put_text(
            f"{prefix}/_summary.json", json.dumps(summary, indent=2, default=str)
        )
        print(f"Artifacts → {prefix}")

    def _write_dag(self, wf: Workflow) -> None:
        from .agents.design_html import build_dag_html, build_mermaid_flowchart

        nodes = []
        edges = []
        for t in wf.tasks.values():
            nodes.append(
                {
                    "id": t.id,
                    "agent": t.agent,
                    "type": t.type.value,
                    "risk_tier": t.risk_tier.value,
                    "status": t.status.value,
                    "description": t.description,
                }
            )
            for d in t.deps:
                edges.append({"from": d, "to": t.id})

        gates = [
            {
                "id": t.id,
                "kind": "human" if t.type == NodeType.APPROVAL else "sync",
                "purpose": t.description or t.id,
            }
            for t in wf.tasks.values()
            if t.type in (NodeType.APPROVAL, NodeType.BARRIER)
        ]
        graph_art = wf.artifacts.get("dependency_graph")
        parallel_waves = []
        if graph_art and isinstance(graph_art.content, dict):
            parallel_waves = graph_art.content.get("parallel_waves") or []

        html_doc = build_dag_html(
            product=str(wf.facts.get("product_name") or "Forge"),
            workflow_id=wf.id,
            playbook_id=wf.playbook_id,
            status=wf.status.value,
            nodes=nodes,
            edges=edges,
            gates=gates,
            parallel_waves=parallel_waves,
        )
        mermaid = build_mermaid_flowchart(nodes, edges)

        prefix = artifact_prefix(wf.id)
        self.objects.put_text(f"{prefix}/forge_dag.mmd", mermaid)
        self.objects.put_text(f"{prefix}/dag_design.html", html_doc)

        payload = {"html": html_doc, "content_type": "text/html"}
        prev = wf.artifacts.get("dag_design_html")
        version = 1 if prev is None else prev.version + 1
        art = Artifact(
            key="dag_design_html",
            version=version,
            task_id="orchestrator",
            content=payload,
            content_hash=content_hash(payload),
        )
        wf.artifacts["dag_design_html"] = art
        wf.artifact_history.append(art)
        self.objects.put_text(
            f"{prefix}/dag_design_html.v{version}.json",
            json.dumps(payload, indent=2, default=str),
        )
