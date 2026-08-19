"""
Route planning, optimisation, execution and field telemetry.

Business shape
--------------
A **template** route is the recurring master plan ("Kadıköy — Monday").  Dated
**instances** are generated from templates or from each customer's visit
calendar, and those are what the field app executes: start → arrive → sell →
complete.  Optimisation rewrites the stop order of an instance; it never
touches a template's membership, only its sequence.

Everything geographic is approximate on purpose (haversine × detour factor):
the product must run without an external routing service, and stop *ordering*
is insensitive to the few percent of error that introduces.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.enums import (
    AuditAction,
    CustomerStatus,
    OrderStatus,
    RouteStatus,
    StopStatus,
    VisitFrequency,
    VisitOutcome,
    WarehouseType,
)
from app.core.exceptions import (
    BusinessRuleError,
    NotFoundError,
    OptimizationError,
    ValidationError,
)
from app.core.logging_config import get_logger
from app.core.utils import (
    D,
    format_hhmm,
    haversine_km,
    money,
    parse_hhmm,
    pct,
    safe_div,
    weekday_code,
)
from app.models.base import utcnow
from app.models.customer import Customer
from app.models.route import GpsEvent, Route, RouteStop, Visit
from app.models.sales import Order, Sale
from app.models.system import Setting
from app.models.vehicle import DaySession, Salesperson, Vehicle
from app.models.warehouse import Warehouse
from app.routing import VrpProblem, VrpStop, VrpVehicle, centroid, minutes_for, optimize, pair_km
from app.services import audit_service, numbering_service

log = get_logger("app.routing.service")

#: Fallbacks used when the ``route`` settings category has not been seeded.
DEFAULT_ROUTE_SETTINGS: dict[str, float] = {
    "avg_speed_kmh": 30.0,
    "road_detour_factor": 1.35,
    "geofence_radius_m": 150.0,
    "workday_minutes": 540.0,
}

#: A stop counts as "delayed" only past this much drift, so normal traffic
#: noise does not flood the supervisor's exception list.
DELAY_TOLERANCE_MIN: int = 15

#: Statuses a route may be started from.
_STARTABLE = (RouteStatus.PLANNED, RouteStatus.OPTIMIZED, RouteStatus.ASSIGNED)


# ===========================================================================
# Small helpers
# ===========================================================================
def _local_now() -> datetime:
    """Wall-clock time in the company's timezone — route plans are local time."""
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(settings.timezone))
    except Exception:  # pragma: no cover - depends on OS tz database
        return datetime.now()


def _local_minutes(moment: datetime | None = None) -> int:
    m = moment or _local_now()
    return m.hour * 60 + m.minute


def route_settings(db: Session) -> dict[str, float]:
    """Read the operator-tunable routing parameters, falling back to defaults."""
    out = dict(DEFAULT_ROUTE_SETTINGS)
    rows = db.execute(select(Setting).where(Setting.category == "route")).scalars().all()
    for row in rows:
        if row.key not in out:
            continue
        raw = row.value if row.value not in (None, "") else row.default_value
        try:
            out[row.key] = float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
    return out


def _warehouse_point(db: Session, warehouse_id: int | None) -> tuple[float, float] | None:
    if not warehouse_id:
        return None
    wh = db.get(Warehouse, warehouse_id)
    if wh and wh.latitude is not None and wh.longitude is not None:
        return (wh.latitude, wh.longitude)
    return None


def _default_depot(db: Session) -> tuple[float, float] | None:
    """First fixed warehouse with coordinates — the company's operating centre."""
    row = db.execute(
        select(Warehouse.latitude, Warehouse.longitude)
        .where(
            Warehouse.is_deleted.is_(False),
            Warehouse.latitude.is_not(None),
            Warehouse.longitude.is_not(None),
            Warehouse.warehouse_type != WarehouseType.VEHICLE,
        )
        .order_by(Warehouse.warehouse_type == WarehouseType.CENTRAL, Warehouse.id)
        .limit(1)
    ).first()
    return (row[0], row[1]) if row else None


def _scoped(stmt: Select, column: Any, salesperson_ids: list[int] | None) -> Select:
    """Apply salesperson data scoping when the caller is not unrestricted."""
    if salesperson_ids:
        return stmt.where(column.in_(salesperson_ids))
    return stmt


def _audit(
    db: Session,
    action: str,
    *,
    audit: dict[str, Any] | None,
    user_id: int | None,
    **kwargs: Any,
) -> None:
    payload = dict(audit or {})
    if user_id is not None:
        payload.setdefault("user_id", user_id)
    audit_service.record(db, action, **payload, **kwargs)


# ===========================================================================
# Route CRUD
# ===========================================================================
def get_route(
    db: Session,
    route_id: int,
    *,
    salesperson_ids: list[int] | None = None,
) -> Route:
    route = db.get(Route, route_id)
    if route is None or route.is_deleted:
        raise NotFoundError("route.not_found", params={"id": route_id})
    if salesperson_ids and route.salesperson_id not in salesperson_ids:
        raise NotFoundError("route.not_found", params={"id": route_id})
    return route


