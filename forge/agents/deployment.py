from __future__ import annotations

from typing import Any

from ..models import TaskNode, Workflow
from ._common import art, publish
from .doc_context import product_name
from .llm_bridge import run_llm_agent


def deployment_recommend(wf: Workflow, task: TaskNode) -> dict[str, Any]:
    """13. Deployment Recommendation — rollout, monitoring, rollback."""
    llm = run_llm_agent(
        wf,
        task,
        agent="release",
        inputs={
            "release_notes": art(wf, "release_notes"),
            "infra": art(wf, "infra"),
            "security_scan": art(wf, "security_scan"),
            "observability_plan": art(wf, "observability_plan"),
        },
        schema_hint=(
            '{"deployment_recommendation":{"environment":"","rollout":{},"monitoring":[],'
            '"alerts":[],"rollback":{},"pre_reqs":[],"post_deploy":[]}}'
        ),
        system_extra="Produce a concrete production deployment recommendation.",
    )
    if llm and isinstance(llm.get("deployment_recommendation"), dict):
        rec = llm["deployment_recommendation"]
        mode = "llm"
    else:
        rec = art(wf, "deployment_recommendation") or {
            "environment": "staging → prod canary",
            "rollout": {"strategy": "canary", "steps": ["5%", "25%", "50%", "100%"]},
            "monitoring": ["redirect_p99", "error_rate", "cache_hit_ratio", "kafka_lag"],
            "alerts": ["RedirectP99High", "CacheHitLow", "KafkaLag"],
            "rollback": {
                "trigger": "error_rate > 1% or p99 > budget for 5m",
                "action": "redeploy previous redirect-api image; invalidate bad cache keys",
            },
            "pre_reqs": ["migrations applied", "dashboards imported", "alerts armed"],
            "post_deploy": ["watch redirect_p99", "verify create path", "spot-check analytics lag"],
        }
        mode = "heuristic"

    rec["product"] = product_name(wf)
    rec["approved_for_prod"] = any(
        a.status == "APPROVED" and a.task_id.startswith("approval.release")
        for a in wf.approvals
    )
    publish(wf, task, "deployment_recommendation", rec, bill=(mode != "llm"))
    return {
        "summary": f"Deployment recommendation for {product_name(wf)}",
        "mode": mode,
    }
