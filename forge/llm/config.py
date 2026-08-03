from __future__ import annotations

import os
from dataclasses import dataclass

from ..secrets import scrub


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class LLMConfig:
    """Production LLM settings loaded from environment."""

    api_key: str
    model: str
    base_url: str | None
    timeout_s: float
    max_retries: int
    temperature: float
    max_output_tokens: int
    enabled_override: bool | None  # None = auto (key present)

    @property
    def enabled(self) -> bool:
        if self.enabled_override is not None:
            return self.enabled_override and bool(self.api_key)
        return bool(self.api_key)

    @classmethod
    def from_env(cls) -> "LLMConfig":
        # The ECS worker gets this injected straight from Parameter Store, so the
        # placeholder never passes through forge.secrets.hydrate — scrub it here
        # too, or `enabled` goes true on a key that cannot authenticate.
        key = scrub(_env("FORGE_LLM_API_KEY")) or scrub(_env("OPENAI_API_KEY"))
        base = _env("FORGE_LLM_BASE_URL") or _env("OPENAI_BASE_URL") or None
        enabled_raw = _env("FORGE_LLM_ENABLED")
        enabled_override: bool | None
        if not enabled_raw:
            enabled_override = None
        else:
            enabled_override = enabled_raw.lower() in {"1", "true", "yes", "on"}
        return cls(
            api_key=key,
            model=_env("FORGE_LLM_MODEL", "gpt-4o-mini"),
            base_url=base,
            timeout_s=_env_float("FORGE_LLM_TIMEOUT_S", 90.0),
            max_retries=_env_int("FORGE_LLM_MAX_RETRIES", 3),
            temperature=_env_float("FORGE_LLM_TEMPERATURE", 0.2),
            max_output_tokens=_env_int("FORGE_LLM_MAX_OUTPUT_TOKENS", 4096),
            enabled_override=enabled_override,
        )


def load_config() -> LLMConfig:
    return LLMConfig.from_env()
