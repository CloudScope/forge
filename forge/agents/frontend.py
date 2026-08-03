from __future__ import annotations

from typing import Any

from ..models import TaskNode, Workflow
from ..workspace import (
    ensure_workspace,
    generate_frontend_workspace,
    publish_manifest,
)
from ._common import art, publish
from .design_html import html_artifact_document
from .doc_context import doc_summary, has_feature, product_name
from .llm_bridge import run_llm_agent
from .react_ui import (
    build_react_frontend,
    build_react_preview_html,
    is_react_source_path,
    looks_like_source,
)
from .ui_spec import derive_ui_spec


def _features(wf: Workflow) -> list[str]:
    summary = doc_summary(wf)
    feats = list(summary.get("features") or [])
    req = art(wf, "reqspec") or {}
    for f in req.get("features_detected") or []:
        if f not in feats:
            feats.append(f)
    if has_feature(wf, "qr_code") or wf.facts.get("feature_qr"):
        if "qr_code" not in feats:
            feats.append("qr_code")
        wf.facts["feature_qr"] = True
    if has_feature(wf, "preview") and "preview" not in feats:
        feats.append("preview")
    return feats


def _figma_context(wf: Workflow) -> dict[str, Any]:
    art_figma = art(wf, "figma_design") or {}
    if not isinstance(art_figma, dict):
        art_figma = {}
    return {
        "provided": bool(
            wf.facts.get("figma_provided")
            or art_figma.get("provided")
            or art_figma.get("files")
            or art_figma.get("url")
        ),
        "url": wf.facts.get("figma_url") or art_figma.get("url") or "",
        "files": list(wf.facts.get("figma_files") or art_figma.get("files") or []),
        "notes": wf.facts.get("figma_notes") or art_figma.get("notes") or "",
        "mode": wf.facts.get("figma_mode") or art_figma.get("mode") or "agent_design",
    }


def _publish_frontend_html(
    wf: Workflow,
    task: TaskNode,
    *,
    product: str,
    features: list[str],
    openapi: dict[str, Any],
    pages: dict[str, str],
    ui_spec: dict[str, Any] | None = None,
) -> None:
    """Results → 10. UI Design always shows the React motion preview — never legacy HTML."""
    # Publish the animated React preview directly (tabs are inside the preview).
    # Avoid nesting another iframe/switcher — that broke when </script> was embedded.
    preview = pages.get("_studio_preview.html") or build_react_preview_html(
        product=product,
        features=features,
        ui_spec=ui_spec,
        openapi=openapi,
    )
    publish(wf, task, "frontend_design_html", preview, bill=False)
    publish(
        wf,
        task,
        "ui_design_html",
        {
            "content_type": "text/html",
            "html": preview,
            "pages": ["ui-design.html"],
            "stack": "react-vite-framer-motion",
        },
        bill=False,
    )


def _is_legacy_html_path(path: str) -> bool:
    p = path.replace("\\", "/").lower()
    if p.startswith("apps/web/") and p.endswith((".html", ".css")):
        return True
    if p in {"dashboard.html", "analytics.html", "keys.html", "tools.html", "styles.css"}:
        return True
    return False


