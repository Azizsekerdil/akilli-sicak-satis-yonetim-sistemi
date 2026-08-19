"""
Analytics API: dashboard, sales analysis, statistics, forecasting, van-load and
order suggestions, targets and anomalies.

Every handler is a plain ``def`` (FastAPI runs them in a worker thread; the ORM
is synchronous) and every one is gated by a permission dependency that also
supplies the caller's data scope.  Nothing here filters data by hand — the
scope goes straight into the service, which applies it inside the SQL.
"""

from __future__ import annotations

from datetime import date, datetime, UTC
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query

from app.core.deps import Ctx, Page, get_page, paginated, require
from app.core.i18n import t
from app.schemas.analytics import (
    AbcRow,
    AnomalyDetectRequest,
    AnomalyOut,
    AnomalyResolveRequest,
    BasketPair,
    ChartResponse,
    CorrelationOut,
    ForecastOut,
    ForecastRequest,
    KpiResponse,
    OrderSuggestion,
    PerformanceRow,
    SalesAnalysisResponse,
    StatisticsOut,
    TargetCreate,
    TargetOut,
    TargetRefreshResult,
    TargetUpdate,
    VanLoadSuggestion,
)
from app.schemas.common import Message, PagedResponse
from app.services import analytics_service

router = APIRouter(prefix="/analytics", tags=["analytics"])


def _now() -> datetime:
    return datetime.now(UTC)


# ===========================================================================
# Dashboard
# ===========================================================================
@router.get("/dashboard", response_model=KpiResponse, summary="Dashboard KPIs / Kontrol paneli")
def dashboard(
    on_date: date | None = Query(default=None, description="Reference date, defaults to today"),
    ctx: Ctx = Depends(require("dashboard.main", "VIEW")),
) -> KpiResponse:
    cards = analytics_service.dashboard(ctx.db, on_date=on_date, ctx_scope=ctx.scope)
    return KpiResponse(as_of=on_date or date.today(), generated_at=_now(), cards=cards)


@router.get("/dashboard/charts", response_model=ChartResponse, summary="Dashboard charts / Grafikler")
def dashboard_charts(
    on_date: date | None = Query(default=None),
    days: int = Query(default=30, ge=7, le=365),
    ctx: Ctx = Depends(require("dashboard.main", "VIEW")),
) -> ChartResponse:
    charts = analytics_service.dashboard_charts(ctx.db, on_date=on_date, ctx_scope=ctx.scope, days=days)
    return ChartResponse(as_of=on_date or date.today(), generated_at=_now(), charts=charts)


# ===========================================================================
# Sales analysis & performance
# ===========================================================================
@router.get("/sales", response_model=SalesAnalysisResponse, summary="Sales analysis / Satış analizi")
def sales_analysis(
    start: date | None = Query(default=None),
    end: date | None = Query(default=None),
    group_by: str = Query(default="day", description=", ".join(analytics_service.GROUP_BY_OPTIONS)),
    limit: int = Query(default=200, ge=1, le=1000),
    ctx: Ctx = Depends(require("analytics.reports", "VIEW")),
) -> SalesAnalysisResponse:
    rows = analytics_service.sales_analysis(
        ctx.db, start=start, end=end, group_by=group_by, ctx_scope=ctx.scope, limit=limit
    )
    begin, finish = analytics_service.resolve_window(start, end)
    return SalesAnalysisResponse(
        group_by=group_by.lower(),
        start=begin,
        end=finish,
        total_amount=sum((Decimal(str(r["sales_amount"])) for r in rows), Decimal("0")),
        rows=rows,
    )


@router.get("/products", response_model=list[PerformanceRow], summary="Product performance / Ürün performansı")
def products(
    start: date | None = Query(default=None),
    end: date | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    ctx: Ctx = Depends(require("analytics.reports", "VIEW")),
) -> list[PerformanceRow]:
    rows = analytics_service.product_performance(ctx.db, start=start, end=end, ctx_scope=ctx.scope, limit=limit)
    return [PerformanceRow(**row) for row in rows]


