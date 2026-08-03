"""Modern animated React (Vite) UI generator — LLD / ReqSpec / Figma driven."""

from __future__ import annotations

import json
import re
from typing import Any

from .ui_spec import derive_ui_spec


def _esc(s: Any) -> str:
    return (
        str(s if s is not None else "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _component_name(screen_id: str) -> str:
    parts = re.split(r"[-_]+", screen_id or "page")
    return "".join(p[:1].upper() + p[1:] for p in parts if p) + "Page"


def _page_filename(screen_id: str) -> str:
    return _component_name(screen_id).replace("Page", "") or "Home"


def build_react_frontend(
    *,
    product: str,
    openapi: dict[str, Any] | None = None,
    product_brief: dict[str, Any] | None = None,
    reqspec: dict[str, Any] | None = None,
    features: list[str] | None = None,
    lld: Any = None,
    figma: dict[str, Any] | None = None,
    ui_spec: dict[str, Any] | None = None,
) -> dict[str, str]:
    """
    Return path → file content for a Vite + React + TS + Framer Motion app.

    Screens/nav/copy come from ui_spec (ReqSpec/LLD/product), not TinyURL hardcoding.
    """
    spec = ui_spec or derive_ui_spec(
        product=product,
        openapi=openapi,
        product_brief=product_brief,
        reqspec=reqspec,
        features=features,
        lld=lld,
        figma=figma,
    )
    name = str(spec.get("product") or product or "App")
    screens = list(spec.get("screens") or [])
    if not screens:
        screens = [
            {
                "id": "home",
                "route": "/",
                "nav": "Home",
                "title": name,
                "subtitle": "Operator console from LLD.",
                "primary_action": "Create",
                "fields": [{"id": "name", "label": "Name", "placeholder": "name"}],
                "empty": "No data yet.",
                "mvp_preview": [],
            }
        ]
    feats = list(features or spec.get("features") or [])
    api_paths = list(spec.get("openapi_paths") or [])
    feats_json = json.dumps(feats)
    api_paths_json = json.dumps(api_paths[:40])
    screens_json = json.dumps(screens)
    figma_note = ""
    if spec.get("figma_provided"):
        figma_note = (
            f"Figma reference applied: {spec.get('figma_url') or 'uploaded export'}. "
            f"{spec.get('figma_notes') or ''}".strip()
        )

    files: dict[str, str] = {}
    pkg_name = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "app"

    files["package.json"] = json.dumps(
        {
            "name": f"{pkg_name}-web",
            "private": True,
            "version": "0.1.0",
            "type": "module",
            "scripts": {
                "dev": "vite --port 5173",
                "build": "tsc -b && vite build",
                "preview": "vite preview --port 5173",
            },
            "dependencies": {
                "framer-motion": "^11.15.0",
                "react": "^18.3.1",
                "react-dom": "^18.3.1",
                "react-router-dom": "^6.28.0",
            },
            "devDependencies": {
                "@types/react": "^18.3.12",
                "@types/react-dom": "^18.3.1",
                "@vitejs/plugin-react": "^4.3.4",
                "typescript": "^5.6.3",
                "vite": "^5.4.11",
            },
        },
        indent=2,
    )

    files["vite.config.ts"] = """import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/v1": "http://127.0.0.1:8080",
      "/healthz": "http://127.0.0.1:8080",
      "/readyz": "http://127.0.0.1:8080",
      "/metrics": "http://127.0.0.1:8080",
    },
  },
});
"""

    files["tsconfig.json"] = json.dumps(
        {
            "compilerOptions": {
                "target": "ES2022",
                "useDefineForClassFields": True,
                "lib": ["ES2022", "DOM", "DOM.Iterable"],
                "module": "ESNext",
                "skipLibCheck": True,
                "moduleResolution": "bundler",
                "allowImportingTsExtensions": True,
                "isolatedModules": True,
                "moduleDetection": "force",
                "noEmit": True,
                "jsx": "react-jsx",
                "strict": True,
                "noUnusedLocals": False,
                "noUnusedParameters": False,
                "noFallthroughCasesInSwitch": True,
                "baseUrl": ".",
                "paths": {"@/*": ["src/*"]},
            },
            "include": ["src"],
        },
        indent=2,
    )

    files["tsconfig.node.json"] = json.dumps(
        {
            "compilerOptions": {
                "target": "ES2022",
                "lib": ["ES2023"],
                "module": "ESNext",
                "skipLibCheck": True,
                "moduleResolution": "bundler",
                "strict": True,
                "noEmit": True,
            },
            "include": ["vite.config.ts"],
        },
        indent=2,
    )

    files["index.html"] = f"""<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>{_esc(name)}</title>
    <link rel="preconnect" href="https://fonts.googleapis.com" />
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
    <link href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,400;0,9..40,500;0,9..40,600;0,9..40,700;1,9..40,400&family=Syne:wght@600;700;800&display=swap" rel="stylesheet" />
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
"""

    files["src/vite-env.d.ts"] = '/// <reference types="vite/client" />\n'

    files["src/main.tsx"] = """import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import "./styles/global.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>
);
"""

    # Routes + pages from ui_spec
    imports: list[str] = []
    routes: list[str] = []
    nav_entries: list[str] = []
    for s in screens:
        sid = str(s.get("id") or "home")
        route = str(s.get("route") or "/")
        comp = _component_name(sid)
        fname = _page_filename(sid)
        imports.append(f'import {comp} from "./pages/{fname}";')
        routes.append(f'          <Route path="{route}" element={{<{comp} />}} />')
        nav_entries.append(
            f'  {{ to: {json.dumps(route)}, label: {json.dumps(str(s.get("nav") or sid))} }},'
        )
        files[f"src/pages/{fname}.tsx"] = _screen_tsx(s, is_home=(route == "/"))

    files["src/App.tsx"] = f"""import {{ AnimatePresence }} from "framer-motion";
import {{ Navigate, Route, Routes, useLocation }} from "react-router-dom";
import Layout from "./components/Layout";
{chr(10).join(imports)}

export default function App() {{
  const location = useLocation();
  return (
    <Layout>
      <AnimatePresence mode="wait">
        <Routes location={{location}} key={{location.pathname}}>
{chr(10).join(routes)}
          <Route path="*" element={{<Navigate to="/" replace />}} />
        </Routes>
      </AnimatePresence>
    </Layout>
  );
}}
"""

    files["src/styles/global.css"] = _global_css()

    files["src/lib/api.ts"] = f"""/** Thin API client — proxy to FastAPI during `npm run dev`. */
export const API_BASE = import.meta.env.VITE_API_BASE || "";

export const PRODUCT = {json.dumps(name)};
export const FEATURES: string[] = {feats_json};
export const OPENAPI_PATHS: string[] = {api_paths_json};
export const UI_SCREENS = {screens_json} as const;
export const FIGMA_NOTE = {json.dumps(figma_note)};
export const LLD_HINT = {json.dumps(str(spec.get("lld_hint") or ""))};

async function request<T>(path: string, init?: RequestInit): Promise<T> {{
  const res = await fetch(`${{API_BASE}}${{path}}`, {{
    headers: {{ "Content-Type": "application/json", ...(init?.headers || {{}}) }},
    ...init,
  }});
  if (!res.ok) {{
    const text = await res.text();
    throw new Error(text || res.statusText);
  }}
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}}

export function apiGet<T = unknown>(path: string) {{
  return request<T>(path);
}}

export function apiPost<T = unknown>(path: string, body: unknown) {{
  return request<T>(path, {{ method: "POST", body: JSON.stringify(body) }});
}}
"""

    files["src/components/Page.tsx"] = """import { motion } from "framer-motion";
import type { ReactNode } from "react";

const variants = {
  initial: { opacity: 0, y: 16, filter: "blur(4px)" },
  animate: { opacity: 1, y: 0, filter: "blur(0px)" },
  exit: { opacity: 0, y: -10, filter: "blur(4px)" },
};

export default function Page({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle?: string;
  children: ReactNode;
}) {
  return (
    <motion.div
      variants={variants}
      initial="initial"
      animate="animate"
      exit="exit"
      transition={{ duration: 0.35, ease: [0.22, 1, 0.36, 1] }}
    >
      <h1 className="page-title">{title}</h1>
      {subtitle ? <p className="page-sub">{subtitle}</p> : null}
      {children}
    </motion.div>
  );
}
"""

    files["src/components/Layout.tsx"] = f"""import {{ NavLink }} from "react-router-dom";
import type {{ ReactNode }} from "react";
import {{ PRODUCT }} from "../lib/api";

const links = [
{chr(10).join(nav_entries)}
];

export default function Layout({{ children }}: {{ children: ReactNode }}) {{
  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="brand">
          {{PRODUCT}} <span>Studio</span>
        </div>
        <nav className="nav">
          {{links.map((l) => (
            <NavLink
              key={{l.to}}
              to={{l.to}}
              end={{l.to === "/"}}
              className={{({{ isActive }}) => (isActive ? "active" : undefined)}}
            >
              {{l.label}}
            </NavLink>
          ))}}
        </nav>
      </header>
      <main className="app-main">{{children}}</main>
    </div>
  );
}}
"""

    files["ui_spec.json"] = json.dumps(spec, indent=2)
    files["README.md"] = f"""# {name} — React UI

Vite + React + TypeScript + Framer Motion operator console, generated from LLD/ReqSpec
{'(+ Figma reference)' if spec.get('figma_provided') else '(agent-designed)'}.

## Run

```bash
cd frontend
npm install
npm run dev
```

Dev server: http://127.0.0.1:5173  
API proxy → FastAPI on http://127.0.0.1:8080
"""

    files["_studio_preview.html"] = build_react_preview_html(ui_spec=spec, features=feats)
    return files


def _screen_tsx(screen: dict[str, Any], *, is_home: bool) -> str:
    title = json.dumps(str(screen.get("title") or "Page"))
    subtitle = json.dumps(str(screen.get("subtitle") or ""))
    action = json.dumps(str(screen.get("primary_action") or "Submit"))
    empty = json.dumps(str(screen.get("empty") or "No data yet."))
    fields = list(screen.get("fields") or [])
    mvp = list(screen.get("mvp_preview") or [])
    field_state = "\n".join(
        f'  const [{f.get("id")}, set{_title(str(f.get("id")))}] = useState("");'
        for f in fields
        if f.get("id")
    )
    field_inputs = "\n".join(
        f"""          <label htmlFor="{f.get("id")}">{_esc(f.get("label") or f.get("id"))}</label>
          <input
            id="{f.get("id")}"
            placeholder={json.dumps(str(f.get("placeholder") or ""))}
            value={{{f.get("id")}}}
            onChange={{(e) => set{_title(str(f.get("id")))}(e.target.value)}}
          />"""
        for f in fields
        if f.get("id")
    )
    reset_body = "\n".join(
        f'                set{_title(str(f.get("id")))}("");' for f in fields if f.get("id")
    )
    mvp_block = ""
    if is_home and mvp:
        chips = "".join(
            f'<span className="chip" key={json.dumps(m)}>{_esc(m)}</span>' for m in mvp[:6]
        )
        # Use JSX properly in the generated file
        mvp_chips_js = ", ".join(json.dumps(m) for m in mvp[:6])
        mvp_block = f"""
      <motion.div
        className="panel"
        style={{{{ marginTop: "1rem" }}}}
        initial={{{{ opacity: 0, y: 14 }}}}
        animate={{{{ opacity: 1, y: 0 }}}}
        transition={{{{ delay: 0.22 }}}}
      >
        <h2>MVP from requirements</h2>
        <div style={{{{ marginTop: "0.5rem" }}}}>
          {{[{mvp_chips_js}].map((m) => (
            <span className="chip" key={{m}}>{{m}}</span>
          ))}}
        </div>
      </motion.div>"""

    form_section = ""
    if fields:
        form_section = f"""
      <motion.section
        className="panel"
        initial={{{{ opacity: 0, y: 18 }}}}
        animate={{{{ opacity: 1, y: 0 }}}}
        transition={{{{ delay: 0.18 }}}}
      >
        <h2>{{{action}}}</h2>
        <form onSubmit={{onSubmit}}>
{field_inputs}
          <div className="form-actions">
            <button className="btn" type="submit" disabled={{busy}}>
              {{busy ? "Working…" : {action}}}
            </button>
            <button
              className="btn secondary"
              type="button"
              onClick={{() => {{
{reset_body}
              }}}}
            >
              Reset
            </button>
          </div>
        </form>
        <p className="muted" style={{{{ marginTop: "0.85rem" }}}}>
          {{msg}}
        </p>
      </motion.section>"""
    else:
        form_section = f"""
      <motion.section
        className="panel"
        initial={{{{ opacity: 0, y: 18 }}}}
        animate={{{{ opacity: 1, y: 0 }}}}
      >
        <button className="btn" type="button" onClick={{() => setMsg("Wire this action to OpenAPI/LLD.")}}>
          {{{action}}}
        </button>
        <p className="muted" style={{{{ marginTop: "0.85rem" }}}}>{{msg}}</p>
      </motion.section>"""

    return f"""import {{ FormEvent, useState }} from "react";
import {{ motion }} from "framer-motion";
import Page from "../components/Page";
import {{ FEATURES, FIGMA_NOTE, LLD_HINT, OPENAPI_PATHS }} from "../lib/api";

export default function {_component_name(str(screen.get("id") or "home"))}() {{
{field_state if field_state else "  // no form fields"}
  const [msg, setMsg] = useState(
    FIGMA_NOTE || LLD_HINT || "Connect the FastAPI backend to load live data."
  );
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: FormEvent) {{
    e.preventDefault();
    setBusy(true);
    setMsg("Submitting… wire this form to the OpenAPI path from LLD.");
    setBusy(false);
  }}

  return (
    <Page title={{{title}}} subtitle={{{subtitle}}}>
      <div className="grid" style={{{{ marginBottom: "1rem" }}}}>
        <motion.div
          className="panel"
          initial={{{{ opacity: 0, scale: 0.96 }}}}
          animate={{{{ opacity: 1, scale: 1 }}}}
          transition={{{{ delay: 0.05 }}}}
        >
          <div className="hero-orb" aria-hidden />
          <p className="muted" style={{{{ margin: "0.9rem 0 0" }}}}>
            UI derived from LLD / ReqSpec{{FIGMA_NOTE ? " + Figma reference" : ""}}.
          </p>
        </motion.div>
        <motion.div
          className="panel stat"
          initial={{{{ opacity: 0, y: 12 }}}}
          animate={{{{ opacity: 1, y: 0 }}}}
          transition={{{{ delay: 0.12 }}}}
        >
          <span>In-scope signals</span>
          <div style={{{{ marginTop: "0.65rem" }}}}>
            {{FEATURES.length
              ? FEATURES.map((f) => (
                  <span className="chip" key={{f}}>
                    {{f}}
                  </span>
                ))
              : OPENAPI_PATHS.slice(0, 4).map((p) => (
                  <span className="chip" key={{p}}>
                    {{p}}
                  </span>
                ))}}
          </div>
        </motion.div>
      </div>
{form_section}
{mvp_block}
      <motion.section
        className="panel"
        style={{{{ marginTop: "1rem" }}}}
        initial={{{{ opacity: 0, y: 18 }}}}
        animate={{{{ opacity: 1, y: 0 }}}}
        transition={{{{ delay: 0.26 }}}}
      >
        <h2>Records</h2>
        <div className="empty">{{{empty}}}</div>
      </motion.section>
    </Page>
  );
}}
"""


def _title(s: str) -> str:
    return "".join(p[:1].upper() + p[1:] for p in re.split(r"[-_]+", s) if p) or "Value"


def _global_css() -> str:
    return """:root {
  --bg0: #07111a;
  --bg1: #0c1a24;
  --bg2: #122433;
  --line: rgba(140, 190, 210, 0.18);
  --text: #e8f2f6;
  --muted: #8aa3b0;
  --accent: #3ecf8e;
  --accent-2: #5eead4;
  --warn: #f0b429;
  --danger: #f07178;
  --glow: rgba(62, 207, 142, 0.35);
  --font-display: "Syne", system-ui, sans-serif;
  --font-body: "DM Sans", system-ui, sans-serif;
  --radius: 16px;
  --shadow: 0 24px 60px rgba(0, 0, 0, 0.35);
}

* { box-sizing: border-box; }
html, body, #root { min-height: 100%; }
body {
  margin: 0;
  font-family: var(--font-body);
  color: var(--text);
  background:
    radial-gradient(900px 480px at 8% -10%, rgba(62, 207, 142, 0.16), transparent 55%),
    radial-gradient(700px 420px at 100% 0%, rgba(94, 234, 212, 0.12), transparent 50%),
    linear-gradient(180deg, var(--bg0), #050b10 100%);
  background-attachment: fixed;
}

a { color: inherit; text-decoration: none; }
button, input, select, textarea { font: inherit; }
button { cursor: pointer; }

.app-shell { min-height: 100vh; display: flex; flex-direction: column; }
.app-header {
  display: flex; align-items: center; justify-content: space-between; gap: 1rem;
  padding: 1.1rem 1.5rem; border-bottom: 1px solid var(--line);
  background: color-mix(in srgb, var(--bg1) 86%, transparent);
  backdrop-filter: blur(12px); position: sticky; top: 0; z-index: 20;
}
.brand {
  font-family: var(--font-display); font-weight: 800; font-size: 1.35rem;
  letter-spacing: -0.03em;
}
.brand span { color: var(--accent); }
.nav { display: flex; gap: 0.35rem; flex-wrap: wrap; }
.nav a {
  padding: 0.45rem 0.85rem; border-radius: 999px; color: var(--muted);
  border: 1px solid transparent; transition: color 160ms ease, border-color 160ms ease, background 160ms ease;
}
.nav a:hover { color: var(--text); border-color: var(--line); }
.nav a.active {
  color: var(--bg0); background: linear-gradient(135deg, var(--accent), var(--accent-2));
  box-shadow: 0 0 24px var(--glow);
}

.app-main { width: min(1080px, 100%); margin: 0 auto; padding: 1.5rem 1.25rem 3rem; }
.page-title {
  font-family: var(--font-display); font-size: clamp(1.6rem, 3vw, 2.2rem);
  font-weight: 700; letter-spacing: -0.03em; margin: 0 0 0.35rem;
}
.page-sub { color: var(--muted); margin: 0 0 1.4rem; max-width: 42rem; line-height: 1.5; }

.grid { display: grid; gap: 1rem; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }
.panel {
  background: linear-gradient(180deg, color-mix(in srgb, var(--bg2) 92%, #1a3a2a), var(--bg1));
  border: 1px solid var(--line); border-radius: var(--radius); padding: 1.1rem 1.15rem;
  box-shadow: var(--shadow);
}
.panel h2 {
  font-family: var(--font-display); font-size: 1.05rem; margin: 0 0 0.75rem; font-weight: 700;
}
.stat b {
  display: block; font-family: var(--font-display); font-size: 1.6rem; margin-top: 0.35rem;
  background: linear-gradient(135deg, var(--text), var(--accent-2));
  -webkit-background-clip: text; background-clip: text; color: transparent;
}
.stat span { color: var(--muted); font-size: 0.82rem; }

label { display: block; font-size: 0.78rem; color: var(--muted); margin: 0.55rem 0 0.25rem; }
input, select, textarea {
  width: 100%; padding: 0.7rem 0.85rem; border-radius: 12px;
  border: 1px solid var(--line); background: rgba(7, 17, 26, 0.65); color: var(--text);
  outline: none; transition: border-color 160ms ease, box-shadow 160ms ease;
}
input:focus, select:focus, textarea:focus {
  border-color: color-mix(in srgb, var(--accent) 60%, var(--line));
  box-shadow: 0 0 0 3px rgba(62, 207, 142, 0.15);
}

.btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 0.4rem;
  margin-top: 0.9rem; padding: 0.7rem 1.15rem; border: 0; border-radius: 12px;
  background: linear-gradient(135deg, var(--accent), #2bb673); color: #04140c;
  font-weight: 700; transition: transform 140ms ease, box-shadow 140ms ease, filter 140ms ease;
}
.btn:hover { transform: translateY(-1px); box-shadow: 0 10px 28px var(--glow); filter: brightness(1.05); }
.btn.secondary {
  background: transparent; color: var(--text); border: 1px solid var(--line); font-weight: 600;
}
.btn.secondary:hover { box-shadow: none; border-color: var(--accent); }

.chip {
  display: inline-flex; align-items: center; padding: 0.25rem 0.65rem; border-radius: 999px;
  border: 1px solid var(--line); background: rgba(62, 207, 142, 0.08);
  color: var(--accent-2); font-size: 0.75rem; margin: 0.15rem 0.3rem 0.15rem 0;
}
.empty {
  border: 1px dashed var(--line); border-radius: 12px; padding: 1.25rem;
  color: var(--muted); text-align: center; line-height: 1.5;
}
.table { width: 100%; border-collapse: collapse; }
.table th, .table td {
  text-align: left; padding: 0.65rem 0.4rem; border-bottom: 1px solid var(--line);
  font-size: 0.9rem;
}
.table th { color: var(--muted); font-weight: 600; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.06em; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.85rem; }
.muted { color: var(--muted); }
.form-actions { display: flex; gap: 0.6rem; flex-wrap: wrap; align-items: center; }

@keyframes floaty {
  0%, 100% { transform: translateY(0); }
  50% { transform: translateY(-6px); }
}
.hero-orb {
  width: 72px; height: 72px; border-radius: 24px;
  background: linear-gradient(145deg, var(--accent), var(--accent-2));
  box-shadow: 0 0 40px var(--glow); animation: floaty 4.5s ease-in-out infinite;
}
"""


def build_react_preview_html(
    *,
    product: str | None = None,
    features: list[str] | None = None,
    api_paths: list[str] | None = None,
    include_tools: bool = False,
    default_tab: str | None = None,
    ui_spec: dict[str, Any] | None = None,
    product_brief: dict[str, Any] | None = None,
    reqspec: dict[str, Any] | None = None,
    openapi: dict[str, Any] | None = None,
    figma: dict[str, Any] | None = None,
) -> str:
    """Self-contained animated HTML for Results → 10. UI Design iframe."""
    spec = ui_spec or derive_ui_spec(
        product=product or "App",
        openapi=openapi,
        product_brief=product_brief,
        reqspec=reqspec,
        features=features,
        figma=figma,
    )
    name = str(spec.get("product") or product or "App")
    screens = list(spec.get("screens") or [])
    feats = list(features or spec.get("features") or [])
    paths = list(api_paths if api_paths is not None else (spec.get("openapi_paths") or []))
    chips = "".join(f'<span class="chip">{_esc(f)}</span>' for f in feats[:12]) or (
        '<span class="muted">From ReqSpec / LLD</span>'
    )
    path_rows = "".join(f"<tr><td class='mono'>{_esc(p)}</td></tr>" for p in paths[:16]) or (
        "<tr><td class='muted'>Paths from OpenAPI when domain-aligned</td></tr>"
    )
    default_id = default_tab or (screens[0].get("id") if screens else "home")
    figma_banner = ""
    if spec.get("figma_provided"):
        figma_banner = (
            f'<p class="muted" style="margin:0 0 1rem">Figma reference: '
            f'{_esc(spec.get("figma_url") or "uploaded export")} — layout follows design export + LLD.</p>'
        )
    elif spec.get("openapi_ignored"):
        figma_banner = (
            '<p class="muted" style="margin:0 0 1rem">'
            "OpenAPI looked like another product — UI IA taken from ReqSpec/LLD domain entities."
            "</p>"
        )

    def tab_btn(sid: str, label: str) -> str:
        cls = "active" if sid == default_id else ""
        return f'<button type="button" class="{cls}" data-tab="{_esc(sid)}">{_esc(label)}</button>'

    tabs_html = "".join(
        tab_btn(str(s.get("id")), str(s.get("nav") or s.get("id"))) for s in screens
    )
    views: list[str] = []
    for s in screens:
        sid = str(s.get("id") or "home")
        hidden = "" if sid == default_id else " hidden"
        fields = list(s.get("fields") or [])
        field_html = "".join(
            f'<div><label>{_esc(f.get("label") or f.get("id"))}</label>'
            f'<input placeholder="{_esc(f.get("placeholder") or "")}" /></div>'
            for f in fields
        )
        form_html = ""
        if fields:
            form_html = f"""
    <div class="panel rise">
      <h2>{_esc(s.get("primary_action") or "Action")}</h2>
      <div class="form-row">{field_html}</div>
      <button class="btn" type="button" data-action="{_esc(sid)}">{_esc(s.get("primary_action") or "Submit")}</button>
      <p class="muted msg" style="margin-top:.85rem">Studio preview — React sources under <span class="mono">frontend/src</span>.</p>
    </div>"""
        mvp = list(s.get("mvp_preview") or [])
        mvp_html = ""
        if mvp:
            mvp_chips = "".join(f'<span class="chip">{_esc(m)}</span>' for m in mvp[:6])
            mvp_html = f'<div class="panel"><h2>MVP from requirements</h2><div>{mvp_chips}</div></div>'
        views.append(
            f"""
  <section class="view" data-view="{_esc(sid)}"{hidden}>
    <h1 class="page-title">{_esc(s.get("title") or sid)}</h1>
    <p class="page-sub">{_esc(s.get("subtitle") or "")}</p>
    {figma_banner if sid == default_id else ""}
    <div class="grid">
      <div class="panel rise"><div class="orb"></div><p class="muted" style="margin:.9rem 0 0">LLD/ReqSpec-driven operator UI (Vite + React + Framer Motion).</p></div>
      <div class="panel rise d1 stat"><span>In-scope signals</span><div style="margin-top:.65rem">{chips}</div></div>
    </div>
    {form_html}
    {mvp_html}
    <div class="panel">
      <h2>Records</h2>
      <div class="empty">{_esc(s.get("empty") or "Empty until API connected.")}</div>
    </div>
  </section>"""
        )

    # Extra OpenAPI panel on last screen if paths exist
    if paths:
        views.append(
            f"""
  <section class="view" data-view="__api__" hidden>
    <h1 class="page-title">API surface</h1>
    <p class="page-sub">OpenAPI paths used when domain-aligned with the product.</p>
    <div class="panel"><table><thead><tr><th>Path</th></tr></thead><tbody>{path_rows}</tbody></table></div>
  </section>"""
        )
        tabs_html += tab_btn("__api__", "API")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{_esc(name)} — UI Design</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Syne:wght@600;700;800&display=swap" rel="stylesheet"/>
<style>
:root {{
  --bg0:#07111a; --bg1:#0c1a24; --bg2:#122433; --line:rgba(140,190,210,.18); --text:#e8f2f6;
  --muted:#8aa3b0; --accent:#3ecf8e; --accent-2:#5eead4; --glow:rgba(62,207,142,.35);
}}
* {{ box-sizing:border-box; }}
body {{
  margin:0; font-family:"DM Sans",system-ui,sans-serif; color:var(--text);
  background:
    radial-gradient(1000px 520px at 10% -12%, rgba(62,207,142,.18), transparent 55%),
    radial-gradient(800px 460px at 100% 0%, rgba(94,234,212,.14), transparent 50%),
    linear-gradient(180deg, var(--bg0), #050b10 100%);
  min-height:100vh;
}}
header {{
  display:flex; justify-content:space-between; align-items:center; gap:1rem; flex-wrap:wrap;
  padding:1.1rem 1.4rem; border-bottom:1px solid var(--line);
  background:rgba(12,26,36,.88); backdrop-filter:blur(14px); position:sticky; top:0; z-index:5;
}}
.brand {{ font-family:"Syne",sans-serif; font-weight:800; font-size:1.35rem; letter-spacing:-.03em; }}
.brand span {{ color:var(--accent); }}
.tabs {{ display:flex; gap:.35rem; flex-wrap:wrap; }}
.tabs button {{
  border:1px solid transparent; background:transparent; color:var(--muted);
  border-radius:999px; padding:.45rem .9rem; cursor:pointer; font:inherit;
  transition: color .18s ease, border-color .18s ease, background .18s ease, transform .18s ease;
}}
.tabs button:hover {{ color:var(--text); border-color:var(--line); transform:translateY(-1px); }}
.tabs button.active {{
  color:#04140c; background:linear-gradient(135deg,var(--accent),var(--accent-2));
  box-shadow:0 0 24px var(--glow);
}}
main {{ width:min(1000px,100%); margin:0 auto; padding:1.5rem 1.25rem 2.8rem; }}
.view {{ animation: enter .5s cubic-bezier(.22,1,.36,1) both; }}
@keyframes enter {{
  from {{ opacity:0; transform:translateY(18px); filter:blur(6px); }}
  to {{ opacity:1; transform:none; filter:none; }}
}}
@keyframes floaty {{ 0%,100%{{transform:translateY(0)}} 50%{{transform:translateY(-7px)}} }}
.page-title {{ font-family:"Syne",sans-serif; font-size:clamp(1.65rem,3.2vw,2.25rem); margin:0 0 .35rem; letter-spacing:-.03em; }}
.page-sub {{ color:var(--muted); margin:0 0 1.25rem; line-height:1.55; max-width:40rem; }}
.grid {{ display:grid; gap:1rem; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); margin-bottom:1rem; }}
.panel {{
  background:linear-gradient(180deg, color-mix(in srgb, var(--bg2) 92%, #1a3a2a), var(--bg1));
  border:1px solid var(--line); border-radius:18px; padding:1.15rem 1.2rem;
  box-shadow:0 24px 60px rgba(0,0,0,.38); margin-bottom:1rem;
}}
.panel h2 {{ font-family:"Syne",sans-serif; margin:0 0 .7rem; font-size:1.05rem; }}
.rise {{ animation: enter .55s cubic-bezier(.22,1,.36,1) both; }}
.rise.d1 {{ animation-delay:.08s; }}
.orb {{
  width:72px; height:72px; border-radius:24px;
  background:linear-gradient(145deg,var(--accent),var(--accent-2));
  box-shadow:0 0 40px var(--glow); animation:floaty 4.5s ease-in-out infinite;
}}
.chip {{
  display:inline-block; padding:.25rem .65rem; border-radius:999px; border:1px solid var(--line);
  background:rgba(62,207,142,.1); color:var(--accent-2); font-size:.75rem; margin:.15rem .25rem 0 0;
}}
label {{ display:block; font-size:.78rem; color:var(--muted); margin:.55rem 0 .25rem; }}
input {{
  width:100%; padding:.75rem .9rem; border-radius:12px; border:1px solid var(--line);
  background:rgba(7,17,26,.7); color:var(--text); outline:none;
}}
.btn {{
  margin-top:.9rem; border:0; border-radius:12px; padding:.72rem 1.15rem; cursor:pointer;
  background:linear-gradient(135deg,var(--accent),#2bb673); color:#04140c; font-weight:700;
}}
.btn:hover {{ filter:brightness(1.04); }}
.empty {{
  border:1px dashed var(--line); border-radius:14px; padding:1.25rem; color:var(--muted); text-align:center; line-height:1.5;
}}
.stat span {{ color:var(--muted); font-size:.82rem; }}
table {{ width:100%; border-collapse:collapse; }}
th,td {{ text-align:left; padding:.55rem .35rem; border-bottom:1px solid var(--line); font-size:.9rem; }}
th {{ color:var(--muted); font-size:.72rem; text-transform:uppercase; letter-spacing:.06em; }}
.mono {{ font-family:ui-monospace,Menlo,monospace; font-size:.84rem; }}
.muted {{ color:var(--muted); }}
.badge {{
  display:inline-block; margin-left:.5rem; padding:.18rem .55rem; border-radius:999px;
  border:1px solid var(--line); font-size:.68rem; color:var(--accent-2); vertical-align:middle;
}}
.form-row {{ display:grid; gap:.75rem; grid-template-columns:1.4fr 1fr; }}
@media (max-width:720px) {{ .form-row {{ grid-template-columns:1fr; }} }}
</style>
</head>
<body>
<header>
  <div class="brand">{_esc(name)} <span>Studio</span><span class="badge">React · LLD</span></div>
  <div class="tabs" id="tabs">{tabs_html}</div>
</header>
<main>
{"".join(views)}
</main>
<script>
const tabs = document.getElementById("tabs");
const views = [...document.querySelectorAll("[data-view]")];
function showTab(id) {{
  tabs.querySelectorAll("button").forEach((b) => b.classList.toggle("active", b.dataset.tab === id));
  views.forEach((v) => {{
    const show = v.dataset.view === id;
    v.hidden = !show;
    if (show) {{ v.style.animation = "none"; v.offsetHeight; v.style.animation = ""; }}
  }});
}}
tabs.addEventListener("click", (e) => {{
  const btn = e.target.closest("button[data-tab]");
  if (btn) showTab(btn.dataset.tab);
}});
document.querySelectorAll("[data-action]").forEach((btn) => {{
  btn.addEventListener("click", () => {{
    const msg = btn.parentElement?.querySelector(".msg");
    if (msg) msg.textContent = "Preview only — open the Vite React app to call the LLD/OpenAPI endpoints.";
  }});
}});
</script>
</body>
</html>
"""


def preview_pages_from_react_files(files: dict[str, str], product: str) -> dict[str, str]:
    """Map for html_artifact_document / Results preview."""
    preview = files.get("_studio_preview.html") or build_react_preview_html(product=product)
    return {"preview.html": preview, "react-app": "Vite React + Framer Motion (see frontend/src)"}


def is_react_source_path(path: str) -> bool:
    p = path.lower()
    return p.endswith(
        (
            ".tsx",
            ".ts",
            ".jsx",
            ".js",
            ".css",
            ".json",
            ".html",
            ".md",
            ".svg",
        )
    )


def looks_like_source(path: str, content: str) -> bool:
    if not isinstance(content, str) or len(content.strip()) < 8:
        return False
    text = content.strip()
    low = path.lower()
    if low.endswith((".tsx", ".jsx")):
        return ("import " in text or "export " in text or "<" in text) and len(text) > 40
    if low.endswith((".ts", ".js")):
        return "export " in text or "import " in text or "function " in text
    if low.endswith(".css"):
        return "{" in text
    if low.endswith(".json"):
        return text.startswith("{") or text.startswith("[")
    if low.endswith(".html"):
        return text.lower().startswith("<!doctype") or text.lower().startswith("<html")
    if low.endswith(".md"):
        return len(text) > 20
    return False
