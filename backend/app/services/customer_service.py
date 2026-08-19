"""
Customer master data and CRM logic.

Owns the ``customers`` aggregate: creation with auto-numbering, Turkish-aware
search, credit control, the denormalised commercial counters kept on the
customer row, geo lookups, churn/decline detection and the contact/note
sub-collections.

The customer row carries denormalised state (``balance``, ``order_count``,
``last_order_date`` …) so list screens and the field application never have to
aggregate the ledger or the sales table.  Those fields are *derived*: they are
recomputed here from the source rows rather than incremented in place, which
means a partially failed posting can never leave them permanently wrong.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Sequence

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from app.core.enums import (
    AuditAction,
    CustomerStatus,
    LedgerEntryType,
    VisitFrequency,
)
from app.core.exceptions import (
    BusinessRuleError,
    ConflictError,
    CreditLimitExceededError,
    NotFoundError,
    ValidationError,
)
from app.core.logging_config import get_logger
from app.core.utils import (
    D,
    haversine_km,
    money,
    pct,
    tr_lower,
    tr_upper,
)
from app.models.base import utcnow
from app.models.customer import Customer, CustomerContact, CustomerLedger, CustomerNote
from app.models.product import Product
from app.models.route import RouteStop
from app.models.sales import Sale, SaleItem
from app.services import audit_service, numbering_service

log = get_logger("app.services.customer")

#: Columns a caller may set through :func:`create` / :func:`update`.
#: Everything else is either derived (balance, statistics) or privileged
#: (``credit_limit`` — guarded by ``crm.credit_limit:UPDATE``).
_EDITABLE_FIELDS: tuple[str, ...] = (
    "name",
    "trade_name",
    "customer_type",
    "channel",
    "sub_channel",
    "status",
    "tax_office",
    "tax_number",
    "national_id",
    "is_e_invoice",
    "address",
    "city",
    "district",
    "neighbourhood",
    "postal_code",
    "latitude",
    "longitude",
    "region_id",
    "phone",
    "mobile",
    "email",
    "contact_person",
    "default_route_id",
    "default_salesperson_id",
    "visit_frequency",
    "visit_days",
    "visit_sequence",
    "service_time_minutes",
    "opening_time",
    "closing_time",
    "is_priority",
    "price_list_id",
    "payment_method",
    "payment_term_days",
    "risk_limit",
    "discount_percent",
    "currency",
    "image_path",
    "notes",
    "tags",
)

#: Degrees of latitude per kilometre — used to pre-filter the geo bounding box.
_KM_PER_DEGREE_LAT = 111.045


# ===========================================================================
# Lookups
# ===========================================================================
def get(db: Session, customer_id: int, *, include_deleted: bool = False) -> Customer:
    """Fetch a customer or raise ``customer.not_found``."""
    customer = db.get(Customer, customer_id)
    if customer is None or (customer.is_deleted and not include_deleted):
        raise NotFoundError("customer.not_found", params={"id": customer_id})
    return customer


def get_by_code(db: Session, code: str) -> Customer | None:
    return db.execute(
        select(Customer).where(
            func.lower(Customer.code) == (code or "").strip().lower(),
            Customer.is_deleted.is_(False),
        )
    ).scalar_one_or_none()


def resolve(db: Session, customer: Customer | int) -> Customer:
    """Accept either an ORM instance or an id — services get called both ways."""
    if isinstance(customer, Customer):
        return customer
    return get(db, int(customer))


# ===========================================================================
# Search
# ===========================================================================
#: Letters whose case mapping ASCII ``lower()``/``upper()`` gets wrong in
#: Turkish, together with their ASCII look-alikes.  Neither SQLite nor a
#: default-collation PostgreSQL maps ``İ -> i`` or ``I -> ı``, so a search for
#: "şişli" would never find "Şişli" and "kadikoy" would never find "Kadıköy".
_TR_AMBIGUOUS = frozenset("cçCÇgğGĞiıİIoöOÖsşSŞuüUÜ")

_LIKE_ESCAPE = "\\"


def _escape_like(text: str) -> str:
    """Neutralise LIKE metacharacters typed by the user."""
    return (
        text.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", f"{_LIKE_ESCAPE}%")
        .replace("_", f"{_LIKE_ESCAPE}_")
    )


def _like_patterns(term: str) -> list[str]:
    """Plain and Turkish-folded lower-case LIKE patterns for the term."""
    raw = (term or "").strip()
    if not raw:
        return []
    variants = {_escape_like(raw.lower()), _escape_like(tr_lower(raw))}
    return [f"%{v}%" for v in variants if v]


def _fuzzy_pattern(term: str) -> str | None:
    """
    Pattern with every Turkish-ambiguous letter replaced by the ``_`` wildcard.

    "sisli", "şişli" and "ŞİŞLİ" all collapse to ``%___l_%``, which matches the
    stored "Şişli" on either database engine without needing a folded shadow
    column or a PostgreSQL-only ``unaccent``.  Returned only when the term
    actually contains an ambiguous letter and is long enough for the remaining
    fixed characters to stay selective.
    """
    folded = tr_lower((term or "").strip())
    if len(folded) < 3:
        return None
    chars = [("_" if ch in _TR_AMBIGUOUS else ch) for ch in _escape_like(folded)]
    if "_" not in chars:
        return None
    return "%" + "".join(chars) + "%"


def _search_condition(term: str):
    """OR-condition matching the free-text term against the identity columns."""
    columns = (
        Customer.name,
        Customer.trade_name,
        Customer.code,
        Customer.phone,
        Customer.mobile,
        Customer.tax_number,
    )
    lower_patterns = _like_patterns(term)
    upper_pattern = f"%{_escape_like(tr_upper((term or '').strip()))}%"
    fuzzy = _fuzzy_pattern(term)

    clauses = []
    for col in columns:
        lowered = func.lower(col)
        clauses += [lowered.like(p, escape=_LIKE_ESCAPE) for p in lower_patterns]
        clauses.append(func.upper(col).like(upper_pattern, escape=_LIKE_ESCAPE))
        if fuzzy:
            clauses.append(lowered.like(fuzzy, escape=_LIKE_ESCAPE))
    return or_(*clauses)


def _scoped(stmt: Select, salesperson_ids: Sequence[int] | None) -> Select:
    """Restrict a customer query to the salespeople the caller may see."""
    if salesperson_ids:
        return stmt.where(Customer.default_salesperson_id.in_(list(salesperson_ids)))
    return stmt


def search(
    db: Session,
    *,
    term: str | None = None,
    customer_type: str | None = None,
    channel: str | None = None,
    status: str | None = None,
    region_id: int | None = None,
    route_id: int | None = None,
    salesperson_id: int | None = None,
    city: str | None = None,
    has_debt: bool | None = None,
    salesperson_ids: Sequence[int] | None = None,
    include_deleted: bool = False,
    order_by: str = "name",
    page: int = 1,
    size: int = 50,
) -> tuple[list[Customer], int]:
    """
    Paged customer search.

    ``salesperson_ids`` is the data-scope restriction from the request context;
    ``salesperson_id`` is an explicit user-chosen filter.  Both may apply.
    """
    stmt = select(Customer)
    if not include_deleted:
        stmt = stmt.where(Customer.is_deleted.is_(False))

    if term and term.strip():
        stmt = stmt.where(_search_condition(term))
    if customer_type:
        stmt = stmt.where(Customer.customer_type == str(customer_type))
    if channel:
        stmt = stmt.where(Customer.channel == str(channel))
    if status:
        stmt = stmt.where(Customer.status == str(status))
    if region_id:
        stmt = stmt.where(Customer.region_id == region_id)
    if salesperson_id:
        stmt = stmt.where(Customer.default_salesperson_id == salesperson_id)
    if city and city.strip():
        city_clauses = [
            func.lower(Customer.city).like(p, escape=_LIKE_ESCAPE)
            for p in _like_patterns(city)
        ]
        city_fuzzy = _fuzzy_pattern(city)
        if city_fuzzy:
            city_clauses.append(
                func.lower(Customer.city).like(city_fuzzy, escape=_LIKE_ESCAPE)
            )
        stmt = stmt.where(or_(*city_clauses))
    if route_id:
        # A customer belongs to a route either by its default assignment or by
        # being an actual stop on it (dated instances of a template route).
        stmt = stmt.where(
            or_(
                Customer.default_route_id == route_id,
                Customer.id.in_(
                    select(RouteStop.customer_id).where(RouteStop.route_id == route_id)
                ),
            )
        )
    if has_debt is True:
        stmt = stmt.where(Customer.balance > 0)
    elif has_debt is False:
        stmt = stmt.where(Customer.balance <= 0)

    stmt = _scoped(stmt, salesperson_ids)

    total = int(
        db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one() or 0
    )

    stmt = stmt.order_by(*_order_clause(order_by))
    page = max(1, int(page))
    size = max(1, int(size))
    rows = list(
        db.execute(stmt.offset((page - 1) * size).limit(size)).scalars().unique().all()
    )
    return rows, total


def _order_clause(order_by: str):
    mapping = {
        "name": (Customer.name.asc(),),
        "code": (Customer.code.asc(),),
        "balance": (Customer.balance.desc(), Customer.name.asc()),
        "overdue": (Customer.overdue_balance.desc(), Customer.name.asc()),
        "sales": (Customer.total_sales_amount.desc(), Customer.name.asc()),
        "last_order": (Customer.last_order_date.desc(), Customer.name.asc()),
        "risk": (Customer.risk_score.desc(), Customer.name.asc()),
        "created": (Customer.created_at.desc(),),
    }
    return mapping.get(order_by, mapping["name"])


# ===========================================================================
# CRUD
# ===========================================================================
def _clean_payload(data: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if k in _EDITABLE_FIELDS}


def _snapshot(customer: Customer) -> dict[str, Any]:
    """Audit snapshot of the fields a user can change plus the money fields."""
    fields = list(_EDITABLE_FIELDS) + ["code", "credit_limit", "balance", "status"]
    return {f: getattr(customer, f, None) for f in fields}


def create(
    db: Session,
    data: dict[str, Any],
    *,
    code: str | None = None,
    credit_limit: Decimal | None = None,
    user_id: int | None = None,
    audit_extra: dict[str, Any] | None = None,
    commit: bool = True,
) -> Customer:
    """Create a customer, allocating a ``MUS-xxxxx`` code when none is supplied."""
    payload = _clean_payload(data)
    if not (payload.get("name") or "").strip():
        raise ValidationError("error.validation_error", detail="name is required")

    code = (code or "").strip() or numbering_service.next_number(db, "CUSTOMER")
    if get_by_code(db, code) is not None:
        raise ConflictError("customer.code_taken", params={"code": code})

    customer = Customer(code=code, created_by_id=user_id, updated_by_id=user_id)
    for field, value in payload.items():
        setattr(customer, field, value)
    if credit_limit is not None:
        customer.credit_limit = money(credit_limit)

    db.add(customer)
    db.flush()

    audit_service.record(
        db,
        AuditAction.CREATE,
        entity_type="customer",
        entity_id=customer.id,
        entity_label=customer.name,
        user_id=user_id,
        summary=f"customer created {customer.code}",
        new_values=_snapshot(customer),
        **(audit_extra or {}),
    )
    if commit:
        db.commit()
    return customer


def update(
    db: Session,
    customer_id: int,
    data: dict[str, Any],
    *,
    user_id: int | None = None,
    audit_extra: dict[str, Any] | None = None,
    commit: bool = True,
) -> Customer:
    """Patch editable fields; ``credit_limit`` is deliberately not one of them."""
    customer = get(db, customer_id)
    before = _snapshot(customer)

    payload = _clean_payload(data)
    if "name" in payload and not (payload["name"] or "").strip():
        raise ValidationError("error.validation_error", detail="name cannot be empty")

    for field, value in payload.items():
        setattr(customer, field, value)
    customer.updated_by_id = user_id
    db.flush()

    changed = {k: v for k, v in _snapshot(customer).items() if before.get(k) != v}
    if changed:
        audit_service.record(
            db,
            AuditAction.UPDATE,
            entity_type="customer",
            entity_id=customer.id,
            entity_label=customer.name,
            user_id=user_id,
            summary=f"customer updated {customer.code}",
            old_values={k: before.get(k) for k in changed},
            new_values=changed,
            **(audit_extra or {}),
        )
    if commit:
        db.commit()
    return customer


def soft_delete(
    db: Session,
    customer_id: int,
    *,
    user_id: int | None = None,
    audit_extra: dict[str, Any] | None = None,
    commit: bool = True,
) -> Customer:
    """
    Logically delete a customer.

    A customer with an open balance is never removed: the receivable has to be
    collected or written off first, otherwise the ageing report silently loses
    money that is still owed.
    """
    customer = get(db, customer_id)
    if money(customer.balance) != Decimal("0"):
        raise BusinessRuleError(
            "customer.has_balance",
            params={"name": customer.name, "balance": str(money(customer.balance))},
            detail="customer still has an open balance",
        )

    customer.is_deleted = True
    customer.deleted_at = utcnow()
    customer.deleted_by_id = user_id
    customer.status = CustomerStatus.PASSIVE
    db.flush()

    audit_service.record(
        db,
        AuditAction.DELETE,
        entity_type="customer",
        entity_id=customer.id,
        entity_label=customer.name,
        user_id=user_id,
        summary=f"customer deleted {customer.code}",
        old_values={"is_deleted": False},
        new_values={"is_deleted": True},
        **(audit_extra or {}),
    )
    if commit:
        db.commit()
    return customer


def set_credit_limit(
    db: Session,
    customer_id: int,
    limit: Decimal | float | str,
    *,
    risk_limit: Decimal | float | str | None = None,
    reason: str | None = None,
    user_id: int | None = None,
    audit_extra: dict[str, Any] | None = None,
    commit: bool = True,
) -> Customer:
    """
    Change the credit ceiling.

    Separated from :func:`update` because raising a limit is a financial
    decision guarded by its own permission (``crm.credit_limit:UPDATE``) and it
    must always leave an audit trail with the old and new value.
    """
    customer = get(db, customer_id)
    new_limit = money(limit)
    if new_limit < 0:
        raise ValidationError("error.validation_error", detail="credit limit cannot be negative")

    old_limit = money(customer.credit_limit)
    old_risk = money(customer.risk_limit)
    customer.credit_limit = new_limit
    if risk_limit is not None:
        customer.risk_limit = money(risk_limit)
    customer.updated_by_id = user_id
    db.flush()

    audit_service.record(
        db,
        AuditAction.UPDATE,
        entity_type="customer",
        entity_id=customer.id,
        entity_label=customer.name,
        user_id=user_id,
        summary=(reason or "credit limit changed")[:512],
        old_values={"credit_limit": old_limit, "risk_limit": old_risk},
        new_values={"credit_limit": new_limit, "risk_limit": money(customer.risk_limit)},
        amount=new_limit,
        **(audit_extra or {}),
    )
    if commit:
        db.commit()
    return customer


def set_status(
    db: Session,
    customer_id: int,
    status: str,
    *,
    user_id: int | None = None,
    audit_extra: dict[str, Any] | None = None,
    commit: bool = True,
) -> Customer:
    """Block / unblock / passivate a customer, always audited."""
    if status not in {s.value for s in CustomerStatus}:
        raise ValidationError("error.validation_error", detail=f"unknown status {status}")
    customer = get(db, customer_id)
    old = customer.status
    customer.status = status
    customer.updated_by_id = user_id
    db.flush()
    audit_service.record(
        db,
        AuditAction.UPDATE,
        entity_type="customer",
        entity_id=customer.id,
        entity_label=customer.name,
        user_id=user_id,
        summary=f"customer status {old} -> {status}",
        old_values={"status": old},
        new_values={"status": status},
        **(audit_extra or {}),
    )
    if commit:
        db.commit()
    return customer


# ===========================================================================
# Credit control
# ===========================================================================
def check_credit(db: Session, customer: Customer | int, amount: Decimal | float | str) -> None:
    """
    Gate a new debt against the customer's credit ceiling.

    Rules:
      * a BLOCKED customer may not buy at all, whatever the limit says;
      * ``credit_limit == 0`` means *unlimited* (the common setting for cash
        customers, who never accumulate a balance anyway);
      * otherwise the projected balance (current balance + this document) must
        stay within the limit.
    """
    cust = resolve(db, customer)

    if cust.status == CustomerStatus.BLOCKED:
        raise BusinessRuleError("customer.blocked", params={"name": cust.name})

    limit = money(cust.credit_limit)
    if limit <= 0:
        return

    balance = money(cust.balance)
    requested = money(amount)
    if balance + requested > limit:
        raise CreditLimitExceededError(
            "customer.credit_limit_exceeded",
            params={
                "name": cust.name,
                "limit": str(limit),
                "balance": str(balance),
                "amount": str(requested),
            },
        )


def available_credit(db: Session, customer: Customer | int) -> Decimal | None:
    """Remaining head-room, or ``None`` when the customer has no limit."""
    cust = resolve(db, customer)
    limit = money(cust.credit_limit)
    if limit <= 0:
        return None
    return money(limit - money(cust.balance))


# ===========================================================================
# Derived state
# ===========================================================================
def recalc_balance(db: Session, customer_id: int) -> Decimal:
    """
    Recompute ``balance`` / ``overdue_balance`` / ``last_payment_date`` from the
    ledger and write them back onto the customer row.

    Debit increases what the customer owes, credit decreases it; the overdue
    figure only counts debit entries that are past their due date and still
    carry an open amount.
    """
    customer = get(db, customer_id, include_deleted=True)
    today = date.today()

    debit, credit = db.execute(
        select(
            func.coalesce(func.sum(CustomerLedger.debit), 0),
            func.coalesce(func.sum(CustomerLedger.credit), 0),
        ).where(CustomerLedger.customer_id == customer_id)
    ).one()

    overdue = db.execute(
        select(func.coalesce(func.sum(CustomerLedger.open_amount), 0)).where(
            CustomerLedger.customer_id == customer_id,
            CustomerLedger.open_amount > 0,
            CustomerLedger.due_date.is_not(None),
            CustomerLedger.due_date < today,
        )
    ).scalar_one()

    paid = db.execute(
        select(func.coalesce(func.sum(CustomerLedger.credit), 0)).where(
            CustomerLedger.customer_id == customer_id,
            CustomerLedger.entry_type == LedgerEntryType.PAYMENT,
        )
    ).scalar_one()

    last_payment = db.execute(
        select(func.max(CustomerLedger.entry_date)).where(
            CustomerLedger.customer_id == customer_id,
            CustomerLedger.entry_type == LedgerEntryType.PAYMENT,
        )
    ).scalar_one()

    customer.balance = money(D(debit) - D(credit))
    customer.overdue_balance = money(overdue)
    customer.total_paid_amount = money(paid)
    customer.last_payment_date = last_payment
    db.flush()
    return customer.balance


def refresh_commercial_stats(db: Session, customer_id: int) -> Customer:
    """Recompute order counters and turnover from the posted, non-cancelled sales."""
    customer = get(db, customer_id, include_deleted=True)

    row = db.execute(
        select(
            func.count(Sale.id),
            func.coalesce(func.sum(Sale.total_amount), 0),
            func.min(Sale.sale_date),
            func.max(Sale.sale_date),
        ).where(
            Sale.customer_id == customer_id,
            Sale.is_deleted.is_(False),
            Sale.is_cancelled.is_(False),
        )
    ).one()

    count = int(row[0] or 0)
    total = money(row[1])
    customer.order_count = count
    customer.total_sales_amount = total
    customer.average_order_value = money(total / count) if count else Decimal("0")
    customer.first_order_date = row[2]
    customer.last_order_date = row[3]
    db.flush()
    return customer


def refresh_all_stats(db: Session, customer_id: int) -> Customer:
    """Convenience for jobs: ledger balance plus sales statistics in one call."""
    recalc_balance(db, customer_id)
    return refresh_commercial_stats(db, customer_id)


# ===========================================================================
# Geo
# ===========================================================================
def nearby(
    db: Session,
    lat: float,
    lng: float,
    radius_km: float = 5.0,
    *,
    salesperson_ids: Sequence[int] | None = None,
    status: str | None = CustomerStatus.ACTIVE,
    limit: int = 100,
) -> list[tuple[Customer, float]]:
    """
    Customers within ``radius_km`` of a point, nearest first.

    A latitude/longitude bounding box narrows the candidate set in SQL (which
    an index can serve), then the exact great-circle distance is applied in
    Python — portable across SQLite and PostgreSQL without PostGIS.
    """
    radius = max(0.05, float(radius_km))
    d_lat = radius / _KM_PER_DEGREE_LAT
    cos_lat = math.cos(math.radians(max(-89.9, min(89.9, float(lat)))))
    d_lng = radius / (_KM_PER_DEGREE_LAT * max(0.01, abs(cos_lat)))

    stmt = select(Customer).where(
        Customer.is_deleted.is_(False),
        Customer.latitude.is_not(None),
        Customer.longitude.is_not(None),
        Customer.latitude.between(lat - d_lat, lat + d_lat),
        Customer.longitude.between(lng - d_lng, lng + d_lng),
    )
    if status:
        stmt = stmt.where(Customer.status == str(status))
    stmt = _scoped(stmt, salesperson_ids)

    hits: list[tuple[Customer, float]] = []
    for customer in db.execute(stmt).scalars().unique().all():
        distance = haversine_km(lat, lng, float(customer.latitude), float(customer.longitude))
        if distance <= radius:
            hits.append((customer, round(distance, 3)))

    hits.sort(key=lambda pair: pair[1])
    return hits[: max(1, int(limit))]


# ===========================================================================
# Churn & decline
# ===========================================================================
def churn_candidates(
    db: Session,
    *,
    days: int = 90,
    salesperson_ids: Sequence[int] | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """
    Active customers that have not ordered for ``days`` days.

    Customers that never ordered count too, but only once they are older than
    the window — otherwise every newly registered prospect would be flagged on
    the day it is created.
    """
    cutoff = date.today() - timedelta(days=max(1, int(days)))

    stmt = select(Customer).where(
        Customer.is_deleted.is_(False),
        Customer.status.in_([CustomerStatus.ACTIVE, CustomerStatus.PASSIVE]),
        or_(
            Customer.last_order_date < cutoff,
            Customer.last_order_date.is_(None),
        ),
    )
    stmt = _scoped(stmt, salesperson_ids)
    stmt = stmt.order_by(Customer.total_sales_amount.desc()).limit(max(1, int(limit)) * 2)

    today = date.today()
    out: list[dict[str, Any]] = []
    for customer in db.execute(stmt).scalars().unique().all():
        if customer.last_order_date is None:
            created = getattr(customer, "created_at", None)
            created_date = created.date() if created is not None else None
            if created_date is not None and created_date > cutoff:
                continue
            idle = (today - created_date).days if created_date else int(days)
        else:
            idle = (today - customer.last_order_date).days

        out.append(
            {
                "customer": customer,
                "days_since_last_order": idle,
                "last_order_date": customer.last_order_date,
                "total_sales_amount": money(customer.total_sales_amount),
                "balance": money(customer.balance),
            }
        )

    out.sort(key=lambda r: (-r["days_since_last_order"], -float(r["total_sales_amount"])))
    return out[: max(1, int(limit))]


def declining(
    db: Session,
    *,
    days: int = 30,
    drop_percent: float = 20.0,
    salesperson_ids: Sequence[int] | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """
    Customers whose turnover in the last ``days`` fell by at least
    ``drop_percent`` against the immediately preceding window of equal length.

    Only customers with real history in the previous window are considered —
    otherwise a first-time buyer looks like a 100 % collapse.
    """
    window = max(1, int(days))
    today = date.today()
    current_start = today - timedelta(days=window - 1)
    previous_start = current_start - timedelta(days=window)
    previous_end = current_start - timedelta(days=1)

    def _totals(start: date, end: date) -> dict[int, Decimal]:
        stmt = (
            select(Sale.customer_id, func.coalesce(func.sum(Sale.total_amount), 0))
            .where(
                Sale.is_deleted.is_(False),
                Sale.is_cancelled.is_(False),
                Sale.sale_date >= start,
                Sale.sale_date <= end,
            )
            .group_by(Sale.customer_id)
        )
        if salesperson_ids:
            stmt = stmt.where(Sale.salesperson_id.in_(list(salesperson_ids)))
        return {int(cid): money(total) for cid, total in db.execute(stmt).all()}

    current = _totals(current_start, today)
    previous = _totals(previous_start, previous_end)

    rows: list[dict[str, Any]] = []
    for customer_id, prev_total in previous.items():
        if prev_total <= 0:
            continue
        curr_total = current.get(customer_id, Decimal("0"))
        drop = pct(prev_total - curr_total, prev_total)
        if drop < float(drop_percent):
            continue
        customer = db.get(Customer, customer_id)
        if customer is None or customer.is_deleted:
            continue
        if salesperson_ids and customer.default_salesperson_id not in set(salesperson_ids):
            # The sale may have been made by a colleague covering the route;
            # the customer itself still has to be inside the caller's scope.
            continue
        rows.append(
            {
                "customer": customer,
                "current_amount": curr_total,
                "previous_amount": prev_total,
                "drop_percent": drop,
                "days": window,
            }
        )

    rows.sort(key=lambda r: (-float(r["previous_amount"] - r["current_amount"]), -r["drop_percent"]))
    return rows[: max(1, int(limit))]


# ===========================================================================
# Sales history
# ===========================================================================
def sales_history(
    db: Session,
    customer_id: int,
    *,
    start: date | None = None,
    end: date | None = None,
    limit: int = 100,
) -> list[Sale]:
    """Recent deliveries for the customer, newest first."""
    get(db, customer_id, include_deleted=True)
    stmt = select(Sale).where(
        Sale.customer_id == customer_id,
        Sale.is_deleted.is_(False),
    )
    if start:
        stmt = stmt.where(Sale.sale_date >= start)
    if end:
        stmt = stmt.where(Sale.sale_date <= end)
    stmt = stmt.order_by(Sale.sale_date.desc(), Sale.id.desc()).limit(max(1, int(limit)))
    return list(db.execute(stmt).scalars().unique().all())


def purchased_products(
    db: Session,
    customer_id: int,
    *,
    start: date | None = None,
    end: date | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """
    The SKU basket: every product the customer has ever bought with volume,
    turnover and the date it was last taken.

    Drives the "usual order" suggestion and the out-of-assortment alert in the
    field application.
    """
    get(db, customer_id, include_deleted=True)

    stmt = (
        select(
            SaleItem.product_id,
            Product.code,
            Product.name,
            func.coalesce(func.sum(SaleItem.base_quantity), 0),
            func.coalesce(func.sum(SaleItem.total_amount), 0),
            func.count(func.distinct(SaleItem.sale_id)),
            func.max(Sale.sale_date),
        )
        .join(Sale, Sale.id == SaleItem.sale_id)
        .join(Product, Product.id == SaleItem.product_id)
        .where(
            Sale.customer_id == customer_id,
            Sale.is_deleted.is_(False),
            Sale.is_cancelled.is_(False),
        )
        .group_by(SaleItem.product_id, Product.code, Product.name)
        .order_by(func.coalesce(func.sum(SaleItem.total_amount), 0).desc())
        .limit(max(1, int(limit)))
    )
    if start:
        stmt = stmt.where(Sale.sale_date >= start)
    if end:
        stmt = stmt.where(Sale.sale_date <= end)

    return [
        {
            "product_id": int(pid),
            "product_code": code,
            "product_name": name,
            "total_quantity": D(quantity),
            "total_amount": money(amount),
            "order_count": int(orders or 0),
            "last_purchase_date": last_date,
        }
        for pid, code, name, quantity, amount, orders, last_date in db.execute(stmt).all()
    ]


# ===========================================================================
# Visit planning
# ===========================================================================
def visit_plan(
    db: Session,
    *,
    weekday: str,
    salesperson_id: int | None = None,
    salesperson_ids: Sequence[int] | None = None,
    route_id: int | None = None,
) -> list[Customer]:
    """
    Customers due to be visited on ``weekday`` (``MON`` … ``SUN``).

    ``visit_days`` is a comma-separated code list, so the SQL pre-filter uses a
    substring match and the exact membership test runs in Python — a
    portable-SQL compromise that avoids array columns.  DAILY customers are
    always due, whatever their day list says.
    """
    code = (weekday or "").strip().upper()[:3]
    if code not in {"MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"}:
        raise ValidationError("error.validation_error", detail=f"bad weekday {weekday!r}")

    stmt = select(Customer).where(
        Customer.is_deleted.is_(False),
        Customer.status == CustomerStatus.ACTIVE,
        or_(
            Customer.visit_frequency == VisitFrequency.DAILY,
            func.upper(Customer.visit_days).like(f"%{code}%"),
        ),
    )
    if salesperson_id:
        stmt = stmt.where(Customer.default_salesperson_id == salesperson_id)
    if route_id:
        stmt = stmt.where(Customer.default_route_id == route_id)
    stmt = _scoped(stmt, salesperson_ids)
    stmt = stmt.order_by(Customer.visit_sequence.asc(), Customer.name.asc())

    return [
        customer
        for customer in db.execute(stmt).scalars().unique().all()
        if customer.visit_frequency == VisitFrequency.DAILY
        or code in customer.visit_day_list()
    ]


# ===========================================================================
# Contacts
# ===========================================================================
def list_contacts(db: Session, customer_id: int) -> list[CustomerContact]:
    get(db, customer_id, include_deleted=True)
    return list(
        db.execute(
            select(CustomerContact)
            .where(CustomerContact.customer_id == customer_id)
            .order_by(CustomerContact.is_primary.desc(), CustomerContact.name.asc())
        )
        .scalars()
        .all()
    )


def add_contact(
    db: Session,
    customer_id: int,
    data: dict[str, Any],
    *,
    user_id: int | None = None,
    commit: bool = True,
) -> CustomerContact:
    """Add a named contact; marking one primary demotes the previous primary."""
    get(db, customer_id)
    if not (data.get("name") or "").strip():
        raise ValidationError("error.validation_error", detail="contact name is required")

    contact = CustomerContact(
        customer_id=customer_id,
        name=data["name"].strip(),
        title=data.get("title"),
        phone=data.get("phone"),
        email=data.get("email"),
        is_primary=bool(data.get("is_primary")),
        notes=data.get("notes"),
    )
    db.add(contact)
    db.flush()
    if contact.is_primary:
        _demote_other_primaries(db, customer_id, contact.id)
    if commit:
        db.commit()
    log.info("contact added customer=%s contact=%s by=%s", customer_id, contact.id, user_id)
    return contact


def _demote_other_primaries(db: Session, customer_id: int, keep_id: int) -> None:
    for other in (
        db.execute(
            select(CustomerContact).where(
                CustomerContact.customer_id == customer_id,
                CustomerContact.id != keep_id,
                CustomerContact.is_primary.is_(True),
            )
        )
        .scalars()
        .all()
    ):
        other.is_primary = False
    db.flush()


def update_contact(
    db: Session,
    contact_id: int,
    data: dict[str, Any],
    *,
    commit: bool = True,
) -> CustomerContact:
    contact = db.get(CustomerContact, contact_id)
    if contact is None:
        raise NotFoundError("customer.contact_not_found", params={"id": contact_id})
    for field in ("name", "title", "phone", "email", "is_primary", "notes"):
        if field in data and data[field] is not None:
            setattr(contact, field, data[field])
    db.flush()
    if contact.is_primary:
        _demote_other_primaries(db, contact.customer_id, contact.id)
    if commit:
        db.commit()
    return contact


def delete_contact(
    db: Session,
    contact_id: int,
    *,
    customer_id: int | None = None,
    commit: bool = True,
) -> None:
    """``customer_id`` guards against deleting a contact through another
    customer's URL, which would bypass that customer's scope check."""
    contact = db.get(CustomerContact, contact_id)
    if contact is None or (customer_id is not None and contact.customer_id != customer_id):
        raise NotFoundError("customer.contact_not_found", params={"id": contact_id})
    db.delete(contact)
    db.flush()
    if commit:
        db.commit()


# ===========================================================================
# Notes
# ===========================================================================
def list_notes(db: Session, customer_id: int, *, limit: int = 100) -> list[CustomerNote]:
    get(db, customer_id, include_deleted=True)
    return list(
        db.execute(
            select(CustomerNote)
            .where(CustomerNote.customer_id == customer_id)
            .order_by(CustomerNote.is_pinned.desc(), CustomerNote.created_at.desc())
            .limit(max(1, int(limit)))
        )
        .scalars()
        .all()
    )


def add_note(
    db: Session,
    customer_id: int,
    data: dict[str, Any],
    *,
    user_id: int | None = None,
    commit: bool = True,
) -> CustomerNote:
    get(db, customer_id)
    body = (data.get("body") or "").strip()
    if not body:
        raise ValidationError("error.validation_error", detail="note body is required")

    note = CustomerNote(
        customer_id=customer_id,
        visit_id=data.get("visit_id"),
        category=data.get("category"),
        body=body,
        is_pinned=bool(data.get("is_pinned")),
        created_by_id=user_id,
        updated_by_id=user_id,
    )
    db.add(note)
    db.flush()
    if commit:
        db.commit()
    return note


def update_note(
    db: Session,
    note_id: int,
    data: dict[str, Any],
    *,
    user_id: int | None = None,
    commit: bool = True,
) -> CustomerNote:
    note = db.get(CustomerNote, note_id)
    if note is None:
        raise NotFoundError("customer.note_not_found", params={"id": note_id})
    if "body" in data and (data["body"] or "").strip():
        note.body = data["body"].strip()
    for field in ("category", "is_pinned", "visit_id"):
        if field in data and data[field] is not None:
            setattr(note, field, data[field])
    note.updated_by_id = user_id
    db.flush()
    if commit:
        db.commit()
    return note


def delete_note(
    db: Session,
    note_id: int,
    *,
    customer_id: int | None = None,
    commit: bool = True,
) -> None:
    """See :func:`delete_contact` for why ``customer_id`` is checked."""
    note = db.get(CustomerNote, note_id)
    if note is None or (customer_id is not None and note.customer_id != customer_id):
        raise NotFoundError("customer.note_not_found", params={"id": note_id})
    db.delete(note)
    db.flush()
    if commit:
        db.commit()
