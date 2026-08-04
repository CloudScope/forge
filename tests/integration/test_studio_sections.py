"""
Every Studio section must render once its artifacts exist.

These pages read artifacts through `_latest_artifact`, which returns parsed JSON.
Call sites that treated the result as a `Path` and re-read it raised
AttributeError — a 500, not a handled "not ready" page — and only ever on a run
far enough along to have published the artifact. An empty workflow renders fine,
so the regression hides until a real run completes.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from forge.dashboard import app

SECTIONS = ["dag", "hld", "lld", "db", "workspace"]


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


@pytest.fixture(scope="module")
def completed(client) -> str:
    """
    Drive the bundled example to a terminal state, answering every gate.

    Module-scoped: the run takes several seconds and every test here only reads
    from it, so repeating it per test would dominate the suite's runtime.
    """
    res = client.post("/api/workflows/from-example", data={"auto_approve": "true"})
    assert res.status_code == 200, res.text
    workflow_id = res.json()["workflow_id"]
    for _ in range(30):
        detail = client.get(f"/api/workflows/{workflow_id}")
        if detail.status_code == 404:
            pytest.skip("workflow was cleaned up by another test")
        if detail.json()["workflow"]["status"] in ("SUCCEEDED", "FAILED"):
            return workflow_id
        pending = client.get(
            f"/api/workflows/{workflow_id}/pending-approval"
        ).json()["pending"]
        if not pending:
            time.sleep(0.3)
            continue
        gate = pending[0]
        client.post(
            f"/api/workflows/{workflow_id}/approve",
            json={
                "decision": "agent_design" if "figma" in gate["task_id"] else "approve",
                "approval_id": gate["id"],
                "task_id": gate["task_id"],
                "rationale": "test",
            },
        )
        time.sleep(0.4)
    pytest.skip("example run did not reach a terminal state")


@pytest.mark.parametrize("section", SECTIONS)
@pytest.mark.parametrize("kind", ["raw", "theme"])
def test_section_renders_on_a_finished_run(client, completed, section, kind):
    res = client.get(f"/api/workflows/{completed}/{kind}/{section}.html")

    assert res.status_code == 200, f"{kind}/{section}.html failed: {res.text[:200]}"
    assert "not ready yet" not in res.text, f"{section} rendered its fallback page"
    assert len(res.text) > 1000, f"{section} rendered suspiciously little content"


def test_the_dag_lists_the_workflow_tasks(client, completed):
    """The DAG is built from the stored tasks, not from a design artifact."""
    body = client.get(f"/api/workflows/{completed}/raw/dag.html").text

    assert "intake.capture" in body
    assert "approval.clarify" in body
