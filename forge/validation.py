from __future__ import annotations

import os
from typing import Any, List

from .codegen_validation import (
    compile_python_sources,
    derive_operation_coverage,
    operations,
    validate_openapi,
)
from .core.paths import paths as forge_paths
from .models import ValidationResult, Workflow

# Minimum share of documented API operations that must have a named test case.
MIN_OPERATION_COVERAGE_PCT = 80.0


def _passed(gate: str, ok: bool, detail: str, *, blocking: bool = True) -> ValidationResult:
    return ValidationResult(
        gate=gate, status="PASS" if ok else "FAIL", blocking=blocking, detail=detail
    )


def _skipped(gate: str, detail: str) -> ValidationResult:
    """
    A gate that cannot apply at this stage.

    Reported as SKIP and non-blocking — never as a green PASS. A gate that silently
    reports PASS when it did not run is worse than no gate at all.
    """
    return ValidationResult(gate=gate, status="SKIP", blocking=False, detail=detail)


def _hld_tenets_text(wf: Workflow) -> str:
    hld = wf.artifacts.get("hld")
    if not hld:
        return ""
    tenets = hld.content.get("tenets") or []
    parts: list[str] = []
    for t in tenets:
        if isinstance(t, str):
            parts.append(t)
        elif isinstance(t, dict):
            parts.append(str(t.get("text") or t.get("name") or t))
        else:
            parts.append(str(t))
    # Also scan full HLD blob for LLM-shaped outputs
    parts.append(str(hld.content))
    return " ".join(parts).lower()


def _tenets_only(wf: Workflow) -> str:
    hld = wf.artifacts.get("hld")
    if not hld:
        return ""
    parts: list[str] = []
    for t in hld.content.get("tenets") or []:
        if isinstance(t, str):
            parts.append(t)
        elif isinstance(t, dict):
            parts.append(str(t.get("text") or t.get("name") or t))
        else:
            parts.append(str(t))
    return " ".join(parts).lower()


# Markers that the design itself claims a dual-write / async side-effect risk.
_DUAL_WRITE_RISK = (
    "dual-write",
    "dual write",
    "outbox",
    "analytics side",
    "side-effect",
    "side effect",
    "click event",
    "click stream",
    "event bus",
    "publish event",
    "async publish",
    "kafka",
)

# Markers that the design claims a cache-first hot path.
_CACHE_FIRST_CLAIM = (
    "cache-first",
    "cache first",
    "redirect path",
    "redirect hot",
    "hot-path",
    "hot path",
)


def _design_claims(blob: str, markers: tuple[str, ...]) -> bool:
    return any(m in blob for m in markers)


def _artifact(wf: Workflow, key: str) -> Any:
    """Latest artifact content, or None. Never raises on unexpected shapes."""
    art = wf.artifacts.get(key)
    return art.content if art is not None else None


def _openapi_gate(wf: Workflow) -> ValidationResult:
    """Structural validation of the frozen API contract — not just its presence."""
    spec = _artifact(wf, "openapi")
    if spec is None:
        return _passed("api.openapi_valid", False, "No OpenAPI artifact published")
    errors = validate_openapi(spec)
    if errors:
        preview = "; ".join(errors[:3])
        more = f" (+{len(errors) - 3} more)" if len(errors) > 3 else ""
        return _passed(
            "api.openapi_valid", False, f"{len(errors)} contract error(s): {preview}{more}"
        )
    return _passed(
        "api.openapi_valid",
        True,
        f"OpenAPI 3.x contract resolves — {len(operations(spec))} operations",
    )


def _compile_gate(wf: Workflow) -> ValidationResult:
    """Every generated Python file must parse. Proves the codegen emitted real code."""
    root = forge_paths().workspaces / wf.id / "backend"
    if not root.exists():
        return _skipped("code.compiles", "No generated backend workspace at this stage")
    errors = compile_python_sources(root)
    if errors:
        preview = "; ".join(errors[:3])
        return _passed(
            "code.compiles", False, f"{len(errors)} syntax error(s) in generated code: {preview}"
        )
    count = sum(1 for _ in root.rglob("*.py"))
    return _passed("code.compiles", True, f"All {count} generated Python files parse")


def _coverage_gate(wf: Workflow) -> ValidationResult:
    """
    Coverage measured across two independent artifacts (OpenAPI × test plan)
    rather than read back from a number the testing agent asserted about itself.
    """
    plan = _artifact(wf, "test_plan")
    if plan is None:
        return _passed("test.coverage_critical", False, "No test plan published")
    cov = derive_operation_coverage(_artifact(wf, "openapi"), plan)
    pct = cov["coverage_pct"]
    if pct is None:
        return _skipped(
            "test.coverage_critical", "No OpenAPI operations to measure coverage against"
        )
    detail = (
        f"{cov['covered_operations']}/{cov['declared_operations']} API operations "
        f"have a named test case ({pct}%)"
    )
    if cov["uncovered"]:
        detail += " — uncovered: " + ", ".join(cov["uncovered"][:3])
    return _passed("test.coverage_critical", pct >= MIN_OPERATION_COVERAGE_PCT, detail)


def _security_verdict_gate(wf: Workflow) -> ValidationResult:
    """
    Derive the security verdict from the scan artifact, never from a mutable fact.

    A workflow fact can be overwritten by a re-plan; the published artifact is the
    evidence of record. Missing or malformed verdict fails closed.
    """
    scan = _artifact(wf, "security_scan")
    if scan is None:
        return _skipped(
            "sec.validation_passed", "No security_scan artifact to judge at this stage"
        )
    content = scan if isinstance(scan, dict) else {}
    verdict = str(content.get("verdict") or "").upper()
    unresolved = content.get("critical_open") or []
    count = len(unresolved) if isinstance(unresolved, list) else 0
    return _passed(
        "sec.validation_passed",
        verdict == "PASS" and count == 0,
        f"security_scan verdict={verdict or 'UNKNOWN'}, unresolved critical/high={count}",
    )


