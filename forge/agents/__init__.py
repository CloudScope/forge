"""
Forge specialized agents — full SDLC roster.

Each agent owns structured artifacts and is invoked by the Orchestration Engine
via the REGISTRY mapping (playbook `agent:` field → callable).
"""

from __future__ import annotations

from ._common import AgentFn, publish
from .api import api_design
from .architecture import architecture_design
from .backend import backend_implement
from .business_analyst import business_analyze
from .compliance import compliance_map
from .dag_builder import dag_build
from .database import database_design
from .deployment import deployment_recommend
from .devops import devops_infra
from .documentation import documentation_write
from .frontend import frontend_implement
from .human_approval import human_approval_present, noop_barrier
from .intake import intake_capture
from .observability import observability_define
from .performance import performance_budget
from .planner import plan_decompose
from .product import product_analyze
from .release import release_readiness
from .requirement import requirement_analyze
from .reviewer import engineering_review
from .risk import risk_assess
from .security import security_review
from .security_scan import security_scan
from .summary import engineering_summary
from .testing import test_generate
from .validation_agent import validation_review

# Primary roster (user-required + handbook catalog)
REGISTRY: dict[str, AgentFn] = {
    # Intake
    "intake": intake_capture,
    "product": product_analyze,
    "requirement": requirement_analyze,
    "business_analyst": business_analyze,
    "dag": dag_build,
    # Plan / design
    "planner": plan_decompose,
    "risk": risk_assess,
    "architecture": architecture_design,
    "database": database_design,
    "api": api_design,
    "performance": performance_budget,
    # Build
    "backend": backend_implement,
    "frontend": frontend_implement,
    "devops": devops_infra,
    # Quality
    "testing": test_generate,
    "documentation": documentation_write,
    "observability": observability_define,
    "security": security_review,
    "security_scan": security_scan,
    "compliance": compliance_map,
    "reviewer": engineering_review,
    "validation": validation_review,
    # Ship / governance
    "release": release_readiness,
    "deployment": deployment_recommend,
    "summary": engineering_summary,
    "human_approval": human_approval_present,
    "barrier": noop_barrier,
    # Backward-compatible aliases (older playbooks / expand nodes)
    "codegen": backend_implement,
    "security_validation": security_review,
    "qr_design": architecture_design,
    "analytics_refactor": architecture_design,
    "db_optimize": database_design,
    "bugfix": backend_implement,
}

AGENT_ROSTER: list[tuple[str, str]] = [
    ("orchestrator", "16. Workflow Orchestrator — DAG, barriers, retries, checkpoints, replan"),
    ("intake", "1. Requirement Intake — PRD capture, stakeholders, constraints"),
    ("requirement", "2. Requirement Analysis — FR/NFR, ambiguities, acceptance"),
    ("product", "Product Analyst — MVP cut, backlog, success metrics"),
    ("planner", "3. Planning — epics/stories/tasks, effort, risk"),
    ("dag", "4. Build Dependency Graph — waves, gates, sync points"),
    ("architecture", "6.1 Architecture — HLD/LLD, ADRs, tech stack"),
    ("database", "6.2 Database — ER, schema, migrations"),
    ("api", "6.3 API Design — OpenAPI, contracts, versioning"),
    ("backend", "6.4 Backend — FastAPI workspace from HLD/LLD/DB/OpenAPI"),
    ("frontend", "6.5 Frontend — UI workspace wired to generated API"),
    ("devops", "6.6 DevOps — Docker, K8s, CI/CD, IaC"),
    ("security", "6.7 Security — threat model, AuthN/Z, OWASP"),
    ("testing", "7. Testing — unit/integration/API/load/chaos"),
    ("validation", "8. Validation — quality, architecture, performance"),
    ("security_scan", "9. Security Review — vuln/dependency/SAST-DAST"),
    ("documentation", "10. Documentation — README, ADRs, guides"),
    ("release", "11. Release Readiness — go/no-go recommendation"),
    ("deployment", "13. Deployment Recommendation — rollout, monitoring, rollback"),
    ("observability", "15. Observability & Audit — SLIs/SLOs, lineage"),
    ("summary", "16. Final Deliverables — engineering summary pack"),
    ("human_approval", "Human Approval — plan, arch, DB, API→codegen, release gates"),
]


__all__ = [
    "REGISTRY",
    "AGENT_ROSTER",
    "publish",
    "human_approval_present",
    "noop_barrier",
]
