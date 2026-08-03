"""Derive operator UI screens from product brief / ReqSpec / LLD / OpenAPI."""

from __future__ import annotations

import re
from typing import Any


def _slug(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (s or "").strip().lower()).strip("-")
    return s or "home"


def _title_case(s: str) -> str:
    return re.sub(r"[-_]+", " ", (s or "").strip()).strip().title() or "Home"


def _openapi_paths(openapi: dict[str, Any] | None) -> list[str]:
    if not openapi:
        return []
    spec = openapi.get("openapi") and openapi or (openapi.get("spec") or openapi)
    if not isinstance(spec, dict):
        return []
    return [p for p in (spec.get("paths") or {}) if isinstance(p, str)]


def _resource_from_path(path: str) -> str | None:
    # /v1/files/{id} -> files
    parts = [p for p in path.strip("/").split("/") if p and not p.startswith("{")]
    if not parts:
        return None
    if parts[0] in {"v1", "v2", "api"} and len(parts) > 1:
        return parts[1]
    if parts[0] in {"healthz", "readyz", "metrics", "docs"}:
        return None
    return parts[0]


def _looks_like_url_shortener(
    product: str, features: list[str], paths: list[str], brief: dict[str, Any]
) -> bool:
    blob = " ".join(
        [
            product or "",
            " ".join(features or []),
            " ".join(paths[:20]),
            str((brief or {}).get("name") or ""),
        ]
    ).lower()
    hits = 0
    for tok in ("short url", "shorten", "tinyurl", "shortlink", "/v1/links", "short_url"):
        if tok in blob:
            hits += 1
    return hits >= 2


def _entities_from_reqspec(reqspec: dict[str, Any] | None) -> list[str]:
    if not reqspec:
        return []
    domain = reqspec.get("domain") or {}
    ents = list(domain.get("entities") or [])
    if ents:
        return [str(e) for e in ents if e]
    # Fallback: nouns from FR texts
    return []


def _mvp_labels(brief: dict[str, Any] | None) -> list[str]:
    brief = brief or {}
    out: list[str] = []
    for item in brief.get("mvp") or []:
        if isinstance(item, dict):
            out.append(str(item.get("description") or item.get("id") or "")[:90])
        else:
            out.append(str(item)[:90])
    return [x for x in out if x]


