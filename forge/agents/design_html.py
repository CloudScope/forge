"""Build real HTML design artifacts from workflow engineering data (no dummy placeholders)."""

from __future__ import annotations

import html
import json
from typing import Any, Optional


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


# Forge Studio theme tokens (match dashboard / 6. API swagger chrome)
_FORGE_THEME_ROOT = """
  :root {
    --bg0:#0e1419; --bg1:#152028; --bg2:#1c2b35; --line:#2a3f4d;
    --text:#e6eef2; --muted:#8aa0ad; --accent:#3ecf8e; --info:#5eb1e8;
    --panel:#152028; --soft:#243642;
  }
"""

_FORGE_HEADER_CSS = """
  header {
    padding: 1.6rem 1.4rem 1.25rem;
    border-bottom: 1px solid var(--line);
    background:
      radial-gradient(700px 180px at 0% 0%, rgba(62,207,142,.18), transparent 55%),
      radial-gradient(500px 160px at 100% 0%, rgba(94,177,232,.12), transparent 50%),
      linear-gradient(180deg, #1c2b35, #152028);
    color: var(--text);
  }
  header h1 { margin: 0; font-size: clamp(1.45rem, 2.8vw, 2.1rem); letter-spacing: -0.02em; color: var(--text); }
  header p { margin: 0.45rem 0 0; color: var(--muted); max-width: 62ch; }
"""


def _mermaid_safe_id(node_id: str) -> str:
    """Mermaid node ids: letters, digits, underscore only."""
    safe = "".join(ch if ch.isalnum() else "_" for ch in str(node_id))
    if not safe or safe[0].isdigit():
        safe = f"n_{safe}"
    return safe


def _plain_label(*parts: Any) -> str:
    """Single-line label safe for Mermaid 10 (no HTML, no dots, no quotes)."""
    cleaned: list[str] = []
    for p in parts:
        if p is None or p == "":
            continue
        text = str(p).replace(".", " ").replace('"', "").replace("'", "")
        text = text.replace("\n", " ").replace("\\n", " ").replace("<", "").replace(">", "")
        text = text.replace("#", "").replace(";", " ").replace("(", "").replace(")", "")
        text = " ".join(text.split())
        if text:
            cleaned.append(text)
    return " | ".join(cleaned) if cleaned else "node"


def build_mermaid_flowchart(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> str:
    """
    Ultra-conservative Mermaid 10 flowchart.

    Only rectangles + simple arrows. No stadium/subroutine/hexagon, no <br/>,
    no class/classDef (those combinations frequently throw 'Syntax error in text').
    """
    lines = ["flowchart TB"]
    known: set[str] = set()
    for n in nodes:
        nid = str(n.get("id") or "")
        safe = _mermaid_safe_id(nid)
        known.add(safe)
        ntype = (n.get("type") or "COMPUTE").upper()
        prefix = {"APPROVAL": "GATE ", "BARRIER": "SYNC "}.get(ntype, "")
        label = _plain_label(prefix + nid, n.get("status"))
        lines.append(f"  {safe}[{label}]")
    for e in edges:
        frm = _mermaid_safe_id(str(e.get("from") or ""))
        to = _mermaid_safe_id(str(e.get("to") or ""))
        if frm and to and frm in known and to in known:
            lines.append(f"  {frm} --> {to}")
    return "\n".join(lines) + "\n"


def _topo_layers(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]]
) -> list[list[dict[str, Any]]]:
    """Layer nodes by longest-path depth for an HTML DAG layout."""
    by_id = {str(n.get("id")): n for n in nodes if n.get("id")}
    deps: dict[str, set[str]] = {i: set() for i in by_id}
    for e in edges:
        frm, to = str(e.get("from") or ""), str(e.get("to") or "")
        if frm in by_id and to in by_id:
            deps[to].add(frm)
    depth: dict[str, int] = {}

    def _depth(nid: str, stack: set[str]) -> int:
        if nid in depth:
            return depth[nid]
        if nid in stack:
            return 0
        stack.add(nid)
        d = 0
        for p in deps.get(nid, ()):
            d = max(d, _depth(p, stack) + 1)
        stack.discard(nid)
        depth[nid] = d
        return d

    for nid in by_id:
        _depth(nid, set())
    if not depth:
        return [nodes] if nodes else []
    max_d = max(depth.values())
    layers: list[list[dict[str, Any]]] = [[] for _ in range(max_d + 1)]
    for nid, d in sorted(depth.items(), key=lambda kv: (kv[1], kv[0])):
        layers[d].append(by_id[nid])
    return layers


def _status_class(status: Any) -> str:
    st = (str(status or "")).upper()
    return {
        "SUCCEEDED": "ok",
        "FAILED": "fail",
        "RUNNING": "run",
        "READY": "run",
        "WAITING_APPROVAL": "wait",
        "COMPENSATED": "comp",
        "SKIPPED": "skip",
    }.get(st, "pending")


def _render_html_dag(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]]
) -> str:
    """Pure HTML/CSS DAG — no Mermaid dependency (avoids CDN parse errors)."""
    layers = _topo_layers(nodes, edges)
    parts: list[str] = ['<div class="html-dag" role="img" aria-label="Dependency DAG">']
    for i, layer in enumerate(layers):
        if i:
            parts.append('<div class="dag-connector" aria-hidden="true">↓</div>')
        parts.append('<div class="dag-layer">')
        for n in layer:
            ntype = (n.get("type") or "COMPUTE").upper()
            st = _status_class(n.get("status"))
            shape = {
                "APPROVAL": "shape-gate",
                "BARRIER": "shape-sync",
            }.get(ntype, "shape-task")
            parts.append(
                f'<div class="dag-node {shape} st-{st}">'
                f'<div class="dag-id">{_esc(n.get("id"))}</div>'
                f'<div class="dag-meta">{_esc(n.get("agent") or "")}'
                f' · {_esc(n.get("type") or "COMPUTE")}</div>'
                f'<div class="dag-status">{_esc(n.get("status") or "—")}</div>'
                f"</div>"
            )
        parts.append("</div>")
        if len(layer) > 1:
            parts.append(
                f'<div class="dag-wave-note">∥ parallel wave · {len(layer)} nodes</div>'
            )
    if not layers:
        parts.append('<p class="muted">No task nodes yet</p>')
    parts.append("</div>")
    return "\n".join(parts)


def build_dag_html(
    *,
    product: str,
    workflow_id: str,
    playbook_id: str = "",
    status: str = "",
    mermaid: str = "",
    nodes: Optional[list[dict[str, Any]]] = None,
    edges: Optional[list[dict[str, Any]]] = None,
    gates: Optional[list[dict[str, Any]]] = None,
    parallel_waves: Optional[list[dict[str, Any]]] = None,
) -> str:
    """Self-contained Dependency DAG HTML with live Mermaid rendering."""
    nodes = nodes or []
    edges = edges or []
    gates = gates or []
    parallel_waves = parallel_waves or []

    # Always rebuild from nodes when available — avoids stale/broken mermaid.
    if nodes:
        mermaid = build_mermaid_flowchart(nodes, edges)
    elif not mermaid:
        mermaid = "flowchart TB\n  empty[\"No tasks yet\"]\n"

    by_type: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for n in nodes:
        t = (n.get("type") or "COMPUTE").upper()
        by_type[t] = by_type.get(t, 0) + 1
        st = (n.get("status") or "UNKNOWN").upper()
        by_status[st] = by_status.get(st, 0) + 1

    node_rows = []
    for n in nodes:
        node_rows.append(
            "<tr>"
            f"<td><code>{_esc(n.get('id'))}</code></td>"
            f"<td>{_esc(n.get('agent'))}</td>"
            f"<td><span class='pill type-{_esc((n.get('type') or 'COMPUTE').lower())}'>"
            f"{_esc(n.get('type') or 'COMPUTE')}</span></td>"
            f"<td><span class='pill risk'>{_esc(n.get('risk_tier') or 'LOW')}</span></td>"
            f"<td><span class='pill st-{(n.get('status') or 'pending').lower()}'>"
            f"{_esc(n.get('status') or '—')}</span></td>"
            f"<td>{_esc(n.get('description') or '')}</td>"
            "</tr>"
        )
    if not node_rows:
        node_rows.append("<tr><td colspan='6' class='muted'>No task nodes yet</td></tr>")

    gate_items = []
    for g in gates:
        gate_items.append(
            f"<li><strong>{_esc(g.get('id'))}</strong> "
            f"<span class='muted'>({_esc(g.get('kind'))})</span> — {_esc(g.get('purpose'))}</li>"
        )
    if not gate_items:
        gate_items.append("<li class='muted'>Gates inferred from APPROVAL / BARRIER nodes</li>")

    wave_items = []
    for index, w in enumerate(parallel_waves, start=1):
        # Waves may come from an LLM: render whatever shape arrives rather than
        # letting a malformed artifact break workflow finalization.
        if not isinstance(w, dict):
            members = w if isinstance(w, (list, tuple)) else [w]
            agents = ", ".join(str(m) for m in members)
            wave_items.append(
                f"<li><strong>wave {index}</strong> → {_esc(agents)}</li>"
            )
            continue
        agents = ", ".join(str(a) for a in (w.get("agents") or []))
        wave_items.append(
            f"<li><strong>{_esc(w.get('wave') or f'wave {index}')}</strong> after "
            f"<code>{_esc(w.get('after'))}</code> → {_esc(agents)} "
            f"sync <code>{_esc(w.get('sync'))}</code></li>"
        )
    if not wave_items:
        wave_items.append("<li class='muted'>Parallel waves derived at runtime from ready-set</li>")

    stat_chips = "".join(
        f"<span class='chip'>{_esc(k)} {_esc(v)}</span>" for k, v in sorted(by_status.items())
    ) or "<span class='muted'>No status yet</span>"
    type_chips = "".join(
        f"<span class='chip'>{_esc(k)} {_esc(v)}</span>" for k, v in sorted(by_type.items())
    )

    html_dag = _render_html_dag(nodes, edges)
    mermaid_esc = _esc(mermaid)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{_esc(product)} — Dependency DAG</title>
