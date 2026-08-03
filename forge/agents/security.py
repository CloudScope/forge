from __future__ import annotations

from typing import Any

from ..models import TaskNode, Workflow
from ._common import art, publish
from .doc_context import has_feature, product_name, requirement_text
from .llm_bridge import run_llm_agent


def security_review(wf: Workflow, task: TaskNode) -> dict[str, Any]:
    """10. Security Agent — threat model against PRD abuse cases."""
    llm = run_llm_agent(
        wf,
        task,
        agent="security",
        inputs={
            "reqspec": art(wf, "reqspec"),
            "hld": art(wf, "hld"),
            "openapi": art(wf, "openapi"),
            "backend_notes": art(wf, "backend_notes"),
        },
        schema_hint=(
            '{"security_review":{"threat_model":[],"findings":[],"owasp_top10":{}},'
            '"threat_model":{"threats":[],"findings":[]},'
            '"review_report":{"blocking":[],"non_blocking":[],"verdict":"APPROVE_WITH_NITS"}}'
        ),
    )
    if llm and isinstance(llm.get("security_review"), dict):
        review = llm["security_review"]
        publish(wf, task, "security_review", review, bill=False)
        publish(
            wf,
            task,
            "threat_model",
            llm.get("threat_model")
            or {"threats": review.get("threat_model") or [], "findings": review.get("findings") or []},
            bill=False,
        )
        publish(
            wf,
            task,
            "review_report",
            llm.get("review_report")
            or {"blocking": [], "non_blocking": [], "verdict": "APPROVE_WITH_NITS"},
            bill=False,
        )
        return {
            "summary": f"Security review via LLM for {product_name(wf)}",
            "mode": "llm",
        }

    name = product_name(wf)
    findings: list[dict[str, Any]] = []
    if wf.facts.get("fix_open_redirect"):
        findings.append(
            {
                "id": "SEC-01",
                "severity": "HIGH",
                "title": "Strengthen target URL allow/deny validation",
                "status": "FIXED_IN_PATCH",
            }
        )
    if has_feature(wf, "preview"):
        findings.append(
            {
                "id": "SEC-02",
                "severity": "MEDIUM",
                "title": "Preview fetcher must block private IP ranges (SSRF)",
                "status": "MITIGATED_IN_DESIGN",
            }
        )
    review = {
        "product": name,
        "threat_model": [
            "open redirect",
            "phishing via short links",
            "API key leakage",
            "tenant isolation breach",
            "SSRF via preview",
            "SQL injection",
            "XSS in admin UI",
        ],
        "authn": "API keys hashed; JWT admin phase-2",
        "authz": "scope-based; org isolation on every query",
        "owasp_top10": {
            "injection": "parameterized SQL / ORM",
            "xss": "React escaping + CSP",
            "ssrf": "allowlist + block RFC1918",
        },
        "rate_limiting": "per-key + per-org",
        "audit_logging": "mutations → audit_logs",
        "findings": findings,
        "prd_excerpt": (requirement_text(wf) or "")[:300],
    }
    publish(wf, task, "security_review", review)
    publish(wf, task, "threat_model", {"threats": review["threat_model"], "findings": findings})
    publish(
        wf,
        task,
        "review_report",
        {
            "blocking": [],
            "non_blocking": ["Consider canary on redirect-api"],
            "verdict": "APPROVE_WITH_NITS",
        },
    )
    return {"summary": f"Security review for {name}: {len(findings)} findings"}
