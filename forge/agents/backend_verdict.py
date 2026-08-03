"""Build a durable backend failure/compensation verdict with root cause."""

from __future__ import annotations

from typing import Any

from ..models import Artifact, TaskNode, Workflow
from ._common import art, content_hash


def _security_findings(wf: Workflow) -> list[dict[str, Any]]:
    scan = art(wf, "security_scan") or {}
    findings = list(scan.get("findings") or scan.get("critical_open") or [])
    out: list[dict[str, Any]] = []
    for f in findings:
        if isinstance(f, dict):
            out.append(
                {
                    "severity": f.get("severity") or "UNKNOWN",
                    "finding": f.get("finding") or f.get("threat") or str(f),
                    "recommendation": f.get("recommendation") or "",
                }
            )
        else:
            out.append({"severity": "UNKNOWN", "finding": str(f), "recommendation": ""})
    return out


def _failed_gates(wf: Workflow) -> list[dict[str, Any]]:
    report = art(wf, "validation_report") or {}
    failed = []
    for r in report.get("results") or []:
        if isinstance(r, dict) and r.get("status") == "FAIL":
            failed.append(
                {
                    "gate": r.get("gate"),
                    "detail": r.get("detail"),
                    "blocking": r.get("blocking"),
                }
            )
    return failed


def build_backend_verdict(wf: Workflow, *, reason: str = "") -> dict[str, Any]:
    """Explain why backend work was rolled back / marked failed."""
    findings = _security_findings(wf)
    failed_gates = _failed_gates(wf)
    scan = art(wf, "security_scan") or {}
    high = [
        f
        for f in findings
        if str(f.get("severity", "")).upper() in {"CRITICAL", "HIGH"}
    ]

    sec_failed = (
        wf.facts.get("security_validation_passed") is False
        or scan.get("verdict") == "FAIL"
    )
    if sec_failed:
        root = "security.scan FAIL → sec.validation_passed gate blocked pre-release"
        if high:
            detail = "; ".join(
                f"{f.get('severity')}: {f.get('finding')}" for f in high[:4]
            )
            root = f"{root}. Unresolved: {detail}"
        cause_label = "security validation"
    elif failed_gates:
        gates = ", ".join(str(g.get("gate")) for g in failed_gates)
        root = f"Blocking validation gate(s) failed: {gates}"
        cause_label = f"validation gate(s): {gates}"
    elif reason:
        root = reason
        cause_label = reason
    else:
        root = "Backend side effects were compensated after a downstream failure"
        cause_label = "a downstream failure"

    remediation = list(scan.get("remediation") or [])
    if not remediation and high:
        remediation = [
            f.get("recommendation") for f in high if f.get("recommendation")
        ]

    return {
        "verdict": "FAIL",
        "status": "COMPENSATED",
        "agent": "backend",
        "task_id": "backend.implement",
        "summary": (
            "Backend code generated successfully, then compensated (rolled back). "
            f"Root cause is {cause_label} — not a codegen crash."
        ),
        "root_cause": root,
        "reason": reason or "task_failed:Blocking validation gates failed",
        "security_scan_verdict": scan.get("verdict") or "unknown",
        "security_validation_passed": wf.facts.get("security_validation_passed"),
        "failed_gates": failed_gates,
        "security_findings": findings,
        "remediation": remediation,
        "workspace_note": (
            "Generated files under var/workspaces/<wf>/backend were removed by the "
            "compensation saga after validate.pre_release failed."
        ),
    }


def publish_backend_verdict(
    wf: Workflow,
    task: TaskNode | None,
    *,
    reason: str = "",
) -> dict[str, Any]:
    """Persist backend_verdict so Results UI can show why backend looks failed."""
    verdict = build_backend_verdict(wf, reason=reason)
    task_id = task.id if task else "backend.implement"
    prev = wf.artifacts.get("backend_verdict")
    version = 1 if prev is None else prev.version + 1
    art_obj = Artifact(
        key="backend_verdict",
        version=version,
        task_id=task_id,
        content=verdict,
        content_hash=content_hash(verdict),
    )
    wf.artifacts["backend_verdict"] = art_obj
    wf.artifact_history.append(art_obj)
    if task is not None:
        task.outputs["backend_verdict"] = {
            "version": version,
            "hash": art_obj.content_hash,
            "verdict": verdict.get("verdict"),
            "root_cause": verdict.get("root_cause"),
        }
        # Replace vague "| compensated" with actionable root cause
        task.error = f"COMPENSATED — {verdict.get('root_cause')}"
    wf.facts["backend_verdict"] = {
        "verdict": verdict.get("verdict"),
        "root_cause": verdict.get("root_cause"),
    }
    return verdict
