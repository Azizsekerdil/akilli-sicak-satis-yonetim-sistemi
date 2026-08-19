"""Request/response schemas for route planning, visits, GPS and the map screen."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from app.core.enums import RouteStatus, StopStatus, VisitOutcome
from app.schemas.common import ORMModel


# ===========================================================================
# Stops
# ===========================================================================
class RouteStopIn(BaseModel):
    customer_id: int
    sequence: int | None = None
    service_time_minutes: int | None = Field(default=None, ge=0, le=600)
    is_priority: bool | None = None
    planned_arrival: str | None = Field(default=None, max_length=8)


class RouteStopOut(ORMModel):
    id: int
    customer_id: int
    customer_code: str | None = None
    customer_name: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    address: str | None = None
    phone: str | None = None

    sequence: int = 0
    status: str = StopStatus.PENDING
    planned_arrival: str | None = None
    planned_departure: str | None = None
    service_time_minutes: int = 10
    distance_from_previous_km: float = 0.0
    travel_time_from_previous_min: int = 0

    arrived_at: datetime | None = None
    departed_at: datetime | None = None
    arrival_lat: float | None = None
    arrival_lng: float | None = None
    geofence_distance_m: float | None = None
    delay_minutes: int = 0
    skip_reason: str | None = None
    is_priority: bool = False


# ===========================================================================
# Routes
# ===========================================================================
class RouteBase(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    is_template: bool = False
    template_id: int | None = None
    route_date: date | None = None
    weekday: str | None = Field(default=None, max_length=8)
    salesperson_id: int | None = None
    vehicle_id: int | None = None
    region_id: int | None = None
    start_warehouse_id: int | None = None
    end_warehouse_id: int | None = None
    planned_start_time: str | None = Field(default=None, max_length=8)


class RouteCreate(RouteBase):
    code: str | None = Field(default=None, max_length=32)
    stops: list[RouteStopIn] = Field(default_factory=list)


class RouteUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    description: str | None = None
    route_date: date | None = None
    weekday: str | None = Field(default=None, max_length=8)
    salesperson_id: int | None = None
    vehicle_id: int | None = None
    region_id: int | None = None
    start_warehouse_id: int | None = None
    end_warehouse_id: int | None = None
    planned_start_time: str | None = Field(default=None, max_length=8)
    status: str | None = None
    is_active: bool | None = None
    #: ``None`` leaves membership untouched; a list replaces it wholesale.
    stops: list[RouteStopIn] | None = None


class RouteListItem(ORMModel):
    id: int
    code: str
    name: str
    is_template: bool = False
    route_date: date | None = None
    weekday: str | None = None
    status: str = RouteStatus.PLANNED
    salesperson_id: int | None = None
    vehicle_id: int | None = None
    region_id: int | None = None
    planned_stops: int = 0
    completed_stops: int = 0
    skipped_stops: int = 0
    planned_distance_km: float = 0.0
    actual_distance_km: float = 0.0
    planned_duration_min: int = 0
    actual_duration_min: int = 0
    total_sales_amount: Decimal = Decimal("0")
    is_optimized: bool = False
    optimizer: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    is_active: bool = True


class RouteOut(RouteListItem):
    description: str | None = None
    template_id: int | None = None
    start_warehouse_id: int | None = None
    end_warehouse_id: int | None = None
    planned_start_time: str | None = None
    planned_volume_l: float = 0.0
    planned_weight_kg: float = 0.0
    actual_stops: int = 0
    optimized_at: datetime | None = None
    optimization_seconds: float | None = None
    optimization_note: str | None = None
    completion_rate: float = 0.0
    salesperson_name: str | None = None
    vehicle_plate: str | None = None
    stops: list[RouteStopOut] = Field(default_factory=list)


# ===========================================================================
# Optimisation
# ===========================================================================
class OptimizeIn(BaseModel):
    prefer_exact: bool = True
    time_limit_s: int = Field(default=10, ge=1, le=120)


class OptimizeOut(BaseModel):
    route_id: int
    code: str
    solver: str
    seconds: float = 0.0
    stops: int = 0
    distance_km: float = 0.0
    duration_min: int = 0
    objective: float = 0.0
    unassigned_customer_ids: list[int] = Field(default_factory=list)
    message: str = ""


class MultiOptimizeIn(BaseModel):
    on_date: date
    vehicle_ids: list[int] = Field(min_length=1)
    customer_ids: list[int] = Field(min_length=1)
    region_id: int | None = None
    prefer_exact: bool = True
    time_limit_s: int = Field(default=15, ge=1, le=300)
    #: Cap each van's share so the whole fleet is used, even when one van could
    #: cover everything more cheaply.
    balance: bool = False


class MultiOptimizeOut(BaseModel):
    on_date: date
    solver: str
    seconds: float = 0.0
    total_distance_km: float = 0.0
    objective: float = 0.0
    vehicles_used: int = 0
    vehicles_offered: int = 0
    routes: list[RouteListItem] = Field(default_factory=list)
    unassigned_customer_ids: list[int] = Field(default_factory=list)
    message: str = ""


class GenerateDailyIn(BaseModel):
    on_date: date
    region_id: int | None = None


class GenerateFromTemplateIn(BaseModel):
    template_id: int
    on_date: date


class GenerateDailyOut(BaseModel):
    on_date: date
    weekday: str
    created: int = 0
    updated: int = 0
    skipped: int = 0
    customers_planned: int = 0
    routes: list[RouteListItem] = Field(default_factory=list)
    message: str = ""


# ===========================================================================
# Execution
# ===========================================================================
class RouteStartIn(BaseModel):
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    odometer_km: float | None = Field(default=None, ge=0)


class RouteCompleteIn(BaseModel):
    actual_distance_km: float | None = Field(default=None, ge=0)
    odometer_km: float | None = Field(default=None, ge=0)
    notes: str | None = None


class ArriveIn(BaseModel):
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)


class ArriveOut(BaseModel):
    stop_id: int
    customer_id: int
    status: str
    arrived_at: datetime | None = None
    geofence_distance_m: float | None = None
    in_geofence: bool | None = None
    geofence_radius_m: float = 150.0
    delay_minutes: int = 0


class SkipIn(BaseModel):
    reason: str = Field(min_length=1, max_length=255)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)


class SkipOut(BaseModel):
    stop_id: int
    customer_id: int
    status: str
    skip_reason: str | None = None
    skipped_stops: int = 0


# ===========================================================================
# Visits
# ===========================================================================
class VisitCreate(BaseModel):
    customer_id: int
    salesperson_id: int | None = None
    outcome: str = VisitOutcome.NO_ORDER
    visit_date: date | None = None
    vehicle_id: int | None = None
    route_id: int | None = None
    route_stop_id: int | None = None
    day_session_id: int | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_minutes: int | None = Field(default=None, ge=0, le=1440)
    sale_amount: Decimal = Decimal("0")
    collected_amount: Decimal = Decimal("0")
    return_amount: Decimal = Decimal("0")
    lines_count: int = Field(default=0, ge=0)
    photo_path: str | None = Field(default=None, max_length=512)
    signature_path: str | None = Field(default=None, max_length=512)
    notes: str | None = None


class VisitOut(ORMModel):
    id: int
    visit_date: date
    customer_id: int
    customer_code: str | None = None
    customer_name: str | None = None
    salesperson_id: int | None = None
    vehicle_id: int | None = None
    route_id: int | None = None
    route_stop_id: int | None = None
    day_session_id: int | None = None
    outcome: str = VisitOutcome.NO_ORDER
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_minutes: int = 0
    latitude: float | None = None
    longitude: float | None = None
    is_in_geofence: bool | None = None
    is_unplanned: bool = False
    sale_amount: Decimal = Decimal("0")
    collected_amount: Decimal = Decimal("0")
    return_amount: Decimal = Decimal("0")
    lines_count: int = 0
    photo_path: str | None = None
    signature_path: str | None = None
    notes: str | None = None
    created_at: datetime | None = None


# ===========================================================================
# GPS
# ===========================================================================
class GpsPointIn(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    recorded_at: datetime | None = None
    accuracy_m: float | None = Field(default=None, ge=0)
    speed_kmh: float | None = Field(default=None, ge=0, le=400)
    heading: float | None = Field(default=None, ge=0, le=360)
    altitude_m: float | None = None
    battery_percent: int | None = Field(default=None, ge=0, le=100)
    event_type: str | None = Field(default=None, max_length=24)


class GpsBatchIn(BaseModel):
    vehicle_id: int | None = None
    salesperson_id: int | None = None
    route_id: int | None = None
    day_session_id: int | None = None
    points: list[GpsPointIn] = Field(default_factory=list, max_length=1000)


class GpsBatchOut(BaseModel):
    inserted: int = 0
    distance_km: float = 0.0
    last_position: dict[str, Any] | None = None


# ===========================================================================
# Map snapshot
# ===========================================================================
class MapVehicle(BaseModel):
    id: int
    code: str
    plate_number: str
    status: str
    latitude: float | None = None
    longitude: float | None = None
    position_at: datetime | None = None
    salesperson_id: int | None = None
    salesperson_name: str | None = None
    is_refrigerated: bool = False


class MapSalesperson(BaseModel):
    id: int
    code: str
    full_name: str
    vehicle_id: int | None = None
    latitude: float | None = None
    longitude: float | None = None
    position_at: datetime | None = None
    speed_kmh: float | None = None


class MapCustomer(BaseModel):
    id: int
    code: str
    name: str
    latitude: float | None = None
    longitude: float | None = None
    status: str
    customer_type: str
    is_priority: bool = False
    balance: Decimal = Decimal("0")
    last_visit_date: date | None = None


class MapWarehouse(BaseModel):
    id: int
    code: str
    name: str
    warehouse_type: str
    latitude: float | None = None
    longitude: float | None = None


class MapRoutePoint(BaseModel):
    sequence: int
    customer_id: int
    name: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    status: str
    planned_arrival: str | None = None


class MapRoute(BaseModel):
    id: int
    code: str
    name: str
    status: str
    salesperson_id: int | None = None
    salesperson_name: str | None = None
    vehicle_id: int | None = None
    planned_distance_km: float = 0.0
    planned_stops: int = 0
    completed_stops: int = 0
    points: list[MapRoutePoint] = Field(default_factory=list)


class MapSnapshotOut(BaseModel):
    on_date: date
    vehicles: list[MapVehicle] = Field(default_factory=list)
    salespeople: list[MapSalesperson] = Field(default_factory=list)
    customers: list[MapCustomer] = Field(default_factory=list)
    warehouses: list[MapWarehouse] = Field(default_factory=list)
    routes: list[MapRoute] = Field(default_factory=list)


# ===========================================================================
# Plan vs actual & efficiency
# ===========================================================================
class DelayedStop(BaseModel):
    customer_id: int
    name: str | None = None
    sequence: int = 0
    planned_arrival: str | None = None
    delay_minutes: int = 0


class UnvisitedCustomer(BaseModel):
    customer_id: int
    code: str | None = None
    name: str | None = None
    sequence: int = 0
    status: str = StopStatus.PENDING
    skip_reason: str | None = None


class PlanVsActualOut(BaseModel):
    route_id: int
    code: str
    route_date: date | None = None
    status: str
    planned_stops: int = 0
    completed: int = 0
    skipped: int = 0
    planned_km: float = 0.0
    actual_km: float = 0.0
    planned_minutes: int = 0
    actual_minutes: int = 0
    deviation_percent: float = 0.0
    time_deviation_percent: float = 0.0
    completion_rate: float = 0.0
    delayed_stops: list[DelayedStop] = Field(default_factory=list)
    unvisited_customers: list[UnvisitedCustomer] = Field(default_factory=list)


class EfficiencyRow(BaseModel):
    salesperson_id: int
    salesperson_code: str | None = None
    salesperson_name: str | None = None
    routes: int = 0
    planned_km: float = 0.0
    actual_km: float = 0.0
    stops_planned: int = 0
    stops_completed: int = 0
    visits: int = 0
    working_hours: float = 0.0
    sales_count: int = 0
    revenue: Decimal = Decimal("0")
    km_per_sale: float = 0.0
    stops_per_hour: float = 0.0
    drop_size: Decimal = Decimal("0")
    completion_rate: float = 0.0
    strike_rate: float = 0.0
