"""Authentication endpoints: login, refresh, logout, profile, password."""

from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import select

from app.core.deps import Context, CurrentUser, DbSession, Lang
from app.core.i18n import t
from app.core.permissions import RESOURCES, permission_code
from app.models.vehicle import Salesperson
from app.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    LoginResponse,
    LogoutRequest,
    PermissionCatalogItem,
    RefreshRequest,
    SessionInfo,
    TokenResponse,
    UserOut,
    UserSessionOut,
)
from app.schemas.common import Message
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


def _client_ip(request: Request) -> str | None:
    """Best guess at the human's address — honours ``X-Forwarded-For``."""
    fwd = request.headers.get("X-Forwarded-For")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


def _peer_ip(request: Request) -> str | None:
    """
    The address of the socket that actually connected.

    Never derived from a header.  A security decision that asks "is this the
    local device?" must use this and not :func:`_client_ip`:
    ``X-Forwarded-For`` is written by the caller, so trusting it there would
    let anyone claim to be ``127.0.0.1`` and walk through the first-run gate.
    """
    return request.client.host if request.client else None


def _session_info(db, user, lang: str) -> SessionInfo:
    menu = auth_service.menu_for(user)
    sp_id = db.execute(
        select(Salesperson.id).where(Salesperson.user_id == user.id)
    ).scalar_one_or_none()
    return SessionInfo(
        user=UserOut.model_validate(user),
        permissions=menu["permissions"],
        modules=menu["modules"],
        resources=menu["resources"],
        data_scope=menu["data_scope"],
        role=menu["role"],
        role_rank=menu["role_rank"],
        salesperson_id=sp_id,
        language=user.language or lang,
    )


@router.post("/login", response_model=LoginResponse, summary="Sign in / Giriş yap")
def login(
    payload: LoginRequest,
    request: Request,
    db: DbSession,
    lang: Lang,
) -> LoginResponse:
    user = auth_service.authenticate(
        db,
        payload.username,
        payload.password,
        ip=_client_ip(request),
        peer_ip=_peer_ip(request),
        user_agent=request.headers.get("User-Agent"),
    )
    tokens = auth_service.issue_tokens(
        db,
        user,
        ip=_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
        device_label=payload.device_label,
    )
    return LoginResponse(
        tokens=TokenResponse(**tokens),
        session=_session_info(db, user, lang),
    )


@router.post("/refresh", response_model=TokenResponse, summary="Rotate tokens")
def refresh(payload: RefreshRequest, db: DbSession) -> TokenResponse:
    return TokenResponse(**auth_service.refresh_tokens(db, payload.refresh_token))


@router.post("/logout", response_model=Message, summary="Sign out / Çıkış yap")
def logout(
    payload: LogoutRequest,
    db: DbSession,
    user: CurrentUser,
    lang: Lang,
) -> Message:
    auth_service.logout(db, user, payload.refresh_token)
    return Message(message=t("auth.logout_success", lang), message_key="auth.logout_success")


@router.get("/me", response_model=SessionInfo, summary="Current session")
def me(db: DbSession, user: CurrentUser, lang: Lang) -> SessionInfo:
    return _session_info(db, user, lang)


@router.post("/change-password", response_model=Message, summary="Change own password")
def change_password(
    payload: ChangePasswordRequest,
    db: DbSession,
    user: CurrentUser,
    lang: Lang,
) -> Message:
    auth_service.change_password(db, user, payload.old_password, payload.new_password)
    return Message(message=t("common.updated", lang), message_key="common.updated")


@router.get("/sessions", response_model=list[UserSessionOut], summary="My active sessions")
def my_sessions(db: DbSession, user: CurrentUser) -> list[UserSessionOut]:
    from app.models.auth import UserSession

    rows = db.execute(
        select(UserSession)
        .where(UserSession.user_id == user.id)
        .order_by(UserSession.created_at.desc())
        .limit(50)
    ).scalars().all()
    return [UserSessionOut.model_validate(r) for r in rows]


@router.delete("/sessions/{token_id}", response_model=Message, summary="Revoke a session")
def revoke(token_id: str, db: DbSession, user: CurrentUser, lang: Lang) -> Message:
    from app.models.auth import UserSession

    row = db.execute(
        select(UserSession).where(
            UserSession.token_id == token_id, UserSession.user_id == user.id
        )
    ).scalar_one_or_none()
    if row:
        auth_service.revoke_session(db, token_id)
    return Message(message=t("common.updated", lang), message_key="common.updated")


@router.get(
    "/permissions/catalog",
    response_model=list[PermissionCatalogItem],
    summary="Full permission catalogue",
)
def permission_catalog(ctx: Context) -> list[PermissionCatalogItem]:
    """Every permission the system defines — used by the role editor."""
    ctx.check("system.roles", "VIEW")
    return [
        PermissionCatalogItem(
            code=permission_code(r.key, a),
            module=r.module,
            resource=r.key,
            action=str(a),
            name_tr=f"{r.name_tr} — {a}",
            name_en=f"{r.name_en} — {a}",
            is_sensitive=r.sensitive,
        )
        for r in RESOURCES
        for a in r.actions
    ]
