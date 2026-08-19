"""Routes, route stops, customer visits and GPS breadcrumbs."""

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

from app.core.enums import RouteStatus, StopStatus, VisitOutcome
from app.models.base import (
    AuthorMixin,
    Base,
    Money,
    SoftDeleteMixin,
    TimestampMixin,
    UTCDateTime,
    fk,
    pk,
)


class Route(Base, TimestampMixin, SoftDeleteMixin, AuthorMixin):
    """
    A planned day of work: an ordered list of customer stops for one
    salesperson/vehicle.

    ``is_template`` routes are the recurring master routes; dated instances are
    generated from them and are what the field app executes.
    """

    __tablename__ = "routes"
    __table_args__ = (
        Index("ix_routes_date_status", "route_date", "status"),
        Index("ix_routes_salesperson_date", "salesperson_id", "route_date"),
    )

    id: Mapped[int] = pk()
    code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    is_template: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    template_id: Mapped[int | None] = fk("routes.id", nullable=True, ondelete="SET NULL")
    route_date: Mapped[date | None] = mapped_column(Date, index=True)
    weekday: Mapped[str | None] = mapped_column(String(8), index=True)

    salesperson_id: Mapped[int | None] = fk("salespersons.id", nullable=True, ondelete="SET NULL")
    vehicle_id: Mapped[int | None] = fk("vehicles.id", nullable=True, ondelete="SET NULL")
    region_id: Mapped[int | None] = fk("regions.id", nullable=True, ondelete="SET NULL")
    start_warehouse_id: Mapped[int | None] = fk("warehouses.id", nullable=True, ondelete="SET NULL")
    end_warehouse_id: Mapped[int | None] = fk("warehouses.id", nullable=True, ondelete="SET NULL")

    status: Mapped[str] = mapped_column(
        String(16), default=RouteStatus.PLANNED, nullable=False, index=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # --- Plan --------------------------------------------------------------
    planned_stops: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    planned_distance_km: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    planned_duration_min: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    planned_start_time: Mapped[str | None] = mapped_column(String(8))
    planned_volume_l: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    planned_weight_kg: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    # --- Actual ------------------------------------------------------------
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    actual_stops: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completed_stops: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_stops: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    actual_distance_km: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    actual_duration_min: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_sales_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)

    # --- Optimisation ------------------------------------------------------
    is_optimized: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    optimizer: Mapped[str | None] = mapped_column(String(32))       # ORTOOLS | SAVINGS | MANUAL
    optimized_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    optimization_seconds: Mapped[float | None] = mapped_column(Float)
    optimization_note: Mapped[str | None] = mapped_column(Text)

    stops: Mapped[list["RouteStop"]] = relationship(
        back_populates="route",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="RouteStop.sequence",
    )

    @property
    def completion_rate(self) -> float:
        if not self.planned_stops:
            return 0.0
        return round(self.completed_stops / self.planned_stops * 100, 2)


class RouteStop(Base, TimestampMixin):
    """One customer stop on a route, with planned vs actual timing."""

    __tablename__ = "route_stops"
    __table_args__ = (
        UniqueConstraint("route_id", "customer_id", name="uq_route_stops_route_customer"),
        Index("ix_route_stops_route_seq", "route_id", "sequence"),
    )

    id: Mapped[int] = pk()
    route_id: Mapped[int] = fk("routes.id", ondelete="CASCADE")
    customer_id: Mapped[int] = fk("customers.id", ondelete="CASCADE")
    sequence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    status: Mapped[str] = mapped_column(
        String(16), default=StopStatus.PENDING, nullable=False, index=True
    )
    planned_arrival: Mapped[str | None] = mapped_column(String(8))
    planned_departure: Mapped[str | None] = mapped_column(String(8))
    service_time_minutes: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    distance_from_previous_km: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    travel_time_from_previous_min: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    arrived_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    departed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    arrival_lat: Mapped[float | None] = mapped_column(Float)
    arrival_lng: Mapped[float | None] = mapped_column(Float)
    #: Metres between the recorded arrival point and the customer's coordinates.
    geofence_distance_m: Mapped[float | None] = mapped_column(Float)
    delay_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skip_reason: Mapped[str | None] = mapped_column(String(255))
    is_priority: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    route: Mapped["Route"] = relationship(back_populates="stops")
    customer: Mapped["Customer"] = relationship(lazy="joined")  # noqa: F821


class Visit(Base, TimestampMixin, AuthorMixin):
    """
    A recorded customer visit — the field activity log.

    Created whether or not a sale happened, so visit-coverage KPIs
    (visited / not-visited) are exact.
    """

    __tablename__ = "visits"
    __table_args__ = (
        Index("ix_visits_customer_date", "customer_id", "visit_date"),
        Index("ix_visits_salesperson_date", "salesperson_id", "visit_date"),
    )

    id: Mapped[int] = pk()
    visit_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    customer_id: Mapped[int] = fk("customers.id", ondelete="CASCADE")
    salesperson_id: Mapped[int | None] = fk("salespersons.id", nullable=True, ondelete="SET NULL")
    vehicle_id: Mapped[int | None] = fk("vehicles.id", nullable=True, ondelete="SET NULL")
    route_id: Mapped[int | None] = fk("routes.id", nullable=True, ondelete="SET NULL")
    route_stop_id: Mapped[int | None] = fk("route_stops.id", nullable=True, ondelete="SET NULL")
    day_session_id: Mapped[int | None] = fk("day_sessions.id", nullable=True, ondelete="SET NULL")

    outcome: Mapped[str] = mapped_column(
        String(24), default=VisitOutcome.NO_ORDER, nullable=False, index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    duration_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    is_in_geofence: Mapped[bool | None] = mapped_column(Boolean)
    is_unplanned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    sale_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    collected_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    return_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    lines_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    photo_path: Mapped[str | None] = mapped_column(String(512))
    signature_path: Mapped[str | None] = mapped_column(String(512))
    notes: Mapped[str | None] = mapped_column(Text)

    customer: Mapped["Customer"] = relationship(lazy="joined")  # noqa: F821


class GpsEvent(Base):
    """
    Vehicle position breadcrumb.

    High-volume table: indexed by (vehicle, recorded_at) and pruned by a
    retention job.
    """

    __tablename__ = "gps_events"
    __table_args__ = (
        Index("ix_gps_events_vehicle_time", "vehicle_id", "recorded_at"),
        Index("ix_gps_events_session", "day_session_id"),
    )

    id: Mapped[int] = pk()
    vehicle_id: Mapped[int | None] = mapped_column(Integer, index=True)
    salesperson_id: Mapped[int | None] = mapped_column(Integer, index=True)
    day_session_id: Mapped[int | None] = mapped_column(Integer)
    route_id: Mapped[int | None] = mapped_column(Integer, index=True)

    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    accuracy_m: Mapped[float | None] = mapped_column(Float)
    speed_kmh: Mapped[float | None] = mapped_column(Float)
    heading: Mapped[float | None] = mapped_column(Float)
    altitude_m: Mapped[float | None] = mapped_column(Float)
    battery_percent: Mapped[int | None] = mapped_column(Integer)
    event_type: Mapped[str | None] = mapped_column(String(24))  # PING | ARRIVE | DEPART | STOP

    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, index=True)
