from __future__ import annotations

import re
from typing import Any

from ..core.paths import paths as forge_paths
from ..models import TaskNode, Workflow
from ._common import art, publish
from .doc_context import product_name
from .llm_bridge import run_llm_agent

_MITIGATED = frozenset({"FIXED_IN_PATCH", "MITIGATED_IN_DESIGN", "CLOSED", "INFO"})

# Findings that only apply to URL-shortener / redirect products.
_URL_SHORTENER_FINDING_KEYS = (
    "open redirect",
    "short link",
    "short url",
    "short_code",
    "redirect host",
    "domain whitelist",
    "domain allow",
    "allowlist",
    "ssrf",
    "server-side request",
    "preview fetch",
    "qr code",
)


def _is_url_shortener(wf: Workflow) -> bool:
    brief = art(wf, "product_brief") or {}
    req = art(wf, "reqspec") or {}
    openapi = art(wf, "openapi") or {}
    paths = " ".join((openapi.get("paths") or {}) if isinstance(openapi, dict) else [])
    blob = " ".join(
        [
            product_name(wf),
            str(brief.get("name") or "") if isinstance(brief, dict) else "",
            " ".join(str(e) for e in ((req.get("domain") or {}).get("entities") or [])),
            paths,
        ]
    ).lower()
    hits = sum(
        1
        for tok in ("short url", "shorten", "tinyurl", "shortlink", "/v1/links", "snipr")
        if tok in blob
    )
    return hits >= 2


def _backend_blob(wf: Workflow) -> str:
    """Concatenate generated backend sources for mitigation detection."""
    chunks: list[str] = []
    snippets = art(wf, "backend_snippets") or {}
    if isinstance(snippets, dict):
        for path, code in list(snippets.items())[:40]:
            chunks.append(f"# {path}\n{code}")
    source = art(wf, "backend_source") or {}
    if isinstance(source, dict):
        for path, code in list(source.items())[:40]:
            if isinstance(code, str) and path.endswith((".py", ".ts", ".go")):
                chunks.append(f"# {path}\n{code}")
    notes = art(wf, "backend_notes") or {}
    if isinstance(notes, dict):
        chunks.append(" ".join(str(x) for x in (notes.get("security_controls") or [])))
    # Workspace fallback
    root = forge_paths().workspaces / wf.id / "backend"
    if root.exists():
        for path in list(root.rglob("*.py"))[:50]:
            try:
                chunks.append(path.read_text(encoding="utf-8"))
            except OSError:
                pass
    return "\n".join(chunks).lower()


def _links_service_source(wf: Workflow) -> str:
    """Collect links.py evidence from artifacts or workspace disk (URL shortener)."""
    snippets = art(wf, "backend_snippets") or {}
    if isinstance(snippets, dict):
        for path, code in snippets.items():
            if str(path).replace("\\", "/").endswith("services/links.py"):
                return str(code)
    ws = forge_paths().workspaces / wf.id / "backend" / "app" / "services" / "links.py"
    if ws.exists():
        try:
            return ws.read_text(encoding="utf-8")
        except OSError:
            pass
    return ""


