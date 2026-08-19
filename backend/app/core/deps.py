"""
FastAPI dependencies: database session, current user, permission gates,
language resolution and data-scope injection.

Usage in a router::

    @router.get("/customers")
    def list_customers(
        ctx: Ctx = Depends(require("crm.customers", "VIEW")),
        db: Session = Depends(get_db),
    ):
        ...
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Callable

from fastapi import Depends, Header, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.enums import PermissionAction
from app.core.exceptions import AuthenticationError, PermissionDeniedError
from app.core.i18n import normalize_language
from app.core.security import TokenError, decode_token
from app.models.auth import User
from app.services import auth_service

bearer_scheme = HTTPBearer(auto_error=False)

DbSession = Annotated[Session, Depends(get_db)]


# ---------------------------------------------------------------------------
# Forced password change
# ---------------------------------------------------------------------------
#: The only paths an account carrying ``must_change_password`` may reach.
#:
#: Everything else — dashboard, customer and staff records, financial data,
#: AI/provider settings, export, backup, every administrative action — stays
#: closed until the password has actually been changed.
#:
#: The flag used to be advisory: the first-run bootstrap set it and the API
#: shipped it to the client, but no server-side check ever consulted it.  A
#: caller who ignored the web UI and spoke to the API directly kept complete
#: access with the initial password.  Enforcing it here is what makes the
#: control real rather than decorative.
#:
#: Matched as path suffixes so the gate does not depend on ``VS_API_PREFIX``.
PASSWORD_CHANGE_ALLOWED_SUFFIXES: tuple[str, ...] = (
    "/auth/change-password",  # the way out
    "/auth/me",               # so the client can see *why* it is blocked
    "/auth/logout",           # never trap someone inside a session
    "/auth/refresh",          # keep the blocked session alive while they type
)


def _password_change_path_allowed(path: str) -> bool:
    clean = path.rstrip("/")
    return any(clean.endswith(suffix) for suffix in PASSWORD_CHANGE_ALLOWED_SUFFIXES)


# ---------------------------------------------------------------------------
# Language
# ---------------------------------------------------------------------------
def get_language(
    lang: str | None = Query(default=None, description="tr | en"),
    accept_language: str | None = Header(default=None, alias="Accept-Language"),
) -> str:
    """Resolve the response language: ?lang= wins, then Accept-Language, then default."""
    return normalize_language(lang or accept_language)


Lang = Annotated[str, Depends(get_language)]


# ---------------------------------------------------------------------------
# Current user
# ---------------------------------------------------------------------------
def get_current_user(
    request: Request,
    db: DbSession,
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> User:
    """
    Resolve the caller from the bearer token, or raise 401.

    Also enforces the forced password change: an account flagged
    ``must_change_password`` gets 403 on everything outside
    :data:`PASSWORD_CHANGE_ALLOWED_SUFFIXES`.
    """
    if creds is None or not creds.credentials:
        raise AuthenticationError("auth.missing_token", status_code=401)
    try:
        payload = decode_token(creds.credentials, expected_type="access")
    except TokenError as exc:
        raise AuthenticationError(str(exc), status_code=401) from exc

    user = db.get(User, int(payload.get("sub", 0)))
    if user is None or not user.is_usable:
        raise AuthenticationError("auth.account_inactive", status_code=401)

    if user.must_change_password and not _password_change_path_allowed(request.url.path):
        raise PermissionDeniedError("auth.password_change_required")

    request.state.user = user
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def get_optional_user(
    request: Request,
    db: DbSession,
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> User | None:
    """
    Like :func:`get_current_user` but returns None instead of raising.

    An account still owing a password change resolves to None — anonymous, the
    least-privileged answer — rather than to the user, so an optional-auth
    handler cannot accidentally treat it as fully signed in.
    """
    try:
        return get_current_user(request, db, creds)
    except (AuthenticationError, PermissionDeniedError):
        return None


# ---------------------------------------------------------------------------
# Request context
# ---------------------------------------------------------------------------
@dataclass
class Ctx:
    """Everything a handler needs about who is calling and what they may see."""

    user: User
    db: Session
    lang: str
    scope: dict[str, Any]
    ip: str | None = None
    user_agent: str | None = None

    @property
    def user_id(self) -> int:
        return self.user.id

    @property
    def role_code(self) -> str:
        return self.user.role.code if self.user.role else ""

    @property
    def unrestricted(self) -> bool:
        return bool(self.scope.get("unrestricted"))

    @property
    def salesperson_ids(self) -> list[int]:
        return list(self.scope.get("salesperson_ids") or [])

    @property
    def region_ids(self) -> list[int]:
        return list(self.scope.get("region_ids") or [])

    def can(self, resource: str, action: str) -> bool:
        return auth_service.has_permission(self.user, resource, action)

    def check(self, resource: str, action: str) -> None:
        auth_service.require_permission(self.user, resource, action)

    def audit_kwargs(self) -> dict[str, Any]:
        """Common audit fields for this request."""
        return {
            "user_id": self.user.id,
            "username": self.user.username,
            "role_code": self.role_code,
            "ip_address": self.ip,
            "user_agent": self.user_agent,
        }


def _client_ip(request: Request) -> str | None:
    fwd = request.headers.get("X-Forwarded-For")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


def get_context(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    lang: Lang,
) -> Ctx:
    return Ctx(
        user=user,
        db=db,
        lang=lang,
        scope=auth_service.scope_context(db, user),
        ip=_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
    )


Context = Annotated[Ctx, Depends(get_context)]


# ---------------------------------------------------------------------------
# Permission gates
# ---------------------------------------------------------------------------
def require(resource: str, action: str | PermissionAction = PermissionAction.VIEW) -> Callable[..., Ctx]:
    """
    Dependency factory enforcing ``resource:action``.

    Returns the :class:`Ctx` so handlers get authorisation and context in one
    dependency instead of two.
    """
    act = str(action)

    def _dep(ctx: Context) -> Ctx:
        if not ctx.can(resource, act):
            raise PermissionDeniedError(
                "auth.permission_denied", params={"resource": resource, "action": act}
            )
        return ctx

    return _dep


def require_any(*pairs: tuple[str, str]) -> Callable[..., Ctx]:
    """Allow the request if *any* of the given (resource, action) pairs is held."""

    def _dep(ctx: Context) -> Ctx:
        if any(ctx.can(r, a) for r, a in pairs):
            return ctx
        raise PermissionDeniedError(
            "auth.permission_denied",
            params={"resource": ",".join(r for r, _ in pairs), "action": "ANY"},
        )

    return _dep


def require_admin() -> Callable[..., Ctx]:
    def _dep(ctx: Context) -> Ctx:
        if not auth_service.is_admin(ctx.user):
            raise PermissionDeniedError("auth.admin_required")
        return ctx

    return _dep


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------
@dataclass
class Page:
    page: int
    size: int

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.size

    @property
    def limit(self) -> int:
        return self.size


def get_page(
    page: int = Query(1, ge=1, le=100_000),
    size: int = Query(50, ge=1, le=500),
) -> Page:
    return Page(page=page, size=size)


Paging = Annotated[Page, Depends(get_page)]


def paginated(items: list[Any], total: int, page: Page) -> dict[str, Any]:
    pages = (total + page.size - 1) // page.size if page.size else 0
    return {
        "items": items,
        "total": total,
        "page": page.page,
        "size": page.size,
        "pages": pages,
        "has_next": page.page < pages,
        "has_prev": page.page > 1,
    }
