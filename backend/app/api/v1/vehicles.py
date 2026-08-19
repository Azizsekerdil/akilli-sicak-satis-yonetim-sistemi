"""
Field operations API: vehicles, salespeople, day sessions and van load-outs.

Literal sub-paths (``/salespersons``, ``/day-sessions``, ``/loads``) are
declared before ``/{vehicle_id}`` so they are matched as routes and never as a
vehicle id.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, select

from app.core.deps import Ctx, Page, get_page, paginated, require, require_any
from app.core.exceptions import NotFoundError, PermissionDeniedError
from app.core.i18n import t
from app.core.utils import D, money
from app.models.product import Product
from app.models.vehicle import DaySession, Salesperson, VanLoad, Vehicle
from app.schemas.common import Message, PagedResponse
from app.schemas.vehicle import (
    AssignVehicleIn,
    CapacityOut,
    CloseDayIn,
    DaySessionOut,
    MaintenanceWarningOut,
    OpenDayIn,
    PositionIn,
    PositionOut,
    ReconciliationOut,
    ReconciliationRow,
    SalespersonCreate,
    SalespersonOut,
    SalespersonUpdate,
    UnloadIn,
    UnloadOut,
    VanLoadCreate,
    VanLoadOut,
    VanStockRow,
    VehicleCreate,
    VehicleOut,
    VehicleUpdate,
)
from app.services import (
    day_session_service,
    stock_service,
    van_load_service,
    vehicle_service,
)

router = APIRouter(prefix="/vehicles", tags=["field"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _count(ctx: Ctx, stmt: Any) -> int:
    return int(
        ctx.db.execute(
            select(func.count()).select_from(stmt.order_by(None).subquery())
        ).scalar_one()
    )


def _scoped_ids(ctx: Ctx) -> list[int] | None:
    """Salespeople this caller may see, or ``None`` when unrestricted."""
    if ctx.unrestricted:
        return None
    return ctx.salesperson_ids or [-1]


def _assert_salesperson_allowed(ctx: Ctx, salesperson_id: int | None) -> None:
    allowed = _scoped_ids(ctx)
    if allowed is None or salesperson_id is None:
        return
    if salesperson_id not in allowed:
        raise PermissionDeniedError(
            "auth.permission_denied", params={"salesperson_id": salesperson_id}
        )


def _counted_to_base(ctx: Ctx, payload: CloseDayIn) -> dict[int, Decimal] | None:
    """Turn counted lines into base units — the ledger's only unit."""
    if payload.counted is None:
        return None
    counted: dict[int, Decimal] = {}
    for line in payload.counted:
        product = ctx.db.get(Product, line.product_id)
        if product is None:
            raise NotFoundError("error.not_found", params={"product_id": line.product_id})
        base = stock_service.to_base(product, D(line.quantity), line.uom or product.base_uom)
        counted[product.id] = counted.get(product.id, D(0)) + D(base)
    return counted


# ===========================================================================
# Vehicles — collection
# ===========================================================================
@router.get("", response_model=PagedResponse[VehicleOut], summary="List vehicles")
def list_vehicles(
    ctx: Ctx = Depends(require("field.vehicles", "VIEW")),
    page: Page = Depends(get_page),
    search: str | None = Query(default=None, max_length=64),
    status_filter: str | None = Query(default=None, alias="status"),
    vehicle_type: str | None = Query(default=None),
    region_id: int | None = Query(default=None),
    is_active: bool | None = Query(default=None),
) -> dict[str, Any]:
    stmt = vehicle_service.list_vehicles_query(
        ctx.db,
        search=search,
        status=status_filter,
        vehicle_type=vehicle_type,
        region_id=region_id,
        is_active=is_active,
        salesperson_ids=_scoped_ids(ctx),
    )
    total = _count(ctx, stmt)
    rows = ctx.db.execute(
        stmt.order_by(Vehicle.plate_number).offset(page.offset).limit(page.limit)
    ).scalars().all()
    return paginated([VehicleOut.model_validate(r) for r in rows], total, page)


