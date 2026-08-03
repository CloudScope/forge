from __future__ import annotations

from typing import Any

from ..models import TaskNode, Workflow
from ._common import art, publish
from .doc_context import has_feature, product_name
from .llm_bridge import run_llm_agent

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "options", "head")


def _resp(code: str, description: str, schema_ref: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"description": description}
    if schema_ref:
        body["content"] = {
            "application/json": {"schema": {"$ref": f"#/components/schemas/{schema_ref}"}}
        }
    return body


def _op(
    *,
    operation_id: str,
    summary: str,
    description: str = "",
    tags: list[str] | None = None,
    parameters: list[dict[str, Any]] | None = None,
    request_body: dict[str, Any] | None = None,
    responses: dict[str, Any] | None = None,
    security: list[dict[str, list[str]]] | None = None,
    idempotent: bool = False,
) -> dict[str, Any]:
    op: dict[str, Any] = {
        "operationId": operation_id,
        "summary": summary,
        "description": description or summary,
        "tags": tags or ["default"],
        "responses": responses or {"200": _resp("200", "OK")},
    }
    if parameters:
        op["parameters"] = parameters
    if request_body:
        op["requestBody"] = request_body
    if security is not None:
        op["security"] = security
    if idempotent:
        op.setdefault("parameters", [])
        op["parameters"].append(
            {
                "name": "Idempotency-Key",
                "in": "header",
                "required": False,
                "schema": {"type": "string", "format": "uuid"},
                "description": "Client-supplied key for safe retries on mutating POSTs",
            }
        )
    return op


def _param(
    name: str,
    *,
    location: str = "path",
    typ: str = "string",
    required: bool = True,
    description: str = "",
) -> dict[str, Any]:
    return {
        "name": name,
        "in": location,
        "required": required,
        "schema": {"type": typ},
        "description": description or name,
    }


