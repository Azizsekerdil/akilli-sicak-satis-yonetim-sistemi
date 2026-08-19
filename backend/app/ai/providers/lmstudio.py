"""
LM Studio — the local, zero-cost, always-first provider.

Runs an OpenAI-compatible server on ``http://localhost:1234/v1`` and needs no
credential, which is precisely why it heads the failover chain: it costs
nothing, keeps company data on-premise, and is never blocked by the AI budget.
"""

from __future__ import annotations

from app.ai.providers.openai_compat import OpenAICompatProvider
from app.core.config import settings
from app.core.enums import AIProvider


class LMStudioProvider(OpenAICompatProvider):
    """Local inference server.  Authentication-free by design."""

    name = str(AIProvider.LMSTUDIO)
    requires_api_key = False

    def __init__(
        self,
        *,
        base_url: str | None = None,
        default_model: str | None = None,
        embedding_model: str | None = None,
        timeout: int | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        api_key: str | None = None,  # noqa: ARG002 - accepted for a uniform factory
    ) -> None:
        super().__init__(
            base_url=base_url or settings.lmstudio_base_url,
            api_key=None,
            default_model=default_model or settings.lmstudio_model,
            timeout=timeout or settings.lmstudio_timeout,
            max_tokens=max_tokens or settings.lmstudio_max_tokens,
            temperature=(
                settings.lmstudio_temperature if temperature is None else temperature
            ),
        )
        self.default_embedding_model = (
            embedding_model or settings.lmstudio_embedding_model
        )

    def _headers(self) -> dict[str, str]:
        # LM Studio ignores Authorization entirely; sending a placeholder key
        # would be noise in the logs of a server that never checks it.
        return super()._headers()


__all__ = ["LMStudioProvider"]
