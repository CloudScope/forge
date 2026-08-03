from __future__ import annotations

from typing import Any

from ..models import TaskNode, Workflow
from ._common import publish


def observability_define(wf: Workflow, task: TaskNode) -> dict[str, Any]:
    """Observability Agent — SLIs/SLOs, dashboards, alerts, traces."""
    o11y = {
        "slis": [
            "redirect_success_ratio",
            "redirect_latency_p99",
            "cache_hit_ratio",
            "create_success_ratio",
            "kafka_consumer_lag",
        ],
        "slos": {
            "redirect_availability": "99.99%",
            "redirect_p99": "50ms origin",
            "analytics_freshness": "5m",
        },
        "error_budgets": {"redirect_availability": "0.01% / 30d"},
        "metrics": [
            "redirect_requests_total",
            "redirect_latency_ms",
            "cache_hits_total",
            "link_creates_total",
            "rate_limited_total",
        ],
        "logs": {
            "format": "JSON",
            "fields": ["request_id", "org_id", "code", "outcome", "latency_ms"],
            "pii_policy": "hash IP/UA; no raw target query strings in info logs",
        },
        "tracing": {
            "propagator": "W3C tracecontext",
            "spans": ["gateway", "redirect.resolve", "redis.get", "vitess.get"],
        },
        "health": [" /healthz liveness", "/readyz readiness (redis+db ping)"],
        "alerts": [
            {"name": "RedirectP99High", "expr": "p99 > 50ms for 5m", "severity": "page"},
            {"name": "CacheHitLow", "expr": "hit_ratio < 0.95 for 15m", "severity": "ticket"},
            {"name": "KafkaLag", "expr": "lag > 1e6", "severity": "page"},
        ],
        "dashboards": {
            "panels": [
                "redirect_p99",
                "cache_hit_ratio",
                "error_rate",
                "kafka_lag",
                "create_rps",
                "rate_limit_rejects",
            ]
        },
    }
    publish(wf, task, "observability_plan", o11y)
    publish(wf, task, "workload_dashboards", o11y["dashboards"])
    publish(wf, task, "alert_rules", o11y["alerts"])
    return {"summary": "SLIs/SLOs, dashboards, alerts, tracing plan"}
