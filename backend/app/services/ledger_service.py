"""
Customer current account (cari hesap): ledger posting, settlement, ageing,
statements and collection risk.

The ledger is **append-only**.  Nothing is ever updated except ``open_amount``
and ``is_settled`` on a debit entry, which record how much of that invoice has
since been collected.  Corrections are posted as new ADJUSTMENT or WRITE_OFF
entries, never by editing history — that is what makes a statement printed
today reproducible tomorrow.

Sign convention
---------------
``debit``  increases what the customer owes us (invoice, debit note).
``credit`` decreases it (payment, credit note, write-off).
``balance = Σ debit − Σ credit`` — positive means the customer owes money.
"""

from __future__ import annotations

import statistics
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Sequence

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from app.core.enums import AuditAction, LedgerEntryType, PaymentStatus
from app.core.exceptions import ValidationError
from app.core.logging_config import get_logger
from app.core.utils import D, clamp, money, safe_div
from app.models.base import utcnow
from app.models.customer import Customer, CustomerLedger
from app.models.sales import Payment
from app.services import audit_service, customer_service

log = get_logger("app.services.ledger")

#: Entry types that increase the receivable.  Used to decide whether a posting
#: opens an item that later has to be settled.
DEBIT_TYPES: frozenset[str] = frozenset(
    {
        LedgerEntryType.INVOICE,
        LedgerEntryType.DEBIT_NOTE,
        LedgerEntryType.OPENING_BALANCE,
    }
)

#: Ageing buckets, in days past due.  ``current`` is "not yet due".
AGING_BUCKETS: tuple[str, ...] = ("current", "d1_30", "d31_60", "d61_90", "d90_plus")

_BOUNCE_WINDOW_DAYS = 180