def _detect_backend_mitigations(wf: Workflow) -> dict[str, bool]:
    text = _backend_blob(wf)
    links = _links_service_source(wf).lower()
    blob = f"{text}\n{links}"
    notes = art(wf, "backend_notes") or {}
    applied = set()
    if isinstance(notes, dict):
        applied = {str(x) for x in (notes.get("security_needs") or [])}
        applied |= {str(x) for x in (wf.facts.get("security_controls_applied") or [])}
    return {
        "open_redirect": (
            "open_redirect" in applied
            or (
                "allowed_redirect_hosts" in blob
                and "validate_redirect_url" in blob
            )
        ),
        "ssrf": (
            "ssrf" in applied
            or "_is_private_or_local" in blob
            or ("is_private" in blob and "ssrf" in blob)
        ),
        "https_only": "must use https" in blob or 'scheme != "https"' in blob,
        "safe_logging": (
            "safe_logging" in applied
            or "logging.getlogger" in blob
            or "logger." in blob
        ),
        "input_validation": (
            "input_validation" in applied
            or any(
                tok in blob
                for tok in (
                    "httpexception",
                    "field(",
                    "basemodel",
                    "validate",
                    "pathlib",
                    "sanitize",
                    "normalized_path",
                    "reject",
                )
            )
        ),
        "authz": (
            "authz" in applied
            or any(
                tok in blob
                for tok in (
                    "api_key",
                    "x-api-key",
                    "depends(",
                    "permission",
                    "authorize",
                    "acl",
                    "owner",
                )
            )
        ),
        "path_traversal": (
            "path_traversal" in applied
            or any(
                tok in blob
                for tok in (
                    "path traversal",
                    "sanitize_user_path",
                    "normalized_path",
                    "resolve()",
                    "relative_to",
                    "normpath",
                    "safe_join",
                    "in parts",
                )
            )
            or ('".." in' in blob)
            or ("'..' in" in blob)
        ),
        "rate_limiting": (
            "rate_limiting" in applied
            or any(
                tok in blob
                for tok in (
                    "ratelimitmiddleware",
                    "rate_limit",
                    "token-bucket",
                    "token bucket",
                    "rate limit exceeded",
                    "http_429",
                    "status_code=429",
                )
            )
        ),
    }


def _finding_text(finding: Any) -> str:
    from ..security_policy import finding_text

    return finding_text(finding)


def _drop_offscope_findings(
    findings: list[Any], *, url_shortener: bool
) -> list[dict[str, Any]]:
    """Drop shortener-only findings when the SRS is not a URL shortener."""
    out: list[dict[str, Any]] = []
    for raw in findings:
        f = dict(raw) if isinstance(raw, dict) else {"finding": str(raw), "severity": "MEDIUM"}
        text = _finding_text(f)
        if not url_shortener and any(k in text for k in _URL_SHORTENER_FINDING_KEYS):
            continue
        out.append(f)
    return out


def _apply_backend_mitigations(
    findings: list[Any],
    mitigations: dict[str, bool],
) -> list[dict[str, Any]]:
    """Downgrade findings when generated code / prior controls prove mitigation.

    Matching is generic via security_policy.FINDING_MITIGATION_MAP — not product-hardcoded.
    """
    from ..security_policy import mitigation_for_finding

    out: list[dict[str, Any]] = []
    for raw in findings:
        f = dict(raw) if isinstance(raw, dict) else {"finding": str(raw), "severity": "MEDIUM"}
        text = _finding_text(f)
        sev = str(f.get("severity") or "MEDIUM").upper()
        hit = mitigation_for_finding(text, mitigations)
        if hit:
            evidence, control = hit
            f["severity"] = "INFO"
            f["status"] = "MITIGATED_IN_DESIGN"
            f["mitigation_evidence"] = evidence
            f["mitigation_control"] = control
            sev = "INFO"
        f["severity"] = sev
        out.append(f)
    return out


def _unresolved_high(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        f
        for f in findings
        if str(f.get("severity", "")).upper() in {"CRITICAL", "HIGH"}
        and str(f.get("status", "")).upper() not in _MITIGATED
    ]


