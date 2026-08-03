"""Derive security controls from prior SDLC artifacts (any domain / SRS)."""

from __future__ import annotations

from typing import Any


# Control ids emitted into generated backends / notes / scan mitigations.
CTRL_PATH_TRAVERSAL = "path_traversal"
CTRL_OPEN_REDIRECT = "open_redirect"
CTRL_SSRF = "ssrf"
CTRL_AUTHZ = "authz"
CTRL_INPUT_VALIDATION = "input_validation"
CTRL_SAFE_LOGGING = "safe_logging"
CTRL_RATE_LIMITING = "rate_limiting"

_PATH_PARAM_HINTS = (
    "path",
    "filepath",
    "file_path",
    "dir",
    "directory",
    "prefix",
    "key",
    "object_key",
    "resource_path",
    "location",
    "filename",
)

_URL_FIELD_HINTS = (
    "url",
    "uri",
    "href",
    "link",
    "redirect",
    "target",
    "longurl",
    "long_url",
    "callback",
    "webhook",
)

# Prior finding / HLD text → control to emit in codegen.
_FINDING_HINTS: dict[str, tuple[str, ...]] = {
    CTRL_PATH_TRAVERSAL: (
        "path traversal",
        "directory traversal",
        "filepath",
        "unauthorized file",
        "escapes root",
        "../",
    ),
    CTRL_OPEN_REDIRECT: (
        "open redirect",
        "redirect host",
        "allowlist",
        "allow-list",
        "whitelist",
        "phishing",
        "malicious url",
        "malicious site",
    ),
    CTRL_SSRF: (
        "ssrf",
        "server-side request",
        "private ip",
        "metadata",
    ),
    CTRL_AUTHZ: (
        "authz",
        "authorization",
        "permission",
        "access control",
        "idor",
        "broken access",
        "api key",
    ),
    CTRL_INPUT_VALIDATION: (
        "injection",
        "xss",
        "sqli",
        "input validation",
        "sanitize",
    ),
    CTRL_RATE_LIMITING: (
        "rate limit",
        "rate limiting",
        "throttl",
        "abuse",
        "token bucket",
        "ddos",
    ),
}

# Finding text → which detected mitigations close it (generic, not product-specific).
FINDING_MITIGATION_MAP: list[tuple[tuple[str, ...], tuple[str, ...], str]] = [
    (
        ("phishing", "malicious url", "malicious site", "trick users"),
        (CTRL_OPEN_REDIRECT, CTRL_SSRF),
        "URL allow-list / https-only guards block untrusted destinations",
    ),
    (
        (
            "open redirect",
            "domain whitelist",
            "domain allow",
            "allow-list",
            "allowlist",
            "whitelist",
            "redirect host",
        ),
        (CTRL_OPEN_REDIRECT,),
        "ALLOWED_REDIRECT_HOSTS + validate_redirect_url",
    ),
    (
        ("ssrf", "server-side request", "internal request", "private ip"),
        (CTRL_SSRF,),
        "SSRF private/loopback guard",
    ),
    (
        ("rate limit", "rate limiting", "throttl", "token bucket", "abuse"),
        (CTRL_RATE_LIMITING,),
        "token-bucket / rate-limit middleware on API routes",
    ),
    (
        ("key leakage", "logging", "sensitive action", "sensitive data", "audit"),
        (CTRL_SAFE_LOGGING,),
        "structured logging without secrets",
    ),
    (
        ("injection", "xss", "sqli", "sanitize", "input validation", "user input"),
        (CTRL_INPUT_VALIDATION, CTRL_OPEN_REDIRECT),
        "request validation / schema guards in generated API",
    ),
    (
        ("authz", "authorization", "permission", "access control", "idor", "api key"),
        (CTRL_AUTHZ,),
        "API key / permission checks in generated API",
    ),
    (
        (
            "path traversal",
            "directory traversal",
            "filepath",
            "file operations",
            "unauthorized file",
            "escapes root",
            "resource root",
            "../",
        ),
        (CTRL_PATH_TRAVERSAL,),
        "sanitize_user_path / normalized_path + resolve()/relative_to()/safe_join",
    ),
]