def _normalize_openapi(spec: dict[str, Any], product: str) -> dict[str, Any]:
    """
    Coerce LLM / legacy pseudo-specs into OpenAPI 3.x the studio + codegen understand.

    Accepts broken shapes like paths keyed by \"POST /v1/links\" with {operation, responses}.
    """
    if not isinstance(spec, dict):
        return {}

    # Unwrap accidental nesting: {"openapi": {...real...}}
    inner = spec.get("openapi")
    if isinstance(inner, dict) and ("paths" in inner or "info" in inner):
        spec = inner

    paths_in = spec.get("paths") or {}
    paths_out: dict[str, Any] = {}

    for raw_key, item in paths_in.items():
        key = str(raw_key).strip()
        if not key:
            continue

        # Already valid: "/v1/links" -> {get/post/...}
        if key.startswith("/") and isinstance(item, dict):
            methods_present = [m for m in HTTP_METHODS if isinstance(item.get(m), dict)]
            if methods_present or any(k.startswith("x-") for k in item):
                path_item = dict(item)
                for m in methods_present:
                    op = dict(path_item[m])
                    op.setdefault("operationId", f"{m}_{key.strip('/').replace('/', '_') or 'root'}")
                    op.setdefault("summary", op["operationId"])
                    op.setdefault("tags", ["default"])
                    # Ensure responses are objects with description
                    responses = op.get("responses") or {"200": {"description": "OK"}}
                    fixed_resp: dict[str, Any] = {}
                    for code, resp in responses.items():
                        if isinstance(resp, str):
                            fixed_resp[str(code)] = {"description": resp}
                        elif isinstance(resp, dict):
                            fixed_resp[str(code)] = resp
                        else:
                            fixed_resp[str(code)] = {"description": "OK"}
                    op["responses"] = fixed_resp
                    path_item[m] = op
                paths_out.setdefault(key, {}).update(path_item)
                continue

        # Legacy: "POST /v1/links" or "GET /{code}"
        method = "get"
        path = key
        parts = key.split(None, 1)
        if len(parts) == 2 and parts[0].lower() in HTTP_METHODS:
            method, path = parts[0].lower(), parts[1]
        if not path.startswith("/"):
            path = "/" + path

        meta = item if isinstance(item, dict) else {}
        op_id = str(meta.get("operation") or meta.get("operationId") or f"{method}_{path.strip('/').replace('/', '_')}")
        summary = str(meta.get("summary") or op_id.replace("_", " "))
        responses_raw = meta.get("responses") or {"200": "OK"}
        responses: dict[str, Any] = {}
        if isinstance(responses_raw, dict):
            for code, resp in responses_raw.items():
                if isinstance(resp, str):
                    responses[str(code)] = {"description": resp}
                elif isinstance(resp, dict):
                    responses[str(code)] = resp
                else:
                    responses[str(code)] = {"description": "OK"}
        else:
            responses = {"200": {"description": "OK"}}

        parameters: list[dict[str, Any]] = []
        for part in path.split("/"):
            if part.startswith("{") and part.endswith("}"):
                parameters.append(_param(part[1:-1], location="path"))

        security = None
        auth = meta.get("auth")
        if auth:
            security = [{"ApiKeyAuth": []}]

        op = _op(
            operation_id=op_id,
            summary=summary,
            parameters=parameters or None,
            responses=responses,
            security=security,
            idempotent=bool(meta.get("idempotency")) or method == "post",
            tags=["legacy-normalized"],
        )
        paths_out.setdefault(path, {})[method] = op

    components = spec.get("components") if isinstance(spec.get("components"), dict) else {}
    schemas_in = components.get("schemas") if isinstance(components.get("schemas"), dict) else {}
    schemas_out: dict[str, Any] = {}
    for name, schema in schemas_in.items():
        if isinstance(schema, list):
            props = {str(f): {"type": "string"} for f in schema}
            schemas_out[str(name)] = {
                "type": "object",
                "properties": props,
                "required": [str(schema[0])] if schema else [],
            }
        elif isinstance(schema, dict):
            schemas_out[str(name)] = schema
        else:
            schemas_out[str(name)] = {"type": "object"}

    # Defaults if empty after normalization
    if not schemas_out:
        schemas_out = {
            "Link": {
                "type": "object",
                "required": ["id", "code", "short_url", "target_url", "status"],
                "properties": {
                    "id": {"type": "string", "format": "uuid"},
                    "code": {"type": "string"},
                    "short_url": {"type": "string", "format": "uri"},
                    "target_url": {"type": "string", "format": "uri"},
                    "status": {"type": "string", "enum": ["active", "disabled", "expired"]},
                    "expires_at": {"type": "string", "format": "date-time", "nullable": True},
                },
            },
            "Error": {
                "type": "object",
                "required": ["code", "message", "request_id"],
                "properties": {
                    "code": {"type": "string"},
                    "message": {"type": "string"},
                    "details": {"type": "object", "additionalProperties": True},
                    "request_id": {"type": "string"},
                },
            },
        }

    security_schemes = components.get("securitySchemes") if isinstance(components, dict) else None
    if not isinstance(security_schemes, dict) or not security_schemes:
        security_schemes = {
            "ApiKeyAuth": {"type": "apiKey", "in": "header", "name": "X-API-Key"}
        }

    info = spec.get("info") if isinstance(spec.get("info"), dict) else {}
    info.setdefault("title", f"{product} API")
    info.setdefault("version", "1.0.0")
    info.setdefault(
        "description",
        "Production URL shortener API — create, redirect, analytics, admin, and health.",
    )

    servers = spec.get("servers")
    if not isinstance(servers, list) or not servers:
        slug = "".join(ch if ch.isalnum() else "-" for ch in product.lower()).strip("-") or "snipr"
        servers = [{"url": "https://api.example.com", "description": "Production"}]

    out: dict[str, Any] = {
        "openapi": "3.0.3",
        "info": info,
        "servers": servers,
        "paths": paths_out,
        "components": {
            "schemas": schemas_out,
            "securitySchemes": security_schemes,
        },
    }
    if isinstance(spec.get("tags"), list):
        out["tags"] = spec["tags"]
    if isinstance(spec.get("conventions"), dict):
        out["x-forge-conventions"] = spec["conventions"]
    return out


