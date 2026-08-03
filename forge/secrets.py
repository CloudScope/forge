"""
Resolve SSM Parameter Store references into the environment at startup.

Terraform passes *parameter names*, not values: `FORGE_API_TOKEN_PARAM` rather
than `FORGE_API_TOKEN`. That keeps the secrets out of the Lambda configuration
that anyone with console read access can see, and lets them be rotated in
Parameter Store without a redeploy.

Standard-tier parameters are free, which is why this is Parameter Store and not
Secrets Manager ($0.40/secret/month) — nothing here needs managed rotation.

A value already present in the environment always wins, so local development and
the test suite never reach for AWS.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("forge.secrets")

# environment variable → the variable holding its parameter name
PARAMETER_REFS = {
    "FORGE_API_TOKEN": "FORGE_API_TOKEN_PARAM",
    "FORGE_LLM_API_KEY": "FORGE_LLM_KEY_PARAM",
}

_hydrated = False


def hydrate(force: bool = False) -> list[str]:
    """
    Populate referenced secrets from Parameter Store. Returns the names resolved.

    Runs once per process (cold start). Failures are logged, never raised: a
    missing LLM key degrades the agents to their heuristic path, and a missing
    API token leaves the stricter loopback-only auth posture in place — both are
    safe outcomes, whereas refusing to boot is not.
    """
    global _hydrated
    if _hydrated and not force:
        return []

    wanted = {
        target: os.environ[ref]
        for target, ref in PARAMETER_REFS.items()
        if os.environ.get(ref) and not os.environ.get(target)
    }
    _hydrated = True
    if not wanted:
        return []

    try:
        import boto3

        ssm = boto3.client("ssm")
    except Exception as exc:  # noqa: BLE001 — boto3 absent locally is expected
        logger.warning("Cannot resolve parameters (%s); using environment as-is", exc)
        return []

    resolved: list[str] = []
    for target, name in wanted.items():
        try:
            response = ssm.get_parameter(Name=name, WithDecryption=True)
        except Exception as exc:  # noqa: BLE001 — one bad parameter must not block boot
            logger.warning("Could not read parameter %s: %s", name, exc)
            continue
        value = (response.get("Parameter") or {}).get("Value") or ""
        if value:
            os.environ[target] = value
            resolved.append(target)

    if resolved:
        logger.info("Resolved %s from Parameter Store", ", ".join(resolved))
    return resolved


def reset() -> None:
    """Test hook: allow re-hydration."""
    global _hydrated
    _hydrated = False
