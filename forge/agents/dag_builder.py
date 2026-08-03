from __future__ import annotations

from typing import Any

from ..models import TaskNode, Workflow
from ._common import art, publish
from .design_html import build_dag_html
from .doc_context import product_name


def dag_build(wf: Workflow, task: TaskNode) -> dict[str, Any]:
    """4. Build Dependency Graph — parallelizable tasks, gates, sync points."""
    plan = art(wf, "execution_plan") or {}
    stories = (art(wf, "task_breakdown") or {}).get("stories") or plan.get("stories") or []
    stages = plan.get("stages") or []

    # Live orchestrator DAG from current workflow tasks (HLD production playbook)
    nodes = []
    edges = []
    for tid, node in wf.tasks.items():
        nodes.append(
            {
                "id": tid,
                "agent": node.agent,
                "type": node.type.value,
                "risk_tier": node.risk_tier.value,
                "status": node.status.value,
                "description": node.description,
            }
        )
        for d in node.deps:
            edges.append({"from": d, "to": tid})

    parallel_waves = [
        {
            "wave": "design",
            "after": "approval.arch",
            "agents": ["database", "api"],
            "sync": "barrier.design",
        },
        {
            "wave": "build",
            "after": "barrier.design",
            "agents": ["backend", "devops", "security"],
            "sync": "barrier.sync",
            "note": "frontend waits on approval.figma",
        },
    ]
    gates = [
        {"id": "approval.plan", "kind": "human", "purpose": "Plan approval"},
        {"id": "approval.arch", "kind": "human", "purpose": "Architecture / LLD freeze"},
        {"id": "approval.db", "kind": "human", "purpose": "DB design freeze → unlock API"},
        {"id": "approval.figma", "kind": "human", "purpose": "Optional Figma → UI coding"},
        {"id": "barrier.sync", "kind": "sync", "purpose": "Parallel exit gate"},
        {"id": "approval.coding", "kind": "human", "purpose": "Coding complete / workspace approve"},
        {"id": "approval.release", "kind": "human", "purpose": "Production Go/No-Go"},
    ]

    mermaid_lines = ["flowchart TB"]
    for n in nodes:
        safe = n["id"].replace(".", "_")
        label = f'{n["id"]}\\n{n["agent"]}'
        if n["type"] == "APPROVAL":
            mermaid_lines.append(f'  {safe}{{{{"{label}"}}}}')
        elif n["type"] == "BARRIER":
            mermaid_lines.append(f'  {safe}[["{label}"]]')
        else:
            mermaid_lines.append(f'  {safe}["{label}"]')
    for e in edges:
        mermaid_lines.append(
            f'  {e["from"].replace(".", "_")} --> {e["to"].replace(".", "_")}'
        )

    graph = {
        "product": product_name(wf),
        "strategy": "dependency_dag",
        "nodes": nodes,
        "edges": edges,
        "story_count": len(stories),
        "stage_count": len(stages),
        "parallel_waves": parallel_waves,
        "gates": gates,
        "entry_gate": "barrier.entry",
        "exit_gate": "barrier.sync",
        "mermaid": "\n".join(mermaid_lines) + "\n",
    }
    publish(wf, task, "dependency_graph", graph)
    publish(wf, task, "forge_dag_spec", {"mermaid": graph["mermaid"], "gates": gates})
    html_doc = build_dag_html(
        product=product_name(wf),
        workflow_id=wf.id,
        playbook_id=wf.playbook_id,
        status=wf.status.value,
        mermaid="",  # rebuild with status class colors from nodes
        nodes=nodes,
        edges=edges,
        gates=gates,
        parallel_waves=parallel_waves,
    )
    publish(wf, task, "dag_design_html", {"html": html_doc, "content_type": "text/html"}, bill=False)
    return {
        "summary": (
            f"DAG built: {len(nodes)} nodes, {len(edges)} edges, "
            f"{len(parallel_waves)} parallel waves, {len(gates)} gates"
        )
    }