def _build_snipr_openapi(wf: Workflow) -> dict[str, Any]:
    name = product_name(wf)
    slug = "".join(ch if ch.isalnum() else "-" for ch in name.lower()).strip("-") or "snipr"

    link_create_body = {
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "required": ["target_url"],
                    "properties": {
                        "target_url": {"type": "string", "format": "uri"},
                        "custom_alias": {"type": "string", "minLength": 3, "maxLength": 64},
                        "expires_at": {"type": "string", "format": "date-time", "nullable": True},
                    },
                }
            }
        },
    }

    paths: dict[str, Any] = {
        "/v1/links": {
            "post": _op(
                operation_id="createLink",
                summary="Create short link",
                description="Create a short URL. Supports Idempotency-Key.",
                tags=["links"],
                request_body=link_create_body,
                responses={
                    "201": _resp("201", "Created", "Link"),
                    "400": _resp("400", "Validation error", "Error"),
                    "401": _resp("401", "Unauthorized", "Error"),
                    "409": _resp("409", "Alias conflict", "Error"),
                },
                security=[{"ApiKeyAuth": []}],
                idempotent=True,
            )
        },
        "/{code}": {
            "get": _op(
                operation_id="redirect",
                summary="Redirect short code",
                description="302 redirect to the target URL.",
                tags=["redirect"],
                parameters=[_param("code", description="Short code")],
                responses={
                    "302": {"description": "Redirect", "headers": {
                        "Location": {"schema": {"type": "string", "format": "uri"}}
                    }},
                    "404": _resp("404", "Not found", "Error"),
                    "410": _resp("410", "Gone / expired", "Error"),
                },
                security=[],
            )
        },
        "/v1/links/{id}": {
            "get": _op(
                operation_id="getLink",
                summary="Get link metadata",
                tags=["links"],
                parameters=[_param("id", description="Link id")],
                responses={
                    "200": _resp("200", "OK", "Link"),
                    "401": _resp("401", "Unauthorized", "Error"),
                    "404": _resp("404", "Not found", "Error"),
                },
                security=[{"ApiKeyAuth": []}],
            ),
            "delete": _op(
                operation_id="deleteLink",
                summary="Delete / disable link",
                tags=["links"],
                parameters=[_param("id")],
                responses={
                    "204": {"description": "Deleted"},
                    "401": _resp("401", "Unauthorized", "Error"),
                    "404": _resp("404", "Not found", "Error"),
                },
                security=[{"ApiKeyAuth": []}],
            ),
        },
        "/healthz": {
            "get": _op(
                operation_id="healthz",
                summary="Liveness probe",
                tags=["ops"],
                responses={"200": {"description": "Alive", "content": {
                    "application/json": {"schema": {"type": "object", "properties": {"status": {"type": "string"}}}}
                }}},
                security=[],
            )
        },
        "/readyz": {
            "get": _op(
                operation_id="readyz",
                summary="Readiness probe",
                tags=["ops"],
                responses={
                    "200": {"description": "Ready"},
                    "503": _resp("503", "Not ready", "Error"),
                },
                security=[],
            )
        },
        "/metrics": {
            "get": _op(
                operation_id="metrics",
                summary="Prometheus metrics",
                tags=["ops"],
                responses={"200": {"description": "text/plain Prometheus exposition"}},
                security=[],
            )
        },
    }

    if has_feature(wf, "bulk"):
        paths["/v1/links/bulk"] = {
            "post": _op(
                operation_id="bulkCreateLinks",
                summary="Bulk create short links",
                tags=["links"],
                request_body={
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["items"],
                                "properties": {
                                    "items": {
                                        "type": "array",
                                        "maxItems": 1000,
                                        "items": {
                                            "type": "object",
                                            "required": ["target_url"],
                                            "properties": {
                                                "target_url": {"type": "string", "format": "uri"},
                                                "custom_alias": {"type": "string"},
                                            },
                                        },
                                    }
                                },
                            }
                        }
                    },
                },
                responses={
                    "207": _resp("207", "Multi-status bulk result", "BulkResult"),
                    "400": _resp("400", "Validation error", "Error"),
                },
                security=[{"ApiKeyAuth": []}],
                idempotent=True,
            )
        }

    if has_feature(wf, "analytics"):
        paths["/v1/links/{id}/analytics"] = {
            "get": _op(
                operation_id="getLinkAnalytics",
                summary="Link click analytics",
                tags=["analytics"],
                parameters=[
                    _param("id"),
                    _param("from", location="query", required=False, description="ISO start"),
                    _param("to", location="query", required=False, description="ISO end"),
                ],
                responses={
                    "200": _resp("200", "OK", "Analytics"),
                    "401": _resp("401", "Unauthorized", "Error"),
                    "404": _resp("404", "Not found", "Error"),
                },
                security=[{"ApiKeyAuth": []}],
            )
        }

    if has_feature(wf, "preview"):
        paths["/v1/links/{id}/preview"] = {
            "get": _op(
                operation_id="previewLink",
                summary="Safe destination preview",
                tags=["links"],
                parameters=[_param("id")],
                responses={
                    "200": _resp("200", "OK", "Preview"),
                    "401": _resp("401", "Unauthorized", "Error"),
                    "404": _resp("404", "Not found", "Error"),
                },
                security=[{"ApiKeyAuth": []}],
            )
        }

    if has_feature(wf, "qr_code") or wf.facts.get("feature_qr"):
        wf.facts["feature_qr"] = True
        paths["/v1/links/{id}/qr"] = {
            "get": _op(
                operation_id="getLinkQr",
                summary="QR code PNG for short link",
                tags=["links"],
                parameters=[_param("id")],
                responses={
                    "200": {
                        "description": "PNG image",
                        "content": {"image/png": {"schema": {"type": "string", "format": "binary"}}},
                    },
                    "401": _resp("401", "Unauthorized", "Error"),
                    "404": _resp("404", "Not found", "Error"),
                },
                security=[{"ApiKeyAuth": []}],
            )
        }

    if has_feature(wf, "admin", "auth"):
        paths["/v1/links/{id}/disable"] = {
            "post": _op(
                operation_id="disableLink",
                summary="Admin disable link",
                tags=["admin"],
                parameters=[_param("id")],
                responses={
                    "200": _resp("200", "Disabled", "Link"),
                    "401": _resp("401", "Unauthorized", "Error"),
                    "403": _resp("403", "Forbidden", "Error"),
                    "404": _resp("404", "Not found", "Error"),
                },
                security=[{"ApiKeyAuth": []}],
            )
        }
        paths["/v1/keys"] = {
            "post": _op(
                operation_id="createApiKey",
                summary="Create API key",
                tags=["admin"],
                request_body={
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["name"],
                                "properties": {
                                    "name": {"type": "string"},
                                    "scopes": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                },
                            }
                        }
                    },
                },
                responses={
                    "201": _resp("201", "Created", "ApiKey"),
                    "401": _resp("401", "Unauthorized", "Error"),
                    "403": _resp("403", "Forbidden", "Error"),
                },
                security=[{"ApiKeyAuth": []}],
                idempotent=True,
            )
        }

    return {
        "openapi": "3.0.3",
        "info": {
            "title": f"{name} API",
            "version": "1.0.0",
            "description": (
                f"Production-grade short-link API for {name}. "
                "Versioned under /v1, API-key auth for management APIs, public redirect on /{code}."
            ),
        },
        "servers": [
            {"url": f"https://api.{slug}.example", "description": "Production"},
            {"url": "http://127.0.0.1:8080", "description": "Local"},
        ],
        "tags": [
            {"name": "links", "description": "Short link lifecycle"},
            {"name": "redirect", "description": "Public redirect"},
            {"name": "analytics", "description": "Click analytics"},
            {"name": "admin", "description": "Admin operations"},
            {"name": "ops", "description": "Health and metrics"},
        ],
        "paths": paths,
        "components": {
            "securitySchemes": {
                "ApiKeyAuth": {"type": "apiKey", "in": "header", "name": "X-API-Key"}
            },
            "schemas": {
                "Link": {
                    "type": "object",
                    "required": ["id", "code", "short_url", "target_url", "status"],
                    "properties": {
                        "id": {"type": "string", "format": "uuid"},
                        "code": {"type": "string"},
                        "short_url": {"type": "string", "format": "uri"},
                        "target_url": {"type": "string", "format": "uri"},
                        "status": {
                            "type": "string",
                            "enum": ["active", "disabled", "expired"],
                        },
                        "expires_at": {
                            "type": "string",
                            "format": "date-time",
                            "nullable": True,
                        },
                        "created_at": {"type": "string", "format": "date-time"},
                    },
                },
                "Error": {
                    "type": "object",
                    "required": ["code", "message", "request_id"],
                    "properties": {
                        "code": {"type": "string"},
                        "message": {"type": "string"},
                        "details": {"type": "object", "additionalProperties": True},
                        "request_id": {"type": "string"},
                    },
                },
                "BulkResult": {
                    "type": "object",
                    "properties": {
                        "succeeded": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/Link"},
                        },
                        "failed": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "index": {"type": "integer"},
                                    "error": {"$ref": "#/components/schemas/Error"},
                                },
                            },
                        },
                    },
                },
                "Analytics": {
                    "type": "object",
                    "properties": {
                        "link_id": {"type": "string"},
                        "clicks": {"type": "integer"},
                        "unique_visitors": {"type": "integer"},
                        "series": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "ts": {"type": "string", "format": "date-time"},
                                    "clicks": {"type": "integer"},
                                },
                            },
                        },
                    },
                },
                "Preview": {
                    "type": "object",
                    "properties": {
                        "target_url": {"type": "string", "format": "uri"},
                        "safe": {"type": "boolean"},
                        "title": {"type": "string"},
                    },
                },
                "ApiKey": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "name": {"type": "string"},
                        "prefix": {"type": "string"},
                        "secret": {"type": "string", "description": "Returned once at creation"},
                        "scopes": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
        },
        "x-forge-conventions": {
            "versioning": "URI prefix /v1",
            "pagination": "cursor-based",
            "idempotency": "Idempotency-Key on mutating POSTs",
            "errors": "RFC7807-inspired Error model",
        },
    }


