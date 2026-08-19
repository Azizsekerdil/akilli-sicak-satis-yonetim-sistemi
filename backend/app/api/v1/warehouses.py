"""
Warehouse, stock, transfer, count and lot endpoints.

Every stock-changing route delegates to :mod:`app.services.stock_service` so
the ledger rules exist in exactly one place; this module only authorises,
validates and shapes the response.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select

from app.core.deps import Ctx, Page, get_page, paginated, require
from app.core.enums import AuditAction, CountStatus, TransferStatus, WarehouseType
from app.core.exceptions import (
    BusinessRuleError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.core.i18n import t
from app.core.utils import D, money, qty
from app.models.product import Product
from app.models.vehicle import Salesperson, Vehicle
from app.models.warehouse import (
    Lot,
    StockBalance,
    StockCount,
    StockMovement,
    StockTransfer,
    Warehouse,
)
from app.schemas.common import Message, PagedResponse
from app.schemas.stock import (
    AdjustmentIn,
    CountCreate,
    CountItem,
    CountOut,
    CountSubmit,
    ExpiryRow,
    LotBlockIn,
    LotCreate,
    LotOut,
    LotUpdate,
    LowStockRow,
    StockBalanceOut,
    StockCardOut,
    StockMovementOut,
    TransferCreate,
    TransferItem,
    TransferOut,
    TransferReceiveIn,
    TransferUpdate,
    ValuationOut,
    VehicleStockRow,
    WarehouseCreate,
    WarehouseOut,
    WarehouseUpdate,
)
from app.services import audit_service, stock_service

router = APIRouter(prefix="/warehouses", tags=["stock"])

ZERO = Decimal("0")


# ===========================================================================
# Shared helpers
# ===========================================================================
def _get_warehouse(ctx: Ctx, warehouse_id: int) -> Warehouse:
    wh = ctx.db.get(Warehouse, warehouse_id)
    if wh is None or wh.is_deleted:
        raise NotFoundError("stock.warehouse_not_found", params={"id": warehouse_id})
    return wh


def _get_lot(ctx: Ctx, lot_id: int) -> Lot:
    lot = ctx.db.get(Lot, lot_id)
    if lot is None:
        raise NotFoundError("stock.lot_not_found", params={"id": lot_id})
    return lot


def _get_transfer(ctx: Ctx, transfer_id: int) -> StockTransfer:
    row = ctx.db.get(StockTransfer, transfer_id)
    if row is None:
        raise NotFoundError("stock.transfer_not_found", params={"id": transfer_id})
    return row


def _get_count(ctx: Ctx, count_id: int) -> StockCount:
    row = ctx.db.get(StockCount, count_id)
    if row is None:
        raise NotFoundError("stock.count_not_found", params={"id": count_id})
    return row


def _warehouse_out(ctx: Ctx, wh: Warehouse, *, with_totals: bool = False) -> WarehouseOut:
    out = WarehouseOut.model_validate(wh)
    if with_totals:
        out.stock_value = stock_service.warehouse_valuation(ctx.db, wh.id)
        out.product_count = int(
            ctx.db.execute(
                select(func.count(func.distinct(StockBalance.product_id))).where(
                    StockBalance.warehouse_id == wh.id, StockBalance.quantity != 0
                )
            ).scalar_one()
        )
    return out


def _lot_out(ctx: Ctx, lot: Lot, *, with_stock: bool = False) -> LotOut:
    # Built field by field rather than via model_validate: ``Lot.days_to_expiry``
    # is a method, and attribute-mode validation would bind it instead of calling it.
    product = lot.product
    out = LotOut(
        id=lot.id,
        product_id=lot.product_id,
        lot_number=lot.lot_number,
        batch_number=lot.batch_number,
        serial_number=lot.serial_number,
        production_date=lot.production_date,
        expiry_date=lot.expiry_date,
        received_date=lot.received_date,
        supplier_name=lot.supplier_name,
        unit_cost=money(lot.unit_cost),
        is_blocked=lot.is_blocked,
        block_reason=lot.block_reason,
        notes=lot.notes,
        product_sku=product.sku if product is not None else None,
        product_name=product.name if product is not None else None,
        days_to_expiry=lot.days_to_expiry(),
    )
    if with_stock:
        total = ctx.db.execute(
            select(func.sum(StockBalance.quantity)).where(StockBalance.lot_id == lot.id)
        ).scalar_one_or_none()
        out.on_hand = qty(D(total))
    return out


def _balance_out(ctx: Ctx, balance: StockBalance) -> StockBalanceOut:
    out = StockBalanceOut.model_validate(balance)
    product = balance.product
    if product is not None:
        out.product_sku = product.sku
        out.product_name = product.name
        out.uom = product.base_uom
        per_case = D(product.units_per_case) or Decimal("1")
        out.case_qty = qty(D(balance.quantity) / per_case)
    if balance.lot_id:
        lot = ctx.db.get(Lot, balance.lot_id)
        if lot is not None:
            out.lot_number = lot.lot_number
            out.expiry_date = lot.expiry_date
            out.days_to_expiry = lot.days_to_expiry()
            out.is_blocked = lot.is_blocked
    return out


def _transfer_out(ctx: Ctx, transfer: StockTransfer) -> TransferOut:
    out = TransferOut.model_validate(transfer)
    source = ctx.db.get(Warehouse, transfer.source_warehouse_id)
    target = ctx.db.get(Warehouse, transfer.target_warehouse_id)
    out.source_warehouse_name = source.name if source else None
    out.target_warehouse_name = target.name if target else None

    lines: list[TransferItem] = []
    total_qty = ZERO
    total_cost = ZERO
    for item in transfer.items:
        product = item.product
        line = TransferItem(
            id=item.id,
            product_id=item.product_id,
            lot_id=item.lot_id,
            quantity=qty(item.quantity),
            received_quantity=qty(item.received_quantity),
            uom=item.uom,
            unit_cost=money(item.unit_cost),
            product_sku=product.sku if product else None,
            product_name=product.name if product else None,
            base_quantity=(
                stock_service.to_base(product, item.quantity, item.uom) if product else None
            ),
        )
        total_qty += D(line.base_quantity or 0)
        total_cost += D(item.unit_cost) * D(line.base_quantity or 0)
        lines.append(line)

    out.items = lines
    out.total_quantity = qty(total_qty)
    out.total_cost = money(total_cost)
    return out


def _count_out(ctx: Ctx, count: StockCount) -> CountOut:
    out = CountOut.model_validate(count)
    warehouse = ctx.db.get(Warehouse, count.warehouse_id)
    out.warehouse_code = warehouse.code if warehouse else None
    out.warehouse_name = warehouse.name if warehouse else None

    lines: list[CountItem] = []
    for item in count.items:
        product = item.product
        lot = ctx.db.get(Lot, item.lot_id) if item.lot_id else None
        lines.append(
            CountItem(
                id=item.id,
                product_id=item.product_id,
                lot_id=item.lot_id,
                system_quantity=qty(item.system_quantity),
                counted_quantity=qty(item.counted_quantity),
                variance_quantity=qty(item.variance_quantity),
                unit_cost=money(item.unit_cost),
                variance_value=money(item.variance_value),
                reason=item.reason,
                product_sku=product.sku if product else None,
                product_name=product.name if product else None,
                uom=product.base_uom if product else None,
                lot_number=lot.lot_number if lot else None,
                expiry_date=lot.expiry_date if lot else None,
            )
        )
    out.items = lines
    return out


def _accessible_vehicle_ids(ctx: Ctx) -> list[int] | None:
    """
    Vehicles the caller may inspect, or ``None`` when unrestricted.

    A salesperson sees their own van only; a supervisor sees their team's.
    """
    if ctx.unrestricted or not ctx.salesperson_ids:
        return None
    ids = ctx.salesperson_ids
    rows = ctx.db.execute(
        select(Vehicle.id)
        .outerjoin(Salesperson, Salesperson.default_vehicle_id == Vehicle.id)
        .where(
            or_(
                Vehicle.default_salesperson_id.in_(ids),
                Salesperson.id.in_(ids),
            )
        )
    ).scalars().all()
    return sorted(set(int(v) for v in rows))


# ===========================================================================
# Warehouse CRUD
# ===========================================================================
@router.get("", response_model=PagedResponse[WarehouseOut], summary="List warehouses")
def list_warehouses(
    ctx: Ctx = Depends(require("stock.warehouses", "VIEW")),
    page: Page = Depends(get_page),
    search: str | None = Query(default=None, description="Code, name or city"),
    warehouse_type: WarehouseType | None = Query(default=None),
    region_id: int | None = Query(default=None),
    is_active: bool | None = Query(default=None),
) -> dict[str, Any]:
    stmt = select(Warehouse).where(Warehouse.is_deleted.is_(False))
    if search:
        term = f"%{search.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Warehouse.code).like(term),
                func.lower(Warehouse.name).like(term),
                func.lower(Warehouse.city).like(term),
            )
        )
    if warehouse_type:
        stmt = stmt.where(Warehouse.warehouse_type == str(warehouse_type))
    if region_id:
        stmt = stmt.where(Warehouse.region_id == region_id)
    if is_active is not None:
        stmt = stmt.where(Warehouse.is_active.is_(is_active))

    total = int(
        ctx.db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    )
    rows = ctx.db.execute(
        stmt.order_by(Warehouse.warehouse_type.asc(), Warehouse.code.asc())
        .offset(page.offset)
        .limit(page.limit)
    ).scalars().all()
    return paginated([_warehouse_out(ctx, r) for r in rows], total, page)


@router.post("", response_model=WarehouseOut, status_code=201, summary="Create warehouse")
def create_warehouse(
    payload: WarehouseCreate,
    ctx: Ctx = Depends(require("stock.warehouses", "CREATE")),
) -> WarehouseOut:
    code = payload.code.strip().upper()
    exists = ctx.db.execute(
        select(Warehouse.id).where(
            func.lower(Warehouse.code) == code.lower(), Warehouse.is_deleted.is_(False)
        )
    ).scalar_one_or_none()
    if exists:
        raise ConflictError("stock.warehouse_code_taken", params={"code": code})

    data = payload.model_dump()
    data["code"] = code
    data["warehouse_type"] = str(payload.warehouse_type)
    data["allocation_strategy"] = str(payload.allocation_strategy)
    warehouse = Warehouse(**data, created_by_id=ctx.user_id)
    ctx.db.add(warehouse)
    ctx.db.flush()

    audit_service.record(
        ctx.db,
        AuditAction.CREATE,
        entity_type="Warehouse",
        entity_id=warehouse.id,
        entity_label=warehouse.code,
        summary=f"warehouse created {warehouse.code}",
        new_values=data,
        **ctx.audit_kwargs(),
    )
    ctx.db.commit()
    return _warehouse_out(ctx, warehouse)


# ---------------------------------------------------------------------------
# Literal sub-paths must be declared before "/{warehouse_id}" or FastAPI would
# try to parse "stock", "transfers", ... as a warehouse id.
# ---------------------------------------------------------------------------
@router.get(
    "/stock/expiring",
    response_model=list[ExpiryRow],
    summary="Expiring and expired stock",
)
def expiring_stock(
    ctx: Ctx = Depends(require("stock.lots", "VIEW")),
    days: int = Query(default=30, ge=0, le=730),
    warehouse_id: int | None = Query(default=None),
    include_expired: bool = Query(default=True),
) -> list[ExpiryRow]:
    rows: list[dict[str, Any]] = []
    if include_expired:
        rows.extend(stock_service.expired(ctx.db, warehouse_id=warehouse_id))
    rows.extend(stock_service.expiring_soon(ctx.db, days=days, warehouse_id=warehouse_id))
    return [ExpiryRow(**row) for row in rows]


@router.get("/stock/low", response_model=list[LowStockRow], summary="Low stock alerts")
def low_stock(
    ctx: Ctx = Depends(require("stock.warehouses", "VIEW")),
    warehouse_id: int | None = Query(default=None),
    include_vehicles: bool = Query(default=False),
) -> list[LowStockRow]:
    rows = stock_service.low_stock(
        ctx.db, warehouse_id=warehouse_id, include_vehicles=include_vehicles
    )
    return [LowStockRow(**row) for row in rows]


@router.get("/stock/card", response_model=StockCardOut, summary="Product stock card")
def stock_card(
    ctx: Ctx = Depends(require("stock.warehouses", "VIEW")),
    product_id: int = Query(...),
    warehouse_id: int = Query(...),
    start: date | None = Query(default=None),
    end: date | None = Query(default=None),
) -> StockCardOut:
    if start and end and end < start:
        raise ValidationError("common.invalid_date_range")
    card = stock_service.stock_card(ctx.db, product_id, warehouse_id, start, end)
    return StockCardOut(**card)


@router.get(
    "/stock/movements",
    response_model=PagedResponse[StockMovementOut],
    summary="Stock ledger",
)
def list_movements(
    ctx: Ctx = Depends(require("stock.warehouses", "VIEW")),
    page: Page = Depends(get_page),
    warehouse_id: int | None = Query(default=None),
    product_id: int | None = Query(default=None),
    movement_type: str | None = Query(default=None),
    start: date | None = Query(default=None),
    end: date | None = Query(default=None),
) -> dict[str, Any]:
    stmt = select(StockMovement)
    if warehouse_id:
        stmt = stmt.where(StockMovement.warehouse_id == warehouse_id)
    if product_id:
        stmt = stmt.where(StockMovement.product_id == product_id)
    if movement_type:
        stmt = stmt.where(StockMovement.movement_type == movement_type)
    if start:
        stmt = stmt.where(StockMovement.moved_at >= stock_service.day_start(start))
    if end:
        stmt = stmt.where(StockMovement.moved_at <= stock_service.day_end(end))
    if not ctx.unrestricted and ctx.salesperson_ids:
        stmt = stmt.where(
            or_(
                StockMovement.salesperson_id.in_(ctx.salesperson_ids),
                StockMovement.salesperson_id.is_(None),
            )
        )

    total = int(
        ctx.db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    )
    rows = ctx.db.execute(
        stmt.order_by(StockMovement.moved_at.desc(), StockMovement.id.desc())
        .offset(page.offset)
        .limit(page.limit)
    ).scalars().all()
    return paginated([StockMovementOut.model_validate(r) for r in rows], total, page)


@router.post(
    "/adjustments",
    response_model=list[StockMovementOut],
    status_code=201,
    summary="Post a manual stock adjustment",
)
def create_adjustment(
    payload: AdjustmentIn,
    ctx: Ctx = Depends(require("stock.adjustments", "CREATE")),
) -> list[StockMovementOut]:
    movements = stock_service.post_adjustment(
        ctx.db,
        warehouse_id=payload.warehouse_id,
        product_id=payload.product_id,
        base_quantity=payload.base_quantity,
        movement_type=str(payload.movement_type),
        lot_id=payload.lot_id,
        unit_cost=payload.unit_cost,
        status=str(payload.status),
        reason=payload.reason,
        user_id=ctx.user_id,
        audit=ctx.audit_kwargs(),
    )
    return [StockMovementOut.model_validate(m) for m in movements]


# ===========================================================================
# Transfers
# ===========================================================================
@router.get(
    "/transfers", response_model=PagedResponse[TransferOut], summary="List transfers"
)
def list_transfers(
    ctx: Ctx = Depends(require("stock.transfers", "VIEW")),
    page: Page = Depends(get_page),
    status: TransferStatus | None = Query(default=None),
    source_warehouse_id: int | None = Query(default=None),
    target_warehouse_id: int | None = Query(default=None),
    vehicle_id: int | None = Query(default=None),
    start: date | None = Query(default=None),
    end: date | None = Query(default=None),
) -> dict[str, Any]:
    stmt = select(StockTransfer)
    if status:
        stmt = stmt.where(StockTransfer.status == str(status))
    if source_warehouse_id:
        stmt = stmt.where(StockTransfer.source_warehouse_id == source_warehouse_id)
    if target_warehouse_id:
        stmt = stmt.where(StockTransfer.target_warehouse_id == target_warehouse_id)
    if vehicle_id:
        stmt = stmt.where(StockTransfer.vehicle_id == vehicle_id)
    if start:
        stmt = stmt.where(StockTransfer.transfer_date >= start)
    if end:
        stmt = stmt.where(StockTransfer.transfer_date <= end)

    total = int(
        ctx.db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    )
    rows = ctx.db.execute(
        stmt.order_by(StockTransfer.transfer_date.desc(), StockTransfer.id.desc())
        .offset(page.offset)
        .limit(page.limit)
    ).scalars().all()
    return paginated([_transfer_out(ctx, r) for r in rows], total, page)


@router.post(
    "/transfers", response_model=TransferOut, status_code=201, summary="Create transfer"
)
def create_transfer(
    payload: TransferCreate,
    ctx: Ctx = Depends(require("stock.transfers", "CREATE")),
) -> TransferOut:
    transfer = stock_service.create_transfer(
        ctx.db,
        source_warehouse_id=payload.source_warehouse_id,
        target_warehouse_id=payload.target_warehouse_id,
        items=[item.model_dump(exclude_none=True) for item in payload.items],
        transfer_date=payload.transfer_date,
        vehicle_id=payload.vehicle_id,
        driver_id=payload.driver_id,
        notes=payload.notes,
        user_id=ctx.user_id,
    )
    audit_service.record(
        ctx.db,
        AuditAction.CREATE,
        entity_type="StockTransfer",
        entity_id=transfer.id,
        entity_label=transfer.document_no,
        summary=f"transfer created {transfer.document_no}",
        new_values={
            "source_warehouse_id": transfer.source_warehouse_id,
            "target_warehouse_id": transfer.target_warehouse_id,
            "lines": len(transfer.items),
        },
        **ctx.audit_kwargs(),
    )
    ctx.db.commit()
    return _transfer_out(ctx, transfer)


@router.get(
    "/transfers/{transfer_id}", response_model=TransferOut, summary="Transfer detail"
)
def get_transfer(
    transfer_id: int,
    ctx: Ctx = Depends(require("stock.transfers", "VIEW")),
) -> TransferOut:
    return _transfer_out(ctx, _get_transfer(ctx, transfer_id))


@router.put(
    "/transfers/{transfer_id}", response_model=TransferOut, summary="Update a draft transfer"
)
def update_transfer(
    transfer_id: int,
    payload: TransferUpdate,
    ctx: Ctx = Depends(require("stock.transfers", "UPDATE")),
) -> TransferOut:
    transfer = _get_transfer(ctx, transfer_id)
    stock_service.update_transfer(
        ctx.db,
        transfer,
        items=(
            [item.model_dump(exclude_none=True) for item in payload.items]
            if payload.items is not None
            else None
        ),
        transfer_date=payload.transfer_date,
        vehicle_id=payload.vehicle_id,
        driver_id=payload.driver_id,
        notes=payload.notes,
        user_id=ctx.user_id,
    )
    audit_service.record(
        ctx.db,
        AuditAction.UPDATE,
        entity_type="StockTransfer",
        entity_id=transfer.id,
        entity_label=transfer.document_no,
        summary=f"transfer updated {transfer.document_no}",
        **ctx.audit_kwargs(),
    )
    ctx.db.commit()
    return _transfer_out(ctx, transfer)


@router.post(
    "/transfers/{transfer_id}/ship",
    response_model=TransferOut,
    summary="Ship a transfer (DRAFT to IN_TRANSIT)",
)
def ship_transfer(
    transfer_id: int,
    ctx: Ctx = Depends(require("stock.transfers", "UPDATE")),
) -> TransferOut:
    transfer = _get_transfer(ctx, transfer_id)
    stock_service.transfer_out(
        ctx.db, transfer, user_id=ctx.user_id, audit=ctx.audit_kwargs()
    )
    return _transfer_out(ctx, transfer)


@router.post(
    "/transfers/{transfer_id}/receive",
    response_model=TransferOut,
    summary="Receive a transfer (IN_TRANSIT to RECEIVED)",
)
def receive_transfer(
    transfer_id: int,
    payload: TransferReceiveIn,
    ctx: Ctx = Depends(require("stock.transfers", "APPROVE")),
) -> TransferOut:
    transfer = _get_transfer(ctx, transfer_id)
    received = (
        {line.item_id: line.received_quantity for line in payload.lines}
        if payload.lines
        else None
    )
    if payload.notes:
        transfer.notes = payload.notes
    stock_service.transfer_in(
        ctx.db,
        transfer,
        received=received,
        user_id=ctx.user_id,
        audit=ctx.audit_kwargs(),
    )
    return _transfer_out(ctx, transfer)


@router.post(
    "/transfers/{transfer_id}/cancel",
    response_model=TransferOut,
    summary="Cancel a draft transfer",
)
def cancel_transfer(
    transfer_id: int,
    ctx: Ctx = Depends(require("stock.transfers", "DELETE")),
) -> TransferOut:
    transfer = _get_transfer(ctx, transfer_id)
    stock_service.cancel_transfer(
        ctx.db, transfer, user_id=ctx.user_id, audit=ctx.audit_kwargs()
    )
    return _transfer_out(ctx, transfer)


# ===========================================================================
# Counts
# ===========================================================================
@router.get("/counts", response_model=PagedResponse[CountOut], summary="List stock counts")
def list_counts(
    ctx: Ctx = Depends(require("stock.counts", "VIEW")),
    page: Page = Depends(get_page),
    warehouse_id: int | None = Query(default=None),
    status: CountStatus | None = Query(default=None),
    is_van_end_of_day: bool | None = Query(default=None),
    start: date | None = Query(default=None),
    end: date | None = Query(default=None),
) -> dict[str, Any]:
    stmt = select(StockCount)
    if warehouse_id:
        stmt = stmt.where(StockCount.warehouse_id == warehouse_id)
    if status:
        stmt = stmt.where(StockCount.status == str(status))
    if is_van_end_of_day is not None:
        stmt = stmt.where(StockCount.is_van_end_of_day.is_(is_van_end_of_day))
    if start:
        stmt = stmt.where(StockCount.count_date >= start)
    if end:
        stmt = stmt.where(StockCount.count_date <= end)

    total = int(
        ctx.db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    )
    rows = ctx.db.execute(
        stmt.order_by(StockCount.count_date.desc(), StockCount.id.desc())
        .offset(page.offset)
        .limit(page.limit)
    ).scalars().all()
    return paginated([_count_out(ctx, r) for r in rows], total, page)


@router.post("/counts", response_model=CountOut, status_code=201, summary="Open a count sheet")
def create_count(
    payload: CountCreate,
    ctx: Ctx = Depends(require("stock.counts", "CREATE")),
) -> CountOut:
    count = stock_service.create_count(
        ctx.db,
        warehouse_id=payload.warehouse_id,
        count_date=payload.count_date,
        counted_by_id=payload.counted_by_id or ctx.user_id,
        day_session_id=payload.day_session_id,
        is_van_end_of_day=payload.is_van_end_of_day,
        product_ids=payload.product_ids or None,
        prefill=payload.prefill,
        notes=payload.notes,
        user_id=ctx.user_id,
    )
    audit_service.record(
        ctx.db,
        AuditAction.CREATE,
        entity_type="StockCount",
        entity_id=count.id,
        entity_label=count.document_no,
        summary=f"count opened {count.document_no}",
        **ctx.audit_kwargs(),
    )
    ctx.db.commit()
    return _count_out(ctx, count)


@router.get("/counts/{count_id}", response_model=CountOut, summary="Count detail")
def get_count(
    count_id: int,
    ctx: Ctx = Depends(require("stock.counts", "VIEW")),
) -> CountOut:
    return _count_out(ctx, _get_count(ctx, count_id))


@router.put(
    "/counts/{count_id}/lines",
    response_model=CountOut,
    summary="Record counted quantities",
)
def set_count_lines(
    count_id: int,
    payload: CountSubmit,
    ctx: Ctx = Depends(require("stock.counts", "UPDATE")),
) -> CountOut:
    count = _get_count(ctx, count_id)
    if payload.notes is not None:
        count.notes = payload.notes
    stock_service.set_counted_quantities(
        ctx.db,
        count,
        [
            {
                "product_id": line.product_id,
                "lot_id": line.lot_id,
                "counted_quantity": line.counted_quantity,
                "reason": line.reason,
            }
            for line in payload.lines
        ],
        counted_by_id=payload.counted_by_id or ctx.user_id,
        user_id=ctx.user_id,
    )
    ctx.db.commit()
    return _count_out(ctx, count)


@router.post(
    "/counts/{count_id}/approve",
    response_model=CountOut,
    summary="Approve a count and post the variances",
)
def approve_count(
    count_id: int,
    ctx: Ctx = Depends(require("stock.counts", "APPROVE")),
) -> CountOut:
    count = _get_count(ctx, count_id)
    stock_service.approve_count(
        ctx.db, count, user_id=ctx.user_id, audit=ctx.audit_kwargs()
    )
    return _count_out(ctx, count)


# ===========================================================================
# Lots
# ===========================================================================
@router.get("/lots", response_model=PagedResponse[LotOut], summary="List lots")
def list_lots(
    ctx: Ctx = Depends(require("stock.lots", "VIEW")),
    page: Page = Depends(get_page),
    product_id: int | None = Query(default=None),
    search: str | None = Query(default=None, description="Lot, batch or serial number"),
    is_blocked: bool | None = Query(default=None),
    expires_before: date | None = Query(default=None),
) -> dict[str, Any]:
    stmt = select(Lot)
    if product_id:
        stmt = stmt.where(Lot.product_id == product_id)
    if search:
        term = f"%{search.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Lot.lot_number).like(term),
                func.lower(Lot.batch_number).like(term),
                func.lower(Lot.serial_number).like(term),
            )
        )
    if is_blocked is not None:
        stmt = stmt.where(Lot.is_blocked.is_(is_blocked))
    if expires_before:
        stmt = stmt.where(Lot.expiry_date.is_not(None), Lot.expiry_date <= expires_before)

    total = int(
        ctx.db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    )
    rows = ctx.db.execute(
        stmt.order_by(Lot.expiry_date.asc(), Lot.id.asc())
        .offset(page.offset)
        .limit(page.limit)
    ).scalars().all()
    return paginated([_lot_out(ctx, r) for r in rows], total, page)


@router.post("/lots", response_model=LotOut, status_code=201, summary="Create a lot")
def create_lot(
    payload: LotCreate,
    ctx: Ctx = Depends(require("stock.lots", "CREATE")),
) -> LotOut:
    lot = stock_service.create_lot(
        ctx.db,
        product_id=payload.product_id,
        lot_number=payload.lot_number,
        expiry_date=payload.expiry_date,
        production_date=payload.production_date,
        unit_cost=payload.unit_cost,
        supplier_name=payload.supplier_name,
        batch_number=payload.batch_number,
        serial_number=payload.serial_number,
        received_date=payload.received_date,
        notes=payload.notes,
        user_id=ctx.user_id,
    )
    audit_service.record(
        ctx.db,
        AuditAction.CREATE,
        entity_type="Lot",
        entity_id=lot.id,
        entity_label=lot.lot_number,
        summary=f"lot created {lot.lot_number}",
        new_values=payload.model_dump(mode="json"),
        **ctx.audit_kwargs(),
    )
    ctx.db.commit()
    return _lot_out(ctx, lot)


@router.get("/lots/{lot_id}", response_model=LotOut, summary="Lot detail")
def get_lot(lot_id: int, ctx: Ctx = Depends(require("stock.lots", "VIEW"))) -> LotOut:
    return _lot_out(ctx, _get_lot(ctx, lot_id), with_stock=True)


@router.put("/lots/{lot_id}", response_model=LotOut, summary="Update a lot")
def update_lot(
    lot_id: int,
    payload: LotUpdate,
    ctx: Ctx = Depends(require("stock.lots", "UPDATE")),
) -> LotOut:
    lot = _get_lot(ctx, lot_id)
    changes = payload.model_dump(exclude_unset=True)
    before = {key: getattr(lot, key) for key in changes}
    for key, value in changes.items():
        if key == "unit_cost" and value is not None:
            setattr(lot, key, money(value))
        else:
            setattr(lot, key, value)
    lot.updated_by_id = ctx.user_id
    ctx.db.flush()

    audit_service.record(
        ctx.db,
        AuditAction.UPDATE,
        entity_type="Lot",
        entity_id=lot.id,
        entity_label=lot.lot_number,
        summary=f"lot updated {lot.lot_number}",
        old_values=before,
        new_values=changes,
        **ctx.audit_kwargs(),
    )
    ctx.db.commit()
    return _lot_out(ctx, lot)


@router.post("/lots/{lot_id}/block", response_model=LotOut, summary="Block or release a lot")
def block_lot(
    lot_id: int,
    payload: LotBlockIn,
    ctx: Ctx = Depends(require("stock.lots", "UPDATE")),
) -> LotOut:
    lot = stock_service.block_lot(
        ctx.db,
        lot_id,
        blocked=payload.blocked,
        reason=payload.reason,
        user_id=ctx.user_id,
        audit=ctx.audit_kwargs(),
    )
    return _lot_out(ctx, lot)


# ===========================================================================
# Vehicle stock
# ===========================================================================
@router.get(
    "/vehicles/{vehicle_id}/stock",
    response_model=list[VehicleStockRow],
    summary="On-board stock of a vehicle",
)
def vehicle_stock(
    vehicle_id: int,
    ctx: Ctx = Depends(require("stock.vehicle_stock", "VIEW")),
) -> list[VehicleStockRow]:
    allowed = _accessible_vehicle_ids(ctx)
    if allowed is not None and vehicle_id not in allowed:
        raise PermissionDeniedError(
            "auth.permission_denied",
            params={"resource": "stock.vehicle_stock", "action": "VIEW"},
        )
    return [VehicleStockRow(**row) for row in stock_service.vehicle_stock(ctx.db, vehicle_id)]


# ===========================================================================
# Warehouse detail (parameterised paths last)
# ===========================================================================
@router.get("/{warehouse_id}", response_model=WarehouseOut, summary="Warehouse detail")
def get_warehouse(
    warehouse_id: int,
    ctx: Ctx = Depends(require("stock.warehouses", "VIEW")),
) -> WarehouseOut:
    return _warehouse_out(ctx, _get_warehouse(ctx, warehouse_id), with_totals=True)


@router.put("/{warehouse_id}", response_model=WarehouseOut, summary="Update warehouse")
def update_warehouse(
    warehouse_id: int,
    payload: WarehouseUpdate,
    ctx: Ctx = Depends(require("stock.warehouses", "UPDATE")),
) -> WarehouseOut:
    warehouse = _get_warehouse(ctx, warehouse_id)
    changes = payload.model_dump(exclude_unset=True)
    before = {key: getattr(warehouse, key) for key in changes}
    for key, value in changes.items():
        setattr(warehouse, key, str(value) if key in ("warehouse_type", "allocation_strategy") and value is not None else value)
    warehouse.updated_by_id = ctx.user_id
    ctx.db.flush()

    audit_service.record(
        ctx.db,
        AuditAction.UPDATE,
        entity_type="Warehouse",
        entity_id=warehouse.id,
        entity_label=warehouse.code,
        summary=f"warehouse updated {warehouse.code}",
        old_values=before,
        new_values=changes,
        **ctx.audit_kwargs(),
    )
    ctx.db.commit()
    return _warehouse_out(ctx, warehouse)


@router.delete("/{warehouse_id}", response_model=Message, summary="Deactivate warehouse")
def delete_warehouse(
    warehouse_id: int,
    ctx: Ctx = Depends(require("stock.warehouses", "DELETE")),
) -> Message:
    """
    Soft-delete only, and never while stock is on hand — the movement ledger
    references this warehouse forever.
    """
    warehouse = _get_warehouse(ctx, warehouse_id)
    on_hand = ctx.db.execute(
        select(func.count()).select_from(
            select(StockBalance.id)
            .where(StockBalance.warehouse_id == warehouse.id, StockBalance.quantity != 0)
            .subquery()
        )
    ).scalar_one()
    if on_hand:
        raise BusinessRuleError(
            "stock.warehouse_not_empty",
            params={"code": warehouse.code, "lines": int(on_hand)},
        )
    if warehouse.warehouse_type == WarehouseType.VEHICLE:
        linked = ctx.db.execute(
            select(Vehicle.id).where(Vehicle.warehouse_id == warehouse.id)
        ).scalar_one_or_none()
        if linked:
            raise BusinessRuleError(
                "stock.warehouse_in_use", params={"code": warehouse.code}
            )

    warehouse.is_deleted = True
    warehouse.is_active = False
    warehouse.deleted_by_id = ctx.user_id
    ctx.db.flush()

    audit_service.record(
        ctx.db,
        AuditAction.DELETE,
        entity_type="Warehouse",
        entity_id=warehouse.id,
        entity_label=warehouse.code,
        summary=f"warehouse deactivated {warehouse.code}",
        **ctx.audit_kwargs(),
    )
    ctx.db.commit()
    return Message(message=t("common.deleted", ctx.lang), message_key="common.deleted")


@router.get(
    "/{warehouse_id}/stock",
    response_model=PagedResponse[StockBalanceOut],
    summary="Stock on hand in a warehouse",
)
def warehouse_stock(
    warehouse_id: int,
    ctx: Ctx = Depends(require("stock.warehouses", "VIEW")),
    page: Page = Depends(get_page),
    search: str | None = Query(default=None, description="SKU or product name"),
    product_id: int | None = Query(default=None),
    status: str | None = Query(default=None),
    include_zero: bool = Query(default=False),
) -> dict[str, Any]:
    warehouse = _get_warehouse(ctx, warehouse_id)
    stmt = (
        select(StockBalance)
        .join(Product, Product.id == StockBalance.product_id)
        .where(StockBalance.warehouse_id == warehouse.id)
    )
    if not include_zero:
        stmt = stmt.where(StockBalance.quantity != 0)
    if product_id:
        stmt = stmt.where(StockBalance.product_id == product_id)
    if status:
        stmt = stmt.where(StockBalance.status == status)
    if search:
        term = f"%{search.lower()}%"
        stmt = stmt.where(
            or_(func.lower(Product.sku).like(term), func.lower(Product.name).like(term))
        )

    total = int(
        ctx.db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    )
    rows = ctx.db.execute(
        stmt.order_by(Product.name.asc(), StockBalance.lot_id.asc())
        .offset(page.offset)
        .limit(page.limit)
    ).scalars().all()
    return paginated([_balance_out(ctx, r) for r in rows], total, page)


@router.get(
    "/{warehouse_id}/valuation",
    response_model=ValuationOut,
    summary="Warehouse stock valuation",
)
def warehouse_valuation(
    warehouse_id: int,
    ctx: Ctx = Depends(require("stock.warehouses", "VIEW")),
) -> ValuationOut:
    warehouse = _get_warehouse(ctx, warehouse_id)
    totals = ctx.db.execute(
        select(
            func.count(func.distinct(StockBalance.product_id)),
            func.count(func.distinct(StockBalance.lot_id)),
        ).where(StockBalance.warehouse_id == warehouse.id, StockBalance.quantity != 0)
    ).one()
    quantities = ctx.db.execute(
        select(func.sum(StockBalance.quantity)).where(
            StockBalance.warehouse_id == warehouse.id
        )
    ).scalar_one_or_none()

    return ValuationOut(
        warehouse_id=warehouse.id,
        warehouse_code=warehouse.code,
        warehouse_name=warehouse.name,
        total_value=stock_service.warehouse_valuation(ctx.db, warehouse.id),
        total_quantity=qty(D(quantities)),
        product_count=int(totals[0] or 0),
        lot_count=int(totals[1] or 0),
    )

