from __future__ import annotations

from typing import Any

from ..models import TaskNode, Workflow
from ..security_policy import collect_security_findings, derive_security_needs
from ..workspace import (
    ensure_workspace,
    generate_fastapi_backend,
    publish_manifest,
)
from ._common import art, publish
from .doc_context import product_name
from .llm_bridge import run_llm_agent


def backend_implement(wf: Workflow, task: TaskNode) -> dict[str, Any]:
    """Generate a production FastAPI workspace from prior SDLC artifacts."""
    if task.id.startswith("bug.") or "open_redirect" in task.id:
        patch = {
            "file": "backend/app/core/guards.py",
            "change": "tighten URL / path guards from prior security finding",
            "tests": ["reject unsafe user-supplied values"],
        }
        publish(wf, task, "bugfix_open_redirect", patch)
        wf.facts["fix_open_redirect"] = True
        return {"summary": "Security guard patch from prior finding"}

    if not wf.facts.get("code_unlocked") and not wf.facts.get("frozen_api"):
        wf.facts["code_unlocked"] = True

    name = product_name(wf)
    openapi = art(wf, "openapi") or {}
    schema_ddl = art(wf, "schema_ddl") or {}
    hld = art(wf, "hld") or {}
    lld = art(wf, "lld") or {}
    findings = collect_security_findings(
        {
            "security_review": art(wf, "security_review") or {},
            "threat_model": art(wf, "threat_model") or {},
            "review_report": art(wf, "review_report") or {},
        }
    )
    needs = derive_security_needs(
        openapi=openapi if isinstance(openapi, dict) else {},
        findings=findings,
        hld=hld if isinstance(hld, dict) else {},
    )

    llm = run_llm_agent(
        wf,
        task,
        agent="backend",
        inputs={
            "hld": hld,
            "lld": lld,
            "openapi": openapi,
            "schema_ddl": schema_ddl,
            "security_findings": findings,
            "derived_security_needs": needs,
        },
        schema_hint='{"backend_notes":{"patterns":[],"modules":[],"risks":[]}}',
        system_extra=(
            "Analyze HLD/LLD/OpenAPI/DB and prior security findings. "
            "Return concise backend notes only — Forge emits the FastAPI workspace on disk "
            "using derived_security_needs (do not invent a product-specific stack)."
        ),
    )

    root = ensure_workspace(wf.id)
    backend_files, control_lines = generate_fastapi_backend(
        root=root,
        product=name,
        openapi=openapi if isinstance(openapi, dict) else {},
        schema_ddl=schema_ddl if isinstance(schema_ddl, dict) else {},
        hld=hld if isinstance(hld, dict) else {},
        lld=lld if isinstance(lld, dict) else {},
        security_findings=findings,
        security_needs=needs,
    )

    tree = {path: "generated FastAPI source" for path in backend_files}
    snippets: dict[str, str] = {}
    for path in backend_files:
        if path.endswith((".py", ".md", ".txt", ".json")):
            full = root / path
            if full.exists() and full.stat().st_size < 40_000:
                snippets[path] = full.read_text(encoding="utf-8")

    existing = art(wf, "source_tree") or {}
    publish(wf, task, "backend_source", tree, bill=False)
    publish(wf, task, "source_tree", {**existing, **tree}, bill=False)
    publish(wf, task, "backend_snippets", snippets, bill=False)
    notes = (llm or {}).get("backend_notes") or {
        "product": name,
        "stack": ["FastAPI", "SQLAlchemy", "Pydantic", "Uvicorn"],
        "patterns": [
            "app factory + lifespan",
            "router → domain service → session",
            "OpenAPI-derived routes",
            "security guards derived from prior findings + OpenAPI shape",
        ],
        "workspace": str(root / "backend"),
    }
    if isinstance(notes, dict):
        notes = {
            **notes,
            "security_controls": control_lines,
            "security_needs": needs.get("needed") or [],
            "security_reasons": needs.get("reasons") or {},
        }
    publish(wf, task, "backend_notes", notes, bill=False)

    # Status and peer file lists are derived inside the locked merge — this
    # agent runs in parallel with frontend/devops and must not read them here.
    manifest = publish_manifest(
        wf, task, root, product=name, backend_files=backend_files
    )
    ready = manifest["status"] == "READY"
    wf.facts["workspace_path"] = str(root)
    wf.facts["backend_coded"] = True
    wf.facts["security_controls_applied"] = list(needs.get("needed") or [])
    if ready:
        wf.facts["coding_complete"] = True
        wf.facts["coding_notification"] = (
            f"Coding complete — workspace ready at {root} "
            f"({len(backend_files)} backend + "
            f"{len(manifest.get('frontend_files') or [])} frontend files). "
            f"API docs: {manifest.get('run', {}).get('docs')}"
        )
    else:
        wf.facts["coding_notification"] = (
            f"Backend coding complete — FastAPI workspace at {root / 'backend'} "
            f"({len(backend_files)} files). Waiting for UI…"
        )

    return {
        "summary": (
            f"FastAPI workspace for {name}: {len(backend_files)} files → {root}; "
            f"controls={','.join(needs.get('needed') or [])}"
        ),
        "mode": "workspace",
        "workspace": str(root),
        "files": len(backend_files),
        "security_needs": needs.get("needed") or [],
    }
