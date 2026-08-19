"""
Analytics service: dashboard, performance analysis, forecasting, suggestions,
anomaly detection and targets.

This module is the bridge between the raw transactional tables and the pure
statistics in :mod:`app.analytics`.  Its job is to turn "sales" into *series*
— correctly scoped to what the caller may see, correctly bucketed, and with
missing periods filled with zeros — and then to turn the numbers that come back
into something a sales manager can act on.

Two rules run through the whole file:

* **Scope first.**  Every aggregate honours the caller's data scope.  A
  salesperson's dashboard must never total the company's revenue.
* **Explain everything.**  A suggested van load or a flagged anomaly always
  carries the reasoning that produced it, in Turkish and English.  A number
  nobody trusts is a number nobody uses.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import combinations
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.analytics import anomaly as anomaly_lib
from app.analytics import correlation as correlation_lib
from app.analytics import descriptive, forecasting, regression
from app.analytics import timeseries as ts
from app.core.config import settings
from app.core.enums import (
    AnomalyType,
    AuditAction,
    CampaignScope,
    CampaignStatus,
    CustomerStatus,
    InvoiceStatus,
    OrderStatus,
    PaymentStatus,
    ProductStatus,
    ReturnDisposition,
    ReturnReason,
    RouteStatus,
    TargetMetric,
    TargetPeriod,
    TargetSubject,
    VehicleStatus,
    WarehouseType,
)
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging_config import get_logger
from app.core.utils import (
    D,
    add_months,
    clamp,
    date_range,
    money,
    month_end,
    month_start,
    pct,
    qty,
    safe_div,
    weekday_code,
)
from app.models.analytics import Anomaly, Forecast, Target
from app.models.campaign import Campaign
from app.models.customer import Customer
from app.models.organization import Region
from app.models.product import Brand, Product, ProductCategory
from app.models.route import Route, RouteStop, Visit
from app.models.sales import Invoice, Order, Payment, ReturnDocument, Sale, SaleItem
from app.models.vehicle import DaySession, Salesperson, Vehicle
from app.models.warehouse import Lot, StockBalance, Warehouse
from app.schemas.common import ChartSeries, KpiCard, SeriesPoint

log = get_logger("app.analytics")

Scope = Mapping[str, Any] | None

#: How far back each granularity looks when building a forecast history.
_LOOKBACK_DAYS: dict[str, int] = {
    "DAILY": 210,
    "WEEKLY": 400,
    "MONTHLY": 760,
    "QUARTERLY": 1_100,
    "YEARLY": 1_830,
}

#: Approximate days per bucket — converts a day horizon into bucket counts.
_BUCKET_DAYS: dict[str, int] = {
    "DAILY": 1, "WEEKLY": 7, "MONTHLY": 30, "QUARTERLY": 91, "YEARLY": 365,
}

#: Van-load safety buffer.  15% covers normal day-to-day demand noise without
#: turning the van into a warehouse; erratic SKUs get extra on top (see below).
_VAN_SAFETY_FACTOR = 1.15

#: Most sales an at-a-time basket analysis will scan — keeps the O(n²) pair
#: expansion bounded on large databases.
_BASKET_SALE_LIMIT = 5_000


# ===========================================================================
# Scope & small helpers
# ===========================================================================
def _sp_ids(ctx_scope: Scope) -> list[int] | None:
    """
    Salesperson ids the caller may see, or ``None`` when unrestricted.

    A restricted caller with no salesperson profile gets ``[-1]`` rather than
    ``None``: an empty restriction must deny everything, never silently widen
    into "see the whole company".
    """
    if not ctx_scope:
        return None
    if ctx_scope.get("unrestricted"):
        return None
    ids = [int(i) for i in (ctx_scope.get("salesperson_ids") or [])]
    return ids or [-1]


def _day(on: date | None) -> date:
    return on or date.today()


def _range(start: date | None, end: date | None, *, default_days: int = 30) -> tuple[date, date]:
    """Normalise a report window, defaulting to the last *default_days* days."""
    today = date.today()
    finish = end or today
    begin = start or (finish - timedelta(days=default_days - 1))
    if begin > finish:
        begin, finish = finish, begin
    return begin, finish


def resolve_window(start: date | None, end: date | None, *, default_days: int = 30) -> tuple[date, date]:
    """Public form of the window normaliser, so the API can echo the dates it used."""
    return _range(start, end, default_days=default_days)


def target_window(period_start: date | None, period_end: date | None) -> tuple[date, date]:
    """Window :func:`refresh_targets` will operate on — defaults to this month."""
    today = date.today()
    return period_start or month_start(today), period_end or month_end(today)


def _sale_filters(start: date | None, end: date | None, sp_ids: list[int] | None) -> list[Any]:
    conds: list[Any] = [Sale.is_cancelled.is_(False), Sale.is_deleted.is_(False)]
    if start is not None:
        conds.append(Sale.sale_date >= start)
    if end is not None:
        conds.append(Sale.sale_date <= end)
    if sp_ids is not None:
        conds.append(Sale.salesperson_id.in_(sp_ids))
    return conds


def _scalar(db: Session, stmt: Any) -> Decimal:
    """Run a single-value aggregate and return it as money (never ``None``)."""
    return money(db.execute(stmt).scalar() or 0)


def _count(db: Session, stmt: Any) -> int:
    return int(db.execute(stmt).scalar() or 0)


def _sales_total(db: Session, start: date, end: date, sp_ids: list[int] | None, column: Any = None) -> Decimal:
    col = column if column is not None else Sale.total_amount
    return _scalar(
        db,
        select(func.coalesce(func.sum(col), 0)).where(*_sale_filters(start, end, sp_ids)),
    )


def _window_total(
    db: Session, start: date, end: date, sp_ids: list[int] | None, *, item_based: bool = False
) -> Decimal:
    """
    Whole-window revenue, used as the denominator for ``share_percent``.

    Deliberately *not* the sum of the returned rows: a top-10 list whose shares
    add up to 100% tells the reader nothing about how much of the business those
    ten actually represent, which is the entire question being asked.
    """
    if not item_based:
        return _sales_total(db, start, end, sp_ids)
    return _scalar(
        db,
        select(func.coalesce(func.sum(SaleItem.total_amount), 0))
        .select_from(SaleItem)
        .join(Sale, Sale.id == SaleItem.sale_id)
        .where(*_sale_filters(start, end, sp_ids)),
    )


def _collections_total(db: Session, start: date, end: date, sp_ids: list[int] | None) -> Decimal:
    conds: list[Any] = [
        Payment.payment_date >= start,
        Payment.payment_date <= end,
        Payment.is_deleted.is_(False),
        Payment.status != PaymentStatus.CANCELLED,
    ]
    if sp_ids is not None:
        conds.append(Payment.salesperson_id.in_(sp_ids))
    return _scalar(db, select(func.coalesce(func.sum(Payment.amount), 0)).where(*conds))


def _card(
    key: str,
    label_tr: str,
    label_en: str,
    value: Any,
    *,
    previous: Any = None,
    fmt: str = "money",
    unit: str | None = None,
    severity: str | None = None,
    icon: str | None = None,
    higher_is_better: bool = True,
) -> KpiCard:
    """
    Build one dashboard tile, deriving the change and trend from *previous*.

    ``trend`` is the direction of the number; ``severity`` is the business
    judgement about it.  They differ on purpose: overdue receivables going *up*
    is trend "up" and severity "critical".
    """
    change = ts.period_over_period(value, previous) if previous is not None else None
    trend: str | None = None
    if change is not None:
        trend = "up" if change > 0.5 else ("down" if change < -0.5 else "flat")
    if severity is None and change is not None:
        improving = change >= 0 if higher_is_better else change <= 0
        severity = "ok" if improving else ("warning" if abs(change) < 20 else "critical")
    return KpiCard(
        key=key,
        label_tr=label_tr,
        label_en=label_en,
        value=value,
        previous_value=previous,
        change_percent=change,
        unit=unit,
        format=fmt,
        trend=trend,
        severity=severity,
        icon=icon,
    )


def _available_stock(db: Session, warehouse_id: int, product_id: int) -> Decimal:
    """
    On-hand available quantity in base units.

    Delegates to :mod:`app.services.stock_service` when it is present so the
    allocation rules stay in one place; the direct balance query is the
    fallback.  The import is deferred rather than top-level because analytics
    must not fail to load if the stock module is unavailable.
    """
    try:
        from app.services import stock_service

        return qty(stock_service.get_available(db, warehouse_id, product_id))
    except Exception:  # pragma: no cover - fallback path
        total = db.execute(
            select(
                func.coalesce(
                    func.sum(StockBalance.quantity - StockBalance.reserved_quantity), 0
                )
            ).where(
                StockBalance.warehouse_id == warehouse_id,
                StockBalance.product_id == product_id,
            )
        ).scalar()
        return qty(total or 0)


def _case_factor(product: Product) -> Decimal:
    """Base units per selling case — never zero, so callers can divide safely."""
    factor = D(product.units_per_case)
    return factor if factor > 0 else Decimal("1")


def _unit_volume_weight(product: Product) -> tuple[float, float]:
    """Volume (l) and weight (kg) of one *base* unit, best effort."""
    factor = float(_case_factor(product))
    volume = product.unit_volume_l
    if volume is None and product.case_volume_l:
        volume = product.case_volume_l / factor
    weight = product.unit_weight_kg
    if weight is None and product.case_weight_kg:
        weight = product.case_weight_kg / factor
    return float(volume or 0.0), float(weight or 0.0)


# ===========================================================================
# Dashboard
# ===========================================================================
def dashboard(db: Session, *, on_date: date | None = None, ctx_scope: Scope = None) -> list[KpiCard]:
    """
    Every headline metric for the main dashboard, in one pass.

    Comparisons are like-for-like: today against yesterday, month-to-date
    against the *same number of days* of the previous month.  Comparing a
    3-day-old month against a full previous month is the classic way to make a
    healthy month look catastrophic, so it is done properly here.
    """
    day = _day(on_date)
    sp_ids = _sp_ids(ctx_scope)
    yesterday = day - timedelta(days=1)
    m_start = month_start(day)
    elapsed_days = (day - m_start).days
    prev_m_start = add_months(m_start, -1)
    prev_m_same_point = min(prev_m_start + timedelta(days=elapsed_days), month_end(prev_m_start))

    cards: list[KpiCard] = []

    # --- Sales ------------------------------------------------------------
    daily = _sales_total(db, day, day, sp_ids)
    daily_prev = _sales_total(db, yesterday, yesterday, sp_ids)
    cards.append(_card("daily_sales", "Günlük Satış", "Daily Sales", daily, previous=daily_prev, icon="cash"))

    mtd = _sales_total(db, m_start, day, sp_ids)
    mtd_prev = _sales_total(db, prev_m_start, prev_m_same_point, sp_ids)
    cards.append(_card("monthly_sales", "Aylık Satış (MTD)", "Monthly Sales (MTD)", mtd, previous=mtd_prev, icon="chart"))

    # --- Target vs actual --------------------------------------------------
    target_value, achievement = _monthly_target_state(db, day, sp_ids, actual=mtd)
    cards.append(
        _card(
            "target_vs_actual", "Hedef Gerçekleşme", "Target Achievement", achievement,
            fmt="percent", unit="%",
            severity="ok" if achievement >= 90 else ("warning" if achievement >= 70 else "critical"),
            icon="target",
        )
    )
    cards.append(_card("monthly_target", "Aylık Hedef", "Monthly Target", target_value, icon="flag"))

    # --- Margin ------------------------------------------------------------
    margin = _sales_total(db, m_start, day, sp_ids, Sale.margin_amount)
    net = _sales_total(db, m_start, day, sp_ids, Sale.net_amount)
    margin_prev = _sales_total(db, prev_m_start, prev_m_same_point, sp_ids, Sale.margin_amount)
    cards.append(_card("gross_margin", "Brüt Kâr (MTD)", "Gross Margin (MTD)", margin, previous=margin_prev, icon="trending"))
    cards.append(
        _card(
            "gross_margin_percent", "Kâr Marjı", "Margin Percent", pct(margin, net),
            fmt="percent", unit="%", icon="percent",
        )
    )

    # --- Cash --------------------------------------------------------------
    collected_today = _collections_total(db, day, day, sp_ids)
    collected_prev = _collections_total(db, yesterday, yesterday, sp_ids)
    cards.append(_card("collections", "Günlük Tahsilat", "Daily Collections", collected_today, previous=collected_prev, icon="wallet"))
    cards.append(
        _card("collections_mtd", "Aylık Tahsilat", "Collections (MTD)", _collections_total(db, m_start, day, sp_ids), icon="wallet")
    )

    open_receivable, overdue_receivable = _receivables(db, day, sp_ids)
    cards.append(_card("open_receivables", "Açık Alacak", "Open Receivables", open_receivable, higher_is_better=False, icon="receipt"))
    cards.append(
        _card(
            "overdue_receivables", "Vadesi Geçmiş Alacak", "Overdue Receivables", overdue_receivable,
            higher_is_better=False, icon="alert",
            severity="ok" if overdue_receivable <= 0 else (
                "warning" if open_receivable and overdue_receivable / open_receivable < Decimal("0.2") else "critical"
            ),
        )
    )

    # --- Activity ----------------------------------------------------------
    order_conds: list[Any] = [
        Order.order_date == day, Order.is_deleted.is_(False), Order.status != OrderStatus.CANCELLED,
    ]
    if sp_ids is not None:
        order_conds.append(Order.salesperson_id.in_(sp_ids))
    cards.append(
        _card("order_count", "Sipariş Adedi", "Order Count",
              _count(db, select(func.count(Order.id)).where(*order_conds)), fmt="integer", icon="list")
    )

    cards.append(
        _card("active_customers", "Aktif Müşteri", "Active Customers",
              _active_customer_count(db, sp_ids), fmt="integer", icon="users")
    )

    visited, planned = _visit_coverage(db, day, sp_ids)
    cards.append(_card("visited_customers", "Ziyaret Edilen", "Visited Customers", visited, fmt="integer", icon="check"))
    cards.append(
        _card(
            "not_visited_customers", "Ziyaret Edilmeyen", "Not Visited", max(0, planned - visited),
            fmt="integer", higher_is_better=False, icon="clock",
            severity="ok" if planned and visited >= planned else ("warning" if visited else "critical"),
        )
    )

    vehicle_conds: list[Any] = [
        Vehicle.is_active.is_(True), Vehicle.is_deleted.is_(False), Vehicle.status == VehicleStatus.ACTIVE,
    ]
    if sp_ids is not None:
        vehicle_conds.append(Vehicle.default_salesperson_id.in_(sp_ids))
    cards.append(
        _card("vehicle_count", "Aktif Araç", "Active Vehicles",
              _count(db, select(func.count(Vehicle.id)).where(*vehicle_conds)), fmt="integer", icon="truck")
    )

    route_conds: list[Any] = [
        Route.route_date == day, Route.is_deleted.is_(False), Route.is_template.is_(False),
        Route.status.in_([RouteStatus.PLANNED, RouteStatus.OPTIMIZED, RouteStatus.ASSIGNED, RouteStatus.IN_PROGRESS]),
    ]
    if sp_ids is not None:
        route_conds.append(Route.salesperson_id.in_(sp_ids))
    cards.append(
        _card("active_routes", "Aktif Rota", "Active Routes",
              _count(db, select(func.count(Route.id)).where(*route_conds)), fmt="integer", icon="map")
    )

    # --- Stock -------------------------------------------------------------
    cards.append(_card("warehouse_stock_value", "Depo Stok Değeri", "Warehouse Stock Value", _stock_value(db, vehicle=False), icon="box"))
    cards.append(_card("van_stock_value", "Araç Stok Değeri", "Van Stock Value", _stock_value(db, vehicle=True, sp_ids=sp_ids), icon="truck"))

    critical = _critical_stock_count(db)
    cards.append(
        _card(
            "critical_stock", "Kritik Stok", "Critical Stock", critical, fmt="integer",
            higher_is_better=False, icon="alert",
            severity="ok" if critical == 0 else ("warning" if critical < 10 else "critical"),
        )
    )

    near_expiry = _near_expiry_count(db, day)
    cards.append(
        _card(
            "near_expiry_products", "SKT Yaklaşan Ürün", "Near-Expiry Products", near_expiry, fmt="integer",
            higher_is_better=False, icon="clock",
            severity="ok" if near_expiry == 0 else ("warning" if near_expiry < 10 else "critical"),
        )
    )

    # --- Returns -----------------------------------------------------------
    returns_mtd = _returns_total(db, m_start, day, sp_ids)
    returns_prev = _returns_total(db, prev_m_start, prev_m_same_point, sp_ids)
    cards.append(
        _card("returns", "İade Tutarı (MTD)", "Returns (MTD)", returns_mtd, previous=returns_prev,
              higher_is_better=False, icon="undo")
    )
    cards.append(
        _card("damaged_goods", "Hasarlı / İmha (MTD)", "Damaged Goods (MTD)",
              _returns_total(db, m_start, day, sp_ids, damaged_only=True), higher_is_better=False, icon="alert")
    )
    return cards


def _monthly_target_state(
    db: Session, day: date, sp_ids: list[int] | None, *, actual: Decimal
) -> tuple[Decimal, float]:
    """Total revenue target covering *day* and how much of it is achieved."""
    conds: list[Any] = [
        Target.metric == TargetMetric.REVENUE,
        Target.period == TargetPeriod.MONTHLY,
        Target.period_start <= day,
        Target.period_end >= day,
    ]
    if sp_ids is not None:
        conds.append(Target.subject_type == TargetSubject.SALESPERSON)
        conds.append(Target.subject_id.in_(sp_ids))
    else:
        conds.append(Target.subject_type == TargetSubject.COMPANY)

    total = _scalar(db, select(func.coalesce(func.sum(Target.target_value), 0)).where(*conds))
    if total <= 0 and sp_ids is None:
        # No company-level target set: fall back to the sum of individual ones
        # so the tile still means something.
        total = _scalar(
            db,
            select(func.coalesce(func.sum(Target.target_value), 0)).where(
                Target.metric == TargetMetric.REVENUE,
                Target.period == TargetPeriod.MONTHLY,
                Target.period_start <= day,
                Target.period_end >= day,
            ),
        )
    return total, pct(actual, total)


def _receivables(db: Session, day: date, sp_ids: list[int] | None) -> tuple[Decimal, Decimal]:
    conds: list[Any] = [
        Invoice.is_deleted.is_(False),
        Invoice.status.notin_([InvoiceStatus.CANCELLED, InvoiceStatus.DRAFT]),
        Invoice.open_amount > 0,
    ]
    if sp_ids is not None:
        conds.append(Invoice.salesperson_id.in_(sp_ids))
    open_amount = _scalar(db, select(func.coalesce(func.sum(Invoice.open_amount), 0)).where(*conds))
    overdue = _scalar(
        db,
        select(func.coalesce(func.sum(Invoice.open_amount), 0)).where(
            *conds, Invoice.due_date.is_not(None), Invoice.due_date < day
        ),
    )
    return open_amount, overdue


def _active_customer_count(db: Session, sp_ids: list[int] | None) -> int:
    conds: list[Any] = [Customer.is_deleted.is_(False), Customer.status == CustomerStatus.ACTIVE]
    if sp_ids is not None:
        conds.append(Customer.default_salesperson_id.in_(sp_ids))
    return _count(db, select(func.count(Customer.id)).where(*conds))


def _visit_coverage(db: Session, day: date, sp_ids: list[int] | None) -> tuple[int, int]:
    """
    (visited, planned) customer counts for *day*.

    Planned comes from the day's route stops; when no route exists (small
    operations run without formal routes) it falls back to the customers whose
    visit plan includes this weekday, so the "not visited" tile is never a
    meaningless zero.
    """
    visit_conds: list[Any] = [Visit.visit_date == day]
    if sp_ids is not None:
        visit_conds.append(Visit.salesperson_id.in_(sp_ids))
    visited = _count(db, select(func.count(func.distinct(Visit.customer_id))).where(*visit_conds))

    route_conds: list[Any] = [
        Route.route_date == day, Route.is_deleted.is_(False), Route.is_template.is_(False),
    ]
    if sp_ids is not None:
        route_conds.append(Route.salesperson_id.in_(sp_ids))
    planned = _count(
        db,
        select(func.count(func.distinct(RouteStop.customer_id)))
        .join(Route, Route.id == RouteStop.route_id)
        .where(*route_conds),
    )

    if planned == 0:
        code = weekday_code(day)
        plan_conds: list[Any] = [
            Customer.is_deleted.is_(False),
            Customer.status == CustomerStatus.ACTIVE,
            Customer.visit_days.is_not(None),
            Customer.visit_days.like(f"%{code}%"),
        ]
        if sp_ids is not None:
            plan_conds.append(Customer.default_salesperson_id.in_(sp_ids))
        planned = _count(db, select(func.count(Customer.id)).where(*plan_conds))
    return visited, planned


def _stock_value(db: Session, *, vehicle: bool, sp_ids: list[int] | None = None) -> Decimal:
    conds: list[Any] = [StockBalance.quantity > 0]
    if vehicle:
        conds.append(Warehouse.warehouse_type == WarehouseType.VEHICLE)
        if sp_ids is not None:
            conds.append(
                Warehouse.id.in_(
                    select(Vehicle.warehouse_id).where(Vehicle.default_salesperson_id.in_(sp_ids))
                )
            )
    else:
        conds.append(Warehouse.warehouse_type != WarehouseType.VEHICLE)
    return _scalar(
        db,
        select(func.coalesce(func.sum(StockBalance.quantity * StockBalance.average_cost), 0))
        .join(Warehouse, Warehouse.id == StockBalance.warehouse_id)
        .where(*conds),
    )


def _critical_stock_count(db: Session) -> int:
    """Products whose depot on-hand has fallen below their minimum level."""
    on_hand = {
        int(pid): D(total)
        for pid, total in db.execute(
            select(StockBalance.product_id, func.coalesce(func.sum(StockBalance.quantity), 0))
            .join(Warehouse, Warehouse.id == StockBalance.warehouse_id)
            .where(Warehouse.warehouse_type != WarehouseType.VEHICLE)
            .group_by(StockBalance.product_id)
        ).all()
    }
    rows = db.execute(
        select(Product.id, Product.min_stock_level).where(
            Product.is_deleted.is_(False),
            Product.status == ProductStatus.ACTIVE,
            Product.min_stock_level > 0,
        )
    ).all()
    return sum(1 for pid, minimum in rows if on_hand.get(int(pid), Decimal("0")) < D(minimum))


def _near_expiry_count(db: Session, day: date) -> int:
    horizon = day + timedelta(days=int(settings.expiry_warning_days))
    return _count(
        db,
        select(func.count(func.distinct(StockBalance.lot_id)))
        .join(Lot, Lot.id == StockBalance.lot_id)
        .where(
            StockBalance.quantity > 0,
            Lot.expiry_date.is_not(None),
            Lot.expiry_date >= day,
            Lot.expiry_date <= horizon,
        ),
    )


def _returns_total(
    db: Session, start: date, end: date, sp_ids: list[int] | None, *, damaged_only: bool = False
) -> Decimal:
    conds: list[Any] = [
        ReturnDocument.return_date >= start,
        ReturnDocument.return_date <= end,
        ReturnDocument.is_deleted.is_(False),
    ]
    if sp_ids is not None:
        conds.append(ReturnDocument.salesperson_id.in_(sp_ids))
    if damaged_only:
        conds.append(
            or_(
                ReturnDocument.disposition == ReturnDisposition.SCRAP,
                ReturnDocument.reason.in_([ReturnReason.DAMAGED, ReturnReason.EXPIRED]),
            )
        )
    return _scalar(db, select(func.coalesce(func.sum(ReturnDocument.total_amount), 0)).where(*conds))


# ===========================================================================
# Dashboard charts
# ===========================================================================
def dashboard_charts(
    db: Session, *, on_date: date | None = None, ctx_scope: Scope = None, days: int = 30
) -> list[ChartSeries]:
    """Every chart the main dashboard renders, already bucketed and gap-filled."""
    day = _day(on_date)
    sp_ids = _sp_ids(ctx_scope)
    window = max(7, int(days))
    start = day - timedelta(days=window - 1)
    charts: list[ChartSeries] = []

    # --- Sales trend -------------------------------------------------------
    rows = db.execute(
        select(Sale.sale_date, func.coalesce(func.sum(Sale.total_amount), 0))
        .where(*_sale_filters(start, day, sp_ids))
        .group_by(Sale.sale_date)
    ).all()
    buckets = ts.resample([(r[0], float(r[1] or 0)) for r in rows], "DAILY", start=start, end=day)
    charts.append(
        ChartSeries(
            key="sales_trend",
            name_tr=f"Satış Trendi ({window} gün)",
            name_en=f"Sales Trend ({window} days)",
            chart_type="area",
            unit=settings.default_currency,
            points=[
                SeriesPoint(label=b.label, value=money(b.value), bucket_date=b.bucket_date) for b in buckets
            ],
        )
    )

    # --- Sales by category -------------------------------------------------
    charts.append(
        _category_chart(db, start, day, sp_ids)
    )

    # --- Top 10 products ---------------------------------------------------
    product_rows = product_performance(db, start=start, end=day, ctx_scope=ctx_scope, limit=10)
    charts.append(
        ChartSeries(
            key="top_products", name_tr="En Çok Satan 10 Ürün", name_en="Top 10 Products",
            chart_type="bar", unit=settings.default_currency,
            points=[SeriesPoint(label=r["label"], value=r["sales_amount"], secondary=r["quantity"]) for r in product_rows],
        )
    )

    # --- Top 10 salespeople ------------------------------------------------
    person_rows = salesperson_performance(db, start=start, end=day, ctx_scope=ctx_scope, limit=10)
    charts.append(
        ChartSeries(
            key="top_salespersons", name_tr="En İyi 10 Plasiyer", name_en="Top 10 Salespeople",
            chart_type="bar", unit=settings.default_currency,
            points=[SeriesPoint(label=r["label"], value=r["sales_amount"], secondary=r["order_count"]) for r in person_rows],
        )
    )

    # --- Top regions -------------------------------------------------------
    region_rows = region_performance(db, start=start, end=day, ctx_scope=ctx_scope, limit=10)
    charts.append(
        ChartSeries(
            key="top_regions", name_tr="Bölge Satışları", name_en="Sales by Region",
            chart_type="bar", unit=settings.default_currency,
            points=[SeriesPoint(label=r["label"], value=r["sales_amount"], secondary=r["customer_count"]) for r in region_rows],
        )
    )

    # --- Collections vs sales ---------------------------------------------
    collection_rows = db.execute(
        select(Payment.payment_date, func.coalesce(func.sum(Payment.amount), 0))
        .where(
            Payment.payment_date >= start,
            Payment.payment_date <= day,
            Payment.is_deleted.is_(False),
            Payment.status != PaymentStatus.CANCELLED,
            *([Payment.salesperson_id.in_(sp_ids)] if sp_ids is not None else []),
        )
        .group_by(Payment.payment_date)
    ).all()
    collected = {
        b.bucket_date: b.value
        for b in ts.resample([(r[0], float(r[1] or 0)) for r in collection_rows], "DAILY", start=start, end=day)
    }
    charts.append(
        ChartSeries(
            key="collection_vs_sales", name_tr="Tahsilat / Satış", name_en="Collections vs Sales",
            chart_type="bar", unit=settings.default_currency,
            points=[
                SeriesPoint(
                    label=b.label, value=money(collected.get(b.bucket_date, 0.0)),
                    secondary=money(b.value), bucket_date=b.bucket_date,
                )
                for b in buckets
            ],
        )
    )

    # --- Monthly comparison (this year vs last) ---------------------------
    charts.append(_monthly_comparison_chart(db, day, sp_ids))
    return charts


def _category_chart(db: Session, start: date, end: date, sp_ids: list[int] | None) -> ChartSeries:
    rows = db.execute(
        select(
            func.coalesce(ProductCategory.name, "—").label("label"),
            func.coalesce(func.sum(SaleItem.total_amount), 0).label("amount"),
        )
        .select_from(SaleItem)
        .join(Sale, Sale.id == SaleItem.sale_id)
        .join(Product, Product.id == SaleItem.product_id)
        .join(ProductCategory, ProductCategory.id == Product.category_id, isouter=True)
        .where(*_sale_filters(start, end, sp_ids))
        .group_by("label")
        .order_by(func.coalesce(func.sum(SaleItem.total_amount), 0).desc())
        .limit(12)
    ).all()
    return ChartSeries(
        key="sales_by_category", name_tr="Kategori Bazlı Satış", name_en="Sales by Category",
        chart_type="donut", unit=settings.default_currency,
        points=[SeriesPoint(label=str(r.label), value=money(r.amount)) for r in rows],
    )


def _monthly_comparison_chart(db: Session, day: date, sp_ids: list[int] | None) -> ChartSeries:
    """Last 12 months against the same months a year earlier."""
    first = month_start(add_months(day, -11))
    rows = db.execute(
        select(Sale.sale_date, func.coalesce(func.sum(Sale.total_amount), 0))
        .where(*_sale_filters(month_start(add_months(day, -23)), day, sp_ids))
        .group_by(Sale.sale_date)
    ).all()
    monthly = {
        b.bucket_date: b.value
        for b in ts.resample([(r[0], float(r[1] or 0)) for r in rows], "MONTHLY")
    }
    points: list[SeriesPoint] = []
    cursor = first
    while cursor <= day:
        previous_year = cursor.replace(year=cursor.year - 1)
        points.append(
            SeriesPoint(
                label=ts.bucket_label(cursor, "MONTHLY"),
                value=money(monthly.get(cursor, 0.0)),
                secondary=money(monthly.get(previous_year, 0.0)),
                bucket_date=cursor,
            )
        )
        cursor = add_months(cursor, 1)
    return ChartSeries(
        key="monthly_comparison", name_tr="Aylık Karşılaştırma (Yıl/Yıl)",
        name_en="Monthly Comparison (YoY)", chart_type="bar",
        unit=settings.default_currency, points=points,
    )


# ===========================================================================
# Sales analysis
# ===========================================================================
GROUP_BY_OPTIONS: tuple[str, ...] = (
    "day", "week", "month", "product", "category", "brand",
    "customer", "salesperson", "region", "route", "channel",
)

_TIME_GROUPS: dict[str, str] = {"day": "DAILY", "week": "WEEKLY", "month": "MONTHLY"}


def sales_analysis(
    db: Session,
    *,
    start: date | None = None,
    end: date | None = None,
    group_by: str = "day",
    ctx_scope: Scope = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """
    Aggregated sales broken down by any supported dimension.

    Time dimensions are grouped in Python (from a per-day SQL aggregate) rather
    than with ``date_trunc``: that function does not exist on SQLite, and the
    per-day result set is small enough that bucketing it in the application is
    both portable and cheap.
    """
    key = (group_by or "day").strip().lower()
    if key not in GROUP_BY_OPTIONS:
        raise ValidationError("analytics.invalid_group_by", params={"group_by": group_by})

    begin, finish = _range(start, end)
    sp_ids = _sp_ids(ctx_scope)

    if key in _TIME_GROUPS:
        rows = _time_grouped_sales(db, begin, finish, sp_ids, _TIME_GROUPS[key])
        total = sum((D(r["sales_amount"]) for r in rows), Decimal("0"))
    else:
        rows = _dimension_grouped_sales(db, begin, finish, sp_ids, key, limit)
        total = _window_total(db, begin, finish, sp_ids, item_based=key in ("product", "category", "brand"))

    for row in rows:
        row["share_percent"] = pct(row["sales_amount"], total)
        row["margin_percent"] = pct(row["margin_amount"], row["net_amount"])
    return rows


def _time_grouped_sales(
    db: Session, start: date, end: date, sp_ids: list[int] | None, granularity: str
) -> list[dict[str, Any]]:
    header_rows = db.execute(
        select(
            Sale.sale_date,
            func.coalesce(func.sum(Sale.total_amount), 0),
            func.coalesce(func.sum(Sale.net_amount), 0),
            func.coalesce(func.sum(Sale.margin_amount), 0),
            func.coalesce(func.sum(Sale.discount_amount + Sale.campaign_discount_amount), 0),
            func.count(Sale.id),
            func.count(func.distinct(Sale.customer_id)),
        )
        .where(*_sale_filters(start, end, sp_ids))
        .group_by(Sale.sale_date)
    ).all()

    quantity_rows = db.execute(
        select(Sale.sale_date, func.coalesce(func.sum(SaleItem.base_quantity), 0))
        .select_from(SaleItem)
        .join(Sale, Sale.id == SaleItem.sale_id)
        .where(*_sale_filters(start, end, sp_ids))
        .group_by(Sale.sale_date)
    ).all()
    quantity_by_day = {r[0]: float(r[1] or 0) for r in quantity_rows}

    def _series(index: int) -> dict[date, float]:
        return {
            b.bucket_date: b.value
            for b in ts.resample([(r[0], float(r[index] or 0)) for r in header_rows], granularity, start=start, end=end)
        }

    amount = _series(1)
    net = _series(2)
    margin = _series(3)
    discount = _series(4)
    orders = _series(5)
    customers = _series(6)
    quantities = {
        b.bucket_date: b.value
        for b in ts.resample(list(quantity_by_day.items()), granularity, start=start, end=end)
    }

    out: list[dict[str, Any]] = []
    for bucket_date in sorted(amount):
        out.append(
            {
                "key": bucket_date.isoformat(),
                "label": ts.bucket_label(bucket_date, granularity),
                "bucket_date": bucket_date,
                "sales_amount": money(amount.get(bucket_date, 0.0)),
                "net_amount": money(net.get(bucket_date, 0.0)),
                "margin_amount": money(margin.get(bucket_date, 0.0)),
                "discount_amount": money(discount.get(bucket_date, 0.0)),
                "quantity": qty(quantities.get(bucket_date, 0.0)),
                "order_count": int(orders.get(bucket_date, 0.0)),
                #: Distinct customers per *day* summed into the bucket — a
                #: customer buying twice in a week is counted twice here.
                "customer_count": int(customers.get(bucket_date, 0.0)),
            }
        )
    return out


def _dimension_grouped_sales(
    db: Session, start: date, end: date, sp_ids: list[int] | None, key: str, limit: int
) -> list[dict[str, Any]]:
    filters = _sale_filters(start, end, sp_ids)
    item_based = key in ("product", "category", "brand")

    if key == "product":
        id_col, label_col = Product.id, Product.name
    elif key == "category":
        id_col, label_col = ProductCategory.id, ProductCategory.name
    elif key == "brand":
        id_col, label_col = Brand.id, Brand.name
    elif key == "customer":
        id_col, label_col = Customer.id, Customer.name
    elif key == "salesperson":
        id_col, label_col = Salesperson.id, Salesperson.full_name
    elif key == "region":
        id_col, label_col = Region.id, Region.name
    elif key == "route":
        id_col, label_col = Route.id, Route.name
    else:                                        # channel
        id_col, label_col = Customer.channel, Customer.channel

    if item_based:
        stmt = (
            select(
                id_col.label("gid"),
                func.coalesce(label_col, "—").label("glabel"),
                func.coalesce(func.sum(SaleItem.total_amount), 0),
                func.coalesce(func.sum(SaleItem.net_amount), 0),
                func.coalesce(func.sum(SaleItem.margin_amount), 0),
                func.coalesce(func.sum(SaleItem.discount_amount + SaleItem.campaign_discount_amount), 0),
                func.coalesce(func.sum(SaleItem.base_quantity), 0),
                func.count(func.distinct(SaleItem.sale_id)),
                func.count(func.distinct(Sale.customer_id)),
            )
            .select_from(SaleItem)
            .join(Sale, Sale.id == SaleItem.sale_id)
            .join(Product, Product.id == SaleItem.product_id)
        )
        if key == "category":
            stmt = stmt.join(ProductCategory, ProductCategory.id == Product.category_id, isouter=True)
        elif key == "brand":
            stmt = stmt.join(Brand, Brand.id == Product.brand_id, isouter=True)
    else:
        stmt = (
            select(
                id_col.label("gid"),
                func.coalesce(label_col, "—").label("glabel"),
                func.coalesce(func.sum(Sale.total_amount), 0),
                func.coalesce(func.sum(Sale.net_amount), 0),
                func.coalesce(func.sum(Sale.margin_amount), 0),
                func.coalesce(func.sum(Sale.discount_amount + Sale.campaign_discount_amount), 0),
                func.coalesce(func.sum(Sale.total_volume_l), 0),
                func.count(Sale.id),
                func.count(func.distinct(Sale.customer_id)),
            )
            .select_from(Sale)
        )
        if key in ("customer", "region", "channel"):
            stmt = stmt.join(Customer, Customer.id == Sale.customer_id)
            if key == "region":
                stmt = stmt.join(Region, Region.id == Customer.region_id, isouter=True)
        elif key == "salesperson":
            stmt = stmt.join(Salesperson, Salesperson.id == Sale.salesperson_id, isouter=True)
        elif key == "route":
            stmt = stmt.join(Route, Route.id == Sale.route_id, isouter=True)

    stmt = (
        stmt.where(*filters)
        .group_by("gid", "glabel")
        .order_by(func.coalesce(func.sum(SaleItem.total_amount if item_based else Sale.total_amount), 0).desc())
        .limit(max(1, int(limit)))
    )

    out: list[dict[str, Any]] = []
    for row in db.execute(stmt).all():
        out.append(
            {
                "key": str(row[0]) if row[0] is not None else "",
                "label": str(row[1]),
                "bucket_date": None,
                "sales_amount": money(row[2]),
                "net_amount": money(row[3]),
                "margin_amount": money(row[4]),
                "discount_amount": money(row[5]),
                "quantity": qty(row[6]) if item_based else Decimal("0"),
                "order_count": int(row[7] or 0),
                "customer_count": int(row[8] or 0),
            }
        )
    return out


# ===========================================================================
# Performance views
# ===========================================================================
def product_performance(
    db: Session, *, start: date | None = None, end: date | None = None,
    ctx_scope: Scope = None, limit: int = 50,
) -> list[dict[str, Any]]:
    """Per-SKU revenue, volume and margin, ranked by revenue."""
    begin, finish = _range(start, end)
    sp_ids = _sp_ids(ctx_scope)
    rows = db.execute(
        select(
            Product.id,
            Product.sku,
            Product.name,
            func.coalesce(ProductCategory.name, "—"),
            func.coalesce(Brand.name, "—"),
            func.coalesce(func.sum(SaleItem.total_amount), 0),
            func.coalesce(func.sum(SaleItem.net_amount), 0),
            func.coalesce(func.sum(SaleItem.margin_amount), 0),
            func.coalesce(func.sum(SaleItem.base_quantity), 0),
            func.count(func.distinct(SaleItem.sale_id)),
            func.count(func.distinct(Sale.customer_id)),
        )
        .select_from(SaleItem)
        .join(Sale, Sale.id == SaleItem.sale_id)
        .join(Product, Product.id == SaleItem.product_id)
        .join(ProductCategory, ProductCategory.id == Product.category_id, isouter=True)
        .join(Brand, Brand.id == Product.brand_id, isouter=True)
        .where(*_sale_filters(begin, finish, sp_ids))
        .group_by(Product.id, Product.sku, Product.name, ProductCategory.name, Brand.name)
        .order_by(func.coalesce(func.sum(SaleItem.total_amount), 0).desc())
        .limit(max(1, int(limit)))
    ).all()

    total = _window_total(db, begin, finish, sp_ids, item_based=True)
    return [
        {
            "rank": i + 1,
            "key": str(r[0]),
            "code": r[1],
            "label": r[2],
            "group_label": r[3],
            "secondary_label": r[4],
            "sales_amount": money(r[5]),
            "net_amount": money(r[6]),
            "margin_amount": money(r[7]),
            "margin_percent": pct(r[7], r[6]),
            "quantity": qty(r[8]),
            "order_count": int(r[9] or 0),
            "customer_count": int(r[10] or 0),
            "share_percent": pct(r[5], total),
        }
        for i, r in enumerate(rows)
    ]


def customer_performance(
    db: Session, *, start: date | None = None, end: date | None = None,
    ctx_scope: Scope = None, limit: int = 50,
) -> list[dict[str, Any]]:
    """Per-customer revenue, basket size, margin and current exposure."""
    begin, finish = _range(start, end)
    sp_ids = _sp_ids(ctx_scope)
    rows = db.execute(
        select(
            Customer.id,
            Customer.code,
            Customer.name,
            func.coalesce(Customer.channel, "—"),
            func.coalesce(func.sum(Sale.total_amount), 0),
            func.coalesce(func.sum(Sale.net_amount), 0),
            func.coalesce(func.sum(Sale.margin_amount), 0),
            func.count(Sale.id),
            func.max(Sale.sale_date),
            func.coalesce(Customer.balance, 0),
            func.coalesce(Customer.overdue_balance, 0),
        )
        .select_from(Sale)
        .join(Customer, Customer.id == Sale.customer_id)
        .where(*_sale_filters(begin, finish, sp_ids))
        .group_by(Customer.id, Customer.code, Customer.name, Customer.channel, Customer.balance, Customer.overdue_balance)
        .order_by(func.coalesce(func.sum(Sale.total_amount), 0).desc())
        .limit(max(1, int(limit)))
    ).all()

    total = _window_total(db, begin, finish, sp_ids)
    return [
        {
            "rank": i + 1,
            "key": str(r[0]),
            "code": r[1],
            "label": r[2],
            "group_label": str(r[3]),
            "sales_amount": money(r[4]),
            "net_amount": money(r[5]),
            "margin_amount": money(r[6]),
            "margin_percent": pct(r[6], r[5]),
            "order_count": int(r[7] or 0),
            "average_order_value": money(D(r[4]) / int(r[7])) if r[7] else Decimal("0"),
            "last_activity": r[8],
            "balance": money(r[9]),
            "overdue_balance": money(r[10]),
            "share_percent": pct(r[4], total),
        }
        for i, r in enumerate(rows)
    ]


def salesperson_performance(
    db: Session, *, start: date | None = None, end: date | None = None,
    ctx_scope: Scope = None, limit: int = 50,
) -> list[dict[str, Any]]:
    """
    Per-salesperson scorecard: revenue, margin, collections and visit activity.

    Collections and visits are fetched separately and merged in Python.  Joining
    them into the sales aggregate would multiply the rows (a fan-out across
    three one-to-many relations) and silently inflate every total.
    """
    begin, finish = _range(start, end)
    sp_ids = _sp_ids(ctx_scope)

    rows = db.execute(
        select(
            Salesperson.id,
            Salesperson.code,
            Salesperson.full_name,
            func.coalesce(func.sum(Sale.total_amount), 0),
            func.coalesce(func.sum(Sale.net_amount), 0),
            func.coalesce(func.sum(Sale.margin_amount), 0),
            func.count(Sale.id),
            func.count(func.distinct(Sale.customer_id)),
        )
        .select_from(Sale)
        .join(Salesperson, Salesperson.id == Sale.salesperson_id)
        .where(*_sale_filters(begin, finish, sp_ids))
        .group_by(Salesperson.id, Salesperson.code, Salesperson.full_name)
        .order_by(func.coalesce(func.sum(Sale.total_amount), 0).desc())
        .limit(max(1, int(limit)))
    ).all()

    ids = [int(r[0]) for r in rows]
    collections: dict[int, Decimal] = {}
    visits: dict[int, int] = {}
    productive: dict[int, int] = {}
    if ids:
        for pid, amount in db.execute(
            select(Payment.salesperson_id, func.coalesce(func.sum(Payment.amount), 0))
            .where(
                Payment.salesperson_id.in_(ids),
                Payment.payment_date >= begin,
                Payment.payment_date <= finish,
                Payment.is_deleted.is_(False),
                Payment.status != PaymentStatus.CANCELLED,
            )
            .group_by(Payment.salesperson_id)
        ).all():
            collections[int(pid)] = money(amount)

        for pid, count, sale_count in db.execute(
            select(
                Visit.salesperson_id,
                func.count(Visit.id),
                func.count(func.distinct(Visit.customer_id)),
            )
            .where(Visit.salesperson_id.in_(ids), Visit.visit_date >= begin, Visit.visit_date <= finish)
            .group_by(Visit.salesperson_id)
        ).all():
            visits[int(pid)] = int(count or 0)
            productive[int(pid)] = int(sale_count or 0)

    targets = _target_lookup(db, begin, finish, TargetSubject.SALESPERSON, TargetMetric.REVENUE)
    total = _window_total(db, begin, finish, sp_ids)

    out: list[dict[str, Any]] = []
    for i, r in enumerate(rows):
        pid = int(r[0])
        target_value = targets.get(pid, Decimal("0"))
        out.append(
            {
                "rank": i + 1,
                "key": str(pid),
                "code": r[1],
                "label": r[2],
                "group_label": "",
                "sales_amount": money(r[3]),
                "net_amount": money(r[4]),
                "margin_amount": money(r[5]),
                "margin_percent": pct(r[5], r[4]),
                "order_count": int(r[6] or 0),
                "customer_count": int(r[7] or 0),
                "collected_amount": collections.get(pid, Decimal("0")),
                "visit_count": visits.get(pid, 0),
                "visited_customers": productive.get(pid, 0),
                "target_value": target_value,
                "achievement_percent": pct(r[3], target_value),
                "share_percent": pct(r[3], total),
            }
        )
    return out


def region_performance(
    db: Session, *, start: date | None = None, end: date | None = None,
    ctx_scope: Scope = None, limit: int = 50,
) -> list[dict[str, Any]]:
    """Per-region revenue and reach, keyed off the customer's region."""
    begin, finish = _range(start, end)
    sp_ids = _sp_ids(ctx_scope)
    rows = db.execute(
        select(
            func.coalesce(Region.id, 0),
            func.coalesce(Region.code, "—"),
            func.coalesce(Region.name, "—"),
            func.coalesce(func.sum(Sale.total_amount), 0),
            func.coalesce(func.sum(Sale.net_amount), 0),
            func.coalesce(func.sum(Sale.margin_amount), 0),
            func.count(Sale.id),
            func.count(func.distinct(Sale.customer_id)),
            func.count(func.distinct(Sale.salesperson_id)),
        )
        .select_from(Sale)
        .join(Customer, Customer.id == Sale.customer_id)
        .join(Region, Region.id == Customer.region_id, isouter=True)
        .where(*_sale_filters(begin, finish, sp_ids))
        .group_by(Region.id, Region.code, Region.name)
        .order_by(func.coalesce(func.sum(Sale.total_amount), 0).desc())
        .limit(max(1, int(limit)))
    ).all()

    total = _window_total(db, begin, finish, sp_ids)
    return [
        {
            "rank": i + 1,
            "key": str(r[0]),
            "code": str(r[1]),
            "label": str(r[2]),
            "group_label": "",
            "sales_amount": money(r[3]),
            "net_amount": money(r[4]),
            "margin_amount": money(r[5]),
            "margin_percent": pct(r[5], r[4]),
            "order_count": int(r[6] or 0),
            "customer_count": int(r[7] or 0),
            "salesperson_count": int(r[8] or 0),
            "share_percent": pct(r[3], total),
        }
        for i, r in enumerate(rows)
    ]


