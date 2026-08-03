from __future__ import annotations

from typing import Any

from ..models import TaskNode, Workflow
from ._common import art, publish
from .design_html import build_database_html, default_url_shortener_schema
from .doc_context import has_feature, product_name, requirement_text
from .llm_bridge import run_llm_agent


def _publish_db_html(
    wf: Workflow,
    task: TaskNode,
    *,
    schema: dict[str, Any],
    migration: dict[str, Any],
    sharding: dict[str, Any],
    index_plan: dict[str, Any],
) -> None:
    html_doc = build_database_html(
        product=product_name(wf),
        schema=schema,
        migration=migration,
        sharding=sharding,
        index_plan=index_plan,
    )
    publish(wf, task, "database_design_html", html_doc, bill=False)


def _is_url_shortener(wf: Workflow) -> bool:
    text = (requirement_text(wf) or "").lower()
    name = product_name(wf).lower()
    needles = ("url short", "short url", "shorten", "tinyurl", "bitly", "snipr", "link short")
    if any(n in text or n in name for n in needles):
        return True
    return has_feature(wf, "short_url", "custom_alias")


def database_design(wf: Workflow, task: TaskNode) -> dict[str, Any]:
    """6.2 Database Agent — typed entities / indexes / sharding + HTML design."""
    if task.id.startswith("db.optimize") or "optimize" in task.id:
        plan = {
            "migrations": ["CREATE INDEX CONCURRENTLY idx_links_org_created"],
            "rollback": "DROP INDEX CONCURRENTLY",
            "risk": "HIGH",
            "approval_required": True,
        }
        publish(wf, task, "db_optimize_plan", plan)
        publish(wf, task, "migration_plan", plan)
        return {"summary": "DB optimize migration plan"}

    llm = run_llm_agent(
        wf,
        task,
        agent="database",
        inputs={
            "hld": art(wf, "hld"),
            "lld": art(wf, "lld"),
            "domain_model": art(wf, "domain_model"),
            "reqspec": art(wf, "reqspec"),
        },
        schema_hint=(
            '{"schema_ddl":{"entities":[{"name":"links","columns":[{"name":"id","type":"BIGINT",'
            '"nullable":false,"key":"PK","notes":"Snowflake"}],"indexes":[],"constraints":[]}],'
            '"tables":{},"indexes":[],"constraints":[],"relationships":[],"sharding":"",'
            '"ddl_excerpt":""},"migration_plan":{},"sharding_strategy":{},"index_plan":{}}'
        ),
        system_extra=(
            "Ground tables in the uploaded PRD and HLD/LLD. Do not invent unrelated entities. "
            "Each column MUST include name, type (SQL), nullable, key (PK|FK|UQ|\"\"), notes. "
            "Prefer schema_ddl.entities[] with full column metadata; also fill tables for compat. "
            "Include relationships (from_table.from_col → to_table.to_col). "
            "Studio renders database_design_html from schema_ddl."
        ),
    )
    if llm and isinstance(llm.get("schema_ddl"), dict):
        schema = llm["schema_ddl"]
        if "sharding" not in schema:
            schema["sharding"] = "hash(short_code) → shard"
        migration = llm.get("migration_plan") or {}
        sharding = llm.get("sharding_strategy") or {"key": "short_code", "method": "hash"}
        index_plan = llm.get("index_plan") or {"indexes": schema.get("indexes") or []}
        publish(wf, task, "schema_ddl", schema, bill=False)
        publish(wf, task, "migration_plan", migration, bill=False)
        publish(wf, task, "sharding_strategy", sharding, bill=False)
        publish(wf, task, "index_plan", index_plan, bill=False)
        _publish_db_html(
            wf,
            task,
            schema=schema,
            migration=migration if isinstance(migration, dict) else {},
            sharding=sharding if isinstance(sharding, dict) else {},
            index_plan=index_plan if isinstance(index_plan, dict) else {},
        )
        ents = schema.get("entities") or schema.get("tables") or {}
        count = len(ents) if isinstance(ents, (list, dict)) else 0
        return {
            "summary": f"Database HTML design via LLM for {product_name(wf)}: {count} entities",
            "mode": "llm",
        }

    name = product_name(wf)
    if _is_url_shortener(wf):
        packed = default_url_shortener_schema(name)
        schema = packed["schema_ddl"]
        migration = packed["migration_plan"]
        sharding = packed["sharding_strategy"]
        index_plan = packed["index_plan"]
    else:
        schema = {
            "product": name,
            "entities": [],
            "tables": {
                "organizations": [
                    {"name": "id", "type": "BIGINT", "nullable": False, "key": "PK"},
                    {"name": "name", "type": "VARCHAR(255)", "nullable": False, "key": ""},
                    {"name": "created_at", "type": "TIMESTAMPTZ", "nullable": False, "key": ""},
                ],
            },
            "constraints": [],
            "indexes": [],
            "sharding": "hash(id)",
            "ddl_excerpt": "",
        }
        migration = {"steps": ["001_init"], "rollback": "down", "risk": "LOW"}
        sharding = {"key": "id", "method": "hash"}
        index_plan = {"indexes": []}

    publish(wf, task, "schema_ddl", schema)
    publish(wf, task, "migration_plan", migration)
    publish(wf, task, "sharding_strategy", sharding)
    publish(wf, task, "index_plan", index_plan)
    _publish_db_html(
        wf,
        task,
        schema=schema,
        migration=migration,
        sharding=sharding,
        index_plan=index_plan,
    )
    ents = schema.get("entities") or schema.get("tables") or {}
    count = len(ents) if isinstance(ents, (list, dict)) else 0
    return {"summary": f"Database HTML design for {name}: {count} entities"}
