"""
Provider registry and factory.

A provider instance is always built from two sources: the operator-editable
:class:`~app.models.ai.AIProviderConfig` row (endpoint, model, limits) and the
environment (the credential).  Keys are deliberately *not* a database column —
the row only records that one exists.
"""

from __future__ import annotations

from typing import Any

from app.ai.base import BaseProvider
from app.ai.providers.claude import ClaudeProvider, resolve_claude_key
from app.ai.providers.lmstudio import LMStudioProvider
from app.ai.providers.nvidia import NvidiaProvider, resolve_nvidia_key
from app.ai.providers.openai_compat import OpenAICompatProvider
from app.core.enums import AIProvider

PROVIDER_CLASSES: dict[str, type[BaseProvider]] = {
    str(AIProvider.LMSTUDIO): LMStudioProvider,
    str(AIProvider.NVIDIA): NvidiaProvider,
    str(AIProvider.CLAUDE): ClaudeProvider,
}

#: Credential resolvers, keyed by provider.  LM Studio is local and needs none.
KEY_RESOLVERS: dict[str, Any] = {
    str(AIProvider.NVIDIA): resolve_nvidia_key,
    str(AIProvider.CLAUDE): resolve_claude_key,
}


def normalize_provider(provider: str) -> str:
    """Accept ``nvidia``, ``NVIDIA`` or ``AIProvider.NVIDIA`` alike."""
    return str(provider or "").strip().upper()


def api_key_for(provider: str) -> str:
    """Current credential for *provider*, or an empty string when unset."""
    resolver = KEY_RESOLVERS.get(normalize_provider(provider))
    return resolver() if resolver else ""


def build_provider(config: Any = None, *, provider: str | None = None) -> BaseProvider:
    """
    Instantiate the client for *provider*, layering the DB row over defaults.

    ``config`` is an :class:`AIProviderConfig` when one exists; passing only
    ``provider`` yields a client built purely from settings, which is what the
    health screen needs before anything has been configured.
    """
    key = normalize_provider(provider or getattr(config, "provider", ""))
    cls = PROVIDER_CLASSES.get(key)
    if cls is None:
        raise KeyError(key)

    kwargs: dict[str, Any] = {"api_key": api_key_for(key)}
    if config is not None:
        kwargs.update(
            base_url=getattr(config, "base_url", None) or None,
            default_model=getattr(config, "default_model", None) or None,
            timeout=getattr(config, "timeout_seconds", None) or None,
            max_tokens=getattr(config, "max_tokens", None) or None,
            temperature=getattr(config, "temperature", None),
        )
    return cls(**{k: v for k, v in kwargs.items() if v is not None})


__all__ = [
    "BaseProvider",
    "ClaudeProvider",
    "LMStudioProvider",
    "NvidiaProvider",
    "OpenAICompatProvider",
    "PROVIDER_CLASSES",
    "api_key_for",
    "build_provider",
    "normalize_provider",
]
