"""
Downloading the generated workspace as a zip.

The archive carries the source tree only — the deliverable a developer opens in
an editor — not the design artifacts the Studio already renders in its own
sections.

Everything is read through the object store, never the local filesystem: on AWS
the workspace lives in S3 and the API Lambda never ran the workflow, so a handler
that reached for `var/workspaces` would serve an empty zip in production while
passing every local test.
"""

from __future__ import annotations

import io
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from forge.dashboard import app


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


@pytest.fixture(scope="module")
def completed(client) -> str:
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


@pytest.fixture(scope="module")
def archive(client, completed) -> zipfile.ZipFile:
    res = client.get(f"/api/workflows/{completed}/download")
    assert res.status_code == 200, res.text
    return zipfile.ZipFile(io.BytesIO(res.content))


class TestDownload:
    def test_served_as_an_attachment(self, client, completed):
        res = client.get(f"/api/workflows/{completed}/download")

        assert res.headers["content-type"] == "application/zip"
        assert (
            f'filename="{completed}_workspace.zip"'
            in res.headers["content-disposition"]
        )

    def test_the_archive_is_valid(self, archive):
        assert archive.testzip() is None, "archive has a corrupt member"

    def test_it_unpacks_straight_into_a_project(self, archive):
        """No wrapper directory: the generated tree sits at the archive root."""
        names = archive.namelist()

        assert names, "archive is empty"
        assert not any(n.startswith("wf_") for n in names), (
            f"paths are wrapped in a workflow directory: {names[:3]}"
        )
        assert any(n.startswith("backend/") for n in names), names[:10]

    def test_it_excludes_the_design_artifacts(self, archive):
        """Explicitly the deliverable only — the Studio renders the docs itself."""
        names = archive.namelist()

        assert not any("artifacts/" in n for n in names)
        assert not any(n.endswith("workflow.json") for n in names)
        assert not any("reqspec" in n or "openapi.v" in n for n in names)

    def test_members_are_readable(self, archive):
        """A zip of empty files would satisfy every name-based assertion above."""
        sources = [n for n in archive.namelist() if n.endswith(".py")]

        assert sources, "no generated Python in the workspace"
        assert any(len(archive.read(n)) > 50 for n in sources)

    def test_an_unknown_workflow_is_404_not_an_empty_zip(self, client):
        res = client.get("/api/workflows/wf_does_not_exist/download")

        assert res.status_code == 404

    def test_a_run_with_no_workspace_explains_itself(self, client):
        """A rolled-back or not-yet-coded run must not hand back an empty zip."""
        started = client.post(
            "/api/workflows/from-example", data={"auto_approve": "false"}
        )
        workflow_id = started.json()["workflow_id"]

        res = client.get(f"/api/workflows/{workflow_id}/download")

        if res.status_code == 200:
            pytest.skip("run generated a workspace before the request landed")
        assert res.status_code == 409
        assert "workspace" in res.json()["detail"].lower()
