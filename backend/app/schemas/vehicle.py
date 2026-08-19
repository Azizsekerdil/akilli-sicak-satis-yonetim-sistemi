"""Vehicle, salesperson, day-session and van load-out schemas."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from app.core.enums import VehicleStatus, VehicleType
from app.schemas.common import ORMModel


# ===========================================================================
# Vehicles
# ===========================================================================
class VehicleCreate(BaseModel):
    plate_number: str = Field(min_length=2, max_length=24)
    name: str | None = Field(default=None, max_length=128)
    code: str | None = Field(default=None, max_length=32)
    vehicle_type: VehicleType = VehicleType.VAN
    region_id: int | None = None
    home_warehouse_id: int | None = None
    capacity_volume_l: float = Field(default=8000.0, gt=0)
    capacity_weight_kg: float = Field(default=3500.0, gt=0)
    capacity_cases: int | None = Field(default=None, ge=0)
    default_salesperson_id: int | None = None
    default_driver_id: int | None = None
    brand: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=64)
    model_year: int | None = Field(default=None, ge=1950, le=2100)
    is_refrigerated: bool = False
    fuel_type: str | None = Field(default=None, max_length=24)
    avg_consumption_l_100km: float | None = Field(default=None, ge=0)
    odometer_km: float = Field(default=0.0, ge=0)
    insurance_expiry: date | None = None
    inspection_expiry: date | None = None
    last_maintenance_at: date | None = None
    notes: str | None = None


class VehicleUpdate(BaseModel):
    plate_number: str | None = Field(default=None, min_length=2, max_length=24)
    name: str | None = Field(default=None, max_length=128)
    vehicle_type: VehicleType | None = None
    status: VehicleStatus | None = None
    is_active: bool | None = None
    region_id: int | None = None
    home_warehouse_id: int | None = None
    capacity_volume_l: float | None = Field(default=None, gt=0)
    capacity_weight_kg: float | None = Field(default=None, gt=0)
    capacity_cases: int | None = Field(default=None, ge=0)
    default_salesperson_id: int | None = None
    default_driver_id: int | None = None
    brand: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=64)
    model_year: int | None = Field(default=None, ge=1950, le=2100)
    is_refrigerated: bool | None = None
    fuel_type: str | None = Field(default=None, max_length=24)
    avg_consumption_l_100km: float | None = Field(default=None, ge=0)
    odometer_km: float | None = Field(default=None, ge=0)
    insurance_expiry: date | None = None
    inspection_expiry: date | None = None
    last_maintenance_at: date | None = None
    notes: str | None = None


class VehicleOut(ORMModel):
    id: int
    code: str
    plate_number: str
    name: str | None = None
    warehouse_id: int | None = None
    home_warehouse_id: int | None = None
    region_id: int | None = None
    vehicle_type: str
    status: str
    is_active: bool = True
    brand: str | None = None
    model: str | None = None
    model_year: int | None = None
    is_refrigerated: bool = False
    capacity_volume_l: float = 0.0
    capacity_weight_kg: float = 0.0
    capacity_cases: int | None = None
    default_driver_id: int | None = None
    default_salesperson_id: int | None = None
    fuel_type: str | None = None
    avg_consumption_l_100km: float | None = None
    odometer_km: float = 0.0
    insurance_expiry: date | None = None
    inspection_expiry: date | None = None
    last_maintenance_at: date | None = None
    last_lat: float | None = None
    last_lng: float | None = None
    last_position_at: datetime | None = None
    notes: str | None = None
    created_at: datetime | None = None


class AssignVehicleIn(BaseModel):
    salesperson_id: int


class CapacityOut(BaseModel):
    """How full the van is against its declared capacity."""

    vehicle_id: int
    plate_number: str
    warehouse_id: int
    volume_l: float = 0.0
    weight_kg: float = 0.0
    capacity_volume_l: float = 0.0
    capacity_weight_kg: float = 0.0
    volume_percent: float = 0.0
    weight_percent: float = 0.0
    base_quantity: Decimal = Decimal("0")
    product_count: int = 0


class VanStockLot(BaseModel):
    lot_id: int
    quantity: Decimal = Decimal("0")


class VanStockRow(BaseModel):
    product_id: int
    sku: str | None = None
    product_name: str | None = None
    base_uom: str | None = None
    units_per_case: Decimal = Decimal("1")
    base_quantity: Decimal = Decimal("0")
    reserved_quantity: Decimal = Decimal("0")
    available_quantity: Decimal = Decimal("0")
    value: Decimal = Decimal("0")
    volume_l: float = 0.0
    weight_kg: float = 0.0
    lots: list[VanStockLot] = Field(default_factory=list)


class MaintenanceWarningOut(BaseModel):
    vehicle_id: int
    plate_number: str
    kind: str                       # INSURANCE | INSPECTION
    expiry_date: date
    days_left: int
    is_expired: bool = False
    severity: str = "INFO"


class PositionIn(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy_m: float | None = Field(default=None, ge=0)
    speed_kmh: float | None = Field(default=None, ge=0)
    heading: float | None = Field(default=None, ge=0, le=360)
    altitude_m: float | None = None
    battery_percent: int | None = Field(default=None, ge=0, le=100)
    event_type: str = Field(default="PING", max_length=24)
    recorded_at: datetime | None = None
    odometer_km: float | None = Field(default=None, ge=0)
    salesperson_id: int | None = None
    day_session_id: int | None = None
    route_id: int | None = None


class PositionOut(ORMModel):
    id: int
    vehicle_id: int | None = None
    salesperson_id: int | None = None
    day_session_id: int | None = None
    route_id: int | None = None
    latitude: float
    longitude: float
    accuracy_m: float | None = None
    speed_kmh: float | None = None
    heading: float | None = None
    event_type: str | None = None
    recorded_at: datetime


# ===========================================================================
# Salespeople
# ===========================================================================
class SalespersonCreate(BaseModel):
    full_name: str = Field(min_length=2, max_length=255)
    code: str | None = Field(default=None, max_length=32)
    user_id: int | None = None
    phone: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=255)
    region_id: int | None = None
    supervisor_id: int | None = None
    default_vehicle_id: int | None = None
    default_warehouse_id: int | None = None
    hire_date: date | None = None
    commission_percent: float = Field(default=0.0, ge=0, le=100)
    max_discount_percent: float = Field(default=10.0, ge=0, le=100)
    can_sell_on_credit: bool = True
    cash_limit: Decimal = Decimal("0")
    notes: str | None = None


class SalespersonUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=255)
    user_id: int | None = None
    phone: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=255)
    region_id: int | None = None
    supervisor_id: int | None = None
    default_vehicle_id: int | None = None
    default_warehouse_id: int | None = None
    hire_date: date | None = None
    is_active: bool | None = None
    commission_percent: float | None = Field(default=None, ge=0, le=100)
    max_discount_percent: float | None = Field(default=None, ge=0, le=100)
    can_sell_on_credit: bool | None = None
    cash_limit: Decimal | None = None
    notes: str | None = None


class SalespersonOut(ORMModel):
    id: int
    code: str
    user_id: int | None = None
    full_name: str
    phone: str | None = None
    email: str | None = None
    region_id: int | None = None
    supervisor_id: int | None = None
    default_vehicle_id: int | None = None
    default_warehouse_id: int | None = None
    hire_date: date | None = None
    is_active: bool = True
    commission_percent: float = 0.0
    max_discount_percent: float = 0.0
    can_sell_on_credit: bool = True
    cash_limit: Decimal = Decimal("0")
    notes: str | None = None
    created_at: datetime | None = None


# ===========================================================================
# Day sessions
# ===========================================================================
class OpenDayIn(BaseModel):
    salesperson_id: int
    vehicle_id: int
    route_id: int | None = None
    start_odometer: float | None = Field(default=None, ge=0)
    session_date: date | None = None
    notes: str | None = None


class CountLineIn(BaseModel):
    """One physically counted product at day end."""

    product_id: int
    quantity: Decimal = Field(ge=0)
    uom: str | None = None


class CloseDayIn(BaseModel):
    counted: list[CountLineIn] | None = None
    declared_cash: Decimal = Field(default=Decimal("0"), ge=0)
    end_odometer: float | None = Field(default=None, ge=0)
    notes: str | None = None


class DaySessionOut(ORMModel):
    id: int
    session_date: date
    salesperson_id: int
    vehicle_id: int
    route_id: int | None = None
    warehouse_id: int | None = None
    status: str
    opened_at: datetime | None = None
    closed_at: datetime | None = None
    opened_by_id: int | None = None
    closed_by_id: int | None = None
    start_odometer_km: float | None = None
    end_odometer_km: float | None = None

    loaded_qty: Decimal = Decimal("0")
    reloaded_qty: Decimal = Decimal("0")
    sold_qty: Decimal = Decimal("0")
    returned_qty: Decimal = Decimal("0")
    wastage_qty: Decimal = Decimal("0")
    theoretical_qty: Decimal = Decimal("0")
    counted_qty: Decimal = Decimal("0")
    variance_qty: Decimal = Decimal("0")
    variance_value: Decimal = Decimal("0")

    total_sales_amount: Decimal = Decimal("0")
    total_collected_cash: Decimal = Decimal("0")
    total_collected_other: Decimal = Decimal("0")
    declared_cash: Decimal = Decimal("0")
    cash_variance: Decimal = Decimal("0")

    visits_planned: int = 0
    visits_done: int = 0
    invoices_count: int = 0
    has_variance: bool = False
    notes: str | None = None


class ReconciliationRow(BaseModel):
    """
    One product's day movement, in base units.

    ``opening + loaded + reloaded - sold + returned - wastage = theoretical``
    and ``variance = theoretical - counted`` (positive means stock is missing).
    """

    product_id: int
    sku: str | None = None
    product_name: str | None = None
    base_uom: str | None = None
    opening: Decimal = Decimal("0")
    loaded: Decimal = Decimal("0")
    reloaded: Decimal = Decimal("0")
    sold: Decimal = Decimal("0")
    returned: Decimal = Decimal("0")
    wastage: Decimal = Decimal("0")
    #: Movements outside the day flow (manual adjustment, transfer).
    other: Decimal = Decimal("0")
    on_hand: Decimal = Decimal("0")
    theoretical: Decimal = Decimal("0")
    counted: Decimal | None = None
    variance: Decimal | None = None
    unit_cost: Decimal = Decimal("0")
    variance_value: Decimal | None = None


class ReconciliationOut(BaseModel):
    session: DaySessionOut
    rows: list[ReconciliationRow] = Field(default_factory=list)
    total_variance_qty: Decimal = Decimal("0")
    total_variance_value: Decimal = Decimal("0")
    cash_expected: Decimal = Decimal("0")
    cash_declared: Decimal = Decimal("0")
    cash_variance: Decimal = Decimal("0")


# ===========================================================================
# Van loads
# ===========================================================================
class VanLoadItem(BaseModel):
    """A requested load line, in the picker's unit of measure."""

    product_id: int
    quantity: Decimal = Field(gt=0)
    uom: str | None = None
    planned_quantity: Decimal | None = Field(default=None, ge=0)
    lot_id: int | None = None


