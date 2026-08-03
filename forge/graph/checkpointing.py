"""
Durable LangGraph checkpointing.

An in-process saver loses every paused approval when the server restarts, which
defeats the point of a workflow that can wait hours at a human gate. Default to
SQLite on the runtime volume; fall back to memory only if that is impossible, and
report which one is live so the posture is never a surprise.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any

from ..core.paths import paths as forge_paths

logger = logging.getLogger("forge.graph")

CHECKPOINT_DB = "checkpoints.sqlite"


def checkpoint_db_path():
    return forge_paths().state / "langgraph" / CHECKPOINT_DB


def build_checkpointer() -> tuple[Any, dict[str, Any]]:
    """
    Return (saver, info). `FORGE_CHECKPOINTER=memory` opts out for ephemeral runs.

    The SQLite connection is shared across the Studio's worker threads, so it is
    opened with check_same_thread=False; SqliteSaver serialises access internally.
    """
    choice = (os.environ.get("FORGE_CHECKPOINTER") or "sqlite").strip().lower()

    if choice in {"memory", "inmemory", "none"}:
        from langgraph.checkpoint.memory import MemorySaver

        return MemorySaver(), {"type": "memory", "durable": False}

    try:
        from langgraph.checkpoint.sqlite import SqliteSaver

        path = checkpoint_db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False)
        saver = SqliteSaver(conn)
        saver.setup()
        return saver, {"type": "sqlite", "durable": True, "path": str(path)}
    except Exception as exc:  # noqa: BLE001 — degrade loudly, never silently
        from langgraph.checkpoint.memory import MemorySaver

        logger.warning(
            "SQLite checkpointer unavailable (%s) — falling back to in-memory. "
            "Paused approvals will not survive a restart.",
            exc,
        )
        return MemorySaver(), {
            "type": "memory",
            "durable": False,
            "fallback_reason": str(exc),
        }
