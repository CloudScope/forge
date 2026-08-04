from __future__ import annotations

import io
import json
import threading
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, Response

from .approval_gates import is_known_decision, sanitize_options
from .auth import StudioAuthMiddleware, auth_mode
from .core.paths import ensure_runtime_dirs, paths as forge_paths
from .execution import get_launcher, using_step_functions
from .storage import (
    artifact_prefix,
    document_store,
    latest_version,
    object_store,
    workspace_prefix,
)

_P = forge_paths()
ROOT = _P.root
STATE = _P.state
ARTIFACTS = _P.artifacts
EXAMPLES = _P.examples
WORKSPACES = _P.workspaces
DELIVERABLES = _P.deliverables
UPLOADS = _P.uploads

@asynccontextmanager
async def _lifespan(_: FastAPI):
    ensure_runtime_dirs()
    try:
        from dotenv import load_dotenv

        load_dotenv(_P.env_file)
    except Exception:
        pass
    # Resolve SSM-referenced secrets before auth or the LLM adapter read them.
    from .secrets import hydrate

    hydrate()
    yield


app = FastAPI(
    title="Forge Agentic SDLC Studio", version="0.5.0", lifespan=_lifespan
)
app.add_middleware(StudioAuthMiddleware)

_run_lock = threading.Lock()
_active_runs: dict[str, str] = {}
_live_runs: dict[str, Any] = {}  # wf_id -> {"engine", "wf", "runtime"?}


def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _latest_artifact(workflow_id: str, key: str) -> Any:
    """Latest version of one artifact, from wherever objects are stored."""
    store = object_store()
    prefix = artifact_prefix(workflow_id)
    chosen = latest_version(store.list_keys(prefix), key)
    if chosen is None:
        return None
    raw = store.get_text(chosen)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _artifact_keys(workflow_id: str) -> list[str]:
    """Distinct artifact names present for a workflow."""
    names = set()
    for key in object_store().list_keys(artifact_prefix(workflow_id)):
        name = key.rsplit("/", 1)[-1]
        if name.startswith("_") or ".v" not in name or not name.endswith(".json"):
            continue
        names.add(name[: -len(".json")].rpartition(".v")[0])
    return sorted(names)


def _workflow_doc(workflow_id: str) -> dict[str, Any] | None:
    """Stored workflow document — the read model for every GET endpoint."""
    from .storage import WORKFLOWS

    return document_store().get(WORKFLOWS, workflow_id)


def _list_workflows() -> list[dict[str, Any]]:
    from .storage import WORKFLOWS

    docs = document_store().list_docs(WORKFLOWS)
    items = []
    for data in sorted(
        (d for d in docs if d.get("id")),
        key=lambda d: d.get("updated_at") or 0,
        reverse=True,
    ):
        facts = data.get("facts") or {}
        items.append(
            {
                "id": data["id"],
                "playbook_id": data.get("playbook_id"),
                "status": data.get("status"),
                "checkpoint_seq": data.get("checkpoint_seq"),
                "budgets": data.get("budgets"),
                "metrics": data.get("metrics") or {},
                "task_count": len(data.get("tasks", {})),
                "approval_count": len(data.get("approvals", [])),
                "product_name": facts.get("product_name"),
                "filename": facts.get("requirement_filename"),
                "from_document": bool(facts.get("from_document")),
            }
        )
    return items


RESULT_ARTIFACT_KEYS = [
    "intake_record",
    "document_summary",
    "reqspec",
    "product_brief",
    "execution_plan",
    "dag_design_html",
    "dependency_graph",
    "architecture_design_html",
    "lld_design_html",
    "hld_html",
    "hld",
    "lld",
    "adrs",
    "database_design_html",
    "schema_ddl",
    "openapi",
    "workspace_manifest",
    "frontend_design_html",
    "ui_design_html",
    "backend_verdict",
    "backend_source",
    "backend_snippets",
    "frontend_source",
    "source_tree",
    "infra",
    "test_plan",
    "security_review",
    "security_scan",
    "documentation",
    "engineering_validation",
    "validation_report",
    "release_notes",
    "deployment_recommendation",
    "observability_plan",
    "engineering_summary",
    "final_deliverables",
]


@app.get("/api/health")
def health() -> dict[str, Any]:
    from .graph import langgraph_available, use_langgraph
    from .graph.checkpointing import build_checkpointer
    from .graph.tracing import configure_langsmith
    from .llm import load_config, llm_enabled

    cfg = load_config()
    ls = configure_langsmith()
    checkpointer: dict[str, Any] = {"type": "n/a"}
    if langgraph_available():
        _, checkpointer = build_checkpointer()
    from .execution import health as execution_health
    from .storage import health as storage_health

    return {
        "status": "ok",
        "service": "forge-studio",
        "auth": auth_mode(),
        "storage": storage_health(),
        "execution": execution_health(),
        "orchestrator": "langgraph" if use_langgraph() else "legacy",
        "langgraph_installed": langgraph_available(),
        "checkpointer": checkpointer,
        "langsmith": ls,
        "llm": {
            "enabled": llm_enabled(),
            "model": cfg.model if llm_enabled() else None,
            "base_url": cfg.base_url if llm_enabled() else None,
            "mode": "llm" if llm_enabled() else "heuristic_fallback",
        },
    }


@app.get("/api/agents")
def agents() -> list[dict[str, str]]:
    from .agents import AGENT_ROSTER

    return [{"id": i, "mission": m} for i, m in AGENT_ROSTER]


def _record_run_state(wf_id: str, state: str) -> None:
    with _run_lock:
        if state == "FINISHED":
            live = _live_runs.get(wf_id) or {}
            wf = live.get("wf")
            status = getattr(getattr(wf, "status", None), "value", None) if wf else None
            state = "WAITING_APPROVAL" if status == "WAITING_APPROVAL" else "FINISHED"
        _active_runs[wf_id] = state


def _start_workflow_thread(wf_id: str, runner) -> None:
    """Begin execution through the configured launcher.

    Locally that is a daemon thread; on AWS the launcher starts a Step Functions
    execution and `runner` is never invoked in this process."""
    get_launcher(_record_run_state).start(wf_id, runner=runner)


def _register_live(engine: Any, wf: Any, runtime: Any = None) -> None:
    _live_runs[wf.id] = {"engine": engine, "wf": wf, "runtime": runtime}


def _make_orchestrator(auto_approve: bool = True, max_workers: int = 4) -> dict[str, Any]:
    """Create LangGraph runtime (default) or legacy engine."""
    from .graph import LangGraphRuntime, use_langgraph

    # Studio: never CLI-demo-auto plan/arch; never block uvicorn on stdin.
    common = dict(
        auto_approve=auto_approve,
        max_workers=max_workers,
        cli_demo_mode=False,
        allow_stdin_prompt=False,
    )
    if use_langgraph():
        runtime = LangGraphRuntime(**common)
        return {
            "engine": runtime.engine,
            "runtime": runtime,
            "orchestrator": "langgraph",
        }
    from .engine import OrchestrationEngine

    engine = OrchestrationEngine(**common)
    return {"engine": engine, "runtime": None, "orchestrator": "legacy"}


def _live_gate(live: dict[str, Any] | None) -> bool:
    """True when a cached run actually holds an open gate."""
    wf = (live or {}).get("wf")
    return bool(wf and any(a.status == "REQUESTED" for a in wf.approvals))


def _ensure_live_paused(workflow_id: str) -> dict[str, Any]:
    """Return live engine+wf for a paused gate; rehydrate from disk if needed."""
    live = _live_runs.get(workflow_id)
    # The cached object is only trustworthy while it still shows the gate *and*
    # the stored document agrees a gate is open. On AWS the run advances inside
    # Fargate, so a warm Lambda keeps serving whatever it last saw: a snapshot
    # from before the gate opened hides the gate, and one from before the run
    # failed accepts decisions on a workflow that is already dead.
    doc_status = (_workflow_doc(workflow_id) or {}).get("status")
    if (
        live
        and live.get("engine")
        and _live_gate(live)
        and doc_status in (None, "WAITING_APPROVAL")
    ):
        return live
    _live_runs.pop(workflow_id, None)
    from .approval import build_request
    from .models import WorkflowStatus

    orch = _make_orchestrator(auto_approve=False)
    engine = orch["engine"]
    runtime = orch["runtime"]
    wf = (runtime.rehydrate(workflow_id) if runtime else engine.rehydrate(workflow_id))
    if wf is None:
        raise HTTPException(404, "Workflow not found")
    if wf.status != WorkflowStatus.WAITING_APPROVAL:
        raise HTTPException(
            409,
            f"Workflow is {wf.status.value}, not waiting for approval.",
        )
    pending = [a for a in wf.approvals if a.status == "REQUESTED"]
    if not pending:
        raise HTTPException(409, "No pending approval request on this workflow.")
    for req in pending:
        if req.options and req.summary:
            continue
        node = wf.tasks.get(req.task_id)
        if not node:
            continue
        rebuilt = build_request(wf, node)
        if not req.options:
            req.options = rebuilt.options
        if not req.summary:
            req.summary = rebuilt.summary
        if not req.title:
            req.title = rebuilt.title
    _register_live(engine, wf, runtime)
    _active_runs[workflow_id] = "WAITING_APPROVAL"
    return _live_runs[workflow_id]


@app.post("/api/workflows/from-document")
async def workflow_from_document(
    file: UploadFile = File(...),
    auto_approve: bool = Form(False),
) -> dict[str, Any]:
    """Upload a requirement document and run the full multi-agent SDLC DAG."""
    from .doc_ingest import ALLOWED_SUFFIXES, extract_text, save_upload, summarize_document

    filename = file.filename or "requirement.md"
    suffix = Path(filename).suffix.lower()
    if suffix and suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(
            400,
            f"Unsupported file type '{suffix}'. Use: {', '.join(sorted(ALLOWED_SUFFIXES))}",
        )
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty upload")
    try:
        path = save_upload(filename, data)
        text = extract_text(path, data)
        summary = summarize_document(text)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    orch = _make_orchestrator(auto_approve=auto_approve)
    engine, runtime = orch["engine"], orch["runtime"]
    wf = engine.prepare_from_document(text=text, filename=filename, summary=summary)
    engine.store.save_workflow(wf)
    _register_live(engine, wf, runtime)

    def _run() -> None:
        if runtime is not None:
            runtime.register(wf)
            runtime.start(wf)
        else:
            engine.run(wf)

    _start_workflow_thread(wf.id, _run)
    return {
        "workflow_id": wf.id,
        "status": "STARTED",
        "playbook_id": wf.playbook_id,
        "filename": filename,
        "product_name": summary.get("product_name"),
        "features_detected": summary.get("features"),
        "fr_lines": len(summary.get("fr_lines") or []),
        "auto_approve": auto_approve,
        "orchestrator": orch["orchestrator"],
        "message": "Orchestrator started all agents from uploaded requirement document",
        "observe_url": f"/?wf={wf.id}",
    }


@app.post("/api/workflows/from-example")
def workflow_from_example(
    name: str = Form("tinyurl-requirements.md"),
    auto_approve: bool = Form(False),
) -> dict[str, Any]:
    """Run SDLC using a bundled example PRD (TinyURL)."""
    from .doc_ingest import extract_text, summarize_document

    path = EXAMPLES / Path(name).name
    if not path.exists():
        raise HTTPException(404, f"Example not found: {name}")
    data = path.read_bytes()
    text = extract_text(path, data)
    summary = summarize_document(text)
    orch = _make_orchestrator(auto_approve=auto_approve)
    engine, runtime = orch["engine"], orch["runtime"]
    wf = engine.prepare_from_document(text=text, filename=path.name, summary=summary)
    engine.store.save_workflow(wf)
    _register_live(engine, wf, runtime)

    def _run() -> None:
        if runtime is not None:
            runtime.register(wf)
            runtime.start(wf)
        else:
            engine.run(wf)

    _start_workflow_thread(wf.id, _run)
    return {
        "workflow_id": wf.id,
        "status": "STARTED",
        "filename": path.name,
        "product_name": summary.get("product_name"),
        "auto_approve": auto_approve,
        "orchestrator": orch["orchestrator"],
        "observe_url": f"/?wf={wf.id}",
    }


@app.get("/api/workflows/{workflow_id}/pending-approval")
def pending_approval(workflow_id: str) -> dict[str, Any]:
    live = _live_runs.get(workflow_id)
    wf_data = _workflow_doc(workflow_id)
    if not wf_data and not live:
        raise HTTPException(404, "Workflow not found")
    approvals: list[dict[str, Any]] = []
    status = (wf_data or {}).get("status")
    # The stored status is the authority on whether a gate is open. A run that
    # has failed or moved on may still carry a REQUESTED approval — in its cached
    # object, in its document, or both — and offering that as a live gate invites
    # a click that answers a question nobody is asking.
    if wf_data and status != "WAITING_APPROVAL":
        _live_runs.pop(workflow_id, None)
        return {
            "workflow_id": workflow_id,
            "status": status,
            "pending": [],
            "gate_note": "No gate is open on this workflow.",
        }
    # It says WAITING_APPROVAL but the cache has no gate: rehydrate, or the Studio
    # renders no buttons for a run parked waiting for exactly that click.
    if status == "WAITING_APPROVAL" and not _live_gate(live):
        try:
            live = _ensure_live_paused(workflow_id)
        except HTTPException:
            live = None
    if live and live.get("wf"):
        approvals = [
            {
                "id": a.id,
                "task_id": a.task_id,
                "title": a.title,
                "summary": a.summary,
                "options": sanitize_options(a.options)
                or [
                    {"id": "approve", "label": "Approve"},
                    {"id": "reject", "label": "Reject"},
                ],
                "status": a.status,
            }
            for a in live["wf"].approvals
            if a.status == "REQUESTED"
        ]
        status = live["wf"].status.value
    else:
        raw = [
            a
            for a in (wf_data or {}).get("approvals") or []
            if a.get("status") == "REQUESTED"
        ]
        for a in raw:
            opts = sanitize_options(a.get("options")) or [
                {"id": "approve", "label": "Approve"},
                {"id": "reject", "label": "Reject"},
            ]
            approvals.append(
                {
                    "id": a.get("id"),
                    "task_id": a.get("task_id"),
                    "title": a.get("title") or a.get("task_id"),
                    "summary": a.get("summary") or "",
                    "options": opts,
                    "status": a.get("status"),
                }
            )
        status = (wf_data or {}).get("status")
    task_id = ""
    if approvals:
        task_id = str(approvals[0].get("task_id") or "")
    if task_id.startswith("approval.api"):
        gate_note = "After approval.api is approved, Forge creates a workspace and codes FastAPI backend + UI."
    elif task_id.startswith("approval.db"):
        gate_note = "After approval.db is approved, the API agent starts."
    elif task_id.startswith("approval.arch"):
        gate_note = "After architecture is approved, database design starts."
    elif task_id.startswith("approval.plan"):
        gate_note = "After the plan is approved, architecture design starts."
    elif task_id.startswith("approval.clarify"):
        gate_note = (
            "Clarify requirements before planning. "
            "Pick a scope option or confirm requirements are clear."
        )
    elif task_id.startswith("approval.coding"):
        gate_note = (
            "Backend + frontend workspace is ready. "
            "Approve to continue testing/validation, or open Workspace in Results."
        )
    elif task_id.startswith("approval.figma"):
        gate_note = (
            "Optional Figma before UI coding. Upload a Figma export/URL and continue, "
            "or continue without Figma so the UI agent designs from LLD/ReqSpec."
        )
    else:
        gate_note = "Human gate — approve to continue the SDLC pipeline."
    return {
        "workflow_id": workflow_id,
        "status": status,
        "pending": approvals,
        "gate_note": gate_note,
    }


