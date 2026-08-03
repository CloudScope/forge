from __future__ import annotations

from typing import Any

from ..models import TaskNode, Workflow
from ._common import art, publish
from .doc_context import doc_summary, has_feature, requirement_text
from .llm_bridge import run_llm_agent


def requirement_analyze(wf: Workflow, task: TaskNode) -> dict[str, Any]:
    """1. Requirement Analysis Agent — FR/NFR from uploaded PRD or defaults."""
    text = requirement_text(wf)
    summary = doc_summary(wf)
    brief = art(wf, "product_brief") or {}

    if text and summary:
        llm = run_llm_agent(
            wf,
            task,
            agent="requirement",
            inputs={"document_summary": summary, "product_brief": brief},
            schema_hint=(
                '{"reqspec":{"product":"","fr":[{"id":"","text":"","priority":"","acceptance":""}],'
                '"nfr":[{"id":"","text":"","category":""}],"constraints":[],"assumptions":[],'
                '"business_rules":[],"domain":{"entities":[],"use_cases":[],"edge_cases":[]}},'
                '"domain_model":{},"ambiguity_report":null,"open_questions":[]}'
            ),
        )
        if llm and isinstance(llm.get("reqspec"), dict):
            reqspec = llm["reqspec"]
            publish(wf, task, "reqspec", reqspec, bill=False)
            if isinstance(llm.get("domain_model"), dict):
                publish(wf, task, "domain_model", llm["domain_model"], bill=False)
            elif reqspec.get("domain"):
                publish(
                    wf,
                    task,
                    "domain_model",
                    {
                        "entities": [
                            {"name": e} if isinstance(e, str) else e
                            for e in (reqspec["domain"].get("entities") or [])
                        ],
                        "use_cases": reqspec["domain"].get("use_cases") or [],
                        "edge_cases": reqspec["domain"].get("edge_cases") or [],
                        "source": "llm",
                    },
                    bill=False,
                )
            open_q = list(llm.get("open_questions") or reqspec.get("open_questions") or [])
            amb = llm.get("ambiguity_report") if isinstance(llm.get("ambiguity_report"), dict) else None
            if amb:
                publish(wf, task, "ambiguity_report", amb, bill=False)
            elif open_q:
                publish(
                    wf,
                    task,
                    "ambiguity_report",
                    {
                        "ambiguity_score": 0.55,
                        "questions": open_q,
                        "options": [
                            {"id": "approve", "label": "Requirements are clear — proceed"},
                            {"id": "reject", "label": "Still ambiguous — stop for rework"},
                        ],
                    },
                    bill=False,
                )
            # Studio always presents approval.clarify; flag helps UI copy.
            score = float((amb or {}).get("ambiguity_score") or (0.55 if open_q else 0.0))
            wf.facts["needs_clarification"] = bool(open_q or score >= 0.4)
            wf.facts["product_name"] = reqspec.get("product") or summary.get("product_name")
            if "ambiguity_report" not in wf.artifacts:
                publish(
                    wf,
                    task,
                    "ambiguity_report",
                    {
                        "ambiguity_score": score,
                        "questions": open_q
                        or [
                            "Confirm MVP feature cut",
                            "Confirm NFR assumptions in the ReqSpec",
                        ],
                        "options": [
                            {"id": "approve", "label": "Requirements are clear — proceed"},
                            {"id": "reject", "label": "Need more clarification — stop"},
                        ],
                    },
                    bill=False,
                )
            fr_n = len(reqspec.get("fr") or [])
            return {
                "summary": f"ReqSpec via LLM: {fr_n} FRs for {wf.facts['product_name']}",
                "mode": "llm",
            }
        return _from_document(wf, task, text, summary)

    ambiguous = bool(wf.facts.get("ambiguous_brief")) or brief.get("status") == "ambiguous"
    if ambiguous and not wf.facts.get("analytics_option"):
        report = {
            "ambiguity_score": 0.91,
            "phrase": "enterprise analytics",
            "questions": [
                "Which metrics are MVP-critical?",
                "Freshness SLA: realtime / NRT / batch?",
                "Retention days?",
                "Tenant isolation model?",
            ],
            "options": [
                {"id": "A", "label": "Essentials", "complexity": "low"},
                {"id": "B", "label": "Product Analytics", "complexity": "medium"},
                {"id": "C", "label": "Enterprise", "complexity": "high"},
            ],
            "assumptions": [
                {"id": "a1", "text": "Near-real-time 1-5 min", "risk": "medium"},
            ],
        }
        publish(wf, task, "ambiguity_report", report)
        if "product_brief" not in wf.artifacts:
            publish(
                wf,
                task,
                "product_brief",
                {"name": "Snipr", "ask": "enterprise analytics", "status": "ambiguous"},
            )
        wf.facts["needs_clarification"] = True
        return {"summary": "Ambiguity detected — clarification required", "escalate": "clarify"}

    return _default_snipr(wf, task, brief)


