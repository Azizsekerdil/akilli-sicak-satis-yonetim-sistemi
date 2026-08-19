"""
Pydantic contracts for the sales module: orders, hot sales, invoices,
collections and returns.

Money is exchanged as ``Decimal`` everywhere — the field app and the backend
must agree on the last kuruş, so no value in this module is ever a float.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.enums import (
    DocumentType,
    InvoiceStatus,
    OrderStatus,
    OrderType,
    PaymentMethod,
    PaymentStatus,
    ReturnDisposition,
    ReturnReason,
)
from app.schemas.common import ORMModel


# ===========================================================================
# Orders
# ===========================================================================
class OrderLineIn(BaseModel):
    """One requested line.  ``uom`` is the selling unit (e.g. CASE)."""

    product_id: int = Field(gt=0)
    quantity: Decimal = Field(gt=0)
    uom: str = Field(min_length=1, max_length=16)
    discount_percent: float = Field(default=0.0, ge=0, le=100)
    #: Only honoured for callers holding ``sales.price_override:EXECUTE``.
    unit_price_override: Decimal | None = Field(default=None, ge=0)
    notes: str | None = Field(default=None, max_length=255)

    def to_line(self) -> dict[str, Any]:
        return {
            "product_id": self.product_id,
            "quantity": self.quantity,
            "uom": self.uom,
            "discount_percent": self.discount_percent,
            "unit_price_override": self.unit_price_override,
            "notes": self.notes,
        }


class OrderCreate(BaseModel):
    customer_id: int = Field(gt=0)
    lines: list[OrderLineIn] = Field(min_length=1)
    order_type: OrderType = OrderType.PRE_SALE
    payment_method: PaymentMethod = PaymentMethod.CASH
    salesperson_id: int | None = None
    vehicle_id: int | None = None
    warehouse_id: int | None = None
    route_id: int | None = None
    visit_id: int | None = None
    day_session_id: int | None = None
    delivery_date: date | None = None
    header_discount_percent: float = Field(default=0.0, ge=0, le=100)
    notes: str | None = None

    @field_validator("lines")
    @classmethod
    def _no_empty(cls, v: list[OrderLineIn]) -> list[OrderLineIn]:
        if not v:
            raise ValueError("order.empty")
        return v


class OrderItemOut(ORMModel):
    id: int
    product_id: int
    line_no: int
    quantity: Decimal
    uom: str
    uom_factor: Decimal
    base_quantity: Decimal
    delivered_quantity: Decimal
    unit_price: Decimal
    list_price: Decimal
    gross_amount: Decimal
    discount_percent: float
    discount_amount: Decimal
    campaign_discount_amount: Decimal
    net_amount: Decimal
    vat_rate: float
    vat_amount: Decimal
    total_amount: Decimal
    unit_cost: Decimal
    is_free_goods: bool
    campaign_id: int | None = None
    notes: str | None = None


class OrderListItem(ORMModel):
    id: int
    order_no: str
    order_type: str
    status: str
    order_date: date
    delivery_date: date | None = None
    customer_id: int
    customer_name: str | None = None
    salesperson_id: int | None = None
    vehicle_id: int | None = None
    currency: str = "TRY"
    net_amount: Decimal
    vat_amount: Decimal
    total_amount: Decimal
    line_count: int
    payment_method: str


class OrderOut(OrderListItem):
    warehouse_id: int | None = None
    route_id: int | None = None
    visit_id: int | None = None
    day_session_id: int | None = None
    gross_amount: Decimal
    line_discount_amount: Decimal
    campaign_discount_amount: Decimal
    header_discount_amount: Decimal
    header_discount_percent: float
    excise_amount: Decimal
    total_cost: Decimal
    margin_amount: Decimal
    payment_term_days: int
    total_volume_l: float
    total_weight_kg: float
    notes: str | None = None
    cancelled_at: datetime | None = None
    cancel_reason: str | None = None
    created_at: datetime | None = None
    items: list[OrderItemOut] = Field(default_factory=list)


class OrderDeliverIn(BaseModel):
    """Turn a pre-sale order into a delivered sale."""

    warehouse_id: int | None = Field(default=None, gt=0)
    vehicle_id: int | None = Field(default=None, gt=0)
    create_invoice: bool = True


# ===========================================================================
# Sales
# ===========================================================================
class SaleItemOut(ORMModel):
    id: int
    product_id: int
    lot_id: int | None = None
    line_no: int
    quantity: Decimal
    uom: str
    uom_factor: Decimal
    base_quantity: Decimal
    unit_price: Decimal
    list_price: Decimal
    gross_amount: Decimal
    discount_percent: float
    discount_amount: Decimal
    campaign_discount_amount: Decimal
    net_amount: Decimal
    vat_rate: float
    vat_amount: Decimal
    excise_amount: Decimal
    total_amount: Decimal
    unit_cost: Decimal
    total_cost: Decimal
    margin_amount: Decimal
    is_free_goods: bool
    campaign_id: int | None = None
    returned_quantity: Decimal


class SaleListItem(ORMModel):
    id: int
    sale_no: str
    sale_date: date
    customer_id: int
    customer_name: str | None = None
    salesperson_id: int | None = None
    vehicle_id: int | None = None
    warehouse_id: int
    is_hot_sale: bool
    is_posted: bool
    is_cancelled: bool
    currency: str = "TRY"
    net_amount: Decimal
    vat_amount: Decimal
    total_amount: Decimal
    paid_amount: Decimal
    due_amount: Decimal
    payment_method: str
    line_count: int


class SaleOut(SaleListItem):
    order_id: int | None = None
    route_id: int | None = None
    visit_id: int | None = None
    day_session_id: int | None = None
    sold_at: datetime | None = None
    posted_at: datetime | None = None
    cancelled_at: datetime | None = None
    cancel_reason: str | None = None
    gross_amount: Decimal
    discount_amount: Decimal
    campaign_discount_amount: Decimal
    excise_amount: Decimal
    total_cost: Decimal
    margin_amount: Decimal
    margin_percent: float = 0.0
    total_volume_l: float
    total_weight_kg: float
    latitude: float | None = None
    longitude: float | None = None
    notes: str | None = None
    items: list[SaleItemOut] = Field(default_factory=list)


class CancelIn(BaseModel):
    reason: str = Field(min_length=3, max_length=255)


# ===========================================================================
# Hot sale (sıcak satış)
# ===========================================================================
class HotSalePaymentIn(BaseModel):
    """Money taken at the point of delivery."""

    method: PaymentMethod = PaymentMethod.CASH
    amount: Decimal = Field(gt=0)
    bank_name: str | None = Field(default=None, max_length=128)
    document_number: str | None = Field(default=None, max_length=64)
    maturity_date: date | None = None
    drawer_name: str | None = Field(default=None, max_length=255)
    reference: str | None = Field(default=None, max_length=128)
    notes: str | None = None


class HotSaleIn(BaseModel):
    """The single payload the field app posts to sell from the van."""

    model_config = ConfigDict(populate_by_name=True)

    customer_id: int = Field(gt=0)
    lines: list[OrderLineIn] = Field(min_length=1)
    payment: HotSalePaymentIn | None = None
    salesperson_id: int | None = Field(default=None, gt=0)
    vehicle_id: int | None = Field(default=None, gt=0)
    route_id: int | None = None
    visit_id: int | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    header_discount_percent: float = Field(default=0.0, ge=0, le=100)
    notes: str | None = None


class HotSaleOut(BaseModel):
    order: OrderOut
    sale: SaleOut
    invoice: "InvoiceOut | None" = None
    payment: "PaymentOut | None" = None
    stock_movements: int = 0


# ===========================================================================
# Invoices
# ===========================================================================
class InvoiceItemOut(ORMModel):
    id: int
    product_id: int
    line_no: int
    description: str | None = None
    quantity: Decimal
    uom: str
    unit_price: Decimal
    discount_amount: Decimal
    net_amount: Decimal
    vat_rate: float
    vat_amount: Decimal
    total_amount: Decimal


class InvoiceListItem(ORMModel):
    id: int
    invoice_no: str
    document_type: str
    status: str
    invoice_date: date
    due_date: date | None = None
    customer_id: int
    customer_name: str | None = None
    salesperson_id: int | None = None
    sale_id: int | None = None
    currency: str = "TRY"
    net_amount: Decimal
    vat_amount: Decimal
    total_amount: Decimal
    paid_amount: Decimal
    open_amount: Decimal
    is_overdue: bool = False


class InvoiceOut(InvoiceListItem):
    serial: str | None = None
    return_id: int | None = None
    discount_amount: Decimal
    excise_amount: Decimal
    issued_at: datetime | None = None
    cancelled_at: datetime | None = None
    cancel_reason: str | None = None
    e_invoice_uuid: str | None = None
    pdf_path: str | None = None
    notes: str | None = None
    items: list[InvoiceItemOut] = Field(default_factory=list)


# ===========================================================================
# Payments / collections
# ===========================================================================
class PaymentCreate(BaseModel):
    customer_id: int = Field(gt=0)
    amount: Decimal = Field(gt=0)
    payment_method: PaymentMethod = PaymentMethod.CASH
    payment_date: date | None = None
    salesperson_id: int | None = None
    sale_id: int | None = None
    visit_id: int | None = None
    day_session_id: int | None = None
    #: Explicit allocation targets; otherwise the oldest open invoices are paid first.
    invoice_ids: list[int] | None = None
    bank_name: str | None = Field(default=None, max_length=128)
    document_number: str | None = Field(default=None, max_length=64)
    maturity_date: date | None = None
    drawer_name: str | None = Field(default=None, max_length=255)
    reference: str | None = Field(default=None, max_length=128)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    notes: str | None = None


class AllocationOut(ORMModel):
    id: int
    payment_id: int
    invoice_id: int
    amount: Decimal
    invoice_no: str | None = None
    invoice_date: date | None = None


class PaymentOut(ORMModel):
    id: int
    payment_no: str
    customer_id: int
    customer_name: str | None = None
    salesperson_id: int | None = None
    sale_id: int | None = None
    visit_id: int | None = None
    day_session_id: int | None = None
    payment_date: date
    received_at: datetime | None = None
    payment_method: str
    status: str
    currency: str = "TRY"
    amount: Decimal
    allocated_amount: Decimal
    unallocated_amount: Decimal
    bank_name: str | None = None
    document_number: str | None = None
    maturity_date: date | None = None
    drawer_name: str | None = None
    bounced_at: datetime | None = None
    reference: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    notes: str | None = None
    allocations: list[AllocationOut] = Field(default_factory=list)


class CollectionMethodTotal(BaseModel):
    payment_method: str
    status: str | None = None
    count: int = 0
    amount: Decimal = Decimal("0")


class CollectionsSummaryOut(BaseModel):
    start: date
    end: date
    salesperson_id: int | None = None
    total_amount: Decimal = Decimal("0")
    cleared_amount: Decimal = Decimal("0")
    pending_amount: Decimal = Decimal("0")
    bounced_amount: Decimal = Decimal("0")
    count: int = 0
    by_method: list[CollectionMethodTotal] = Field(default_factory=list)


# ===========================================================================
# Returns
# ===========================================================================
class ReturnLineIn(BaseModel):
    product_id: int = Field(gt=0)
    quantity: Decimal = Field(gt=0)
    uom: str = Field(min_length=1, max_length=16)
    sale_item_id: int | None = None
    lot_id: int | None = None
    unit_price: Decimal | None = Field(default=None, ge=0)
    reason: ReturnReason | None = None
    disposition: ReturnDisposition | None = None
    expiry_date: date | None = None

    def to_line(self) -> dict[str, Any]:
        return {
            "product_id": self.product_id,
            "quantity": self.quantity,
            "uom": self.uom,
            "sale_item_id": self.sale_item_id,
            "lot_id": self.lot_id,
            "unit_price": self.unit_price,
            "reason": str(self.reason) if self.reason else None,
            "disposition": str(self.disposition) if self.disposition else None,
            "expiry_date": self.expiry_date,
        }


class ReturnCreate(BaseModel):
    customer_id: int = Field(gt=0)
    lines: list[ReturnLineIn] = Field(min_length=1)
    reason: ReturnReason = ReturnReason.OTHER
    disposition: ReturnDisposition = ReturnDisposition.RESALEABLE
    warehouse_id: int | None = Field(default=None, gt=0)
    vehicle_id: int | None = Field(default=None, gt=0)
    sale_id: int | None = None
    salesperson_id: int | None = None
    visit_id: int | None = None
    day_session_id: int | None = None
    creates_credit_note: bool = True
    post_now: bool = False
    notes: str | None = None


class ReturnItemOut(ORMModel):
    id: int
    product_id: int
    sale_item_id: int | None = None
    lot_id: int | None = None
    line_no: int
    quantity: Decimal
    uom: str
    uom_factor: Decimal
    base_quantity: Decimal
    unit_price: Decimal
    net_amount: Decimal
    vat_rate: float
    vat_amount: Decimal
    total_amount: Decimal
    unit_cost: Decimal
    reason: str
    disposition: str
    expiry_date: date | None = None


class ReturnListItem(ORMModel):
    id: int
    return_no: str
    return_date: date
    customer_id: int
    customer_name: str | None = None
    sale_id: int | None = None
    salesperson_id: int | None = None
    warehouse_id: int
    reason: str
    disposition: str
    is_posted: bool
    currency: str = "TRY"
    net_amount: Decimal
    vat_amount: Decimal
    total_amount: Decimal
    line_count: int


class ReturnOut(ReturnListItem):
    vehicle_id: int | None = None
    visit_id: int | None = None
    day_session_id: int | None = None
    posted_at: datetime | None = None
    creates_credit_note: bool = True
    total_cost: Decimal
    photo_path: str | None = None
    notes: str | None = None
    credit_note: InvoiceOut | None = None
    items: list[ReturnItemOut] = Field(default_factory=list)


# ===========================================================================
# Field-app summaries
# ===========================================================================
class DailySummaryOut(BaseModel):
    """What the salesperson sees at the bottom of the screen all day."""

    on: date
    salesperson_id: int | None = None
    sales_count: int = 0
    customers_served: int = 0
    lines_sold: int = 0
    gross_amount: Decimal = Decimal("0")
    discount_amount: Decimal = Decimal("0")
    net_amount: Decimal = Decimal("0")
    vat_amount: Decimal = Decimal("0")
    total_amount: Decimal = Decimal("0")
    total_cost: Decimal = Decimal("0")
    margin_amount: Decimal = Decimal("0")
    margin_percent: float = 0.0
    average_basket: Decimal = Decimal("0")
    collected_amount: Decimal = Decimal("0")
    collected_cash: Decimal = Decimal("0")
    collected_other: Decimal = Decimal("0")
    pending_instruments: Decimal = Decimal("0")
    collections_by_method: dict[str, Decimal] = Field(default_factory=dict)
    returns_count: int = 0
    returns_amount: Decimal = Decimal("0")
    invoices_count: int = 0
    open_invoice_amount: Decimal = Decimal("0")


class OpenInvoiceOut(BaseModel):
    """A collectable receivable, oldest first — the collection worklist."""

    invoice_id: int
    invoice_no: str
    invoice_date: date
    due_date: date | None = None
    customer_id: int
    customer_name: str | None = None
    total_amount: Decimal
    paid_amount: Decimal
    open_amount: Decimal
    days_overdue: int = 0
    status: str


HotSaleOut.model_rebuild()


__all__ = [
    "OrderLineIn",
    "OrderCreate",
    "OrderItemOut",
    "OrderListItem",
    "OrderOut",
    "OrderDeliverIn",
    "SaleItemOut",
    "SaleListItem",
    "SaleOut",
    "CancelIn",
    "HotSalePaymentIn",
    "HotSaleIn",
    "HotSaleOut",
    "InvoiceItemOut",
    "InvoiceListItem",
    "InvoiceOut",
    "PaymentCreate",
    "AllocationOut",
    "PaymentOut",
    "CollectionMethodTotal",
    "CollectionsSummaryOut",
    "ReturnLineIn",
    "ReturnCreate",
    "ReturnItemOut",
    "ReturnListItem",
    "ReturnOut",
    "DailySummaryOut",
    "OpenInvoiceOut",
    "DocumentType",
    "InvoiceStatus",
    "OrderStatus",
    "PaymentStatus",
]
