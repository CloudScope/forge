from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..models import TaskNode, Workflow
from ._common import art, publish
from .doc_context import product_name, requirement_text

from ..core.paths import paths as forge_paths

DELIVERABLES = forge_paths().deliverables


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def engineering_summary(wf: Workflow, task: TaskNode) -> dict[str, Any]:
    """16. Final Deliverables — engineering summary + materialized artifact pack."""
    name = product_name(wf)
    pack = {
        "requirement_analysis": art(wf, "reqspec"),
        "intake_record": art(wf, "intake_record"),
        "product_brief": art(wf, "product_brief"),
        "engineering_plan": art(wf, "execution_plan"),
        "task_breakdown": art(wf, "task_breakdown"),
        "dependency_graph": art(wf, "dependency_graph"),
        "dag_design_html": art(wf, "dag_design_html"),
        "hld": art(wf, "hld"),
        "lld": art(wf, "lld"),
        "adrs": art(wf, "adrs"),
        "architecture_design_html": art(wf, "architecture_design_html"),
        "lld_design_html": art(wf, "lld_design_html"),
        "database_design_html": art(wf, "database_design_html"),
        "database_design": art(wf, "schema_ddl"),
        "api_specification": art(wf, "openapi"),
        "backend_source": art(wf, "backend_source"),
        "frontend_source": art(wf, "frontend_source"),
        "frontend_design_html": art(wf, "frontend_design_html"),
        "source_tree": art(wf, "source_tree"),
        "backend_snippets": art(wf, "backend_snippets"),
        "infra": art(wf, "infra"),
        "test_plan": art(wf, "test_plan"),
        "validation_report": art(wf, "validation_report"),
        "engineering_validation": art(wf, "engineering_validation"),
        "security_review": art(wf, "security_review"),
        "security_scan": art(wf, "security_scan"),
        "documentation": art(wf, "documentation"),
        "release_notes": art(wf, "release_notes"),
        "deployment_recommendation": art(wf, "deployment_recommendation"),
        "observability_plan": art(wf, "observability_plan"),
        "risk_register": art(wf, "risk_register"),
    }

    approvals = [
        {
            "title": a.title,
            "status": a.status,
            "decision": a.decision,
            "rationale": a.rationale,
            "task_id": a.task_id,
        }
        for a in wf.approvals
    ]
    summary = {
        "product": name,
        "workflow_id": wf.id,
        "playbook_id": wf.playbook_id,
        # Terminal node runs while workflow is still RUNNING; report success intent.
        "status": "SUCCEEDED"
        if wf.status.value in ("RUNNING", "SUCCEEDED")
        else wf.status.value,
        "principle": (
            "Agents execute with controlled autonomy within defined boundaries. "
            "Humans own oversight, approvals, and final quality."
        ),
        "llm": {
            "enabled": wf.facts.get("llm_enabled"),
            "model": wf.facts.get("llm_model"),
            "calls": wf.budgets.get("llm_calls"),
        },
        "budgets": wf.budgets,
        "approvals": approvals,
        "deliverable_keys": [k for k, v in pack.items() if v is not None],
        "stages_completed": [
            tid
            for tid, n in wf.tasks.items()
            if n.status.value == "SUCCEEDED"
        ],
        "reference_workload": "URL Shortener Platform",
    }

    # Materialize production deliverable pack on disk
    out = DELIVERABLES / wf.id
    mapping = {
        "01-requirement-analysis": ["intake_record", "requirement_analysis", "product_brief"],
        "02-engineering-plan": ["engineering_plan", "task_breakdown", "dependency_graph", "risk_register"],
        "03-architecture": [
            "hld",
            "lld",
            "adrs",
            "architecture_design_html",
            "lld_design_html",
        ],
        "04-database": ["database_design", "database_design_html"],
        "05-api": ["api_specification"],
        "06-code": [
            "backend_source",
            "frontend_source",
            "frontend_design_html",
            "source_tree",
            "backend_snippets",
        ],
        "07-tests": ["test_plan"],
        "08-validation-security": [
            "validation_report",
            "engineering_validation",
            "security_review",
            "security_scan",
        ],
        "09-documentation": ["documentation"],
        "10-deployment": ["infra", "release_notes", "deployment_recommendation", "observability_plan"],
    }
    written: list[str] = []
    for folder, keys in mapping.items():
        for key in keys:
            data = pack.get(key)
            if data is None:
                continue
            rel = f"{folder}/{key}.json"
            _write_json(out / rel, data)
            written.append(rel)

    # Snippets as source files when present
    snippets = pack.get("backend_snippets") or {}
    if isinstance(snippets, dict):
        for path, code in snippets.items():
            if isinstance(code, str) and code.strip():
                rel = f"06-code/src/{path}"
                _write_text(out / rel, code)
                written.append(rel)

    # Materialize HTML designs as real .html files (not JSON-only)
    arch_html = pack.get("architecture_design_html")
    if isinstance(arch_html, str) and arch_html.strip():
        rel = "03-architecture/architecture_design.html"
        _write_text(out / rel, arch_html)
        written.append(rel)
    lld_html = pack.get("lld_design_html")
    if isinstance(lld_html, str) and lld_html.strip():
        rel = "03-architecture/lld_design.html"
        _write_text(out / rel, lld_html)
        written.append(rel)
    db_html = pack.get("database_design_html")
    if isinstance(db_html, str) and db_html.strip():
        rel = "04-database/database_design.html"
        _write_text(out / rel, db_html)
        written.append(rel)
    fe_html = pack.get("frontend_design_html")
    if isinstance(fe_html, str) and fe_html.strip():
        rel = "06-code/frontend_design.html"
        _write_text(out / rel, fe_html)
        written.append(rel)
    fe_pages = pack.get("frontend_source") or {}
    if isinstance(fe_pages, dict):
        for path, code in fe_pages.items():
            if not isinstance(code, str) or len(code.strip()) < 8:
                continue
            low = path.lower()
            is_web = (
                code.lstrip().lower().startswith("<!doctype")
                or code.lstrip().lower().startswith("<html")
                or low.endswith(
                    (
                        ".css",
                        ".tsx",
                        ".ts",
                        ".jsx",
                        ".js",
                        ".json",
                        ".html",
                        ".md",
                    )
                )
            )
            if is_web:
                rel = f"06-code/frontend/{path.lstrip('/')}"
                _write_text(out / rel, code)
                written.append(rel)

    dag = (art(wf, "forge_dag_spec") or {}).get("mermaid") or ""
    if not dag and pack.get("dependency_graph"):
        dag = (pack["dependency_graph"] or {}).get("mermaid") or ""
    if dag:
        _write_text(out / "forge_dag.mmd", dag)
        written.append("forge_dag.mmd")

    md = [
        f"# Engineering Summary — {name}",
        "",
        f"- Workflow: `{wf.id}`",
        f"- Playbook: `{wf.playbook_id}`",
        f"- Status: **{wf.status.value}**",
        f"- LLM: `{summary['llm']}`",
        "",
        "## Principle",
        summary["principle"],
        "",
        "## Approvals",
    ]
    for a in approvals:
        md.append(f"- {a['title']}: {a['status']} ({a.get('decision')})")
    md += [
        "",
        "## Deliverables",
        *[f"- `{p}`" for p in written],
        "",
        "## Source PRD excerpt",
        "```",
        (requirement_text(wf) or "")[:1500],
        "```",
        "",
    ]
    _write_text(out / "ENGINEERING_SUMMARY.md", "\n".join(md))
    written.append("ENGINEERING_SUMMARY.md")

    summary["materialized_path"] = str(out)
    summary["materialized_files"] = written
    publish(wf, task, "engineering_summary", summary)
    publish(wf, task, "final_deliverables", {"path": str(out), "files": written})
    return {
        "summary": f"Engineering summary materialized → {out} ({len(written)} files)"
    }
