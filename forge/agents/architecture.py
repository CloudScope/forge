from __future__ import annotations

from functools import lru_cache
from typing import Any

from ..core.paths import paths as forge_paths
from ..models import TaskNode, Workflow
from ._common import art, publish
from .design_html import (
    _default_aws_context_layers,
    _default_url_shortener_lld,
    _merge_lld_defaults,
    build_architecture_html,
    build_lld_html,
)
from .doc_context import doc_summary, has_feature, product_name, requirement_text
from .llm_bridge import run_llm_agent

_HLD_REFERENCE_NAME = "tinyurl-system-design-hld.md"
_LLD_REFERENCE_NAME = "url-shortener-lld.md"


@lru_cache(maxsize=4)
def _load_hld_reference() -> str:
    """Canonical TinyURL-class HLD playbook for Architecture Agent."""
    path = forge_paths().examples / _HLD_REFERENCE_NAME
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


@lru_cache(maxsize=4)
def _load_lld_reference() -> str:
    """Canonical Bitly-clone LLD masterclass playbook for Architecture Agent."""
    path = forge_paths().examples / _LLD_REFERENCE_NAME
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def _is_url_shortener(wf: Workflow) -> bool:
    """True only when the uploaded SRS/PRD is clearly a URL shortener."""
    text = (requirement_text(wf) or "").lower()
    name = product_name(wf).lower()
    summary = " ".join(
        str(x)
        for x in (
            (doc_summary(wf) or {}).get("features") or [],
            (art(wf, "product_brief") or {}).get("name") or "",
            wf.facts.get("requirement_filename") or "",
        )
    ).lower()
    blob = f"{text}\n{name}\n{summary}"
    # File-system / other domains must not inherit TinyURL mode via brand defaults.
    if any(
        tok in blob
        for tok in (
            "file system",
            "filesystem",
            "object-oriented file",
            "inode",
            "directory hierarchy",
        )
    ) and not any(tok in blob for tok in ("url short", "short url", "tinyurl", "bitly")):
        return False
    needles = (
        "url short",
        "short url",
        "shorten",
        "tinyurl",
        "link short",
        "bitly",
    )
    hits = sum(1 for n in needles if n in blob)
    # Require real SRS evidence — do not treat bare product name "Snipr" alone as proof.
    if hits >= 1:
        return True
    return has_feature(wf, "short_url", "custom_alias") and (
        "short" in blob or "url" in blob or "link" in blob
    )


def _publish_design_html(
    wf: Workflow,
    task: TaskNode,
    *,
    hld: dict[str, Any],
    lld: dict[str, Any],
    adrs: list[Any],
    capacity: dict[str, Any],
    budget: dict[str, Any],
    sequences: dict[str, Any],
) -> str:
    name = product_name(wf)
    req = art(wf, "reqspec") or {}
    html_doc = build_architecture_html(
        product=name,
        hld=hld,
        lld=lld if isinstance(lld, dict) else {},
        adrs=adrs if isinstance(adrs, list) else [],
        reqspec=req,
        capacity=capacity,
        perf_budget=budget,
        sequences=sequences,
    )
    lld_html = build_lld_html(
        product=name,
        lld=lld if isinstance(lld, dict) else {},
        hld=hld,
        reqspec=req,
    )
    publish(wf, task, "architecture_design_html", html_doc, bill=False)
    publish(wf, task, "lld_design_html", lld_html, bill=False)
    publish(
        wf,
        task,
        "hld_html",
        {"content_type": "text/html", "html": html_doc, "product": name},
        bill=False,
    )
    return html_doc


