from __future__ import annotations

from typing import Any

from ..models import TaskNode, Workflow
from ._common import art, publish
from .doc_context import product_name, requirement_text
from .llm_bridge import run_llm_agent


def plan_decompose(wf: Workflow, task: TaskNode) -> dict[str, Any]:
    """3. Planning Agent — epics/stories from ReqSpec + PRD."""
    req = art(wf, "reqspec") or {}
    brief = art(wf, "product_brief") or {}
    frs = req.get("fr") or []
    name = product_name(wf)
    fr_count = len(frs)

    llm = run_llm_agent(
        wf,
        task,
        agent="planner",
        inputs={"reqspec": req, "product_brief": brief},
        schema_hint=(
            '{"execution_plan":{"strategy":"dependency_dag","epics":[],"stories":[],"stages":[],'
            '"human_gates":[],"parallelism":[]},"task_breakdown":{"epics":[],"stories":[]},'
            '"dependency_graph":{},"risk_register":[{"id":"","text":"","score":0}]}'
        ),
    )
    if llm and isinstance(llm.get("execution_plan"), dict):
        plan = llm["execution_plan"]
        publish(wf, task, "execution_plan", plan, bill=False)
        publish(
            wf,
            task,
            "task_breakdown",
            llm.get("task_breakdown")
            or {"epics": plan.get("epics"), "stories": plan.get("stories")},
            bill=False,
        )
        publish(
            wf,
            task,
            "dependency_graph",
            llm.get("dependency_graph") or {"stages": plan.get("stages") or []},
            bill=False,
        )
        risks = llm.get("risk_register") or plan.get("risks") or []
        publish(wf, task, "risk_register", risks, bill=False)
        return {
            "summary": f"DAG plan via LLM for {name}: {len(plan.get('stories') or [])} stories",
            "mode": "llm",
        }

    epics = [
        {
            "id": "E-01",
            "title": f"{name} link lifecycle",
            "features": ["create", "redirect", "alias", "expiration", "admin"],
        },
        {
            "id": "E-02",
            "title": "Analytics pipeline",
            "features": ["ingest", "aggregate", "query API"],
        },
        {
            "id": "E-03",
            "title": "Platform hardening",
            "features": ["authn", "rate_limit", "observability", "CI/CD"],
        },
    ]
    stories = []
    owners = ["backend", "backend", "backend", "backend", "frontend", "devops"]
    for i, fr in enumerate(frs[:12], start=1):
        stories.append(
            {
                "id": f"S-{i:02d}",
                "epic": "E-01" if i <= 4 else ("E-02" if i <= 7 else "E-03"),
                "title": str(fr.get("text", f"Story {i}"))[:120],
                "owner": owners[(i - 1) % len(owners)],
                "priority": "P0" if fr.get("priority") == "must" else "P1",
                "complexity": "M",
                "effort_days": 3,
                "deps": [f"S-{i-1:02d}"] if i > 1 else [],
                "risk": "MEDIUM",
                "acceptance": fr.get("acceptance") or fr.get("text"),
                "fr_id": fr.get("id"),
            }
        )
    if not stories:
        stories = [
            {
                "id": "S-01",
                "epic": "E-01",
                "title": "Short code generation + create API",
                "owner": "backend",
                "priority": "P0",
                "complexity": "M",
                "effort_days": 3,
                "deps": [],
                "risk": "MEDIUM",
                "acceptance": "Unique codes",
            }
        ]

    stages = [
        {"id": "architecture", "agent": "architecture", "deps": []},
        {"id": "approval.arch", "agent": "human_approval", "deps": ["architecture"]},
        {"id": "database", "agent": "database", "deps": ["approval.arch"]},
        {"id": "api", "agent": "api", "deps": ["approval.arch"]},
        {"id": "backend", "agent": "backend", "deps": ["database", "api"]},
        {"id": "frontend", "agent": "frontend", "deps": ["api"]},
        {"id": "devops", "agent": "devops", "deps": ["approval.arch"]},
        {"id": "testing", "agent": "testing", "deps": ["backend", "frontend"]},
        {"id": "security", "agent": "security", "deps": ["backend"]},
        {"id": "documentation", "agent": "documentation", "deps": ["backend", "devops"]},
        {
            "id": "validation",
            "agent": "validation",
            "deps": ["testing", "security", "documentation"],
        },
        {"id": "approval.release", "agent": "human_approval", "deps": ["validation"]},
        {"id": "release", "agent": "release", "deps": ["approval.release"]},
    ]

    risks = [
        {
            "id": "R-01",
            "category": "architecture",
            "text": "Multi-region complexity vs MVP single-region",
            "score": 12,
            "mitigation": "Design shard keys now; defer multi-region",
            "owner": "architecture",
        },
        {
            "id": "R-02",
            "category": "security",
            "text": "Short-link phishing / open redirect abuse",
            "score": 16,
            "mitigation": "URL validation, rate limits, abuse scanning",
            "owner": "security",
        },
        {
            "id": "R-03",
            "category": "performance",
            "text": "Hot-key cache stampede on popular codes",
            "score": 12,
            "mitigation": "singleflight + soft TTL + CDN",
            "owner": "backend",
        },
    ]

    plan = {
        "strategy": "dependency_dag",
        "product": name,
        "fr_coverage": fr_count,
        "epics": epics,
        "stories": stories,
        "parallelism": [
            "database ∥ api after arch freeze",
            "backend ∥ frontend ∥ devops after design barrier",
            "testing ∥ security ∥ documentation after impl barrier",
        ],
        "stages": stages,
        "human_gates": ["architecture freeze", "production release"],
        "rollback_strategy": "Invalidate dependent tasks; preserve frozen ADRs unless arch rejected",
        "success_criteria": [
            "All P0 stories acceptance met",
            "Blocking validation gates green",
            "Human Go on release",
        ],
        "source_document": bool(requirement_text(wf)),
        "mvp_count": len(brief.get("mvp") or []),
    }
    publish(wf, task, "execution_plan", plan)
    publish(wf, task, "task_breakdown", {"epics": epics, "stories": stories})
    publish(
        wf,
        task,
        "dependency_graph",
        {"stages": stages, "story_deps": {s["id"]: s["deps"] for s in stories}},
    )
    publish(wf, task, "risk_register", risks)
    return {
        "summary": f"DAG plan for {name}: {len(epics)} epics, {len(stories)} stories, {fr_count} FRs"
    }
