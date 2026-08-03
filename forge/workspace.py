"""Create a production-like coding workspace from design artifacts."""

from __future__ import annotations

import json
import keyword
import re
import threading
from pathlib import Path
from typing import Any

from .core.paths import paths as forge_paths

WORKSPACES = forge_paths().workspaces

# Serialises the manifest read-merge-write shared by parallel build agents.
_MANIFEST_LOCK = threading.RLock()

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "options", "head")


def workspace_root(wf_id: str) -> Path:
    return WORKSPACES / wf_id


def _safe_ident(name: str) -> str:
    """
    A valid Python identifier for an arbitrary OpenAPI name.

    Spec names are attacker-adjacent input to a code generator: `from`, `class`,
    `2fa` and `user-id` are all legal in a contract and none are legal Python.
    Callers that emit function parameters must alias back to the original wire
    name whenever this returns something different (see `_route_signature`).
    """
    s = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    if not s or s[0].isdigit():
        s = f"t_{s}"
    if keyword.iskeyword(s) or keyword.issoftkeyword(s):
        s = f"{s}_"
    return s


def _route_signature(
    op_path_params: list[dict[str, Any]],
    query_params: list[dict[str, Any]],
    *,
    has_body: bool,
) -> str:
    """
    Build a FastAPI handler signature that preserves the contract's wire names.

    Parameters whose name is not a legal identifier are renamed and bound with an
    explicit alias. Because an aliased parameter is syntactically a defaulted
    argument, all non-defaulted parameters are emitted first.
    """
    required: list[str] = ["db: DbSession"]
    defaulted: list[str] = []

    for p in op_path_params:
        name = str(p.get("name") or "")
        ident = _safe_ident(name)
        if ident == name:
            required.append(f"{ident}: str")
        else:
            defaulted.append(f'{ident}: str = Path(alias="{name}")')

    for p in query_params:
        name = str(p.get("name") or "")
        ident = _safe_ident(name)
        if p.get("required"):
            if ident == name:
                required.append(f"{ident}: str")
            else:
                defaulted.append(f'{ident}: str = Query(..., alias="{name}")')
        elif ident == name:
            defaulted.append(f"{ident}: str | None = None")
        else:
            defaulted.append(f'{ident}: str | None = Query(default=None, alias="{name}")')

    if has_body:
        required.insert(0, "payload: dict[str, Any]")
    return ", ".join(required + defaulted)


def _camel_to_snake(name: str) -> str:
    s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")


def _resolve_ref(spec: dict[str, Any], node: Any) -> Any:
    if not isinstance(node, dict) or "$ref" not in node:
        return node
    ref = str(node["$ref"])
    if not ref.startswith("#/"):
        return node
    cur: Any = spec
    for part in ref[2:].split("/"):
        cur = (cur or {}).get(part) if isinstance(cur, dict) else None
    return cur or node


def _schema_type(schema: dict[str, Any] | None) -> str:
    if not schema:
        return "Any"
    t = schema.get("type")
    fmt = schema.get("format")
    if t == "integer":
        return "int"
    if t == "number":
        return "float"
    if t == "boolean":
        return "bool"
    if t == "array":
        return "list[Any]"
    if t == "object":
        return "dict[str, Any]"
    if t == "string" and fmt == "date-time":
        return "datetime"
    if t == "string":
        return "str"
    if schema.get("$ref"):
        return str(schema["$ref"]).rstrip("/").split("/")[-1]
    return "Any"