def _ensure_url_shortener_tenets(hld: dict[str, Any]) -> None:
    tenets = [str(t) for t in (hld.get("tenets") or [])]
    joined = " ".join(tenets).lower()
    have_readable = any(
        k in joined for k in ("base-58", "base58", "base62", "readab")
    )
    if "outbox" not in joined:
        tenets.append("outbox for mutations")
    if "cache" not in joined:
        tenets.append("cache-first redirect path")
    if "unpredict" not in joined and "guess" not in joined:
        tenets.append("unpredictable short codes (random ID from range)")
    if not have_readable:
        tenets.append("readable Base-58 encoding")
    hld["tenets"] = tenets

    if not hld.get("building_blocks"):
        hld["building_blocks"] = [
            "database",
            "sequencer",
            "base58_encoder",
            "load_balancer",
            "cache",
            "rate_limiter",
            "app_servers",
        ]
    if not hld.get("short_url_generator"):
        hld["short_url_generator"] = {
            "id_strategy": "64-bit sequencer ranges; random pick for unpredictability",
            "encoding": "Base-58",
            "min_code_length": 6,
            "custom_alias": "validate + uniqueness + mark decoded ID used",
        }
    if not hld.get("nfr_compliance"):
        hld["nfr_compliance"] = {
            "availability": [
                "replication of DB/cache/app",
                "Route 53 + multi-region failover",
                "rate limiters vs abuse",
                "S3 backups",
            ],
            "scalability": [
                "horizontal app scale on EKS/ECS",
                "DB sharding + consistent hashing",
                "64-bit ID space",
            ],
            "readability": ["Base-58 alphabet; no ambiguous URL-unsafe chars"],
            "latency": ["CloudFront + ElastiCache cache-first redirect", "async geo replication"],
            "unpredictability": ["random ID from unused range; optional salt"],
        }
    ctx = hld.setdefault("context", {})
    if not isinstance(ctx, dict):
        hld["context"] = {}
        ctx = hld["context"]
    ctx.setdefault(
        "actors",
        ["End users", "API clients (api_dev_key)", "Operators", "Admins"],
    )
    ctx.setdefault(
        "external",
        [
            "Amazon Route 53",
            "Amazon CloudFront",
            "AWS WAF",
            "Application Load Balancer",
            "Amazon ElastiCache",
            "Amazon DocumentDB / DynamoDB",
            "Amazon S3",
            "Amazon CloudWatch",
        ],
    )
    ctx.setdefault("regions", ["us-east-1 (primary)", "us-west-2 (DR / secondary)"])
    ctx.setdefault(
        "consistency",
        "Eventual consistency across regions OK (create → first redirect lag)",
    )
    ctx.setdefault(
        "request_flow",
        "Client → Route 53 → CloudFront → ALB → Rate limit → App (Link/Redirect/SUG) "
        "→ ElastiCache → DocumentDB/DynamoDB; backups → S3; analytics async via MSK",
    )
    if not ctx.get("layers"):
        name = str(hld.get("product") or "URL Shortener")
        comps = " ".join(str(c) for c in (hld.get("components") or [])).lower()
        analytics = "analytics" in comps or "kafka" in comps or "clickhouse" in comps
        ctx["layers"] = _default_aws_context_layers(name, analytics=analytics)
    ctx.setdefault(
        "aws_services",
        {
            "dns_gslb": "Amazon Route 53",
            "cdn": "Amazon CloudFront",
            "waf": "AWS WAF",
            "load_balancer": "Application Load Balancer",
            "rate_limiter": "API Gateway usage plans / app token bucket",
            "app": "Amazon EKS / ECS",
            "cache": "Amazon ElastiCache (Redis/Memcached)",
            "database": "Amazon DocumentDB (Mongo-compatible) or DynamoDB",
            "backups": "Amazon S3",
            "observability": "Amazon CloudWatch",
        },
    )