@router.get("/customers", response_model=list[PerformanceRow], summary="Customer performance / Müşteri performansı")
def customers(
    start: date | None = Query(default=None),
    end: date | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    ctx: Ctx = Depends(require("analytics.reports", "VIEW")),
) -> list[PerformanceRow]:
    rows = analytics_service.customer_performance(ctx.db, start=start, end=end, ctx_scope=ctx.scope, limit=limit)
    return [PerformanceRow(**row) for row in rows]


@router.get("/salespersons", response_model=list[PerformanceRow], summary="Salesperson performance / Plasiyer performansı")
def salespersons(
    start: date | None = Query(default=None),
    end: date | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    ctx: Ctx = Depends(require("analytics.reports", "VIEW")),
) -> list[PerformanceRow]:
    rows = analytics_service.salesperson_performance(ctx.db, start=start, end=end, ctx_scope=ctx.scope, limit=limit)
    return [PerformanceRow(**row) for row in rows]


@router.get("/regions", response_model=list[PerformanceRow], summary="Region performance / Bölge performansı")
def regions(
    start: date | None = Query(default=None),
    end: date | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    ctx: Ctx = Depends(require("analytics.reports", "VIEW")),
) -> list[PerformanceRow]:
    rows = analytics_service.region_performance(ctx.db, start=start, end=end, ctx_scope=ctx.scope, limit=limit)
    return [PerformanceRow(**row) for row in rows]


@router.get("/abc", response_model=list[AbcRow], summary="ABC (Pareto) analysis / ABC analizi")
def abc(
    start: date | None = Query(default=None),
    end: date | None = Query(default=None),
    metric: str = Query(default="revenue", description="revenue | volume"),
    limit: int = Query(default=1000, ge=1, le=5000),
    ctx: Ctx = Depends(require("analytics.reports", "VIEW")),
) -> list[AbcRow]:
    rows = analytics_service.abc_analysis(
        ctx.db, start=start, end=end, ctx_scope=ctx.scope, metric=metric, limit=limit
    )
    return [AbcRow(**row) for row in rows]


@router.get("/basket", response_model=list[BasketPair], summary="Basket affinity / Sepet analizi")
def basket(
    start: date | None = Query(default=None),
    end: date | None = Query(default=None),
    min_support: float = Query(default=0.02, ge=0.0, le=1.0),
    limit: int = Query(default=50, ge=1, le=500),
    ctx: Ctx = Depends(require("analytics.reports", "VIEW")),
) -> list[BasketPair]:
    rows = analytics_service.basket_analysis(
        ctx.db, start=start, end=end, ctx_scope=ctx.scope, min_support=min_support, limit=limit
    )
    return [BasketPair(**row) for row in rows]


# ===========================================================================
# Statistics
# ===========================================================================
@router.get("/statistics", response_model=StatisticsOut, summary="Statistics overview / İstatistik özeti")
def statistics(
    start: date | None = Query(default=None),
    end: date | None = Query(default=None),
    ctx: Ctx = Depends(require("analytics.statistics", "VIEW")),
) -> StatisticsOut:
    return StatisticsOut(**analytics_service.statistics_overview(ctx.db, start=start, end=end, ctx_scope=ctx.scope))


@router.get("/correlations", response_model=CorrelationOut, summary="Correlations / Korelasyonlar")
def correlations(
    start: date | None = Query(default=None),
    end: date | None = Query(default=None),
    method: str = Query(default="pearson", description="pearson | spearman"),
    limit: int = Query(default=10, ge=1, le=50),
    ctx: Ctx = Depends(require("analytics.statistics", "VIEW")),
) -> CorrelationOut:
    return CorrelationOut(
        **analytics_service.correlations(
            ctx.db, start=start, end=end, ctx_scope=ctx.scope, limit=limit, method=method
        )
    )