class VanLoadItemOut(ORMModel):
    id: int
    product_id: int
    lot_id: int | None = None
    planned_quantity: Decimal = Decimal("0")
    quantity: Decimal = Decimal("0")
    uom: str
    base_quantity: Decimal = Decimal("0")
    unit_cost: Decimal = Decimal("0")
    ai_reason: str | None = None


class VanLoadCreate(BaseModel):
    vehicle_id: int
    source_warehouse_id: int
    lines: list[VanLoadItem] = Field(min_length=1)
    salesperson_id: int | None = None
    load_date: date | None = None
    is_reload: bool = False
    day_session_id: int | None = None
    notes: str | None = None


class VanLoadOut(ORMModel):
    id: int
    document_no: str
    load_date: date
    day_session_id: int | None = None
    vehicle_id: int
    salesperson_id: int | None = None
    source_warehouse_id: int
    is_reload: bool = False
    is_ai_suggested: bool = False
    ai_confidence: float | None = None
    ai_explanation: str | None = None
    is_posted: bool = False
    posted_at: datetime | None = None
    total_volume_l: float = 0.0
    total_weight_kg: float = 0.0
    total_cost: Decimal = Decimal("0")
    notes: str | None = None
    items: list[VanLoadItemOut] = Field(default_factory=list)
    created_at: datetime | None = None


class UnloadIn(BaseModel):
    vehicle_id: int
    target_warehouse_id: int
    #: ``None`` unloads everything currently on the van.
    lines: list[VanLoadItem] | None = None
    salesperson_id: int | None = None
    day_session_id: int | None = None


class UnloadLineOut(BaseModel):
    product_id: int
    base_quantity: Decimal = Decimal("0")
    value: Decimal = Decimal("0")


class UnloadOut(BaseModel):
    vehicle_id: int
    van_warehouse_id: int
    target_warehouse_id: int
    day_session_id: int | None = None
    reference_no: str
    lines: list[UnloadLineOut] = Field(default_factory=list)
    total_base_quantity: Decimal = Decimal("0")
    total_value: Decimal = Decimal("0")
