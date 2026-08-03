from __future__ import annotations

from typing import Any

from ..models import TaskNode, Workflow
from ._common import art, publish


def business_analyze(wf: Workflow, task: TaskNode) -> dict[str, Any]:
    """Business / Product Analyst — domain model, use cases, edge cases."""
    req = art(wf, "reqspec") or {}
    domain = {
        "entities": [
            {
                "name": "Organization",
                "attrs": ["id", "name", "plan", "created_at"],
                "relations": ["has many Links", "has many ApiKeys", "has many Users"],
            },
            {
                "name": "User",
                "attrs": ["id", "org_id", "email", "role"],
                "relations": ["belongs to Organization"],
            },
            {
                "name": "Link",
                "attrs": [
                    "id",
                    "code",
                    "target_url",
                    "org_id",
                    "alias",
                    "expires_at",
                    "status",
                    "created_at",
                ],
                "relations": ["belongs to Organization", "has many ClickEvents"],
            },
            {
                "name": "Alias",
                "attrs": ["org_id", "alias", "link_id"],
                "relations": ["maps to Link"],
            },
            {
                "name": "ApiKey",
                "attrs": ["id", "org_id", "hash", "scopes", "rate_limit", "last_used_at"],
                "relations": ["belongs to Organization"],
            },
            {
                "name": "ClickEvent",
                "attrs": ["id", "link_id", "ts", "country", "ua_hash", "referrer"],
                "relations": ["belongs to Link"],
            },
            {
                "name": "AuditLog",
                "attrs": ["id", "actor", "action", "resource", "ts", "payload"],
                "relations": [],
            },
        ],
        "use_cases": [
            {"id": "UC-01", "name": "CreateLink", "actor": "API client", "fr": "FR-01"},
            {"id": "UC-02", "name": "ResolveRedirect", "actor": "End user", "fr": "FR-02"},
            {"id": "UC-03", "name": "ViewAnalytics", "actor": "Operator", "fr": "FR-04"},
            {"id": "UC-04", "name": "ManageKeys", "actor": "Admin", "fr": "FR-05"},
            {"id": "UC-05", "name": "BulkCreate", "actor": "API client", "fr": "FR-07"},
            {"id": "UC-06", "name": "DisableLink", "actor": "Admin", "fr": "FR-09"},
        ],
        "edge_cases": [
            "expired link",
            "disabled link",
            "hot key stampede",
            "open redirect / javascript: URLs",
            "alias collision under concurrency",
            "bulk partial failure",
            "SSRF via preview fetcher",
            "rate limit burst across keys",
        ],
        "data_dictionary": {
            "code": "Base62 short code derived from snowflake id",
            "status": "active|disabled|expired",
            "scopes": "links:write|links:read|analytics:read|admin",
        },
        "fr_coverage": [f.get("id") for f in req.get("fr", [])],
    }
    er = {
        "mermaid": (
            "erDiagram\n"
            "  ORGANIZATION ||--o{ USER : has\n"
            "  ORGANIZATION ||--o{ LINK : owns\n"
            "  ORGANIZATION ||--o{ API_KEY : issues\n"
            "  LINK ||--o{ CLICK_EVENT : generates\n"
            "  LINK ||--o| ALIAS : named_by\n"
            "  ORGANIZATION ||--o{ AUDIT_LOG : records\n"
        )
    }
    publish(wf, task, "domain_model", domain)
    publish(wf, task, "use_case_catalog", domain["use_cases"])
    publish(wf, task, "edge_case_list", domain["edge_cases"])
    publish(wf, task, "er_diagram", er)
    # Keep domain nested in reqspec consumers that expect it
    if "reqspec" in wf.artifacts:
        merged = dict(wf.artifacts["reqspec"].content)
        merged["domain"] = {
            "entities": [e["name"] for e in domain["entities"]],
            "use_cases": [u["name"] for u in domain["use_cases"]],
            "edge_cases": domain["edge_cases"],
        }
        publish(wf, task, "reqspec", merged)
    return {"summary": f"Domain model: {len(domain['entities'])} entities, {len(domain['use_cases'])} use cases"}
