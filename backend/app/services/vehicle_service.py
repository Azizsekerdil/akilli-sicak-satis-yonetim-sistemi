"""
Vehicles, salespeople and vehicle physical state.

A van is only usable once it owns a ``VEHICLE``-type warehouse: every stock
rule in the system is written against warehouses, so provisioning that
warehouse when the vehicle is created is what turns the van into a first-class
stock location instead of a special case.

Capacity is evaluated in **base units** converted to litres/kilograms through
the product master, because the field app loads in cases while the ledger — and
therefore every capacity figure derived from it — is kept in base units.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from app.core.enums import AuditAction, VehicleStatus, VehicleType, WarehouseType
from app.core.exceptions import (
    BusinessRuleError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from app.core.utils import D, money, pct, tr_upper
from app.models.base import utcnow
from app.models.product import Product
from app.models.route import GpsEvent
from app.models.vehicle import DaySession, Salesperson, Vehicle
from app.models.warehouse import Warehouse
from app.services import audit_service, numbering_service, stock_service

#: How far ahead insurance / inspection expiries are flagged.
EXPIRY_WARNING_DAYS = 30

#: Vehicle statuses that may still be loaded and sold from.
_USABLE_STATUSES = frozenset({VehicleStatus.ACTIVE})

_VEHICLE_UPDATABLE = (
    "name", "region_id", "home_warehouse_id", "vehicle_type", "status",
    "is_active", "brand", "model", "model_year", "is_refrigerated",
    "capacity_volume_l", "capacity_weight_kg", "capacity_cases",
    "default_driver_id", "default_salesperson_id", "fuel_type",
    "avg_consumption_l_100km", "odometer_km", "insurance_expiry",
    "inspection_expiry", "last_maintenance_at", "notes",
)

_SALESPERSON_UPDATABLE = (
    "full_name", "phone", "email", "region_id", "supervisor_id",
    "default_vehicle_id", "default_warehouse_id", "hire_date", "is_active",
    "commission_percent", "max_discount_percent", "can_sell_on_credit",
    "cash_limit", "user_id", "notes",
)


# ===========================================================================
# Helpers
# ===========================================================================
def normalize_plate(plate: str) -> str:
    """Plates are compared without spacing and in Turkish-correct uppercase."""
    return tr_upper((plate or "").strip()).replace(" ", "").replace("-", "")


def unit_volume_l(product: Product) -> float:
    """Litres occupied by one **base unit** of the product."""
    if product.unit_volume_l:
        return float(product.unit_volume_l)
    upc = float(D(product.units_per_case) or 1)
    if product.case_volume_l and upc:
        return float(product.case_volume_l) / upc
    return 0.0


def unit_weight_kg(product: Product) -> float:
    """Kilograms of one **base unit** of the product."""
    if product.unit_weight_kg:
        return float(product.unit_weight_kg)
    upc = float(D(product.units_per_case) or 1)
    if product.case_weight_kg and upc:
        return float(product.case_weight_kg) / upc
    return 0.0


def get_vehicle(db: Session, vehicle_id: int) -> Vehicle:
    vehicle = db.get(Vehicle, vehicle_id)
    if vehicle is None or vehicle.is_deleted:
        raise NotFoundError("vehicle.not_found", params={"id": vehicle_id})
    return vehicle


def get_salesperson(db: Session, salesperson_id: int) -> Salesperson:
    person = db.get(Salesperson, salesperson_id)
    if person is None or person.is_deleted:
        raise NotFoundError("error.not_found", params={"id": salesperson_id})
    return person


def warehouse_for(db: Session, vehicle_id: int) -> Warehouse:
    """The van's stock location.  Every van stock path goes through here."""
    vehicle = get_vehicle(db, vehicle_id)
    if not vehicle.warehouse_id:
        raise NotFoundError("vehicle.no_warehouse", params={"vehicle_id": vehicle_id})
    warehouse = db.get(Warehouse, vehicle.warehouse_id)
    if warehouse is None:
        raise NotFoundError("vehicle.no_warehouse", params={"vehicle_id": vehicle_id})
    return warehouse