@app.post("/api/workflows/{workflow_id}/figma")
async def upload_figma_design(
    workflow_id: str,
    file: UploadFile | None = File(None),
    figma_url: str = Form(""),
    notes: str = Form(""),
) -> dict[str, Any]:
    """Attach optional Figma export/URL while paused at approval.figma."""
    live = _ensure_live_paused(workflow_id)
    engine = live["engine"]
    wf = live["wf"]
    url = (figma_url or "").strip()
    note = (notes or "").strip()
    saved_files: list[str] = []
    figma_dir = WORKSPACES / workflow_id / "figma"
    figma_dir.mkdir(parents=True, exist_ok=True)

    if file is not None and file.filename:
        raw = await file.read()
        if not raw:
            raise HTTPException(400, "Empty Figma upload")
        name = Path(file.filename).name
        suffix = Path(name).suffix.lower()
        if suffix not in {
            ".png",
            ".jpg",
            ".jpeg",
            ".webp",
            ".pdf",
            ".svg",
            ".json",
            ".fig",
            ".zip",
        }:
            raise HTTPException(
                400,
                "Unsupported Figma file type — use png/jpg/webp/pdf/svg/json/fig/zip",
            )
        dest = figma_dir / name
        dest.write_bytes(raw)
        saved_files.append(str(dest.relative_to(WORKSPACES / workflow_id)))

    if not saved_files and not url:
        raise HTTPException(400, "Provide a Figma file upload and/or figma_url")

    payload = {
        "provided": True,
        "url": url,
        "files": saved_files,
        "notes": note,
        "mode": "figma",
    }
    wf.facts["figma_provided"] = True
    wf.facts["figma_url"] = url
    wf.facts["figma_files"] = saved_files
    wf.facts["figma_notes"] = note
    wf.facts["figma_mode"] = "figma"
    task = wf.tasks.get("approval.figma")
    if task is not None:
        from .agents._common import publish

        publish(wf, task, "figma_design", payload, bill=False)
    else:
        from .models import Artifact
        import time as _time
        import hashlib

        content = payload
        raw = json.dumps(content, sort_keys=True, default=str).encode("utf-8")
        wf.artifacts["figma_design"] = Artifact(
            key="figma_design",
            version=1,
            task_id="approval.figma",
            content=content,
            content_hash=hashlib.sha256(raw).hexdigest()[:16],
            created_at=_time.time(),
        )
    engine.store.checkpoint(wf)
    engine.store.save_workflow(wf)
    return {
        "workflow_id": workflow_id,
        "figma": payload,
        "message": "Figma attached — choose “I uploaded Figma” to continue UI coding",
    }


@app.post("/api/workflows/{workflow_id}/approve")
async def approve_workflow(
    workflow_id: str,
    payload: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    """Submit human decision for a paused gate (e.g. approval.db → unlock API)."""
    live = _ensure_live_paused(workflow_id)
    engine = live["engine"]
    wf = live["wf"]
    runtime = live.get("runtime")
    decision = str(payload.get("decision") or "").strip()
    rationale = str(payload.get("rationale") or "Approved in Forge Studio")
    if not decision:
        raise HTTPException(400, "decision is required")
    # The engine fails the workflow on anything it cannot approve, so an id from
    # a stale or malformed gate menu must be refused here rather than resumed.
    if not is_known_decision(decision):
        raise HTTPException(400, f"Unknown decision '{decision}' — cannot resume this gate")

    def _resume() -> None:
        if runtime is not None:
            runtime.resume_approval(
                wf,
                decision=decision,
                rationale=rationale,
                approval_id=payload.get("approval_id"),
                task_id=payload.get("task_id"),
            )
        else:
            engine.submit_approval(
                wf,
                decision=decision,
                rationale=rationale,
                approval_id=payload.get("approval_id"),
                task_id=payload.get("task_id"),
            )

    launcher = get_launcher(_record_run_state)

    if using_step_functions():
        # Record the decision here, before any handoff. Execution belongs to a
        # Fargate segment, but the *decision* must not: this Lambda cannot run a
        # workflow, and a background thread does not survive the response being
        # returned. Persisting first also makes the gate idempotent — a worker
        # that starts without `--decision` rehydrates a state where the gate is
        # already approved and simply carries on instead of re-pausing on it.
        engine.record_approval(
            wf,
            decision=decision,
            rationale=rationale,
            approval_id=payload.get("approval_id"),
            task_id=payload.get("task_id"),
        )
        released = launcher.resume(
            workflow_id,
            {
                "decision": decision,
                "rationale": rationale,
                "approval_id": payload.get("approval_id"),
                "task_id": payload.get("task_id"),
            },
        )
        if not released.get("handled"):
            # No parked execution to release (token lost, or it timed out), so
            # nothing would continue the run. Start a fresh execution; the worker
            # resumes from the decision just persisted.
            released = launcher.start(workflow_id)
    else:
        # In-process: the same thread records the decision and drives the run.
        released = launcher.resume(
            workflow_id,
            {
                "decision": decision,
                "rationale": rationale,
                "approval_id": payload.get("approval_id"),
                "task_id": payload.get("task_id"),
            },
        )
        if not released.get("handled"):
            _start_workflow_thread(workflow_id, _resume)

    return {
        "workflow_id": workflow_id,
        "status": "RESUMING",
        "decision": decision,
        "orchestrator": "langgraph" if runtime is not None else "legacy",
        "execution": released.get("mode"),
        "message": "Approval submitted — orchestrator resuming",
    }


@app.get("/api/workflows")
def workflows() -> list[dict[str, Any]]:
    return _list_workflows()


def _build_dag_html_for_workflow(workflow_id: str) -> str:
    """Build pure HTML/CSS DAG page from workflow tasks (no Mermaid)."""
    from .agents.design_html import build_dag_html

    wf = _workflow_doc(workflow_id)
    if not wf:
        raise HTTPException(404, "Workflow not found")
    facts = wf.get("facts") or {}
    dep = _latest_artifact(workflow_id, "dependency_graph") or {}
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    for tid, t in (wf.get("tasks") or {}).items():
        nodes.append(
            {
                "id": tid,
                "agent": t.get("agent"),
                "type": t.get("type") or "COMPUTE",
                "risk_tier": t.get("risk_tier") or "LOW",
                "status": t.get("status"),
                "description": t.get("description") or "",
            }
        )
        for d in t.get("deps") or []:
            edges.append({"from": d, "to": tid})
    gates = [
        {
            "id": n["id"],
            "kind": "human" if n["type"] == "APPROVAL" else "sync",
            "purpose": n.get("description") or n["id"],
        }
        for n in nodes
        if n["type"] in ("APPROVAL", "BARRIER")
    ]
    return build_dag_html(
        product=str(facts.get("product_name") or "Forge"),
        workflow_id=workflow_id,
        playbook_id=str(wf.get("playbook_id") or ""),
        status=str(wf.get("status") or ""),
        nodes=nodes,
        edges=edges,
        gates=gates,
        parallel_waves=(dep.get("parallel_waves") if isinstance(dep, dict) else None) or [],
    )


@app.get("/api/workflows/{workflow_id}")
def workflow_detail(workflow_id: str) -> dict[str, Any]:
    data = _workflow_doc(workflow_id)
    if not data:
        raise HTTPException(404, "Workflow not found")
    summary = _read_json(ARTIFACTS / workflow_id / "_summary.json")
    return {"workflow": data, "summary": summary}


def _zip_workflow(workflow_id: str) -> bytes:
    """
    Everything a run produced, as one archive.

    Read through the object store rather than the filesystem so this works
    identically on both backends: locally the store *is* the var tree, and on AWS
    it is the S3 bucket the workers sync their output to. The API host holds no
    copy of either — a Lambda that never ran the workflow still serves the zip.
    """
    doc = _workflow_doc(workflow_id)
    if not doc:
        raise HTTPException(404, "Workflow not found")

    store = object_store()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            f"{workflow_id}/workflow.json", json.dumps(doc, indent=2, default=str)
        )
        for prefix, folder in (
            (artifact_prefix(workflow_id), "artifacts"),
            (workspace_prefix(workflow_id), "workspace"),
        ):
            for key in store.list_keys(prefix):
                blob = store.get_bytes(key)
                if blob is None:
                    continue
                relative = key[len(prefix) :].lstrip("/")
                if not relative:
                    continue
                archive.writestr(f"{workflow_id}/{folder}/{relative}", blob)
    return buffer.getvalue()


@app.get("/api/workflows/{workflow_id}/download")
def download_workflow(workflow_id: str) -> Response:
    """Download every artifact and generated file for a run as a zip."""
    payload = _zip_workflow(workflow_id)
    return Response(
        content=payload,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{workflow_id}_forge.zip"',
            "Content-Length": str(len(payload)),
        },
    )


def _artifact_html_content(workflow_id: str, key: str) -> str | None:
    raw = _latest_artifact(workflow_id, key)
    if not raw:
        return None
    if isinstance(raw, str) and ("<!doctype" in raw.lower() or "<html" in raw.lower()):
        return raw
    if isinstance(raw, dict) and isinstance(raw.get("html"), str):
        return raw["html"]
    return None


def _workspace_manifest(workflow_id: str) -> dict[str, Any]:
    ws_path = WORKSPACES / workflow_id / "workspace_manifest.json"
    data = _read_json(ws_path)
    if data:
        return data
    # Fallback: artifact copy if present
    art = _latest_artifact(workflow_id, "workspace_manifest")
    return art if isinstance(art, dict) else {}


STUDIO_SECTIONS: list[tuple[str, str]] = [
    ("dag", "1. DAG"),
    ("hld", "2. HLD"),
    ("lld", "3. LLD"),
]

# Design pages that must never wrap in the STUDIO_SECTIONS theme chrome.
_BARE_STUDIO_SECTIONS = frozenset({"dag", "hld", "lld", "db", "workspace"})


def _latest_artifact_json(workflow_id: str, key: str) -> Any:
    """
    Kept as the intent-revealing name at its many call sites.

    `_latest_artifact` already parses; the second `_read_json` this used to do
    was a leftover from when it returned a Path, and it raised AttributeError on
    every artifact that actually existed.
    """
    return _latest_artifact(workflow_id, key)


def _raw_section_html(workflow_id: str, section: str) -> str:
    from .agents.design_html import (
        build_architecture_html,
        build_database_html,
        build_lld_html,
        build_workspace_html,
    )

    section = section.lower().strip()
    wf = _workflow_doc(workflow_id) or {}
    facts = wf.get("facts") or {}
    product = str(facts.get("product_name") or "Forge")

    if section == "dag":
        return _build_dag_html_for_workflow(workflow_id)

    if section == "hld":
        hld = _latest_artifact_json(workflow_id, "hld")
        if isinstance(hld, dict):
            lld = _latest_artifact_json(workflow_id, "lld") or {}
            adrs = _latest_artifact_json(workflow_id, "adrs") or []
            if isinstance(adrs, dict):
                adrs = adrs.get("adrs") or adrs.get("items") or []
            return build_architecture_html(
                product=product,
                hld=hld,
                lld=lld if isinstance(lld, dict) else {},
                adrs=adrs if isinstance(adrs, list) else [],
                reqspec=_latest_artifact_json(workflow_id, "reqspec") or {},
                capacity=_latest_artifact_json(workflow_id, "capacity_model") or {},
                perf_budget=_latest_artifact_json(workflow_id, "perf_budget") or {},
                sequences=_latest_artifact_json(workflow_id, "sequence_flows") or {},
            )
        html = _artifact_html_content(workflow_id, "architecture_design_html")
        if html:
            return html
        raise HTTPException(404, "HLD design HTML not produced yet")

    if section == "lld":
        lld = _latest_artifact_json(workflow_id, "lld")
        if isinstance(lld, dict):
            return build_lld_html(
                product=product,
                lld=lld,
                hld=_latest_artifact_json(workflow_id, "hld") or {},
                reqspec=_latest_artifact_json(workflow_id, "reqspec") or {},
            )
        html = _artifact_html_content(workflow_id, "lld_design_html")
        if html:
            return html
        raise HTTPException(404, "LLD design HTML not produced yet")

    if section == "db":
        schema = _latest_artifact_json(workflow_id, "schema_ddl")
        if isinstance(schema, dict):
            return build_database_html(
                product=product,
                schema=schema,
                migration=_latest_artifact_json(workflow_id, "migration_plan") or {},
                sharding=(
                    _latest_artifact_json(workflow_id, "sharding_strategy")
                    or _latest_artifact_json(workflow_id, "sharding_plan")
                    or {}
                ),
                index_plan=_latest_artifact_json(workflow_id, "index_plan") or {},
            )
        html = _artifact_html_content(workflow_id, "database_design_html")
        if html:
            return html
        raise HTTPException(404, "DB design HTML not produced yet")

    if section == "workspace":
        manifest = _workspace_manifest(workflow_id)
        if not manifest:
            raise HTTPException(404, "Workspace not created yet")
        return build_workspace_html(manifest)
    raise HTTPException(404, f"Unknown section '{section}'")


@app.get("/api/workflows/{workflow_id}/dag.html")
def workflow_dag_html_compat(workflow_id: str) -> Response:
    """Back-compat alias — older clients/caches still request /dag.html."""
    return Response(
        status_code=307,
        headers={
            "Location": f"/api/workflows/{workflow_id}/theme/dag.html",
            "Cache-Control": "no-store",
        },
    )


