"""Shared rules for which approval gates must wait for a human."""

from __future__ import annotations

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