def _merge_llm_into_scaffold(
    scaffold: dict[str, str], llm_files: dict[str, Any]
) -> tuple[dict[str, str], int]:
    """Overlay LLM React/TS/CSS onto the Vite scaffold. Ignores legacy HTML pages."""
    out = dict(scaffold)
    overlays = 0
    for path, content in llm_files.items():
        if not isinstance(content, str):
            continue
        if _is_legacy_html_path(str(path)):
            continue  # never accept plain HTML operator pages
        rel = str(path).replace("\\", "/")
        for prefix in ("frontend/",):
            if rel.startswith(prefix):
                rel = rel[len(prefix) :]
        # Map bare component files into src/
        if rel.endswith((".tsx", ".jsx")) and "/" not in rel:
            rel = f"src/pages/{rel}"
        if rel.endswith((".ts", ".css")) and "/" not in rel:
            rel = f"src/{rel}" if rel.endswith(".ts") else f"src/styles/{rel}"
        if not is_react_source_path(rel):
            continue
        # Reject HTML blobs even if path looks like tsx
        low = content.lstrip().lower()
        if low.startswith("<!doctype") or (
            low.startswith("<html") and not rel.endswith((".tsx", ".jsx"))
        ):
            continue
        if not looks_like_source(rel, content):
            continue
        # Never let LLM wipe the studio preview / package scaffold essentials
        if rel in {"_studio_preview.html", "package.json", "vite.config.ts", "index.html"}:
            continue
        out[rel] = content
        overlays += 1
    return out, overlays


