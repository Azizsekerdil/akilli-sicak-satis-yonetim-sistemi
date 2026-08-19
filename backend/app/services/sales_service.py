"""
Orders, deliveries and the hot-sale (sıcak satış) flow.

The document chain is Order → Sale → Invoice → Payment.  In pre-sale the links
are separated in time; in a hot sale the salesperson creates all four at the
customer's door and the whole thing must be atomic — goods leave the van, the
receivable is created and the money is taken, or none of it happened.

Traceability rule
-----------------
One ``SaleItem`` is written **per lot** that the allocation touched.  A single
ordered line of 30 pieces satisfied from two batches becomes two sale lines, so
"which batch did this customer receive" is answerable from a single row lookup
years later — which is exactly what a food-safety recall needs.

Cross-module services (stock, pricing, ledger, day sessions) are imported
inside the functions that use them: the service layer is mutually referential
and deferring those imports keeps every module independently importable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from app.core.enums import (
    AuditAction,
    LedgerEntryType,
    OrderStatus,
    OrderType,
    PaymentMethod,
    PaymentStatus,
    StockMovementType,
    VisitOutcome,
)
from app.core.exceptions import (
    BusinessRuleError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from app.core.logging_config import get_logger
from app.core.utils import money, pct, qty
from app.models.base import utcnow
from app.models.campaign import Campaign, CampaignApplication
from app.models.customer import Customer
from app.models.route import Visit
from app.models.sales import Invoice, Order, OrderItem, Payment, Sale, SaleItem
from app.models.vehicle import DaySession, Salesperson
from app.models.warehouse import StockMovement
from app.services import audit_service, invoice_service, numbering_service, payment_service

log = get_logger("app.sales")

#: Methods where the money is in hand at the door — no credit is extended, so
#: a customer sitting on their limit can still buy for cash.
IMMEDIATE_METHODS: frozenset[str] = frozenset(
    {PaymentMethod.CASH, PaymentMethod.CREDIT_CARD}
)

_ACTIVE_ORDER_STATUSES = (
    OrderStatus.DRAFT,
    OrderStatus.CONFIRMED,
    OrderStatus.PARTIALLY_DELIVERED,
)


# ===========================================================================
# Line helpers
# ===========================================================================
@dataclass(slots=True)
class _Split:
    """One lot's share of an ordered line."""

    quantity: Decimal
    base_quantity: Decimal
    gross_amount: Decimal
    discount_amount: Decimal
    campaign_discount_amount: Decimal
    net_amount: Decimal
    vat_amount: Decimal
    excise_amount: Decimal
    total_amount: Decimal


def _normalise_lines(lines: Sequence[Mapping[str, Any] | Any]) -> list[dict[str, Any]]:
    """Accept dicts or any object exposing the line attributes."""
    out: list[dict[str, Any]] = []
    for raw in lines:
        if isinstance(raw, Mapping):
            src: Mapping[str, Any] = raw
            get = src.get
        else:
            get = lambda key, default=None: getattr(raw, key, default)  # noqa: E731
        product_id = int(get("product_id") or 0)
        quantity = qty(get("quantity") or 0)
        if product_id <= 0 or quantity <= 0:
            raise ValidationError("order.invalid_line", params={"product_id": product_id})
        out.append(
            {
                "product_id": product_id,
                "quantity": quantity,
                "uom": str(get("uom") or ""),
                "discount_percent": float(get("discount_percent") or 0.0),
                "unit_price_override": (
                    None if get("unit_price_override") in (None, "")
                    else money(get("unit_price_override"))
                ),
                "notes": get("notes"),
            }
        )
    return out


def _price(
    db: Session,
    *,
    customer: Customer,
    lines: Sequence[Mapping[str, Any] | Any],
    on_date: date,
    salesperson: Salesperson | None,
    header_discount_percent: float,
) -> Any:
    from app.services import pricing_service

    normalised = _normalise_lines(lines)
    inputs = [
        pricing_service.LineInput(
            product_id=row["product_id"],
            quantity=row["quantity"],
            uom=row["uom"],
            discount_percent=row["discount_percent"],
            unit_price_override=row["unit_price_override"],
        )
        for row in normalised
    ]
    return pricing_service.price_basket(
        db,
        customer=customer,
        lines=inputs,
        on_date=on_date,
        salesperson=salesperson,
        apply_campaigns=True,
        header_discount_percent=header_discount_percent,
    )


def _split_line(item: OrderItem, movements: Sequence[StockMovement]) -> list[_Split]:
    """
    Distribute an ordered line's money across the lots it was picked from.

    The last lot absorbs the rounding remainder so the split always sums back
    to the line exactly — an invoice that is one kuruş off is a rejected
    invoice.
    """
    total_base = qty(item.base_quantity)
    if not movements or total_base <= 0:
        return []

    remaining = _Split(
        quantity=qty(item.quantity),
        base_quantity=total_base,
        gross_amount=money(item.gross_amount),
        discount_amount=money(item.discount_amount),
        campaign_discount_amount=money(item.campaign_discount_amount),
        net_amount=money(item.net_amount),
        vat_amount=money(item.vat_amount),
        excise_amount=Decimal("0"),
        total_amount=money(item.total_amount),
    )
    splits: list[_Split] = []
    last = len(movements) - 1
    for index, movement in enumerate(movements):
        if index == last:
            splits.append(remaining)
            break
        share = qty(abs(movement.quantity))
        ratio = share / total_base
        part = _Split(
            quantity=qty(item.quantity * ratio),
            base_quantity=share,
            gross_amount=money(item.gross_amount * ratio),
            discount_amount=money(item.discount_amount * ratio),
            campaign_discount_amount=money(item.campaign_discount_amount * ratio),
            net_amount=money(item.net_amount * ratio),
            vat_amount=money(item.vat_amount * ratio),
            excise_amount=Decimal("0"),
            total_amount=money(item.total_amount * ratio),
        )
        splits.append(part)
        remaining = _Split(
            quantity=qty(remaining.quantity - part.quantity),
            base_quantity=qty(remaining.base_quantity - part.base_quantity),
            gross_amount=money(remaining.gross_amount - part.gross_amount),
            discount_amount=money(remaining.discount_amount - part.discount_amount),
            campaign_discount_amount=money(
                remaining.campaign_discount_amount - part.campaign_discount_amount
            ),
            net_amount=money(remaining.net_amount - part.net_amount),
            vat_amount=money(remaining.vat_amount - part.vat_amount),
            excise_amount=Decimal("0"),
            total_amount=money(remaining.total_amount - part.total_amount),
        )
    return splits


