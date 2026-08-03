from __future__ import annotations

import time
from typing import Any, Optional

from .models import TaskStatus, Workflow


def empty_metrics() -> dict[str, Any]:
    return {
        "task_succeeded": 0,
        "task_failed": 0,
        "task_skipped": 0,
        "task_retries": 0,
        "rollbacks": 0,
        "compensations": 0,
        "parallel_batches": 0,
        "parallel_max_width": 0,
        "safe_stops": 0,
        "e2e_latency_ms": None,
        "mttr_ms": None,
        "success_rate": None,
        "failure_windows": [],
        "started_at": None,
        "finished_at": None,
    }


def ensure_metrics(wf: Workflow) -> dict[str, Any]:
    if not wf.metrics:
        wf.metrics = empty_metrics()
    return wf.metrics


def mark_started(wf: Workflow) -> None:
    m = ensure_metrics(wf)
    if m.get("started_at") is None:
        m["started_at"] = time.time()


def record_retry(wf: Workflow) -> None:
    ensure_metrics(wf)["task_retries"] += 1


def record_parallel_batch(wf: Workflow, width: int) -> None:
    m = ensure_metrics(wf)
    m["parallel_batches"] += 1
    m["parallel_max_width"] = max(int(m.get("parallel_max_width") or 0), width)


def record_safe_stop(wf: Workflow) -> None:
    ensure_metrics(wf)["safe_stops"] += 1


def record_compensation(wf: Workflow, count: int = 1) -> None:
    m = ensure_metrics(wf)
    m["compensations"] += count
    m["rollbacks"] += count


def open_failure_window(wf: Workflow, task_id: str, reason: str) -> None:
    m = ensure_metrics(wf)
    m["failure_windows"].append(
        {
            "task_id": task_id,
            "reason": reason,
            "started_at": time.time(),
            "recovered_at": None,
            "duration_ms": None,
        }
    )


def close_failure_window(wf: Workflow, task_id: Optional[str] = None) -> None:
    """Close the most recent open failure window (optionally matching task_id)."""
    m = ensure_metrics(wf)
    for window in reversed(m["failure_windows"]):
        if window.get("recovered_at") is not None:
            continue
        if task_id and window.get("task_id") != task_id:
            continue
        now = time.time()
        window["recovered_at"] = now
        window["duration_ms"] = round((now - window["started_at"]) * 1000, 1)
        break
    _recompute_mttr(wf)


def _recompute_mttr(wf: Workflow) -> None:
    m = ensure_metrics(wf)
    closed = [
        w["duration_ms"]
        for w in m["failure_windows"]
        if w.get("duration_ms") is not None
    ]
    m["mttr_ms"] = round(sum(closed) / len(closed), 1) if closed else None


def finalize(wf: Workflow) -> dict[str, Any]:
    """Recompute terminal reliability metrics from task state + clocks."""
    m = ensure_metrics(wf)
    succeeded = failed = skipped = 0
    for t in wf.tasks.values():
        if t.status == TaskStatus.SUCCEEDED:
            succeeded += 1
        elif t.status == TaskStatus.FAILED:
            failed += 1
        elif t.status in (TaskStatus.SKIPPED, TaskStatus.COMPENSATED):
            skipped += 1
            if t.status == TaskStatus.COMPENSATED:
                # already counted in compensations; keep skip tally for rate denom
                pass
    m["task_succeeded"] = succeeded
    m["task_failed"] = failed
    m["task_skipped"] = skipped
    denom = succeeded + failed
    m["success_rate"] = round(succeeded / denom, 4) if denom else None
    m["finished_at"] = time.time()
    started = m.get("started_at") or wf.created_at
    m["e2e_latency_ms"] = round((m["finished_at"] - started) * 1000, 1)
    _recompute_mttr(wf)
    return m


def aggregate_platform(workflows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate reliability across saved workflow snapshots."""
    n = len(workflows)
    success_rates: list[float] = []
    mttrs: list[float] = []
    latencies: list[float] = []
    retries = rollbacks = compensations = parallel_batches = 0
    parallel_max = 0
    terminal_ok = terminal_fail = 0

    for w in workflows:
        m = w.get("metrics") or {}
        if m.get("success_rate") is not None:
            success_rates.append(float(m["success_rate"]))
        if m.get("mttr_ms") is not None:
            mttrs.append(float(m["mttr_ms"]))
        if m.get("e2e_latency_ms") is not None:
            latencies.append(float(m["e2e_latency_ms"]))
        retries += int(m.get("task_retries") or 0)
        rollbacks += int(m.get("rollbacks") or 0)
        compensations += int(m.get("compensations") or 0)
        parallel_batches += int(m.get("parallel_batches") or 0)
        parallel_max = max(parallel_max, int(m.get("parallel_max_width") or 0))
        status = (w.get("status") or "").upper()
        if status == "SUCCEEDED":
            terminal_ok += 1
        elif status in ("FAILED", "PARTIAL"):
            terminal_fail += 1

    terminal = terminal_ok + terminal_fail
    return {
        "workflows": n,
        "workflow_success_rate": round(terminal_ok / terminal, 4) if terminal else None,
        "avg_task_success_rate": round(sum(success_rates) / len(success_rates), 4)
        if success_rates
        else None,
        "avg_mttr_ms": round(sum(mttrs) / len(mttrs), 1) if mttrs else None,
        "avg_e2e_latency_ms": round(sum(latencies) / len(latencies), 1)
        if latencies
        else None,
        "total_retries": retries,
        "total_rollbacks": rollbacks,
        "total_compensations": compensations,
        "parallel_batches": parallel_batches,
        "parallel_max_width": parallel_max,
        "terminal_succeeded": terminal_ok,
        "terminal_failed": terminal_fail,
    }
