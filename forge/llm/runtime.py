from __future__ import annotations

import json
import logging
from typing import Any, Optional

from ..models import TaskNode, Workflow
from .client import LLMResult, get_client
from .errors import LLMError
from .prompts import system_prompt

logger = logging.getLogger("forge.llm")


def llm_enabled() -> bool:
    return get_client().enabled()


def apply_usage(wf: Workflow, result: LLMResult) -> None:
    wf.budgets["tokens"] = float(wf.budgets.get("tokens") or 0) + result.total_tokens
    wf.budgets["usd_spent"] = float(wf.budgets.get("usd_spent") or 0) + result.cost_usd
    wf.budgets["llm_calls"] = float(wf.budgets.get("llm_calls") or 0) + 1
    wf.facts["llm_enabled"] = True
    wf.facts["llm_model"] = result.model


def complete_json(
    wf: Workflow,
    task: TaskNode,
    *,
    agent: str,
    user_payload: dict[str, Any],
    schema_hint: str = "",
    system_extra: str = "",
    audit_append: Optional[Any] = None,
) -> dict[str, Any]:
    """
    Call LLM for structured JSON. Raises LLMError on failure.

    Updates workflow budgets and optionally writes an audit event.
    """
    client = get_client()
    system = system_prompt(agent, extra=system_extra)
    user_obj = {
        "agent": agent,
        "task_id": task.id,
        "mission": task.description,
        "risk_tier": task.risk_tier.value
        if hasattr(task.risk_tier, "value")
        else str(task.risk_tier),
        "workflow_id": wf.id,
        "facts": {
            k: v
            for k, v in wf.facts.items()
            if k
            in {
                "product_name",
                "feature_qr",
                "from_document",
                "requirement_filename",
                "analytics_option",
            }
        },
        "inputs": user_payload,
    }
    if schema_hint:
        user_obj["required_json_shape"] = schema_hint
    user = json.dumps(user_obj, default=str)
    # Bound prompt size for production safety
    if len(user) > 120_000:
        user = user[:120_000] + ',"_truncated":true}'

    result = client.complete_json(system=system, user=user)
    apply_usage(wf, result)

    meta = {
        "model": result.model,
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "total_tokens": result.total_tokens,
        "cost_usd": round(result.cost_usd, 6),
        "latency_ms": round(result.latency_ms, 1),
        "attempts": result.attempts,
        "agent": agent,
        "task_id": task.id,
    }
    wf.events.append({"type": "LLM_COMPLETED", "payload": meta})
    if audit_append is not None:
        try:
            audit_append(
                wf,
                "LLM_COMPLETED",
                task_id=task.id,
                agent=agent,
                payload=meta,
            )
        except Exception:  # noqa: BLE001
            logger.debug("audit append failed for LLM_COMPLETED", exc_info=True)

    # Stamp provenance on output
    out = dict(result.content)
    out.setdefault("_meta", {})
    if isinstance(out["_meta"], dict):
        out["_meta"].update({"generator": "llm", **meta})
    return out


def try_complete_json(
    wf: Workflow,
    task: TaskNode,
    *,
    agent: str,
    user_payload: dict[str, Any],
    schema_hint: str = "",
    system_extra: str = "",
    audit_append: Optional[Any] = None,
) -> Optional[dict[str, Any]]:
    """LLM call with soft failure → None (caller uses heuristics)."""
    if not llm_enabled():
        wf.facts["llm_enabled"] = False
        return None
    try:
        return complete_json(
            wf,
            task,
            agent=agent,
            user_payload=user_payload,
            schema_hint=schema_hint,
            system_extra=system_extra,
            audit_append=audit_append,
        )
    except LLMError as exc:
        logger.warning("LLM fallback for %s/%s: %s", agent, task.id, exc)
        wf.events.append(
            {
                "type": "LLM_FALLBACK",
                "payload": {"agent": agent, "task_id": task.id, "error": str(exc)},
            }
        )
        return None
