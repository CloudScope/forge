from __future__ import annotations

from .models import ApprovalRequest, RiskTier, TaskNode, Workflow, new_id


def build_request(wf: Workflow, task: TaskNode) -> ApprovalRequest:
    product = wf.facts.get("product_name") or "Product"
    if task.id.startswith("approval.plan"):
        plan = wf.artifacts.get("execution_plan")
        stages = len((plan.content or {}).get("stages") or []) if plan else 0
        title = f"Approve {product} engineering plan"
        summary = f"Review epics/stories, risks, and DAG ({stages} planned stages) before parallel execution"
        options = [
            {"id": "approve", "label": "Approve plan — proceed to architecture"},
            {"id": "reject", "label": "Reject — replan"},
        ]
    elif task.id.startswith("approval.arch"):
        title = f"Freeze {product} HLD + ADRs"
        summary = "Human Architecture Review Gate — HLD/LLD HTML; ADRs; capacity"
        options = [
            {"id": "approve", "label": "Approve as proposed"},
            {"id": "reject", "label": "Reject — replan"},
        ]
    elif task.id.startswith("approval.db"):
        schema = wf.artifacts.get("schema_ddl")
        tables = list((schema.content or {}).get("tables") or {}) if schema else []
        title = f"Approve {product} database design"
        summary = (
            "Review Database Design HTML + LLD before API agent starts. "
            f"Tables: {', '.join(tables[:8]) or 'n/a'}"
        )
        options = [
            {"id": "approve", "label": "Approve DB/LLD — start API agent"},
            {"id": "reject", "label": "Reject — rework schema"},
        ]
    elif task.id.startswith("approval.api"):
        openapi = wf.artifacts.get("openapi")
        paths = list((openapi.content or {}).get("paths") or {}) if openapi else []
        title = f"Approve {product} API contract"
        summary = (
            "Review OpenAPI (Swagger) before workspace codegen. "
            f"Endpoints: {', '.join(paths[:8]) or 'n/a'}. "
            "On approve: create workspace and generate FastAPI backend + UI."
        )
        options = [
            {"id": "approve", "label": "Approve API — start backend & UI coding"},
            {"id": "reject", "label": "Reject — rework API"},
        ]
    elif task.id.startswith("approval.release"):
        title = "Production release Go/No-Go"
        summary = "All blocking validation gates must be green"
        options = [
            {"id": "go", "label": "Go"},
            {"id": "nogo", "label": "No-Go"},
        ]
    elif task.id.startswith("approval.clarify"):
        report = wf.artifacts.get("ambiguity_report")
        content = report.content if report else {}
        questions = content.get("questions") or []
        q_preview = "; ".join(str(q) for q in questions[:3]) if questions else "confirm scope & assumptions"
        score = content.get("ambiguity_score")
        title = f"Clarify requirements — {product}"
        summary = (
            f"Review requirement ambiguities before planning continues. "
            f"Score={score if score is not None else 'n/a'}. Focus: {q_preview}"
        )
        options = content.get("options") or [
            {"id": "approve", "label": "Requirements are clear — proceed"},
            {"id": "A", "label": "Essentials scope"},
            {"id": "B", "label": "Product analytics scope"},
            {"id": "C", "label": "Enterprise scope"},
            {"id": "reject", "label": "Still ambiguous — stop"},
        ]
    elif task.id.startswith("approval.coding"):
        ws = wf.facts.get("workspace_path") or "workspace pending"
        note = wf.facts.get("coding_notification") or (
            f"Coding complete — workspace ready at {ws}"
        )
        manifest = wf.artifacts.get("workspace_manifest")
        m = manifest.content if manifest else {}
        be = len(m.get("backend_files") or [])
        fe = len(m.get("frontend_files") or [])
        docs = (m.get("run") or {}).get("docs") or "http://127.0.0.1:8080/docs"
        title = f"Coding complete — {product}"
        summary = (
            f"{note} "
            f"({be} backend + {fe} frontend files). API docs: {docs}"
        )
        options = [
            {"id": "approve", "label": "Approve workspace — continue to tests"},
            {"id": "open_workspace", "label": "Approve & open Workspace"},
            {"id": "reject", "label": "Reject codegen — stop"},
        ]
    elif task.id.startswith("approval.figma"):
        title = f"Figma design input — {product}"
        summary = (
            "Optional human-in-the-loop before UI coding. "
            "Upload a Figma export (PNG/PDF/SVG/JSON) or paste a Figma URL to guide the UI agent, "
            "or continue without Figma and let the agent design from LLD/ReqSpec."
        )
        options = [
            {
                "id": "agent_design",
                "label": "Continue without Figma — design UI from LLD",
            },
            {
                "id": "figma_uploaded",
                "label": "I uploaded Figma — design UI from Figma + LLD",
            },
            {"id": "reject", "label": "Reject — stop"},
        ]
    elif task.id.startswith("approval.migration"):
        title = "Approve production DB migration"
        summary = "CREATE INDEX CONCURRENTLY + rollback plan"
        options = [
            {"id": "approve", "label": "Approve migration"},
            {"id": "reject", "label": "Reject"},
        ]
    else:
        title = f"Approval for {task.id}"
        summary = task.description or task.id
        options = [
            {"id": "approve", "label": "Approve"},
            {"id": "reject", "label": "Reject"},
        ]

    return ApprovalRequest(
        id=new_id("appr"),
        task_id=task.id,
        risk_tier=task.risk_tier if isinstance(task.risk_tier, RiskTier) else RiskTier.HIGH,
        title=title,
        summary=summary,
        options=options,
    )


def auto_decide(req: ApprovalRequest) -> tuple[str, str]:
    """Demo-mode decisions for unattended runs."""
    if req.task_id.startswith("approval.clarify"):
        return "A", "Demo auto-select Option A — Essentials analytics"
    if req.task_id.startswith("approval.release"):
        return "go", "Demo auto Go — validation assumed green"
    if req.task_id.startswith("approval.figma"):
        return "agent_design", "Demo — no Figma; UI agent designs from LLD"
    return "approve", "Demo auto-approve"