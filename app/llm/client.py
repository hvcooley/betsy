"""Anthropic wrapper: retries, logging, versioning — TODO"""

import anthropic

from app.config import settings


def get_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)
