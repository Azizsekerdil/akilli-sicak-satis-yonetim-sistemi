"""
Provider routing, failover, cost accounting and budget enforcement.

Every model call in the system goes through :class:`AIRouter`.  That is what
makes three otherwise independent guarantees possible:

* **Failover** — providers are tried in a configured order; a dead one is
  marked unhealthy, put in a short cool-down, and skipped by the next request
  instead of stalling it again.
* **Accounting** — *every* attempt (success or failure) becomes an
  :class:`~app.models.ai.AIRequest` row and is rolled up into
  :class:`~app.models.ai.AIUsageDaily`, so token spend can never drift away
  from what actually happened.
* **Budget** — a paid provider is checked against the monthly budget *before*
  the call goes out.  The free local provider is never blocked: running out of
  budget must degrade quality, not availability.

Telemetry is written on its own database session on purpose.  If a caller's
transaction later rolls back, the record of what was sent to a third party —
and what it cost — must survive regardless.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai.base import BaseProvider, ChatMessage, ChatResult, EmbeddingResult, short_error
from app.ai.providers import api_key_for, build_provider, normalize_provider
from app.core.config import settings
from app.core.db import SessionLocal
from app.core.enums import AIProvider, AIRequestStatus, AITaskType
from app.core.exceptions import AIBudgetExceededError, AIProviderError
from app.core.logging_config import get_logger, redact
from app.core.utils import D, dumps, loads, money, month_start
from app.models.ai import AIProviderConfig, AIRequest, AIUsageDaily
from app.models.base import utcnow

log = get_logger("app.ai.router")

#: How long a provider stays skipped after a failure.  Long enough to stop a
#: burst of requests hammering a dead endpoint, short enough that a restarted
#: LM Studio is picked up again within a minute.
HEALTH_COOLDOWN_SECONDS = 60.0

#: Truncation limits for the request log — enough to debug, never a full dump.
_PREVIEW_LIMIT = 1000


@dataclass(slots=True)
class ProviderChoice:
    """A candidate provider with everything the call needs already resolved."""

    provider: str
    model: str
    config: AIProviderConfig | None
    client: BaseProvider
    is_paid: bool
    timeout: int


# ===========================================================================
# Budget
# ===========================================================================
def monthly_spend(db: Session, *, on: date | None = None) -> Decimal:
    """Total estimated cost booked for the calendar month containing *on*."""
    start = month_start(on or date.today())
    total = db.execute(
        select(func.sum(AIUsageDaily.estimated_cost)).where(AIUsageDaily.usage_date >= start)
    ).scalar()
    return money(total or 0)


def budget_state(db: Session) -> dict[str, Any]:
    """Spend versus the configured monthly ceiling, with warn/exceeded flags."""
    spent = monthly_spend(db)
    budget = money(settings.ai_monthly_budget_usd)
    percent = float(spent / budget * 100) if budget > 0 else 0.0
    return {
        "spent_this_month": spent,
        "budget": budget,
        "percent": round(percent, 2),
        "warn": budget > 0 and percent >= float(settings.ai_budget_warn_pct),
        "exceeded": budget > 0 and spent >= budget,
        "currency": "USD",
        "period_start": month_start(date.today()),
    }


def estimate_cost(
    config: AIProviderConfig | None, input_tokens: int, output_tokens: int
) -> Decimal:
    """cost = in/1000 * input_rate + out/1000 * output_rate (always Decimal)."""
    if config is None:
        return money(0)
    thousand = Decimal("1000")
    return money(
        D(input_tokens) / thousand * D(config.input_cost_per_1k)
        + D(output_tokens) / thousand * D(config.output_cost_per_1k)
    )


# ===========================================================================
# Router
# ===========================================================================
class AIRouter:
    """Chooses a provider and a model, then owns the call's whole life cycle."""

    def __init__(self) -> None:
        #: provider -> unix timestamp until which it is skipped.
        self._cooldown: dict[str, float] = {}

    # ------------------------------------------------------------------ #
    # Configuration lookup
    # ------------------------------------------------------------------ #
    def _session(self, db: Session | None) -> tuple[Session, bool]:
        """Return a usable session and whether the caller must close it."""
        if db is not None:
            return db, False
        return SessionLocal(), True

    def configs(self, db: Session | None = None) -> list[AIProviderConfig]:
        """All provider rows, ordered by the configured failover chain."""
        session, owned = self._session(db)
        try:
            rows = list(session.execute(select(AIProviderConfig)).scalars())
        finally:
            if owned:
                session.close()

        chain = [p.upper() for p in settings.failover_chain]

        def sort_key(cfg: AIProviderConfig) -> tuple[int, int, str]:
            provider = normalize_provider(cfg.provider)
            position = chain.index(provider) if provider in chain else len(chain)
            return (position, int(cfg.failover_priority or 100), provider)

        return sorted(rows, key=sort_key)

    def config_for(self, db: Session | None, provider: str) -> AIProviderConfig | None:
        key = normalize_provider(provider)
        session, owned = self._session(db)
        try:
            return session.execute(
                select(AIProviderConfig).where(AIProviderConfig.provider == key)
            ).scalar_one_or_none()
        finally:
            if owned:
                session.close()

    # ------------------------------------------------------------------ #
    # Health
    # ------------------------------------------------------------------ #
    def _in_cooldown(self, provider: str) -> bool:
        until = self._cooldown.get(normalize_provider(provider), 0.0)
        return until > time.time()

    def cool_down(self, provider: str, seconds: float = HEALTH_COOLDOWN_SECONDS) -> None:
        self._cooldown[normalize_provider(provider)] = time.time() + seconds

    def clear_cooldown(self, provider: str | None = None) -> None:
        if provider is None:
            self._cooldown.clear()
        else:
            self._cooldown.pop(normalize_provider(provider), None)

    # ------------------------------------------------------------------ #
    # Selection
    # ------------------------------------------------------------------ #
    def pick_model(
        self,
        provider: str | AIProviderConfig,
        task_type: str = AITaskType.GENERAL,
    ) -> str:
        """
        Best model for *task_type*, from the provider's ``task_model_map``.

        Falls back to the provider's default model so a task type nobody has
        mapped yet still answers rather than erroring.
        """
        config = provider if isinstance(provider, AIProviderConfig) else self.config_for(None, provider)
        if config is None:
            return ""
        mapping = loads(config.task_model_map, {}) or {}
        if isinstance(mapping, dict):
            chosen = mapping.get(str(task_type)) or mapping.get(str(AITaskType.GENERAL))
            if chosen:
                return str(chosen)
        return config.default_model or ""

    def _is_eligible(self, config: AIProviderConfig) -> tuple[bool, str]:
        provider = normalize_provider(config.provider)
        if not config.is_enabled:
            return False, "disabled"
        if provider not in {str(p) for p in AIProvider}:
            return False, "unknown provider"
        needs_key = provider != str(AIProvider.LMSTUDIO)
        if needs_key and not (config.has_api_key and api_key_for(provider)):
            return False, "no api key"
        if not config.base_url:
            return False, "no base url"
        return True, ""

    def candidates(
        self,
        task_type: str = AITaskType.GENERAL,
        *,
        preferred: str | None = None,
        db: Session | None = None,
        include_unhealthy: bool = True,
    ) -> list[ProviderChoice]:
        """
        Ordered list of usable providers.

        Healthy providers come first; providers currently in cool-down are kept
        at the tail rather than dropped, so a total outage still gets one last
        try instead of an immediate hard failure.
        """
        healthy: list[ProviderChoice] = []
        degraded: list[ProviderChoice] = []
        preferred_key = normalize_provider(preferred) if preferred else ""

        for config in self.configs(db):
            ok, reason = self._is_eligible(config)
            if not ok:
                log.debug("provider %s skipped: %s", config.provider, reason)
                continue
            provider = normalize_provider(config.provider)
            model = self.pick_model(config, task_type)
            if not model:
                continue
            try:
                client = build_provider(config, provider=provider)
            except KeyError:
                continue
            choice = ProviderChoice(
                provider=provider,
                model=model,
                config=config,
                client=client,
                is_paid=(
                    D(config.input_cost_per_1k) > 0 or D(config.output_cost_per_1k) > 0
                ),
                timeout=int(config.timeout_seconds or 120),
            )
            if self._in_cooldown(provider) or not config.is_healthy:
                degraded.append(choice)
            else:
                healthy.append(choice)

        ordered = healthy + (degraded if include_unhealthy else [])
        if preferred_key:
            ordered.sort(key=lambda c: 0 if c.provider == preferred_key else 1)
        return ordered

    def pick_provider(
        self,
        task_type: str = AITaskType.GENERAL,
        *,
        preferred: str | None = None,
        db: Session | None = None,
    ) -> str | None:
        """Name of the provider that would answer *task_type* right now."""
        options = self.candidates(task_type, preferred=preferred, db=db)
        return options[0].provider if options else None

    # ------------------------------------------------------------------ #
    # Chat
    # ------------------------------------------------------------------ #
    def chat(
        self,
        db: Session,
        messages: list[ChatMessage],
        *,
        task_type: str = AITaskType.GENERAL,
        agent_kind: str | None = None,
        user_id: int | None = None,
        conversation_id: int | None = None,
        preferred_provider: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout: int | None = None,
        json_schema: dict[str, Any] | None = None,
        stream: bool = False,
    ) -> ChatResult:
        """
        Send *messages* to the first provider that answers.

        Raises :class:`AIProviderError` (``ai.all_failed``) only after every
        candidate has been tried, or :class:`AIBudgetExceededError` when the
        only remaining candidates are paid ones and the budget is spent.
        """
        options = self.candidates(task_type, preferred=preferred_provider, db=db)
        if not options:
            raise AIProviderError("ai.no_provider")

        tried: list[str] = []
        errors: list[str] = []
        budget_blocked = False
        budget: dict[str, Any] | None = None

        for index, choice in enumerate(options):
            if choice.is_paid:
                if budget is None:
                    budget = budget_state(db)
                if budget["exceeded"]:
                    budget_blocked = True
                    self._log_attempt(
                        provider=choice.provider,
                        model=choice.model,
                        task_type=task_type,
                        agent_kind=agent_kind,
                        user_id=user_id,
                        conversation_id=conversation_id,
                        status=AIRequestStatus.BUDGET_EXCEEDED,
                        attempt=index + 1,
                        failover_from=",".join(tried) or None,
                        messages=messages,
                        result=None,
                        error=(
                            f"monthly budget exhausted "
                            f"({budget['spent_this_month']}/{budget['budget']} USD)"
                        ),
                        latency_ms=0,
                    )
                    tried.append(choice.provider)
                    continue

            started = time.perf_counter()
            try:
                result = choice.client.chat(
                    messages,
                    model=model if (model and index == 0) else choice.model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    timeout=timeout or choice.timeout,
                    json_schema=json_schema,
                    stream=stream,
                )
            except Exception as exc:
                latency_ms = int((time.perf_counter() - started) * 1000)
                reason = short_error(exc)
                errors.append(f"{choice.provider}: {reason}")
                self.cool_down(choice.provider)
                self._log_attempt(
                    provider=choice.provider,
                    model=choice.model,
                    task_type=task_type,
                    agent_kind=agent_kind,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    status=_status_for(exc),
                    attempt=index + 1,
                    failover_from=",".join(tried) or None,
                    messages=messages,
                    result=None,
                    error=reason,
                    latency_ms=latency_ms,
                )
                tried.append(choice.provider)
                log.warning("AI provider %s failed: %s", choice.provider, reason)
                continue

            self.clear_cooldown(choice.provider)
            self._log_attempt(
                provider=choice.provider,
                model=result.model or choice.model,
                task_type=task_type,
                agent_kind=agent_kind,
                user_id=user_id,
                conversation_id=conversation_id,
                status=AIRequestStatus.SUCCESS,
                attempt=index + 1,
                failover_from=",".join(tried) or None,
                messages=messages,
                result=result,
                error=None,
                latency_ms=result.latency_ms,
            )
            return result

        if budget_blocked and not errors:
            state = budget or budget_state(db)
            raise AIBudgetExceededError(
                "ai.budget_exceeded",
                params={
                    "spent": str(state["spent_this_month"]),
                    "budget": str(state["budget"]),
                },
            )
        raise AIProviderError("ai.all_failed", detail="; ".join(errors)[:500] or None)

    # ------------------------------------------------------------------ #
    # Embeddings
    # ------------------------------------------------------------------ #
    def embed(
        self,
        db: Session,
        texts: list[str],
        *,
        preferred_provider: str | None = None,
        user_id: int | None = None,
    ) -> EmbeddingResult:
        """Vector embeddings with the same failover behaviour as :meth:`chat`."""
        options = self.candidates(
            AITaskType.EMBEDDING, preferred=preferred_provider, db=db
        )
        options = [o for o in options if getattr(o.config, "supports_embeddings", False)]
        if not options:
            raise AIProviderError("ai.no_provider")

        errors: list[str] = []
        tried: list[str] = []
        for index, choice in enumerate(options):
            started = time.perf_counter()
            try:
                result = choice.client.embed(texts, model=choice.model)
            except Exception as exc:
                errors.append(f"{choice.provider}: {short_error(exc)}")
                self.cool_down(choice.provider)
                tried.append(choice.provider)
                continue
            self._log_attempt(
                provider=choice.provider,
                model=result.model or choice.model,
                task_type=AITaskType.EMBEDDING,
                agent_kind=None,
                user_id=user_id,
                conversation_id=None,
                status=AIRequestStatus.SUCCESS,
                attempt=index + 1,
                failover_from=",".join(tried) or None,
                messages=[],
                result=ChatResult(
                    content="",
                    model=result.model,
                    provider=choice.provider,
                    input_tokens=result.tokens,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                ),
                error=None,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
            return result
        raise AIProviderError("ai.all_failed", detail="; ".join(errors)[:500] or None)

    # ------------------------------------------------------------------ #
    # Telemetry
    # ------------------------------------------------------------------ #
    def _log_attempt(
        self,
        *,
        provider: str,
        model: str,
        task_type: str,
        agent_kind: str | None,
        user_id: int | None,
        conversation_id: int | None,
        status: str,
        attempt: int,
        failover_from: str | None,
        messages: list[ChatMessage],
        result: ChatResult | None,
        error: str | None,
        latency_ms: int,
    ) -> str:
        """
        Persist one attempt and roll it into the daily usage table.

        Returns the request id.  Never raises: losing a log line must not lose
        the answer the user is waiting for.
        """
        request_id = uuid.uuid4().hex
        if not settings.ai_request_log_enabled:
            return request_id

        session = SessionLocal()
        try:
            config = session.execute(
                select(AIProviderConfig).where(
                    AIProviderConfig.provider == normalize_provider(provider)
                )
            ).scalar_one_or_none()

            cost = estimate_cost(
                config,
                result.input_tokens if result else 0,
                result.output_tokens if result else 0,
            )
            row = AIRequest(
                request_id=request_id,
                provider=normalize_provider(provider),
                model=model or "",
                task_type=str(task_type),
                agent_kind=str(agent_kind) if agent_kind else None,
                user_id=user_id,
                conversation_id=conversation_id,
                status=str(status),
                failover_from=(failover_from or None),
                attempt=attempt,
                prompt_preview=_preview(_last_user_text(messages)),
                response_preview=_preview(result.content if result else None),
                error_message=redact(error)[:512] if error else None,
                input_tokens=result.input_tokens if result else 0,
                output_tokens=result.output_tokens if result else 0,
                reasoning_tokens=result.reasoning_tokens if result else 0,
                total_tokens=result.total_tokens if result else 0,
                estimated_cost=cost,
                latency_ms=int(latency_ms or 0),
            )
            session.add(row)
            self._rollup(session, row, agent_kind)
            self._update_health(session, config, row)
            session.commit()
        except Exception:
            session.rollback()
            log.exception("Failed to record AI request for %s", provider)
        finally:
            session.close()
        return request_id

    @staticmethod
    def _rollup(session: Session, row: AIRequest, agent_kind: str | None) -> None:
        """Upsert the (day, provider, model, user) bucket the cost screens read."""
        today = date.today()
        bucket = session.execute(
            select(AIUsageDaily).where(
                AIUsageDaily.usage_date == today,
                AIUsageDaily.provider == row.provider,
                AIUsageDaily.model == row.model,
                AIUsageDaily.user_id == (row.user_id or 0),
            )
        ).scalar_one_or_none()
        if bucket is None:
            bucket = AIUsageDaily(
                usage_date=today,
                provider=row.provider,
                model=row.model,
                user_id=row.user_id or 0,
                agent_kind=str(agent_kind) if agent_kind else None,
            )
            session.add(bucket)
            session.flush()

        previous = bucket.request_count or 0
        bucket.request_count = previous + 1
        if row.status != str(AIRequestStatus.SUCCESS):
            bucket.error_count = (bucket.error_count or 0) + 1
        bucket.input_tokens = (bucket.input_tokens or 0) + row.input_tokens
        bucket.output_tokens = (bucket.output_tokens or 0) + row.output_tokens
        bucket.total_tokens = (bucket.total_tokens or 0) + row.total_tokens
        bucket.estimated_cost = money(D(bucket.estimated_cost) + D(row.estimated_cost))
        bucket.avg_latency_ms = round(
            ((bucket.avg_latency_ms or 0.0) * previous + row.latency_ms)
            / max(1, bucket.request_count),
            2,
        )
        if agent_kind and not bucket.agent_kind:
            bucket.agent_kind = str(agent_kind)

    @staticmethod
    def _update_health(
        session: Session, config: AIProviderConfig | None, row: AIRequest
    ) -> None:
        """Keep the provider row's live health honest after every attempt."""
        if config is None:
            return
        previous = config.request_count or 0
        config.request_count = previous + 1
        if row.status == str(AIRequestStatus.SUCCESS):
            config.last_ok_at = utcnow()
            config.last_latency_ms = row.latency_ms
            config.avg_latency_ms = round(
                ((config.avg_latency_ms or 0.0) * previous + row.latency_ms)
                / max(1, config.request_count),
                2,
            )
            config.is_healthy = True
            config.last_error = None
        elif row.status != str(AIRequestStatus.BUDGET_EXCEEDED):
            config.error_count = (config.error_count or 0) + 1
            config.last_error_at = utcnow()
            config.last_error = (row.error_message or "")[:512] or None
            config.is_healthy = False


def _status_for(exc: Exception) -> str:
    """Map a provider exception onto the request-status vocabulary."""
    if isinstance(exc, AIProviderError):
        if exc.status_code == 504:
            return str(AIRequestStatus.TIMEOUT)
        if exc.status_code == 429:
            return str(AIRequestStatus.RATE_LIMITED)
    return str(AIRequestStatus.FAILED)


def _last_user_text(messages: list[ChatMessage]) -> str | None:
    for message in reversed(messages):
        if message.role == "user":
            return message.content
    return messages[-1].content if messages else None


def _preview(text: str | None) -> str | None:
    """Redacted, length-capped copy of a prompt or answer for the request log."""
    if not text:
        return None
    return redact(text)[:_PREVIEW_LIMIT]


def system_message(text: str) -> ChatMessage:
    return ChatMessage(role="system", content=text)


def user_message(text: str, images: list[str] | None = None) -> ChatMessage:
    return ChatMessage(role="user", content=text, images=images)


def facts_block(payload: Any) -> str:
    """Render real figures as compact JSON for inclusion in a prompt."""
    return dumps(payload, indent=2)


#: Process-wide singleton — the cool-down map is deliberately shared.
ai_router = AIRouter()

__all__ = [
    "AIRouter",
    "ProviderChoice",
    "ai_router",
    "budget_state",
    "estimate_cost",
    "facts_block",
    "monthly_spend",
    "system_message",
    "user_message",
]
