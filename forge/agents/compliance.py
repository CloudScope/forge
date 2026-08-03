from __future__ import annotations

from typing import Any

from ..models import TaskNode, Workflow
from ._common import art, publish


def compliance_map(wf: Workflow, task: TaskNode) -> dict[str, Any]:
    """Compliance Agent — control mapping + gaps."""
    sec = art(wf, "security_review") or {}
    mapping = {
        "frameworks": ["SOC2-aligned", "GDPR-ready retention"],
        "controls": [
            {
                "id": "C-01",
                "name": "access_logging",
                "evidence": "audit_logs table + gateway access logs",
                "status": "mapped",
            },
            {
                "id": "C-02",
                "name": "encryption_at_rest",
                "evidence": "RDS/S3/CH encryption flags in terraform",
                "status": "mapped",
            },
            {
                "id": "C-03",
                "name": "retention_policy_90d",
                "evidence": "ClickHouse TTL + archival plan",
                "status": "mapped",
            },
            {
                "id": "C-04",
                "name": "secret_management",
                "evidence": "KMS + no plaintext API keys",
                "status": "mapped",
            },
        ],
        "gaps": sec.get("compliance", {}).get("gaps", [])
        if isinstance(sec.get("compliance"), dict)
        else [],
        "pii": {
            "ip_ua": "hashed",
            "retention_days": 90,
            "subject_requests": "phase-2 erasure workflow",
        },
    }
    publish(wf, task, "compliance_mapping", mapping)
    return {"summary": f"Compliance: {len(mapping['controls'])} controls mapped"}