def require_usable(vehicle: Vehicle) -> None:
    """A van in maintenance must not be loaded or sold from."""
    if not vehicle.is_active or vehicle.status not in _USABLE_STATUSES:
        raise BusinessRuleError("vehicle.inactive", params={"plate": vehicle.plate_number})


def ensure_warehouse(db: Session, vehicle: Vehicle, *, user_id: int | None = None) -> Warehouse:
    """
    Return the van's warehouse, creating it if the vehicle has none.

    Idempotent: an existing ``VH-<plate>`` warehouse is adopted rather than
    duplicated, so re-creating a vehicle after a soft delete keeps its history.
    """
    if vehicle.warehouse_id:
        existing = db.get(Warehouse, vehicle.warehouse_id)
        if existing is not None:
            return existing

    code = f"VH-{normalize_plate(vehicle.plate_number)}"
    warehouse = db.execute(
        select(Warehouse).where(Warehouse.code == code)
    ).scalar_one_or_none()

    if warehouse is None:
        home = db.get(Warehouse, vehicle.home_warehouse_id) if vehicle.home_warehouse_id else None
        warehouse = Warehouse(
            code=code,
            name=vehicle.name or vehicle.plate_number,
            name_en=vehicle.name or vehicle.plate_number,
            warehouse_type=WarehouseType.VEHICLE,
            region_id=vehicle.region_id or (home.region_id if home else None),
            company_id=home.company_id if home else None,
            branch_id=home.branch_id if home else None,
            parent_id=home.id if home else None,
            capacity_volume_l=vehicle.capacity_volume_l,
            capacity_weight_kg=vehicle.capacity_weight_kg,
            allows_negative_stock=False,
            allocation_strategy="FEFO",
            is_active=True,
            created_by_id=user_id,
        )
        db.add(warehouse)
        db.flush()

    vehicle.warehouse_id = warehouse.id
    db.flush()
    return warehouse


# ===========================================================================
# Vehicle CRUD
# ===========================================================================
def list_vehicles_query(
    db: Session,
    *,
    search: str | None = None,
    status: str | None = None,
    vehicle_type: str | None = None,
    region_id: int | None = None,
    is_active: bool | None = None,
    salesperson_ids: list[int] | None = None,
) -> Select[tuple[Vehicle]]:
    """Filtered vehicle query — the API adds ordering and pagination."""
    stmt = select(Vehicle).where(Vehicle.is_deleted.is_(False))

    if search:
        term = f"%{search.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Vehicle.plate_number).like(term),
                func.lower(Vehicle.code).like(term),
                func.lower(func.coalesce(Vehicle.name, "")).like(term),
            )
        )
    if status:
        stmt = stmt.where(Vehicle.status == status)
    if vehicle_type:
        stmt = stmt.where(Vehicle.vehicle_type == vehicle_type)
    if region_id is not None:
        stmt = stmt.where(Vehicle.region_id == region_id)
    if is_active is not None:
        stmt = stmt.where(Vehicle.is_active.is_(is_active))
    if salesperson_ids:
        # A salesperson also sees any van they have actually worked a day on,
        # not only the one assigned to them by default.
        driven = select(DaySession.vehicle_id).where(
            DaySession.salesperson_id.in_(salesperson_ids)
        )
        stmt = stmt.where(
            or_(
                Vehicle.default_salesperson_id.in_(salesperson_ids),
                Vehicle.id.in_(driven),
            )
        )
    return stmt


