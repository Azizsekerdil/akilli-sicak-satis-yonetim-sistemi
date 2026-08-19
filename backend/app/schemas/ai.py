"""
Pydantic contracts for the AI API.

One rule shapes every schema here: a credential never appears in a response
model.  ``ProviderUpdateIn`` accepts an ``api_key`` on the way in and it is
excluded from serialisation; on the way out a provider only ever reports
``configured: bool`` and a masked hint.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import AIAgentKind, AIPermissionLevel, AITaskType
from app.schemas.common import ORMModel


# ===========================================================================
# Providers
# ===========================================================================
class ProviderStatusOut(BaseModel):
    """Operational state of one provider — never its credential."""

    provider: str
    display_name: str = ""
    enabled: bool = True
    #: Whether a usable credential exists.  Boolean only, by design.
    configured: bool = False
    healthy: bool = True
    base_url: str = ""
    model: str | None = None
    masked_key: str = ""
    latency_ms: int | None = None
    avg_latency_ms: float = 0.0
    error_rate: float = 0.0
    requests: int = 0
    errors: int = 0
    last_ok_at: datetime | None = None
    last_error_at: datetime | None = None
    last_error: str | None = None
    failover_priority: int = 100
    supports_vision: bool = False
    supports_embeddings: bool = False
    input_cost_per_1k: Decimal = Decimal("0")
    output_cost_per_1k: Decimal = Decimal("0")
    task_model_map: dict[str, str] = Field(default_factory=dict)


class ProviderUpdateIn(BaseModel):
    """Editable provider settings.  ``api_key`` is write-only."""

    model_config = ConfigDict(extra="forbid")

    is_enabled: bool | None = None
    base_url: str | None = Field(default=None, max_length=255)
    default_model: str | None = Field(default=None, max_length=128)
    #: Written to the .env file and never stored in the database.
    api_key: str | None = Field(default=None, exclude=True, repr=False, max_length=512)
    timeout_seconds: int | None = Field(default=None, ge=5, le=900)
    max_tokens: int | None = Field(default=None, ge=64, le=200_000)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    failover_priority: int | None = Field(default=None, ge=1, le=1000)
    task_model_map: dict[str, str] | None = None
    input_cost_per_1k: Decimal | None = Field(default=None, ge=0)
    output_cost_per_1k: Decimal | None = Field(default=None, ge=0)


class ModelListOut(BaseModel):
    provider: str
    models: list[str] = Field(default_factory=list)
    default_model: str | None = None
    count: int = 0


class TestConnectionOut(BaseModel):
    provider: str
    ok: bool = False
    latency_ms: int = 0
    models: list[str] = Field(default_factory=list)
    error: str | None = None
    message: str = ""


class HealthOut(BaseModel):
    """Whole-subsystem health, used by the AI status panel."""

    healthy: bool = False
    active_provider: str | None = None
    providers: list[ProviderStatusOut] = Field(default_factory=list)
    budget: "BudgetOut | None" = None
    checked_at: datetime | None = None


# ===========================================================================
# Copilot
# ===========================================================================
class AskIn(BaseModel):
    """A question for the copilot, plus any subject the caller already knows."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=4000)
    conversation_id: int | None = None
    agent_kind: AIAgentKind | None = None
    language: str | None = Field(default=None, max_length=8)
    preferred_provider: str | None = Field(default=None, max_length=16)
    customer_id: int | None = None
    product_id: int | None = None
    route_id: int | None = None
    salesperson_id: int | None = None
    vehicle_id: int | None = None
    horizon_days: int | None = Field(default=None, ge=1, le=365)


class AskOut(BaseModel):
    """The copilot's answer with full provenance."""

    conversation_id: int
    message_id: int | None = None
    agent_kind: str
    answer: str = ""
    reasoning: str | None = None
    provider: str | None = None
    model: str | None = None
    data_context: dict[str, Any] = Field(default_factory=dict)
    suggestion: dict[str, Any] | None = None
    confidence: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    #: Set when the figures are present but no provider produced a narrative.
    error_key: str | None = None
    degraded: bool = False


class MessageOut(ORMModel):
    id: int
    conversation_id: int
    role: str
    content: str
    reasoning: str | None = None
    provider: str | None = None
    model: str | None = None
    data_context: dict[str, Any] | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    created_at: datetime | None = None


