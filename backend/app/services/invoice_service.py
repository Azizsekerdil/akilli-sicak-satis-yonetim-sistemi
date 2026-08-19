"""
Fiscal documents: invoices (fatura), waybills (irsaliye) and credit notes
(iade faturası).

An invoice is the *receivable*: ``open_amount`` is what is still collectable
and is the only figure ageing and collection worklists ever look at.  Payments
never touch ``total_amount`` — they move money from ``open_amount`` into
``paid_amount`` so the original document stays exactly as it was issued.

Cross-module services are imported inside the functions that use them; the
service layer is mutually referential (invoices → ledger → customers → sales)
and deferring those imports keeps every module independently importable.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from app.core.enums import AuditAction, DocumentType, InvoiceStatus, LedgerEntryType
from app.core.exceptions import BusinessRuleError, ConflictError, NotFoundError
from app.core.logging_config import get_logger
from app.core.utils import money
from app.models.base import utcnow
from app.models.customer import Customer
from app.models.product import Product
from app.models.sales import Invoice, InvoiceItem, ReturnDocument, Sale
from app.services import audit_service, numbering_service

log = get_logger("app.sales.invoice")

#: Numbering sequence per document type — waybills and credit notes are
#: legally distinct series from ordinary invoices.
_NUMBER_KEY: dict[str, str] = {
    DocumentType.INVOICE: "INVOICE",
    DocumentType.WAYBILL: "WAYBILL",
    DocumentType.CREDIT_NOTE: "CREDIT_NOTE",
    DocumentType.RECEIPT: "INVOICE",
    DocumentType.PROFORMA: "INVOICE",
}

_CLOSED_STATUSES = (InvoiceStatus.CANCELLED,)


# ===========================================================================
# Creation
# ===========================================================================
def create_from_sale(
    db: Session,
    sale: Sale,
    *,
    document_type: str = DocumentType.INVOICE,
    user_id: int | None = None,
    invoice_date: date | None = None,
    with_waybill: bool = True,
) -> Invoice:
    """
    Issue the fiscal document for a delivered sale.

    The due date is derived from the customer's payment terms, not from the
    payment method: a cash customer with 0 terms gets ``due_date == today`` and
    therefore shows as collectable immediately, which is what the field app
    needs to prompt for money at the door.
    """
    if sale.is_cancelled:
        raise BusinessRuleError("sale.cancelled", params={"sale_no": sale.sale_no})

    customer = db.get(Customer, sale.customer_id)
    if customer is None:
        raise NotFoundError("customer.not_found", params={"id": sale.customer_id})

    doc_type = str(document_type)
    on = invoice_date or sale.sale_date or date.today()
    term_days = int(customer.payment_term_days or 0)

    invoice = Invoice(
        invoice_no=numbering_service.next_number(db, _NUMBER_KEY.get(doc_type, "INVOICE"), on=on),
        document_type=doc_type,
        status=InvoiceStatus.ISSUED,
        sale_id=sale.id,
        customer_id=sale.customer_id,
        salesperson_id=sale.salesperson_id,
        invoice_date=on,
        due_date=on + timedelta(days=term_days),
        issued_at=utcnow(),
        currency=sale.currency,
        net_amount=money(sale.net_amount),
        discount_amount=money(sale.discount_amount + sale.campaign_discount_amount),
        vat_amount=money(sale.vat_amount),
        excise_amount=money(sale.excise_amount),
        total_amount=money(sale.total_amount),
        paid_amount=Decimal("0"),
        open_amount=money(sale.total_amount),
        created_by_id=user_id,
        updated_by_id=user_id,
    )
    # The van hands over goods and paperwork at the same moment, so the
    # delivery note number is issued together with the invoice and kept on the
    # document rather than in a separate row nobody would ever query.
    if with_waybill and doc_type == DocumentType.INVOICE:
        invoice.serial = numbering_service.next_number(db, "WAYBILL", on=on)[:16]

    db.add(invoice)
    db.flush()

    # Appended through the relationship so ``invoice.items`` is populated for
    # the caller without a re-query in the same session.
    for line_no, item in enumerate(sale.items, start=1):
        product = db.get(Product, item.product_id)
        invoice.items.append(
            InvoiceItem(
                product_id=item.product_id,
                line_no=line_no,
                description=(product.name if product else None),
                quantity=item.quantity,
                uom=item.uom,
                unit_price=item.unit_price,
                discount_amount=money(item.discount_amount + item.campaign_discount_amount),
                net_amount=item.net_amount,
                vat_rate=item.vat_rate,
                vat_amount=item.vat_amount,
                total_amount=item.total_amount,
            )
        )
    db.flush()

    audit_service.record(
        db,
        AuditAction.CREATE,
        entity_type="Invoice",
        entity_id=invoice.id,
        entity_label=invoice.invoice_no,
        user_id=user_id,
        summary=f"{doc_type} {invoice.invoice_no} for sale {sale.sale_no}",
        amount=invoice.total_amount,
        new_values={
            "invoice_no": invoice.invoice_no,
            "document_type": doc_type,
            "customer_id": invoice.customer_id,
            "total_amount": str(invoice.total_amount),
            "due_date": invoice.due_date.isoformat() if invoice.due_date else None,
        },
    )
    return invoice


def create_credit_note(
    db: Session,
    return_doc: ReturnDocument,
    *,
    user_id: int | None = None,
) -> Invoice:
    """
    Issue the credit note for a posted return.

    Amounts are stored **positive** and the direction is carried by
    ``document_type``: keeping every fiscal figure non-negative means reports
    can sum invoices and credit notes separately without sign gymnastics, and
    the ledger entry (a credit) is what actually reduces the customer's debt.
    ``open_amount`` is zero because a credit note is not collectable.
    """
    customer = db.get(Customer, return_doc.customer_id)
    if customer is None:
        raise NotFoundError("customer.not_found", params={"id": return_doc.customer_id})

    on = return_doc.return_date or date.today()
    note = Invoice(
        invoice_no=numbering_service.next_number(db, "CREDIT_NOTE", on=on),
        document_type=DocumentType.CREDIT_NOTE,
        status=InvoiceStatus.ISSUED,
        sale_id=return_doc.sale_id,
        return_id=return_doc.id,
        customer_id=return_doc.customer_id,
        salesperson_id=return_doc.salesperson_id,
        invoice_date=on,
        due_date=on,
        issued_at=utcnow(),
        currency=return_doc.currency,
        net_amount=money(return_doc.net_amount),
        discount_amount=Decimal("0"),
        vat_amount=money(return_doc.vat_amount),
        excise_amount=Decimal("0"),
        total_amount=money(return_doc.total_amount),
        paid_amount=Decimal("0"),
        open_amount=Decimal("0"),
        notes=return_doc.notes,
        created_by_id=user_id,
        updated_by_id=user_id,
    )
    db.add(note)
    db.flush()

    for line_no, item in enumerate(return_doc.items, start=1):
        product = db.get(Product, item.product_id)
        note.items.append(
            InvoiceItem(
                product_id=item.product_id,
                line_no=line_no,
                description=(product.name if product else None),
                quantity=item.quantity,
                uom=item.uom,
                unit_price=item.unit_price,
                discount_amount=Decimal("0"),
                net_amount=item.net_amount,
                vat_rate=item.vat_rate,
                vat_amount=item.vat_amount,
                total_amount=item.total_amount,
            )
        )
    db.flush()

    audit_service.record(
        db,
        AuditAction.CREATE,
        entity_type="Invoice",
        entity_id=note.id,
        entity_label=note.invoice_no,
        user_id=user_id,
        summary=f"CREDIT_NOTE {note.invoice_no} for return {return_doc.return_no}",
        amount=note.total_amount,
        new_values={
            "invoice_no": note.invoice_no,
            "return_id": return_doc.id,
            "total_amount": str(note.total_amount),
        },
    )
    return note


# ===========================================================================
# Status maintenance
# ===========================================================================
def refresh_status(db: Session, invoice: Invoice, *, as_of: date | None = None) -> str:
    """
    Recompute ``status`` and ``open_amount`` from what has actually been paid.

    Called after every allocation and every reversal, so the receivable state
    is never inferred at read time.
    """
    if invoice.status == InvoiceStatus.CANCELLED:
        return invoice.status

    total = money(invoice.total_amount)
    paid = money(invoice.paid_amount)
    if paid < 0:
        paid = Decimal("0")
    invoice.paid_amount = paid
    invoice.open_amount = money(max(Decimal("0"), total - paid))

    ref = as_of or date.today()
    if invoice.open_amount <= 0 and total > 0:
        invoice.status = InvoiceStatus.PAID
    elif invoice.due_date and invoice.due_date < ref:
        invoice.status = InvoiceStatus.OVERDUE
    elif paid > 0:
        invoice.status = InvoiceStatus.PARTIALLY_PAID
    else:
        invoice.status = InvoiceStatus.ISSUED
    db.flush()
    return invoice.status


def mark_paid(db: Session, invoice: Invoice, *, user_id: int | None = None) -> Invoice:
    """Force-settle an invoice (write-off / manual reconciliation)."""
    invoice.paid_amount = money(invoice.total_amount)
    invoice.open_amount = Decimal("0")
    invoice.status = InvoiceStatus.PAID
    invoice.updated_by_id = user_id
    db.flush()
    audit_service.record(
        db,
        AuditAction.UPDATE,
        entity_type="Invoice",
        entity_id=invoice.id,
        entity_label=invoice.invoice_no,
        user_id=user_id,
        summary=f"Invoice {invoice.invoice_no} marked paid",
        amount=invoice.total_amount,
    )
    return invoice


def refresh_overdue(db: Session, *, as_of: date | None = None) -> int:
    """Flip ISSUED/PARTIALLY_PAID invoices past their due date to OVERDUE."""
    ref = as_of or date.today()
    rows = db.execute(
        select(Invoice).where(
            Invoice.is_deleted.is_(False),
            Invoice.open_amount > 0,
            Invoice.due_date.is_not(None),
            Invoice.due_date < ref,
            Invoice.status.in_((InvoiceStatus.ISSUED, InvoiceStatus.PARTIALLY_PAID)),
        )
    ).scalars().all()
    for inv in rows:
        inv.status = InvoiceStatus.OVERDUE
    db.flush()
    return len(rows)


# ===========================================================================
# Cancellation
# ===========================================================================
def cancel(
    db: Session,
    invoice: Invoice,
    *,
    reason: str,
    user_id: int | None = None,
) -> Invoice:
    """
    Cancel an issued document and reverse its ledger effect.

    A partially or fully collected invoice may not be cancelled — the money is
    already in the till, so the correct instrument is a credit note.
    """
    from app.services import customer_service, ledger_service

    if invoice.status == InvoiceStatus.CANCELLED:
        raise ConflictError("invoice.already_cancelled", params={"no": invoice.invoice_no})
    if money(invoice.paid_amount) > 0:
        raise BusinessRuleError("invoice.cannot_cancel_paid", params={"no": invoice.invoice_no})

    old = {
        "status": invoice.status,
        "open_amount": str(invoice.open_amount),
        "total_amount": str(invoice.total_amount),
    }

    if invoice.document_type != DocumentType.CREDIT_NOTE and money(invoice.total_amount) > 0:
        ledger_service.post_entry(
            db,
            customer_id=invoice.customer_id,
            entry_type=LedgerEntryType.CREDIT_NOTE,
            entry_date=date.today(),
            credit=money(invoice.total_amount),
            reference_type="INVOICE_CANCEL",
            reference_id=invoice.id,
            reference_no=invoice.invoice_no,
            salesperson_id=invoice.salesperson_id,
            description=reason,
            user_id=user_id,
        )

    invoice.status = InvoiceStatus.CANCELLED
    invoice.open_amount = Decimal("0")
    invoice.cancelled_at = utcnow()
    invoice.cancel_reason = reason[:255]
    invoice.updated_by_id = user_id
    db.flush()

    customer_service.recalc_balance(db, invoice.customer_id)

    audit_service.record(
        db,
        AuditAction.CANCEL,
        entity_type="Invoice",
        entity_id=invoice.id,
        entity_label=invoice.invoice_no,
        user_id=user_id,
        summary=f"Invoice {invoice.invoice_no} cancelled: {reason}",
        amount=invoice.total_amount,
        old_values=old,
        new_values={"status": invoice.status, "reason": reason},
    )
    return invoice


# ===========================================================================
# Queries
# ===========================================================================
def get(db: Session, invoice_id: int) -> Invoice:
    inv = db.get(Invoice, invoice_id)
    if inv is None or inv.is_deleted:
        raise NotFoundError("invoice.not_found", params={"id": invoice_id})
    return inv


def outstanding(
    db: Session,
    customer_id: int,
    *,
    as_of: date | None = None,
    include_credit_notes: bool = False,
) -> list[Invoice]:
    """Open receivables for one customer, oldest first — the FIFO pay order."""
    stmt = (
        select(Invoice)
        .where(
            Invoice.customer_id == customer_id,
            Invoice.is_deleted.is_(False),
            Invoice.status.not_in(_CLOSED_STATUSES),
            Invoice.open_amount > 0,
        )
        # coalesce keeps the ordering portable: SQLite and PostgreSQL disagree
        # on where NULL due dates sort, and FIFO collection must be stable.
        .order_by(
            func.coalesce(Invoice.due_date, Invoice.invoice_date).asc(),
            Invoice.invoice_date.asc(),
            Invoice.id.asc(),
        )
    )
    if not include_credit_notes:
        stmt = stmt.where(Invoice.document_type != DocumentType.CREDIT_NOTE)
    if as_of is not None:
        stmt = stmt.where(Invoice.invoice_date <= as_of)
    return list(db.execute(stmt).scalars().all())


def overdue(
    db: Session,
    *,
    as_of: date | None = None,
    customer_id: int | None = None,
    salesperson_ids: list[int] | None = None,
) -> list[Invoice]:
    """Every receivable past its due date — the collection worklist."""
    ref = as_of or date.today()
    stmt = (
        select(Invoice)
        .where(
            Invoice.is_deleted.is_(False),
            Invoice.status.not_in(_CLOSED_STATUSES),
            Invoice.open_amount > 0,
            Invoice.due_date.is_not(None),
            Invoice.due_date < ref,
            Invoice.document_type != DocumentType.CREDIT_NOTE,
        )
        .order_by(Invoice.due_date.asc(), Invoice.id.asc())
    )
    if customer_id:
        stmt = stmt.where(Invoice.customer_id == customer_id)
    if salesperson_ids:
        stmt = stmt.where(Invoice.salesperson_id.in_(salesperson_ids))
    return list(db.execute(stmt).scalars().all())


def total_outstanding(db: Session, customer_id: int) -> Decimal:
    value = db.execute(
        select(func.coalesce(func.sum(Invoice.open_amount), 0)).where(
            Invoice.customer_id == customer_id,
            Invoice.is_deleted.is_(False),
            Invoice.status.not_in(_CLOSED_STATUSES),
        )
    ).scalar_one()
    return money(value)


def _apply_filters(
    stmt: Select[Any],
    *,
    customer_id: int | None,
    salesperson_id: int | None,
    sale_id: int | None,
    document_type: str | None,
    status: str | None,
    start: date | None,
    end: date | None,
    only_open: bool,
    search: str | None,
    salesperson_ids: list[int] | None,
) -> Select[Any]:
    stmt = stmt.where(Invoice.is_deleted.is_(False))
    if customer_id:
        stmt = stmt.where(Invoice.customer_id == customer_id)
    if salesperson_id:
        stmt = stmt.where(Invoice.salesperson_id == salesperson_id)
    if sale_id:
        stmt = stmt.where(Invoice.sale_id == sale_id)
    if document_type:
        stmt = stmt.where(Invoice.document_type == str(document_type))
    if status:
        stmt = stmt.where(Invoice.status == str(status))
    if start:
        stmt = stmt.where(Invoice.invoice_date >= start)
    if end:
        stmt = stmt.where(Invoice.invoice_date <= end)
    if only_open:
        stmt = stmt.where(Invoice.open_amount > 0, Invoice.status.not_in(_CLOSED_STATUSES))
    if search:
        term = f"%{search.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Invoice.invoice_no).like(term),
                func.lower(func.coalesce(Invoice.serial, "")).like(term),
            )
        )
    if salesperson_ids:
        stmt = stmt.where(Invoice.salesperson_id.in_(salesperson_ids))
    return stmt


def list_invoices(
    db: Session,
    *,
    customer_id: int | None = None,
    salesperson_id: int | None = None,
    sale_id: int | None = None,
    document_type: str | None = None,
    status: str | None = None,
    start: date | None = None,
    end: date | None = None,
    only_open: bool = False,
    search: str | None = None,
    salesperson_ids: list[int] | None = None,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[Invoice], int]:
    """Paged invoice list plus the unpaged total, for the standard envelope."""
    filters = dict(
        customer_id=customer_id,
        salesperson_id=salesperson_id,
        sale_id=sale_id,
        document_type=document_type,
        status=status,
        start=start,
        end=end,
        only_open=only_open,
        search=search,
        salesperson_ids=salesperson_ids,
    )
    total = db.execute(
        _apply_filters(select(func.count(Invoice.id)), **filters)  # type: ignore[arg-type]
    ).scalar_one()
    rows = db.execute(
        _apply_filters(select(Invoice), **filters)  # type: ignore[arg-type]
        .order_by(Invoice.invoice_date.desc(), Invoice.id.desc())
        .offset(offset)
        .limit(limit)
    ).scalars().all()
    return list(rows), int(total)


def days_overdue(invoice: Invoice, *, as_of: date | None = None) -> int:
    if not invoice.due_date or invoice.open_amount <= 0:
        return 0
    delta = (as_of or date.today()) - invoice.due_date
    return max(0, delta.days)


def issued_at_or_now(invoice: Invoice) -> datetime:
    return invoice.issued_at or utcnow()
