"""
Atomic document-number generation.

Uses ``SELECT ... FOR UPDATE`` on PostgreSQL and relies on SQLite's write lock
otherwise, so two concurrent hot sales can never be issued the same invoice
number.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.base import utcnow
from app.models.system import NumberSequence

#: key -> (prefix, padding, reset period)
#: period:  "*" never resets, "Y" yearly, "YM" monthly
DEFAULTS: dict[str, tuple[str, int, str]] = {
    "ORDER": ("SIP", 6, "Y"),
    "SALE": ("SAT", 6, "Y"),
    "INVOICE": ("FTR", 6, "Y"),
    "WAYBILL": ("IRS", 6, "Y"),
    "CREDIT_NOTE": ("IAD", 6, "Y"),
    "PAYMENT": ("THS", 6, "Y"),
    "RETURN": ("RET", 6, "Y"),
    "TRANSFER": ("TRF", 6, "Y"),
    "COUNT": ("SAY", 6, "Y"),
    "VAN_LOAD": ("YUK", 6, "Y"),
    "CUSTOMER": ("MUS", 5, "*"),
    "PRODUCT": ("URN", 5, "*"),
    "ROUTE": ("ROT", 4, "*"),
}


def _period_key(period_kind: str, on: date) -> str:
    if period_kind == "Y":
        return f"{on.year}"
    if period_kind == "YM":
        return f"{on.year}-{on.month:02d}"
    return "*"


def next_number(db: Session, key: str, *, on: date | None = None) -> str:
    """
    Return the next document number for *key*, e.g. ``FTR-2026-000123``.

    Flushes but does not commit; the number is only consumed if the caller's
    transaction commits.
    """
    key = key.upper()
    prefix, padding, period_kind = DEFAULTS.get(key, (key[:3].upper(), 6, "Y"))
    on = on or date.today()
    period = _period_key(period_kind, on)

    stmt = select(NumberSequence).where(
        NumberSequence.key == key, NumberSequence.period == period
    )
    if settings.is_postgres:
        stmt = stmt.with_for_update()

    seq = db.execute(stmt).scalar_one_or_none()
    if seq is None:
        seq = NumberSequence(
            key=key, period=period, prefix=prefix, padding=padding, next_value=1
        )
        db.add(seq)
        db.flush()

    value = seq.next_value
    seq.next_value = value + 1
    seq.last_issued_at = utcnow()
    db.flush()

    middle = "" if period == "*" else f"-{period}"
    return f"{seq.prefix}{middle}-{value:0{seq.padding}d}"


def peek_number(db: Session, key: str, *, on: date | None = None) -> str:
    """Preview the next number without consuming it."""
    key = key.upper()
    prefix, padding, period_kind = DEFAULTS.get(key, (key[:3].upper(), 6, "Y"))
    on = on or date.today()
    period = _period_key(period_kind, on)
    seq = db.execute(
        select(NumberSequence).where(
            NumberSequence.key == key, NumberSequence.period == period
        )
    ).scalar_one_or_none()
    value = seq.next_value if seq else 1
    pad = seq.padding if seq else padding
    pre = seq.prefix if seq else prefix
    middle = "" if period == "*" else f"-{period}"
    return f"{pre}{middle}-{value:0{pad}d}"
