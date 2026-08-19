"""
AI subsystem persistence: provider configuration, request/usage accounting,
conversations, and the permission-gated development terminal.

Credentials are **never** stored here in plaintext.  ``api_key_ref`` names the
environment variable / secret-store entry that holds the real key.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import (
    AIAgentKind,
    AIPermissionLevel,
    AIRequestStatus,
    AITaskType,
)
from app.models.base import (
    AuthorMixin,
    Base,
    Money,
    TimestampMixin,
    UTCDateTime,
    fk,
    pk,
    utcnow,
)


class AIProviderConfig(Base, TimestampMixin, AuthorMixin):
    """Per-provider settings and live health/latency statistics."""

    __tablename__ = "ai_provider_configs"
    __table_args__ = (UniqueConstraint("provider", name="uq_ai_provider_configs_provider"),)

    id: Mapped[int] = pk()
    provider: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(64), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    base_url: Mapped[str] = mapped_column(String(255), nullable=False)
    default_model: Mapped[str | None] = mapped_column(String(128))

    #: Name of the env var / secret entry holding the key — NOT the key itself.
    api_key_ref: Mapped[str | None] = mapped_column(String(64))
    has_api_key: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    timeout_seconds: Mapped[int] = mapped_column(Integer, default=120, nullable=False)
    max_tokens: Mapped[int] = mapped_column(Integer, default=2048, nullable=False)
    temperature: Mapped[float] = mapped_column(Float, default=0.3, nullable=False)
    context_length: Mapped[int | None] = mapped_column(Integer)
    supports_streaming: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    supports_vision: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    supports_embeddings: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    failover_priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    #: JSON: {"GENERAL": "model-a", "VISION": "model-b", ...}
    task_model_map: Mapped[str | None] = mapped_column(Text)

    input_cost_per_1k: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    output_cost_per_1k: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)

    # --- Live health -------------------------------------------------------
    last_ok_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_error_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_error: Mapped[str | None] = mapped_column(String(512))
    last_latency_ms: Mapped[int | None] = mapped_column(Integer)
    avg_latency_ms: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    request_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_healthy: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    @property
    def error_rate(self) -> float:
        if not self.request_count:
            return 0.0
        return round(self.error_count / self.request_count * 100, 2)


class AIRequest(Base):
    """One model call: what was asked, which provider answered, what it cost."""

    __tablename__ = "ai_requests"
    __table_args__ = (
        Index("ix_ai_requests_provider_time", "provider", "created_at"),
        Index("ix_ai_requests_user_time", "user_id", "created_at"),
    )

    id: Mapped[int] = pk()
    request_id: Mapped[str] = mapped_column(String(48), nullable=False, unique=True, index=True)
    provider: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    model: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    task_type: Mapped[str] = mapped_column(String(24), default=AITaskType.GENERAL, nullable=False)
    agent_kind: Mapped[str | None] = mapped_column(String(24), index=True)

    user_id: Mapped[int | None] = mapped_column(Integer, index=True)
    conversation_id: Mapped[int | None] = mapped_column(Integer, index=True)

    status: Mapped[str] = mapped_column(
        String(20), default=AIRequestStatus.SUCCESS, nullable=False, index=True
    )
    #: Providers tried before this one succeeded, comma-separated.
    failover_from: Mapped[str | None] = mapped_column(String(64))
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    prompt_preview: Mapped[str | None] = mapped_column(String(1024))
    response_preview: Mapped[str | None] = mapped_column(String(1024))
    error_message: Mapped[str | None] = mapped_column(String(512))

    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reasoning_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    estimated_cost: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)

    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, nullable=False, index=True
    )


class AIUsageDaily(Base):
    """Daily rollup per (date, provider, model, user) — powers the cost screens."""

    __tablename__ = "ai_usage_daily"
    __table_args__ = (
        UniqueConstraint(
            "usage_date", "provider", "model", "user_id", name="uq_ai_usage_daily_key"
        ),
    )

    id: Mapped[int] = pk()
    usage_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    agent_kind: Mapped[str | None] = mapped_column(String(24))

    request_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    estimated_cost: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    avg_latency_ms: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)


class AIConversation(Base, TimestampMixin):
    """A chat thread with the AI sales manager / copilot."""

    __tablename__ = "ai_conversations"
    __table_args__ = (Index("ix_ai_conversations_user_time", "user_id", "updated_at"),)

    id: Mapped[int] = pk()
    user_id: Mapped[int | None] = mapped_column(Integer, index=True)
    title: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    agent_kind: Mapped[str] = mapped_column(
        String(24), default=AIAgentKind.ORCHESTRATOR, nullable=False
    )
    language: Mapped[str] = mapped_column(String(8), default="tr", nullable=False)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    message_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_cost: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)

    messages: Mapped[list["AIMessage"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="AIMessage.id",
    )


class AIMessage(Base):
    """One turn in an AI conversation."""

    __tablename__ = "ai_messages"
    __table_args__ = (Index("ix_ai_messages_conv", "conversation_id", "id"),)

    id: Mapped[int] = pk()
    conversation_id: Mapped[int] = fk("ai_conversations.id", ondelete="CASCADE")
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user|assistant|system|tool
    content: Mapped[str] = mapped_column(Text, nullable=False)
    #: Thinking output from reasoning models (LM Studio ``reasoning_content``).
    reasoning: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(String(16))
    model: Mapped[str | None] = mapped_column(String(128))

    #: JSON list of tools invoked and their (redacted) results.
    tool_calls: Mapped[str | None] = mapped_column(Text)
    #: JSON: the read-only SQL executed and row count, for transparency.
    data_context: Mapped[str | None] = mapped_column(Text)

    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, nullable=False
    )

    conversation: Mapped["AIConversation"] = relationship(back_populates="messages")


class AISuggestion(Base, TimestampMixin):
    """
    A concrete recommendation produced by an agent (order suggestion, van load,
    route change…) together with the reasoning that produced it.

    Persisted so suggestions can be accepted/rejected and later scored for
    accuracy — an AI feature you cannot measure is an AI feature you cannot trust.
    """

    __tablename__ = "ai_suggestions"
    __table_args__ = (
        Index("ix_ai_suggestions_kind_subject", "suggestion_kind", "subject_type", "subject_id"),
    )

    id: Mapped[int] = pk()
    suggestion_kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    agent_kind: Mapped[str | None] = mapped_column(String(24))
    subject_type: Mapped[str] = mapped_column(String(24), default="CUSTOMER", nullable=False)
    subject_id: Mapped[int | None] = mapped_column(Integer, index=True)
    suggested_for: Mapped[date | None] = mapped_column(Date, index=True)

    #: JSON payload: the structured suggestion (e.g. list of product/qty).
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    explanation: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    provider: Mapped[str | None] = mapped_column(String(16))
    model: Mapped[str | None] = mapped_column(String(128))

    is_accepted: Mapped[bool | None] = mapped_column(Boolean, index=True)
    acted_on_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    acted_by_id: Mapped[int | None] = mapped_column(Integer)
    #: Filled later: how close the suggestion was to reality (0-100).
    accuracy_score: Mapped[float | None] = mapped_column(Float)


class AITerminalSession(Base, TimestampMixin):
    """A session of the in-app AI development terminal, with its permission tier."""

    __tablename__ = "ai_terminal_sessions"
    __table_args__ = (Index("ix_ai_terminal_sessions_user", "user_id", "created_at"),)

    id: Mapped[int] = pk()
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    permission_level: Mapped[str] = mapped_column(
        String(24), default=AIPermissionLevel.READ_ONLY, nullable=False
    )
    provider: Mapped[str | None] = mapped_column(String(16))
    model: Mapped[str | None] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    command_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    commands: Mapped[list["AITerminalCommand"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="AITerminalCommand.id"
    )


class AITerminalCommand(Base):
    """
    A single terminal action.

    Every action — including ones that were **blocked** or are awaiting user
    approval — is recorded, so the terminal is fully auditable.
    """

    __tablename__ = "ai_terminal_commands"
    __table_args__ = (Index("ix_ai_terminal_commands_session", "session_id", "id"),)

    id: Mapped[int] = pk()
    session_id: Mapped[int] = fk("ai_terminal_sessions.id", ondelete="CASCADE")
    user_id: Mapped[int | None] = mapped_column(Integer, index=True)

    instruction: Mapped[str] = mapped_column(Text, nullable=False)
    action_type: Mapped[str] = mapped_column(String(32), nullable=False)   # READ_FILE|WRITE_FILE|RUN_TESTS|...
    target: Mapped[str | None] = mapped_column(String(1024))
    command: Mapped[str | None] = mapped_column(Text)
    required_level: Mapped[str] = mapped_column(
        String(24), default=AIPermissionLevel.READ_ONLY, nullable=False
    )

    is_allowed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    approved_by_id: Mapped[int | None] = mapped_column(Integer)
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    block_reason: Mapped[str | None] = mapped_column(String(512))

    exit_code: Mapped[int | None] = mapped_column(Integer)
    output_preview: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    executed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, nullable=False
    )

    session: Mapped["AITerminalSession"] = relationship(back_populates="commands")