# ===========================================================================
# Forecasting & suggestions
# ===========================================================================
@router.post("/forecast", response_model=ForecastOut, summary="Run a demand forecast / Talep tahmini")
def forecast(
    payload: ForecastRequest,
    ctx: Ctx = Depends(require("analytics.forecasts", "EXECUTE")),
) -> ForecastOut:
    """
    Back-test several methods on the subject's own history, use the winner and
    persist the result so the prediction stays auditable.
    """
    result = analytics_service.forecast_demand(
        ctx.db,
        product_id=payload.product_id,
        customer_id=payload.customer_id,
        salesperson_id=payload.salesperson_id,
        horizon_days=payload.horizon_days,
        granularity=payload.granularity,
        ctx_scope=ctx.scope,
        user_id=ctx.user_id,
        persist=payload.persist,
    )
    return ForecastOut(**result)


@router.get(
    "/forecast/van-load",
    response_model=list[VanLoadSuggestion],
    summary="Suggested van load / Araç yükleme önerisi",
)
def van_load(
    salesperson_id: int = Query(..., ge=1),
    vehicle_id: int | None = Query(default=None, ge=1),
    on_date: date | None = Query(default=None),
    weeks_back: int = Query(default=8, ge=2, le=26),
    ctx: Ctx = Depends(require("analytics.forecasts", "VIEW")),
) -> list[VanLoadSuggestion]:
    rows = analytics_service.suggest_van_load(
        ctx.db,
        salesperson_id=salesperson_id,
        vehicle_id=vehicle_id,
        on_date=on_date,
        weeks_back=weeks_back,
        ctx_scope=ctx.scope,
    )
    return [VanLoadSuggestion(**row) for row in rows]


@router.get(
    "/forecast/order-suggestion",
    response_model=list[OrderSuggestion],
    summary="Suggested order for a customer / Sipariş önerisi",
)
def order_suggestion(
    customer_id: int = Query(..., ge=1),
    on_date: date | None = Query(default=None),
    lookback_days: int = Query(default=180, ge=30, le=730),
    limit: int = Query(default=25, ge=1, le=200),
    ctx: Ctx = Depends(require("analytics.forecasts", "VIEW")),
) -> list[OrderSuggestion]:
    rows = analytics_service.suggest_order(
        ctx.db,
        customer_id=customer_id,
        on_date=on_date,
        lookback_days=lookback_days,
        limit=limit,
        ctx_scope=ctx.scope,
    )
    return [OrderSuggestion(**row) for row in rows]


# ===========================================================================
# Targets
# ===========================================================================
@router.get("/targets", response_model=PagedResponse[TargetOut], summary="List targets / Hedefler")
def list_targets(
    subject_type: str | None = Query(default=None),
    subject_id: int | None = Query(default=None),
    metric: str | None = Query(default=None),
    period: str | None = Query(default=None),
    start: date | None = Query(default=None),
    end: date | None = Query(default=None),
    page: Page = Depends(get_page),
    ctx: Ctx = Depends(require("analytics.targets", "VIEW")),
) -> Any:
    rows, total = analytics_service.list_targets(
        ctx.db,
        subject_type=subject_type,
        subject_id=subject_id,
        metric=metric,
        period=period,
        start=start,
        end=end,
        ctx_scope=ctx.scope,
        offset=page.offset,
        limit=page.limit,
    )
    return paginated([TargetOut.model_validate(row) for row in rows], total, page)


@router.post("/targets", response_model=TargetOut, summary="Create or replace a target / Hedef tanımla")
def create_target(
    payload: TargetCreate,
    ctx: Ctx = Depends(require("analytics.targets", "CREATE")),
) -> TargetOut:
    """Upsert: re-posting the same (subject, metric, period) revises the target."""
    row = analytics_service.create_target(
        ctx.db,
        subject_type=payload.subject_type,
        subject_id=payload.subject_id,
        metric=payload.metric,
        period=payload.period,
        period_start=payload.period_start,
        period_end=payload.period_end,
        target_value=payload.target_value,
        currency=payload.currency,
        notes=payload.notes,
        user_id=ctx.user_id,
        audit=ctx.audit_kwargs(),
    )
    return TargetOut.model_validate(row)