def create_vehicle(
    db: Session,
    *,
    plate_number: str,
    name: str | None = None,
    code: str | None = None,
    vehicle_type: str = VehicleType.VAN,
    region_id: int | None = None,
    home_warehouse_id: int | None = None,
    capacity_volume_l: float = 8000.0,
    capacity_weight_kg: float = 3500.0,
    capacity_cases: int | None = None,
    default_salesperson_id: int | None = None,
    default_driver_id: int | None = None,
    user_id: int | None = None,
    commit: bool = True,
    **extra: Any,
) -> Vehicle:
    """Create a vehicle and provision its stock location in one transaction."""
    plate = normalize_plate(plate_number)
    if not plate:
        raise ValidationError("error.validation_error", params={"field": "plate_number"})

    clash = db.execute(
        select(Vehicle.id).where(Vehicle.plate_number == plate)
    ).scalar_one_or_none()
    if clash:
        raise ConflictError("error.conflict", params={"plate": plate})

    vehicle = Vehicle(
        code=code or numbering_service.next_number(db, "VEHICLE"),
        plate_number=plate,
        name=name,
        vehicle_type=vehicle_type,
        status=VehicleStatus.ACTIVE,
        is_active=True,
        region_id=region_id,
        home_warehouse_id=home_warehouse_id,
        capacity_volume_l=capacity_volume_l,
        capacity_weight_kg=capacity_weight_kg,
        capacity_cases=capacity_cases,
        default_salesperson_id=default_salesperson_id,
        default_driver_id=default_driver_id,
        created_by_id=user_id,
    )
    for field, value in extra.items():
        if field in _VEHICLE_UPDATABLE and value is not None:
            setattr(vehicle, field, value)

    db.add(vehicle)
    db.flush()

    ensure_warehouse(db, vehicle, user_id=user_id)

    audit_service.record(
        db,
        AuditAction.CREATE,
        entity_type="Vehicle",
        entity_id=vehicle.id,
        entity_label=vehicle.plate_number,
        user_id=user_id,
        summary=f"vehicle.created:{vehicle.plate_number}",
        new_values={"plate_number": plate, "warehouse_id": vehicle.warehouse_id},
    )
    if commit:
        db.commit()
        db.refresh(vehicle)
    return vehicle


def update_vehicle(
    db: Session,
    vehicle: Vehicle,
    data: dict[str, Any],
    *,
    user_id: int | None = None,
    commit: bool = True,
) -> Vehicle:
    """Apply a partial update, keeping the van warehouse's identity in sync."""
    before = {f: getattr(vehicle, f) for f in _VEHICLE_UPDATABLE}
    before["plate_number"] = vehicle.plate_number

    new_plate = data.get("plate_number")
    if new_plate:
        plate = normalize_plate(new_plate)
        if plate != vehicle.plate_number:
            clash = db.execute(
                select(Vehicle.id).where(
                    Vehicle.plate_number == plate, Vehicle.id != vehicle.id
                )
            ).scalar_one_or_none()
            if clash:
                raise ConflictError("error.conflict", params={"plate": plate})
            vehicle.plate_number = plate
            if vehicle.warehouse_id:
                warehouse = db.get(Warehouse, vehicle.warehouse_id)
                if warehouse is not None:
                    warehouse.code = f"VH-{plate}"

    for field in _VEHICLE_UPDATABLE:
        if field in data and data[field] is not None:
            setattr(vehicle, field, data[field])

    if vehicle.warehouse_id:
        warehouse = db.get(Warehouse, vehicle.warehouse_id)
        if warehouse is not None:
            warehouse.name = vehicle.name or vehicle.plate_number
            warehouse.capacity_volume_l = vehicle.capacity_volume_l
            warehouse.capacity_weight_kg = vehicle.capacity_weight_kg
            warehouse.is_active = vehicle.is_active

    vehicle.updated_by_id = user_id
    db.flush()

    audit_service.record(
        db,
        AuditAction.UPDATE,
        entity_type="Vehicle",
        entity_id=vehicle.id,
        entity_label=vehicle.plate_number,
        user_id=user_id,
        summary=f"vehicle.updated:{vehicle.plate_number}",
        old_values=before,
        new_values={f: getattr(vehicle, f) for f in _VEHICLE_UPDATABLE},
    )
    if commit:
        db.commit()
        db.refresh(vehicle)
    return vehicle


