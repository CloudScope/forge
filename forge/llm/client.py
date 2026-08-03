from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

from .config import LLMConfig, load_config
from .errors import LLMConfigError, LLMResponseError, LLMTransportError
from .pricing import estimate_cost_usd

logger = logging.getLogger("forge.llm")


@dataclass
class LLMResult:
    content: dict[str, Any]
    raw_text: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float
    latency_ms: float
    attempts: int


class LLMClient:
    """
    Production-style OpenAI-compatible chat client.

    - Retries with exponential backoff on transient failures
    - JSON object response mode
    - Timeout + token/cost accounting metadata
    """

    def __init__(self, config: Optional[LLMConfig] = None):
        self.config = config or load_config()
        self._client = None

    def enabled(self) -> bool:
        return self.config.enabled

    def _get_client(self):
        if self._client is not None:
            return self._client
        if not self.config.api_key:
            raise LLMConfigError(
                "No LLM API key. Set FORGE_LLM_API_KEY or OPENAI_API_KEY."
            )
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise LLMConfigError(
                "openai package not installed. Run: pip install openai"
            ) from exc
        kwargs: dict[str, Any] = {
            "api_key": self.config.api_key,
            "timeout": self.config.timeout_s,
            "max_retries": 0,  # we own retry loop
        }
        if self.config.base_url:
            kwargs["base_url"] = self.config.base_url
        self._client = OpenAI(**kwargs)
        return self._client

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
    ) -> LLMResult:
        if not self.enabled():
            raise LLMConfigError("LLM disabled or API key missing")

        client = self._get_client()
        model = model or self.config.model
        temperature = (
            self.config.temperature if temperature is None else temperature
        )
        last_err: Exception | None = None
        started = time.time()
        attempts = 0

        for attempt in range(1, self.config.max_retries + 1):
            attempts = attempt
            try:
                resp = client.chat.completions.create(
                    model=model,
                    temperature=temperature,
                    max_tokens=self.config.max_output_tokens,
                    response_format={"type": "json_object"},
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                )
                choice = resp.choices[0].message.content or ""
                usage = resp.usage
                prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
                completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
                total_tokens = int(
                    getattr(usage, "total_tokens", 0)
                    or (prompt_tokens + completion_tokens)
                )
                try:
                    data = json.loads(choice)
                except json.JSONDecodeError as exc:
                    raise LLMResponseError(f"Invalid JSON from model: {exc}") from exc
                if not isinstance(data, dict):
                    raise LLMResponseError("Model JSON root must be an object")
                latency_ms = (time.time() - started) * 1000
                cost = estimate_cost_usd(
                    model=model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                )
                return LLMResult(
                    content=data,
                    raw_text=choice,
                    model=getattr(resp, "model", None) or model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    cost_usd=cost,
                    latency_ms=latency_ms,
                    attempts=attempts,
                )
            except LLMResponseError:
                raise
            except Exception as exc:  # noqa: BLE001 — classify below
                last_err = exc
                name = type(exc).__name__
                msg = str(exc).lower()
                retryable = any(
                    x in name.lower() or x in msg
                    for x in (
                        "timeout",
                        "rate",
                        "429",
                        "500",
                        "502",
                        "503",
                        "504",
                        "unavailable",
                        "connection",
                        "apiconnection",
                        "internalserver",
                    )
                )
                logger.warning(
                    "LLM attempt %s/%s failed (%s): %s",
                    attempt,
                    self.config.max_retries,
                    name,
                    exc,
                )
                if not retryable or attempt >= self.config.max_retries:
                    break
                time.sleep(min(2 ** (attempt - 1), 8))

        raise LLMTransportError(
            f"LLM call failed after {attempts} attempts: {last_err}"
        )


_default_client: LLMClient | None = None


def get_client() -> LLMClient:
    global _default_client
    if _default_client is None:
        _default_client = LLMClient()
    return _default_client
