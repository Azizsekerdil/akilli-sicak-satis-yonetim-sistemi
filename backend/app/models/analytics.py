"""Targets, forecasts, anomalies and pre-aggregated KPI snapshots."""

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
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import (
    AnomalySeverity,
    AnomalyType,
    ForecastMethod,
    TargetMetric,
    TargetPeriod,
    TargetSubject,
)
from app.models.base import (
    AuthorMixin,
    Base,
    Money,
    Quantity,
    TimestampMixin,
    UTCDateTime,
    fk,
    pk,
)


class Target(Base, TimestampMixin, AuthorMixin):
    """A sales/collection target for a subject over a period."""

    __tablename__ = "targets"
    __table_args__ = (
        UniqueConstraint(
            "subject_type", "subject_id", "metric", "period", "period_start",
            name="uq_targets_subject_metric_period",
        ),
        Index("ix_targets_period_range", "period_start", "period_end"),
    )

    id: Mapped[int] = pk()
    subject_type: Mapped[str] = mapped_column(
        String(24), default=TargetSubject.SALESPERSON, nullable=False, index=True
    )
    subject_id: Mapped[int] = mapped_column(Integer, default=0, nullable=False, index=True)
    metric: Mapped[str] = mapped_column(String(24), default=TargetMetric.REVENUE, nullable=False)
    period: Mapped[str] = mapped_column(String(16), default=TargetPeriod.MONTHLY, nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)

    target_value: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    actual_value: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="TRY", nullable=False)
    #: Written by the forecast agent — projected end-of-period value.
    projected_value: Mapped[Decimal | None] = mapped_column(Money)
    risk_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    last_calculated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    notes: Mapped[str | None] = mapped_column(Text)

    @property
    def achievement_percent(self) -> float:
        if not self.target_value:
            return 0.0
        return round(float(self.actual_value / self.target_value * 100), 2)


class Forecast(Base, TimestampMixin):
    """
    A generated demand/sales forecast for one (subject, product, period) cell.

    Storing the method and the interval makes every prediction explainable and
    back-testable rather than a black box.
    """

    __tablename__ = "forecasts"
    __table_args__ = (
        Index("ix_forecasts_subject_prod_date", "subject_type", "subject_id", "product_id", "target_date"),
    )

    id: Mapped[int] = pk()
    run_id: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    subject_type: Mapped[str] = mapped_column(String(24), default="COMPANY", nullable=False)
    subject_id: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    product_id: Mapped[int | None] = fk("products.id", nullable=True, ondelete="CASCADE")
    customer_id: Mapped[int | None] = mapped_column(Integer, index=True)

    target_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    horizon_days: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    granularity: Mapped[str] = mapped_column(String(16), default="DAILY", nullable=False)

    method: Mapped[str] = mapped_column(
        String(24), default=ForecastMethod.ENSEMBLE, nullable=False, index=True
    )
    predicted_quantity: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("0"), nullable=False)
    lower_bound: Mapped[Decimal | None] = mapped_column(Quantity)
    upper_bound: Mapped[Decimal | None] = mapped_column(Quantity)
    predicted_amount: Mapped[Decimal | None] = mapped_column(Money)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    #: Filled in after the fact so accuracy can be measured.
    actual_quantity: Mapped[Decimal | None] = mapped_column(Quantity)
    absolute_error: Mapped[Decimal | None] = mapped_column(Quantity)
    explanation: Mapped[str | None] = mapped_column(Text)
    generated_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)


class Anomaly(Base, TimestampMixin):
    """A detected outlier, with the statistics that justified flagging it."""

    __tablename__ = "anomalies"
    __table_args__ = (
        Index("ix_anomalies_type_date", "anomaly_type", "detected_on"),
        Index("ix_anomalies_subject", "subject_type", "subject_id"),
        Index("ix_anomalies_open", "is_resolved", "severity"),
    )

    id: Mapped[int] = pk()
    anomaly_type: Mapped[str] = mapped_column(
        String(32), default=AnomalyType.SALES_DROP, nullable=False, index=True
    )
    severity: Mapped[str] = mapped_column(
        String(16), default=AnomalySeverity.MEDIUM, nullable=False, index=True
    )
    subject_type: Mapped[str] = mapped_column(String(24), default="CUSTOMER", nullable=False)
    subject_id: Mapped[int | None] = mapped_column(Integer, index=True)
    subject_label: Mapped[str | None] = mapped_column(String(255))

    detected_on: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    observed_value: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    expected_value: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    deviation: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    z_score: Mapped[float | None] = mapped_column(Float)
    method: Mapped[str | None] = mapped_column(String(32))

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    is_resolved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    resolved_by_id: Mapped[int | None] = mapped_column(Integer)
    resolution_note: Mapped[str | None] = mapped_column(Text)


class KpiSnapshot(Base):
    """
    Daily pre-aggregated metrics.

    Lets the dashboard answer "last 12 months by region" without scanning
    millions of sale lines.  Rebuildable at any time from the raw tables.
    """

    __tablename__ = "kpi_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_date", "subject_type", "subject_id", name="uq_kpi_snapshots_date_subject"
        ),
        Index("ix_kpi_snapshots_subject_date", "subject_type", "subject_id", "snapshot_date"),
    )

    id: Mapped[int] = pk()
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    subject_type: Mapped[str] = mapped_column(String(24), default="COMPANY", nullable=False)
    subject_id: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    sales_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    sales_quantity: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("0"), nullable=False)
    sales_cost: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    margin_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    order_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    line_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    collected_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    return_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    discount_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)

    active_customers: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    visited_customers: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    productive_visits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    planned_visits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    new_customers: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    distance_km: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    computed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
