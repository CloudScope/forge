"""HTTP API surface — re-exports the FastAPI application for uvicorn."""

from forge.dashboard import app, serve

__all__ = ["app", "serve"]