def _excise_by_item(order: Order) -> dict[int, Decimal]:
    """
    Spread the order's excise (ÖTV) header total across its lines by net value.

    ``OrderItem`` carries no excise column, so the header figure is the only
    stored truth; prorating keeps the sale lines summing back to it.
    """
    total = money(order.excise_amount)
    shares: dict[int, Decimal] = {item.id: Decimal("0") for item in order.items}
    if total <= 0 or not order.items:
        return shares
    net_total = money(sum((money(i.net_amount) for i in order.items), Decimal("0")))
    if net_total <= 0:
        return shares
    remaining = total
    for index, item in enumerate(order.items):
        if index == len(order.items) - 1:
            shares[item.id] = remaining
            break
        share = money(total * money(item.net_amount) / net_total)
        shares[item.id] = share
        remaining = money(remaining - share)
    return shares


# ===========================================================================
# Orders
# ===========================================================================
def create_order(
    db: Session,
    *,
    customer_id: int,
    lines: Sequence[Mapping[str, Any] | Any],
    order_type: str = OrderType.PRE_SALE,
    salesperson_id: int | None = None,
    vehicle_id: int | None = None,
    warehouse_id: int | None = None,
    route_id: int | None = None,
    visit_id: int | None = None,
    day_session_id: int | None = None,
    payment_method: str = PaymentMethod.CASH,
    header_discount_percent: float = 0.0,
    order_date: date | None = None,
    delivery_date: date | None = None,
    notes: str | None = None,
    user_id: int | None = None,
) -> Order:
    """
    Price a basket and persist it as a confirmed order.

    The credit check runs *after* pricing because the exposure being checked is
    the priced total, not the requested quantity — and it is skipped for
    immediate-settlement methods, where no credit is extended at all.
    """
    from app.services import customer_service

    if not lines:
        raise ValidationError("order.empty")

    customer = db.get(Customer, customer_id)
    if customer is None or customer.is_deleted:
        raise NotFoundError("customer.not_found", params={"id": customer_id})

    salesperson = db.get(Salesperson, salesperson_id) if salesperson_id else None
    on = order_date or date.today()

    priced = _price(
        db,
        customer=customer,
        lines=lines,
        on_date=on,
        salesperson=salesperson,
        header_discount_percent=header_discount_percent,
    )
    if not priced.lines:
        raise ValidationError("order.empty")

    method = str(payment_method)
    if method not in IMMEDIATE_METHODS:
        customer_service.check_credit(db, customer, money(priced.total_amount))

    header_discount_amount = money(getattr(priced, "header_discount_amount", 0) or 0)

    order = Order(
        order_no=numbering_service.next_number(db, "ORDER", on=on),
        order_type=str(order_type),
        status=OrderStatus.CONFIRMED,
        order_date=on,
        delivery_date=delivery_date or (on if str(order_type) == OrderType.HOT_SALE else None),
        ordered_at=utcnow(),
        customer_id=customer_id,
        salesperson_id=salesperson_id,
        vehicle_id=vehicle_id,
        warehouse_id=warehouse_id,
        route_id=route_id,
        visit_id=visit_id,
        day_session_id=day_session_id,
        price_list_id=customer.price_list_id,
        currency=customer.currency or "TRY",
        gross_amount=money(priced.gross_amount),
        line_discount_amount=money(priced.line_discount_amount),
        campaign_discount_amount=money(priced.campaign_discount_amount),
        header_discount_amount=header_discount_amount,
        header_discount_percent=float(header_discount_percent or 0.0),
        net_amount=money(priced.net_amount),
        vat_amount=money(priced.vat_amount),
        excise_amount=money(priced.excise_amount),
        total_amount=money(priced.total_amount),
        total_cost=money(priced.total_cost),
        margin_amount=money(priced.margin_amount),
        payment_method=method,
        payment_term_days=int(customer.payment_term_days or 0),
        total_volume_l=float(priced.total_volume_l or 0.0),
        total_weight_kg=float(priced.total_weight_kg or 0.0),
        line_count=len(priced.lines),
        notes=notes,
        created_by_id=user_id,
        updated_by_id=user_id,
    )
    db.add(order)
    db.flush()

    # Appended through the relationship (not db.add) so ``order.items`` is
    # populated for the caller in this same session — post_sale reads it.
    for line_no, line in enumerate(priced.lines, start=1):
        order.items.append(
            OrderItem(
                product_id=line.product_id,
                line_no=line_no,
                quantity=qty(line.quantity),
                uom=line.uom,
                uom_factor=qty(line.uom_factor),
                base_quantity=qty(line.base_quantity),
                delivered_quantity=Decimal("0"),
                unit_price=money(line.unit_price),
                list_price=money(line.list_price),
                gross_amount=money(line.gross_amount),
                discount_percent=float(line.discount_percent or 0.0),
                discount_amount=money(line.discount_amount),
                campaign_discount_amount=money(line.campaign_discount_amount),
                net_amount=money(line.net_amount),
                vat_rate=float(line.vat_rate or 0.0),
                vat_amount=money(line.vat_amount),
                total_amount=money(line.total_amount),
                unit_cost=money(line.unit_cost),
                is_free_goods=bool(line.is_free_goods),
                campaign_id=line.campaign_id,
            )
        )
    db.flush()

    _record_campaign_applications(db, order, priced, salesperson_id=salesperson_id)

    audit_service.record(
        db,
        AuditAction.CREATE,
        entity_type="Order",
        entity_id=order.id,
        entity_label=order.order_no,
        user_id=user_id,
        summary=f"{order.order_type} order {order.order_no} for customer {customer.code}",
        amount=order.total_amount,
        new_values={
            "order_no": order.order_no,
            "customer_id": customer_id,
            "lines": order.line_count,
            "total_amount": str(order.total_amount),
            "payment_method": method,
        },
    )
    return order


