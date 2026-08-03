from __future__ import annotations

from typing import Any

from ..models import TaskNode, Workflow
from ._common import art, publish
from .doc_context import doc_summary, product_name, requirement_text
from .llm_bridge import run_llm_agent


def product_analyze(wf: Workflow, task: TaskNode) -> dict[str, Any]:
    """2. Product Analyst Agent — MVP cut from PRD / ReqSpec."""
    text = requirement_text(wf)
    summary = doc_summary(wf)
    req = art(wf, "reqspec") or {}

    if text or req:
        llm = run_llm_agent(
            wf,
            task,
            agent="product",
            inputs={"reqspec": req, "document_summary": summary},
            schema_hint=(
                '{"product_brief":{"name":"","mvp":[],"phase2":[],"success_metrics":{},'
                '"business_goals":[],"status":"ready"},"backlog":{"must":[],"should":[],"could":[]},'
                '"success_metrics":{}}'
            ),
        )
        if llm and isinstance(llm.get("product_brief"), dict):
            brief = llm["product_brief"]
            publish(wf, task, "product_brief", brief, bill=False)
            publish(
                wf,
                task,
                "backlog",
                llm.get("backlog") or {"must": []},
                bill=False,
            )
            metrics = llm.get("success_metrics") or brief.get("success_metrics") or {}
            publish(wf, task, "success_metrics", metrics, bill=False)
            wf.facts["product_name"] = brief.get("name") or product_name(wf)
            return {
                "summary": f"ProductBrief via LLM for {wf.facts['product_name']}",
                "mode": "llm",
            }

        name = product_name(wf)
        frs = req.get("fr") or []
        mvp = []
        phase2 = []
        for fr in frs:
            item = {
                "id": fr.get("id"),
                "feature": fr.get("text"),
                "acceptance": fr.get("acceptance") or fr.get("text"),
            }
            if fr.get("priority") in ("must", "P0", None) and len(mvp) < 8:
                mvp.append(item)
            else:
                phase2.append(item.get("feature") or item.get("id"))

        if not mvp and summary.get("features"):
            mvp = [
                {"id": f"MVP-{i:02d}", "feature": f, "acceptance": f"Deliver {f}"}
                for i, f in enumerate(summary["features"][:6], start=1)
            ]

        metrics = {
            "redirect_p99_ms": summary.get("redirect_p99_ms") or 50,
            "availability": 0.9999,
            "cache_hit_ratio": 0.98,
            "create_p99_ms": 200,
        }
        brief = {
            "name": name,
            "title": summary.get("title") or req.get("title") or name,
            "tagline": "URL shortener platform from uploaded requirements",
            "business_goals": req.get("business_goals") or summary.get("goals") or [],
            "mvp": mvp,
            "phase2": phase2
            or ["custom_domains", "orgs_sso", "warehouse_export"],
            "success_metrics": metrics,
            "stakeholders": ["PM", "Platform Eng", "SRE", "Security"],
            "status": "ready",
            "source": "uploaded_document" if text else "reqspec",
            "ask": (summary.get("excerpt") or text or "")[:240],
        }
        backlog = {
            "must": [m.get("id") for m in mvp if m.get("id")],
            "should": [f.get("id") for f in frs if f.get("priority") == "should"],
            "could": brief["phase2"][:5],
            "wont_mvp": ["custom_domains", "sso"],
        }
        publish(wf, task, "product_brief", brief)
        publish(wf, task, "backlog", backlog)
        publish(wf, task, "success_metrics", metrics)
        return {"summary": f"ProductBrief for {name} — MVP {len(mvp)} items from PRD"}

    # No PRD/ReqSpec yet — stay product-agnostic (greenfield facts only)
    name = product_name(wf)
    ambiguous = bool(wf.facts.get("ambiguous_brief"))
    brief = {
        "name": name,
        "mvp": [],
        "phase2": [],
        "success_metrics": {},
        "status": "ambiguous" if ambiguous else "awaiting_requirements",
        "ask": wf.facts.get("ask")
        or ("clarify analytics scope" if ambiguous else "awaiting requirement document"),
        "source": "facts_only",
    }
    publish(wf, task, "product_brief", brief)
    publish(wf, task, "backlog", {"must": [], "should": [], "could": []})
    publish(wf, task, "success_metrics", {})
    return {"summary": f"ProductBrief skeleton for {name} (no PRD yet)"}