class ConversationOut(ORMModel):
    id: int
    user_id: int | None = None
    title: str = ""
    agent_kind: str = str(AIAgentKind.ORCHESTRATOR)
    language: str = "tr"
    is_archived: bool = False
    message_count: int = 0
    total_tokens: int = 0
    total_cost: Decimal = Decimal("0")
    created_at: datetime | None = None
    updated_at: datetime | None = None
    messages: list[MessageOut] = Field(default_factory=list)


# ===========================================================================
# Usage & budget
# ===========================================================================
class UsageRow(BaseModel):
    """One bucket of the usage summary; ``key`` depends on ``group_by``."""

    key: str
    label: str = ""
    bucket_date: date | None = None
    requests: int = 0
    errors: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost: Decimal = Decimal("0")
    avg_latency_ms: float = 0.0


class UsageSummaryOut(BaseModel):
    start: date
    end: date
    group_by: str = "day"
    rows: list[UsageRow] = Field(default_factory=list)
    total_requests: int = 0
    total_errors: int = 0
    total_tokens: int = 0
    total_cost: Decimal = Decimal("0")
    currency: str = "USD"


class BudgetOut(BaseModel):
    spent_this_month: Decimal = Decimal("0")
    budget: Decimal = Decimal("0")
    percent: float = 0.0
    warn: bool = False
    exceeded: bool = False
    currency: str = "USD"
    period_start: date | None = None


# ===========================================================================
# SQL / analyst
# ===========================================================================
class SqlQueryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)
    #: Optional hand-written SQL.  It still goes through the read-only guard.
    sql: str | None = Field(default=None, max_length=8000)
    max_rows: int = Field(default=200, ge=1, le=5000)
    explain: bool = True
    language: str | None = Field(default=None, max_length=8)


class SqlQueryOut(BaseModel):
    question: str
    sql: str | None = None
    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    answer: str = ""
    provider: str | None = None
    model: str | None = None
    error_key: str | None = None


# ===========================================================================
# Suggestions
# ===========================================================================
class SuggestionOut(BaseModel):
    """A concrete recommendation plus the narrative that justifies it."""

    suggestion_id: int | None = None
    suggestion_kind: str
    agent_kind: str
    subject_type: str = "CUSTOMER"
    subject_id: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    explanation: str = ""
    reasoning: str | None = None
    confidence: float = 0.0
    provider: str | None = None
    model: str | None = None
    error_key: str | None = None
    degraded: bool = False


# ===========================================================================
# Terminal
# ===========================================================================
class TerminalSessionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="", max_length=255)
    permission_level: AIPermissionLevel = AIPermissionLevel.READ_ONLY


class TerminalSessionOut(ORMModel):
    id: int
    user_id: int
    title: str = ""
    permission_level: str = str(AIPermissionLevel.READ_ONLY)
    provider: str | None = None
    model: str | None = None
    is_active: bool = True
    command_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class TerminalCommandIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: int
    instruction: str = Field(min_length=1, max_length=2000)
    #: READ_FILE | LIST_DIR | WRITE_FILE | RUN_TESTS | PACKAGE_INSTALL | GIT | SHELL
    requested_action: str = Field(default="SHELL", max_length=32)
    target: str | None = Field(default=None, max_length=1024)
    #: Command line, or file content when ``requested_action`` is WRITE_FILE.
    command: str | None = Field(default=None, max_length=200_000)
    #: Echoed back by the user to authorise a SYSTEM_COMMAND.
    approve_token: str | None = Field(default=None, max_length=64)


class TerminalCommandOut(BaseModel):
    id: int
    session_id: int
    action_type: str
    required_level: str
    is_allowed: bool = False
    requires_approval: bool = False
    approve_token: str | None = None
    block_reason: str | None = None
    command: str | None = None
    target: str | None = None
    exit_code: int | None = None
    output: str = ""
    duration_ms: int = 0
    message_key: str | None = None
    created_at: datetime | None = None


# ===========================================================================
# Misc
# ===========================================================================
class TaskTypeOption(BaseModel):
    value: AITaskType
    default_model: str | None = None


HealthOut.model_rebuild()


__all__ = [
    "AskIn",
    "AskOut",
    "BudgetOut",
    "ConversationOut",
    "HealthOut",
    "MessageOut",
    "ModelListOut",
    "ProviderStatusOut",
    "ProviderUpdateIn",
    "SqlQueryIn",
    "SqlQueryOut",
    "SuggestionOut",
    "TaskTypeOption",
    "TerminalCommandIn",
    "TerminalCommandOut",
    "TerminalSessionIn",
    "TerminalSessionOut",
    "TestConnectionOut",
    "UsageRow",
    "UsageSummaryOut",
]
