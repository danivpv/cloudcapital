"""Runtime secret resolution and shared API header auth.

Secrets live in Secrets Manager and are referenced by ARN through env vars:

- ``CC_OPENROUTER_SECRET_ARN`` — JSON secret, key ``api_key``
- ``CC_AUTH_SECRET_ARN``       — JSON secret, key ``header`` (x-demo-auth gate)

Local/SAM runs leave the ARNs empty: auth is disabled and the LLM key is read
from the environment directly (CC_OPENROUTER_API_KEY).
"""

from __future__ import annotations

import functools
import json
from typing import Any


@functools.lru_cache(maxsize=8)
def secret_payload(arn: str) -> str | dict[str, Any]:
    """Return the secret as plaintext str or parsed JSON dict.

    Accepts both storage styles: e.g. the OpenRouter key may be stored as the
    raw ``sk-or-...`` string, or as ``{"api_key": "..."}`` JSON.
    """
    import boto3

    response = boto3.client("secretsmanager").get_secret_value(SecretId=arn)
    raw = response.get("SecretString") or ""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def resolve_secret(arn: str | None, *keys: str) -> str | None:
    """Plaintext secret, or the first non-empty key of a JSON secret.

    None if no ARN or nothing matches.
    """
    if not arn:
        return None
    payload = secret_payload(arn)
    if isinstance(payload, str):
        return payload or None
    for key in keys:
        value = payload.get(key)
        if value:
            return str(value)
    return None


def authorized(event: dict[str, Any], arn: str | None) -> bool:
    """Enforce the x-demo-auth shared header when an auth secret is configured.

    No ARN (local development) -> open access.
    """
    if not arn:
        return True
    expected = resolve_secret(arn, "header")
    headers = {str(k).lower(): v for k, v in (event.get("headers") or {}).items()}
    return headers.get("x-demo-auth") == expected
