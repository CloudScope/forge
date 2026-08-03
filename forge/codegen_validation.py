"""
Semantic validation of generated engineering output.

Existence checks only prove an agent ran. These checks prove that what it produced
is structurally usable: the API contract resolves, and the emitted Python actually
parses. Everything here is pure-stdlib and side-effect free — no subprocess, no
imports of generated code, no network.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

HTTP_METHODS = frozenset(
    {"get", "post", "put", "patch", "delete", "options", "head", "trace"}
)

# Cap the walk so a runaway generator cannot stall a validation gate.
MAX_SOURCE_FILES = 500


def _ref_targets(node: Any, found: set[str]) -> None:
    """Collect every $ref string in an arbitrarily nested spec fragment."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            found.add(ref)
        for value in node.values():
            _ref_targets(value, found)
    elif isinstance(node, list):
        for item in node:
            _ref_targets(item, found)


def _resolve_local_ref(spec: dict[str, Any], ref: str) -> bool:
    """True when a local `#/a/b/c` pointer resolves inside this document."""
    if not ref.startswith("#/"):
        # External refs are out of scope for an offline gate — treat as unresolvable.
        return False
    cursor: Any = spec
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(cursor, dict) or part not in cursor:
            return False
        cursor = cursor[part]
    return True


def validate_openapi(spec: Any) -> list[str]:
    """
    Structurally validate an OpenAPI 3.x document.

    Returns a list of human-readable errors; empty means the contract is coherent.
    """
    errors: list[str] = []
    if not isinstance(spec, dict):
        return ["OpenAPI document is not a JSON object"]

    version = spec.get("openapi")
    if not isinstance(version, str) or not version.startswith("3."):
        errors.append(f"unsupported or missing 'openapi' version: {version!r}")

    info = spec.get("info")
    if not isinstance(info, dict):
        errors.append("missing 'info' object")
    else:
        for field in ("title", "version"):
            if not str(info.get(field) or "").strip():
                errors.append(f"info.{field} is empty")

    paths = spec.get("paths")
    if not isinstance(paths, dict) or not paths:
        errors.append("document declares no paths")
        return errors

    operation_ids: dict[str, str] = {}
    for path, item in paths.items():
        if not isinstance(path, str) or not path.startswith("/"):
            errors.append(f"path {path!r} must start with '/'")
            continue
        if not isinstance(item, dict):
            errors.append(f"{path}: path item is not an object")
            continue

        declared = {m for m in item if m.lower() in HTTP_METHODS}
        if not declared:
            errors.append(f"{path}: no HTTP operations declared")

        # Every {param} in the template needs a matching parameter definition.
        template_params = set(re.findall(r"\{([^}/]+)\}", path))

        for method in declared:
            op = item[method]
            if not isinstance(op, dict):
                errors.append(f"{method.upper()} {path}: operation is not an object")
                continue

            responses = op.get("responses")
            if not isinstance(responses, dict) or not responses:
                errors.append(f"{method.upper()} {path}: no responses defined")
            else:
                for code in responses:
                    if str(code) != "default" and not re.fullmatch(r"[1-5][0-9Xx]{2}", str(code)):
                        errors.append(
                            f"{method.upper()} {path}: invalid response code {code!r}"
                        )

            op_id = op.get("operationId")
            if isinstance(op_id, str) and op_id:
                previous = operation_ids.get(op_id)
                if previous:
                    errors.append(
                        f"duplicate operationId {op_id!r} on {previous} and "
                        f"{method.upper()} {path}"
                    )
                operation_ids[op_id] = f"{method.upper()} {path}"

            named = {
                str(p.get("name"))
                for p in (list(item.get("parameters") or []) + list(op.get("parameters") or []))
                if isinstance(p, dict) and p.get("in") == "path"
            }
            for missing in template_params - named:
                errors.append(
                    f"{method.upper()} {path}: path parameter '{missing}' is not declared"
                )

    refs: set[str] = set()
    _ref_targets(spec, refs)
    for ref in sorted(refs):
        if not _resolve_local_ref(spec, ref):
            errors.append(f"unresolvable $ref: {ref}")

    return errors


def operations(spec: Any) -> list[str]:
    """Every declared operation as `METHOD /path`, for coverage accounting."""
    out: list[str] = []
    if not isinstance(spec, dict):
        return out
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method in item:
            if method.lower() in HTTP_METHODS:
                out.append(f"{method.upper()} {path}")
    return sorted(out)


def compile_python_sources(root: Path) -> list[str]:
    """
    Parse every generated .py file. Returns syntax errors as `path:line: message`.

    Uses the builtin compiler in 'exec' mode: catches syntax errors without
    importing, executing, or writing bytecode.
    """
    errors: list[str] = []
    if not root.exists():
        return [f"workspace path does not exist: {root}"]

    for path in sorted(root.rglob("*.py"))[:MAX_SOURCE_FILES]:
        if any(part in {"__pycache__", ".venv", "node_modules"} for part in path.parts):
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"{path.name}: unreadable ({exc})")
            continue
        try:
            compile(source, str(path), "exec")
        except SyntaxError as exc:
            rel = path.relative_to(root) if path.is_relative_to(root) else path
            errors.append(f"{rel}:{exc.lineno}: {exc.msg}")
    return errors


def derive_operation_coverage(
    spec: Any, test_plan: Any
) -> dict[str, Any]:
    """
    Measure how many documented API operations have at least one named test case.

    This replaces a self-asserted coverage number with one derived from two
    independent artifacts: the frozen OpenAPI contract and the test plan.
    """
    declared = operations(spec)
    if not declared:
        return {
            "declared_operations": 0,
            "covered_operations": 0,
            "coverage_pct": None,
            "uncovered": [],
            "method": "no OpenAPI operations to measure",
        }

    cases: list[str] = []
    if isinstance(test_plan, dict):
        for value in test_plan.values():
            if isinstance(value, list):
                for entry in value:
                    if isinstance(entry, dict):
                        cases.append(" ".join(str(v) for v in entry.values()))
                    else:
                        cases.append(str(entry))
    blob = " ".join(cases).lower()

    covered: list[str] = []
    uncovered: list[str] = []
    for op in declared:
        _, _, path = op.partition(" ")
        # An operation counts as covered when the plan names its path template
        # (or that path with the parameter placeholders stripped).
        template = path.lower()
        stripped = re.sub(r"\{[^}]+\}", "", template).rstrip("/")
        if template in blob or (len(stripped) > 2 and stripped in blob):
            covered.append(op)
        else:
            uncovered.append(op)

    pct = round(100.0 * len(covered) / len(declared), 1)
    return {
        "declared_operations": len(declared),
        "covered_operations": len(covered),
        "coverage_pct": pct,
        "uncovered": uncovered,
        "method": "OpenAPI operations named by at least one test case",
    }
