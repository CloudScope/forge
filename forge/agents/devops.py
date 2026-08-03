from __future__ import annotations

from typing import Any

from ..models import TaskNode, Workflow
from ..workspace import ensure_workspace, generate_infra_workspace, publish_manifest
from ._common import art, publish
from .doc_context import has_feature, product_name
from .llm_bridge import run_llm_agent


def devops_infra(wf: Workflow, task: TaskNode) -> dict[str, Any]:
    """DevOps Agent — write complete Terraform/K8s/CI into var/workspaces/<wf>/infra."""
    name = product_name(wf)
    include_analytics = has_feature(wf, "analytics")

    # Optional LLM notes; durable files are always written to the workspace.
    llm = run_llm_agent(
        wf,
        task,
        agent="devops",
        inputs={
            "hld": art(wf, "hld"),
            "backend_source": art(wf, "backend_source"),
            "workspace_path": wf.facts.get("workspace_path"),
        },
        schema_hint=(
            '{"infra_notes":{"environments":[],"rollout":"","risks":[]},'
            '"cicd_pipeline":{"pr":[],"main":[],"release":[]}}'
        ),
        system_extra=(
            "Forge materializes complete Terraform under workspaces/<id>/infra/terraform. "
            "Return concise infra_notes + cicd_pipeline only — do not invent file contents."
        ),
    )

    root = ensure_workspace(wf.id)
    infra_files = generate_infra_workspace(
        root=root,
        product=name,
        wf_id=wf.id,
        include_analytics=include_analytics,
    )

    infra = {
        "product": name,
        "workspace": str(root / "infra"),
        "terraform_root": str(root / "infra" / "terraform"),
        "terraform": [
            "vpc",
            "eks",
            "rds_postgres",
            "elasticache_redis",
            "s3_archives",
            "iam_irsa",
        ],
        "k8s": [
            "namespace",
            "link-api",
            "hpa",
            "pdb",
            "network_policies",
            "ingress",
        ]
        + (["analytics-api"] if include_analytics else []),
        "ci": ["lint", "unit", "compileall", "image_build"],
        "cd": {
            "strategy": "canary",
            "steps": ["5%", "25%", "50%", "100%"],
            "auto_rollback": "error_rate > 1% or p99 > budget",
        },
        "environments": ["dev", "staging", "prod"],
        "files": infra_files,
        "notes": (llm or {}).get("infra_notes") if isinstance(llm, dict) else None,
    }

    tree = {path: "generated infra" for path in infra_files}
    existing = art(wf, "source_tree") or {}
    publish(wf, task, "infra", infra, bill=False)
    publish(wf, task, "source_tree", {**existing, **tree}, bill=False)
    publish(
        wf,
        task,
        "cicd_pipeline",
        (llm or {}).get("cicd_pipeline")
        if isinstance(llm, dict) and isinstance((llm or {}).get("cicd_pipeline"), dict)
        else {
            "pr": ["lint", "unit", "compileall"],
            "main": ["build", "staging"],
            "release": ["canary"],
        },
        bill=False,
    )

    publish_manifest(wf, task, root, product=name, infra_files=infra_files)
    wf.facts["workspace_path"] = str(root)
    wf.facts["infra_path"] = str(root / "infra")
    wf.facts["terraform_path"] = str(root / "infra" / "terraform")

    return {
        "summary": (
            f"DevOps workspace for {name}: {len(infra_files)} infra files → "
            f"{root / 'infra'} (complete Terraform + K8s + CI/CD)"
        ),
        "mode": "workspace",
        "workspace": str(root / "infra"),
        "files": len(infra_files),
    }
