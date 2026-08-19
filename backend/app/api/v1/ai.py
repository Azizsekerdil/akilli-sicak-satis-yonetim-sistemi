"""
AI endpoints: providers, copilot, assistant, analyst SQL, usage and terminal.

Every handler degrades rather than crashes.  When no provider answers, the
response is a structured error carrying an i18n key (``ai.all_failed``,
``ai.budget_exceeded``, …) — and for the assistant endpoints the computed
figures are still returned with ``degraded: true``, because a salesperson
standing in a shop needs the order proposal even when the narrative is missing.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from typing import Any, Iterator

from fastapi import APIRouter, Body, Depends, Path, Query

from app.core.deps import Ctx, Page, get_page, paginated, require, require_any
from app.core.enums import AIPermissionLevel
from app.core.exceptions import AIProviderError, AppError, PermissionDeniedError
from app.core.i18n import t
from app.core.logging_config import get_logger
from app.core.utils import loads, parse_date
from app.models.customer import Customer
from app.models.vehicle import Salesperson
from app.schemas.ai import (
    AskIn,
    AskOut,
    BudgetOut,
    ConversationOut,
    HealthOut,
    MessageOut,
    ModelListOut,
    ProviderStatusOut,
    ProviderUpdateIn,
    SqlQueryIn,
    SqlQueryOut,
    SuggestionOut,
    TerminalCommandIn,
    TerminalCommandOut,
    TerminalSessionIn,
    TerminalSessionOut,
    TestConnectionOut,
    UsageSummaryOut,
)
from app.schemas.common import Message, PagedResponse
from app.services import ai_service

log = get_logger("app.api.ai")

router = APIRouter(prefix="/ai", tags=["ai"])


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
@contextmanager
def ai_guard(operation: str) -> Iterator[None]:
    """
    Turn any unexpected failure into a structured AI error.

    Domain errors already carry an i18n key and pass straight through; anything
    else would otherwise surface as a 500 with a traceback, which is never an
    acceptable answer from an AI panel.
    """
    try:
        yield
    except AppError:
        raise
    except Exception as exc:  # noqa: BLE001 - deliberate boundary
        log.exception("AI endpoint '%s' failed", operation)
        raise AIProviderError(
            "ai.all_failed", detail=f"{type(exc).__name__}: {exc}"[:300]
        ) from exc


def _own_scope(ctx: Ctx) -> list[int] | None:
    """Salesperson ids this caller may see, or ``None`` when unrestricted."""
    if ctx.unrestricted:
        return None
    return ctx.salesperson_ids or []


def _assert_customer_visible(ctx: Ctx, customer_id: int) -> Customer:
    customer = ctx.db.get(Customer, customer_id)
    if customer is None or customer.is_deleted:
        raise AppError("customer.not_found", params={"id": customer_id}, status_code=404)
    scope = _own_scope(ctx)
    if scope is not None and customer.default_salesperson_id not in scope:
        raise PermissionDeniedError("auth.permission_denied")
    return customer


def _assert_salesperson_visible(ctx: Ctx, salesperson_id: int) -> Salesperson:
    salesperson = ctx.db.get(Salesperson, salesperson_id)
    if salesperson is None or salesperson.is_deleted:
        raise AppError("error.not_found", params={"id": salesperson_id}, status_code=404)
    scope = _own_scope(ctx)
    if scope is not None and salesperson_id not in scope:
        raise PermissionDeniedError("auth.permission_denied")
    return salesperson


def _message_out(message: Any) -> MessageOut:
    return MessageOut(
        id=message.id,
        conversation_id=message.conversation_id,
        role=message.role,
        content=message.content,
        reasoning=message.reasoning,
        provider=message.provider,
        model=message.model,
        data_context=loads(message.data_context, None),
        input_tokens=message.input_tokens,
        output_tokens=message.output_tokens,
        latency_ms=message.latency_ms,
        created_at=message.created_at,
    )


def _conversation_out(conversation: Any, *, with_messages: bool = False) -> ConversationOut:
    """Explicit projection — the list view must not drag every message with it."""
    return ConversationOut(
        id=conversation.id,
        user_id=conversation.user_id,
        title=conversation.title,
        agent_kind=conversation.agent_kind,
        language=conversation.language,
        is_archived=conversation.is_archived,
        message_count=conversation.message_count,
        total_tokens=conversation.total_tokens,
        total_cost=conversation.total_cost,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        messages=[_message_out(m) for m in conversation.messages] if with_messages else [],
    )


# ===========================================================================
# Providers
# ===========================================================================
@router.get(
    "/providers",
    response_model=list[ProviderStatusOut],
    summary="AI provider status / Sağlayıcı durumu",
)
def list_providers(ctx: Ctx = Depends(require("ai.providers", "VIEW"))) -> list[ProviderStatusOut]:
    with ai_guard("list_providers"):
        return [ProviderStatusOut(**row) for row in ai_service.provider_status(ctx.db)]


@router.put(
    "/providers/{provider}",
    response_model=ProviderStatusOut,
    summary="Update provider / Sağlayıcıyı güncelle",
)
def update_provider(
    payload: ProviderUpdateIn,
    provider: str = Path(..., max_length=16),
    ctx: Ctx = Depends(require("ai.providers", "UPDATE")),
) -> ProviderStatusOut:
    with ai_guard("update_provider"):
        row = ai_service.update_provider(
            ctx.db,
            provider,
            payload.model_dump(exclude_unset=True, exclude_none=True),
            user_id=ctx.user_id,
            audit_context=ctx.audit_kwargs(),
        )
        return ProviderStatusOut(**row)


@router.post(
    "/providers/{provider}/test",
    response_model=TestConnectionOut,
    summary="Test connection / Bağlantıyı test et",
)
def test_provider(
    provider: str = Path(..., max_length=16),
    ctx: Ctx = Depends(require("ai.providers", "EXECUTE")),
) -> TestConnectionOut:
    with ai_guard("test_provider"):
        return TestConnectionOut(**ai_service.test_provider(ctx.db, provider, lang=ctx.lang))


@router.get(
    "/providers/{provider}/models",
    response_model=ModelListOut,
    summary="Live model list / Canlı model listesi",
)
def provider_models(
    provider: str = Path(..., max_length=16),
    ctx: Ctx = Depends(require("ai.providers", "VIEW")),
) -> ModelListOut:
    with ai_guard("provider_models"):
        return ModelListOut(**ai_service.list_models(ctx.db, provider))


@router.get("/health", response_model=HealthOut, summary="AI health / AI sağlığı")
def ai_health(
    ctx: Ctx = Depends(
        require_any(("ai.providers", "VIEW"), ("ai.usage", "VIEW"), ("ai.copilot", "VIEW"))
    ),
) -> HealthOut:
    with ai_guard("ai_health"):
        payload = ai_service.health(ctx.db)
        return HealthOut(
            healthy=payload["healthy"],
            active_provider=payload["active_provider"],
            providers=[ProviderStatusOut(**p) for p in payload["providers"]],
            budget=BudgetOut(**payload["budget"]),
            checked_at=payload["checked_at"],
        )


# ===========================================================================
# Copilot
# ===========================================================================
@router.post("/ask", response_model=AskOut, summary="Ask the AI sales manager / AI'ya sor")
def ask(payload: AskIn, ctx: Ctx = Depends(require("ai.copilot", "EXECUTE"))) -> AskOut:
    with ai_guard("ask"):
        result = ai_service.ask(
            ctx.db,
            question=payload.question,
            user=ctx.user,
            conversation_id=payload.conversation_id,
            agent_kind=str(payload.agent_kind) if payload.agent_kind else None,
            language=payload.language or ctx.lang,
            preferred_provider=payload.preferred_provider,
            salesperson_ids=_own_scope(ctx),
            unrestricted=ctx.unrestricted,
            customer_id=payload.customer_id,
            product_id=payload.product_id,
            route_id=payload.route_id,
            salesperson_id=payload.salesperson_id,
            vehicle_id=payload.vehicle_id,
            horizon_days=payload.horizon_days,
        )
        return AskOut(**result)


@router.get(
    "/conversations",
    response_model=PagedResponse[ConversationOut],
    summary="Conversations / Görüşmeler",
)
def list_conversations(
    include_archived: bool = Query(False),
    page: Page = Depends(get_page),
    ctx: Ctx = Depends(require("ai.copilot", "VIEW")),
) -> dict[str, Any]:
    with ai_guard("list_conversations"):
        rows, total = ai_service.list_conversations(
            ctx.db,
            user_id=None if ctx.unrestricted else ctx.user_id,
            include_archived=include_archived,
            limit=page.limit,
            offset=page.offset,
        )
        return paginated([_conversation_out(c) for c in rows], total, page)


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationOut,
    summary="Conversation detail / Görüşme detayı",
)
def get_conversation(
    conversation_id: int,
    ctx: Ctx = Depends(require("ai.copilot", "VIEW")),
) -> ConversationOut:
    with ai_guard("get_conversation"):
        conversation = ai_service.get_conversation(
            ctx.db, conversation_id, user_id=ctx.user_id, unrestricted=ctx.unrestricted
        )
        return _conversation_out(conversation, with_messages=True)


@router.delete(
    "/conversations/{conversation_id}",
    response_model=Message,
    summary="Delete conversation / Görüşmeyi sil",
)
def delete_conversation(
    conversation_id: int,
    ctx: Ctx = Depends(require("ai.copilot", "EXECUTE")),
) -> Message:
    with ai_guard("delete_conversation"):
        ai_service.delete_conversation(
            ctx.db, conversation_id, user_id=ctx.user_id, unrestricted=ctx.unrestricted
        )
        return Message(success=True, message_key="common.deleted", message=t("common.deleted", ctx.lang))


# ===========================================================================
# Assistant
# ===========================================================================
@router.post(
    "/assistant/customer/{customer_id}",
    response_model=SuggestionOut,
    summary="Order suggestion / Sipariş önerisi",
)
def suggest_order(
    customer_id: int,
    on_date: str | None = Query(None, description="YYYY-MM-DD"),
    ctx: Ctx = Depends(require("ai.assistant", "EXECUTE")),
) -> SuggestionOut:
    with ai_guard("suggest_order"):
        _assert_customer_visible(ctx, customer_id)
        return SuggestionOut(
            **ai_service.customer_order_suggestion(
                ctx.db,
                customer_id=customer_id,
                language=ctx.lang,
                user=ctx.user,
                on_date=parse_date(on_date),
            )
        )


@router.post(
    "/assistant/van-load",
    response_model=SuggestionOut,
    summary="Van load suggestion / Araç yükleme önerisi",
)
def suggest_van_load(
    salesperson_id: int = Query(..., ge=1),
    vehicle_id: int | None = Query(None, ge=1),
    on_date: str | None = Query(None, description="YYYY-MM-DD"),
    ctx: Ctx = Depends(require("ai.assistant", "EXECUTE")),
) -> SuggestionOut:
    with ai_guard("suggest_van_load"):
        _assert_salesperson_visible(ctx, salesperson_id)
        return SuggestionOut(
            **ai_service.van_load_suggestion(
                ctx.db,
                salesperson_id=salesperson_id,
                vehicle_id=vehicle_id,
                language=ctx.lang,
                user=ctx.user,
                on_date=parse_date(on_date),
            )
        )


@router.post(
    "/assistant/customer/{customer_id}/risk",
    response_model=SuggestionOut,
    summary="Collection risk / Tahsilat riski",
)
def customer_risk(
    customer_id: int,
    ctx: Ctx = Depends(require("ai.assistant", "EXECUTE")),
) -> SuggestionOut:
    with ai_guard("customer_risk"):
        _assert_customer_visible(ctx, customer_id)
        return SuggestionOut(
            **ai_service.collection_risk(
                ctx.db, customer_id=customer_id, language=ctx.lang, user=ctx.user
            )
        )


# ===========================================================================
# Data analyst
# ===========================================================================
@router.post("/sql", response_model=SqlQueryOut, summary="Read-only NL query / Salt-okunur sorgu")
def analyst_sql(
    payload: SqlQueryIn,
    ctx: Ctx = Depends(require("ai.copilot", "EXECUTE")),
) -> SqlQueryOut:
    with ai_guard("analyst_sql"):
        return SqlQueryOut(
            **ai_service.analyst_query(
                ctx.db,
                question=payload.question,
                sql=payload.sql,
                max_rows=payload.max_rows,
                explain=payload.explain,
                language=payload.language or ctx.lang,
                user=ctx.user,
            )
        )


# ===========================================================================
# Usage & budget
# ===========================================================================
@router.get("/usage", response_model=UsageSummaryOut, summary="Token & cost / Token ve maliyet")
def usage(
    start: str | None = Query(None, description="YYYY-MM-DD"),
    end: str | None = Query(None, description="YYYY-MM-DD"),
    group_by: str = Query("day", pattern="^(day|provider|model|user|agent)$"),
    ctx: Ctx = Depends(require("ai.usage", "VIEW")),
) -> UsageSummaryOut:
    with ai_guard("usage"):
        return UsageSummaryOut(
            **ai_service.usage_summary(
                ctx.db,
                start=parse_date(start),
                end=parse_date(end),
                group_by=group_by,
                # Without company-wide visibility a user only sees their own spend.
                user_ids=None if ctx.unrestricted else [ctx.user_id],
            )
        )


@router.get("/budget", response_model=BudgetOut, summary="Monthly budget / Aylık bütçe")
def budget(ctx: Ctx = Depends(require("ai.usage", "VIEW"))) -> BudgetOut:
    with ai_guard("budget"):
        return BudgetOut(**ai_service.budget_status(ctx.db))


# ===========================================================================
# Terminal
# ===========================================================================
@router.post(
    "/terminal/sessions",
    response_model=TerminalSessionOut,
    summary="Open terminal session / Terminal oturumu aç",
)
def create_terminal_session(
    payload: TerminalSessionIn = Body(default_factory=TerminalSessionIn),
    ctx: Ctx = Depends(require("ai.terminal", "EXECUTE")),
) -> TerminalSessionOut:
    with ai_guard("create_terminal_session"):
        session = ai_service.create_terminal_session(
            ctx.db,
            user=ctx.user,
            title=payload.title,
            permission_level=str(payload.permission_level or AIPermissionLevel.READ_ONLY),
            audit_context=ctx.audit_kwargs(),
        )
        return TerminalSessionOut.model_validate(session)


@router.get(
    "/terminal/sessions",
    response_model=list[TerminalSessionOut],
    summary="Terminal sessions / Terminal oturumları",
)
def list_terminal_sessions(
    active_only: bool = Query(False),
    ctx: Ctx = Depends(require("ai.terminal", "VIEW")),
) -> list[TerminalSessionOut]:
    with ai_guard("list_terminal_sessions"):
        rows = ai_service.list_terminal_sessions(
            ctx.db,
            user_id=None if ctx.unrestricted else ctx.user_id,
            active_only=active_only,
        )
        return [TerminalSessionOut.model_validate(row) for row in rows]


@router.post(
    "/terminal/run",
    response_model=TerminalCommandOut,
    summary="Run terminal action / Terminal komutu çalıştır",
)
def run_terminal_command(
    payload: TerminalCommandIn,
    ctx: Ctx = Depends(require("ai.terminal", "EXECUTE")),
) -> TerminalCommandOut:
    with ai_guard("run_terminal_command"):
        session = ai_service.get_terminal_session(
            ctx.db, payload.session_id, user=ctx.user, unrestricted=ctx.unrestricted
        )
        result = ai_service.run_command(
            ctx.db,
            session,
            payload.instruction,
            user=ctx.user,
            requested_action=payload.requested_action,
            target=payload.target,
            command=payload.command,
            approve_token=payload.approve_token,
            lang=ctx.lang,
            audit_context=ctx.audit_kwargs(),
        )
        return TerminalCommandOut(**result)


__all__ = ["router"]


# Keep a stable import for tooling that expects the date type here.
_ = date
