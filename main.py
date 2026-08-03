"""
ASGI entrypoint for production uvicorn.

  uvicorn main:app --host 0.0.0.0 --port 8787
  uvicorn main:app --reload --port 8787
"""

from __future__ import annotations

from forge.core.paths import ensure_runtime_dirs

ensure_runtime_dirs()

from forge.dashboard import app  # noqa: E402

__all__ = ["app"]
