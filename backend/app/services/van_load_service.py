"""
Van load-out and unload.

Loading a van is a two-legged stock transfer, never a single movement: goods
leave the depot (``TRANSFER_OUT``) and arrive in the van's warehouse
(``VEHICLE_LOAD``) with the **same lot ids**, so a bottle sold from a van can
still be traced back to the batch it was produced in — which is the whole point
of lot tracking in food and beverage distribution.

The load document is created as a draft first: the picker may adjust
quantities, and nothing touches the ledger until :func:`post_load`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Sequence

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.core.enums import (
    AuditAction,
    DaySessionStatus,
    StockMovementType,
    WarehouseType,
)
from app.core.exceptions import (
    BusinessRuleError,
    NotFoundError,
    ValidationError,
)
from app.core.utils import D, money, qty
from app.models.base import utcnow
from app.models.product import Product
from app.models.vehicle import DaySession, VanLoad, VanLoadItem
from app.models.warehouse import Warehouse
from app.services import (
    audit_service,
    day_session_service,
    numbering_service,
    stock_service,
    vehicle_service,
)

#: Reference type stamped on every movement produced by a load document.
LOAD_REFERENCE = "VAN_LOAD"
#: Reference type stamped on movements produced by an unload.
UNLOAD_REFERENCE = "VAN_UNLOAD"


@dataclass
class LoadLine:
    """One requested line of a load-out, in the picker's unit of measure."""

    product_id: int
    quantity: Decimal
    uom: str | None = None
    planned_quantity: Decimal | None = None
    lot_id: int | None = None


def _coerce_lines(lines: Sequence[LoadLine | dict[str, Any]]) -> list[LoadLine]:
    """Accept dicts (API payloads, AI suggestions) as well as dataclasses."""
    out: list[LoadLine] = []
    for line in lines:
        if isinstance(line, LoadLine):
            out.append(line)
            continue
        if not isinstance(line, dict):
            raise ValidationError("error.validation_error", params={"field": "lines"})
        out.append(
            LoadLine(
                product_id=int(line["product_id"]),
                quantity=D(line.get("quantity")),
                uom=line.get("uom"),
                planned_quantity=(
                    D(line["planned_quantity"]) if line.get("planned_quantity") is not None else None
                ),
                lot_id=line.get("lot_id"),
            )
        )
    return out


def _require_warehouse(db: Session, warehouse_id: int) -> Warehouse:
    warehouse = db.get(Warehouse, warehouse_id)
    if warehouse is None or warehouse.is_deleted:
        raise NotFoundError("stock.warehouse_not_found", params={"id": warehouse_id})
    return warehouse


def _require_product(db: Session, product_id: int) -> Product:
    product = db.get(Product, product_id)
    if product is None or product.is_deleted:
        raise NotFoundError("error.not_found", params={"product_id": product_id})
    return product


def get_load(db: Session, load_id: int) -> VanLoad:
    load = db.get(VanLoad, load_id)
    if load is None:
        raise NotFoundError("error.not_found", params={"id": load_id})
    return load