def _from_document(
    wf: Workflow, task: TaskNode, text: str, summary: dict[str, Any]
) -> dict[str, Any]:
    name = summary.get("product_name") or "Product"
    frs: list[dict[str, Any]] = []
    for i, line in enumerate(summary.get("fr_lines") or [], start=1):
        frs.append(
            {
                "id": f"FR-{i:02d}",
                "text": line,
                "priority": "must" if i <= 5 else "should",
                "acceptance": f"Satisfies: {line[:120]}",
                "source": "uploaded_prd",
            }
        )
    if not frs:
        # Derive from detected features
        feature_fr = {
            "short_url": "Create short URL from validated target",
            "redirect": "Redirect with 302 via cache-first path",
            "custom_alias": "Custom alias with org-scoped uniqueness",
            "analytics": "Click analytics with daily aggregates",
            "expiration": "Link expiration returns 410",
            "qr_code": "QR code generation PNG/SVG",
            "rate_limiting": "API keys + rate limits",
            "bulk": "Bulk URL creation with per-row errors",
            "preview": "URL validation and safe preview",
            "admin": "Admin APIs for disable/enable and key rotation",
            "health": "Health and metrics endpoints",
        }
        for i, feat in enumerate(summary.get("features") or [], start=1):
            frs.append(
                {
                    "id": f"FR-{i:02d}",
                    "text": feature_fr.get(feat, feat),
                    "priority": "must",
                    "acceptance": f"Feature '{feat}' implemented and tested",
                    "source": "feature_detection",
                }
            )

    nfrs: list[dict[str, Any]] = []
    for i, line in enumerate(summary.get("nfr_lines") or [], start=1):
        nfrs.append({"id": f"NFR-{i:02d}", "text": line, "category": "stated"})
    if not nfrs:
        nfrs = [
            {
                "id": "NFR-01",
                "text": f"redirect p99 < {summary.get('redirect_p99_ms', 50)}ms origin",
                "category": "performance",
            },
            {
                "id": "NFR-02",
                "text": f"availability {summary.get('availability', '99.99%')} for redirect",
                "category": "reliability",
            },
            {
                "id": "NFR-03",
                "text": "cache-first redirect path",
                "category": "scalability",
            },
        ]

    if summary.get("ambiguous") and not wf.facts.get("clarification_accepted"):
        report = {
            "ambiguity_score": 0.75,
            "phrase": "TBD/TODO markers in PRD",
            "questions": [
                "Which TBD items are MVP-blocking?",
                "Confirm latency and availability SLOs",
                "Confirm multi-region requirement for MVP",
            ],
            "options": [
                {"id": "A", "label": "Proceed with documented assumptions"},
                {"id": "B", "label": "Pause for stakeholder workshop"},
            ],
            "assumptions": [
                {"id": "a1", "text": "Single-region MVP", "risk": "medium"},
            ],
            "source_excerpt": text[:400],
        }
        publish(wf, task, "ambiguity_report", report)
        wf.facts["needs_clarification"] = True

    reqspec = {
        "product": name,
        "title": summary.get("title"),
        "business_goals": summary.get("goals")
        or [
            "Scalable short-link platform",
            "Measurable click analytics",
        ],
        "fr": frs,
        "nfr": nfrs,
        "constraints": [
            ln
            for ln in (summary.get("sections") or {}).get("constraints & assumptions", "").splitlines()
            if ln.strip()
        ]
        or [
            "No sync DB on redirect happy path",
            "API keys stored hashed only",
        ],
        "assumptions": [
            {"id": "A-01", "text": "Single primary region for MVP", "risk": "medium"},
            {"id": "A-02", "text": "Org-level tenancy", "risk": "low"},
        ],
        "business_rules": [
            "Short codes are immutable once published",
            "Disabled links do not redirect",
        ],
        "features_detected": summary.get("features") or [],
        "source": {
            "type": "uploaded_document",
            "filename": wf.facts.get("requirement_filename"),
            "char_count": summary.get("char_count"),
        },
        "domain": {
            "entities": ["Organization", "User", "Link", "Alias", "ApiKey", "ClickEvent", "AuditLog"],
            "use_cases": ["CreateLink", "ResolveRedirect", "ViewAnalytics", "ManageKeys"],
            "edge_cases": [
                "expired link",
                "disabled link",
                "open redirect",
                "hot key stampede",
            ],
        },
    }
    if has_feature(wf, "qr_code"):
        wf.facts["feature_qr"] = True
        reqspec["fr"].append(
            {
                "id": "FR-30",
                "text": "QR code generation",
                "priority": "should",
                "acceptance": "PNG/SVG for a link id",
            }
        )

    publish(wf, task, "reqspec", reqspec)
    publish(
        wf,
        task,
        "domain_model",
        {
            "entities": [{"name": e} for e in reqspec["domain"]["entities"]],
            "use_cases": [{"name": u} for u in reqspec["domain"]["use_cases"]],
            "edge_cases": reqspec["domain"]["edge_cases"],
            "source": "requirement_agent",
        },
    )
    if "ambiguity_report" not in wf.artifacts:
        publish(
            wf,
            task,
            "ambiguity_report",
            {
                "ambiguity_score": 0.2,
                "questions": [
                    "Confirm MVP feature cut is acceptable",
                    "Confirm latency / availability assumptions",
                ],
                "options": [
                    {"id": "approve", "label": "Requirements are clear — proceed"},
                    {"id": "reject", "label": "Need more clarification — stop"},
                ],
            },
            bill=False,
        )
    wf.facts["needs_clarification"] = bool(wf.facts.get("needs_clarification"))
    wf.facts["product_name"] = name
    return {"summary": f"ReqSpec from PRD: {len(frs)} FRs / {len(nfrs)} NFRs for {name}"}