def delete_vehicle(
    db: Session,
    vehicle: Vehicle,
    *,
    user_id: int | None = None,
    commit: bool = True,
) -> Vehicle:
    """
    Soft-delete a vehicle.

    Refused while the van still holds stock: deactivating a loaded van would
    strand inventory in a location nobody can see any more.
    """
    if vehicle.warehouse_id:
        on_hand = sum(
            (D(b.quantity) for b in stock_service.get_balances(db, vehicle.warehouse_id)),
            D(0),
        )
        if on_hand > 0:
            raise BusinessRuleError(
                "vehicle.has_stock", params={"plate": vehicle.plate_number, "quantity": str(on_hand)}
            )

    open_session = db.execute(
        select(DaySession.id).where(
            DaySession.vehicle_id == vehicle.id, DaySession.status == "OPEN"
        )
    ).scalar_one_or_none()
    if open_session:
        raise BusinessRuleError("day.already_open", params={"session_id": open_session})

    vehicle.is_deleted = True
    vehicle.deleted_at = utcnow()
    vehicle.deleted_by_id = user_id
    vehicle.is_active = False
    vehicle.status = VehicleStatus.INACTIVE
    if vehicle.warehouse_id:
        warehouse = db.get(Warehouse, vehicle.warehouse_id)
        if warehouse is not None:
            warehouse.is_active = False
    db.flush()

    audit_service.record(
        db,
        AuditAction.DELETE,
        entity_type="Vehicle",
        entity_id=vehicle.id,
        entity_label=vehicle.plate_number,
        user_id=user_id,
        summary=f"vehicle.deleted:{vehicle.plate_number}",
    )
    if commit:
        db.commit()
    return vehicle


# ===========================================================================
# Salesperson CRUD
# ===========================================================================
def list_salespersons_query(
    db: Session,
    *,
    search: str | None = None,
    region_id: int | None = None,
    is_active: bool | None = None,
    supervisor_id: int | None = None,
    salesperson_ids: list[int] | None = None,
) -> Select[tuple[Salesperson]]:
    stmt = select(Salesperson).where(Salesperson.is_deleted.is_(False))
    if search:
        term = f"%{search.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Salesperson.full_name).like(term),
                func.lower(Salesperson.code).like(term),
            )
        )
    if region_id is not None:
        stmt = stmt.where(Salesperson.region_id == region_id)
    if is_active is not None:
        stmt = stmt.where(Salesperson.is_active.is_(is_active))
    if supervisor_id is not None:
        stmt = stmt.where(Salesperson.supervisor_id == supervisor_id)
    if salesperson_ids:
        stmt = stmt.where(Salesperson.id.in_(salesperson_ids))
    return stmt


def create_salesperson(
    db: Session,
    *,
    full_name: str,
    code: str | None = None,
    link_user_id: int | None = None,
    phone: str | None = None,
    email: str | None = None,
    region_id: int | None = None,
    supervisor_id: int | None = None,
    default_vehicle_id: int | None = None,
    default_warehouse_id: int | None = None,
    hire_date: date | None = None,
    user_id: int | None = None,
    commit: bool = True,
    **extra: Any,
) -> Salesperson:
    """Create a field sales profile, optionally bound to a login account."""
    if not (full_name or "").strip():
        raise ValidationError("error.validation_error", params={"field": "full_name"})

    if link_user_id is not None:
        taken = db.execute(
            select(Salesperson.id).where(Salesperson.user_id == link_user_id)
        ).scalar_one_or_none()
        if taken:
            raise ConflictError("error.conflict", params={"user_id": link_user_id})

    person = Salesperson(
        code=code or numbering_service.next_number(db, "SALESPERSON"),
        user_id=link_user_id,
        full_name=full_name.strip(),
        phone=phone,
        email=email,
        region_id=region_id,
        supervisor_id=supervisor_id,
        default_vehicle_id=default_vehicle_id,
        default_warehouse_id=default_warehouse_id,
        hire_date=hire_date,
        is_active=True,
        created_by_id=user_id,
    )
    for field, value in extra.items():
        if field in _SALESPERSON_UPDATABLE and value is not None:
            setattr(person, field, value)

    db.add(person)
    db.flush()

    if default_vehicle_id:
        assign(db, default_vehicle_id, person.id, user_id=user_id, commit=False)

    audit_service.record(
        db,
        AuditAction.CREATE,
        entity_type="Salesperson",
        entity_id=person.id,
        entity_label=person.full_name,
        user_id=user_id,
        summary=f"salesperson.created:{person.code}",
        new_values={"code": person.code, "user_id": link_user_id},
    )
    if commit:
        db.commit()
        db.refresh(person)
    return person


