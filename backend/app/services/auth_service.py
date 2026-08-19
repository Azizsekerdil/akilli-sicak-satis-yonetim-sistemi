"""
Authentication and authorisation service.

Handles login (with lockout), token issue/refresh/revoke, permission
resolution (role grants + per-user overrides) and data-scope computation.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.enums import AuditAction, DataScope, RoleCode, UserStatus
from app.core.exceptions import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.core.logging_config import get_logger
from app.core.permissions import (
    RESOURCE_BY_KEY,
    ROLE_BY_CODE,
    accessible_modules,
    role_permissions,
)
from app.core.security import (
    TokenError,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    password_strength_errors,
    verify_password,
)
from app.core.utils import dumps, is_loopback_address, loads
from app.models.auth import LoginAttempt, Role, User, UserSession
from app.models.base import utcnow
from app.models.vehicle import Salesperson
from app.services import audit_service

log = get_logger("app.auth")


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------
def _record_attempt(
    db: Session,
    username: str,
    *,
    ok: bool,
    user_id: int | None = None,
    ip: str | None = None,
    ua: str | None = None,
    reason: str | None = None,
) -> None:
    db.add(
        LoginAttempt(
            username=username[:64],
            user_id=user_id,
            successful=ok,
            ip_address=ip,
            user_agent=(ua or "")[:512] or None,
            failure_reason=reason,
            attempted_at=utcnow(),
        )
    )


def authenticate(
    db: Session,
    username: str,
    password: str,
    *,
    ip: str | None = None,
    peer_ip: str | None = None,
    user_agent: str | None = None,
) -> User:
    """
    Verify credentials.

    Raises :class:`AuthenticationError` with an i18n key.  The same key is used
    for "unknown user" and "wrong password" so the endpoint cannot be used to
    enumerate accounts.

    While an account still carries the first-run bootstrap credential it is
    accepted **only from the local device** — see
    :data:`app.models.auth.User.is_bootstrap_credential`.  The check runs after
    the password has been verified, so a remote attacker learns nothing from
    the difference between "wrong password" and "right password, wrong
    network".

    ``ip`` and ``peer_ip`` are deliberately separate:

    * ``ip`` is the best guess at who the human is, honouring
      ``X-Forwarded-For``.  It is what the audit trail records.
    * ``peer_ip`` is the address of the socket that actually connected.  It is
      the **only** input to the local-device decision, because a forwarded
      header is written by the client and a caller who could set it would
      otherwise defeat the gate by claiming to be ``127.0.0.1``.

    When ``peer_ip`` is not supplied the gate falls back to ``ip``; callers
    inside the process (tests, the installer) pass it explicitly.
    """
    uname = (username or "").strip()
    user = db.execute(
        select(User).where(func.lower(User.username) == uname.lower())
    ).scalar_one_or_none()

    if user is None:
        _record_attempt(db, uname, ok=False, ip=ip, ua=user_agent, reason="unknown_user")
        db.commit()
        raise AuthenticationError("auth.invalid_credentials")

    if user.is_deleted or user.status in (UserStatus.INACTIVE, UserStatus.SUSPENDED):
        _record_attempt(db, uname, ok=False, user_id=user.id, ip=ip, ua=user_agent, reason="inactive")
        db.commit()
        raise AuthenticationError("auth.account_inactive")

    if user.is_locked:
        _record_attempt(db, uname, ok=False, user_id=user.id, ip=ip, ua=user_agent, reason="locked")
        db.commit()
        raise AuthenticationError(
            "auth.account_locked",
            params={"until": user.locked_until.isoformat() if user.locked_until else ""},
        )

    if not verify_password(password, user.password_hash):
        user.failed_login_count += 1
        reason = "bad_password"
        if user.failed_login_count >= settings.max_login_attempts:
            user.locked_until = utcnow() + timedelta(minutes=settings.lockout_minutes)
            user.status = UserStatus.LOCKED
            reason = "locked_out"
            log.warning("Account locked after %d failures: %s", user.failed_login_count, uname)
        _record_attempt(db, uname, ok=False, user_id=user.id, ip=ip, ua=user_agent, reason=reason)
        audit_service.record(
            db, AuditAction.LOGIN_FAILED, user_id=user.id, username=uname,
            ip_address=ip, user_agent=user_agent, summary=reason,
        )
        db.commit()
        raise AuthenticationError("auth.invalid_credentials")

    # The password is right.  If this is still the first-run credential, the
    # connection must originate on the machine the server runs on.  Note the
    # socket peer, never the forwarded header — see the docstring.
    origin = peer_ip if peer_ip is not None else ip
    if user.is_bootstrap_credential and not is_loopback_address(origin):
        _record_attempt(
            db, uname, ok=False, user_id=user.id, ip=ip, ua=user_agent,
            reason="bootstrap_remote_refused",
        )
        audit_service.record(
            db, AuditAction.LOGIN_FAILED, user_id=user.id, username=uname,
            ip_address=ip, user_agent=user_agent,
            summary="bootstrap credential refused from a remote address",
        )
        log.warning(
            "Bootstrap credential refused from non-loopback address (user=%s)", uname
        )
        db.commit()
        raise AuthenticationError("auth.bootstrap_local_only", status_code=403)

    # Success
    user.failed_login_count = 0
    user.locked_until = None
    if user.status == UserStatus.LOCKED:
        user.status = UserStatus.ACTIVE
    user.last_login_at = utcnow()
    user.last_login_ip = ip
    _record_attempt(db, uname, ok=True, user_id=user.id, ip=ip, ua=user_agent)
    audit_service.record(
        db, AuditAction.LOGIN, entity_type="User", entity_id=user.id,
        user_id=user.id, username=user.username, role_code=user.role.code if user.role else None,
        ip_address=ip, user_agent=user_agent, summary="login_ok",
    )
    db.commit()
    return user


def issue_tokens(
    db: Session,
    user: User,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
    device_label: str | None = None,
) -> dict[str, Any]:
    """Create an access/refresh pair and persist the revocable session."""
    role_code = user.role.code if user.role else ""
    access = create_access_token(user.id, role=role_code, scope=effective_scope(user))
    refresh = create_refresh_token(user.id)
    payload = decode_token(refresh, expected_type="refresh")

    db.add(
        UserSession(
            user_id=user.id,
            token_id=payload["jti"],
            ip_address=ip,
            user_agent=(user_agent or "")[:512] or None,
            device_label=device_label,
            expires_at=utcnow() + timedelta(days=settings.refresh_token_days),
            is_active=True,
            last_seen_at=utcnow(),
        )
    )
    db.commit()
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "bearer",
        "expires_in": settings.access_token_minutes * 60,
    }


def refresh_tokens(db: Session, refresh_token: str) -> dict[str, Any]:
    """Exchange a valid, non-revoked refresh token for a new pair (rotation)."""
    try:
        payload = decode_token(refresh_token, expected_type="refresh")
    except TokenError as exc:
        raise AuthenticationError(str(exc)) from exc

    session = db.execute(
        select(UserSession).where(UserSession.token_id == payload["jti"])
    ).scalar_one_or_none()
    if session is None or not session.is_active or session.expires_at <= utcnow():
        raise AuthenticationError("auth.session_expired")

    user = db.get(User, int(payload["sub"]))
    if user is None or not user.is_usable:
        raise AuthenticationError("auth.account_inactive")

    # Rotate: the old session is retired as the new one is created.
    session.is_active = False
    session.revoked_at = utcnow()
    db.flush()
    return issue_tokens(db, user, ip=session.ip_address, user_agent=session.user_agent)


def revoke_session(db: Session, token_id: str) -> None:
    session = db.execute(
        select(UserSession).where(UserSession.token_id == token_id)
    ).scalar_one_or_none()
    if session:
        session.is_active = False
        session.revoked_at = utcnow()
        db.commit()


def logout(db: Session, user: User, refresh_token: str | None = None) -> None:
    if refresh_token:
        try:
            revoke_session(db, decode_token(refresh_token, expected_type="refresh")["jti"])
        except TokenError:
            pass
    else:
        active = db.execute(
            select(UserSession).where(
                UserSession.user_id == user.id, UserSession.is_active.is_(True)
            )
        ).scalars().all()
        for s in active:
            s.is_active = False
            s.revoked_at = utcnow()
    audit_service.record(
        db, AuditAction.LOGOUT, user_id=user.id, username=user.username, summary="logout"
    )
    db.commit()


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------
def effective_permissions(user: User) -> set[str]:
    """
    Role grants, plus per-user ``grant`` additions, minus ``revoke`` removals.

    Overrides are stored as JSON on the user:
    ``{"grant": ["stock.counts:APPROVE"], "revoke": ["crm.customers:DELETE"]}``
    """
    role_code = user.role.code if user.role else ""
    perms = set(role_permissions(role_code))

    overrides = loads(user.permission_overrides, {}) or {}
    for code in overrides.get("grant", []) or []:
        if ":" in code and code.split(":")[0] in RESOURCE_BY_KEY:
            perms.add(code)
    for code in overrides.get("revoke", []) or []:
        perms.discard(code)
    return perms


def has_permission(user: User, resource: str, action: str) -> bool:
    return f"{resource}:{action}" in effective_permissions(user)


def require_permission(user: User, resource: str, action: str) -> None:
    if not has_permission(user, resource, action):
        raise PermissionDeniedError(
            "auth.permission_denied", params={"resource": resource, "action": action}
        )


def effective_scope(user: User) -> str:
    """Data scope for the user, taking the role default unless overridden."""
    overrides = loads(user.permission_overrides, {}) or {}
    scope = overrides.get("data_scope")
    if scope in {s.value for s in DataScope}:
        return scope
    return user.role.data_scope if user.role else DataScope.NONE


def scope_context(db: Session, user: User) -> dict[str, Any]:
    """
    Everything a query needs to filter rows for this user.

    ``salesperson_ids`` is empty for ALL scope (meaning: no restriction).
    """
    scope = effective_scope(user)
    ctx: dict[str, Any] = {
        "scope": scope,
        "user_id": user.id,
        "region_ids": [],
        "salesperson_ids": [],
        "unrestricted": scope == DataScope.ALL,
    }
    if scope == DataScope.ALL:
        return ctx

    own = db.execute(
        select(Salesperson.id).where(Salesperson.user_id == user.id)
    ).scalars().all()

    if scope == DataScope.REGION and user.region_id:
        ctx["region_ids"] = [user.region_id]
        ctx["salesperson_ids"] = list(
            db.execute(
                select(Salesperson.id).where(Salesperson.region_id == user.region_id)
            ).scalars().all()
        )
    elif scope == DataScope.TEAM:
        team = list(own)
        if own:
            team += list(
                db.execute(
                    select(Salesperson.id).where(Salesperson.supervisor_id.in_(own))
                ).scalars().all()
            )
        ctx["salesperson_ids"] = sorted(set(team))
    elif scope == DataScope.OWN:
        ctx["salesperson_ids"] = list(own)
    else:  # NONE
        ctx["salesperson_ids"] = [-1]
    return ctx


def menu_for(user: User) -> dict[str, Any]:
    """Permissions + modules + scope, shipped to the frontend after login."""
    perms = sorted(effective_permissions(user))
    return {
        "permissions": perms,
        "modules": sorted(accessible_modules(user.role.code if user.role else "")),
        "resources": sorted({p.split(":")[0] for p in perms}),
        "data_scope": effective_scope(user),
        "role": user.role.code if user.role else None,
        "role_rank": user.role.rank if user.role else 999,
    }


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------
def create_user(
    db: Session,
    *,
    username: str,
    password: str,
    full_name: str,
    role_code: str,
    email: str | None = None,
    phone: str | None = None,
    region_id: int | None = None,
    company_id: int | None = None,
    language: str = "tr",
    actor: User | None = None,
) -> User:
    username = (username or "").strip()
    if not username:
        raise ValidationError("user.username_required")

    errors = password_strength_errors(password)
    if errors:
        raise ValidationError("password.policy_failed", params={"rules": ",".join(errors)})

    exists = db.execute(
        select(User.id).where(func.lower(User.username) == username.lower())
    ).scalar_one_or_none()
    if exists:
        raise ConflictError("user.username_taken", params={"username": username})

    role = db.execute(select(Role).where(Role.code == role_code)).scalar_one_or_none()
    if role is None:
        raise NotFoundError("role.not_found", params={"code": role_code})

    # Privilege-escalation guard: you cannot create someone above your own rank.
    if actor is not None and actor.role is not None and role.rank < actor.role.rank:
        raise PermissionDeniedError("auth.cannot_grant_higher_role")

    user = User(
        username=username,
        password_hash=hash_password(password),
        full_name=full_name.strip(),
        email=(email or "").strip() or None,
        phone=phone,
        role_id=role.id,
        region_id=region_id,
        company_id=company_id,
        language=language,
        status=UserStatus.ACTIVE,
        password_changed_at=utcnow(),
        created_by_id=actor.id if actor else None,
    )
    db.add(user)
    db.flush()
    audit_service.record(
        db, AuditAction.CREATE, entity_type="User", entity_id=user.id,
        entity_label=user.username,
        user_id=actor.id if actor else None,
        username=actor.username if actor else None,
        summary=f"user created role={role.code}",
        new_values={"username": username, "role": role.code, "full_name": full_name},
    )
    db.commit()
    return user


def change_password(
    db: Session, user: User, old_password: str, new_password: str, *, actor: User | None = None
) -> None:
    if not verify_password(old_password, user.password_hash):
        raise AuthenticationError("auth.wrong_current_password")
    errors = password_strength_errors(new_password)
    if errors:
        raise ValidationError("password.policy_failed", params={"rules": ",".join(errors)})
    if verify_password(new_password, user.password_hash):
        raise ValidationError("password.must_differ")

    user.password_hash = hash_password(new_password)
    user.password_changed_at = utcnow()
    user.must_change_password = False
    #: One-way.  Nothing sets this back to True, so the first-run credential
    #: cannot be resurrected by an administrative reset or by re-running the
    #: bootstrap.
    user.is_bootstrap_credential = False
    audit_service.record(
        db, AuditAction.UPDATE, entity_type="User", entity_id=user.id,
        entity_label=user.username, user_id=(actor or user).id,
        username=(actor or user).username, summary="password changed",
    )
    db.commit()


def reset_password(db: Session, target: User, new_password: str, *, actor: User) -> None:
    """
    Administrative reset — forces a change at next login.

    A reset never restores first-run status: ``is_bootstrap_credential`` is
    left alone (it is only ever cleared, in :func:`change_password`), so an
    account that has already been through a password change cannot be talked
    back into local-only bootstrap privileges, and the first-run password
    cannot be reinstated by resetting.
    """
    if actor.role and target.role and target.role.rank < actor.role.rank:
        raise PermissionDeniedError("auth.cannot_modify_higher_role")
    errors = password_strength_errors(new_password)
    if errors:
        raise ValidationError("password.policy_failed", params={"rules": ",".join(errors)})

    target.password_hash = hash_password(new_password)
    target.password_changed_at = utcnow()
    target.must_change_password = True
    target.failed_login_count = 0
    target.locked_until = None
    if target.status == UserStatus.LOCKED:
        target.status = UserStatus.ACTIVE
    audit_service.record(
        db, AuditAction.PERMISSION_CHANGE, entity_type="User", entity_id=target.id,
        entity_label=target.username, user_id=actor.id, username=actor.username,
        summary="password reset by admin",
    )
    db.commit()


def set_permission_overrides(
    db: Session, target: User, *, grant: list[str], revoke: list[str], data_scope: str | None, actor: User
) -> User:
    if actor.role and target.role and target.role.rank < actor.role.rank:
        raise PermissionDeniedError("auth.cannot_modify_higher_role")

    actor_perms = effective_permissions(actor)
    for code in grant:
        if code not in actor_perms:
            raise PermissionDeniedError("auth.cannot_grant_unheld", params={"permission": code})

    old = target.permission_overrides
    payload: dict[str, Any] = {"grant": sorted(set(grant)), "revoke": sorted(set(revoke))}
    if data_scope:
        payload["data_scope"] = data_scope
    target.permission_overrides = dumps(payload)

    audit_service.record(
        db, AuditAction.PERMISSION_CHANGE, entity_type="User", entity_id=target.id,
        entity_label=target.username, user_id=actor.id, username=actor.username,
        summary="permission overrides updated",
        old_values={"overrides": old}, new_values=payload,
    )
    db.commit()
    return target


def is_admin(user: User) -> bool:
    return bool(
        user.role
        and user.role.code in (RoleCode.SYSTEM_ADMIN, RoleCode.COMPANY_OWNER)
    )


def role_catalogue() -> list[dict[str, Any]]:
    return [
        {
            "code": r.code,
            "name_tr": r.name_tr,
            "name_en": r.name_en,
            "rank": r.rank,
            "data_scope": r.scope,
            "permission_count": len(role_permissions(r.code)),
        }
        for r in ROLE_BY_CODE.values()
    ]
