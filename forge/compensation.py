from __future__ import annotations

import shutil
from typing import Any, Callable, Optional

from .core.paths import paths as forge_paths
from .models import NodeType, RiskTier, TaskNode, TaskStatus, Workflow

# Agents whose SUCCEEDED work can leave durable side effects.
SIDE_EFFECT_AGENTS = frozenset(
    {
        "backend",
        "frontend",
        "database",
        "devops",
        "deployment",
        "api",
    }
)

_P = forge_paths()
WORKSPACES = _P.workspaces
ARTIFACTS = _P.artifacts


def is_side_effecting(node: TaskNode) -> bool:
    if node.type != NodeType.COMPUTE:
        return False
    if node.agent in SIDE_EFFECT_AGENTS:
        return True
    # Explicit playbook/opt-in via description tag or fact
    return "side_effect" in (node.description or "").lower()


def reverse_topo_succeeded(wf: Workflow) -> list[TaskNode]:
    """Succeeded side-effect nodes in reverse dependency order (compensate last-first)."""
    succeeded = [
        n
        for n in wf.tasks.values()
        if n.status == TaskStatus.SUCCEEDED and is_side_effecting(n)
    ]
    order: list[TaskNode] = []
    visiting: set[str] = set()
    seen: set[str] = set()

    def visit(n: TaskNode) -> None:
        if n.id in seen:
            return
        if n.id in visiting:
            return
        visiting.add(n.id)
        for child in wf.tasks.values():
            if n.id in child.deps and child.status == TaskStatus.SUCCEEDED:
                if is_side_effecting(child):
                    visit(child)
        visiting.discard(n.id)
        seen.add(n.id)
        order.append(n)

    for n in succeeded:
        visit(n)
    # visit appends roots last → reverse for compensate-newest-first already
    # Actually visit does post-order: children before parent. We want children first
    # (newest effects first). Post-order gives dependents before dependencies — correct.
    return order


# Keep failure explanations when rolling back implementation artifacts.
_KEEP_ON_COMPENSATE = frozenset(
    {
        "reqspec",
        "hld",
        "lld",
        "adrs",
        "product_brief",
        "backend_verdict",
        "security_scan",
        "security_review",
        "validation_report",
    }
)


def _compensate_workspace(wf: Workflow, node: TaskNode) -> dict[str, Any]:
    """Best-effort undo for codegen/workspace side effects."""
    actions: list[str] = []
    ws = WORKSPACES / wf.id
    if node.agent == "backend" and (ws / "backend").exists():
        shutil.rmtree(ws / "backend", ignore_errors=True)
        actions.append("removed workspaces/*/backend")
    if node.agent == "frontend" and (ws / "frontend").exists():
        shutil.rmtree(ws / "frontend", ignore_errors=True)
        actions.append("removed workspaces/*/frontend")
    if node.agent == "devops":
        if (ws / "infra").exists():
            shutil.rmtree(ws / "infra", ignore_errors=True)
            actions.append("removed workspaces/*/infra")
        gh = ws / ".github"
        if gh.is_dir():
            shutil.rmtree(gh, ignore_errors=True)
            actions.append("removed workspaces/*/.github")
    if node.agent in ("devops", "deployment"):
        for key in list(wf.artifacts.keys()):
            if key in ("infra", "cicd_pipeline", "deployment_recommendation"):
                art = wf.artifacts.get(key)
                if art and art.task_id == node.id:
                    del wf.artifacts[key]
                    actions.append(f"dropped artifact {key}")
    # Soft-clear outputs produced solely by this task (history retained)
    cleared = 0
    for key, art in list(wf.artifacts.items()):
        if key in _KEEP_ON_COMPENSATE:
            continue
        if art.task_id == node.id:
            # Keep design artifacts; drop implementation outputs on compensate
            if node.agent in SIDE_EFFECT_AGENTS and key.startswith(
                ("backend_", "frontend_", "source_", "workspace_")
            ):
                del wf.artifacts[key]
                cleared += 1
    if cleared:
        actions.append(f"cleared {cleared} implementation artifacts")
    if not actions:
        actions.append("recorded compensation (no durable files)")
    return {"actions": actions}


def compensate_node(wf: Workflow, node: TaskNode, *, reason: str = "") -> dict[str, Any]:
    detail = _compensate_workspace(wf, node)
    node.status = TaskStatus.COMPENSATED
    node.error = (node.error or "") + " | compensated"
    node.outputs["compensation"] = detail
    if node.agent == "backend":
        from .agents.backend_verdict import publish_backend_verdict

        verdict = publish_backend_verdict(wf, node, reason=reason)
        detail["backend_verdict"] = {
            "verdict": verdict.get("verdict"),
            "root_cause": verdict.get("root_cause"),
        }
        actions = list(detail.get("actions") or [])
        actions.append("published backend_verdict with root cause")
        detail["actions"] = actions
    return detail


def run_compensation_saga(
    wf: Workflow,
    *,
    reason: str,
    audit: Optional[Callable[..., Any]] = None,
    stop_before: Optional[set[str]] = None,
) -> list[dict[str, Any]]:
    """
    Saga-style rollback: compensate succeeded side-effect nodes newest-first.

    High/CRITICAL compensations are still executed in the prototype (demo),
    but are audited with risk_tier for human review in production.
    """
    stop_before = stop_before or set()
    results: list[dict[str, Any]] = []
    chain = reverse_topo_succeeded(wf)
    for node in chain:
        if node.id in stop_before:
            break
        if audit:
            audit(
                wf,
                "COMPENSATION_STARTED",
                task_id=node.id,
                agent=node.agent,
                payload={"reason": reason, "risk_tier": node.risk_tier.value},
            )
        detail = compensate_node(wf, node, reason=reason)
        entry = {
            "task_id": node.id,
            "agent": node.agent,
            "risk_tier": node.risk_tier.value,
            "detail": detail,
        }
        results.append(entry)
        if audit:
            audit(
                wf,
                "COMPENSATION_COMPLETED",
                task_id=node.id,
                agent=node.agent,
                payload=entry,
            )
        # Production rule: CRITICAL compensation would pause for human — prototype records it
        if node.risk_tier == RiskTier.CRITICAL and audit:
            audit(
                wf,
                "COMPENSATION_CRITICAL_NOTED",
                task_id=node.id,
                payload={"note": "would require human gate in production"},
            )
    if audit:
        audit(
            wf,
            "ROLLBACK_SAGA_FINISHED",
            payload={"reason": reason, "compensated": [r["task_id"] for r in results]},
        )
    return results
