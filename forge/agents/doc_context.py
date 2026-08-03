from __future__ import annotations

from typing import Any

from ..models import Workflow
from ._common import art


def requirement_text(wf: Workflow) -> str:
    raw = art(wf, "raw_requirement")
    if isinstance(raw, dict):
        return str(raw.get("text") or "")
    if isinstance(raw, str):
        return raw
    return str(wf.facts.get("requirement_text") or "")


def doc_summary(wf: Workflow) -> dict[str, Any]:
    s = art(wf, "document_summary")
    if isinstance(s, dict):
        return s
    return dict(wf.facts.get("document_summary") or {})


def product_name(wf: Workflow) -> str:
    """Resolve product name from prior artifacts — never invent TinyURL/Snipr defaults."""
    if wf.facts.get("product_name"):
        return str(wf.facts["product_name"]).strip() or "Product"
    s = doc_summary(wf)
    if s.get("product_name"):
        return str(s["product_name"]).strip() or "Product"
    brief = art(wf, "product_brief") or {}
    if isinstance(brief, dict) and brief.get("name"):
        return str(brief["name"]).strip() or "Product"
    # Last resort: filename stem (not a URL-shortener brand)
    fn = str(wf.facts.get("requirement_filename") or "").strip()
    if fn:
        stem = fn.rsplit(".", 1)[0].replace("_", " ").replace("-", " ").strip()
        if stem:
            return stem
    return "Product"


def has_feature(wf: Workflow, *keys: str) -> bool:
    feats = set(doc_summary(wf).get("features") or [])
    # also honor workflow facts toggles
    for k in keys:
        if k in feats:
            return True
        if wf.facts.get(f"feature_{k}") or wf.facts.get(k):
            return True
    text = requirement_text(wf).lower()
    aliases = {
        "qr_code": ["qr"],
        "custom_alias": ["alias"],
        "rate_limiting": ["rate limit"],
        "short_url": ["short url", "shorten", "tinyurl"],
    }
    for k in keys:
        if k in text:
            return True
        for a in aliases.get(k, []):
            if a in text:
                return True
    return False
