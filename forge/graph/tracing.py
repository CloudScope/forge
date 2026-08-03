"""LangSmith tracing bootstrap for Forge LangGraph runs."""

from __future__ import annotations

import os
from typing import Any


def configure_langsmith() -> dict[str, Any]:
    """
    Enable LangSmith when LANGSMITH_API_KEY / LANGCHAIN_API_KEY is set.

    Env:
      LANGSMITH_API_KEY / LANGCHAIN_API_KEY
      LANGSMITH_PROJECT / LANGCHAIN_PROJECT  (default: forge-sdlc)
      LANGSMITH_TRACING / LANGCHAIN_TRACING_V2  (default: true when key present)
      LANGSMITH_ENDPOINT
    """
    api_key = (
        os.environ.get("LANGSMITH_API_KEY")
        or os.environ.get("LANGCHAIN_API_KEY")
        or ""
    ).strip()
    project = (
        os.environ.get("LANGSMITH_PROJECT")
        or os.environ.get("LANGCHAIN_PROJECT")
        or "forge-sdlc"
    ).strip()
    endpoint = (os.environ.get("LANGSMITH_ENDPOINT") or "").strip()

    tracing_flag = (
        os.environ.get("LANGSMITH_TRACING")
        or os.environ.get("LANGCHAIN_TRACING_V2")
        or ""
    ).strip().lower()

    enabled = bool(api_key) and tracing_flag not in {"0", "false", "no", "off"}
    if enabled and tracing_flag == "":
        # Key present → turn tracing on unless explicitly disabled.
        os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")
        os.environ.setdefault("LANGSMITH_TRACING", "true")

    if api_key:
        os.environ.setdefault("LANGCHAIN_API_KEY", api_key)
        os.environ.setdefault("LANGSMITH_API_KEY", api_key)
    os.environ.setdefault("LANGCHAIN_PROJECT", project)
    os.environ.setdefault("LANGSMITH_PROJECT", project)
    if endpoint:
        os.environ.setdefault("LANGCHAIN_ENDPOINT", endpoint)
        os.environ.setdefault("LANGSMITH_ENDPOINT", endpoint)

    return {
        "enabled": enabled,
        "project": project,
        "endpoint": endpoint or None,
        "api_key_set": bool(api_key),
    }


def run_config(thread_id: str, *, tags: list[str] | None = None) -> dict[str, Any]:
    """RunnableConfig for a Forge workflow thread (LangGraph + LangSmith)."""
    meta = configure_langsmith()
    cfg: dict[str, Any] = {
        "configurable": {"thread_id": thread_id},
        "tags": tags or ["forge", "sdlc", "langgraph"],
        "metadata": {
            "workflow_id": thread_id,
            "forge_runtime": "langgraph",
            "langsmith_project": meta.get("project"),
        },
    }
    return cfg