def _publish_llm_artifacts(
    wf: Workflow,
    task: TaskNode,
    llm: dict[str, Any],
) -> dict[str, Any]:
    hld = llm["hld"]
    if not isinstance(hld.get("product"), str) or not hld["product"].strip():
        hld["product"] = product_name(wf)
    if _is_url_shortener(wf):
        _ensure_url_shortener_tenets(hld)
    # Non-URL-shortener: do NOT inject redirect/short-code tenets.

    lld = llm.get("lld") or {}
    if _is_url_shortener(wf) and isinstance(lld, dict):
        lld = _merge_lld_defaults(product_name(wf), lld, hld)
    adrs = llm.get("adrs") or []
    capacity = llm.get("capacity_estimate") or {}
    budget = llm.get("perf_budget") or {}
    sequences = llm.get("sequence_diagrams") or {}
    publish(wf, task, "hld", hld, bill=False)
    publish(wf, task, "lld", lld, bill=False)
    publish(wf, task, "adrs", adrs, bill=False)
    publish(wf, task, "capacity_estimate", capacity, bill=False)
    publish(wf, task, "perf_budget", budget, bill=False)
    publish(wf, task, "sequence_diagrams", sequences, bill=False)
    publish(
        wf,
        task,
        "workload_dashboards",
        llm.get("workload_dashboards")
        or {"panels": ["latency_p99", "error_rate", "saturation"]},
        bill=False,
    )
    if _is_url_shortener(wf) and _load_hld_reference():
        publish(
            wf,
            task,
            "hld_reference_used",
            {"source": _HLD_REFERENCE_NAME, "trained": True},
            bill=False,
        )
    _publish_design_html(
        wf,
        task,
        hld=hld,
        lld=lld if isinstance(lld, dict) else {},
        adrs=adrs if isinstance(adrs, list) else [],
        capacity=capacity if isinstance(capacity, dict) else {},
        budget=budget if isinstance(budget, dict) else {},
        sequences=sequences if isinstance(sequences, dict) else {},
    )
    if "qr" in str(hld.get("components")).lower():
        wf.facts["feature_qr"] = True
    return {
        "summary": f"HLD/LLD/ADRs + HTML design via LLM for {product_name(wf)}",
        "mode": "llm",
        "hld_reference": bool(_is_url_shortener(wf) and _load_hld_reference()),
    }


def _heuristic_generic_hld(wf: Workflow) -> dict[str, Any]:
    """SRS-derived HLD when LLM is unavailable — never reuse TinyURL topology."""
    name = product_name(wf)
    req = art(wf, "reqspec") or {}
    domain = art(wf, "domain_model") or {}
    brief = art(wf, "product_brief") or {}
    summary = doc_summary(wf)
    entities: list[str] = []
    for src in (
        domain.get("entities"),
        (req.get("domain") or {}).get("entities") if isinstance(req.get("domain"), dict) else None,
        brief.get("entities") if isinstance(brief, dict) else None,
    ):
        if not isinstance(src, list):
            continue
        for e in src:
            if isinstance(e, str) and e.strip():
                entities.append(e.strip())
            elif isinstance(e, dict):
                label = e.get("name") or e.get("id") or e.get("entity")
                if label:
                    entities.append(str(label).strip())
    # Dedupe preserving order
    seen: set[str] = set()
    entities = [e for e in entities if not (e.lower() in seen or seen.add(e.lower()))]  # type: ignore[func-returns-value]

    components: list[str] = ["api-gateway"]
    for ent in entities[:10]:
        slug = "".join(ch if ch.isalnum() else "-" for ch in ent.lower()).strip("-")
        if slug:
            components.append(f"{slug}-service")
    frs = req.get("fr") or []
    fr_blob = " ".join(
        str(f.get("text") if isinstance(f, dict) else f) for f in frs
    ).lower()
    if "search" in fr_blob and "search-service" not in components:
        components.append("search-service")
    if any(k in fr_blob for k in ("permission", "acl", "auth", "role")):
        if "permission-service" not in components:
            components.append("permission-service")
    if "cache" in fr_blob or "performance" in fr_blob:
        components.append("cache")
    components.append("postgres")
    if not entities:
        components = [
            "api-gateway",
            "application-api",
            "domain-service",
            "postgres",
            "cache",
            "object-store",
        ]

    features = summary.get("features") or req.get("features_detected") or []
    hld = {
        "product": name,
        "style": "modular services derived from SRS domain model",
        "source_requirements": (requirement_text(wf) or "")[:800],
        "context": {
            "actors": ["End users", "API clients", "Operators"],
            "external": ["Identity provider", "Object storage", "Observability"],
            "request_flow": f"Client → API gateway → {name} services → datastore",
            "consistency": "Strong consistency within a session; eventual across replicas",
        },
        "components": components,
        "building_blocks": components,
        "tenets": [
            "ground architecture in uploaded SRS only",
            "clear service boundaries from domain entities",
            "validate authorization on mutating operations",
        ],
        "nfr_targets": {
            "availability": summary.get("availability") or "99.9%",
        },
        "features_in_scope": features,
        "diagrams": {
            "context": f"C4 context for {name}: clients → gateway → domain services → data",
            "deployment": "Containerized services, managed DB, cache, object storage",
        },
    }
    services = {
        c: {
            "handlers": ["HandleRequest"],
            "deps": ["postgres"],
            "invariants": ["authorize before mutate"],
        }
        for c in components
        if c.endswith("-service") or c.endswith("-api")
    }
    lld = {
        "product": name,
        "services": services or {
            "application-api": {
                "handlers": ["HandleRequest"],
                "deps": ["postgres", "cache"],
            }
        },
        "entities": entities,
        "consistency": "strong within aggregate; eventual for projections",
        "derived_from_fr": [
            f.get("id") for f in frs[:12] if isinstance(f, dict) and f.get("id")
        ],
    }
    adrs = [
        {
            "id": f"ADR-{name.upper()[:8]}-001",
            "decision": "Service boundaries follow SRS domain entities",
            "status": "proposed",
            "rationale": "Keep architecture aligned with the uploaded requirements",
        },
        {
            "id": f"ADR-{name.upper()[:8]}-002",
            "decision": "API gateway + modular backend services",
            "status": "proposed",
            "rationale": "Isolate domain capabilities for independent scale/deploy",
        },
    ]
    capacity = {"read_rps_target": 5_000, "write_rps_target": 500}
    budget = {"api_p99_ms": 200}
    sequences = {
        "primary": hld["diagrams"]["context"],
    }
    return {
        "hld": hld,
        "lld": lld,
        "adrs": adrs,
        "capacity": capacity,
        "budget": budget,
        "sequences": sequences,
        "components": components,
    }


