"""
Provider-agnostic AI contract.

Every model vendor speaks a slightly different dialect (OpenAI-compatible
``chat/completions``, Anthropic's ``/messages``, …).  The rest of the system
must not care, so each vendor is wrapped in a :class:`BaseProvider` that always
returns the same :class:`ChatResult`.

Two rules drive the design:

* **Never leak a credential.**  Keys live in the environment; only
  :func:`app.core.security.mask_secret` output is ever returned or logged.
* **Reasoning models are normal models.**  LM Studio's local models emit a
  separate ``reasoning_content`` stream and burn completion tokens on thinking
  before producing an answer, so reasoning text and reasoning-token counts are
  first-class fields rather than an afterthought.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.exceptions import AIProviderError
from app.core.logging_config import get_logger
from app.core.security import mask_secret

log = get_logger("app.ai.provider")

#: Reasoning models spend completion tokens *thinking* before they emit a single
#: character of answer.  Anything below this and the model reliably runs out of
#: budget mid-thought and returns empty content.
MIN_SAFE_MAX_TOKENS = 512

#: Connection-level failures are retried exactly once: a cold local server or a
#: recycled keep-alive socket is common, but a second failure means the provider
#: is genuinely down and the router should fail over instead of stalling.
_CONNECT_RETRIES = 1


@dataclass(slots=True)
class ChatMessage:
    """One turn of a conversation.  ``images`` may hold data: URIs or URLs."""

    role: str
    content: str
    images: list[str] | None = None

    def is_system(self) -> bool:
        return self.role == "system"


@dataclass(slots=True)
class ChatResult:
    """Normalised model answer plus the accounting the cost tracker needs."""

    content: str
    reasoning: str | None = None
    model: str = ""
    provider: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    finish_reason: str | None = None
    raw: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.total_tokens:
            self.total_tokens = self.input_tokens + self.output_tokens

    @property
    def is_empty(self) -> bool:
        return not (self.content or "").strip()


@dataclass(slots=True)
class EmbeddingResult:
    vectors: list[list[float]] = field(default_factory=list)
    model: str = ""
    tokens: int = 0

    @property
    def dimensions(self) -> int:
        return len(self.vectors[0]) if self.vectors else 0


class BaseProvider(ABC):
    """
    Common behaviour for every AI vendor.

    Subclasses implement the wire format; this class owns configuration,
    the HTTP client, timeouts and the single connection retry.
    """

    #: Stable identifier, matching :class:`app.core.enums.AIProvider`.
    name: str = "BASE"
    #: A local server needs no credential; remote vendors do.
    requires_api_key: bool = True

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        default_model: str | None = None,
        timeout: int = 120,
        max_tokens: int = 2048,
        temperature: float = 0.3,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self._api_key = (api_key or "").strip()
        self.default_model = default_model or ""
        self.timeout = int(timeout or 120)
        self.max_tokens = int(max_tokens or 2048)
        self.temperature = float(temperature if temperature is not None else 0.3)

    # ------------------------------------------------------------------ #
    # Configuration
    # ------------------------------------------------------------------ #
    def is_configured(self) -> bool:
        """True when the provider has everything it needs to be called."""
        if not self.base_url:
            return False
        return bool(self._api_key) if self.requires_api_key else True

    @property
    def masked_key(self) -> str:
        return mask_secret(self._api_key)

    def _require_configured(self) -> None:
        if not self.base_url:
            raise AIProviderError("ai.no_provider", params={"provider": self.name})
        if self.requires_api_key and not self._api_key:
            raise AIProviderError("ai.no_api_key", params={"provider": self.name})

    def _effective_max_tokens(self, requested: int | None) -> int:
        return max(int(requested or self.max_tokens), MIN_SAFE_MAX_TOKENS)

    # ------------------------------------------------------------------ #
    # HTTP plumbing
    # ------------------------------------------------------------------ #
    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json", "Accept": "application/json"}

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """
        Perform one API call, retrying once on a *connection* error only.

        A 4xx/5xx response is never retried here — the router decides whether to
        fail over, and retrying a rejected request just doubles the bill.
        """
        self._require_configured()
        url = self._url(path)
        limit = timeout or self.timeout
        last_exc: Exception | None = None

        for attempt in range(_CONNECT_RETRIES + 1):
            try:
                with httpx.Client(timeout=httpx.Timeout(limit, connect=min(15.0, limit))) as client:
                    response = client.request(
                        method, url, json=json_body, headers=self._headers()
                    )
                if response.status_code >= 400:
                    raise AIProviderError(
                        "ai.connection_failed",
                        params={"error": self._error_summary(response)},
                        detail=f"{self.name} HTTP {response.status_code}",
                        status_code=429 if response.status_code == 429 else 502,
                    )
                return response.json()
            except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as exc:
                last_exc = exc
                if attempt < _CONNECT_RETRIES:
                    time.sleep(0.4)
                    continue
            except httpx.TimeoutException as exc:
                raise AIProviderError(
                    "ai.connection_failed",
                    params={"error": f"timeout after {limit}s"},
                    detail=f"{self.name} timeout",
                    status_code=504,
                ) from exc
            except ValueError as exc:  # malformed JSON body
                raise AIProviderError(
                    "ai.connection_failed",
                    params={"error": "invalid JSON response"},
                    detail=f"{self.name} invalid JSON",
                ) from exc

        raise AIProviderError(
            "ai.connection_failed",
            params={"error": str(last_exc) if last_exc else "connection failed"},
            detail=f"{self.name} unreachable",
        )

    @staticmethod
    def _error_summary(response: httpx.Response) -> str:
        """Squeeze a vendor error body into one short, credential-free line."""
        try:
            body = response.json()
        except ValueError:
            return f"HTTP {response.status_code}: {response.text[:180]}"
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                return f"HTTP {response.status_code}: {err.get('message') or err.get('type')}"
            if isinstance(err, str):
                return f"HTTP {response.status_code}: {err[:180]}"
            if body.get("message"):
                return f"HTTP {response.status_code}: {str(body['message'])[:180]}"
        return f"HTTP {response.status_code}"

    # ------------------------------------------------------------------ #
    # Abstract API
    # ------------------------------------------------------------------ #
    @abstractmethod
    def list_models(self) -> list[str]:
        """Live model catalogue from the vendor."""

    @abstractmethod
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
        """Single-turn completion over a full message history."""

    @abstractmethod
    def embed(self, texts: list[str], *, model: str | None = None) -> EmbeddingResult:
        """Vector embeddings for one or more texts."""

    # ------------------------------------------------------------------ #
    # Health
    # ------------------------------------------------------------------ #
    def test_connection(self) -> dict[str, Any]:
        """
        Cheap liveness probe: list models and measure round-trip latency.

        Never raises — the caller renders the result in a status panel, and a
        dead provider must not take the settings screen down with it.
        """
        started = time.perf_counter()
        if not self.is_configured():
            return {
                "ok": False,
                "latency_ms": 0,
                "models": [],
                "error": "not configured",
            }
        try:
            models = self.list_models()
            return {
                "ok": True,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "models": models,
                "error": None,
            }
        except Exception as exc:
            return {
                "ok": False,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "models": [],
                "error": short_error(exc),
            }


def short_error(exc: Exception) -> str:
    """One-line, credential-free description of a failure."""
    if isinstance(exc, AIProviderError):
        reason = exc.params.get("error") or exc.detail or exc.message_key
        return str(reason)[:300]
    return f"{type(exc).__name__}: {exc}"[:300]


__all__ = [
    "BaseProvider",
    "ChatMessage",
    "ChatResult",
    "EmbeddingResult",
    "MIN_SAFE_MAX_TOKENS",
    "short_error",
]