def list_routes(
    db: Session,
    *,
    offset: int = 0,
    limit: int = 50,
    route_date: date | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    status: str | None = None,
    salesperson_id: int | None = None,
    vehicle_id: int | None = None,
    region_id: int | None = None,
    is_template: bool | None = None,
    search: str | None = None,
    salesperson_ids: list[int] | None = None,
) -> tuple[list[Route], int]:
    """Filtered, scoped, paginated route list plus the unpaginated total."""
    stmt = select(Route).where(Route.is_deleted.is_(False))
    if route_date is not None:
        stmt = stmt.where(Route.route_date == route_date)
    if date_from is not None:
        stmt = stmt.where(Route.route_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(Route.route_date <= date_to)
    if status:
        stmt = stmt.where(Route.status == status)
    if salesperson_id is not None:
        stmt = stmt.where(Route.salesperson_id == salesperson_id)
    if vehicle_id is not None:
        stmt = stmt.where(Route.vehicle_id == vehicle_id)
    if region_id is not None:
        stmt = stmt.where(Route.region_id == region_id)
    if is_template is not None:
        stmt = stmt.where(Route.is_template.is_(is_template))
    if search:
        term = f"%{search.lower()}%"
        stmt = stmt.where(
            or_(func.lower(Route.code).like(term), func.lower(Route.name).like(term))
        )
    stmt = _scoped(stmt, Route.salesperson_id, salesperson_ids)

    total = db.execute(
        select(func.count()).select_from(stmt.subquery())
    ).scalar_one()
    rows = db.execute(
        stmt.order_by(Route.route_date.desc().nullslast(), Route.id.desc())
        .offset(offset)
        .limit(limit)
    ).scalars().all()
    return list(rows), int(total)


def create_route(
    db: Session,
    *,
    data: dict[str, Any],
    stops: list[dict[str, Any]] | None = None,
    user_id: int | None = None,
    audit: dict[str, Any] | None = None,
    commit: bool = True,
) -> Route:
    """Create a template or a dated route instance together with its stops."""
    payload = dict(data)
    code = (payload.pop("code", None) or "").strip() or numbering_service.next_number(db, "ROUTE")
    name = (payload.pop("name", None) or "").strip()
    if not name:
        raise ValidationError("validation.required", params={"field": "name"})

    route = Route(code=code, name=name, created_by_id=user_id, updated_by_id=user_id)
    for key in (
        "description", "is_template", "template_id", "route_date", "weekday",
        "salesperson_id", "vehicle_id", "region_id", "start_warehouse_id",
        "end_warehouse_id", "planned_start_time", "is_active",
    ):
        if key in payload and payload[key] is not None:
            setattr(route, key, payload[key])
    if route.route_date and not route.weekday:
        route.weekday = weekday_code(route.route_date)

    db.add(route)
    db.flush()

    if stops:
        _replace_stops(db, route, stops)
    route.planned_stops = len(route.stops)

    _audit(
        db, AuditAction.CREATE,
        audit=audit, user_id=user_id,
        entity_type="Route", entity_id=route.id, entity_label=route.code,
        summary=f"route created stops={route.planned_stops}",
        new_values={
            "code": route.code, "name": route.name,
            "route_date": route.route_date, "salesperson_id": route.salesperson_id,
            "vehicle_id": route.vehicle_id, "is_template": route.is_template,
        },
    )
    if commit:
        db.commit()
    return route


def update_route(
    db: Session,
    route: Route,
    *,
    data: dict[str, Any],
    stops: list[dict[str, Any]] | None = None,
    user_id: int | None = None,
    audit: dict[str, Any] | None = None,
    commit: bool = True,
) -> Route:
    before = {
        "name": route.name, "status": route.status,
        "salesperson_id": route.salesperson_id, "vehicle_id": route.vehicle_id,
        "route_date": route.route_date, "planned_start_time": route.planned_start_time,
    }
    for key in (
        "name", "description", "route_date", "weekday", "salesperson_id",
        "vehicle_id", "region_id", "start_warehouse_id", "end_warehouse_id",
        "planned_start_time", "status", "is_active",
    ):
        if key in data and data[key] is not None:
            setattr(route, key, data[key])
    if route.route_date and not route.weekday:
        route.weekday = weekday_code(route.route_date)

    if stops is not None:
        _replace_stops(db, route, stops)
        # Membership changed, so any previous optimisation is stale.
        route.is_optimized = False
    route.planned_stops = len(route.stops)
    route.updated_by_id = user_id
    db.flush()

    _audit(
        db, AuditAction.UPDATE,
        audit=audit, user_id=user_id,
        entity_type="Route", entity_id=route.id, entity_label=route.code,
        summary="route updated",
        old_values=before,
        new_values={
            "name": route.name, "status": route.status,
            "salesperson_id": route.salesperson_id, "vehicle_id": route.vehicle_id,
            "route_date": route.route_date, "planned_start_time": route.planned_start_time,
        },
    )
    if commit:
        db.commit()
    return route


def delete_route(
    db: Session,
    route: Route,
    *,
    user_id: int | None = None,
    audit: dict[str, Any] | None = None,
    commit: bool = True,
) -> None:
    """Soft-delete: an executed route is operational history and never vanishes."""
    if route.status == RouteStatus.IN_PROGRESS:
        raise BusinessRuleError("route.in_progress", params={"id": route.id})
    route.is_deleted = True
    route.deleted_at = utcnow()
    route.deleted_by_id = user_id
    route.is_active = False
    db.flush()
    _audit(
        db, AuditAction.DELETE,
        audit=audit, user_id=user_id,
        entity_type="Route", entity_id=route.id, entity_label=route.code,
        summary="route deleted",
    )
    if commit:
        db.commit()


def _replace_stops(db: Session, route: Route, stops: list[dict[str, Any]]) -> None:
    """
    Rewrite the stop list, preserving execution state for customers that stay.

    Dropping and recreating every row would lose arrival times mid-day, so
    existing stops are reused and only genuinely removed customers are deleted.
    """
    existing = {s.customer_id: s for s in route.stops}
    seen: set[int] = set()

    for index, raw in enumerate(stops, start=1):
        customer_id = int(raw.get("customer_id") or 0)
        if not customer_id or customer_id in seen:
            continue
        seen.add(customer_id)
        customer = db.get(Customer, customer_id)
        if customer is None or customer.is_deleted:
            raise NotFoundError("customer.not_found", params={"id": customer_id})

        stop = existing.get(customer_id)
        if stop is None:
            stop = RouteStop(route_id=route.id, customer_id=customer_id)
            db.add(stop)
            route.stops.append(stop)
        stop.sequence = int(raw.get("sequence") or index)
        stop.service_time_minutes = int(
            raw.get("service_time_minutes") or customer.service_time_minutes or 10
        )
        stop.is_priority = bool(raw.get("is_priority", customer.is_priority))
        if raw.get("planned_arrival"):
            stop.planned_arrival = raw["planned_arrival"]

    for customer_id, stop in existing.items():
        if customer_id not in seen:
            route.stops.remove(stop)
            db.delete(stop)
    db.flush()


# ===========================================================================
# Templates & daily generation
# ===========================================================================
def generate_from_template(
    db: Session,
    template_id: int,
    on_date: date,
    *,
    user_id: int | None = None,
    audit: dict[str, Any] | None = None,
    commit: bool = True,
) -> Route:
    """
    Materialise a template into a dated, executable route.

    Idempotent: calling it twice for the same template and date returns the
    instance that already exists rather than duplicating a salesperson's day.
    """
    template = db.get(Route, template_id)
    if template is None or template.is_deleted or not template.is_template:
        raise NotFoundError("route.not_found", params={"id": template_id})

    existing = db.execute(
        select(Route).where(
            Route.template_id == template_id,
            Route.route_date == on_date,
            Route.is_deleted.is_(False),
        )
    ).scalars().first()
    if existing is not None:
        return existing

    instance = Route(
        code=f"{template.code}-{on_date:%Y%m%d}",
        name=template.name,
        description=template.description,
        is_template=False,
        template_id=template.id,
        route_date=on_date,
        weekday=weekday_code(on_date),
        salesperson_id=template.salesperson_id,
        vehicle_id=template.vehicle_id,
        region_id=template.region_id,
        start_warehouse_id=template.start_warehouse_id,
        end_warehouse_id=template.end_warehouse_id,
        planned_start_time=template.planned_start_time,
        status=RouteStatus.PLANNED,
        created_by_id=user_id,
        updated_by_id=user_id,
    )
    db.add(instance)
    db.flush()

    for source in sorted(template.stops, key=lambda s: (s.sequence, s.id)):
        db.add(
            RouteStop(
                route_id=instance.id,
                customer_id=source.customer_id,
                sequence=source.sequence,
                service_time_minutes=source.service_time_minutes,
                is_priority=source.is_priority,
                planned_arrival=source.planned_arrival,
                planned_departure=source.planned_departure,
                status=StopStatus.PENDING,
            )
        )
    db.flush()
    db.refresh(instance)
    instance.planned_stops = len(instance.stops)

    _audit(
        db, AuditAction.CREATE,
        audit=audit, user_id=user_id,
        entity_type="Route", entity_id=instance.id, entity_label=instance.code,
        summary=f"route generated from template {template.code} for {on_date}",
    )
    if commit:
        db.commit()
    return instance


def _is_due(customer: Customer, on_date: date) -> bool:
    """
    Whether *customer* is scheduled to be visited on *on_date*.

    ``visit_days`` carries the weekday pattern; ``visit_frequency`` thins it out
    for customers that are not served every matching week.
    """
    days = customer.visit_day_list()
    code = weekday_code(on_date)
    freq = customer.visit_frequency

    if freq == VisitFrequency.DAILY:
        return not days or code in days
    if freq == VisitFrequency.ON_DEMAND:
        return False
    if code not in days:
        return False
    if freq == VisitFrequency.BIWEEKLY:
        # Alternate ISO weeks, split by customer id so the load stays even
        # across the two weeks instead of every account landing together.
        return (on_date.isocalendar()[1] % 2) == (customer.id % 2)
    if freq == VisitFrequency.MONTHLY:
        return on_date.day <= 7          # first matching weekday of the month
    return True                          # WEEKLY / TWICE_WEEKLY


def generate_daily_routes(
    db: Session,
    *,
    on_date: date,
    region_id: int | None = None,
    salesperson_ids: list[int] | None = None,
    user_id: int | None = None,
    audit: dict[str, Any] | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """
    Build one route per salesperson from the customer visit calendar.

    Customers already planned on another route for the same date are skipped,
    so re-running the generator after a manual edit never double-books a stop.
    """
    weekday = weekday_code(on_date)
    stmt = select(Customer).where(
        Customer.is_deleted.is_(False),
        Customer.status == CustomerStatus.ACTIVE,
        Customer.default_salesperson_id.is_not(None),
    )
    if region_id is not None:
        stmt = stmt.where(Customer.region_id == region_id)
    stmt = _scoped(stmt, Customer.default_salesperson_id, salesperson_ids)
    candidates = db.execute(stmt).scalars().all()

    already = set(
        db.execute(
            select(RouteStop.customer_id)
            .join(Route, Route.id == RouteStop.route_id)
            .where(Route.route_date == on_date, Route.is_deleted.is_(False))
        ).scalars().all()
    )

    by_salesperson: dict[int, list[Customer]] = {}
    skipped = 0
    for customer in candidates:
        if not _is_due(customer, on_date):
            continue
        if customer.id in already:
            skipped += 1
            continue
        by_salesperson.setdefault(int(customer.default_salesperson_id or 0), []).append(customer)

    created: list[Route] = []
    updated: list[Route] = []
    for sp_id, customers in sorted(by_salesperson.items()):
        salesperson = db.get(Salesperson, sp_id)
        if salesperson is None or not salesperson.is_active:
            continue

        route = db.execute(
            select(Route).where(
                Route.route_date == on_date,
                Route.salesperson_id == sp_id,
                Route.is_template.is_(False),
                Route.is_deleted.is_(False),
            ).order_by(Route.id)
        ).scalars().first()
        is_new = route is None
        if route is None:
            route = Route(
                code=f"{salesperson.code}-{on_date:%Y%m%d}",
                name=f"{salesperson.full_name} / {on_date:%d.%m.%Y}",
                is_template=False,
                route_date=on_date,
                weekday=weekday,
                salesperson_id=sp_id,
                vehicle_id=salesperson.default_vehicle_id,
                region_id=salesperson.region_id,
                start_warehouse_id=salesperson.default_warehouse_id,
                end_warehouse_id=salesperson.default_warehouse_id,
                status=RouteStatus.PLANNED,
                created_by_id=user_id,
                updated_by_id=user_id,
            )
            db.add(route)
            db.flush()

        next_seq = max([s.sequence for s in route.stops], default=0)
        for customer in sorted(customers, key=lambda c: (c.visit_sequence, c.id)):
            next_seq += 1
            db.add(
                RouteStop(
                    route_id=route.id,
                    customer_id=customer.id,
                    sequence=next_seq,
                    service_time_minutes=customer.service_time_minutes or 10,
                    is_priority=customer.is_priority,
                    status=StopStatus.PENDING,
                )
            )
        db.flush()
        db.refresh(route)
        route.planned_stops = len(route.stops)
        (created if is_new else updated).append(route)

    db.flush()
    _audit(
        db, AuditAction.CREATE,
        audit=audit, user_id=user_id,
        entity_type="Route", entity_id=None,
        entity_label=f"daily/{on_date}",
        summary=(
            f"daily routes generated date={on_date} created={len(created)} "
            f"updated={len(updated)} skipped={skipped}"
        ),
    )
    if commit:
        db.commit()

    return {
        "on_date": on_date,
        "weekday": weekday,
        "created": len(created),
        "updated": len(updated),
        "skipped": skipped,
        "customers_planned": sum(len(v) for v in by_salesperson.values()),
        "routes": created + updated,
    }


# ===========================================================================
# Optimisation
# ===========================================================================
def _order_demand(db: Session, customer_ids: list[int], on_date: date | None) -> dict[int, tuple[float, float]]:
    """
    Volume/weight already committed per customer for the day.

    Pre-sale routes deliver confirmed orders, so their capacity constraint is
    real; hot-sale routes have no orders yet and simply come back empty.
    """
    if not customer_ids or on_date is None:
        return {}
    rows = db.execute(
        select(
            Order.customer_id,
            func.sum(Order.total_volume_l),
            func.sum(Order.total_weight_kg),
        )
        .where(
            Order.customer_id.in_(customer_ids),
            Order.is_deleted.is_(False),
            Order.delivery_date == on_date,
            Order.status.in_(
                (OrderStatus.CONFIRMED, OrderStatus.PARTIALLY_DELIVERED)
            ),
        )
        .group_by(Order.customer_id)
    ).all()
    return {int(r[0]): (float(r[1] or 0.0), float(r[2] or 0.0)) for r in rows}


def _vrp_stop(
    stop: RouteStop,
    customer: Customer,
    demand: dict[int, tuple[float, float]],
) -> VrpStop:
    volume, weight = demand.get(customer.id, (0.0, 0.0))
    return VrpStop(
        customer_id=customer.id,
        lat=float(customer.latitude),          # caller guarantees coordinates
        lng=float(customer.longitude),
        demand_volume=volume,
        demand_weight=weight,
        service_minutes=float(stop.service_time_minutes or customer.service_time_minutes or 10),
        ready_minutes=parse_hhmm(customer.opening_time),
        due_minutes=parse_hhmm(customer.closing_time),
        priority=bool(stop.is_priority or customer.is_priority),
    )


def _vrp_vehicle(
    vehicle: Vehicle | None,
    depot: tuple[float, float],
    *,
    start_minutes: int,
    max_minutes: int,
    end: tuple[float, float] | None = None,
    max_stops: int = 0,
) -> VrpVehicle:
    end_point = end or depot
    return VrpVehicle(
        vehicle_id=vehicle.id if vehicle else 0,
        capacity_volume=float(vehicle.capacity_volume_l) if vehicle else 0.0,
        capacity_weight=float(vehicle.capacity_weight_kg) if vehicle else 0.0,
        start_lat=depot[0],
        start_lng=depot[1],
        end_lat=end_point[0],
        end_lng=end_point[1],
        start_minutes=start_minutes,
        max_minutes=max_minutes,
        max_stops=max_stops,
    )


def _resolve_depot(
    db: Session,
    route: Route,
    vehicle: Vehicle | None,
    points: list[tuple[float, float]],
) -> tuple[float, float] | None:
    """Where the van starts: explicit warehouse → vehicle home → any depot → centroid."""
    candidates = [route.start_warehouse_id]
    if vehicle is not None:
        candidates.append(vehicle.home_warehouse_id)
    if route.salesperson_id:
        salesperson = db.get(Salesperson, route.salesperson_id)
        if salesperson is not None:
            candidates.append(salesperson.default_warehouse_id)
    for warehouse_id in candidates:
        point = _warehouse_point(db, warehouse_id)
        if point:
            return point
    return _default_depot(db) or centroid(points)


def _apply_sequence(
    db: Session,
    route: Route,
    ordered: list[RouteStop],
    arrivals: list[float],
    tail: list[RouteStop],
    depot: tuple[float, float],
    end_point: tuple[float, float],
    params: dict[str, float],
) -> tuple[float, float]:
    """Write the solver's order back onto the RouteStop rows; returns (km, minutes)."""
    factor = params["road_detour_factor"]
    speed = params["avg_speed_kmh"]
    total_km = 0.0
    previous: tuple[float, float] = depot

    for index, stop in enumerate(ordered, start=1):
        customer = stop.customer
        point = (float(customer.latitude), float(customer.longitude))
        leg_km = pair_km(previous, point, factor)
        stop.sequence = index
        stop.distance_from_previous_km = round(leg_km, 3)
        stop.travel_time_from_previous_min = int(round(minutes_for(leg_km, speed)))
        arrival = arrivals[index - 1] if index - 1 < len(arrivals) else None
        if arrival is not None:
            stop.planned_arrival = format_hhmm(int(round(arrival)))
            stop.planned_departure = format_hhmm(
                int(round(arrival + (stop.service_time_minutes or 0)))
            )
        total_km += leg_km
        previous = point

    if ordered:
        total_km += pair_km(previous, end_point, factor)

    # Stops the solver could not place (no coordinates, or window infeasible)
    # keep their relative order at the end so nothing is silently lost.
    seq = len(ordered)
    for stop in tail:
        seq += 1
        stop.sequence = seq
        stop.distance_from_previous_km = 0.0
        stop.travel_time_from_previous_min = 0

    duration = sum(int(s.service_time_minutes or 0) for s in ordered) + minutes_for(total_km, speed)
    db.flush()
    return total_km, duration


def optimize_route(
    db: Session,
    route: Route,
    *,
    prefer_exact: bool = True,
    time_limit_s: int = 10,
    user_id: int | None = None,
    audit: dict[str, Any] | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """
    Re-sequence one route's stops to minimise driving time.

    Raises :class:`OptimizationError` when no stop has coordinates — ordering
    geography we do not know is guesswork, and a silently random order is worse
    than telling the planner to geocode the customers first.
    """
    stops = sorted(route.stops, key=lambda s: (s.sequence, s.id))
    if not stops:
        raise BusinessRuleError("route.no_stops", params={"id": route.id})

    geo: list[RouteStop] = []
    tail: list[RouteStop] = []
    for stop in stops:
        customer = stop.customer
        if customer is not None and customer.latitude is not None and customer.longitude is not None:
            geo.append(stop)
        else:
            tail.append(stop)
    if not geo:
        raise OptimizationError("route.no_coordinates", params={"id": route.id})

    params = route_settings(db)
    vehicle = db.get(Vehicle, route.vehicle_id) if route.vehicle_id else None
    points = [(float(s.customer.latitude), float(s.customer.longitude)) for s in geo]
    depot = _resolve_depot(db, route, vehicle, points)
    if depot is None:
        raise OptimizationError("route.no_coordinates", params={"id": route.id})

    demand = _order_demand(db, [s.customer_id for s in geo], route.route_date)
    start_minutes = parse_hhmm(route.planned_start_time) or 480
    end_point = _warehouse_point(db, route.end_warehouse_id) or depot

    problem = VrpProblem(
        depot=depot,
        stops=[_vrp_stop(s, s.customer, demand) for s in geo],
        vehicles=[
            _vrp_vehicle(
                vehicle,
                depot,
                start_minutes=start_minutes,
                max_minutes=int(params["workday_minutes"]),
                end=end_point,
            )
        ],
        avg_speed_kmh=params["avg_speed_kmh"],
        detour_factor=params["road_detour_factor"],
    )
    solution = optimize(problem, prefer_exact=prefer_exact, time_limit_s=time_limit_s)

    by_customer = {s.customer_id: s for s in geo}
    ordered: list[RouteStop] = []
    arrivals: list[float] = []
    for vrp_route in solution.routes:
        for position, customer_id in enumerate(vrp_route.stop_ids):
            stop = by_customer.pop(customer_id, None)
            if stop is None:
                continue
            ordered.append(stop)
            if position < len(vrp_route.arrival_minutes):
                arrivals.append(vrp_route.arrival_minutes[position])

    unplaced = [by_customer[cid] for cid in sorted(by_customer)]
    total_km, duration = _apply_sequence(
        db, route, ordered, arrivals, unplaced + tail, depot, end_point, params
    )
    if solution.routes:
        # The solver's clock includes waiting for shops that open late, which a
        # plain travel+service sum would understate.
        duration = sum(r.duration_min for r in solution.routes)

    route.planned_stops = len(stops)
    route.planned_distance_km = round(total_km, 3)
    route.planned_duration_min = int(round(duration))
    route.planned_volume_l = round(sum(v for v, _ in demand.values()), 3)
    route.planned_weight_kg = round(sum(w for _, w in demand.values()), 3)
    route.is_optimized = True
    route.optimizer = solution.solver_name
    route.optimized_at = utcnow()
    route.optimization_seconds = solution.seconds
    route.optimization_note = (
        f"solver={solution.solver_name} objective={solution.objective} "
        f"placed={len(ordered)} unplaced={len(unplaced) + len(tail)}"
    )
    if route.status == RouteStatus.PLANNED:
        route.status = RouteStatus.OPTIMIZED
    route.updated_by_id = user_id
    db.flush()

    _audit(
        db, AuditAction.UPDATE,
        audit=audit, user_id=user_id,
        entity_type="Route", entity_id=route.id, entity_label=route.code,
        summary=(
            f"route optimised solver={solution.solver_name} "
            f"stops={len(ordered)} km={route.planned_distance_km}"
        ),
        new_values={
            "solver": solution.solver_name,
            "distance_km": route.planned_distance_km,
            "duration_min": route.planned_duration_min,
            "sequence": [s.customer_id for s in ordered],
        },
    )
    if commit:
        db.commit()

    return {
        "route_id": route.id,
        "code": route.code,
        "solver": solution.solver_name,
        "seconds": solution.seconds,
        "stops": len(ordered),
        "distance_km": route.planned_distance_km,
        "duration_min": route.planned_duration_min,
        "objective": solution.objective,
        "unassigned_customer_ids": [s.customer_id for s in unplaced + tail],
    }


def optimize_multi(
    db: Session,
    *,
    on_date: date,
    vehicle_ids: list[int],
    customer_ids: list[int],
    prefer_exact: bool = True,
    time_limit_s: int = 15,
    region_id: int | None = None,
    balance: bool = False,
    user_id: int | None = None,
    audit: dict[str, Any] | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """
    Split a set of customers across several vans in one solve.

    Solving the fleet jointly beats optimising each van separately: the
    assignment of a customer to a vehicle is exactly where most of the saving
    is, and that decision cannot be made one route at a time.

    By default the cheapest plan wins, which may leave a van idle when one can
    cover everything.  ``balance=True`` caps each van's share so the workload
    is spread across the whole fleet instead.
    """
    if not vehicle_ids:
        raise ValidationError("validation.required", params={"field": "vehicle_ids"})
    if not customer_ids:
        raise ValidationError("validation.required", params={"field": "customer_ids"})

    vehicles = db.execute(
        select(Vehicle).where(Vehicle.id.in_(vehicle_ids), Vehicle.is_deleted.is_(False))
        .order_by(Vehicle.id)
    ).scalars().all()
    if not vehicles:
        raise NotFoundError("vehicle.not_found", params={"id": vehicle_ids})

    customers = db.execute(
        select(Customer).where(
            Customer.id.in_(customer_ids),
            Customer.is_deleted.is_(False),
            Customer.latitude.is_not(None),
            Customer.longitude.is_not(None),
        ).order_by(Customer.id)
    ).scalars().all()
    if not customers:
        raise OptimizationError("route.no_coordinates")

    params = route_settings(db)
    points = [(float(c.latitude), float(c.longitude)) for c in customers]
    depot = (
        _warehouse_point(db, vehicles[0].home_warehouse_id)
        or _default_depot(db)
        or centroid(points)
    )
    if depot is None:
        raise OptimizationError("route.no_coordinates")

    demand = _order_demand(db, [c.id for c in customers], on_date)
    workday = int(params["workday_minutes"])
    per_vehicle_cap = -(-len(customers) // len(vehicles)) if balance else 0

    problem = VrpProblem(
        depot=depot,
        stops=[
            VrpStop(
                customer_id=c.id,
                lat=float(c.latitude),
                lng=float(c.longitude),
                demand_volume=demand.get(c.id, (0.0, 0.0))[0],
                demand_weight=demand.get(c.id, (0.0, 0.0))[1],
                service_minutes=float(c.service_time_minutes or 10),
                ready_minutes=parse_hhmm(c.opening_time),
                due_minutes=parse_hhmm(c.closing_time),
                priority=bool(c.is_priority),
            )
            for c in customers
        ],
        vehicles=[
            _vrp_vehicle(
                v,
                _warehouse_point(db, v.home_warehouse_id) or depot,
                start_minutes=480,
                max_minutes=workday,
                max_stops=per_vehicle_cap,
            )
            for v in vehicles
        ],
        avg_speed_kmh=params["avg_speed_kmh"],
        detour_factor=params["road_detour_factor"],
    )
    solution = optimize(problem, prefer_exact=prefer_exact, time_limit_s=time_limit_s)

    by_id = {c.id: c for c in customers}
    vehicles_by_id = {v.id: v for v in vehicles}
    created: list[Route] = []

    for vrp_route in solution.routes:
        vehicle = vehicles_by_id.get(vrp_route.vehicle_id)
        if vehicle is None or not vrp_route.stop_ids:
            continue
        salesperson_id = vehicle.default_salesperson_id
        route = Route(
            code=f"{vehicle.code}-{on_date:%Y%m%d}",
            name=f"{vehicle.plate_number} / {on_date:%d.%m.%Y}",
            is_template=False,
            route_date=on_date,
            weekday=weekday_code(on_date),
            salesperson_id=salesperson_id,
            vehicle_id=vehicle.id,
            region_id=region_id or vehicle.region_id,
            start_warehouse_id=vehicle.home_warehouse_id,
            end_warehouse_id=vehicle.home_warehouse_id,
            planned_start_time=format_hhmm(480),
            status=RouteStatus.OPTIMIZED,
            planned_stops=len(vrp_route.stop_ids),
            planned_distance_km=round(vrp_route.distance_km, 3),
            planned_duration_min=int(round(vrp_route.duration_min)),
            planned_volume_l=round(vrp_route.load_volume, 3),
            planned_weight_kg=round(vrp_route.load_weight, 3),
            is_optimized=True,
            optimizer=solution.solver_name,
            optimized_at=utcnow(),
            optimization_seconds=solution.seconds,
            optimization_note=f"multi-vehicle solve, objective={solution.objective}",
            created_by_id=user_id,
            updated_by_id=user_id,
        )
        db.add(route)
        db.flush()

        for position, customer_id in enumerate(vrp_route.stop_ids, start=1):
            customer = by_id.get(customer_id)
            if customer is None:
                continue
            arrival = (
                vrp_route.arrival_minutes[position - 1]
                if position - 1 < len(vrp_route.arrival_minutes)
                else None
            )
            service = int(customer.service_time_minutes or 10)
            db.add(
                RouteStop(
                    route_id=route.id,
                    customer_id=customer_id,
                    sequence=position,
                    service_time_minutes=service,
                    is_priority=customer.is_priority,
                    status=StopStatus.PENDING,
                    planned_arrival=format_hhmm(int(round(arrival))) if arrival is not None else None,
                    planned_departure=(
                        format_hhmm(int(round(arrival + service))) if arrival is not None else None
                    ),
                )
            )
        db.flush()
        created.append(route)

    _audit(
        db, AuditAction.CREATE,
        audit=audit, user_id=user_id,
        entity_type="Route", entity_id=None, entity_label=f"multi/{on_date}",
        summary=(
            f"multi-vehicle optimisation date={on_date} vehicles={len(vehicles)} "
            f"routes={len(created)} unassigned={len(solution.unassigned)} "
            f"km={solution.total_distance_km}"
        ),
        new_values={
            "solver": solution.solver_name,
            "routes": [{"route_id": r.id, "vehicle_id": r.vehicle_id} for r in created],
        },
    )
    if commit:
        db.commit()

    return {
        "on_date": on_date,
        "solver": solution.solver_name,
        "seconds": solution.seconds,
        "total_distance_km": solution.total_distance_km,
        "objective": solution.objective,
        # Only vans that actually received work get a route — an empty route is
        # noise on the planner's board.
        "vehicles_used": len(created),
        "vehicles_offered": len(vehicles),
        "routes": created,
        "unassigned_customer_ids": list(solution.unassigned),
    }


# ===========================================================================
# Execution
# ===========================================================================
def start_route(
    db: Session,
    route: Route,
    *,
    latitude: float | None = None,
    longitude: float | None = None,
    odometer_km: float | None = None,
    user_id: int | None = None,
    audit: dict[str, Any] | None = None,
    commit: bool = True,
) -> Route:
    if route.status == RouteStatus.COMPLETED:
        raise BusinessRuleError("route.already_completed", params={"id": route.id})
    if route.status not in _STARTABLE and route.status != RouteStatus.IN_PROGRESS:
        raise BusinessRuleError("route.not_startable", params={"status": route.status})
    if not route.stops:
        raise BusinessRuleError("route.no_stops", params={"id": route.id})

    if route.status != RouteStatus.IN_PROGRESS:
        route.status = RouteStatus.IN_PROGRESS
        route.started_at = utcnow()
    route.updated_by_id = user_id

    if route.vehicle_id and latitude is not None and longitude is not None:
        vehicle = db.get(Vehicle, route.vehicle_id)
        if vehicle is not None:
            vehicle.last_lat = latitude
            vehicle.last_lng = longitude
            vehicle.last_position_at = utcnow()
            if odometer_km is not None:
                vehicle.odometer_km = float(odometer_km)
    db.flush()

    _audit(
        db, AuditAction.UPDATE,
        audit=audit, user_id=user_id,
        entity_type="Route", entity_id=route.id, entity_label=route.code,
        summary=f"route started stops={len(route.stops)}",
    )
    if commit:
        db.commit()
    return route


def complete_route(
    db: Session,
    route: Route,
    *,
    actual_distance_km: float | None = None,
    odometer_km: float | None = None,
    notes: str | None = None,
    user_id: int | None = None,
    audit: dict[str, Any] | None = None,
    commit: bool = True,
) -> Route:
    """Close the day's route and freeze its actuals for plan-vs-actual reporting."""
    if route.status == RouteStatus.COMPLETED:
        raise BusinessRuleError("route.already_completed", params={"id": route.id})

    now = utcnow()
    route.status = RouteStatus.COMPLETED
    route.completed_at = now
    route.completed_stops = sum(1 for s in route.stops if s.status == StopStatus.COMPLETED)
    route.skipped_stops = sum(1 for s in route.stops if s.status == StopStatus.SKIPPED)
    route.actual_stops = sum(
        1 for s in route.stops if s.status in (StopStatus.ARRIVED, StopStatus.COMPLETED)
    )
    if route.started_at is not None:
        route.actual_duration_min = max(0, int((now - route.started_at).total_seconds() // 60))

    if actual_distance_km is not None:
        route.actual_distance_km = float(actual_distance_km)
    elif not route.actual_distance_km:
        route.actual_distance_km = round(gps_distance_km(db, route_id=route.id), 3)

    route.total_sales_amount = money(
        db.execute(
            select(func.coalesce(func.sum(Sale.total_amount), 0)).where(
                Sale.route_id == route.id,
                Sale.is_cancelled.is_(False),
                Sale.is_deleted.is_(False),
            )
        ).scalar_one()
    )
    if notes:
        route.description = ((route.description or "") + f"\n{notes}").strip()
    route.updated_by_id = user_id

    if odometer_km is not None and route.vehicle_id:
        vehicle = db.get(Vehicle, route.vehicle_id)
        if vehicle is not None:
            vehicle.odometer_km = float(odometer_km)
    db.flush()

    _audit(
        db, AuditAction.UPDATE,
        audit=audit, user_id=user_id,
        entity_type="Route", entity_id=route.id, entity_label=route.code,
        summary=(
            f"route completed done={route.completed_stops}/{route.planned_stops} "
            f"km={route.actual_distance_km}"
        ),
        amount=route.total_sales_amount,
        new_values={
            "completed_stops": route.completed_stops,
            "skipped_stops": route.skipped_stops,
            "actual_distance_km": route.actual_distance_km,
            "actual_duration_min": route.actual_duration_min,
        },
    )
    if commit:
        db.commit()
    return route


def _get_stop(db: Session, route: Route, stop_id: int) -> RouteStop:
    stop = db.get(RouteStop, stop_id)
    if stop is None or stop.route_id != route.id:
        raise NotFoundError("route.stop_not_found", params={"id": stop_id})
    return stop


def _geofence(
    db: Session,
    customer: Customer,
    latitude: float | None,
    longitude: float | None,
    radius_m: float,
) -> tuple[float | None, bool | None]:
    """Distance in metres from the customer's registered location, and whether it is inside."""
    if (
        latitude is None
        or longitude is None
        or customer.latitude is None
        or customer.longitude is None
    ):
        return None, None
    metres = haversine_km(latitude, longitude, customer.latitude, customer.longitude) * 1000.0
    return round(metres, 1), metres <= radius_m


def arrive_at_stop(
    db: Session,
    route: Route,
    stop_id: int,
    *,
    latitude: float | None = None,
    longitude: float | None = None,
    user_id: int | None = None,
    audit: dict[str, Any] | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """
    Record arrival at a stop and check the salesperson is actually there.

    The geofence result is stored rather than enforced: a shop may legitimately
    have wrong coordinates, so the supervisor gets an exception to review
    instead of the salesperson getting blocked in front of a customer.
    """
    stop = _get_stop(db, route, stop_id)
    params = route_settings(db)
    radius = params["geofence_radius_m"]
    now = utcnow()

    distance_m, inside = _geofence(db, stop.customer, latitude, longitude, radius)
    stop.arrived_at = now
    stop.arrival_lat = latitude
    stop.arrival_lng = longitude
    stop.geofence_distance_m = distance_m
    stop.status = StopStatus.ARRIVED

    planned = parse_hhmm(stop.planned_arrival)
    if planned is not None:
        stop.delay_minutes = max(0, _local_minutes() - planned)

    if route.status in _STARTABLE:
        route.status = RouteStatus.IN_PROGRESS
        route.started_at = route.started_at or now
    route.actual_stops = sum(
        1 for s in route.stops if s.status in (StopStatus.ARRIVED, StopStatus.COMPLETED)
    )
    db.flush()

    _audit(
        db, AuditAction.UPDATE,
        audit=audit, user_id=user_id,
        entity_type="RouteStop", entity_id=stop.id,
        entity_label=f"{route.code}#{stop.sequence}",
        summary=(
            f"arrived customer={stop.customer_id} geofence={inside} "
            f"distance_m={distance_m} delay={stop.delay_minutes}"
        ),
    )
    if commit:
        db.commit()

    return {
        "stop_id": stop.id,
        "customer_id": stop.customer_id,
        "status": stop.status,
        "arrived_at": stop.arrived_at,
        "geofence_distance_m": distance_m,
        "in_geofence": inside,
        "geofence_radius_m": radius,
        "delay_minutes": stop.delay_minutes,
    }


def skip_stop(
    db: Session,
    route: Route,
    stop_id: int,
    *,
    reason: str,
    latitude: float | None = None,
    longitude: float | None = None,
    user_id: int | None = None,
    audit: dict[str, Any] | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Mark a stop as not served.  A reason is mandatory — skips are audited."""
    if not (reason or "").strip():
        raise ValidationError("validation.required", params={"field": "reason"})

    stop = _get_stop(db, route, stop_id)
    if stop.status == StopStatus.COMPLETED:
        raise BusinessRuleError("route.stop_completed", params={"id": stop.id})

    stop.status = StopStatus.SKIPPED
    stop.skip_reason = reason.strip()[:255]
    stop.departed_at = utcnow()
    if latitude is not None and longitude is not None:
        stop.arrival_lat = latitude
        stop.arrival_lng = longitude
        distance_m, _ = _geofence(
            db, stop.customer, latitude, longitude, route_settings(db)["geofence_radius_m"]
        )
        stop.geofence_distance_m = distance_m

    route.skipped_stops = sum(1 for s in route.stops if s.status == StopStatus.SKIPPED)
    db.flush()

    _audit(
        db, AuditAction.UPDATE,
        audit=audit, user_id=user_id,
        entity_type="RouteStop", entity_id=stop.id,
        entity_label=f"{route.code}#{stop.sequence}",
        summary=f"stop skipped customer={stop.customer_id} reason={stop.skip_reason}",
    )
    if commit:
        db.commit()

    return {
        "stop_id": stop.id,
        "customer_id": stop.customer_id,
        "status": stop.status,
        "skip_reason": stop.skip_reason,
        "skipped_stops": route.skipped_stops,
    }


# ===========================================================================
# Visits
# ===========================================================================
def _open_day_session(db: Session, salesperson_id: int | None, on: date) -> DaySession | None:
    """Prefer the field module's own resolver, fall back to a direct lookup."""
    if not salesperson_id:
        return None
    try:
        from app.services import day_session_service

        getter = getattr(day_session_service, "get_open_session", None)
        if getter is not None:
            return getter(db, salesperson_id=salesperson_id, on=on)
    except Exception:  # the field module may not be present yet
        log.debug("day_session_service unavailable; using direct lookup")
    return db.execute(
        select(DaySession)
        .where(DaySession.salesperson_id == salesperson_id, DaySession.session_date == on)
        .order_by(DaySession.id.desc())
    ).scalars().first()


def record_visit(
    db: Session,
    *,
    customer_id: int,
    salesperson_id: int | None = None,
    outcome: str = VisitOutcome.NO_ORDER,
    visit_date: date | None = None,
    vehicle_id: int | None = None,
    route_id: int | None = None,
    route_stop_id: int | None = None,
    day_session_id: int | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
    duration_minutes: int | None = None,
    sale_amount: Decimal | float | int = 0,
    collected_amount: Decimal | float | int = 0,
    return_amount: Decimal | float | int = 0,
    lines_count: int = 0,
    photo_path: str | None = None,
    signature_path: str | None = None,
    notes: str | None = None,
    user_id: int | None = None,
    audit: dict[str, Any] | None = None,
    commit: bool = True,
) -> Visit:
    """
    Log a customer visit — the single source of truth for coverage KPIs.

    Written whether or not anything was sold, because "visited and did not buy"
    and "never visited" are completely different management problems.
    """
    customer = db.get(Customer, customer_id)
    if customer is None or customer.is_deleted:
        raise NotFoundError("customer.not_found", params={"id": customer_id})

    on = visit_date or _local_now().date()
    now = utcnow()
    session = None
    if day_session_id:
        session = db.get(DaySession, day_session_id)
    if session is None:
        session = _open_day_session(db, salesperson_id, on)

    stop: RouteStop | None = None
    if route_stop_id:
        stop = db.get(RouteStop, route_stop_id)
    elif route_id:
        stop = db.execute(
            select(RouteStop).where(
                RouteStop.route_id == route_id, RouteStop.customer_id == customer_id
            )
        ).scalars().first()
    else:
        stop = db.execute(
            select(RouteStop)
            .join(Route, Route.id == RouteStop.route_id)
            .where(
                RouteStop.customer_id == customer_id,
                Route.route_date == on,
                Route.is_deleted.is_(False),
            )
            .order_by(RouteStop.id.desc())
        ).scalars().first()

    radius = route_settings(db)["geofence_radius_m"]
    _, inside = _geofence(db, customer, latitude, longitude, radius)

    duration = duration_minutes
    if duration is None and started_at is not None:
        duration = max(0, int(((ended_at or now) - started_at).total_seconds() // 60))

    visit = Visit(
        visit_date=on,
        customer_id=customer_id,
        salesperson_id=salesperson_id,
        vehicle_id=vehicle_id or (session.vehicle_id if session else None),
        route_id=route_id or (stop.route_id if stop else None),
        route_stop_id=stop.id if stop else None,
        day_session_id=session.id if session else None,
        outcome=str(outcome),
        started_at=started_at,
        ended_at=ended_at or now,
        duration_minutes=int(duration or 0),
        latitude=latitude,
        longitude=longitude,
        is_in_geofence=inside,
        is_unplanned=stop is None,
        sale_amount=money(sale_amount),
        collected_amount=money(collected_amount),
        return_amount=money(return_amount),
        lines_count=int(lines_count or 0),
        photo_path=photo_path,
        signature_path=signature_path,
        notes=notes,
        created_by_id=user_id,
        updated_by_id=user_id,
    )
    db.add(visit)
    db.flush()

    customer.last_visit_date = on

    if stop is not None:
        stop.status = (
            StopStatus.SKIPPED if str(outcome) == VisitOutcome.CLOSED else StopStatus.COMPLETED
        )
        stop.departed_at = now
        stop.arrived_at = stop.arrived_at or started_at or now
        route = db.get(Route, stop.route_id)
        if route is not None:
            route.completed_stops = sum(
                1 for s in route.stops if s.status == StopStatus.COMPLETED
            )
            route.skipped_stops = sum(1 for s in route.stops if s.status == StopStatus.SKIPPED)
            route.actual_stops = sum(
                1 for s in route.stops if s.status in (StopStatus.ARRIVED, StopStatus.COMPLETED)
            )
            route.total_sales_amount = money(D(route.total_sales_amount) + D(sale_amount))

    if session is not None:
        session.visits_done = int(session.visits_done or 0) + 1
    db.flush()

    _audit(
        db, AuditAction.CREATE,
        audit=audit, user_id=user_id,
        entity_type="Visit", entity_id=visit.id, entity_label=customer.code,
        summary=(
            f"visit recorded customer={customer_id} outcome={outcome} "
            f"geofence={inside} unplanned={visit.is_unplanned}"
        ),
        amount=visit.sale_amount,
    )
    if commit:
        db.commit()
    return visit


def list_visits(
    db: Session,
    *,
    offset: int = 0,
    limit: int = 50,
    customer_id: int | None = None,
    salesperson_id: int | None = None,
    route_id: int | None = None,
    outcome: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    salesperson_ids: list[int] | None = None,
) -> tuple[list[Visit], int]:
    stmt = select(Visit)
    if customer_id is not None:
        stmt = stmt.where(Visit.customer_id == customer_id)
    if salesperson_id is not None:
        stmt = stmt.where(Visit.salesperson_id == salesperson_id)
    if route_id is not None:
        stmt = stmt.where(Visit.route_id == route_id)
    if outcome:
        stmt = stmt.where(Visit.outcome == outcome)
    if date_from is not None:
        stmt = stmt.where(Visit.visit_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(Visit.visit_date <= date_to)
    stmt = _scoped(stmt, Visit.salesperson_id, salesperson_ids)

    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = db.execute(
        stmt.order_by(Visit.visit_date.desc(), Visit.id.desc()).offset(offset).limit(limit)
    ).scalars().all()
    return list(rows), int(total)


# ===========================================================================
# Plan vs actual
# ===========================================================================
def gps_distance_km(
    db: Session,
    *,
    route_id: int | None = None,
    vehicle_id: int | None = None,
    on: date | None = None,
) -> float:
    """Track length from the breadcrumb trail — the only honest 'actual km'."""
    stmt = select(GpsEvent.latitude, GpsEvent.longitude).order_by(
        GpsEvent.recorded_at, GpsEvent.id
    )
    if route_id is not None:
        stmt = stmt.where(GpsEvent.route_id == route_id)
    if vehicle_id is not None:
        stmt = stmt.where(GpsEvent.vehicle_id == vehicle_id)
    if on is not None:
        start = datetime(on.year, on.month, on.day)
        stmt = stmt.where(
            GpsEvent.recorded_at >= start, GpsEvent.recorded_at < start + timedelta(days=1)
        )
    rows = db.execute(stmt).all()
    total = 0.0
    for i in range(1, len(rows)):
        total += haversine_km(rows[i - 1][0], rows[i - 1][1], rows[i][0], rows[i][1])
    return total


def plan_vs_actual(db: Session, route: Route) -> dict[str, Any]:
    """Compare what was planned with what the field actually did."""
    stops = sorted(route.stops, key=lambda s: (s.sequence, s.id))
    completed = sum(1 for s in stops if s.status == StopStatus.COMPLETED)
    skipped = sum(1 for s in stops if s.status == StopStatus.SKIPPED)
    delayed = [s for s in stops if s.delay_minutes > DELAY_TOLERANCE_MIN]

    planned_km = float(route.planned_distance_km or 0.0)
    actual_km = float(route.actual_distance_km or 0.0)
    if actual_km <= 0.0:
        actual_km = round(gps_distance_km(db, route_id=route.id), 3)

    planned_minutes = int(route.planned_duration_min or 0)
    actual_minutes = int(route.actual_duration_min or 0)
    if actual_minutes <= 0 and route.started_at is not None:
        end = route.completed_at or utcnow()
        actual_minutes = max(0, int((end - route.started_at).total_seconds() // 60))

    unvisited = [
        {
            "customer_id": s.customer_id,
            "code": s.customer.code if s.customer else None,
            "name": s.customer.name if s.customer else None,
            "sequence": s.sequence,
            "status": s.status,
            "skip_reason": s.skip_reason,
        }
        for s in stops
        if s.status in (StopStatus.PENDING, StopStatus.SKIPPED, StopStatus.FAILED)
    ]

    return {
        "route_id": route.id,
        "code": route.code,
        "route_date": route.route_date,
        "status": route.status,
        "planned_stops": len(stops),
        "completed": completed,
        "skipped": skipped,
        "planned_km": round(planned_km, 3),
        "actual_km": round(actual_km, 3),
        "planned_minutes": planned_minutes,
        "actual_minutes": actual_minutes,
        "deviation_percent": pct(actual_km - planned_km, planned_km) if planned_km else 0.0,
        "time_deviation_percent": (
            pct(actual_minutes - planned_minutes, planned_minutes) if planned_minutes else 0.0
        ),
        "completion_rate": pct(completed, len(stops)) if stops else 0.0,
        "delayed_stops": [
            {
                "customer_id": s.customer_id,
                "name": s.customer.name if s.customer else None,
                "sequence": s.sequence,
                "planned_arrival": s.planned_arrival,
                "delay_minutes": s.delay_minutes,
            }
            for s in delayed
        ],
        "unvisited_customers": unvisited,
    }


# ===========================================================================
# GPS telemetry
# ===========================================================================
def record_gps(
    db: Session,
    *,
    vehicle_id: int | None,
    points: list[dict[str, Any]],
    salesperson_id: int | None = None,
    route_id: int | None = None,
    day_session_id: int | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """
    Bulk-ingest breadcrumbs and refresh the vehicle's last known position.

    Called from a mobile client on a poor connection, so it is deliberately
    tolerant: malformed points are dropped rather than failing the whole batch.
    """
    if not points:
        return {"inserted": 0, "distance_km": 0.0, "last_position": None}

    vehicle = db.get(Vehicle, vehicle_id) if vehicle_id else None
    previous: tuple[float, float] | None = None
    if vehicle is not None and vehicle.last_lat is not None and vehicle.last_lng is not None:
        previous = (vehicle.last_lat, vehicle.last_lng)

    inserted = 0
    distance = 0.0
    latest: GpsEvent | None = None

    for raw in points:
        try:
            latitude = float(raw["latitude"])
            longitude = float(raw["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
            continue

        recorded = raw.get("recorded_at") or utcnow()
        event = GpsEvent(
            vehicle_id=vehicle_id,
            salesperson_id=salesperson_id or raw.get("salesperson_id"),
            day_session_id=day_session_id or raw.get("day_session_id"),
            route_id=route_id or raw.get("route_id"),
            latitude=latitude,
            longitude=longitude,
            accuracy_m=raw.get("accuracy_m"),
            speed_kmh=raw.get("speed_kmh"),
            heading=raw.get("heading"),
            altitude_m=raw.get("altitude_m"),
            battery_percent=raw.get("battery_percent"),
            event_type=raw.get("event_type") or "PING",
            recorded_at=recorded,
        )
        db.add(event)
        inserted += 1
        if previous is not None:
            distance += haversine_km(previous[0], previous[1], latitude, longitude)
        previous = (latitude, longitude)
        latest = event

    db.flush()

    if vehicle is not None and latest is not None:
        vehicle.last_lat = latest.latitude
        vehicle.last_lng = latest.longitude
        vehicle.last_position_at = latest.recorded_at
        if distance > 0:
            vehicle.odometer_km = float(vehicle.odometer_km or 0.0) + distance

    if route_id and distance > 0:
        route = db.get(Route, route_id)
        if route is not None:
            route.actual_distance_km = round(float(route.actual_distance_km or 0.0) + distance, 3)
    db.flush()

    if commit:
        db.commit()
    return {
        "inserted": inserted,
        "distance_km": round(distance, 3),
        "last_position": (
            {
                "latitude": latest.latitude,
                "longitude": latest.longitude,
                "recorded_at": latest.recorded_at,
            }
            if latest is not None
            else None
        ),
    }


# ===========================================================================
# Map & efficiency
# ===========================================================================
def _latest_positions(db: Session, on_date: date) -> dict[int, GpsEvent]:
    """Most recent breadcrumb per salesperson for the day."""
    start = datetime(on_date.year, on_date.month, on_date.day)
    rows = db.execute(
        select(GpsEvent)
        .where(
            GpsEvent.recorded_at >= start,
            GpsEvent.recorded_at < start + timedelta(days=1),
            GpsEvent.salesperson_id.is_not(None),
        )
        .order_by(GpsEvent.salesperson_id, GpsEvent.recorded_at)
    ).scalars().all()
    latest: dict[int, GpsEvent] = {}
    for event in rows:
        latest[int(event.salesperson_id)] = event      # ordered ascending: last wins
    return latest


def map_snapshot(
    db: Session,
    *,
    on_date: date,
    region_id: int | None = None,
    salesperson_ids: list[int] | None = None,
    include_customers: bool = True,
    customer_limit: int = 2000,
) -> dict[str, Any]:
    """Everything the live map screen draws for one day, in a single round trip."""
    veh_stmt = select(Vehicle).where(Vehicle.is_deleted.is_(False), Vehicle.is_active.is_(True))
    if region_id is not None:
        veh_stmt = veh_stmt.where(Vehicle.region_id == region_id)
    vehicles = db.execute(veh_stmt.order_by(Vehicle.id)).scalars().all()

    sp_stmt = select(Salesperson).where(
        Salesperson.is_deleted.is_(False), Salesperson.is_active.is_(True)
    )
    if region_id is not None:
        sp_stmt = sp_stmt.where(Salesperson.region_id == region_id)
    if salesperson_ids:
        sp_stmt = sp_stmt.where(Salesperson.id.in_(salesperson_ids))
    salespeople = db.execute(sp_stmt.order_by(Salesperson.id)).scalars().all()

    positions = _latest_positions(db, on_date)
    vehicle_by_id = {v.id: v for v in vehicles}

    route_stmt = select(Route).where(
        Route.route_date == on_date, Route.is_deleted.is_(False), Route.is_template.is_(False)
    )
    if region_id is not None:
        route_stmt = route_stmt.where(Route.region_id == region_id)
    route_stmt = _scoped(route_stmt, Route.salesperson_id, salesperson_ids)
    routes = db.execute(route_stmt.order_by(Route.id)).scalars().all()

    warehouses = db.execute(
        select(Warehouse).where(
            Warehouse.is_deleted.is_(False),
            Warehouse.latitude.is_not(None),
            Warehouse.longitude.is_not(None),
            Warehouse.warehouse_type != WarehouseType.VEHICLE,
        ).order_by(Warehouse.id)
    ).scalars().all()

    customers: list[dict[str, Any]] = []
    if include_customers:
        cust_stmt = select(Customer).where(
            Customer.is_deleted.is_(False),
            Customer.latitude.is_not(None),
            Customer.longitude.is_not(None),
        )
        if region_id is not None:
            cust_stmt = cust_stmt.where(Customer.region_id == region_id)
        cust_stmt = _scoped(cust_stmt, Customer.default_salesperson_id, salesperson_ids)
        customers = [
            {
                "id": c.id,
                "code": c.code,
                "name": c.name,
                "latitude": c.latitude,
                "longitude": c.longitude,
                "status": c.status,
                "customer_type": c.customer_type,
                "is_priority": c.is_priority,
                "balance": c.balance,
                "last_visit_date": c.last_visit_date,
            }
            for c in db.execute(cust_stmt.order_by(Customer.id).limit(customer_limit))
            .scalars()
            .all()
        ]

    salesperson_names = {s.id: s.full_name for s in salespeople}

    return {
        "on_date": on_date,
        "vehicles": [
            {
                "id": v.id,
                "code": v.code,
                "plate_number": v.plate_number,
                "status": v.status,
                "latitude": v.last_lat,
                "longitude": v.last_lng,
                "position_at": v.last_position_at,
                "salesperson_id": v.default_salesperson_id,
                "salesperson_name": salesperson_names.get(v.default_salesperson_id or 0),
                "is_refrigerated": v.is_refrigerated,
            }
            for v in vehicles
        ],
        "salespeople": [
            {
                "id": s.id,
                "code": s.code,
                "full_name": s.full_name,
                "vehicle_id": s.default_vehicle_id,
                "latitude": (
                    positions[s.id].latitude
                    if s.id in positions
                    else (vehicle_by_id.get(s.default_vehicle_id or 0).last_lat
                          if vehicle_by_id.get(s.default_vehicle_id or 0) else None)
                ),
                "longitude": (
                    positions[s.id].longitude
                    if s.id in positions
                    else (vehicle_by_id.get(s.default_vehicle_id or 0).last_lng
                          if vehicle_by_id.get(s.default_vehicle_id or 0) else None)
                ),
                "position_at": positions[s.id].recorded_at if s.id in positions else None,
                "speed_kmh": positions[s.id].speed_kmh if s.id in positions else None,
            }
            for s in salespeople
        ],
        "warehouses": [
            {
                "id": w.id,
                "code": w.code,
                "name": w.name,
                "warehouse_type": w.warehouse_type,
                "latitude": w.latitude,
                "longitude": w.longitude,
            }
            for w in warehouses
        ],
        "customers": customers,
        "routes": [
            {
                "id": r.id,
                "code": r.code,
                "name": r.name,
                "status": r.status,
                "salesperson_id": r.salesperson_id,
                "salesperson_name": salesperson_names.get(r.salesperson_id or 0),
                "vehicle_id": r.vehicle_id,
                "planned_distance_km": r.planned_distance_km,
                "completed_stops": r.completed_stops,
                "planned_stops": r.planned_stops,
                "points": [
                    {
                        "sequence": s.sequence,
                        "customer_id": s.customer_id,
                        "name": s.customer.name if s.customer else None,
                        "latitude": s.customer.latitude if s.customer else None,
                        "longitude": s.customer.longitude if s.customer else None,
                        "status": s.status,
                        "planned_arrival": s.planned_arrival,
                    }
                    for s in sorted(r.stops, key=lambda x: (x.sequence, x.id))
                    if s.customer is not None
                    and s.customer.latitude is not None
                    and s.customer.longitude is not None
                ],
            }
            for r in routes
        ],
    }


def route_efficiency(
    db: Session,
    *,
    start: date,
    end: date,
    salesperson_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    """
    Per-salesperson field productivity for the period.

    The three numbers a field manager actually steers on: kilometres burned per
    sale, stops served per hour, and average drop size.
    """
    route_stmt = (
        select(
            Route.salesperson_id,
            func.count(Route.id),
            func.coalesce(func.sum(Route.planned_distance_km), 0.0),
            func.coalesce(func.sum(Route.actual_distance_km), 0.0),
            func.coalesce(func.sum(Route.planned_stops), 0),
            func.coalesce(func.sum(Route.completed_stops), 0),
            func.coalesce(func.sum(Route.actual_duration_min), 0),
        )
        .where(
            Route.route_date >= start,
            Route.route_date <= end,
            Route.is_deleted.is_(False),
            Route.is_template.is_(False),
            Route.salesperson_id.is_not(None),
        )
        .group_by(Route.salesperson_id)
    )
    route_stmt = _scoped(route_stmt, Route.salesperson_id, salesperson_ids)
    route_rows = {int(r[0]): r for r in db.execute(route_stmt).all()}

    sale_stmt = (
        select(
            Sale.salesperson_id,
            func.count(Sale.id),
            func.coalesce(func.sum(Sale.total_amount), 0),
        )
        .where(
            Sale.sale_date >= start,
            Sale.sale_date <= end,
            Sale.is_cancelled.is_(False),
            Sale.is_deleted.is_(False),
            Sale.salesperson_id.is_not(None),
        )
        .group_by(Sale.salesperson_id)
    )
    sale_stmt = _scoped(sale_stmt, Sale.salesperson_id, salesperson_ids)
    sale_rows = {int(r[0]): r for r in db.execute(sale_stmt).all()}

    visit_stmt = (
        select(Visit.salesperson_id, func.count(Visit.id))
        .where(
            Visit.visit_date >= start,
            Visit.visit_date <= end,
            Visit.salesperson_id.is_not(None),
        )
        .group_by(Visit.salesperson_id)
    )
    visit_stmt = _scoped(visit_stmt, Visit.salesperson_id, salesperson_ids)
    visit_rows = {int(r[0]): int(r[1]) for r in db.execute(visit_stmt).all()}

    ids = sorted(set(route_rows) | set(sale_rows) | set(visit_rows))
    if not ids:
        return []
    names = {
        s.id: (s.full_name, s.code)
        for s in db.execute(
            select(Salesperson).where(Salesperson.id.in_(ids))
        ).scalars().all()
    }

    out: list[dict[str, Any]] = []
    for sp_id in ids:
        r = route_rows.get(sp_id)
        s = sale_rows.get(sp_id)
        routes_count = int(r[1]) if r else 0
        planned_km = float(r[2]) if r else 0.0
        actual_km = float(r[3]) if r else 0.0
        stops_planned = int(r[4]) if r else 0
        stops_completed = int(r[5]) if r else 0
        minutes = int(r[6]) if r else 0
        sales_count = int(s[1]) if s else 0
        revenue = money(s[2]) if s else Decimal("0")
        visits = visit_rows.get(sp_id, 0)
        driven = actual_km or planned_km
        hours = minutes / 60.0

        name, code = names.get(sp_id, (None, None))
        out.append(
            {
                "salesperson_id": sp_id,
                "salesperson_code": code,
                "salesperson_name": name,
                "routes": routes_count,
                "planned_km": round(planned_km, 2),
                "actual_km": round(actual_km, 2),
                "stops_planned": stops_planned,
                "stops_completed": stops_completed,
                "visits": visits,
                "working_hours": round(hours, 2),
                "sales_count": sales_count,
                "revenue": revenue,
                "km_per_sale": round(safe_div(driven, sales_count), 2),
                "stops_per_hour": round(safe_div(stops_completed, hours), 2),
                "drop_size": money(safe_div(revenue, sales_count)),
                "completion_rate": pct(stops_completed, stops_planned),
                "strike_rate": pct(sales_count, visits),
            }
        )
    out.sort(key=lambda row: (-row["stops_per_hour"], row["salesperson_id"]))
    return out