def _finding_blob(findings: list[Any]) -> str:
    parts: list[str] = []
    for raw in findings or []:
        if isinstance(raw, dict):
            parts.extend(
                str(raw.get(k) or "")
                for k in (
                    "finding",
                    "threat",
                    "description",
                    "recommendation",
                    "title",
                    "id",
                    "endpoint",
                )
            )
        else:
            parts.append(str(raw))
    return " ".join(parts).lower()


def finding_text(finding: Any) -> str:
    if isinstance(finding, dict):
        return " ".join(
            str(finding.get(k) or "")
            for k in ("finding", "threat", "description", "recommendation", "title")
        ).lower()
    return str(finding).lower()


def mitigation_for_finding(
    text: str, mitigations: dict[str, bool]
) -> tuple[str, str] | None:
    """If prior findings match a control that codegen emitted, return (evidence, control)."""
    for keywords, controls, evidence in FINDING_MITIGATION_MAP:
        if any(k in text for k in keywords) and any(mitigations.get(c) for c in controls):
            return evidence, next(c for c in controls if mitigations.get(c))
    return None


def _openapi_signals(openapi: dict[str, Any] | None) -> dict[str, bool]:
    import re

    spec = openapi or {}
    path_params = False
    url_fields = False
    has_security = False
    redirectish = False
    rate_limit_hint = False
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        pl = str(path).lower()
        if "redirect" in pl:
            redirectish = True
        if "rate" in pl or "throttle" in pl:
            rate_limit_hint = True
        for var in re.findall(r"\{([^}]+)\}", pl):
            if any(h in var for h in _PATH_PARAM_HINTS):
                path_params = True
            if any(h in var for h in _URL_FIELD_HINTS):
                url_fields = True
        for method, op in item.items():
            if method.startswith("x-") or method not in {
                "get",
                "post",
                "put",
                "patch",
                "delete",
                "options",
                "head",
            }:
                continue
            if not isinstance(op, dict):
                continue
            if op.get("security") is not None or spec.get("security"):
                has_security = True
            params = list(item.get("parameters") or []) + list(op.get("parameters") or [])
            for p in params:
                if not isinstance(p, dict):
                    continue
                name = str(p.get("name") or "").lower()
                if any(h in name for h in _PATH_PARAM_HINTS):
                    path_params = True
                if any(h in name for h in _URL_FIELD_HINTS):
                    url_fields = True
            body = op.get("requestBody") or {}
            content = body.get("content") if isinstance(body, dict) else {}
            for media in (content or {}).values():
                schema = (media or {}).get("schema") or {}
                props = schema.get("properties") if isinstance(schema, dict) else {}
                for pname, pdef in (props or {}).items():
                    n = str(pname).lower()
                    if any(h in n for h in _URL_FIELD_HINTS):
                        url_fields = True
                    if any(h in n for h in _PATH_PARAM_HINTS):
                        path_params = True
                    if isinstance(pdef, dict) and str(pdef.get("format") or "") in {
                        "uri",
                        "url",
                    }:
                        url_fields = True
            ext = " ".join(str(v) for v in op.values() if isinstance(v, str)).lower()
            if "rate" in ext or "throttl" in ext:
                rate_limit_hint = True
    schemes = ((spec.get("components") or {}).get("securitySchemes")) or {}
    if schemes:
        has_security = True
    return {
        "path_params": path_params,
        "url_fields": url_fields,
        "has_security": has_security,
        "redirectish": redirectish,
        "rate_limit_hint": rate_limit_hint,
    }


