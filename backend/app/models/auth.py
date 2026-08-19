"""Users, roles, permissions, sessions and login attempts."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import DataScope, UserStatus
from app.models.base import (
    AuthorMixin,
    Base,
    SoftDeleteMixin,
    TimestampMixin,
    UTCDateTime,
    fk,
    pk,
)


class Permission(Base, TimestampMixin):
    """
    A single grantable capability.

    ``resource`` is the screen/module (e.g. ``sales.hot_sale``) and ``action``
    is one of :class:`~app.core.enums.PermissionAction`.  The pair is unique.
    """

    __tablename__ = "permissions"
    __table_args__ = (
        UniqueConstraint("resource", "action", name="uq_permissions_resource_action"),
        Index("ix_permissions_module", "module"),
    )

    id: Mapped[int] = pk()
    code: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    module: Mapped[str] = mapped_column(String(64), nullable=False)
    resource: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    name_tr: Mapped[str] = mapped_column(String(255), nullable=False)
    name_en: Mapped[str] = mapped_column(String(255), nullable=False)
    is_sensitive: Mapped[bool] = mapped_column(Boolean, default=False)


class RolePermission(Base, TimestampMixin):
    """Grant of a permission to a role, optionally narrowing the data scope."""

    __tablename__ = "role_permissions"
    __table_args__ = (
        UniqueConstraint("role_id", "permission_id", name="uq_role_permissions_role_perm"),
    )

    id: Mapped[int] = pk()
    role_id: Mapped[int] = fk("roles.id", ondelete="CASCADE")
    permission_id: Mapped[int] = fk("permissions.id", ondelete="CASCADE")
    data_scope: Mapped[str] = mapped_column(String(16), default=DataScope.ALL, nullable=False)

    role: Mapped["Role"] = relationship(back_populates="role_permissions")
    permission: Mapped["Permission"] = relationship(lazy="joined")


class Role(Base, TimestampMixin, SoftDeleteMixin, AuthorMixin):
    """A named bundle of permissions with a default data scope."""

    __tablename__ = "roles"

    id: Mapped[int] = pk()
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name_tr: Mapped[str] = mapped_column(String(128), nullable=False)
    name_en: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    data_scope: Mapped[str] = mapped_column(String(16), default=DataScope.OWN, nullable=False)
    is_system: Mapped[bool] = mapped_column(Boolean, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    #: Lower number = more privileged.  Used to stop privilege escalation.
    rank: Mapped[int] = mapped_column(Integer, default=100, nullable=False)

    role_permissions: Mapped[list["RolePermission"]] = relationship(
        back_populates="role", cascade="all, delete-orphan", lazy="selectin"
    )
    users: Mapped[list["User"]] = relationship(back_populates="role")

    def name(self, lang: str = "tr") -> str:
        return self.name_en if lang == "en" else self.name_tr


class User(Base, TimestampMixin, SoftDeleteMixin, AuthorMixin):
    """System user.  A salesperson/driver also has a :class:`Salesperson` profile."""

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("username", name="uq_users_username"),
        Index("ix_users_role_status", "role_id", "status"),
    )

    id: Mapped[int] = pk()
    company_id: Mapped[int | None] = fk("companies.id", nullable=True, ondelete="SET NULL")
    role_id: Mapped[int] = fk("roles.id")
    region_id: Mapped[int | None] = fk("regions.id", nullable=True, ondelete="SET NULL")
    manager_id: Mapped[int | None] = fk("users.id", nullable=True, ondelete="SET NULL")

    username: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    email: Mapped[str | None] = mapped_column(String(255), index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    phone: Mapped[str | None] = mapped_column(String(32))
    avatar_path: Mapped[str | None] = mapped_column(String(512))

    status: Mapped[str] = mapped_column(String(16), default=UserStatus.ACTIVE, nullable=False, index=True)
    language: Mapped[str] = mapped_column(String(8), default="tr", nullable=False)
    theme: Mapped[str] = mapped_column(String(16), default="light")

    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    #: True only for the account the first-run bootstrap created, and only
    #: until its password is changed for the first time.  While it is set the
    #: account may sign in **from the local device only** — see
    #: :func:`app.services.auth_service.authenticate`.  Nothing ever sets it
    #: back to True: an administrative password reset forces a change but does
    #: not restore first-run privileges.
    is_bootstrap_credential: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    password_changed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_login_ip: Mapped[str | None] = mapped_column(String(64))
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime)

    #: Per-user overrides layered on top of the role, stored as JSON text:
    #: {"grant": ["sales.hot_sale:CREATE"], "revoke": ["crm.customers:DELETE"]}
    permission_overrides: Mapped[str | None] = mapped_column(Text)

    role: Mapped["Role"] = relationship(back_populates="users", lazy="joined")

    @property
    def is_locked(self) -> bool:
        from app.models.base import utcnow

        return bool(self.locked_until and self.locked_until > utcnow())

    @property
    def is_usable(self) -> bool:
        return self.status == UserStatus.ACTIVE and not self.is_deleted and not self.is_locked


class UserSession(Base, TimestampMixin):
    """Issued refresh token / active session.  Revocable server-side."""

    __tablename__ = "user_sessions"
    __table_args__ = (Index("ix_user_sessions_user_active", "user_id", "is_active"),)

    id: Mapped[int] = pk()
    user_id: Mapped[int] = fk("users.id", ondelete="CASCADE")
    token_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    device_label: Mapped[str | None] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class LoginAttempt(Base):
    """Every authentication attempt — feeds lockout policy and the security log."""

    __tablename__ = "login_attempts"
    __table_args__ = (Index("ix_login_attempts_username_time", "username", "attempted_at"),)

    id: Mapped[int] = pk()
    username: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[int | None] = mapped_column(Integer, index=True)
    successful: Mapped[bool] = mapped_column(Boolean, nullable=False, index=True)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    failure_reason: Mapped[str | None] = mapped_column(String(64))
    attempted_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, index=True
    )
