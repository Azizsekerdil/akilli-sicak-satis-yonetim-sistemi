"""Authentication and user-management schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    device_label: str | None = Field(default=None, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str | None = None


class RoleOut(ORMModel):
    id: int
    code: str
    name_tr: str
    name_en: str
    data_scope: str
    rank: int
    is_active: bool = True


class UserOut(ORMModel):
    id: int
    username: str
    full_name: str
    email: str | None = None
    phone: str | None = None
    status: str
    language: str
    theme: str = "light"
    region_id: int | None = None
    company_id: int | None = None
    must_change_password: bool = False
    last_login_at: datetime | None = None
    role: RoleOut | None = None


class SessionInfo(BaseModel):
    """Everything the SPA needs immediately after login."""

    user: UserOut
    permissions: list[str]
    modules: list[str]
    resources: list[str]
    data_scope: str
    role: str | None = None
    role_rank: int = 999
    salesperson_id: int | None = None
    language: str = "tr"


class LoginResponse(BaseModel):
    tokens: TokenResponse
    session: SessionInfo


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(min_length=1)
    new_password: str = Field(min_length=8, max_length=256)


class ResetPasswordRequest(BaseModel):
    user_id: int
    new_password: str = Field(min_length=8, max_length=256)


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=256)
    full_name: str = Field(min_length=2, max_length=255)
    role_code: str
    email: str | None = None
    phone: str | None = None
    region_id: int | None = None
    company_id: int | None = None
    language: str = "tr"


class UserUpdate(BaseModel):
    full_name: str | None = Field(default=None, max_length=255)
    email: str | None = None
    phone: str | None = None
    role_code: str | None = None
    region_id: int | None = None
    status: str | None = None
    language: str | None = None
    theme: str | None = None


class PermissionOverrideRequest(BaseModel):
    grant: list[str] = Field(default_factory=list)
    revoke: list[str] = Field(default_factory=list)
    data_scope: str | None = None


class PermissionCatalogItem(BaseModel):
    code: str
    module: str
    resource: str
    action: str
    name_tr: str
    name_en: str
    is_sensitive: bool = False


class UserSessionOut(ORMModel):
    id: int
    token_id: str
    ip_address: str | None = None
    device_label: str | None = None
    user_agent: str | None = None
    is_active: bool
    expires_at: datetime
    last_seen_at: datetime | None = None
    created_at: datetime | None = None