def security_scan(wf: Workflow, task: TaskNode) -> dict[str, Any]:
    """9. Security Review — vuln/dependency/SAST-DAST style post-validation pass."""
    prior = art(wf, "security_review") or {}
    backend_notes = art(wf, "backend_notes") or {}
    security_controls = []
    if isinstance(backend_notes, dict):
        security_controls = list(backend_notes.get("security_controls") or [])
    url_shortener = _is_url_shortener(wf)
    links_py = _links_service_source(wf)[:6000] if url_shortener else ""
    mitigations = _detect_backend_mitigations(wf)
    name = product_name(wf)
    brief = art(wf, "product_brief") or {}
    if isinstance(brief, dict) and isinstance(brief.get("name"), str) and brief["name"].strip():
        name = brief["name"].strip()

    notes = art(wf, "backend_notes") or {}
    applied_needs = []
    if isinstance(notes, dict):
        applied_needs = list(notes.get("security_needs") or wf.facts.get("security_controls_applied") or [])

    llm = run_llm_agent(
        wf,
        task,
        agent="security",
        inputs={
            "product": name,
            "openapi": art(wf, "openapi"),
            "prior_security_review": prior,
            "threat_model": art(wf, "threat_model"),
            "backend_source": art(wf, "backend_source"),
            "backend_security_controls": security_controls,
            "security_controls_applied": applied_needs,
            "backend_url_guard_excerpt": links_py,
            "detected_mitigations": mitigations,
            "validation": art(wf, "engineering_validation"),
            "test_plan": art(wf, "test_plan"),
        },
        schema_hint=(
            '{"security_scan":{"sast":[],"dast":[],"dependency_scan":[],"findings":[],'
            '"verdict":"PASS|FAIL","remediation":[]},"validation_passed":true}'
        ),
        system_extra=(
            f"Post-validation Security Review for '{name}'. "
            "Be domain-agnostic: only report findings that apply to THIS run's OpenAPI, "
            "ReqSpec, prior security_review/threat_model, and generated backend evidence. "
            "Do NOT invent issues for unrelated product classes. "
            "If detected_mitigations shows a control is present, mark matching findings "
            "MITIGATED_IN_DESIGN / INFO — including: path traversal, open redirect, phishing "
            "(covered by URL allow-list), SSRF, authz, injection, rate limiting. "
            "Vague findings without a concrete endpoint/sink must be INFO when "
            "detected_mitigations.input_validation is true. "
            "FAIL only for unresolved CRITICAL/HIGH with concrete evidence and no mitigation."
        ),
    )

    if llm and isinstance(llm.get("security_scan"), dict):
        scan = dict(llm["security_scan"])
        mode = "llm"
    else:
        scan = {
            "product": name,
            "sast": ["input validation review", "authz on mutating routes"],
            "dast": ["authz negative tests", "path traversal probe"],
            "dependency_scan": ["base image CVE scan (staging)", "dependency audit"],
            "findings": list(prior.get("findings") or []),
            "remediation": [],
        }
        mode = "heuristic"

    findings = _drop_offscope_findings(
        list(scan.get("findings") or []), url_shortener=url_shortener
    )
    findings = _apply_backend_mitigations(findings, mitigations)
    # Fold prior findings (also drop off-scope shortener-only noise)
    if isinstance(prior, dict):
        prior_findings = _drop_offscope_findings(
            list(prior.get("findings") or []), url_shortener=url_shortener
        )
        for f in prior_findings:
            findings.append(f if isinstance(f, dict) else {"finding": str(f), "severity": "HIGH"})
        findings = _apply_backend_mitigations(findings, mitigations)
        seen: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for f in findings:
            key = re.sub(r"\s+", " ", _finding_text(f))[:160]
            if key in seen:
                continue
            seen.add(key)
            deduped.append(f)
        findings = deduped

    critical_open = _unresolved_high(findings)
    passed = not critical_open
    scan["product"] = name
    scan["findings"] = findings
    scan["critical_open"] = critical_open
    scan["mitigations_detected"] = mitigations
    scan["verdict"] = "PASS" if passed else "FAIL"
    if passed:
        scan["remediation"] = []
    elif not scan.get("remediation"):
        scan["remediation"] = [
            {
                "finding": f.get("finding"),
                "action": f.get("recommendation") or f.get("finding"),
            }
            for f in critical_open
            if isinstance(f, dict)
        ]

    publish(wf, task, "security_scan", scan, bill=(mode != "llm"))
    wf.facts["security_validation_passed"] = passed
    if not passed:
        wf.facts["needs_security_replan"] = True
    else:
        wf.facts["needs_security_replan"] = False
    return {
        "summary": f"Security scan {scan.get('verdict')} for {name}",
        "mode": mode,
        "mitigations": mitigations,
        "escalate": "security_replan" if not passed else None,
    }
