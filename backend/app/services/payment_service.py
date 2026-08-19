"""
Collections (tahsilat): recording money, allocating it to invoices and
handling instruments that are promises rather than cash.

Two rules drive everything here:

1. **Cash-basis for instruments.**  A cheque or promissory note is a *promise*.
   It is recorded immediately (so the field is auditable and the customer can
   see it) but it is ``PENDING``: it allocates nothing, posts no ledger entry
   and does not reduce the balance until it clears.  Bouncing one has to be
   able to undo the settlement cleanly, which is only possible if the
   settlement happened in exactly one place — :func:`_settle`.

2. **FIFO allocation.**  Money pays the oldest open invoice first unless the
   caller names specific invoices.  That matches how ageing buckets are read
   and stops old debt from being hidden behind fresh invoices.

Cross-module services are imported inside the functions that use them; the
service layer is mutually referential and deferring those imports keeps every
module independently importable.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from app.core.enums import (
    AuditAction,
    InvoiceStatus,
    LedgerEntryType,
    PaymentMethod,
    PaymentStatus,
)
from app.core.exceptions import BusinessRuleError, ConflictError, NotFoundError, ValidationError
from app.core.logging_config import get_logger
from app.core.utils import money
from app.models.base import utcnow
from app.models.customer import Customer
from app.models.sales import Invoice, Payment, PaymentAllocation, Sale
from app.services import audit_service, invoice_service, numbering_service

log = get_logger("app.sales.payment")

#: Instruments that only become money later — recorded PENDING, settled on clearing.
DEFERRED_METHODS: frozenset[str] = frozenset(
    {PaymentMethod.CHEQUE, PaymentMethod.PROMISSORY_NOTE}
)

#: Methods counted as physical cash in the van at end of day.
CASH_METHODS: frozenset[str] = frozenset({PaymentMethod.CASH})


# ===========================================================================
# Recording
# ===========================================================================
def record_payment(
    db: Session,
    *,
    customer_id: int,
    amount: Decimal,
    payment_method: str = PaymentMethod.CASH,
    payment_date: date | None = None,
    salesperson_id: int | None = None,
    sale_id: int | None = None,
    visit_id: int | None = None,
    day_session_id: int | None = None,
    invoice_ids: list[int] | None = None,
    bank_name: str | None = None,
    document_number: str | None = None,
    maturity_date: date | None = None,
    drawer_name: str | None = None,
    reference: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    notes: str | None = None,
    user_id: int | None = None,
) -> Payment:
    """
    Record a collection and, when it is real money, settle it against invoices.

    Does not commit — the caller's transaction owns the whole hot-sale or
    collection operation.
    """
    value = money(amount)
    if value <= 0:
        raise ValidationError("payment.amount_positive")

    customer = db.get(Customer, customer_id)
    if customer is None or customer.is_deleted:
        raise NotFoundError("customer.not_found", params={"id": customer_id})

    method = str(payment_method)
    on = payment_date or date.today()
    is_deferred = method in DEFERRED_METHODS

    payment = Payment(
        payment_no=numbering_service.next_number(db, "PAYMENT", on=on),
        customer_id=customer_id,
        salesperson_id=salesperson_id,
        sale_id=sale_id,
        visit_id=visit_id,
        day_session_id=day_session_id,
        payment_date=on,
        received_at=utcnow(),
        payment_method=method,
        status=PaymentStatus.PENDING if is_deferred else PaymentStatus.CLEARED,
        currency=customer.currency or "TRY",
        amount=value,
        allocated_amount=Decimal("0"),
        unallocated_amount=value,
        bank_name=bank_name,
        document_number=document_number,
        maturity_date=maturity_date,
        drawer_name=drawer_name,
        reference=reference,
        latitude=latitude,
        longitude=longitude,
        notes=notes,
        created_by_id=user_id,
        updated_by_id=user_id,
    )
    db.add(payment)
    db.flush()

    if not is_deferred:
        _settle(db, payment, invoice_ids=invoice_ids, user_id=user_id)

    audit_service.record(
        db,
        AuditAction.PAYMENT,
        entity_type="Payment",
        entity_id=payment.id,
        entity_label=payment.payment_no,
        user_id=user_id,
        summary=f"{method} collection {payment.payment_no} from customer {customer.code}",
        amount=value,
        new_values={
            "payment_no": payment.payment_no,
            "customer_id": customer_id,
            "method": method,
            "status": payment.status,
            "amount": str(value),
            "allocated": str(payment.allocated_amount),
        },
    )
    return payment


# ===========================================================================
# Settlement / reversal
# ===========================================================================
def _settle(
    db: Session,
    payment: Payment,
    *,
    invoice_ids: list[int] | None = None,
    user_id: int | None = None,
) -> list[PaymentAllocation]:
    """
    Apply a cleared payment: allocate to invoices, post the ledger credit and
    move the customer's denormalised balance.

    Single point of truth so :func:`bounce_payment` can undo it exactly.
    """
    from app.services import customer_service, ledger_service

    targets = _allocation_targets(db, payment, invoice_ids)
    remaining = money(payment.amount)
    allocations: list[PaymentAllocation] = []

    for invoice in targets:
        if remaining <= 0:
            break
        take = money(min(remaining, money(invoice.open_amount)))
        if take <= 0:
            continue
        allocations.append(_allocate(db, payment, invoice, take, user_id=user_id))
        remaining = money(remaining - take)

    payment.allocated_amount = money(payment.amount - remaining)
    payment.unallocated_amount = money(remaining)
    db.flush()

    ledger_service.post_entry(
        db,
        customer_id=payment.customer_id,
        entry_type=LedgerEntryType.PAYMENT,
        entry_date=payment.payment_date,
        credit=money(payment.amount),
        reference_type="PAYMENT",
        reference_id=payment.id,
        reference_no=payment.payment_no,
        salesperson_id=payment.salesperson_id,
        description=payment.notes or payment.payment_method,
        user_id=user_id,
    )

    customer = db.get(Customer, payment.customer_id)
    if customer is not None:
        customer.total_paid_amount = money(customer.total_paid_amount + payment.amount)
        if customer.last_payment_date is None or payment.payment_date > customer.last_payment_date:
            customer.last_payment_date = payment.payment_date
    customer_service.recalc_balance(db, payment.customer_id)

    if payment.sale_id:
        _sync_sale_paid(db, payment.sale_id)

    db.flush()
    return allocations


def _allocation_targets(
    db: Session, payment: Payment, invoice_ids: list[int] | None
) -> list[Invoice]:
    """Named invoices in the order given, otherwise the oldest open ones."""
    if invoice_ids:
        rows = db.execute(
            select(Invoice).where(
                Invoice.id.in_(invoice_ids),
                Invoice.customer_id == payment.customer_id,
                Invoice.is_deleted.is_(False),
                Invoice.status != InvoiceStatus.CANCELLED,
            )
        ).scalars().all()
        by_id = {row.id: row for row in rows}
        missing = [i for i in invoice_ids if i not in by_id]
        if missing:
            raise NotFoundError("invoice.not_found", params={"id": missing[0]})
        return [by_id[i] for i in invoice_ids]

    # A payment taken at the door pays that door's invoice first; only then
    # does the rest roll onto older debt.
    ordered = invoice_service.outstanding(db, payment.customer_id)
    if payment.sale_id:
        ordered.sort(key=lambda inv: (inv.sale_id != payment.sale_id,))
    return ordered


def _allocate(
    db: Session,
    payment: Payment,
    invoice: Invoice,
    take: Decimal,
    *,
    user_id: int | None = None,
) -> PaymentAllocation:
    existing = db.execute(
        select(PaymentAllocation).where(
            PaymentAllocation.payment_id == payment.id,
            PaymentAllocation.invoice_id == invoice.id,
        )
    ).scalar_one_or_none()

    if existing is None:
        existing = PaymentAllocation(
            payment_id=payment.id, invoice_id=invoice.id, amount=take
        )
        db.add(existing)
    else:
        existing.amount = money(existing.amount + take)

    invoice.paid_amount = money(invoice.paid_amount + take)
    invoice.updated_by_id = user_id
    db.flush()
    invoice_service.refresh_status(db, invoice)
    return existing


def _unallocate(db: Session, payment: Payment, *, user_id: int | None = None) -> Decimal:
    """Roll back every allocation of a payment, restoring the invoices."""
    released = Decimal("0")
    for alloc in list(payment.allocations):
        invoice = db.get(Invoice, alloc.invoice_id)
        if invoice is not None:
            invoice.paid_amount = money(invoice.paid_amount - alloc.amount)
            invoice.updated_by_id = user_id
            db.flush()
            invoice_service.refresh_status(db, invoice)
        released = money(released + alloc.amount)
        db.delete(alloc)
    payment.allocated_amount = Decimal("0")
    payment.unallocated_amount = money(payment.amount)
    db.flush()
    return released


def _sync_sale_paid(db: Session, sale_id: int) -> None:
    """Keep ``Sale.paid_amount`` in step with the invoices raised for it."""
    sale = db.get(Sale, sale_id)
    if sale is None:
        return
    paid = db.execute(
        select(func.coalesce(func.sum(Invoice.paid_amount), 0)).where(
            Invoice.sale_id == sale_id,
            Invoice.is_deleted.is_(False),
            Invoice.status != InvoiceStatus.CANCELLED,
        )
    ).scalar_one()
    sale.paid_amount = money(paid)
    sale.due_amount = money(max(Decimal("0"), sale.total_amount - sale.paid_amount))
    db.flush()


# ===========================================================================
# Instrument lifecycle
# ===========================================================================
def clear_payment(
    db: Session,
    payment: Payment,
    *,
    user_id: int | None = None,
    invoice_ids: list[int] | None = None,
) -> Payment:
    """A cheque/note has been honoured — it becomes money and settles now."""
    if payment.status == PaymentStatus.CLEARED:
        raise ConflictError("payment.already_cleared", params={"no": payment.payment_no})
    if payment.status == PaymentStatus.CANCELLED:
        raise BusinessRuleError("payment.cancelled", params={"no": payment.payment_no})

    old_status = payment.status
    payment.status = PaymentStatus.CLEARED
    payment.bounced_at = None
    payment.updated_by_id = user_id
    db.flush()

    _settle(db, payment, invoice_ids=invoice_ids, user_id=user_id)

    audit_service.record(
        db,
        AuditAction.PAYMENT,
        entity_type="Payment",
        entity_id=payment.id,
        entity_label=payment.payment_no,
        user_id=user_id,
        summary=f"Payment {payment.payment_no} cleared",
        amount=payment.amount,
        old_values={"status": old_status},
        new_values={"status": payment.status},
    )
    return payment


def bounce_payment(
    db: Session,
    payment: Payment,
    *,
    reason: str | None = None,
    user_id: int | None = None,
) -> Payment:
    """
    A cheque came back unpaid (karşılıksız).

    If it had already been treated as money, every effect is reversed: the
    allocations are released, a debit note puts the amount back on the ledger
    and the customer's balance rises again.
    """
    from app.services import customer_service, ledger_service

    if payment.status == PaymentStatus.BOUNCED:
        raise ConflictError("payment.already_bounced", params={"no": payment.payment_no})

    old_status = payment.status
    was_settled = old_status == PaymentStatus.CLEARED

    if was_settled:
        _unallocate(db, payment, user_id=user_id)
        ledger_service.post_entry(
            db,
            customer_id=payment.customer_id,
            entry_type=LedgerEntryType.DEBIT_NOTE,
            entry_date=date.today(),
            debit=money(payment.amount),
            reference_type="PAYMENT_BOUNCE",
            reference_id=payment.id,
            reference_no=payment.payment_no,
            salesperson_id=payment.salesperson_id,
            description=reason or "bounced",
            user_id=user_id,
        )
        customer = db.get(Customer, payment.customer_id)
        if customer is not None:
            customer.total_paid_amount = money(
                max(Decimal("0"), customer.total_paid_amount - payment.amount)
            )
        customer_service.recalc_balance(db, payment.customer_id)
        if payment.sale_id:
            _sync_sale_paid(db, payment.sale_id)

    payment.status = PaymentStatus.BOUNCED
    payment.bounced_at = utcnow()
    payment.allocated_amount = Decimal("0")
    payment.unallocated_amount = money(payment.amount)
    payment.updated_by_id = user_id
    if reason:
        payment.notes = f"{payment.notes}\n{reason}".strip() if payment.notes else reason
    db.flush()

    audit_service.record(
        db,
        AuditAction.PAYMENT,
        entity_type="Payment",
        entity_id=payment.id,
        entity_label=payment.payment_no,
        user_id=user_id,
        summary=f"Payment {payment.payment_no} bounced: {reason or ''}".strip(),
        amount=payment.amount,
        old_values={"status": old_status},
        new_values={"status": payment.status, "reversed": was_settled},
    )
    return payment


def cancel_payment(
    db: Session,
    payment: Payment,
    *,
    reason: str,
    user_id: int | None = None,
) -> Payment:
    """Void a mistaken collection, reversing it if it had already settled."""
    from app.services import customer_service, ledger_service

    if payment.status == PaymentStatus.CANCELLED:
        raise ConflictError("payment.already_cancelled", params={"no": payment.payment_no})

    old_status = payment.status
    if old_status == PaymentStatus.CLEARED:
        _unallocate(db, payment, user_id=user_id)
        ledger_service.post_entry(
            db,
            customer_id=payment.customer_id,
            entry_type=LedgerEntryType.DEBIT_NOTE,
            entry_date=date.today(),
            debit=money(payment.amount),
            reference_type="PAYMENT_CANCEL",
            reference_id=payment.id,
            reference_no=payment.payment_no,
            salesperson_id=payment.salesperson_id,
            description=reason,
            user_id=user_id,
        )
        customer = db.get(Customer, payment.customer_id)
        if customer is not None:
            customer.total_paid_amount = money(
                max(Decimal("0"), customer.total_paid_amount - payment.amount)
            )
        customer_service.recalc_balance(db, payment.customer_id)
        if payment.sale_id:
            _sync_sale_paid(db, payment.sale_id)

    payment.status = PaymentStatus.CANCELLED
    payment.is_deleted = True
    payment.deleted_at = utcnow()
    payment.deleted_by_id = user_id
    payment.updated_by_id = user_id
    db.flush()

    audit_service.record(
        db,
        AuditAction.CANCEL,
        entity_type="Payment",
        entity_id=payment.id,
        entity_label=payment.payment_no,
        user_id=user_id,
        summary=f"Payment {payment.payment_no} cancelled: {reason}",
        amount=payment.amount,
        old_values={"status": old_status},
        new_values={"status": payment.status, "reason": reason},
    )
    return payment


# ===========================================================================
# Queries
# ===========================================================================
def get(db: Session, payment_id: int) -> Payment:
    payment = db.get(Payment, payment_id)
    if payment is None or payment.is_deleted:
        raise NotFoundError("payment.not_found", params={"id": payment_id})
    return payment


def _apply_filters(
    stmt: Select[Any],
    *,
    customer_id: int | None,
    salesperson_id: int | None,
    sale_id: int | None,
    day_session_id: int | None,
    payment_method: str | None,
    status: str | None,
    start: date | None,
    end: date | None,
    search: str | None,
    salesperson_ids: list[int] | None,
) -> Select[Any]:
    stmt = stmt.where(Payment.is_deleted.is_(False))
    if customer_id:
        stmt = stmt.where(Payment.customer_id == customer_id)
    if salesperson_id:
        stmt = stmt.where(Payment.salesperson_id == salesperson_id)
    if sale_id:
        stmt = stmt.where(Payment.sale_id == sale_id)
    if day_session_id:
        stmt = stmt.where(Payment.day_session_id == day_session_id)
    if payment_method:
        stmt = stmt.where(Payment.payment_method == str(payment_method))
    if status:
        stmt = stmt.where(Payment.status == str(status))
    if start:
        stmt = stmt.where(Payment.payment_date >= start)
    if end:
        stmt = stmt.where(Payment.payment_date <= end)
    if search:
        term = f"%{search.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Payment.payment_no).like(term),
                func.lower(func.coalesce(Payment.document_number, "")).like(term),
                func.lower(func.coalesce(Payment.reference, "")).like(term),
                func.lower(func.coalesce(Payment.drawer_name, "")).like(term),
            )
        )
    if salesperson_ids:
        stmt = stmt.where(Payment.salesperson_id.in_(salesperson_ids))
    return stmt


def list_payments(
    db: Session,
    *,
    customer_id: int | None = None,
    salesperson_id: int | None = None,
    sale_id: int | None = None,
    day_session_id: int | None = None,
    payment_method: str | None = None,
    status: str | None = None,
    start: date | None = None,
    end: date | None = None,
    search: str | None = None,
    salesperson_ids: list[int] | None = None,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[Payment], int]:
    filters = dict(
        customer_id=customer_id,
        salesperson_id=salesperson_id,
        sale_id=sale_id,
        day_session_id=day_session_id,
        payment_method=payment_method,
        status=status,
        start=start,
        end=end,
        search=search,
        salesperson_ids=salesperson_ids,
    )
    total = db.execute(
        _apply_filters(select(func.count(Payment.id)), **filters)  # type: ignore[arg-type]
    ).scalar_one()
    rows = db.execute(
        _apply_filters(select(Payment), **filters)  # type: ignore[arg-type]
        .order_by(Payment.payment_date.desc(), Payment.id.desc())
        .offset(offset)
        .limit(limit)
    ).scalars().all()
    return list(rows), int(total)


def collections_summary(
    db: Session,
    *,
    start: date,
    end: date,
    salesperson_id: int | None = None,
    salesperson_ids: list[int] | None = None,
) -> dict[str, Any]:
    """Collected money grouped by instrument — the cash-up view."""
    stmt = (
        select(
            Payment.payment_method,
            Payment.status,
            func.count(Payment.id),
            func.coalesce(func.sum(Payment.amount), 0),
        )
        .where(
            Payment.is_deleted.is_(False),
            Payment.payment_date >= start,
            Payment.payment_date <= end,
            Payment.status != PaymentStatus.CANCELLED,
        )
        .group_by(Payment.payment_method, Payment.status)
        .order_by(Payment.payment_method.asc())
    )
    if salesperson_id:
        stmt = stmt.where(Payment.salesperson_id == salesperson_id)
    if salesperson_ids:
        stmt = stmt.where(Payment.salesperson_id.in_(salesperson_ids))

    by_method: list[dict[str, Any]] = []
    totals = {
        "total_amount": Decimal("0"),
        "cleared_amount": Decimal("0"),
        "pending_amount": Decimal("0"),
        "bounced_amount": Decimal("0"),
    }
    count = 0
    for method, status, rows, amount in db.execute(stmt).all():
        value = money(amount)
        by_method.append(
            {
                "payment_method": method,
                "status": status,
                "count": int(rows),
                "amount": value,
            }
        )
        count += int(rows)
        totals["total_amount"] = money(totals["total_amount"] + value)
        if status == PaymentStatus.CLEARED:
            totals["cleared_amount"] = money(totals["cleared_amount"] + value)
        elif status == PaymentStatus.PENDING:
            totals["pending_amount"] = money(totals["pending_amount"] + value)
        elif status == PaymentStatus.BOUNCED:
            totals["bounced_amount"] = money(totals["bounced_amount"] + value)

    return {
        "start": start,
        "end": end,
        "salesperson_id": salesperson_id,
        "count": count,
        "by_method": by_method,
        **totals,
    }
