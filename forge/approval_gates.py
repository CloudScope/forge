"""Shared rules for which approval gates must wait for a human."""

from __future__ import annotations

from typing import Any

# Never silent-auto, even in CLI demo / Studio fast mode.
_HARD_FORCE_PREFIXES = (
    "approval.clarify",
    "approval.coding",
    "approval.figma",
)

# Always pause in Studio. CLI demo mode may auto-decide these.
_STUDIO_FORCE_PREFIXES = (
    "approval.plan",
    "approval.arch",
)


# ── Decision vocabulary ──────────────────────────────────────────────────────
# The single source of truth for what a gate button may submit. The engine
# treats anything outside APPROVE_DECISIONS as a rejection that fails the whole
# workflow, so an id it does not recognise is destructive, not inert: a gate
# offering "proceed" would kill the run the moment it was clicked.
APPROVE_DECISIONS = frozenset(
    {
        "approve",
        "go",
        "A",
        "B",
        "C",
        "open_workspace",
        "agent_design",
        "figma_uploaded",
        "skip_figma",
    }
)

REJECT_DECISIONS = frozenset({"reject", "nogo"})


def is_approve_decision(decision: str) -> bool:
    d = (decision or "").strip()
    return d in APPROVE_DECISIONS or d.lower() in APPROVE_DECISIONS


def is_known_decision(decision: str) -> bool:
    """False for an id no gate can honour — reject it at the edge, do not guess."""
    d = (decision or "").strip()
    return is_approve_decision(d) or d.lower() in REJECT_DECISIONS


def sanitize_options(options: Any) -> list[dict[str, Any]] | None:
    """
    Constrain LLM-authored gate options to ids the engine understands.

    An agent asked for "options" will happily invent ids like "proceed" or
    "continue". Rendering those produces a button that fails the workflow, so
    unknown ids are dropped and the caller falls back to its own defaults.
    """
    if not isinstance(options, list):
        return None
    kept = [
        o
        for o in options
        if isinstance(o, dict) and is_known_decision(str(o.get("id") or ""))
    ]
    # A menu with no way forward is worse than the default menu.
    return kept if any(is_approve_decision(str(o.get("id"))) for o in kept) else None


def is_approval_task(task_id: str) -> bool:
    return (task_id or "").startswith("approval.")


def force_human_gate(task_id: str, *, cli_demo_mode: bool = False) -> bool:
    """
    Return True when this gate must pause for a human decision.

    - Clarify / Figma / Coding: always (Studio + CLI).
    - Plan / Arch: always in Studio; CLI demo mode may auto-approve.
    - DB / API / Release: follow auto_approve (Studio checkbox).
    """
    tid = task_id or ""
    if tid.startswith(_HARD_FORCE_PREFIXES):
        return True
    if tid.startswith(_STUDIO_FORCE_PREFIXES):
        return not cli_demo_mode
    return False
