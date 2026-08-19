"""
Shared implementation for every OpenAI-compatible endpoint.

LM Studio (local) and NVIDIA NIM (hosted) both expose ``/chat/completions``,
``/embeddings`` and ``/models`` with the same JSON shapes, so the wire logic
lives here once.  Subclasses only supply authentication and defaults.

The one non-standard detail this class handles deliberately: local reasoning
models return the answer in ``choices[0].message.content`` *and* their internal
monologue in ``choices[0].message.reasoning_content``, with the thinking cost
reported under ``usage.completion_tokens_details.reasoning_tokens``.  Ignoring
those fields would under-report token spend and hide the model's reasoning from
the audit trail.
"""

from __future__ import annotations

import time
from typing import Any

from app.ai.base import BaseProvider, ChatMessage, ChatResult, EmbeddingResult
from app.core.exceptions import AIProviderError


def _content_parts(message: ChatMessage) -> Any:
    """
    Render one message body.

    Plain text stays a plain string — some local servers reject the multi-part
    form for text-only turns — while images switch to the vision content array.
    """
    if not message.images:
        return message.content
    parts: list[dict[str, Any]] = []
    if message.content:
        parts.append({"type": "text", "text": message.content})
    for image in message.images:
        parts.append({"type": "image_url", "image_url": {"url": image}})
    return parts


class OpenAICompatProvider(BaseProvider):
    """Base class for vendors speaking the OpenAI REST dialect."""

    chat_path = "/chat/completions"
    embeddings_path = "/embeddings"
    models_path = "/models"

    #: Embedding model used when the caller does not name one.
    default_embedding_model: str = ""

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

        body: dict[str, Any] = {
            "model": chosen,
            "messages": [
                {"role": m.role, "content": _content_parts(m)} for m in messages
            ],
            "max_tokens": self._effective_max_tokens(max_tokens),
            "temperature": (
                self.temperature if temperature is None else float(temperature)
            ),
            # Streaming is accepted at the API surface for forward compatibility,
            # but the ORM and handlers are synchronous: we always consume the
            # complete response so token accounting is exact.
            "stream": False,
        }
        if json_schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": json_schema.get("name", "response"),
                    "schema": json_schema.get("schema", json_schema),
                    "strict": bool(json_schema.get("strict", True)),
                },
            }

        started = time.perf_counter()
        payload = self._request("POST", self.chat_path, json_body=body, timeout=timeout)
        latency_ms = int((time.perf_counter() - started) * 1000)
        return self._parse_chat(payload, chosen, latency_ms, stream_requested=stream)

    def _parse_chat(
        self,
        payload: dict[str, Any],
        model: str,
        latency_ms: int,
        *,
        stream_requested: bool = False,
    ) -> ChatResult:
        choices = payload.get("choices") or []
        message = (choices[0].get("message") or {}) if choices else {}
        finish_reason = choices[0].get("finish_reason") if choices else None

        content = message.get("content")
        if isinstance(content, list):  # some servers return content parts
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        reasoning = message.get("reasoning_content") or message.get("reasoning")

        usage = payload.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        result = ChatResult(
            content=(content or "").strip(),
            reasoning=(reasoning or None),
            model=str(payload.get("model") or model),
            provider=self.name,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            reasoning_tokens=int(details.get("reasoning_tokens") or 0),
            total_tokens=int(usage.get("total_tokens") or 0),
            latency_ms=latency_ms,
            finish_reason=str(finish_reason) if finish_reason else None,
            raw=payload if stream_requested else None,
        )
        if result.is_empty and result.reasoning:
            # The model spent its whole budget thinking.  Surfacing the reasoning
            # beats returning a blank answer the user cannot act on.
            raise AIProviderError(
                "ai.connection_failed",
                params={"error": "model produced reasoning but no answer"},
                detail=f"{self.name}/{model} exhausted max_tokens while reasoning",
            )
        return result

    # ------------------------------------------------------------------ #
    # Embeddings
    # ------------------------------------------------------------------ #
    def embed(self, texts: list[str], *, model: str | None = None) -> EmbeddingResult:
        chosen = model or self.default_embedding_model or self.default_model
        if not chosen:
            raise AIProviderError("ai.no_provider", params={"provider": self.name})
        payload = self._request(
            "POST",
            self.embeddings_path,
            json_body={"model": chosen, "input": list(texts)},
        )
        rows = payload.get("data") or []
        vectors = [
            [float(v) for v in (row.get("embedding") or [])]
            for row in rows
            if isinstance(row, dict)
        ]
        usage = payload.get("usage") or {}
        return EmbeddingResult(
            vectors=vectors,
            model=str(payload.get("model") or chosen),
            tokens=int(usage.get("total_tokens") or usage.get("prompt_tokens") or 0),
        )


__all__ = ["OpenAICompatProvider"]
