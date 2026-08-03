from __future__ import annotations

# Rough USD / 1M tokens for demo cost accounting (override via FORGE_LLM_*_PER_1M)
DEFAULT_INPUT_PER_1M = 0.15
DEFAULT_OUTPUT_PER_1M = 0.60


def estimate_cost_usd(
    *,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    input_per_1m: float | None = None,
    output_per_1m: float | None = None,
) -> float:
    """Estimate USD spend for budget dashboards (not billing-grade)."""
    # Slightly higher defaults for larger models
    if input_per_1m is None or output_per_1m is None:
        lower = model.lower()
        if "gpt-4o" in lower and "mini" not in lower:
            input_per_1m = input_per_1m or 2.50
            output_per_1m = output_per_1m or 10.0
        elif "gpt-4.1" in lower:
            input_per_1m = input_per_1m or 2.0
            output_per_1m = output_per_1m or 8.0
        else:
            input_per_1m = input_per_1m or DEFAULT_INPUT_PER_1M
            output_per_1m = output_per_1m or DEFAULT_OUTPUT_PER_1M
    return (prompt_tokens / 1_000_000.0) * input_per_1m + (
        completion_tokens / 1_000_000.0
    ) * output_per_1m