def evaluate(wf: Workflow, stage: str = "pre_release") -> List[ValidationResult]:
    results: list[ValidationResult] = []

    def has(*keys: str) -> bool:
        return all(k in wf.artifacts for k in keys)

    hld_blob = _hld_tenets_text(wf)
    tenets = _tenets_only(wf)

    # Context-driven: only require outbox / cache-first when THIS run's HLD claims them.
    dual_write_risk = _design_claims(hld_blob, _DUAL_WRITE_RISK)
    cache_first_claim = _design_claims(hld_blob, _CACHE_FIRST_CLAIM)
    outbox_ok = (not dual_write_risk) or ("outbox" in tenets) or ("outbox" in hld_blob)
    cache_ok = (not cache_first_claim) or ("cache" in tenets) or ("cache" in hld_blob)

    # Stages where an artifact's producing agent has not run yet by design.
    # These are reported SKIP (non-blocking), never as a green PASS.
    docs_deferred = stage == "quality_gate" and not has("documentation")
    o11y_deferred = (
        stage == "quality_gate"
        and not has("workload_dashboards")
        and not has("observability_plan")
    )
    scan_deferred = stage == "brownfield" and not has("security_scan")

    results += [
        _passed("arch.adr_complete", has("hld", "adrs"), "HLD and ADRs present"),
        _passed("arch.nfr_covered", has("reqspec"), "ReqSpec present for NFR mapping"),
        _passed(
            "arch.capacity_present",
            has("capacity_estimate"),
            "Capacity estimate published by the architecture agent",
        ),
        _passed(
            "plan.decomposed",
            has("execution_plan") or has("risk_register") or has("task_breakdown"),
            "Execution plan or risks present",
        ),
        _passed(
            "plan.dag",
            has("dependency_graph") or has("forge_dag_spec"),
            "Dependency DAG present",
        ),
        _passed(
            "domain.model",
            has("domain_model") or has("reqspec"),
            "Domain model or ReqSpec present",
        ),
        _passed("code.test_scaffold", has("test_plan"), "Test plan present"),
        _passed(
            "code.no_dual_write",
            has("hld") and outbox_ok,
            (
                "Outbox / dual-write mitigation present in HLD"
                if dual_write_risk
                else "No dual-write / async side-effect pattern claimed in HLD (N/A)"
            ),
        ),
        _passed(
            "code.source_present",
            has("source_tree") or has("backend_source"),
            "Source artifacts present",
        ),
        _passed("sec.threat_model", has("security_review"), "Security review present"),
        _skipped("sec.scan", "Security scan runs after this stage on brownfield")
        if scan_deferred
        else _passed("sec.scan", has("security_scan"), "Post-validation security scan present"),
        _passed(
            "sec.open_redirect",
            (not wf.facts.get("fix_open_redirect")) or has("bugfix_open_redirect"),
            "Open redirect addressed when in scope",
        ),
        _passed(
            "perf.cache_first",
            has("hld") and cache_ok,
            (
                "Cache-first tenet present"
                if cache_first_claim
                else "No cache-first hot-path claimed in HLD (N/A)"
            ),
        ),
        _passed("perf.budget", has("perf_budget"), "Perf budget defined"),
        _passed("scale.shard_key", has("schema_ddl"), "Sharding strategy documented"),
        _coverage_gate(wf),
        _skipped("docs.runbook_exists", "Documentation agent runs after this stage")
        if docs_deferred
        else _passed("docs.runbook_exists", has("documentation"), "Runbooks present"),
        _openapi_gate(wf),
        _compile_gate(wf),
        _passed("risk.register_current", has("risk_register"), "Risk register present"),
        _skipped("o11y.dashboards", "Observability agent runs after this stage")
        if o11y_deferred
        else _passed(
            "o11y.dashboards",
            has("workload_dashboards") or has("observability_plan"),
            "Observability dashboards or plan present",
        ),
        _passed(
            "eng.validation",
            has("engineering_validation") or has("review_report"),
            "Engineering validation or review present",
        ),
        _security_verdict_gate(wf),
    ]

    if stage == "brownfield" and wf.facts.get("feature_qr"):
        results.append(
            _passed(
                "db.migration_plan",
                has("db_optimize_plan"),
                "DB optimize plan required for brownfield",
            )
        )

    inject = os.environ.get("FORGE_INJECT_FAIL")
    if inject:
        for r in results:
            if r.gate == inject:
                r.status = "FAIL"
                r.blocking = True
                r.detail = f"Injected failure via FORGE_INJECT_FAIL ({r.detail})"

    return results


def overall_pass(results: List[ValidationResult]) -> bool:
    """
    A stage passes when every blocking gate passed.

    SKIP is non-blocking by construction: it means "this gate does not apply yet",
    and the gate is re-evaluated for real at a later stage.
    """
    return all(r.status == "PASS" or not r.blocking for r in results)


def summarize(results: List[ValidationResult]) -> dict:
    """Counts for audit payloads and the Studio validation panel."""
    return {
        "passed": sum(1 for r in results if r.status == "PASS"),
        "failed": sum(1 for r in results if r.status == "FAIL"),
        "skipped": sum(1 for r in results if r.status == "SKIP"),
        "blocking_failures": [
            r.gate for r in results if r.status == "FAIL" and r.blocking
        ],
    }
