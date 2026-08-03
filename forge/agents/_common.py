from __future__ import annotations

import hashlib
import json
import threading
from typing import Any, Callable

from ..models import Artifact, TaskNode, Workflow

# Guards artifact/budget mutations when the orchestrator fans out workers.
_ARTIFACT_LOCK = threading.RLock()


def content_hash(content: Any) -> str:
    raw = json.dumps(content, sort_keys=True, default=str).encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def publish(
    wf: Workflow,
    task: TaskNode,
    key: str,
    content: Any,
    *,
    bill: bool = True,
) -> Artifact:
    """Publish a versioned artifact. `bill=False` when LLM already charged usage."""
    with _ARTIFACT_LOCK:
        prev = wf.artifacts.get(key)
        version = 1 if prev is None else prev.version + 1
        art = Artifact(
            key=key,
            version=version,
            task_id=task.id,
            content=content,
            content_hash=content_hash(content),
        )
        wf.artifacts[key] = art
        wf.artifact_history.append(art)
        task.outputs[key] = {"version": version, "hash": art.content_hash}
        if bill:
            wf.budgets["tokens"] += 1200
            wf.budgets["usd_spent"] += 0.02
        return art


AgentFn = Callable[[Workflow, TaskNode], dict[str, Any]]


def art(wf: Workflow, key: str) -> Any:
    """Return latest artifact content or None."""
    a = wf.artifacts.get(key)
    return a.content if a else None