def update_salesperson(
    db: Session,
    person: Salesperson,
    data: dict[str, Any],
    *,
    user_id: int | None = None,
    commit: bool = True,
) -> Salesperson:
    before = {f: getattr(person, f) for f in _SALESPERSON_UPDATABLE}

    new_user_id = data.get("user_id")
    if new_user_id is not None and new_user_id != person.user_id:
        taken = db.execute(
            select(Salesperson.id).where(
                Salesperson.user_id == new_user_id, Salesperson.id != person.id
            )
        ).scalar_one_or_none()
        if taken:
            raise ConflictError("error.conflict", params={"user_id": new_user_id})

    for field in _SALESPERSON_UPDATABLE:
        if field in data and data[field] is not None:
            setattr(person, field, data[field])

    person.updated_by_id = user_id
    db.flush()

    audit_service.record(
        db,
        AuditAction.UPDATE,
        entity_type="Salesperson",
        entity_id=person.id,
        entity_label=person.full_name,
        user_id=user_id,
        summary=f"salesperson.updated:{person.code}",
        old_values=before,
        new_values={f: getattr(person, f) for f in _SALESPERSON_UPDATABLE},
    )
    if commit:
        db.commit()
        db.refresh(person)
    return person


def delete_salesperson(
    db: Session,
    person: Salesperson,
    *,
    user_id: int | None = None,
    commit: bool = True,
) -> Salesperson:
    """Soft-delete a salesperson; refused while a day session is still open."""
    open_session = db.execute(
        select(DaySession.id).where(
            DaySession.salesperson_id == person.id, DaySession.status == "OPEN"
        )
    ).scalar_one_or_none()
    if open_session:
        raise BusinessRuleError("day.already_open", params={"session_id": open_session})

    person.is_deleted = True
    person.deleted_at = utcnow()
    person.deleted_by_id = user_id
    person.is_active = False
    db.flush()

    audit_service.record(
        db,
        AuditAction.DELETE,
        entity_type="Salesperson",
        entity_id=person.id,
        entity_label=person.full_name,
        user_id=user_id,
        summary=f"salesperson.deleted:{person.code}",
    )
    if commit:
        db.commit()
    return person


# ===========================================================================
# Assignment, capacity, maintenance, position
# ===========================================================================
def assign(
    db: Session,
    vehicle_id: int,
    salesperson_id: int,
    *,
    user_id: int | None = None,
    commit: bool = True,
) -> Vehicle:
    """Bind a van to its default salesperson (both sides of the link)."""
    vehicle = get_vehicle(db, vehicle_id)
    person = get_salesperson(db, salesperson_id)
    require_usable(vehicle)

    previous = vehicle.default_salesperson_id
    vehicle.default_salesperson_id = person.id
    vehicle.updated_by_id = user_id
    person.default_vehicle_id = vehicle.id
    if not person.default_warehouse_id and vehicle.home_warehouse_id:
        person.default_warehouse_id = vehicle.home_warehouse_id
    person.updated_by_id = user_id
    db.flush()

    audit_service.record(
        db,
        AuditAction.UPDATE,
        entity_type="Vehicle",
        entity_id=vehicle.id,
        entity_label=vehicle.plate_number,
        user_id=user_id,
        summary=f"vehicle.assigned:{person.code}",
        old_values={"default_salesperson_id": previous},
        new_values={"default_salesperson_id": person.id},
    )
    if commit:
        db.commit()
    return vehicle