def _heuristic_url_shortener_hld(wf: Workflow) -> dict[str, Any]:
    """Educative TinyURL-class HLD when LLM is unavailable."""
    name = product_name(wf)
    summary = doc_summary(wf)
    req = art(wf, "reqspec") or {}
    p99 = summary.get("redirect_p99_ms") or 50
    features = summary.get("features") or req.get("features_detected") or []

    components = [
        "dns-gslb",
        "load-balancer",
        "rate-limiter",
        "link-api",
        "redirect-api",
        "short-url-generator",
        "cache-cluster",
        "nosql-db",
        "object-storage-backup",
    ]
    analytics_on = has_feature(wf, "analytics") or "analytics" in features
    if analytics_on:
        components += ["analytics-api", "kafka", "flink", "clickhouse"]
    if has_feature(wf, "qr_code") or wf.facts.get("feature_qr"):
        components.append("qr-service")
        wf.facts["feature_qr"] = True

    context_layers = _default_aws_context_layers(name, analytics=analytics_on)

    hld = {
        "product": name,
        "style": "URL shortener HLD — sequencer + Base-58 + cache-first redirect",
        "reference": _HLD_REFERENCE_NAME,
        "source_requirements": (requirement_text(wf) or "")[:500],
        "context": {
            "actors": ["End users", "API clients (api_dev_key)", "Operators", "Admins"],
            "external": [
                "Amazon Route 53",
                "Amazon CloudFront",
                "AWS WAF",
                "Application Load Balancer",
                "Amazon ElastiCache",
                "Amazon DocumentDB / DynamoDB",
                "Amazon S3",
                "Amazon CloudWatch",
            ],
            "regions": ["us-east-1 (primary)", "us-west-2 (DR / secondary)"],
            "consistency": (
                "Eventual consistency across regions OK (create → first redirect lag)"
            ),
            "request_flow": (
                "Client → Route 53 → CloudFront → ALB → Rate limit → "
                f"{name} App (Link API / Redirect API / SUG) → ElastiCache → "
                "DocumentDB/DynamoDB; daily snapshots → S3; analytics async (MSK)"
            ),
            "aws_services": {
                "dns_gslb": "Amazon Route 53",
                "cdn": "Amazon CloudFront",
                "waf": "AWS WAF",
                "load_balancer": "Application Load Balancer",
                "tls": "AWS Certificate Manager",
                "rate_limiter": "API Gateway usage plans / app token bucket",
                "app": "Amazon EKS / ECS (multi-AZ)",
                "cache": "Amazon ElastiCache (Redis/Memcached)",
                "database": "Amazon DocumentDB (Mongo) or DynamoDB",
                "id_store": "DynamoDB / DocumentDB (used/unused ID ranges)",
                "backups": "Amazon S3",
                "secrets": "AWS Secrets Manager / SSM Parameter Store",
                "observability": "Amazon CloudWatch",
                "analytics_bus": "Amazon MSK / Kinesis (optional)",
            },
            "layers": context_layers,
        },
        "building_blocks": [
            "database",
            "sequencer",
            "base58_encoder",
            "load_balancer",
            "cache",
            "rate_limiter",
            "app_servers",
        ],
        "components": components,
        "tenets": [
            "cache-first redirect path",
            "redirect happy path must not wait on DB",
            "outbox for mutations / analytics side-effects",
            "unpredictable short codes (random ID from range)",
            "readable Base-58 encoding (no 0/O/I/l/+/)",
            "eventual geo consistency acceptable after create",
        ],
        "apis": {
            "shorten": "shortURL(api_dev_key, original_url, custom_alias?, expiry_date?)",
            "redirect": "redirectURL(url_key) → 302",
            "delete": "deleteURL(api_dev_key, url_key)",
            "update": "updateURL(api_dev_key, url_key, original_url) when AuthZ allows",
        },
        "short_url_generator": {
            "id_strategy": "64-bit sequencer; ranges per app server; random pick",
            "encoding": "Base-58",
            "min_code_length": 6,
            "max_code_length": 11,
            "sequencer_start": ">= 1e9 for min 6-char codes",
            "custom_alias": "validate ≤11 chars → uniqueness → decode Base-58 → mark ID used",
        },
        "workflows": {
            "shorten": (
                "Client→GSLB/LB→RateLimiter→App→SUG(ID+Base58)→DB persist→return short URL"
            ),
            "redirect": (
                "Client→LB→App→Cache hit? 302 : DB→populate cache→302 "
                "(never wait on analytics)"
            ),
            "delete": "AuthZ→delete mapping→cache invalidate; expiry job purges after retention",
            "custom_alias": (
                "Validate format→DB uniqueness→mark decoded ID used→map alias→long URL"
            ),
        },
        "caching_strategy": {
            "L1": "in-process (optional)",
            "L2": "DC-local Memcached/Redis cluster (hot 20% of redirects)",
            "invalidation": "delete/update/expire → invalidate key",
            "rule": "80/20 — cache top traffic mappings for latency",
        },
        "authn_z": {
            "api": "api_dev_key (hashed) + scopes",
            "rate_limiting": "fixed-window or token bucket per api_dev_key",
        },
        "scalability": (
            "Horizontal app servers; DB sharding + consistent hashing; "
            "NoSQL (or sharded SQL) for independent mappings; 64-bit ID space"
        ),
        "failure_recovery": "replica failover; cache soft TTL + singleflight on miss; outbox retry",
        "disaster_recovery": "daily DB/cache snapshots to object storage; GSLB regional failover",
        "nfr_targets": {
            "redirect_p99_ms": p99,
            "availability": summary.get("availability") or "99.99%",
            "cache_hit_ratio": 0.8,
        },
        "nfr_compliance": {
            "availability": [
                "DB/cache/app replication",
                "GSLB",
                "rate limiters",
                "S3-style backups",
            ],
            "scalability": [
                "horizontal scale",
                "shard + consistent hashing",
                "NoSQL or sharded SQL",
            ],
            "readability": ["Base-58; drop ambiguous and URL-unsafe chars"],
            "latency": ["cache-first redirect", "DC-local cache", "fast encode"],
            "unpredictability": ["random ID from unused pool; optional salt"],
        },
        "features_in_scope": features,
        "diagrams": {
            "context": (
                "Client → Route 53 → CloudFront → ALB → Rate limit → "
                f"{name} App (Link/Redirect/SUG) → ElastiCache → DocumentDB/DynamoDB "
                "→ S3 backups; analytics async via MSK"
            ),
            "deployment": (
                "Multi-AZ EKS/ECS + ElastiCache + DocumentDB/DynamoDB; "
                "Route 53 multi-region; S3 backups; CloudWatch"
            ),
            "sequence_redirect": (
                "CloudFront/ALB→redirect-api→ElastiCache→(miss)DocumentDB→populate→302"
            ),
        },
    }

    lld = _default_url_shortener_lld(name)
    lld["product"] = name
    lld["derived_from_fr"] = [f.get("id") for f in (req.get("fr") or [])[:12]]
    # Keep HLD readability option visible alongside Base62 default from LLD masterclass
    lld["encoding"]["base58_alternative"] = (
        "Base-58 alphabet (no 0/O/I/l) when PRD prioritizes readability — see HLD ADR"
    )
    if "analytics-api" in components:
        lld["services"]["analytics-api"] = {
            "handlers": ["GetLinkStats"],
            "deps": ["clickhouse"],
            "invariants": ["async only — off redirect path"],
        }
    if "qr-service" in components:
        lld["services"]["qr-service"] = {
            "handlers": ["RenderQr"],
            "deps": ["link-api"],
        }

    prefix = name.upper()[:8]
    adrs = [
        {
            "id": f"ADR-{prefix}-001",
            "decision": "64-bit sequencer + Base-58 short codes",
            "options": ["Base-62", "Base-64", "hash truncation", "Base-58"],
            "status": "proposed",
            "rationale": (
                "Base-58 improves readability (no 0/O/I/l/+/); "
                "random ID from range keeps codes unpredictable"
            ),
        },
        {
            "id": f"ADR-{prefix}-002",
            "decision": "NoSQL (Mongo-style) for short↔long mappings",
            "options": ["MongoDB", "Cassandra", "Postgres+Vitess"],
            "status": "proposed",
            "rationale": (
                "Independent mappings, read-heavy; leader-follower replicas; "
                "unique indexes prevent collisions. Prefer Vitess if PRD mandates SQL."
            ),
        },
        {
            "id": f"ADR-{prefix}-003",
            "decision": "DC-local cache (Memcached/Redis) on redirect path",
            "options": ["global cache", "DC-local cache", "CDN-only"],
            "status": "proposed",
            "rationale": f"Meet redirect p99 < {p99}ms; 80/20 hot set in cache",
        },
        {
            "id": f"ADR-{prefix}-004",
            "decision": "Transactional outbox for mutations / click side-effects",
            "status": "proposed",
            "rationale": "Avoid dual-write inconsistency; redirect never waits on analytics",
        },
        {
            "id": f"ADR-{prefix}-005",
            "decision": "GSLB + rate limit per api_dev_key",
            "status": "proposed",
            "rationale": "Availability across regions; protect create APIs from abuse",
        },
    ]

    # Reference-scale estimates; override when PRD supplies traffic
    capacity = {
        "assumptions": {
            "write_read_ratio": "1:100",
            "new_urls_per_month": "200M (reference; override from PRD)",
            "bytes_per_entry": 500,
            "retention_years": 5,
            "cache_rule": "80/20 of daily redirects",
        },
        "write_qps": 76,
        "read_qps": 7600,
        "redirect_rps_target": 7600,
        "create_rps_target": 76,
        "storage_tb": 6,
        "entries_5y": "12B",
        "cache_memory_gb": 66,
        "bandwidth": {"ingress_kbps": 304, "egress_mbps": 30.4},
        "method": "Back-of-envelope capacity model; recompute when PRD supplies traffic",
    }
    budget = {
        "redirect_p99_ms": p99,
        "create_p99_ms": 200,
        "cache_hit_ratio_target": 0.80,
        "analytics_freshness_s": 300,
    }
    sequences = {
        "redirect": hld["diagrams"]["sequence_redirect"],
        "create_link": (
            "Client→LB→RateLimiter→link-api→SUG→DB(+used ID)→201 short URL"
        ),
        "delete_link": "Client→LB→AuthZ→DB delete→cache invalidate→200",
        "custom_alias": "Client→validate→DB unique?→mark ID used→persist→201",
    }
    return {
        "hld": hld,
        "lld": lld,
        "adrs": adrs,
        "capacity": capacity,
        "budget": budget,
        "sequences": sequences,
        "components": components,
    }