@router.post(
    "",
    response_model=VehicleOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a vehicle (auto-provisions its warehouse)",
)
def create_vehicle(
    payload: VehicleCreate,
    ctx: Ctx = Depends(require("field.vehicles", "CREATE")),
) -> VehicleOut:
    data = payload.model_dump(exclude_none=True)
    vehicle = vehicle_service.create_vehicle(
        ctx.db,
        plate_number=data.pop("plate_number"),
        user_id=ctx.user_id,
        **data,
    )
    return VehicleOut.model_validate(vehicle)


@router.get(
    "/maintenance-warnings",
    response_model=list[MaintenanceWarningOut],
    summary="Insurance / inspection documents expiring soon",
)
def maintenance_warnings(
    ctx: Ctx = Depends(require("field.vehicles", "VIEW")),
    within_days: int = Query(default=vehicle_service.EXPIRY_WARNING_DAYS, ge=0, le=365),
    vehicle_id: int | None = Query(default=None),
) -> list[MaintenanceWarningOut]:
    rows = vehicle_service.maintenance_warnings(
        ctx.db, within_days=within_days, vehicle_id=vehicle_id
    )
    return [MaintenanceWarningOut.model_validate(r) for r in rows]


# ===========================================================================
# Salespeople
# ===========================================================================
@router.get(
    "/salespersons",
    response_model=PagedResponse[SalespersonOut],
    summary="List salespeople",
)
def list_salespersons(
    ctx: Ctx = Depends(require("field.salespersons", "VIEW")),
    page: Page = Depends(get_page),
    search: str | None = Query(default=None, max_length=64),
    region_id: int | None = Query(default=None),
    supervisor_id: int | None = Query(default=None),
    is_active: bool | None = Query(default=None),
) -> dict[str, Any]:
    stmt = vehicle_service.list_salespersons_query(
        ctx.db,
        search=search,
        region_id=region_id,
        supervisor_id=supervisor_id,
        is_active=is_active,
        salesperson_ids=_scoped_ids(ctx),
    )
    total = _count(ctx, stmt)
    rows = ctx.db.execute(
        stmt.order_by(Salesperson.full_name).offset(page.offset).limit(page.limit)
    ).scalars().all()
    return paginated([SalespersonOut.model_validate(r) for r in rows], total, page)


@router.post(
    "/salespersons",
    response_model=SalespersonOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a salesperson",
)
def create_salesperson(
    payload: SalespersonCreate,
    ctx: Ctx = Depends(require("field.salespersons", "CREATE")),
) -> SalespersonOut:
    data = payload.model_dump(exclude_none=True)
    person = vehicle_service.create_salesperson(
        ctx.db,
        full_name=data.pop("full_name"),
        link_user_id=data.pop("user_id", None),
        user_id=ctx.user_id,
        **data,
    )
    return SalespersonOut.model_validate(person)


@router.get(
    "/salespersons/{salesperson_id}",
    response_model=SalespersonOut,
    summary="Salesperson detail",
)
def get_salesperson(
    salesperson_id: int,
    ctx: Ctx = Depends(require("field.salespersons", "VIEW")),
) -> SalespersonOut:
    person = vehicle_service.get_salesperson(ctx.db, salesperson_id)
    _assert_salesperson_allowed(ctx, person.id)
    return SalespersonOut.model_validate(person)


@router.put(
    "/salespersons/{salesperson_id}",
    response_model=SalespersonOut,
    summary="Update a salesperson",
)
def update_salesperson(
    salesperson_id: int,
    payload: SalespersonUpdate,
    ctx: Ctx = Depends(require("field.salespersons", "UPDATE")),
) -> SalespersonOut:
    person = vehicle_service.get_salesperson(ctx.db, salesperson_id)
    person = vehicle_service.update_salesperson(
        ctx.db, person, payload.model_dump(exclude_unset=True), user_id=ctx.user_id
    )
    return SalespersonOut.model_validate(person)


@router.delete(
    "/salespersons/{salesperson_id}",
    response_model=Message,
    summary="Deactivate a salesperson",
)
def delete_salesperson(
    salesperson_id: int,
    ctx: Ctx = Depends(require("field.salespersons", "DELETE")),
) -> Message:
    person = vehicle_service.get_salesperson(ctx.db, salesperson_id)
    vehicle_service.delete_salesperson(ctx.db, person, user_id=ctx.user_id)
    return Message(message=t("common.deleted", ctx.lang), message_key="common.deleted")


