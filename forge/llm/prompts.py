from __future__ import annotations

from functools import lru_cache

from ..core.paths import paths as forge_paths

PROMPTS_DIR = forge_paths().prompts


@lru_cache(maxsize=64)
def load_prompt(name: str) -> str:
    """Load `prompts/{name}.txt` or `prompts/{name}_agent.txt`."""
    candidates = [
        PROMPTS_DIR / f"{name}.txt",
        PROMPTS_DIR / f"{name}_agent.txt",
        PROMPTS_DIR / f"{name.replace('_agent', '')}_agent.txt",
    ]
    for path in candidates:
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    raise FileNotFoundError(f"Prompt not found for agent '{name}' in {PROMPTS_DIR}")


def system_prompt(agent: str, extra: str = "") -> str:
    try:
        base = load_prompt(agent)
    except FileNotFoundError:
        base = (
            f"You are the {agent} agent for Forge, a production Agentic SDLC platform. "
            "Return valid JSON only. Be precise, production-minded, and do not invent SLAs."
        )
    footer = (
        "\n\nOUTPUT RULES:\n"
        "- Respond with a single JSON object only (no markdown fences).\n"
        "- Cite the uploaded requirement document; do not ignore it.\n"
        "- Prefer concrete, implementable engineering artifacts.\n"
        "- If information is missing, put questions under key `open_questions` "
        "and proceed with explicit `assumptions`.\n"
    )
    if extra:
        return f"{base}\n\n{extra.strip()}{footer}"
    return f"{base}{footer}"
