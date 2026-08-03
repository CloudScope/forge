"""
Shared pytest fixtures.

`forge.core.paths` resolves VAR_ROOT at import time, so the runtime root must be
redirected *before* any forge module is imported. Keep these lines at the top.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

_TEST_VAR_ROOT = Path(tempfile.mkdtemp(prefix="forge-tests-"))
os.environ["FORGE_VAR_ROOT"] = str(_TEST_VAR_ROOT)

# Tests must never reach a real model: agents fall back to heuristics.
os.environ["FORGE_LLM_ENABLED"] = "false"
os.environ.pop("FORGE_LLM_API_KEY", None)
os.environ.pop("OPENAI_API_KEY", None)
# Deterministic auth posture for API tests unless a test overrides it.
os.environ.pop("FORGE_API_TOKEN", None)

import pytest  # noqa: E402

from forge.core.paths import ensure_runtime_dirs  # noqa: E402
from forge.engine import OrchestrationEngine  # noqa: E402
from forge.models import (  # noqa: E402
    NodeType,
    RiskTier,
    TaskNode,
    TaskStatus,
    Workflow,
    WorkflowStatus,
    new_id,
)


@pytest.fixture(scope="session", autouse=True)
def _runtime_dirs():
    ensure_runtime_dirs()
    yield
    shutil.rmtree(_TEST_VAR_ROOT, ignore_errors=True)


@pytest.fixture
def var_root() -> Path:
    return _TEST_VAR_ROOT


@pytest.fixture
def engine() -> OrchestrationEngine:
    """Auto-approving engine in CLI-demo mode (no human gate blocking)."""
    return OrchestrationEngine(
        auto_approve=True,
        max_workers=4,
        cli_demo_mode=True,
        allow_stdin_prompt=False,
    )


def make_node(
    node_id: str,
    *,
    agent: str = "barrier",
    deps: list[str] | None = None,
    node_type: NodeType = NodeType.COMPUTE,
    risk: RiskTier = RiskTier.LOW,
    condition: str | None = None,
    status: TaskStatus = TaskStatus.PENDING,
) -> TaskNode:
    return TaskNode(
        id=node_id,
        agent=agent,
        type=node_type,
        deps=list(deps or []),
        risk_tier=risk,
        condition=condition,
        status=status,
    )


@pytest.fixture
def make_workflow():
    """Build an in-memory Workflow from TaskNodes without touching a playbook."""

    def _factory(nodes: list[TaskNode], **facts) -> Workflow:
        wf = Workflow(
            id=new_id("wf"),
            playbook_id="test.playbook",
            status=WorkflowStatus.RUNNING,
        )
        for n in nodes:
            wf.tasks[n.id] = n
        wf.facts.update(facts)
        return wf

    return _factory