def _default_snipr(wf: Workflow, task: TaskNode, brief: dict[str, Any]) -> dict[str, Any]:
    reqspec = {
        "business_goals": brief.get("business_goals")
        or ["Scalable short-link platform", "Measurable click analytics"],
        "fr": [
            {"id": "FR-01", "text": "Create short URL", "priority": "must", "acceptance": "Unique code"},
            {"id": "FR-02", "text": "Redirect with 302", "priority": "must", "acceptance": "Cache-first"},
            {"id": "FR-03", "text": "Custom alias", "priority": "should", "acceptance": "Org unique"},
            {"id": "FR-04", "text": "Click analytics", "priority": "must", "acceptance": "Daily aggregates"},
            {"id": "FR-05", "text": "API keys + rate limits", "priority": "must", "acceptance": "Hashed keys"},
        ],
        "nfr": [
            {"id": "NFR-01", "text": "redirect p99 < 50ms origin", "category": "performance"},
            {"id": "NFR-02", "text": "cache-first redirect path", "category": "scalability"},
        ],
        "constraints": ["No sync DB on redirect happy path"],
        "assumptions": [{"id": "A-01", "text": "Single-region MVP", "risk": "medium"}],
        "business_rules": ["Short codes immutable"],
        "domain": {
            "entities": ["Link", "Organization", "ApiKey", "ClickEvent", "Alias"],
            "use_cases": ["CreateLink", "ResolveRedirect", "ViewAnalytics"],
            "edge_cases": ["expired link", "open redirect"],
        },
    }
    if wf.facts.get("analytics_option"):
        reqspec["fr"].append(
            {
                "id": "FR-20",
                "text": f"Enterprise analytics option {wf.facts['analytics_option']}",
                "priority": "must",
            }
        )
    if wf.facts.get("feature_qr"):
        reqspec["fr"].append({"id": "FR-30", "text": "QR code generation", "priority": "should"})
    publish(wf, task, "reqspec", reqspec)
    if "ambiguity_report" not in wf.artifacts:
        publish(
            wf,
            task,
            "ambiguity_report",
            {
                "ambiguity_score": 0.25,
                "questions": [
                    "Confirm MVP short-link scope",
                    "Confirm analytics depth for v1",
                ],
                "options": [
                    {"id": "approve", "label": "Requirements are clear — proceed"},
                    {"id": "A", "label": "Essentials analytics"},
                    {"id": "B", "label": "Product analytics"},
                    {"id": "C", "label": "Enterprise analytics"},
                    {"id": "reject", "label": "Need more clarification — stop"},
                ],
            },
            bill=False,
        )
    wf.facts["needs_clarification"] = True
    return {"summary": f"ReqSpec with {len(reqspec['fr'])} FRs + domain model"}
