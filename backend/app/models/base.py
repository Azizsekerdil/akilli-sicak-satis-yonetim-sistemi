"""Shared model mixins and column helpers."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, Money, Quantity, UTCDateTime

# Re-exported so model modules need a single import line.
__all__ = [
    "Base",
    "Money",
    "Quantity",
    "UTCDateTime",
    "TimestampMixin",
    "SoftDeleteMixin",
    "AuthorMixin",
    "CodeNameMixin",
    "utcnow",
    "ZERO",
    "JSONText",
    "pk",
    "fk",
    "Any",
    "date",
    "datetime",
    "Decimal",
]

ZERO = Decimal("0")


def utcnow() -> datetime:
    """Timezone-aware UTC now — used for all defaults."""
    return datetime.now(UTC)


def pk() -> Mapped[int]:
    return mapped_column(Integer, primary_key=True, autoincrement=True)


def fk(target: str, *, nullable: bool = False, ondelete: str = "RESTRICT", index: bool = True):
    """Foreign-key column helper with sensible defaults."""
    return mapped_column(
        Integer,
        ForeignKey(target, ondelete=ondelete),
        nullable=nullable,
        index=index,
    )


class JSONText(Text):
    """
    Portable JSON storage.

    PostgreSQL has native JSON/JSONB, SQLite does not.  Storing serialised JSON
    in TEXT behaves identically on both, which keeps migrations trivial.  Use
    :func:`app.core.json_utils.loads/dumps` when reading/writing.
    """

    __visit_name__ = "TEXT"


class TimestampMixin:
    """created_at / updated_at, maintained by the database where possible."""

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
        nullable=False,
    )


class SoftDeleteMixin:
    """Logical deletion — records are never physically removed."""

    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    deleted_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class AuthorMixin:
    """Who created / last modified the row (no FK, so history survives user deletion)."""

    created_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    updated_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class CodeNameMixin:
    """Business code + bilingual-capable display name, shared by most master data."""

    code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    name_en: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)

    def display_name(self, lang: str = "tr") -> str:
        if lang == "en" and self.name_en:
            return self.name_en
        return self.name
