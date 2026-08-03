"""Forge LLM adapter — OpenAI-compatible, env-configured, heuristic fallback."""

from .client import LLMClient, LLMResult, get_client
from .config import LLMConfig, load_config
from .errors import LLMConfigError, LLMError, LLMResponseError, LLMTransportError
from .runtime import complete_json, llm_enabled, try_complete_json

__all__ = [
    "LLMClient",
    "LLMConfig",
    "LLMError",
    "LLMConfigError",
    "LLMResponseError",
    "LLMTransportError",
    "LLMResult",
    "complete_json",
    "get_client",
    "llm_enabled",
    "load_config",
    "try_complete_json",
]