@app.get("/api/workflows/{workflow_id}/raw/{section}.html")
def workflow_raw_section(workflow_id: str, section: str) -> Response:
    """Raw design page (embedded inside theme shell iframe)."""
    try:
        html = _raw_section_html(workflow_id, section)
    except HTTPException as exc:
        html = (
            "<!DOCTYPE html><html><head><meta charset='utf-8'/>"
            "<title>Not ready</title>"
            "<style>body{font-family:system-ui;padding:2rem;color:#334;background:#f6f8fa}"
            "h1{font-size:1.1rem;margin:0 0 .5rem}p{color:#667}</style></head><body>"
            f"<h1>{section.upper()} not ready yet</h1>"
            f"<p>{exc.detail}</p>"
            "<p>Run or continue the workflow, then refresh this section.</p>"
            "</body></html>"
        )
    return Response(
        content=html,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/workflows/{workflow_id}/theme/{section}.html")
def workflow_theme_section(workflow_id: str, section: str) -> Response:
    """Forge theme chrome (nav + design iframe) — no workspace tree."""
    from .agents.design_html import build_theme_shell_html

    section = section.lower().strip()
    # DAG / HLD / LLD / DB / Workspace: never serve STUDIO_SECTIONS chrome — raw only.
    if section in _BARE_STUDIO_SECTIONS:
        return workflow_raw_section(workflow_id, section)

    titles = {sid: label for sid, label in STUDIO_SECTIONS}
    if section not in titles:
        raise HTTPException(404, f"Unknown theme section '{section}'")
    _raw_section_html(workflow_id, section)

    wf = _workflow_doc(workflow_id) or {}
    facts = wf.get("facts") or {}
    product = str(facts.get("product_name") or "Forge")
    html = build_theme_shell_html(
        workflow_id=workflow_id,
        section=section,
        product=product,
        title=titles[section],
        content_url=f"/api/workflows/{workflow_id}/raw/{section}.html",
        sections=STUDIO_SECTIONS,
    )
    return Response(
        content=html,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/workflows/{workflow_id}/trace")
def workflow_trace(workflow_id: str) -> dict[str, Any]:
    from .audit import AuditTraceStore

    events = AuditTraceStore().read(workflow_id, limit=500)
    return {"workflow_id": workflow_id, "events": events}


@app.get("/api/workflows/{workflow_id}/memory")
def workflow_memory(workflow_id: str) -> dict[str, Any]:
    from .memory import MemoryContextStore

    data = MemoryContextStore().load_workflow_memory(workflow_id)
    if not data:
        raise HTTPException(404, "Memory not found")
    return data


@app.get("/api/agents/memory")
def agent_memories() -> list[dict[str, Any]]:
    from .memory import MemoryContextStore

    return MemoryContextStore().list_agent_memories()


@app.get("/api/workflows/{workflow_id}/results")
def workflow_results(workflow_id: str) -> dict[str, Any]:
    """Return the actual engineering deliverables produced for a workflow."""
    wf = _workflow_doc(workflow_id)
    if not wf:
        raise HTTPException(404, "Workflow not found")
    artifacts: dict[str, Any] = {}
    available = set(_artifact_keys(workflow_id))
    for key in RESULT_ARTIFACT_KEYS:
        if key not in available:
            continue
        raw = _latest_artifact(workflow_id, key)
        # Unwrap {"html": "..."} wrappers for studio rendering
        if isinstance(raw, dict) and isinstance(raw.get("html"), str):
            artifacts[key] = raw["html"]
        else:
            artifacts[key] = raw
    # Repair legacy pseudo-OpenAPI so section "6. API" renders operations
    if isinstance(artifacts.get("openapi"), dict):
        from .agents.api import _normalize_openapi

        product = str((wf.get("facts") or {}).get("product_name") or "Snipr")
        artifacts["openapi"] = _normalize_openapi(artifacts["openapi"], product)
    # Markers for theme iframe viewers (/theme/{section}.html)
    for key, section in (
        ("dag_design_html", "dag"),
        ("architecture_design_html", "hld"),
        ("lld_design_html", "lld"),
        ("database_design_html", "db"),
        ("workspace_manifest", "workspace"),
    ):
        if key == "workspace_manifest":
            manifest = artifacts.get(key) if isinstance(artifacts.get(key), dict) else None
            if not manifest:
                manifest = _workspace_manifest(workflow_id) or None
            if manifest:
                artifacts[key] = {**manifest, "ready": True}
            else:
                artifacts[key] = {"ready": False, "status": "PENDING"}
            continue
        existing = artifacts.get(key)
        ready = existing is not None or (section == "dag") or (
            section in ("hld", "lld", "db")
            and _artifact_html_content(
                workflow_id,
                {
                    "hld": "architecture_design_html",
                    "lld": "lld_design_html",
                    "db": "database_design_html",
                }[section],
            )
            is not None
        )
        artifacts[key] = {"content_type": "text/html", "ready": ready}

    facts = wf.get("facts") or {}
    return {
        "workflow_id": workflow_id,
        "status": wf.get("status"),
        "playbook_id": wf.get("playbook_id"),
        "product_name": facts.get("product_name"),
        "filename": facts.get("requirement_filename"),
        "features": (facts.get("document_summary") or {}).get("features")
        or (artifacts.get("document_summary") or {}).get("features"),
        "approvals": wf.get("approvals") or [],
        "metrics": wf.get("metrics") or {},
        "tasks": {
            tid: {"agent": t.get("agent"), "status": t.get("status"), "description": t.get("description")}
            for tid, t in (wf.get("tasks") or {}).items()
        },
        "artifacts": artifacts,
    }


_FINISHED_STATUSES = frozenset({"SUCCEEDED", "FAILED", "CANCELLED", "PARTIAL"})


def _rmtree(path: Path) -> bool:
    import shutil

    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
        return True
    if path.is_file() or path.is_symlink():
        path.unlink(missing_ok=True)
        return True
    return False


def _wipe_directory(root: Path) -> int:
    """Remove every entry under root (keep the directory itself)."""
    if not root.exists():
        return 0
    removed = 0
    for path in list(root.iterdir()):
        if _rmtree(path):
            removed += 1
    return removed


def _delete_workflow_files(wf_id: str) -> bool:
    """Delete one workflow's state, artifacts, workspace, deliverables and memory."""
    from .audit import AuditTraceStore
    from .memory import MemoryContextStore
    from .state_store import StateStore

    removed_any = StateStore().delete_workflow(wf_id)
    if AuditTraceStore().delete(wf_id):
        removed_any = True
    if MemoryContextStore().delete_workflow_memory(wf_id):
        removed_any = True

    store = object_store()
    for prefix in (
        artifact_prefix(wf_id),
        f"workspaces/{wf_id}",
        f"deliverables/{wf_id}",
    ):
        if store.delete_prefix(prefix):
            removed_any = True
    for key in store.list_keys("uploads"):
        if key.rsplit("/", 1)[-1].startswith(wf_id):
            store.delete(key)
            removed_any = True

    _active_runs.pop(wf_id, None)
    _live_runs.pop(wf_id, None)
    return removed_any


@app.delete("/api/workflows/cleanup")
def cleanup_workflows(finished_only: bool = False, confirm: bool = False) -> dict[str, Any]:
    """
    Remove workflow runs. Use finished_only=true to keep live/active runs.

    A full wipe destroys every workflow, artifact, workspace, deliverable and audit
    trace on this host, so it requires an explicit confirm=true. Accidental
    invocation must not be able to erase the audit trail.
    """
    if not finished_only and not confirm:
        raise HTTPException(
            400,
            "Refusing to wipe all workflow state. Re-send with confirm=true, "
            "or use finished_only=true to keep live runs.",
        )
    from .state_store import StateStore
    from .storage import MEMORY, document_store as _docs

    removed = {"workflows": 0, "artifacts": 0, "workspaces": 0, "deliverables": 0}
    store = object_store()

    if finished_only:
        for data in _list_workflows():
            wf_id = data.get("id")
            status = str(data.get("status") or "")
            if not wf_id or wf_id in _live_runs or wf_id in _active_runs:
                continue
            # Remove finished runs plus stale RUNNING/WAITING leftovers from a
            # previous process; never a run this process is still driving.
            if status not in _FINISHED_STATUSES and status not in (
                "RUNNING",
                "WAITING_APPROVAL",
            ):
                continue
            had_artifacts = bool(store.list_keys(artifact_prefix(wf_id)))
            had_ws = bool(store.list_keys(f"workspaces/{wf_id}"))
            had_del = bool(store.list_keys(f"deliverables/{wf_id}"))
            if _delete_workflow_files(wf_id):
                removed["workflows"] += 1
                removed["artifacts"] += int(had_artifacts)
                removed["workspaces"] += int(had_ws)
                removed["deliverables"] += int(had_del)
        return {"status": "cleaned", "finished_only": True, **removed}

    from .audit import AuditTraceStore

    # Audit streams are keyed per workflow, so drop them before the index goes.
    audit = AuditTraceStore()
    for data in _list_workflows():
        if data.get("id"):
            audit.delete(data["id"])
    wiped = StateStore().delete_everything()
    removed["workflows"] = wiped["workflows"]
    docs = _docs()
    for key in docs.list_keys(MEMORY):
        docs.delete(MEMORY, key)
    removed["artifacts"] = store.delete_prefix("artifacts")
    removed["workspaces"] = store.delete_prefix("workspaces")
    removed["deliverables"] = store.delete_prefix("deliverables")
    store.delete_prefix("uploads")
    _active_runs.clear()
    _live_runs.clear()
    return {"status": "cleaned", "finished_only": False, **removed}


@app.get("/api/platform/overview")
def platform_overview() -> dict[str, Any]:
    from .reliability import aggregate_platform

    wfs = _list_workflows()
    by_status: dict[str, int] = {}
    total_tokens = 0.0
    total_usd = 0.0
    for w in wfs:
        by_status[w["status"]] = by_status.get(w["status"], 0) + 1
        budgets = w.get("budgets") or {}
        total_tokens += float(budgets.get("tokens") or 0)
        total_usd += float(budgets.get("usd_spent") or 0)
    agents = [
        "orchestrator",
        "product",
        "requirement",
        "business_analyst",
        "planner",
        "risk",
        "architecture",
        "database",
        "api",
        "performance",
        "backend",
        "frontend",
        "devops",
        "testing",
        "documentation",
        "observability",
        "security",
        "compliance",
        "reviewer",
        "validation",
        "release",
        "human_approval",
    ]
    from .llm import load_config, llm_enabled

    cfg = load_config()
    reliability = aggregate_platform(wfs)
    return {
        "workflows": len(wfs),
        "by_status": by_status,
        "total_tokens": total_tokens,
        "total_usd": total_usd,
        "agent_roster": agents,
        "recent": wfs[:8],
        "reliability": reliability,
        "llm": {
            "enabled": llm_enabled(),
            "model": cfg.model if llm_enabled() else None,
            "mode": "llm" if llm_enabled() else "heuristic_fallback",
        },
    }


@app.get("/api/platform/reliability")
def platform_reliability() -> dict[str, Any]:
    from .reliability import aggregate_platform

    return aggregate_platform(_list_workflows())


@app.post("/api/workflows/{workflow_id}/safe-stop")
def workflow_safe_stop(workflow_id: str) -> dict[str, Any]:
    """Request orchestrator safe-stop for a live running workflow."""
    live = _live_runs.get(workflow_id)
    if not live:
        raise HTTPException(404, "No live workflow in this studio process")
    engine = live.get("engine")
    if engine is None or not hasattr(engine, "request_safe_stop"):
        raise HTTPException(400, "Engine does not support safe-stop")
    engine.request_safe_stop()
    return {
        "workflow_id": workflow_id,
        "status": "SAFE_STOP_REQUESTED",
        "message": "Orchestrator will finish in-flight workers, compensate side effects, checkpoint as PARTIAL",
    }


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Forge — Agentic SDLC Studio</title>
<link rel="preconnect" href="https://fonts.googleapis.com" />
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=Source+Serif+4:opsz,wght@8..60,500;8..60,700&family=DM+Sans:wght@400;500;600&display=swap" rel="stylesheet" />
<style>
  :root {
    --bg0: #0e1419;
    --bg1: #152028;
    --bg2: #1c2b35;
    --line: #2a3f4d;
    --text: #e6eef2;
    --muted: #8aa0ad;
    --accent: #3ecf8e;
    --warn: #e8b84a;
    --danger: #e86a5a;
    --info: #5eb1e8;
    --chip: #243642;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    min-height: 100vh;
    font-family: "DM Sans", system-ui, sans-serif;
    color: var(--text);
    background:
      radial-gradient(1200px 600px at 10% -10%, #1a3a2a 0%, transparent 55%),
      radial-gradient(900px 500px at 100% 0%, #1a2a3a 0%, transparent 50%),
      linear-gradient(180deg, var(--bg0), #0a1014 100%);
  }
  header {
    padding: 28px 32px 12px;
    display: flex;
    justify-content: space-between;
    align-items: flex-end;
    gap: 16px;
    flex-wrap: wrap;
  }
  .brand {
    font-family: "Source Serif 4", Georgia, serif;
    font-size: 2rem;
    font-weight: 700;
    letter-spacing: -0.02em;
    margin: 0;
  }
  .brand span { color: var(--accent); }
  .sub { color: var(--muted); margin: 6px 0 0; font-size: 0.95rem; }
  .meta {
    font-family: "IBM Plex Mono", monospace;
    font-size: 0.75rem;
    color: var(--muted);
    text-align: right;
  }
  main {
    padding: 12px 32px 40px;
    display: grid;
    grid-template-columns: 320px 1fr;
    gap: 20px;
  }
  @media (max-width: 960px) {
    main { grid-template-columns: 1fr; }
  }
  .panel {
    background: color-mix(in srgb, var(--bg1) 88%, transparent);
    border: 1px solid var(--line);
    border-radius: 14px;
    padding: 16px;
    backdrop-filter: blur(8px);
  }
  .panel h2 {
    margin: 0 0 12px;
    font-size: 0.8rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--muted);
    font-weight: 600;
  }
  .stats {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 12px;
    margin-bottom: 20px;
  }
  @media (max-width: 960px) {
    .stats { grid-template-columns: repeat(2, 1fr); }
  }
  .stat {
    background: var(--bg2);
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 14px;
  }
  .stat .label { color: var(--muted); font-size: 0.75rem; }
  .stat .value {
    font-family: "IBM Plex Mono", monospace;
    font-size: 1.35rem;
    margin-top: 6px;
    color: var(--accent);
  }
  .wf-list { display: flex; flex-direction: column; gap: 10px; max-height: 70vh; overflow: auto; }
  .wf-item {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 8px;
    text-align: center;
    background: linear-gradient(180deg, color-mix(in srgb, var(--bg2) 92%, #1a2a22), var(--bg2));
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 16px 14px 14px;
    color: var(--text);
    cursor: pointer;
    font: inherit;
    width: 100%;
    min-height: 118px;
    transition: border-color 140ms ease, transform 140ms ease, box-shadow 140ms ease;
  }
  .wf-item:hover {
    border-color: color-mix(in srgb, var(--accent) 55%, var(--line));
    transform: translateY(-1px);
  }
  .wf-item.active {
    border-color: var(--accent);
    box-shadow: 0 0 0 1px color-mix(in srgb, var(--accent) 35%, transparent);
  }
  .wf-item.status-ok { border-color: color-mix(in srgb, var(--accent) 45%, var(--line)); }
  .wf-item.status-ok.active { border-color: var(--accent); }
  .wf-item.status-fail { border-color: color-mix(in srgb, var(--danger) 50%, var(--line)); }
  .wf-item.status-run { border-color: color-mix(in srgb, var(--info) 45%, var(--line)); }
  .wf-item.status-wait { border-color: color-mix(in srgb, var(--warn) 55%, var(--line)); }
  .wf-item .wf-product {
    font-family: "Source Serif 4", Georgia, serif;
    font-size: 1.15rem;
    font-weight: 650;
    letter-spacing: -0.02em;
    line-height: 1.2;
  }
  .wf-item .wf-file {
    font-size: 0.78rem;
    line-height: 1.35;
    color: var(--muted);
    overflow-wrap: anywhere;
    word-break: break-word;
    max-width: 100%;
  }
  .wf-item .wf-meta {
    font-family: "IBM Plex Mono", monospace;
    font-size: 0.65rem;
    color: color-mix(in srgb, var(--muted) 80%, transparent);
  }
  .chip {
    display: inline-block;
    padding: 4px 12px;
    border-radius: 999px;
    background: var(--chip);
    font-size: 0.68rem;
    letter-spacing: 0.04em;
    font-family: "IBM Plex Mono", monospace;
  }
  .chip.ok { color: var(--accent); background: color-mix(in srgb, var(--accent) 14%, var(--chip)); }
  .chip.fail { color: var(--danger); background: color-mix(in srgb, var(--danger) 14%, var(--chip)); }
  .chip.run { color: var(--info); background: color-mix(in srgb, var(--info) 14%, var(--chip)); }
  .chip.wait { color: var(--warn); background: color-mix(in srgb, var(--warn) 16%, var(--chip)); }
  .tabs { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; }
  .tab {
    border: 1px solid var(--line);
    background: transparent;
    color: var(--muted);
    border-radius: 999px;
    padding: 6px 12px;
    cursor: pointer;
    font: inherit;
    font-size: 0.85rem;
  }
  .tab.active { color: var(--text); border-color: var(--accent); background: #1a3328; }
  .content { min-height: 420px; }
  pre, .mono {
    font-family: "IBM Plex Mono", monospace;
    font-size: 0.78rem;
    white-space: pre-wrap;
    word-break: break-word;
    margin: 0;
  }
  .timeline { display: flex; flex-direction: column; gap: 8px; max-height: 60vh; overflow: auto; }
  .evt {
    border-left: 2px solid var(--line);
    padding: 6px 0 6px 12px;
  }
  .evt .t { color: var(--muted); font-size: 0.7rem; }
  .evt .type { color: var(--info); }
  .tasks { display: grid; gap: 6px; }
  .task {
    display: grid;
    grid-template-columns: 1.2fr 1fr auto;
    gap: 8px;
    padding: 8px 10px;
    background: var(--bg2);
    border-radius: 8px;
    border: 1px solid var(--line);
    font-size: 0.85rem;
  }
  .empty { color: var(--muted); padding: 24px 0; }
  a { color: var(--info); }
  .upload-wrap { padding: 0 32px 8px; }
  .upload {
    border: 1px dashed color-mix(in srgb, var(--accent) 55%, var(--line));
    background: linear-gradient(135deg, #152820 0%, var(--bg1) 55%, #152028 100%);
    border-radius: 16px;
    padding: 20px 22px;
    display: grid;
    grid-template-columns: 1.4fr 1fr;
    gap: 18px;
    align-items: center;
  }
  @media (max-width: 960px) { .upload { grid-template-columns: 1fr; } }
  .upload h2 {
    margin: 0 0 6px;
    font-family: "Source Serif 4", Georgia, serif;
    font-size: 1.35rem;
  }
  .upload p { margin: 0 0 12px; color: var(--muted); font-size: 0.92rem; }
  .drop {
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 18px;
    text-align: center;
    background: var(--bg2);
    cursor: pointer;
    transition: border-color 120ms ease, transform 120ms ease;
  }
  .drop:hover, .drop.drag { border-color: var(--accent); transform: translateY(-1px); }
  .drop strong { display: block; margin-bottom: 6px; }
  .actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
  .btn {
    border: 1px solid var(--line);
    background: #1a3328;
    color: var(--text);
    border-radius: 999px;
    padding: 8px 14px;
    cursor: pointer;
    font: inherit;
    font-size: 0.88rem;
  }
  .btn.secondary { background: transparent; }
  .btn:disabled { opacity: 0.5; cursor: wait; }
  .status-line { margin-top: 10px; font-family: "IBM Plex Mono", monospace; font-size: 0.75rem; color: var(--info); min-height: 1.2em; }
  .agent-steps { display: flex; flex-wrap: wrap; gap: 6px; }
  .result-nav { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 12px; }
  .result-nav button {
    border: 1px solid var(--line);
    background: var(--bg2);
    color: var(--muted);
    border-radius: 8px;
    padding: 6px 10px;
    cursor: pointer;
    font: inherit;
    font-size: 0.78rem;
  }
  .result-nav button.active { color: var(--accent); border-color: var(--accent); }
  .result-body {
    max-height: 62vh;
    overflow: auto;
    background: transparent;
    border: 0;
    border-radius: 0;
    padding: 0;
  }
  .result-meta { margin-bottom: 12px; color: var(--muted); font-size: 0.85rem; }
  .file-list { display: grid; gap: 4px; margin-top: 8px; }
  .file-list div { font-family: "IBM Plex Mono", monospace; font-size: 0.74rem; color: var(--text); }
  .design-frame {
    width: 100%;
    min-height: 640px;
    border: 0;
    border-radius: 0;
    background: #0b1220;
    margin-top: 0;
    display: block;
  }
  .design-wrap {
    margin-top: 0;
    border: 1px solid var(--line);
    border-radius: 10px;
    overflow: hidden;
    background: var(--panel, var(--bg1));
  }
  .design-toolbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 12px 14px;
    border-bottom: 1px solid var(--line);
    background: color-mix(in srgb, #61affe 8%, transparent);
  }
  .design-toolbar .result-meta { margin-bottom: 0; color: var(--text); font-size: 0.9rem; }
  .design-fs {
    position: fixed;
    inset: 0;
    z-index: 1000;
    background: #0b1220;
    display: flex;
    flex-direction: column;
  }
  .design-fs[hidden] { display: none !important; }
  .design-fs-bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 10px 16px;
    border-bottom: 1px solid var(--line);
    background: var(--panel);
    color: var(--text);
    font-size: 0.9rem;
  }
  .swagger-header-actions .result-meta {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: flex-end;
    gap: 8px;
  }
  .design-fs-frame {
    flex: 1;
    width: 100%;
    border: 0;
    background: #fff;
  }
  .swagger {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .swagger-header {
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 12px 14px;
    background: color-mix(in srgb, #61affe 8%, var(--panel, var(--bg1)));
  }
  .swagger-header-top {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px;
  }
  .swagger-header-top .swagger-header-text { min-width: 0; flex: 1; }
  .swagger-header-actions {
    flex-shrink: 0;
    display: flex;
    align-items: center;
    gap: 8px;
    margin-left: auto;
  }
  .swagger-header-actions .result-meta { margin: 0; }
  .swagger-header h3 {
    margin: 0 0 4px;
    font-size: 1.15rem;
    color: var(--text);
  }
  .swagger-header .ver {
    font-family: "IBM Plex Mono", monospace;
    font-size: 0.75rem;
    color: var(--accent);
    margin-left: 8px;
  }
  .swagger-header p {
    margin: 6px 0 0;
    color: var(--muted);
    font-size: 0.88rem;
  }
  .swagger-op {
    border: 1px solid var(--line);
    border-radius: 10px;
    overflow: hidden;
    background: var(--panel, var(--bg1));
  }
  .swagger-op[data-method="get"] { border-color: color-mix(in srgb, #61affe 55%, var(--line)); }
  .swagger-op[data-method="post"] { border-color: color-mix(in srgb, #49cc90 55%, var(--line)); }
  .swagger-op[data-method="put"] { border-color: color-mix(in srgb, #fca130 55%, var(--line)); }
  .swagger-op[data-method="delete"] { border-color: color-mix(in srgb, #f93e3e 55%, var(--line)); }
  .swagger-op[data-method="patch"] { border-color: color-mix(in srgb, #50e3c2 55%, var(--line)); }
  .swagger-op-summary {
    display: grid;
    grid-template-columns: 78px 1fr auto;
    gap: 10px;
    align-items: center;
    width: 100%;
    padding: 10px 12px;
    border: 0;
    background: transparent;
    color: var(--text);
    text-align: left;
    cursor: pointer;
    font: inherit;
  }
  .swagger-op[data-method="get"] .swagger-op-summary { background: color-mix(in srgb, #61affe 12%, transparent); }
  .swagger-op[data-method="post"] .swagger-op-summary { background: color-mix(in srgb, #49cc90 12%, transparent); }
  .swagger-op[data-method="put"] .swagger-op-summary { background: color-mix(in srgb, #fca130 12%, transparent); }
  .swagger-op[data-method="delete"] .swagger-op-summary { background: color-mix(in srgb, #f93e3e 12%, transparent); }
  .swagger-op[data-method="patch"] .swagger-op-summary { background: color-mix(in srgb, #50e3c2 12%, transparent); }
  .swagger-method {
    font-family: "IBM Plex Mono", monospace;
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.04em;
    text-align: center;
    padding: 5px 6px;
    border-radius: 6px;
    color: #0b1220;
  }
  .swagger-method.get { background: #61affe; }
  .swagger-method.post { background: #49cc90; }
  .swagger-method.put { background: #fca130; }
  .swagger-method.delete { background: #f93e3e; color: #fff; }
  .swagger-method.patch { background: #50e3c2; }
  .swagger-method.options,
  .swagger-method.head { background: #9012fe; color: #fff; }
  .swagger-path {
    font-family: "IBM Plex Mono", monospace;
    font-size: 0.86rem;
    word-break: break-all;
  }
  .swagger-path small {
    display: block;
    margin-top: 2px;
    font-family: "DM Sans", system-ui, sans-serif;
    font-size: 0.8rem;
    color: var(--muted);
  }
  .swagger-chevron {
    color: var(--muted);
    font-size: 0.85rem;
    transition: transform 0.15s ease;
  }
  .swagger-op.open .swagger-chevron { transform: rotate(90deg); }
  .swagger-op-body {
    display: none;
    padding: 12px 14px 14px;
    border-top: 1px solid var(--line);
    background: color-mix(in srgb, var(--bg2) 70%, transparent);
  }
  .swagger-op.open .swagger-op-body { display: block; }
  .swagger-section { margin-top: 12px; }
  .swagger-section:first-child { margin-top: 0; }
  .swagger-section h4 {
    margin: 0 0 8px;
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--muted);
  }
  .swagger-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.82rem;
  }
  .swagger-table th,
  .swagger-table td {
    text-align: left;
    padding: 6px 8px;
    border-bottom: 1px solid var(--line);
    vertical-align: top;
  }
  .swagger-table th { color: var(--muted); font-weight: 500; }
  .swagger-table code,
  .swagger-code {
    font-family: "IBM Plex Mono", monospace;
    font-size: 0.78rem;
    color: var(--accent);
  }
  .swagger-badge {
    display: inline-block;
    margin-left: 6px;
    padding: 1px 6px;
    border-radius: 999px;
    font-size: 0.68rem;
    background: var(--chip);
    color: var(--warn);
  }
  .swagger-resp {
    border: 1px solid var(--line);
    border-radius: 8px;
    margin-bottom: 8px;
    overflow: hidden;
  }
  .swagger-resp-head {
    display: flex;
    gap: 10px;
    align-items: baseline;
    padding: 8px 10px;
    background: var(--bg2);
    font-size: 0.84rem;
  }
  .swagger-code-status {
    font-family: "IBM Plex Mono", monospace;
    font-weight: 600;
    color: var(--info);
    min-width: 2.5rem;
  }
  .swagger-schema {
    margin: 0;
    padding: 10px;
    overflow: auto;
    max-height: 240px;
    font-family: "IBM Plex Mono", monospace;
    font-size: 0.74rem;
    color: var(--text);
    background: #0b1220;
  }
  .swagger-schemas details {
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 8px 10px;
    margin-bottom: 8px;
    background: var(--bg2);
  }
  .swagger-schemas summary {
    cursor: pointer;
    font-family: "IBM Plex Mono", monospace;
    font-size: 0.82rem;
    color: var(--accent);
  }
  .toast {
    position: fixed;
    right: 24px;
    bottom: 24px;
    z-index: 1100;
    max-width: min(440px, calc(100vw - 32px));
    padding: 14px 16px;
    border-radius: 12px;
    border: 1px solid color-mix(in srgb, var(--accent) 50%, var(--line));
    background: color-mix(in srgb, var(--bg1) 92%, #1a3a2a);
    color: var(--text);
    box-shadow: 0 12px 40px rgba(0,0,0,.35);
    display: none;
  }
  .toast.show { display: block; animation: toast-in .2s ease; }
  .toast strong { display: block; margin-bottom: 4px; color: var(--accent); }
  .toast p { margin: 0; font-size: 0.86rem; color: var(--muted); }
  .toast button {
    margin-top: 10px;
    border: 1px solid var(--line);
    background: var(--bg2);
    color: var(--text);
    border-radius: 8px;
    padding: 6px 10px;
    cursor: pointer;
    font: inherit;
  }
  @keyframes toast-in {
    from { opacity: 0; transform: translateY(8px); }
    to { opacity: 1; transform: translateY(0); }
  }
  .approval-modal {
    position: fixed;
    inset: 0;
    z-index: 1100;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 1.25rem;
    background: rgba(6, 10, 16, 0.72);
    backdrop-filter: blur(4px);
  }
  .approval-modal[hidden] { display: none !important; }
  .approval-modal-card {
    width: min(560px, 100%);
    max-height: min(85vh, 720px);
    overflow: auto;
    border: 1px solid var(--line);
    border-radius: 14px;
    background: var(--panel);
    color: var(--text);
    box-shadow: 0 24px 64px rgba(0,0,0,.45);
    padding: 1.25rem 1.35rem 1.35rem;
  }
  .approval-modal-card h3 {
    margin: 0 0 .35rem;
    font-size: 1.15rem;
  }
  .approval-modal-card .appr-title {
    font-weight: 600;
    margin: .55rem 0 .35rem;
  }
  .approval-modal-card .appr-summary {
    color: var(--muted);
    font-size: .9rem;
    margin: 0 0 .75rem;
    line-height: 1.45;
  }
  .approval-modal-card .appr-note {
    font-size: .8rem;
    color: var(--muted);
    margin: 0 0 1rem;
  }
  .approval-modal-actions {
    display: flex;
    flex-wrap: wrap;
    gap: .55rem;
  }
  .approval-modal-actions .btn { margin: 0; }
</style>
</head>
<body>
<div id="design-fs" class="design-fs" hidden>
  <div class="design-fs-bar">
    <span id="design-fs-title">Full screen</span>
    <button type="button" class="btn secondary" id="design-fs-close">Close</button>
  </div>
  <iframe id="design-fs-frame" class="design-fs-frame" sandbox="allow-scripts allow-same-origin" title="Full screen"></iframe>
</div>
<div id="approval-modal" class="approval-modal" hidden role="dialog" aria-modal="true" aria-labelledby="approval-modal-heading">
  <div class="approval-modal-card">
    <h3 id="approval-modal-heading">Human approval required</h3>
    <div class="appr-title" id="approval-modal-title"></div>
    <p class="appr-summary" id="approval-modal-summary"></p>
    <p class="appr-note" id="approval-modal-note"></p>
    <div id="figma-upload-box" hidden style="margin:12px 0;padding:12px;border:1px dashed var(--line);border-radius:12px">
      <label style="display:block;font-size:0.82rem;color:var(--muted);margin-bottom:6px">Optional Figma URL</label>
      <input id="figma-url-input" type="url" placeholder="https://www.figma.com/design/…" style="width:100%;margin-bottom:10px;padding:8px 10px;border-radius:8px;border:1px solid var(--line);background:var(--panel);color:inherit" />
      <label style="display:block;font-size:0.82rem;color:var(--muted);margin-bottom:6px">Or upload Figma export (png/pdf/svg/json/zip)</label>
      <input id="figma-file-input" type="file" accept=".png,.jpg,.jpeg,.webp,.pdf,.svg,.json,.fig,.zip,image/*" />
      <p id="figma-upload-status" style="font-size:0.8rem;color:var(--muted);margin:8px 0 0"></p>
    </div>
    <div class="approval-modal-actions" id="approval-modal-actions"></div>
  </div>
</div>
<div id="toast" class="toast" role="status" aria-live="polite"></div>
<header>
  <div>
    <h1 class="brand">Forge <span>Studio</span></h1>
    <p class="sub">Production Agentic SDLC — Intake → Plan Gate → Parallel Agents → Sync → Validate → Release</p>
  </div>
  <div class="meta" id="clock">loading…</div>
</header>
<section class="upload-wrap">
  <div class="upload">
    <div>
      <h2>Upload requirement document</h2>
      <p>Upload a PRD as <b>.md</b>, <b>.txt</b>, <b>.pdf</b>, or <b>.docx</b>. Pipeline: HLD/LLD → DB → <b>human approval</b> → API → build → sync → validate → release.</p>
      <div class="agent-steps" id="agent-steps"></div>
      <label style="display:flex;gap:8px;align-items:flex-start;margin:10px 0;font-size:0.88rem;color:var(--muted);line-height:1.45">
        <input type="checkbox" id="chk-human" checked style="margin-top:0.2em;flex-shrink:0" />
        <span>Require human approvals for DB→API / API→codegen / release. <b>Plan</b>, <b>Architecture</b>, <b>Clarify</b>, <b>Figma</b>, and <b>Coding complete</b> always show a modal.</span>
      </label>
      <div class="actions">
        <button class="btn" id="btn-example" type="button">Run TinyURL example PRD</button>
        <label class="btn secondary" for="file-input">Choose file</label>
        <button class="btn secondary" id="btn-cleanup-finished" type="button">Clear finished</button>
        <button class="btn secondary" id="btn-cleanup" type="button">Clear all runs</button>
        <input id="file-input" type="file" accept=".md,.txt,.markdown,.rst,.pdf,.docx,.doc,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/msword" hidden />
      </div>
      <div class="status-line" id="upload-status"></div>
      <div id="approval-panel" class="panel" style="display:none;margin-top:12px;padding:12px;border:1px solid var(--line);border-radius:12px;background:var(--panel)"></div>
    </div>
    <div class="drop" id="dropzone">
      <strong>Drop PRD here</strong>
      <span class="mono" style="color:var(--muted)">.md · .txt · .pdf · .docx</span>
    </div>
  </div>
</section>
<main>
  <aside class="panel">
    <h2>Workflows</h2>
    <div class="wf-list" id="wf-list"><div class="empty">Upload a PRD to start the agentic SDLC</div></div>
  </aside>
  <section>
    <div class="stats" id="stats"></div>
    <div class="panel">
      <div class="tabs" id="tabs">
        <button class="tab active" data-tab="results">Results</button>
        <button class="tab" data-tab="tasks">Tasks</button>
        <button class="tab" data-tab="trace">Audit Trace</button>
        <button class="tab" data-tab="dag">DAG</button>
        <button class="tab" data-tab="artifacts">Artifacts</button>
        <button class="btn secondary" id="download-btn" style="margin-left:auto" disabled
                title="Download every artifact and generated file as a zip">Download .zip</button>
      </div>
      <div class="content" id="content"><div class="empty">Upload a requirement document to see HLD, LLD, APIs, and code</div></div>
    </div>
  </section>
</main>
<script>
const RESULT_SECTIONS = [
  ["dag_design_html", "1. DAG"],
  ["architecture_design_html", "2. HLD"],
  ["lld_design_html", "3. LLD"],
  ["adrs", "4. ADRs"],
  ["database_design_html", "5. DB Design"],
  ["openapi", "6. API"],
  ["workspace_manifest", "7. Workspace"],
  ["backend_source", "8. Backend"],
  ["backend_snippets", "9. Code snippets"],
  ["frontend_design_html", "10. UI Design"],
  ["infra", "11. DevOps"],
  ["security_review", "12. Security"],
  ["source_tree", "13. Source tree"],
  ["test_plan", "14. Tests"],
  ["engineering_validation", "15. Validation"],
  ["security_scan", "16. Sec Review"],
  ["documentation", "17. Docs"],
  ["release_notes", "18. Release"],
  ["deployment_recommendation", "19. Deploy"],
  ["observability_plan", "20. Observability"],
  ["engineering_summary", "21. Summary"],
  ["final_deliverables", "22. Deliverables"],
];
const state = {
  workflows: [], selected: null, tab: "results", detail: null, results: null,
  resultKey: "dag_design_html", trace: [], overview: null, uploading: false,
  fePages: null, codingToastFor: null,
  // Approval id the modal is currently built for, and the one whose decision is
  // already in flight. The poll runs every few seconds and would otherwise
  // rebuild and re-open the modal on top of the user each tick.
  approvalRenderedFor: null, approvalSubmittedFor: null
};

function chipClass(status) {
  if (!status) return "";
  if (status === "SUCCEEDED") return "ok";
  if (status === "FAILED") return "fail";
  if (status.includes("WAITING")) return "wait";
  return "run";
}

function fmtRate(v) {
  if (v == null || v === undefined) return "—";
  return (Number(v) * 100).toFixed(0) + "%";
}
function fmtMs(v) {
  if (v == null || v === undefined) return "—";
  const n = Number(v);
  if (n >= 1000) return (n / 1000).toFixed(1) + "s";
  return Math.round(n) + "ms";
}

async function refreshOverview() {
  state.overview = await fetch("/api/platform/overview").then(r => r.json());
  const o = state.overview;
  const llm = o.llm || {};
  const rel = o.reliability || {};
  document.getElementById("stats").innerHTML = `
    <div class="stat"><div class="label">Workflows</div><div class="value">${o.workflows}</div></div>
    <div class="stat"><div class="label">Success rate</div><div class="value">${fmtRate(rel.workflow_success_rate)}</div></div>
    <div class="stat"><div class="label">Avg MTTR</div><div class="value">${fmtMs(rel.avg_mttr_ms)}</div></div>
    <div class="stat"><div class="label">E2E latency</div><div class="value">${fmtMs(rel.avg_e2e_latency_ms)}</div></div>
    <div class="stat"><div class="label">Retries / rollbacks</div><div class="value" style="font-size:1.1rem">${rel.total_retries || 0} / ${rel.total_rollbacks || 0}</div></div>
    <div class="stat"><div class="label">Parallel max</div><div class="value">${rel.parallel_max_width || 0}</div></div>
    <div class="stat"><div class="label">Tokens</div><div class="value">${Math.round(o.total_tokens)}</div></div>
    <div class="stat"><div class="label">LLM</div><div class="value" style="font-size:1rem">${llm.enabled ? (llm.model || "on") : "fallback"}</div></div>
  `;
}

async function refreshWorkflows() {
  state.workflows = await fetch("/api/workflows").then(r => r.json());
  const el = document.getElementById("wf-list");
  if (!state.workflows.length) {
    el.innerHTML = `<div class="empty">No runs yet. Upload a requirement document.</div>`;
    return;
  }
  el.innerHTML = state.workflows.map(w => {
    const tone = chipClass(w.status);
    const product = w.product_name || w.id;
    const file = w.filename || w.playbook_id || "";
    const meta = [w.id, w.task_count != null ? `${w.task_count} tasks` : ""].filter(Boolean).join(" · ");
    return `
    <button class="wf-item status-${tone} ${state.selected === w.id ? "active" : ""}" data-id="${w.id}">
      <div class="wf-product">${esc(product)}</div>
      <div class="wf-file">${esc(file)}</div>
      <span class="chip ${tone}">${esc(w.status || "")}</span>
      <div class="wf-meta">${esc(meta)}</div>
    </button>`;
  }).join("");
  el.querySelectorAll(".wf-item").forEach(btn => {
    btn.addEventListener("click", () => selectWorkflow(btn.dataset.id));
  });
}

async function selectWorkflow(id, { resetSection = false } = {}) {
  state.selected = id;
  const dl = document.getElementById("download-btn");
  if (dl) dl.disabled = false;
  const [detail, trace, results] = await Promise.all([
    fetch(`/api/workflows/${id}`).then(r => r.json()),
    fetch(`/api/workflows/${id}/trace`).then(r => r.json()),
    fetch(`/api/workflows/${id}/results`).then(r => r.ok ? r.json() : null).catch(() => null),
  ]);
  state.detail = detail;
  state.trace = trace.events || [];
  state.results = results;
  // Keep the section the user clicked. Only auto-pick DAG (etc.) when the
  // current key is unknown — never bounce back on poll just because an
  // artifact is still pending / was compensated away.
  const known = RESULT_SECTIONS.some(([k]) => k === state.resultKey);
  if (resetSection || !known) {
    const first = RESULT_SECTIONS.find(([k]) => results?.artifacts?.[k] != null);
    if (first) state.resultKey = first[0];
  }
  await refreshWorkflows();
  renderContent();
  // Always sync approval UI — show modal/panel when paused, hide when not.
  await refreshApprovalPanel(id);
}

function extractHtml(value) {
  if (typeof value === "string" && /<!doctype html|<html[\s>]/i.test(value)) return value;
  if (value && typeof value === "object" && typeof value.html === "string") return value.html;
  return null;
}

function fsBtnHtml(attr = "data-panel-fs") {
  return `<button type="button" class="btn secondary" ${attr}>Full screen</button>`;
}

function withFsMeta(meta = "") {
  const m = String(meta || "");
  if (/data-(panel|design|dag)-fs/.test(m)) return m;
  return m ? `${m} ${fsBtnHtml()}` : fsBtnHtml();
}

function renderSwaggerShell({ title, version = "", description = "", meta = "", body = "", fs = true } = {}) {
  // fs:false when body already embeds its own Full screen control (e.g. nested HTML preview).
  const actions = fs ? withFsMeta(meta) : (meta || "");
  return `<div class="swagger">
    <div class="swagger-header">
      <div class="swagger-header-top">
        <div class="swagger-header-text">
          <h3>${esc(title)}${version ? `<span class="ver">${esc(version)}</span>` : ""}</h3>
          ${description ? `<p>${esc(description)}</p>` : ""}
        </div>
        <div class="swagger-header-actions"><div class="result-meta">${actions}</div></div>
      </div>
    </div>
    ${body}
  </div>`;
}

function renderSwaggerAccordion(entries, { tag = "DOC", method = "get", openFirst = false } = {}) {
  const rows = (entries || []).map(([label, html], i) => {
    const id = `op-${tag}-${i}-${String(label).slice(0, 24).replace(/[^a-zA-Z0-9_-]/g, "_")}`;
    return `<div class="swagger-op ${openFirst && i === 0 ? "open" : ""}" data-method="${method}" data-op-id="${id}">
      <button type="button" class="swagger-op-summary" data-swagger-toggle="${id}">
        <span class="swagger-method ${method}">${esc(tag)}</span>
        <span class="swagger-path">${esc(label)}</span>
        <span class="swagger-chevron">▶</span>
      </button>
      <div class="swagger-op-body">${html}</div>
    </div>`;
  });
  return rows.join("") || `<div class="empty">No content</div>`;
}

function renderJsonAccordion(value, { title = "Artifact", version = "", description = "" } = {}) {
  if (value == null) return `<div class="empty">Not produced yet</div>`;
  if (typeof value !== "object") {
    return renderSwaggerShell({
      title,
      version,
      description,
      body: `<div class="swagger-op" data-method="get"><div class="swagger-op-body" style="display:block"><pre class="swagger-schema">${esc(String(value))}</pre></div></div>`,
    });
  }
  const entries = Object.entries(value).map(([k, v]) => {
    const pretty = typeof v === "string" ? v : JSON.stringify(v, null, 2);
    return [k, `<pre class="swagger-schema">${esc(pretty)}</pre>`];
  });
  return renderSwaggerShell({
    title,
    version,
    description,
    meta: `${entries.length} fields`,
    body: renderSwaggerAccordion(entries, { tag: "JSON", method: "get", openFirst: true }),
  });
}

function renderHtmlFrame(html, { toolbarLabel = "Rendered HTML design" } = {}) {
  // Use a Blob URL instead of srcdoc="…" — large HTML with <script> breaks
  // attribute embedding and left the UI Design preview blank.
  const fid = "df-" + Math.random().toString(36).slice(2, 10);
  state._frameHtml = state._frameHtml || {};
  state._frameHtml[fid] = html;
  return renderSwaggerShell({
    title: toolbarLabel,
    meta: `<button type="button" class="btn secondary" data-design-fs>Full screen</button>`,
    body: `<div class="design-wrap"><iframe class="design-frame" id="${fid}" data-blob-frame="${fid}" sandbox="allow-scripts allow-same-origin" title="Design preview"></iframe></div>`,
  });
}

function mountDesignFrames(root) {
  const scope = root || document;
  scope.querySelectorAll("iframe.design-frame[data-blob-frame]").forEach((frame) => {
    const id = frame.getAttribute("data-blob-frame");
    const html = state._frameHtml && state._frameHtml[id];
    if (!html) return;
    if (frame.dataset.blobUrl) {
      try { URL.revokeObjectURL(frame.dataset.blobUrl); } catch (_) {}
    }
    const url = URL.createObjectURL(new Blob([html], { type: "text/html;charset=utf-8" }));
    frame.dataset.blobUrl = url;
    frame.src = url;
  });
}

const STUDIO_FRAME_KEYS = {
  dag_design_html: { section: "dag", label: "1. DAG", bare: true },
  architecture_design_html: { section: "hld", label: "2. HLD", bare: true },
  lld_design_html: { section: "lld", label: "3. LLD", bare: true },
  database_design_html: { section: "db", label: "5. DB Design", bare: true },
  workspace_manifest: { section: "workspace", label: "7. Workspace", bare: true },
};

function renderStudioFrame(workflowId, section, { toolbarLabel = "Studio", bare = false } = {}) {
  // DAG / HLD / LLD / DB / Workspace: raw content only (no theme top bar).
  const kind = bare ? "raw" : "theme";
  const url = `/api/workflows/${encodeURIComponent(workflowId)}/${kind}/${encodeURIComponent(section)}.html?t=${Date.now()}`;
  return `<div class="design-wrap">
    <div class="design-toolbar">
      <div class="result-meta">${esc(toolbarLabel)}</div>
      <button type="button" class="btn secondary" data-dag-fs data-url="${url}">Full screen</button>
    </div>
    <iframe class="design-frame" src="${url}" title="${esc(toolbarLabel)}"></iframe>
  </div>`;
}

function openDesignFullscreen(htmlOrUrl, { asUrl = false, title = "" } = {}) {
  const overlay = document.getElementById("design-fs");
  const frame = document.getElementById("design-fs-frame");
  const titleEl = document.getElementById("design-fs-title");
  if (!overlay || !frame || !htmlOrUrl) return;
  if (titleEl) {
    titleEl.textContent = title || sectionLabelForKey(state.resultKey) || "Full screen";
  }
  if (asUrl) {
    frame.removeAttribute("srcdoc");
    frame.src = htmlOrUrl;
  } else {
    frame.removeAttribute("src");
    frame.srcdoc = htmlOrUrl;
  }
  overlay.hidden = false;
  document.body.style.overflow = "hidden";
}

function closeDesignFullscreen() {
  const overlay = document.getElementById("design-fs");
  const frame = document.getElementById("design-fs-frame");
  if (!overlay || !frame) return;
  overlay.hidden = true;
  frame.removeAttribute("srcdoc");
  frame.removeAttribute("src");
  document.body.style.overflow = "";
}

function panelFullscreenHtml(panelEl, title = "") {
  if (!panelEl) return "";
  const clone = panelEl.cloneNode(true);
  clone.querySelectorAll("[data-panel-fs], [data-design-fs], [data-dag-fs], .design-toolbar").forEach((b) => b.remove());
  // Expand all accordion rows in the fullscreen copy
  clone.querySelectorAll(".swagger-op").forEach((op) => op.classList.add("open"));
  clone.querySelectorAll("details").forEach((d) => { d.open = true; });
  const pageTitle = title || sectionLabelForKey(state.resultKey) || "Full screen";
  const css = `
    :root { --bg:#0e1419; --panel:#152028; --line:#2a3f4d; --text:#e6eef2; --muted:#8aa0ad;
      --accent:#3ecf8e; --get:#61affe; --post:#49cc90; --fail:#e5534b; }
    * { box-sizing:border-box; }
    body { margin:0; padding:1.25rem 1.5rem 2rem; font-family:"DM Sans",system-ui,sans-serif;
      background:var(--bg); color:var(--text); }
    .swagger { display:flex; flex-direction:column; gap:10px; max-width:1100px; margin:0 auto; }
    .swagger-header { border:1px solid var(--line); border-radius:10px; padding:14px 16px; background:var(--panel); }
    .swagger-header h3 { margin:0; font-size:1.15rem; }
    .swagger-header .ver { margin-left:.55rem; color:var(--muted); font-size:.85rem; font-weight:500; }
    .swagger-header p { margin:.45rem 0 0; color:var(--muted); font-size:.9rem; }
    .swagger-header-actions { display:none; }
    .swagger-op { border:1px solid var(--line); border-radius:10px; background:var(--panel); overflow:hidden; }
    .swagger-op-summary { width:100%; display:flex; align-items:center; gap:10px; padding:12px 14px;
      border:0; background:transparent; color:var(--text); cursor:pointer; text-align:left; font:inherit; }
    .swagger-method { font-size:.68rem; font-weight:700; padding:.2rem .45rem; border-radius:4px;
      background:color-mix(in srgb, var(--get) 22%, transparent); color:var(--get); font-family:monospace; }
    .swagger-method.post { background:color-mix(in srgb, var(--post) 22%, transparent); color:var(--post); }
    .swagger-path { flex:1; font-family:monospace; font-size:.88rem; }
    .swagger-chevron { color:var(--muted); font-size:.7rem; }
    .swagger-op .swagger-op-body { display:none; padding:0 14px 14px; border-top:1px solid var(--line); }
    .swagger-op.open .swagger-op-body { display:block; }
    .swagger-op.open .swagger-chevron { transform:rotate(90deg); display:inline-block; }
    .swagger-schema, pre { margin:0; white-space:pre-wrap; word-break:break-word; font-family:ui-monospace,monospace;
      font-size:.82rem; background:#0b1220; border:1px solid var(--line); border-radius:8px; padding:12px; color:var(--text); }
    .chip { display:inline-block; padding:.15rem .5rem; border-radius:999px; border:1px solid var(--line);
      font-size:.72rem; font-weight:600; margin-right:.35rem; }
    .chip.fail { background:color-mix(in srgb, var(--fail) 22%, transparent); color:#ffb4ae; border-color:var(--fail); }
    .empty, .muted { color:var(--muted); }
    .swagger-code { font-family:ui-monospace,monospace; color:var(--get); }
    .design-wrap { border:1px solid var(--line); border-radius:10px; overflow:hidden; min-height:70vh; }
    .design-frame, iframe { width:100%; min-height:70vh; border:0; background:#fff; }
    .tasks, .timeline { display:flex; flex-direction:column; gap:8px; max-width:1100px; margin:0 auto; }
    .task, .evt { border:1px solid var(--line); border-radius:10px; padding:12px 14px; background:var(--panel); }
    .mono { font-family:ui-monospace,monospace; }
  `;
  // Escape <\/script> so this template does not terminate the dashboard <script> tag.
  return `<!DOCTYPE html><html><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>${esc(pageTitle)}</title><style>${css}</style></head><body>${clone.outerHTML}
<script>
document.querySelectorAll("[data-swagger-toggle]").forEach((btn)=>{
  btn.addEventListener("click",()=>{ const op=btn.closest(".swagger-op"); if(op) op.classList.toggle("open"); });
});
<\/script></body></html>`;
}

function bindDesignFullscreenButtons(root) {
  const scope = root || document;
  scope.querySelectorAll("[data-design-fs]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const rootEl = btn.closest(".swagger") || btn.closest(".design-wrap") || scope;
      const iframe = rootEl.querySelector("iframe.design-frame");
      if (!iframe) return;
      const title = sectionLabelForKey(state.resultKey) || "Full screen";
      const fid = iframe.getAttribute("data-blob-frame");
      const raw = fid && state._frameHtml ? state._frameHtml[fid] : null;
      if (raw) openDesignFullscreen(raw, { title });
      else if (iframe.srcdoc) openDesignFullscreen(iframe.srcdoc, { title });
      else if (iframe.src) openDesignFullscreen(iframe.src, { asUrl: true, title });
    });
  });
  scope.querySelectorAll("[data-dag-fs]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const rootEl = btn.closest(".swagger") || btn.closest(".design-wrap") || scope;
      const iframe = rootEl.querySelector("iframe.design-frame");
      const url = btn.dataset.url || (iframe && iframe.src);
      const title = sectionLabelForKey(state.resultKey) || "Full screen";
      if (url) openDesignFullscreen(url, { asUrl: true, title });
    });
  });
  scope.querySelectorAll("[data-panel-fs]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const panel = btn.closest(".swagger") || btn.closest(".result-body") || scope;
      const title = sectionLabelForKey(state.resultKey) || "Full screen";
      const html = panelFullscreenHtml(panel, title);
      if (html) openDesignFullscreen(html, { title });
    });
  });
}

function backendVerdictPanels(verdict) {
  if (!verdict || typeof verdict !== "object") return [];
  if (String(verdict.verdict || "").toUpperCase() !== "FAIL"
      && String(verdict.status || "").toUpperCase() !== "COMPENSATED") {
    return [];
  }
  const findings = (verdict.security_findings || []).map(f =>
    `<div>• <span class="swagger-code">${esc(f.severity || "")}</span> ${esc(f.finding || "")}`
    + (f.recommendation ? ` — <span class="muted">${esc(f.recommendation)}</span>` : "")
    + `</div>`
  ).join("");
  const gates = (verdict.failed_gates || []).map(g =>
    `<div>• <span class="swagger-code">${esc(g.gate || "")}</span> ${esc(g.detail || "")}</div>`
  ).join("");
  const rem = (verdict.remediation || []).map(r => `<div>• ${esc(r)}</div>`).join("");
  return [
    ["Verdict", `<div><span class="chip fail">${esc(verdict.verdict || "FAIL")}</span>`
      + ` <span class="chip">${esc(verdict.status || "COMPENSATED")}</span>`
      + `<p style="margin:.65rem 0 0">${esc(verdict.summary || "")}</p></div>`],
    ["Root cause", `<pre class="swagger-schema">${esc(verdict.root_cause || "")}</pre>`],
    ["Failed gates", gates || "<div class='empty'>None listed</div>"],
    ["Security findings", findings || "<div class='empty'>None listed</div>"],
    ["Remediation", rem || "<div class='empty'>None listed</div>"],
  ];
}

function renderSourceTree(tree, { previewHtml = false, title = "Source tree", extraPanels = [] } = {}) {
  if (previewHtml && tree && typeof tree === "object") {
    const entries = Object.entries(tree);
    if (entries.some(([, v]) => typeof v === "string" && /<!doctype|<html[\s>]/i.test(v))) {
      const htmlFiles = entries.filter(([, v]) => typeof v === "string" && /<!doctype|<html[\s>]/i.test(v));
      state.fePages = Object.fromEntries(htmlFiles);
      const opts = htmlFiles.map(([p]) => `<option value="${p.replace(/"/g, "&quot;")}">${p}</option>`).join("");
      const first = htmlFiles[0][1];
      return renderSwaggerShell({
        title: "Frontend pages",
        version: "HTML",
        description: "Select a page to preview",
        fs: false,
        meta: `<select id="fe-page" style="margin-top:6px;width:100%;max-width:420px;padding:8px;border-radius:8px;border:1px solid var(--line);background:var(--panel);color:var(--text)">${opts}</select>`,
        body: `<div id="fe-preview">${renderHtmlFrame(first, { toolbarLabel: "Frontend HTML preview" })}</div>`,
      });
    }
  }
  const entries = tree && typeof tree === "object" ? Object.entries(tree) : [];
  const filePanels = entries.map(([path, desc]) => {
    const label = typeof desc === "string" ? desc : JSON.stringify(desc, null, 2);
    return [path, `<pre class="swagger-schema">${esc(label)}</pre>`];
  });
  const panels = [...(extraPanels || []), ...filePanels];
  if (!panels.length) {
    return renderSwaggerShell({
      title,
      version: "empty",
      description: "No backend source available",
      body: "<div class='empty'>No source tree</div>",
    });
  }
  return renderSwaggerShell({
    title,
    version: filePanels.length ? `${filePanels.length} files` : (extraPanels[0] ? "verdict" : "0 files"),
    description: extraPanels.length ? "Failure verdict shown below — codegen was rolled back" : "",
    body: renderSwaggerAccordion(panels, { tag: extraPanels.length ? "VERDICT" : "FILE", method: "get", openFirst: true }),
  });
}

function esc(s) {
  return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function resolveRef(spec, node) {
  if (!node || typeof node !== "object") return node;
  if (!node.$ref) return node;
  const ref = String(node.$ref);
  if (!ref.startsWith("#/")) return node;
  let cur = spec;
  for (const part of ref.slice(2).split("/")) {
    cur = cur && cur[part];
  }
  return cur || node;
}

function schemaPreview(spec, schema, depth = 0) {
  if (!schema || depth > 4) return "{}";
  const resolved = resolveRef(spec, schema);
  if (!resolved || typeof resolved !== "object") return String(resolved);
  if (resolved.type === "array") {
    return `[${schemaPreview(spec, resolved.items || {}, depth + 1)}]`;
  }
  if (resolved.properties) {
    const indent = "  ".repeat(depth + 1);
    const close = "  ".repeat(depth);
    const req = new Set(resolved.required || []);
    const lines = Object.entries(resolved.properties).map(([k, v]) => {
      const child = resolveRef(spec, v);
      const t = child.type || (child.properties ? "object" : child.$ref ? child.$ref.split("/").pop() : "any");
      const mark = req.has(k) ? "" : "?";
      return `${indent}${k}${mark}: ${t}`;
    });
    return `{\n${lines.join(",\n")}\n${close}}`;
  }
  if (resolved.$ref) return resolved.$ref.split("/").pop();
  return resolved.type || "object";
}

function renderParamRows(spec, params) {
  if (!params || !params.length) return `<div class="result-meta">No parameters</div>`;
  const rows = params.map((p) => {
    const schema = resolveRef(spec, p.schema || {});
    const type = schema.type || (schema.$ref ? schema.$ref.split("/").pop() : "any");
    return `<tr>
      <td><code>${esc(p.name)}</code>${p.required ? `<span class="swagger-badge">required</span>` : ""}</td>
      <td>${esc(p.in || "")}</td>
      <td class="swagger-code">${esc(type)}${schema.format ? ` (${esc(schema.format)})` : ""}</td>
      <td>${esc(p.description || "")}</td>
    </tr>`;
  }).join("");
  return `<table class="swagger-table">
    <thead><tr><th>Name</th><th>In</th><th>Type</th><th>Description</th></tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}

function renderRequestBody(spec, body) {
  if (!body) return "";
  const content = body.content || {};
  const blocks = Object.entries(content).map(([mime, media]) => {
    const schema = media.schema || {};
    return `<div style="margin-bottom:8px">
      <div class="result-meta">${esc(mime)}${body.required ? ` · <span class="swagger-badge">required</span>` : ""}</div>
      <pre class="swagger-schema">${esc(schemaPreview(spec, schema))}</pre>
    </div>`;
  }).join("");
  return `<div class="swagger-section"><h4>Request body</h4>${blocks || `<div class="result-meta">No content schema</div>`}</div>`;
}

function renderResponses(spec, responses) {
  if (!responses || !Object.keys(responses).length) return "";
  const blocks = Object.entries(responses).map(([code, resp]) => {
    const resolved = resolveRef(spec, resp);
    const desc = resolved.description || (resp.$ref ? resp.$ref.split("/").pop() : "");
    const content = resolved.content || {};
    const schemas = Object.entries(content).map(([mime, media]) =>
      `<div class="result-meta" style="padding:8px 10px 0">${esc(mime)}</div>
       <pre class="swagger-schema">${esc(schemaPreview(spec, media.schema || {}))}</pre>`
    ).join("");
    const fallback = !schemas && resolved.properties
      ? `<pre class="swagger-schema">${esc(schemaPreview(spec, resolved))}</pre>`
      : "";
    return `<div class="swagger-resp">
      <div class="swagger-resp-head">
        <span class="swagger-code-status">${esc(code)}</span>
        <span>${esc(desc)}</span>
      </div>
      ${schemas || fallback}
    </div>`;
  }).join("");
  return `<div class="swagger-section"><h4>Responses</h4>${blocks}</div>`;
}

function showToast(title, message, { selectWorkspace = false } = {}) {
  const el = document.getElementById("toast");
  if (!el) return;
  el.innerHTML = `<strong>${esc(title)}</strong><p>${esc(message)}</p>
    <button type="button" id="toast-dismiss">Dismiss</button>
    ${selectWorkspace ? `<button type="button" id="toast-open" style="margin-left:6px">Open Workspace</button>` : ""}`;
  el.classList.add("show");
  const dismiss = () => el.classList.remove("show");
  document.getElementById("toast-dismiss")?.addEventListener("click", dismiss);
  document.getElementById("toast-open")?.addEventListener("click", () => {
    state.resultKey = "workspace_manifest";
    state.tab = "results";
    document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.dataset.tab === "results"));
    renderContent();
    dismiss();
  });
  setTimeout(dismiss, 12000);
}

function renderOpenApi(value) {
  const spec = value && value.openapi ? value : (value && value.spec) || value;
  if (!spec || typeof spec !== "object" || !spec.paths) {
    return renderJsonAccordion(value, { title: "API", version: "raw" });
  }
  const info = spec.info || {};
  const methods = ["get", "post", "put", "patch", "delete", "options", "head"];
  const ops = [];
  for (const [path, item] of Object.entries(spec.paths || {})) {
    for (const method of methods) {
      const op = item[method];
      if (!op) continue;
      const params = [...(item.parameters || []), ...(op.parameters || [])];
      const id = `op-${ops.length}`;
      ops.push(`<div class="swagger-op" data-method="${method}" data-op-id="${id}">
        <button type="button" class="swagger-op-summary" data-swagger-toggle="${id}">
          <span class="swagger-method ${method}">${method.toUpperCase()}</span>
          <span class="swagger-path">${esc(path)}${op.summary ? `<small>${esc(op.summary)}</small>` : ""}</span>
          <span class="swagger-chevron">▶</span>
        </button>
        <div class="swagger-op-body">
          ${op.operationId ? `<div class="result-meta">operationId: <span class="swagger-code">${esc(op.operationId)}</span></div>` : ""}
          ${op.description ? `<p style="margin:8px 0;color:var(--muted);font-size:0.88rem">${esc(op.description)}</p>` : ""}
          <div class="swagger-section"><h4>Parameters</h4>${renderParamRows(spec, params)}</div>
          ${renderRequestBody(spec, op.requestBody)}
          ${renderResponses(spec, op.responses)}
        </div>
      </div>`);
    }
  }
  const schemas = Object.entries((spec.components && spec.components.schemas) || {});
  const schemaBlock = schemas.length ? `<div class="swagger-section swagger-schemas">
    <h4>Schemas</h4>
    ${schemas.map(([name, schema]) =>
      `<details><summary>${esc(name)}</summary><pre class="swagger-schema">${esc(schemaPreview(spec, schema))}</pre></details>`
    ).join("")}
  </div>` : "";
  return renderSwaggerShell({
    title: info.title || "API",
    version: info.version ? `v${info.version}` : "OpenAPI",
    description: info.description || "",
    meta: `OpenAPI ${esc(spec.openapi || "3.x")} · ${ops.length} operations`,
    body: `${ops.join("") || `<div class="empty">No paths in OpenAPI spec</div>`}${schemaBlock}`,
  });
}

function sectionLabelForKey(key) {
  const hit = RESULT_SECTIONS.find(([k]) => k === key);
  return hit ? hit[1] : key;
}

function renderResultBody(key, value) {
  // Backend: on FAIL/COMPENSATED, show Verdict panels under 8. Backend (no separate tab).
  if (key === "backend_source") {
    const verdict = state.results?.artifacts?.backend_verdict;
    const failPanels = backendVerdictPanels(verdict);
    const tree = value && typeof value === "object" && value.ready !== false ? value : null;
    if (failPanels.length || (tree && Object.keys(tree).length)) {
      return renderSourceTree(tree || {}, {
        title: "8. Backend",
        extraPanels: failPanels,
      });
    }
    return renderSwaggerShell({
      title: "8. Backend",
      version: "pending",
      description: "Not produced yet (workflow still running or failed)",
    });
  }
  if (value == null) {
    return renderSwaggerShell({
      title: sectionLabelForKey(key),
      version: "pending",
      description: "Not produced yet (workflow still running or failed)",
    });
  }
  // DAG / HLD / LLD / DB / Workspace — design viewer + Full screen
  if (STUDIO_FRAME_KEYS[key]) {
    if (!state.selected) {
      return renderSwaggerShell({ title: sectionLabelForKey(key), description: "Select a workflow" });
    }
    const meta = STUDIO_FRAME_KEYS[key];
    if (value && value.ready === false && meta.section !== "dag") {
      return renderSwaggerShell({
        title: meta.label,
        version: "pending",
        description: `${meta.label} not produced yet`,
      });
    }
    return renderStudioFrame(state.selected, meta.section, {
      toolbarLabel: meta.label,
      bare: !!meta.bare,
    });
  }
  const htmlDoc = extractHtml(value);
  if (htmlDoc && (key.includes("html") || key.includes("design"))) {
    return renderHtmlFrame(htmlDoc, { toolbarLabel: sectionLabelForKey(key) });
  }
  if (key === "frontend_source") {
    // React sources: show file tree (not legacy HTML page picker)
    const entries = value && typeof value === "object" ? Object.keys(value) : [];
    const isReact = entries.some((p) => /\.(tsx|ts|jsx)$/i.test(p) || p === "package.json");
    if (isReact) {
      return renderSourceTree(value, {
        previewHtml: false,
        title: "10. UI Design — React source",
      });
    }
    return renderSourceTree(value, { previewHtml: true });
  }
  if (key === "source_tree") {
    return renderSourceTree(value);
  }
  if (key === "backend_snippets" && typeof value === "object") {
    const entries = Object.entries(value).map(([path, code]) =>
      [path, `<pre class="swagger-schema">${esc(String(code))}</pre>`]
    );
    return renderSwaggerShell({
      title: "9. Code snippets",
      version: `${entries.length} files`,
      body: renderSwaggerAccordion(entries, { tag: "CODE", method: "post", openFirst: true }),
    });
  }
  if (key === "reqspec" && value && value.fr) {
    const frs = (value.fr || []).map(f => `<div>• <span class="swagger-code">${esc(f.id)}</span> ${esc(f.text)}</div>`).join("");
    const nfrs = (value.nfr || []).map(f => `<div>• <span class="swagger-code">${esc(f.id)}</span> ${esc(f.text)}</div>`).join("");
    return renderSwaggerShell({
      title: value.product || "Requirements",
      version: "FR/NFR",
      description: `Source: ${(value.source||{}).filename || value.source?.type || ""}`,
      body: renderSwaggerAccordion([
        ["Functional requirements", frs || "<div class='empty'>None</div>"],
        ["Non-functional requirements", nfrs || "<div class='empty'>None</div>"],
        ["Full JSON", `<pre class="swagger-schema">${esc(JSON.stringify(value, null, 2))}</pre>`],
      ], { tag: "REQ", method: "get", openFirst: true }),
    });
  }
  if (key === "hld" && value && typeof value === "object") {
    return renderSwaggerShell({
      title: value.product || "HLD",
      version: value.style || "architecture",
      description: "Structured HLD — open 2. HLD for the visual design",
      body: renderSwaggerAccordion([
        ["Components", `<div>${esc((value.components||[]).join(", "))}</div>`],
        ["Tenets", `<div>${(value.tenets||[]).map(t=>`• ${esc(t)}`).join("<br/>")}</div>`],
        ["Full JSON", `<pre class="swagger-schema">${esc(JSON.stringify(value, null, 2))}</pre>`],
      ], { tag: "HLD", method: "get", openFirst: true }),
    });
  }
  if (key === "openapi") {
    return renderOpenApi(value);
  }
  if (typeof value === "string") {
    return renderSwaggerShell({
      title: sectionLabelForKey(key),
      version: "text",
      body: `<div class="swagger-op" data-method="get"><div class="swagger-op-body" style="display:block"><pre class="swagger-schema">${esc(value)}</pre></div></div>`,
    });
  }
  return renderJsonAccordion(value, {
    title: sectionLabelForKey(key),
    version: "JSON",
  });
}

function renderResults() {
  const c = document.getElementById("content");
  const r = state.results;
  if (!r) {
    c.innerHTML = `<div class="empty">No results yet</div>`;
    return;
  }
  if (r.status !== "SUCCEEDED" && r.status !== "FAILED" && r.status !== "WAITING_APPROVAL" && r.status !== "PARTIAL") {
    c.innerHTML = `<div class="empty">Workflow <span class="chip run">${r.status}</span> — agents still running. Partial design HTML appears at approval pauses.
      <div style="margin-top:10px"><button class="btn" id="btn-safe-stop">Safe-stop workflow</button></div></div>`;
    const stopBtn = document.getElementById("btn-safe-stop");
    if (stopBtn) {
      stopBtn.addEventListener("click", async () => {
        stopBtn.disabled = true;
        await fetch(`/api/workflows/${r.workflow_id}/safe-stop`, { method: "POST" });
        document.getElementById("upload-status").textContent = "Safe-stop requested…";
      });
    }
    return;
  }
  const arts = r.artifacts || {};
  const m = r.metrics || {};
  // Legacy: verdict used to be its own tab — always open under 8. Backend.
  if (state.resultKey === "backend_verdict") state.resultKey = "backend_source";
  const nav = RESULT_SECTIONS.map(([k, label]) => {
    const art = arts[k];
    const ready = (art != null && art.ready !== false)
      || (k === "backend_source" && arts.backend_verdict != null);
    return `<button type="button" data-key="${k}" class="${state.resultKey===k?"active":""}" style="${ready?"":"opacity:.55"}" title="${ready?"Open section":"Not produced yet — click for details"}">${label}</button>`;
  }).join("");
  const body = renderResultBody(state.resultKey, arts[state.resultKey]);
  c.innerHTML = `
    <div class="result-meta">
      <b>${r.product_name || r.workflow_id}</b> · ${r.filename || ""} ·
      <span class="chip ${chipClass(r.status)}">${r.status}</span>
      ${(r.features||[]).map(f=>`<span class="chip">${f}</span>`).join(" ")}
    </div>
    <div class="result-meta" style="display:flex;flex-wrap:wrap;gap:10px;margin-bottom:14px">
      <span>success <b>${fmtRate(m.success_rate)}</b></span>
      <span>retries <b>${m.task_retries ?? 0}</b></span>
      <span>rollbacks <b>${m.rollbacks ?? 0}</b></span>
      <span>MTTR <b>${fmtMs(m.mttr_ms)}</b></span>
      <span>e2e <b>${fmtMs(m.e2e_latency_ms)}</b></span>
      <span>parallel max <b>${m.parallel_max_width ?? 0}</b></span>
    </div>
    <div class="result-nav" id="result-nav">${nav}</div>
    <div class="result-body">${body}</div>
  `;
  document.getElementById("result-nav").addEventListener("click", (ev) => {
    const btn = ev.target.closest("button[data-key]");
    if (!btn) return;
    state.resultKey = btn.dataset.key;
    renderResults();
  });
  const feSel = document.getElementById("fe-page");
  const feBox = document.getElementById("fe-preview");
  if (feSel && feBox && state.fePages) {
    feSel.addEventListener("change", () => {
      const html = state.fePages[feSel.value];
      if (html) {
        feBox.innerHTML = renderHtmlFrame(html, { toolbarLabel: "Frontend HTML preview" });
        mountDesignFrames(feBox);
        bindDesignFullscreenButtons(feBox);
      }
    });
  }
  mountDesignFrames(c);
  bindDesignFullscreenButtons(c);
  c.querySelectorAll("[data-swagger-toggle]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const op = btn.closest(".swagger-op");
      if (op) op.classList.toggle("open");
    });
  });
}

function renderContent() {
  const c = document.getElementById("content");
  if (!state.detail) {
    c.innerHTML = `<div class="empty">Upload a requirement document to begin</div>`;
    return;
  }
  const wf = state.detail.workflow;
  if (state.tab === "results") {
    renderResults();
  } else if (state.tab === "tasks") {
    const tasks = Object.values(wf.tasks || {});
    c.innerHTML = renderSwaggerShell({
      title: "Tasks",
      version: String(tasks.length),
      body: `<div class="tasks">${tasks.map(t => `
      <div class="task">
        <span class="mono">${t.id}</span>
        <span>${t.agent}</span>
        <span class="chip ${chipClass(t.status)}">${t.status}</span>
      </div>`).join("")}</div>`,
    });
    bindDesignFullscreenButtons(c);
  } else if (state.tab === "trace") {
    c.innerHTML = renderSwaggerShell({
      title: "Trace",
      version: String(state.trace.length),
      body: `<div class="timeline">${state.trace.slice().reverse().map(e => `
      <div class="evt">
        <div class="t">${new Date(e.ts * 1000).toLocaleString()} · ${e.agent || "orchestrator"}</div>
        <div><span class="type">${e.type}</span> ${e.task_id ? `<span class="mono"> ${e.task_id}</span>` : ""}</div>
      </div>`).join("") || '<div class="empty">No audit events</div>'}</div>`,
    });
    bindDesignFullscreenButtons(c);
  } else if (state.tab === "dag") {
    if (!state.selected) {
      c.innerHTML = `<div class="empty">Select a workflow</div>`;
    } else {
      c.innerHTML = renderStudioFrame(state.selected, "dag", { toolbarLabel: "1. DAG", bare: true });
      bindDesignFullscreenButtons(c);
    }
  } else if (state.tab === "artifacts") {
    const keys = Object.keys(state.results?.artifacts || state.detail.summary?.artifacts || {});
    c.innerHTML = renderSwaggerShell({
      title: "Artifacts",
      version: String(keys.length),
      body: `<div class="tasks">${keys.map(k => `
      <div class="task"><span class="mono">${k}</span><span></span><span class="chip">ready</span></div>
    `).join("") || '<div class="empty">No artifacts yet</div>'}</div>`,
    });
    bindDesignFullscreenButtons(c);
  }
}

function closeApprovalModal() {
  const figmaBox = document.getElementById("figma-upload-box");
  if (figmaBox) figmaBox.hidden = true;
  const modal = document.getElementById("approval-modal");
  if (modal) modal.hidden = true;
  document.body.style.overflow = "";
}

async function submitApprovalDecision(id, appr, decision, btn) {
  if (btn) btn.disabled = true;
  // Claim this gate before the request so an in-flight poll cannot re-open it.
  state.approvalSubmittedFor = String(appr.id || appr.task_id || "");
  const openWorkspace = decision === "open_workspace";
  document.getElementById("upload-status").textContent = `Submitting ${decision}…`;
  const res = await fetch(`/api/workflows/${id}/approve`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      decision,
      approval_id: appr.id,
      task_id: appr.task_id,
      rationale: "Approved in Forge Studio"
    })
  });
  const out = await res.json().catch(() => ({}));
  if (!res.ok) {
    document.getElementById("upload-status").textContent = out.detail || "Approval failed";
    if (btn) btn.disabled = false;
    // Release the claim so the gate can be answered again.
    state.approvalSubmittedFor = null;
    state.approvalRenderedFor = null;
    return;
  }
  closeApprovalModal();
  state.approvalRenderedFor = null;
  const panel = document.getElementById("approval-panel");
  if (panel) { panel.style.display = "none"; panel.innerHTML = ""; }
  const task = appr.task_id || "";
  if (openWorkspace || task.includes("approval.coding")) {
    state.resultKey = "workspace_manifest";
    state.tab = "results";
    document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.dataset.tab === "results"));
  }
  document.getElementById("upload-status").textContent = task.includes("approval.clarify")
    ? "Clarification accepted — continuing analysis…"
    : task.includes("approval.figma")
    ? (decision === "figma_uploaded"
      ? "Figma attached — UI agent designing from Figma + LLD…"
      : "Continuing without Figma — UI agent designing from LLD…")
    : task.includes("approval.coding")
    ? (openWorkspace
      ? "Workspace approved — opening Workspace; continuing to tests…"
      : "Workspace approved — continuing to tests…")
    : task.includes("approval.api")
    ? "API approved — creating workspace and coding FastAPI + UI…"
    : task.includes("approval.db")
    ? "DB approved — starting API agent…"
    : "Approval submitted — resuming…";
  await waitForWorkflow(id, 300000);
  await selectWorkflow(id);
  if (openWorkspace) state.resultKey = "workspace_manifest";
  renderContent();
}

async function refreshApprovalPanel(id) {
  const panel = document.getElementById("approval-panel");
  const modal = document.getElementById("approval-modal");
  if (!panel || !modal) return;
  const data = await fetch(`/api/workflows/${id}/pending-approval`).then(r => r.json()).catch(() => null);
  if (!data || !data.pending || !data.pending.length) {
    panel.style.display = "none";
    panel.innerHTML = "";
    closeApprovalModal();
    state.approvalRenderedFor = null;
    state.approvalSubmittedFor = null;
    return;
  }
  const appr = data.pending[0];
  const apprKey = String(appr.id || appr.task_id || "");
  // Resume is asynchronous: the gate stays REQUESTED for a moment after the
  // POST returns. Without this the next tick re-opens the modal the user just
  // answered, and re-enables the buttons they already clicked.
  if (state.approvalSubmittedFor === apprKey) return;
  // Already showing this exact gate — leave the DOM alone.
  if (state.approvalRenderedFor === apprKey && !modal.hidden) return;
  const taskId = String(appr.task_id || "");
  const isClarify = taskId.includes("approval.clarify");
  const isCoding = taskId.includes("approval.coding");
  const isFigma = taskId.includes("approval.figma");
  const heading = isClarify
    ? "Clarify requirements"
    : isFigma
    ? "Figma design input (optional)"
    : isCoding
    ? "Coding complete — approve workspace"
    : "Human approval required";
  const options = (appr.options && appr.options.length)
    ? appr.options
    : (isCoding
      ? [
          {id: "approve", label: "Approve workspace — continue to tests"},
          {id: "open_workspace", label: "Approve & open Workspace"},
          {id: "reject", label: "Reject codegen — stop"},
        ]
      : isFigma
      ? [
          {id: "agent_design", label: "Continue without Figma — design UI from LLD"},
          {id: "figma_uploaded", label: "I uploaded Figma — design UI from Figma + LLD"},
          {id: "reject", label: "Reject — stop"},
        ]
      : [
          {id: "approve", label: "Approve"},
          {id: "reject", label: "Reject"},
        ]);
  const optsHtml = options.map(o => {
    const reject = String(o.id).toLowerCase() === "reject" || String(o.id).toLowerCase() === "nogo";
    return `<button type="button" class="btn ${reject ? "secondary" : ""}" data-decision="${esc(o.id)}">${esc(o.label || o.id)}</button>`;
  }).join("");

  // Inline panel under upload (always visible while paused — backup if modal missed)
  panel.style.display = "block";
  panel.innerHTML = `
    <h3 style="margin:0 0 6px;font-size:1rem">${esc(heading)}</h3>
    <div class="result-meta"><b>${esc(appr.title || appr.task_id)}</b> · <span class="chip">WAITING_APPROVAL</span></div>
    <p style="color:var(--muted);font-size:0.88rem;margin:6px 0 10px">${esc(appr.summary || "")}</p>
    <p style="font-size:0.8rem;color:var(--muted)">${esc(data.gate_note || "")}</p>
    <div class="approval-modal-actions">${optsHtml}</div>
  `;
  document.getElementById("upload-status").textContent =
    `Paused at ${taskId || "approval"} — use the modal (or buttons below) to approve.`;

  // Modal (primary UX)
  document.getElementById("approval-modal-heading").textContent = heading;
  document.getElementById("approval-modal-title").textContent = appr.title || appr.task_id || "";
  document.getElementById("approval-modal-summary").textContent = appr.summary || "";
  document.getElementById("approval-modal-note").textContent = data.gate_note
    || (isClarify
      ? "Choose how to interpret ambiguous requirements, then the plan agent continues."
      : isFigma
      ? "Upload a Figma export/URL first (optional), then continue — or skip Figma and design from LLD."
      : isCoding
      ? "Review the generated workspace, then approve to run tests and validation."
      : "Approve to continue the SDLC pipeline.");
  const figmaBox = document.getElementById("figma-upload-box");
  if (figmaBox) figmaBox.hidden = !isFigma;
  if (isFigma) {
    const st = document.getElementById("figma-upload-status");
    if (st) st.textContent = "";
  }
  document.getElementById("approval-modal-actions").innerHTML = optsHtml;
  modal.hidden = false;
  document.body.style.overflow = "hidden";
  state.approvalRenderedFor = apprKey;

  const bind = (root) => {
    root.querySelectorAll("button[data-decision]").forEach(btn => {
      btn.addEventListener("click", () => {
        const decision = btn.dataset.decision;
        const run = async () => {
          if (isFigma && decision === "figma_uploaded") {
            await uploadFigmaAttachment(id);
          }
          await submitApprovalDecision(id, appr, decision, btn);
        };
        run().catch((err) => {
          document.getElementById("upload-status").textContent = `Error: ${err.message || err}`;
          btn.disabled = false;
          // A throw must not leave the gate permanently suppressed.
          state.approvalSubmittedFor = null;
          state.approvalRenderedFor = null;
        });
      });
    });
  };
  bind(panel);
  bind(document.getElementById("approval-modal-actions"));
}

async function uploadFigmaAttachment(workflowId) {
  const fileEl = document.getElementById("figma-file-input");
  const urlEl = document.getElementById("figma-url-input");
  const statusEl = document.getElementById("figma-upload-status");
  const file = fileEl && fileEl.files && fileEl.files[0];
  const url = (urlEl && urlEl.value || "").trim();
  if (!file && !url) {
    throw new Error("Upload a Figma file or paste a Figma URL before choosing “I uploaded Figma”");
  }
  const body = new FormData();
  if (file) body.append("file", file);
  if (url) body.append("figma_url", url);
  if (statusEl) statusEl.textContent = "Uploading Figma…";
  const res = await fetch(`/api/workflows/${workflowId}/figma`, { method: "POST", body });
  const out = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(out.detail || "Figma upload failed");
  if (statusEl) statusEl.textContent = "Figma attached.";
  return out;
}

async function waitForWorkflow(id, timeoutMs = 300000) {
  const start = Date.now();
  let sawCodeUnlock = false;
  while (Date.now() - start < timeoutMs) {
    const detail = await fetch(`/api/workflows/${id}`).then(r => r.json());
    const wf = detail.workflow || {};
    const status = wf.status;
    const facts = wf.facts || {};
    const tasks = wf.tasks || {};
    const pendingTask = Object.values(tasks).find(t => t.status === "WAITING_APPROVAL");
    let phase = "Design in progress…";
    if (pendingTask?.id?.includes("approval.clarify")) phase = "Paused — clarify requirements in the modal";
    else if (pendingTask?.id?.includes("approval.figma")) phase = "Paused — optional Figma upload before UI coding";
    else if (pendingTask?.id?.includes("approval.coding")) phase = "Paused — coding complete; approve workspace in the modal";
    else if (pendingTask?.id?.includes("approval.db")) phase = "Paused — approve DB/LLD to start API";
    else if (pendingTask?.id?.includes("approval.api")) phase = "Paused — approve API to start coding";
    else if (pendingTask?.id?.includes("approval.arch")) phase = "Paused — approve architecture";
    else if (pendingTask?.id?.includes("approval.plan")) phase = "Paused — approve plan";
    else if (facts.code_unlocked || facts.frozen_api) {
      sawCodeUnlock = true;
      phase = facts.coding_approved
        ? "Workspace approved — testing…"
        : facts.coding_complete
        ? "Coding complete — waiting for workspace approval"
        : "Creating workspace and coding FastAPI + UI…";
    } else if (facts.api_unlocked) {
      phase = "API design running…";
    }
    document.getElementById("upload-status").textContent =
      `Workflow ${id} · ${status} · ${phase}`;

    if (status === "WAITING_APPROVAL") {
      await selectWorkflow(id);
      await refreshApprovalPanel(id);
      if (pendingTask?.id?.includes("approval.coding")) {
        state.resultKey = "workspace_manifest";
        state.codingToastFor = id;
        renderContent();
      }
      return status;
    }
    if (status === "SUCCEEDED" || status === "FAILED") {
      const panel = document.getElementById("approval-panel");
      if (panel) { panel.style.display = "none"; panel.innerHTML = ""; }
      closeApprovalModal();
      return status;
    }
    await new Promise(r => setTimeout(r, 800));
  }
  return "TIMEOUT";
}

function autoApproveFlag() {
  const el = document.getElementById("chk-human");
  return el && el.checked ? "false" : "true";
}

document.getElementById("tabs").addEventListener("click", (ev) => {
  const btn = ev.target.closest(".tab");
  if (!btn) return;
  state.tab = btn.dataset.tab;
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t === btn));
  renderContent();
});

// Fetched as a blob rather than navigating to the URL: a navigation would drop
// the token header, and this way a failure surfaces as a message instead of the
// browser opening an error page over the Studio.
async function downloadWorkflowZip(id, btn) {
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Preparing…";
  try {
    const res = await fetch(`/api/workflows/${id}/download`);
    if (!res.ok) {
      const out = await res.json().catch(() => ({}));
      throw new Error(out.detail || `HTTP ${res.status}`);
    }
    const url = URL.createObjectURL(await res.blob());
    const a = document.createElement("a");
    a.href = url;
    a.download = `${id}_forge.zip`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 0);
    document.getElementById("upload-status").textContent = "Artifacts downloaded.";
  } catch (err) {
    document.getElementById("upload-status").textContent =
      `Download failed: ${err.message || err}`;
  } finally {
    btn.disabled = !state.selected;
    btn.textContent = label;
  }
}

document.getElementById("download-btn").addEventListener("click", (ev) => {
  if (state.selected) downloadWorkflowZip(state.selected, ev.currentTarget);
});

async function startAndShow(startPromise) {
  state.uploading = true;
  state.tab = "results";
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.dataset.tab === "results"));
  document.getElementById("btn-example").disabled = true;
  document.getElementById("btn-cleanup").disabled = true;
  document.getElementById("btn-cleanup-finished").disabled = true;
  try {
    const data = await startPromise;
    await refreshOverview();
    await refreshWorkflows();
    await selectWorkflow(data.workflow_id);
    const finalStatus = await waitForWorkflow(data.workflow_id);
    await refreshOverview();
    await selectWorkflow(data.workflow_id);
    state.resultKey = "architecture_design_html";
    state.tab = "results";
    document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.dataset.tab === "results"));
    renderContent();
    const facts = state.detail?.workflow?.facts || {};
    const pending = Object.values(state.detail?.workflow?.tasks || {}).find(t => t.status === "WAITING_APPROVAL");
    let pauseMsg = "Paused for human approval";
    if (pending?.id?.includes("approval.clarify")) {
      pauseMsg = "Paused — clarify requirements in the modal, then continue";
    } else if (pending?.id?.includes("approval.figma")) {
      pauseMsg = "Paused — upload Figma (optional) or continue so the UI agent designs from LLD";
    } else if (pending?.id?.includes("approval.coding")) {
      pauseMsg = "Paused — coding complete; approve the workspace in the modal to continue";
    } else if (pending?.id?.includes("approval.api")) {
      pauseMsg = "Paused — review OpenAPI (Swagger), then approve to create workspace & start coding";
    } else if (pending?.id?.includes("approval.db")) {
      pauseMsg = "Paused — review DB/LLD HTML, then approve to start API agent";
    }
    document.getElementById("upload-status").textContent =
      finalStatus === "SUCCEEDED"
        ? (facts.coding_complete
          ? `Done · coding complete · workspace ${facts.workspace_path || ""}`
          : `Done · ${data.product_name || data.workflow_id}`)
        : finalStatus === "WAITING_APPROVAL"
        ? pauseMsg
        : `Finished with status ${finalStatus}`;
  } catch (err) {
    document.getElementById("upload-status").textContent = `Error: ${err.message || err}`;
  } finally {
    state.uploading = false;
    document.getElementById("btn-example").disabled = false;
    document.getElementById("btn-cleanup").disabled = false;
    document.getElementById("btn-cleanup-finished").disabled = false;
  }
}

async function uploadFile(file) {
  if (!file || state.uploading) return;
  document.getElementById("upload-status").textContent = `Uploading ${file.name}…`;
  const body = new FormData();
  body.append("file", file);
  body.append("auto_approve", autoApproveFlag());
  await startAndShow((async () => {
    const res = await fetch("/api/workflows/from-document", { method: "POST", body });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Upload failed");
    return data;
  })());
}

async function runExample() {
  if (state.uploading) return;
  document.getElementById("upload-status").textContent = "Starting TinyURL example PRD…";
  const body = new FormData();
  body.append("name", "tinyurl-requirements.md");
  body.append("auto_approve", autoApproveFlag());
  await startAndShow((async () => {
    const res = await fetch("/api/workflows/from-example", { method: "POST", body });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Failed");
    return data;
  })());
}

async function cleanupRuns({ finishedOnly = false } = {}) {
  if (state.uploading) return;
  const msg = finishedOnly
    ? "Clear finished and stale runs (including their workspaces)? Live runs in this session are kept."
    : "Clear all workflow runs, artifacts, and workspaces under var/workspaces & var/deliverables?";
  if (!confirm(msg)) return;
  const url = finishedOnly
    ? "/api/workflows/cleanup?finished_only=true"
    : "/api/workflows/cleanup?confirm=true";
  const res = await fetch(url, { method: "DELETE" });
  const data = await res.json().catch(() => ({}));
  const keepSelected = finishedOnly && state.selected
    && state.workflows?.some(w => w.id === state.selected
      && (w.status === "RUNNING" || w.status === "WAITING_APPROVAL"));
  if (!keepSelected) {
    state.selected = null;
    state.detail = null;
    state.results = null;
    const dl = document.getElementById("download-btn");
    if (dl) dl.disabled = true;
    document.getElementById("content").innerHTML = `<div class="empty">Upload a requirement document to see HLD, LLD, APIs, and code</div>`;
  }
  const n = data.workflows ?? 0;
  const ws = data.workspaces ?? 0;
  document.getElementById("upload-status").textContent = finishedOnly
    ? `Cleared ${n} finished/stale run${n === 1 ? "" : "s"} (${ws} workspace${ws === 1 ? "" : "s"})`
    : `Cleared all runs · removed ${ws} workspace folder${ws === 1 ? "" : "s"}`;
  await refreshOverview();
  await refreshWorkflows();
  if (keepSelected) await selectWorkflow(state.selected);
}

async function boot() {
  document.getElementById("clock").textContent = new Date().toLocaleString();

  const fileInput = document.getElementById("file-input");
  const drop = document.getElementById("dropzone");
  document.getElementById("btn-example").addEventListener("click", (e) => {
    e.preventDefault();
    runExample().catch((err) => {
      document.getElementById("upload-status").textContent = `Error: ${err.message || err}`;
    });
  });
  document.getElementById("btn-cleanup-finished").addEventListener("click", (e) => {
    e.preventDefault();
    cleanupRuns({ finishedOnly: true }).catch((err) => {
      document.getElementById("upload-status").textContent = `Error: ${err.message || err}`;
    });
  });
  document.getElementById("btn-cleanup").addEventListener("click", (e) => {
    e.preventDefault();
    cleanupRuns({ finishedOnly: false }).catch((err) => {
      document.getElementById("upload-status").textContent = `Error: ${err.message || err}`;
    });
  });
  document.getElementById("design-fs-close").addEventListener("click", closeDesignFullscreen);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !document.getElementById("design-fs").hidden) {
      closeDesignFullscreen();
    }
  });
  fileInput.addEventListener("change", () => {
    if (fileInput.files[0]) uploadFile(fileInput.files[0]);
  });
  drop.addEventListener("click", () => fileInput.click());
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("drag"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("drag"));
  drop.addEventListener("drop", (e) => {
    e.preventDefault();
    drop.classList.remove("drag");
    if (e.dataTransfer.files[0]) uploadFile(e.dataTransfer.files[0]);
  });

  try {
    const agents = await fetch("/api/agents").then(r => r.json()).catch(() => []);
    document.getElementById("agent-steps").innerHTML = agents.slice(0, 16).map(a =>
      `<span class="chip">${a.id}</span>`
    ).join("");
    await refreshOverview();
    await refreshWorkflows();
    const params = new URLSearchParams(location.search);
    const wfParam = params.get("wf");
    if (wfParam) await selectWorkflow(wfParam);
    else if (state.workflows[0]) await selectWorkflow(state.workflows[0].id);
  } catch (err) {
    document.getElementById("upload-status").textContent =
      `Studio API error: ${err.message || err}. Start with: cd app && uvicorn main:app --port 8787`;
  }

  setInterval(async () => {
    try {
      document.getElementById("clock").textContent = new Date().toLocaleString();
      await refreshOverview();
      await refreshWorkflows();
      // Poll must preserve Results nav (e.g. stay on 8. Backend, not jump to 1. DAG).
      if (state.selected && !state.uploading) {
        await selectWorkflow(state.selected, { resetSection: false });
      }
    } catch (_) { /* keep UI responsive while API is down */ }
  }, 4000);
}
boot();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(DASHBOARD_HTML)


def serve(host: str = "127.0.0.1", port: int = 8787, reload: bool = False) -> None:
    """Run the studio via uvicorn (also: `uvicorn main:app --reload`)."""
    import uvicorn

    ensure_runtime_dirs()
    print(f"Forge Agentic SDLC Studio → http://{host}:{port}/")
    print("  Upload a requirement document to run all agents end-to-end")
    print("  Production entry: uvicorn main:app --host 0.0.0.0 --port 8787")
    uvicorn.run(
        "forge.dashboard:app" if reload else app,
        host=host,
        port=port,
        log_level="info",
        reload=reload,
    )
