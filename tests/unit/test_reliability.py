"""Reliability metrics: success rate, retries, rollbacks, MTTR, latency."""

from __future__ import annotations

import time

from forge.models import TaskStatus
from forge.reliability import (
    aggregate_platform,
    close_failure_window,
    finalize,
    mark_started,
    open_failure_window,
    record_compensation,
    record_parallel_batch,
    record_retry,
    record_safe_stop,
)
from tests.conftest import make_node


class TestCounters:
    def test_retries_and_rollbacks_accumulate(self, make_workflow):
        wf = make_workflow([])

        record_retry(wf)
        record_retry(wf)
        record_compensation(wf, 3)
        record_safe_stop(wf)

        assert wf.metrics["task_retries"] == 2
        assert wf.metrics["compensations"] == 3
        assert wf.metrics["rollbacks"] == 3
        assert wf.metrics["safe_stops"] == 1

    def test_parallel_width_tracks_the_maximum(self, make_workflow):
        wf = make_workflow([])

        record_parallel_batch(wf, 2)
        record_parallel_batch(wf, 5)
        record_parallel_batch(wf, 3)

        assert wf.metrics["parallel_batches"] == 3
        assert wf.metrics["parallel_max_width"] == 5


class TestSuccessRate:
    def test_rate_counts_only_terminal_outcomes(self, make_workflow):
        wf = make_workflow(
            [make_node("a"), make_node("b"), make_node("c"), make_node("d")]
        )
        wf.tasks["a"].status = TaskStatus.SUCCEEDED
        wf.tasks["b"].status = TaskStatus.SUCCEEDED
        wf.tasks["c"].status = TaskStatus.SUCCEEDED
        wf.tasks["d"].status = TaskStatus.FAILED

        m = finalize(wf)

        assert m["task_succeeded"] == 3
        assert m["task_failed"] == 1
        assert m["success_rate"] == 0.75

    def test_skipped_nodes_do_not_penalise_the_rate(self, make_workflow):
        wf = make_workflow([make_node("a"), make_node("b")])
        wf.tasks["a"].status = TaskStatus.SUCCEEDED
        wf.tasks["b"].status = TaskStatus.SKIPPED

        m = finalize(wf)

        assert m["success_rate"] == 1.0
        assert m["task_skipped"] == 1

    def test_rate_is_undefined_with_no_terminal_tasks(self, make_workflow):
        wf = make_workflow([make_node("a")])

        assert finalize(wf)["success_rate"] is None


class TestMTTR:
    def test_mttr_averages_closed_failure_windows(self, make_workflow):
        wf = make_workflow([])

        open_failure_window(wf, "t1", "boom")
        time.sleep(0.01)
        close_failure_window(wf, "t1")

        assert wf.metrics["mttr_ms"] is not None
        assert wf.metrics["mttr_ms"] >= 10

    def test_open_windows_are_excluded_from_mttr(self, make_workflow):
        wf = make_workflow([])

        open_failure_window(wf, "t1", "still broken")

        assert wf.metrics["mttr_ms"] is None
        assert wf.metrics["failure_windows"][0]["recovered_at"] is None

    def test_closing_matches_the_named_task(self, make_workflow):
        wf = make_workflow([])

        open_failure_window(wf, "t1", "a")
        open_failure_window(wf, "t2", "b")
        close_failure_window(wf, "t2")

        windows = {w["task_id"]: w for w in wf.metrics["failure_windows"]}
        assert windows["t2"]["recovered_at"] is not None
        assert windows["t1"]["recovered_at"] is None


def test_e2e_latency_is_measured_from_start(make_workflow):
    wf = make_workflow([])
    mark_started(wf)
    time.sleep(0.01)

    m = finalize(wf)

    assert m["e2e_latency_ms"] >= 10
    assert m["finished_at"] >= m["started_at"]


def test_mark_started_is_idempotent(make_workflow):
    wf = make_workflow([])

    mark_started(wf)
    first = wf.metrics["started_at"]
    time.sleep(0.01)
    mark_started(wf)

    assert wf.metrics["started_at"] == first


class TestPlatformAggregation:
    def test_aggregates_across_workflow_snapshots(self):
        out = aggregate_platform(
            [
                {"status": "SUCCEEDED", "metrics": {"success_rate": 1.0, "mttr_ms": 100.0,
                                                    "e2e_latency_ms": 1000.0, "task_retries": 1,
                                                    "parallel_max_width": 4}},
                {"status": "FAILED", "metrics": {"success_rate": 0.5, "mttr_ms": 300.0,
                                                 "e2e_latency_ms": 2000.0, "rollbacks": 2,
                                                 "parallel_max_width": 2}},
            ]
        )

        assert out["workflows"] == 2
        assert out["workflow_success_rate"] == 0.5
        assert out["avg_task_success_rate"] == 0.75
        assert out["avg_mttr_ms"] == 200.0
        assert out["avg_e2e_latency_ms"] == 1500.0
        assert out["total_retries"] == 1
        assert out["total_rollbacks"] == 2
        assert out["parallel_max_width"] == 4

    def test_empty_platform_reports_no_rates(self):
        out = aggregate_platform([])

        assert out["workflows"] == 0
        assert out["workflow_success_rate"] is None
        assert out["avg_mttr_ms"] is None


def test_engine_run_finalises_metrics(engine, make_workflow):
    from forge.models import NodeType

    wf = make_workflow([make_node("a", node_type=NodeType.BARRIER)])

    engine.run(wf)

    assert wf.metrics["success_rate"] == 1.0
    assert wf.metrics["e2e_latency_ms"] is not None
    assert wf.metrics["finished_at"] is not None
