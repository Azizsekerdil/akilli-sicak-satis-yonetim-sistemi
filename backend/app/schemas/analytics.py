"""
Response and request schemas for the analytics module.

Numbers that represent money or quantities stay ``Decimal`` all the way to the
JSON encoder; statistical outputs (coefficients, indices, probabilities) are
plain floats, because rounding a correlation to four decimal places of Decimal
precision would be false accuracy.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.common import ChartSeries, KpiCard, ORMModel


# ===========================================================================
# Dashboard
# ===========================================================================
class KpiResponse(BaseModel):
    """Dashboard tiles for one reference date."""

    as_of: date
    generated_at: datetime
    cards: list[KpiCard] = Field(default_factory=list)


class ChartResponse(BaseModel):
    """Dashboard charts for one reference date."""

    as_of: date
    generated_at: datetime
    charts: list[ChartSeries] = Field(default_factory=list)


# ===========================================================================
# Sales analysis & performance
# ===========================================================================
class SalesAnalysisRow(BaseModel):
    key: str = ""
    label: str = ""
    bucket_date: date | None = None
    sales_amount: Decimal = Decimal("0")
    net_amount: Decimal = Decimal("0")
    margin_amount: Decimal = Decimal("0")
    margin_percent: float = 0.0
    discount_amount: Decimal = Decimal("0")
    quantity: Decimal = Decimal("0")
    order_count: int = 0
    customer_count: int = 0
    share_percent: float = 0.0


class SalesAnalysisResponse(BaseModel):
    group_by: str
    start: date
    end: date
    total_amount: Decimal = Decimal("0")
    rows: list[SalesAnalysisRow] = Field(default_factory=list)


class PerformanceRow(BaseModel):
    """
    One ranked row of a performance table.

    Shared by the product, customer, salesperson and region views: the common
    columns are always present and the view-specific ones default to ``None``,
    which keeps the frontend on a single table component.
    """

    rank: int = 0
    key: str = ""
    code: str | None = None
    label: str = ""
    group_label: str | None = None
    secondary_label: str | None = None

    sales_amount: Decimal = Decimal("0")
    net_amount: Decimal = Decimal("0")
    margin_amount: Decimal = Decimal("0")
    margin_percent: float = 0.0
    quantity: Decimal | None = None
    order_count: int = 0
    customer_count: int = 0
    share_percent: float = 0.0

    # --- View-specific ----------------------------------------------------
    average_order_value: Decimal | None = None
    last_activity: date | None = None
    balance: Decimal | None = None
    overdue_balance: Decimal | None = None
    collected_amount: Decimal | None = None
    visit_count: int | None = None
    visited_customers: int | None = None
    salesperson_count: int | None = None
    target_value: Decimal | None = None
    achievement_percent: float | None = None


class AbcRow(BaseModel):
    rank: int
    product_id: int
    sku: str
    name: str
    value: Decimal = Decimal("0")
    sales_amount: Decimal = Decimal("0")
    quantity: Decimal = Decimal("0")
    margin_amount: Decimal = Decimal("0")
    order_count: int = 0
    share_percent: float = 0.0
    cumulative_percent: float = 0.0
    abc_class: str = "C"


class BasketPair(BaseModel):
    """A pair of products bought together, with association strength."""

    product_a_id: int
    product_a_name: str
    product_b_id: int
    product_b_name: str
    pair_count: int = 0
    basket_count: int = 0
    support: float = 0.0
    confidence_a_to_b: float = 0.0
    confidence_b_to_a: float = 0.0
    #: > 1 means the pairing is stronger than the two products' popularity alone
    #: would predict — the only figure worth acting on.
    lift: float = 0.0


# ===========================================================================
# Forecasting
# ===========================================================================
class ForecastRequest(BaseModel):
    product_id: int | None = None
    customer_id: int | None = None
    salesperson_id: int | None = None
    horizon_days: int = Field(default=14, ge=1, le=365)
    granularity: str = Field(default="DAILY", description="DAILY|WEEKLY|MONTHLY|QUARTERLY|YEARLY")
    persist: bool = True


class ForecastPoint(BaseModel):
    #: Not named ``date`` — see the note in ``schemas.common.SeriesPoint``.
    bucket_date: date
    label: str = ""
    value: Decimal = Decimal("0")
    lower: Decimal = Decimal("0")
    upper: Decimal = Decimal("0")
    amount: Decimal = Decimal("0")


class ForecastCandidate(BaseModel):
    """Back-test score of one method the ensemble considered."""

    method: str
    mae: float = 0.0
    mape: float = 0.0
    rmse: float = 0.0
    bias: float = 0.0


class ForecastOut(BaseModel):
    run_id: str
    subject_type: str
    subject_id: int = 0
    product_id: int | None = None
    customer_id: int | None = None
    salesperson_id: int | None = None
    granularity: str = "DAILY"
    horizon_days: int = 0
    method: str
    confidence: float = 0.0
    mae: float = 0.0
    history_points: int = 0
    total_forecast_quantity: Decimal = Decimal("0")
    total_forecast_amount: Decimal = Decimal("0")
    points: list[ForecastPoint] = Field(default_factory=list)
    candidates: list[ForecastCandidate] = Field(default_factory=list)
    explanation_tr: str = ""
    explanation_en: str = ""
    generated_at: datetime


class VanLoadSuggestion(BaseModel):
    product_id: int
    sku: str
    name: str
    suggested_cases: int = 0
    base_quantity: Decimal = Decimal("0")
    uom: str = "CASE"
    volume_l: float = 0.0
    weight_kg: float = 0.0
    confidence: float = 0.0
    on_van_quantity: Decimal = Decimal("0")
    depot_available: Decimal = Decimal("0")
    reason_tr: str = ""
    reason_en: str = ""


class OrderSuggestion(BaseModel):
    product_id: int
    sku: str
    product: str
    uom: str = "CASE"
    avg_quantity: Decimal = Decimal("0")
    avg_cases: float = 0.0
    purchase_count: int = 0
    days_since_last: int = 0
    days_of_cover: float = 0.0
    last_purchase_date: date | None = None
    #: Modelled likelihood that the customer has already run out.
    depletion_probability: float = 0.0
    suggested_quantity: Decimal = Decimal("0")
    suggested_cases: int = 0
    reason_tr: str = ""
    reason_en: str = ""


# ===========================================================================
# Anomalies
# ===========================================================================
class AnomalyOut(ORMModel):
    id: int
    anomaly_type: str
    severity: str
    subject_type: str
    subject_id: int | None = None
    subject_label: str | None = None
    detected_on: date
    observed_value: float = 0.0
    expected_value: float = 0.0
    deviation: float = 0.0
    z_score: float | None = None
    method: str | None = None
    title: str
    description: str | None = None
    is_resolved: bool = False
    resolved_at: datetime | None = None
    resolved_by_id: int | None = None
    resolution_note: str | None = None
    created_at: datetime | None = None


class AnomalyDetectRequest(BaseModel):
    start: date | None = None
    end: date | None = None


class AnomalyResolveRequest(BaseModel):
    note: str | None = Field(default=None, max_length=2000)


# ===========================================================================
# Targets
# ===========================================================================
class TargetCreate(BaseModel):
    subject_type: str = "SALESPERSON"
    subject_id: int = 0
    metric: str = "REVENUE"
    period: str = "MONTHLY"
    period_start: date
    period_end: date
    target_value: Decimal = Field(default=Decimal("0"), ge=0)
    currency: str | None = None
    notes: str | None = None


class TargetUpdate(BaseModel):
    target_value: Decimal | None = Field(default=None, ge=0)
    period_end: date | None = None
    notes: str | None = None


class TargetOut(ORMModel):
    id: int
    subject_type: str
    subject_id: int
    metric: str
    period: str
    period_start: date
    period_end: date
    target_value: Decimal = Decimal("0")
    actual_value: Decimal = Decimal("0")
    projected_value: Decimal | None = None
    currency: str = "TRY"
    #: 0-100; combines the projected shortfall with how much time is left.
    risk_score: float = 0.0
    achievement_percent: float = 0.0
    last_calculated_at: datetime | None = None
    notes: str | None = None


class TargetRefreshResult(BaseModel):
    refreshed: int = 0
    period_start: date
    period_end: date
    targets: list[TargetOut] = Field(default_factory=list)


# ===========================================================================
# Statistics
# ===========================================================================
class DescriptiveStats(BaseModel):
    count: int = 0
    sum: float = 0.0
    mean: float = 0.0
    median: float = 0.0
    mode: float = 0.0
    std: float = 0.0
    variance: float = 0.0
    min: float = 0.0
    max: float = 0.0
    q1: float = 0.0
    q3: float = 0.0
    iqr: float = 0.0
    p90: float = 0.0
    p95: float = 0.0
    #: Coefficient of variation as a percentage — the volatility of the series.
    cv: float = 0.0


class HistogramBin(BaseModel):
    lower: float
    upper: float
    count: int
    label: str
    share_percent: float = 0.0


class HistogramOut(BaseModel):
    bins: list[HistogramBin] = Field(default_factory=list)
    total: int = 0
    bin_width: float = 0.0


class TrendOut(BaseModel):
    slope: float = 0.0
    intercept: float = 0.0
    r_squared: float = 0.0
    direction: str = "flat"
    n: int = 0


class RegressionOut(BaseModel):
    slope: float = 0.0
    intercept: float = 0.0
    r_squared: float = 0.0
    #: Normal approximation to the t-test — an indication, not a published
    #: p-value (no scipy/statsmodels dependency in this project).
    p_hint: float = 1.0
    std_error: float = 0.0
    n: int = 0
    is_significant: bool = False


class CorrelationPair(BaseModel):
    subject_a: str
    subject_b: str
    coefficient: float = 0.0
    strength: str = ""
    strength_tr: str = ""
    direction: str = "positive"
    interpretation_tr: str = ""
    interpretation_en: str = ""


class CorrelationOut(BaseModel):
    start: date
    end: date
    method: str = "pearson"
    matrix: dict[str, dict[str, float]] = Field(default_factory=dict)
    top: list[CorrelationPair] = Field(default_factory=list)


class WeekdayIndex(BaseModel):
    weekday: str
    #: 1.0 = an average day; 1.30 = that weekday runs 30% above trend.
    index: float = 1.0


class GrowthOut(BaseModel):
    day_over_day: float = 0.0
    week_over_week: float = 0.0
    period_over_period: float = 0.0


class StatisticsOut(BaseModel):
    start: date
    end: date
    days: int = 0
    descriptive: dict[str, DescriptiveStats] = Field(default_factory=dict)
    histogram: HistogramOut
    trend: TrendOut
    regression: RegressionOut
    growth: GrowthOut
    weekday_profile: list[WeekdayIndex] = Field(default_factory=list)
    decomposition: dict[str, list[float | None]] = Field(default_factory=dict)
    correlation_matrix: dict[str, dict[str, float]] = Field(default_factory=dict)
    top_correlations: list[CorrelationPair] = Field(default_factory=list)
    series: dict[str, list[Any]] = Field(default_factory=dict)