def frontend_implement(wf: Workflow, task: TaskNode) -> dict[str, Any]:
    """Generate a modern animated React (Vite) UI into the workflow workspace."""
    name = product_name(wf)
    feats = _features(wf)
    openapi = art(wf, "openapi") or {}
    brief = art(wf, "product_brief") or {}
    reqspec = art(wf, "reqspec") or {}
    hld = art(wf, "hld") or {}
    lld = art(wf, "lld") or {}
    figma = _figma_context(wf)
    # Prefer full product title from brief when intake truncated it
    if isinstance(brief, dict) and isinstance(brief.get("name"), str) and brief["name"].strip():
        name = brief["name"].strip()

    ui_spec = derive_ui_spec(
        product=name,
        openapi=openapi if isinstance(openapi, dict) else {},
        product_brief=brief if isinstance(brief, dict) else {},
        reqspec=reqspec if isinstance(reqspec, dict) else {},
        features=feats,
        lld=lld,
        figma=figma,
    )
    publish(wf, task, "ui_spec", ui_spec, bill=False)
    name = str(ui_spec.get("product") or name)

    scaffold = build_react_frontend(
        product=name,
        openapi=openapi if isinstance(openapi, dict) else {},
        product_brief=brief if isinstance(brief, dict) else {},
        reqspec=reqspec if isinstance(reqspec, dict) else {},
        features=feats,
        lld=lld,
        figma=figma,
        ui_spec=ui_spec,
    )

    screen_paths = [
        f"src/pages/{(s.get('id') or 'home').title().replace('-', '')}.tsx"
        for s in (ui_spec.get("screens") or [])
    ]
    figma_instruction = (
        "A Figma export/URL was provided — mirror its IA, layout density, and naming in React. "
        if figma.get("provided")
        else "No Figma — design the operator UI strictly from ui_spec / LLD / ReqSpec domain entities. "
    )
    llm = run_llm_agent(
        wf,
        task,
        agent="frontend",
        inputs={
            "ui_spec": ui_spec,
            "openapi": openapi,
            "product_brief": brief,
            "reqspec": reqspec,
            "features": feats,
            "hld": hld,
            "lld": lld,
            "figma": figma,
            "stack": "vite-react-ts-framer-motion",
        },
        schema_hint=(
            '{"frontend_source":{"src/pages/Home.tsx":"import …",'
            '"src/components/Layout.tsx":"…","src/styles/global.css":"…"},'
            '"frontend_notes":{"a11y_checklist":[],"screens":[],"motion":[],"figma_mode":""}}'
        ),
        system_extra=(
            f"{figma_instruction}"
            "Emit React + TypeScript + Framer Motion source files for a Vite app. "
            "Values must be full file contents (tsx/ts/css), never one-line descriptions. "
            "Screens/nav MUST match ui_spec.screens (product domain) — never invent TinyURL/short-link UI "
            "unless ui_spec.domain is url_shortener. "
            "Empty states only — do not invent fake demo rows."
        ),
    )

    pages: dict[str, str] = dict(scaffold)
    mode = "heuristic"
    if llm and isinstance(llm.get("frontend_source"), dict):
        merged, overlays = _merge_llm_into_scaffold(scaffold, llm["frontend_source"])
        if overlays:
            pages = merged
            mode = "llm"
            publish(
                wf, task, "frontend_notes", llm.get("frontend_notes") or {}, bill=False
            )
        # If LLM only returned legacy HTML, keep full React scaffold (mode stays heuristic).

    if mode == "heuristic":
        publish(
            wf,
            task,
            "frontend_notes",
            {
                "product": name,
                "domain": ui_spec.get("domain"),
                "figma_mode": figma.get("mode") or ("figma" if figma.get("provided") else "agent_design"),
                "stack": ["vite", "react-18", "typescript", "framer-motion", "react-router"],
                "a11y_checklist": ["keyboard nav", "aria labels", "contrast AA", "focus rings"],
                "motion": [
                    "page enter/exit via AnimatePresence",
                    "staggered panels",
                    "floating hero orb",
                    "CTA hover lift",
                ],
                "screens": screen_paths
                or [f"src/pages/{s.get('id')}.tsx" for s in (ui_spec.get("screens") or [])],
                "ui_spec_nav": [s.get("nav") for s in (ui_spec.get("screens") or [])],
                "run": "cd frontend && npm install && npm run dev",
            },
        )

    root = ensure_workspace(wf.id)
    frontend_files = generate_frontend_workspace(
        root=root,
        product=name,
        pages=pages,
        openapi=openapi if isinstance(openapi, dict) else {},
        stack="react",
    )

    existing = art(wf, "source_tree") or {}
    tree_labels = {}
    for path in frontend_files:
        if path.endswith(".tsx"):
            tree_labels[path] = "React component"
        elif path.endswith(".ts"):
            tree_labels[path] = "TypeScript module"
        elif path.endswith(".css"):
            tree_labels[path] = "stylesheet"
        elif path.endswith("package.json"):
            tree_labels[path] = "npm package manifest"
        elif path.endswith(".html"):
            tree_labels[path] = "HTML entry / preview"
        else:
            tree_labels[path] = "frontend file"

    # Artifact for Results: React sources (exclude huge duplicate preview if desired)
    source_artifact = {
        k: v
        for k, v in pages.items()
        if not k.endswith("_studio_preview.html")
    }
    publish(wf, task, "frontend_source", source_artifact, bill=(mode != "llm"))
    publish(wf, task, "source_tree", {**existing, **tree_labels}, bill=False)
    _publish_frontend_html(
        wf,
        task,
        product=name,
        features=feats,
        openapi=openapi if isinstance(openapi, dict) else {},
        pages=pages,
        ui_spec=ui_spec,
    )

    manifest = publish_manifest(
        wf, task, root, product=name, frontend_files=frontend_files
    )
    backend_files = list(manifest.get("backend_files") or [])
    wf.facts["workspace_path"] = str(root)
    wf.facts["frontend_coded"] = True
    wf.facts["frontend_stack"] = "react-vite-framer-motion"
    if backend_files:
        wf.facts["coding_complete"] = True
        wf.facts["coding_notification"] = (
            f"Coding complete — workspace ready at {root} "
            f"({len(backend_files)} backend + {len(frontend_files)} React files). "
            f"UI: cd frontend && npm install && npm run dev · "
            f"API docs: {manifest.get('run', {}).get('docs')}"
        )
    else:
        wf.facts["coding_notification"] = (
            f"Frontend coding complete — React UI at {root / 'frontend'} "
            f"({len(frontend_files)} files). Run: npm install && npm run dev"
        )

    return {
        "summary": f"React UI workspace for {name}: {len(frontend_files)} files (Vite + Framer Motion)",
        "mode": mode,
        "stack": "react-vite-framer-motion",
        "workspace": str(root),
        "coding_complete": bool(wf.facts.get("coding_complete")),
    }