def van_stock(db: Session, vehicle_id: int) -> list[dict[str, Any]]:
    """Current van contents, one row per product, with volume/weight."""
    warehouse = warehouse_for(db, vehicle_id)
    rows: dict[int, dict[str, Any]] = {}
    for balance in stock_service.get_balances(db, warehouse.id):
        quantity = D(balance.quantity)
        if quantity == 0:
            continue
        product = balance.product
        row = rows.setdefault(
            balance.product_id,
            {
                "product_id": balance.product_id,
                "sku": product.sku if product else None,
                "product_name": product.name if product else None,
                "base_uom": product.base_uom if product else None,
                "units_per_case": D(product.units_per_case) if product else D(1),
                "base_quantity": D(0),
                "reserved_quantity": D(0),
                "available_quantity": D(0),
                "value": D(0),
                "volume_l": 0.0,
                "weight_kg": 0.0,
                "lots": [],
            },
        )
        row["base_quantity"] += quantity
        row["reserved_quantity"] += D(balance.reserved_quantity)
        row["available_quantity"] += D(balance.available)
        row["value"] += quantity * D(balance.average_cost)
        if product is not None:
            row["volume_l"] += float(quantity) * unit_volume_l(product)
            row["weight_kg"] += float(quantity) * unit_weight_kg(product)
        if balance.lot_id:
            row["lots"].append({"lot_id": balance.lot_id, "quantity": quantity})

    for row in rows.values():
        row["value"] = money(row["value"])
        row["volume_l"] = round(row["volume_l"], 3)
        row["weight_kg"] = round(row["weight_kg"], 3)
    return sorted(rows.values(), key=lambda r: (r["product_name"] or "", r["product_id"]))


def capacity_usage(db: Session, vehicle_id: int) -> dict[str, Any]:
    """
    How full the van is right now.

    Percentages are reported against the vehicle's declared capacity so the
    load-out screen can refuse an over-capacity plan before it is posted.
    """
    vehicle = get_vehicle(db, vehicle_id)
    warehouse = warehouse_for(db, vehicle_id)

    volume_l = 0.0
    weight_kg = 0.0
    base_quantity = D(0)
    products: set[int] = set()

    for balance in stock_service.get_balances(db, warehouse.id):
        quantity = D(balance.quantity)
        if quantity == 0 or balance.product is None:
            continue
        base_quantity += quantity
        products.add(balance.product_id)
        volume_l += float(quantity) * unit_volume_l(balance.product)
        weight_kg += float(quantity) * unit_weight_kg(balance.product)

    return {
        "vehicle_id": vehicle.id,
        "plate_number": vehicle.plate_number,
        "warehouse_id": warehouse.id,
        "volume_l": round(volume_l, 3),
        "weight_kg": round(weight_kg, 3),
        "capacity_volume_l": float(vehicle.capacity_volume_l or 0.0),
        "capacity_weight_kg": float(vehicle.capacity_weight_kg or 0.0),
        "volume_percent": pct(volume_l, vehicle.capacity_volume_l or 0),
        "weight_percent": pct(weight_kg, vehicle.capacity_weight_kg or 0),
        "base_quantity": base_quantity,
        "product_count": len(products),
    }


