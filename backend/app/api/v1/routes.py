"""
Route planning, optimisation, execution, visits, GPS and the live map.

Literal sub-paths (``/routes/map``, ``/routes/visits`` …) are declared before
``/routes/{route_id}`` so FastAPI matches them first.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query

from app.core.deps import Ctx, Page, get_page, paginated, require, require_any
from app.core.i18n import t
from app.core.utils import display_money
from app.models.customer import Customer
from app.models.route import RouteStop, Visit
from app.models.vehicle import Salesperson, Vehicle
from app.schemas.common import Message, PagedResponse
from app.schemas.route import (
    ArriveIn,
    ArriveOut,
    EfficiencyRow,
    GenerateDailyIn,
    GenerateDailyOut,
    GenerateFromTemplateIn,
    GpsBatchIn,
    GpsBatchOut,
    MapSnapshotOut,
    MultiOptimizeIn,
    MultiOptimizeOut,
    OptimizeIn,
    OptimizeOut,
    PlanVsActualOut,
    RouteCompleteIn,
    RouteCreate,
    RouteListItem,
    RouteOut,
    RouteStartIn,
    RouteStopOut,
    RouteUpdate,
    SkipIn,
    SkipOut,
    VisitCreate,
    VisitOut,
)
from app.services import route_service

router = APIRouter(prefix="/routes", tags=["field"])


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------
def _scope(ctx: Ctx) -> list[int] | None:
    """
    Salesperson ids this caller may see, or ``None`` for unrestricted.

    Fails closed: a scoped user with no salesperson profile gets an impossible
    id rather than the whole company's data.
    """
    if ctx.unrestricted:
        return None
    return ctx.salesperson_ids or [-1]


def _stop_out(stop: RouteStop) -> RouteStopOut:
    customer: Customer | None = stop.customer
    return RouteStopOut(
        id=stop.id,
        customer_id=stop.customer_id,
        customer_code=customer.code if customer else None,
        customer_name=customer.name if customer else None,
        latitude=customer.latitude if customer else None,
        longitude=customer.longitude if customer else None,
        address=customer.address if customer else None,
        phone=(customer.phone or customer.mobile) if customer else None,
        sequence=stop.sequence,
        status=stop.status,
        planned_arrival=stop.planned_arrival,
        planned_departure=stop.planned_departure,
        service_time_minutes=stop.service_time_minutes,
        distance_from_previous_km=stop.distance_from_previous_km,
        travel_time_from_previous_min=stop.travel_time_from_previous_min,
        arrived_at=stop.arrived_at,
        departed_at=stop.departed_at,
        arrival_lat=stop.arrival_lat,
        arrival_lng=stop.arrival_lng,
        geofence_distance_m=stop.geofence_distance_m,
        delay_minutes=stop.delay_minutes,
        skip_reason=stop.skip_reason,
        is_priority=stop.is_priority,
    )


def _route_out(ctx: Ctx, route) -> RouteOut:
    out = RouteOut.model_validate(route)
    out.completion_rate = route.completion_rate
    if route.salesperson_id:
        salesperson = ctx.db.get(Salesperson, route.salesperson_id)
        out.salesperson_name = salesperson.full_name if salesperson else None
    if route.vehicle_id:
        vehicle = ctx.db.get(Vehicle, route.vehicle_id)
        out.vehicle_plate = vehicle.plate_number if vehicle else None
    out.stops = [
        _stop_out(s) for s in sorted(route.stops, key=lambda x: (x.sequence, x.id))
    ]
    return out


def _visit_out(visit: Visit) -> VisitOut:
    out = VisitOut.model_validate(visit)
    if visit.customer is not None:
        out.customer_code = visit.customer.code
        out.customer_name = visit.customer.name
    return out


# ===========================================================================
# Collection endpoints
# ===========================================================================
@router.get("", response_model=PagedResponse[RouteListItem], summary="List routes / Rotalar")
def list_routes(
    ctx: Ctx = Depends(require("field.routes", "VIEW")),
    page: Page = Depends(get_page),
    route_date: date | None = Query(default=None),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    status: str | None = Query(default=None),
    salesperson_id: int | None = Query(default=None),
    vehicle_id: int | None = Query(default=None),
    region_id: int | None = Query(default=None),
    is_template: bool | None = Query(default=None),
    search: str | None = Query(default=None, max_length=128),
) -> PagedResponse[RouteListItem]:
    rows, total = route_service.list_routes(
        ctx.db,
        offset=page.offset,
        limit=page.limit,
        route_date=route_date,
        date_from=date_from,
        date_to=date_to,
        status=status,
        salesperson_id=salesperson_id,
        vehicle_id=vehicle_id,
        region_id=region_id,
        is_template=is_template,
        search=search,
        salesperson_ids=_scope(ctx),
    )
    return PagedResponse[RouteListItem](
        **paginated([RouteListItem.model_validate(r) for r in rows], total, page)
    )


@router.post("", response_model=RouteOut, status_code=201, summary="Create route / Rota oluştur")
def create_route(
    payload: RouteCreate,
    ctx: Ctx = Depends(require("field.routes", "CREATE")),
) -> RouteOut:
    route = route_service.create_route(
        ctx.db,
        data=payload.model_dump(exclude={"stops"}),
        stops=[s.model_dump() for s in payload.stops],
        user_id=ctx.user_id,
        audit=ctx.audit_kwargs(),
    )
    return _route_out(ctx, route)


# ---------------------------------------------------------------------------
# Literal sub-paths — must precede /{route_id}
# ---------------------------------------------------------------------------
@router.post(
    "/optimize-multi",
    response_model=MultiOptimizeOut,
    summary="Optimise several vehicles at once / Çoklu araç optimizasyonu",
)
def optimize_multi(
    payload: MultiOptimizeIn,
    ctx: Ctx = Depends(require("field.routes", "EXECUTE")),
) -> MultiOptimizeOut:
    result = route_service.optimize_multi(
        ctx.db,
        on_date=payload.on_date,
        vehicle_ids=payload.vehicle_ids,
        customer_ids=payload.customer_ids,
        prefer_exact=payload.prefer_exact,
        time_limit_s=payload.time_limit_s,
        region_id=payload.region_id,
        balance=payload.balance,
        user_id=ctx.user_id,
        audit=ctx.audit_kwargs(),
    )
    return MultiOptimizeOut(
        on_date=result["on_date"],
        solver=result["solver"],
        seconds=result["seconds"],
        total_distance_km=result["total_distance_km"],
        objective=result["objective"],
        vehicles_used=result["vehicles_used"],
        vehicles_offered=result["vehicles_offered"],
        routes=[RouteListItem.model_validate(r) for r in result["routes"]],
        unassigned_customer_ids=result["unassigned_customer_ids"],
        message=t(
            "route.optimized",
            ctx.lang,
            stops=sum(r.planned_stops for r in result["routes"]),
            distance=result["total_distance_km"],
        ),
    )


@router.post(
    "/generate-daily",
    response_model=GenerateDailyOut,
    summary="Generate the day's routes / Günlük rotaları oluştur",
)
def generate_daily(
    payload: GenerateDailyIn,
    ctx: Ctx = Depends(require("field.routes", "CREATE")),
) -> GenerateDailyOut:
    result = route_service.generate_daily_routes(
        ctx.db,
        on_date=payload.on_date,
        region_id=payload.region_id,
        salesperson_ids=_scope(ctx),
        user_id=ctx.user_id,
        audit=ctx.audit_kwargs(),
    )
    return GenerateDailyOut(
        on_date=result["on_date"],
        weekday=result["weekday"],
        created=result["created"],
        updated=result["updated"],
        skipped=result["skipped"],
        customers_planned=result["customers_planned"],
        routes=[RouteListItem.model_validate(r) for r in result["routes"]],
        message=t("common.created", ctx.lang),
    )


@router.post(
    "/generate-from-template",
    response_model=RouteOut,
    status_code=201,
    summary="Instantiate a template route / Şablondan rota üret",
)
def generate_from_template(
    payload: GenerateFromTemplateIn,
    ctx: Ctx = Depends(require("field.routes", "CREATE")),
) -> RouteOut:
    route = route_service.generate_from_template(
        ctx.db,
        payload.template_id,
        payload.on_date,
        user_id=ctx.user_id,
        audit=ctx.audit_kwargs(),
    )
    return _route_out(ctx, route)


@router.get("/map", response_model=MapSnapshotOut, summary="Live map snapshot / Canlı harita")
def map_snapshot(
    ctx: Ctx = Depends(require("field.map", "VIEW")),
    on_date: date | None = Query(default=None),
    region_id: int | None = Query(default=None),
    include_customers: bool = Query(default=True),
    customer_limit: int = Query(default=2000, ge=1, le=10000),
) -> MapSnapshotOut:
    snapshot = route_service.map_snapshot(
        ctx.db,
        on_date=on_date or date.today(),
        region_id=region_id,
        salesperson_ids=_scope(ctx),
        include_customers=include_customers,
        customer_limit=customer_limit,
    )
    return MapSnapshotOut.model_validate(snapshot)


@router.get("/visits", response_model=PagedResponse[VisitOut], summary="List visits / Ziyaretler")
def list_visits(
    ctx: Ctx = Depends(require("field.visits", "VIEW")),
    page: Page = Depends(get_page),
    customer_id: int | None = Query(default=None),
    salesperson_id: int | None = Query(default=None),
    route_id: int | None = Query(default=None),
    outcome: str | None = Query(default=None),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
) -> PagedResponse[VisitOut]:
    rows, total = route_service.list_visits(
        ctx.db,
        offset=page.offset,
        limit=page.limit,
        customer_id=customer_id,
        salesperson_id=salesperson_id,
        route_id=route_id,
        outcome=outcome,
        date_from=date_from,
        date_to=date_to,
        salesperson_ids=_scope(ctx),
    )
    return PagedResponse[VisitOut](
        **paginated([_visit_out(v) for v in rows], total, page)
    )


@router.post(
    "/visits",
    response_model=VisitOut,
    status_code=201,
    summary="Record a visit / Ziyaret kaydet",
)
def create_visit(
    payload: VisitCreate,
    ctx: Ctx = Depends(require("field.visits", "CREATE")),
) -> VisitOut:
    data = payload.model_dump()
    scope = _scope(ctx)
    if scope and not data.get("salesperson_id"):
        data["salesperson_id"] = scope[0]
    visit = route_service.record_visit(
        ctx.db, **data, user_id=ctx.user_id, audit=ctx.audit_kwargs()
    )
    return _visit_out(visit)


@router.get(
    "/efficiency",
    response_model=list[EfficiencyRow],
    summary="Field efficiency / Saha verimliliği",
)
def efficiency(
    start: date = Query(...),
    end: date = Query(...),
    ctx: Ctx = Depends(require("field.routes", "VIEW")),
) -> list[EfficiencyRow]:
    rows = route_service.route_efficiency(
        ctx.db, start=start, end=end, salesperson_ids=_scope(ctx)
    )
    return [EfficiencyRow(**row) for row in rows]


@router.post("/gps", response_model=GpsBatchOut, summary="Push GPS breadcrumbs / GPS gönder")
def push_gps(
    payload: GpsBatchIn,
    ctx: Ctx = Depends(require_any(("field.routes", "EXECUTE"), ("field.map", "VIEW"))),
) -> GpsBatchOut:
    result = route_service.record_gps(
        ctx.db,
        vehicle_id=payload.vehicle_id,
        points=[p.model_dump() for p in payload.points],
        salesperson_id=payload.salesperson_id,
        route_id=payload.route_id,
        day_session_id=payload.day_session_id,
    )
    return GpsBatchOut(**result)


# ===========================================================================
# Single-route endpoints
# ===========================================================================
@router.get("/{route_id}", response_model=RouteOut, summary="Route detail / Rota detayı")
def get_route(
    route_id: int,
    ctx: Ctx = Depends(require("field.routes", "VIEW")),
) -> RouteOut:
    route = route_service.get_route(ctx.db, route_id, salesperson_ids=_scope(ctx))
    return _route_out(ctx, route)


@router.put("/{route_id}", response_model=RouteOut, summary="Update route / Rotayı güncelle")
def update_route(
    route_id: int,
    payload: RouteUpdate,
    ctx: Ctx = Depends(require("field.routes", "UPDATE")),
) -> RouteOut:
    route = route_service.get_route(ctx.db, route_id, salesperson_ids=_scope(ctx))
    stops = payload.stops
    route = route_service.update_route(
        ctx.db,
        route,
        data=payload.model_dump(exclude={"stops"}, exclude_none=True),
        stops=[s.model_dump() for s in stops] if stops is not None else None,
        user_id=ctx.user_id,
        audit=ctx.audit_kwargs(),
    )
    return _route_out(ctx, route)


@router.delete("/{route_id}", response_model=Message, summary="Delete route / Rotayı sil")
def delete_route(
    route_id: int,
    ctx: Ctx = Depends(require("field.routes", "DELETE")),
) -> Message:
    route = route_service.get_route(ctx.db, route_id, salesperson_ids=_scope(ctx))
    route_service.delete_route(
        ctx.db, route, user_id=ctx.user_id, audit=ctx.audit_kwargs()
    )
    return Message(message=t("common.deleted", ctx.lang), message_key="common.deleted")


@router.post(
    "/{route_id}/optimize",
    response_model=OptimizeOut,
    summary="Optimise stop order / Rotayı optimize et",
)
def optimize_route(
    route_id: int,
    payload: OptimizeIn | None = None,
    ctx: Ctx = Depends(require("field.routes", "EXECUTE")),
) -> OptimizeOut:
    options = payload or OptimizeIn()
    route = route_service.get_route(ctx.db, route_id, salesperson_ids=_scope(ctx))
    result = route_service.optimize_route(
        ctx.db,
        route,
        prefer_exact=options.prefer_exact,
        time_limit_s=options.time_limit_s,
        user_id=ctx.user_id,
        audit=ctx.audit_kwargs(),
    )
    return OptimizeOut(
        **result,
        message=t(
            "route.optimized",
            ctx.lang,
            stops=result["stops"],
            distance=display_money(result["distance_km"]),
        ),
    )


@router.post("/{route_id}/start", response_model=RouteOut, summary="Start route / Rotayı başlat")
def start_route(
    route_id: int,
    payload: RouteStartIn | None = None,
    ctx: Ctx = Depends(require("field.routes", "EXECUTE")),
) -> RouteOut:
    options = payload or RouteStartIn()
    route = route_service.get_route(ctx.db, route_id, salesperson_ids=_scope(ctx))
    route = route_service.start_route(
        ctx.db,
        route,
        latitude=options.latitude,
        longitude=options.longitude,
        odometer_km=options.odometer_km,
        user_id=ctx.user_id,
        audit=ctx.audit_kwargs(),
    )
    return _route_out(ctx, route)


@router.post(
    "/{route_id}/complete",
    response_model=RouteOut,
    summary="Complete route / Rotayı tamamla",
)
def complete_route(
    route_id: int,
    payload: RouteCompleteIn | None = None,
    ctx: Ctx = Depends(require("field.routes", "EXECUTE")),
) -> RouteOut:
    options = payload or RouteCompleteIn()
    route = route_service.get_route(ctx.db, route_id, salesperson_ids=_scope(ctx))
    route = route_service.complete_route(
        ctx.db,
        route,
        actual_distance_km=options.actual_distance_km,
        odometer_km=options.odometer_km,
        notes=options.notes,
        user_id=ctx.user_id,
        audit=ctx.audit_kwargs(),
    )
    return _route_out(ctx, route)


@router.post(
    "/{route_id}/stops/{stop_id}/arrive",
    response_model=ArriveOut,
    summary="Arrive at a stop / Durağa varış",
)
def arrive_at_stop(
    route_id: int,
    stop_id: int,
    payload: ArriveIn | None = None,
    ctx: Ctx = Depends(require("field.routes", "EXECUTE")),
) -> ArriveOut:
    options = payload or ArriveIn()
    route = route_service.get_route(ctx.db, route_id, salesperson_ids=_scope(ctx))
    result = route_service.arrive_at_stop(
        ctx.db,
        route,
        stop_id,
        latitude=options.latitude,
        longitude=options.longitude,
        user_id=ctx.user_id,
        audit=ctx.audit_kwargs(),
    )
    return ArriveOut(**result)


@router.post(
    "/{route_id}/stops/{stop_id}/skip",
    response_model=SkipOut,
    summary="Skip a stop / Durağı atla",
)
def skip_stop(
    route_id: int,
    stop_id: int,
    payload: SkipIn,
    ctx: Ctx = Depends(require("field.routes", "EXECUTE")),
) -> SkipOut:
    route = route_service.get_route(ctx.db, route_id, salesperson_ids=_scope(ctx))
    result = route_service.skip_stop(
        ctx.db,
        route,
        stop_id,
        reason=payload.reason,
        latitude=payload.latitude,
        longitude=payload.longitude,
        user_id=ctx.user_id,
        audit=ctx.audit_kwargs(),
    )
    return SkipOut(**result)


@router.get(
    "/{route_id}/plan-vs-actual",
    response_model=PlanVsActualOut,
    summary="Plan vs actual / Plan-gerçekleşme",
)
def plan_vs_actual(
    route_id: int,
    ctx: Ctx = Depends(require("field.routes", "VIEW")),
) -> PlanVsActualOut:
    route = route_service.get_route(ctx.db, route_id, salesperson_ids=_scope(ctx))
    return PlanVsActualOut.model_validate(route_service.plan_vs_actual(ctx.db, route))


@router.get(
    "/{route_id}/stops",
    response_model=list[RouteStopOut],
    summary="Route stops / Rota durakları",
)
def route_stops(
    route_id: int,
    ctx: Ctx = Depends(require("field.routes", "VIEW")),
) -> list[RouteStopOut]:
    route = route_service.get_route(ctx.db, route_id, salesperson_ids=_scope(ctx))
    return [_stop_out(s) for s in sorted(route.stops, key=lambda x: (x.sequence, x.id))]