def _domain_blob(wf: Workflow) -> str:
    brief = art(wf, "product_brief") or {}
    req = art(wf, "reqspec") or {}
    ents = (req.get("domain") or {}).get("entities") or []
    return " ".join(
        [
            product_name(wf),
            str(brief.get("name") or ""),
            " ".join(str(e) for e in ents),
            " ".join(str(f) for f in (req.get("features_detected") or [])[:20]),
        ]
    ).lower()


def _is_url_shortener_domain(wf: Workflow) -> bool:
    blob = _domain_blob(wf)
    hits = sum(
        1
        for tok in ("short url", "shorten", "tinyurl", "shortlink", "short_url", "snipr")
        if tok in blob
    )
    return hits >= 2


def _is_file_system_domain(wf: Workflow) -> bool:
    blob = _domain_blob(wf)
    return any(
        tok in blob
        for tok in (
            "file system",
            "filesystem",
            "directory",
            "directories",
            "inode",
            "object-oriented file",
        )
    )


def _build_filesystem_openapi(wf: Workflow) -> dict[str, Any]:
    """OpenAPI fallback for Object-Oriented File System style PRDs."""
    brief = art(wf, "product_brief") or {}
    name = (
        brief.get("name")
        if isinstance(brief.get("name"), str) and brief["name"].strip()
        else product_name(wf)
    )
    slug = "".join(ch if ch.isalnum() else "-" for ch in str(name).lower()).strip("-") or "fs"
    err = {
        "401": _resp("401", "Unauthorized", "Error"),
        "403": _resp("403", "Forbidden", "Error"),
        "404": _resp("404", "Not found", "Error"),
    }
    paths: dict[str, Any] = {
        "/healthz": {
            "get": _op(operation_id="healthz", summary="Liveness", tags=["ops"], security=[])
        },
        "/readyz": {
            "get": _op(operation_id="readyz", summary="Readiness", tags=["ops"], security=[])
        },
        "/v1/directories": {
            "get": _op(
                operation_id="listDirectories",
                summary="List directories",
                tags=["directories"],
                parameters=[
                    _param("path", location="query", required=False, description="Parent path"),
                ],
                responses={"200": _resp("200", "OK", "DirectoryList"), **err},
                security=[{"ApiKeyAuth": []}],
            ),
            "post": _op(
                operation_id="createDirectory",
                summary="Create directory",
                tags=["directories"],
                request_body={
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["name", "parent_path"],
                                "properties": {
                                    "name": {"type": "string"},
                                    "parent_path": {"type": "string", "default": "/"},
                                },
                            }
                        }
                    },
                },
                responses={"201": _resp("201", "Created", "Directory"), **err},
                security=[{"ApiKeyAuth": []}],
                idempotent=True,
            ),
        },
        "/v1/directories/{id}": {
            "get": _op(
                operation_id="getDirectory",
                summary="Get directory",
                tags=["directories"],
                parameters=[_param("id")],
                responses={"200": _resp("200", "OK", "Directory"), **err},
                security=[{"ApiKeyAuth": []}],
            ),
            "delete": _op(
                operation_id="deleteDirectory",
                summary="Delete directory",
                tags=["directories"],
                parameters=[_param("id")],
                responses={"204": _resp("204", "Deleted"), **err},
                security=[{"ApiKeyAuth": []}],
            ),
        },
        "/v1/files": {
            "get": _op(
                operation_id="listFiles",
                summary="List files",
                tags=["files"],
                parameters=[
                    _param("path", location="query", required=False, description="Directory path"),
                ],
                responses={"200": _resp("200", "OK", "FileList"), **err},
                security=[{"ApiKeyAuth": []}],
            ),
            "post": _op(
                operation_id="createFile",
                summary="Create file",
                tags=["files"],
                request_body={
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["name", "parent_path"],
                                "properties": {
                                    "name": {"type": "string"},
                                    "parent_path": {"type": "string", "default": "/"},
                                    "content": {"type": "string"},
                                },
                            }
                        }
                    },
                },
                responses={"201": _resp("201", "Created", "File"), **err},
                security=[{"ApiKeyAuth": []}],
                idempotent=True,
            ),
        },
        "/v1/files/{id}": {
            "get": _op(
                operation_id="getFile",
                summary="Read file metadata/content",
                tags=["files"],
                parameters=[_param("id")],
                responses={"200": _resp("200", "OK", "File"), **err},
                security=[{"ApiKeyAuth": []}],
            ),
            "put": _op(
                operation_id="updateFile",
                summary="Write/update file",
                tags=["files"],
                parameters=[_param("id")],
                request_body={
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "content": {"type": "string"},
                                    "name": {"type": "string"},
                                },
                            }
                        }
                    },
                },
                responses={"200": _resp("200", "OK", "File"), **err},
                security=[{"ApiKeyAuth": []}],
            ),
            "delete": _op(
                operation_id="deleteFile",
                summary="Delete file",
                tags=["files"],
                parameters=[_param("id")],
                responses={"204": _resp("204", "Deleted"), **err},
                security=[{"ApiKeyAuth": []}],
            ),
        },
        "/v1/search": {
            "get": _op(
                operation_id="searchNodes",
                summary="Search files and directories",
                tags=["search"],
                parameters=[
                    _param("q", location="query", required=True, description="Name/ext/path query"),
                    _param("recursive", location="query", typ="boolean", required=False),
                ],
                responses={"200": _resp("200", "OK", "SearchResult"), **err},
                security=[{"ApiKeyAuth": []}],
            )
        },
        "/v1/permissions/{path}": {
            "get": _op(
                operation_id="getPermissions",
                summary="Get permissions for path",
                tags=["permissions"],
                parameters=[_param("path", description="URL-encoded filesystem path")],
                responses={"200": _resp("200", "OK", "Permission"), **err},
                security=[{"ApiKeyAuth": []}],
            ),
            "put": _op(
                operation_id="setPermissions",
                summary="Set permissions for path",
                tags=["permissions"],
                parameters=[_param("path")],
                request_body={
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "owner": {"type": "string"},
                                    "mode": {"type": "string"},
                                    "acl": {
                                        "type": "array",
                                        "items": {"type": "object"},
                                    },
                                },
                            }
                        }
                    },
                },
                responses={"200": _resp("200", "OK", "Permission"), **err},
                security=[{"ApiKeyAuth": []}],
            ),
        },
    }
    return {
        "openapi": "3.0.3",
        "info": {
            "title": f"{name} API",
            "version": "1.0.0",
            "description": f"File/directory operations API for {name}.",
        },
        "servers": [
            {"url": f"https://api.{slug}.example", "description": "Production"},
            {"url": "http://127.0.0.1:8080", "description": "Local"},
        ],
        "tags": [
            {"name": "directories", "description": "Directory tree"},
            {"name": "files", "description": "File CRUD"},
            {"name": "search", "description": "Search"},
            {"name": "permissions", "description": "ACL / mode"},
            {"name": "ops", "description": "Health"},
        ],
        "paths": paths,
        "components": {
            "securitySchemes": {
                "ApiKeyAuth": {"type": "apiKey", "in": "header", "name": "X-API-Key"}
            },
            "schemas": {
                "Directory": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "name": {"type": "string"},
                        "path": {"type": "string"},
                        "parent_path": {"type": "string"},
                        "created_at": {"type": "string", "format": "date-time"},
                    },
                },
                "DirectoryList": {
                    "type": "object",
                    "properties": {
                        "items": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/Directory"},
                        }
                    },
                },
                "File": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "name": {"type": "string"},
                        "path": {"type": "string"},
                        "size": {"type": "integer"},
                        "content": {"type": "string"},
                        "created_at": {"type": "string", "format": "date-time"},
                    },
                },
                "FileList": {
                    "type": "object",
                    "properties": {
                        "items": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/File"},
                        }
                    },
                },
                "SearchResult": {
                    "type": "object",
                    "properties": {
                        "items": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "type": {"type": "string", "enum": ["file", "directory"]},
                                    "path": {"type": "string"},
                                    "name": {"type": "string"},
                                },
                            },
                        }
                    },
                },
                "Permission": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "owner": {"type": "string"},
                        "mode": {"type": "string"},
                    },
                },
                "Error": {
                    "type": "object",
                    "required": ["code", "message", "request_id"],
                    "properties": {
                        "code": {"type": "string"},
                        "message": {"type": "string"},
                        "request_id": {"type": "string"},
                    },
                },
            },
        },
    }


