"""
System administration endpoints.

Users and roles, the audit trail (including chain verification), notifications,
settings, backup/restore, the training centre, the i18n catalogue and a system
information summary.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.core.config import settings
from app.core.db import engine
from app.core.deps import Ctx, Page, get_page, paginated, require
from app.core.enums import AuditAction
from app.core.exceptions import NotFoundError, ValidationError
from app.core.i18n import catalogue, normalize_language, t
from app.core.permissions import RESOURCES, ROLE_BY_CODE, role_permissions
from app.models.auth import Role, User
from app.models.system import AuditLog, BackupRecord
from app.models.vehicle import Salesperson
from app.schemas.common import Message
from app.schemas.system import (
    AuditLogOut,
    AuditVerifyOut,
    BackupCreateIn,
    BackupOut,
    HealthOut,
    LessonOut,
    LessonProgressIn,
    NotificationBroadcastIn,
    NotificationOut,
    NotificationSummaryOut,
    PasswordResetIn,
    PermissionOverrideIn,
    RoleOut,
    RolePermissionMatrixOut,
    SettingBulkUpdateIn,
    SettingGroupOut,
    SettingsImportIn,
    SystemInfoOut,
    TrainingSummaryOut,
    UserAdminOut,
    UserCreateIn,
    UserUpdateIn,
)
from app.services import (
    audit_service,
    auth_service,
    backup_service,
    health_service,
    notification_service,
    setting_service,
    training_service,
)
from app.core.utils import loads

router = APIRouter(prefix="/system", tags=["system"])


# ===========================================================================
# Users
# ===========================================================================
def _user_out(db, user: User) -> UserAdminOut:
    sp_id = db.execute(
        select(Salesperson.id).where(Salesperson.user_id == user.id)
    ).scalar_one_or_none()
    return UserAdminOut(
        id=user.id,
        username=user.username,
        full_name=user.full_name,
        email=user.email,
        phone=user.phone,
        role_id=user.role_id,
        role_code=user.role.code if user.role else None,
        role_name=user.role.name_tr if user.role else None,
        role_rank=user.role.rank if user.role else None,
        region_id=user.region_id,
        company_id=user.company_id,
        manager_id=user.manager_id,
        status=user.status,
        language=user.language,
        data_scope=auth_service.effective_scope(user),
        must_change_password=user.must_change_password,
        is_deleted=user.is_deleted,
        last_login_at=user.last_login_at,
        last_login_ip=user.last_login_ip,
        failed_login_count=user.failed_login_count,
        locked_until=user.locked_until,
        salesperson_id=sp_id,
        permission_count=len(auth_service.effective_permissions(user)),
        created_at=user.created_at,
    )


@router.get("/users", summary="List users")
def list_users(
    ctx: Ctx = Depends(require("system.users", "VIEW")),
    page: Page = Depends(get_page),
    term: str | None = Query(default=None),
    role_code: str | None = Query(default=None),
    status: str | None = Query(default=None),
    include_deleted: bool = Query(default=False),
) -> dict[str, Any]:
    conds: list[Any] = []
    if not include_deleted:
        conds.append(User.is_deleted.is_(False))
    if term:
        like = f"%{term.strip().lower()}%"
        conds.append(
            func.lower(User.username).like(like) | func.lower(User.full_name).like(like)
        )
    if status:
        conds.append(User.status == status)
    if role_code:
        role = ctx.db.execute(select(Role).where(Role.code == role_code)).scalar_one_or_none()
        conds.append(User.role_id == (role.id if role else -1))

    total = int(
        ctx.db.execute(select(func.count(User.id)).where(*conds)).scalar_one() or 0
    )
    rows = (
        ctx.db.execute(
            select(User)
            .where(*conds)
            .order_by(User.full_name)
            .offset(page.offset)
            .limit(page.limit)
        )
        .scalars()
        .all()
    )
    return paginated([_user_out(ctx.db, u) for u in rows], total, page)


@router.get("/users/{user_id}", response_model=UserAdminOut, summary="Get a user")
def get_user(user_id: int, ctx: Ctx = Depends(require("system.users", "VIEW"))) -> UserAdminOut:
    user = ctx.db.get(User, user_id)
    if user is None:
        raise NotFoundError("user.not_found", params={"id": user_id})
    return _user_out(ctx.db, user)


@router.post("/users", response_model=UserAdminOut, summary="Create a user")
def create_user(
    payload: UserCreateIn, ctx: Ctx = Depends(require("system.users", "CREATE"))
) -> UserAdminOut:
    user = auth_service.create_user(
        ctx.db,
        username=payload.username,
        password=payload.password,
        full_name=payload.full_name,
        role_code=payload.role_code,
        email=payload.email,
        phone=payload.phone,
        region_id=payload.region_id,
        company_id=payload.company_id,
        language=payload.language,
        actor=ctx.user,
    )
    return _user_out(ctx.db, user)


@router.put("/users/{user_id}", response_model=UserAdminOut, summary="Update a user")
def update_user(
    user_id: int,
    payload: UserUpdateIn,
    ctx: Ctx = Depends(require("system.users", "UPDATE")),
) -> UserAdminOut:
    user = ctx.db.get(User, user_id)
    if user is None:
        raise NotFoundError("user.not_found", params={"id": user_id})

    # A lower-ranked administrator must not be able to edit a higher-ranked one.
    if ctx.user.role and user.role and user.role.rank < ctx.user.role.rank:
        from app.core.exceptions import PermissionDeniedError

        raise PermissionDeniedError("auth.cannot_modify_higher_role")

    before = {
        "full_name": user.full_name,
        "role": user.role.code if user.role else None,
        "status": user.status,
    }

    if payload.full_name is not None:
        user.full_name = payload.full_name
    if payload.email is not None:
        user.email = payload.email or None
    if payload.phone is not None:
        user.phone = payload.phone or None
    if payload.region_id is not None:
        user.region_id = payload.region_id
    if payload.manager_id is not None:
        user.manager_id = payload.manager_id
    if payload.status is not None:
        user.status = payload.status
        if payload.status == "ACTIVE":
            user.locked_until = None
            user.failed_login_count = 0
    if payload.language is not None:
        user.language = payload.language
    if payload.role_code is not None:
        role = ctx.db.execute(
            select(Role).where(Role.code == payload.role_code)
        ).scalar_one_or_none()
        if role is None:
            raise NotFoundError("role.not_found", params={"code": payload.role_code})
        if ctx.user.role and role.rank < ctx.user.role.rank:
            from app.core.exceptions import PermissionDeniedError

            raise PermissionDeniedError("auth.cannot_grant_higher_role")
        user.role_id = role.id

    user.updated_by_id = ctx.user.id
    audit_service.record(
        ctx.db,
        AuditAction.UPDATE,
        entity_type="User",
        entity_id=user.id,
        entity_label=user.username,
        summary="user updated",
        old_values=before,
        new_values=payload.model_dump(exclude_none=True),
        **ctx.audit_kwargs(),
    )
    ctx.db.commit()
    ctx.db.refresh(user)
    return _user_out(ctx.db, user)


@router.delete("/users/{user_id}", response_model=Message, summary="Deactivate a user")
def delete_user(
    user_id: int, ctx: Ctx = Depends(require("system.users", "DELETE"))
) -> Message:
    user = ctx.db.get(User, user_id)
    if user is None:
        raise NotFoundError("user.not_found", params={"id": user_id})
    if user.id == ctx.user.id:
        raise ValidationError("user.cannot_delete_self")
    if ctx.user.role and user.role and user.role.rank < ctx.user.role.rank:
        from app.core.exceptions import PermissionDeniedError

        raise PermissionDeniedError("auth.cannot_modify_higher_role")

    from app.models.base import utcnow

    user.is_deleted = True
    user.deleted_at = utcnow()
    user.deleted_by_id = ctx.user.id
    user.status = "INACTIVE"
    audit_service.record(
        ctx.db,
        AuditAction.DELETE,
        entity_type="User",
        entity_id=user.id,
        entity_label=user.username,
        summary="user deactivated",
        **ctx.audit_kwargs(),
    )
    ctx.db.commit()
    return Message(message=t("common.deleted", ctx.lang), message_key="common.deleted")


@router.post(
    "/users/{user_id}/reset-password", response_model=Message, summary="Reset a password"
)
def reset_password(
    user_id: int,
    payload: PasswordResetIn,
    ctx: Ctx = Depends(require("system.users", "UPDATE")),
) -> Message:
    user = ctx.db.get(User, user_id)
    if user is None:
        raise NotFoundError("user.not_found", params={"id": user_id})
    auth_service.reset_password(ctx.db, user, payload.new_password, actor=ctx.user)
    return Message(message=t("common.updated", ctx.lang), message_key="common.updated")


@router.put(
    "/users/{user_id}/permissions", response_model=UserAdminOut, summary="Override permissions"
)
def set_permissions(
    user_id: int,
    payload: PermissionOverrideIn,
    ctx: Ctx = Depends(require("system.users", "UPDATE")),
) -> UserAdminOut:
    user = ctx.db.get(User, user_id)
    if user is None:
        raise NotFoundError("user.not_found", params={"id": user_id})
    auth_service.set_permission_overrides(
        ctx.db,
        user,
        grant=payload.grant,
        revoke=payload.revoke,
        data_scope=payload.data_scope,
        actor=ctx.user,
    )
    return _user_out(ctx.db, user)


@router.get(
    "/users/{user_id}/permissions", summary="Effective permissions for a user"
)
def get_permissions(
    user_id: int, ctx: Ctx = Depends(require("system.users", "VIEW"))
) -> dict[str, Any]:
    user = ctx.db.get(User, user_id)
    if user is None:
        raise NotFoundError("user.not_found", params={"id": user_id})
    role_code = user.role.code if user.role else ""
    overrides = loads(user.permission_overrides, {}) or {}
    return {
        "role": role_code,
        "role_permissions": sorted(role_permissions(role_code)),
        "effective": sorted(auth_service.effective_permissions(user)),
        "grant": overrides.get("grant", []),
        "revoke": overrides.get("revoke", []),
        "data_scope": auth_service.effective_scope(user),
    }


# ===========================================================================
# Roles
# ===========================================================================
@router.get("/roles", response_model=list[RoleOut], summary="List roles")
def list_roles(ctx: Ctx = Depends(require("system.roles", "VIEW"))) -> list[RoleOut]:
    counts = dict(
        ctx.db.execute(
            select(User.role_id, func.count(User.id))
            .where(User.is_deleted.is_(False))
            .group_by(User.role_id)
        ).all()
    )
    rows = ctx.db.execute(select(Role).order_by(Role.rank)).scalars().all()
    out: list[RoleOut] = []
    for r in rows:
        out.append(
            RoleOut(
                code=r.code,
                name=r.name_en if ctx.lang == "en" else r.name_tr,
                name_tr=r.name_tr,
                name_en=r.name_en,
                rank=r.rank,
                data_scope=r.data_scope,
                permission_count=len(role_permissions(r.code)),
                user_count=int(counts.get(r.id, 0)),
                is_system=r.is_system,
            )
        )
    return out


@router.get(
    "/roles/matrix",
    response_model=RolePermissionMatrixOut,
    summary="Role × permission matrix",
)
def role_matrix(
    ctx: Ctx = Depends(require("system.roles", "VIEW")),
) -> RolePermissionMatrixOut:
    resources = [
        {
            "key": r.key,
            "module": r.module,
            "name": r.name_en if ctx.lang == "en" else r.name_tr,
            "name_tr": r.name_tr,
            "name_en": r.name_en,
            "actions": list(r.actions),
            "is_sensitive": r.sensitive,
        }
        for r in RESOURCES
    ]
    roles = list_roles(ctx)
    return RolePermissionMatrixOut(
        resources=resources,
        roles=roles,
        matrix={code: sorted(role_permissions(code)) for code in ROLE_BY_CODE},
    )


# ===========================================================================
# Audit
# ===========================================================================
@router.get("/audit", summary="Audit log")
def list_audit(
    ctx: Ctx = Depends(require("system.audit", "VIEW")),
    page: Page = Depends(get_page),
    action: str | None = Query(default=None),
    entity_type: str | None = Query(default=None),
    entity_id: int | None = Query(default=None),
    user_id: int | None = Query(default=None),
    is_ai_action: bool | None = Query(default=None),
    start: date | None = Query(default=None),
    end: date | None = Query(default=None),
) -> dict[str, Any]:
    conds: list[Any] = []
    if action:
        conds.append(AuditLog.action == action)
    if entity_type:
        conds.append(AuditLog.entity_type == entity_type)
    if entity_id is not None:
        conds.append(AuditLog.entity_id == entity_id)
    if user_id is not None:
        conds.append(AuditLog.user_id == user_id)
    if is_ai_action is not None:
        conds.append(AuditLog.is_ai_action.is_(is_ai_action))
    if start:
        conds.append(AuditLog.created_at >= datetime.combine(start, datetime.min.time()))
    if end:
        conds.append(AuditLog.created_at <= datetime.combine(end, datetime.max.time()))

    total = int(
        ctx.db.execute(select(func.count(AuditLog.id)).where(*conds)).scalar_one() or 0
    )
    rows = (
        ctx.db.execute(
            select(AuditLog)
            .where(*conds)
            .order_by(AuditLog.id.desc())
            .offset(page.offset)
            .limit(page.limit)
        )
        .scalars()
        .all()
    )
    items = []
    for r in rows:
        item = AuditLogOut.model_validate(r)
        item.old_values = loads(r.old_values, None)
        item.new_values = loads(r.new_values, None)
        items.append(item)
    return paginated(items, total, page)


@router.get(
    "/audit/verify", response_model=AuditVerifyOut, summary="Verify audit chain integrity"
)
def verify_audit(ctx: Ctx = Depends(require("system.audit", "VIEW"))) -> AuditVerifyOut:
    return AuditVerifyOut(**audit_service.verify_chain(ctx.db))


# ===========================================================================
# Notifications
# ===========================================================================
@router.get("/notifications", summary="My notifications")
def list_notifications(
    ctx: Ctx = Depends(require("system.notifications", "VIEW")),
    page: Page = Depends(get_page),
    unread_only: bool = Query(default=False),
    notification_type: str | None = Query(default=None),
    severity: str | None = Query(default=None),
) -> dict[str, Any]:
    rows, total = notification_service.list_for(
        ctx.db,
        ctx.user,
        unread_only=unread_only,
        notification_type=notification_type,
        severity=severity,
        page=page.page,
        size=page.size,
    )
    items = [
        NotificationOut(**notification_service.as_dict(r, ctx.lang)) for r in rows
    ]
    return paginated(items, total, page)


@router.get(
    "/notifications/summary",
    response_model=NotificationSummaryOut,
    summary="Unread notification count",
)
def notification_summary(
    ctx: Ctx = Depends(require("system.notifications", "VIEW")),
) -> NotificationSummaryOut:
    _, total = notification_service.list_for(ctx.db, ctx.user, page=1, size=1)
    return NotificationSummaryOut(
        unread=notification_service.unread_count(ctx.db, ctx.user), total=total
    )


@router.post(
    "/notifications/{notification_id}/read", response_model=Message, summary="Mark read"
)
def mark_read(
    notification_id: int, ctx: Ctx = Depends(require("system.notifications", "UPDATE"))
) -> Message:
    notification_service.mark_read(ctx.db, ctx.user, notification_id)
    return Message(message=t("common.updated", ctx.lang), message_key="common.updated")


@router.post("/notifications/read-all", response_model=Message, summary="Mark all read")
def mark_all_read(
    ctx: Ctx = Depends(require("system.notifications", "UPDATE")),
) -> Message:
    count = notification_service.mark_all_read(ctx.db, ctx.user)
    return Message(
        message=t("common.updated", ctx.lang),
        message_key="common.updated",
        data={"count": count},
    )


@router.delete(
    "/notifications/{notification_id}", response_model=Message, summary="Dismiss"
)
def dismiss_notification(
    notification_id: int, ctx: Ctx = Depends(require("system.notifications", "UPDATE"))
) -> Message:
    notification_service.dismiss(ctx.db, ctx.user, notification_id)
    return Message(message=t("common.updated", ctx.lang), message_key="common.updated")


@router.post("/notifications/check", summary="Run the alert rules now")
def run_notification_checks(
    ctx: Ctx = Depends(require("system.settings", "UPDATE")),
) -> dict[str, Any]:
    created = notification_service.run_checks(ctx.db)
    return {"created": len(created)}


@router.post(
    "/notifications/broadcast", response_model=Message, summary="Broadcast an announcement"
)
def broadcast(
    payload: NotificationBroadcastIn,
    ctx: Ctx = Depends(require("system.settings", "UPDATE")),
) -> Message:
    notification_service.broadcast(
        ctx.db,
        title_tr=payload.title_tr,
        title_en=payload.title_en,
        body_tr=payload.body_tr,
        body_en=payload.body_en,
        severity=payload.severity,
        role_code=payload.role_code,
        expires_at=payload.expires_at,
    )
    ctx.db.commit()
    return Message(message=t("common.created", ctx.lang), message_key="common.created")


# ===========================================================================
# Health
# ===========================================================================
@router.get("/health", response_model=HealthOut, summary="System health")
def system_health(
    ctx: Ctx = Depends(require("system.health", "VIEW")),
    refresh: bool = Query(default=True),
) -> HealthOut:
    return HealthOut(**health_service.summary(ctx.db, refresh=refresh))


# ===========================================================================
# Settings
# ===========================================================================
@router.get("/settings", response_model=list[SettingGroupOut], summary="All settings")
def list_settings(
    ctx: Ctx = Depends(require("system.settings", "VIEW")),
) -> list[SettingGroupOut]:
    return [SettingGroupOut(**g) for g in setting_service.all_grouped(ctx.db, ctx.lang)]


@router.put("/settings", response_model=list[SettingGroupOut], summary="Update settings")
def update_settings(
    payload: SettingBulkUpdateIn,
    ctx: Ctx = Depends(require("system.settings", "UPDATE")),
) -> list[SettingGroupOut]:
    setting_service.bulk_update(
        ctx.db,
        [i.model_dump() for i in payload.items],
        user=ctx.user,
    )
    ctx.db.commit()
    return [SettingGroupOut(**g) for g in setting_service.all_grouped(ctx.db, ctx.lang)]


@router.get("/settings/export", summary="Export settings")
def export_settings(ctx: Ctx = Depends(require("system.settings", "VIEW"))) -> dict[str, Any]:
    return backup_service.export_settings(ctx.db)


@router.post("/settings/import", response_model=Message, summary="Import settings")
def import_settings(
    payload: SettingsImportIn,
    ctx: Ctx = Depends(require("system.settings", "UPDATE")),
) -> Message:
    applied = backup_service.import_settings(ctx.db, payload.payload, user=ctx.user)
    ctx.db.commit()
    return Message(
        message=t("common.updated", ctx.lang),
        message_key="common.updated",
        data={"applied": applied},
    )


# ===========================================================================
# Backups
# ===========================================================================
@router.get("/backups", summary="List backups")
def list_backups(
    ctx: Ctx = Depends(require("system.backup", "VIEW")),
    page: Page = Depends(get_page),
    status: str | None = Query(default=None),
    backup_type: str | None = Query(default=None),
) -> dict[str, Any]:
    rows, total = backup_service.list_backups(
        ctx.db, status=status, backup_type=backup_type, page=page.page, size=page.size
    )
    return paginated([BackupOut.model_validate(r) for r in rows], total, page)


@router.post("/backups", response_model=BackupOut, summary="Create a backup now")
def create_backup(
    payload: BackupCreateIn, ctx: Ctx = Depends(require("system.backup", "CREATE"))
) -> BackupOut:
    record = backup_service.create_backup(
        ctx.db,
        backup_type=payload.backup_type,
        trigger="MANUAL",
        include_files=payload.include_files,
        user_id=ctx.user.id,
        username=ctx.user.username,
        notes=payload.notes,
    )
    return BackupOut.model_validate(record)


@router.post(
    "/backups/{backup_id}/verify", response_model=BackupOut, summary="Verify a backup"
)
def verify_backup(
    backup_id: int, ctx: Ctx = Depends(require("system.backup", "EXECUTE"))
) -> BackupOut:
    record = ctx.db.get(BackupRecord, backup_id)
    if record is None:
        raise NotFoundError("backup.not_found", params={"id": backup_id})
    return BackupOut.model_validate(
        backup_service.verify_backup(ctx.db, record, user_id=ctx.user.id)
    )


@router.post(
    "/backups/{backup_id}/restore", response_model=BackupOut, summary="Restore a backup"
)
def restore_backup(
    backup_id: int,
    payload: dict[str, Any],
    ctx: Ctx = Depends(require("system.backup", "EXECUTE")),
) -> BackupOut:
    record = ctx.db.get(BackupRecord, backup_id)
    if record is None:
        raise NotFoundError("backup.not_found", params={"id": backup_id})
    return BackupOut.model_validate(
        backup_service.restore_backup(
            ctx.db,
            record,
            user_id=ctx.user.id,
            username=ctx.user.username,
            confirm=bool(payload.get("confirm")),
        )
    )


@router.delete("/backups/{backup_id}", response_model=Message, summary="Delete a backup")
def delete_backup(
    backup_id: int, ctx: Ctx = Depends(require("system.backup", "EXECUTE"))
) -> Message:
    record = ctx.db.get(BackupRecord, backup_id)
    if record is None:
        raise NotFoundError("backup.not_found", params={"id": backup_id})
    backup_service.delete_backup(
        ctx.db, record, user_id=ctx.user.id, username=ctx.user.username
    )
    ctx.db.delete(record)
    ctx.db.commit()
    return Message(message=t("common.deleted", ctx.lang), message_key="common.deleted")


# ===========================================================================
# Training centre
# ===========================================================================
@router.get("/training/lessons", response_model=list[LessonOut], summary="Lessons")
def list_lessons(
    ctx: Ctx = Depends(require("system.training", "VIEW")),
    module: str | None = Query(default=None),
) -> list[LessonOut]:
    rows = training_service.list_lessons(
        ctx.db, user=ctx.user, lang=ctx.lang, module=module
    )
    return [LessonOut(**r) for r in rows]


@router.get(
    "/training/summary", response_model=TrainingSummaryOut, summary="My training progress"
)
def training_summary(
    ctx: Ctx = Depends(require("system.training", "VIEW")),
) -> TrainingSummaryOut:
    return TrainingSummaryOut(**training_service.progress_summary(ctx.db, ctx.user.id))


@router.get(
    "/training/lessons/{identifier}", response_model=LessonOut, summary="One lesson"
)
def get_lesson(
    identifier: str, ctx: Ctx = Depends(require("system.training", "VIEW"))
) -> LessonOut:
    key: int | str = int(identifier) if identifier.isdigit() else identifier
    return LessonOut(
        **training_service.get_lesson(ctx.db, key, user=ctx.user, lang=ctx.lang)
    )


@router.post(
    "/training/lessons/{lesson_id}/progress",
    response_model=Message,
    summary="Record lesson progress",
)
def set_progress(
    lesson_id: int,
    payload: LessonProgressIn,
    ctx: Ctx = Depends(require("system.training", "VIEW")),
) -> Message:
    training_service.mark_progress(
        ctx.db,
        user_id=ctx.user.id,
        lesson_id=lesson_id,
        last_step=payload.last_step,
        progress_percent=payload.progress_percent,
        is_completed=payload.is_completed,
        score=payload.score,
    )
    ctx.db.commit()
    return Message(message=t("common.saved", ctx.lang), message_key="common.saved")


# ===========================================================================
# i18n + info
# ===========================================================================
@router.get("/i18n/{lang}", summary="Message catalogue for the frontend")
def get_catalogue(lang: str) -> dict[str, str]:
    """Public on purpose — the login screen needs translations before sign-in."""
    return catalogue(normalize_language(lang))


@router.get("/info", response_model=SystemInfoOut, summary="System information")
def system_info(ctx: Ctx = Depends(require("system.health", "VIEW"))) -> SystemInfoOut:
    from app.api.v1 import REGISTERED
    from app.models.ai import AIProviderConfig
    from app.models.customer import Customer
    from app.models.product import Product
    from app.models.sales import Sale
    from app.models.vehicle import Vehicle
    from app.models.warehouse import Warehouse

    def count(model: Any) -> int:
        try:
            return int(ctx.db.execute(select(func.count(model.id))).scalar_one() or 0)
        except Exception:
            return 0

    providers = [
        {
            "provider": p.provider,
            "enabled": p.is_enabled,
            "configured": p.has_api_key,
            "healthy": p.is_healthy,
            "model": p.default_model,
        }
        for p in ctx.db.execute(select(AIProviderConfig)).scalars()
    ]

    return SystemInfoOut(
        app_name=settings.app_name,
        app_version=settings.app_version,
        environment=settings.env,
        default_language=settings.default_language,
        default_currency=settings.default_currency,
        timezone=settings.timezone,
        database_engine=engine.dialect.name,
        api_prefix=settings.api_prefix,
        counts={
            "users": count(User),
            "customers": count(Customer),
            "products": count(Product),
            "warehouses": count(Warehouse),
            "vehicles": count(Vehicle),
            "sales": count(Sale),
            "audit_logs": count(AuditLog),
        },
        modules=list(REGISTERED),
        ai_providers=providers,
    )
