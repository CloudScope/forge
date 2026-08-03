"""Parameter Store hydration. Must never block startup on an AWS failure."""

from __future__ import annotations

import pytest

from forge import secrets


@pytest.fixture(autouse=True)
def _fresh():
    secrets.reset()
    yield
    secrets.reset()


class FakeSSM:
    def __init__(self, values: dict[str, str], fail: set[str] | None = None):
        self.values = values
        self.fail = fail or set()
        self.requested: list[str] = []

    def get_parameter(self, Name: str, WithDecryption: bool = False):  # noqa: N803
        self.requested.append(Name)
        if Name in self.fail:
            raise RuntimeError("ParameterNotFound")
        return {"Parameter": {"Value": self.values[Name]}}


@pytest.fixture
def fake_ssm(monkeypatch):
    def _install(values, fail=None):
        client = FakeSSM(values, fail)
        module = type("boto3", (), {"client": staticmethod(lambda _service: client)})
        monkeypatch.setitem(__import__("sys").modules, "boto3", module)
        return client

    return _install


def test_resolves_referenced_parameters(monkeypatch, fake_ssm):
    monkeypatch.setenv("FORGE_API_TOKEN_PARAM", "/forge/dev/api-token")
    monkeypatch.delenv("FORGE_API_TOKEN", raising=False)
    client = fake_ssm({"/forge/dev/api-token": "s3cret"})

    resolved = secrets.hydrate()

    assert resolved == ["FORGE_API_TOKEN"]
    assert client.requested == ["/forge/dev/api-token"]


def test_an_existing_value_is_never_overwritten(monkeypatch, fake_ssm):
    """Local development and tests must never reach for AWS."""
    monkeypatch.setenv("FORGE_API_TOKEN_PARAM", "/forge/dev/api-token")
    monkeypatch.setenv("FORGE_API_TOKEN", "local-token")
    client = fake_ssm({"/forge/dev/api-token": "remote"})

    assert secrets.hydrate() == []
    assert client.requested == []


def test_no_references_means_no_aws_call(monkeypatch, fake_ssm):
    monkeypatch.delenv("FORGE_API_TOKEN_PARAM", raising=False)
    monkeypatch.delenv("FORGE_LLM_KEY_PARAM", raising=False)
    client = fake_ssm({})

    assert secrets.hydrate() == []
    assert client.requested == []


def test_resolves_several_parameters(monkeypatch, fake_ssm):
    monkeypatch.setenv("FORGE_API_TOKEN_PARAM", "/p/token")
    monkeypatch.setenv("FORGE_LLM_KEY_PARAM", "/p/llm")
    monkeypatch.delenv("FORGE_API_TOKEN", raising=False)
    monkeypatch.delenv("FORGE_LLM_API_KEY", raising=False)
    fake_ssm({"/p/token": "t", "/p/llm": "sk-x"})

    assert set(secrets.hydrate()) == {"FORGE_API_TOKEN", "FORGE_LLM_API_KEY"}


class TestFailuresDoNotBlockStartup:
    """A degraded posture is safe; refusing to boot is not."""

    def test_one_unreadable_parameter_does_not_stop_the_others(
        self, monkeypatch, fake_ssm
    ):
        monkeypatch.setenv("FORGE_API_TOKEN_PARAM", "/p/token")
        monkeypatch.setenv("FORGE_LLM_KEY_PARAM", "/p/missing")
        monkeypatch.delenv("FORGE_API_TOKEN", raising=False)
        monkeypatch.delenv("FORGE_LLM_API_KEY", raising=False)
        fake_ssm({"/p/token": "t"}, fail={"/p/missing"})

        assert secrets.hydrate() == ["FORGE_API_TOKEN"]

    def test_a_missing_token_leaves_the_stricter_auth_posture(
        self, monkeypatch, fake_ssm
    ):
        from forge.auth import configured_token

        monkeypatch.setenv("FORGE_API_TOKEN_PARAM", "/p/token")
        monkeypatch.delenv("FORGE_API_TOKEN", raising=False)
        fake_ssm({}, fail={"/p/token"})

        secrets.hydrate()

        assert configured_token() is None  # loopback-only, not open

    def test_boto3_absent_is_tolerated(self, monkeypatch):
        import sys

        monkeypatch.setenv("FORGE_API_TOKEN_PARAM", "/p/token")
        monkeypatch.delenv("FORGE_API_TOKEN", raising=False)
        monkeypatch.setitem(sys.modules, "boto3", None)

        assert secrets.hydrate() == []

    def test_an_empty_parameter_is_ignored(self, monkeypatch, fake_ssm):
        monkeypatch.setenv("FORGE_API_TOKEN_PARAM", "/p/token")
        monkeypatch.delenv("FORGE_API_TOKEN", raising=False)
        fake_ssm({"/p/token": ""})

        assert secrets.hydrate() == []


def test_hydration_runs_once_per_process(monkeypatch, fake_ssm):
    monkeypatch.setenv("FORGE_API_TOKEN_PARAM", "/p/token")
    monkeypatch.delenv("FORGE_API_TOKEN", raising=False)
    client = fake_ssm({"/p/token": "t"})

    secrets.hydrate()
    monkeypatch.delenv("FORGE_API_TOKEN", raising=False)
    secrets.hydrate()

    assert client.requested == ["/p/token"], "cold-start cache did not hold"