def _record_campaign_applications(
    db: Session,
    order: Order,
    priced: Any,
    *,
    salesperson_id: int | None,
) -> None:
    """Persist which promotions fired, so their ROI can be measured later."""
    from app.services import campaign_service

    for entry in getattr(priced, "applied_campaigns", None) or []:
        data: Mapping[str, Any] = entry if isinstance(entry, Mapping) else {}
        campaign_id = int(data.get("campaign_id") or data.get("id") or 0)
        if campaign_id <= 0:
            continue
        campaign = db.get(Campaign, campaign_id)
        if campaign is None:
            continue

        campaign_service.record_application(
            db,
            campaign,
            reference_type="ORDER",
            reference_id=order.id,
            customer_id=order.customer_id,
            salesperson_id=salesperson_id,
            basket_amount=money(order.gross_amount),
            discount_amount=money(data.get("discount_amount") or 0),
            free_goods_quantity=qty(data.get("free_goods_quantity") or 0),
            free_goods_cost=money(data.get("free_goods_cost") or 0),
            times_applied=int(data.get("times_applied") or 1),
            explanation=str(data.get("explanation") or data.get("name") or campaign.name),
            on=order.order_date,
        )
    db.flush()


def _clear_campaign_applications(db: Session, order: Order) -> None:
    """Undo the campaign records of an order that is being re-priced."""
    rows = db.execute(
        select(CampaignApplication).where(
            CampaignApplication.reference_type == "ORDER",
            CampaignApplication.reference_id == order.id,
        )
    ).scalars().all()
    for row in rows:
        campaign = db.get(Campaign, row.campaign_id)
        if campaign is not None:
            campaign.application_count = max(
                0, int(campaign.application_count or 0) - int(row.times_applied or 1)
            )
            campaign.total_discount_given = money(
                max(Decimal("0"), campaign.total_discount_given - row.discount_amount)
            )
            campaign.total_free_goods_cost = money(
                max(Decimal("0"), campaign.total_free_goods_cost - row.free_goods_cost)
            )
            campaign.total_incremental_revenue = money(
                max(Decimal("0"), campaign.total_incremental_revenue - row.basket_amount)
            )
        db.delete(row)
    db.flush()


def update_order(
    db: Session,
    order: Order,
    *,
    lines: Sequence[Mapping[str, Any] | Any] | None = None,
    payment_method: str | None = None,
    header_discount_percent: float | None = None,
    delivery_date: date | None = None,
    notes: str | None = None,
    user_id: int | None = None,
) -> Order:
    """
    Amend an order that has not been delivered.

    Changing lines re-prices the whole basket rather than patching totals:
    campaigns are basket-level, so a single added case can change the discount
    on every other line.
    """
    from app.services import customer_service

    if order.status not in _ACTIVE_ORDER_STATUSES:
        raise BusinessRuleError("order.not_editable", params={"no": order.order_no})

    old = {
        "total_amount": str(order.total_amount),
        "line_count": order.line_count,
        "payment_method": order.payment_method,
    }

    if payment_method is not None:
        order.payment_method = str(payment_method)
    if delivery_date is not None:
        order.delivery_date = delivery_date
    if notes is not None:
        order.notes = notes
    if header_discount_percent is not None:
        order.header_discount_percent = float(header_discount_percent)

    if lines is not None:
        if not lines:
            raise ValidationError("order.empty")
        customer = db.get(Customer, order.customer_id)
        if customer is None:
            raise NotFoundError("customer.not_found", params={"id": order.customer_id})
        salesperson = (
            db.get(Salesperson, order.salesperson_id) if order.salesperson_id else None
        )
        priced = _price(
            db,
            customer=customer,
            lines=lines,
            on_date=order.order_date,
            salesperson=salesperson,
            header_discount_percent=float(order.header_discount_percent or 0.0),
        )
        if order.payment_method not in IMMEDIATE_METHODS:
            customer_service.check_credit(db, customer, money(priced.total_amount))

        _clear_campaign_applications(db, order)
        order.items.clear()
        db.flush()

        for line_no, line in enumerate(priced.lines, start=1):
            order.items.append(
                OrderItem(
                    product_id=line.product_id,
                    line_no=line_no,
                    quantity=qty(line.quantity),
                    uom=line.uom,
                    uom_factor=qty(line.uom_factor),
                    base_quantity=qty(line.base_quantity),
                    delivered_quantity=Decimal("0"),
                    unit_price=money(line.unit_price),
                    list_price=money(line.list_price),
                    gross_amount=money(line.gross_amount),
                    discount_percent=float(line.discount_percent or 0.0),
                    discount_amount=money(line.discount_amount),
                    campaign_discount_amount=money(line.campaign_discount_amount),
                    net_amount=money(line.net_amount),
                    vat_rate=float(line.vat_rate or 0.0),
                    vat_amount=money(line.vat_amount),
                    total_amount=money(line.total_amount),
                    unit_cost=money(line.unit_cost),
                    is_free_goods=bool(line.is_free_goods),
                    campaign_id=line.campaign_id,
                )
            )

        order.gross_amount = money(priced.gross_amount)
        order.line_discount_amount = money(priced.line_discount_amount)
        order.campaign_discount_amount = money(priced.campaign_discount_amount)
        order.header_discount_amount = money(
            getattr(priced, "header_discount_amount", 0) or 0
        )
        order.net_amount = money(priced.net_amount)
        order.vat_amount = money(priced.vat_amount)
        order.excise_amount = money(priced.excise_amount)
        order.total_amount = money(priced.total_amount)
        order.total_cost = money(priced.total_cost)
        order.margin_amount = money(priced.margin_amount)
        order.total_volume_l = float(priced.total_volume_l or 0.0)
        order.total_weight_kg = float(priced.total_weight_kg or 0.0)
        order.line_count = len(priced.lines)
        db.flush()
        _record_campaign_applications(db, order, priced, salesperson_id=order.salesperson_id)

    order.updated_by_id = user_id
    db.flush()

    audit_service.record(
        db,
        AuditAction.UPDATE,
        entity_type="Order",
        entity_id=order.id,
        entity_label=order.order_no,
        user_id=user_id,
        summary=f"Order {order.order_no} updated",
        amount=order.total_amount,
        old_values=old,
        new_values={
            "total_amount": str(order.total_amount),
            "line_count": order.line_count,
            "payment_method": order.payment_method,
        },
    )
    return order