@router.put("/targets/{target_id}", response_model=TargetOut, summary="Update a target / Hedef güncelle")
def update_target(
    target_id: int,
    payload: TargetUpdate,
    ctx: Ctx = Depends(require("analytics.targets", "UPDATE")),
) -> TargetOut:
    row = analytics_service.update_target(
        ctx.db,
        target_id,
        target_value=payload.target_value,
        period_end=payload.period_end,
        notes=payload.notes,
        user_id=ctx.user_id,
        audit=ctx.audit_kwargs(),
    )
    return TargetOut.model_validate(row)


@router.delete("/targets/{target_id}", response_model=Message, summary="Delete a target / Hedef sil")
def delete_target(
    target_id: int,
    ctx: Ctx = Depends(require("analytics.targets", "DELETE")),
) -> Message:
    analytics_service.delete_target(ctx.db, target_id, user_id=ctx.user_id, audit=ctx.audit_kwargs())
    return Message(message=t("common.deleted", ctx.lang), message_key="common.deleted")


@router.post(
    "/targets/refresh",
    response_model=TargetRefreshResult,
    summary="Recompute actuals, projections and risk / Hedefleri yenile",
)
def refresh_targets(
    period_start: date | None = Query(default=None),
    period_end: date | None = Query(default=None),
    ctx: Ctx = Depends(require("analytics.targets", "UPDATE")),
) -> TargetRefreshResult:
    rows = analytics_service.refresh_targets(
        ctx.db, period_start, period_end, ctx_scope=ctx.scope, user_id=ctx.user_id
    )
    resolved_start, resolved_end = analytics_service.target_window(period_start, period_end)
    return TargetRefreshResult(
        refreshed=len(rows),
        period_start=resolved_start,
        period_end=resolved_end,
        targets=[TargetOut.model_validate(row) for row in rows],
    )


# ===========================================================================
# Anomalies
# ===========================================================================
@router.get("/anomalies", response_model=PagedResponse[AnomalyOut], summary="List anomalies / Anomaliler")
def list_anomalies(
    start: date | None = Query(default=None),
    end: date | None = Query(default=None),
    anomaly_type: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    is_resolved: bool | None = Query(default=None),
    page: Page = Depends(get_page),
    ctx: Ctx = Depends(require("analytics.anomalies", "VIEW")),
) -> Any:
    rows, total = analytics_service.list_anomalies(
        ctx.db,
        start=start,
        end=end,
        anomaly_type=anomaly_type,
        severity=severity,
        is_resolved=is_resolved,
        ctx_scope=ctx.scope,
        offset=page.offset,
        limit=page.limit,
    )
    return paginated([AnomalyOut.model_validate(row) for row in rows], total, page)


@router.post(
    "/anomalies/detect",
    response_model=list[AnomalyOut],
    summary="Run anomaly detection / Anomali taraması",
)
def detect_anomalies(
    payload: AnomalyDetectRequest,
    ctx: Ctx = Depends(require("analytics.anomalies", "UPDATE")),
) -> list[AnomalyOut]:
    """Returns only the *newly* created records; existing ones are not duplicated."""
    rows = analytics_service.detect_anomalies(
        ctx.db, start=payload.start, end=payload.end, ctx_scope=ctx.scope, user_id=ctx.user_id
    )
    return [AnomalyOut.model_validate(row) for row in rows]


@router.put(
    "/anomalies/{anomaly_id}/resolve",
    response_model=AnomalyOut,
    summary="Resolve an anomaly / Anomaliyi kapat",
)
def resolve_anomaly(
    anomaly_id: int,
    payload: AnomalyResolveRequest,
    ctx: Ctx = Depends(require("analytics.anomalies", "UPDATE")),
) -> AnomalyOut:
    row = analytics_service.resolve_anomaly(
        ctx.db, anomaly_id, note=payload.note, user_id=ctx.user_id, audit=ctx.audit_kwargs()
    )
    return AnomalyOut.model_validate(row)