def derive_ui_spec(
    *,
    product: str,
    openapi: dict[str, Any] | None = None,
    product_brief: dict[str, Any] | None = None,
    reqspec: dict[str, Any] | None = None,
    features: list[str] | None = None,
    lld: Any = None,
    figma: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Build a screen/nav spec for the React UI generator.

    Prefer ReqSpec domain + product brief over OpenAPI when the API artifact
    clearly belongs to another product (e.g. TinyURL paths on a File System PRD).
    """
    brief = product_brief or {}
    req = reqspec or {}
    feats = list(features or [])
    name = (
        (brief.get("name") if isinstance(brief.get("name"), str) else None)
        or req.get("product")
        or product
        or "Product"
    )
    # Prefer longer product title from brief
    if isinstance(brief.get("name"), str) and len(brief["name"]) > len(str(name)):
        name = brief["name"]

    paths = _openapi_paths(openapi)
    entities = _entities_from_reqspec(req)
    mvp = _mvp_labels(brief)
    url_shortener = _looks_like_url_shortener(str(name), feats, paths, brief)

    # If OpenAPI is URL-shortener but product is not, ignore those paths for UI.
    use_openapi = bool(paths) and (
        url_shortener
        or not any(
            t in str(name).lower()
            for t in ("file system", "filesystem", "directory", "inode")
        )
    )
    if not url_shortener and any(
        "/v1/links" in p or p.endswith("/{code}") for p in paths
    ):
        use_openapi = False

    resources: list[str] = []
    if use_openapi:
        for p in paths:
            r = _resource_from_path(p)
            if r and r not in resources:
                resources.append(r)
    if not resources and entities:
        resources = [_slug(e) for e in entities]
    if not resources:
        resources = ["workspace", "operations", "settings"]

    screens: list[dict[str, Any]] = []
    # Home / explorer
    if not url_shortener:
        primary_entity = entities[0] if entities else _title_case(resources[0])
        screens.append(
            {
                "id": "home",
                "route": "/",
                "nav": "Explorer",
                "title": f"{primary_entity} explorer",
                "subtitle": (
                    f"Operate on {name} using the LLD/ReqSpec domain model. "
                    "Empty until the generated API is connected."
                ),
                "primary_action": f"Create {primary_entity.lower()}",
                "fields": [
                    {"id": "name", "label": "Name", "placeholder": f"new-{_slug(primary_entity)}"},
                    {"id": "path", "label": "Parent path", "placeholder": "/"},
                ],
                "empty": f"No {primary_entity.lower()} nodes yet. Wire list/create APIs from OpenAPI/LLD.",
                "mvp_preview": mvp[:6],
            }
        )
        # Entity screens
        for ent in (entities or [_title_case(r) for r in resources[:4]])[:4]:
            sid = _slug(ent)
            if sid == "home":
                continue
            screens.append(
                {
                    "id": sid,
                    "route": f"/{sid}",
                    "nav": _title_case(ent),
                    "title": _title_case(ent),
                    "subtitle": f"LLD-aligned view for {ent} in {name}.",
                    "primary_action": f"New {ent}",
                    "fields": [
                        {"id": "name", "label": "Name", "placeholder": ent.lower()},
                        {"id": "notes", "label": "Notes", "placeholder": "optional"},
                    ],
                    "empty": f"No {ent.lower()} records yet.",
                    "mvp_preview": [],
                }
            )
        # Search if FR mentions search
        fr_blob = " ".join(str(f.get("text") if isinstance(f, dict) else f) for f in (req.get("fr") or []))
        if re.search(r"\bsearch\b", fr_blob, re.I) or "search" in " ".join(mvp).lower():
            screens.append(
                {
                    "id": "search",
                    "route": "/search",
                    "nav": "Search",
                    "title": "Search",
                    "subtitle": "Exact name, extension, path, and recursive patterns from the SRS.",
                    "primary_action": "Run search",
                    "fields": [
                        {"id": "query", "label": "Query", "placeholder": "*.txt OR /docs/**"},
                    ],
                    "empty": "Run a search after the API is connected.",
                    "mvp_preview": [],
                }
            )
    else:
        # URL shortener domain (legacy TinyURL / Snipr)
        screens = [
            {
                "id": "links",
                "route": "/",
                "nav": "Links",
                "title": "Short links",
                "subtitle": "Create and manage branded short URLs.",
                "primary_action": "Shorten",
                "fields": [
                    {"id": "url", "label": "Destination URL", "placeholder": "https://…"},
                    {"id": "alias", "label": "Custom alias", "placeholder": "launch-2026"},
                ],
                "empty": "No links yet. Wire GET /v1/links.",
                "mvp_preview": mvp[:6],
            },
            {
                "id": "analytics",
                "route": "/analytics",
                "nav": "Analytics",
                "title": "Analytics",
                "subtitle": "Traffic insights when analytics API is connected.",
                "primary_action": "Refresh",
                "fields": [],
                "empty": "No analytics yet.",
                "mvp_preview": [],
            },
            {
                "id": "keys",
                "route": "/keys",
                "nav": "API Keys",
                "title": "API keys",
                "subtitle": "Issue and revoke keys for programmatic access.",
                "primary_action": "Create key",
                "fields": [],
                "empty": "No keys yet.",
                "mvp_preview": [],
            },
        ]

    # Dedupe routes
    seen = set()
    uniq: list[dict[str, Any]] = []
    for s in screens:
        if s["route"] in seen:
            continue
        seen.add(s["route"])
        uniq.append(s)

    figma = figma or {}
    return {
        "product": name,
        "domain": "url_shortener" if url_shortener else "generic",
        "entities": entities,
        "features": feats,
        "openapi_paths": paths if use_openapi else [],
        "openapi_ignored": (not use_openapi) and bool(paths),
        "screens": uniq,
        "mvp": mvp[:10],
        "figma_provided": bool(figma.get("provided")),
        "figma_url": figma.get("url") or "",
        "figma_files": list(figma.get("files") or []),
        "figma_notes": figma.get("notes") or "",
        "lld_hint": (
            "Use LLD/ReqSpec domain entities and FR workflows for IA and copy."
            if not url_shortener
            else "URL shortener operator console."
        ),
    }
