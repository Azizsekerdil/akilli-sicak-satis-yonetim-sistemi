"""
Anthropic Claude — a different wire format, not an OpenAI clone.

Three differences drive this implementation:

* Authentication is ``x-api-key`` plus a mandatory ``anthropic-version`` header,
  not ``Authorization: Bearer``.
* The system prompt is a **top-level ``system`` field**, not a message with
  ``role="system"`` — sending it as a message is rejected.
* Structured output is requested through a forced tool call rather than
  ``response_format``.

No key is configured by default.  The client stays constructible and simply
reports "not configured" so the provider screen renders instead of crashing.
"""

from __future__ import annotations

import base64
import os
import time
from typing import Any

from app.ai.base import BaseProvider, ChatMessage, ChatResult, EmbeddingResult
from app.core.config import settings
from app.core.enums import AIProvider
from app.core.exceptions import AIProviderError
from app.core.utils import dumps

#: Pinned by Anthropic's own guidance — the API refuses requests without it.
ANTHROPIC_VERSION = "2023-06-01"


def resolve_claude_key() -> str:
    return (
        settings.claude_api_key
        or os.getenv("VS_CLAUDE_API_KEY", "")
        or os.getenv("ANTHROPIC_API_KEY", "")
        or os.getenv("CLAUDE_API_KEY", "")
    ).strip()


def _image_block(image: str) -> dict[str, Any]:
    """Turn a data: URI or an https URL into an Anthropic image block."""
    if image.startswith("data:"):
        header, _, payload = image.partition(",")
        media_type = header[5:].split(";")[0] or "image/png"
        if ";base64" not in header:
            payload = base64.b64encode(payload.encode()).decode()
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": payload},
        }
    return {"type": "image", "source": {"type": "url", "url": image}}


class ClaudeProvider(BaseProvider):
    """Anthropic Messages API client."""

    name = str(AIProvider.CLAUDE)
    requires_api_key = True

    messages_path = "/messages"
    models_path = "/models"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        default_model: str | None = None,
        timeout: int | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        embedding_model: str | None = None,  # noqa: ARG002 - uniform factory signature
    ) -> None:
        super().__init__(
            base_url=base_url or settings.claude_base_url,
            api_key=api_key or resolve_claude_key(),
            default_model=default_model or settings.claude_model,
            timeout=timeout or settings.claude_timeout,
            max_tokens=max_tokens or settings.claude_max_tokens,
            temperature=(
                settings.claude_temperature if temperature is None else temperature
            ),
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-api-key": self._api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        }

    # ------------------------------------------------------------------ #
    # Models
    # ------------------------------------------------------------------ #
    def list_models(self) -> list[str]:
        payload = self._request("GET", self.models_path, timeout=min(30, self.timeout))
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            return []
        return sorted(
            str(item.get("id")) for item in data if isinstance(item, dict) and item.get("id")
        )

    # ------------------------------------------------------------------ #
    # Chat
    # ------------------------------------------------------------------ #
    def _split_messages(
        self, messages: list[ChatMessage]
    ) -> tuple[str, list[dict[str, Any]]]:
        """Hoist system turns into the top-level field, keep the rest in order."""
        system_parts: list[str] = []
        turns: list[dict[str, Any]] = []
        for message in messages:
            if message.is_system():
                if message.content:
                    system_parts.append(message.content)
                continue
            role = "assistant" if message.role == "assistant" else "user"
            if message.images:
                blocks: list[dict[str, Any]] = [_image_block(i) for i in message.images]
                if message.content:
                    blocks.append({"type": "text", "text": message.content})
                turns.append({"role": role, "content": blocks})
            else:
                turns.append({"role": role, "content": message.content})

        if not turns:
            # The API requires at least one user turn; an all-system prompt is a
            # caller bug we translate into a harmless nudge rather than a 400.
            turns.append({"role": "user", "content": "."})
        return "\n\n".join(system_parts), turns

    def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout: int | None = None,
        json_schema: dict[str, Any] | None = None,
        stream: bool = False,
    ) -> ChatResult:
        chosen = model or self.default_model
        if not chosen:
            raise AIProviderError("ai.no_provider", params={"provider": self.name})

        system_prompt, turns = self._split_messages(messages)
        body: dict[str, Any] = {
            "model": chosen,
            "messages": turns,
            "max_tokens": self._effective_max_tokens(max_tokens),
            "temperature": (
                self.temperature if temperature is None else float(temperature)
            ),
        }
        if system_prompt:
            body["system"] = system_prompt
        if json_schema:
            tool_name = json_schema.get("name", "structured_response")
            body["tools"] = [
                {
                    "name": tool_name,
                    "description": json_schema.get(
                        "description", "Return the answer in this exact structure."
                    ),
                    "input_schema": json_schema.get("schema", json_schema),
                }
            ]
            body["tool_choice"] = {"type": "tool", "name": tool_name}

        started = time.perf_counter()
        payload = self._request(
            "POST", self.messages_path, json_body=body, timeout=timeout
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        return self._parse_message(payload, chosen, latency_ms, keep_raw=stream)

    def _parse_message(
        self,
        payload: dict[str, Any],
        model: str,
        latency_ms: int,
        *,
        keep_raw: bool = False,
    ) -> ChatResult:
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        for block in payload.get("content") or []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                text_parts.append(block.get("text", ""))
            elif kind == "thinking":
                thinking_parts.append(block.get("thinking", ""))
            elif kind == "tool_use":
                # Forced-tool structured output: the arguments *are* the answer.
                text_parts.append(dumps(block.get("input") or {}))

        usage = payload.get("usage") or {}
        return ChatResult(
            content="".join(text_parts).strip(),
            reasoning="\n".join(p for p in thinking_parts if p) or None,
            model=str(payload.get("model") or model),
            provider=self.name,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            reasoning_tokens=0,
            total_tokens=int(usage.get("input_tokens") or 0)
            + int(usage.get("output_tokens") or 0),
            latency_ms=latency_ms,
            finish_reason=payload.get("stop_reason"),
            raw=payload if keep_raw else None,
        )

    # ------------------------------------------------------------------ #
    # Embeddings
    # ------------------------------------------------------------------ #
    def embed(self, texts: list[str], *, model: str | None = None) -> EmbeddingResult:
        """Anthropic ships no embedding endpoint — the router must fail over."""
        raise AIProviderError(
            "ai.connection_failed",
            params={"error": "embeddings are not supported by this provider"},
            detail=f"{self.name} has no embeddings endpoint",
        )


__all__ = ["ClaudeProvider", "resolve_claude_key", "ANTHROPIC_VERSION"]
