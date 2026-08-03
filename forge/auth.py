"""
Access control for the Studio control plane.

The Studio can start workflows, approve high-risk gates, and delete every artifact
on the box. Two postures, chosen by whether `FORGE_API_TOKEN` is set:

* **token configured** — every request must present it (Authorization: Bearer,
  X-Forge-Token, or the `forge_token` cookie). This is the posture for any host
  reachable by more than the operator.
* **no token** — requests are accepted only from the loopback interface. Binding
  to 0.0.0.0 for a container port mapping stays safe: a remote caller is refused
  and told to configure a token.

Fail-closed: an unrecognised client is rejected, never defaulted to allowed.
"""

from __future__ import annotations

import hmac
import os
from typing import Iterable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

# Health is intentionally open: container liveness probes run before credentials
# are injected, and it exposes no workflow data.
PUBLIC_PATHS: frozenset[str] = frozenset({"/api/health"})

TOKEN_COOKIE = "forge_token"
TOKEN_HEADER = "x-forge-token"

LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})


def configured_token() -> str | None:
    """The shared token, or None when the Studio is in loopback-only mode."""
    token = (os.environ.get("FORGE_API_TOKEN") or "").strip()
    return token or None


def _presented_token(request: Request) -> str | None:
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    header = request.headers.get(TOKEN_HEADER)
    if header:
        return header.strip()
    cookie = request.cookies.get(TOKEN_COOKIE)
    if cookie:
        return cookie.strip()
    # Bootstrap only: lets an operator open the Studio from a link. The value is
    # moved into an HttpOnly cookie by the response hook below.
    query = request.query_params.get("token")
    return query.strip() if query else None


def _is_loopback(request: Request) -> bool:
    client = request.client
    if client is None:
        # No peer information (e.g. a misconfigured proxy) — refuse rather than guess.
        return False
    return client.host in LOOPBACK_HOSTS


def _forbidden(detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=403)


class StudioAuthMiddleware(BaseHTTPMiddleware):
    """Authenticate every request that is not explicitly public."""

    def __init__(self, app, public_paths: Iterable[str] = PUBLIC_PATHS):
        super().__init__(app)
        self.public_paths = frozenset(public_paths)

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in self.public_paths:
            return _harden(await call_next(request))

        expected = configured_token()
        if expected is None:
            if not _is_loopback(request):
                return _harden(
                    _forbidden(
                        "Forge Studio accepts remote requests only when FORGE_API_TOKEN "
                        "is configured. Set it and send 'Authorization: Bearer <token>'."
                    )
                )
            return _harden(await call_next(request))

        presented = _presented_token(request)
        if not presented or not hmac.compare_digest(presented, expected):
            return _harden(_forbidden("Missing or invalid Forge API token."))

        response = await call_next(request)
        # Promote a ?token= bootstrap into a cookie so the SPA's own fetches work.
        if request.query_params.get("token") and not request.cookies.get(TOKEN_COOKIE):
            response.set_cookie(
                TOKEN_COOKIE,
                presented,
                httponly=True,
                samesite="strict",
                secure=request.url.scheme == "https",
            )
        return _harden(response)


def _harden(response: Response) -> Response:
    """Baseline response headers for a control plane that renders generated HTML."""
    headers = response.headers
    headers.setdefault("X-Content-Type-Options", "nosniff")
    # SAMEORIGIN, not DENY: the Studio embeds its own design pages in iframes.
    headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    headers.setdefault("Referrer-Policy", "no-referrer")
    return response


def auth_mode() -> dict[str, object]:
    """Reported by /api/health so an operator can see the active posture."""
    token = configured_token()
    return {
        "mode": "token" if token else "loopback_only",
        "token_configured": bool(token),
        "public_paths": sorted(PUBLIC_PATHS),
    }
