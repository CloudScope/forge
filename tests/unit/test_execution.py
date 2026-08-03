"""Execution launchers and the Step Functions handlers that bracket a segment."""

from __future__ import annotations

import json
import threading

import pytest

from forge.aws_lambda import read_state, register_token, register_token_handler
from forge.execution import (
    StepFunctionsLauncher,
    ThreadLauncher,
    execution_mode,
    get_launcher,
    token_key,
    using_step_functions,
)
from forge.storage import WORKFLOWS, document_store


class FakeSfnClient:
    """Records calls instead of reaching AWS."""

    def __init__(self):
        self.started: list[dict] = []
        self.successes: list[dict] = []

    def start_execution(self, **kwargs):
        self.started.append(kwargs)
        return {"executionArn": f"arn:aws:states:::execution/{kwargs['name']}"}

    def send_task_success(self, **kwargs):
        self.successes.append(kwargs)
        return {}


@pytest.fixture
def sfn():
    return StepFunctionsLauncher(
        "arn:aws:states:us-east-1:1:stateMachine:forge", client=FakeSfnClient()
    )


class TestThreadLauncher:
    def test_runs_the_callable(self):
        done = threading.Event()
        launcher = ThreadLauncher()

        launcher.start("wf_1", runner=done.set)

        assert done.wait(timeout=2), "runner never executed"

    def test_reports_state_transitions(self):
        states: list[tuple[str, str]] = []
        finished = threading.Event()

        def on_state(wf_id, state):
            states.append((wf_id, state))
            if state != "RUNNING":
                finished.set()

        ThreadLauncher(on_state).start("wf_1", runner=lambda: None)
        assert finished.wait(timeout=2)

        assert ("wf_1", "RUNNING") in states
        assert ("wf_1", "FINISHED") in states

    def test_a_failing_run_is_reported_not_raised(self):
        states: list[str] = []
        finished = threading.Event()

        def on_state(_wf, state):
            states.append(state)
            if state != "RUNNING":
                finished.set()

        def boom():
            raise RuntimeError("agent exploded")

        ThreadLauncher(on_state).start("wf_1", runner=boom)
        assert finished.wait(timeout=2)

        assert any(s.startswith("FAILED:") for s in states)

    def test_a_runner_is_required(self):
        with pytest.raises(ValueError):
            ThreadLauncher().start("wf_1")

    def test_resume_is_left_to_the_caller(self):
        assert ThreadLauncher().resume("wf_1", {})["handled"] is False


class TestStepFunctionsLauncher:
    def test_start_begins_an_execution(self, sfn):
        result = sfn.start("wf_1")

        assert result["started"] is True
        payload = json.loads(sfn.client.started[0]["input"])
        assert payload["workflow_id"] == "wf_1"
        assert sfn.client.started[0]["name"].startswith("wf_1-")

    def test_start_ignores_the_local_runner(self, sfn):
        """Execution happens in Fargate; the API process must not run the workflow."""
        sfn.start("wf_1", runner=lambda: pytest.fail("runner must not be called"))

    def test_resume_without_a_parked_token_is_not_handled(self, sfn):
        assert sfn.resume("wf_1", {"decision": "approve"})["handled"] is False

    def test_resume_releases_a_parked_token(self, sfn):
        sfn.register_token("wf_1", "token-abc")

        result = sfn.resume("wf_1", {"decision": "approve", "rationale": "ok"})

        assert result["handled"] is True
        sent = sfn.client.successes[0]
        assert sent["taskToken"] == "token-abc"
        assert json.loads(sent["output"])["decision"] == "approve"

    def test_a_released_token_is_consumed(self, sfn):
        sfn.register_token("wf_1", "token-abc")
        sfn.resume("wf_1", {"decision": "approve"})

        assert sfn.pending_token("wf_1") is None

    def test_configuration_is_required(self, monkeypatch):
        monkeypatch.delenv("FORGE_STATE_MACHINE_ARN", raising=False)

        with pytest.raises(RuntimeError, match="FORGE_STATE_MACHINE_ARN"):
            StepFunctionsLauncher()


class TestFactory:
    def test_thread_is_the_default(self):
        assert execution_mode() == "thread"
        assert using_step_functions() is False
        assert isinstance(get_launcher(), ThreadLauncher)

    def test_health_reports_the_mode(self):
        from forge.execution import health

        assert health()["mode"] == "thread"


class TestLambdaHandlers:
    """The state machine's Choice branches on exactly these verdicts."""

    def _store(self, doc):
        document_store().put(WORKFLOWS, doc["id"], doc)

    def test_paused_run_is_detected_from_a_waiting_task(self):
        self._store(
            {
                "id": "wf_paused",
                "status": "WAITING_APPROVAL",
                "tasks": {"approval.coding": {"status": "WAITING_APPROVAL"}},
                "approvals": [
                    {
                        "id": "appr_1",
                        "task_id": "approval.coding",
                        "status": "REQUESTED",
                        "title": "Coding complete",
                        "options": [{"id": "approve"}],
                    }
                ],
            }
        )

        state = read_state("wf_paused")

        assert state["status"] == "PAUSED"
        assert state["gate"] == "approval.coding"
        assert state["pending_approval"]["approval_id"] == "appr_1"

    def test_finished_run_reports_its_terminal_status(self):
        self._store({"id": "wf_done", "status": "SUCCEEDED", "tasks": {}})

        assert read_state("wf_done")["status"] == "SUCCEEDED"

    def test_failed_run_is_reported_as_failed(self):
        self._store({"id": "wf_bad", "status": "FAILED", "tasks": {}})

        assert read_state("wf_bad")["status"] == "FAILED"

    def test_unknown_run_fails_closed(self):
        assert read_state("wf_missing")["status"] == "FAILED"

    def test_register_parks_the_token_where_the_api_looks(self):
        register_token("wf_1", "token-xyz", gate="approval.arch")

        parked = document_store().get(WORKFLOWS, token_key("wf_1"))
        assert parked["task_token"] == "token-xyz"
        assert parked["gate"] == "approval.arch"

    def test_handler_dispatches_on_action(self):
        self._store({"id": "wf_h", "status": "SUCCEEDED", "tasks": {}})

        assert register_token_handler({"action": "read_state", "workflow_id": "wf_h"})[
            "status"
        ] == "SUCCEEDED"
        assert register_token_handler(
            {"action": "register", "workflow_id": "wf_h", "task_token": "t"}
        )["registered"] is True

    def test_handler_rejects_an_unknown_action(self):
        with pytest.raises(ValueError, match="Unknown action"):
            register_token_handler({"action": "nope", "workflow_id": "wf_h"})

    def test_handler_requires_a_workflow_id(self):
        with pytest.raises(ValueError, match="workflow_id"):
            register_token_handler({"action": "read_state"})

    def test_registering_without_a_token_is_rejected(self):
        with pytest.raises(ValueError, match="task_token"):
            register_token_handler({"action": "register", "workflow_id": "wf_h"})