# ===========================================================================
# Day sessions
# ===========================================================================
@router.get(
    "/day-sessions",
    response_model=PagedResponse[DaySessionOut],
    summary="List day sessions",
)
def list_day_sessions(
    ctx: Ctx = Depends(require("field.day_session", "VIEW")),
    page: Page = Depends(get_page),
    salesperson_id: int | None = Query(default=None),
    vehicle_id: int | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    has_variance: bool | None = Query(default=None),
) -> dict[str, Any]:
    stmt = day_session_service.list_sessions_query(
        ctx.db,
        salesperson_id=salesperson_id,
        vehicle_id=vehicle_id,
        status=status_filter,
        date_from=date_from,
        date_to=date_to,
        has_variance=has_variance,
        salesperson_ids=_scoped_ids(ctx),
    )
    total = _count(ctx, stmt)
    rows = ctx.db.execute(
        stmt.order_by(DaySession.session_date.desc(), DaySession.id.desc())
        .offset(page.offset)
        .limit(page.limit)
    ).scalars().all()
    return paginated([DaySessionOut.model_validate(r) for r in rows], total, page)


@router.post(
    "/day-sessions/open",
    response_model=DaySessionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Open the working day / Gün başlat",
)
def open_day(
    payload: OpenDayIn,
    ctx: Ctx = Depends(require("field.day_session", "CREATE")),
) -> DaySessionOut:
    _assert_salesperson_allowed(ctx, payload.salesperson_id)
    session = day_session_service.open_day(
        ctx.db,
        salesperson_id=payload.salesperson_id,
        vehicle_id=payload.vehicle_id,
        route_id=payload.route_id,
        start_odometer=payload.start_odometer,
        session_date=payload.session_date,
        notes=payload.notes,
        user_id=ctx.user_id,
    )
    return DaySessionOut.model_validate(session)


@router.get(
    "/day-sessions/{session_id}",
    response_model=DaySessionOut,
    summary="Day session detail",
)
def get_day_session(
    session_id: int,
    ctx: Ctx = Depends(require("field.day_session", "VIEW")),
    refresh: bool = Query(default=False, description="Recompute totals from the ledger"),
) -> DaySessionOut:
    session = day_session_service.get_session(ctx.db, session_id)
    _assert_salesperson_allowed(ctx, session.salesperson_id)
    if refresh:
        day_session_service.recalculate(ctx.db, session, commit=True)
    return DaySessionOut.model_validate(session)


@router.get(
    "/day-sessions/{session_id}/reconciliation",
    response_model=ReconciliationOut,
    summary="End-of-day reconciliation / Gün sonu mutabakatı",
)
def reconciliation(
    session_id: int,
    ctx: Ctx = Depends(require("field.day_session", "VIEW")),
) -> ReconciliationOut:
    session = day_session_service.get_session(ctx.db, session_id)
    _assert_salesperson_allowed(ctx, session.salesperson_id)

    summary = day_session_service.recalculate(ctx.db, session, commit=True)
    rows = [ReconciliationRow.model_validate(row) for row in summary["rows"]]
    return ReconciliationOut(
        session=DaySessionOut.model_validate(session),
        rows=rows,
        total_variance_qty=session.variance_qty,
        total_variance_value=session.variance_value,
        cash_expected=session.total_collected_cash,
        cash_declared=session.declared_cash,
        cash_variance=session.cash_variance,
    )


@router.post(
    "/day-sessions/{session_id}/close",
    response_model=DaySessionOut,
    summary="Close the day against a van count / Gün kapat",
)
def close_day(
    session_id: int,
    payload: CloseDayIn,
    ctx: Ctx = Depends(require("field.day_session", "UPDATE")),
) -> DaySessionOut:
    session = day_session_service.get_session(ctx.db, session_id)
    _assert_salesperson_allowed(ctx, session.salesperson_id)
    counted = _counted_to_base(ctx, payload)
    session = day_session_service.close_day(
        ctx.db,
        session,
        counted=counted,
        declared_cash=money(payload.declared_cash),
        end_odometer=payload.end_odometer,
        notes=payload.notes,
        user_id=ctx.user_id,
    )
    return DaySessionOut.model_validate(session)