# ===========================================================================
# Posting
# ===========================================================================
def _last_entry(db: Session, customer_id: int) -> CustomerLedger | None:
    """
    Most recently *posted* entry, by id.

    Ordering by id rather than by ``entry_date`` is deliberate: a back-dated
    correction chains onto the balance as it stands when it is posted, so
    ``balance_after`` always reflects the order in which the business actually
    recorded the movements and never has to be rewritten retroactively.
    """
    return db.execute(
        select(CustomerLedger)
        .where(CustomerLedger.customer_id == customer_id)
        .order_by(CustomerLedger.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def post_entry(
    db: Session,
    *,
    customer_id: int,
    entry_type: str,
    entry_date: date,
    debit: Decimal | float | str = Decimal("0"),
    credit: Decimal | float | str = Decimal("0"),
    due_date: date | None = None,
    reference_type: str | None = None,
    reference_id: int | None = None,
    reference_no: str | None = None,
    salesperson_id: int | None = None,
    description: str | None = None,
    user_id: int | None = None,
    auto_settle: bool = False,
    commit: bool = False,
) -> CustomerLedger:
    """
    Append one current-account entry and refresh the customer's balance.

    Does not commit by default — the entry belongs to the caller's document
    transaction (a sale, a collection), so the invoice and its ledger line
    either both land or neither does.

    With ``auto_settle`` a credit entry is immediately FIFO-allocated against
    the open debit items, which is what a collection posting wants.
    """
    customer = customer_service.get(db, customer_id, include_deleted=True)

    debit_amount = money(debit)
    credit_amount = money(credit)
    if debit_amount < 0 or credit_amount < 0:
        raise ValidationError(
            "error.validation_error", detail="ledger amounts must be non-negative"
        )
    if debit_amount == 0 and credit_amount == 0:
        raise ValidationError("payment.amount_positive")

    kind = str(entry_type)
    if kind not in {e.value for e in LedgerEntryType}:
        raise ValidationError("error.validation_error", detail=f"unknown entry type {kind}")

    if due_date is None and debit_amount > 0:
        # Terms come from the customer card unless the document overrides them.
        due_date = entry_date + timedelta(days=int(customer.payment_term_days or 0))

    previous = _last_entry(db, customer_id)
    opening = money(previous.balance_after) if previous else Decimal("0")

    open_amount = debit_amount if (kind in DEBIT_TYPES and debit_amount > 0) else Decimal("0")

    entry = CustomerLedger(
        customer_id=customer_id,
        entry_type=kind,
        entry_date=entry_date,
        due_date=due_date,
        debit=debit_amount,
        credit=credit_amount,
        balance_after=money(opening + debit_amount - credit_amount),
        currency=customer.currency or "TRY",
        open_amount=open_amount,
        is_settled=open_amount <= 0,
        reference_type=reference_type,
        reference_id=reference_id,
        reference_no=reference_no,
        salesperson_id=salesperson_id,
        description=(description or "")[:512] or None,
        created_by_id=user_id,
    )
    db.add(entry)
    db.flush()

    if auto_settle and credit_amount > 0:
        settle(
            db,
            customer_id=customer_id,
            amount=credit_amount,
            payment_id=reference_id if reference_type == "payment" else None,
            user_id=user_id,
            commit=False,
        )

    customer_service.recalc_balance(db, customer_id)

    audit_service.record(
        db,
        AuditAction.PAYMENT if kind == LedgerEntryType.PAYMENT else AuditAction.CREATE,
        entity_type="customer_ledger",
        entity_id=entry.id,
        entity_label=f"{customer.code} {kind}",
        user_id=user_id,
        summary=f"ledger {kind} {reference_no or ''}".strip(),
        new_values={
            "customer_id": customer_id,
            "entry_type": kind,
            "entry_date": entry_date,
            "debit": debit_amount,
            "credit": credit_amount,
            "due_date": due_date,
            "balance_after": entry.balance_after,
        },
        amount=money(debit_amount - credit_amount),
    )
    if commit:
        db.commit()
    return entry


def settle(
    db: Session,
    *,
    customer_id: int,
    amount: Decimal | float | str,
    payment_id: int | None = None,
    on_date: date | None = None,
    user_id: int | None = None,
    commit: bool = False,
) -> list[tuple[int, Decimal]]:
    """
    Apply ``amount`` to the oldest open debit entries (FIFO by due date).

    Turkish trade practice settles the oldest receivable first, which is also
    what keeps the ageing report honest: money collected today must retire the
    oldest debt, not the newest invoice.

    Returns ``[(ledger_id, applied_amount), …]``.  Any surplus that exceeds the
    open items is simply not allocated — it stays in the balance as a credit
    and will absorb the next invoice.
    """
    remaining = money(amount)
    if remaining <= 0:
        raise ValidationError("payment.amount_positive")

    customer_service.get(db, customer_id, include_deleted=True)

    open_items = (
        db.execute(
            select(CustomerLedger)
            .where(
                CustomerLedger.customer_id == customer_id,
                CustomerLedger.open_amount > 0,
                CustomerLedger.is_settled.is_(False),
            )
            .order_by(
                func.coalesce(CustomerLedger.due_date, CustomerLedger.entry_date).asc(),
                CustomerLedger.id.asc(),
            )
        )
        .scalars()
        .all()
    )

    applied: list[tuple[int, Decimal]] = []
    for item in open_items:
        if remaining <= 0:
            break
        take = min(money(item.open_amount), remaining)
        item.open_amount = money(money(item.open_amount) - take)
        item.is_settled = item.open_amount <= 0
        remaining = money(remaining - take)
        applied.append((item.id, take))

    db.flush()
    customer_service.recalc_balance(db, customer_id)

    if applied:
        audit_service.record(
            db,
            AuditAction.PAYMENT,
            entity_type="customer",
            entity_id=customer_id,
            entity_label=f"settlement customer#{customer_id}",
            user_id=user_id,
            summary=(
                f"settled {len(applied)} open item(s)"
                + (f" from payment#{payment_id}" if payment_id else "")
            ),
            new_values={
                "on_date": on_date or date.today(),
                "payment_id": payment_id,
                "allocations": [{"ledger_id": lid, "amount": amt} for lid, amt in applied],
            },
            amount=money(money(amount) - remaining),
        )
    if commit:
        db.commit()
    return applied


def unsettle(
    db: Session,
    *,
    customer_id: int,
    amount: Decimal | float | str,
    user_id: int | None = None,
    commit: bool = False,
) -> list[tuple[int, Decimal]]:
    """
    Reverse a settlement (bounced cheque, cancelled collection) by re-opening
    the most recently retired items — LIFO, the mirror image of :func:`settle`.
    """
    remaining = money(amount)
    if remaining <= 0:
        raise ValidationError("payment.amount_positive")

    items = (
        db.execute(
            select(CustomerLedger)
            .where(
                CustomerLedger.customer_id == customer_id,
                CustomerLedger.entry_type.in_(list(DEBIT_TYPES)),
                CustomerLedger.debit > 0,
            )
            .order_by(
                func.coalesce(CustomerLedger.due_date, CustomerLedger.entry_date).desc(),
                CustomerLedger.id.desc(),
            )
        )
        .scalars()
        .all()
    )

    restored: list[tuple[int, Decimal]] = []
    for item in items:
        if remaining <= 0:
            break
        room = money(money(item.debit) - money(item.open_amount))
        if room <= 0:
            continue
        give = min(room, remaining)
        item.open_amount = money(money(item.open_amount) + give)
        item.is_settled = False
        remaining = money(remaining - give)
        restored.append((item.id, give))

    db.flush()
    customer_service.recalc_balance(db, customer_id)
    if restored:
        audit_service.record(
            db,
            AuditAction.PAYMENT,
            entity_type="customer",
            entity_id=customer_id,
            entity_label=f"unsettle customer#{customer_id}",
            user_id=user_id,
            summary=f"re-opened {len(restored)} item(s)",
            new_values={"restored": [{"ledger_id": i, "amount": a} for i, a in restored]},
            amount=money(money(amount) - remaining),
        )
    if commit:
        db.commit()
    return restored


# ===========================================================================
# Reading the ledger
# ===========================================================================
def entries(
    db: Session,
    customer_id: int,
    *,
    start: date | None = None,
    end: date | None = None,
    entry_type: str | None = None,
    only_open: bool = False,
    page: int = 1,
    size: int = 100,
) -> tuple[list[CustomerLedger], int]:
    """Paged ledger movements for one customer, newest first."""
    customer_service.get(db, customer_id, include_deleted=True)

    stmt = select(CustomerLedger).where(CustomerLedger.customer_id == customer_id)
    if start:
        stmt = stmt.where(CustomerLedger.entry_date >= start)
    if end:
        stmt = stmt.where(CustomerLedger.entry_date <= end)
    if entry_type:
        stmt = stmt.where(CustomerLedger.entry_type == str(entry_type))
    if only_open:
        stmt = stmt.where(CustomerLedger.open_amount > 0)

    total = int(
        db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one() or 0
    )
    page = max(1, int(page))
    size = max(1, int(size))
    rows = list(
        db.execute(
            stmt.order_by(CustomerLedger.entry_date.desc(), CustomerLedger.id.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
        .scalars()
        .unique()
        .all()
    )
    return rows, total


def open_items(db: Session, customer_id: int) -> list[CustomerLedger]:
    """Unsettled debit entries, oldest due first — the collection work list."""
    return list(
        db.execute(
            select(CustomerLedger)
            .where(
                CustomerLedger.customer_id == customer_id,
                CustomerLedger.open_amount > 0,
            )
            .order_by(
                func.coalesce(CustomerLedger.due_date, CustomerLedger.entry_date).asc(),
                CustomerLedger.id.asc(),
            )
        )
        .scalars()
        .unique()
        .all()
    )


# ===========================================================================
# Ageing
# ===========================================================================
def _open_items_query(
    customer_id: int | None,
    salesperson_ids: Sequence[int] | None,
) -> Select:
    stmt = select(
        CustomerLedger.customer_id,
        CustomerLedger.open_amount,
        CustomerLedger.due_date,
        CustomerLedger.entry_date,
    ).where(CustomerLedger.open_amount > 0)

    if customer_id is not None:
        stmt = stmt.where(CustomerLedger.customer_id == customer_id)
    if salesperson_ids:
        # Scope by the customer's owner, not by whoever raised the invoice —
        # the receivable belongs to the account, not to the document.
        stmt = stmt.join(Customer, Customer.id == CustomerLedger.customer_id).where(
            Customer.default_salesperson_id.in_(list(salesperson_ids))
        )
    return stmt


def _bucket_for(days_past_due: int) -> str:
    if days_past_due <= 0:
        return "current"
    if days_past_due <= 30:
        return "d1_30"
    if days_past_due <= 60:
        return "d31_60"
    if days_past_due <= 90:
        return "d61_90"
    return "d90_plus"


def aging(
    db: Session,
    *,
    customer_id: int | None = None,
    as_of: date | None = None,
    salesperson_ids: Sequence[int] | None = None,
) -> dict[str, Decimal]:
    """
    Bucket the open receivable by days past due.

    ``current`` is everything not yet due; ``overdue`` is the sum of the four
    past-due buckets.  Entries without a due date are aged from their entry
    date, so an unterminated opening balance is never invisible.
    """
    reference = as_of or date.today()
    result: dict[str, Decimal] = {bucket: Decimal("0") for bucket in AGING_BUCKETS}

    rows = db.execute(_open_items_query(customer_id, salesperson_ids)).all()
    for _cid, open_amount, due_date, entry_date in rows:
        effective_due = due_date or entry_date
        days = (reference - effective_due).days if effective_due else 0
        result[_bucket_for(days)] += money(open_amount)

    total = sum(result.values(), Decimal("0"))
    result = {k: money(v) for k, v in result.items()}
    result["total"] = money(total)
    result["overdue"] = money(total - result["current"])
    return result


def aging_by_customer(
    db: Session,
    *,
    as_of: date | None = None,
    salesperson_ids: Sequence[int] | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Per-customer ageing rows, biggest overdue first (the collection sheet)."""
    reference = as_of or date.today()
    per_customer: dict[int, dict[str, Decimal]] = {}

    for cid, open_amount, due_date, entry_date in db.execute(
        _open_items_query(None, salesperson_ids)
    ).all():
        bucket = per_customer.setdefault(
            int(cid), {b: Decimal("0") for b in AGING_BUCKETS}
        )
        effective_due = due_date or entry_date
        days = (reference - effective_due).days if effective_due else 0
        bucket[_bucket_for(days)] += money(open_amount)

    rows: list[dict[str, Any]] = []
    for cid, buckets in per_customer.items():
        customer = db.get(Customer, cid)
        if customer is None or customer.is_deleted:
            continue
        total = money(sum(buckets.values(), Decimal("0")))
        row: dict[str, Any] = {k: money(v) for k, v in buckets.items()}
        row.update(
            {
                "customer_id": cid,
                "customer_code": customer.code,
                "customer_name": customer.name,
                "total": total,
                "overdue": money(total - buckets["current"]),
            }
        )
        rows.append(row)

    rows.sort(key=lambda r: (-float(r["overdue"]), -float(r["total"])))
    return rows[: max(1, int(limit))]


# ===========================================================================
# Statement
# ===========================================================================
def statement(db: Session, customer_id: int, start: date, end: date) -> dict[str, Any]:
    """
    Account statement (ekstre) for a period.

    ``opening`` is the balance carried in from everything before ``start``;
    each row shows the running balance so the printed statement reconciles
    line by line to ``closing``.
    """
    customer = customer_service.get(db, customer_id, include_deleted=True)
    if end < start:
        raise ValidationError("error.validation_error", detail="end date precedes start date")

    prior = db.execute(
        select(
            func.coalesce(func.sum(CustomerLedger.debit), 0),
            func.coalesce(func.sum(CustomerLedger.credit), 0),
        ).where(
            CustomerLedger.customer_id == customer_id,
            CustomerLedger.entry_date < start,
        )
    ).one()
    opening = money(D(prior[0]) - D(prior[1]))

    rows_raw = (
        db.execute(
            select(CustomerLedger)
            .where(
                CustomerLedger.customer_id == customer_id,
                CustomerLedger.entry_date >= start,
                CustomerLedger.entry_date <= end,
            )
            .order_by(CustomerLedger.entry_date.asc(), CustomerLedger.id.asc())
        )
        .scalars()
        .unique()
        .all()
    )

    running = opening
    total_debit = Decimal("0")
    total_credit = Decimal("0")
    rows: list[dict[str, Any]] = []
    for entry in rows_raw:
        debit = money(entry.debit)
        credit = money(entry.credit)
        running = money(running + debit - credit)
        total_debit += debit
        total_credit += credit
        rows.append(
            {
                "id": entry.id,
                "entry_date": entry.entry_date,
                "due_date": entry.due_date,
                "entry_type": entry.entry_type,
                "reference_type": entry.reference_type,
                "reference_id": entry.reference_id,
                "reference_no": entry.reference_no,
                "description": entry.description,
                "debit": debit,
                "credit": credit,
                "balance": running,
                "open_amount": money(entry.open_amount),
                "is_settled": bool(entry.is_settled),
            }
        )

    return {
        "customer_id": customer_id,
        "customer_code": customer.code,
        "customer_name": customer.name,
        "currency": customer.currency or "TRY",
        "start": start,
        "end": end,
        "opening": opening,
        "rows": rows,
        "closing": money(running),
        "totals": {
            "debit": money(total_debit),
            "credit": money(total_credit),
            "movement": money(total_debit - total_credit),
            "row_count": Decimal(len(rows)),
        },
    }


# ===========================================================================
# Risk
# ===========================================================================
def _payment_intervals(db: Session, customer_id: int, *, take: int = 13) -> list[int]:
    """Day gaps between the customer's last consecutive payments."""
    dates = list(
        db.execute(
            select(CustomerLedger.entry_date)
            .where(
                CustomerLedger.customer_id == customer_id,
                CustomerLedger.entry_type == LedgerEntryType.PAYMENT,
            )
            .order_by(CustomerLedger.entry_date.desc())
            .limit(max(2, int(take)))
        )
        .scalars()
        .all()
    )
    dates = sorted(d for d in dates if d is not None)
    return [(b - a).days for a, b in zip(dates, dates[1:]) if (b - a).days >= 0]


def risk_score(db: Session, customer: Customer | int) -> float:
    """
    Collection-risk score from 0 (safe) to 100 (do not sell on credit).

    Weighting — chosen so that *money already late* dominates, and behavioural
    signals only refine the picture:

    ==========================  ======  ==============================================
    Component                   Weight  Definition
    ==========================  ======  ==============================================
    Overdue ratio                 30    overdue_balance / balance
    Worst days past due           25    linear 0 → 90+ days on the oldest open item
    Payment irregularity          15    coefficient of variation of the gaps between
                                        consecutive payments; unknown behaviour with
                                        an open balance scores half weight
    Credit utilisation            20    balance / credit_limit (unlimited → 0)
    Bounced instruments           10    5 points per bounced cheque/note in 180 days
    ==========================  ======  ==============================================

    The score is intentionally computed, not learned: a collections clerk has
    to be able to explain to a customer *why* their account was flagged.
    """
    cust = customer_service.resolve(db, customer)
    today = date.today()

    balance = money(cust.balance)
    overdue = money(cust.overdue_balance)

    # 1) Overdue ratio ------------------------------------------------------
    overdue_ratio = clamp(safe_div(overdue, balance), 0.0, 1.0) if balance > 0 else 0.0
    score = overdue_ratio * 30.0

    # 2) Worst days past due -----------------------------------------------
    oldest_due = db.execute(
        select(func.min(func.coalesce(CustomerLedger.due_date, CustomerLedger.entry_date)))
        .where(
            CustomerLedger.customer_id == cust.id,
            CustomerLedger.open_amount > 0,
        )
    ).scalar_one_or_none()
    days_past_due = (today - oldest_due).days if oldest_due else 0
    score += clamp(days_past_due / 90.0, 0.0, 1.0) * 25.0

    # 3) Payment irregularity ----------------------------------------------
    intervals = _payment_intervals(db, cust.id)
    if len(intervals) >= 2:
        mean_gap = statistics.fmean(intervals)
        cv = statistics.pstdev(intervals) / mean_gap if mean_gap > 0 else 0.0
        score += clamp(cv, 0.0, 1.0) * 15.0
    elif balance > 0:
        # No track record but money outstanding — treat as medium uncertainty.
        score += 7.5

    # 4) Credit utilisation -------------------------------------------------
    limit = money(cust.credit_limit)
    if limit > 0:
        score += clamp(safe_div(balance, limit), 0.0, 1.0) * 20.0

    # 5) Bounced instruments ------------------------------------------------
    bounced = int(
        db.execute(
            select(func.count(Payment.id)).where(
                Payment.customer_id == cust.id,
                Payment.status == PaymentStatus.BOUNCED,
                Payment.payment_date >= today - timedelta(days=_BOUNCE_WINDOW_DAYS),
                Payment.is_deleted.is_(False),
            )
        ).scalar_one()
        or 0
    )
    score += min(10.0, bounced * 5.0)

    return round(clamp(score, 0.0, 100.0), 2)


def risk_detail(db: Session, customer: Customer | int) -> dict[str, Any]:
    """Score plus the inputs that produced it, for the risk screen."""
    cust = customer_service.resolve(db, customer)
    today = date.today()
    score = risk_score(db, cust)

    oldest_due = db.execute(
        select(func.min(func.coalesce(CustomerLedger.due_date, CustomerLedger.entry_date)))
        .where(CustomerLedger.customer_id == cust.id, CustomerLedger.open_amount > 0)
    ).scalar_one_or_none()
    bounced = int(
        db.execute(
            select(func.count(Payment.id)).where(
                Payment.customer_id == cust.id,
                Payment.status == PaymentStatus.BOUNCED,
                Payment.payment_date >= today - timedelta(days=_BOUNCE_WINDOW_DAYS),
                Payment.is_deleted.is_(False),
            )
        ).scalar_one()
        or 0
    )
    limit = money(cust.credit_limit)
    balance = money(cust.balance)
    intervals = _payment_intervals(db, cust.id)

    if score >= 70:
        band = "CRITICAL"
    elif score >= 50:
        band = "HIGH"
    elif score >= 30:
        band = "MEDIUM"
    else:
        band = "LOW"

    return {
        "customer_id": cust.id,
        "customer_code": cust.code,
        "customer_name": cust.name,
        "risk_score": score,
        "risk_band": band,
        "balance": balance,
        "overdue_balance": money(cust.overdue_balance),
        "credit_limit": limit,
        "credit_utilisation_percent": round(clamp(safe_div(balance, limit) * 100, 0.0, 999.0), 2)
        if limit > 0
        else 0.0,
        "days_past_due": (today - oldest_due).days if oldest_due else 0,
        "bounced_payments_180d": bounced,
        "average_payment_interval_days": (
            round(statistics.fmean(intervals), 1) if intervals else None
        ),
        "last_payment_date": cust.last_payment_date,
        "aging": aging(db, customer_id=cust.id),
    }


def refresh_risk(db: Session, customer: Customer | int, *, commit: bool = False) -> float:
    """Recompute and persist ``customer.risk_score`` with its timestamp."""
    cust = customer_service.resolve(db, customer)
    score = risk_score(db, cust)
    cust.risk_score = score
    cust.risk_updated_at = utcnow()
    db.flush()
    if commit:
        db.commit()
    return score


# ===========================================================================
# Collection work lists
# ===========================================================================
def top_debtors(
    db: Session,
    limit: int = 20,
    *,
    salesperson_ids: Sequence[int] | None = None,
    min_amount: Decimal | float | str = 0,
) -> list[dict[str, Any]]:
    """Customers with the largest open balance, worst first."""
    stmt = select(Customer).where(
        Customer.is_deleted.is_(False),
        Customer.balance > money(min_amount),
    )
    if salesperson_ids:
        stmt = stmt.where(Customer.default_salesperson_id.in_(list(salesperson_ids)))
    stmt = stmt.order_by(Customer.balance.desc()).limit(max(1, int(limit)))

    return [
        {
            "customer_id": c.id,
            "customer_code": c.code,
            "customer_name": c.name,
            "balance": money(c.balance),
            "overdue_balance": money(c.overdue_balance),
            "credit_limit": money(c.credit_limit),
            "risk_score": float(c.risk_score or 0.0),
            "last_payment_date": c.last_payment_date,
            "salesperson_id": c.default_salesperson_id,
            "phone": c.phone or c.mobile,
        }
        for c in db.execute(stmt).scalars().unique().all()
    ]


def overdue_list(
    db: Session,
    *,
    min_days: int = 1,
    salesperson_ids: Sequence[int] | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """
    Open items at least ``min_days`` past due, oldest first.

    One row per ledger item (not per customer) because a collector chases
    individual invoices, and the customer may have some current and some very
    late documents at the same time.
    """
    today = date.today()
    cutoff = today - timedelta(days=max(0, int(min_days)))

    stmt = (
        select(CustomerLedger, Customer)
        .join(Customer, Customer.id == CustomerLedger.customer_id)
        .where(
            CustomerLedger.open_amount > 0,
            Customer.is_deleted.is_(False),
            or_(
                CustomerLedger.due_date <= cutoff,
                CustomerLedger.due_date.is_(None),
            ),
        )
    )
    if salesperson_ids:
        stmt = stmt.where(Customer.default_salesperson_id.in_(list(salesperson_ids)))
    stmt = stmt.order_by(
        func.coalesce(CustomerLedger.due_date, CustomerLedger.entry_date).asc()
    ).limit(max(1, int(limit)) * 2)

    rows: list[dict[str, Any]] = []
    for entry, customer in db.execute(stmt).unique().all():
        effective_due = entry.due_date or entry.entry_date
        days = (today - effective_due).days if effective_due else 0
        if days < int(min_days):
            continue
        rows.append(
            {
                "ledger_id": entry.id,
                "customer_id": customer.id,
                "customer_code": customer.code,
                "customer_name": customer.name,
                "phone": customer.phone or customer.mobile,
                "salesperson_id": customer.default_salesperson_id,
                "entry_type": entry.entry_type,
                "entry_date": entry.entry_date,
                "due_date": entry.due_date,
                "reference_no": entry.reference_no,
                "open_amount": money(entry.open_amount),
                "days_past_due": days,
                "bucket": _bucket_for(days),
            }
        )
        if len(rows) >= int(limit):
            break
    return rows
