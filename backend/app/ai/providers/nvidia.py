"""
NVIDIA NIM — hosted, OpenAI-compatible, Bearer-authenticated.

The credential is read from the environment (``VS_NVIDIA_API_KEY`` first, then
a bare ``NVIDIA_API_KEY`` so an existing developer environment keeps working).
It is never written to the database, never logged and never returned by the API.
"""

from __future__ import annotations

import os

from app.ai.providers.openai_compat import OpenAICompatProvider
from app.core.config import settings
from app.core.enums import AIProvider


def resolve_nvidia_key() -> str:
    """Prefer the namespaced variable, fall back to the vendor's own name."""
    return (
        settings.nvidia_api_key
        or os.getenv("VS_NVIDIA_API_KEY", "")
        or os.getenv("NVIDIA_API_KEY", "")
    ).strip()


class NvidiaProvider(OpenAICompatProvider):
    """NVIDIA's model catalogue behind an OpenAI-shaped API."""

    name = str(AIProvider.NVIDIA)
    requires_api_key = True

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        default_model: str | None = None,
        embedding_model: str | None = None,
        timeout: int | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url or settings.nvidia_base_url,
            api_key=api_key or resolve_nvidia_key(),
            default_model=default_model or settings.nvidia_model,
            timeout=timeout or settings.nvidia_timeout,
            max_tokens=max_tokens or settings.nvidia_max_tokens,
            temperature=(
                settings.nvidia_temperature if temperature is None else temperature
            ),
        )
        self.default_embedding_model = embedding_model or "baai/bge-m3"

    def _headers(self) -> dict[str, str]:
        headers = super()._headers()
        headers["Authorization"] = f"Bearer {self._api_key}"
        return headers


__all__ = ["NvidiaProvider", "resolve_nvidia_key"]
