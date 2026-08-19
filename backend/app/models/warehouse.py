"""
Warehouses, lots, stock balances and the immutable stock movement ledger.

Design
------
``stock_movements`` is an **append-only ledger** and the single source of
truth.  ``stock_balances`` is a materialised per-(warehouse, product, lot,
status) balance maintained inside the same transaction, so reads are O(1)
while the ledger stays fully auditable and replayable.

A sales vehicle is modelled as a warehouse of type ``VEHICLE`` — the van is
literally a mobile depot, so every stock rule applies to it unchanged.
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
    CountStatus,
    StockStatus,
    TransferStatus,
    WarehouseType,
)
from app.models.base import (
    AuthorMixin,
    Base,
    CodeNameMixin,
    Money,
    Quantity,
    SoftDeleteMixin,
    TimestampMixin,
    UTCDateTime,
    fk,
    pk,
    utcnow,
)


class Warehouse(Base, CodeNameMixin, TimestampMixin, SoftDeleteMixin, AuthorMixin):
    """Any stock-holding location: central, regional, transit, vehicle or quarantine."""

    __tablename__ = "warehouses"
    __table_args__ = (
        UniqueConstraint("code", name="uq_warehouses_code"),
        Index("ix_warehouses_type_active", "warehouse_type", "is_active"),
    )

    id: Mapped[int] = pk()
    company_id: Mapped[int | None] = fk("companies.id", nullable=True, ondelete="SET NULL")
    region_id: Mapped[int | None] = fk("regions.id", nullable=True, ondelete="SET NULL")
    branch_id: Mapped[int | None] = fk("branches.id", nullable=True, ondelete="SET NULL")
    parent_id: Mapped[int | None] = fk("warehouses.id", nullable=True, ondelete="SET NULL")

    warehouse_type: Mapped[str] = mapped_column(
        String(16), default=WarehouseType.CENTRAL, nullable=False, index=True
    )
    manager_id: Mapped[int | None] = fk("users.id", nullable=True, ondelete="SET NULL")

    address: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(String(96))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)

    capacity_volume_l: Mapped[float | None] = mapped_column(Float)
    capacity_weight_kg: Mapped[float | None] = mapped_column(Float)
    allows_negative_stock: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    allocation_strategy: Mapped[str] = mapped_column(String(8), default="FEFO", nullable=False)

    @property
    def is_vehicle(self) -> bool:
        return self.warehouse_type == WarehouseType.VEHICLE


class Lot(Base, TimestampMixin, AuthorMixin):
    """
    Production lot / batch.  Carries the expiry date that drives FEFO picking.

    ``expiry_date`` is indexed because every FEFO allocation orders by it.
    """

    __tablename__ = "lots"
    __table_args__ = (
        UniqueConstraint("product_id", "lot_number", name="uq_lots_product_lot"),
        Index("ix_lots_product_expiry", "product_id", "expiry_date"),
    )

    id: Mapped[int] = pk()
    product_id: Mapped[int] = fk("products.id", ondelete="CASCADE")
    lot_number: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    batch_number: Mapped[str | None] = mapped_column(String(64))
    serial_number: Mapped[str | None] = mapped_column(String(96), index=True)

    production_date: Mapped[date | None] = mapped_column(Date, index=True)
    expiry_date: Mapped[date | None] = mapped_column(Date, index=True)
    received_date: Mapped[date | None] = mapped_column(Date)

    supplier_name: Mapped[str | None] = mapped_column(String(255))
    unit_cost: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    block_reason: Mapped[str | None] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(Text)

    product: Mapped["Product"] = relationship(lazy="joined")  # noqa: F821

    def days_to_expiry(self, ref: date | None = None) -> int | None:
        if not self.expiry_date:
            return None
        return (self.expiry_date - (ref or date.today())).days

    def is_expired(self, ref: date | None = None) -> bool:
        d = self.days_to_expiry(ref)
        return d is not None and d < 0


class StockBalance(Base, TimestampMixin):
    """
    Materialised on-hand balance for one (warehouse, product, lot, status) key.

    ``quantity`` is in the product's **base unit**.  ``available`` subtracts
    reservations and is what the sales flow may consume.
    """

    __tablename__ = "stock_balances"
    __table_args__ = (
        UniqueConstraint(
            "warehouse_id", "product_id", "lot_id", "status",
            name="uq_stock_balances_wh_prod_lot_status",
        ),
        Index("ix_stock_balances_wh_prod", "warehouse_id", "product_id"),
        Index("ix_stock_balances_prod_status", "product_id", "status"),
    )

    id: Mapped[int] = pk()
    warehouse_id: Mapped[int] = fk("warehouses.id", ondelete="CASCADE")
    product_id: Mapped[int] = fk("products.id", ondelete="CASCADE")
    #: 0 sentinel (not NULL) so the unique constraint works on both backends.
    lot_id: Mapped[int] = mapped_column(Integer, default=0, nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(16), default=StockStatus.AVAILABLE, nullable=False
    )

    quantity: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("0"), nullable=False)
    reserved_quantity: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("0"), nullable=False)
    average_cost: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    last_movement_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    warehouse: Mapped["Warehouse"] = relationship(lazy="joined")
    product: Mapped["Product"] = relationship(lazy="joined")  # noqa: F821

    @property
    def available(self) -> Decimal:
        return self.quantity - self.reserved_quantity

    @property
    def value(self) -> Decimal:
        return (self.quantity * self.average_cost).quantize(Decimal("0.0001"))


class StockMovement(Base):
    """
    Immutable stock ledger entry.

    Never updated or deleted — corrections are posted as new movements.  This
    is what makes van reconciliation and stock audits provable.
    """

    __tablename__ = "stock_movements"
    __table_args__ = (
        Index("ix_stock_movements_wh_prod_time", "warehouse_id", "product_id", "moved_at"),
        Index("ix_stock_movements_ref", "reference_type", "reference_id"),
        Index("ix_stock_movements_type_time", "movement_type", "moved_at"),
    )

    id: Mapped[int] = pk()
    warehouse_id: Mapped[int] = fk("warehouses.id", ondelete="RESTRICT")
    product_id: Mapped[int] = fk("products.id", ondelete="RESTRICT")
    lot_id: Mapped[int | None] = fk("lots.id", nullable=True, ondelete="SET NULL")

    movement_type: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), default=StockStatus.AVAILABLE, nullable=False)

    #: Signed: positive = stock in, negative = stock out.  Base units.
    quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    unit_cost: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    total_cost: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)

    #: Running balance of that (warehouse, product) after this movement.
    balance_after: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("0"), nullable=False)

    counterparty_warehouse_id: Mapped[int | None] = mapped_column(Integer, index=True)
    reference_type: Mapped[str | None] = mapped_column(String(32), index=True)
    reference_id: Mapped[int | None] = mapped_column(Integer, index=True)
    reference_no: Mapped[str | None] = mapped_column(String(64))

    salesperson_id: Mapped[int | None] = mapped_column(Integer, index=True)
    customer_id: Mapped[int | None] = mapped_column(Integer, index=True)
    day_session_id: Mapped[int | None] = mapped_column(Integer, index=True)

    moved_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, nullable=False, index=True
    )
    created_by_id: Mapped[int | None] = mapped_column(Integer, index=True)
    notes: Mapped[str | None] = mapped_column(Text)

    product: Mapped["Product"] = relationship(lazy="joined")  # noqa: F821
    lot: Mapped["Lot | None"] = relationship(lazy="joined")


class StockTransfer(Base, TimestampMixin, AuthorMixin):
    """Warehouse-to-warehouse (or warehouse-to-vehicle) transfer document."""

    __tablename__ = "stock_transfers"
    __table_args__ = (
        UniqueConstraint("document_no", name="uq_stock_transfers_document_no"),
        Index("ix_stock_transfers_status_date", "status", "transfer_date"),
    )

    id: Mapped[int] = pk()
    document_no: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_warehouse_id: Mapped[int] = fk("warehouses.id")
    target_warehouse_id: Mapped[int] = fk("warehouses.id")
    status: Mapped[str] = mapped_column(String(16), default=TransferStatus.DRAFT, nullable=False, index=True)
    transfer_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    shipped_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    received_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    received_by_id: Mapped[int | None] = mapped_column(Integer)
    vehicle_id: Mapped[int | None] = mapped_column(Integer, index=True)
    driver_id: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(Text)

    items: Mapped[list["StockTransferItem"]] = relationship(
        back_populates="transfer", cascade="all, delete-orphan", lazy="selectin"
    )


class StockTransferItem(Base):
    __tablename__ = "stock_transfer_items"

    id: Mapped[int] = pk()
    transfer_id: Mapped[int] = fk("stock_transfers.id", ondelete="CASCADE")
    product_id: Mapped[int] = fk("products.id")
    lot_id: Mapped[int | None] = fk("lots.id", nullable=True, ondelete="SET NULL")
    quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    received_quantity: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("0"), nullable=False)
    uom: Mapped[str] = mapped_column(String(16), nullable=False)
    unit_cost: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)

    transfer: Mapped["StockTransfer"] = relationship(back_populates="items")
    product: Mapped["Product"] = relationship(lazy="joined")  # noqa: F821


class StockCount(Base, TimestampMixin, AuthorMixin):
    """Physical count (sayım) — including the end-of-day van count."""

    __tablename__ = "stock_counts"
    __table_args__ = (
        UniqueConstraint("document_no", name="uq_stock_counts_document_no"),
        Index("ix_stock_counts_wh_date", "warehouse_id", "count_date"),
    )

    id: Mapped[int] = pk()
    document_no: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    warehouse_id: Mapped[int] = fk("warehouses.id")
    status: Mapped[str] = mapped_column(String(16), default=CountStatus.DRAFT, nullable=False, index=True)
    count_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    counted_by_id: Mapped[int | None] = mapped_column(Integer)
    approved_by_id: Mapped[int | None] = mapped_column(Integer)
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    day_session_id: Mapped[int | None] = mapped_column(Integer, index=True)
    is_van_end_of_day: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    total_variance_qty: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("0"), nullable=False)
    total_variance_value: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    items: Mapped[list["StockCountItem"]] = relationship(
        back_populates="count", cascade="all, delete-orphan", lazy="selectin"
    )


class StockCountItem(Base):
    """One counted line: system (theoretical) vs counted (physical)."""

    __tablename__ = "stock_count_items"
    __table_args__ = (Index("ix_stock_count_items_count_prod", "count_id", "product_id"),)

    id: Mapped[int] = pk()
    count_id: Mapped[int] = fk("stock_counts.id", ondelete="CASCADE")
    product_id: Mapped[int] = fk("products.id")
    lot_id: Mapped[int | None] = fk("lots.id", nullable=True, ondelete="SET NULL")

    system_quantity: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("0"), nullable=False)
    counted_quantity: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("0"), nullable=False)
    variance_quantity: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("0"), nullable=False)
    unit_cost: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    variance_value: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255))

    count: Mapped["StockCount"] = relationship(back_populates="items")
    product: Mapped["Product"] = relationship(lazy="joined")  # noqa: F821
