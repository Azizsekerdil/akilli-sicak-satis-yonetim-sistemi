"""Pydantic schemas for system administration: users, roles, audit, settings,
notifications, health, backups and the training centre."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


# ===========================================================================
# Users & roles
# ===========================================================================
class UserAdminOut(ORMModel):
    """A user as the administration screen sees them (never any hash)."""

    id: int
    username: str
    full_name: str
    email: str | None = None
    phone: str | None = None
    role_id: int
    role_code: str | None = None
    role_name: str | None = None
    role_rank: int | None = None
    region_id: int | None = None
    company_id: int | None = None
    manager_id: int | None = None
    status: str
    language: str = "tr"
    data_scope: str | None = None
    must_change_password: bool = False
    is_deleted: bool = False
    last_login_at: datetime | None = None
    last_login_ip: str | None = None
    failed_login_count: int = 0
    locked_until: datetime | None = None
    salesperson_id: int | None = None
    permission_count: int = 0
    created_at: datetime | None = None


class UserCreateIn(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=2, max_length=255)
    role_code: str
    email: str | None = None
    phone: str | None = None
    region_id: int | None = None
    company_id: int | None = None
    language: Literal["tr", "en"] = "tr"


class UserUpdateIn(BaseModel):
    full_name: str | None = Field(default=None, max_length=255)
    email: str | None = None
    phone: str | None = None
    role_code: str | None = None
    region_id: int | None = None
    manager_id: int | None = None
    status: Literal["ACTIVE", "INACTIVE", "LOCKED", "SUSPENDED"] | None = None
    language: Literal["tr", "en"] | None = None


class PasswordResetIn(BaseModel):
    new_password: str = Field(min_length=8, max_length=128)


class PermissionOverrideIn(BaseModel):
    """Per-user grants/revokes layered on top of the role."""

    grant: list[str] = Field(default_factory=list)
    revoke: list[str] = Field(default_factory=list)
    data_scope: Literal["ALL", "REGION", "TEAM", "OWN", "NONE"] | None = None


class RoleOut(BaseModel):
    code: str
    name: str
    name_tr: str
    name_en: str
    rank: int
    data_scope: str
    permission_count: int = 0
    user_count: int = 0
    is_system: bool = True


class RolePermissionMatrixOut(BaseModel):
    """Roles down one axis, permission codes along the other."""

    resources: list[dict[str, Any]] = Field(default_factory=list)
    roles: list[RoleOut] = Field(default_factory=list)
    #: ``{role_code: [permission codes]}``
    matrix: dict[str, list[str]] = Field(default_factory=dict)


# ===========================================================================
# Audit
# ===========================================================================
class AuditLogOut(ORMModel):
    id: int
    action: str
    entity_type: str | None = None
    entity_id: int | None = None
    entity_label: str | None = None
    user_id: int | None = None
    username: str | None = None
    role_code: str | None = None
    ip_address: str | None = None
    request_path: str | None = None
    request_method: str | None = None
    is_ai_action: bool = False
    ai_agent_kind: str | None = None
    summary: str | None = None
    old_values: Any = None
    new_values: Any = None
    amount: Decimal | None = None
    checksum: str | None = None
    created_at: datetime


class AuditVerifyOut(BaseModel):
    valid: bool
    checked: int = 0
    broken_at: int | None = None
    reason: str | None = None


# ===========================================================================
# Notifications
# ===========================================================================
class NotificationOut(BaseModel):
    id: int
    notification_type: str
    severity: str
    title: str
    title_tr: str
    title_en: str | None = None
    body: str | None = None
    entity_type: str | None = None
    entity_id: int | None = None
    action_url: str | None = None
    is_read: bool = False
    is_dismissed: bool = False
    created_at: datetime
    read_at: datetime | None = None
    expires_at: datetime | None = None


class NotificationSummaryOut(BaseModel):
    unread: int = 0
    total: int = 0


class NotificationBroadcastIn(BaseModel):
    title_tr: str = Field(max_length=255)
    title_en: str = Field(max_length=255)
    body_tr: str | None = None
    body_en: str | None = None
    severity: Literal["INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    role_code: str | None = None
    expires_at: datetime | None = None


# ===========================================================================
# Health
# ===========================================================================
class HealthComponentOut(BaseModel):
    component: str
    label_tr: str | None = None
    label_en: str | None = None
    state: str
    message: str = ""
    latency_ms: int | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    checked_at: datetime | None = None


class HealthOut(BaseModel):
    state: str
    checked_at: datetime
    app_version: str
    environment: str
    components: list[HealthComponentOut] = Field(default_factory=list)


# ===========================================================================
# Settings
# ===========================================================================
class SettingOut(BaseModel):
    id: int
    category: str
    key: str
    #: Masked when the setting is flagged secret.
    value: str | None = None
    value_type: str = "string"
    default_value: str | None = None
    label: str
    label_tr: str | None = None
    label_en: str | None = None
    description: str | None = None
    is_secret: bool = False
    is_editable: bool = True
    requires_restart: bool = False
    sort_order: int = 0


class SettingGroupOut(BaseModel):
    category: str
    label: str
    label_tr: str
    label_en: str
    items: list[SettingOut] = Field(default_factory=list)


class SettingUpdateIn(BaseModel):
    category: str
    key: str
    value: Any = None


class SettingBulkUpdateIn(BaseModel):
    items: list[SettingUpdateIn] = Field(default_factory=list)


# ===========================================================================
# Backups
# ===========================================================================
class BackupOut(ORMModel):
    id: int
    backup_type: str
    status: str
    trigger: str
    file_name: str
    file_path: str
    size_bytes: int = 0
    checksum_sha256: str | None = None
    database_engine: str | None = None
    app_version: str | None = None
    table_count: int = 0
    row_count: int = 0
    includes_files: bool = False
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_seconds: float = 0.0
    verified_at: datetime | None = None
    verify_message: str | None = None
    restored_at: datetime | None = None
    error_message: str | None = None
    notes: str | None = None
    created_at: datetime | None = None


class BackupCreateIn(BaseModel):
    backup_type: Literal["FULL", "DATABASE", "FILES", "SETTINGS", "INCREMENTAL"] = "FULL"
    include_files: bool = False
    notes: str | None = Field(default=None, max_length=1000)


class RestoreIn(BaseModel):
    """Restoring overwrites the live database — ``confirm`` must be explicit."""

    confirm: bool = False


class BackupStorageOut(BaseModel):
    count: int = 0
    total_bytes: int = 0
    directory: str
    free_bytes: int = 0
    newest_file: str | None = None
    newest_at: str | None = None
    newest_age_days: int | None = None
    newest_verified: bool = False


class SettingsImportIn(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)


# ===========================================================================
# Training
# ===========================================================================
class LessonStepOut(BaseModel):
    index: int
    title: str
    detail: str
    title_tr: str | None = None
    title_en: str | None = None
    detail_tr: str | None = None
    detail_en: str | None = None
    screen: str | None = None


class LessonOut(BaseModel):
    id: int
    code: str
    module: str | None = None
    sort_order: int = 0
    title: str
    title_tr: str
    title_en: str
    summary: str = ""
    body: str | None = None
    body_tr: str | None = None
    body_en: str | None = None
    target_route: str | None = None
    estimated_minutes: int = 5
    required_role: str | None = None
    step_count: int = 0
    steps: list[LessonStepOut] = Field(default_factory=list)
    is_completed: bool = False
    progress_percent: float = 0.0
    last_step: int = 0
    completed_at: datetime | None = None


class LessonProgressIn(BaseModel):
    last_step: int | None = Field(default=None, ge=0, le=100)
    progress_percent: float | None = Field(default=None, ge=0, le=100)
    is_completed: bool | None = None
    score: float | None = Field(default=None, ge=0, le=100)


class TrainingSummaryOut(BaseModel):
    total_lessons: int = 0
    completed_lessons: int = 0
    in_progress_lessons: int = 0
    not_started_lessons: int = 0
    completion_percent: float = 0.0
    total_minutes: int = 0
    last_activity_at: datetime | None = None


# ===========================================================================
# System info
# ===========================================================================
class SystemInfoOut(BaseModel):
    app_name: str
    app_version: str
    environment: str
    default_language: str
    default_currency: str
    timezone: str
    database_engine: str
    api_prefix: str
    started_at: datetime | None = None
    #: ``{"customers": 1234, "products": 567, …}``
    counts: dict[str, int] = Field(default_factory=dict)
    modules: list[str] = Field(default_factory=list)
    ai_providers: list[dict[str, Any]] = Field(default_factory=list)


__all__ = [
    "AuditLogOut",
    "AuditVerifyOut",
    "BackupCreateIn",
    "BackupOut",
    "BackupStorageOut",
    "HealthComponentOut",
    "HealthOut",
    "LessonOut",
    "LessonProgressIn",
    "LessonStepOut",
    "NotificationBroadcastIn",
    "NotificationOut",
    "NotificationSummaryOut",
    "PasswordResetIn",
    "PermissionOverrideIn",
    "RestoreIn",
    "RoleOut",
    "RolePermissionMatrixOut",
    "SettingBulkUpdateIn",
    "SettingGroupOut",
    "SettingOut",
    "SettingUpdateIn",
    "SettingsImportIn",
    "SystemInfoOut",
    "TrainingSummaryOut",
    "UserAdminOut",
    "UserCreateIn",
    "UserUpdateIn",
]