def architecture_design(wf: Workflow, task: TaskNode) -> dict[str, Any]:
    """6.1 Architecture Agent — HLD/LLD/ADRs + proper HTML design page."""
    if task.id.startswith("feature.qr") or "qr" in task.id:
        design = {
            "service": "qr-service",
            "formats": ["png", "svg"],
            "endpoint": "GET /v1/links/{id}/qr",
        }
        publish(wf, task, "qr_design", design)
        wf.facts["feature_qr"] = True
        return {"summary": "QR feature design"}

    if task.id.startswith("analytics.") or (
        "analytics" in task.id and "refactor" in task.id
    ):
        plan = {
            "changes": [
                "split raw vs aggregate topics",
                "Flink windowed aggregations",
                "ClickHouse TTL 90d hot / S3 cold",
            ],
            "option": wf.facts.get("analytics_option", "A"),
        }
        publish(wf, task, "analytics_refactor_plan", plan)
        return {"summary": "Analytics refactor plan"}

    hld_ref = _load_hld_reference()
    lld_ref = _load_lld_reference()
    url_shortener = _is_url_shortener(wf)
    name = product_name(wf)
    req = art(wf, "reqspec") or {}
    domain = art(wf, "domain_model") or {}
    brief = art(wf, "product_brief") or {}
    srs_excerpt = (requirement_text(wf) or "")[:12000]

    if url_shortener:
        system_extra = (
            f"Design HLD/LLD for URL shortener product '{name}'. "
            "Tenets MUST include cache-first redirect and outbox for mutations. "
            "Redirect happy path must not wait on DB. "
            "Ground every component in the uploaded PRD."
        )
        if hld_ref or lld_ref:
            system_extra += (
                "\n\nURL SHORTENER MODE: Follow hld_reference for HLD and lld_reference for LLD. "
                "HLD: capacity, building blocks, AWS layers, workflows, NFR compliance. "
                "LLD MUST include: strategies_considered, id_generation, encoding, classes, "
                "entities, apis, cache, redirect_policy, write_flow, read_flow, services."
            )
        schema_hint = (
            '{"hld":{"product":"","style":"","components":[],"building_blocks":[],'
            '"tenets":["cache-first redirect path","outbox for mutations",'
            '"unpredictable short codes","readable Base-58 encoding"],'
            '"context":{"actors":[],"external":[],"regions":[],"consistency":"",'
            '"request_flow":"","aws_services":{},"layers":[{"title":"","tone":"",'
            '"nodes":[{"name":"","aws":""}]}]},"apis":{},'
            '"short_url_generator":{},"workflows":{},"caching_strategy":{},'
            '"nfr_targets":{},"nfr_compliance":{}},'
            '"lld":{"services":{},"classes":[],"entities":[],"apis":{},'
            '"id_generation":{},"encoding":{},"cache":{},"redirect_policy":{},'
            '"write_flow":[],"read_flow":[],"strategies_considered":[],"scaling":[]},'
            '"adrs":[{"id":"","decision":"","options":[],"rationale":""}],'
            '"capacity_estimate":{},"perf_budget":{},'
            '"sequence_diagrams":{},"workload_dashboards":{"panels":[]}}'
        )
    else:
        system_extra = (
            f"You are the Architecture Agent. Design a production HLD + LLD for product '{name}' "
            "STRICTLY from the uploaded SRS/PRD, reqspec, and domain_model.\n"
            "CRITICAL RULES:\n"
            "1. components[] MUST be services for THIS domain only (derive from domain entities + FRs).\n"
            "2. Do NOT reuse URL-shortener topology (no link-api, redirect-api, short URL generator, "
            "Base58/Base62, TinyURL, Snipr) unless the SRS is clearly a URL shortener.\n"
            "3. hld.product MUST equal the product name from the SRS.\n"
            "4. tenets/NFRs/request_flow must match THIS SRS — not a generic shortener playbook.\n"
            "5. LLD services/entities must map to reqspec FR ids and domain entities.\n"
            "6. Prefer OpenAI/structured JSON fidelity over templates."
        )
        schema_hint = (
            '{"hld":{"product":"","style":"","components":["api-gateway","<domain>-service"],'
            '"building_blocks":[],"tenets":["grounded in SRS"],'
            '"context":{"actors":[],"external":[],"consistency":"","request_flow":"",'
            '"layers":[{"title":"","tone":"","nodes":[{"name":"","aws":""}]}]},'
            '"apis":{},"nfr_targets":{},"workflows":{}},'
            '"lld":{"product":"","services":{},"classes":[],"entities":[],"apis":{},'
            '"scaling":[],"consistency":""},'
            '"adrs":[{"id":"","decision":"","options":[],"rationale":""}],'
            '"capacity_estimate":{},"perf_budget":{},'
            '"sequence_diagrams":{},"workload_dashboards":{"panels":[]}}'
        )

    llm = run_llm_agent(
        wf,
        task,
        agent="architecture",
        inputs={
            "product_name": name,
            "requirement_filename": wf.facts.get("requirement_filename"),
            "srs_text": srs_excerpt,
            "reqspec": req,
            "domain_model": domain,
            "product_brief": brief,
            "execution_plan": art(wf, "execution_plan"),
            "document_summary": doc_summary(wf),
            "is_url_shortener_domain": url_shortener,
            "hld_reference": hld_ref[:50000] if (url_shortener and hld_ref) else None,
            "hld_reference_source": _HLD_REFERENCE_NAME if url_shortener else None,
            "lld_reference": lld_ref[:50000] if (url_shortener and lld_ref) else None,
            "lld_reference_source": _LLD_REFERENCE_NAME if url_shortener else None,
        },
        schema_hint=schema_hint,
        system_extra=system_extra,
    )
    if llm and isinstance(llm.get("hld"), dict):
        return _publish_llm_artifacts(wf, task, llm)

    # LLM unavailable / failed — domain-specific heuristics only
    if url_shortener:
        built = _heuristic_url_shortener_hld(wf)
    else:
        built = _heuristic_generic_hld(wf)
    hld = built["hld"]
    lld = built["lld"]
    adrs = built["adrs"]
    capacity = built["capacity"]
    budget = built["budget"]
    sequences = built["sequences"]
    components = built["components"]

    publish(wf, task, "hld", hld)
    publish(wf, task, "lld", lld)
    publish(wf, task, "adrs", adrs)
    publish(wf, task, "capacity_estimate", capacity)
    publish(wf, task, "perf_budget", budget)
    publish(wf, task, "sequence_diagrams", sequences)
    publish(
        wf,
        task,
        "workload_dashboards",
        {
            "panels": (
                ["redirect_p99", "cache_hit_ratio", "error_rate", "kafka_lag"]
                if url_shortener
                else ["latency_p99", "error_rate", "saturation"]
            )
        },
    )
    if url_shortener and hld_ref:
        publish(
            wf,
            task,
            "hld_reference_used",
            {"source": _HLD_REFERENCE_NAME, "trained": True, "mode": "heuristic"},
            bill=False,
        )
    _publish_design_html(
        wf,
        task,
        hld=hld,
        lld=lld,
        adrs=adrs,
        capacity=capacity,
        budget=budget,
        sequences=sequences,
    )
    return {
        "summary": (
            f"HLD/LLD/ADRs + HTML design for {name} "
            f"({len(components)} components; "
            f"{'TinyURL reference heuristic' if url_shortener else 'SRS-derived heuristic — set FORGE_LLM_API_KEY for OpenAI HLD'})"
        ),
        "mode": "heuristic",
    }
