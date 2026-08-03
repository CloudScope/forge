from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional
import time
import uuid


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    WAITING_INPUT = "WAITING_INPUT"
    INVALIDATED = "INVALIDATED"
    SKIPPED = "SKIPPED"
    COMPENSATED = "COMPENSATED"


class WorkflowStatus(str, Enum):
    CREATED = "CREATED"
    PLANNING = "PLANNING"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    REPLANNING = "REPLANNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"


class RiskTier(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class NodeType(str, Enum):
    COMPUTE = "COMPUTE"
    APPROVAL = "APPROVAL"
    BARRIER = "BARRIER"
    CONDITIONAL = "CONDITIONAL"


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


@dataclass
class TaskNode:
    id: str
    agent: str
    type: NodeType = NodeType.COMPUTE
    deps: list[str] = field(default_factory=list)
    risk_tier: RiskTier = RiskTier.LOW
    description: str = ""
    condition: Optional[str] = None  # simple expr: "true" / "false" / fact key
    soft: bool = False
    status: TaskStatus = TaskStatus.PENDING
    attempt: int = 0
    max_attempts: int = 3
    outputs: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None


@dataclass
class Artifact:
    key: str
    version: int
    task_id: str
    content: Any
    content_hash: str
    created_at: float = field(default_factory=time.time)


@dataclass
class ApprovalRequest:
    id: str
    task_id: str
    risk_tier: RiskTier
    title: str
    summary: str
    options: list[dict[str, str]]
    status: str = "REQUESTED"  # REQUESTED|APPROVED|REJECTED
    decision: Optional[str] = None
    rationale: str = ""


@dataclass
class ValidationResult:
    gate: str
    status: str  # PASS|FAIL
    blocking: bool = True
    detail: str = ""


@dataclass
class Workflow:
    id: str
    playbook_id: str
    status: WorkflowStatus = WorkflowStatus.CREATED
    tasks: dict[str, TaskNode] = field(default_factory=dict)
    facts: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, Artifact] = field(default_factory=dict)  # key -> latest
    artifact_history: list[Artifact] = field(default_factory=list)
    approvals: list[ApprovalRequest] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    checkpoint_seq: int = 0
    budgets: dict[str, float] = field(
        default_factory=lambda: {"usd_spent": 0.0, "tokens": 0.0}
    )
    metrics: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)