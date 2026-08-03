from __future__ import annotations


class LLMError(Exception):
    """Base LLM failure (caller should fall back to heuristics)."""


class LLMConfigError(LLMError):
    """Missing/invalid configuration."""


class LLMResponseError(LLMError):
    """Model returned unusable content."""


class LLMTransportError(LLMError):
    """Network/API transport failure after retries."""