def _target_lookup(
    db: Session, start: date, end: date, subject_type: str, metric: str
) -> dict[int, Decimal]:
    """Target values by subject id overlapping the window."""
    rows = db.execute(
        select(Target.subject_id, func.coalesce(func.sum(Target.target_value), 0)).where(
            Target.subject_type == subject_type,
            Target.metric == metric,
            Target.period_start <= end,
            Target.period_end >= start,
        ).group_by(Target.subject_id)
    ).all()
    return {int(r[0]): money(r[1]) for r in rows}


# ===========================================================================
# ABC & basket analysis
# ===========================================================================
def abc_analysis(
    db: Session, *, start: date | None = None, end: date | None = None,
    ctx_scope: Scope = None, metric: str = "revenue", limit: int = 1_000,
) -> list[dict[str, Any]]:
    """
    Pareto classification of the SKU range.

    Class A carries the first 80% of the chosen metric, B the next 15%, C the
    remaining 5%.  The point is stocking policy: A items must never be out of
    stock on the van, C items are candidates for delisting or order-only supply.
    """
    begin, finish = _range(start, end, default_days=90)
    sp_ids = _sp_ids(ctx_scope)
    by_volume = str(metric).lower() in ("volume", "quantity")
    value_column = (
        func.coalesce(func.sum(SaleItem.base_quantity), 0)
        if by_volume
        else func.coalesce(func.sum(SaleItem.total_amount), 0)
    )

    # Pareto cut-offs must be measured against the *whole* range, otherwise the
    # row limit would silently promote mid-range SKUs into class A.
    total = _scalar(
        db,
        select(
            func.coalesce(
                func.sum(SaleItem.base_quantity if by_volume else SaleItem.total_amount), 0
            )
        )
        .select_from(SaleItem)
        .join(Sale, Sale.id == SaleItem.sale_id)
        .where(*_sale_filters(begin, finish, sp_ids)),
    )

    rows = db.execute(
        select(
            Product.id,
            Product.sku,
            Product.name,
            value_column,
            func.coalesce(func.sum(SaleItem.total_amount), 0),
            func.coalesce(func.sum(SaleItem.base_quantity), 0),
            func.coalesce(func.sum(SaleItem.margin_amount), 0),
            func.count(func.distinct(SaleItem.sale_id)),
        )
        .select_from(SaleItem)
        .join(Sale, Sale.id == SaleItem.sale_id)
        .join(Product, Product.id == SaleItem.product_id)
        .where(*_sale_filters(begin, finish, sp_ids))
        .group_by(Product.id, Product.sku, Product.name)
        .order_by(value_column.desc())
        .limit(max(1, int(limit)))
    ).all()

    out: list[dict[str, Any]] = []
    cumulative = Decimal("0")
    for i, r in enumerate(rows):
        cumulative += D(r[3])
        cumulative_share = pct(cumulative, total)
        if cumulative_share <= 80.0:
            abc_class = "A"
        elif cumulative_share <= 95.0:
            abc_class = "B"
        else:
            abc_class = "C"
        out.append(
            {
                "rank": i + 1,
                "product_id": int(r[0]),
                "sku": r[1],
                "name": r[2],
                "value": qty(r[3]) if by_volume else money(r[3]),
                "sales_amount": money(r[4]),
                "quantity": qty(r[5]),
                "margin_amount": money(r[6]),
                "order_count": int(r[7] or 0),
                "share_percent": pct(r[3], total),
                "cumulative_percent": cumulative_share,
                "abc_class": abc_class,
            }
        )
    return out


