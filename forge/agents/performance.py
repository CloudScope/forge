from __future__ import annotations

from typing import Any

from ..models import TaskNode, Workflow
from ._common import art, publish


def performance_budget(wf: Workflow, task: TaskNode) -> dict[str, Any]:
    """Performance Agent — budgets, bottlenecks, load guidance."""
    metrics = art(wf, "success_metrics") or {}
    budget = {
        "redirect_p99_ms": metrics.get("redirect_p99_ms", 50),
        "create_p99_ms": metrics.get("create_p99_ms", 200),
        "cache_hit_ratio_target": metrics.get("cache_hit_ratio", 0.98),
        "analytics_freshness_s": 300,
        "error_budget": "0.01% monthly for redirect availability",
        "load_targets": {
            "redirect_rps": 100_000,
            "create_rps": 2_000,
        },
        "bottlenecks": [
            "Redis cluster network",
            "hot short codes",
            "Vitess primary on create path",
        ],
        "optimizations": [
            "singleflight on cache miss",
            "soft TTL + background refresh",
            "CDN for top-N codes",
        ],
    }
    publish(wf, task, "perf_budget", budget)
    return {"summary": "Performance budgets + bottleneck analysis"}
