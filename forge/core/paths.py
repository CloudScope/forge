"""Central filesystem layout for Forge (production tree)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[2]
VAR_ROOT = Path(os.environ.get("FORGE_VAR_ROOT", APP_ROOT / "var"))


def _first_existing(*candidates: Path, fallback: Path) -> Path:
    for c in candidates:
        if c.exists():
            return c
    return fallback


@dataclass(frozen=True)
class ForgePaths:
    """Resolved directories for config + runtime data."""

    root: Path
    var: Path
    playbooks: Path
    prompts: Path
    examples: Path
    state: Path
    artifacts: Path
    uploads: Path
    workspaces: Path
    deliverables: Path
    env_file: Path


def paths() -> ForgePaths:
    root = APP_ROOT
    var = VAR_ROOT
    return ForgePaths(
        root=root,
        var=var,
        playbooks=_first_existing(root / "config" / "playbooks", root / "playbooks", fallback=root / "config" / "playbooks"),
        prompts=_first_existing(root / "config" / "prompts", root / "prompts", fallback=root / "config" / "prompts"),
        examples=_first_existing(root / "config" / "examples", root / "examples", fallback=root / "config" / "examples"),
        state=_first_existing(var / "state", root / "state", fallback=var / "state"),
        artifacts=_first_existing(var / "artifacts", root / "artifacts", fallback=var / "artifacts"),
        uploads=_first_existing(var / "uploads", root / "uploads", fallback=var / "uploads"),
        workspaces=_first_existing(var / "workspaces", root / "workspaces", fallback=var / "workspaces"),
        deliverables=_first_existing(var / "deliverables", root / "deliverables", fallback=var / "deliverables"),
        env_file=root / ".env",
    )


def ensure_runtime_dirs() -> ForgePaths:
    p = paths()
    for d in (
        p.var,
        p.state,
        p.state / "workflows",
        p.state / "checkpoints",
        p.state / "audit" / "traces",
        p.state / "memory" / "workflow",
        p.state / "memory" / "agent",
        p.artifacts,
        p.uploads,
        p.workspaces,
        p.deliverables,
        p.playbooks,
        p.prompts,
        p.examples,
    ):
        d.mkdir(parents=True, exist_ok=True)
    return p