def basket_analysis(
    db: Session, *, start: date | None = None, end: date | None = None,
    ctx_scope: Scope = None, min_support: float = 0.02, limit: int = 50,
) -> list[dict[str, Any]]:
    """
    Products bought together (market-basket / affinity analysis).

    Reports support, both directional confidences and lift.  *Lift* is the one
    to act on: a pair can co-occur in 30% of baskets simply because both items
    are bestsellers, and only lift above 1 says the pairing is real.  Feeds
    cross-sell prompts in the field app and van-load bundling.
    """
    begin, finish = _range(start, end, default_days=90)
    sp_ids = _sp_ids(ctx_scope)

    recent_sales = select(Sale.id).where(*_sale_filters(begin, finish, sp_ids)).order_by(
        Sale.sale_date.desc()
    ).limit(_BASKET_SALE_LIMIT).subquery()

    rows = db.execute(
        select(SaleItem.sale_id, SaleItem.product_id)
        .where(SaleItem.sale_id.in_(select(recent_sales.c.id)))
        .distinct()
    ).all()

    baskets: dict[int, set[int]] = {}
    for sale_id, product_id in rows:
        baskets.setdefault(int(sale_id), set()).add(int(product_id))

    total_baskets = len(baskets)
    if total_baskets < 2:
        return []

    singles: dict[int, int] = {}
    pairs: dict[tuple[int, int], int] = {}
    for products in baskets.values():
        for product_id in products:
            singles[product_id] = singles.get(product_id, 0) + 1
        if len(products) < 2:
            continue
        for a, b in combinations(sorted(products), 2):
            pairs[(a, b)] = pairs.get((a, b), 0) + 1

    threshold = max(1, int(round(float(min_support) * total_baskets)))
    candidates = [(pair, count) for pair, count in pairs.items() if count >= threshold]
    candidates.sort(key=lambda item: item[1], reverse=True)
    candidates = candidates[: max(1, int(limit))]

    names = _product_names(db, {pid for pair, _ in candidates for pid in pair})

    out: list[dict[str, Any]] = []
    for (a, b), count in candidates:
        support = count / total_baskets
        support_a = singles.get(a, 0) / total_baskets
        support_b = singles.get(b, 0) / total_baskets
        confidence_ab = safe_div(count, singles.get(a, 0))
        confidence_ba = safe_div(count, singles.get(b, 0))
        lift = safe_div(support, support_a * support_b) if support_a and support_b else 0.0
        out.append(
            {
                "product_a_id": a,
                "product_a_name": names.get(a, str(a)),
                "product_b_id": b,
                "product_b_name": names.get(b, str(b)),
                "pair_count": count,
                "basket_count": total_baskets,
                "support": round(support, 6),
                "confidence_a_to_b": round(confidence_ab, 6),
                "confidence_b_to_a": round(confidence_ba, 6),
                "lift": round(lift, 4),
            }
        )
    out.sort(key=lambda row: (row["lift"], row["support"]), reverse=True)
    return out