<style>
  :root {{
    --bg: #f4f7fb; --panel: #ffffff; --ink: #0f172a; --muted: #64748b;
    --line: #e2e8f0; --accent: #0f766e;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; font-family: "Source Sans 3", "Segoe UI", system-ui, sans-serif;
    color: var(--ink); background:
      radial-gradient(1200px 500px at 10% -10%, #ccfbf1 0%, transparent 55%),
      radial-gradient(900px 400px at 100% 0%, #dbeafe 0%, transparent 50%),
      var(--bg);
  }}
  header {{
    padding: 1.6rem 1.8rem 1.1rem; border-bottom: 1px solid var(--line);
    background: linear-gradient(180deg, rgba(255,255,255,.92), rgba(255,255,255,.7));
  }}
  h1 {{ margin: 0 0 .35rem; font-size: 1.65rem; letter-spacing: -0.02em; }}
  .sub {{ color: var(--muted); font-size: .95rem; }}
  .meta {{ display: flex; flex-wrap: wrap; gap: .45rem; margin-top: .85rem; }}
  .chip {{
    display: inline-flex; align-items: center; gap: .3rem;
    padding: .28rem .55rem; border-radius: 999px; background: #ecfeff;
    border: 1px solid #99f6e4; color: #115e59; font-size: .78rem; font-weight: 600;
  }}
  main {{ padding: 1.2rem 1.8rem 2.5rem; display: grid; gap: 1rem; }}
  section {{
    background: var(--panel); border: 1px solid var(--line); border-radius: 16px;
    padding: 1rem 1.15rem 1.2rem; box-shadow: 0 10px 30px rgba(15, 23, 42, .04);
  }}
  h2 {{ margin: 0 0 .75rem; font-size: 1.05rem; }}
  .legend {{ display: flex; flex-wrap: wrap; gap: .5rem; margin-bottom: .8rem; font-size: .8rem; }}
  .legend span {{ display: inline-flex; align-items: center; gap: .35rem; color: var(--muted); }}
  .sw {{ width: .7rem; height: .7rem; border-radius: 3px; border: 1px solid rgba(0,0,0,.15); }}
  .sw.ok {{ background: #dcfce7; }} .sw.fail {{ background: #fee2e2; }}
  .sw.run {{ background: #dbeafe; }} .sw.wait {{ background: #fef3c7; }}
  .sw.comp {{ background: #f3e8ff; }} .sw.skip {{ background: #f1f5f9; }}
  .sw.approval {{ background: #ffedd5; border-radius: 999px; }}
  .sw.barrier {{ background: #e2e8f0; width: 1rem; border-radius: 2px; }}
  .html-dag {{ padding: .4rem .2rem 1rem; }}
  .dag-layer {{
    display: flex; flex-wrap: wrap; gap: .65rem; justify-content: center; align-items: stretch;
  }}
  .dag-connector {{
    text-align: center; color: #94a3b8; font-size: 1.1rem; line-height: 1.4; margin: .15rem 0;
  }}
  .dag-wave-note {{
    text-align: center; font-size: .72rem; color: var(--accent); font-weight: 700;
    margin: .2rem 0 .55rem; letter-spacing: .02em;
  }}
  .dag-node {{
    min-width: 148px; max-width: 220px; padding: .55rem .7rem;
    border: 1px solid var(--line); border-radius: 12px; background: #fff;
    box-shadow: 0 4px 14px rgba(15,23,42,.05);
  }}
  .dag-node.shape-gate {{ border-radius: 999px; border-color: #fdba74; background: #fff7ed; }}
  .dag-node.shape-sync {{ border-radius: 4px; border-color: #94a3b8; background: #f8fafc; }}
  .dag-node.st-ok {{ border-color: #86efac; background: #f0fdf4; }}
  .dag-node.st-fail {{ border-color: #fca5a5; background: #fef2f2; }}
  .dag-node.st-run {{ border-color: #93c5fd; background: #eff6ff; }}
  .dag-node.st-wait {{ border-color: #fcd34d; background: #fffbeb; }}
  .dag-node.st-comp {{ border-color: #d8b4fe; background: #faf5ff; }}
  .dag-node.st-skip {{ border-color: #cbd5e1; background: #f8fafc; opacity: .85; }}
  .dag-id {{ font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: .78rem; font-weight: 700; }}
  .dag-meta {{ color: var(--muted); font-size: .72rem; margin-top: .2rem; }}
  .dag-status {{ margin-top: .35rem; font-size: .7rem; font-weight: 700; letter-spacing: .03em; }}
  table {{ width: 100%; border-collapse: collapse; font-size: .86rem; }}
  th, td {{ text-align: left; padding: .45rem .4rem; border-bottom: 1px solid var(--line); vertical-align: top; }}
  th {{ color: var(--muted); font-weight: 600; font-size: .75rem; text-transform: uppercase; letter-spacing: .04em; }}
  code {{ font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: .8rem; }}
  .pill {{
    display: inline-block; padding: .12rem .4rem; border-radius: 999px;
    font-size: .72rem; font-weight: 700; border: 1px solid var(--line); background: #f8fafc;
  }}
  .type-approval {{ background: #ffedd5; border-color: #fdba74; color: #9a3412; }}
  .type-barrier {{ background: #e2e8f0; border-color: #94a3b8; color: #334155; }}
  .type-compute {{ background: #ecfeff; border-color: #5eead4; color: #0f766e; }}
  .st-succeeded {{ background: #dcfce7; color: #166534; }}
  .st-failed {{ background: #fee2e2; color: #991b1b; }}
  .st-running, .st-ready {{ background: #dbeafe; color: #1d4ed8; }}
  .st-waiting_approval {{ background: #fef3c7; color: #92400e; }}
  .st-compensated {{ background: #f3e8ff; color: #6b21a8; }}
  .muted {{ color: var(--muted); }}
  ul {{ margin: .2rem 0 0; padding-left: 1.1rem; }}
  li {{ margin: .25rem 0; }}
  .cols {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }}
  details.mmd {{ margin-top: .6rem; }}
  details.mmd pre {{
    overflow: auto; background: #0f172a; color: #e2e8f0; padding: .8rem 1rem;
    border-radius: 10px; font-size: .72rem; line-height: 1.45;
  }}
  @media (max-width: 900px) {{ .cols {{ grid-template-columns: 1fr; }} main, header {{ padding-left: 1rem; padding-right: 1rem; }} }}
</style>
</head>
<body>
<header>
  <h1>{_esc(product)} · Dependency DAG</h1>
  <div class="sub">Workflow <code>{_esc(workflow_id)}</code>
    · playbook <code>{_esc(playbook_id or '—')}</code>
    · status <strong>{_esc(status or '—')}</strong>
    · {len(nodes)} nodes · {len(edges)} edges</div>
  <div class="meta">{type_chips} {stat_chips}</div>
</header>
<main>
  <section>
    <h2>Orchestration graph</h2>
    <div class="legend">
      <span><i class="sw approval"></i> Approval gate</span>
      <span><i class="sw barrier"></i> Sync barrier</span>
      <span><i class="sw ok"></i> Succeeded</span>
      <span><i class="sw run"></i> Running / ready</span>
      <span><i class="sw wait"></i> Waiting approval</span>
      <span><i class="sw fail"></i> Failed</span>
      <span><i class="sw skip"></i> Skipped</span>
      <span><i class="sw comp"></i> Compensated</span>
    </div>
    {html_dag}
    <details class="mmd">
      <summary class="muted">Mermaid source (export)</summary>
      <pre>{mermaid_esc}</pre>
    </details>
  </section>
  <div class="cols">
    <section>
      <h2>Gates</h2>
      <ul>{"".join(gate_items)}</ul>
    </section>
    <section>
      <h2>Parallel waves</h2>
      <ul>{"".join(wave_items)}</ul>
    </section>
  </div>
  <section>
    <h2>Task nodes</h2>
    <div style="overflow:auto">
      <table>
        <thead><tr><th>Id</th><th>Agent</th><th>Type</th><th>Risk</th><th>Status</th><th>Description</th></tr></thead>
        <tbody>{"".join(node_rows)}</tbody>
      </table>
    </div>
  </section>
</main>
</body>
</html>
"""


def _list_items(items: list[Any] | None, empty: str = "None specified in requirements") -> str:
    if not items:
        return f"<li class='muted'>{_esc(empty)}</li>"
    out = []
    for item in items:
        if isinstance(item, dict):
            label = item.get("id") or item.get("name") or item.get("decision") or item.get("text")
            detail = item.get("text") or item.get("rationale") or item.get("acceptance") or ""
            out.append(f"<li><strong>{_esc(label)}</strong> {_esc(detail)}</li>")
        else:
            out.append(f"<li>{_esc(item)}</li>")
    return "\n".join(out)


def _chip_row(items: list[Any] | None) -> str:
    if not items:
        return "<span class='muted'>No components derived yet</span>"
    return "\n".join(f"<span class='chip'>{_esc(c)}</span>" for c in items)


def _default_aws_context_layers(product: str, *, analytics: bool = False) -> list[dict[str, Any]]:
    """Educative TinyURL HLD mapped onto AWS services for System context."""
    layers: list[dict[str, Any]] = [
        {
            "id": "clients",
            "title": "Clients",
            "tone": "actor",
            "nodes": [
                {"name": "Web / Mobile browsers", "aws": "HTTPS clients"},
                {"name": "API clients", "aws": "api_dev_key"},
                {"name": "Operators / Admins", "aws": "IAM users/roles"},
            ],
        },
        {
            "id": "edge",
            "title": "Edge & global traffic",
            "tone": "edge",
            "nodes": [
                {"name": "DNS / GSLB", "aws": "Amazon Route 53"},
                {"name": "CDN (hot short links)", "aws": "Amazon CloudFront"},
                {"name": "WAF / bot filter", "aws": "AWS WAF"},
            ],
        },
        {
            "id": "ingress",
            "title": "Regional ingress",
            "tone": "ingress",
            "nodes": [
                {"name": "Local load balancer", "aws": "Application Load Balancer"},
                {"name": "Rate limiter", "aws": "API Gateway usage plans / token bucket"},
                {"name": "TLS termination", "aws": "ACM certificates"},
            ],
        },
        {
            "id": "app",
            "title": f"{product} application tier",
            "tone": "app",
            "nodes": [
                {"name": "Link API (shorten/update/delete)", "aws": "Amazon EKS / ECS"},
                {"name": "Redirect API (cache-first)", "aws": "Amazon EKS / ECS"},
                {"name": "Short URL Generator", "aws": "SUG on app pods (sequencer + Base-58)"},
                {"name": "Auto scaling", "aws": "HPA / ECS Service Auto Scaling"},
            ],
        },
        {
            "id": "data",
            "title": "Data & cache plane",
            "tone": "data",
            "nodes": [
                {"name": "Hot URL cache (DC-local)", "aws": "Amazon ElastiCache (Redis/Memcached)"},
                {"name": "Short↔long mappings + users", "aws": "Amazon DocumentDB (Mongo) / DynamoDB"},
                {"name": "Used/unused ID ranges", "aws": "DynamoDB / DocumentDB"},
                {"name": "Multi-AZ replicas", "aws": "Multi-AZ + read replicas"},
            ],
        },
        {
            "id": "platform",
            "title": "Platform, DR & ops",
            "tone": "platform",
            "nodes": [
                {"name": "Daily backups / snapshots", "aws": "Amazon S3"},
                {"name": "Secrets & config", "aws": "AWS Secrets Manager / SSM"},
                {"name": "Metrics / logs / alarms", "aws": "Amazon CloudWatch"},
                {"name": "Multi-region failover", "aws": "Route 53 health checks"},
            ],
        },
    ]
    if analytics:
        layers.append(
            {
                "id": "analytics",
                "title": "Analytics (async — off redirect path)",
                "tone": "analytics",
                "nodes": [
                    {"name": "Click / outbox events", "aws": "Amazon MSK (Kafka) / Kinesis"},
                    {"name": "Stream processing", "aws": "Apache Flink on Kinesis Analytics / EKS"},
                    {"name": "Aggregates warehouse", "aws": "ClickHouse on EC2/EKS or Redshift"},
                ],
            }
        )
    return layers


def _normalize_context_layers(
    product: str,
    context: dict[str, Any],
    hld: dict[str, Any],
) -> list[dict[str, Any]]:
    raw = context.get("layers") or hld.get("context_layers") or []
    if isinstance(raw, list) and raw:
        layers: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            nodes = item.get("nodes") or item.get("services") or []
            norm_nodes = []
            for n in nodes:
                if isinstance(n, dict):
                    norm_nodes.append(
                        {
                            "name": n.get("name") or n.get("component") or "service",
                            "aws": n.get("aws") or n.get("service") or n.get("detail") or "",
                        }
                    )
                else:
                    norm_nodes.append({"name": str(n), "aws": ""})
            layers.append(
                {
                    "id": item.get("id") or item.get("title") or "layer",
                    "title": item.get("title") or item.get("id") or "Layer",
                    "tone": item.get("tone") or "app",
                    "nodes": norm_nodes,
                }
            )
        if layers:
            return layers

    comps = " ".join(str(c) for c in (hld.get("components") or [])).lower()
    analytics = "analytics" in comps or "kafka" in comps or "clickhouse" in comps
    aws_map = context.get("aws_services") or hld.get("aws_services") or {}
    layers = _default_aws_context_layers(product, analytics=analytics)
    if isinstance(aws_map, dict) and aws_map:
        # Overlay explicit aws_services labels onto matching node names when provided
        flat = {str(k).lower(): str(v) for k, v in aws_map.items()}
        for layer in layers:
            for node in layer["nodes"]:
                key = str(node["name"]).lower()
                for fk, fv in flat.items():
                    if fk in key or key in fk:
                        node["aws"] = fv
    return layers


def _hld_looks_url_shortener(product: str, hld: dict[str, Any] | None) -> bool:
    blob = " ".join(
        [
            product or "",
            str((hld or {}).get("product") or ""),
            str((hld or {}).get("style") or ""),
            " ".join(str(c) for c in ((hld or {}).get("components") or [])),
            " ".join(str(c) for c in ((hld or {}).get("building_blocks") or [])),
            str(((hld or {}).get("diagrams") or {}).get("sequence_redirect") or ""),
        ]
    ).lower()
    # Explicit non-shortener domains — never show TinyURL topology / Short URL generator.
    if any(
        tok in blob
        for tok in (
            "file system",
            "filesystem",
            "object-oriented file",
            "file management",
            "directory management",
            "inode",
            "metadata management",
            "permission management",
        )
    ) and not any(tok in blob for tok in ("url short", "short url", "tinyurl", "bitly", "redirect-api")):
        return False
    hits = sum(
        1
        for tok in (
            "redirect-api",
            "link-api",
            "short url",
            "short-url",
            "tinyurl",
            "base58",
            "base62",
            "short_url_generator",
            "sequencer",
        )
        if tok in blob
    )
    return hits >= 2


def _render_generic_hld_topology(product: str, hld: dict[str, Any] | None) -> str:
    """Component topology from architecture HLD — works for any SRS domain."""
    p = _esc(product)
    comps = [str(c) for c in ((hld or {}).get("components") or []) if c]
    if not comps:
        comps = [str(c) for c in ((hld or {}).get("building_blocks") or []) if c]
    if not comps:
        comps = ["api-gateway", "application-service", "datastore"]
    # Cap for readable SVG
    comps = comps[:10]
    width = max(1180, 160 + len(comps) * 170)
    nodes = []
    # Client node
    nodes.append(("Client", 40, "#3ecf8e"))
    x = 200
    for c in comps:
        nodes.append((c, x, "#3ecf8e"))
        x += 170
    node_svg = []
    for i, (label, nx, stroke) in enumerate(nodes):
        short = label if len(label) <= 22 else label[:20] + "…"
        node_svg.append(
            f"""
        <g class="topo-node topo-primary" filter="url(#topo-soft)">
          <rect x="{nx}" y="160" width="140" height="88" rx="10" fill="#152028" stroke="{stroke}" stroke-width="1.6"/>
          <text x="{nx + 70}" y="200" text-anchor="middle" fill="#e6eef2" font-size="12" font-weight="700" font-family="DM Sans,sans-serif">{_esc(short)}</text>
          <text x="{nx + 70}" y="222" text-anchor="middle" fill="#8aa0ad" font-size="10" font-family="DM Sans,sans-serif">HLD component</text>
        </g>"""
        )
        if i < len(nodes) - 1:
            x1 = nx + 140
            x2 = nodes[i + 1][1]
            node_svg.append(
                f'<path d="M{x1},204 H{x2}" fill="none" stroke="#3a5160" stroke-width="1.6" marker-end="url(#topo-mk)"/>'
            )
    return f"""
    <div class="topo-wrap" aria-label="Animated HLD component topology">
      <div class="flow-diagram-head">
        <div>
          <h3>{p} — HLD component topology</h3>
          <p class="flow-sub">Derived from this product's architecture components (SRS-grounded)</p>
        </div>
        <div class="flow-legend">
          <span class="leg write"><i></i> Primary path</span>
        </div>
      </div>
      <svg class="topo-svg" viewBox="0 0 {width} 360" role="img">
        <defs>
          <marker id="topo-mk" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto">
            <path d="M0,0 L7,3 L0,6 Z" fill="#3ecf8e"/>
          </marker>
          <filter id="topo-soft" x="-20%" y="-20%" width="140%" height="140%">
            <feDropShadow dx="0" dy="2" stdDeviation="2" flood-color="#000" flood-opacity="0.35"/>
          </filter>
        </defs>
        {"".join(node_svg)}
        <text x="40" y="300" fill="#8aa0ad" font-size="12" font-family="DM Sans,sans-serif">
          Topology built from HLD.components for {_esc(product)} — not a shared template
        </text>
      </svg>
    </div>"""


def _render_hld_topology_diagram(product: str, hld: dict[str, Any] | None = None) -> str:
    """HLD topology: URL-shortener classic diagram only when HLD is shortener-shaped."""
    if _hld_looks_url_shortener(product, hld):
        return _render_url_shortener_topology(product)
    return _render_generic_hld_topology(product, hld)


def _render_url_shortener_topology(product: str) -> str:
    """Classic URL-shortener HLD topology (LB → web → app → cache/DB/SUG) with motion."""
    p = _esc(product)
    return f"""
    <div class="topo-wrap" aria-label="Animated HLD component topology">
      <div class="flow-diagram-head">
        <div>
          <h3>{p} — HLD component topology</h3>
          <p class="flow-sub">Request hops animate left→right · side services pulse on use · AWS labels under each node</p>
        </div>
        <div class="flow-legend">
          <span class="leg write"><i></i> Primary path</span>
          <span class="leg read"><i></i> Side services</span>
        </div>
      </div>
      <svg class="topo-svg" viewBox="0 0 1180 420" role="img">
        <defs>
          <marker id="topo-mk" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto">
            <path d="M0,0 L7,3 L0,6 Z" fill="#3ecf8e"/>
          </marker>
          <marker id="topo-mk-bi" markerWidth="8" markerHeight="8" refX="1" refY="3" orient="auto">
            <path d="M7,0 L0,3 L7,6 Z" fill="#3ecf8e"/>
          </marker>
          <linearGradient id="topo-glow" x1="0" y1="0" x2="1" y2="0">
            <stop offset="0%" stop-color="#3ecf8e" stop-opacity="0"/>
            <stop offset="50%" stop-color="#3ecf8e" stop-opacity="0.9"/>
            <stop offset="100%" stop-color="#3ecf8e" stop-opacity="0"/>
          </linearGradient>
          <filter id="topo-soft" x="-20%" y="-20%" width="140%" height="140%">
            <feDropShadow dx="0" dy="2" stdDeviation="2" flood-color="#000" flood-opacity="0.35"/>
          </filter>
        </defs>

        <!-- Side: Rate limiter (above web) -->
        <g class="topo-node topo-side" data-node="rate" filter="url(#topo-soft)">
          <rect x="310" y="28" width="150" height="78" rx="10" fill="#152028" stroke="#5eb1e8" stroke-width="1.5"/>
          <text x="385" y="58" text-anchor="middle" fill="#e6eef2" font-size="13" font-weight="700" font-family="DM Sans,sans-serif">Rate limiter</text>
          <text x="385" y="78" text-anchor="middle" fill="#5eb1e8" font-size="10" font-family="IBM Plex Mono,monospace">API GW / token bucket</text>
          <text x="385" y="94" text-anchor="middle" fill="#8aa0ad" font-size="10" font-family="DM Sans,sans-serif">per api_dev_key</text>
        </g>

        <!-- Side: Cache (above app) -->
        <g class="topo-node topo-side" data-node="cache" filter="url(#topo-soft)">
          <rect x="560" y="28" width="140" height="78" rx="10" fill="#152028" stroke="#5eb1e8" stroke-width="1.5"/>
          <text x="630" y="58" text-anchor="middle" fill="#e6eef2" font-size="13" font-weight="700" font-family="DM Sans,sans-serif">Cache</text>
          <text x="630" y="78" text-anchor="middle" fill="#5eb1e8" font-size="10" font-family="IBM Plex Mono,monospace">Amazon ElastiCache</text>
          <text x="630" y="94" text-anchor="middle" fill="#8aa0ad" font-size="10" font-family="DM Sans,sans-serif">hot short→long</text>
        </g>

        <!-- Side: Database -->
        <g class="topo-node topo-side" data-node="db" filter="url(#topo-soft)">
          <rect x="720" y="28" width="150" height="78" rx="10" fill="#152028" stroke="#5eb1e8" stroke-width="1.5"/>
          <text x="795" y="58" text-anchor="middle" fill="#e6eef2" font-size="13" font-weight="700" font-family="DM Sans,sans-serif">Database</text>
          <text x="795" y="78" text-anchor="middle" fill="#5eb1e8" font-size="10" font-family="IBM Plex Mono,monospace">DocumentDB / DynamoDB</text>
          <text x="795" y="94" text-anchor="middle" fill="#8aa0ad" font-size="10" font-family="DM Sans,sans-serif">URL mappings</text>
        </g>

        <!-- Primary row -->
        <g class="topo-node topo-primary" data-node="user" filter="url(#topo-soft)">
          <rect x="40" y="200" width="120" height="88" rx="10" fill="#152028" stroke="#3ecf8e" stroke-width="1.6"/>
          <text x="100" y="238" text-anchor="middle" fill="#e6eef2" font-size="14" font-weight="700" font-family="DM Sans,sans-serif">User</text>
          <text x="100" y="258" text-anchor="middle" fill="#8aa0ad" font-size="10" font-family="DM Sans,sans-serif">client / browser</text>
        </g>
        <g class="topo-node topo-primary" data-node="lb" filter="url(#topo-soft)">
          <rect x="200" y="200" width="130" height="88" rx="10" fill="#152028" stroke="#3ecf8e" stroke-width="1.6"/>
          <text x="265" y="232" text-anchor="middle" fill="#e6eef2" font-size="13" font-weight="700" font-family="DM Sans,sans-serif">Load balancer</text>
          <text x="265" y="252" text-anchor="middle" fill="#3ecf8e" font-size="10" font-family="IBM Plex Mono,monospace">ALB / Route 53</text>
          <text x="265" y="270" text-anchor="middle" fill="#8aa0ad" font-size="10" font-family="DM Sans,sans-serif">GSLB · multi-AZ</text>
        </g>
        <g class="topo-node topo-primary" data-node="web" filter="url(#topo-soft)">
          <rect x="370" y="200" width="140" height="88" rx="10" fill="#152028" stroke="#3ecf8e" stroke-width="1.6"/>
          <text x="440" y="232" text-anchor="middle" fill="#e6eef2" font-size="13" font-weight="700" font-family="DM Sans,sans-serif">Web servers</text>
          <text x="440" y="252" text-anchor="middle" fill="#3ecf8e" font-size="10" font-family="IBM Plex Mono,monospace">EKS / ECS ingress</text>
          <text x="440" y="270" text-anchor="middle" fill="#8aa0ad" font-size="10" font-family="DM Sans,sans-serif">TLS · HTTP edge</text>
        </g>
        <g class="topo-node topo-primary" data-node="app" filter="url(#topo-soft)">
          <rect x="550" y="200" width="160" height="88" rx="10" fill="#152028" stroke="#3ecf8e" stroke-width="1.8"/>
          <text x="630" y="228" text-anchor="middle" fill="#e6eef2" font-size="13" font-weight="700" font-family="DM Sans,sans-serif">Application</text>
          <text x="630" y="248" text-anchor="middle" fill="#3ecf8e" font-size="10" font-family="IBM Plex Mono,monospace">Link + Redirect API</text>
          <text x="630" y="266" text-anchor="middle" fill="#8aa0ad" font-size="10" font-family="DM Sans,sans-serif">Amazon EKS / ECS</text>
        </g>
        <g class="topo-node topo-primary" data-node="sug" filter="url(#topo-soft)">
          <rect x="760" y="200" width="170" height="88" rx="10" fill="#152028" stroke="#3ecf8e" stroke-width="1.6"/>
          <text x="845" y="228" text-anchor="middle" fill="#e6eef2" font-size="13" font-weight="700" font-family="DM Sans,sans-serif">Short URL generator</text>
          <text x="845" y="248" text-anchor="middle" fill="#3ecf8e" font-size="10" font-family="IBM Plex Mono,monospace">sequencer + Base-58</text>
          <text x="845" y="266" text-anchor="middle" fill="#8aa0ad" font-size="10" font-family="DM Sans,sans-serif">unique ID · encode</text>
        </g>
        <g class="topo-node topo-side" data-node="s3" filter="url(#topo-soft)">
          <rect x="980" y="200" width="150" height="88" rx="10" fill="#152028" stroke="#2a3f4d" stroke-width="1.4"/>
          <text x="1055" y="236" text-anchor="middle" fill="#e6eef2" font-size="13" font-weight="700" font-family="DM Sans,sans-serif">Object storage</text>
          <text x="1055" y="256" text-anchor="middle" fill="#8aa0ad" font-size="10" font-family="IBM Plex Mono,monospace">Amazon S3 backups</text>
          <text x="1055" y="274" text-anchor="middle" fill="#8aa0ad" font-size="10" font-family="DM Sans,sans-serif">DR snapshots</text>
        </g>

        <!-- Static bidirectional connectors (dashed) -->
        <g class="topo-links" fill="none" stroke="#3a5160" stroke-width="1.6" stroke-dasharray="5 4">
          <path id="p-u-lb" d="M160,244 H200" marker-start="url(#topo-mk-bi)" marker-end="url(#topo-mk)"/>
          <path id="p-lb-web" d="M330,244 H370" marker-start="url(#topo-mk-bi)" marker-end="url(#topo-mk)"/>
          <path id="p-web-app" d="M510,244 H550" marker-start="url(#topo-mk-bi)" marker-end="url(#topo-mk)"/>
          <path id="p-app-sug" d="M710,244 H760" marker-start="url(#topo-mk-bi)" marker-end="url(#topo-mk)"/>
          <path id="p-web-rate" d="M440,200 V106" marker-start="url(#topo-mk-bi)" marker-end="url(#topo-mk)"/>
          <path id="p-app-cache" d="M600,200 V106" marker-end="url(#topo-mk)"/>
          <path id="p-app-db" d="M660,200 V160 H795 V106" marker-end="url(#topo-mk)"/>
          <path id="p-db-s3" d="M930,244 H980" stroke-dasharray="3 3" marker-end="url(#topo-mk)"/>
        </g>

        <!-- Animated flow overlays -->
        <g class="topo-anim" fill="none" stroke-linecap="round">
          <path class="topo-flow-line f1" d="M160,244 H200" stroke="url(#topo-glow)" stroke-width="3"/>
          <path class="topo-flow-line f2" d="M330,244 H370" stroke="url(#topo-glow)" stroke-width="3"/>
          <path class="topo-flow-line f3" d="M440,200 V106" stroke="#5eb1e8" stroke-width="2.5" opacity="0.85"/>
          <path class="topo-flow-line f4" d="M510,244 H550" stroke="url(#topo-glow)" stroke-width="3"/>
          <path class="topo-flow-line f5" d="M600,200 V106" stroke="#5eb1e8" stroke-width="2.5" opacity="0.85"/>
          <path class="topo-flow-line f6" d="M660,200 V160 H795 V106" stroke="#5eb1e8" stroke-width="2.5" opacity="0.85"/>
          <path class="topo-flow-line f7" d="M710,244 H760" stroke="url(#topo-glow)" stroke-width="3"/>
        </g>

        <!-- Moving packets -->
        <circle class="topo-packet pk1" r="5" fill="#3ecf8e">
          <animateMotion dur="7s" repeatCount="indefinite" path="M160,244 H200 H330 H370 H440"/>
        </circle>
        <circle class="topo-packet pk2" r="4" fill="#5eb1e8">
          <animateMotion dur="7s" begin="1.2s" repeatCount="indefinite" path="M440,200 V106 V200"/>
        </circle>
        <circle class="topo-packet pk3" r="5" fill="#3ecf8e">
          <animateMotion dur="7s" begin="2s" repeatCount="indefinite" path="M510,244 H550 H630"/>
        </circle>
        <circle class="topo-packet pk4" r="4" fill="#5eb1e8">
          <animateMotion dur="7s" begin="3s" repeatCount="indefinite" path="M630,200 V106"/>
        </circle>
        <circle class="topo-packet pk5" r="4" fill="#5eb1e8">
          <animateMotion dur="7s" begin="3.6s" repeatCount="indefinite" path="M660,200 V160 H795 V106"/>
        </circle>
        <circle class="topo-packet pk6" r="5" fill="#3ecf8e">
          <animateMotion dur="7s" begin="4.4s" repeatCount="indefinite" path="M710,244 H760 H845"/>
        </circle>

        <text x="40" y="340" fill="#8aa0ad" font-size="12" font-family="DM Sans,sans-serif">
          Topology: User ↔ Load balancer ↔ Web servers ↔ Application ↔ Short URL generator
        </text>
        <text x="40" y="362" fill="#8aa0ad" font-size="12" font-family="DM Sans,sans-serif">
          Branches: Web ↔ Rate limiter · App → Cache + Database · backups → S3
        </text>
        <text x="40" y="390" fill="#5a6f7c" font-size="11" font-family="DM Sans,sans-serif">
          Animation cycles a request hop sequence · prefers-reduced-motion disables motion
        </text>
      </svg>
    </div>"""


def _render_connected_flow_diagram(product: str) -> str:
    """Production-grade write/read path diagram with orthogonal, directional AWS flows."""
    p = _esc(product)
    # Two swimlanes, left→right only. Write = accent; Read = info. Orthogonal connectors.
    return f"""
    <div class="flow-diagram-wrap">
      <div class="flow-diagram-head">
        <div>
          <h3>{p} — production request paths</h3>
          <p class="flow-sub">Orthogonal flows · AWS services on every hop · cache-first redirect</p>
        </div>
        <div class="flow-legend">
          <span class="leg write"><i></i> Write path — create short link</span>
          <span class="leg read"><i></i> Read path — resolve &amp; redirect</span>
        </div>
      </div>
      <svg class="flow-svg" viewBox="0 0 1180 560" role="img"
           aria-label="Production write and read paths with AWS services">
        <defs>
          <marker id="mk-w" markerWidth="9" markerHeight="9" refX="8" refY="3" orient="auto">
            <path d="M0,0 L8,3 L0,6 Z" fill="#3ecf8e"/>
          </marker>
          <marker id="mk-r" markerWidth="9" markerHeight="9" refX="8" refY="3" orient="auto">
            <path d="M0,0 L8,3 L0,6 Z" fill="#5eb1e8"/>
          </marker>
        </defs>

        <!-- ========== WRITE PATH ========== -->
        <rect x="16" y="16" width="1148" height="232" rx="10" fill="#121a21" stroke="#243642"/>
        <text x="36" y="42" fill="#3ecf8e" font-size="12" font-weight="700" font-family="IBM Plex Mono,monospace"
              letter-spacing="0.08em">WRITE PATH · CREATE SHORT LINK</text>

        <!-- W nodes: y=88, h=88, centers at 115 / 300 / 520 / 760 / 1000 -->
        <g>
          <rect x="40" y="72" width="150" height="96" rx="8" fill="#152028" stroke="#3ecf8e" stroke-width="1.4"/>
          <text x="115" y="108" text-anchor="middle" fill="#e6eef2" font-size="13" font-weight="700" font-family="DM Sans,sans-serif">API Client</text>
          <text x="115" y="128" text-anchor="middle" fill="#8aa0ad" font-size="10" font-family="DM Sans,sans-serif">POST /v1/links</text>
          <text x="115" y="146" text-anchor="middle" fill="#3ecf8e" font-size="10" font-family="IBM Plex Mono,monospace">api_dev_key</text>
        </g>
        <g>
          <rect x="225" y="72" width="150" height="96" rx="8" fill="#152028" stroke="#2a3f4d" stroke-width="1.4"/>
          <text x="300" y="104" text-anchor="middle" fill="#e6eef2" font-size="13" font-weight="700" font-family="DM Sans,sans-serif">Edge + Ingress</text>
          <text x="300" y="124" text-anchor="middle" fill="#8aa0ad" font-size="10" font-family="IBM Plex Mono,monospace">Route 53</text>
          <text x="300" y="140" text-anchor="middle" fill="#8aa0ad" font-size="10" font-family="IBM Plex Mono,monospace">CloudFront · WAF · ALB</text>
          <text x="300" y="156" text-anchor="middle" fill="#8aa0ad" font-size="10" font-family="DM Sans,sans-serif">rate limit</text>
        </g>
        <g>
          <rect x="430" y="72" width="180" height="96" rx="8" fill="#152028" stroke="#3ecf8e" stroke-width="1.6"/>
          <text x="520" y="104" text-anchor="middle" fill="#e6eef2" font-size="13" font-weight="700" font-family="DM Sans,sans-serif">Link API + SUG</text>
          <text x="520" y="124" text-anchor="middle" fill="#3ecf8e" font-size="10" font-family="IBM Plex Mono,monospace">Amazon EKS / ECS</text>
          <text x="520" y="142" text-anchor="middle" fill="#8aa0ad" font-size="10" font-family="DM Sans,sans-serif">sequencer · Base-58</text>
          <text x="520" y="158" text-anchor="middle" fill="#8aa0ad" font-size="10" font-family="DM Sans,sans-serif">allocate ID · encode</text>
        </g>
        <g>
          <rect x="670" y="72" width="180" height="96" rx="8" fill="#152028" stroke="#5eb1e8" stroke-width="1.4"/>
          <text x="760" y="104" text-anchor="middle" fill="#e6eef2" font-size="13" font-weight="700" font-family="DM Sans,sans-serif">Mapping store</text>
          <text x="760" y="124" text-anchor="middle" fill="#5eb1e8" font-size="10" font-family="IBM Plex Mono,monospace">DocumentDB / DynamoDB</text>
          <text x="760" y="142" text-anchor="middle" fill="#8aa0ad" font-size="10" font-family="DM Sans,sans-serif">short ↔ long · used IDs</text>
          <text x="760" y="158" text-anchor="middle" fill="#8aa0ad" font-size="10" font-family="DM Sans,sans-serif">unique index</text>
        </g>
        <g>
          <rect x="910" y="72" width="180" height="96" rx="8" fill="#152028" stroke="#2a3f4d" stroke-width="1.4"/>
          <text x="1000" y="108" text-anchor="middle" fill="#e6eef2" font-size="13" font-weight="700" font-family="DM Sans,sans-serif">Durability</text>
          <text x="1000" y="128" text-anchor="middle" fill="#8aa0ad" font-size="10" font-family="IBM Plex Mono,monospace">Amazon S3</text>
          <text x="1000" y="146" text-anchor="middle" fill="#8aa0ad" font-size="10" font-family="DM Sans,sans-serif">snapshots · DR</text>
        </g>

        <!-- Write arrows: baseline y=120, left → right -->
        <line x1="190" y1="120" x2="225" y2="120" stroke="#3ecf8e" stroke-width="2" marker-end="url(#mk-w)"/>
        <circle cx="207" cy="120" r="10" fill="#0e1419" stroke="#3ecf8e" stroke-width="1.5"/>
        <text x="207" y="124" text-anchor="middle" fill="#3ecf8e" font-size="10" font-weight="700" font-family="IBM Plex Mono,monospace">1</text>
        <text x="207" y="60" text-anchor="middle" fill="#8aa0ad" font-size="10" font-family="DM Sans,sans-serif">long URL</text>

        <line x1="375" y1="120" x2="430" y2="120" stroke="#3ecf8e" stroke-width="2" marker-end="url(#mk-w)"/>
        <circle cx="402" cy="120" r="10" fill="#0e1419" stroke="#3ecf8e" stroke-width="1.5"/>
        <text x="402" y="124" text-anchor="middle" fill="#3ecf8e" font-size="10" font-weight="700" font-family="IBM Plex Mono,monospace">2</text>
        <text x="402" y="60" text-anchor="middle" fill="#8aa0ad" font-size="10" font-family="DM Sans,sans-serif">route + limit</text>

        <line x1="610" y1="120" x2="670" y2="120" stroke="#3ecf8e" stroke-width="2" marker-end="url(#mk-w)"/>
        <circle cx="640" cy="120" r="10" fill="#0e1419" stroke="#3ecf8e" stroke-width="1.5"/>
        <text x="640" y="124" text-anchor="middle" fill="#3ecf8e" font-size="10" font-weight="700" font-family="IBM Plex Mono,monospace">3</text>
        <text x="640" y="60" text-anchor="middle" fill="#8aa0ad" font-size="10" font-family="DM Sans,sans-serif">persist mapping</text>

        <line x1="850" y1="120" x2="910" y2="120" stroke="#3ecf8e" stroke-width="2" stroke-dasharray="4 3" marker-end="url(#mk-w)"/>
        <circle cx="880" cy="120" r="10" fill="#0e1419" stroke="#3ecf8e" stroke-width="1.5"/>
        <text x="880" y="124" text-anchor="middle" fill="#3ecf8e" font-size="10" font-weight="700" font-family="IBM Plex Mono,monospace">4</text>
        <text x="880" y="60" text-anchor="middle" fill="#8aa0ad" font-size="10" font-family="DM Sans,sans-serif">async backup</text>

        <!-- Return path: DB → App → Client (orthogonal below) -->
        <path d="M760,168 L760,196 L115,196 L115,168" fill="none" stroke="#3ecf8e" stroke-width="2" marker-end="url(#mk-w)"/>
        <circle cx="440" cy="196" r="10" fill="#0e1419" stroke="#3ecf8e" stroke-width="1.5"/>
        <text x="440" y="200" text-anchor="middle" fill="#3ecf8e" font-size="10" font-weight="700" font-family="IBM Plex Mono,monospace">5</text>
        <text x="440" y="220" text-anchor="middle" fill="#8aa0ad" font-size="10" font-family="DM Sans,sans-serif">201 ← short URL returned to client</text>

        <!-- ========== READ PATH ========== -->
        <rect x="16" y="268" width="1148" height="268" rx="10" fill="#121a21" stroke="#243642"/>
        <text x="36" y="294" fill="#5eb1e8" font-size="12" font-weight="700" font-family="IBM Plex Mono,monospace"
              letter-spacing="0.08em">READ PATH · CACHE-FIRST REDIRECT</text>

        <!-- R nodes: y=332 -->
        <g>
          <rect x="40" y="320" width="140" height="96" rx="8" fill="#152028" stroke="#5eb1e8" stroke-width="1.4"/>
          <text x="110" y="356" text-anchor="middle" fill="#e6eef2" font-size="13" font-weight="700" font-family="DM Sans,sans-serif">Browser</text>
          <text x="110" y="376" text-anchor="middle" fill="#8aa0ad" font-size="10" font-family="DM Sans,sans-serif">GET /{{code}}</text>
          <text x="110" y="394" text-anchor="middle" fill="#5eb1e8" font-size="10" font-family="IBM Plex Mono,monospace">short link click</text>
        </g>
        <g>
          <rect x="220" y="320" width="150" height="96" rx="8" fill="#152028" stroke="#2a3f4d" stroke-width="1.4"/>
          <text x="295" y="352" text-anchor="middle" fill="#e6eef2" font-size="13" font-weight="700" font-family="DM Sans,sans-serif">Edge</text>
          <text x="295" y="372" text-anchor="middle" fill="#8aa0ad" font-size="10" font-family="IBM Plex Mono,monospace">CloudFront</text>
          <text x="295" y="388" text-anchor="middle" fill="#8aa0ad" font-size="10" font-family="IBM Plex Mono,monospace">Route 53 · ALB</text>
          <text x="295" y="404" text-anchor="middle" fill="#8aa0ad" font-size="10" font-family="DM Sans,sans-serif">TLS terminate</text>
        </g>
        <g>
          <rect x="410" y="320" width="160" height="96" rx="8" fill="#152028" stroke="#5eb1e8" stroke-width="1.6"/>
          <text x="490" y="352" text-anchor="middle" fill="#e6eef2" font-size="13" font-weight="700" font-family="DM Sans,sans-serif">Redirect API</text>
          <text x="490" y="372" text-anchor="middle" fill="#5eb1e8" font-size="10" font-family="IBM Plex Mono,monospace">Amazon EKS / ECS</text>
          <text x="490" y="390" text-anchor="middle" fill="#8aa0ad" font-size="10" font-family="DM Sans,sans-serif">resolve code</text>
          <text x="490" y="406" text-anchor="middle" fill="#8aa0ad" font-size="10" font-family="DM Sans,sans-serif">no analytics wait</text>
        </g>
        <g>
          <rect x="610" y="320" width="150" height="96" rx="8" fill="#152028" stroke="#5eb1e8" stroke-width="1.4"/>
          <text x="685" y="352" text-anchor="middle" fill="#e6eef2" font-size="13" font-weight="700" font-family="DM Sans,sans-serif">Hot cache</text>
          <text x="685" y="372" text-anchor="middle" fill="#5eb1e8" font-size="10" font-family="IBM Plex Mono,monospace">ElastiCache</text>
          <text x="685" y="390" text-anchor="middle" fill="#8aa0ad" font-size="10" font-family="DM Sans,sans-serif">L2 hit → return</text>
          <text x="685" y="406" text-anchor="middle" fill="#8aa0ad" font-size="10" font-family="DM Sans,sans-serif">miss → step 4</text>
        </g>
        <g>
          <rect x="800" y="320" width="150" height="96" rx="8" fill="#152028" stroke="#2a3f4d" stroke-width="1.4"/>
          <text x="875" y="356" text-anchor="middle" fill="#e6eef2" font-size="13" font-weight="700" font-family="DM Sans,sans-serif">Mapping DB</text>
          <text x="875" y="376" text-anchor="middle" fill="#8aa0ad" font-size="10" font-family="IBM Plex Mono,monospace">DocumentDB</text>
          <text x="875" y="394" text-anchor="middle" fill="#8aa0ad" font-size="10" font-family="DM Sans,sans-serif">populate cache</text>
        </g>
        <g>
          <rect x="990" y="320" width="150" height="96" rx="8" fill="#152028" stroke="#3ecf8e" stroke-width="1.4"/>
          <text x="1065" y="356" text-anchor="middle" fill="#e6eef2" font-size="13" font-weight="700" font-family="DM Sans,sans-serif">Destination</text>
          <text x="1065" y="376" text-anchor="middle" fill="#3ecf8e" font-size="10" font-family="DM Sans,sans-serif">HTTP 302 / 301</text>
          <text x="1065" y="394" text-anchor="middle" fill="#8aa0ad" font-size="10" font-family="DM Sans,sans-serif">original long URL</text>
        </g>

        <!-- Read arrows: baseline y=368, left → right -->
        <line x1="180" y1="368" x2="220" y2="368" stroke="#5eb1e8" stroke-width="2" marker-end="url(#mk-r)"/>
        <circle cx="200" cy="368" r="10" fill="#0e1419" stroke="#5eb1e8" stroke-width="1.5"/>
        <text x="200" y="372" text-anchor="middle" fill="#5eb1e8" font-size="10" font-weight="700" font-family="IBM Plex Mono,monospace">1</text>

        <line x1="370" y1="368" x2="410" y2="368" stroke="#5eb1e8" stroke-width="2" marker-end="url(#mk-r)"/>
        <circle cx="390" cy="368" r="10" fill="#0e1419" stroke="#5eb1e8" stroke-width="1.5"/>
        <text x="390" y="372" text-anchor="middle" fill="#5eb1e8" font-size="10" font-weight="700" font-family="IBM Plex Mono,monospace">2</text>

        <line x1="570" y1="368" x2="610" y2="368" stroke="#5eb1e8" stroke-width="2" marker-end="url(#mk-r)"/>
        <circle cx="590" cy="368" r="10" fill="#0e1419" stroke="#5eb1e8" stroke-width="1.5"/>
        <text x="590" y="372" text-anchor="middle" fill="#5eb1e8" font-size="10" font-weight="700" font-family="IBM Plex Mono,monospace">3</text>
        <text x="590" y="308" text-anchor="middle" fill="#8aa0ad" font-size="10" font-family="DM Sans,sans-serif">cache lookup</text>

        <line x1="760" y1="368" x2="800" y2="368" stroke="#5eb1e8" stroke-width="2" stroke-dasharray="4 3" marker-end="url(#mk-r)"/>
        <circle cx="780" cy="368" r="10" fill="#0e1419" stroke="#5eb1e8" stroke-width="1.5"/>
        <text x="780" y="372" text-anchor="middle" fill="#5eb1e8" font-size="10" font-weight="700" font-family="IBM Plex Mono,monospace">4</text>
        <text x="780" y="308" text-anchor="middle" fill="#8aa0ad" font-size="10" font-family="DM Sans,sans-serif">miss only</text>

        <!-- Response: Cache/DB → Redirect API → Destination (orthogonal) -->
        <path d="M685,416 L685,452 L1065,452 L1065,416" fill="none" stroke="#5eb1e8" stroke-width="2" marker-end="url(#mk-r)"/>
        <circle cx="875" cy="452" r="10" fill="#0e1419" stroke="#5eb1e8" stroke-width="1.5"/>
        <text x="875" y="456" text-anchor="middle" fill="#5eb1e8" font-size="10" font-weight="700" font-family="IBM Plex Mono,monospace">5</text>
        <text x="875" y="480" text-anchor="middle" fill="#8aa0ad" font-size="10" font-family="DM Sans,sans-serif">Location header → browser follows to destination</text>

        <!-- Cache hit shortcut annotation -->
        <path d="M685,320 L685,300 L1065,300 L1065,320" fill="none" stroke="#3ecf8e" stroke-width="1.5" stroke-dasharray="3 3" marker-end="url(#mk-w)"/>
        <text x="875" y="292" text-anchor="middle" fill="#3ecf8e" font-size="10" font-family="DM Sans,sans-serif">cache hit: skip DB · direct 302</text>
      </svg>
    </div>"""


def _render_system_context_section(
    product: str,
    context: dict[str, Any],
    hld: dict[str, Any],
) -> str:
    actors = context.get("actors") or []
    external = context.get("external") or []
    regions = context.get("regions") or hld.get("regions") or [
        "us-east-1 (primary)",
        "us-west-2 (DR / secondary)",
    ]
    consistency = context.get("consistency") or hld.get("consistency_model") or (
        "Eventual consistency across regions OK (create → first redirect lag)"
    )
    layers = _normalize_context_layers(product, context, hld)
    shortener_ctx = _hld_looks_url_shortener(product, hld)
    flow = (
        context.get("request_flow")
        or (hld.get("diagrams") or {}).get("context")
        or (
            "Client → Route 53 → CloudFront → ALB → Rate limit → "
            f"{product} App (Link/Redirect/SUG) → ElastiCache → DocumentDB/DynamoDB → S3 backups"
            if shortener_ctx
            else f"Client → API gateway → {product} domain services → datastore / cache"
        )
    )

    # Horizontal connected AWS pipeline (each section → next)
    if shortener_ctx:
        pipeline = [
            ("Clients", "User / API / Browser"),
            ("Route 53", "DNS / GSLB"),
            ("CloudFront + WAF", "Edge CDN"),
            ("ALB + rate limit", "Regional ingress"),
            (f"{product} App", "EKS/ECS · SUG"),
            ("ElastiCache", "Hot mappings"),
            ("DocumentDB / DynamoDB", "URL mapping store"),
            ("S3 + CloudWatch", "Backup · ops"),
        ]
    else:
        comps = [str(c) for c in (hld.get("components") or [])[:4] if c]
        mid = comps[0] if comps else f"{product} services"
        pipeline = [
            ("Clients", "User / API / Browser"),
            ("API gateway", "Auth · routing"),
            (mid, "Domain services"),
            ("Datastore", "Primary persistence"),
            ("Cache / objects", "Perf · files"),
            ("Observability", "Metrics · logs"),
        ]
    pipe_parts = []
    for i, (title, sub) in enumerate(pipeline):
        pipe_parts.append(
            f"""
        <div class="pipe-node">
          <div class="pipe-title">{_esc(title)}</div>
          <div class="pipe-sub">{_esc(sub)}</div>
        </div>"""
        )
        if i < len(pipeline) - 1:
            pipe_parts.append(
                '<div class="pipe-arrow" aria-hidden="true">'
                '<span class="pipe-line"></span><span class="pipe-head">▶</span></div>'
            )

    layer_html = []
    for i, layer in enumerate(layers):
        tone = _esc(layer.get("tone") or "app")
        nodes_html = []
        for n in layer.get("nodes") or []:
            aws = n.get("aws") or ""
            nodes_html.append(
                f"""
            <div class="ctx-node tone-{tone}">
              <div class="ctx-name">{_esc(n.get("name"))}</div>
              {"<div class='ctx-aws'>" + _esc(aws) + "</div>" if aws else ""}
            </div>"""
            )
        arrow = (
            '<div class="ctx-arrow-row" aria-hidden="true">'
            '<span class="ctx-arrow-line"></span><span class="ctx-arrow-head">▼</span>'
            "</div>"
            if i < len(layers) - 1
            else ""
        )
        layer_html.append(
            f"""
        <div class="ctx-layer tone-{tone}">
          <div class="ctx-layer-title">{_esc(layer.get("title"))}</div>
          <div class="ctx-nodes">{"".join(nodes_html)}</div>
        </div>
        {arrow}"""
        )

    actor_chips = _chip_row(actors) if actors else ""
    external_chips = _chip_row(external) if external else ""
    region_chips = _chip_row(regions)
    shortener = _hld_looks_url_shortener(product, hld)
    topo_svg = _render_hld_topology_diagram(product, hld)
    flow_svg = _render_connected_flow_diagram(product) if shortener else ""
    lead = (
        "Animated HLD topology (URL-shortener building blocks) plus production write/read paths "
        "with AWS on every hop."
        if shortener
        else "HLD topology derived from this product's architecture components (from the uploaded SRS)."
    )

    return f"""
  <section class="system-context">
    <h2>1. System context</h2>
    <p class="muted ctx-lead">
      {lead}
    </p>
    {topo_svg}
    {flow_svg}
    <div class="pipe-row" aria-label="AWS request pipeline">
      <div class="k" style="width:100%;margin-bottom:0.45rem">AWS request pipeline (left → right)</div>
      {"".join(pipe_parts)}
    </div>
    <div class="ctx-meta grid">
      <div class="card">
        <h3>Actors</h3>
        <div>{actor_chips or "<span class='muted'>Clients</span>"}</div>
      </div>
      <div class="card">
        <h3>Regions</h3>
        <div>{region_chips}</div>
      </div>
      <div class="card">
        <h3>Consistency</h3>
        <p>{_esc(consistency)}</p>
      </div>
      <div class="card">
        <h3>External systems</h3>
        <div>{external_chips or "<span class='muted'>—</span>"}</div>
      </div>
    </div>
    <div class="ctx-flow">
      <div class="k">Primary request flow</div>
      <pre>{_esc(flow)}</pre>
    </div>
    <div class="ctx-stack">
      <div class="k" style="margin-bottom:0.5rem">Layered AWS building blocks (top → bottom)</div>
      {"".join(layer_html)}
    </div>
  </section>"""


def build_architecture_html(
    *,
    product: str,
    hld: dict[str, Any],
    lld: dict[str, Any],
    adrs: list[Any],
    reqspec: dict[str, Any] | None = None,
    capacity: dict[str, Any] | None = None,
    perf_budget: dict[str, Any] | None = None,
    sequences: dict[str, Any] | None = None,
) -> str:
    """Self-contained Architecture Design HTML (HLD visual, not a JSON dump)."""
    reqspec = reqspec or {}
    capacity = capacity or {}
    perf_budget = perf_budget or {}
    sequences = sequences or {}
    style = hld.get("style") or "service architecture"
    tenets = hld.get("tenets") or []
    components = hld.get("components") or []
    building_blocks = hld.get("building_blocks") or []
    context = hld.get("context") if isinstance(hld.get("context"), dict) else {}
    caching = hld.get("caching_strategy") or {}
    nfr = hld.get("nfr_targets") or {}
    nfr_compliance = hld.get("nfr_compliance") or {}
    shortener_hld = _hld_looks_url_shortener(product, hld)
    sug = (hld.get("short_url_generator") or {}) if shortener_hld else {}
    apis = hld.get("apis") or {}
    workflows = hld.get("workflows") or {}
    services = (lld.get("services") or {}) if isinstance(lld, dict) else {}
    frs = reqspec.get("fr") or []

    sug_rows = "".join(
        f"<tr><th>{_esc(k)}</th><td>{_esc(v)}</td></tr>" for k, v in sug.items()
    ) or ""
    api_rows = "".join(
        f"<tr><th>{_esc(k)}</th><td><code>{_esc(v)}</code></td></tr>" for k, v in apis.items()
    ) or ""
    wf_blocks = "".join(
        f"<div class='seq'><h4>{_esc(k)}</h4><pre>{_esc(v)}</pre></div>"
        for k, v in workflows.items()
    )
    nfr_comp_html = ""
    if isinstance(nfr_compliance, dict) and nfr_compliance:
        parts = []
        for k, vals in nfr_compliance.items():
            if isinstance(vals, list):
                parts.append(f"<tr><th>{_esc(k)}</th><td><ul>{_list_items(vals)}</ul></td></tr>")
            else:
                parts.append(f"<tr><th>{_esc(k)}</th><td>{_esc(vals)}</td></tr>")
        nfr_comp_html = f"<table>{''.join(parts)}</table>"
    cap_assumptions = capacity.get("assumptions") or {}
    cap_rows = []
    for key in (
        "write_qps",
        "read_qps",
        "redirect_rps_target",
        "create_rps_target",
        "storage_tb",
        "cache_memory_gb",
        "entries_5y",
        "method",
    ):
        if key in capacity and capacity[key] is not None:
            cap_rows.append(
                f"<tr><th>{_esc(key)}</th><td>{_esc(capacity[key])}</td></tr>"
            )
    if isinstance(capacity.get("bandwidth"), dict):
        for bk, bv in capacity["bandwidth"].items():
            cap_rows.append(f"<tr><th>bandwidth.{_esc(bk)}</th><td>{_esc(bv)}</td></tr>")
    if isinstance(cap_assumptions, dict):
        for ak, av in cap_assumptions.items():
            cap_rows.append(f"<tr><th>assumption.{_esc(ak)}</th><td>{_esc(av)}</td></tr>")
    capacity_table = (
        f"<table>{''.join(cap_rows)}</table>"
        if cap_rows
        else "<p class='muted'>No capacity estimate.</p>"
    )
    blocks_section = (
        f"""
  <section>
    <h2>2a. Building blocks</h2>
    <div>{_chip_row(building_blocks)}</div>
  </section>"""
        if building_blocks
        else ""
    )
    sug_section = (
        f"""
  <section>
    <h2>2b. Short URL generator</h2>
    <table>{sug_rows}</table>
  </section>"""
        if sug_rows
        else ""
    )
    api_section = (
        f"""
  <section>
    <h2>2c. System APIs</h2>
    <table>{api_rows}</table>
  </section>"""
        if api_rows
        else ""
    )
    wf_section = (
        f"""
  <section>
    <h2>2d. Workflows</h2>
    {wf_blocks}
  </section>"""
        if wf_blocks
        else ""
    )
    nfr_comp_section = (
        f"""
  <section>
    <h2>5b. NFR compliance</h2>
    {nfr_comp_html}
  </section>"""
        if nfr_comp_html
        else ""
    )
    capacity_section = f"""
  <section>
    <h2>5a. Capacity estimate</h2>
    {capacity_table}
  </section>"""

    service_cards = []
    for svc, meta in services.items():
        if not isinstance(meta, dict):
            meta = {"handlers": [str(meta)]}
        handlers = ", ".join(meta.get("handlers") or []) or "—"
        deps = ", ".join(meta.get("deps") or []) or "—"
        inv = ", ".join(meta.get("invariants") or []) or "—"
        service_cards.append(
            f"""
            <article class="card">
              <h3>{_esc(svc)}</h3>
              <p><span class="k">Handlers</span> {_esc(handlers)}</p>
              <p><span class="k">Deps</span> {_esc(deps)}</p>
              <p><span class="k">Invariants</span> {_esc(inv)}</p>
            </article>"""
        )
    if not service_cards:
        service_cards.append("<p class='muted'>LLD services will appear after architecture synthesis.</p>")

    seq_blocks = []
    for name, body in sequences.items():
        seq_blocks.append(
            f"<div class='seq'><h4>{_esc(name)}</h4><pre>{_esc(body)}</pre></div>"
        )
    if not seq_blocks:
        seq_blocks.append("<p class='muted'>No sequence flows recorded.</p>")

    cache_rows = "".join(
        f"<tr><th>{_esc(k)}</th><td>{_esc(v)}</td></tr>" for k, v in caching.items()
    ) or "<tr><td colspan='2' class='muted'>Not specified</td></tr>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{_esc(product)} — Architecture Design</title>
<style>
{_FORGE_THEME_ROOT}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; font-family: "DM Sans", "IBM Plex Sans", system-ui, sans-serif;
    color: var(--text); background: var(--bg0);
  }}
{_FORGE_HEADER_CSS}
  main {{ max-width: 1100px; margin: 0 auto; padding: 1.25rem; display: grid; gap: 1rem; }}
  section {{
    background: var(--bg1); border: 1px solid var(--line); border-radius: 10px;
    padding: 1.1rem 1.2rem;
  }}
  h2 {{ margin: 0 0 0.75rem; font-size: 0.78rem; color: var(--muted); letter-spacing: 0.08em; text-transform: uppercase; }}
  h3 {{ margin: 0 0 0.4rem; font-size: 1rem; color: var(--text); }}
  .muted {{ color: var(--muted); }}
  .chip {{
    display: inline-block; margin: 0.2rem 0.35rem 0.2rem 0; padding: 0.28rem 0.65rem;
    border-radius: 8px; background: var(--soft); color: var(--accent); font-size: 0.82rem;
    border: 1px solid var(--line); font-family: "IBM Plex Mono", monospace;
  }}
  .grid {{ display: grid; gap: 0.75rem; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }}
  .card {{ border: 1px solid var(--line); border-radius: 10px; padding: 0.85rem; background: var(--bg2); }}
  .k {{ display: inline-block; min-width: 5.5rem; color: var(--muted); font-size: 0.72rem; text-transform: uppercase; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ text-align: left; padding: 0.45rem 0.35rem; border-bottom: 1px solid var(--line); vertical-align: top; color: var(--text); }}
  th {{ width: 30%; color: var(--muted); font-weight: 600; }}
  ul {{ margin: 0.2rem 0 0; padding-left: 1.1rem; color: var(--text); }}
  .diagram {{
    display: flex; flex-wrap: wrap; gap: 0.55rem; align-items: center; justify-content: center;
    padding: 1rem; background: var(--bg2); border-radius: 10px; border: 1px solid var(--line);
  }}
  .node {{
    min-width: 120px; text-align: center; padding: 0.7rem 0.8rem; border-radius: 10px;
    background: var(--bg1); border: 1px solid var(--line); font-size: 0.86rem; font-weight: 600; color: var(--text);
  }}
  .node.actor {{ border-color: color-mix(in srgb, var(--accent) 45%, var(--line)); background: color-mix(in srgb, var(--accent) 12%, var(--bg1)); }}
  .node.data {{ border-color: color-mix(in srgb, var(--info) 45%, var(--line)); background: color-mix(in srgb, var(--info) 12%, var(--bg1)); }}
  .arrow {{ color: var(--muted); font-weight: 700; }}
  .ctx-lead {{ margin: 0 0 0.85rem; line-height: 1.45; }}
  .ctx-meta {{ margin: 0.9rem 0; }}
  .ctx-meta .card h3 {{ font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); }}
  .ctx-meta .card p {{ margin: 0.35rem 0 0; font-size: 0.9rem; line-height: 1.4; }}
  .flow-diagram-wrap {{
    margin: 0 0 1rem; padding: 0.9rem 1rem 0.6rem; border-radius: 12px;
    background: linear-gradient(180deg, #101820 0%, #152028 100%);
    border: 1px solid var(--line); overflow-x: auto;
  }}
  .flow-diagram-head {{
    display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between;
    gap: 0.6rem; margin-bottom: 0.55rem;
  }}
  .flow-diagram-head h3 {{ margin: 0; font-size: 1rem; color: var(--text); }}
  .flow-sub {{ margin: 0.25rem 0 0; font-size: 0.78rem; color: var(--muted); }}
  .flow-legend {{ display: flex; flex-wrap: wrap; gap: 0.85rem; font-size: 0.8rem; color: var(--muted); }}
  .flow-legend .leg {{ display: inline-flex; align-items: center; gap: 0.4rem; }}
  .flow-legend .leg i {{
    display: inline-block; width: 22px; height: 3px; border-radius: 2px;
  }}
  .flow-legend .leg.write i {{ background: var(--accent); }}
  .flow-legend .leg.read i {{ background: var(--info); }}
  .flow-svg {{ width: 100%; min-width: 860px; height: auto; display: block; }}
  .topo-wrap {{
    margin: 0 0 1rem; padding: 0.9rem 1rem 0.55rem; border-radius: 12px;
    background: linear-gradient(165deg, #0f1820 0%, #152028 55%, #101820 100%);
    border: 1px solid var(--line); overflow-x: auto;
  }}
  .topo-svg {{ width: 100%; min-width: 900px; height: auto; display: block; }}
  .topo-node[data-node="user"] rect {{ animation: topo-glow-green 7s ease-in-out infinite; animation-delay: 0s; }}
  .topo-node[data-node="lb"] rect {{ animation: topo-glow-green 7s ease-in-out infinite; animation-delay: 0.7s; }}
  .topo-node[data-node="web"] rect {{ animation: topo-glow-green 7s ease-in-out infinite; animation-delay: 1.4s; }}
  .topo-node[data-node="rate"] rect {{ animation: topo-glow-blue 7s ease-in-out infinite; animation-delay: 2.1s; }}
  .topo-node[data-node="app"] rect {{ animation: topo-glow-green 7s ease-in-out infinite; animation-delay: 2.8s; }}
  .topo-node[data-node="cache"] rect {{ animation: topo-glow-blue 7s ease-in-out infinite; animation-delay: 3.5s; }}
  .topo-node[data-node="db"] rect {{ animation: topo-glow-blue 7s ease-in-out infinite; animation-delay: 4.0s; }}
  .topo-node[data-node="sug"] rect {{ animation: topo-glow-green 7s ease-in-out infinite; animation-delay: 4.6s; }}
  .topo-node[data-node="s3"] rect {{ animation: topo-glow-green 7s ease-in-out infinite; animation-delay: 5.4s; }}
  @keyframes topo-glow-green {{
    0%, 14%, 100% {{ stroke: #3ecf8e; opacity: 1; }}
    7% {{ stroke: #7dffc0; opacity: 1; }}
  }}
  @keyframes topo-glow-blue {{
    0%, 14%, 100% {{ stroke: #5eb1e8; opacity: 1; }}
    7% {{ stroke: #9ad4ff; opacity: 1; }}
  }}
  .topo-flow-line {{
    stroke-dasharray: 12 120;
    animation: topo-dash 2.2s linear infinite;
  }}
  .topo-flow-line.f2 {{ animation-delay: 0.3s; }}
  .topo-flow-line.f3 {{ animation-delay: 0.6s; }}
  .topo-flow-line.f4 {{ animation-delay: 0.9s; }}
  .topo-flow-line.f5 {{ animation-delay: 1.2s; }}
  .topo-flow-line.f6 {{ animation-delay: 1.5s; }}
  .topo-flow-line.f7 {{ animation-delay: 1.8s; }}
  @keyframes topo-dash {{
    to {{ stroke-dashoffset: -132; }}
  }}
  .topo-packet {{ filter: drop-shadow(0 0 4px rgba(62,207,142,0.85)); }}
  @media (prefers-reduced-motion: reduce) {{
    .topo-node rect, .topo-flow-line {{ animation: none !important; }}
    .topo-packet {{ display: none; }}
  }}
  .pipe-row {{
    display: flex; flex-wrap: wrap; align-items: stretch; gap: 0.25rem 0;
    margin: 0 0 1rem; padding: 0.85rem; border-radius: 12px;
    background: var(--bg2); border: 1px solid var(--line);
  }}
  .pipe-node {{
    flex: 1 1 100px; min-width: 100px; max-width: 140px; padding: 0.55rem 0.5rem;
    border-radius: 8px; background: var(--bg1); border: 1px solid var(--line); text-align: center;
  }}
  .pipe-title {{ font-size: 0.78rem; font-weight: 700; color: var(--text); line-height: 1.25; }}
  .pipe-sub {{ margin-top: 0.25rem; font-size: 0.68rem; color: var(--accent); font-family: "IBM Plex Mono", monospace; line-height: 1.3; }}
  .pipe-arrow {{
    display: flex; align-items: center; color: var(--accent); padding: 0 0.1rem; flex: 0 0 auto;
  }}
  .pipe-line {{
    display: inline-block; width: 14px; height: 2px; background: var(--accent); opacity: 0.75;
  }}
  .pipe-head {{ font-size: 0.7rem; line-height: 1; margin-left: -2px; }}
  .ctx-flow {{
    margin: 0 0 1rem; padding: 0.75rem 0.9rem; border-radius: 10px;
    background: var(--bg2); border: 1px solid var(--line);
  }}
  .ctx-flow pre {{
    margin: 0.35rem 0 0; white-space: pre-wrap; font-family: "IBM Plex Mono", monospace;
    font-size: 0.8rem; color: var(--text); line-height: 1.45;
  }}
  .ctx-stack {{
    display: grid; gap: 0.15rem; padding: 0.85rem; border-radius: 12px;
    background: linear-gradient(180deg, #101820 0%, #152028 100%);
    border: 1px solid var(--line);
  }}
  .ctx-layer {{
    border: 1px solid var(--line); border-radius: 10px; padding: 0.7rem 0.8rem; background: var(--bg1);
  }}
  .ctx-layer-title {{
    font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.07em;
    color: var(--muted); margin-bottom: 0.55rem; font-weight: 700;
  }}
  .ctx-nodes {{ display: grid; gap: 0.5rem; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); }}
  .ctx-node {{
    border-radius: 8px; padding: 0.55rem 0.65rem; border: 1px solid var(--line); background: var(--bg2);
  }}
  .ctx-name {{ font-size: 0.84rem; font-weight: 650; color: var(--text); line-height: 1.3; }}
  .ctx-aws {{
    margin-top: 0.28rem; font-size: 0.72rem; color: var(--accent);
    font-family: "IBM Plex Mono", monospace; line-height: 1.3;
  }}
  .ctx-arrow-row {{
    display: flex; flex-direction: column; align-items: center; color: var(--accent);
    padding: 0.15rem 0; opacity: 0.85;
  }}
  .ctx-arrow-line {{ width: 2px; height: 10px; background: var(--accent); }}
  .ctx-arrow-head {{ font-size: 0.65rem; line-height: 1; margin-top: -2px; }}
  .ctx-layer.tone-actor {{ border-color: color-mix(in srgb, var(--accent) 40%, var(--line)); }}
  .ctx-layer.tone-edge {{ border-color: color-mix(in srgb, #f0b429 35%, var(--line)); }}
  .ctx-layer.tone-ingress {{ border-color: color-mix(in srgb, var(--info) 40%, var(--line)); }}
  .ctx-layer.tone-app {{ border-color: color-mix(in srgb, var(--accent) 30%, var(--line)); }}
  .ctx-layer.tone-data {{ border-color: color-mix(in srgb, var(--info) 45%, var(--line)); }}
  .ctx-layer.tone-platform {{ border-color: color-mix(in srgb, #9b8cff 35%, var(--line)); }}
  .ctx-layer.tone-analytics {{ border-color: color-mix(in srgb, #e89b5e 40%, var(--line)); }}
  .ctx-node.tone-actor {{ background: color-mix(in srgb, var(--accent) 10%, var(--bg2)); }}
  .ctx-node.tone-edge {{ background: color-mix(in srgb, #f0b429 8%, var(--bg2)); }}
  .ctx-node.tone-ingress {{ background: color-mix(in srgb, var(--info) 10%, var(--bg2)); }}
  .ctx-node.tone-data {{ background: color-mix(in srgb, var(--info) 12%, var(--bg2)); }}
  .ctx-node.tone-analytics {{ background: color-mix(in srgb, #e89b5e 10%, var(--bg2)); }}
  @media (max-width: 720px) {{
    .pipe-arrow {{ display: none; }}
    .pipe-node {{ max-width: none; flex: 1 1 45%; }}
  }}
  .seq pre {{
    margin: 0.35rem 0 0; white-space: pre-wrap; background: #0b1220; color: var(--text);
    padding: 0.8rem; border-radius: 10px; font-size: 0.82rem; overflow: auto;
    border: 1px solid var(--line);
  }}
  footer {{ text-align: center; color: var(--muted); font-size: 0.8rem; padding: 0.5rem 0 1.5rem; }}
</style>
</head>
<body>
<header>
  <h1>{_esc(product)}</h1>
  <p>Architecture Design (HLD) — {_esc(style)}. Generated from the requirement-driven Forge Architecture Agent.</p>
</header>
<main>
  {_render_system_context_section(product, context, hld)}

  <section>
    <h2>2. Component topology</h2>
    <div>{_chip_row(components)}</div>
  </section>
  {blocks_section}
  {sug_section}
  {api_section}
  {wf_section}

  <section>
    <h2>3. Architecture tenets</h2>
    <ul>{_list_items(tenets)}</ul>
  </section>

  <section>
    <h2>4. Low-level design — services</h2>
    <div class="grid">{"".join(service_cards)}</div>
  </section>

  <section>
    <h2>5. Caching &amp; NFR targets</h2>
    <div class="grid">
      <div>
        <table>{cache_rows}</table>
      </div>
      <div>
        <table>
          <tr><th>Redirect p99</th><td>{_esc((nfr.get("redirect_p99_ms") or perf_budget.get("redirect_p99_ms") or "—"))} ms</td></tr>
          <tr><th>Availability</th><td>{_esc(nfr.get("availability") or "—")}</td></tr>
          <tr><th>Cache hit target</th><td>{_esc(perf_budget.get("cache_hit_ratio_target") or "—")}</td></tr>
          <tr><th>Redirect RPS</th><td>{_esc(capacity.get("redirect_rps_target") or capacity.get("read_qps") or "—")}</td></tr>
        </table>
      </div>
    </div>
  </section>
  {capacity_section}
  {nfr_comp_section}

  <section>
    <h2>6. Sequence flows</h2>
    {"".join(seq_blocks)}
  </section>

  <section>
    <h2>7. ADRs</h2>
    <ul>{_list_items(adrs if isinstance(adrs, list) else [adrs])}</ul>
  </section>

  <section>
    <h2>8. Requirements mapped</h2>
    <ul>{_list_items(frs[:16], empty="No functional requirements in ReqSpec")}</ul>
  </section>
</main>
<footer>Forge Architecture Agent · design artifact · not a JSON dump</footer>
</body>
</html>
"""


def _default_url_shortener_lld(product: str) -> dict[str, Any]:
    """Bitly-clone LLD masterclass defaults (Snowflake + Base62 + Redis + 302)."""
    return {
        "style": "Bitly-clone LLD — Snowflake ID + Base62 + Redis cache-aside",
        "reference": "url-shortener-lld.md",
        "strategies_considered": [
            {
                "name": "Hashing (MD5/SHA truncated)",
                "verdict": "rejected",
                "rationale": "Collisions at scale; full hash too long for short links",
            },
            {
                "name": "Distributed unique ID + Base62 encoding",
                "verdict": "selected",
                "rationale": "Industry standard; unique without hash collision loops",
            },
            {
                "name": "Base-58 encoding",
                "verdict": "optional",
                "rationale": "Better readability (no 0/O/I/l); use if PRD prioritizes typeability",
            },
        ],
        "id_generation": {
            "strategy": "Twitter Snowflake (64-bit)",
            "layout": "timestamp | worker_id | sequence",
            "why": "No central counter hotspot; roughly time-ordered; unique per worker",
            "encoding": "Base62 (0-9a-zA-Z) → ~7 char codes for common ID ranges",
        },
        "encoding": {
            "alphabet": "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
            "name": "Base62",
            "encode": "repeated mod 62; remainders reversed → short_code",
            "decode": "Σ value(c_i) * 62^pos → numeric id",
            "min_length": 7,
        },
        "classes": [
            {
                "name": "UrlController",
                "stereotype": "controller",
                "responsibility": "POST shorten / DELETE link",
                "attributes": ["- urlService: UrlShorteningService", "- rateLimiter: RateLimiter"],
                "methods": ["+ shorten(req): ShortUrlResponse", "+ delete(code): void"],
            },
            {
                "name": "RedirectController",
                "stereotype": "controller",
                "responsibility": "GET /{code} → 302/404",
                "attributes": ["- urlService: UrlShorteningService"],
                "methods": ["+ redirect(code): ResponseEntity"],
            },
            {
                "name": "UrlShorteningService",
                "stereotype": "service",
                "responsibility": "Orchestrate create, lookup, disable",
                "attributes": [
                    "- idGenerator: SnowflakeIdGenerator",
                    "- encoder: Base62Encoder",
                    "- repository: UrlRepository",
                    "- cache: CacheService",
                ],
                "methods": [
                    "+ shorten(url, alias?): ShortUrl",
                    "+ resolve(code): String",
                    "+ delete(code): void",
                    "+ update(code, url): void",
                ],
            },
            {
                "name": "SnowflakeIdGenerator",
                "stereotype": "component",
                "responsibility": "Allocate unique 64-bit IDs",
                "attributes": ["- workerId: long", "- sequence: long"],
                "methods": ["+ nextId(): long"],
            },
            {
                "name": "Base62Encoder",
                "stereotype": "component",
                "responsibility": "ID ↔ short_code",
                "attributes": ["- alphabet: String"],
                "methods": ["+ encode(id): String", "+ decode(code): long"],
            },
            {
                "name": "UrlRepository",
                "stereotype": "repository",
                "responsibility": "Persist / load UrlMapping",
                "attributes": ["- db: DataSource"],
                "methods": [
                    "+ save(entity): UrlMapping",
                    "+ findByShortCode(code): Optional",
                    "+ delete(code): void",
                    "+ existsAlias(alias): boolean",
                ],
            },
            {
                "name": "CacheService",
                "stereotype": "component",
                "responsibility": "Redis cache-aside for redirects",
                "attributes": ["- redis: RedisClient"],
                "methods": ["+ get(code): String", "+ set(code, url): void", "+ invalidate(code): void"],
            },
            {
                "name": "RateLimiter",
                "stereotype": "component",
                "responsibility": "Per api_dev_key write limits",
                "attributes": ["- limit: int", "- windowSec: int"],
                "methods": ["+ allow(apiKey): boolean"],
            },
            {
                "name": "UrlMapping",
                "stereotype": "entity",
                "responsibility": "Persistence model",
                "attributes": [
                    "- id: long",
                    "- shortCode: String",
                    "- originalUrl: String",
                    "- expiresAt: Instant",
                    "- status: Status",
                ],
                "methods": ["+ isActive(): boolean"],
            },
        ],
        "relationships": [
            {"from": "UrlController", "to": "UrlShorteningService", "type": "dependency", "label": "uses"},
            {"from": "RedirectController", "to": "UrlShorteningService", "type": "dependency", "label": "uses"},
            {"from": "UrlShorteningService", "to": "SnowflakeIdGenerator", "type": "dependency", "label": "uses"},
            {"from": "UrlShorteningService", "to": "Base62Encoder", "type": "dependency", "label": "uses"},
            {"from": "UrlShorteningService", "to": "UrlRepository", "type": "dependency", "label": "uses"},
            {"from": "UrlShorteningService", "to": "CacheService", "type": "dependency", "label": "uses"},
            {"from": "UrlShorteningService", "to": "RateLimiter", "type": "dependency", "label": "uses"},
            {"from": "UrlRepository", "to": "UrlMapping", "type": "association", "label": "persists"},
        ],
        "entities": [
            {
                "name": "UrlMapping",
                "fields": [
                    {"name": "id", "type": "BIGINT", "notes": "Snowflake PK"},
                    {"name": "short_code", "type": "VARCHAR(16)", "notes": "UNIQUE"},
                    {"name": "original_url", "type": "TEXT", "notes": "destination"},
                    {"name": "user_id", "type": "VARCHAR", "notes": "nullable"},
                    {"name": "custom_alias", "type": "BOOLEAN", "notes": "true if user-chosen"},
                    {"name": "created_at", "type": "TIMESTAMP", "notes": ""},
                    {"name": "expires_at", "type": "TIMESTAMP", "notes": "nullable"},
                    {"name": "status", "type": "ENUM", "notes": "active|disabled|expired"},
                ],
                "indexes": ["UNIQUE(short_code)", "INDEX(user_id, created_at)", "INDEX(expires_at)"],
            }
        ],
        "apis": {
            "shorten": {
                "method": "POST",
                "path": "/api/v1/shorten",
                "request": {
                    "url": "string (required)",
                    "customAlias": "string?",
                    "expiresAt": "ISO-8601?",
                },
                "response": {
                    "shortUrl": "https://host/{code}",
                    "code": "string",
                    "expiresAt": "ISO-8601?",
                },
                "status": 201,
            },
            "redirect": {
                "method": "GET",
                "path": "/{code}",
                "response": "302 Location: original_url | 404",
                "status": 302,
            },
            "delete": {
                "method": "DELETE",
                "path": "/api/v1/links/{code}",
                "response": "200 removed | 404",
                "status": 200,
            },
        },
        "redirect_policy": {
            "default": "302 Temporary Redirect",
            "rationale": (
                "Every click hits origin → accurate analytics, expiry, and disable. "
                "Bare 301 may be cached forever by browsers (interview trap)."
            ),
            "alternative": "301 + Cache-Control: private, max-age=90 for bounded browser cache",
        },
        "cache": {
            "store": "Redis (ElastiCache)",
            "key": "url:{short_code}",
            "value": "original_url (+ optional metadata JSON)",
            "pattern": "cache-aside on redirect",
            "ttl": "aligned with link expiry; default hours–days for hot keys",
            "invalidation": "delete / update / expire → DEL url:{code}",
        },
        "write_flow": [
            "Validate URL + AuthZ + rate limit (api_dev_key)",
            "Custom alias? uniqueness check : Snowflake nextId → Base62 encode",
            "Persist UrlMapping (UNIQUE short_code)",
            "Optional cache SET",
            "Return 201 { shortUrl, code }",
            "Async outbox for analytics / audit (never on critical path)",
        ],
        "read_flow": [
            "Parse code from path",
            "Redis GET url:{code}",
            "Hit → 302 Location",
            "Miss → DB findByShortCode → if active, cache SET → 302",
            "Missing / expired / disabled → 404",
            "Emit click event asynchronously after response",
        ],
        "services": {
            "redirect-api": {
                "handlers": ["redirect(code)"],
                "deps": ["CacheService", "UrlRepository"],
                "invariants": ["cache-first", "never block on analytics", "302 by default"],
                "methods": ["redirect"],
            },
            "link-api": {
                "handlers": ["shorten", "delete", "update", "customAlias"],
                "deps": [
                    "SnowflakeIdGenerator",
                    "Base62Encoder",
                    "UrlRepository",
                    "CacheService",
                    "RateLimiter",
                ],
                "invariants": ["unique short_code", "rate-limited writes"],
                "methods": ["shorten", "delete", "update"],
            },
            "short-url-generator": {
                "handlers": ["nextId", "encode", "decode"],
                "deps": ["SnowflakeIdGenerator", "Base62Encoder"],
                "invariants": ["globally unique IDs", "deterministic encode/decode"],
                "methods": ["allocateCode"],
            },
        },
        "consistency": (
            "Strong uniqueness on short_code at write; cache-aside eventual for reads; "
            "geo replicas may lag briefly after create"
        ),
        "scaling": [
            "Stateless app servers behind ALB",
            "Redis cluster for hot keys",
            "Unique Snowflake worker_id per instance",
            "Shard DB by short_code hash at extreme scale",
            "Async analytics via Kafka/MSK",
        ],
    }


def _merge_lld_defaults(product: str, lld: dict[str, Any], hld: dict[str, Any]) -> dict[str, Any]:
    """Fill missing LLD masterclass fields for URL shorteners."""
    style = str(hld.get("style") or lld.get("style") or "").lower()
    comps = " ".join(str(c) for c in (hld.get("components") or [])).lower()
    is_shortener = any(
        k in style or k in comps or k in product.lower()
        for k in ("url short", "shortener", "tinyurl", "bitly", "snipr", "base-58", "base58", "base62")
    ) or bool(hld.get("short_url_generator"))
    if not is_shortener:
        return lld
    defaults = _default_url_shortener_lld(product)
    out = dict(defaults)
    out.update({k: v for k, v in lld.items() if v not in (None, "", [], {})})
    # Deep-merge services
    svc = dict(defaults.get("services") or {})
    svc.update(lld.get("services") or {})
    out["services"] = svc
    for key in (
        "classes",
        "entities",
        "apis",
        "write_flow",
        "read_flow",
        "strategies_considered",
        "id_generation",
        "encoding",
        "cache",
        "redirect_policy",
        "scaling",
        "relationships",
    ):
        if not lld.get(key):
            out[key] = defaults[key]
    return out


def _uml_trunc(text: str, max_chars: int = 28) -> str:
    t = str(text)
    return t if len(t) <= max_chars else t[: max_chars - 1] + "…"


def _uml_box(
    *,
    x: float,
    y: float,
    w: float,
    h: float,
    name: str,
    stereotype: str,
    attributes: list[str],
    methods: list[str],
) -> str:
    """Render one fixed-size UML class box."""
    attrs = [_uml_trunc(a) for a in (attributes or [])][:4] or ["—"]
    meths = [_uml_trunc(m) for m in (methods or [])][:4] or ["—"]
    header_h = 34
    sep1 = y + header_h
    attr_top = sep1 + 14
    row_h = 15
    sep2 = attr_top + 4 * row_h + 2
    meth_top = sep2 + 14
    attr_lines = "".join(
        f'<text x="{x + 8}" y="{attr_top + i * row_h}" fill="#b7c7d0" font-size="9.5" '
        f'font-family="IBM Plex Mono,monospace">{_esc(a)}</text>'
        for i, a in enumerate(attrs)
    )
    meth_lines = "".join(
        f'<text x="{x + 8}" y="{meth_top + i * row_h}" fill="#cfe0e8" font-size="9.5" '
        f'font-family="IBM Plex Mono,monospace">{_esc(m)}</text>'
        for i, m in enumerate(meths)
    )
    stroke = "#5eb1e8" if stereotype == "entity" else "#3ecf8e"
    stereo = (
        f'<text x="{x + w / 2}" y="{y + 12}" text-anchor="middle" fill="#8aa0ad" '
        f'font-size="9" font-family="DM Sans,sans-serif">&laquo;{_esc(stereotype)}&raquo;</text>'
        if stereotype
        else ""
    )
    name_y = y + (26 if stereotype else 20)
    return f"""
    <g class="uml-class" data-class="{_esc(name)}">
      <rect x="{x}" y="{y}" width="{w}" height="{h}" rx="4" fill="#152028" stroke="{stroke}" stroke-width="1.5"/>
      {stereo}
      <text x="{x + w / 2}" y="{name_y}" text-anchor="middle" fill="#e6eef2" font-size="11.5"
            font-weight="700" font-family="DM Sans,sans-serif">{_esc(_uml_trunc(name, 22))}</text>
      <line x1="{x}" y1="{sep1}" x2="{x + w}" y2="{sep1}" stroke="#2a3f4d"/>
      {attr_lines}
      <line x1="{x}" y1="{sep2}" x2="{x + w}" y2="{sep2}" stroke="#2a3f4d"/>
      {meth_lines}
    </g>"""


def _render_uml_class_diagram(
    classes: list[Any],
    relationships: list[Any] | None = None,
) -> str:
    """Aligned UML class diagram with orthogonal from→to connectors + motion."""
    by_name: dict[str, dict[str, Any]] = {}
    for c in classes or []:
        if isinstance(c, dict) and c.get("name"):
            meths = []
            for m in c.get("methods") or []:
                s = str(m)
                meths.append(s if s.startswith(("+", "-", "#")) else f"+ {s}()")
            by_name[str(c["name"])] = {
                "name": str(c["name"]),
                "stereotype": str(c.get("stereotype") or ""),
                "attributes": [str(a) for a in (c.get("attributes") or [])],
                "methods": meths,
            }
        elif c:
            by_name[str(c)] = {
                "name": str(c),
                "stereotype": "",
                "attributes": [],
                "methods": [],
            }
    if not by_name:
        return "<p class='muted'>No classes available for UML diagram.</p>"

    def _pick(*names: str) -> list[dict[str, Any]]:
        return [by_name[n] for n in names if n in by_name]

    # Prefer stable Bitly LLD order; fall back to stereotype grouping
    controllers = _pick("UrlController", "RedirectController")
    services = _pick("UrlShorteningService")
    components = _pick(
        "SnowflakeIdGenerator",
        "Base62Encoder",
        "UrlRepository",
        "CacheService",
        "RateLimiter",
    )
    entities = _pick("UrlMapping")

    used = {c["name"] for c in controllers + services + components + entities}
    for c in by_name.values():
        if c["name"] in used:
            continue
        st, name = c["stereotype"], c["name"].lower()
        if st == "controller" or "controller" in name:
            controllers.append(c)
        elif st == "service" or "service" in name:
            services.append(c)
        elif st == "entity" or "mapping" in name or "entity" in name:
            entities.append(c)
        else:
            components.append(c)

    layers = [L for L in (controllers, services, components, entities) if L]

    box_w, box_h = 196, 168
    gap_x, gap_y = 40, 56
    margin_x, margin_y = 48, 36
    max_cols = max(len(L) for L in layers)
    width = max(margin_x * 2 + max_cols * box_w + (max_cols - 1) * gap_x, 920)

    positions: dict[str, tuple[float, float, float, float]] = {}
    boxes_svg: list[str] = []
    y = float(margin_y)
    for layer in layers:
        n = len(layer)
        layer_w = n * box_w + max(0, n - 1) * gap_x
        x0 = (width - layer_w) / 2
        for i, c in enumerate(layer):
            x = x0 + i * (box_w + gap_x)
            boxes_svg.append(
                _uml_box(
                    x=x,
                    y=y,
                    w=box_w,
                    h=box_h,
                    name=c["name"],
                    stereotype=c["stereotype"],
                    attributes=c["attributes"],
                    methods=c["methods"],
                )
            )
            positions[c["name"]] = (x, y, box_w, box_h)
        y += box_h + gap_y

    rels = list(relationships or [])
    if not rels:
        svc = "UrlShorteningService" if "UrlShorteningService" in positions else None
        if not svc:
            svc = next((n for n in positions if "service" in n.lower()), None)
        for n in positions:
            if "controller" in n.lower() and svc:
                rels.append({"from": n, "to": svc, "type": "dependency", "label": "uses"})
        if svc:
            for n in (
                "SnowflakeIdGenerator",
                "Base62Encoder",
                "UrlRepository",
                "CacheService",
                "RateLimiter",
            ):
                if n in positions:
                    rels.append({"from": svc, "to": n, "type": "dependency", "label": "uses"})
        if "UrlController" in positions and "RateLimiter" in positions:
            # already covered via service→RateLimiter; keep controller→rate if present in defaults
            pass
        if "UrlRepository" in positions and "UrlMapping" in positions:
            rels.append(
                {"from": "UrlRepository", "to": "UrlMapping", "type": "association", "label": "persists"}
            )

    # Drop invalid / duplicate edges
    seen: set[tuple[str, str]] = set()
    clean_rels: list[dict[str, Any]] = []
    for r in rels:
        if not isinstance(r, dict):
            continue
        a, b = str(r.get("from") or ""), str(r.get("to") or "")
        if a not in positions or b not in positions or a == b:
            continue
        key = (a, b)
        if key in seen:
            continue
        seen.add(key)
        clean_rels.append(r)
    rels = clean_rels

    # Fan-out slot index on shared source bottom / target top
    out_slots: dict[str, list[str]] = {}
    in_slots: dict[str, list[str]] = {}
    for r in rels:
        a, b = str(r["from"]), str(r["to"])
        out_slots.setdefault(a, []).append(b)
        in_slots.setdefault(b, []).append(a)

    def _port(box: tuple[float, float, float, float], side: str, index: int, total: int) -> tuple[float, float]:
        x, y0, w, h = box
        # Spread ports evenly across the side (keep inset from corners)
        t = (index + 1) / (total + 1)
        if side == "bottom":
            return x + w * t, y0 + h
        if side == "top":
            return x + w * t, y0
        if side == "right":
            return x + w, y0 + h * t
        return x, y0 + h * t

    edges: list[str] = []
    packets: list[str] = []
    for edge_i, r in enumerate(rels):
        a, b = str(r["from"]), str(r["to"])
        ax, ay, aw, ah = positions[a]
        bx, by, bw, bh = positions[b]
        a_cx, a_cy = ax + aw / 2, ay + ah / 2
        b_cx, b_cy = bx + bw / 2, by + bh / 2

        # Dominant direction decides sides (prefer vertical stack routing)
        if abs(b_cy - a_cy) >= abs(b_cx - a_cx) * 0.35:
            if b_cy > a_cy:
                side1, side2 = "bottom", "top"
            else:
                side1, side2 = "top", "bottom"
        else:
            if b_cx > a_cx:
                side1, side2 = "right", "left"
            else:
                side1, side2 = "left", "right"

        outs = out_slots[a]
        ins = in_slots[b]
        oi = outs.index(b)
        ii = ins.index(a)
        x1, y1 = _port(positions[a], side1, oi, len(outs))
        x2, y2 = _port(positions[b], side2, ii, len(ins))

        # Clean orthogonal route with one elbow lane in the gap
        if side1 == "bottom" and side2 == "top":
            mid_y = (y1 + y2) / 2
            if abs(x1 - x2) < 1.5:
                path_d = f"M{x1:.1f},{y1:.1f} L{x2:.1f},{y2:.1f}"
            else:
                path_d = (
                    f"M{x1:.1f},{y1:.1f} L{x1:.1f},{mid_y:.1f} "
                    f"L{x2:.1f},{mid_y:.1f} L{x2:.1f},{y2:.1f}"
                )
            lx, ly = (x1 + x2) / 2, mid_y - 6
        elif side1 == "top" and side2 == "bottom":
            mid_y = (y1 + y2) / 2
            if abs(x1 - x2) < 1.5:
                path_d = f"M{x1:.1f},{y1:.1f} L{x2:.1f},{y2:.1f}"
            else:
                path_d = (
                    f"M{x1:.1f},{y1:.1f} L{x1:.1f},{mid_y:.1f} "
                    f"L{x2:.1f},{mid_y:.1f} L{x2:.1f},{y2:.1f}"
                )
            lx, ly = (x1 + x2) / 2, mid_y - 6
        elif side1 in ("left", "right") and side2 in ("left", "right"):
            mid_x = (x1 + x2) / 2
            path_d = (
                f"M{x1:.1f},{y1:.1f} L{mid_x:.1f},{y1:.1f} "
                f"L{mid_x:.1f},{y2:.1f} L{x2:.1f},{y2:.1f}"
            )
            lx, ly = mid_x, (y1 + y2) / 2 - 6
        else:
            # L-shape
            path_d = f"M{x1:.1f},{y1:.1f} L{x1:.1f},{y2:.1f} L{x2:.1f},{y2:.1f}"
            lx, ly = x1 + 8, (y1 + y2) / 2

        is_dep = str(r.get("type") or "dependency") == "dependency"
        color = "#5eb1e8" if is_dep else "#3ecf8e"
        label = str(r.get("label") or ("uses" if is_dep else "persists"))
        pid = f"uml-e{edge_i}"
        delay = f"{(edge_i % 8) * 0.28:.2f}s"
        dash = 'stroke-dasharray="6 5"' if is_dep else ""
        mk = "dep" if is_dep else "assoc"
        edges.append(
            f'<path id="{pid}" class="uml-edge {"uml-dep" if is_dep else "uml-assoc"}" '
            f'd="{path_d}" fill="none" stroke="{color}" stroke-width="1.7" {dash} '
            f'marker-end="url(#uml-arrow-{mk})"/>'
            f'<text class="uml-edge-label" x="{lx:.1f}" y="{ly:.1f}" text-anchor="middle" '
            f'fill="{color}" font-size="9" font-family="DM Sans,sans-serif">{_esc(label)}</text>'
        )
        packets.append(
            f'<circle class="uml-packet" r="3.5" fill="{color}">'
            f'<animateMotion dur="2.6s" begin="{delay}" repeatCount="indefinite" rotate="auto">'
            f'<mpath href="#{pid}"/>'
            f"</animateMotion></circle>"
        )

    height = y - gap_y + margin_y
    return f"""
    <div class="uml-wrap" aria-label="UML class diagram">
      <div class="uml-legend">
        <span><i class="dep"></i> dependency → depends on</span>
        <span><i class="assoc"></i> association →</span>
        <span>packets animate along from → to</span>
      </div>
      <svg class="uml-svg" viewBox="0 0 {width:.0f} {height:.0f}" role="img">
        <defs>
          <marker id="uml-arrow-dep" markerWidth="9" markerHeight="9" refX="8" refY="3" orient="auto">
            <path d="M0,0 L8,3 L0,6 Z" fill="#5eb1e8"/>
          </marker>
          <marker id="uml-arrow-assoc" markerWidth="9" markerHeight="9" refX="8" refY="3" orient="auto">
            <path d="M0,0 L8,3 L0,6 Z" fill="#3ecf8e"/>
          </marker>
        </defs>
        <g class="uml-edges">{"".join(edges)}</g>
        <g class="uml-boxes">{"".join(boxes_svg)}</g>
        <g class="uml-packets">{"".join(packets)}</g>
      </svg>
    </div>"""


def build_lld_html(
    *,
    product: str,
    lld: dict[str, Any],
    hld: dict[str, Any] | None = None,
    reqspec: dict[str, Any] | None = None,
) -> str:
    """Self-contained Low-Level Design HTML (Bitly LLD masterclass layout)."""
    hld = hld or {}
    reqspec = reqspec or {}
    lld = _merge_lld_defaults(product, lld if isinstance(lld, dict) else {}, hld)
    services = lld.get("services") or {}
    fr_ids = lld.get("derived_from_fr") or [f.get("id") for f in (reqspec.get("fr") or [])[:12]]
    classes = lld.get("classes") or []
    entities = lld.get("entities") or []
    apis = lld.get("apis") or {}
    strategies = lld.get("strategies_considered") or []
    id_gen = lld.get("id_generation") or {}
    encoding = lld.get("encoding") or {}
    cache = lld.get("cache") or {}
    redirect_policy = lld.get("redirect_policy") or {}
    write_flow = lld.get("write_flow") or []
    read_flow = lld.get("read_flow") or []
    scaling = lld.get("scaling") or []

    cards = []
    for svc, meta in services.items():
        if not isinstance(meta, dict):
            meta = {"detail": str(meta)}
        handlers = meta.get("handlers") or meta.get("methods") or []
        deps = meta.get("deps") or []
        inv = meta.get("invariants") or []
        cards.append(
            f"""
            <article class="card">
              <h3>{_esc(svc)}</h3>
              <p class="k">Handlers / methods</p>
              <ul>{_list_items(handlers, empty="—")}</ul>
              <p class="k">Dependencies</p>
              <ul>{_list_items(deps, empty="—")}</ul>
              <p class="k">Invariants</p>
              <ul>{_list_items(inv, empty="—")}</ul>
            </article>"""
        )
    if not cards:
        cards.append("<p class='muted'>No LLD services in this design.</p>")

    relationships = lld.get("relationships") or []
    uml_diagram = _render_uml_class_diagram(classes, relationships if isinstance(relationships, list) else [])

    class_cards = []
    for c in classes:
        if not isinstance(c, dict):
            class_cards.append(f"<article class='card'><h3>{_esc(c)}</h3></article>")
            continue
        stereo = c.get("stereotype") or ""
        stereo_html = (
            f" <span class='pill optional'>{_esc(stereo)}</span>" if stereo else ""
        )
        class_cards.append(
            f"""
            <article class="card">
              <h3>{_esc(c.get("name"))}{stereo_html}</h3>
              <p>{_esc(c.get("responsibility") or "")}</p>
              <p class="k">Attributes</p>
              <ul>{_list_items(c.get("attributes") or [], empty="—")}</ul>
              <p class="k">Methods</p>
              <ul>{_list_items(c.get("methods") or [], empty="—")}</ul>
            </article>"""
        )

    strategy_rows = []
    for s in strategies:
        if isinstance(s, dict):
            strategy_rows.append(
                "<tr>"
                f"<td>{_esc(s.get('name'))}</td>"
                f"<td><span class='pill {_esc(s.get('verdict') or '')}'>{_esc(s.get('verdict'))}</span></td>"
                f"<td>{_esc(s.get('rationale'))}</td>"
                "</tr>"
            )
        else:
            strategy_rows.append(f"<tr><td colspan='3'>{_esc(s)}</td></tr>")

    entity_html = []
    for ent in entities:
        if not isinstance(ent, dict):
            continue
        fields = ent.get("fields") or []
        field_rows = []
        for f in fields:
            if isinstance(f, dict):
                field_rows.append(
                    "<tr>"
                    f"<td><code>{_esc(f.get('name'))}</code></td>"
                    f"<td>{_esc(f.get('type'))}</td>"
                    f"<td>{_esc(f.get('notes'))}</td>"
                    "</tr>"
                )
            else:
                field_rows.append(f"<tr><td colspan='3'>{_esc(f)}</td></tr>")
        entity_html.append(
            f"""
            <div class="card">
              <h3>{_esc(ent.get("name"))}</h3>
              <table>
                <tr><th>Field</th><th>Type</th><th>Notes</th></tr>
                {"".join(field_rows)}
              </table>
              <p class="k">Indexes</p>
              <ul>{_list_items(ent.get("indexes") or [], empty="—")}</ul>
            </div>"""
        )

    api_blocks = []
    for name, spec in apis.items():
        if not isinstance(spec, dict):
            api_blocks.append(f"<div class='card'><h3>{_esc(name)}</h3><pre>{_esc(spec)}</pre></div>")
            continue
        req = spec.get("request")
        req_html = ""
        if isinstance(req, dict):
            req_html = "<table>" + "".join(
                f"<tr><th>{_esc(k)}</th><td>{_esc(v)}</td></tr>" for k, v in req.items()
            ) + "</table>"
        elif req:
            req_html = f"<pre>{_esc(req)}</pre>"
        resp = spec.get("response")
        if isinstance(resp, dict):
            resp_html = "<table>" + "".join(
                f"<tr><th>{_esc(k)}</th><td>{_esc(v)}</td></tr>" for k, v in resp.items()
            ) + "</table>"
        else:
            resp_html = f"<pre>{_esc(resp)}</pre>"
        api_blocks.append(
            f"""
            <article class="card api-card">
              <h3><span class="verb">{_esc(spec.get("method") or "")}</span> {_esc(spec.get("path") or name)}</h3>
              <p class="k">Request</p>{req_html or "<p class='muted'>—</p>"}
              <p class="k">Response ({_esc(spec.get("status") or "")})</p>{resp_html}
            </article>"""
        )

    id_rows = "".join(
        f"<tr><th>{_esc(k)}</th><td>{_esc(v)}</td></tr>" for k, v in id_gen.items()
    )
    enc_rows = "".join(
        f"<tr><th>{_esc(k)}</th><td><code>{_esc(v)}</code></td></tr>" for k, v in encoding.items()
    )
    cache_rows = "".join(
        f"<tr><th>{_esc(k)}</th><td>{_esc(v)}</td></tr>" for k, v in cache.items()
    )
    redir_rows = "".join(
        f"<tr><th>{_esc(k)}</th><td>{_esc(v)}</td></tr>" for k, v in redirect_policy.items()
    )

    # Compact LLD sequence SVG (write + read)
    flow_svg = f"""
    <div class="lld-flow">
      <svg viewBox="0 0 1100 220" class="lld-flow-svg" role="img" aria-label="LLD write and read flows">
        <defs>
          <marker id="lld-mk" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto">
            <path d="M0,0 L7,3 L0,6 Z" fill="#3ecf8e"/>
          </marker>
          <marker id="lld-mk-b" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto">
            <path d="M0,0 L7,3 L0,6 Z" fill="#5eb1e8"/>
          </marker>
        </defs>
        <text x="20" y="28" fill="#3ecf8e" font-size="12" font-weight="700" font-family="IBM Plex Mono,monospace">WRITE · SHORTEN</text>
        <rect x="20" y="44" width="120" height="44" rx="8" fill="#152028" stroke="#3ecf8e"/>
        <text x="80" y="71" text-anchor="middle" fill="#e6eef2" font-size="11" font-family="DM Sans,sans-serif">Controller</text>
        <rect x="180" y="44" width="130" height="44" rx="8" fill="#152028" stroke="#3ecf8e"/>
        <text x="245" y="71" text-anchor="middle" fill="#e6eef2" font-size="11" font-family="DM Sans,sans-serif">Service</text>
        <rect x="350" y="44" width="130" height="44" rx="8" fill="#152028" stroke="#3ecf8e"/>
        <text x="415" y="71" text-anchor="middle" fill="#e6eef2" font-size="11" font-family="DM Sans,sans-serif">Snowflake+B62</text>
        <rect x="520" y="44" width="120" height="44" rx="8" fill="#152028" stroke="#3ecf8e"/>
        <text x="580" y="71" text-anchor="middle" fill="#e6eef2" font-size="11" font-family="DM Sans,sans-serif">Repository</text>
        <rect x="680" y="44" width="100" height="44" rx="8" fill="#152028" stroke="#5eb1e8"/>
        <text x="730" y="71" text-anchor="middle" fill="#e6eef2" font-size="11" font-family="DM Sans,sans-serif">Redis</text>
        <rect x="820" y="44" width="120" height="44" rx="8" fill="#152028" stroke="#3ecf8e"/>
        <text x="880" y="71" text-anchor="middle" fill="#e6eef2" font-size="11" font-family="DM Sans,sans-serif">201 shortUrl</text>
        <line x1="140" y1="66" x2="180" y2="66" stroke="#3ecf8e" stroke-width="2" marker-end="url(#lld-mk)"/>
        <line x1="310" y1="66" x2="350" y2="66" stroke="#3ecf8e" stroke-width="2" marker-end="url(#lld-mk)"/>
        <line x1="480" y1="66" x2="520" y2="66" stroke="#3ecf8e" stroke-width="2" marker-end="url(#lld-mk)"/>
        <line x1="640" y1="66" x2="680" y2="66" stroke="#5eb1e8" stroke-width="2" marker-end="url(#lld-mk-b)"/>
        <line x1="780" y1="66" x2="820" y2="66" stroke="#3ecf8e" stroke-width="2" marker-end="url(#lld-mk)"/>
        <text x="20" y="130" fill="#5eb1e8" font-size="12" font-weight="700" font-family="IBM Plex Mono,monospace">READ · REDIRECT (cache-aside)</text>
        <rect x="20" y="146" width="120" height="44" rx="8" fill="#152028" stroke="#5eb1e8"/>
        <text x="80" y="173" text-anchor="middle" fill="#e6eef2" font-size="11" font-family="DM Sans,sans-serif">GET /code</text>
        <rect x="180" y="146" width="120" height="44" rx="8" fill="#152028" stroke="#5eb1e8"/>
        <text x="240" y="173" text-anchor="middle" fill="#e6eef2" font-size="11" font-family="DM Sans,sans-serif">Redis GET</text>
        <rect x="340" y="146" width="130" height="44" rx="8" fill="#152028" stroke="#5eb1e8"/>
        <text x="405" y="173" text-anchor="middle" fill="#e6eef2" font-size="11" font-family="DM Sans,sans-serif">DB on miss</text>
        <rect x="510" y="146" width="130" height="44" rx="8" fill="#152028" stroke="#5eb1e8"/>
        <text x="575" y="173" text-anchor="middle" fill="#e6eef2" font-size="11" font-family="DM Sans,sans-serif">SET cache</text>
        <rect x="680" y="146" width="140" height="44" rx="8" fill="#152028" stroke="#3ecf8e"/>
        <text x="750" y="173" text-anchor="middle" fill="#e6eef2" font-size="11" font-family="DM Sans,sans-serif">302 Location</text>
        <line x1="140" y1="168" x2="180" y2="168" stroke="#5eb1e8" stroke-width="2" marker-end="url(#lld-mk-b)"/>
        <line x1="300" y1="168" x2="340" y2="168" stroke="#5eb1e8" stroke-width="2" stroke-dasharray="4 3" marker-end="url(#lld-mk-b)"/>
        <line x1="470" y1="168" x2="510" y2="168" stroke="#5eb1e8" stroke-width="2" marker-end="url(#lld-mk-b)"/>
        <line x1="640" y1="168" x2="680" y2="168" stroke="#3ecf8e" stroke-width="2" marker-end="url(#lld-mk)"/>
        <text x="20" y="212" fill="#8aa0ad" font-size="11" font-family="DM Sans,sans-serif">
          {_esc(product)} LLD · Snowflake → Base62 · Redis cache-aside · 302 for analytics control
        </text>
      </svg>
    </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{_esc(product)} — Low-Level Design</title>
<style>
{_FORGE_THEME_ROOT}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; font-family:"DM Sans",system-ui,sans-serif; color:var(--text); background:var(--bg0); }}
{_FORGE_HEADER_CSS}
  main {{ max-width:1100px; margin:0 auto; padding:1.2rem; display:grid; gap:1rem; }}
  section {{ background:var(--bg1); border:1px solid var(--line); border-radius:10px; padding:1.1rem; }}
  h2 {{ margin:0 0 .7rem; font-size:.78rem; text-transform:uppercase; letter-spacing:.08em; color:var(--muted); }}
  h3 {{ margin:0 0 .4rem; font-size:1rem; color:var(--text); }}
  .grid {{ display:grid; gap:.8rem; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); }}
  .card {{ border:1px solid var(--line); border-radius:10px; padding:.85rem; background:var(--bg2); color:var(--text); }}
  .k {{ margin:.55rem 0 .2rem; font-size:.72rem; text-transform:uppercase; color:var(--muted); }}
  ul {{ margin:.2rem 0 0; padding-left:1.1rem; color:var(--text); }}
  .muted {{ color:var(--muted); }}
  table {{ width:100%; border-collapse:collapse; }}
  th, td {{ text-align:left; padding:.4rem .3rem; border-bottom:1px solid var(--line); vertical-align:top; color:var(--text); font-size:.9rem; }}
  th {{ color:var(--muted); font-weight:600; width:28%; }}
  code {{ font-family:"IBM Plex Mono",monospace; font-size:.82rem; color:var(--accent); }}
  pre {{ margin:.3rem 0 0; white-space:pre-wrap; background:#0b1220; padding:.7rem; border-radius:8px; border:1px solid var(--line); font-size:.82rem; }}
  .pill {{ display:inline-block; padding:.12rem .45rem; border-radius:999px; font-size:.72rem; text-transform:uppercase; border:1px solid var(--line); }}
  .pill.selected {{ color:#0e1419; background:var(--accent); border-color:var(--accent); }}
  .pill.rejected {{ color:#f0b4b4; background:#3a1f1f; }}
  .pill.optional {{ color:var(--info); background:color-mix(in srgb, var(--info) 15%, var(--bg2)); }}
  .verb {{ display:inline-block; min-width:3.2rem; color:var(--accent); font-family:"IBM Plex Mono",monospace; font-size:.8rem; }}
  .lld-flow {{ margin-top:.4rem; padding:.6rem; border-radius:10px; background:var(--bg2); border:1px solid var(--line); overflow-x:auto; }}
  .lld-flow-svg {{ width:100%; min-width:780px; height:auto; display:block; }}
  .uml-wrap {{
    padding:.75rem; border-radius:10px; background:linear-gradient(180deg,#101820,#152028);
    border:1px solid var(--line); overflow-x:auto;
  }}
  .uml-legend {{
    display:flex; flex-wrap:wrap; gap:.9rem; margin-bottom:.55rem;
    font-size:.78rem; color:var(--muted);
  }}
  .uml-legend i {{
    display:inline-block; width:22px; height:2px; margin-right:.35rem; vertical-align:middle;
    background:var(--muted);
  }}
  .uml-legend i.dep {{
    background:repeating-linear-gradient(90deg, #5eb1e8 0 4px, transparent 4px 7px);
  }}
  .uml-legend i.assoc {{ background:var(--accent); }}
  .uml-svg {{ width:100%; min-width:760px; height:auto; display:block; }}
  .uml-edge {{
    stroke-linecap:round; stroke-linejoin:round;
  }}
  .uml-dep {{
    animation: uml-dash-flow 1.2s linear infinite;
  }}
  .uml-assoc {{
    stroke-dasharray: 10 0;
    animation: uml-pulse-stroke 2.4s ease-in-out infinite;
  }}
  @keyframes uml-dash-flow {{
    to {{ stroke-dashoffset: -44; }}
  }}
  @keyframes uml-pulse-stroke {{
    0%, 100% {{ opacity: 0.75; }}
    50% {{ opacity: 1; }}
  }}
  .uml-packet {{
    filter: drop-shadow(0 0 3px rgba(94,177,232,0.85));
  }}
  @media (prefers-reduced-motion: reduce) {{
    .uml-dep, .uml-assoc {{ animation: none !important; }}
    .uml-packet {{ display: none; }}
  }}
  .steps {{ counter-reset: step; list-style:none; padding:0; margin:0; }}
  .steps li {{
    position:relative; padding:.45rem .5rem .45rem 2.2rem; border-bottom:1px solid var(--line);
  }}
  .steps li::before {{
    counter-increment: step; content: counter(step);
    position:absolute; left:0; top:.45rem; width:1.5rem; height:1.5rem; border-radius:50%;
    background:var(--soft); color:var(--accent); font-size:.72rem; font-weight:700;
    display:flex; align-items:center; justify-content:center; font-family:"IBM Plex Mono",monospace;
  }}
  footer {{ text-align:center; color:var(--muted); font-size:.8rem; padding:0 0 1.4rem; }}
</style>
</head>
<body>
<header>
  <h1>{_esc(product)} · LLD</h1>
  <p>Low-Level Design masterclass — classes, data model, APIs, Snowflake/Base62, Redis, 301 vs 302.
     {_esc(lld.get("style") or hld.get("style") or "service LLD")}.</p>
</header>
<main>
  <section>
    <h2>1. Write &amp; read flows</h2>
    {flow_svg}
    <div class="grid" style="margin-top:.85rem">
      <div>
        <p class="k">Write path</p>
        <ol class="steps">{"".join(f"<li>{_esc(s)}</li>" for s in write_flow) or "<li class='muted'>—</li>"}</ol>
      </div>
      <div>
        <p class="k">Read path</p>
        <ol class="steps">{"".join(f"<li>{_esc(s)}</li>" for s in read_flow) or "<li class='muted'>—</li>"}</ol>
      </div>
    </div>
  </section>

  <section>
    <h2>2. Short-code strategies</h2>
    <table>
      <tr><th>Approach</th><th>Verdict</th><th>Rationale</th></tr>
      {"".join(strategy_rows) or "<tr><td colspan='3' class='muted'>Not specified</td></tr>"}
    </table>
    <div class="grid" style="margin-top:.85rem">
      <div class="card">
        <h3>ID generation</h3>
        <table>{id_rows or "<tr><td class='muted'>—</td></tr>"}</table>
      </div>
      <div class="card">
        <h3>Encoding</h3>
        <table>{enc_rows or "<tr><td class='muted'>—</td></tr>"}</table>
      </div>
    </div>
  </section>

  <section>
    <h2>3. Classes / modules</h2>
    <p class="muted" style="margin:0 0 .75rem">UML class diagram — controllers → service → components → entity.</p>
    {uml_diagram}
    <div class="grid" style="margin-top:.9rem">{"".join(class_cards) or "<p class='muted'>No classes listed</p>"}</div>
  </section>

  <section>
    <h2>4. Entity / data model</h2>
    <div class="grid">{"".join(entity_html) or "<p class='muted'>No entities listed</p>"}</div>
  </section>

  <section>
    <h2>5. API contracts</h2>
    <div class="grid">{"".join(api_blocks) or "<p class='muted'>No APIs listed</p>"}</div>
  </section>

  <section>
    <h2>6. Cache &amp; redirect policy</h2>
    <div class="grid">
      <div class="card">
        <h3>Redis cache-aside</h3>
        <table>{cache_rows or "<tr><td class='muted'>—</td></tr>"}</table>
      </div>
      <div class="card">
        <h3>301 vs 302</h3>
        <table>{redir_rows or "<tr><td class='muted'>—</td></tr>"}</table>
      </div>
    </div>
  </section>

  <section>
    <h2>7. Service design</h2>
    <div class="grid">{"".join(cards)}</div>
  </section>

  <section>
    <h2>8. Consistency &amp; scaling</h2>
    <p>{_esc(lld.get("consistency") or "See HLD tenets")}</p>
    <ul>{_list_items(scaling, empty="No scaling notes")}</ul>
  </section>

  <section>
    <h2>9. Requirements mapped</h2>
    <ul>{_list_items(fr_ids, empty="No FR mapping")}</ul>
  </section>
</main>
<footer>Forge Architecture Agent · LLD masterclass design</footer>
</body>
</html>
"""


def default_url_shortener_schema(product: str) -> dict[str, Any]:
    """Canonical Bitly/TinyURL schema with typed columns for DB Design."""
    entities = [
        {
            "name": "organizations",
            "description": "Tenant / billing account",
            "columns": [
                {"name": "id", "type": "BIGINT", "nullable": False, "key": "PK", "notes": "Snowflake"},
                {"name": "name", "type": "VARCHAR(255)", "nullable": False, "key": "", "notes": ""},
                {"name": "plan", "type": "VARCHAR(64)", "nullable": False, "key": "", "notes": "free|pro|enterprise"},
                {"name": "created_at", "type": "TIMESTAMPTZ", "nullable": False, "key": "", "notes": "DEFAULT now()"},
            ],
        },
        {
            "name": "users",
            "description": "Operators belonging to an organization",
            "columns": [
                {"name": "id", "type": "BIGINT", "nullable": False, "key": "PK", "notes": ""},
                {"name": "org_id", "type": "BIGINT", "nullable": False, "key": "FK", "notes": "→ organizations.id"},
                {"name": "email", "type": "VARCHAR(320)", "nullable": False, "key": "UQ", "notes": ""},
                {"name": "role", "type": "VARCHAR(32)", "nullable": False, "key": "", "notes": "admin|member"},
                {"name": "created_at", "type": "TIMESTAMPTZ", "nullable": False, "key": "", "notes": ""},
            ],
        },
        {
            "name": "links",
            "description": "Core short ↔ long URL mapping (hot path)",
            "columns": [
                {"name": "id", "type": "BIGINT", "nullable": False, "key": "PK", "notes": "Snowflake ID"},
                {"name": "short_code", "type": "VARCHAR(16)", "nullable": False, "key": "UQ", "notes": "Base62/Base58"},
                {"name": "original_url", "type": "TEXT", "nullable": False, "key": "", "notes": "destination"},
                {"name": "org_id", "type": "BIGINT", "nullable": True, "key": "FK", "notes": "nullable for anon"},
                {"name": "user_id", "type": "BIGINT", "nullable": True, "key": "FK", "notes": "→ users.id"},
                {"name": "custom_alias", "type": "BOOLEAN", "nullable": False, "key": "", "notes": "DEFAULT false"},
                {"name": "status", "type": "VARCHAR(16)", "nullable": False, "key": "", "notes": "active|disabled|expired"},
                {"name": "expires_at", "type": "TIMESTAMPTZ", "nullable": True, "key": "", "notes": "NULL = no expiry"},
                {"name": "created_at", "type": "TIMESTAMPTZ", "nullable": False, "key": "", "notes": ""},
                {"name": "updated_at", "type": "TIMESTAMPTZ", "nullable": False, "key": "", "notes": ""},
            ],
        },
        {
            "name": "aliases",
            "description": "Org-scoped custom vanity aliases",
            "columns": [
                {"name": "org_id", "type": "BIGINT", "nullable": False, "key": "PK", "notes": "composite PK"},
                {"name": "alias", "type": "VARCHAR(64)", "nullable": False, "key": "PK", "notes": "composite PK"},
                {"name": "link_id", "type": "BIGINT", "nullable": False, "key": "FK", "notes": "→ links.id"},
                {"name": "created_at", "type": "TIMESTAMPTZ", "nullable": False, "key": "", "notes": ""},
            ],
        },
        {
            "name": "api_keys",
            "description": "api_dev_key credentials + rate limits",
            "columns": [
                {"name": "id", "type": "BIGINT", "nullable": False, "key": "PK", "notes": ""},
                {"name": "org_id", "type": "BIGINT", "nullable": False, "key": "FK", "notes": "→ organizations.id"},
                {"name": "key_hash", "type": "BYTEA", "nullable": False, "key": "UQ", "notes": "sha256 hash"},
                {"name": "key_prefix", "type": "VARCHAR(12)", "nullable": False, "key": "", "notes": "display prefix"},
                {"name": "scopes", "type": "TEXT[]", "nullable": False, "key": "", "notes": "e.g. links:write"},
                {"name": "rate_limit_rpm", "type": "INTEGER", "nullable": False, "key": "", "notes": "writes / min"},
                {"name": "created_at", "type": "TIMESTAMPTZ", "nullable": False, "key": "", "notes": ""},
                {"name": "revoked_at", "type": "TIMESTAMPTZ", "nullable": True, "key": "", "notes": ""},
            ],
        },
        {
            "name": "outbox",
            "description": "Transactional outbox for async side-effects",
            "columns": [
                {"name": "id", "type": "BIGINT", "nullable": False, "key": "PK", "notes": ""},
                {"name": "aggregate_type", "type": "VARCHAR(64)", "nullable": False, "key": "", "notes": "link|click"},
                {"name": "aggregate_id", "type": "BIGINT", "nullable": False, "key": "", "notes": ""},
                {"name": "event_type", "type": "VARCHAR(64)", "nullable": False, "key": "", "notes": ""},
                {"name": "payload", "type": "JSONB", "nullable": False, "key": "", "notes": ""},
                {"name": "created_at", "type": "TIMESTAMPTZ", "nullable": False, "key": "", "notes": ""},
                {"name": "published_at", "type": "TIMESTAMPTZ", "nullable": True, "key": "", "notes": "NULL = pending"},
            ],
        },
        {
            "name": "click_aggregates",
            "description": "Daily click rollups (not on redirect hot path)",
            "columns": [
                {"name": "link_id", "type": "BIGINT", "nullable": False, "key": "PK", "notes": "composite PK"},
                {"name": "day", "type": "DATE", "nullable": False, "key": "PK", "notes": "UTC day"},
                {"name": "clicks", "type": "BIGINT", "nullable": False, "key": "", "notes": "DEFAULT 0"},
                {"name": "unique_approx", "type": "BIGINT", "nullable": True, "key": "", "notes": "HLL optional"},
            ],
        },
        {
            "name": "audit_logs",
            "description": "Admin / mutation audit trail",
            "columns": [
                {"name": "id", "type": "BIGINT", "nullable": False, "key": "PK", "notes": ""},
                {"name": "actor_user_id", "type": "BIGINT", "nullable": True, "key": "FK", "notes": ""},
                {"name": "action", "type": "VARCHAR(64)", "nullable": False, "key": "", "notes": ""},
                {"name": "resource_type", "type": "VARCHAR(64)", "nullable": False, "key": "", "notes": ""},
                {"name": "resource_id", "type": "VARCHAR(64)", "nullable": False, "key": "", "notes": ""},
                {"name": "payload", "type": "JSONB", "nullable": True, "key": "", "notes": ""},
                {"name": "ts", "type": "TIMESTAMPTZ", "nullable": False, "key": "", "notes": "partition key"},
            ],
        },
    ]
    # Compat shape for workspace codegen
    tables: dict[str, list[dict[str, Any]]] = {
        e["name"]: e["columns"] for e in entities
    }
    relationships = [
        {"from": "users.org_id", "to": "organizations.id", "type": "many-to-one"},
        {"from": "links.org_id", "to": "organizations.id", "type": "many-to-one"},
        {"from": "links.user_id", "to": "users.id", "type": "many-to-one"},
        {"from": "aliases.org_id", "to": "organizations.id", "type": "many-to-one"},
        {"from": "aliases.link_id", "to": "links.id", "type": "many-to-one"},
        {"from": "api_keys.org_id", "to": "organizations.id", "type": "many-to-one"},
        {"from": "click_aggregates.link_id", "to": "links.id", "type": "many-to-one"},
    ]
    indexes = [
        {"name": "uq_links_short_code", "table": "links", "columns": ["short_code"], "unique": True},
        {"name": "idx_links_org_created", "table": "links", "columns": ["org_id", "created_at DESC"], "unique": False},
        {"name": "idx_links_expires", "table": "links", "columns": ["expires_at"], "unique": False},
        {"name": "uq_users_email", "table": "users", "columns": ["email"], "unique": True},
        {"name": "uq_api_keys_hash", "table": "api_keys", "columns": ["key_hash"], "unique": True},
        {
            "name": "idx_outbox_unpublished",
            "table": "outbox",
            "columns": ["published_at"],
            "unique": False,
            "where": "published_at IS NULL",
        },
    ]
    constraints = [
        "PRIMARY KEY (links.id)",
        "UNIQUE (links.short_code)",
        "PRIMARY KEY (aliases.org_id, aliases.alias)",
        "CHECK (links.status IN ('active','disabled','expired'))",
        "FOREIGN KEY (links.org_id) REFERENCES organizations(id)",
        "FOREIGN KEY (aliases.link_id) REFERENCES links(id)",
    ]
    ddl = """CREATE TABLE links (
  id            BIGINT PRIMARY KEY,
  short_code    VARCHAR(16) NOT NULL,
  original_url  TEXT NOT NULL,
  org_id        BIGINT NULL REFERENCES organizations(id),
  user_id       BIGINT NULL REFERENCES users(id),
  custom_alias  BOOLEAN NOT NULL DEFAULT FALSE,
  status        VARCHAR(16) NOT NULL,
  expires_at    TIMESTAMPTZ NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_links_short_code UNIQUE (short_code),
  CONSTRAINT ck_links_status CHECK (status IN ('active','disabled','expired'))
);
CREATE INDEX idx_links_org_created ON links (org_id, created_at DESC);"""
    return {
        "schema_ddl": {
            "product": product,
            "entities": entities,
            "tables": tables,
            "relationships": relationships,
            "indexes": indexes,
            "constraints": constraints,
            "sharding": "hash(short_code) → Vitess / DocumentDB shard",
            "partitioning": {"audit_logs": "RANGE (ts) monthly"},
            "ddl_excerpt": ddl,
        },
        "migration_plan": {
            "steps": [
                "001_organizations_users",
                "002_links_aliases",
                "003_api_keys_outbox",
                "004_click_aggregates_audit",
            ],
            "rollback": "down migrations per version",
            "risk": "MEDIUM",
        },
        "sharding_strategy": {
            "key": "short_code",
            "method": "hash",
            "shards_mvp": 4,
            "notes": "Redirect lookup by short_code; shard-local cache",
        },
        "index_plan": {"indexes": indexes},
    }


def _normalize_db_entities(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize schema.tables / schema.entities into typed entity list."""
    entities = schema.get("entities")
    if isinstance(entities, list) and entities:
        out = []
        for e in entities:
            if not isinstance(e, dict) or not e.get("name"):
                continue
            cols = []
            for c in e.get("columns") or []:
                if isinstance(c, dict) and c.get("name"):
                    cols.append(
                        {
                            "name": str(c.get("name")),
                            "type": str(c.get("type") or c.get("data_type") or "TEXT"),
                            "nullable": bool(c.get("nullable", True)),
                            "key": str(c.get("key") or ""),
                            "notes": str(c.get("notes") or c.get("comment") or ""),
                        }
                    )
                elif isinstance(c, str):
                    # legacy "id PK" / "code UNIQUE"
                    parts = c.split()
                    name = parts[0] if parts else c
                    key = ""
                    joined = c.upper()
                    if "PK" in joined or "PRIMARY" in joined:
                        key = "PK"
                    elif "FK" in joined:
                        key = "FK"
                    elif "UNIQUE" in joined or "UQ" in joined:
                        key = "UQ"
                    cols.append(
                        {
                            "name": name,
                            "type": "TEXT",
                            "nullable": "NULL" in joined and "NOT NULL" not in joined,
                            "key": key,
                            "notes": c,
                        }
                    )
            out.append(
                {
                    "name": str(e["name"]),
                    "description": str(e.get("description") or ""),
                    "columns": cols,
                }
            )
        if out:
            return out

    tables = schema.get("tables") or {}
    out = []
    if isinstance(tables, dict):
        for tname, cols in tables.items():
            col_list: list[dict[str, Any]] = []
            if isinstance(cols, dict):
                for k, v in cols.items():
                    col_list.append(
                        {
                            "name": str(k),
                            "type": str(v) if not isinstance(v, dict) else str(v.get("type") or "TEXT"),
                            "nullable": bool(v.get("nullable", True)) if isinstance(v, dict) else True,
                            "key": str(v.get("key") or "") if isinstance(v, dict) else "",
                            "notes": str(v.get("notes") or "") if isinstance(v, dict) else "",
                        }
                    )
            elif isinstance(cols, list):
                for c in cols:
                    if isinstance(c, dict) and c.get("name"):
                        col_list.append(
                            {
                                "name": str(c["name"]),
                                "type": str(c.get("type") or "TEXT"),
                                "nullable": bool(c.get("nullable", True)),
                                "key": str(c.get("key") or ""),
                                "notes": str(c.get("notes") or ""),
                            }
                        )
                    else:
                        s = str(c)
                        parts = s.split()
                        name = parts[0] if parts else s
                        key = "PK" if "PK" in s.upper() else ("FK" if "FK" in s.upper() else ("UQ" if "UNIQUE" in s.upper() else ""))
                        col_list.append(
                            {
                                "name": name,
                                "type": "TEXT",
                                "nullable": True,
                                "key": key,
                                "notes": s,
                            }
                        )
            out.append({"name": str(tname), "description": "", "columns": col_list})
    return out


def build_database_html(
    *,
    product: str,
    schema: dict[str, Any],
    migration: dict[str, Any] | None = None,
    sharding: dict[str, Any] | None = None,
    index_plan: dict[str, Any] | None = None,
) -> str:
    """Self-contained Database Design HTML — typed entities, not a JSON dump."""
    migration = migration if isinstance(migration, dict) else {}
    sharding = sharding if isinstance(sharding, dict) else {}
    index_plan = index_plan if isinstance(index_plan, dict) else {}
    schema = schema if isinstance(schema, dict) else {}

    # If schema is thin/legacy for a URL shortener product, enrich for display
    entities = _normalize_db_entities(schema)
    if not entities or all(not e.get("columns") for e in entities):
        packed = default_url_shortener_schema(product)
        schema = {**packed["schema_ddl"], **{k: v for k, v in schema.items() if v}}
        entities = _normalize_db_entities(schema)
        if not migration:
            migration = packed["migration_plan"]
        if not sharding:
            sharding = packed["sharding_strategy"]
        if not index_plan:
            index_plan = packed["index_plan"]

    entity_cards = []
    for ent in entities:
        rows = []
        for c in ent.get("columns") or []:
            key = (c.get("key") or "").upper()
            key_html = f'<span class="key key-{_esc(key.lower())}">{_esc(key)}</span>' if key else "—"
            null_html = "YES" if c.get("nullable") else "<strong>NO</strong>"
            rows.append(
                "<tr>"
                f"<td><code>{_esc(c.get('name'))}</code></td>"
                f"<td><code class='dtype'>{_esc(c.get('type'))}</code></td>"
                f"<td>{null_html}</td>"
                f"<td>{key_html}</td>"
                f"<td class='muted'>{_esc(c.get('notes') or '—')}</td>"
                "</tr>"
            )
        desc = ent.get("description") or ""
        entity_cards.append(
            f"""
            <article class="entity-card">
              <div class="entity-head">
                <h3>{_esc(ent.get("name"))}</h3>
                {"<p class='muted'>" + _esc(desc) + "</p>" if desc else ""}
              </div>
              <table class="col-table">
                <thead>
                  <tr>
                    <th>Column</th><th>Data type</th><th>Nullable</th><th>Key</th><th>Notes</th>
                  </tr>
                </thead>
                <tbody>
                  {"".join(rows) or "<tr><td colspan='5' class='muted'>No columns</td></tr>"}
                </tbody>
              </table>
            </article>"""
        )
    if not entity_cards:
        entity_cards.append("<p class='muted'>No entities derived from requirements.</p>")

    # Relationships
    rels = schema.get("relationships") or []
    rel_rows = []
    for r in rels:
        if isinstance(r, dict):
            rel_rows.append(
                "<tr>"
                f"<td><code>{_esc(r.get('from'))}</code></td>"
                f"<td>{_esc(r.get('type') or 'FK')}</td>"
                f"<td><code>{_esc(r.get('to'))}</code></td>"
                "</tr>"
            )
        else:
            rel_rows.append(f"<tr><td colspan='3'>{_esc(r)}</td></tr>")

    indexes = schema.get("indexes") or index_plan.get("indexes") or []
    index_rows = []
    for ix in indexes:
        if isinstance(ix, dict):
            cols = ix.get("columns") or []
            col_s = ", ".join(str(c) for c in cols) if isinstance(cols, list) else str(cols)
            uniq = "UNIQUE" if ix.get("unique") else "INDEX"
            where = f" WHERE {ix.get('where')}" if ix.get("where") else ""
            index_rows.append(
                "<tr>"
                f"<td><code>{_esc(ix.get('name') or '—')}</code></td>"
                f"<td>{_esc(ix.get('table') or '—')}</td>"
                f"<td>{_esc(uniq)}</td>"
                f"<td><code>{_esc(col_s)}{_esc(where)}</code></td>"
                "</tr>"
            )
        else:
            index_rows.append(f"<tr><td colspan='4'>{_esc(ix)}</td></tr>")

    constraints = schema.get("constraints") or []
    steps = migration.get("steps") or []
    ddl = schema.get("ddl_excerpt") or ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{_esc(product)} — Database Design</title>
<style>
{_FORGE_THEME_ROOT}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; font-family:"DM Sans",system-ui,sans-serif; color:var(--text); background:var(--bg0); }}
{_FORGE_HEADER_CSS}
  main {{ max-width:1100px; margin:0 auto; padding:1.2rem; display:grid; gap:1rem; }}
  section {{ background:var(--bg1); border:1px solid var(--line); border-radius:10px; padding:1.1rem; }}
  h2 {{ margin:0 0 .7rem; font-size:.78rem; text-transform:uppercase; letter-spacing:.08em; color:var(--muted); }}
  h3 {{ margin:0; font-size:1.05rem; color:var(--accent); font-family:"IBM Plex Mono",monospace; }}
  .muted {{ color:var(--muted); }}
  .entity-card {{
    border:1px solid var(--line); border-radius:10px; background:var(--bg2);
    margin-bottom:.85rem; overflow:hidden;
  }}
  .entity-card:last-child {{ margin-bottom:0; }}
  .entity-head {{ padding:.75rem .9rem; border-bottom:1px solid var(--line); }}
  .entity-head p {{ margin:.3rem 0 0; font-size:.85rem; }}
  .col-table {{ width:100%; border-collapse:collapse; }}
  .col-table th, .col-table td {{
    text-align:left; padding:.45rem .7rem; border-bottom:1px solid var(--line);
    font-size:.86rem; vertical-align:top; color:var(--text);
  }}
  .col-table th {{
    color:var(--muted); font-size:.72rem; text-transform:uppercase; letter-spacing:.05em;
    background:#121a21; position:sticky; top:0;
  }}
  .col-table tr:last-child td {{ border-bottom:none; }}
  code {{ font-family:"IBM Plex Mono",monospace; font-size:.8rem; color:var(--text); }}
  code.dtype {{ color:var(--info); }}
  .key {{
    display:inline-block; min-width:1.8rem; text-align:center; padding:.1rem .35rem;
    border-radius:4px; font-size:.68rem; font-weight:700; font-family:"IBM Plex Mono",monospace;
    border:1px solid var(--line);
  }}
  .key-pk {{ color:#0e1419; background:var(--accent); border-color:var(--accent); }}
  .key-fk {{ color:var(--info); background:color-mix(in srgb, var(--info) 18%, var(--bg2)); }}
  .key-uq {{ color:#f0b429; background:color-mix(in srgb, #f0b429 16%, var(--bg2)); }}
  table.meta {{ width:100%; border-collapse:collapse; }}
  table.meta th, table.meta td {{
    text-align:left; padding:.45rem .3rem; border-bottom:1px solid var(--line); color:var(--text);
  }}
  table.meta th {{ color:var(--muted); width:28%; }}
  ul {{ margin:.2rem 0 0; padding-left:1.1rem; color:var(--text); }}
  pre {{
    margin:0; white-space:pre-wrap; background:#0b1220; color:var(--text);
    padding:.85rem; border-radius:10px; font-size:.82rem; border:1px solid var(--line);
    font-family:"IBM Plex Mono",monospace;
  }}
  .grid {{ display:grid; gap:.8rem; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); }}
  footer {{ text-align:center; color:var(--muted); font-size:.8rem; padding:0 0 1.4rem; }}
</style>
</head>
<body>
<header>
  <h1>{_esc(product)} · Database Design</h1>
  <p>Entity model with SQL data types, keys, and relationships — approve before API generation.</p>
</header>
<main>
  <section>
    <h2>1. Entities &amp; columns</h2>
    {"".join(entity_cards)}
  </section>

  <section>
    <h2>2. Relationships</h2>
    <table class="meta">
      <tr><th>From</th><th>Type</th><th>To</th></tr>
      {"".join(rel_rows) or "<tr><td colspan='3' class='muted'>No relationships listed</td></tr>"}
    </table>
  </section>

  <section>
    <h2>3. Indexes</h2>
    <table class="meta">
      <tr><th>Name</th><th>Table</th><th>Kind</th><th>Columns</th></tr>
      {"".join(index_rows) or "<tr><td colspan='4' class='muted'>No indexes</td></tr>"}
    </table>
  </section>

  <section>
    <h2>4. Constraints</h2>
    <ul>{_list_items(constraints, empty="No constraints listed")}</ul>
  </section>

  <section>
    <h2>5. Sharding &amp; migrations</h2>
    <table class="meta">
      <tr><th>Sharding</th><td>{_esc(schema.get("sharding") or sharding.get("method") or "—")}</td></tr>
      <tr><th>Shard key</th><td><code>{_esc(sharding.get("key") or "—")}</code></td></tr>
      <tr><th>Shards (MVP)</th><td>{_esc(sharding.get("shards_mvp") or "—")}</td></tr>
      <tr><th>Migration risk</th><td>{_esc(migration.get("risk") or "—")}</td></tr>
      <tr><th>Rollback</th><td>{_esc(migration.get("rollback") or "—")}</td></tr>
    </table>
    <h2 style="margin-top:1rem">Migration steps</h2>
    <ul>{_list_items(steps, empty="No migration steps")}</ul>
  </section>

  <section>
    <h2>6. DDL excerpt</h2>
    <pre>{_esc(ddl) if ddl else "No DDL excerpt"}</pre>
  </section>
</main>
<footer>Forge Database Agent · typed entity design</footer>
</body>
</html>
"""


def build_frontend_pages(
    *,
    product: str,
    openapi: dict[str, Any] | None,
    product_brief: dict[str, Any] | None,
    features: list[str] | None,
) -> dict[str, str]:
    """Return path → real HTML page content for operator UX."""
    openapi = openapi or {}
    brief = product_brief or {}
    features = features or brief.get("features") or []
    paths = list((openapi.get("paths") or {}).keys())
    mvp = brief.get("mvp") or []
    mvp_labels = []
    for item in mvp:
        if isinstance(item, dict):
            mvp_labels.append(str(item.get("feature") or item.get("id")))
        else:
            mvp_labels.append(str(item))

    api_rows = "".join(f"<tr><td>{_esc(p)}</td></tr>" for p in paths[:24]) or (
        "<tr><td class='muted'>OpenAPI paths unavailable</td></tr>"
    )
    feature_chips = "".join(f"<span class='chip'>{_esc(f)}</span>" for f in features) or (
        "".join(f"<span class='chip'>{_esc(f)}</span>" for f in mvp_labels[:8])
        or "<span class='muted'>No features detected</span>"
    )

    shared_css = """
:root{--bg:#f6f3ee;--ink:#1c1915;--muted:#6b645b;--line:#ddd4c7;--panel:#fffdf9;--accent:#b45309;--soft:#f3e7d3}
*{box-sizing:border-box}body{margin:0;font-family:"Fraunces","Georgia",serif;background:linear-gradient(160deg,#f8f1e4,#f6f3ee 40%,#ebe4d8);color:var(--ink)}
header{padding:1.6rem 1.4rem;background:#1c1915;color:#f8f1e4}header h1{margin:0;font-size:1.7rem;font-weight:600}
header p{margin:.35rem 0 0;color:#d8cbb8;font-family:"IBM Plex Sans",sans-serif;font-size:.92rem}
nav{display:flex;gap:.5rem;flex-wrap:wrap;padding:.8rem 1.2rem;border-bottom:1px solid var(--line);background:#fff9f0;font-family:"IBM Plex Sans",sans-serif}
nav a{color:var(--accent);text-decoration:none;padding:.35rem .7rem;border-radius:999px;border:1px solid transparent}
nav a.active,nav a:hover{border-color:#e7c9a0;background:#fff}
main{max-width:980px;margin:0 auto;padding:1.2rem;font-family:"IBM Plex Sans",sans-serif}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:1rem;margin-bottom:1rem}
.chip{display:inline-block;margin:.15rem .3rem .15rem 0;padding:.25rem .6rem;border-radius:999px;background:var(--soft);font-size:.8rem}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:.55rem .4rem;border-bottom:1px solid var(--line);font-size:.92rem}
label{display:block;font-size:.8rem;color:var(--muted);margin:.5rem 0 .2rem}
input,select,button{font:inherit}input,select{width:100%;padding:.55rem .65rem;border:1px solid var(--line);border-radius:10px;background:#fff}
button{margin-top:.8rem;background:var(--accent);color:#fff;border:0;border-radius:10px;padding:.6rem 1rem;cursor:pointer}
.muted{color:var(--muted)}.grid{display:grid;gap:.8rem;grid-template-columns:repeat(auto-fit,minmax(220px,1fr))}
.stat{padding:.9rem;border-radius:12px;background:#fff7ea;border:1px solid #edd8b4}.stat b{display:block;font-size:1.3rem;font-family:Fraunces,serif}
"""

    def shell(title: str, active: str, body: str) -> str:
        links = [
            ("dashboard.html", "Links"),
            ("analytics.html", "Analytics"),
            ("keys.html", "API Keys"),
        ]
        if "preview" in features or "qr_code" in features:
            links.append(("tools.html", "Tools"))
        nav = "".join(
            f"<a href='{href}' class='{'active' if href==active else ''}'>{label}</a>"
            for href, label in links
        )
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{_esc(product)} — {_esc(title)}</title>
<style>{shared_css}</style>
</head>
<body>
<header>
  <h1>{_esc(product)}</h1>
  <p>Operator UI design generated from product brief + OpenAPI (Frontend Engineer Agent).</p>
</header>
<nav>{nav}</nav>
<main>{body}</main>
</body>
</html>
"""

    dashboard = shell(
        "Links",
        "dashboard.html",
        f"""
<section class="panel">
  <h2 style="margin:0 0 .6rem;font-family:Fraunces,serif">Create short link</h2>
  <form onsubmit="event.preventDefault();document.getElementById('out').textContent='Design preview — wire to POST /v1/links';">
    <label>Destination URL</label>
    <input name="url" type="url" required placeholder="https://…"/>
    <label>Custom alias (optional)</label>
    <input name="alias" placeholder="launch-2026"/>
    <button type="submit">Shorten</button>
  </form>
  <p id="out" class="muted" style="margin-top:.8rem"></p>
</section>
<section class="panel">
  <h2 style="margin:0 0 .6rem;font-family:Fraunces,serif">In-scope features</h2>
  <div>{feature_chips}</div>
</section>
<section class="panel">
  <h2 style="margin:0 0 .6rem;font-family:Fraunces,serif">Recent links</h2>
  <table>
    <thead><tr><th>Code</th><th>Target</th><th>Status</th></tr></thead>
    <tbody>
      <tr><td class="muted" colspan="3">Empty until API is connected — layout only</td></tr>
    </tbody>
  </table>
</section>
""",
    )

    analytics = shell(
        "Analytics",
        "analytics.html",
        f"""
<section class="grid">
  <div class="stat"><span class="muted">Clicks (24h)</span><b>—</b></div>
  <div class="stat"><span class="muted">Unique visitors</span><b>—</b></div>
  <div class="stat"><span class="muted">Top country</span><b>—</b></div>
</section>
<section class="panel">
  <h2 style="margin:0 0 .6rem;font-family:Fraunces,serif">Bound API operations</h2>
  <table><thead><tr><th>Path</th></tr></thead><tbody>{api_rows}</tbody></table>
</section>
""",
    )

    keys = shell(
        "API Keys",
        "keys.html",
        """
<section class="panel">
  <h2 style="margin:0 0 .6rem;font-family:Fraunces,serif">API key management</h2>
  <label>Key name</label>
  <input placeholder="ci-bot"/>
  <label>Scopes</label>
  <select><option>links:write</option><option>analytics:read</option><option>admin</option></select>
  <button type="button">Create key</button>
  <p class="muted" style="margin-top:.8rem">UI shell aligned to OpenAPI key operations.</p>
</section>
""",
    )

    pages = {
        "apps/web/dashboard.html": dashboard,
        "apps/web/analytics.html": analytics,
        "apps/web/keys.html": keys,
        "apps/web/styles.css": shared_css.strip() + "\n",
    }

    if "preview" in features or "qr_code" in features:
        tools_bits = []
        if "preview" in features:
            tools_bits.append(
                "<section class='panel'><h2 style='margin:0 0 .6rem;font-family:Fraunces,serif'>Safe link preview</h2>"
                "<label>Short code</label><input placeholder='abc123'/>"
                "<button type='button'>Preview</button>"
                "<p class='muted'>Blocks private IP ranges (SSRF-safe).</p></section>"
            )
        if "qr_code" in features:
            tools_bits.append(
                "<section class='panel'><h2 style='margin:0 0 .6rem;font-family:Fraunces,serif'>QR code</h2>"
                "<label>Short code</label><input placeholder='abc123'/>"
                "<button type='button'>Download PNG</button></section>"
            )
        pages["apps/web/tools.html"] = shell("Tools", "tools.html", "".join(tools_bits))

    # Combined design pack for studio preview
    pages["apps/web/INDEX_DESIGN.html"] = shell(
        "Design pack",
        "dashboard.html",
        f"""
<section class="panel">
  <h2 style="margin:0 0 .6rem;font-family:Fraunces,serif">{_esc(product)} UI design pack</h2>
  <p>Pages: {", ".join(_esc(p.replace("apps/web/", "")) for p in pages if p.endswith(".html") and "INDEX" not in p)}</p>
  <p class="muted">Open dashboard.html / analytics.html / keys.html as the operator experience.</p>
  <h3 style="margin:1rem 0 .4rem;font-family:Fraunces,serif">MVP from product brief</h3>
  <ul>{_list_items(mvp_labels or features)}</ul>
</section>
""",
    )
    return pages


def html_artifact_document(title: str, pages: dict[str, str]) -> str:
    """Single HTML document embedding a page switcher for studio Results."""
    names = [p for p in pages if p.endswith(".html")]
    if not names:
        return "<!DOCTYPE html><html><body><p>No HTML pages</p></body></html>"
    # Single page: return raw HTML (avoids nested iframe + script embedding issues)
    if len(names) == 1:
        return pages[names[0]]
    default = "apps/web/dashboard.html" if "apps/web/dashboard.html" in pages else names[0]
    options = "".join(
        f"<option value='{_esc(n)}' {'selected' if n==default else ''}>{_esc(n)}</option>"
        for n in names
    )
    # Embed as JSON inside <script>. Escape "<" so nested </script> in page HTML
    # cannot terminate this host script (would leave the preview iframe blank).
    payload = json.dumps(
        {k: v for k, v in pages.items() if k.endswith(".html")}
    ).replace("<", "\\u003c")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{_esc(title)} — Frontend Design</title>
<style>
  body{{margin:0;font-family:system-ui,sans-serif;background:#07111a;color:#eee}}
  .bar{{display:flex;gap:.6rem;align-items:center;padding:.55rem .8rem;background:#0c1a24;border-bottom:1px solid rgba(140,190,210,.18)}}
  select{{flex:1;padding:.4rem;border-radius:8px;border:1px solid rgba(140,190,210,.25);background:#122433;color:#eee}}
  iframe{{width:100%;height:calc(100vh - 48px);border:0;background:#07111a}}
</style>
</head>
<body>
<div class="bar">
  <strong>{_esc(title)}</strong>
  <select id="page">{options}</select>
</div>
<iframe id="frame" title="design preview" sandbox="allow-scripts allow-same-origin"></iframe>
<script>
const PAGES = {payload};
const frame = document.getElementById('frame');
const sel = document.getElementById('page');
function show() {{
  const html = PAGES[sel.value] || '<p style="color:#8aa3b0;padding:2rem;font-family:system-ui">Preview missing</p>';
  frame.srcdoc = html;
}}
sel.addEventListener('change', show);
show();
</script>
</body>
</html>
"""


def _group_files(paths: list[str]) -> list[tuple[str, list[str]]]:
    groups: dict[str, list[str]] = {}
    for p in paths:
        parts = str(p).split("/")
        file = parts.pop() if parts else str(p)
        directory = "/".join(parts) if parts else "."
        groups.setdefault(directory, []).append(file)
    return sorted(groups.items(), key=lambda kv: kv[0])


def build_workspace_html(manifest: dict[str, Any]) -> str:
    """Beautiful standalone workspace page (backend/frontend/infra trees + run commands)."""
    be = list(manifest.get("backend_files") or [])
    fe = list(manifest.get("frontend_files") or [])
    infra = list(manifest.get("infra_files") or [])
    run = manifest.get("run") or {}
    ready = manifest.get("status") == "READY" or bool(manifest.get("coding_complete"))
    product = manifest.get("product") or "Product"
    total = int(manifest.get("file_count") or (len(be) + len(fe) + len(infra)))
    path = str(manifest.get("path") or "")
    short = path
    if "/workspaces/" in path:
        short = "workspaces/" + path.split("/workspaces/", 1)[-1]
    msg = manifest.get("message") or (
        "Agents finished scaffolding backend, frontend, and infra." if ready else "Coding in progress…"
    )

    def tree(paths: list[str], kind: str) -> str:
        groups = _group_files(paths)
        if not groups:
            return f"<p class='muted'>No {kind} files yet</p>"
        bits = ["<div class='tree'>"]
        shown = 0
        for directory, files in groups:
            if shown >= 48:
                break
            bits.append(f"<div class='folder'>{_esc(directory)}/</div>")
            for f in files:
                if shown >= 48:
                    break
                cls = "file fe" if kind == "frontend" else ("file infra" if kind == "infra" else "file")
                bits.append(
                    f"<div class='{cls}'><span class='dot'></span><span>{_esc(f)}</span></div>"
                )
                shown += 1
        if len(paths) > shown:
            bits.append(f"<p class='muted'>… +{len(paths) - shown} more</p>")
        bits.append("</div>")
        return "\n".join(bits)

    cmds = []
    for tag, key, cls in (
        ("Backend", "backend", ""),
        ("Frontend", "frontend", "fe"),
        ("API docs", "docs", "docs"),
        ("TF init", "terraform_init", "tf"),
        ("TF plan", "terraform_plan", "tf"),
        ("TF apply", "terraform_apply", "tf"),
    ):
        cmd = run.get(key)
        if cmd:
            cmds.append(
                f"<div class='cmd'><span class='tag {cls}'>{_esc(tag)}</span>"
                f"<code>{_esc(cmd)}</code></div>"
            )

    badge = "Ready" if ready else str(manifest.get("status") or "Building")
    cmd_html = "".join(cmds) or "<p class='muted'>No run commands yet</p>"
    be_tree = tree(be, "backend")
    fe_tree = tree(fe, "frontend")
    infra_tree = tree(infra, "infra")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{_esc(product)} — Workspace</title>
<style>
  :root {{ --bg:#0e1419; --panel:#152028; --line:#2a3f4d; --text:#e6eef2; --muted:#8aa0ad;
    --accent:#3ecf8e; --info:#5eb1e8; --warn:#e8b84a; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; font-family:"DM Sans",system-ui,sans-serif; color:var(--text);
    background: radial-gradient(900px 400px at 0% 0%, rgba(62,207,142,.16), transparent 55%),
      radial-gradient(700px 360px at 100% 0%, rgba(94,177,232,.12), transparent 50%), var(--bg); }}
  .hero {{ padding:1.5rem 1.6rem 1.2rem; border-bottom:1px solid var(--line); }}
  .kicker {{ margin:0; font-size:.72rem; letter-spacing:.14em; text-transform:uppercase; color:var(--muted); font-weight:600; }}
  h1 {{ margin:.35rem 0 0; font-family:"Source Serif 4",Georgia,serif; font-size:1.8rem; }}
  .badge {{ display:inline-flex; margin-left:.6rem; padding:.2rem .55rem; border-radius:999px; font-size:.72rem; font-weight:700;
    border:1px solid rgba(62,207,142,.45); background:rgba(62,207,142,.12); color:var(--accent); vertical-align:middle; }}
  .msg {{ color:var(--muted); max-width:52ch; }}
  .path {{ display:inline-block; margin-top:.7rem; font-family:ui-monospace,monospace; font-size:.74rem;
    padding:.4rem .65rem; border-radius:8px; background:rgba(0,0,0,.28); border:1px solid var(--line); }}
  .stats {{ display:grid; grid-template-columns:repeat(4,1fr); gap:.65rem; padding:1rem 1.5rem; border-bottom:1px solid var(--line); }}
  .stat {{ padding:.75rem .9rem; border-radius:12px; background:rgba(28,43,53,.75); border:1px solid var(--line); }}
  .stat .n {{ font-family:ui-monospace,monospace; font-size:1.4rem; color:var(--accent); }}
  .stat .l {{ font-size:.7rem; color:var(--muted); text-transform:uppercase; letter-spacing:.04em; }}
  .cols {{ display:grid; grid-template-columns:1fr 1fr 1fr; }}
  .col {{ padding:1.1rem 1.25rem; min-height:240px; }}
  .col + .col {{ border-left:1px solid var(--line); }}
  .col h2 {{ margin:0 0 .7rem; font-size:.78rem; letter-spacing:.1em; text-transform:uppercase; color:var(--muted); }}
  .tree {{ max-height:340px; overflow:auto; }}
  .folder {{ font-family:ui-monospace,monospace; font-size:.7rem; color:var(--warn); margin:.4rem 0 .15rem; }}
  .file {{ display:flex; gap:.45rem; align-items:center; font-family:ui-monospace,monospace; font-size:.74rem;
    padding:.28rem .45rem; border-radius:7px; }}
  .file:hover {{ background:rgba(62,207,142,.08); }}
  .dot {{ width:.35rem; height:.35rem; border-radius:50%; background:var(--accent); }}
  .file.fe .dot {{ background:var(--info); }}
  .file.infra .dot {{ background:var(--warn); }}
  .run {{ padding:1.15rem 1.5rem 1.4rem; border-top:1px solid var(--line); background:rgba(0,0,0,.18); }}
  .run h2 {{ margin:0 0 .7rem; font-size:.78rem; letter-spacing:.1em; text-transform:uppercase; color:var(--muted); }}
  .cmd {{ display:grid; grid-template-columns:88px 1fr; gap:.55rem; align-items:center; margin-bottom:.5rem;
    padding:.55rem .7rem; border-radius:10px; background:#0a1014; border:1px solid var(--line); }}
  .tag {{ font-size:.68rem; font-weight:700; letter-spacing:.08em; text-transform:uppercase; color:var(--accent); }}
  .tag.fe {{ color:var(--info); }} .tag.docs {{ color:var(--warn); }} .tag.tf {{ color:#c4a5ff; }}
  code {{ font-family:ui-monospace,monospace; font-size:.74rem; color:#d7e6ee; word-break:break-all; }}
  .muted {{ color:var(--muted); }}
  @media (max-width:1100px) {{ .cols,.stats {{ grid-template-columns:1fr 1fr; }} }}
  @media (max-width:900px) {{ .cols,.stats {{ grid-template-columns:1fr; }} .col + .col {{ border-left:0; border-top:1px solid var(--line); }} }}
</style>
</head>
<body>
  <div class="hero">
    <p class="kicker">Generated coding workspace</p>
    <h1>{_esc(product)} <span class="badge">{_esc(badge)}</span></h1>
    <p class="msg">{_esc(msg)}</p>
    <div class="path">{_esc(short)}</div>
  </div>
  <div class="stats">
    <div class="stat"><div class="n">{total}</div><div class="l">Total files</div></div>
    <div class="stat"><div class="n">{len(be)}</div><div class="l">Backend</div></div>
    <div class="stat"><div class="n">{len(fe)}</div><div class="l">Frontend</div></div>
    <div class="stat"><div class="n">{len(infra)}</div><div class="l">Infra / Terraform</div></div>
  </div>
  <div class="cols">
    <div class="col"><h2>Backend tree · {len(be)}</h2>{be_tree}</div>
    <div class="col"><h2>Frontend tree · {len(fe)}</h2>{fe_tree}</div>
    <div class="col"><h2>Infra / Terraform · {len(infra)}</h2>{infra_tree}</div>
  </div>
  <div class="run"><h2>Run locally</h2>{cmd_html}</div>
</body>
</html>
"""


def build_theme_shell_html(
    *,
    workflow_id: str,
    section: str,
    product: str,
    title: str,
    content_url: str,
    sections: list[tuple[str, str]],
) -> str:
    """
    Forge theme chrome only (no workspace tree): top nav + full-bleed design iframe.
    Used for DAG / HLD / LLD / DB Design / Workspace with Full screen on the client.
    """
    nav = "".join(
        f'<a class="nav {"active" if sid == section else ""}" '
        f'href="/api/workflows/{_esc(workflow_id)}/theme/{_esc(sid)}.html">{_esc(label)}</a>'
        for sid, label in sections
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{_esc(product)} — {_esc(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;600&family=IBM+Plex+Mono:wght@500&family=Source+Serif+4:opsz,wght@8..60,700&display=swap" rel="stylesheet"/>
<style>
  :root {{ --bg:#0e1419; --panel:#152028; --line:#2a3f4d; --text:#e6eef2; --muted:#8aa0ad;
    --accent:#3ecf8e; --info:#61affe; --get:#61affe; --post:#49cc90; }}
  * {{ box-sizing:border-box; }}
  html, body {{ height:100%; margin:0; }}
  body {{
    font-family:"DM Sans",system-ui,sans-serif; color:var(--text);
    background: var(--bg);
    display:grid; grid-template-rows:auto 1fr;
  }}
  .top {{
    display:flex; flex-wrap:wrap; gap:.75rem 1rem; align-items:center; justify-content:space-between;
    padding:.85rem 1.1rem; border-bottom:1px solid var(--line);
    background: color-mix(in srgb, var(--get) 8%, var(--panel));
  }}
  .brand {{ font-size:1.1rem; font-weight:600; letter-spacing:-.01em; }}
  .navs {{ display:flex; flex-wrap:wrap; gap:.45rem; }}
  .nav {{
    text-decoration:none; color:var(--text); font-size:.78rem; font-weight:600;
    padding:.42rem .75rem; border-radius:10px; border:1px solid var(--line);
    background: var(--panel); font-family:"IBM Plex Mono",monospace;
  }}
  .nav:hover {{ border-color: color-mix(in srgb, var(--get) 55%, var(--line));
    background: color-mix(in srgb, var(--get) 12%, transparent); }}
  .nav.active {{
    color: #0b1220; border-color: var(--post); background: var(--post);
  }}
  .main {{
    min-width:0; min-height:0; display:flex; padding:.75rem;
    background: var(--bg);
  }}
  .frame-wrap {{
    flex:1; min-height:0; display:flex; border:1px solid var(--line); border-radius:10px;
    overflow:hidden; background: var(--panel);
  }}
  .frame-wrap iframe {{ flex:1; width:100%; border:0; background:#0b1220; min-height:0; }}
</style>
</head>
<body>
  <div class="top">
    <div class="brand">{_esc(title)}</div>
    <div class="navs">{nav}</div>
  </div>
  <div class="main">
    <div class="frame-wrap">
      <iframe src="{_esc(content_url)}" title="{_esc(title)}"></iframe>
    </div>
  </div>
</body>
</html>
"""