def cancel_order(
    db: Session,
    order: Order,
    *,
    reason: str,
    user_id: int | None = None,
) -> Order:
    """Cancel an order that has not been delivered yet."""
    if order.status == OrderStatus.CANCELLED:
        raise ConflictError("order.already_cancelled", params={"no": order.order_no})
    if order.status in (OrderStatus.DELIVERED, OrderStatus.INVOICED):
        raise BusinessRuleError("order.cannot_cancel_delivered", params={"no": order.order_no})

    old_status = order.status
    order.status = OrderStatus.CANCELLED
    order.cancelled_at = utcnow()
    order.cancel_reason = reason[:255]
    order.updated_by_id = user_id
    db.flush()

    audit_service.record(
        db,
        AuditAction.CANCEL,
        entity_type="Order",
        entity_id=order.id,
        entity_label=order.order_no,
        user_id=user_id,
        summary=f"Order {order.order_no} cancelled: {reason}",
        amount=order.total_amount,
        old_values={"status": old_status},
        new_values={"status": order.status, "reason": reason},
    )
    return order


# ===========================================================================
# Delivery (posting a sale)
# ===========================================================================
def post_sale(
    db: Session,
    order: Order,
    *,
    warehouse_id: int,
    user_id: int | None = None,
    allow_expired: bool = False,
) -> Sale:
    """
    Turn a confirmed order into a delivered sale and take the goods out of
    stock, one ``SaleItem`` per lot the allocation touched.

    Cost and margin come from the *allocated lot costs*, never from the
    product's standard cost: FEFO may hand over an older, cheaper batch and the
    margin report has to reflect what actually left the van.
    """
    from app.services import stock_service

    if order.status == OrderStatus.CANCELLED:
        raise BusinessRuleError("order.cancelled", params={"no": order.order_no})
    if order.status in (OrderStatus.DELIVERED, OrderStatus.INVOICED):
        raise ConflictError("order.already_delivered", params={"no": order.order_no})
    if not order.items:
        raise ValidationError("order.empty")

    on = order.delivery_date or order.order_date or date.today()
    sale = Sale(
        sale_no=numbering_service.next_number(db, "SALE", on=on),
        order_id=order.id,
        sale_date=on,
        sold_at=utcnow(),
        customer_id=order.customer_id,
        salesperson_id=order.salesperson_id,
        vehicle_id=order.vehicle_id,
        warehouse_id=warehouse_id,
        route_id=order.route_id,
        visit_id=order.visit_id,
        day_session_id=order.day_session_id,
        is_hot_sale=order.order_type == OrderType.HOT_SALE,
        is_posted=False,
        currency=order.currency,
        gross_amount=money(order.gross_amount),
        discount_amount=money(order.line_discount_amount + order.header_discount_amount),
        campaign_discount_amount=money(order.campaign_discount_amount),
        net_amount=money(order.net_amount),
        vat_amount=money(order.vat_amount),
        excise_amount=money(order.excise_amount),
        total_amount=money(order.total_amount),
        paid_amount=Decimal("0"),
        due_amount=money(order.total_amount),
        payment_method=order.payment_method,
        total_volume_l=float(order.total_volume_l or 0.0),
        total_weight_kg=float(order.total_weight_kg or 0.0),
        notes=order.notes,
        created_by_id=user_id,
        updated_by_id=user_id,
    )
    db.add(sale)
    db.flush()

    excise_shares = _excise_by_item(order)
    total_cost = Decimal("0")
    line_no = 0

    for item in order.items:
        movements = stock_service.issue_stock(
            db,
            warehouse_id=warehouse_id,
            product_id=item.product_id,
            base_quantity=qty(item.base_quantity),
            movement_type=StockMovementType.SALE,
            allow_expired=allow_expired,
            reference_type="SALE",
            reference_id=sale.id,
            reference_no=sale.sale_no,
            salesperson_id=sale.salesperson_id,
            customer_id=sale.customer_id,
            day_session_id=sale.day_session_id,
            user_id=user_id,
        )
        splits = _split_line(item, movements)
        excise_remaining = excise_shares.get(item.id, Decimal("0"))

        for index, (movement, split) in enumerate(zip(movements, splits)):
            line_no += 1
            if index == len(splits) - 1:
                excise_part = excise_remaining
            else:
                ratio = split.base_quantity / qty(item.base_quantity)
                excise_part = money(excise_shares.get(item.id, Decimal("0")) * ratio)
                excise_remaining = money(excise_remaining - excise_part)

            unit_cost = money(movement.unit_cost)
            line_cost = money(unit_cost * split.base_quantity)
            total_cost = money(total_cost + line_cost)

            sale.items.append(
                SaleItem(
                    order_item_id=item.id,
                    product_id=item.product_id,
                    lot_id=movement.lot_id,
                    line_no=line_no,
                    quantity=split.quantity,
                    uom=item.uom,
                    uom_factor=item.uom_factor,
                    base_quantity=split.base_quantity,
                    unit_price=money(item.unit_price),
                    list_price=money(item.list_price),
                    gross_amount=split.gross_amount,
                    discount_percent=float(item.discount_percent or 0.0),
                    discount_amount=split.discount_amount,
                    campaign_discount_amount=split.campaign_discount_amount,
                    net_amount=split.net_amount,
                    vat_rate=float(item.vat_rate or 0.0),
                    vat_amount=split.vat_amount,
                    excise_amount=excise_part,
                    total_amount=split.total_amount,
                    unit_cost=unit_cost,
                    total_cost=line_cost,
                    margin_amount=money(split.net_amount - line_cost),
                    is_free_goods=bool(item.is_free_goods),
                    campaign_id=item.campaign_id,
                    returned_quantity=Decimal("0"),
                )
            )
        item.delivered_quantity = qty(item.quantity)

    sale.line_count = line_no
    sale.total_cost = total_cost
    sale.margin_amount = money(sale.net_amount - total_cost)
    sale.is_posted = True
    sale.posted_at = utcnow()

    order.status = OrderStatus.DELIVERED
    order.updated_by_id = user_id
    db.flush()

    audit_service.record(
        db,
        AuditAction.SALE,
        entity_type="Sale",
        entity_id=sale.id,
        entity_label=sale.sale_no,
        user_id=user_id,
        summary=f"Sale {sale.sale_no} posted from order {order.order_no}",
        amount=sale.total_amount,
        new_values={
            "sale_no": sale.sale_no,
            "order_no": order.order_no,
            "warehouse_id": warehouse_id,
            "lines": sale.line_count,
            "total_amount": str(sale.total_amount),
            "total_cost": str(sale.total_cost),
        },
    )
    return sale