def _product_names(db: Session, ids: Iterable[int]) -> dict[int, str]:
    id_list = [int(i) for i in ids]
    if not id_list:
        return {}
    return {
        int(pid): name
        for pid, name in db.execute(select(Product.id, Product.name).where(Product.id.in_(id_list))).all()
    }


# ===========================================================================
# Forecasting
# ===========================================================================
def forecast_demand(
    db: Session,
    *,
    product_id: int | None = None,
    customer_id: int | None = None,
    salesperson_id: int | None = None,
    horizon_days: int = 14,
    granularity: str = "DAILY",
    ctx_scope: Scope = None,
    user_id: int | None = None,
    persist: bool = True,
    commit: bool = True,
) -> dict[str, Any]:
    """
    Forecast demand for a product / customer / salesperson (or the company).

    The history is built from delivered ``SaleItem`` quantities — not orders —
    because an order that was never delivered is not demand that stock had to
    cover.  The chosen method, its back-tested error and the bilingual
    explanation are persisted with the numbers so a forecast can always be
    audited after the fact.
    """
    granularity = (granularity or "DAILY").strip().upper()
    if granularity not in ts.GRANULARITIES:
        raise ValidationError("analytics.invalid_granularity", params={"granularity": granularity})

    horizon_days = int(clamp(float(horizon_days or 14), 1, 365))
    bucket_days = _BUCKET_DAYS[granularity]
    horizon_buckets = max(1, -(-horizon_days // bucket_days))     # ceil division

    today = date.today()
    start = today - timedelta(days=_LOOKBACK_DAYS[granularity])
    sp_ids = _sp_ids(ctx_scope)

    conds = _sale_filters(start, today, sp_ids)
    if product_id:
        conds.append(SaleItem.product_id == int(product_id))
    if customer_id:
        conds.append(Sale.customer_id == int(customer_id))
    if salesperson_id:
        conds.append(Sale.salesperson_id == int(salesperson_id))

    rows = db.execute(
        select(
            Sale.sale_date,
            func.coalesce(func.sum(SaleItem.base_quantity), 0),
            func.coalesce(func.sum(SaleItem.total_amount), 0),
        )
        .select_from(SaleItem)
        .join(Sale, Sale.id == SaleItem.sale_id)
        .where(*conds)
        .group_by(Sale.sale_date)
    ).all()

    buckets = ts.resample([(r[0], float(r[1] or 0)) for r in rows], granularity, start=start, end=today)
    series = [b.value for b in buckets]
    last_bucket = buckets[-1].bucket_date if buckets else today

    total_quantity = sum(float(r[1] or 0) for r in rows)
    total_amount = sum(float(r[2] or 0) for r in rows)
    average_price = safe_div(total_amount, total_quantity)

    label_tr, label_en, subject_type, subject_id = _forecast_subject(
        db, product_id, customer_id, salesperson_id
    )

    result = forecasting.ensemble(
        series,
        horizon_buckets,
        ts.seasonal_period_for(granularity),
        last_date=last_bucket,
        granularity=granularity,
        label_tr=label_tr,
        label_en=label_en,
    )

    run_id = uuid.uuid4().hex[:24]
    generated_at = datetime.now(UTC)
    explanation = f"{result['explanation_tr']}\n---\n{result['explanation_en']}"

    points: list[dict[str, Any]] = []
    for point in result["points"]:
        predicted = qty(point["value"])
        target_date = point["date"] or (last_bucket + timedelta(days=bucket_days * (point["index"] + 1)))
        points.append(
            {
                "bucket_date": target_date,
                "label": ts.bucket_label(target_date, granularity),
                "value": predicted,
                "lower": qty(point["lower"]),
                "upper": qty(point["upper"]),
                "amount": money(D(str(point["value"])) * D(str(average_price))),
            }
        )
        if persist:
            db.add(
                Forecast(
                    run_id=run_id,
                    subject_type=subject_type,
                    subject_id=subject_id,
                    product_id=int(product_id) if product_id else None,
                    customer_id=int(customer_id) if customer_id else None,
                    target_date=target_date,
                    horizon_days=horizon_days,
                    granularity=granularity,
                    method=result["method"],
                    predicted_quantity=predicted,
                    lower_bound=qty(point["lower"]),
                    upper_bound=qty(point["upper"]),
                    predicted_amount=money(D(str(point["value"])) * D(str(average_price))),
                    confidence=float(result["confidence"]),
                    explanation=explanation,
                    generated_at=generated_at,
                )
            )

    if persist:
        db.flush()
        audit_service_record(
            db,
            AuditAction.CREATE,
            entity_type="Forecast",
            entity_label=f"{subject_type}#{subject_id} {granularity}",
            user_id=user_id,
            summary=f"forecast run {run_id} method={result['method']} horizon={horizon_days}d",
        )
        if commit:
            db.commit()

    return {
        "run_id": run_id,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "product_id": product_id,
        "customer_id": customer_id,
        "salesperson_id": salesperson_id,
        "granularity": granularity,
        "horizon_days": horizon_days,
        "method": result["method"],
        "confidence": float(result["confidence"]),
        "mae": float(result["mae"]),
        "history_points": int(result["history_points"]),
        "total_forecast_quantity": qty(sum(float(p["value"]) for p in points)),
        "total_forecast_amount": money(sum(D(str(p["amount"])) for p in points)),
        "points": points,
        "candidates": result["candidates"],
        "explanation_tr": result["explanation_tr"],
        "explanation_en": result["explanation_en"],
        "generated_at": generated_at,
    }


def _forecast_subject(
    db: Session, product_id: int | None, customer_id: int | None, salesperson_id: int | None
) -> tuple[str, str, str, int]:
    """Resolve the bilingual label and (subject_type, subject_id) of a forecast."""
    if product_id:
        product = db.get(Product, int(product_id))
        if product is None:
            raise NotFoundError("product.not_found", params={"id": product_id})
        return product.name, product.name_en or product.name, str(TargetSubject.PRODUCT), int(product_id)
    if customer_id:
        customer = db.get(Customer, int(customer_id))
        if customer is None:
            raise NotFoundError("customer.not_found", params={"id": customer_id})
        return customer.name, customer.name, str(TargetSubject.CUSTOMER), int(customer_id)
    if salesperson_id:
        person = db.get(Salesperson, int(salesperson_id))
        if person is None:
            raise NotFoundError("salesperson.not_found", params={"id": salesperson_id})
        return person.full_name, person.full_name, str(TargetSubject.SALESPERSON), int(salesperson_id)
    return "Toplam talep", "Total demand", str(TargetSubject.COMPANY), 0


def audit_service_record(db: Session, action: str, **kwargs: Any) -> None:
    """Thin wrapper so audit failures can never break an analytics response."""
    from app.services import audit_service

    try:
        audit_service.record(db, action, **kwargs)
    except Exception:  # pragma: no cover - auditing must not mask the result
        log.exception("audit record failed for analytics action %s", action)


# ===========================================================================
# Van load suggestion
# ===========================================================================
def suggest_van_load(
    db: Session,
    *,
    salesperson_id: int,
    vehicle_id: int | None = None,
    on_date: date | None = None,
    weeks_back: int = 8,
    ctx_scope: Scope = None,
) -> list[dict[str, Any]]:
    """
    Suggest what to load onto a van for one salesperson's day.

    The statistical chain, in order:

    1. **Demand base** — average daily demand for the customers actually on
       today's route, over the last *weeks_back* weeks.
    2. **Weekday index** — Saturday is not Tuesday; the seasonal index for this
       weekday scales the base.
    3. **Trend** — last four weeks against the four before, clamped so one good
       week cannot double a load-out.
    4. **Campaign uplift** — a live promotion on the SKU raises expected demand.
    5. **Minus what is already on the van** — this is a top-up, not a reload.
    6. **Safety factor** — plus extra for erratic SKUs (high coefficient of
       variation), because a stock-out at the customer costs more than a case
       riding back to the depot.
    7. **Hard limits** — depot availability, shelf life, and the van's volume
       and weight capacity, applied last and in that order.

    The AI layer adds narrative on top; the numbers here stand on their own.
    """
    day = _day(on_date)
    person = db.get(Salesperson, int(salesperson_id))
    if person is None:
        raise NotFoundError("salesperson.not_found", params={"id": salesperson_id})

    allowed = _sp_ids(ctx_scope)
    if allowed is not None and int(salesperson_id) not in allowed:
        raise NotFoundError("salesperson.not_found", params={"id": salesperson_id})

    vehicle = _resolve_vehicle(db, person, vehicle_id)
    van_warehouse_id = int(vehicle.warehouse_id) if vehicle and vehicle.warehouse_id else None
    source_warehouse_id = _source_warehouse_id(db, person, vehicle)

    customer_ids = _route_customer_ids(db, int(salesperson_id), day)
    if not customer_ids:
        return []

    history_start = day - timedelta(days=int(weeks_back) * 7)
    rows = db.execute(
        select(
            SaleItem.product_id,
            Sale.sale_date,
            func.coalesce(func.sum(SaleItem.base_quantity), 0),
        )
        .select_from(SaleItem)
        .join(Sale, Sale.id == SaleItem.sale_id)
        .where(
            Sale.customer_id.in_(customer_ids),
            *_sale_filters(history_start, day - timedelta(days=1), None),
        )
        .group_by(SaleItem.product_id, Sale.sale_date)
    ).all()
    if not rows:
        return []

    by_product: dict[int, list[tuple[date, float]]] = {}
    for product_id, sale_date, quantity in rows:
        by_product.setdefault(int(product_id), []).append((sale_date, float(quantity or 0)))

    products = {
        int(p.id): p
        for p in db.execute(
            select(Product).where(
                Product.id.in_(list(by_product.keys())),
                Product.is_deleted.is_(False),
                Product.is_sellable.is_(True),
                Product.status == ProductStatus.ACTIVE,
            )
        ).scalars().all()
    }
    campaign_uplift = _campaign_uplift(db, day, set(products.keys()))
    weekday = weekday_code(day)
    weekday_slot = day.weekday()

    candidates: list[dict[str, Any]] = []
    for product_id, product in products.items():
        history = by_product.get(product_id, [])
        buckets = ts.resample(history, "DAILY", start=history_start, end=day - timedelta(days=1))
        series = [b.value for b in buckets]
        if not series or sum(series) <= 0:
            continue

        stats = descriptive.summary(series)
        base_daily = float(stats["mean"])

        indices = ts.seasonality(series, 7)
        # Series starts on ``history_start``; align the weekday to its slot.
        offset = (weekday_slot - history_start.weekday()) % 7
        weekday_index = float(indices[offset % len(indices)]) if indices else 1.0
        weekday_index = clamp(weekday_index, 0.4, 2.5)

        trend_factor = _recent_trend_factor(series)
        uplift = campaign_uplift.get(product_id, 1.0)

        # Erratic SKUs get a bigger buffer: cv is a percentage, so cv=100 (the
        # standard deviation equals the mean) adds another 15 points of safety.
        safety = _VAN_SAFETY_FACTOR + clamp(float(stats["cv"]) / 100.0 * 0.15, 0.0, 0.25)

        expected_base = base_daily * weekday_index * trend_factor * uplift * safety
        if expected_base <= 0:
            continue

        on_van = _available_stock(db, van_warehouse_id, product_id) if van_warehouse_id else Decimal("0")
        needed = D(str(expected_base)) - on_van
        if needed <= 0:
            continue

        depot_available = (
            _available_stock(db, source_warehouse_id, product_id) if source_warehouse_id else needed
        )
        if depot_available <= 0:
            continue
        if _shelf_life_blocked(db, source_warehouse_id, product):
            continue

        loadable = min(needed, depot_available)
        factor = _case_factor(product)
        cases = (loadable / factor).quantize(Decimal("1"), rounding="ROUND_CEILING")
        if cases <= 0:
            continue
        base_quantity = qty(cases * factor)

        volume_per_unit, weight_per_unit = _unit_volume_weight(product)
        confidence = clamp(
            0.9 - float(stats["cv"]) / 400.0 - (0.2 if len(series) < 14 else 0.0), 0.15, 0.95
        )

        candidates.append(
            {
                "product_id": product_id,
                "sku": product.sku,
                "name": product.name,
                "suggested_cases": int(cases),
                "base_quantity": base_quantity,
                "uom": product.sales_uom,
                "expected_value": float(base_quantity) * float(D(product.sale_price)) / float(factor),
                "volume_l": float(base_quantity) * volume_per_unit,
                "weight_kg": float(base_quantity) * weight_per_unit,
                "confidence": round(confidence, 3),
                "on_van_quantity": on_van,
                "depot_available": depot_available,
                "reason_tr": (
                    f"{weekday} günü bu rotadaki {len(customer_ids)} müşteri son {weeks_back} haftada "
                    f"günde ortalama {base_daily:,.1f} birim aldı. Gün endeksi ×{weekday_index:,.2f}, "
                    f"son 4 hafta trendi ×{trend_factor:,.2f}"
                    + (f", kampanya etkisi ×{uplift:,.2f}" if uplift > 1.0 else "")
                    + f", güvenlik payı ×{safety:,.2f}. Araçta {on_van:,.0f} birim var, "
                    f"{int(cases)} koli yüklenmesi öneriliyor."
                ),
                "reason_en": (
                    f"The {len(customer_ids)} customers on this route bought {base_daily:,.1f} units per day "
                    f"on average over the last {weeks_back} weeks. Weekday index ×{weekday_index:,.2f}, "
                    f"four-week trend ×{trend_factor:,.2f}"
                    + (f", campaign uplift ×{uplift:,.2f}" if uplift > 1.0 else "")
                    + f", safety factor ×{safety:,.2f}. The van already holds {on_van:,.0f} units, "
                    f"so loading {int(cases)} cases is suggested."
                ),
            }
        )

    candidates.sort(key=lambda row: row["expected_value"], reverse=True)
    return _fit_to_capacity(candidates, vehicle)


def _resolve_vehicle(db: Session, person: Salesperson, vehicle_id: int | None) -> Vehicle | None:
    if vehicle_id:
        vehicle = db.get(Vehicle, int(vehicle_id))
        if vehicle is None:
            raise NotFoundError("vehicle.not_found", params={"id": vehicle_id})
        return vehicle
    if person.default_vehicle_id:
        return db.get(Vehicle, int(person.default_vehicle_id))
    return None


def _source_warehouse_id(db: Session, person: Salesperson, vehicle: Vehicle | None) -> int | None:
    """Depot the van loads from: the salesperson's, then the van's home, then any central."""
    if person.default_warehouse_id:
        return int(person.default_warehouse_id)
    if vehicle and vehicle.home_warehouse_id:
        return int(vehicle.home_warehouse_id)
    return db.execute(
        select(Warehouse.id)
        .where(
            Warehouse.warehouse_type.in_([WarehouseType.CENTRAL, WarehouseType.REGIONAL]),
            Warehouse.is_active.is_(True),
            Warehouse.is_deleted.is_(False),
        )
        .order_by(Warehouse.id)
        .limit(1)
    ).scalar_one_or_none()


def _route_customer_ids(db: Session, salesperson_id: int, day: date) -> list[int]:
    """Customers on this salesperson's route for *day*, with a visit-plan fallback."""
    ids = [
        int(cid)
        for cid in db.execute(
            select(func.distinct(RouteStop.customer_id))
            .join(Route, Route.id == RouteStop.route_id)
            .where(
                Route.route_date == day,
                Route.salesperson_id == salesperson_id,
                Route.is_deleted.is_(False),
                Route.is_template.is_(False),
            )
        ).scalars().all()
    ]
    if ids:
        return ids

    code = weekday_code(day)
    return [
        int(cid)
        for cid in db.execute(
            select(Customer.id).where(
                Customer.default_salesperson_id == salesperson_id,
                Customer.is_deleted.is_(False),
                Customer.status == CustomerStatus.ACTIVE,
                Customer.visit_days.is_not(None),
                Customer.visit_days.like(f"%{code}%"),
            )
        ).scalars().all()
    ]


def _recent_trend_factor(series: Sequence[float], window: int = 28) -> float:
    """
    Last four weeks against the four before, as a multiplier.

    Clamped to [0.7, 1.4]: a promotion week or a public holiday must not be
    allowed to double or halve tomorrow's load.
    """
    if len(series) < window:
        return 1.0
    half = window // 2
    recent = sum(series[-half:])
    previous = sum(series[-window:-half])
    if previous <= 0:
        return 1.2 if recent > 0 else 1.0
    return float(clamp(recent / previous, 0.7, 1.4))


def _campaign_uplift(db: Session, day: date, product_ids: set[int]) -> dict[int, float]:
    """
    Expected demand multiplier per product from live campaigns.

    Deliberately conservative: a live promotion adds at most 40%, scaled by its
    discount depth.  Over-loading on a campaign that under-performs turns into
    returns and expiry write-offs.
    """
    if not product_ids:
        return {}
    campaigns = db.execute(
        select(Campaign).where(
            Campaign.status == CampaignStatus.ACTIVE,
            Campaign.is_deleted.is_(False),
            Campaign.start_date <= day,
            Campaign.end_date >= day,
        )
    ).scalars().all()
    if not campaigns:
        return {}

    products = {
        int(p.id): p
        for p in db.execute(select(Product).where(Product.id.in_(list(product_ids)))).scalars().all()
    }
    weekday = weekday_code(day)
    uplift: dict[int, float] = {}

    for campaign in campaigns:
        if campaign.active_weekdays and weekday not in campaign.active_weekdays.upper():
            continue
        depth = float(campaign.discount_percent or 0.0)
        factor = 1.0 + clamp(depth / 100.0 * 2.0, 0.05, 0.40)
        scope_values = {v.strip() for v in (campaign.scope_values or "").split(",") if v.strip()}

        for product_id, product in products.items():
            matched = False
            if campaign.scope == CampaignScope.ALL:
                matched = True
            elif campaign.scope == CampaignScope.PRODUCT:
                matched = str(product_id) in scope_values
            elif campaign.scope == CampaignScope.CATEGORY:
                matched = product.category_id is not None and str(product.category_id) in scope_values
            elif campaign.scope == CampaignScope.BRAND:
                matched = product.brand_id is not None and str(product.brand_id) in scope_values
            if matched:
                uplift[product_id] = max(uplift.get(product_id, 1.0), factor)
    return uplift


def _shelf_life_blocked(db: Session, warehouse_id: int | None, product: Product) -> bool:
    """
    True when nothing in stock meets the product's minimum remaining shelf life.

    Loading a case that expires inside the customer's own selling window just
    moves the write-off from our depot to their shelf, and they will send it
    back.
    """
    minimum = product.min_remaining_shelf_life_days
    if not minimum or not warehouse_id or not product.is_lot_tracked:
        return False
    cutoff = date.today() + timedelta(days=int(minimum))
    fresh = db.execute(
        select(func.coalesce(func.sum(StockBalance.quantity), 0))
        .join(Lot, Lot.id == StockBalance.lot_id)
        .where(
            StockBalance.warehouse_id == warehouse_id,
            StockBalance.product_id == int(product.id),
            StockBalance.quantity > 0,
            or_(Lot.expiry_date.is_(None), Lot.expiry_date >= cutoff),
        )
    ).scalar()
    return D(fresh or 0) <= 0


def _fit_to_capacity(candidates: list[dict[str, Any]], vehicle: Vehicle | None) -> list[dict[str, Any]]:
    """
    Trim the suggestion list to the van's volume and weight limits.

    Items are already sorted by expected value, so the cut falls on the least
    valuable lines — the same call a good salesperson makes by eye.
    """
    if vehicle is None:
        return [_strip_internal(row) for row in candidates]

    max_volume = float(vehicle.capacity_volume_l or 0.0)
    max_weight = float(vehicle.capacity_weight_kg or 0.0)
    used_volume = used_weight = 0.0
    kept: list[dict[str, Any]] = []

    for row in candidates:
        volume = float(row["volume_l"])
        weight = float(row["weight_kg"])
        if max_volume > 0 and used_volume + volume > max_volume:
            continue
        if max_weight > 0 and used_weight + weight > max_weight:
            continue
        used_volume += volume
        used_weight += weight
        kept.append(row)
    return [_strip_internal(row) for row in kept]


def _strip_internal(row: dict[str, Any]) -> dict[str, Any]:
    """Drop the ranking helpers the API contract does not expose."""
    out = dict(row)
    out.pop("expected_value", None)
    return out


# ===========================================================================
# Order suggestion (depletion model)
# ===========================================================================
def suggest_order(
    db: Session,
    *,
    customer_id: int,
    on_date: date | None = None,
    lookback_days: int = 180,
    limit: int = 25,
    ctx_scope: Scope = None,
) -> list[dict[str, Any]]:
    """
    Suggest what a customer is likely to need, ranked by "have they run out?".

    The model is a consumption-rate depletion estimate: a customer who buys 10
    cases every 14 days consumes ~0.71 cases a day, so 18 days after the last
    delivery they are past empty.  The probability is a logistic curve on the
    ratio ``days_since_last / days_of_cover`` — smooth rather than a hard
    threshold, because purchase intervals are noisy and a step function would
    make the suggestion list flicker from one day to the next.
    """
    day = _day(on_date)
    customer = db.get(Customer, int(customer_id))
    if customer is None or customer.is_deleted:
        raise NotFoundError("customer.not_found", params={"id": customer_id})

    allowed = _sp_ids(ctx_scope)
    if allowed is not None and (customer.default_salesperson_id or -1) not in allowed:
        raise NotFoundError("customer.not_found", params={"id": customer_id})

    start = day - timedelta(days=int(lookback_days))
    rows = db.execute(
        select(
            SaleItem.product_id,
            Product.sku,
            Product.name,
            Product.units_per_case,
            Product.sales_uom,
            func.coalesce(func.sum(SaleItem.base_quantity), 0),
            func.count(func.distinct(Sale.id)),
            func.min(Sale.sale_date),
            func.max(Sale.sale_date),
        )
        .select_from(SaleItem)
        .join(Sale, Sale.id == SaleItem.sale_id)
        .join(Product, Product.id == SaleItem.product_id)
        .where(
            Sale.customer_id == int(customer_id),
            Product.is_deleted.is_(False),
            Product.is_sellable.is_(True),
            *_sale_filters(start, day, None),
        )
        .group_by(SaleItem.product_id, Product.sku, Product.name, Product.units_per_case, Product.sales_uom)
    ).all()

    out: list[dict[str, Any]] = []
    for (
        product_id, sku, name, units_per_case, sales_uom,
        total_quantity, purchase_count, first_date, last_date,
    ) in rows:
        purchases = int(purchase_count or 0)
        total = float(total_quantity or 0)
        if purchases <= 0 or total <= 0 or last_date is None:
            continue

        factor = float(D(units_per_case)) or 1.0
        average_quantity = total / purchases
        days_since_last = max(0, (day - last_date).days)

        # Consumption rate over the *observed* span, not the whole lookback:
        # a customer onboarded last month must not look like a slow mover.
        span_days = max(1, (last_date - first_date).days) if purchases > 1 else max(1, int(lookback_days // 4))
        daily_rate = total / span_days
        days_of_cover = average_quantity / daily_rate if daily_rate > 0 else float(span_days)
        ratio = days_since_last / days_of_cover if days_of_cover > 0 else 0.0
        probability = 1.0 / (1.0 + pow(2.718281828, -4.0 * (ratio - 1.0)))

        suggested_base = average_quantity if probability >= 0.35 else 0.0
        suggested_cases = int(-(-suggested_base // factor)) if suggested_base > 0 else 0

        out.append(
            {
                "product_id": int(product_id),
                "sku": sku,
                "product": name,
                "uom": sales_uom,
                "avg_quantity": qty(average_quantity),
                "avg_cases": round(average_quantity / factor, 2),
                "purchase_count": purchases,
                "days_since_last": days_since_last,
                "days_of_cover": round(days_of_cover, 1),
                "last_purchase_date": last_date,
                "depletion_probability": round(clamp(probability, 0.0, 0.99), 4),
                "suggested_quantity": qty(suggested_cases * factor),
                "suggested_cases": suggested_cases,
                "reason_tr": (
                    f"{name}: son {purchases} alımda ortalama {average_quantity / factor:,.1f} koli aldı, "
                    f"tipik olarak {days_of_cover:,.0f} günde tüketiyor. Son alımdan bu yana "
                    f"{days_since_last} gün geçti — stoğunun bitmiş olma olasılığı "
                    f"%{clamp(probability, 0.0, 0.99) * 100:,.0f}."
                ),
                "reason_en": (
                    f"{name}: averaged {average_quantity / factor:,.1f} cases across {purchases} purchases and "
                    f"typically runs through them in {days_of_cover:,.0f} days. It has been "
                    f"{days_since_last} days since the last delivery — roughly a "
                    f"{clamp(probability, 0.0, 0.99) * 100:,.0f}% chance they have run out."
                ),
            }
        )

    out.sort(key=lambda row: (row["depletion_probability"], float(row["avg_quantity"])), reverse=True)
    return out[: max(1, int(limit))]


# ===========================================================================
# Anomaly detection
# ===========================================================================
def detect_anomalies(
    db: Session,
    *,
    start: date | None = None,
    end: date | None = None,
    ctx_scope: Scope = None,
    user_id: int | None = None,
    commit: bool = True,
) -> list[Anomaly]:
    """
    Scan the window for sales, discount, return, stock and collection anomalies.

    Existing open records for the same (type, subject, date) are skipped, so
    running the detector twice a day does not bury the list under duplicates.
    """
    begin, finish = _range(start, end, default_days=60)
    sp_ids = _sp_ids(ctx_scope)
    found: list[Anomaly] = []

    found += _detect_sales_anomalies(db, begin, finish, sp_ids)
    found += _detect_discount_anomalies(db, begin, finish, sp_ids)
    found += _detect_return_anomalies(db, begin, finish, sp_ids)
    found += _detect_stock_variance(db, begin, finish, sp_ids)
    found += _detect_collection_anomalies(db, begin, finish, sp_ids)

    persisted: list[Anomaly] = []
    for candidate in found:
        if _anomaly_exists(db, candidate):
            continue
        db.add(candidate)
        persisted.append(candidate)

    if persisted:
        db.flush()
        audit_service_record(
            db,
            AuditAction.CREATE,
            entity_type="Anomaly",
            entity_label=f"{begin}..{finish}",
            user_id=user_id,
            summary=f"{len(persisted)} anomalies detected",
        )
        if commit:
            db.commit()
    return persisted


def _anomaly_exists(db: Session, candidate: Anomaly) -> bool:
    return bool(
        db.execute(
            select(Anomaly.id).where(
                Anomaly.anomaly_type == candidate.anomaly_type,
                Anomaly.subject_type == candidate.subject_type,
                Anomaly.subject_id == candidate.subject_id,
                Anomaly.detected_on == candidate.detected_on,
            ).limit(1)
        ).scalar_one_or_none()
    )


def _daily_series(
    db: Session, start: date, end: date, column: Any, model: Any, date_column: Any, conds: list[Any]
) -> list[ts.Bucket]:
    rows = db.execute(
        select(date_column, func.coalesce(func.sum(column), 0)).where(*conds).group_by(date_column)
    ).all()
    return ts.resample([(r[0], float(r[1] or 0)) for r in rows], "DAILY", start=start, end=end)


def _detect_sales_anomalies(
    db: Session, start: date, end: date, sp_ids: list[int] | None
) -> list[Anomaly]:
    """Spikes and drops in daily revenue, judged against the weekday pattern."""
    buckets = _daily_series(
        db, start, end, Sale.total_amount, Sale, Sale.sale_date, _sale_filters(start, end, sp_ids)
    )
    series = [b.value for b in buckets]
    if len(series) < 10:
        return []

    out: list[Anomaly] = []
    detections = anomaly_lib.seasonal_residual_outliers(series, 7, threshold=2.5)
    if not detections:
        detections = anomaly_lib.zscore_outliers(series, 2.8)

    for hit in detections:
        bucket = buckets[int(hit["index"])]
        rising = hit["direction"] == "high"
        out.append(
            Anomaly(
                anomaly_type=AnomalyType.SALES_SPIKE if rising else AnomalyType.SALES_DROP,
                severity=hit["severity"],
                subject_type=str(TargetSubject.COMPANY),
                subject_id=0,
                subject_label="Toplam satış / Total sales",
                detected_on=bucket.bucket_date,
                observed_value=float(hit["value"]),
                expected_value=float(hit["expected"]),
                deviation=float(hit["value"]) - float(hit["expected"]),
                z_score=float(hit.get("z_score") or 0.0),
                method=str(hit.get("method")),
                title=(
                    f"{bucket.label}: satışta {'sıçrama' if rising else 'düşüş'} / "
                    f"sales {'spike' if rising else 'drop'}"
                ),
                description=(
                    f"Gerçekleşen {hit['value']:,.2f}, beklenen {hit['expected']:,.2f} "
                    f"(z={hit.get('z_score', 0):.2f}). / Observed {hit['value']:,.2f} against an "
                    f"expected {hit['expected']:,.2f}."
                ),
            )
        )

    # Sustained level shifts matter more than single days — flag them too.
    for shift in anomaly_lib.change_point(series):
        bucket = buckets[int(shift["index"])]
        rising = shift["direction"] == "up"
        out.append(
            Anomaly(
                anomaly_type=AnomalyType.SALES_SPIKE if rising else AnomalyType.SALES_DROP,
                severity=shift["severity"],
                subject_type=str(TargetSubject.COMPANY),
                subject_id=0,
                subject_label="Toplam satış / Total sales",
                detected_on=bucket.bucket_date,
                observed_value=float(shift["mean_after"]),
                expected_value=float(shift["mean_before"]),
                deviation=float(shift["deviation"]),
                method="cusum",
                title=f"{bucket.label}: kalıcı seviye değişimi / sustained level shift",
                description=(
                    f"Ortalama {shift['mean_before']:,.2f} seviyesinden {shift['mean_after']:,.2f} "
                    f"seviyesine kaydı. / Average level moved from {shift['mean_before']:,.2f} to "
                    f"{shift['mean_after']:,.2f}."
                ),
            )
        )
    return out


def _detect_discount_anomalies(
    db: Session, start: date, end: date, sp_ids: list[int] | None
) -> list[Anomaly]:
    """Sales whose discount rate is far outside the normal distribution."""
    rows = db.execute(
        select(Sale.id, Sale.sale_no, Sale.sale_date, Sale.salesperson_id, Sale.gross_amount, Sale.discount_amount + Sale.campaign_discount_amount)
        .where(*_sale_filters(start, end, sp_ids), Sale.gross_amount > 0)
    ).all()
    if len(rows) < 12:
        return []

    percents = [float(D(r[5]) / D(r[4]) * 100) for r in rows]
    hits = anomaly_lib.iqr_outliers(percents, 2.0)

    out: list[Anomaly] = []
    for hit in hits:
        if hit["direction"] != "high":
            continue
        row = rows[int(hit["index"])]
        out.append(
            Anomaly(
                anomaly_type=AnomalyType.UNUSUAL_DISCOUNT,
                severity=hit["severity"],
                subject_type="SALE",
                subject_id=int(row[0]),
                subject_label=str(row[1]),
                detected_on=row[2],
                observed_value=float(hit["value"]),
                expected_value=float(hit["expected"]),
                deviation=float(hit["deviation"]),
                z_score=float(hit.get("z_score") or 0.0),
                method="iqr",
                title=f"{row[1]}: olağandışı iskonto / unusual discount",
                description=(
                    f"İskonto %{hit['value']:,.1f}; tipik seviye %{hit['expected']:,.1f}. / "
                    f"Discount of {hit['value']:,.1f}% against a typical {hit['expected']:,.1f}%."
                ),
            )
        )
    return out


def _detect_return_anomalies(
    db: Session, start: date, end: date, sp_ids: list[int] | None
) -> list[Anomaly]:
    """Days where returns are abnormally high relative to the usual level."""
    conds: list[Any] = [
        ReturnDocument.return_date >= start,
        ReturnDocument.return_date <= end,
        ReturnDocument.is_deleted.is_(False),
    ]
    if sp_ids is not None:
        conds.append(ReturnDocument.salesperson_id.in_(sp_ids))

    buckets = _daily_series(
        db, start, end, ReturnDocument.total_amount, ReturnDocument, ReturnDocument.return_date, conds
    )
    series = [b.value for b in buckets]
    if len(series) < 10 or sum(series) <= 0:
        return []

    out: list[Anomaly] = []
    for hit in anomaly_lib.iqr_outliers(series, 2.0):
        if hit["direction"] != "high":
            continue
        bucket = buckets[int(hit["index"])]
        out.append(
            Anomaly(
                anomaly_type=AnomalyType.UNUSUAL_RETURN,
                severity=hit["severity"],
                subject_type=str(TargetSubject.COMPANY),
                subject_id=0,
                subject_label="İade / Returns",
                detected_on=bucket.bucket_date,
                observed_value=float(hit["value"]),
                expected_value=float(hit["expected"]),
                deviation=float(hit["deviation"]),
                method="iqr",
                title=f"{bucket.label}: yüksek iade tutarı / high return volume",
                description=(
                    f"İade {hit['value']:,.2f}; normal seviye {hit['expected']:,.2f}. / "
                    f"Returns of {hit['value']:,.2f} against a normal {hit['expected']:,.2f}."
                ),
            )
        )
    return out


def _detect_stock_variance(
    db: Session, start: date, end: date, sp_ids: list[int] | None
) -> list[Anomaly]:
    """Day sessions closing with an unusual physical-count variance."""
    conds: list[Any] = [DaySession.session_date >= start, DaySession.session_date <= end]
    if sp_ids is not None:
        conds.append(DaySession.salesperson_id.in_(sp_ids))

    rows = db.execute(
        select(
            DaySession.id, DaySession.session_date, DaySession.salesperson_id,
            DaySession.variance_value, DaySession.variance_qty,
        ).where(*conds)
    ).all()
    if len(rows) < 8:
        return []

    values = [abs(float(D(r[3]))) for r in rows]
    out: list[Anomaly] = []
    for hit in anomaly_lib.zscore_outliers(values, 2.5):
        row = rows[int(hit["index"])]
        if float(D(row[3])) == 0:
            continue
        out.append(
            Anomaly(
                anomaly_type=AnomalyType.STOCK_VARIANCE,
                severity=hit["severity"],
                subject_type=str(TargetSubject.SALESPERSON),
                subject_id=int(row[2]) if row[2] else None,
                subject_label=f"Gün {row[1]} / session #{row[0]}",
                detected_on=row[1],
                observed_value=float(D(row[3])),
                expected_value=float(hit["expected"]),
                deviation=float(hit["deviation"]),
                z_score=float(hit.get("z_score") or 0.0),
                method="zscore",
                title=f"{row[1]}: yüksek sayım farkı / high count variance",
                description=(
                    f"Fark değeri {float(D(row[3])):,.2f}, miktar farkı {float(D(row[4])):,.2f}. / "
                    f"Variance value {float(D(row[3])):,.2f} on {float(D(row[4])):,.2f} units."
                ),
            )
        )
    return out


def _detect_collection_anomalies(
    db: Session, start: date, end: date, sp_ids: list[int] | None
) -> list[Anomaly]:
    """Unusually weak (or strong) collection days."""
    conds: list[Any] = [
        Payment.payment_date >= start,
        Payment.payment_date <= end,
        Payment.is_deleted.is_(False),
        Payment.status != PaymentStatus.CANCELLED,
    ]
    if sp_ids is not None:
        conds.append(Payment.salesperson_id.in_(sp_ids))

    buckets = _daily_series(db, start, end, Payment.amount, Payment, Payment.payment_date, conds)
    series = [b.value for b in buckets]
    if len(series) < 10 or sum(series) <= 0:
        return []

    out: list[Anomaly] = []
    for hit in anomaly_lib.zscore_outliers(series, 2.5):
        bucket = buckets[int(hit["index"])]
        out.append(
            Anomaly(
                anomaly_type=AnomalyType.COLLECTION_ANOMALY,
                severity=hit["severity"],
                subject_type=str(TargetSubject.COMPANY),
                subject_id=0,
                subject_label="Tahsilat / Collections",
                detected_on=bucket.bucket_date,
                observed_value=float(hit["value"]),
                expected_value=float(hit["expected"]),
                deviation=float(hit["deviation"]),
                z_score=float(hit.get("z_score") or 0.0),
                method="zscore",
                title=f"{bucket.label}: olağandışı tahsilat / unusual collection day",
                description=(
                    f"Tahsilat {hit['value']:,.2f}; beklenen {hit['expected']:,.2f}. / "
                    f"Collected {hit['value']:,.2f} against an expected {hit['expected']:,.2f}."
                ),
            )
        )
    return out


def list_anomalies(
    db: Session,
    *,
    start: date | None = None,
    end: date | None = None,
    anomaly_type: str | None = None,
    severity: str | None = None,
    is_resolved: bool | None = None,
    ctx_scope: Scope = None,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[Anomaly], int]:
    conds: list[Any] = []
    if start:
        conds.append(Anomaly.detected_on >= start)
    if end:
        conds.append(Anomaly.detected_on <= end)
    if anomaly_type:
        conds.append(Anomaly.anomaly_type == anomaly_type)
    if severity:
        conds.append(Anomaly.severity == severity)
    if is_resolved is not None:
        conds.append(Anomaly.is_resolved.is_(bool(is_resolved)))

    sp_ids = _sp_ids(ctx_scope)
    if sp_ids is not None:
        # A restricted caller sees company-wide anomalies plus their own.
        conds.append(
            or_(
                Anomaly.subject_type != str(TargetSubject.SALESPERSON),
                Anomaly.subject_id.in_(sp_ids),
            )
        )

    total = _count(db, select(func.count(Anomaly.id)).where(*conds))
    rows = db.execute(
        select(Anomaly)
        .where(*conds)
        .order_by(Anomaly.detected_on.desc(), Anomaly.id.desc())
        .offset(max(0, int(offset)))
        .limit(max(1, int(limit)))
    ).scalars().all()
    return list(rows), total


def resolve_anomaly(
    db: Session,
    anomaly_id: int,
    *,
    note: str | None = None,
    user_id: int | None = None,
    audit: Mapping[str, Any] | None = None,
    commit: bool = True,
) -> Anomaly:
    """Close an anomaly with a resolution note (append-only in spirit: never deleted)."""
    row = db.get(Anomaly, int(anomaly_id))
    if row is None:
        raise NotFoundError("analytics.anomaly_not_found", params={"id": anomaly_id})

    before = {"is_resolved": row.is_resolved, "resolution_note": row.resolution_note}
    row.is_resolved = True
    row.resolved_at = datetime.now(UTC)
    row.resolved_by_id = user_id
    row.resolution_note = note
    db.flush()

    audit_service_record(
        db,
        AuditAction.UPDATE,
        entity_type="Anomaly",
        entity_id=row.id,
        entity_label=row.title,
        old_values=before,
        new_values={"is_resolved": True, "resolution_note": note},
        summary="anomaly resolved",
        **(dict(audit) if audit else {"user_id": user_id}),
    )
    if commit:
        db.commit()
    return row


# ===========================================================================
# Targets
# ===========================================================================
def list_targets(
    db: Session,
    *,
    subject_type: str | None = None,
    subject_id: int | None = None,
    metric: str | None = None,
    period: str | None = None,
    start: date | None = None,
    end: date | None = None,
    ctx_scope: Scope = None,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[Target], int]:
    conds: list[Any] = []
    if subject_type:
        conds.append(Target.subject_type == subject_type)
    if subject_id is not None:
        conds.append(Target.subject_id == int(subject_id))
    if metric:
        conds.append(Target.metric == metric)
    if period:
        conds.append(Target.period == period)
    if start:
        conds.append(Target.period_end >= start)
    if end:
        conds.append(Target.period_start <= end)

    sp_ids = _sp_ids(ctx_scope)
    if sp_ids is not None:
        conds.append(
            or_(
                Target.subject_type != str(TargetSubject.SALESPERSON),
                Target.subject_id.in_(sp_ids),
            )
        )

    total = _count(db, select(func.count(Target.id)).where(*conds))
    rows = db.execute(
        select(Target)
        .where(*conds)
        .order_by(Target.period_start.desc(), Target.id.desc())
        .offset(max(0, int(offset)))
        .limit(max(1, int(limit)))
    ).scalars().all()
    return list(rows), total


def create_target(
    db: Session,
    *,
    subject_type: str,
    subject_id: int,
    metric: str,
    period: str,
    period_start: date,
    period_end: date,
    target_value: Decimal,
    currency: str | None = None,
    notes: str | None = None,
    user_id: int | None = None,
    audit: Mapping[str, Any] | None = None,
    commit: bool = True,
) -> Target:
    """
    Create — or overwrite — the target for one (subject, metric, period) cell.

    Upsert rather than insert because the table has a unique constraint on that
    combination: re-submitting a revised target is a normal management action,
    not an error the user should have to resolve by hand.
    """
    if period_end < period_start:
        raise ValidationError("analytics.invalid_period")
    if D(target_value) < 0:
        raise ValidationError("analytics.invalid_target_value")

    existing = db.execute(
        select(Target).where(
            Target.subject_type == subject_type,
            Target.subject_id == int(subject_id),
            Target.metric == metric,
            Target.period == period,
            Target.period_start == period_start,
        )
    ).scalar_one_or_none()

    if existing is not None:
        before = {"target_value": str(existing.target_value), "period_end": str(existing.period_end)}
        existing.target_value = money(target_value)
        existing.period_end = period_end
        existing.currency = currency or existing.currency
        existing.notes = notes if notes is not None else existing.notes
        existing.updated_by_id = user_id
        db.flush()
        _refresh_one_target(db, existing)
        audit_service_record(
            db, AuditAction.UPDATE, entity_type="Target", entity_id=existing.id,
            entity_label=f"{subject_type}#{subject_id} {metric}",
            old_values=before, new_values={"target_value": str(existing.target_value)},
            amount=existing.target_value, summary="target updated",
            **(dict(audit) if audit else {"user_id": user_id}),
        )
        if commit:
            db.commit()
        return existing

    row = Target(
        subject_type=subject_type,
        subject_id=int(subject_id),
        metric=metric,
        period=period,
        period_start=period_start,
        period_end=period_end,
        target_value=money(target_value),
        currency=currency or settings.default_currency,
        notes=notes,
        created_by_id=user_id,
    )
    db.add(row)
    db.flush()
    _refresh_one_target(db, row)

    audit_service_record(
        db, AuditAction.CREATE, entity_type="Target", entity_id=row.id,
        entity_label=f"{subject_type}#{subject_id} {metric}",
        new_values={"target_value": str(row.target_value), "period_start": str(period_start)},
        amount=row.target_value, summary="target created",
        **(dict(audit) if audit else {"user_id": user_id}),
    )
    if commit:
        db.commit()
    return row


def update_target(
    db: Session,
    target_id: int,
    *,
    target_value: Decimal | None = None,
    period_end: date | None = None,
    notes: str | None = None,
    user_id: int | None = None,
    audit: Mapping[str, Any] | None = None,
    commit: bool = True,
) -> Target:
    row = db.get(Target, int(target_id))
    if row is None:
        raise NotFoundError("analytics.target_not_found", params={"id": target_id})

    before = {"target_value": str(row.target_value), "period_end": str(row.period_end), "notes": row.notes}
    if target_value is not None:
        if D(target_value) < 0:
            raise ValidationError("analytics.invalid_target_value")
        row.target_value = money(target_value)
    if period_end is not None:
        if period_end < row.period_start:
            raise ValidationError("analytics.invalid_period")
        row.period_end = period_end
    if notes is not None:
        row.notes = notes
    row.updated_by_id = user_id
    db.flush()
    _refresh_one_target(db, row)

    audit_service_record(
        db, AuditAction.UPDATE, entity_type="Target", entity_id=row.id,
        entity_label=f"{row.subject_type}#{row.subject_id} {row.metric}",
        old_values=before, new_values={"target_value": str(row.target_value)},
        amount=row.target_value, summary="target updated",
        **(dict(audit) if audit else {"user_id": user_id}),
    )
    if commit:
        db.commit()
    return row


def delete_target(
    db: Session,
    target_id: int,
    *,
    user_id: int | None = None,
    audit: Mapping[str, Any] | None = None,
    commit: bool = True,
) -> None:
    row = db.get(Target, int(target_id))
    if row is None:
        raise NotFoundError("analytics.target_not_found", params={"id": target_id})

    snapshot = {
        "subject_type": row.subject_type, "subject_id": row.subject_id,
        "metric": row.metric, "target_value": str(row.target_value),
    }
    db.delete(row)
    db.flush()
    audit_service_record(
        db, AuditAction.DELETE, entity_type="Target", entity_id=int(target_id),
        entity_label=f"{snapshot['subject_type']}#{snapshot['subject_id']}",
        old_values=snapshot, summary="target deleted",
        **(dict(audit) if audit else {"user_id": user_id}),
    )
    if commit:
        db.commit()


def refresh_targets(
    db: Session,
    period_start: date | None = None,
    period_end: date | None = None,
    *,
    ctx_scope: Scope = None,
    user_id: int | None = None,
    commit: bool = True,
) -> list[Target]:
    """
    Recompute actual, projected and risk for every target in the window.

    ``projected_value`` is a straight run-rate: what the subject reaches if the
    remaining days look like the days so far.  ``risk_score`` (0-100) combines
    the projected shortfall with how much of the period is already gone —
    missing by 30% on day 2 is recoverable, missing by 30% on day 28 is not.
    """
    today = date.today()
    window_start, window_end = target_window(period_start, period_end)

    rows = db.execute(
        select(Target).where(Target.period_start <= window_end, Target.period_end >= window_start)
    ).scalars().all()

    for row in rows:
        _refresh_one_target(db, row, as_of=today)

    db.flush()
    if rows:
        audit_service_record(
            db, AuditAction.UPDATE, entity_type="Target",
            entity_label=f"{window_start}..{window_end}", user_id=user_id,
            summary=f"{len(rows)} targets refreshed",
        )
        if commit:
            db.commit()
    return list(rows)


def _refresh_one_target(db: Session, row: Target, *, as_of: date | None = None) -> Target:
    today = as_of or date.today()
    actual = _target_actual(db, row)
    row.actual_value = actual

    total_days = max(1, (row.period_end - row.period_start).days + 1)
    elapsed_days = int(clamp((today - row.period_start).days + 1, 0, total_days))
    elapsed_fraction = elapsed_days / total_days

    if elapsed_days <= 0:
        projected = Decimal("0")
    elif today >= row.period_end:
        projected = actual
    else:
        projected = money(actual / Decimal(elapsed_days) * Decimal(total_days))
    row.projected_value = projected

    target_value = D(row.target_value)
    if target_value <= 0:
        row.risk_score = 0.0
    else:
        gap = max(0.0, 1.0 - float(projected / target_value))
        row.risk_score = round(clamp(gap * 100.0 * (0.6 + 0.4 * elapsed_fraction), 0.0, 100.0), 2)
    row.last_calculated_at = datetime.now(UTC)
    return row


def _target_actual(db: Session, row: Target) -> Decimal:
    """Realised value of one target's metric over its own period and subject."""
    start, end = row.period_start, row.period_end
    metric = row.metric
    subject_type = row.subject_type
    subject_id = int(row.subject_id or 0)

    if metric == TargetMetric.COLLECTION:
        conds: list[Any] = [
            Payment.payment_date >= start, Payment.payment_date <= end,
            Payment.is_deleted.is_(False), Payment.status != PaymentStatus.CANCELLED,
        ]
        if subject_type == TargetSubject.SALESPERSON and subject_id:
            conds.append(Payment.salesperson_id == subject_id)
        elif subject_type == TargetSubject.CUSTOMER and subject_id:
            conds.append(Payment.customer_id == subject_id)
        return _scalar(db, select(func.coalesce(func.sum(Payment.amount), 0)).where(*conds))

    if metric == TargetMetric.VISITS:
        conds = [Visit.visit_date >= start, Visit.visit_date <= end]
        if subject_type == TargetSubject.SALESPERSON and subject_id:
            conds.append(Visit.salesperson_id == subject_id)
        elif subject_type == TargetSubject.CUSTOMER and subject_id:
            conds.append(Visit.customer_id == subject_id)
        return Decimal(_count(db, select(func.count(Visit.id)).where(*conds)))

    if metric == TargetMetric.NEW_CUSTOMERS:
        conds = [
            Customer.is_deleted.is_(False),
            Customer.first_order_date.is_not(None),
            Customer.first_order_date >= start,
            Customer.first_order_date <= end,
        ]
        if subject_type == TargetSubject.SALESPERSON and subject_id:
            conds.append(Customer.default_salesperson_id == subject_id)
        elif subject_type == TargetSubject.REGION and subject_id:
            conds.append(Customer.region_id == subject_id)
        return Decimal(_count(db, select(func.count(Customer.id)).where(*conds)))

    # Sales-based metrics: revenue, volume, margin.
    item_based = metric == TargetMetric.VOLUME or subject_type in (
        TargetSubject.PRODUCT, TargetSubject.CATEGORY, TargetSubject.BRAND,
    )
    conds = _sale_filters(start, end, None)

    if subject_type == TargetSubject.SALESPERSON and subject_id:
        conds.append(Sale.salesperson_id == subject_id)
    elif subject_type == TargetSubject.ROUTE and subject_id:
        conds.append(Sale.route_id == subject_id)
    elif subject_type == TargetSubject.CUSTOMER and subject_id:
        conds.append(Sale.customer_id == subject_id)
    elif subject_type == TargetSubject.REGION and subject_id:
        conds.append(Sale.customer_id.in_(select(Customer.id).where(Customer.region_id == subject_id)))

    if not item_based:
        column = Sale.margin_amount if metric == TargetMetric.MARGIN else Sale.total_amount
        return _scalar(db, select(func.coalesce(func.sum(column), 0)).where(*conds))

    if metric == TargetMetric.VOLUME:
        column = SaleItem.base_quantity
    elif metric == TargetMetric.MARGIN:
        column = SaleItem.margin_amount
    else:
        column = SaleItem.total_amount

    stmt = (
        select(func.coalesce(func.sum(column), 0))
        .select_from(SaleItem)
        .join(Sale, Sale.id == SaleItem.sale_id)
    )
    if subject_type == TargetSubject.PRODUCT and subject_id:
        conds.append(SaleItem.product_id == subject_id)
    elif subject_type in (TargetSubject.CATEGORY, TargetSubject.BRAND) and subject_id:
        stmt = stmt.join(Product, Product.id == SaleItem.product_id)
        conds.append(
            Product.category_id == subject_id
            if subject_type == TargetSubject.CATEGORY
            else Product.brand_id == subject_id
        )
    return _scalar(db, stmt.where(*conds))


# ===========================================================================
# Statistics screen
# ===========================================================================
def statistics_overview(
    db: Session, *, start: date | None = None, end: date | None = None, ctx_scope: Scope = None
) -> dict[str, Any]:
    """
    Everything the statistics screen shows in one call.

    Six daily business series are built over the same window so they are
    directly comparable, then summarised, correlated and trend-fitted.  Building
    them from one shared calendar is what makes the correlations meaningful — a
    correlation between series with different gaps is an artefact.
    """
    begin, finish = _range(start, end, default_days=90)
    sp_ids = _sp_ids(ctx_scope)
    calendar = date_range(begin, finish)

    sales_rows = db.execute(
        select(
            Sale.sale_date,
            func.coalesce(func.sum(Sale.total_amount), 0),
            func.coalesce(func.sum(Sale.margin_amount), 0),
            func.coalesce(func.sum(Sale.discount_amount + Sale.campaign_discount_amount), 0),
            func.count(Sale.id),
        )
        .where(*_sale_filters(begin, finish, sp_ids))
        .group_by(Sale.sale_date)
    ).all()

    def _bucketed(index: int) -> list[float]:
        return [
            b.value
            for b in ts.resample([(r[0], float(r[index] or 0)) for r in sales_rows], "DAILY", start=begin, end=finish)
        ]

    sales = _bucketed(1)
    margin = _bucketed(2)
    discount = _bucketed(3)
    order_count = _bucketed(4)

    payment_conds: list[Any] = [
        Payment.payment_date >= begin, Payment.payment_date <= finish,
        Payment.is_deleted.is_(False), Payment.status != PaymentStatus.CANCELLED,
    ]
    if sp_ids is not None:
        payment_conds.append(Payment.salesperson_id.in_(sp_ids))
    collections = [
        b.value
        for b in _daily_series(db, begin, finish, Payment.amount, Payment, Payment.payment_date, payment_conds)
    ]

    visit_conds: list[Any] = [Visit.visit_date >= begin, Visit.visit_date <= finish]
    if sp_ids is not None:
        visit_conds.append(Visit.salesperson_id.in_(sp_ids))
    visit_rows = db.execute(
        select(Visit.visit_date, func.count(Visit.id)).where(*visit_conds).group_by(Visit.visit_date)
    ).all()
    visits = [
        b.value
        for b in ts.resample([(r[0], float(r[1] or 0)) for r in visit_rows], "DAILY", start=begin, end=finish)
    ]

    return_conds: list[Any] = [
        ReturnDocument.return_date >= begin, ReturnDocument.return_date <= finish,
        ReturnDocument.is_deleted.is_(False),
    ]
    if sp_ids is not None:
        return_conds.append(ReturnDocument.salesperson_id.in_(sp_ids))
    returns = [
        b.value
        for b in _daily_series(
            db, begin, finish, ReturnDocument.total_amount, ReturnDocument, ReturnDocument.return_date, return_conds
        )
    ]

    series_map = {
        "Satış / Sales": sales,
        "Tahsilat / Collections": collections,
        "Ziyaret / Visits": visits,
        "Sipariş / Orders": order_count,
        "İskonto / Discount": discount,
        "İade / Returns": returns,
    }
    matrix = correlation_lib.correlation_matrix(series_map)
    #: Average basket value per selling day — undefined on days with no orders,
    #: so those days are excluded rather than counted as a zero basket.
    basket_values = [v / c for v, c in zip(sales, order_count, strict=False) if c > 0]

    weekday_indices = ts.seasonality(sales, 7)
    weekday_start = begin.weekday()
    weekday_profile = [
        {
            "weekday": ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"][(weekday_start + i) % 7],
            "index": round(float(weekday_indices[i]), 4),
        }
        for i in range(len(weekday_indices))
    ]

    fit = regression.linear_regression(list(range(len(sales))), sales)

    return {
        "start": begin,
        "end": finish,
        "days": len(calendar),
        "descriptive": {
            "sales": descriptive.summary(sales),
            "collections": descriptive.summary(collections),
            "margin": descriptive.summary(margin),
            "visits": descriptive.summary(visits),
            "orders": descriptive.summary(order_count),
            "basket_value": descriptive.summary(basket_values),
            "returns": descriptive.summary(returns),
        },
        "histogram": descriptive.histogram(sales, 10),
        "trend": ts.trend(sales),
        "regression": fit.as_dict(),
        "growth": {
            "day_over_day": ts.period_over_period(sales[-1] if sales else 0, sales[-2] if len(sales) > 1 else 0),
            "week_over_week": ts.period_over_period(sum(sales[-7:]), sum(sales[-14:-7])) if len(sales) >= 14 else 0.0,
            "period_over_period": ts.period_over_period(
                sum(sales[len(sales) // 2 :]), sum(sales[: len(sales) // 2])
            ) if len(sales) >= 4 else 0.0,
        },
        "weekday_profile": weekday_profile,
        "decomposition": ts.decompose(sales, 7),
        "correlation_matrix": matrix,
        "top_correlations": correlation_lib.top_correlations(matrix, 8),
        "series": {
            "labels": [d.isoformat() for d in calendar],
            "sales": sales,
            "collections": collections,
            "visits": visits,
            "orders": order_count,
            "discount": discount,
            "returns": returns,
        },
    }


def correlations(
    db: Session, *, start: date | None = None, end: date | None = None,
    ctx_scope: Scope = None, limit: int = 10, method: str = "pearson",
) -> dict[str, Any]:
    """Standalone correlation view (same series as the statistics screen)."""
    overview = statistics_overview(db, start=start, end=end, ctx_scope=ctx_scope)
    series = overview["series"]
    data = {
        "Satış / Sales": series["sales"],
        "Tahsilat / Collections": series["collections"],
        "Ziyaret / Visits": series["visits"],
        "Sipariş / Orders": series["orders"],
        "İskonto / Discount": series["discount"],
        "İade / Returns": series["returns"],
    }
    matrix = correlation_lib.correlation_matrix(data, method=method)
    return {
        "start": overview["start"],
        "end": overview["end"],
        "method": method,
        "matrix": matrix,
        "top": correlation_lib.top_correlations(matrix, limit),
    }
