"""Control-plane access control and destructive-endpoint guards."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from forge.auth import TOKEN_COOKIE
from forge.dashboard import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def remote_client():
    """A caller that is not on the loopback interface."""
    return TestClient(app, client=("203.0.113.9", 51234))


class TestLoopbackOnlyMode:
    """With no token configured, only local callers are served."""

    def test_local_caller_is_allowed(self, client):
        assert client.get("/api/workflows").status_code == 200

    def test_remote_caller_is_refused(self, remote_client):
        res = remote_client.get("/api/workflows")

        assert res.status_code == 403
        assert "FORGE_API_TOKEN" in res.json()["detail"]

    def test_remote_caller_cannot_reach_destructive_endpoints(self, remote_client):
        res = remote_client.delete("/api/workflows/cleanup?confirm=true")

        assert res.status_code == 403

    def test_health_stays_open_for_liveness_probes(self, remote_client):
        res = remote_client.get("/api/health")

        assert res.status_code == 200
        assert res.json()["auth"]["mode"] == "loopback_only"


class TestTokenMode:
    @pytest.fixture(autouse=True)
    def _token(self, monkeypatch):
        monkeypatch.setenv("FORGE_API_TOKEN", "s3cret-token")

    def test_request_without_credentials_is_refused(self, client):
        res = client.get("/api/workflows")

        assert res.status_code == 403
        assert "Missing or invalid" in res.json()["detail"]

    def test_local_caller_is_no_longer_trusted_implicitly(self, client):
        """Configuring a token must tighten, never loosen, the posture."""
        assert client.get("/api/workflows").status_code == 403

    def test_bearer_token_is_accepted(self, client):
        res = client.get(
            "/api/workflows", headers={"Authorization": "Bearer s3cret-token"}
        )

        assert res.status_code == 200

    def test_custom_header_is_accepted(self, client):
        res = client.get("/api/workflows", headers={"X-Forge-Token": "s3cret-token"})

        assert res.status_code == 200

    def test_wrong_token_is_refused(self, client):
        res = client.get("/api/workflows", headers={"Authorization": "Bearer nope"})

        assert res.status_code == 403

    def test_query_bootstrap_sets_a_cookie_for_the_spa(self, client):
        res = client.get("/api/workflows?token=s3cret-token")

        assert res.status_code == 200
        assert res.cookies.get(TOKEN_COOKIE) == "s3cret-token"

    def test_cookie_authenticates_subsequent_requests(self, client):
        client.cookies.set(TOKEN_COOKIE, "s3cret-token")

        assert client.get("/api/workflows").status_code == 200

    def test_remote_caller_with_a_valid_token_is_allowed(self, remote_client):
        res = remote_client.get(
            "/api/workflows", headers={"Authorization": "Bearer s3cret-token"}
        )

        assert res.status_code == 200

    def test_health_reports_token_mode(self, client):
        assert client.get("/api/health").json()["auth"]["mode"] == "token"


class TestDestructiveCleanupGuard:
    def test_full_wipe_requires_explicit_confirmation(self, client):
        res = client.delete("/api/workflows/cleanup")

        assert res.status_code == 400
        assert "confirm=true" in res.json()["detail"]

    def test_confirmed_wipe_is_accepted(self, client):
        res = client.delete("/api/workflows/cleanup?confirm=true")

        assert res.status_code == 200
        assert res.json()["status"] == "cleaned"

    def test_finished_only_cleanup_needs_no_confirmation(self, client):
        res = client.delete("/api/workflows/cleanup?finished_only=true")

        assert res.status_code == 200
        assert res.json()["finished_only"] is True


class TestResponseHardening:
    def test_baseline_security_headers_are_set(self, client):
        headers = client.get("/api/health").headers

        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "SAMEORIGIN"
        assert headers["Referrer-Policy"] == "no-referrer"

    def test_headers_are_present_on_rejections_too(self, remote_client):
        headers = remote_client.get("/api/workflows").headers

        assert headers["X-Content-Type-Options"] == "nosniff"
