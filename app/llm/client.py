"""Anthropic client construction, and the one place credentials are read.

Thin on purpose. Retries for transport failures (429, 5xx, connection resets) are
the SDK's job and it does them with backoff already; what this module adds is the
project's timeout, and a `MissingAPIKey` that fails at construction rather than on
the turn where a patient was waiting.

The *validation* retry ladder — a response that parsed as JSON but did not satisfy
the schema — is not here. That one is specific to the turn contract and lives in
`app/llm/anthropic_engine.py`, because exhausting it is a clinical event (a hard
failure is Tier 1) rather than a networking one.
"""

from __future__ import annotations

import anthropic

from app.config import settings


class MissingAPIKey(RuntimeError):
    """No credential configured. Raised at construction, never mid-conversation."""


def get_client(*, api_key: str | None = None, timeout: float | None = None) -> anthropic.Anthropic:
    """Build a client, or say plainly that there is nothing to build it from.

    An empty key would otherwise surface as an `AuthenticationError` on the first
    turn, which reads as a failed check-in rather than as a machine that was never
    configured to run one.
    """
    key = api_key if api_key is not None else settings.anthropic_api_key
    if not key:
        raise MissingAPIKey(
            "no Anthropic API key configured; set anthropic_api_key in .env "
            "(the deterministic doubles in app/llm/fake.py need no key, but they are "
            "test doubles and must never serve a patient)"
        )
    return anthropic.Anthropic(
        api_key=key,
        timeout=timeout if timeout is not None else settings.turn_timeout_seconds,
    )