# ===========================================================================
# Van loads
# ===========================================================================
@router.get("/loads", response_model=PagedResponse[VanLoadOut], summary="List van loads")
def list_loads(
    ctx: Ctx = Depends(require("stock.van_load", "VIEW")),
    page: Page = Depends(get_page),
    vehicle_id: int | None = Query(default=None),
    salesperson_id: int | None = Query(default=None),
    day_session_id: int | None = Query(default=None),
    source_warehouse_id: int | None = Query(default=None),
    is_posted: bool | None = Query(default=None),
    is_reload: bool | None = Query(default=None),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
) -> dict[str, Any]:
    stmt = van_load_service.list_loads_query(
        ctx.db,
        vehicle_id=vehicle_id,
        salesperson_id=salesperson_id,
        day_session_id=day_session_id,
        source_warehouse_id=source_warehouse_id,
        is_posted=is_posted,
        is_reload=is_reload,
        date_from=date_from,
        date_to=date_to,
        salesperson_ids=_scoped_ids(ctx),
    )
    total = _count(ctx, stmt)
    rows = ctx.db.execute(
        stmt.order_by(VanLoad.load_date.desc(), VanLoad.id.desc())
        .offset(page.offset)
        .limit(page.limit)
    ).scalars().all()
    return paginated([VanLoadOut.model_validate(r) for r in rows], total, page)


@router.post(
    "/loads",
    response_model=VanLoadOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a van load-out (draft)",
)
def create_load(
    payload: VanLoadCreate,
    ctx: Ctx = Depends(require("stock.van_load", "CREATE")),
) -> VanLoadOut:
    _assert_salesperson_allowed(ctx, payload.salesperson_id)
    load = van_load_service.create_load(
        ctx.db,
        vehicle_id=payload.vehicle_id,
        salesperson_id=payload.salesperson_id,
        source_warehouse_id=payload.source_warehouse_id,
        lines=[line.model_dump() for line in payload.lines],
        load_date=payload.load_date,
        is_reload=payload.is_reload,
        day_session_id=payload.day_session_id,
        notes=payload.notes,
        user_id=ctx.user_id,
    )
    return VanLoadOut.model_validate(load)


@router.get("/loads/{load_id}", response_model=VanLoadOut, summary="Van load detail")
def get_load(
    load_id: int,
    ctx: Ctx = Depends(require("stock.van_load", "VIEW")),
) -> VanLoadOut:
    load = van_load_service.get_load(ctx.db, load_id)
    _assert_salesperson_allowed(ctx, load.salesperson_id)
    return VanLoadOut.model_validate(load)


@router.post(
    "/loads/{load_id}/post",
    response_model=VanLoadOut,
    summary="Post a load-out to the stock ledger",
)
def post_load(
    load_id: int,
    ctx: Ctx = Depends(require("stock.van_load", "EXECUTE")),
) -> VanLoadOut:
    load = van_load_service.get_load(ctx.db, load_id)
    _assert_salesperson_allowed(ctx, load.salesperson_id)
    load = van_load_service.post_load(ctx.db, load, user_id=ctx.user_id)
    return VanLoadOut.model_validate(load)


@router.post(
    "/unload",
    response_model=UnloadOut,
    summary="Return van stock to a depot / Araç boşaltma",
)
def unload(
    payload: UnloadIn,
    ctx: Ctx = Depends(require("stock.van_load", "EXECUTE")),
) -> UnloadOut:
    _assert_salesperson_allowed(ctx, payload.salesperson_id)
    result = van_load_service.unload(
        ctx.db,
        vehicle_id=payload.vehicle_id,
        target_warehouse_id=payload.target_warehouse_id,
        lines=[line.model_dump() for line in payload.lines] if payload.lines else None,
        salesperson_id=payload.salesperson_id,
        day_session_id=payload.day_session_id,
        user_id=ctx.user_id,
    )
    return UnloadOut.model_validate(result)


