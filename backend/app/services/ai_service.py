"""
AI application service: providers, copilot, usage accounting and the terminal.

This module is the only thing the API layer talks to.  It owns persistence
(conversations, messages, suggestions, terminal commands), auditing, and the
credential-handling policy:

* An API key submitted through the UI is written to the project ``.env`` file
  and exported to the running process.  The database records only
  ``has_api_key=True`` and the *name* of the variable holding it.
* No function in this module returns, logs or audits a key value.  Displays get
  :func:`app.core.security.mask_secret` output and nothing else.
"""

from __future__ import annotations

import os
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai import terminal_guard
from app.ai.agents import get_agent
from app.ai.agents.base_agent import AgentResult
from app.ai.agents.collection_risk_agent import CollectionRiskAgent
from app.ai.agents.data_analyst_agent import DataAnalystAgent
from app.ai.agents.inventory_agent import InventoryAgent
from app.ai.agents.orchestrator import OrchestratorAgent
from app.ai.agents.sales_agent import SalesAgent
from app.ai.providers import api_key_for, build_provider, normalize_provider
from app.ai.router import ai_router, budget_state
from app.core.config import PROJECT_ROOT, settings
from app.core.enums import (
    AI_PERMISSION_ORDER,
    AIAgentKind,
    AIPermissionLevel,
    AuditAction,
)
from app.core.exceptions import (
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.core.i18n import normalize_language, t
from app.core.logging_config import get_logger
from app.core.security import mask_secret
from app.core.utils import D, dumps, loads, money
from app.models.ai import (
    AIConversation,
    AIMessage,
    AIProviderConfig,
    AIRequest,
    AISuggestion,
    AITerminalCommand,
    AITerminalSession,
    AIUsageDaily,
)
from app.models.auth import User
from app.models.base import utcnow
from app.services import audit_service, auth_service

log = get_logger("app.ai.service")

#: Environment variable that holds each provider's credential.
API_KEY_REFS: dict[str, str] = {
    "LMSTUDIO": "",  # local server, no credential
    "NVIDIA": "VS_NVIDIA_API_KEY",
    "CLAUDE": "VS_CLAUDE_API_KEY",
}

#: Creating a terminal session above this tier is an administrator decision.
SELF_SERVICE_MAX_LEVEL = str(AIPermissionLevel.RUN_TESTS)


def _audit_fields(
    user: User | None, audit_context: dict[str, Any] | None, **extra: Any
) -> dict[str, Any]:
    """
    Merge caller-supplied audit context with the actor's identity.

    The request context (IP, user agent, role) wins where it overlaps, so a
    single dictionary is passed to :func:`audit_service.record` and duplicate
    keyword arguments are impossible.
    """
    fields: dict[str, Any] = {}
    if user is not None:
        fields["user_id"] = user.id
        fields["username"] = user.username
    fields.update({k: v for k, v in extra.items() if v is not None})
    fields.update(audit_context or {})
    return fields


# ===========================================================================
# Provider configuration
# ===========================================================================
def _get_config(db: Session, provider: str) -> AIProviderConfig:
    key = normalize_provider(provider)
    config = db.execute(
        select(AIProviderConfig).where(AIProviderConfig.provider == key)
    ).scalar_one_or_none()
    if config is None:
        raise NotFoundError("ai.no_provider", params={"provider": provider})
    return config


def _status_row(config: AIProviderConfig) -> dict[str, Any]:
    provider = normalize_provider(config.provider)
    key = api_key_for(provider)
    needs_key = bool(API_KEY_REFS.get(provider))
    return {
        "provider": provider,
        "display_name": config.display_name,
        "enabled": bool(config.is_enabled),
        # Boolean only — the UI must never receive the credential itself.
        "configured": (bool(key) if needs_key else True) and bool(config.base_url),
        "healthy": bool(config.is_healthy),
        "base_url": config.base_url,
        "model": config.default_model,
        "masked_key": mask_secret(key) if needs_key else "",
        "latency_ms": config.last_latency_ms,
        "avg_latency_ms": round(float(config.avg_latency_ms or 0.0), 2),
        "error_rate": config.error_rate,
        "requests": int(config.request_count or 0),
        "errors": int(config.error_count or 0),
        "last_ok_at": config.last_ok_at,
        "last_error_at": config.last_error_at,
        "last_error": config.last_error,
        "failover_priority": int(config.failover_priority or 100),
        "supports_vision": bool(config.supports_vision),
        "supports_embeddings": bool(config.supports_embeddings),
        "input_cost_per_1k": money(config.input_cost_per_1k),
        "output_cost_per_1k": money(config.output_cost_per_1k),
        "task_model_map": loads(config.task_model_map, {}) or {},
    }


def provider_status(db: Session) -> list[dict[str, Any]]:
    """Per-provider operational status, in failover order."""
    return [_status_row(config) for config in ai_router.configs(db)]


def test_provider(db: Session, provider: str, *, lang: str = "tr") -> dict[str, Any]:
    """
    Probe a provider for real and write the result back onto its health fields.

    Uses the model catalogue rather than a completion: it exercises exactly the
    same endpoint, authentication and network path, without spending tokens.
    """
    config = _get_config(db, provider)
    client = build_provider(config, provider=config.provider)
    result = client.test_connection()

    if result["ok"]:
        config.last_ok_at = utcnow()
        config.last_latency_ms = int(result["latency_ms"])
        config.is_healthy = True
        config.last_error = None
        ai_router.clear_cooldown(config.provider)
        message = t("ai.connection_ok", lang, latency=result["latency_ms"])
    else:
        config.last_error_at = utcnow()
        config.last_error = str(result.get("error") or "")[:512] or None
        config.is_healthy = False
        config.error_count = (config.error_count or 0) + 1
        ai_router.cool_down(config.provider)
        message = t("ai.connection_failed", lang, error=result.get("error") or "")

    db.commit()
    return {
        "provider": normalize_provider(config.provider),
        "ok": bool(result["ok"]),
        "latency_ms": int(result["latency_ms"]),
        "models": list(result.get("models") or []),
        "error": result.get("error"),
        "message": message,
    }


def list_models(db: Session, provider: str) -> dict[str, Any]:
    """Live model catalogue straight from the provider."""
    config = _get_config(db, provider)
    client = build_provider(config, provider=config.provider)
    models = client.list_models()
    return {
        "provider": normalize_provider(config.provider),
        "models": models,
        "default_model": config.default_model,
        "count": len(models),
    }


# ---------------------------------------------------------------------------
# Credential storage
# ---------------------------------------------------------------------------
def _env_file() -> Path:
    return Path(PROJECT_ROOT) / ".env"


def _ensure_env_ignored() -> None:
    """Make sure ``.env`` is git-ignored before a secret is written into it."""
    gitignore = Path(PROJECT_ROOT) / ".gitignore"
    try:
        existing = gitignore.read_text(encoding="utf-8") if gitignore.is_file() else ""
        if not any(line.strip() in (".env", "/.env", "*.env") for line in existing.splitlines()):
            with gitignore.open("a", encoding="utf-8") as handle:
                if existing and not existing.endswith("\n"):
                    handle.write("\n")
                handle.write(".env\n")
    except OSError:
        log.warning("Could not update .gitignore; refusing to leave .env unprotected")
        raise


def write_env_secret(name: str, value: str) -> None:
    """
    Persist a credential to the project ``.env`` and export it to this process.

    The file is created if missing, existing assignments are replaced in place,
    and the value is never echoed anywhere — not to the log, not to the audit
    trail, not to the response.
    """
    if not name:
        raise ValidationError("ai.no_api_key")
    _ensure_env_ignored()

    path = _env_file()
    lines: list[str] = []
    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()

    replaced = False
    for index, line in enumerate(lines):
        if line.strip().startswith(f"{name}="):
            lines[index] = f"{name}={value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{name}={value}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:  # tighten permissions where the platform supports it
        os.chmod(path, 0o600)
    except (OSError, NotImplementedError):  # pragma: no cover - Windows/ACL
        pass

    os.environ[name] = value
    # Settings are cached for the process lifetime; refresh the field in place
    # so the new key takes effect without a restart.
    attribute = name.removeprefix("VS_").lower()
    if hasattr(settings, attribute):
        setattr(settings, attribute, value)
    log.info("Stored credential in .env as %s (value not logged)", name)


def update_provider(
    db: Session,
    provider: str,
    payload: dict[str, Any],
    *,
    user_id: int | None = None,
    audit_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Apply operator changes to one provider.

    A supplied ``api_key`` goes to the ``.env`` file; the database records only
    that a key now exists and which variable holds it.  The audit entry captures
    *that* a key changed, never the value.
    """
    config = _get_config(db, provider)
    key = normalize_provider(config.provider)

    before = {
        "is_enabled": config.is_enabled,
        "base_url": config.base_url,
        "default_model": config.default_model,
        "timeout_seconds": config.timeout_seconds,
        "max_tokens": config.max_tokens,
        "temperature": config.temperature,
        "failover_priority": config.failover_priority,
        "has_api_key": config.has_api_key,
        "input_cost_per_1k": str(config.input_cost_per_1k),
        "output_cost_per_1k": str(config.output_cost_per_1k),
    }

    simple_fields = (
        "is_enabled", "base_url", "default_model", "timeout_seconds",
        "max_tokens", "temperature", "failover_priority",
        "input_cost_per_1k", "output_cost_per_1k",
    )
    for field in simple_fields:
        if payload.get(field) is not None:
            value = payload[field]
            if field in ("input_cost_per_1k", "output_cost_per_1k"):
                value = money(value)
            setattr(config, field, value)

    if payload.get("task_model_map") is not None:
        config.task_model_map = dumps(payload["task_model_map"])

    api_key = (payload.get("api_key") or "").strip()
    key_changed = False
    if api_key:
        ref = API_KEY_REFS.get(key)
        if not ref:
            raise ValidationError("ai.no_api_key", params={"provider": key})
        write_env_secret(ref, api_key)
        config.api_key_ref = ref
        config.has_api_key = True
        key_changed = True
        # A new credential deserves a fresh chance even if the old one failed.
        config.is_healthy = True
        config.last_error = None
        ai_router.clear_cooldown(key)

    after = {
        "is_enabled": config.is_enabled,
        "base_url": config.base_url,
        "default_model": config.default_model,
        "timeout_seconds": config.timeout_seconds,
        "max_tokens": config.max_tokens,
        "temperature": config.temperature,
        "failover_priority": config.failover_priority,
        "has_api_key": config.has_api_key,
        "input_cost_per_1k": str(config.input_cost_per_1k),
        "output_cost_per_1k": str(config.output_cost_per_1k),
        "api_key_changed": key_changed,
    }

    audit_service.record(
        db,
        AuditAction.SETTING_CHANGE,
        entity_type="AIProviderConfig",
        entity_id=config.id,
        entity_label=key,
        summary=f"AI provider {key} updated" + (" (credential rotated)" if key_changed else ""),
        old_values=before,
        new_values=after,
        **_audit_fields(None, audit_context, user_id=user_id),
    )
    db.commit()
    return _status_row(config)


# ===========================================================================
# Usage & budget
# ===========================================================================
def budget_status(db: Session) -> dict[str, Any]:
    """Monthly spend against the configured ceiling."""
    return budget_state(db)


_GROUPINGS = ("day", "provider", "model", "user", "agent")


def usage_summary(
    db: Session,
    *,
    start: date | None = None,
    end: date | None = None,
    group_by: str = "day",
    user_ids: list[int] | None = None,
) -> dict[str, Any]:
    """
    Token and cost totals over a period, bucketed by day/provider/model/user/agent.

    Reads the daily rollup rather than the raw request log so the screens stay
    fast as the request table grows.
    """
    grouping = (group_by or "day").lower()
    if grouping not in _GROUPINGS:
        raise ValidationError("error.validation_error", params={"field": "group_by"})

    period_end = end or date.today()
    period_start = start or period_end.replace(day=1)

    column = {
        "day": AIUsageDaily.usage_date,
        "provider": AIUsageDaily.provider,
        "model": AIUsageDaily.model,
        "user": AIUsageDaily.user_id,
        "agent": AIUsageDaily.agent_kind,
    }[grouping]

    conditions = [
        AIUsageDaily.usage_date >= period_start,
        AIUsageDaily.usage_date <= period_end,
    ]
    if user_ids:
        conditions.append(AIUsageDaily.user_id.in_(user_ids))

    rows = db.execute(
        select(
            column.label("bucket"),
            func.sum(AIUsageDaily.request_count),
            func.sum(AIUsageDaily.error_count),
            func.sum(AIUsageDaily.input_tokens),
            func.sum(AIUsageDaily.output_tokens),
            func.sum(AIUsageDaily.total_tokens),
            func.sum(AIUsageDaily.estimated_cost),
            func.avg(AIUsageDaily.avg_latency_ms),
        )
        .where(*conditions)
        .group_by(column)
        .order_by(column)
    ).all()

    usernames: dict[int, str] = {}
    if grouping == "user":
        ids = [int(r[0]) for r in rows if r[0]]
        if ids:
            usernames = {
                int(uid): name
                for uid, name in db.execute(
                    select(User.id, User.full_name).where(User.id.in_(ids))
                ).all()
            }

    result_rows: list[dict[str, Any]] = []
    for bucket, requests, errors, tokens_in, tokens_out, tokens, cost, latency in rows:
        label = str(bucket) if bucket is not None else "-"
        if grouping == "user":
            label = usernames.get(int(bucket or 0), f"#{bucket}" if bucket else "system")
        result_rows.append(
            {
                "key": str(bucket) if bucket is not None else "-",
                "label": label,
                "bucket_date": bucket if grouping == "day" and isinstance(bucket, date) else None,
                "requests": int(requests or 0),
                "errors": int(errors or 0),
                "input_tokens": int(tokens_in or 0),
                "output_tokens": int(tokens_out or 0),
                "total_tokens": int(tokens or 0),
                "cost": money(cost or 0),
                "avg_latency_ms": round(float(latency or 0.0), 2),
            }
        )

    return {
        "start": period_start,
        "end": period_end,
        "group_by": grouping,
        "rows": result_rows,
        "total_requests": sum(r["requests"] for r in result_rows),
        "total_errors": sum(r["errors"] for r in result_rows),
        "total_tokens": sum(r["total_tokens"] for r in result_rows),
        "total_cost": money(sum((D(r["cost"]) for r in result_rows), Decimal("0"))),
        "currency": "USD",
    }


def health(db: Session) -> dict[str, Any]:
    """Subsystem health for the status panel — never raises."""
    providers = provider_status(db)
    active = ai_router.pick_provider(db=db)
    return {
        "healthy": any(p["enabled"] and p["configured"] and p["healthy"] for p in providers),
        "active_provider": active,
        "providers": providers,
        "budget": budget_state(db),
        "checked_at": datetime.now(),
    }


# ===========================================================================
# Conversations
# ===========================================================================
def list_conversations(
    db: Session,
    *,
    user_id: int | None = None,
    include_archived: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[AIConversation], int]:
    """A user's conversations, newest first, with the total for pagination."""
    conditions = []
    if user_id is not None:
        conditions.append(AIConversation.user_id == user_id)
    if not include_archived:
        conditions.append(AIConversation.is_archived.is_(False))

    total = db.execute(
        select(func.count(AIConversation.id)).where(*conditions)
    ).scalar_one()
    rows = list(
        db.execute(
            select(AIConversation)
            .where(*conditions)
            .order_by(AIConversation.updated_at.desc(), AIConversation.id.desc())
            .offset(offset)
            .limit(limit)
        ).scalars()
    )
    return rows, int(total or 0)


def get_conversation(
    db: Session, conversation_id: int, *, user_id: int | None = None, unrestricted: bool = False
) -> AIConversation:
    """One conversation with its messages, enforcing ownership."""
    conversation = db.get(AIConversation, conversation_id)
    if conversation is None:
        raise NotFoundError("error.not_found", params={"id": conversation_id})
    if not unrestricted and user_id is not None and conversation.user_id != user_id:
        raise PermissionDeniedError("auth.permission_denied")
    return conversation


def create_conversation(
    db: Session,
    *,
    user_id: int | None,
    title: str = "",
    agent_kind: str = str(AIAgentKind.ORCHESTRATOR),
    language: str = "tr",
) -> AIConversation:
    conversation = AIConversation(
        user_id=user_id,
        title=(title or "")[:255],
        agent_kind=str(agent_kind),
        language=normalize_language(language),
    )
    db.add(conversation)
    db.flush()
    return conversation


def delete_conversation(
    db: Session,
    conversation_id: int,
    *,
    user_id: int | None = None,
    unrestricted: bool = False,
) -> None:
    conversation = get_conversation(
        db, conversation_id, user_id=user_id, unrestricted=unrestricted
    )
    db.delete(conversation)
    db.commit()


def _append_message(
    db: Session,
    conversation: AIConversation,
    *,
    role: str,
    content: str,
    reasoning: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    data_context: dict[str, Any] | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    latency_ms: int = 0,
) -> AIMessage:
    message = AIMessage(
        conversation_id=conversation.id,
        role=role,
        content=content or "",
        reasoning=reasoning,
        provider=provider,
        model=model,
        data_context=dumps(data_context) if data_context else None,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
    )
    db.add(message)
    conversation.message_count = (conversation.message_count or 0) + 1
    conversation.total_tokens = (conversation.total_tokens or 0) + input_tokens + output_tokens
    conversation.updated_at = utcnow()
    db.flush()
    return message


# ===========================================================================
# Copilot
# ===========================================================================
def ask(
    db: Session,
    *,
    question: str,
    user: User | None = None,
    conversation_id: int | None = None,
    agent_kind: str | None = None,
    language: str = "tr",
    preferred_provider: str | None = None,
    salesperson_ids: list[int] | None = None,
    unrestricted: bool = True,
    **context: Any,
) -> dict[str, Any]:
    """
    Copilot entry point: route the question, gather real data, persist the turn.

    Always returns a payload.  When no provider answers, ``answer`` is empty and
    ``error_key`` carries the i18n key — the caller still gets the figures the
    agent collected, which is the difference between a degraded feature and a
    broken one.
    """
    lang = normalize_language(language)
    user_id = user.id if user else None

    if conversation_id:
        conversation = get_conversation(
            db, conversation_id, user_id=user_id, unrestricted=unrestricted
        )
    else:
        conversation = create_conversation(
            db,
            user_id=user_id,
            title=question[:120],
            agent_kind=str(agent_kind or AIAgentKind.ORCHESTRATOR),
            language=lang,
        )

    _append_message(db, conversation, role="user", content=question)

    orchestrator = OrchestratorAgent()
    result: AgentResult = orchestrator.run(
        db,
        question=question,
        language=lang,
        user_id=user_id,
        conversation_id=conversation.id,
        agent_kind=agent_kind,
        salesperson_ids=salesperson_ids,
        **{k: v for k, v in context.items() if v is not None},
    )

    message = _append_message(
        db,
        conversation,
        role="assistant",
        content=result.answer,
        reasoning=result.reasoning,
        provider=result.provider,
        model=result.model,
        data_context=result.data_context,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        latency_ms=result.latency_ms,
    )
    conversation.agent_kind = result.agent_kind
    # Recomputed rather than accumulated: the request log is the source of truth
    # for cost, and it is written on its own transaction.
    conversation.total_cost = _conversation_cost(db, conversation.id)
    db.commit()

    payload = result.to_dict()
    payload.update(
        {
            "conversation_id": conversation.id,
            "message_id": message.id,
            "degraded": bool(result.error_key),
        }
    )
    return payload


def _conversation_cost(db: Session, conversation_id: int) -> Decimal:
    """Total cost booked against this conversation across every attempt."""
    total = db.execute(
        select(func.sum(AIRequest.estimated_cost)).where(
            AIRequest.conversation_id == conversation_id
        )
    ).scalar()
    return money(total or 0)


# ===========================================================================
# Assistant shortcuts (structured suggestion + narrative)
# ===========================================================================
def _persist_suggestion(
    db: Session,
    *,
    kind: str,
    agent_kind: str,
    subject_type: str,
    subject_id: int | None,
    payload: dict[str, Any],
    explanation: str,
    confidence: float,
    provider: str | None,
    model: str | None,
    suggested_for: date | None = None,
) -> AISuggestion:
    suggestion = AISuggestion(
        suggestion_kind=kind,
        agent_kind=agent_kind,
        subject_type=subject_type,
        subject_id=subject_id,
        suggested_for=suggested_for or date.today(),
        payload=dumps(payload),
        explanation=explanation or None,
        confidence=float(confidence or 0.0),
        provider=provider,
        model=model,
    )
    db.add(suggestion)
    db.flush()
    return suggestion


def customer_order_suggestion(
    db: Session,
    *,
    customer_id: int,
    language: str = "tr",
    user: User | None = None,
    on_date: date | None = None,
) -> dict[str, Any]:
    """Order proposal for one customer plus the salesperson-facing narrative."""
    agent = SalesAgent()
    result = agent.run(
        db,
        customer_id=customer_id,
        language=normalize_language(language),
        on_date=on_date,
        user_id=user.id if user else None,
    )
    suggestion = _persist_suggestion(
        db,
        kind="ORDER",
        agent_kind=result.agent_kind,
        subject_type="CUSTOMER",
        subject_id=customer_id,
        payload=result.suggestion or {},
        explanation=result.answer,
        confidence=result.confidence,
        provider=result.provider,
        model=result.model,
        suggested_for=on_date,
    )
    db.commit()
    return _suggestion_payload(result, suggestion, kind="ORDER", subject_id=customer_id)


def van_load_suggestion(
    db: Session,
    *,
    salesperson_id: int,
    vehicle_id: int | None = None,
    language: str = "tr",
    user: User | None = None,
    on_date: date | None = None,
) -> dict[str, Any]:
    """Van-load proposal for one salesperson plus its explanation."""
    agent = InventoryAgent()
    result = agent.run(
        db,
        salesperson_id=salesperson_id,
        vehicle_id=vehicle_id,
        on_date=on_date,
        language=normalize_language(language),
        user_id=user.id if user else None,
    )
    suggestion = _persist_suggestion(
        db,
        kind="VAN_LOAD",
        agent_kind=result.agent_kind,
        subject_type="SALESPERSON",
        subject_id=salesperson_id,
        payload=result.suggestion or {},
        explanation=result.answer,
        confidence=result.confidence,
        provider=result.provider,
        model=result.model,
        suggested_for=on_date,
    )
    db.commit()
    return _suggestion_payload(
        result, suggestion, kind="VAN_LOAD", subject_id=salesperson_id,
        subject_type="SALESPERSON",
    )


def collection_risk(
    db: Session,
    *,
    customer_id: int,
    language: str = "tr",
    user: User | None = None,
) -> dict[str, Any]:
    """Payment-risk assessment for one customer."""
    agent = CollectionRiskAgent()
    result = agent.run(
        db,
        customer_id=customer_id,
        language=normalize_language(language),
        user_id=user.id if user else None,
    )
    suggestion = _persist_suggestion(
        db,
        kind="COLLECTION_RISK",
        agent_kind=result.agent_kind,
        subject_type="CUSTOMER",
        subject_id=customer_id,
        payload=result.suggestion or {},
        explanation=result.answer,
        confidence=result.confidence,
        provider=result.provider,
        model=result.model,
    )
    db.commit()
    return _suggestion_payload(
        result, suggestion, kind="COLLECTION_RISK", subject_id=customer_id
    )


def _suggestion_payload(
    result: AgentResult,
    suggestion: AISuggestion,
    *,
    kind: str,
    subject_id: int | None,
    subject_type: str = "CUSTOMER",
) -> dict[str, Any]:
    return {
        "suggestion_id": suggestion.id,
        "suggestion_kind": kind,
        "agent_kind": result.agent_kind,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "payload": result.suggestion or {},
        "explanation": result.answer,
        "reasoning": result.reasoning,
        "confidence": result.confidence,
        "provider": result.provider,
        "model": result.model,
        "error_key": result.error_key,
        "degraded": bool(result.error_key),
    }


def analyst_query(
    db: Session,
    *,
    question: str,
    sql: str | None = None,
    max_rows: int = 200,
    explain: bool = True,
    language: str = "tr",
    user: User | None = None,
) -> dict[str, Any]:
    """Natural-language (or hand-written) read-only query, executed and explained."""
    agent = DataAnalystAgent()
    result = agent.run(
        db,
        question=question,
        sql=sql,
        max_rows=max_rows,
        explain=explain,
        language=normalize_language(language),
        user_id=user.id if user else None,
    )
    facts = result.data_context
    return {
        "question": question,
        "sql": facts.get("sql"),
        "columns": facts.get("columns") or [],
        "rows": facts.get("rows") or [],
        "row_count": int(facts.get("row_count") or 0),
        "truncated": bool(facts.get("truncated")),
        "answer": result.answer,
        "provider": result.provider,
        "model": result.model,
        "error_key": result.error_key,
    }


def agent_answer(
    db: Session,
    kind: str,
    *,
    language: str = "tr",
    user: User | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run one named agent directly, bypassing orchestration."""
    agent = get_agent(kind)
    result = agent.run(
        db,
        language=normalize_language(language),
        user_id=user.id if user else None,
        **kwargs,
    )
    return result.to_dict()


# ===========================================================================
# Terminal
# ===========================================================================
def create_terminal_session(
    db: Session,
    *,
    user: User,
    title: str = "",
    permission_level: str = str(AIPermissionLevel.READ_ONLY),
    audit_context: dict[str, Any] | None = None,
) -> AITerminalSession:
    """
    Open a terminal session at a given permission tier.

    Anything above ``RUN_TESTS`` — installing packages, git operations, system
    commands — is an administrator's decision, not a self-service one.
    """
    level = str(permission_level or AIPermissionLevel.READ_ONLY).upper()
    if level not in AI_PERMISSION_ORDER:
        raise ValidationError("error.validation_error", params={"field": "permission_level"})
    if terminal_guard.level_rank(level) > terminal_guard.level_rank(
        SELF_SERVICE_MAX_LEVEL
    ) and not auth_service.is_admin(user):
        raise PermissionDeniedError("auth.admin_required", params={"level": level})

    session = AITerminalSession(
        user_id=user.id,
        title=(title or "")[:255],
        permission_level=level,
    )
    db.add(session)
    db.flush()

    audit_service.record(
        db,
        AuditAction.PERMISSION_CHANGE,
        entity_type="AITerminalSession",
        entity_id=session.id,
        entity_label=level,
        summary=f"AI terminal session opened at {level}",
        new_values={"permission_level": level, "title": session.title},
        is_ai_action=True,
        ai_agent_kind=str(AIAgentKind.CODING),
        **_audit_fields(user, audit_context),
    )
    db.commit()
    return session


def list_terminal_sessions(
    db: Session, *, user_id: int | None = None, active_only: bool = False, limit: int = 50
) -> list[AITerminalSession]:
    conditions = []
    if user_id is not None:
        conditions.append(AITerminalSession.user_id == user_id)
    if active_only:
        conditions.append(AITerminalSession.is_active.is_(True))
    return list(
        db.execute(
            select(AITerminalSession)
            .where(*conditions)
            .order_by(AITerminalSession.id.desc())
            .limit(limit)
        ).scalars()
    )


def get_terminal_session(
    db: Session, session_id: int, *, user: User, unrestricted: bool = False
) -> AITerminalSession:
    session = db.get(AITerminalSession, session_id)
    if session is None:
        raise NotFoundError("error.not_found", params={"id": session_id})
    if not unrestricted and session.user_id != user.id and not auth_service.is_admin(user):
        raise PermissionDeniedError("auth.permission_denied")
    return session


def run_command(
    db: Session,
    session: AITerminalSession | int,
    instruction: str,
    *,
    user: User,
    requested_action: str = terminal_guard.ActionType.SHELL,
    target: str | None = None,
    command: str | None = None,
    approve_token: str | None = None,
    lang: str = "tr",
    audit_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Evaluate and (when permitted) execute one terminal action.

    Every outcome — executed, blocked or awaiting approval — is persisted as an
    :class:`AITerminalCommand` and written to the audit log with
    ``is_ai_action=True``.  Nothing runs before the record exists.
    """
    if isinstance(session, int):
        session = get_terminal_session(db, session, user=user)

    decision = terminal_guard.classify(
        requested_action=requested_action, target=target, command=command
    )
    decision = terminal_guard.authorize(decision, session.permission_level)

    record = AITerminalCommand(
        session_id=session.id,
        user_id=user.id,
        instruction=instruction[:4000],
        action_type=decision.action_type,
        target=(decision.resolved_target or decision.target or "")[:1024] or None,
        command=(decision.command if decision.action_type != terminal_guard.ActionType.WRITE_FILE else None),
        required_level=decision.required_level,
        is_allowed=False,
        requires_approval=decision.requires_approval,
        block_reason=decision.block_reason,
    )
    db.add(record)
    db.flush()

    message_key: str | None = None
    output = ""
    exit_code: int | None = None
    token: str | None = None

    if decision.block_reason:
        # A tier problem is a translatable condition; a hard block is a policy
        # statement whose reason *is* the message, so it is returned verbatim.
        if "requires permission level" in decision.block_reason:
            message_key = "ai.permission_required"
        output = decision.block_reason
    elif decision.requires_approval and not terminal_guard.verify_approval_token(
        session.id, decision.action_type, decision.command or "", approve_token
    ):
        # Hand back the token; the user must send it with the same command to run it.
        token = terminal_guard.make_approval_token(
            session.id, decision.action_type, decision.command or ""
        )
        message_key = "ai.approval_required"
        output = t("ai.approval_required", lang)
    else:
        try:
            execution = terminal_guard.execute(decision)
            exit_code = execution.exit_code
            output = execution.output
            record.is_allowed = True
            record.requires_approval = False
            record.executed_at = utcnow()
            record.duration_ms = execution.duration_ms
            if approve_token:
                record.approved_by_id = user.id
                record.approved_at = utcnow()
        except PermissionError as exc:
            record.block_reason = str(exc)[:512]
            message_key = "ai.permission_required"
            output = str(exc)
        except OSError as exc:
            record.block_reason = f"{type(exc).__name__}: {exc}"[:512]
            output = record.block_reason

    record.exit_code = exit_code
    record.output_preview = (output or "")[: terminal_guard.MAX_OUTPUT_CHARS]
    session.command_count = (session.command_count or 0) + 1

    audit_service.record(
        db,
        AuditAction.AI_ACTION,
        entity_type="AITerminalCommand",
        entity_id=record.id,
        entity_label=decision.action_type,
        summary=(
            f"AI terminal {decision.action_type}: "
            f"{'executed' if record.is_allowed else 'blocked/pending'} "
            f"({decision.block_reason or 'ok'})"
        )[:512],
        new_values={
            "action_type": decision.action_type,
            "required_level": decision.required_level,
            "session_level": session.permission_level,
            "target": record.target,
            "command": record.command,
            "allowed": record.is_allowed,
            "requires_approval": record.requires_approval,
            "block_reason": record.block_reason,
            "exit_code": exit_code,
        },
        is_ai_action=True,
        ai_agent_kind=str(AIAgentKind.CODING),
        **_audit_fields(user, audit_context),
    )
    db.commit()

    return {
        "id": record.id,
        "session_id": session.id,
        "action_type": decision.action_type,
        "required_level": decision.required_level,
        "is_allowed": record.is_allowed,
        "requires_approval": bool(record.requires_approval),
        "approve_token": token,
        "block_reason": record.block_reason,
        "command": record.command,
        "target": record.target,
        "exit_code": exit_code,
        "output": output,
        "duration_ms": record.duration_ms,
        "message_key": message_key,
        "created_at": record.created_at,
    }


__all__ = [
    "agent_answer",
    "analyst_query",
    "ask",
    "budget_status",
    "collection_risk",
    "create_conversation",
    "create_terminal_session",
    "customer_order_suggestion",
    "delete_conversation",
    "get_conversation",
    "get_terminal_session",
    "health",
    "list_conversations",
    "list_models",
    "list_terminal_sessions",
    "provider_status",
    "run_command",
    "test_provider",
    "update_provider",
    "usage_summary",
    "van_load_suggestion",
    "write_env_secret",
]