def _movement_count(db: Session, sale_id: int) -> int:
    return int(
        db.execute(
            select(func.count(StockMovement.id)).where(
                StockMovement.reference_type == "SALE",
                StockMovement.reference_id == sale_id,
            )
        ).scalar_one()
    )


def _complete_delivery(
    db: Session,
    order: Order,
    *,
    warehouse_id: int,
    user_id: int | None,
    create_invoice: bool = True,
    payment: Mapping[str, Any] | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    day_session: DaySession | None = None,
    allow_expired: bool = False,
) -> dict[str, Any]:
    """Shared tail of hot sale and pre-sale delivery: stock, invoice, money, stats."""
    from app.services import customer_service, ledger_service

    sale = post_sale(
        db, order, warehouse_id=warehouse_id, user_id=user_id, allow_expired=allow_expired
    )
    sale.latitude = latitude
    sale.longitude = longitude
    db.flush()

    invoice: Invoice | None = None
    if create_invoice:
        invoice = invoice_service.create_from_sale(db, sale, user_id=user_id)
        ledger_service.post_entry(
            db,
            customer_id=sale.customer_id,
            entry_type=LedgerEntryType.INVOICE,
            entry_date=invoice.invoice_date,
            debit=money(invoice.total_amount),
            due_date=invoice.due_date,
            reference_type="INVOICE",
            reference_id=invoice.id,
            reference_no=invoice.invoice_no,
            salesperson_id=sale.salesperson_id,
            description=f"{sale.sale_no}",
            user_id=user_id,
        )
        order.status = OrderStatus.INVOICED
        db.flush()

    payment_row: Payment | None = None
    if payment:
        payment_row = payment_service.record_payment(
            db,
            customer_id=sale.customer_id,
            amount=money(payment.get("amount") or 0),
            payment_method=str(payment.get("method") or payment.get("payment_method") or PaymentMethod.CASH),
            payment_date=sale.sale_date,
            salesperson_id=sale.salesperson_id,
            sale_id=sale.id,
            visit_id=sale.visit_id,
            day_session_id=sale.day_session_id,
            invoice_ids=[invoice.id] if invoice is not None else None,
            bank_name=payment.get("bank_name"),
            document_number=payment.get("document_number"),
            maturity_date=payment.get("maturity_date"),
            drawer_name=payment.get("drawer_name"),
            reference=payment.get("reference"),
            latitude=latitude,
            longitude=longitude,
            notes=payment.get("notes"),
            user_id=user_id,
        )

    _update_customer_stats(db, sale)
    customer_service.recalc_balance(db, sale.customer_id)
    _update_visit(db, sale, payment_row)
    _update_day_session(db, sale, payment_row, day_session)

    return {
        "order": order,
        "sale": sale,
        "invoice": invoice,
        "payment": payment_row,
        "stock_movements": _movement_count(db, sale.id),
    }