# ===========================================================================
# Vehicles — single record (declared last: /{vehicle_id} must not shadow the
# literal sub-paths above)
# ===========================================================================
@router.get("/{vehicle_id}", response_model=VehicleOut, summary="Vehicle detail")
def get_vehicle(
    vehicle_id: int,
    ctx: Ctx = Depends(require("field.vehicles", "VIEW")),
) -> VehicleOut:
    return VehicleOut.model_validate(vehicle_service.get_vehicle(ctx.db, vehicle_id))


@router.put("/{vehicle_id}", response_model=VehicleOut, summary="Update a vehicle")
def update_vehicle(
    vehicle_id: int,
    payload: VehicleUpdate,
    ctx: Ctx = Depends(require("field.vehicles", "UPDATE")),
) -> VehicleOut:
    vehicle = vehicle_service.get_vehicle(ctx.db, vehicle_id)
    vehicle = vehicle_service.update_vehicle(
        ctx.db, vehicle, payload.model_dump(exclude_unset=True), user_id=ctx.user_id
    )
    return VehicleOut.model_validate(vehicle)


@router.delete("/{vehicle_id}", response_model=Message, summary="Retire a vehicle")
def delete_vehicle(
    vehicle_id: int,
    ctx: Ctx = Depends(require("field.vehicles", "DELETE")),
) -> Message:
    vehicle = vehicle_service.get_vehicle(ctx.db, vehicle_id)
    vehicle_service.delete_vehicle(ctx.db, vehicle, user_id=ctx.user_id)
    return Message(message=t("common.deleted", ctx.lang), message_key="common.deleted")


@router.post(
    "/{vehicle_id}/assign",
    response_model=VehicleOut,
    summary="Assign the van to a salesperson",
)
def assign_vehicle(
    vehicle_id: int,
    payload: AssignVehicleIn,
    ctx: Ctx = Depends(require("field.vehicles", "UPDATE")),
) -> VehicleOut:
    vehicle = vehicle_service.assign(
        ctx.db, vehicle_id, payload.salesperson_id, user_id=ctx.user_id
    )
    return VehicleOut.model_validate(vehicle)


@router.get(
    "/{vehicle_id}/stock",
    response_model=list[VanStockRow],
    summary="Current van stock / Araç stoğu",
)
def vehicle_stock(
    vehicle_id: int,
    ctx: Ctx = Depends(
        require_any(("stock.vehicle_stock", "VIEW"), ("field.vehicles", "VIEW"))
    ),
) -> list[VanStockRow]:
    rows = vehicle_service.van_stock(ctx.db, vehicle_id)
    return [VanStockRow.model_validate(r) for r in rows]


@router.get(
    "/{vehicle_id}/capacity",
    response_model=CapacityOut,
    summary="Van capacity usage",
)
def vehicle_capacity(
    vehicle_id: int,
    ctx: Ctx = Depends(
        require_any(("stock.vehicle_stock", "VIEW"), ("field.vehicles", "VIEW"))
    ),
) -> CapacityOut:
    return CapacityOut.model_validate(vehicle_service.capacity_usage(ctx.db, vehicle_id))


@router.post(
    "/{vehicle_id}/position",
    response_model=PositionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Record a GPS position",
)
def record_position(
    vehicle_id: int,
    payload: PositionIn,
    ctx: Ctx = Depends(
        require_any(
            ("field.vehicles", "UPDATE"),
            ("field.day_session", "UPDATE"),
            ("field.map", "VIEW"),
        )
    ),
) -> PositionOut:
    event = vehicle_service.update_position(
        ctx.db,
        vehicle_id,
        payload.latitude,
        payload.longitude,
        salesperson_id=payload.salesperson_id,
        day_session_id=payload.day_session_id,
        route_id=payload.route_id,
        accuracy_m=payload.accuracy_m,
        speed_kmh=payload.speed_kmh,
        heading=payload.heading,
        altitude_m=payload.altitude_m,
        battery_percent=payload.battery_percent,
        event_type=payload.event_type,
        recorded_at=payload.recorded_at,
        odometer_km=payload.odometer_km,
    )
    return PositionOut.model_validate(event)
