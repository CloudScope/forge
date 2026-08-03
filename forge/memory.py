from __future__ import annotations

import time
from typing import Any

from .models import Workflow
from .storage import MEMORY, document_store

# Scope prefixes keep agent and workflow memory in one collection without collisions.
AGENT_SCOPE = "agent"
WORKFLOW_SCOPE = "workflow"

# Bounded history per agent — memory is context, not an archive.
MAX_AGENT_ENTRIES = 100


def agent_key(agent_id: str) -> str:
    return f"{AGENT_SCOPE}#{agent_id}"


def workflow_key(workflow_id: str) -> str:
    return f"{WORKFLOW_SCOPE}#{workflow_id}"


class MemoryContextStore:
    """
    Layered memory:
      - working: current task prompt/tool results
      - workflow: shared facts + frozen decisions for a run
      - agent: per-agent long-term preferences / past mistakes
      - organizational: playbooks, standards (read-only refs)
    """

    def __init__(self, root: Any = None):
        # `root` retained for call-site compatibility.
        self.root = root
        self.docs = document_store()

    def load_agent_memory(self, agent_id: str) -> dict[str, Any]:
        doc = self.docs.get(MEMORY, agent_key(agent_id))
        return doc or {"agent_id": agent_id, "entries": []}

    def remember_agent(
        self, agent_id: str, kind: str, content: Any, citation: str = ""
    ) -> None:
        mem = self.load_agent_memory(agent_id)
        entries = list(mem.get("entries") or [])
        entries.append(
            {
                "ts": time.time(),
                "kind": kind,
                "content": content,
                "citation": citation,
            }
        )
        mem["entries"] = entries[-MAX_AGENT_ENTRIES:]
        mem["agent_id"] = agent_id
        self.docs.put(MEMORY, agent_key(agent_id), mem)

    def load_workflow_memory(self, workflow_id: str) -> dict[str, Any] | None:
        return self.docs.get(MEMORY, workflow_key(workflow_id))

    def list_agent_memories(self) -> list[dict[str, Any]]:
        out = []
        for key in self.docs.list_keys(MEMORY):
            if not key.startswith(f"{AGENT_SCOPE}#"):
                continue
            doc = self.docs.get(MEMORY, key)
            if doc:
                out.append(doc)
        return out

    def delete_workflow_memory(self, workflow_id: str) -> bool:
        return self.docs.delete(MEMORY, workflow_key(workflow_id))

    def context_bundle(self, wf: Workflow, task_id: str, agent_id: str) -> dict[str, Any]:
        """Build the ContextBundle an agent receives before execution."""
        task = wf.tasks[task_id]
        agent_mem = self.load_agent_memory(agent_id)
        recent_artifacts = {
            k: {"version": a.version, "hash": a.content_hash, "task_id": a.task_id}
            for k, a in wf.artifacts.items()
        }
        frozen = {
            k: v
            for k, v in wf.facts.items()
            if k.startswith("frozen_") or k in ("analytics_option", "feature_qr")
        }
        bundle = {
            "workflow_id": wf.id,
            "task_id": task_id,
            "agent_id": agent_id,
            "mission": task.description,
            "risk_tier": task.risk_tier.value,
            "working": {
                "deps": task.deps,
                "attempt": task.attempt,
            },
            "workflow_memory": {
                "facts": dict(wf.facts),
                "frozen_decisions": frozen,
                "artifact_index": recent_artifacts,
            },
            "agent_memory": (agent_mem.get("entries") or [])[-5:],
            "organizational": {
                "playbook_id": wf.playbook_id,
                "standards": ["production-first", "secure-by-design", "auditable"],
            },
        }
        # Persist the workflow memory snapshot alongside the bundle.
        self.docs.put(
            MEMORY,
            workflow_key(wf.id),
            {
                "workflow_id": wf.id,
                "updated_at": time.time(),
                "facts": wf.facts,
                "artifact_keys": list(wf.artifacts.keys()),
                "last_bundle_task": task_id,
            },
        )
        return bundle