def check_capacity(
    db: Session,
    vehicle: Vehicle,
    *,
    added_volume_l: float = 0.0,
    added_weight_kg: float = 0.0,
) -> dict[str, Any]:
    """
    Validate that current contents plus an incoming load still fit.

    Raises :class:`BusinessRuleError` ``vehicle.capacity_exceeded`` — the load
    screen relies on this being refused *before* any stock moves.
    """
    usage = capacity_usage(db, vehicle.id)
    volume = usage["volume_l"] + added_volume_l
    weight = usage["weight_kg"] + added_weight_kg
    capacity_volume = float(vehicle.capacity_volume_l or 0.0)
    capacity_weight = float(vehicle.capacity_weight_kg or 0.0)

    if capacity_volume > 0 and volume > capacity_volume:
        raise BusinessRuleError(
            "vehicle.capacity_exceeded",
            params={"used": round(volume, 2), "capacity": round(capacity_volume, 2), "unit": "L"},
        )
    if capacity_weight > 0 and weight > capacity_weight:
        raise BusinessRuleError(
            "vehicle.capacity_exceeded",
            params={"used": round(weight, 2), "capacity": round(capacity_weight, 2), "unit": "kg"},
        )
    return {
        "volume_l": round(volume, 3),
        "weight_kg": round(weight, 3),
        "volume_percent": pct(volume, capacity_volume),
        "weight_percent": pct(weight, capacity_weight),
    }


def maintenance_warnings(
    db: Session,
    *,
    within_days: int = EXPIRY_WARNING_DAYS,
    vehicle_id: int | None = None,
    on: date | None = None,
) -> list[dict[str, Any]]:
    """
    Insurance / inspection documents expiring soon (or already expired).

    An expired inspection makes the van illegal to drive, so this feeds both
    the fleet screen and the morning day-open check.
    """
    today = on or date.today()
    stmt = select(Vehicle).where(Vehicle.is_deleted.is_(False))
    if vehicle_id is not None:
        stmt = stmt.where(Vehicle.id == vehicle_id)

    warnings: list[dict[str, Any]] = []
    for vehicle in db.execute(stmt).scalars().all():
        for kind, expiry in (
            ("INSURANCE", vehicle.insurance_expiry),
            ("INSPECTION", vehicle.inspection_expiry),
        ):
            if expiry is None:
                continue
            days_left = (expiry - today).days
            if days_left > within_days:
                continue
            warnings.append(
                {
                    "vehicle_id": vehicle.id,
                    "plate_number": vehicle.plate_number,
                    "kind": kind,
                    "expiry_date": expiry,
                    "days_left": days_left,
                    "is_expired": days_left < 0,
                    "severity": "CRITICAL" if days_left < 0 else ("WARNING" if days_left <= 7 else "INFO"),
                }
            )
    return sorted(warnings, key=lambda w: w["days_left"])


def update_position(
    db: Session,
    vehicle_id: int,
    lat: float,
    lng: float,
    *,
    salesperson_id: int | None = None,
    day_session_id: int | None = None,
    route_id: int | None = None,
    accuracy_m: float | None = None,
    speed_kmh: float | None = None,
    heading: float | None = None,
    altitude_m: float | None = None,
    battery_percent: int | None = None,
    event_type: str = "PING",
    recorded_at: datetime | None = None,
    odometer_km: float | None = None,
    commit: bool = True,
) -> GpsEvent:
    """
    Record a GPS breadcrumb and cache it on the vehicle.

    The breadcrumb table is the audit trail (route deviation, visit geofence);
    the cached ``last_*`` columns exist so the live map never has to scan it.
    """
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lng <= 180.0):
        raise ValidationError("error.validation_error", params={"lat": lat, "lng": lng})

    vehicle = get_vehicle(db, vehicle_id)
    stamp = recorded_at or utcnow()

    event = GpsEvent(
        vehicle_id=vehicle.id,
        salesperson_id=salesperson_id or vehicle.default_salesperson_id,
        day_session_id=day_session_id,
        route_id=route_id,
        latitude=float(lat),
        longitude=float(lng),
        accuracy_m=accuracy_m,
        speed_kmh=speed_kmh,
        heading=heading,
        altitude_m=altitude_m,
        battery_percent=battery_percent,
        event_type=event_type,
        recorded_at=stamp,
    )
    db.add(event)

    vehicle.last_lat = float(lat)
    vehicle.last_lng = float(lng)
    vehicle.last_position_at = stamp
    if odometer_km is not None and odometer_km > (vehicle.odometer_km or 0.0):
        vehicle.odometer_km = float(odometer_km)
    db.flush()

    if commit:
        db.commit()
    return event
