"""
Shared agent scaffolding.

Every agent follows the same three-step shape:

1. **Gather** real figures from the database (never from the model).
2. **Narrate** them by handing the model a system prompt, the question, and a
   ``DATA`` block containing exactly those figures.
3. **Degrade** gracefully — if no provider answers, the caller still receives
   the data and a machine-readable error key instead of an exception page.

The system prompts are bilingual by construction: ``SYSTEM_PROMPT_TR`` and
``SYSTEM_PROMPT_EN`` are mandatory class attributes and the language of the
request decides which one is sent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.ai.base import ChatMessage, ChatResult
from app.ai.router import ai_router, facts_block
from app.core.enums import AIAgentKind, AITaskType
from app.core.exceptions import AIBudgetExceededError, AIProviderError
from app.core.i18n import normalize_language
from app.core.logging_config import get_logger

log = get_logger("app.ai.agent")

#: Appended to every system prompt.  These are the rules that make the output
#: auditable: no invented figures, no silent gap-filling.
GROUNDING_RULES_TR = """
KURALLAR:
- Yalnızca sana verilen DATA bloğundaki sayıları kullan. Kendi kafandan rakam üretme.
- Bir bilgi DATA içinde yoksa "veri yok" de; tahmin yürütme.
- Her önerini DATA'daki somut bir rakama dayandır ve o rakamı cümlede belirt.
- Para birimi TRY'dir; tutarları binlik ayraçla ve iki ondalıkla yaz.
- Kısa, iş odaklı ve uygulanabilir yaz. En fazla 6 madde.
- Cevabını Türkçe ver.
""".strip()

GROUNDING_RULES_EN = """
RULES:
- Use only the numbers given in the DATA block. Never invent figures.
- If something is not in DATA, say "no data"; do not guess.
- Ground every recommendation in a concrete figure from DATA and quote it.
- Amounts are in TRY; write them with thousands separators and two decimals.
- Be short, commercial and actionable. At most 6 bullet points.
- Answer in English.
""".strip()


@dataclass(slots=True)
class AgentResult:
    """What an agent hands back: the data, the narrative, and the provenance."""

    agent_kind: str
    answer: str = ""
    reasoning: str | None = None
    provider: str | None = None
    model: str | None = None
    data_context: dict[str, Any] = field(default_factory=dict)
    suggestion: dict[str, Any] | None = None
    confidence: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    #: i18n key set when the narrative could not be produced (data is still valid).
    error_key: str | None = None

    @property
    def narrated(self) -> bool:
        return bool(self.answer.strip())

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_kind": self.agent_kind,
            "answer": self.answer,
            "reasoning": self.reasoning,
            "provider": self.provider,
            "model": self.model,
            "data_context": self.data_context,
            "suggestion": self.suggestion,
            "confidence": self.confidence,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_ms": self.latency_ms,
            "error_key": self.error_key,
        }


class BaseAgent:
    """Base class for every business agent."""

    kind: str = str(AIAgentKind.ORCHESTRATOR)
    task_type: str = str(AITaskType.ANALYSIS)
    max_tokens: int = 1400
    temperature: float = 0.2

    SYSTEM_PROMPT_TR: str = ""
    SYSTEM_PROMPT_EN: str = ""

    # ------------------------------------------------------------------ #
    # Prompt assembly
    # ------------------------------------------------------------------ #
    def system_prompt(self, language: str) -> str:
        lang = normalize_language(language)
        if lang == "en":
            return f"{self.SYSTEM_PROMPT_EN.strip()}\n\n{GROUNDING_RULES_EN}"
        return f"{self.SYSTEM_PROMPT_TR.strip()}\n\n{GROUNDING_RULES_TR}"

    def compose(
        self,
        *,
        language: str,
        question: str,
        facts: Any,
        history: list[ChatMessage] | None = None,
    ) -> list[ChatMessage]:
        """System prompt + prior turns + (question, DATA) as one user turn."""
        messages: list[ChatMessage] = [
            ChatMessage(role="system", content=self.system_prompt(language))
        ]
        messages.extend(history or [])
        label = "SORU" if normalize_language(language) != "en" else "QUESTION"
        messages.append(
            ChatMessage(
                role="user",
                content=f"{label}:\n{question}\n\nDATA:\n{facts_block(facts)}",
            )
        )
        return messages

    # ------------------------------------------------------------------ #
    # Model call
    # ------------------------------------------------------------------ #
    def narrate(
        self,
        db: Any,
        *,
        question: str,
        facts: Any,
        language: str = "tr",
        user_id: int | None = None,
        conversation_id: int | None = None,
        preferred_provider: str | None = None,
        history: list[ChatMessage] | None = None,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int | None = None,
    ) -> tuple[ChatResult | None, str | None]:
        """
        Ask the router for a narrative.

        Returns ``(result, error_key)`` — exactly one of the two is set.  The
        agent never raises on provider failure so a caller that only needs the
        underlying figures still gets them.
        """
        messages = self.compose(
            language=language, question=question, facts=facts, history=history
        )
        try:
            result = ai_router.chat(
                db,
                messages,
                task_type=self.task_type,
                agent_kind=self.kind,
                user_id=user_id,
                conversation_id=conversation_id,
                preferred_provider=preferred_provider,
                max_tokens=max_tokens or self.max_tokens,
                temperature=self.temperature,
                json_schema=json_schema,
            )
            return result, None
        except AIBudgetExceededError:
            log.warning("Agent %s blocked by AI budget", self.kind)
            return None, "ai.budget_exceeded"
        except AIProviderError as exc:
            log.warning("Agent %s got no provider answer: %s", self.kind, exc.detail)
            return None, exc.message_key

    def build_result(
        self,
        *,
        data_context: dict[str, Any],
        result: ChatResult | None,
        error_key: str | None,
        suggestion: dict[str, Any] | None = None,
        confidence: float = 0.0,
    ) -> AgentResult:
        return AgentResult(
            agent_kind=self.kind,
            answer=result.content if result else "",
            reasoning=result.reasoning if result else None,
            provider=result.provider if result else None,
            model=result.model if result else None,
            data_context=data_context,
            suggestion=suggestion,
            confidence=confidence,
            input_tokens=result.input_tokens if result else 0,
            output_tokens=result.output_tokens if result else 0,
            latency_ms=result.latency_ms if result else 0,
            error_key=error_key,
        )

    # ------------------------------------------------------------------ #
    # Entry point
    # ------------------------------------------------------------------ #
    def run(self, db: Any, **kwargs: Any) -> AgentResult:  # pragma: no cover - abstract
        raise NotImplementedError


__all__ = [
    "AgentResult",
    "BaseAgent",
    "GROUNDING_RULES_EN",
    "GROUNDING_RULES_TR",
]