def list_loads_query(
    db: Session,
    *,
    vehicle_id: int | None = None,
    salesperson_id: int | None = None,
    day_session_id: int | None = None,
    source_warehouse_id: int | None = None,
    is_posted: bool | None = None,
    is_reload: bool | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    salesperson_ids: list[int] | None = None,
) -> Select[tuple[VanLoad]]:
    stmt = select(VanLoad)
    if vehicle_id is not None:
        stmt = stmt.where(VanLoad.vehicle_id == vehicle_id)
    if salesperson_id is not None:
        stmt = stmt.where(VanLoad.salesperson_id == salesperson_id)
    if day_session_id is not None:
        stmt = stmt.where(VanLoad.day_session_id == day_session_id)
    if source_warehouse_id is not None:
        stmt = stmt.where(VanLoad.source_warehouse_id == source_warehouse_id)
    if is_posted is not None:
        stmt = stmt.where(VanLoad.is_posted.is_(is_posted))
    if is_reload is not None:
        stmt = stmt.where(VanLoad.is_reload.is_(is_reload))
    if date_from is not None:
        stmt = stmt.where(VanLoad.load_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(VanLoad.load_date <= date_to)
    if salesperson_ids:
        stmt = stmt.where(VanLoad.salesperson_id.in_(salesperson_ids))
    return stmt


# ===========================================================================
# Creating the document
# ===========================================================================
def create_load(
    db: Session,
    *,
    vehicle_id: int,
    salesperson_id: int | None,
    source_warehouse_id: int,
    lines: Sequence[LoadLine | dict[str, Any]],
    load_date: date | None = None,
    is_reload: bool = False,
    day_session_id: int | None = None,
    notes: str | None = None,
    is_ai_suggested: bool = False,
    ai_confidence: float | None = None,
    ai_explanation: str | None = None,
    user_id: int | None = None,
    commit: bool = True,
) -> VanLoad:
    """
    Build a draft load-out and prove it fits in the van before anyone picks it.

    Capacity is checked against the van's *current* contents plus this load, so
    a reload on a still-half-full van is refused rather than discovered at the
    tailgate.
    """
    parsed = _coerce_lines(lines)
    if not parsed:
        raise ValidationError("error.validation_error", params={"field": "lines"})

    vehicle = vehicle_service.get_vehicle(db, vehicle_id)
    vehicle_service.require_usable(vehicle)
    van_warehouse = vehicle_service.ensure_warehouse(db, vehicle, user_id=user_id)

    source = _require_warehouse(db, source_warehouse_id)
    if source.id == van_warehouse.id:
        raise BusinessRuleError("vehicle.same_warehouse", params={"warehouse_id": source.id})
    if source.warehouse_type == WarehouseType.VEHICLE:
        raise BusinessRuleError("vehicle.same_warehouse", params={"warehouse_id": source.id})

    on = load_date or date.today()
    if salesperson_id is None:
        salesperson_id = vehicle.default_salesperson_id
    if day_session_id is None and salesperson_id:
        open_session = day_session_service.get_open_session(db, salesperson_id=salesperson_id)
        if open_session is not None and open_session.vehicle_id == vehicle.id:
            day_session_id = open_session.id

    load = VanLoad(
        document_no=numbering_service.next_number(db, "VAN_LOAD", on=on),
        load_date=on,
        day_session_id=day_session_id,
        vehicle_id=vehicle.id,
        salesperson_id=salesperson_id,
        source_warehouse_id=source.id,
        is_reload=is_reload,
        is_ai_suggested=is_ai_suggested,
        ai_confidence=ai_confidence,
        ai_explanation=ai_explanation,
        is_posted=False,
        notes=notes,
        created_by_id=user_id,
    )

    total_volume = 0.0
    total_weight = 0.0
    total_cost = D(0)

    for line in parsed:
        product = _require_product(db, line.product_id)
        uom = line.uom or product.sales_uom
        quantity = qty(line.quantity)
        if quantity <= 0:
            raise ValidationError(
                "error.validation_error", params={"product_id": product.id, "quantity": str(quantity)}
            )
        base_quantity = qty(stock_service.to_base(product, quantity, uom))
        unit_cost = money(product.cost_price)

        total_volume += float(base_quantity) * vehicle_service.unit_volume_l(product)
        total_weight += float(base_quantity) * vehicle_service.unit_weight_kg(product)
        total_cost += base_quantity * unit_cost

        load.items.append(
            VanLoadItem(
                product_id=product.id,
                lot_id=line.lot_id,
                planned_quantity=qty(line.planned_quantity if line.planned_quantity is not None else quantity),
                quantity=quantity,
                uom=uom,
                base_quantity=base_quantity,
                unit_cost=unit_cost,
            )
        )

    vehicle_service.check_capacity(
        db, vehicle, added_volume_l=total_volume, added_weight_kg=total_weight
    )

    load.total_volume_l = round(total_volume, 3)
    load.total_weight_kg = round(total_weight, 3)
    load.total_cost = money(total_cost)

    db.add(load)
    db.flush()

    audit_service.record(
        db,
        AuditAction.CREATE,
        entity_type="VanLoad",
        entity_id=load.id,
        entity_label=load.document_no,
        user_id=user_id,
        summary=f"van_load.created:{load.document_no}",
        amount=load.total_cost,
        new_values={
            "vehicle_id": vehicle.id,
            "source_warehouse_id": source.id,
            "is_reload": is_reload,
            "lines": len(load.items),
        },
    )
    if commit:
        db.commit()
        db.refresh(load)
    return load


# ===========================================================================
# Posting to the ledger
# ===========================================================================
def post_load(
    db: Session,
    load: VanLoad,
    *,
    user_id: int | None = None,
    commit: bool = True,
) -> VanLoad:
    """
    Move the goods: depot ``TRANSFER_OUT`` -> van ``VEHICLE_LOAD``, lot by lot.

    The depot leg allocates (FEFO/FIFO) and tells us which lots actually left;
    the van leg mirrors each of those allocations so both warehouses agree on
    lot and cost.
    """
    if load.is_posted:
        raise BusinessRuleError("van_load.already_posted", params={"document_no": load.document_no})
    if not load.items:
        raise ValidationError("error.validation_error", params={"field": "lines"})

    vehicle = vehicle_service.get_vehicle(db, load.vehicle_id)
    vehicle_service.require_usable(vehicle)
    van_warehouse = vehicle_service.ensure_warehouse(db, vehicle, user_id=user_id)
    source = _require_warehouse(db, load.source_warehouse_id)

    vehicle_service.check_capacity(
        db,
        vehicle,
        added_volume_l=float(load.total_volume_l or 0.0),
        added_weight_kg=float(load.total_weight_kg or 0.0),
    )

    total_cost = D(0)

    for item in load.items:
        base_quantity = qty(item.base_quantity)
        if base_quantity <= 0:
            continue

        issued = stock_service.issue_stock(
            db,
            warehouse_id=source.id,
            product_id=item.product_id,
            base_quantity=base_quantity,
            movement_type=StockMovementType.TRANSFER_OUT,
            reference_type=LOAD_REFERENCE,
            reference_id=load.id,
            reference_no=load.document_no,
            counterparty_warehouse_id=van_warehouse.id,
            salesperson_id=load.salesperson_id,
            day_session_id=load.day_session_id,
            user_id=user_id,
        )

        line_cost = D(0)
        line_quantity = D(0)
        lot_ids: list[int] = []
        for movement in issued:
            moved = qty(abs(D(movement.quantity)))
            if moved <= 0:
                continue
            stock_service.receive_stock(
                db,
                warehouse_id=van_warehouse.id,
                product_id=item.product_id,
                base_quantity=moved,
                movement_type=StockMovementType.VEHICLE_LOAD,
                lot_id=movement.lot_id,
                unit_cost=money(movement.unit_cost),
                reference_type=LOAD_REFERENCE,
                reference_id=load.id,
                reference_no=load.document_no,
                counterparty_warehouse_id=source.id,
                salesperson_id=load.salesperson_id,
                day_session_id=load.day_session_id,
                user_id=user_id,
            )
            line_quantity += moved
            line_cost += moved * money(movement.unit_cost)
            if movement.lot_id:
                lot_ids.append(movement.lot_id)

        # A line split across lots keeps no single lot id; the ledger holds the
        # per-lot truth and the document keeps the weighted average cost.
        item.lot_id = lot_ids[0] if len(set(lot_ids)) == 1 else None
        item.unit_cost = money(line_cost / line_quantity) if line_quantity else item.unit_cost
        total_cost += line_cost

    load.total_cost = money(total_cost)
    load.is_posted = True
    load.posted_at = utcnow()
    load.updated_by_id = user_id
    db.flush()

    # The van's day summary is derived from the ledger, so refresh it now that
    # the ledger has changed rather than letting the morning figures go stale.
    if load.day_session_id:
        session = db.get(DaySession, load.day_session_id)
        if session is not None and session.status == DaySessionStatus.OPEN:
            day_session_service.recalculate(db, session)

    audit_service.record(
        db,
        AuditAction.STOCK_ADJUSTMENT,
        entity_type="VanLoad",
        entity_id=load.id,
        entity_label=load.document_no,
        user_id=user_id,
        summary=f"van_load.posted:{load.document_no}",
        amount=load.total_cost,
        new_values={
            "vehicle_id": vehicle.id,
            "source_warehouse_id": source.id,
            "van_warehouse_id": van_warehouse.id,
            "is_reload": load.is_reload,
        },
    )
    if commit:
        db.commit()
        db.refresh(load)
    return load


# ===========================================================================
# Returning stock to the depot
# ===========================================================================
def _van_contents(db: Session, warehouse_id: int) -> dict[int, Decimal]:
    """Everything currently on the van, per product, in base units."""
    totals: dict[int, Decimal] = {}
    for balance in stock_service.get_balances(db, warehouse_id):
        quantity = D(balance.quantity)
        if quantity <= 0:
            continue
        totals[balance.product_id] = totals.get(balance.product_id, D(0)) + quantity
    return totals


def unload(
    db: Session,
    *,
    vehicle_id: int,
    target_warehouse_id: int,
    lines: Sequence[LoadLine | dict[str, Any]] | None = None,
    salesperson_id: int | None = None,
    day_session_id: int | None = None,
    user_id: int | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """
    Return van stock to a depot: van ``VEHICLE_UNLOAD`` -> depot ``TRANSFER_IN``.

    ``lines=None`` empties the van completely, which is the normal end-of-day
    action; a partial list is used when only part of the load comes back.
    """
    vehicle = vehicle_service.get_vehicle(db, vehicle_id)
    van_warehouse = vehicle_service.warehouse_for(db, vehicle.id)
    target = _require_warehouse(db, target_warehouse_id)
    if target.id == van_warehouse.id:
        raise BusinessRuleError("vehicle.same_warehouse", params={"warehouse_id": target.id})

    if salesperson_id is None:
        salesperson_id = vehicle.default_salesperson_id
    if day_session_id is None and salesperson_id:
        open_session = day_session_service.get_open_session(db, salesperson_id=salesperson_id)
        if open_session is not None and open_session.vehicle_id == vehicle.id:
            day_session_id = open_session.id

    requested: dict[int, Decimal] = {}
    if lines is None:
        requested = _van_contents(db, van_warehouse.id)
    else:
        for line in _coerce_lines(lines):
            product = _require_product(db, line.product_id)
            uom = line.uom or product.sales_uom
            base_quantity = qty(stock_service.to_base(product, qty(line.quantity), uom))
            if base_quantity <= 0:
                raise ValidationError(
                    "error.validation_error", params={"product_id": product.id}
                )
            requested[product.id] = requested.get(product.id, D(0)) + base_quantity

    if not requested:
        raise ValidationError("error.validation_error", params={"field": "lines"})

    reference_no = f"UNLOAD-{vehicle.plate_number}-{date.today().isoformat()}"
    result_lines: list[dict[str, Any]] = []
    total_base = D(0)
    total_value = D(0)

    for product_id, base_quantity in sorted(requested.items()):
        issued = stock_service.issue_stock(
            db,
            warehouse_id=van_warehouse.id,
            product_id=product_id,
            base_quantity=qty(base_quantity),
            movement_type=StockMovementType.VEHICLE_UNLOAD,
            allow_expired=True,
            reference_type=UNLOAD_REFERENCE,
            reference_id=vehicle.id,
            reference_no=reference_no,
            counterparty_warehouse_id=target.id,
            salesperson_id=salesperson_id,
            day_session_id=day_session_id,
            user_id=user_id,
        )

        moved_total = D(0)
        value_total = D(0)
        for movement in issued:
            moved = qty(abs(D(movement.quantity)))
            if moved <= 0:
                continue
            stock_service.receive_stock(
                db,
                warehouse_id=target.id,
                product_id=product_id,
                base_quantity=moved,
                movement_type=StockMovementType.TRANSFER_IN,
                lot_id=movement.lot_id,
                unit_cost=money(movement.unit_cost),
                reference_type=UNLOAD_REFERENCE,
                reference_id=vehicle.id,
                reference_no=reference_no,
                counterparty_warehouse_id=van_warehouse.id,
                salesperson_id=salesperson_id,
                day_session_id=day_session_id,
                user_id=user_id,
            )
            moved_total += moved
            value_total += moved * money(movement.unit_cost)

        result_lines.append(
            {
                "product_id": product_id,
                "base_quantity": qty(moved_total),
                "value": money(value_total),
            }
        )
        total_base += moved_total
        total_value += value_total

    db.flush()

    audit_service.record(
        db,
        AuditAction.STOCK_ADJUSTMENT,
        entity_type="Vehicle",
        entity_id=vehicle.id,
        entity_label=vehicle.plate_number,
        user_id=user_id,
        summary=f"van_load.unloaded:{vehicle.plate_number}",
        amount=money(total_value),
        new_values={
            "target_warehouse_id": target.id,
            "lines": len(result_lines),
            "base_quantity": str(qty(total_base)),
        },
    )
    if commit:
        db.commit()

    return {
        "vehicle_id": vehicle.id,
        "van_warehouse_id": van_warehouse.id,
        "target_warehouse_id": target.id,
        "day_session_id": day_session_id,
        "reference_no": reference_no,
        "lines": result_lines,
        "total_base_quantity": qty(total_base),
        "total_value": money(total_value),
    }
