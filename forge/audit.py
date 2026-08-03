from __future__ import annotations

import itertools
import threading
import time
from typing import Any, Optional

from .models import Workflow
from .storage import document_store


class AuditTraceStore:
    """
    Append-only audit / trace log for every orchestrator and agent action.

    One stream per workflow, ordered by a monotonic sequence number so events stay
    in causal order regardless of clock skew across parallel workers — or across
    separate execution segments when a run resumes on different infrastructure.
    """

    def __init__(self, root: Any = None):
        # `root` retained for call-site compatibility; the storage factory owns
        # placement now.
        self.root = root
        self.docs = document_store()
        self._lock = threading.Lock()
        self._counters: dict[str, itertools.count] = {}

    def _next_seq(self, workflow_id: str) -> int:
        counter = self._counters.get(workflow_id)
        if counter is None:
            # Continue an existing stream when a later segment resumes the run.
            existing = self.docs.read_events(workflow_id)
            start = max((int(e.get("seq") or 0) for e in existing), default=0) + 1
            counter = itertools.count(start)
            self._counters[workflow_id] = counter
        return next(counter)

    def append(
        self,
        wf: Workflow,
        event_type: str,
        *,
        task_id: Optional[str] = None,
        agent: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
        span: Optional[str] = None,
    ) -> dict[str, Any]:
        evt = {
            "ts": time.time(),
            "trace_id": wf.id,
            "span_id": span or task_id or "orchestrator",
            "workflow_id": wf.id,
            "playbook_id": wf.playbook_id,
            "type": event_type,
            "task_id": task_id,
            "agent": agent,
            "payload": payload or {},
            "budgets": dict(wf.budgets),
        }
        with self._lock:
            seq = self._next_seq(wf.id)
            evt["seq"] = seq
            self.docs.append_event(wf.id, seq, evt)
            # Mirror into in-memory workflow events for checkpoint compatibility.
            wf.events.append(evt)
        return evt

    def read(self, workflow_id: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        events = self.docs.read_events(workflow_id, limit=limit)
        return sorted(events, key=lambda e: int(e.get("seq") or 0))

    def delete(self, workflow_id: str) -> bool:
        self._counters.pop(workflow_id, None)
        return self.docs.delete_events(workflow_id)