def hot_sale(
    db: Session,
    *,
    customer_id: int,
    lines: Sequence[Mapping[str, Any] | Any],
    salesperson_id: int,
    vehicle_id: int,
    payment: Mapping[str, Any] | None = None,
    route_id: int | None = None,
    visit_id: int | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    header_discount_percent: float = 0.0,
    notes: str | None = None,
    user_id: int | None = None,
) -> dict[str, Any]:
    """
    Sell from the van in a single transaction.

    Order, sale, stock movements, invoice, ledger entry and collection either
    all land together or none of them do — a partially applied hot sale would
    leave the van's physical stock disagreeing with the books, which is the one
    failure this system exists to prevent.
    """
    from app.services import day_session_service, stock_service

    session = day_session_service.require_open_session(
        db, salesperson_id=salesperson_id, vehicle_id=vehicle_id
    )
    warehouse_id = stock_service.vehicle_warehouse_id(db, vehicle_id)

    method = str(
        (payment or {}).get("method")
        or (payment or {}).get("payment_method")
        or PaymentMethod.OPEN_ACCOUNT
    )

    try:
        order = create_order(
            db,
            customer_id=customer_id,
            lines=lines,
            order_type=OrderType.HOT_SALE,
            salesperson_id=salesperson_id,
            vehicle_id=vehicle_id,
            warehouse_id=warehouse_id,
            route_id=route_id,
            visit_id=visit_id,
            day_session_id=session.id,
            payment_method=method,
            header_discount_percent=header_discount_percent,
            notes=notes,
            user_id=user_id,
        )
        result = _complete_delivery(
            db,
            order,
            warehouse_id=warehouse_id,
            user_id=user_id,
            create_invoice=True,
            payment=payment,
            latitude=latitude,
            longitude=longitude,
            day_session=session,
        )

        sale: Sale = result["sale"]
        invoice: Invoice | None = result["invoice"]
        payment_row: Payment | None = result["payment"]
        audit_service.record(
            db,
            AuditAction.SALE,
            entity_type="Sale",
            entity_id=sale.id,
            entity_label=sale.sale_no,
            user_id=user_id,
            summary=(
                f"Hot sale {sale.sale_no} / invoice "
                f"{invoice.invoice_no if invoice else '-'} at customer {sale.customer_id}"
            ),
            amount=sale.total_amount,
            new_values={
                "order_no": order.order_no,
                "sale_no": sale.sale_no,
                "invoice_no": invoice.invoice_no if invoice else None,
                "payment_no": payment_row.payment_no if payment_row else None,
                "stock_movements": result["stock_movements"],
                "total_amount": str(sale.total_amount),
                "latitude": latitude,
                "longitude": longitude,
            },
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return result


def deliver_order(
    db: Session,
    order: Order,
    *,
    warehouse_id: int | None = None,
    vehicle_id: int | None = None,
    create_invoice: bool = True,
    user_id: int | None = None,
) -> dict[str, Any]:
    """Deliver a pre-sale order from a depot or a van; commits on success."""
    from app.services import stock_service

    target = warehouse_id or order.warehouse_id
    if target is None:
        source_vehicle = vehicle_id or order.vehicle_id
        if source_vehicle is None:
            raise ValidationError("order.warehouse_required", params={"no": order.order_no})
        target = stock_service.vehicle_warehouse_id(db, source_vehicle)

    try:
        result = _complete_delivery(
            db,
            order,
            warehouse_id=target,
            user_id=user_id,
            create_invoice=create_invoice,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return result


# ===========================================================================
# Denormalised counters
# ===========================================================================
def _update_customer_stats(db: Session, sale: Sale, *, sign: int = 1) -> None:
    customer = db.get(Customer, sale.customer_id)
    if customer is None:
        return
    amount = money(sale.total_amount) * sign
    customer.total_sales_amount = money(max(Decimal("0"), customer.total_sales_amount + amount))
    customer.order_count = max(0, int(customer.order_count or 0) + sign)
    if customer.order_count:
        customer.average_order_value = money(
            customer.total_sales_amount / Decimal(customer.order_count)
        )
    else:
        customer.average_order_value = Decimal("0")
    if sign > 0:
        if customer.first_order_date is None or sale.sale_date < customer.first_order_date:
            customer.first_order_date = sale.sale_date
        if customer.last_order_date is None or sale.sale_date > customer.last_order_date:
            customer.last_order_date = sale.sale_date
        customer.last_visit_date = sale.sale_date
    db.flush()


def _update_visit(db: Session, sale: Sale, payment: Payment | None, *, sign: int = 1) -> None:
    if not sale.visit_id:
        return
    visit = db.get(Visit, sale.visit_id)
    if visit is None:
        return
    visit.sale_amount = money(max(Decimal("0"), visit.sale_amount + money(sale.total_amount) * sign))
    visit.lines_count = max(0, int(visit.lines_count or 0) + int(sale.line_count) * sign)
    if payment is not None and payment.status == PaymentStatus.CLEARED:
        visit.collected_amount = money(
            max(Decimal("0"), visit.collected_amount + money(payment.amount) * sign)
        )
    if sign > 0:
        visit.outcome = VisitOutcome.SALE
    db.flush()


def _update_day_session(
    db: Session,
    sale: Sale,
    payment: Payment | None,
    session: DaySession | None = None,
    *,
    sign: int = 1,
) -> None:
    if session is None:
        if not sale.day_session_id:
            return
        session = db.get(DaySession, sale.day_session_id)
    if session is None:
        return

    sold_base = qty(sum((qty(i.base_quantity) for i in sale.items), Decimal("0")))
    session.sold_qty = qty(max(Decimal("0"), session.sold_qty + sold_base * sign))
    session.total_sales_amount = money(
        max(Decimal("0"), session.total_sales_amount + money(sale.total_amount) * sign)
    )
    session.invoices_count = max(0, int(session.invoices_count or 0) + sign)

    if payment is not None and payment.status == PaymentStatus.CLEARED:
        value = money(payment.amount) * sign
        if payment.payment_method in payment_service.CASH_METHODS:
            session.total_collected_cash = money(
                max(Decimal("0"), session.total_collected_cash + value)
            )
        else:
            session.total_collected_other = money(
                max(Decimal("0"), session.total_collected_other + value)
            )
    db.flush()


# ===========================================================================
# Cancellation
# ===========================================================================
def cancel_sale(
    db: Session,
    sale: Sale,
    *,
    reason: str,
    user_id: int | None = None,
) -> Sale:
    """
    Reverse a delivery completely: goods back into stock, ledger reversed,
    invoice cancelled, counters wound back.

    A sale with money against it is never cancelled — the collection is a fact
    and undoing it silently would break the cash-up.  Issue a return instead.
    """
    from app.services import customer_service, stock_service

    if sale.is_cancelled:
        raise ConflictError("sale.already_cancelled", params={"no": sale.sale_no})
    if money(sale.paid_amount) > 0:
        raise BusinessRuleError("sale.cannot_cancel_paid", params={"no": sale.sale_no})

    for item in sale.items:
        if qty(item.base_quantity) <= 0:
            continue
        stock_service.receive_stock(
            db,
            warehouse_id=sale.warehouse_id,
            product_id=item.product_id,
            base_quantity=qty(item.base_quantity),
            movement_type=StockMovementType.SALE_RETURN,
            lot_id=item.lot_id,
            unit_cost=money(item.unit_cost),
            reference_type="SALE_CANCEL",
            reference_id=sale.id,
            reference_no=sale.sale_no,
            salesperson_id=sale.salesperson_id,
            customer_id=sale.customer_id,
            day_session_id=sale.day_session_id,
            user_id=user_id,
            notes=reason,
        )

    invoices = db.execute(
        select(Invoice).where(Invoice.sale_id == sale.id, Invoice.is_deleted.is_(False))
    ).scalars().all()
    for invoice in invoices:
        if invoice.status != "CANCELLED":
            invoice_service.cancel(db, invoice, reason=reason, user_id=user_id)

    _update_customer_stats(db, sale, sign=-1)
    _update_visit(db, sale, None, sign=-1)
    _update_day_session(db, sale, None, sign=-1)

    sale.is_cancelled = True
    sale.cancelled_at = utcnow()
    sale.cancel_reason = reason[:255]
    sale.due_amount = Decimal("0")
    sale.updated_by_id = user_id

    if sale.order_id:
        order = db.get(Order, sale.order_id)
        if order is not None:
            order.status = OrderStatus.CANCELLED
            order.cancelled_at = utcnow()
            order.cancel_reason = reason[:255]
    db.flush()

    customer_service.recalc_balance(db, sale.customer_id)

    audit_service.record(
        db,
        AuditAction.CANCEL,
        entity_type="Sale",
        entity_id=sale.id,
        entity_label=sale.sale_no,
        user_id=user_id,
        summary=f"Sale {sale.sale_no} cancelled: {reason}",
        amount=sale.total_amount,
        old_values={"is_cancelled": False, "total_amount": str(sale.total_amount)},
        new_values={"is_cancelled": True, "reason": reason},
    )
    return sale


# ===========================================================================
# Queries
# ===========================================================================
def get_sale(db: Session, sale_id: int) -> Sale:
    sale = db.get(Sale, sale_id)
    if sale is None or sale.is_deleted:
        raise NotFoundError("sale.not_found", params={"id": sale_id})
    return sale


def get_order(db: Session, order_id: int) -> Order:
    order = db.get(Order, order_id)
    if order is None or order.is_deleted:
        raise NotFoundError("order.not_found", params={"id": order_id})
    return order


def _scope(stmt: Select[Any], column: Any, salesperson_ids: Iterable[int] | None) -> Select[Any]:
    ids = list(salesperson_ids or [])
    return stmt.where(column.in_(ids)) if ids else stmt


def _sale_filters(
    stmt: Select[Any],
    *,
    start: date | None,
    end: date | None,
    customer_id: int | None,
    salesperson_id: int | None,
    vehicle_id: int | None,
    route_id: int | None,
    day_session_id: int | None,
    include_cancelled: bool,
    search: str | None,
    salesperson_ids: Iterable[int] | None,
) -> Select[Any]:
    stmt = stmt.where(Sale.is_deleted.is_(False))
    if not include_cancelled:
        stmt = stmt.where(Sale.is_cancelled.is_(False))
    if start:
        stmt = stmt.where(Sale.sale_date >= start)
    if end:
        stmt = stmt.where(Sale.sale_date <= end)
    if customer_id:
        stmt = stmt.where(Sale.customer_id == customer_id)
    if salesperson_id:
        stmt = stmt.where(Sale.salesperson_id == salesperson_id)
    if vehicle_id:
        stmt = stmt.where(Sale.vehicle_id == vehicle_id)
    if route_id:
        stmt = stmt.where(Sale.route_id == route_id)
    if day_session_id:
        stmt = stmt.where(Sale.day_session_id == day_session_id)
    if search:
        term = f"%{search.lower()}%"
        stmt = stmt.join(Customer, Customer.id == Sale.customer_id).where(
            or_(
                func.lower(Sale.sale_no).like(term),
                func.lower(Customer.name).like(term),
                func.lower(Customer.code).like(term),
            )
        )
    return _scope(stmt, Sale.salesperson_id, salesperson_ids)


def list_sales(
    db: Session,
    *,
    start: date | None = None,
    end: date | None = None,
    customer_id: int | None = None,
    salesperson_id: int | None = None,
    vehicle_id: int | None = None,
    route_id: int | None = None,
    day_session_id: int | None = None,
    include_cancelled: bool = False,
    search: str | None = None,
    salesperson_ids: Iterable[int] | None = None,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[Sale], int]:
    filters = dict(
        start=start,
        end=end,
        customer_id=customer_id,
        salesperson_id=salesperson_id,
        vehicle_id=vehicle_id,
        route_id=route_id,
        day_session_id=day_session_id,
        include_cancelled=include_cancelled,
        search=search,
        salesperson_ids=salesperson_ids,
    )
    total = db.execute(
        _sale_filters(select(func.count(Sale.id)), **filters)  # type: ignore[arg-type]
    ).scalar_one()
    rows = db.execute(
        _sale_filters(select(Sale), **filters)  # type: ignore[arg-type]
        .order_by(Sale.sale_date.desc(), Sale.id.desc())
        .offset(offset)
        .limit(limit)
    ).scalars().all()
    return list(rows), int(total)


def _order_filters(
    stmt: Select[Any],
    *,
    start: date | None,
    end: date | None,
    customer_id: int | None,
    salesperson_id: int | None,
    order_type: str | None,
    status: str | None,
    open_only: bool,
    search: str | None,
    salesperson_ids: Iterable[int] | None,
) -> Select[Any]:
    stmt = stmt.where(Order.is_deleted.is_(False))
    if start:
        stmt = stmt.where(Order.order_date >= start)
    if end:
        stmt = stmt.where(Order.order_date <= end)
    if customer_id:
        stmt = stmt.where(Order.customer_id == customer_id)
    if salesperson_id:
        stmt = stmt.where(Order.salesperson_id == salesperson_id)
    if order_type:
        stmt = stmt.where(Order.order_type == str(order_type))
    if status:
        stmt = stmt.where(Order.status == str(status))
    if open_only:
        stmt = stmt.where(Order.status.in_(_ACTIVE_ORDER_STATUSES))
    if search:
        term = f"%{search.lower()}%"
        stmt = stmt.join(Customer, Customer.id == Order.customer_id).where(
            or_(
                func.lower(Order.order_no).like(term),
                func.lower(Customer.name).like(term),
                func.lower(Customer.code).like(term),
            )
        )
    return _scope(stmt, Order.salesperson_id, salesperson_ids)


def list_orders(
    db: Session,
    *,
    start: date | None = None,
    end: date | None = None,
    customer_id: int | None = None,
    salesperson_id: int | None = None,
    order_type: str | None = None,
    status: str | None = None,
    open_only: bool = False,
    search: str | None = None,
    salesperson_ids: Iterable[int] | None = None,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[Order], int]:
    filters = dict(
        start=start,
        end=end,
        customer_id=customer_id,
        salesperson_id=salesperson_id,
        order_type=order_type,
        status=status,
        open_only=open_only,
        search=search,
        salesperson_ids=salesperson_ids,
    )
    total = db.execute(
        _order_filters(select(func.count(Order.id)), **filters)  # type: ignore[arg-type]
    ).scalar_one()
    rows = db.execute(
        _order_filters(select(Order), **filters)  # type: ignore[arg-type]
        .order_by(Order.order_date.desc(), Order.id.desc())
        .offset(offset)
        .limit(limit)
    ).scalars().all()
    return list(rows), int(total)


# ===========================================================================
# Field-app summary
# ===========================================================================
def daily_summary(
    db: Session,
    *,
    on: date | None = None,
    salesperson_id: int | None = None,
    salesperson_ids: Iterable[int] | None = None,
) -> dict[str, Any]:
    """One day's trading in a single payload — the field app's status bar."""
    from app.models.sales import ReturnDocument

    day = on or date.today()
    scope_ids = list(salesperson_ids or [])

    sales_stmt = select(
        func.count(Sale.id),
        func.count(func.distinct(Sale.customer_id)),
        func.coalesce(func.sum(Sale.line_count), 0),
        func.coalesce(func.sum(Sale.gross_amount), 0),
        func.coalesce(func.sum(Sale.discount_amount + Sale.campaign_discount_amount), 0),
        func.coalesce(func.sum(Sale.net_amount), 0),
        func.coalesce(func.sum(Sale.vat_amount), 0),
        func.coalesce(func.sum(Sale.total_amount), 0),
        func.coalesce(func.sum(Sale.total_cost), 0),
    ).where(
        Sale.is_deleted.is_(False),
        Sale.is_cancelled.is_(False),
        Sale.sale_date == day,
    )
    if salesperson_id:
        sales_stmt = sales_stmt.where(Sale.salesperson_id == salesperson_id)
    if scope_ids:
        sales_stmt = sales_stmt.where(Sale.salesperson_id.in_(scope_ids))

    (
        sales_count,
        customers_served,
        lines_sold,
        gross,
        discount,
        net,
        vat,
        total,
        cost,
    ) = db.execute(sales_stmt).one()

    collections = payment_service.collections_summary(
        db,
        start=day,
        end=day,
        salesperson_id=salesperson_id,
        salesperson_ids=scope_ids or None,
    )
    by_method: dict[str, Decimal] = {}
    cash = Decimal("0")
    other = Decimal("0")
    for row in collections["by_method"]:
        if row["status"] != PaymentStatus.CLEARED:
            continue
        method = str(row["payment_method"])
        by_method[method] = money(by_method.get(method, Decimal("0")) + row["amount"])
        if method in payment_service.CASH_METHODS:
            cash = money(cash + row["amount"])
        else:
            other = money(other + row["amount"])

    returns_stmt = select(
        func.count(ReturnDocument.id),
        func.coalesce(func.sum(ReturnDocument.total_amount), 0),
    ).where(
        ReturnDocument.is_deleted.is_(False),
        ReturnDocument.return_date == day,
    )
    if salesperson_id:
        returns_stmt = returns_stmt.where(ReturnDocument.salesperson_id == salesperson_id)
    if scope_ids:
        returns_stmt = returns_stmt.where(ReturnDocument.salesperson_id.in_(scope_ids))
    returns_count, returns_amount = db.execute(returns_stmt).one()

    invoices_stmt = select(
        func.count(Invoice.id),
        func.coalesce(func.sum(Invoice.open_amount), 0),
    ).where(
        Invoice.is_deleted.is_(False),
        Invoice.invoice_date == day,
        Invoice.status != "CANCELLED",
    )
    if salesperson_id:
        invoices_stmt = invoices_stmt.where(Invoice.salesperson_id == salesperson_id)
    if scope_ids:
        invoices_stmt = invoices_stmt.where(Invoice.salesperson_id.in_(scope_ids))
    invoices_count, open_invoice_amount = db.execute(invoices_stmt).one()

    net_amount = money(net)
    total_amount = money(total)
    total_cost = money(cost)
    margin = money(net_amount - total_cost)

    return {
        "on": day,
        "salesperson_id": salesperson_id,
        "sales_count": int(sales_count or 0),
        "customers_served": int(customers_served or 0),
        "lines_sold": int(lines_sold or 0),
        "gross_amount": money(gross),
        "discount_amount": money(discount),
        "net_amount": net_amount,
        "vat_amount": money(vat),
        "total_amount": total_amount,
        "total_cost": total_cost,
        "margin_amount": margin,
        "margin_percent": pct(margin, net_amount),
        "average_basket": money(total_amount / Decimal(sales_count)) if sales_count else Decimal("0"),
        "collected_amount": money(collections["cleared_amount"]),
        "collected_cash": cash,
        "collected_other": other,
        "pending_instruments": money(collections["pending_amount"]),
        "collections_by_method": by_method,
        "returns_count": int(returns_count or 0),
        "returns_amount": money(returns_amount),
        "invoices_count": int(invoices_count or 0),
        "open_invoice_amount": money(open_invoice_amount),
    }