def _build_fallback_openapi(wf: Workflow) -> dict[str, Any]:
    """Pick domain-appropriate OpenAPI when LLM is unavailable or empty."""
    if _is_file_system_domain(wf) and not _is_url_shortener_domain(wf):
        return _build_filesystem_openapi(wf)
    return _build_snipr_openapi(wf)


def _openapi_matches_domain(wf: Workflow, openapi: dict[str, Any]) -> bool:
    """Reject TinyURL-shaped OpenAPI when the PRD is a file system (etc.)."""
    paths = list((openapi or {}).get("paths") or {})
    if not paths:
        return False
    if _is_file_system_domain(wf) and not _is_url_shortener_domain(wf):
        linkish = sum(1 for p in paths if "/links" in p or p in {"/{code}", "/v1/links"})
        fsish = sum(
            1
            for p in paths
            if any(x in p for x in ("/files", "/directories", "/search", "/permissions"))
        )
        if linkish and not fsish:
            return False
    return True


def api_design(wf: Workflow, task: TaskNode) -> dict[str, Any]:
    """6. API Design Agent — real OpenAPI 3.0.3 for studio + codegen."""
    name = product_name(wf)
    brief = art(wf, "product_brief") or {}
    if isinstance(brief, dict) and isinstance(brief.get("name"), str) and brief["name"].strip():
        name = brief["name"].strip()
    domain_hint = (
        "file-system directories/files/search/permissions"
        if _is_file_system_domain(wf)
        else "product domain from ReqSpec — NOT a URL shortener unless the PRD is"
    )
    llm = run_llm_agent(
        wf,
        task,
        agent="api",
        inputs={
            "reqspec": art(wf, "reqspec"),
            "hld": art(wf, "hld"),
            "schema_ddl": art(wf, "schema_ddl"),
            "product_brief": brief,
            "domain_hint": domain_hint,
        },
        schema_hint=(
            '{"openapi":"3.0.3","info":{"title":"","version":"1.0.0"},'
            '"paths":{"/v1/...":{"get":{"operationId":"…","responses":{"200":{"description":"OK"}}}}},'
            '"components":{"schemas":{},"securitySchemes":{}},'
            '"api_error_model":{"codes":["VALIDATION_ERROR","NOT_FOUND"]}}'
        ),
        system_extra=(
            f"Design OpenAPI for THIS product only ({name}). Domain: {domain_hint}. "
            "Do not emit /v1/links or short-URL redirects unless the PRD is a URL shortener."
        ),
    )

    openapi: dict[str, Any]
    mode = "heuristic"
    error_model = {
        "codes": [
            "VALIDATION_ERROR",
            "UNAUTHORIZED",
            "NOT_FOUND",
            "GONE",
            "CONFLICT",
            "RATE_LIMITED",
            "INTERNAL",
        ]
    }

    if llm and isinstance(llm, dict):
        candidate = llm.get("openapi") if isinstance(llm.get("openapi"), dict) else llm
        if isinstance(candidate, dict) and (candidate.get("paths") or candidate.get("openapi")):
            openapi = _normalize_openapi(candidate, name)
            mode = "llm"
            if isinstance(llm.get("api_error_model"), dict):
                error_model = llm["api_error_model"]
            if not _openapi_matches_domain(wf, openapi):
                openapi = _build_fallback_openapi(wf)
                mode = "heuristic-domain-correction"
        else:
            openapi = _build_fallback_openapi(wf)
    else:
        openapi = _build_fallback_openapi(wf)

    # Final safety pass — guarantee swagger-renderable ops
    openapi = _normalize_openapi(openapi, name)
    if not _openapi_matches_domain(wf, openapi):
        openapi = _normalize_openapi(_build_fallback_openapi(wf), name)
        mode = "heuristic-domain-correction"
    n_ops = sum(
        1
        for item in (openapi.get("paths") or {}).values()
        if isinstance(item, dict)
        for m in HTTP_METHODS
        if isinstance(item.get(m), dict)
    )
    if n_ops == 0:
        openapi = _normalize_openapi(_build_fallback_openapi(wf), name)
        mode = "heuristic-fallback"
        n_ops = sum(
            1
            for item in (openapi.get("paths") or {}).values()
            if isinstance(item, dict)
            for m in HTTP_METHODS
            if isinstance(item.get(m), dict)
        )

    publish(wf, task, "openapi", openapi, bill=False)
    publish(wf, task, "api_error_model", error_model, bill=False)
    return {
        "summary": f"OpenAPI 3.0.3 for {name}: {len(openapi.get('paths') or {})} paths · {n_ops} operations ({mode})",
        "mode": mode,
        "operations": n_ops,
    }
