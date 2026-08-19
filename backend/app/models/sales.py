"""
Orders, deliveries (sales), invoices, payments and returns.

Document chain
--------------
``Order``   — what the customer asked for (pre-sale *or* hot-sale).
``Sale``    — what was actually handed over; this is what moves stock.
``Invoice`` — the fiscal document (fatura / irsaliye) for a sale.
``Payment`` — money received, allocated across open invoices.
``ReturnDocument`` — goods coming back, with a disposition decision.

In a hot sale (sıcak satış) the order, sale and invoice are created in one
transaction; in pre-sale they are separated in time.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

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
from app.models.base import (
    AuthorMixin,
    Base,
    Money,
    Quantity,
    SoftDeleteMixin,
    TimestampMixin,
    UTCDateTime,
    fk,
    pk,
)


# ===========================================================================
# Orders
# ===========================================================================
class Order(Base, TimestampMixin, SoftDeleteMixin, AuthorMixin):
    """Customer order header."""

    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("order_no", name="uq_orders_order_no"),
        Index("ix_orders_customer_date", "customer_id", "order_date"),
        Index("ix_orders_salesperson_date", "salesperson_id", "order_date"),
        Index("ix_orders_status_type", "status", "order_type"),
    )

    id: Mapped[int] = pk()
    order_no: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    #: Client-generated UUID.  The field app works offline and retries on
    #: reconnect; the unique index makes a replayed submission a no-op instead
    #: of a duplicate order that double-counts stock and money.
    client_uid: Mapped[str | None] = mapped_column(String(36), unique=True, index=True)
    order_type: Mapped[str] = mapped_column(
        String(16), default=OrderType.HOT_SALE, nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(24), default=OrderStatus.DRAFT, nullable=False, index=True
    )

    order_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    delivery_date: Mapped[date | None] = mapped_column(Date, index=True)
    ordered_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    customer_id: Mapped[int] = fk("customers.id")
    salesperson_id: Mapped[int | None] = fk("salespersons.id", nullable=True, ondelete="SET NULL")
    vehicle_id: Mapped[int | None] = fk("vehicles.id", nullable=True, ondelete="SET NULL")
    warehouse_id: Mapped[int | None] = fk("warehouses.id", nullable=True, ondelete="SET NULL")
    route_id: Mapped[int | None] = fk("routes.id", nullable=True, ondelete="SET NULL")
    visit_id: Mapped[int | None] = fk("visits.id", nullable=True, ondelete="SET NULL")
    day_session_id: Mapped[int | None] = fk("day_sessions.id", nullable=True, ondelete="SET NULL")
    price_list_id: Mapped[int | None] = fk("price_lists.id", nullable=True, ondelete="SET NULL")

    # --- Money -------------------------------------------------------------
    currency: Mapped[str] = mapped_column(String(8), default="TRY", nullable=False)
    gross_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    line_discount_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    campaign_discount_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    header_discount_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    header_discount_percent: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    net_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    vat_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    excise_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False, index=True)
    total_cost: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    margin_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)

    payment_method: Mapped[str] = mapped_column(
        String(24), default=PaymentMethod.CASH, nullable=False
    )
    payment_term_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    total_volume_l: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    total_weight_kg: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    line_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    is_ai_assisted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    cancel_reason: Mapped[str | None] = mapped_column(String(255))

    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", lazy="selectin"
    )
    customer: Mapped["Customer"] = relationship(lazy="joined")  # noqa: F821


class OrderItem(Base):
    __tablename__ = "order_items"
    __table_args__ = (Index("ix_order_items_order_prod", "order_id", "product_id"),)

    id: Mapped[int] = pk()
    order_id: Mapped[int] = fk("orders.id", ondelete="CASCADE")
    product_id: Mapped[int] = fk("products.id")
    line_no: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    uom: Mapped[str] = mapped_column(String(16), nullable=False)
    uom_factor: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("1"), nullable=False)
    base_quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    delivered_quantity: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("0"), nullable=False)

    unit_price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    list_price: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    gross_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    discount_percent: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    discount_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    campaign_discount_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    net_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    vat_rate: Mapped[float] = mapped_column(Float, default=20.0, nullable=False)
    vat_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    unit_cost: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)

    #: Free goods granted by a campaign are priced at zero but still move stock.
    is_free_goods: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    campaign_id: Mapped[int | None] = mapped_column(Integer, index=True)
    is_ai_suggested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notes: Mapped[str | None] = mapped_column(String(255))

    order: Mapped["Order"] = relationship(back_populates="items")
    product: Mapped["Product"] = relationship(lazy="joined")  # noqa: F821


# ===========================================================================
# Sales (deliveries)
# ===========================================================================
class Sale(Base, TimestampMixin, SoftDeleteMixin, AuthorMixin):
    """
    A completed delivery.  Posting a sale writes the stock movements that
    decrement the van (or warehouse) and the customer ledger entry.
    """

    __tablename__ = "sales"
    __table_args__ = (
        UniqueConstraint("sale_no", name="uq_sales_sale_no"),
        Index("ix_sales_customer_date", "customer_id", "sale_date"),
        Index("ix_sales_salesperson_date", "salesperson_id", "sale_date"),
        Index("ix_sales_date_amount", "sale_date", "total_amount"),
    )

    id: Mapped[int] = pk()
    sale_no: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    #: Offline-sync idempotency key — see Order.client_uid.
    client_uid: Mapped[str | None] = mapped_column(String(36), unique=True, index=True)
    order_id: Mapped[int | None] = fk("orders.id", nullable=True, ondelete="SET NULL")
    sale_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    sold_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)

    customer_id: Mapped[int] = fk("customers.id")
    salesperson_id: Mapped[int | None] = fk("salespersons.id", nullable=True, ondelete="SET NULL")
    vehicle_id: Mapped[int | None] = fk("vehicles.id", nullable=True, ondelete="SET NULL")
    warehouse_id: Mapped[int] = fk("warehouses.id")
    route_id: Mapped[int | None] = fk("routes.id", nullable=True, ondelete="SET NULL")
    visit_id: Mapped[int | None] = fk("visits.id", nullable=True, ondelete="SET NULL")
    day_session_id: Mapped[int | None] = fk("day_sessions.id", nullable=True, ondelete="SET NULL")

    is_hot_sale: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    is_posted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    posted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    is_cancelled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    cancel_reason: Mapped[str | None] = mapped_column(String(255))

    currency: Mapped[str] = mapped_column(String(8), default="TRY", nullable=False)
    gross_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    discount_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    campaign_discount_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    net_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    vat_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    excise_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False, index=True)
    total_cost: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    margin_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)

    paid_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    due_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    payment_method: Mapped[str] = mapped_column(String(24), default=PaymentMethod.CASH, nullable=False)

    total_volume_l: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    total_weight_kg: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    line_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    signature_path: Mapped[str | None] = mapped_column(String(512))
    notes: Mapped[str | None] = mapped_column(Text)

    items: Mapped[list["SaleItem"]] = relationship(
        back_populates="sale", cascade="all, delete-orphan", lazy="selectin"
    )
    customer: Mapped["Customer"] = relationship(lazy="joined")  # noqa: F821

    @property
    def margin_percent(self) -> float:
        if not self.net_amount:
            return 0.0
        return round(float(self.margin_amount / self.net_amount * 100), 2)


class SaleItem(Base):
    """
    A delivered line.  ``lot_id`` records which batch actually left stock,
    which is what makes food-safety traceability and FEFO auditable.
    """

    __tablename__ = "sale_items"
    __table_args__ = (
        Index("ix_sale_items_sale_prod", "sale_id", "product_id"),
    )

    id: Mapped[int] = pk()
    sale_id: Mapped[int] = fk("sales.id", ondelete="CASCADE")
    order_item_id: Mapped[int | None] = mapped_column(Integer, index=True)
    product_id: Mapped[int] = fk("products.id")
    lot_id: Mapped[int | None] = fk("lots.id", nullable=True, ondelete="SET NULL")
    line_no: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    uom: Mapped[str] = mapped_column(String(16), nullable=False)
    uom_factor: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("1"), nullable=False)
    base_quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)

    unit_price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    list_price: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    gross_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    discount_percent: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    discount_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    campaign_discount_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    net_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    vat_rate: Mapped[float] = mapped_column(Float, default=20.0, nullable=False)
    vat_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    excise_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    unit_cost: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    total_cost: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    margin_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)

    is_free_goods: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    campaign_id: Mapped[int | None] = mapped_column(Integer, index=True)
    returned_quantity: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("0"), nullable=False)

    sale: Mapped["Sale"] = relationship(back_populates="items")
    product: Mapped["Product"] = relationship(lazy="joined")  # noqa: F821


# ===========================================================================
# Invoices
# ===========================================================================
class Invoice(Base, TimestampMixin, SoftDeleteMixin, AuthorMixin):
    """Fiscal document: invoice (fatura), waybill (irsaliye) or credit note."""

    __tablename__ = "invoices"
    __table_args__ = (
        UniqueConstraint("invoice_no", name="uq_invoices_invoice_no"),
        Index("ix_invoices_customer_date", "customer_id", "invoice_date"),
        Index("ix_invoices_status_due", "status", "due_date"),
    )

    id: Mapped[int] = pk()
    invoice_no: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    document_type: Mapped[str] = mapped_column(
        String(16), default=DocumentType.INVOICE, nullable=False, index=True
    )
    serial: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(
        String(24), default=InvoiceStatus.DRAFT, nullable=False, index=True
    )

    sale_id: Mapped[int | None] = fk("sales.id", nullable=True, ondelete="SET NULL")
    return_id: Mapped[int | None] = mapped_column(Integer, index=True)
    customer_id: Mapped[int] = fk("customers.id")
    salesperson_id: Mapped[int | None] = fk("salespersons.id", nullable=True, ondelete="SET NULL")

    invoice_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    due_date: Mapped[date | None] = mapped_column(Date, index=True)
    issued_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    currency: Mapped[str] = mapped_column(String(8), default="TRY", nullable=False)
    net_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    discount_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    vat_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    excise_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    paid_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    open_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False, index=True)

    e_invoice_uuid: Mapped[str | None] = mapped_column(String(64))
    pdf_path: Mapped[str | None] = mapped_column(String(512))
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    cancel_reason: Mapped[str | None] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(Text)

    items: Mapped[list["InvoiceItem"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan", lazy="selectin"
    )
    customer: Mapped["Customer"] = relationship(lazy="joined")  # noqa: F821

    @property
    def is_overdue(self) -> bool:
        return bool(
            self.due_date
            and self.open_amount > 0
            and self.due_date < date.today()
            and self.status not in (InvoiceStatus.CANCELLED, InvoiceStatus.PAID)
        )


class InvoiceItem(Base):
    __tablename__ = "invoice_items"

    id: Mapped[int] = pk()
    invoice_id: Mapped[int] = fk("invoices.id", ondelete="CASCADE")
    product_id: Mapped[int] = fk("products.id")
    line_no: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    description: Mapped[str | None] = mapped_column(String(255))

    quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    uom: Mapped[str] = mapped_column(String(16), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    discount_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    net_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    vat_rate: Mapped[float] = mapped_column(Float, default=20.0, nullable=False)
    vat_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)

    invoice: Mapped["Invoice"] = relationship(back_populates="items")


# ===========================================================================
# Payments / collections
# ===========================================================================
class Payment(Base, TimestampMixin, SoftDeleteMixin, AuthorMixin):
    """A collection (tahsilat) from a customer, allocatable across invoices."""

    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("payment_no", name="uq_payments_payment_no"),
        Index("ix_payments_customer_date", "customer_id", "payment_date"),
        Index("ix_payments_salesperson_date", "salesperson_id", "payment_date"),
        Index("ix_payments_method_status", "payment_method", "status"),
    )

    id: Mapped[int] = pk()
    payment_no: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    #: Offline-sync idempotency key — see Order.client_uid.
    client_uid: Mapped[str | None] = mapped_column(String(36), unique=True, index=True)
    customer_id: Mapped[int] = fk("customers.id")
    salesperson_id: Mapped[int | None] = fk("salespersons.id", nullable=True, ondelete="SET NULL")
    sale_id: Mapped[int | None] = fk("sales.id", nullable=True, ondelete="SET NULL")
    visit_id: Mapped[int | None] = fk("visits.id", nullable=True, ondelete="SET NULL")
    day_session_id: Mapped[int | None] = fk("day_sessions.id", nullable=True, ondelete="SET NULL")

    payment_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    received_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    payment_method: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(16), default=PaymentStatus.CLEARED, nullable=False, index=True
    )

    currency: Mapped[str] = mapped_column(String(8), default="TRY", nullable=False)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    allocated_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    unallocated_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)

    # --- Instrument details (cheque / promissory note) ---------------------
    bank_name: Mapped[str | None] = mapped_column(String(128))
    document_number: Mapped[str | None] = mapped_column(String(64), index=True)
    maturity_date: Mapped[date | None] = mapped_column(Date, index=True)
    drawer_name: Mapped[str | None] = mapped_column(String(255))
    bounced_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    reference: Mapped[str | None] = mapped_column(String(128))
    receipt_path: Mapped[str | None] = mapped_column(String(512))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str | None] = mapped_column(Text)

    allocations: Mapped[list["PaymentAllocation"]] = relationship(
        back_populates="payment", cascade="all, delete-orphan", lazy="selectin"
    )
    customer: Mapped["Customer"] = relationship(lazy="joined")  # noqa: F821


class PaymentAllocation(Base, TimestampMixin):
    """How much of a payment was applied to a specific invoice."""

    __tablename__ = "payment_allocations"
    __table_args__ = (
        UniqueConstraint("payment_id", "invoice_id", name="uq_payment_allocations_pay_inv"),
    )

    id: Mapped[int] = pk()
    payment_id: Mapped[int] = fk("payments.id", ondelete="CASCADE")
    invoice_id: Mapped[int] = fk("invoices.id", ondelete="CASCADE")
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)

    payment: Mapped["Payment"] = relationship(back_populates="allocations")
    invoice: Mapped["Invoice"] = relationship(lazy="joined")


# ===========================================================================
# Returns
# ===========================================================================
class ReturnDocument(Base, TimestampMixin, SoftDeleteMixin, AuthorMixin):
    """Goods returned by a customer (iade)."""

    __tablename__ = "returns"
    __table_args__ = (
        UniqueConstraint("return_no", name="uq_returns_return_no"),
        Index("ix_returns_customer_date", "customer_id", "return_date"),
        Index("ix_returns_reason", "reason"),
    )

    id: Mapped[int] = pk()
    return_no: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    #: Offline-sync idempotency key — see Order.client_uid.
    client_uid: Mapped[str | None] = mapped_column(String(36), unique=True, index=True)
    return_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    customer_id: Mapped[int] = fk("customers.id")
    sale_id: Mapped[int | None] = fk("sales.id", nullable=True, ondelete="SET NULL")
    salesperson_id: Mapped[int | None] = fk("salespersons.id", nullable=True, ondelete="SET NULL")
    vehicle_id: Mapped[int | None] = fk("vehicles.id", nullable=True, ondelete="SET NULL")
    warehouse_id: Mapped[int] = fk("warehouses.id")
    visit_id: Mapped[int | None] = fk("visits.id", nullable=True, ondelete="SET NULL")
    day_session_id: Mapped[int | None] = fk("day_sessions.id", nullable=True, ondelete="SET NULL")

    reason: Mapped[str] = mapped_column(String(24), default=ReturnReason.OTHER, nullable=False)
    disposition: Mapped[str] = mapped_column(
        String(16), default=ReturnDisposition.RESALEABLE, nullable=False, index=True
    )
    is_posted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    posted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    creates_credit_note: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    currency: Mapped[str] = mapped_column(String(8), default="TRY", nullable=False)
    net_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    vat_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    total_cost: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    line_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    photo_path: Mapped[str | None] = mapped_column(String(512))
    notes: Mapped[str | None] = mapped_column(Text)

    items: Mapped[list["ReturnItem"]] = relationship(
        back_populates="return_doc", cascade="all, delete-orphan", lazy="selectin"
    )
    customer: Mapped["Customer"] = relationship(lazy="joined")  # noqa: F821


class ReturnItem(Base):
    __tablename__ = "return_items"
    __table_args__ = (Index("ix_return_items_return_prod", "return_id", "product_id"),)

    id: Mapped[int] = pk()
    return_id: Mapped[int] = fk("returns.id", ondelete="CASCADE")
    sale_item_id: Mapped[int | None] = mapped_column(Integer, index=True)
    product_id: Mapped[int] = fk("products.id")
    lot_id: Mapped[int | None] = fk("lots.id", nullable=True, ondelete="SET NULL")
    line_no: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    uom: Mapped[str] = mapped_column(String(16), nullable=False)
    uom_factor: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("1"), nullable=False)
    base_quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)

    unit_price: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    net_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    vat_rate: Mapped[float] = mapped_column(Float, default=20.0, nullable=False)
    vat_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    unit_cost: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)

    reason: Mapped[str] = mapped_column(String(24), default=ReturnReason.OTHER, nullable=False)
    disposition: Mapped[str] = mapped_column(
        String(16), default=ReturnDisposition.RESALEABLE, nullable=False
    )
    expiry_date: Mapped[date | None] = mapped_column(Date)

    return_doc: Mapped["ReturnDocument"] = relationship(back_populates="items")
    product: Mapped["Product"] = relationship(lazy="joined")  # noqa: F821
