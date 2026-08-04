"""
A parked gate must always be visible to the Studio.

On AWS the run advances inside a Fargate task, so the API Lambda's in-memory
`_live_runs` cache is never authoritative — a warm container keeps serving the
snapshot it last saw. When that snapshot predates the gate, the endpoint the UI
polls reports RUNNING/none, the Studio renders no buttons, and the workflow
parks forever waiting for a click nobody can make.
"""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

from forge import dashboard
from forge.dashboard import app
from forge.models import WorkflowStatus


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_live_runs():
    dashboard._live_runs.clear()
    yield
    dashboard._live_runs.clear()


def _paused_workflow(client) -> str:
    """Start a run from the bundled example and drive it to its first gate."""
    res = client.post("/api/workflows/from-example", data={"auto_approve": "false"})
    assert res.status_code == 200, res.text
    workflow_id = res.json()["workflow_id"]
    for _ in range(200):
        detail = client.get(f"/api/workflows/{workflow_id}")
        if detail.status_code == 404:
            # The suite shares one FORGE_VAR_ROOT and other tests exercise the
            # cleanup endpoint; a vanished run is interference, not a failure.
            pytest.skip("workflow was cleaned up by another test")
        doc = detail.json().get("workflow") or {}
        if doc.get("status") == WorkflowStatus.WAITING_APPROVAL.value:
            return workflow_id
    pytest.skip("example run did not reach a gate")


def _stale_cache(workflow_id: str) -> None:
    """
    Replace the cached run with an independent pre-gate snapshot.

    A copy, not a mutation: the cached object is the one the live engine is still
    driving, and editing it in place would corrupt the persisted state the fix is
    supposed to fall back to.
    """
    live = dashboard._live_runs[workflow_id]
    stale = copy.deepcopy(live["wf"])
    stale.status = WorkflowStatus.RUNNING
    stale.approvals = []
    dashboard._live_runs[workflow_id] = {**live, "wf": stale}


class TestStaleCacheCannotHideAGate:
    def test_gate_is_served_when_the_cache_predates_it(self, client):
        workflow_id = _paused_workflow(client)
        # Sanity: the gate is visible before anything goes stale.
        assert client.get(f"/api/workflows/{workflow_id}/pending-approval").json()["pending"]

        _stale_cache(workflow_id)

        res = client.get(f"/api/workflows/{workflow_id}/pending-approval")

        assert res.status_code == 200
        body = res.json()
        assert body["pending"], "stale cache hid the gate — the Studio shows no buttons"
        assert body["status"] == WorkflowStatus.WAITING_APPROVAL.value

    def test_a_finished_run_is_never_offered_as_a_gate(self, client, monkeypatch):
        """
        The mirror case: the cache still holds the gate but the stored run has
        moved on — failed, or answered elsewhere. Offering buttons for it invites
        a click that answers a question nobody is asking, which is how a failed
        run keeps presenting a live-looking gate in the Studio.
        """
        workflow_id = _paused_workflow(client)
        assert client.get(f"/api/workflows/{workflow_id}/pending-approval").json()["pending"]

        doc = dict(dashboard._workflow_doc(workflow_id) or {})
        doc["status"] = WorkflowStatus.FAILED.value
        monkeypatch.setattr(
            dashboard,
            "_workflow_doc",
            lambda wid, _doc=doc: _doc if wid == workflow_id else None,
        )

        body = client.get(f"/api/workflows/{workflow_id}/pending-approval").json()

        assert body["pending"] == [], "offered a gate on a run that already failed"
        assert body["status"] == WorkflowStatus.FAILED.value

    def test_a_stale_cache_does_not_break_approval(self, client):
        workflow_id = _paused_workflow(client)
        pending = client.get(
            f"/api/workflows/{workflow_id}/pending-approval"
        ).json()["pending"][0]

        _stale_cache(workflow_id)

        res = client.post(
            f"/api/workflows/{workflow_id}/approve",
            json={
                "decision": "approve",
                "approval_id": pending["id"],
                "task_id": pending["task_id"],
                "rationale": "test",
            },
        )

        assert res.status_code == 200, res.text