def collect_security_findings(artifacts: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten findings from security_review / threat_model / review_report."""
    out: list[dict[str, Any]] = []
    for key in ("security_review", "threat_model", "review_report"):
        raw = artifacts.get(key)
        if not isinstance(raw, dict):
            continue
        for f in raw.get("findings") or []:
            if isinstance(f, dict):
                out.append(f)
            else:
                out.append({"finding": str(f), "severity": "MEDIUM"})
        for f in raw.get("threats") or []:
            if isinstance(f, dict):
                out.append(f)
            else:
                out.append({"threat": str(f), "severity": "MEDIUM"})
    return out


def derive_security_needs(
    *,
    openapi: dict[str, Any] | None = None,
    findings: list[Any] | None = None,
    hld: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return which controls codegen must emit, based on prior steps only.

    Not product-specific: driven by OpenAPI shape + security findings + HLD hints.
    """
    signals = _openapi_signals(openapi)
    blob = _finding_blob(list(findings or []))
    hld_blob = " ".join(
        str(x)
        for x in (
            (hld or {}).get("tenets") or [],
            (hld or {}).get("components") or [],
            (hld or {}).get("security") or [],
            ((hld or {}).get("authn_z") or {}),
            ((hld or {}).get("context") or {}),
        )
    ).lower()
    combined = f"{blob} {hld_blob}"

    needed: set[str] = {CTRL_INPUT_VALIDATION, CTRL_SAFE_LOGGING}
    reasons: dict[str, list[str]] = {
        CTRL_INPUT_VALIDATION: ["default API request validation"],
        CTRL_SAFE_LOGGING: ["default structured audit logging"],
    }

    def _need(ctrl: str, reason: str) -> None:
        needed.add(ctrl)
        reasons.setdefault(ctrl, []).append(reason)

    if signals["path_params"]:
        _need(CTRL_PATH_TRAVERSAL, "OpenAPI exposes path-like parameters")
    if signals["url_fields"] or signals["redirectish"]:
        _need(CTRL_OPEN_REDIRECT, "OpenAPI exposes URL/redirect fields")
        _need(CTRL_SSRF, "OpenAPI URL fields can trigger server-side fetch/redirect")
    if signals["has_security"]:
        _need(CTRL_AUTHZ, "OpenAPI declares security schemes")
    if signals.get("rate_limit_hint"):
        _need(CTRL_RATE_LIMITING, "OpenAPI/HLD hints rate limiting")

    for ctrl, hints in _FINDING_HINTS.items():
        if any(h in combined for h in hints):
            _need(ctrl, f"prior security artifact mentions: {ctrl}")

    labels = {
        CTRL_PATH_TRAVERSAL: (
            "path traversal guard: reject '..', normalize, resolve()+relative_to()/safe_join"
        ),
        CTRL_OPEN_REDIRECT: (
            "open-redirect / phishing guard: https-only + host allow-list for URL fields"
        ),
        CTRL_SSRF: "SSRF guard: reject private/loopback/link-local resolved hosts",
        CTRL_AUTHZ: "authz hooks on mutating routes (API key / permission checks)",
        CTRL_INPUT_VALIDATION: "request validation via Pydantic / explicit reject paths",
        CTRL_SAFE_LOGGING: "structured logging without sensitive payload dumps",
        CTRL_RATE_LIMITING: "rate limiting: token-bucket middleware on mutating/API routes",
    }
    controls = [labels[c] for c in sorted(needed) if c in labels]

    return {
        "needed": sorted(needed),
        "reasons": reasons,
        "signals": signals,
        "controls": controls,
    }


def path_like_param_names(openapi: dict[str, Any] | None) -> set[str]:
    """Parameter names that should be passed through path sanitization."""
    names: set[str] = set()
    for item in ((openapi or {}).get("paths") or {}).values():
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if method.startswith("x-") or not isinstance(op, dict):
                continue
            params = list(item.get("parameters") or []) + list(op.get("parameters") or [])
            for p in params:
                if not isinstance(p, dict):
                    continue
                name = str(p.get("name") or "")
                if any(h in name.lower() for h in _PATH_PARAM_HINTS):
                    names.add(name)
    return names
