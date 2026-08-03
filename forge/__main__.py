from __future__ import annotations

import argparse
import sys

from .engine import OrchestrationEngine


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv

        from .core.paths import ensure_runtime_dirs, paths

        p = ensure_runtime_dirs()
        load_dotenv(p.env_file)
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    parser = argparse.ArgumentParser(
        prog="forge",
        description="Forge — production multi-agent SDLC platform",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="Run a scenario playbook")
    run_p.add_argument(
        "scenario",
        choices=[
            "greenfield",
            "brownfield",
            "ambiguous",
            "from_document",
            "production",
            "all",
        ],
        help="Scenario to execute",
    )
    run_p.add_argument(
        "--interactive",
        action="store_true",
        help="Prompt for human approvals instead of auto-approve",
    )
    run_p.add_argument(
        "--document",
        default=None,
        help="Path to requirement document (for from_document scenario)",
    )
    run_p.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Max parallel compute workers for ready-set fan-out (default: 4)",
    )

    dash_p = sub.add_parser("dashboard", help="Start upload studio via uvicorn")
    dash_p.add_argument("--host", default="127.0.0.1")
    dash_p.add_argument("--port", type=int, default=8787)
    dash_p.add_argument("--reload", action="store_true", help="Auto-reload (dev)")

    agents_p = sub.add_parser("agents", help="List the agent roster")

    args = parser.parse_args(argv)

    if args.cmd == "agents":
        from .agents import AGENT_ROSTER

        print("\nForge agent roster (end-to-end SDLC)\n" + "-" * 72)
        for name, mission in AGENT_ROSTER:
            print(f"  {name:<22} {mission}")
        print("\nStores: memory (context) · audit (traces) · state (checkpoints)")
        print("UI:     uvicorn main:app --reload --port 8787")
        print("   or:  python -m forge dashboard\n")
        return 0

    if args.cmd == "dashboard":
        from .dashboard import serve

        serve(host=args.host, port=args.port, reload=args.reload)
        return 0

    from .graph import LangGraphRuntime, use_langgraph
    from .graph.tracing import configure_langsmith

    workers = max(1, args.workers)
    auto = not args.interactive
    lg = use_langgraph()
    ls = configure_langsmith()
    print(
        f"Orchestrator: {'langgraph' if lg else 'legacy'} | "
        f"LangSmith: {'on' if ls.get('enabled') else 'off'} "
        f"(project={ls.get('project')})"
    )
    # Unattended CLI demos may auto-decide plan/arch; --interactive prompts on stdin.
    runtime = (
        LangGraphRuntime(
            auto_approve=auto,
            max_workers=workers,
            cli_demo_mode=auto,
            allow_stdin_prompt=args.interactive,
        )
        if lg
        else None
    )
    engine = runtime.engine if runtime else OrchestrationEngine(
        auto_approve=auto,
        max_workers=workers,
        cli_demo_mode=auto,
        allow_stdin_prompt=args.interactive,
    )
    scenarios = (
        ["greenfield", "brownfield", "ambiguous"]
        if args.scenario == "all"
        else [args.scenario]
    )

    failed = False
    for name in scenarios:
        print("\n" + "=" * 72)
        print(f"SCENARIO: {name.upper()}")
        print("=" * 72)
        try:
            if name == "greenfield":
                wf = engine.plan("greenfield")
                if runtime:
                    runtime.start(wf)
                else:
                    engine.run(wf)
            elif name == "brownfield":
                # Brownfield impact path stays on the classic engine API.
                engine.run_brownfield_with_impact()
            elif name == "ambiguous":
                wf = engine.plan("ambiguous", facts={"ambiguous_brief": True})
                if runtime:
                    runtime.start(wf)
                else:
                    engine.run(wf)
            elif name in ("from_document", "production"):
                from pathlib import Path

                from .core.paths import paths as forge_paths
                from .doc_ingest import extract_text, summarize_document

                doc = Path(
                    args.document
                    or forge_paths().examples / "tinyurl-requirements.md"
                )
                data = doc.read_bytes()
                text = extract_text(doc, data)
                summary = summarize_document(text)
                wf = engine.prepare_from_document(
                    text=text, filename=doc.name, summary=summary
                )
                if runtime:
                    runtime.start(wf)
                else:
                    engine.run(wf)
        except Exception as exc:
            failed = True
            print(f"\nScenario {name} FAILED: {exc}", file=sys.stderr)

    if not failed:
        print("\nTip: open the studio → uvicorn main:app --port 8787")
        print("Upload URL → http://127.0.0.1:8787/")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