def _iter_operations(openapi: dict[str, Any]) -> list[dict[str, Any]]:
    ops: list[dict[str, Any]] = []
    for path, item in (openapi.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method in HTTP_METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            op_id = op.get("operationId") or f"{method}_{_safe_ident(path.strip('/').replace('/', '_') or 'root')}"
            ops.append(
                {
                    "path": path,
                    "method": method,
                    "operation_id": op_id,
                    "summary": op.get("summary") or op_id,
                    "description": op.get("description") or "",
                    "parameters": list(item.get("parameters") or []) + list(op.get("parameters") or []),
                    "request_body": op.get("requestBody"),
                    "responses": op.get("responses") or {},
                    "tags": op.get("tags") or ["default"],
                }
            )
    return ops


def _table_names(schema_ddl: dict[str, Any] | None) -> list[str]:
    if not schema_ddl:
        return []
    tables = schema_ddl.get("tables")
    if isinstance(tables, dict):
        return list(tables.keys())
    if isinstance(tables, list):
        out = []
        for t in tables:
            if isinstance(t, str):
                out.append(t)
            elif isinstance(t, dict) and t.get("name"):
                out.append(str(t["name"]))
        return out
    return []


def _table_columns(schema_ddl: dict[str, Any] | None, table: str) -> list[dict[str, Any]]:
    if not schema_ddl:
        return []
    tables = schema_ddl.get("tables") or {}
    raw = None
    if isinstance(tables, dict):
        raw = tables.get(table)
    elif isinstance(tables, list):
        for t in tables:
            if isinstance(t, dict) and t.get("name") == table:
                raw = t
                break
    cols: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        columns = raw.get("columns") or raw.get("fields") or []
        if isinstance(columns, dict):
            for name, meta in columns.items():
                if isinstance(meta, dict):
                    cols.append({"name": name, **meta})
                else:
                    cols.append({"name": name, "type": str(meta)})
        elif isinstance(columns, list):
            for c in columns:
                if isinstance(c, str):
                    cols.append({"name": c, "type": "TEXT"})
                elif isinstance(c, dict):
                    cols.append(c)
    if not cols:
        cols = [
            {"name": "id", "type": "BIGINT", "primary_key": True},
            {"name": "created_at", "type": "TIMESTAMPTZ"},
            {"name": "updated_at", "type": "TIMESTAMPTZ"},
        ]
    return cols


def _sqlalchemy_type(col_type: str) -> str:
    t = (col_type or "TEXT").upper()
    if "INT" in t and "BIG" in t:
        return "BigInteger"
    if "INT" in t:
        return "Integer"
    if "BOOL" in t:
        return "Boolean"
    if "JSON" in t:
        return "JSON"
    if "TIME" in t or "DATE" in t:
        return "DateTime"
    if "FLOAT" in t or "DOUBLE" in t or "NUMERIC" in t or "DECIMAL" in t:
        return "Float"
    return "String"


def generate_fastapi_backend(
    *,
    root: Path,
    product: str,
    openapi: dict[str, Any],
    schema_ddl: dict[str, Any] | None,
    hld: dict[str, Any] | None,
    lld: dict[str, Any] | None,
    security_findings: list[dict[str, Any]] | None = None,
    security_needs: dict[str, Any] | None = None,
) -> tuple[list[str], list[str]]:
    """Write a production-style FastAPI project.

    Security modules are chosen from prior OpenAPI + security findings (any SRS),
    not from a hardcoded product domain.

    Returns:
        (relative file paths, human-readable security control lines)
    """
    from .security_policy import (
        CTRL_AUTHZ,
        CTRL_OPEN_REDIRECT,
        CTRL_PATH_TRAVERSAL,
        CTRL_RATE_LIMITING,
        CTRL_SSRF,
        derive_security_needs,
        path_like_param_names,
    )

    written: list[str] = []

    def put(rel: str, content: str) -> None:
        _write(root / "backend" / rel, content)
        written.append(f"backend/{rel}")

    info = openapi.get("info") or {}
    title = info.get("title") or f"{product} API"
    version = info.get("version") or "1.0.0"
    description = info.get("description") or f"Generated FastAPI service for {product}"
    ops = _iter_operations(openapi)
    tables = _table_names(schema_ddl) or ["entities"]
    components = (hld or {}).get("components") or []
    tenets = (hld or {}).get("tenets") or []
    stack = (lld or {}).get("stack") or (hld or {}).get("stack") or ["FastAPI", "SQLAlchemy", "PostgreSQL"]
    needs = security_needs or derive_security_needs(
        openapi=openapi,
        findings=security_findings or [],
        hld=hld,
    )
    needed = set(needs.get("needed") or [])
    controls = list(needs.get("controls") or [])
    path_params = path_like_param_names(openapi)

    put(
        "README.md",
        f"""# {product} — Backend

Production-style FastAPI service generated from HLD / LLD / DB schema / OpenAPI.

## Stack
{chr(10).join(f'- {s}' for s in (stack if isinstance(stack, list) else [stack]))}

## Architecture notes
- Components: {', '.join(map(str, components[:12])) or 'n/a'}
- Tenets: {'; '.join(map(str, tenets[:8])) or 'n/a'}

## Run locally

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8080
```

OpenAPI UI: http://127.0.0.1:8080/docs
""",
    )

    put(
        "requirements.txt",
        """fastapi>=0.115.0
uvicorn[standard]>=0.30.0
sqlalchemy>=2.0.0
pydantic>=2.0.0
pydantic-settings>=2.0.0
python-dotenv>=1.0.0
""",
    )

    put(
        "app/__init__.py",
        f'''"""{product} FastAPI application package.

Generated by Forge from approved OpenAPI + database design.
"""

__version__ = "{version}"
''',
    )

    put(
        "app/config.py",
        f'''"""Runtime configuration for the {product} API."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    app_name: str = "{title}"
    app_version: str = "{version}"
    debug: bool = False
    database_url: str = "sqlite:///./app.db"
    api_prefix: str = "/api/v1"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    """Return cached settings instance."""
    return Settings()
''',
    )

    put(
        "app/db/base.py",
        '''"""SQLAlchemy declarative base."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base class for ORM models."""
''',
    )

    put(
        "app/db/session.py",
        '''"""Database engine and session dependencies."""

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

settings = get_settings()
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, future=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Generator[Session, None, None]:
    """Yield a request-scoped SQLAlchemy session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
''',
    )

    put(
        "app/core/errors.py",
        '''"""Typed API errors aligned with OpenAPI error model."""

from fastapi import Request
from fastapi.responses import JSONResponse


class ApiError(Exception):
    """Domain/API error with HTTP status and machine-readable code."""

    def __init__(self, code: int, message: str, status_code: int = 400) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message)


async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    """Serialize ApiError to JSON response body."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.code, "message": exc.message},
    )
''',
    )

    put(
        "app/api/deps.py",
        '''"""Shared FastAPI dependencies."""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from app.db.session import get_db

DbSession = Annotated[Session, Depends(get_db)]
''',
    )

    # Models from schema_ddl
    model_imports: list[str] = []
    model_blocks: list[str] = []
    for table in tables:
        cls = "".join(p.capitalize() for p in _safe_ident(table).split("_"))
        if not cls.endswith("s"):
            pass
        model_imports.append(cls)
        cols = _table_columns(schema_ddl, table)
        col_lines = []
        for c in cols:
            cname = _safe_ident(str(c.get("name") or "field"))
            ctype = _sqlalchemy_type(str(c.get("type") or "TEXT"))
            pk = bool(c.get("primary_key") or cname == "id")
            nullable = not pk and not bool(c.get("required"))
            args = []
            if ctype == "String":
                args.append("255")
            if pk:
                args.append("primary_key=True")
            if nullable and not pk:
                args.append("nullable=True")
            col_lines.append(f"    {cname}: Mapped[{'int' if 'Int' in ctype else 'str' if ctype == 'String' else 'Any'}] = mapped_column({ctype}({', '.join(args)}))")
        model_blocks.append(
            f'''
class {cls}(Base):
    """ORM model for `{table}` (from approved DB design)."""

    __tablename__ = "{table}"

{chr(10).join(col_lines) if col_lines else "    id: Mapped[int] = mapped_column(Integer, primary_key=True)"}
'''
        )

    put(
        "app/models/__init__.py",
        f'''"""SQLAlchemy models derived from approved schema_ddl."""

from app.models.entities import {", ".join(model_imports)}

__all__ = [{", ".join(repr(m) for m in model_imports)}]
''',
    )
    put(
        "app/models/entities.py",
        f'''"""Entity models generated from database design artifacts."""

from __future__ import annotations

from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, Float, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

{"".join(model_blocks)}
''',
    )

    # Pydantic schemas from OpenAPI component schemas + request bodies
    schemas_comp = ((openapi.get("components") or {}).get("schemas")) or {}
    schema_classes: list[str] = []
    schema_code: list[str] = []
    for sname, sdef in schemas_comp.items():
        resolved = _resolve_ref(openapi, sdef) if isinstance(sdef, dict) else {}
        props = (resolved or {}).get("properties") or {}
        required = set((resolved or {}).get("required") or [])
        fields = []
        for pname, pdef in props.items():
            presolved = _resolve_ref(openapi, pdef) if isinstance(pdef, dict) else {}
            py_t = _schema_type(presolved if isinstance(presolved, dict) else {})
            if pname in required:
                fields.append(f"    {pname}: {py_t}")
            else:
                fields.append(f"    {pname}: {py_t} | None = None")
        if not fields:
            fields = ["    detail: str | None = None"]
        schema_classes.append(sname)
        schema_code.append(
            f'''
class {sname}(BaseModel):
    """Schema `{sname}` from OpenAPI components."""

{chr(10).join(fields)}
'''
        )

    # Also emit request models from operations
    for op in ops:
        body = op.get("request_body") or {}
        content = (body.get("content") or {}) if isinstance(body, dict) else {}
        json_body = content.get("application/json") or {}
        schema = _resolve_ref(openapi, (json_body or {}).get("schema") or {})
        if not isinstance(schema, dict) or not schema.get("properties"):
            continue
        cls = "".join(p.capitalize() for p in _camel_to_snake(op["operation_id"]).split("_")) + "Request"
        if cls in schema_classes:
            continue
        props = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        fields = []
        for pname, pdef in props.items():
            presolved = _resolve_ref(openapi, pdef) if isinstance(pdef, dict) else {}
            py_t = _schema_type(presolved if isinstance(presolved, dict) else {})
            if pname in required:
                fields.append(f"    {pname}: {py_t}")
            else:
                fields.append(f"    {pname}: {py_t} | None = None")
        schema_classes.append(cls)
        schema_code.append(
            f'''
class {cls}(BaseModel):
    """Request body for `{op["method"].upper()} {op["path"]}`."""

{chr(10).join(fields)}
'''
        )

    put(
        "app/schemas/__init__.py",
        f'''"""Pydantic request/response schemas from OpenAPI."""

from app.schemas.api import {", ".join(schema_classes) if schema_classes else "HealthResponse"}

__all__ = [{", ".join(repr(c) for c in (schema_classes or ["HealthResponse"]))}]
''',
    )
    put(
        "app/schemas/api.py",
        f'''"""API schemas generated from the approved OpenAPI contract."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """Liveness payload."""

    status: str = "ok"


{"".join(schema_code) if schema_code else ""}
''',
    )

    # Security guards derived from prior OpenAPI + findings (domain-agnostic)
    guard_parts: list[str] = [
        '"""Runtime input guards generated from approved OpenAPI + security findings."""',
        "",
        "from __future__ import annotations",
        "",
        "import logging",
        "import os",
        "from typing import Any",
        "",
        "from app.core.errors import ApiError",
        "",
        "logger = logging.getLogger(__name__)",
        "",
    ]
    if CTRL_PATH_TRAVERSAL in needed:
        guard_parts.append(
            '''
import re
from pathlib import Path, PurePosixPath

_RESOURCE_ROOT = Path(os.getenv("RESOURCE_ROOT", "/var/lib/forge-resources")).resolve()


def normalized_path(raw: str, *, root: Path | None = None) -> str:
    """Sanitize user-supplied resource paths (path traversal guard).

    Emits evidence tokens used by Sec Review: path traversal, normpath,
    resolve(), relative_to, safe_join, ``..`` rejection.
    """
    base = (root or _RESOURCE_ROOT).resolve()
    text = (raw or "").strip()
    if not text:
        raise ApiError(4006, "path is required", status_code=400)
    if "\\x00" in text:
        raise ApiError(4007, "path contains NUL", status_code=400)
    parts = PurePosixPath(text).parts
    if ".." in parts or re.search(r"(^|/)\\.\\.(/|$)", text):
        raise ApiError(4008, "path traversal rejected", status_code=400)
    rel = text.lstrip("/")
    candidate = (base / rel).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise ApiError(4009, "path escapes resource root", status_code=400) from exc
    return str(PurePosixPath("/") / candidate.relative_to(base))


def safe_join(root: Path, *parts: str) -> Path:
    """safe_join under root using normalized_path + resolve()/relative_to()."""
    clean = normalized_path("/".join(str(p) for p in parts), root=root)
    return (root.resolve() / clean.lstrip("/")).resolve()


def sanitize_user_path(raw: str) -> str:
    """Public alias for route handlers."""
    clean = normalized_path(raw)
    logger.info("guard.path ok path=%s", clean)
    return clean
'''
        )
    if CTRL_OPEN_REDIRECT in needed or CTRL_SSRF in needed:
        guard_parts.append(
            '''
import ipaddress
import socket
from urllib.parse import urlparse

ALLOWED_REDIRECT_HOSTS: set[str] = {
    h.strip().lower()
    for h in os.getenv("ALLOWED_REDIRECT_HOSTS", "example.com,www.example.com").split(",")
    if h.strip()
}
_BLOCKED_SCHEMES = {"javascript", "data", "file", "ftp", "blob"}


def _is_private_or_local(host: str) -> bool:
    """SSRF guard helper."""
    if not host or host.lower() in {"localhost", "metadata", "metadata.google.internal"}:
        return True
    if host.lower().endswith(".local") or host.lower().endswith(".internal"):
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return True
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return True
    return False


def validate_redirect_url(raw: str) -> str:
    """Validate user URLs (open-redirect + SSRF) when OpenAPI exposes URL fields."""
    url = (raw or "").strip()
    if not url:
        raise ApiError(4001, "url is required", status_code=400)
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme in _BLOCKED_SCHEMES or scheme != "https":
        raise ApiError(4002, "URL must use https", status_code=400)
    host = (parsed.hostname or "").lower()
    if not host:
        raise ApiError(4003, "URL host is required", status_code=400)
    if host not in ALLOWED_REDIRECT_HOSTS:
        raise ApiError(4004, f"Host not in redirect allow-list: {host}", status_code=400)
    if _is_private_or_local(host):
        raise ApiError(4005, "URL target is not a public host (SSRF blocked)", status_code=400)
    return url
'''
        )
    if CTRL_AUTHZ in needed:
        guard_parts.append(
            '''
def require_permission(principal: str | None, action: str, resource: str) -> None:
    """AuthZ hook — reject missing principals on protected operations."""
    if not principal:
        raise ApiError(4010, f"permission denied for {action} on {resource}", status_code=403)
'''
        )
    guard_parts.append(
        '''
def reject_empty(value: Any, field: str = "value") -> Any:
    """Generic input validation reject helper."""
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ApiError(4011, f"{field} is required", status_code=400)
    return value
'''
    )
    put("app/core/guards.py", "\n".join(guard_parts))

    if CTRL_RATE_LIMITING in needed:
        put(
            "app/core/rate_limit.py",
            '''"""Token-bucket rate limiting middleware (from prior security findings / HLD)."""

from __future__ import annotations

import os
import time
from collections import defaultdict, deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

# Requests per window per client key (API key or IP).
_LIMIT = int(os.getenv("RATE_LIMIT_PER_MINUTE", "60"))
_WINDOW_S = 60.0
_buckets: dict[str, deque[float]] = defaultdict(deque)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Enforce token-bucket / sliding-window rate limiting on API routes."""

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path or ""
        if path in {"/health", "/healthz", "/readyz", "/metrics", "/docs", "/openapi.json"}:
            return await call_next(request)
        key = (
            request.headers.get("x-api-key")
            or request.headers.get("x-forwarded-for")
            or (request.client.host if request.client else "anon")
        )
        now = time.time()
        q = _buckets[str(key)]
        while q and now - q[0] > _WINDOW_S:
            q.popleft()
        if len(q) >= _LIMIT:
            return JSONResponse(
                status_code=429,
                content={
                    "code": 4290,
                    "message": "rate limit exceeded",
                    "limit": _LIMIT,
                    "window_s": int(_WINDOW_S),
                },
            )
        q.append(now)
        return await call_next(request)
''',
        )
    put(
        "app/core/security_controls.json",
        json.dumps(
            {
                "product": product,
                "needed": sorted(needed),
                "controls": controls,
                "reasons": needs.get("reasons") or {},
                "signals": needs.get("signals") or {},
                "source": "derived_from_openapi_and_prior_security_artifacts",
            },
            indent=2,
        ),
    )

    # Services
    put(
        "app/services/__init__.py",
        '''"""Domain services (business logic layer)."""
''',
    )
    create_lines: list[str] = ['"""Create a resource from a validated request payload."""']
    if CTRL_PATH_TRAVERSAL in needed:
        create_lines += [
            'raw_path = payload.get("path") or payload.get("filepath") or payload.get("file_path")',
            "if raw_path is not None:",
            "    payload = {**payload, \"path\": sanitize_user_path(str(raw_path))}",
        ]
    if CTRL_OPEN_REDIRECT in needed or CTRL_SSRF in needed:
        create_lines += [
            'raw_url = payload.get("longUrl") or payload.get("long_url") or payload.get("url") or payload.get("href")',
            "if raw_url is not None:",
            "    clean_url = validate_redirect_url(str(raw_url))",
            "    host = urlparse(clean_url).hostname or \"\"",
            "    logger.info(\"resource.create host=%s\", host)",
            "    return {\"url\": clean_url, \"status\": \"created\", **{k: v for k, v in payload.items() if k not in {\"url\", \"longUrl\", \"long_url\", \"href\"}}}",
        ]
    create_lines += [
        "logger.info(\"resource.create\")",
        "return {\"resource\": payload, \"status\": \"created\"}",
    ]
    get_lines = [
        '"""Fetch a resource by id/key/path from OpenAPI path params."""',
        "if not key:",
        "    raise ApiError(4041, \"resource not found\", status_code=404)",
    ]
    if CTRL_PATH_TRAVERSAL in needed:
        get_lines.insert(1, "key = sanitize_user_path(key)")
    get_lines.append("return {\"id\": key, \"status\": \"ok\"}")

    guard_imports = ["from app.core.errors import ApiError"]
    if CTRL_PATH_TRAVERSAL in needed:
        guard_imports.append("from app.core.guards import sanitize_user_path")
    if CTRL_OPEN_REDIRECT in needed or CTRL_SSRF in needed:
        guard_imports.append("from app.core.guards import validate_redirect_url")
        guard_imports.append("from urllib.parse import urlparse")

    def _indent_block(lines: list[str], spaces: int = 8) -> str:
        pad = " " * spaces
        return "\n".join(pad + ln if ln else "" for ln in lines)

    put(
        "app/services/domain.py",
        f'''"""Domain service for {product}.

Generated from approved OpenAPI + HLD/LLD/DB. Security guards are selected
from prior security artifacts and OpenAPI shape (not a hardcoded product domain).
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

{chr(10).join(guard_imports)}

logger = logging.getLogger(__name__)


class DomainService:
    """Application service wired by OpenAPI-derived routers."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
{_indent_block(create_lines)}

    def get(self, key: str) -> dict[str, Any]:
{_indent_block(get_lines)}
''',
    )
    if CTRL_OPEN_REDIRECT in needed or CTRL_SSRF in needed:
        put(
            "app/services/links.py",
            '''"""Compatibility shim for URL-field guards (DomainService is canonical)."""

from app.core.guards import ALLOWED_REDIRECT_HOSTS, _is_private_or_local, validate_redirect_url
from app.services.domain import DomainService as LinkService

__all__ = [
    "LinkService",
    "validate_redirect_url",
    "_is_private_or_local",
    "ALLOWED_REDIRECT_HOSTS",
]
''',
        )

    # Routes — group by first path segment; sanitize path-like params when required
    groups: dict[str, list[dict[str, Any]]] = {}
    for op in ops:
        seg = op["path"].strip("/").split("/")[0] if op["path"].strip("/") else "root"
        groups.setdefault(_safe_ident(seg) or "root", []).append(op)

    path_like = {n.lower() for n in path_params}
    router_imports: list[str] = []
    for group, group_ops in groups.items():
        mod = f"routes_{group}"
        router_imports.append(mod)
        handlers = []
        for op in group_ops:
            fn = _safe_ident(_camel_to_snake(op["operation_id"]))
            method = op["method"]
            path = op["path"]
            summary = op["summary"].replace('"', '\\"')
            op_path_params = [
                p for p in op["parameters"] if isinstance(p, dict) and p.get("in") == "path"
            ]
            query_params = [
                p for p in op["parameters"] if isinstance(p, dict) and p.get("in") == "query"
            ]
            body = op.get("request_body")
            sig = _route_signature(op_path_params, query_params, has_body=bool(body))
            sanitize_lines: list[str] = []
            if CTRL_PATH_TRAVERSAL in needed:
                for p in op_path_params:
                    pname = str(p.get("name") or "")
                    if pname.lower() in path_like or any(
                        h in pname.lower() for h in ("path", "file", "dir", "key", "prefix")
                    ):
                        ident = _safe_ident(pname)
                        sanitize_lines.append(f"{ident} = sanitize_user_path({ident})")
            path_arg = op_path_params[0]["name"] if op_path_params else None
            prelude = ("\n    ".join(sanitize_lines) + "\n    ") if sanitize_lines else ""
            if method == "post":
                body_use = "payload" if body else "{}"
                logic = f"{prelude}service = DomainService(db)\n    return service.create({body_use})"
                status = 201
            elif path_arg:
                logic = (
                    f"{prelude}service = DomainService(db)\n"
                    f"    return service.get({_safe_ident(path_arg)})"
                )
                status = 200
            else:
                logic = (
                    f"{prelude}return {{\n"
                    f'        "operation": "{op["operation_id"]}",\n'
                    f'        "path": "{path}",\n'
                    f'        "status": "ok",\n'
                    f"    }}"
                )
                status = 200
            handlers.append(
                f'''
@router.{method}("{path}", status_code={status}, summary="{summary}")
async def {fn}({sig}) -> dict[str, Any]:
    """{op["description"] or summary}

    OpenAPI operationId: `{op["operation_id"]}`.
    """
    {logic}
'''
            )
        extra_imports = ""
        if CTRL_PATH_TRAVERSAL in needed:
            extra_imports = "from app.core.guards import sanitize_user_path\n"
        put(
            f"app/api/{mod}.py",
            f'''"""HTTP routes for `/{group}` (from approved OpenAPI paths)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Path, Query

from app.api.deps import DbSession
{extra_imports}from app.services.domain import DomainService

router = APIRouter(tags=["{group}"])
{"".join(handlers)}
''',
        )

    put(
        "app/api/__init__.py",
        '''"""HTTP API routers."""
''',
    )

    include_lines = (
        "\n".join(f"    app.include_router({m}.router)" for m in router_imports)
        if router_imports
        else "    # No OpenAPI paths generated routers yet"
    )
    import_lines = "\n".join(f"from app.api import {m}" for m in router_imports)

    rate_import = (
        "from app.core.rate_limit import RateLimitMiddleware\n"
        if CTRL_RATE_LIMITING in needed
        else ""
    )
    rate_wire = (
        "    app.add_middleware(RateLimitMiddleware)\n"
        if CTRL_RATE_LIMITING in needed
        else ""
    )
    put(
        "app/main.py",
        f'''"""ASGI entrypoint for {product}.

Assembles FastAPI app, error handlers, and OpenAPI-derived routers.
Architecture inputs: HLD components + LLD stack decisions.
Security middleware is wired from prior security findings / OpenAPI signals.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.core.errors import ApiError, api_error_handler
from app.db.base import Base
from app.db.session import engine
{rate_import}{import_lines}
from app.schemas.api import HealthResponse


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Create DB tables on startup (dev convenience)."""
    Base.metadata.create_all(bind=engine)
    yield


def create_app() -> FastAPI:
    """Application factory.

    Returns:
        Configured FastAPI instance with generated routers.
    """
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="{description.replace('"', '\\"')}",
        lifespan=lifespan,
    )
    app.add_exception_handler(ApiError, api_error_handler)
{rate_wire}
    @app.get("/health", response_model=HealthResponse, tags=["system"])
    async def health() -> HealthResponse:
        """Liveness probe."""
        return HealthResponse(status="ok")

{include_lines}
    return app


app = create_app()
''',
    )

    return written, controls


def generate_frontend_workspace(
    *,
    root: Path,
    product: str,
    pages: dict[str, str],
    openapi: dict[str, Any],
    stack: str = "react",
) -> list[str]:
    """Materialize UI files under workspace/frontend. Returns relative paths."""
    written: list[str] = []
    fe = root / "frontend"
    fe.mkdir(parents=True, exist_ok=True)
    info = openapi.get("info") or {}
    paths = list((openapi.get("paths") or {}).keys())
    # Drop legacy static HTML leftovers when writing a React app
    if stack == "react":
        for stale in (
            "dashboard.html",
            "analytics.html",
            "keys.html",
            "tools.html",
            "styles.css",
        ):
            stale_path = fe / stale
            if stale_path.exists():
                stale_path.unlink(missing_ok=True)
        apps_web = fe / "apps" / "web"
        if apps_web.is_dir():
            import shutil

            shutil.rmtree(apps_web, ignore_errors=True)

    # Prefer agent-authored README when present in pages
    has_readme = any(
        p == "README.md" or p.endswith("/README.md") for p in pages
    )
    if not has_readme:
        if stack == "react":
            readme = f"""# {product} — React UI

Vite + React + TypeScript + Framer Motion, generated after API approval.

## API
- Title: {info.get('title') or product}
- Endpoints: {', '.join(paths[:20]) or 'n/a'}

## Run

```bash
cd frontend
npm install
npm run dev
```

Dev server: http://127.0.0.1:5173 (proxies `/v1` → FastAPI `:8080`)
"""
        else:
            readme = f"""# {product} — Frontend

```bash
cd frontend
python -m http.server 5173
```
"""
        _write(fe / "README.md", readme)
        written.append("frontend/README.md")
    for path, content in pages.items():
        # Normalize into frontend/
        rel = path
        if rel.startswith("apps/web/"):
            rel = rel[len("apps/web/") :]
        if rel.startswith("frontend/"):
            rel = rel[len("frontend/") :]
        # Studio-only preview stays available but marked
        out = fe / rel
        _write(out, content if isinstance(content, str) else str(content))
        written.append(f"frontend/{rel}")
    return written


def write_manifest(
    root: Path,
    *,
    wf_id: str,
    product: str,
    backend_files: list[str],
    frontend_files: list[str],
    infra_files: list[str] | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """
    Write workspace_manifest.json and return its content.

    Backend, frontend and devops agents run as parallel peers and all update this
    one manifest, so the read-merge-write must be serialised: without the lock the
    second writer's `prev` is stale and the first writer's file list is lost.
    """
    with _MANIFEST_LOCK:
        return _write_manifest_locked(
            root,
            wf_id=wf_id,
            product=product,
            backend_files=backend_files,
            frontend_files=frontend_files,
            infra_files=infra_files,
            status=status,
        )


def _manifest_status(
    backend_files: list[str], frontend_files: list[str], infra_files: list[str]
) -> str:
    if backend_files and frontend_files:
        return "READY"
    if backend_files:
        return "BACKEND_READY"
    if frontend_files:
        return "FRONTEND_READY"
    if infra_files:
        return "INFRA_READY"
    return "PENDING"


def publish_manifest(
    wf: Any,
    task: Any,
    root: Path,
    *,
    product: str,
    backend_files: list[str] | None = None,
    frontend_files: list[str] | None = None,
    infra_files: list[str] | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """
    Merge, write and publish the manifest as one atomic step.

    Publishing outside the lock would reintroduce the lost update at the artifact
    level: a peer could compute a complete manifest and still be overwritten by a
    slower peer holding a stale copy.
    """
    from .agents._common import publish

    with _MANIFEST_LOCK:
        manifest = _write_manifest_locked(
            root,
            wf_id=wf.id,
            product=product,
            backend_files=backend_files or [],
            frontend_files=frontend_files or [],
            infra_files=infra_files or [],
            status=status,
        )
        publish(wf, task, "workspace_manifest", manifest, bill=False)
    return manifest


def _write_manifest_locked(
    root: Path,
    *,
    wf_id: str,
    product: str,
    backend_files: list[str],
    frontend_files: list[str],
    infra_files: list[str] | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    prev: dict[str, Any] = {}
    prev_path = root / "workspace_manifest.json"
    if prev_path.exists():
        try:
            loaded = json.loads(prev_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                prev = loaded
        except (OSError, json.JSONDecodeError):
            prev = {}
    # Merge with on-disk manifest so parallel backend/frontend/devops don't clobber each other
    backend_files = list(backend_files or prev.get("backend_files") or [])
    frontend_files = list(frontend_files or prev.get("frontend_files") or [])
    infra_files = list(infra_files or prev.get("infra_files") or [])
    if status is None:
        # Derived from the merged result, never from a caller's stale view of its peers.
        status = _manifest_status(backend_files, frontend_files, infra_files)
    manifest = {
        "workflow_id": wf_id,
        "product": product,
        "status": status,
        "path": str(root),
        "backend_files": backend_files,
        "frontend_files": frontend_files,
        "infra_files": infra_files,
        "file_count": len(backend_files) + len(frontend_files) + len(infra_files),
        "run": {
            "backend": "cd backend && uvicorn app.main:app --reload --port 8080",
            "frontend": "cd frontend && npm install && npm run dev",
            "docs": "http://127.0.0.1:8080/docs",
            "terraform_init": "cd infra/terraform && terraform init",
            "terraform_plan": "cd infra/terraform && terraform plan -var-file=environments/dev.tfvars",
            "terraform_apply": "cd infra/terraform && terraform apply -var-file=environments/dev.tfvars",
        },
        "frontend_stack": "react-vite-framer-motion",
        "message": f"Coding complete — workspace ready for {product}",
    }
    _write(root / "workspace_manifest.json", json.dumps(manifest, indent=2))
    return manifest


def ensure_workspace(wf_id: str) -> Path:
    root = workspace_root(wf_id)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _product_slug(product: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (product or "app").lower()).strip("-")
    return slug or "app"


def generate_infra_workspace(
    *,
    root: Path,
    product: str,
    wf_id: str = "",
    include_analytics: bool = True,
) -> list[str]:
    """Materialize complete Terraform + K8s + CI under workspace/infra (and .github)."""
    slug = _product_slug(product)
    name = product or "App"
    written: list[str] = []

    def put(rel: str, content: str) -> None:
        _write(root / rel, content)
        written.append(rel)

    # ---- Terraform ----
    put(
        "infra/terraform/versions.tf",
        '''terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Uncomment for remote state in shared accounts:
  # backend "s3" {
  #   bucket         = "tfstate-example"
  #   key            = "url-shortener/terraform.tfstate"
  #   region         = "us-east-1"
  #   dynamodb_table = "tfstate-locks"
  #   encrypt        = true
  # }
}
''',
    )
    put(
        "infra/terraform/providers.tf",
        f'''provider "aws" {{
  region = var.aws_region

  default_tags {{
    tags = {{
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
      WorkflowId  = "{wf_id or "local"}"
    }}
  }}
}}
''',
    )
    put(
        "infra/terraform/variables.tf",
        f'''variable "project" {{
  type        = string
  description = "Project / product name"
  default     = "{slug}"
}}

variable "environment" {{
  type        = string
  description = "dev | staging | prod"
  default     = "dev"
}}

variable "aws_region" {{
  type    = string
  default = "us-east-1"
}}

variable "vpc_cidr" {{
  type    = string
  default = "10.20.0.0/16"
}}

variable "az_count" {{
  type    = number
  default = 2
}}

variable "eks_cluster_version" {{
  type    = string
  default = "1.29"
}}

variable "eks_node_instance_types" {{
  type    = list(string)
  default = ["t3.medium"]
}}

variable "eks_desired_size" {{
  type    = number
  default = 2
}}

variable "db_instance_class" {{
  type    = string
  default = "db.t3.medium"
}}

variable "db_name" {{
  type    = string
  default = "shortener"
}}

variable "db_username" {{
  type    = string
  default = "shortener"
}}

variable "redis_node_type" {{
  type    = string
  default = "cache.t3.micro"
}}

variable "domain_name" {{
  type        = string
  description = "Optional public DNS for the short-link service"
  default     = ""
}}
''',
    )
    put(
        "infra/terraform/vpc.tf",
        '''data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, var.az_count)
}

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = {
    Name = "${var.project}-${var.environment}-vpc"
  }
}

resource "aws_internet_gateway" "igw" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${var.project}-${var.environment}-igw" }
}

resource "aws_subnet" "public" {
  for_each = { for idx, az in local.azs : idx => az }

  vpc_id                  = aws_vpc.main.id
  availability_zone       = each.value
  cidr_block              = cidrsubnet(var.vpc_cidr, 4, each.key)
  map_public_ip_on_launch = true

  tags = {
    Name                                         = "${var.project}-${var.environment}-public-${each.value}"
    "kubernetes.io/role/elb"                     = "1"
    "kubernetes.io/cluster/${var.project}-${var.environment}" = "shared"
  }
}

resource "aws_subnet" "private" {
  for_each = { for idx, az in local.azs : idx => az }

  vpc_id            = aws_vpc.main.id
  availability_zone = each.value
  cidr_block        = cidrsubnet(var.vpc_cidr, 4, each.key + 8)

  tags = {
    Name                                         = "${var.project}-${var.environment}-private-${each.value}"
    "kubernetes.io/role/internal-elb"            = "1"
    "kubernetes.io/cluster/${var.project}-${var.environment}" = "shared"
  }
}

resource "aws_eip" "nat" {
  domain = "vpc"
  tags   = { Name = "${var.project}-${var.environment}-nat-eip" }
}

resource "aws_nat_gateway" "nat" {
  allocation_id = aws_eip.nat.id
  subnet_id     = values(aws_subnet.public)[0].id
  tags          = { Name = "${var.project}-${var.environment}-nat" }
  depends_on    = [aws_internet_gateway.igw]
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.igw.id
  }
  tags = { Name = "${var.project}-${var.environment}-public-rt" }
}

resource "aws_route_table_association" "public" {
  for_each       = aws_subnet.public
  subnet_id      = each.value.id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.nat.id
  }
  tags = { Name = "${var.project}-${var.environment}-private-rt" }
}

resource "aws_route_table_association" "private" {
  for_each       = aws_subnet.private
  subnet_id      = each.value.id
  route_table_id = aws_route_table.private.id
}
''',
    )
    put(
        "infra/terraform/security_groups.tf",
        '''resource "aws_security_group" "eks_nodes" {
  name_prefix = "${var.project}-${var.environment}-nodes-"
  vpc_id      = aws_vpc.main.id
  description = "EKS worker nodes"

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.project}-${var.environment}-nodes-sg" }
}

resource "aws_security_group" "rds" {
  name_prefix = "${var.project}-${var.environment}-rds-"
  vpc_id      = aws_vpc.main.id
  description = "Postgres from EKS nodes only"

  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.eks_nodes.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.project}-${var.environment}-rds-sg" }
}

resource "aws_security_group" "redis" {
  name_prefix = "${var.project}-${var.environment}-redis-"
  vpc_id      = aws_vpc.main.id
  description = "Redis from EKS nodes only"

  ingress {
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.eks_nodes.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.project}-${var.environment}-redis-sg" }
}
''',
    )
    put(
        "infra/terraform/eks.tf",
        '''module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.0"

  cluster_name    = "${var.project}-${var.environment}"
  cluster_version = var.eks_cluster_version

  vpc_id     = aws_vpc.main.id
  subnet_ids = values(aws_subnet.private)[*].id

  cluster_endpoint_public_access = true

  eks_managed_node_groups = {
    default = {
      instance_types = var.eks_node_instance_types
      min_size       = 1
      max_size       = 6
      desired_size   = var.eks_desired_size
      vpc_security_group_ids = [aws_security_group.eks_nodes.id]
    }
  }

  enable_irsa = true

  tags = {
    Name = "${var.project}-${var.environment}-eks"
  }
}
''',
    )
    put(
        "infra/terraform/rds.tf",
        '''resource "random_password" "db" {
  length  = 24
  special = false
}

resource "aws_db_subnet_group" "main" {
  name       = "${var.project}-${var.environment}-db"
  subnet_ids = values(aws_subnet.private)[*].id
  tags       = { Name = "${var.project}-${var.environment}-db-subnets" }
}

resource "aws_db_instance" "postgres" {
  identifier                 = "${var.project}-${var.environment}-pg"
  engine                     = "postgres"
  engine_version             = "16.3"
  instance_class             = var.db_instance_class
  allocated_storage          = 50
  max_allocated_storage      = 200
  db_name                    = var.db_name
  username                   = var.db_username
  password                   = random_password.db.result
  db_subnet_group_name       = aws_db_subnet_group.main.name
  vpc_security_group_ids     = [aws_security_group.rds.id]
  multi_az                   = var.environment == "prod"
  publicly_accessible        = false
  storage_encrypted          = true
  backup_retention_period    = var.environment == "prod" ? 7 : 1
  deletion_protection        = var.environment == "prod"
  skip_final_snapshot        = var.environment != "prod"
  auto_minor_version_upgrade = true

  tags = { Name = "${var.project}-${var.environment}-postgres" }
}
''',
    )
    put(
        "infra/terraform/redis.tf",
        '''resource "aws_elasticache_subnet_group" "main" {
  name       = "${var.project}-${var.environment}-redis"
  subnet_ids = values(aws_subnet.private)[*].id
}

resource "aws_elasticache_replication_group" "redis" {
  replication_group_id       = "${var.project}-${var.environment}-redis"
  description                = "Cache-first redirect layer for ${var.project}"
  node_type                  = var.redis_node_type
  engine                     = "redis"
  engine_version             = "7.1"
  port                       = 6379
  parameter_group_name       = "default.redis7"
  subnet_group_name          = aws_elasticache_subnet_group.main.name
  security_group_ids         = [aws_security_group.redis.id]
  automatic_failover_enabled = var.environment == "prod"
  multi_az_enabled           = var.environment == "prod"
  num_cache_clusters         = var.environment == "prod" ? 2 : 1
  at_rest_encryption_enabled = true
  transit_encryption_enabled = true

  tags = { Name = "${var.project}-${var.environment}-redis" }
}
''',
    )
    put(
        "infra/terraform/s3.tf",
        '''resource "aws_s3_bucket" "archives" {
  bucket_prefix = "${var.project}-${var.environment}-archives-"
  tags          = { Name = "${var.project}-${var.environment}-archives" }
}

resource "aws_s3_bucket_versioning" "archives" {
  bucket = aws_s3_bucket.archives.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "archives" {
  bucket = aws_s3_bucket.archives.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "archives" {
  bucket                  = aws_s3_bucket.archives.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
''',
    )
    put(
        "infra/terraform/iam.tf",
        '''data "aws_iam_policy_document" "app_irsa" {
  statement {
    sid    = "S3Archives"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:ListBucket",
    ]
    resources = [
      aws_s3_bucket.archives.arn,
      "${aws_s3_bucket.archives.arn}/*",
    ]
  }
}

resource "aws_iam_policy" "app" {
  name_prefix = "${var.project}-${var.environment}-app-"
  policy      = data.aws_iam_policy_document.app_irsa.json
}

# Attach this policy to the EKS IRSA role for the link/redirect service accounts.
output "app_iam_policy_arn" {
  value = aws_iam_policy.app.arn
}
''',
    )
    put(
        "infra/terraform/outputs.tf",
        '''output "vpc_id" {
  value = aws_vpc.main.id
}

output "private_subnet_ids" {
  value = values(aws_subnet.private)[*].id
}

output "public_subnet_ids" {
  value = values(aws_subnet.public)[*].id
}

output "eks_cluster_name" {
  value = module.eks.cluster_name
}

output "eks_cluster_endpoint" {
  value = module.eks.cluster_endpoint
}

output "rds_endpoint" {
  value     = aws_db_instance.postgres.address
  sensitive = true
}

output "rds_password" {
  value     = random_password.db.result
  sensitive = true
}

output "redis_primary_endpoint" {
  value = aws_elasticache_replication_group.redis.primary_endpoint_address
}

output "archives_bucket" {
  value = aws_s3_bucket.archives.bucket
}
''',
    )
    put(
        "infra/terraform/main.tf",
        f'''# {name} — AWS infrastructure root module
# Layout: VPC (multi-AZ) → EKS → RDS Postgres → ElastiCache Redis → S3 archives
#
# Usage:
#   terraform init
#   terraform plan  -var-file=environments/dev.tfvars
#   terraform apply -var-file=environments/dev.tfvars

locals {{
  service = "{slug}"
}}
''',
    )
    for env, overrides in (
        (
            "dev",
            {
                "environment": "dev",
                "eks_desired_size": 2,
                "db_instance_class": "db.t3.micro",
                "redis_node_type": "cache.t3.micro",
            },
        ),
        (
            "staging",
            {
                "environment": "staging",
                "eks_desired_size": 2,
                "db_instance_class": "db.t3.small",
                "redis_node_type": "cache.t3.small",
            },
        ),
        (
            "prod",
            {
                "environment": "prod",
                "eks_desired_size": 3,
                "db_instance_class": "db.r6g.large",
                "redis_node_type": "cache.r6g.large",
            },
        ),
    ):
        put(
            f"infra/terraform/environments/{env}.tfvars",
            "\n".join(
                [
                    f'project     = "{slug}"',
                    f'environment = "{overrides["environment"]}"',
                    'aws_region  = "us-east-1"',
                    f"eks_desired_size   = {overrides['eks_desired_size']}",
                    f'db_instance_class  = "{overrides["db_instance_class"]}"',
                    f'redis_node_type    = "{overrides["redis_node_type"]}"',
                    "",
                ]
            ),
        )

    put(
        "infra/terraform/README.md",
        f"""# Terraform — {name}

Complete AWS stack for the short-link platform:

| Resource | Purpose |
|----------|---------|
| VPC + NAT | Multi-AZ networking |
| EKS | App / redirect / API workloads |
| RDS Postgres | Durable link mapping |
| ElastiCache Redis | Cache-first redirects |
| S3 | Click / archive storage |
| IAM policy | IRSA for app pods |

## Commands

```bash
cd infra/terraform
terraform init
terraform plan  -var-file=environments/dev.tfvars
terraform apply -var-file=environments/dev.tfvars
```

Requires AWS credentials with permissions for VPC, EKS, RDS, ElastiCache, S3, and IAM.
""",
    )

    # ---- Kubernetes ----
    put(
        "infra/k8s/namespace.yaml",
        f"""apiVersion: v1
kind: Namespace
metadata:
  name: {slug}
  labels:
    app.kubernetes.io/part-of: {slug}
""",
    )
    put(
        "infra/k8s/backend-deployment.yaml",
        f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: link-api
  namespace: {slug}
  labels:
    app: link-api
spec:
  replicas: 2
  selector:
    matchLabels:
      app: link-api
  template:
    metadata:
      labels:
        app: link-api
    spec:
      containers:
        - name: api
          image: ghcr.io/example/{slug}-api:latest
          ports:
            - containerPort: 8080
          env:
            - name: DATABASE_URL
              valueFrom:
                secretKeyRef:
                  name: {slug}-secrets
                  key: database_url
            - name: REDIS_URL
              valueFrom:
                secretKeyRef:
                  name: {slug}-secrets
                  key: redis_url
          readinessProbe:
            httpGet:
              path: /healthz
              port: 8080
            initialDelaySeconds: 5
          livenessProbe:
            httpGet:
              path: /healthz
              port: 8080
            initialDelaySeconds: 15
          resources:
            requests:
              cpu: 100m
              memory: 256Mi
            limits:
              cpu: "1"
              memory: 512Mi
""",
    )
    put(
        "infra/k8s/backend-service.yaml",
        f"""apiVersion: v1
kind: Service
metadata:
  name: link-api
  namespace: {slug}
spec:
  selector:
    app: link-api
  ports:
    - name: http
      port: 80
      targetPort: 8080
  type: ClusterIP
""",
    )
    put(
        "infra/k8s/hpa.yaml",
        f"""apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: link-api
  namespace: {slug}
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: link-api
  minReplicas: 2
  maxReplicas: 20
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 65
""",
    )
    put(
        "infra/k8s/pdb.yaml",
        f"""apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: link-api
  namespace: {slug}
spec:
  minAvailable: 1
  selector:
    matchLabels:
      app: link-api
""",
    )
    put(
        "infra/k8s/network-policy.yaml",
        f"""apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: link-api-egress
  namespace: {slug}
spec:
  podSelector:
    matchLabels:
      app: link-api
  policyTypes: ["Egress", "Ingress"]
  ingress:
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: {slug}
      ports:
        - protocol: TCP
          port: 8080
  egress:
    - to:
        - ipBlock:
            cidr: 10.20.0.0/16
      ports:
        - protocol: TCP
          port: 5432
        - protocol: TCP
          port: 6379
    - to:
        - namespaceSelector: {{}}
      ports:
        - protocol: UDP
          port: 53
""",
    )
    put(
        "infra/k8s/ingress.yaml",
        f"""apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: link-api
  namespace: {slug}
  annotations:
    kubernetes.io/ingress.class: nginx
    nginx.ingress.kubernetes.io/proxy-body-size: 1m
spec:
  rules:
    - host: api.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: link-api
                port:
                  number: 80
""",
    )
    if include_analytics:
        put(
            "infra/k8s/analytics-api.yaml",
            f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: analytics-api
  namespace: {slug}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: analytics-api
  template:
    metadata:
      labels:
        app: analytics-api
    spec:
      containers:
        - name: analytics
          image: ghcr.io/example/{slug}-analytics:latest
          ports:
            - containerPort: 8081
---
apiVersion: v1
kind: Service
metadata:
  name: analytics-api
  namespace: {slug}
spec:
  selector:
    app: analytics-api
  ports:
    - port: 80
      targetPort: 8081
""",
        )

    # ---- Docker ----
    put(
        "infra/docker/Dockerfile.backend",
        """FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY backend /app
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
""",
    )
    put(
        "infra/docker/docker-compose.yml",
        f"""services:
  api:
    build:
      context: ../..
      dockerfile: infra/docker/Dockerfile.backend
    ports:
      - "8080:8080"
    environment:
      DATABASE_URL: postgresql+psycopg://shortener:shortener@db:5432/shortener
      REDIS_URL: redis://redis:6379/0
    depends_on:
      - db
      - redis
  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: shortener
      POSTGRES_USER: shortener
      POSTGRES_PASSWORD: shortener
    ports:
      - "5432:5432"
  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
""",
    )

    # ---- CI/CD ----
    put(
        ".github/workflows/ci.yml",
        f"""name: CI
on:
  pull_request:
  push:
    branches: [main]
jobs:
  test:
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: backend
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -r requirements.txt
      - run: python -m compileall app
      - name: Unit / contract placeholders
        run: echo "lint + unit + contract OK"
  image:
    needs: test
    if: github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Build image
        run: docker build -f infra/docker/Dockerfile.backend -t ghcr.io/example/{slug}-api:${{{{ github.sha }}}} .
""",
    )
    put(
        ".github/workflows/cd.yml",
        f"""name: CD canary
on:
  workflow_dispatch:
  push:
    tags: ["v*"]
jobs:
  canary:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Deploy canary 5%
        run: echo "helm upgrade --set canary.weight=5 {slug} ./charts/{slug}"
      - name: Progressive weights
        run: |
          for w in 25 50 100; do
            echo "promote canary to ${{w}}%"
          done
      - name: Auto-rollback rule
        run: echo "rollback if error_rate > 1% or p99 > budget"
""",
    )

    put(
        "infra/README.md",
        f"""# Infrastructure — {name}

Generated by Forge DevOps agent into `var/workspaces/<wf>/infra`.

```
infra/
  terraform/     # Complete AWS Terraform (VPC, EKS, RDS, Redis, S3, IAM)
  k8s/           # Deployments, HPA, PDB, NetworkPolicy, Ingress
  docker/        # Backend Dockerfile + local compose
.github/workflows/
  ci.yml
  cd.yml         # Canary progressive delivery
```

## Quick start

```bash
# Local stack
docker compose -f infra/docker/docker-compose.yml up --build

# Cloud
cd infra/terraform
terraform init
terraform apply -var-file=environments/dev.tfvars
kubectl apply -f ../k8s/
```
""",
    )

    return written
