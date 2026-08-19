"""
Customer returns (iade) and their stock disposition.

A return is two decisions in one document: *what came back* and *where it
goes*.  Only ``RESALEABLE`` goods rejoin sellable stock; ``QUARANTINE`` comes
back under a non-sellable status pending inspection; ``SCRAP`` is written off.

Scrap posts **two** movements — the goods really do come back onto the van
before they are destroyed, and recording only the write-off would drive the
van's balance negative for stock it demonstrably received.  Both movements
together keep the end-of-day identity
``loaded + reloaded - sold - returned - wastage = theoretical`` exact.

Cross-module services are imported inside the functions that use them; the
service layer is mutually referential and deferring those imports keeps every
module independently importable.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from app.core.enums import (
    AuditAction,
    LedgerEntryType,
    ReturnDisposition,
    ReturnReason,
    StockMovementType,
    StockStatus,
)
from app.core.exceptions import (
    BusinessRuleError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from app.core.logging_config import get_logger
from app.core.utils import money, qty, vat_from_net
from app.models.base import utcnow
from app.models.customer import Customer
from app.models.product import Product
from app.models.route import Visit
from app.models.sales import Invoice, ReturnDocument, ReturnItem, Sale, SaleItem
from app.models.vehicle import DaySession
from app.services import audit_service, invoice_service, numbering_service

log = get_logger("app.sales.return")


# ===========================================================================
# Creation
# ===========================================================================
def create_return(
    db: Session,
    *,
    customer_id: int,
    lines: Sequence[Mapping[str, Any] | Any],
    warehouse_id: int,
    reason: str = ReturnReason.OTHER,
    disposition: str = ReturnDisposition.RESALEABLE,
    sale_id: int | None = None,
    salesperson_id: int | None = None,
    vehicle_id: int | None = None,
    visit_id: int | None = None,
    day_session_id: int | None = None,
    creates_credit_note: bool = True,
    return_date: date | None = None,
    notes: str | None = None,
    user_id: int | None = None,
) -> ReturnDocument:
    """
    Record goods coming back, priced at what the customer actually paid.

    Pricing from the original sale line (when known) is deliberate: refunding a
    campaign-discounted case at today's list price would hand the customer free
    money every time a promotion ends.
    """
    from app.services import stock_service

    if not lines:
        raise ValidationError("return.empty")

    customer = db.get(Customer, customer_id)
    if customer is None or customer.is_deleted:
        raise NotFoundError("customer.not_found", params={"id": customer_id})

    sale: Sale | None = None
    if sale_id:
        sale = db.get(Sale, sale_id)
        if sale is None or sale.is_deleted:
            raise NotFoundError("sale.not_found", params={"id": sale_id})
        if sale.customer_id != customer_id:
            raise ValidationError("return.customer_mismatch", params={"sale_id": sale_id})

    on = return_date or date.today()
    doc = ReturnDocument(
        return_no=numbering_service.next_number(db, "RETURN", on=on),
        return_date=on,
        customer_id=customer_id,
        sale_id=sale_id,
        salesperson_id=salesperson_id,
        vehicle_id=vehicle_id,
        warehouse_id=warehouse_id,
        visit_id=visit_id,
        day_session_id=day_session_id,
        reason=str(reason),
        disposition=str(disposition),
        is_posted=False,
        creates_credit_note=bool(creates_credit_note),
        currency=customer.currency or "TRY",
        notes=notes,
        created_by_id=user_id,
        updated_by_id=user_id,
    )
    db.add(doc)
    db.flush()

    net_total = Decimal("0")
    vat_total = Decimal("0")
    grand_total = Decimal("0")
    cost_total = Decimal("0")

    for line_no, raw in enumerate(_normalise_lines(lines), start=1):
        product = db.get(Product, raw["product_id"])
        if product is None or product.is_deleted:
            raise NotFoundError("product.not_found", params={"id": raw["product_id"]})
        if not product.is_returnable:
            raise BusinessRuleError("return.product_not_returnable", params={"sku": product.sku})

        sale_item = _resolve_sale_item(db, raw["sale_item_id"], sale)
        line_qty = qty(raw["quantity"])
        uom = raw["uom"] or (sale_item.uom if sale_item else product.sales_uom)
        factor = qty(stock_service.uom_factor(product, uom))
        base_quantity = qty(stock_service.to_base(product, line_qty, uom))

        if sale_item is not None:
            _check_against_sale(sale_item, base_quantity)

        unit_price = _refund_unit_price(
            db,
            product=product,
            customer=customer,
            sale_item=sale_item,
            uom=uom,
            uom_factor=factor,
            quantity=line_qty,
            override=raw["unit_price"],
            on=on,
        )
        vat_rate = float(sale_item.vat_rate if sale_item else product.vat_rate or 0.0)
        # Costs are stored per **base** unit throughout the stock ledger.
        unit_cost = money(sale_item.unit_cost if sale_item else product.cost_price)

        net_amount = money(unit_price * line_qty)
        vat_amount = vat_from_net(net_amount, vat_rate)
        total_amount = money(net_amount + vat_amount)

        doc.items.append(
            ReturnItem(
                sale_item_id=sale_item.id if sale_item else None,
                product_id=product.id,
                lot_id=raw["lot_id"] or (sale_item.lot_id if sale_item else None),
                line_no=line_no,
                quantity=line_qty,
                uom=uom,
                uom_factor=factor,
                base_quantity=base_quantity,
                unit_price=unit_price,
                net_amount=net_amount,
                vat_rate=vat_rate,
                vat_amount=vat_amount,
                total_amount=total_amount,
                unit_cost=unit_cost,
                reason=str(raw["reason"] or reason),
                disposition=str(raw["disposition"] or disposition),
                expiry_date=raw["expiry_date"],
            )
        )
        net_total = money(net_total + net_amount)
        vat_total = money(vat_total + vat_amount)
        grand_total = money(grand_total + total_amount)
        cost_total = money(cost_total + unit_cost * base_quantity)

    doc.net_amount = net_total
    doc.vat_amount = vat_total
    doc.total_amount = grand_total
    doc.total_cost = cost_total
    doc.line_count = len(doc.items)
    db.flush()

    audit_service.record(
        db,
        AuditAction.CREATE,
        entity_type="ReturnDocument",
        entity_id=doc.id,
        entity_label=doc.return_no,
        user_id=user_id,
        summary=f"Return {doc.return_no} from customer {customer.code} ({doc.reason})",
        amount=doc.total_amount,
        new_values={
            "return_no": doc.return_no,
            "customer_id": customer_id,
            "sale_id": sale_id,
            "disposition": doc.disposition,
            "lines": doc.line_count,
            "total_amount": str(doc.total_amount),
        },
    )
    return doc


def _refund_unit_price(
    db: Session,
    *,
    product: Product,
    customer: Customer,
    sale_item: SaleItem | None,
    uom: str,
    uom_factor: Decimal,
    quantity: Decimal,
    override: Decimal | None,
    on: date,
) -> Decimal:
    """
    What one *uom* of the returned goods is worth back to the customer.

    The original sale line is the reference, but it may have been sold in a
    different packaging (a case sold, a single piece returned).  Converting
    through the price per **base** unit is what stops a one-bottle return being
    refunded at the price of a whole case.
    """
    from app.services import pricing_service

    if override is not None:
        return money(override)
    if sale_item is not None:
        sale_factor = qty(sale_item.uom_factor) or Decimal("1")
        return money(money(sale_item.unit_price) / sale_factor * uom_factor)

    price, _ = pricing_service.resolve_unit_price(
        db, product, uom=uom, customer=customer, quantity=quantity, on_date=on
    )
    return money(price)


def _normalise_lines(lines: Sequence[Mapping[str, Any] | Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in lines:
        if isinstance(raw, Mapping):
            get = raw.get
        else:
            get = lambda key, default=None: getattr(raw, key, default)  # noqa: E731
        product_id = int(get("product_id") or 0)
        quantity = qty(get("quantity") or 0)
        if product_id <= 0 or quantity <= 0:
            raise ValidationError("return.invalid_line", params={"product_id": product_id})
        unit_price = get("unit_price")
        out.append(
            {
                "product_id": product_id,
                "quantity": quantity,
                "uom": (get("uom") or "") or None,
                "sale_item_id": get("sale_item_id"),
                "lot_id": get("lot_id"),
                "unit_price": None if unit_price in (None, "") else money(unit_price),
                "reason": get("reason"),
                "disposition": get("disposition"),
                "expiry_date": get("expiry_date"),
            }
        )
    return out


def _resolve_sale_item(db: Session, sale_item_id: Any, sale: Sale | None) -> SaleItem | None:
    if not sale_item_id:
        return None
    item = db.get(SaleItem, int(sale_item_id))
    if item is None:
        raise NotFoundError("sale.item_not_found", params={"id": sale_item_id})
    if sale is not None and item.sale_id != sale.id:
        raise ValidationError("return.sale_item_mismatch", params={"id": sale_item_id})
    return item


def _check_against_sale(sale_item: SaleItem, base_quantity: Decimal) -> None:
    """
    A customer may never return more of a line than they were delivered.

    Compared in base units because the return may be booked in a different
    packaging (a single piece out of a delivered case).
    """
    sale_factor = qty(sale_item.uom_factor) or Decimal("1")
    already = qty(sale_item.returned_quantity) * sale_factor
    delivered = qty(sale_item.base_quantity)
    if qty(base_quantity + already) > delivered:
        raise BusinessRuleError(
            "return.quantity_exceeds_sale",
            params={
                "sale_item_id": sale_item.id,
                "delivered": str(delivered),
                "requested": str(base_quantity),
            },
        )


# ===========================================================================
# Posting
# ===========================================================================
def post_return(
    db: Session,
    return_doc: ReturnDocument,
    *,
    user_id: int | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """
    Move the goods and, when the return is credited, raise the credit note and
    reverse the receivable.
    """
    from app.services import customer_service, ledger_service

    if return_doc.is_posted:
        raise ConflictError("return.already_posted", params={"no": return_doc.return_no})
    if not return_doc.items:
        raise ValidationError("return.empty")

    try:
        movements = _post_stock(db, return_doc, user_id=user_id)
        _update_sale_items(db, return_doc)

        credit_note: Invoice | None = None
        if return_doc.creates_credit_note and money(return_doc.total_amount) > 0:
            credit_note = invoice_service.create_credit_note(db, return_doc, user_id=user_id)
            ledger_service.post_entry(
                db,
                customer_id=return_doc.customer_id,
                entry_type=LedgerEntryType.CREDIT_NOTE,
                entry_date=return_doc.return_date,
                credit=money(return_doc.total_amount),
                reference_type="CREDIT_NOTE",
                reference_id=credit_note.id,
                reference_no=credit_note.invoice_no,
                salesperson_id=return_doc.salesperson_id,
                description=f"{return_doc.return_no} ({return_doc.reason})",
                user_id=user_id,
            )
            customer_service.recalc_balance(db, return_doc.customer_id)

        return_doc.is_posted = True
        return_doc.posted_at = utcnow()
        return_doc.updated_by_id = user_id
        db.flush()

        _update_visit(db, return_doc)
        _update_day_session(db, return_doc)

        audit_service.record(
            db,
            AuditAction.UPDATE,
            entity_type="ReturnDocument",
            entity_id=return_doc.id,
            entity_label=return_doc.return_no,
            user_id=user_id,
            summary=f"Return {return_doc.return_no} posted ({return_doc.disposition})",
            amount=return_doc.total_amount,
            new_values={
                "is_posted": True,
                "stock_movements": len(movements),
                "credit_note": credit_note.invoice_no if credit_note else None,
            },
        )
        if commit:
            db.commit()
        return {
            "return_doc": return_doc,
            "credit_note": credit_note,
            "stock_movements": len(movements),
        }
    except Exception:
        if commit:
            db.rollback()
        raise


def _post_stock(db: Session, doc: ReturnDocument, *, user_id: int | None) -> list[Any]:
    from app.services import stock_service

    movements: list[Any] = []
    for item in doc.items:
        base_quantity = qty(item.base_quantity)
        if base_quantity <= 0:
            continue
        disposition = str(item.disposition or doc.disposition)
        status = {
            ReturnDisposition.RESALEABLE: StockStatus.AVAILABLE,
            ReturnDisposition.QUARANTINE: StockStatus.QUARANTINE,
            ReturnDisposition.SCRAP: StockStatus.DAMAGED,
        }.get(disposition, StockStatus.AVAILABLE)

        movements.append(
            stock_service.receive_stock(
                db,
                warehouse_id=doc.warehouse_id,
                product_id=item.product_id,
                base_quantity=base_quantity,
                movement_type=StockMovementType.SALE_RETURN,
                lot_id=item.lot_id,
                unit_cost=money(item.unit_cost),
                status=status,
                reference_type="RETURN",
                reference_id=doc.id,
                reference_no=doc.return_no,
                salesperson_id=doc.salesperson_id,
                customer_id=doc.customer_id,
                day_session_id=doc.day_session_id,
                user_id=user_id,
                notes=item.reason,
            )
        )

        if disposition == ReturnDisposition.SCRAP:
            movements.append(
                stock_service.post_movement(
                    db,
                    warehouse_id=doc.warehouse_id,
                    product_id=item.product_id,
                    movement_type=StockMovementType.WASTAGE,
                    base_quantity=-base_quantity,
                    lot_id=item.lot_id,
                    unit_cost=money(item.unit_cost),
                    status=StockStatus.DAMAGED,
                    reference_type="RETURN",
                    reference_id=doc.id,
                    reference_no=doc.return_no,
                    salesperson_id=doc.salesperson_id,
                    customer_id=doc.customer_id,
                    day_session_id=doc.day_session_id,
                    user_id=user_id,
                    notes=item.reason,
                )
            )
    return movements


def _update_sale_items(db: Session, doc: ReturnDocument) -> None:
    """
    Keep ``SaleItem.returned_quantity`` truthful so re-returns are blocked.

    Stored in the *sale line's* unit, converting through base units, because
    the return may have been booked in a different packaging.
    """
    for item in doc.items:
        if not item.sale_item_id:
            continue
        sale_item = db.get(SaleItem, item.sale_item_id)
        if sale_item is None:
            continue
        sale_factor = qty(sale_item.uom_factor) or Decimal("1")
        sale_item.returned_quantity = qty(
            sale_item.returned_quantity + qty(item.base_quantity) / sale_factor
        )
    db.flush()


def _update_visit(db: Session, doc: ReturnDocument) -> None:
    if not doc.visit_id:
        return
    visit = db.get(Visit, doc.visit_id)
    if visit is None:
        return
    visit.return_amount = money(visit.return_amount + doc.total_amount)
    db.flush()


def _update_day_session(db: Session, doc: ReturnDocument) -> None:
    if not doc.day_session_id:
        return
    session = db.get(DaySession, doc.day_session_id)
    if session is None:
        return
    returned = qty(sum((qty(i.base_quantity) for i in doc.items), Decimal("0")))
    wasted = qty(
        sum(
            (
                qty(i.base_quantity)
                for i in doc.items
                if str(i.disposition or doc.disposition) == ReturnDisposition.SCRAP
            ),
            Decimal("0"),
        )
    )
    session.returned_qty = qty(session.returned_qty + returned)
    session.wastage_qty = qty(session.wastage_qty + wasted)
    db.flush()


# ===========================================================================
# Queries
# ===========================================================================
def get(db: Session, return_id: int) -> ReturnDocument:
    doc = db.get(ReturnDocument, return_id)
    if doc is None or doc.is_deleted:
        raise NotFoundError("return.not_found", params={"id": return_id})
    return doc


def credit_note_for(db: Session, return_id: int) -> Invoice | None:
    return db.execute(
        select(Invoice).where(Invoice.return_id == return_id, Invoice.is_deleted.is_(False))
    ).scalars().first()


def _apply_filters(
    stmt: Select[Any],
    *,
    start: date | None,
    end: date | None,
    customer_id: int | None,
    salesperson_id: int | None,
    sale_id: int | None,
    reason: str | None,
    disposition: str | None,
    is_posted: bool | None,
    search: str | None,
    salesperson_ids: Iterable[int] | None,
) -> Select[Any]:
    stmt = stmt.where(ReturnDocument.is_deleted.is_(False))
    if start:
        stmt = stmt.where(ReturnDocument.return_date >= start)
    if end:
        stmt = stmt.where(ReturnDocument.return_date <= end)
    if customer_id:
        stmt = stmt.where(ReturnDocument.customer_id == customer_id)
    if salesperson_id:
        stmt = stmt.where(ReturnDocument.salesperson_id == salesperson_id)
    if sale_id:
        stmt = stmt.where(ReturnDocument.sale_id == sale_id)
    if reason:
        stmt = stmt.where(ReturnDocument.reason == str(reason))
    if disposition:
        stmt = stmt.where(ReturnDocument.disposition == str(disposition))
    if is_posted is not None:
        stmt = stmt.where(ReturnDocument.is_posted.is_(is_posted))
    if search:
        term = f"%{search.lower()}%"
        stmt = stmt.join(Customer, Customer.id == ReturnDocument.customer_id).where(
            or_(
                func.lower(ReturnDocument.return_no).like(term),
                func.lower(Customer.name).like(term),
                func.lower(Customer.code).like(term),
            )
        )
    ids = list(salesperson_ids or [])
    if ids:
        stmt = stmt.where(ReturnDocument.salesperson_id.in_(ids))
    return stmt


def list_returns(
    db: Session,
    *,
    start: date | None = None,
    end: date | None = None,
    customer_id: int | None = None,
    salesperson_id: int | None = None,
    sale_id: int | None = None,
    reason: str | None = None,
    disposition: str | None = None,
    is_posted: bool | None = None,
    search: str | None = None,
    salesperson_ids: Iterable[int] | None = None,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[ReturnDocument], int]:
    filters = dict(
        start=start,
        end=end,
        customer_id=customer_id,
        salesperson_id=salesperson_id,
        sale_id=sale_id,
        reason=reason,
        disposition=disposition,
        is_posted=is_posted,
        search=search,
        salesperson_ids=salesperson_ids,
    )
    total = db.execute(
        _apply_filters(select(func.count(ReturnDocument.id)), **filters)  # type: ignore[arg-type]
    ).scalar_one()
    rows = db.execute(
        _apply_filters(select(ReturnDocument), **filters)  # type: ignore[arg-type]
        .order_by(ReturnDocument.return_date.desc(), ReturnDocument.id.desc())
        .offset(offset)
        .limit(limit)
    ).scalars().all()
    return list(rows), int(total)
