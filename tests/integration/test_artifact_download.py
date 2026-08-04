"""
Downloading a run's output as a single archive.

Everything is read through the object store, never the local filesystem: on AWS
the artifacts live in S3 and the API Lambda never ran the workflow, so a handler
that reached for `var/artifacts` would serve an empty zip in production while
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
        assert f'filename="{completed}_forge.zip"' in res.headers["content-disposition"]

    def test_the_archive_is_valid(self, archive):
        assert archive.testzip() is None, "archive has a corrupt member"

    def test_it_carries_the_workflow_document(self, archive, completed):
        assert f"{completed}/workflow.json" in archive.namelist()

    def test_it_carries_the_published_artifacts(self, archive, completed):
        names = archive.namelist()
        artifacts = [n for n in names if n.startswith(f"{completed}/artifacts/")]

        assert len(artifacts) > 10, f"expected a full artifact set, got {artifacts}"
        assert any("reqspec" in n for n in artifacts)
        assert any("openapi" in n for n in artifacts)

    def test_members_are_readable(self, archive, completed):
        """A zip of empty files would satisfy every name-based assertion above."""
        doc = archive.read(f"{completed}/workflow.json")

        assert len(doc) > 100
        assert completed.encode() in doc

    def test_an_unknown_workflow_is_404_not_an_empty_zip(self, client):
        res = client.get("/api/workflows/wf_does_not_exist/download")

        assert res.status_code == 404
