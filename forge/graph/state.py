"""LangGraph channel state for Forge (Workflow object lives in session registry)."""

from __future__ import annotations

from typing import Any, Optional, TypedDict


class ForgeGraphState(TypedDict, total=False):
    workflow_id: str
    playbook_id: str
    status: str
    step: int
    last_batch: list[str]
    pending_approval: Optional[dict[str, Any]]
    error: Optional[str]
    done: bool
    runtime: str  # "langgraph"
