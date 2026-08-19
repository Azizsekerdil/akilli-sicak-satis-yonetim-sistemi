"""System tables: settings, audit log, notifications, backups and training."""

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
    BackupStatus,
    BackupType,
    NotificationSeverity,
    NotificationType,
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


class Setting(Base, TimestampMixin, AuthorMixin):
    """Runtime-editable key/value configuration (non-secret values only)."""

    __tablename__ = "settings"
    __table_args__ = (
        UniqueConstraint("category", "key", name="uq_settings_category_key"),
    )

    id: Mapped[int] = pk()
    category: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(96), nullable=False, index=True)
    value: Mapped[str | None] = mapped_column(Text)
    value_type: Mapped[str] = mapped_column(String(16), default="string", nullable=False)
    default_value: Mapped[str | None] = mapped_column(Text)

    label_tr: Mapped[str | None] = mapped_column(String(255))
    label_en: Mapped[str | None] = mapped_column(String(255))
    description_tr: Mapped[str | None] = mapped_column(Text)
    description_en: Mapped[str | None] = mapped_column(Text)

    #: True when the value is a credential reference — the API masks it.
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_editable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    requires_restart: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class AuditLog(Base):
    """
    Tamper-evident activity log.

    Append-only by policy: no API path updates or deletes these rows, and each
    row carries a SHA-256 ``checksum`` over its own content chained to the
    previous entry's checksum, so any silent edit is detectable.
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_user_time", "user_id", "created_at"),
        Index("ix_audit_logs_entity", "entity_type", "entity_id"),
    )

    id: Mapped[int] = pk()
    action: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    entity_type: Mapped[str | None] = mapped_column(String(48), index=True)
    entity_id: Mapped[int | None] = mapped_column(Integer, index=True)
    entity_label: Mapped[str | None] = mapped_column(String(255))

    user_id: Mapped[int | None] = mapped_column(Integer, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    role_code: Mapped[str | None] = mapped_column(String(64))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    request_path: Mapped[str | None] = mapped_column(String(255))
    request_method: Mapped[str | None] = mapped_column(String(8))

    #: True when an AI agent performed the action on the user's behalf.
    is_ai_action: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    ai_agent_kind: Mapped[str | None] = mapped_column(String(24))
    ai_request_id: Mapped[str | None] = mapped_column(String(48))

    summary: Mapped[str | None] = mapped_column(String(512))
    #: JSON before/after snapshots (secrets stripped before writing).
    old_values: Mapped[str | None] = mapped_column(Text)
    new_values: Mapped[str | None] = mapped_column(Text)
    amount: Mapped[Decimal | None] = mapped_column(Money)

    checksum: Mapped[str | None] = mapped_column(String(64))
    previous_checksum: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, nullable=False, index=True
    )


class Notification(Base):
    """An alert raised for a user or a role."""

    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_user_read", "user_id", "is_read"),
        Index("ix_notifications_type_time", "notification_type", "created_at"),
    )

    id: Mapped[int] = pk()
    notification_type: Mapped[str] = mapped_column(
        String(32), default=NotificationType.SYSTEM, nullable=False, index=True
    )
    severity: Mapped[str] = mapped_column(
        String(16), default=NotificationSeverity.INFO, nullable=False, index=True
    )

    #: Either a specific user, or every user holding this role.
    user_id: Mapped[int | None] = mapped_column(Integer, index=True)
    role_code: Mapped[str | None] = mapped_column(String(64), index=True)

    title_tr: Mapped[str] = mapped_column(String(255), nullable=False)
    title_en: Mapped[str | None] = mapped_column(String(255))
    body_tr: Mapped[str | None] = mapped_column(Text)
    body_en: Mapped[str | None] = mapped_column(Text)

    entity_type: Mapped[str | None] = mapped_column(String(48))
    entity_id: Mapped[int | None] = mapped_column(Integer)
    action_url: Mapped[str | None] = mapped_column(String(512))
    #: Stops the same alert being raised twice for the same cause.
    dedupe_key: Mapped[str | None] = mapped_column(String(128), index=True)

    is_read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    read_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    is_dismissed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, nullable=False, index=True
    )

    def title(self, lang: str = "tr") -> str:
        return (self.title_en or self.title_tr) if lang == "en" else self.title_tr


class BackupRecord(Base, TimestampMixin, AuthorMixin):
    """One backup run, with the checksum used to verify and restore it."""

    __tablename__ = "backups"
    __table_args__ = (
        Index("ix_backups_status_time", "status", "created_at"),
    )

    id: Mapped[int] = pk()
    backup_type: Mapped[str] = mapped_column(
        String(16), default=BackupType.FULL, nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(16), default=BackupStatus.RUNNING, nullable=False, index=True
    )
    trigger: Mapped[str] = mapped_column(String(16), default="MANUAL", nullable=False)  # MANUAL|SCHEDULED

    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    #: Full backup this incremental one builds on.
    base_backup_id: Mapped[int | None] = fk("backups.id", nullable=True, ondelete="SET NULL")

    database_engine: Mapped[str | None] = mapped_column(String(24))
    app_version: Mapped[str | None] = mapped_column(String(24))
    schema_version: Mapped[str | None] = mapped_column(String(48))
    table_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    includes_files: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    verify_message: Mapped[str | None] = mapped_column(String(512))
    restored_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    restored_by_id: Mapped[int | None] = mapped_column(Integer)
    error_message: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)


class TrainingLesson(Base, TimestampMixin):
    """A lesson in the in-app training centre (Eğitim Merkezi)."""

    __tablename__ = "training_lessons"
    __table_args__ = (UniqueConstraint("code", name="uq_training_lessons_code"),)

    id: Mapped[int] = pk()
    code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False, index=True)
    module: Mapped[str | None] = mapped_column(String(48), index=True)

    title_tr: Mapped[str] = mapped_column(String(255), nullable=False)
    title_en: Mapped[str] = mapped_column(String(255), nullable=False)
    summary_tr: Mapped[str | None] = mapped_column(Text)
    summary_en: Mapped[str | None] = mapped_column(Text)
    body_tr: Mapped[str] = mapped_column(Text, nullable=False)
    body_en: Mapped[str] = mapped_column(Text, nullable=False)
    #: JSON array of {title_tr, title_en, detail_tr, detail_en, screen}
    steps: Mapped[str | None] = mapped_column(Text)
    #: In-app route the lesson refers to, e.g. "/sales/hot-sale"
    target_route: Mapped[str | None] = mapped_column(String(128))
    estimated_minutes: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    required_role: Mapped[str | None] = mapped_column(String(64))
    is_published: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    progress: Mapped[list["TrainingProgress"]] = relationship(
        back_populates="lesson", cascade="all, delete-orphan"
    )

    def title(self, lang: str = "tr") -> str:
        return self.title_en if lang == "en" else self.title_tr


class TrainingProgress(Base, TimestampMixin):
    """Per-user completion state for a lesson."""

    __tablename__ = "training_progress"
    __table_args__ = (
        UniqueConstraint("user_id", "lesson_id", name="uq_training_progress_user_lesson"),
    )

    id: Mapped[int] = pk()
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    lesson_id: Mapped[int] = fk("training_lessons.id", ondelete="CASCADE")
    is_completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    progress_percent: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    last_step: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    score: Mapped[float | None] = mapped_column(Float)

    lesson: Mapped["TrainingLesson"] = relationship(back_populates="progress")


class NumberSequence(Base, TimestampMixin):
    """
    Atomic document-number generator (orders, invoices, transfers…).

    Incremented inside the caller's transaction with a locking read so two
    concurrent sales can never receive the same invoice number.
    """

    __tablename__ = "number_sequences"
    __table_args__ = (
        UniqueConstraint("key", "period", name="uq_number_sequences_key_period"),
    )

    id: Mapped[int] = pk()
    key: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    #: Reset scope, e.g. "2026" or "2026-08"; "*" means never reset.
    period: Mapped[str] = mapped_column(String(16), default="*", nullable=False)
    prefix: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    padding: Mapped[int] = mapped_column(Integer, default=6, nullable=False)
    next_value: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    last_issued_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class HealthCheckResult(Base):
    """Latest result per health-check component, shown on the system health screen."""

    __tablename__ = "health_check_results"
    __table_args__ = (UniqueConstraint("component", name="uq_health_check_results_component"),)

    id: Mapped[int] = pk()
    component: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(16), default="UNKNOWN", nullable=False, index=True)
    message: Mapped[str | None] = mapped_column(String(512))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    details: Mapped[str | None] = mapped_column(Text)
    checked_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, nullable=False
    )
    checked_on: Mapped[date | None] = mapped_column(Date)
