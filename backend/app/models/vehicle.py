"""
Sales vehicles, salespeople, daily work sessions and van load-outs.

A vehicle owns exactly one ``Warehouse`` row of type ``VEHICLE`` — the van
*is* a mobile depot, so all stock logic is shared with fixed warehouses.

The end-of-day reconciliation identity implemented by the services layer:

    morning_load + intraday_reload - sold - returned - wastage = theoretical
    theoretical - physical_count                              = variance
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

from app.core.enums import DaySessionStatus, VehicleStatus, VehicleType
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


class Vehicle(Base, TimestampMixin, SoftDeleteMixin, AuthorMixin):
    """A sales vehicle / van."""

    __tablename__ = "vehicles"
    __table_args__ = (
        UniqueConstraint("plate_number", name="uq_vehicles_plate"),
        UniqueConstraint("warehouse_id", name="uq_vehicles_warehouse"),
        Index("ix_vehicles_status_active", "status", "is_active"),
    )

    id: Mapped[int] = pk()
    code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    plate_number: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String(128))

    #: The VEHICLE-type warehouse that holds this van's stock.
    warehouse_id: Mapped[int | None] = fk("warehouses.id", nullable=True, ondelete="SET NULL")
    region_id: Mapped[int | None] = fk("regions.id", nullable=True, ondelete="SET NULL")
    home_warehouse_id: Mapped[int | None] = fk("warehouses.id", nullable=True, ondelete="SET NULL")

    vehicle_type: Mapped[str] = mapped_column(String(16), default=VehicleType.VAN, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=VehicleStatus.ACTIVE, nullable=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    brand: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(64))
    model_year: Mapped[int | None] = mapped_column(Integer)
    is_refrigerated: Mapped[bool] = mapped_column(Boolean, default=False)

    capacity_volume_l: Mapped[float] = mapped_column(Float, default=8000.0, nullable=False)
    capacity_weight_kg: Mapped[float] = mapped_column(Float, default=3500.0, nullable=False)
    capacity_cases: Mapped[int | None] = mapped_column(Integer)

    default_driver_id: Mapped[int | None] = fk("users.id", nullable=True, ondelete="SET NULL")
    default_salesperson_id: Mapped[int | None] = mapped_column(Integer, index=True)

    fuel_type: Mapped[str | None] = mapped_column(String(24))
    avg_consumption_l_100km: Mapped[float | None] = mapped_column(Float)
    odometer_km: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    insurance_expiry: Mapped[date | None] = mapped_column(Date)
    inspection_expiry: Mapped[date | None] = mapped_column(Date)
    last_maintenance_at: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)

    #: Last known position (updated from GPS events).
    last_lat: Mapped[float | None] = mapped_column(Float)
    last_lng: Mapped[float | None] = mapped_column(Float)
    last_position_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    warehouse: Mapped["Warehouse | None"] = relationship(  # noqa: F821
        foreign_keys=[warehouse_id], lazy="joined"
    )


class Salesperson(Base, TimestampMixin, SoftDeleteMixin, AuthorMixin):
    """
    Field sales profile (plasiyer).

    Separate from ``User`` because a salesperson has commercial attributes
    (targets, commission, default route/vehicle) that a generic user has not.
    """

    __tablename__ = "salespersons"
    __table_args__ = (
        UniqueConstraint("code", name="uq_salespersons_code"),
        UniqueConstraint("user_id", name="uq_salespersons_user"),
    )

    id: Mapped[int] = pk()
    code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    user_id: Mapped[int | None] = fk("users.id", nullable=True, ondelete="SET NULL")
    full_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    phone: Mapped[str | None] = mapped_column(String(32))
    email: Mapped[str | None] = mapped_column(String(255))

    region_id: Mapped[int | None] = fk("regions.id", nullable=True, ondelete="SET NULL")
    supervisor_id: Mapped[int | None] = fk("salespersons.id", nullable=True, ondelete="SET NULL")
    default_vehicle_id: Mapped[int | None] = fk("vehicles.id", nullable=True, ondelete="SET NULL")
    default_warehouse_id: Mapped[int | None] = fk("warehouses.id", nullable=True, ondelete="SET NULL")

    hire_date: Mapped[date | None] = mapped_column(Date)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)

    commission_percent: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    max_discount_percent: Mapped[float] = mapped_column(Float, default=10.0, nullable=False)
    can_sell_on_credit: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    cash_limit: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    vehicle: Mapped["Vehicle | None"] = relationship(lazy="joined")


class DaySession(Base, TimestampMixin, AuthorMixin):
    """
    One salesperson's working day on one vehicle: open → sell → count → close.

    Holds the reconciliation totals produced at day end.
    """

    __tablename__ = "day_sessions"
    __table_args__ = (
        UniqueConstraint(
            "salesperson_id", "vehicle_id", "session_date", name="uq_day_sessions_sp_veh_date"
        ),
        Index("ix_day_sessions_date_status", "session_date", "status"),
    )

    id: Mapped[int] = pk()
    session_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    salesperson_id: Mapped[int] = fk("salespersons.id")
    vehicle_id: Mapped[int] = fk("vehicles.id")
    route_id: Mapped[int | None] = mapped_column(Integer, index=True)
    warehouse_id: Mapped[int | None] = fk("warehouses.id", nullable=True, ondelete="SET NULL")

    status: Mapped[str] = mapped_column(
        String(16), default=DaySessionStatus.OPEN, nullable=False, index=True
    )
    opened_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    opened_by_id: Mapped[int | None] = mapped_column(Integer)
    closed_by_id: Mapped[int | None] = mapped_column(Integer)

    start_odometer_km: Mapped[float | None] = mapped_column(Float)
    end_odometer_km: Mapped[float | None] = mapped_column(Float)

    # --- Reconciliation summary (all in base units / TRY) ------------------
    loaded_qty: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("0"), nullable=False)
    reloaded_qty: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("0"), nullable=False)
    sold_qty: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("0"), nullable=False)
    returned_qty: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("0"), nullable=False)
    wastage_qty: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("0"), nullable=False)
    theoretical_qty: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("0"), nullable=False)
    counted_qty: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("0"), nullable=False)
    variance_qty: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("0"), nullable=False)
    variance_value: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)

    total_sales_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    total_collected_cash: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    total_collected_other: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    declared_cash: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    cash_variance: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)

    visits_planned: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    visits_done: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    invoices_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    has_variance: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    notes: Mapped[str | None] = mapped_column(Text)

    salesperson: Mapped["Salesperson"] = relationship(lazy="joined")
    vehicle: Mapped["Vehicle"] = relationship(lazy="joined")


class VanLoad(Base, TimestampMixin, AuthorMixin):
    """Load-out document: goods moved from a depot onto a van."""

    __tablename__ = "van_loads"
    __table_args__ = (
        UniqueConstraint("document_no", name="uq_van_loads_document_no"),
        Index("ix_van_loads_date_vehicle", "load_date", "vehicle_id"),
    )

    id: Mapped[int] = pk()
    document_no: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: Offline-sync idempotency key — see Order.client_uid.
    client_uid: Mapped[str | None] = mapped_column(String(36), unique=True, index=True)
    load_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    day_session_id: Mapped[int | None] = fk("day_sessions.id", nullable=True, ondelete="SET NULL")
    vehicle_id: Mapped[int] = fk("vehicles.id")
    salesperson_id: Mapped[int | None] = fk("salespersons.id", nullable=True, ondelete="SET NULL")
    source_warehouse_id: Mapped[int] = fk("warehouses.id")

    #: True when this is an extra top-up during the day, not the morning load.
    is_reload: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_ai_suggested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ai_confidence: Mapped[float | None] = mapped_column(Float)
    ai_explanation: Mapped[str | None] = mapped_column(Text)

    is_posted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    posted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    total_volume_l: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    total_weight_kg: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    total_cost: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    items: Mapped[list["VanLoadItem"]] = relationship(
        back_populates="load", cascade="all, delete-orphan", lazy="selectin"
    )
    vehicle: Mapped["Vehicle"] = relationship(lazy="joined")


class VanLoadItem(Base):
    __tablename__ = "van_load_items"
    __table_args__ = (Index("ix_van_load_items_load_prod", "load_id", "product_id"),)

    id: Mapped[int] = pk()
    load_id: Mapped[int] = fk("van_loads.id", ondelete="CASCADE")
    product_id: Mapped[int] = fk("products.id")
    lot_id: Mapped[int | None] = fk("lots.id", nullable=True, ondelete="SET NULL")

    #: Requested / suggested quantity, in the chosen UoM.
    planned_quantity: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("0"), nullable=False)
    #: Actually loaded quantity, in the chosen UoM.
    quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    uom: Mapped[str] = mapped_column(String(16), nullable=False)
    #: quantity converted to product base units — what hits the ledger.
    base_quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    unit_cost: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    ai_reason: Mapped[str | None] = mapped_column(Text)

    load: Mapped["VanLoad"] = relationship(back_populates="items")
    product: Mapped["Product"] = relationship(lazy="joined")  # noqa: F821
