"""
Stock engine — allocation, the movement ledger, transfers and physical counts.

Everything in the system that moves goods posts through this module, so the
rules live here exactly once:

* ``stock_movements`` is append-only and is the source of truth.  Corrections
  are new movements, never edits — that is what makes van reconciliation and
  stock audits provable.
* ``stock_balances`` is a materialised per-(warehouse, product, lot, status)
  balance kept in step inside the same transaction, so reads stay O(1).
* Picking is **FEFO** by default: in food & beverage distribution the oldest
  expiry must leave first or the van ends the week full of unsellable goods.
* A vehicle is a ``VEHICLE``-type warehouse, so vans obey the identical rules.

Money is always :class:`~decimal.Decimal`; quantities are always in the
product's **base unit** by the time they reach the ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.enums import (
    AllocationStrategy,
    AuditAction,
    CountStatus,
    StockMovementType,
    StockStatus,
    TransferStatus,
    WarehouseType,
)
from app.core.exceptions import (
    BusinessRuleError,
    InsufficientStockError,
    NotFoundError,
    ValidationError,
)
from app.core.logging_config import get_logger
from app.core.utils import D, money, qty
from app.models.base import utcnow
from app.models.product import Product
from app.models.vehicle import Vehicle
from app.models.warehouse import (
    Lot,
    StockBalance,
    StockCount,
    StockCountItem,
    StockMovement,
    StockTransfer,
    StockTransferItem,
    Warehouse,
)
from app.services import audit_service, numbering_service

log = get_logger("app.stock")

ZERO = Decimal("0")

#: Reference type stamped on movements produced by a transfer document.
REF_TRANSFER = "STOCK_TRANSFER"
#: Reference type stamped on movements produced by an approved count.
REF_COUNT = "STOCK_COUNT"
#: Reference type stamped on manual adjustments.
REF_ADJUSTMENT = "STOCK_ADJUSTMENT"


# ===========================================================================
# Allocation result
# ===========================================================================
@dataclass
class Allocation:
    """One lot's contribution to a requested quantity."""

    lot_id: int | None
    quantity: Decimal
    unit_cost: Decimal
    expiry_date: date | None


# ===========================================================================
# Small internal helpers
# ===========================================================================
def _warehouse(db: Session, warehouse_id: int) -> Warehouse:
    wh = db.get(Warehouse, warehouse_id)
    if wh is None or wh.is_deleted:
        raise NotFoundError("stock.warehouse_not_found", params={"id": warehouse_id})
    return wh


def _product(db: Session, product_id: int) -> Product:
    prod = db.get(Product, product_id)
    if prod is None or prod.is_deleted:
        raise NotFoundError("product.not_found", params={"id": product_id})
    return prod


def _lot(db: Session, lot_id: int) -> Lot:
    lot = db.get(Lot, lot_id)
    if lot is None:
        raise NotFoundError("stock.lot_not_found", params={"id": lot_id})
    return lot


def _lot_key(lot_id: int | None) -> int:
    """Balances key untracked stock on lot ``0`` so the unique index still works."""
    return int(lot_id or 0)


def _resolve_cost(
    balance: StockBalance | None, lot: Lot | None, product: Product
) -> Decimal:
    """Cost to stamp on an issue: moving average, else lot cost, else product cost."""
    if balance is not None and D(balance.average_cost) > 0:
        return money(balance.average_cost)
    if lot is not None and D(lot.unit_cost) > 0:
        return money(lot.unit_cost)
    return money(product.cost_price)


def _sort_key(strategy: str, balance: StockBalance, lot: Lot | None) -> tuple[date, int]:
    """
    Ordering key for picking.

    Lots with no relevant date sort **last** (``date.max``) — an unknown expiry
    must never jump ahead of a lot with a real, nearer expiry date.
    """
    lot_id = _lot_key(balance.lot_id)
    if strategy == AllocationStrategy.FEFO:
        when = lot.expiry_date if lot else None
    else:
        when = (lot.received_date or lot.production_date) if lot else None
    return (when or date.max, lot_id)


def _available_candidates(
    db: Session,
    warehouse_id: int,
    product_id: int,
    *,
    allow_expired: bool = False,
    on: date | None = None,
) -> list[tuple[StockBalance, Lot | None, Decimal]]:
    """Balance rows that may actually be picked, with their free quantity."""
    today = on or date.today()
    rows = db.execute(
        select(StockBalance).where(
            StockBalance.warehouse_id == warehouse_id,
            StockBalance.product_id == product_id,
            StockBalance.status == StockStatus.AVAILABLE,
        )
    ).scalars().all()

    out: list[tuple[StockBalance, Lot | None, Decimal]] = []
    for row in rows:
        free = qty(D(row.quantity) - D(row.reserved_quantity))
        if free <= 0:
            continue
        lot = db.get(Lot, row.lot_id) if row.lot_id else None
        if lot is not None:
            if lot.is_blocked:
                continue
            if not allow_expired and lot.is_expired(today):
                continue
        out.append((row, lot, free))
    return out


def day_start(on: date) -> datetime:
    """Inclusive lower bound for a date filter on a timestamp column."""
    return datetime.combine(on, time.min, tzinfo=UTC)


def day_end(on: date) -> datetime:
    """Inclusive upper bound for a date filter on a timestamp column."""
    return datetime.combine(on, time.max, tzinfo=UTC)


def _on_hand(db: Session, warehouse_id: int, product_id: int) -> Decimal:
    """Total physical quantity of a product in a warehouse across every lot and status."""
    rows = db.execute(
        select(StockBalance.quantity).where(
            StockBalance.warehouse_id == warehouse_id,
            StockBalance.product_id == product_id,
        )
    ).scalars().all()
    total = ZERO
    for value in rows:
        total += D(value)
    return qty(total)


# ===========================================================================
# Units of measure
# ===========================================================================
def uom_factor(product: Product, uom: str | None) -> Decimal:
    """
    How many base units one *uom* contains.

    Resolution order: explicit ``ProductUnit`` row, the product's own
    base/sales unit, then failure — guessing a factor would silently corrupt
    the ledger.
    """
    if not uom or uom == product.base_uom:
        return Decimal("1")

    for unit in product.units or []:
        if unit.uom == uom:
            factor = D(unit.factor)
            if factor > 0:
                return factor

    if uom == product.sales_uom:
        factor = D(product.units_per_case)
        if factor > 0:
            return factor

    raise ValidationError("product.invalid_uom", params={"uom": uom, "name": product.name})


def to_base(product: Product, quantity: Decimal | float | str, uom: str | None) -> Decimal:
    """Convert a quantity expressed in *uom* into base units."""
    return qty(D(quantity) * uom_factor(product, uom))


def from_base(product: Product, base_quantity: Decimal, uom: str | None) -> Decimal:
    """Convert base units back into *uom* (for display / documents)."""
    factor = uom_factor(product, uom)
    if factor == 0:
        return ZERO
    return qty(D(base_quantity) / factor)


# ===========================================================================
# Reads
# ===========================================================================
def get_available(db: Session, warehouse_id: int, product_id: int) -> Decimal:
    """
    Sellable quantity: AVAILABLE status, less reservations, less blocked and
    expired lots.  This — not the raw on-hand figure — is what the sales flow
    may consume.
    """
    total = ZERO
    for _balance, _lot, free in _available_candidates(db, warehouse_id, product_id):
        total += free
    return qty(total)


def get_on_hand(db: Session, warehouse_id: int, product_id: int) -> Decimal:
    """Physical quantity present, including blocked, expired and damaged stock."""
    return _on_hand(db, warehouse_id, product_id)


def get_balances(
    db: Session,
    warehouse_id: int,
    *,
    product_ids: list[int] | None = None,
    include_zero: bool = False,
) -> list[StockBalance]:
    """Materialised balance rows of a warehouse, newest movement first."""
    stmt = select(StockBalance).where(StockBalance.warehouse_id == warehouse_id)
    if product_ids:
        stmt = stmt.where(StockBalance.product_id.in_(product_ids))
    if not include_zero:
        stmt = stmt.where(StockBalance.quantity != 0)
    stmt = stmt.order_by(StockBalance.product_id.asc(), StockBalance.lot_id.asc())
    return list(db.execute(stmt).scalars().all())


# ===========================================================================
# Balance maintenance
# ===========================================================================
def _apply_balance(
    db: Session,
    warehouse_id: int,
    product_id: int,
    lot_id: int | None,
    status: str,
    delta: Decimal,
    unit_cost: Decimal,
) -> StockBalance:
    """
    Upsert one balance bucket and maintain its moving-average cost.

    Receipts re-weight the average — ``(old_qty*old_cost + in_qty*in_cost) /
    new_qty`` — while issues leave it untouched, which is what keeps margin
    reporting stable when the same SKU arrives at different prices.

    A zero *unit_cost* on a receipt means "cost unknown" (free goods, van
    returns, count surplus), so it never drags the average down to zero.
    """
    key_lot = _lot_key(lot_id)
    balance = db.execute(
        select(StockBalance).where(
            StockBalance.warehouse_id == warehouse_id,
            StockBalance.product_id == product_id,
            StockBalance.lot_id == key_lot,
            StockBalance.status == str(status),
        )
    ).scalar_one_or_none()

    if balance is None:
        balance = StockBalance(
            warehouse_id=warehouse_id,
            product_id=product_id,
            lot_id=key_lot,
            status=str(status),
            quantity=ZERO,
            reserved_quantity=ZERO,
            average_cost=ZERO,
        )
        db.add(balance)
        db.flush()

    old_qty = qty(balance.quantity)
    change = qty(delta)
    new_qty = qty(old_qty + change)
    in_cost = money(unit_cost)

    if change > 0 and in_cost > 0:
        if old_qty > 0 and new_qty > 0:
            balance.average_cost = money(
                (old_qty * D(balance.average_cost) + change * in_cost) / new_qty
            )
        elif new_qty > 0:
            # Empty (or negative) bucket: the incoming receipt defines the cost.
            balance.average_cost = in_cost

    balance.quantity = new_qty
    balance.last_movement_at = utcnow()
    db.flush()
    return balance


# ===========================================================================
# The ledger
# ===========================================================================
def post_movement(
    db: Session,
    *,
    warehouse_id: int,
    product_id: int,
    movement_type: str,
    base_quantity: Decimal,
    lot_id: int | None = None,
    unit_cost: Decimal = Decimal("0"),
    status: str = StockStatus.AVAILABLE,
    reference_type: str | None = None,
    reference_id: int | None = None,
    reference_no: str | None = None,
    counterparty_warehouse_id: int | None = None,
    salesperson_id: int | None = None,
    customer_id: int | None = None,
    day_session_id: int | None = None,
    user_id: int | None = None,
    notes: str | None = None,
    moved_at: datetime | None = None,
) -> StockMovement:
    """
    Append one immutable ledger row and move the matching balance bucket.

    *base_quantity* is **signed**: positive is stock in, negative is stock out.
    ``balance_after`` is the post-update on-hand total of that
    (warehouse, product) across every lot, so a stock card reads correctly even
    when picking spans several lots.
    """
    change = qty(base_quantity)
    if change == 0:
        raise ValidationError(
            "stock.quantity_positive", params={"quantity": str(base_quantity)}
        )

    warehouse = _warehouse(db, warehouse_id)
    product = _product(db, product_id)

    cost = money(unit_cost)
    if cost <= 0 and change < 0:
        # Issues inherit the bucket's average so COGS stays meaningful.
        bucket = db.execute(
            select(StockBalance).where(
                StockBalance.warehouse_id == warehouse_id,
                StockBalance.product_id == product_id,
                StockBalance.lot_id == _lot_key(lot_id),
                StockBalance.status == str(status),
            )
        ).scalar_one_or_none()
        cost = _resolve_cost(bucket, db.get(Lot, lot_id) if lot_id else None, product)

    _apply_balance(db, warehouse.id, product.id, lot_id, str(status), change, cost)

    movement = StockMovement(
        warehouse_id=warehouse.id,
        product_id=product.id,
        lot_id=lot_id or None,
        movement_type=str(movement_type),
        status=str(status),
        quantity=change,
        unit_cost=cost,
        total_cost=money(cost * abs(change)),
        balance_after=_on_hand(db, warehouse.id, product.id),
        counterparty_warehouse_id=counterparty_warehouse_id,
        reference_type=reference_type,
        reference_id=reference_id,
        reference_no=reference_no,
        salesperson_id=salesperson_id,
        customer_id=customer_id,
        day_session_id=day_session_id,
        moved_at=moved_at or utcnow(),
        created_by_id=user_id,
        notes=notes,
    )
    db.add(movement)
    db.flush()
    return movement


# ===========================================================================
# Allocation
# ===========================================================================
def allocate(
    db: Session,
    warehouse_id: int,
    product_id: int,
    base_quantity: Decimal,
    *,
    strategy: str | None = None,
    allow_expired: bool = False,
) -> list[Allocation]:
    """
    Decide which lots satisfy *base_quantity*, without touching the ledger.

    Strategy precedence: explicit argument, then the warehouse's own
    ``allocation_strategy``, then the system default.  Blocked lots are never
    picked and expired lots only when *allow_expired* is set (write-off and
    return-to-depot flows need them).

    A warehouse flagged ``allows_negative_stock`` absorbs any shortfall onto the
    last picked lot instead of failing — some depots post receipts after the
    van has already left, and blocking the sale would be worse than the
    temporary negative.
    """
    wanted = qty(base_quantity)
    if wanted <= 0:
        raise ValidationError(
            "stock.quantity_positive", params={"quantity": str(base_quantity)}
        )

    warehouse = _warehouse(db, warehouse_id)
    product = _product(db, product_id)

    chosen = str(
        strategy or warehouse.allocation_strategy or settings.stock_allocation_strategy
    ).upper()
    reverse = chosen == AllocationStrategy.LIFO
    order_by = AllocationStrategy.FIFO if reverse else chosen

    candidates = _available_candidates(
        db, warehouse.id, product.id, allow_expired=allow_expired
    )
    candidates.sort(key=lambda c: _sort_key(order_by, c[0], c[1]), reverse=reverse)

    out: list[Allocation] = []
    remaining = wanted
    for balance, lot, free in candidates:
        if remaining <= 0:
            break
        take = free if free < remaining else remaining
        out.append(
            Allocation(
                lot_id=_lot_key(balance.lot_id) or None,
                quantity=qty(take),
                unit_cost=_resolve_cost(balance, lot, product),
                expiry_date=lot.expiry_date if lot else None,
            )
        )
        remaining = qty(remaining - take)

    if remaining > 0:
        if not warehouse.allows_negative_stock:
            raise InsufficientStockError(
                "stock.insufficient",
                params={
                    "product": product.name,
                    "sku": product.sku,
                    "requested": str(wanted),
                    "available": str(qty(wanted - remaining)),
                },
            )
        if out:
            out[-1].quantity = qty(out[-1].quantity + remaining)
        else:
            # Nothing on hand at all: the shortfall lands on the untracked bucket.
            out.append(
                Allocation(
                    lot_id=None,
                    quantity=remaining,
                    unit_cost=money(product.cost_price),
                    expiry_date=None,
                )
            )
        log.warning(
            "negative stock allowed warehouse=%s product=%s shortfall=%s",
            warehouse.code, product.sku, remaining,
        )

    return out


# ===========================================================================
# Issue / receive
# ===========================================================================
def issue_stock(
    db: Session,
    *,
    warehouse_id: int,
    product_id: int,
    base_quantity: Decimal,
    movement_type: str,
    allow_expired: bool = False,
    strategy: str | None = None,
    **movement_kwargs: Any,
) -> list[StockMovement]:
    """
    Take stock out: allocate by FEFO/FIFO then post one negative movement per lot.

    Raises :class:`InsufficientStockError` before writing anything, so a failed
    issue never leaves half a document in the ledger.
    """
    amount = qty(base_quantity)
    if amount <= 0:
        raise ValidationError(
            "stock.quantity_positive", params={"quantity": str(base_quantity)}
        )

    allocations = allocate(
        db,
        warehouse_id,
        product_id,
        amount,
        strategy=strategy,
        allow_expired=allow_expired,
    )
    movements: list[StockMovement] = []
    for alloc in allocations:
        movements.append(
            post_movement(
                db,
                warehouse_id=warehouse_id,
                product_id=product_id,
                movement_type=movement_type,
                base_quantity=-alloc.quantity,
                lot_id=alloc.lot_id,
                unit_cost=alloc.unit_cost,
                **movement_kwargs,
            )
        )
    return movements


def receive_stock(
    db: Session,
    *,
    warehouse_id: int,
    product_id: int,
    base_quantity: Decimal,
    movement_type: str,
    lot_id: int | None = None,
    unit_cost: Decimal = Decimal("0"),
    **movement_kwargs: Any,
) -> StockMovement:
    """Bring stock in as a single positive movement against one lot."""
    amount = qty(base_quantity)
    if amount <= 0:
        raise ValidationError(
            "stock.quantity_positive", params={"quantity": str(base_quantity)}
        )

    cost = money(unit_cost)
    if cost <= 0:
        lot = db.get(Lot, lot_id) if lot_id else None
        cost = _resolve_cost(None, lot, _product(db, product_id))

    return post_movement(
        db,
        warehouse_id=warehouse_id,
        product_id=product_id,
        movement_type=movement_type,
        base_quantity=amount,
        lot_id=lot_id,
        unit_cost=cost,
        **movement_kwargs,
    )


def post_adjustment(
    db: Session,
    *,
    warehouse_id: int,
    product_id: int,
    base_quantity: Decimal,
    movement_type: str = StockMovementType.ADJUSTMENT,
    lot_id: int | None = None,
    unit_cost: Decimal = Decimal("0"),
    status: str = StockStatus.AVAILABLE,
    reason: str | None = None,
    user_id: int | None = None,
    audit: dict[str, Any] | None = None,
    commit: bool = True,
) -> list[StockMovement]:
    """
    Manual correction of a balance — the one place stock changes with no source
    document, therefore always audited with the value at stake.

    Negative adjustments pick lots by the warehouse strategy unless the caller
    named a specific lot.
    """
    change = qty(base_quantity)
    if change == 0:
        raise ValidationError(
            "stock.quantity_positive", params={"quantity": str(base_quantity)}
        )

    warehouse = _warehouse(db, warehouse_id)
    product = _product(db, product_id)

    if change > 0:
        movements = [
            receive_stock(
                db,
                warehouse_id=warehouse.id,
                product_id=product.id,
                base_quantity=change,
                movement_type=movement_type,
                lot_id=lot_id,
                unit_cost=unit_cost,
                status=status,
                reference_type=REF_ADJUSTMENT,
                user_id=user_id,
                notes=reason,
            )
        ]
    elif lot_id:
        movements = [
            post_movement(
                db,
                warehouse_id=warehouse.id,
                product_id=product.id,
                movement_type=movement_type,
                base_quantity=change,
                lot_id=lot_id,
                unit_cost=unit_cost,
                status=status,
                reference_type=REF_ADJUSTMENT,
                user_id=user_id,
                notes=reason,
            )
        ]
    else:
        movements = issue_stock(
            db,
            warehouse_id=warehouse.id,
            product_id=product.id,
            base_quantity=-change,
            movement_type=movement_type,
            allow_expired=True,
            status=status,
            reference_type=REF_ADJUSTMENT,
            user_id=user_id,
            notes=reason,
        )

    value = money(sum((D(m.total_cost) for m in movements), ZERO))
    audit_service.record(
        db,
        AuditAction.STOCK_ADJUSTMENT,
        entity_type="StockMovement",
        entity_id=movements[0].id if movements else None,
        entity_label=f"{warehouse.code}/{product.sku}",
        amount=value,
        summary=f"{movement_type} {change} {product.base_uom} @ {warehouse.code}",
        new_values={
            "warehouse_id": warehouse.id,
            "product_id": product.id,
            "lot_id": lot_id,
            "base_quantity": str(change),
            "reason": reason,
        },
        **(audit or {}),
    )
    if commit:
        db.commit()
    return movements


# ===========================================================================
# Lots
# ===========================================================================
def create_lot(
    db: Session,
    *,
    product_id: int,
    lot_number: str,
    expiry_date: date | None = None,
    production_date: date | None = None,
    unit_cost: Decimal = Decimal("0"),
    supplier_name: str | None = None,
    batch_number: str | None = None,
    serial_number: str | None = None,
    received_date: date | None = None,
    notes: str | None = None,
    user_id: int | None = None,
) -> Lot:
    """
    Register a production lot.

    When the product declares a ``shelf_life_days`` and no expiry was supplied,
    it is derived from the production date — field staff routinely scan only
    the batch code.
    """
    product = _product(db, product_id)
    number = (lot_number or "").strip()
    if not number:
        raise ValidationError("stock.lot_number_required", params={"sku": product.sku})

    existing = db.execute(
        select(Lot).where(Lot.product_id == product.id, Lot.lot_number == number)
    ).scalar_one_or_none()
    if existing is not None:
        raise ValidationError(
            "stock.lot_number_taken", params={"lot": number, "sku": product.sku}
        )

    if expiry_date is None and production_date and product.shelf_life_days:
        expiry_date = production_date + timedelta(days=int(product.shelf_life_days))

    lot = Lot(
        product_id=product.id,
        lot_number=number,
        batch_number=batch_number,
        serial_number=serial_number,
        production_date=production_date,
        expiry_date=expiry_date,
        received_date=received_date or date.today(),
        supplier_name=supplier_name,
        unit_cost=money(unit_cost) if D(unit_cost) > 0 else money(product.cost_price),
        notes=notes,
        created_by_id=user_id,
    )
    db.add(lot)
    db.flush()
    return lot


def get_or_create_lot(
    db: Session,
    *,
    product_id: int,
    lot_number: str,
    expiry_date: date | None = None,
    production_date: date | None = None,
    unit_cost: Decimal = Decimal("0"),
    supplier_name: str | None = None,
    user_id: int | None = None,
) -> Lot:
    """Idempotent lot lookup — goods receipts replay the same batch codes daily."""
    number = (lot_number or "").strip()
    if not number:
        raise ValidationError("stock.lot_number_required", params={"id": product_id})

    lot = db.execute(
        select(Lot).where(Lot.product_id == product_id, Lot.lot_number == number)
    ).scalar_one_or_none()
    if lot is not None:
        # A later receipt may carry information the first one lacked.
        if expiry_date and not lot.expiry_date:
            lot.expiry_date = expiry_date
        if production_date and not lot.production_date:
            lot.production_date = production_date
        if D(unit_cost) > 0:
            lot.unit_cost = money(unit_cost)
        db.flush()
        return lot

    return create_lot(
        db,
        product_id=product_id,
        lot_number=number,
        expiry_date=expiry_date,
        production_date=production_date,
        unit_cost=unit_cost,
        supplier_name=supplier_name,
        user_id=user_id,
    )


def block_lot(
    db: Session,
    lot_id: int,
    *,
    blocked: bool = True,
    reason: str | None = None,
    user_id: int | None = None,
    audit: dict[str, Any] | None = None,
    commit: bool = True,
) -> Lot:
    """Quarantine (or release) a lot — recall handling depends on this being audited."""
    lot = _lot(db, lot_id)
    before = {"is_blocked": lot.is_blocked, "block_reason": lot.block_reason}
    lot.is_blocked = bool(blocked)
    lot.block_reason = reason if blocked else None
    lot.updated_by_id = user_id
    db.flush()

    audit_service.record(
        db,
        AuditAction.UPDATE,
        entity_type="Lot",
        entity_id=lot.id,
        entity_label=lot.lot_number,
        summary=f"lot {'blocked' if blocked else 'released'}",
        old_values=before,
        new_values={"is_blocked": lot.is_blocked, "block_reason": lot.block_reason},
        **(audit or {}),
    )
    if commit:
        db.commit()
    return lot


# ===========================================================================
# Transfers
# ===========================================================================
def create_transfer(
    db: Session,
    *,
    source_warehouse_id: int,
    target_warehouse_id: int,
    items: list[dict[str, Any]],
    transfer_date: date | None = None,
    vehicle_id: int | None = None,
    driver_id: int | None = None,
    notes: str | None = None,
    user_id: int | None = None,
) -> StockTransfer:
    """Create a DRAFT transfer document.  No stock moves until it is shipped."""
    source = _warehouse(db, source_warehouse_id)
    target = _warehouse(db, target_warehouse_id)
    if source.id == target.id:
        raise ValidationError("stock.transfer_same_warehouse", params={"code": source.code})
    if not items:
        raise ValidationError("stock.transfer_empty")

    transfer = StockTransfer(
        document_no=numbering_service.next_number(db, "TRANSFER", on=transfer_date),
        source_warehouse_id=source.id,
        target_warehouse_id=target.id,
        status=TransferStatus.DRAFT,
        transfer_date=transfer_date or date.today(),
        vehicle_id=vehicle_id,
        driver_id=driver_id,
        notes=notes,
        created_by_id=user_id,
    )
    db.add(transfer)
    db.flush()
    _replace_transfer_items(db, transfer, items)
    return transfer


def _replace_transfer_items(
    db: Session, transfer: StockTransfer, items: list[dict[str, Any]]
) -> None:
    for existing in list(transfer.items):
        db.delete(existing)
    db.flush()

    for raw in items:
        product = _product(db, int(raw["product_id"]))
        uom = raw.get("uom") or product.sales_uom
        quantity = qty(raw.get("quantity", 0))
        if quantity <= 0:
            raise ValidationError(
                "stock.quantity_positive", params={"sku": product.sku}
            )
        uom_factor(product, uom)  # validates the unit before the document is stored
        lot_id = raw.get("lot_id")
        unit_cost = raw.get("unit_cost")
        transfer.items.append(
            StockTransferItem(
                product_id=product.id,
                lot_id=int(lot_id) if lot_id else None,
                quantity=quantity,
                received_quantity=ZERO,
                uom=str(uom),
                unit_cost=money(unit_cost) if unit_cost is not None else money(product.cost_price),
            )
        )
    db.flush()


def update_transfer(
    db: Session,
    transfer: StockTransfer,
    *,
    items: list[dict[str, Any]] | None = None,
    transfer_date: date | None = None,
    vehicle_id: int | None = None,
    driver_id: int | None = None,
    notes: str | None = None,
    user_id: int | None = None,
) -> StockTransfer:
    """Edit a transfer.  Only DRAFT documents may change — shipped stock is history."""
    if transfer.status != TransferStatus.DRAFT:
        raise BusinessRuleError(
            "stock.transfer_not_editable", params={"document_no": transfer.document_no}
        )
    if transfer_date is not None:
        transfer.transfer_date = transfer_date
    if vehicle_id is not None:
        transfer.vehicle_id = vehicle_id
    if driver_id is not None:
        transfer.driver_id = driver_id
    if notes is not None:
        transfer.notes = notes
    transfer.updated_by_id = user_id
    if items is not None:
        if not items:
            raise ValidationError("stock.transfer_empty")
        _replace_transfer_items(db, transfer, items)
    db.flush()
    return transfer


def transfer_out(
    db: Session,
    transfer: StockTransfer,
    *,
    user_id: int | None = None,
    audit: dict[str, Any] | None = None,
    commit: bool = True,
) -> list[StockMovement]:
    """
    Ship a DRAFT transfer: issue every line from the source and mark IN_TRANSIT.

    The goods are deliberately *not* added to the target yet — until someone
    receives them they are in transit, and that gap is exactly what a transit
    loss looks like on the ledger.
    """
    if transfer.status != TransferStatus.DRAFT:
        raise BusinessRuleError(
            "stock.transfer_not_shippable", params={"document_no": transfer.document_no}
        )
    if not transfer.items:
        raise ValidationError("stock.transfer_empty")

    source = _warehouse(db, transfer.source_warehouse_id)
    movements: list[StockMovement] = []
    for item in transfer.items:
        product = _product(db, item.product_id)
        base = to_base(product, item.quantity, item.uom)
        if item.lot_id:
            movements.append(
                post_movement(
                    db,
                    warehouse_id=source.id,
                    product_id=product.id,
                    movement_type=StockMovementType.TRANSFER_OUT,
                    base_quantity=-base,
                    lot_id=item.lot_id,
                    unit_cost=item.unit_cost,
                    reference_type=REF_TRANSFER,
                    reference_id=transfer.id,
                    reference_no=transfer.document_no,
                    counterparty_warehouse_id=transfer.target_warehouse_id,
                    user_id=user_id,
                )
            )
        else:
            movements.extend(
                issue_stock(
                    db,
                    warehouse_id=source.id,
                    product_id=product.id,
                    base_quantity=base,
                    movement_type=StockMovementType.TRANSFER_OUT,
                    reference_type=REF_TRANSFER,
                    reference_id=transfer.id,
                    reference_no=transfer.document_no,
                    counterparty_warehouse_id=transfer.target_warehouse_id,
                    user_id=user_id,
                )
            )

    transfer.status = TransferStatus.IN_TRANSIT
    transfer.shipped_at = utcnow()
    transfer.updated_by_id = user_id
    db.flush()

    audit_service.record(
        db,
        AuditAction.UPDATE,
        entity_type="StockTransfer",
        entity_id=transfer.id,
        entity_label=transfer.document_no,
        amount=money(sum((D(m.total_cost) for m in movements), ZERO)),
        summary=f"transfer shipped {transfer.document_no}",
        new_values={"status": TransferStatus.IN_TRANSIT, "lines": len(transfer.items)},
        **(audit or {}),
    )
    if commit:
        db.commit()
    return movements


def transfer_in(
    db: Session,
    transfer: StockTransfer,
    *,
    received: dict[int, Decimal] | None = None,
    user_id: int | None = None,
    audit: dict[str, Any] | None = None,
    commit: bool = True,
) -> list[StockMovement]:
    """
    Receive an IN_TRANSIT transfer into the target warehouse.

    Lots and costs are mirrored from the shipped movements rather than re-picked,
    so the same batch that left the depot is the batch that arrives.  *received*
    maps transfer-item id to the quantity actually accepted (in the item's UoM);
    anything missing is a transit shortage and is recorded on the audit trail.
    """
    if transfer.status != TransferStatus.IN_TRANSIT:
        raise BusinessRuleError(
            "stock.transfer_not_receivable", params={"document_no": transfer.document_no}
        )

    target = _warehouse(db, transfer.target_warehouse_id)

    shipped = db.execute(
        select(StockMovement).where(
            StockMovement.reference_type == REF_TRANSFER,
            StockMovement.reference_id == transfer.id,
            StockMovement.movement_type == StockMovementType.TRANSFER_OUT,
        ).order_by(StockMovement.id.asc())
    ).scalars().all()

    by_product: dict[int, list[list[Any]]] = {}
    for mv in shipped:
        by_product.setdefault(mv.product_id, []).append(
            [mv.lot_id, qty(abs(D(mv.quantity))), money(mv.unit_cost)]
        )

    movements: list[StockMovement] = []
    shortages: list[dict[str, Any]] = []

    for item in transfer.items:
        product = _product(db, item.product_id)
        wanted = item.quantity if received is None else qty(received.get(item.id, ZERO))
        wanted = qty(wanted)
        if wanted < 0:
            raise ValidationError("stock.quantity_positive", params={"sku": product.sku})
        item.received_quantity = wanted
        if wanted < item.quantity:
            shortages.append(
                {
                    "product_id": product.id,
                    "sku": product.sku,
                    "shipped": str(item.quantity),
                    "received": str(wanted),
                    "uom": item.uom,
                }
            )
        if wanted == 0:
            continue

        remaining = to_base(product, wanted, item.uom)
        buckets = by_product.get(product.id) or [[item.lot_id, remaining, money(item.unit_cost)]]
        for bucket in buckets:
            if remaining <= 0:
                break
            lot_id, available, cost = bucket
            take = available if available < remaining else remaining
            if take <= 0:
                continue
            movements.append(
                receive_stock(
                    db,
                    warehouse_id=target.id,
                    product_id=product.id,
                    base_quantity=take,
                    movement_type=StockMovementType.TRANSFER_IN,
                    lot_id=lot_id,
                    unit_cost=cost,
                    reference_type=REF_TRANSFER,
                    reference_id=transfer.id,
                    reference_no=transfer.document_no,
                    counterparty_warehouse_id=transfer.source_warehouse_id,
                    user_id=user_id,
                )
            )
            bucket[1] = qty(available - take)
            remaining = qty(remaining - take)

        if remaining > 0:
            # More received than shipped (recount at the gate): book the excess
            # against the line's own lot so nothing is silently lost.
            movements.append(
                receive_stock(
                    db,
                    warehouse_id=target.id,
                    product_id=product.id,
                    base_quantity=remaining,
                    movement_type=StockMovementType.TRANSFER_IN,
                    lot_id=item.lot_id,
                    unit_cost=item.unit_cost,
                    reference_type=REF_TRANSFER,
                    reference_id=transfer.id,
                    reference_no=transfer.document_no,
                    counterparty_warehouse_id=transfer.source_warehouse_id,
                    user_id=user_id,
                )
            )

    transfer.status = TransferStatus.RECEIVED
    transfer.received_at = utcnow()
    transfer.received_by_id = user_id
    transfer.updated_by_id = user_id
    db.flush()

    audit_service.record(
        db,
        AuditAction.UPDATE,
        entity_type="StockTransfer",
        entity_id=transfer.id,
        entity_label=transfer.document_no,
        amount=money(sum((D(m.total_cost) for m in movements), ZERO)),
        summary=f"transfer received {transfer.document_no}",
        new_values={"status": TransferStatus.RECEIVED, "shortages": shortages},
        **(audit or {}),
    )
    if commit:
        db.commit()
    return movements


def post_transfer(
    db: Session,
    transfer: StockTransfer,
    *,
    user_id: int | None = None,
    audit: dict[str, Any] | None = None,
    commit: bool = True,
) -> StockTransfer:
    """Ship and receive in one step — used when both ends are handled together."""
    transfer_out(db, transfer, user_id=user_id, audit=audit, commit=False)
    transfer_in(db, transfer, user_id=user_id, audit=audit, commit=False)
    if commit:
        db.commit()
    return transfer


def cancel_transfer(
    db: Session,
    transfer: StockTransfer,
    *,
    user_id: int | None = None,
    audit: dict[str, Any] | None = None,
    commit: bool = True,
) -> StockTransfer:
    """Cancel a DRAFT transfer.  Shipped goods must be returned by a new transfer."""
    if transfer.status != TransferStatus.DRAFT:
        raise BusinessRuleError(
            "stock.transfer_not_cancellable", params={"document_no": transfer.document_no}
        )
    transfer.status = TransferStatus.CANCELLED
    transfer.updated_by_id = user_id
    db.flush()
    audit_service.record(
        db,
        AuditAction.CANCEL,
        entity_type="StockTransfer",
        entity_id=transfer.id,
        entity_label=transfer.document_no,
        summary="transfer cancelled",
        **(audit or {}),
    )
    if commit:
        db.commit()
    return transfer


# ===========================================================================
# Physical counts
# ===========================================================================
def create_count(
    db: Session,
    *,
    warehouse_id: int,
    count_date: date | None = None,
    counted_by_id: int | None = None,
    day_session_id: int | None = None,
    is_van_end_of_day: bool = False,
    product_ids: list[int] | None = None,
    prefill: bool = True,
    notes: str | None = None,
    user_id: int | None = None,
) -> StockCount:
    """
    Open a count sheet.

    By default it is pre-filled from the current balances so the counter sees
    every line that *should* be there — a blank sheet hides missing stock.
    """
    warehouse = _warehouse(db, warehouse_id)
    count = StockCount(
        document_no=numbering_service.next_number(db, "COUNT", on=count_date),
        warehouse_id=warehouse.id,
        status=CountStatus.DRAFT,
        count_date=count_date or date.today(),
        counted_by_id=counted_by_id,
        day_session_id=day_session_id,
        is_van_end_of_day=is_van_end_of_day,
        notes=notes,
        created_by_id=user_id,
    )
    db.add(count)
    db.flush()

    if prefill:
        for balance in get_balances(db, warehouse.id, product_ids=product_ids):
            if balance.status != StockStatus.AVAILABLE:
                continue
            lot = db.get(Lot, balance.lot_id) if balance.lot_id else None
            unit_cost = _resolve_cost(balance, lot, balance.product)
            count.items.append(
                StockCountItem(
                    product_id=balance.product_id,
                    lot_id=_lot_key(balance.lot_id) or None,
                    system_quantity=qty(balance.quantity),
                    counted_quantity=ZERO,
                    variance_quantity=qty(-D(balance.quantity)),
                    unit_cost=unit_cost,
                    variance_value=money(-D(balance.quantity) * unit_cost),
                )
            )
        db.flush()
    return count


def set_counted_quantities(
    db: Session,
    count: StockCount,
    lines: list[dict[str, Any]],
    *,
    counted_by_id: int | None = None,
    user_id: int | None = None,
) -> StockCount:
    """
    Record what was physically found.

    Each line is ``{product_id, lot_id?, counted_quantity, reason?}`` in base
    units.  Lines for products not on the sheet are added — the van often
    carries stock the system does not know about.
    """
    if count.status in (CountStatus.APPROVED, CountStatus.CANCELLED):
        raise BusinessRuleError(
            "stock.count_not_editable", params={"document_no": count.document_no}
        )

    index: dict[tuple[int, int], StockCountItem] = {
        (item.product_id, _lot_key(item.lot_id)): item for item in count.items
    }

    for raw in lines:
        product = _product(db, int(raw["product_id"]))
        lot_id = raw.get("lot_id")
        key = (product.id, _lot_key(lot_id))
        counted = qty(raw.get("counted_quantity", 0))
        if counted < 0:
            raise ValidationError("stock.quantity_positive", params={"sku": product.sku})

        item = index.get(key)
        if item is None:
            item = StockCountItem(
                product_id=product.id,
                lot_id=_lot_key(lot_id) or None,
                system_quantity=ZERO,
                unit_cost=money(product.cost_price),
            )
            count.items.append(item)
            index[key] = item

        item.counted_quantity = counted
        if raw.get("reason") is not None:
            item.reason = str(raw["reason"])[:255]

    _recompute_count(db, count)
    count.status = CountStatus.COUNTED
    if counted_by_id is not None:
        count.counted_by_id = counted_by_id
    count.updated_by_id = user_id
    db.flush()
    return count


def _recompute_count(db: Session, count: StockCount) -> None:
    """Refresh system quantities, variances and document totals from live balances."""
    total_qty = ZERO
    total_value = ZERO
    for item in count.items:
        balance = db.execute(
            select(StockBalance).where(
                StockBalance.warehouse_id == count.warehouse_id,
                StockBalance.product_id == item.product_id,
                StockBalance.lot_id == _lot_key(item.lot_id),
                StockBalance.status == StockStatus.AVAILABLE,
            )
        ).scalar_one_or_none()
        lot = db.get(Lot, item.lot_id) if item.lot_id else None
        product = _product(db, item.product_id)

        item.system_quantity = qty(balance.quantity) if balance is not None else ZERO
        item.unit_cost = _resolve_cost(balance, lot, product)
        item.variance_quantity = qty(D(item.counted_quantity) - D(item.system_quantity))
        item.variance_value = money(D(item.variance_quantity) * D(item.unit_cost))
        total_qty += D(item.variance_quantity)
        total_value += D(item.variance_value)

    count.total_variance_qty = qty(total_qty)
    count.total_variance_value = money(total_value)
    db.flush()


def approve_count(
    db: Session,
    count: StockCount,
    *,
    user_id: int | None = None,
    audit: dict[str, Any] | None = None,
    commit: bool = True,
) -> StockCount:
    """
    Approve a count: post a COUNT_ADJUSTMENT movement for every variance so the
    ledger and the shelf agree again, then lock the document.

    Only the lines on the sheet are adjusted — a product absent from the sheet
    was not counted, which is not the same as counted zero.
    """
    if count.status == CountStatus.APPROVED:
        raise BusinessRuleError(
            "stock.count_already_approved", params={"document_no": count.document_no}
        )
    if count.status == CountStatus.CANCELLED:
        raise BusinessRuleError(
            "stock.count_not_editable", params={"document_no": count.document_no}
        )

    _recompute_count(db, count)

    posted = 0
    for item in count.items:
        variance = qty(item.variance_quantity)
        if variance == 0:
            continue
        post_movement(
            db,
            warehouse_id=count.warehouse_id,
            product_id=item.product_id,
            movement_type=StockMovementType.COUNT_ADJUSTMENT,
            base_quantity=variance,
            lot_id=_lot_key(item.lot_id) or None,
            unit_cost=item.unit_cost,
            reference_type=REF_COUNT,
            reference_id=count.id,
            reference_no=count.document_no,
            day_session_id=count.day_session_id,
            user_id=user_id,
            notes=item.reason,
        )
        posted += 1

    count.status = CountStatus.APPROVED
    count.approved_by_id = user_id
    count.approved_at = utcnow()
    count.updated_by_id = user_id
    db.flush()

    audit_service.record(
        db,
        AuditAction.STOCK_VARIANCE if posted else AuditAction.UPDATE,
        entity_type="StockCount",
        entity_id=count.id,
        entity_label=count.document_no,
        amount=money(count.total_variance_value),
        summary=f"count approved {count.document_no} variances={posted}",
        new_values={
            "status": CountStatus.APPROVED,
            "total_variance_qty": str(count.total_variance_qty),
            "total_variance_value": str(count.total_variance_value),
            "adjusted_lines": posted,
        },
        **(audit or {}),
    )
    if commit:
        db.commit()
    return count


# ===========================================================================
# Reconciliation support (day-end)
# ===========================================================================
def theoretical_stock(db: Session, warehouse_id: int) -> dict[int, Decimal]:
    """
    What the ledger says should be on the shelf, per product, in base units.

    Day-end reconciliation compares this against the physical van count:
    ``theoretical - counted = variance``.
    """
    rows = db.execute(
        select(StockBalance.product_id, StockBalance.quantity).where(
            StockBalance.warehouse_id == warehouse_id
        )
    ).all()
    out: dict[int, Decimal] = {}
    for product_id, quantity in rows:
        out[product_id] = qty(out.get(product_id, ZERO) + D(quantity))
    return {pid: value for pid, value in out.items() if value != 0}


def reconcile_variance(
    db: Session,
    warehouse_id: int,
    counted: dict[int, Decimal],
) -> dict[str, Any]:
    """
    Compare a physical count against the theoretical stock without posting anything.

    Returns the per-product differences plus totals, so the day-close screen can
    show the variance before anyone commits to it.
    """
    theoretical = theoretical_stock(db, warehouse_id)
    product_ids = sorted(set(theoretical) | {int(k) for k in counted})

    lines: list[dict[str, Any]] = []
    total_qty = ZERO
    total_value = ZERO
    for product_id in product_ids:
        product = db.get(Product, product_id)
        if product is None:
            continue
        system = qty(theoretical.get(product_id, ZERO))
        found = qty(counted.get(product_id, ZERO))
        variance = qty(found - system)
        unit_cost = _average_cost(db, warehouse_id, product_id) or money(product.cost_price)
        value = money(variance * unit_cost)
        total_qty += variance
        total_value += value
        lines.append(
            {
                "product_id": product_id,
                "sku": product.sku,
                "name": product.name,
                "uom": product.base_uom,
                "system_quantity": system,
                "counted_quantity": found,
                "variance_quantity": variance,
                "unit_cost": unit_cost,
                "variance_value": value,
            }
        )

    return {
        "warehouse_id": warehouse_id,
        "lines": lines,
        "total_variance_qty": qty(total_qty),
        "total_variance_value": money(total_value),
        "has_variance": any(line["variance_quantity"] != 0 for line in lines),
    }


def _average_cost(db: Session, warehouse_id: int, product_id: int) -> Decimal:
    """Quantity-weighted average cost of a product in a warehouse."""
    rows = db.execute(
        select(StockBalance.quantity, StockBalance.average_cost).where(
            StockBalance.warehouse_id == warehouse_id,
            StockBalance.product_id == product_id,
        )
    ).all()
    total_qty = ZERO
    total_value = ZERO
    for quantity, cost in rows:
        total_qty += D(quantity)
        total_value += D(quantity) * D(cost)
    if total_qty <= 0:
        return ZERO
    return money(total_value / total_qty)


# ===========================================================================
# Vehicles
# ===========================================================================
def vehicle_warehouse_id(db: Session, vehicle_id: int) -> int:
    """The VEHICLE-type warehouse backing a van; every van stock rule needs it."""
    vehicle = db.get(Vehicle, vehicle_id)
    if vehicle is None or vehicle.is_deleted:
        raise NotFoundError("vehicle.not_found", params={"id": vehicle_id})
    if not vehicle.warehouse_id:
        raise NotFoundError(
            "vehicle.no_warehouse", params={"vehicle": vehicle.plate_number}
        )
    return int(vehicle.warehouse_id)


def vehicle_stock(db: Session, vehicle_id: int) -> list[dict[str, Any]]:
    """On-board stock of a van, aggregated per product with its lot breakdown."""
    warehouse_id = vehicle_warehouse_id(db, vehicle_id)
    rows = get_balances(db, warehouse_id)

    grouped: dict[int, dict[str, Any]] = {}
    for balance in rows:
        product = balance.product
        entry = grouped.get(product.id)
        if entry is None:
            entry = {
                "product_id": product.id,
                "sku": product.sku,
                "name": product.name,
                "uom": product.base_uom,
                "base_qty": ZERO,
                "case_qty": ZERO,
                "lots": [],
                "value": ZERO,
            }
            grouped[product.id] = entry

        quantity = qty(balance.quantity)
        entry["base_qty"] = qty(D(entry["base_qty"]) + quantity)
        entry["value"] = money(D(entry["value"]) + quantity * D(balance.average_cost))

        lot = db.get(Lot, balance.lot_id) if balance.lot_id else None
        entry["lots"].append(
            {
                "lot_id": lot.id if lot else None,
                "lot_number": lot.lot_number if lot else None,
                "expiry_date": lot.expiry_date if lot else None,
                "qty": quantity,
            }
        )

    out: list[dict[str, Any]] = []
    for entry in grouped.values():
        product = db.get(Product, entry["product_id"])
        per_case = D(product.units_per_case) if product else Decimal("1")
        entry["case_qty"] = qty(D(entry["base_qty"]) / per_case) if per_case > 0 else ZERO
        out.append(entry)

    out.sort(key=lambda e: str(e["name"]))
    return out


# ===========================================================================
# Valuation & alerts
# ===========================================================================
def warehouse_valuation(db: Session, warehouse_id: int) -> Decimal:
    """Total value of everything held in a warehouse, at moving-average cost."""
    rows = db.execute(
        select(StockBalance.quantity, StockBalance.average_cost).where(
            StockBalance.warehouse_id == warehouse_id
        )
    ).all()
    total = ZERO
    for quantity, cost in rows:
        total += D(quantity) * D(cost)
    return money(total)


def _expiry_rows(
    db: Session,
    *,
    warehouse_id: int | None,
    earliest: date | None,
    latest: date | None,
) -> list[dict[str, Any]]:
    stmt = (
        select(StockBalance, Lot)
        .join(Lot, Lot.id == StockBalance.lot_id)
        .where(StockBalance.quantity > 0, Lot.expiry_date.is_not(None))
    )
    if warehouse_id:
        stmt = stmt.where(StockBalance.warehouse_id == warehouse_id)
    if earliest is not None:
        stmt = stmt.where(Lot.expiry_date >= earliest)
    if latest is not None:
        stmt = stmt.where(Lot.expiry_date <= latest)
    stmt = stmt.order_by(Lot.expiry_date.asc())

    today = date.today()
    out: list[dict[str, Any]] = []
    for balance, lot in db.execute(stmt).all():
        quantity = qty(balance.quantity)
        out.append(
            {
                "warehouse_id": balance.warehouse_id,
                "warehouse_code": balance.warehouse.code,
                "warehouse_name": balance.warehouse.name,
                "product_id": balance.product_id,
                "sku": balance.product.sku,
                "name": balance.product.name,
                "lot_id": lot.id,
                "lot_number": lot.lot_number,
                "expiry_date": lot.expiry_date,
                "days_to_expiry": lot.days_to_expiry(today),
                "quantity": quantity,
                "uom": balance.product.base_uom,
                "unit_cost": money(balance.average_cost),
                "value": money(quantity * D(balance.average_cost)),
                "is_blocked": lot.is_blocked,
            }
        )
    return out


def expiring_soon(
    db: Session, *, days: int | None = None, warehouse_id: int | None = None
) -> list[dict[str, Any]]:
    """Stock whose expiry falls inside the warning window — sell it or move it."""
    horizon = int(days if days is not None else settings.expiry_warning_days)
    today = date.today()
    return _expiry_rows(
        db,
        warehouse_id=warehouse_id,
        earliest=today,
        latest=today + timedelta(days=max(0, horizon)),
    )


def expired(db: Session, *, warehouse_id: int | None = None) -> list[dict[str, Any]]:
    """Stock already past its expiry date; it must be written off, never sold."""
    return _expiry_rows(
        db,
        warehouse_id=warehouse_id,
        earliest=None,
        latest=date.today() - timedelta(days=1),
    )


def low_stock(
    db: Session, *, warehouse_id: int | None = None, include_vehicles: bool = False
) -> list[dict[str, Any]]:
    """
    Products at or below their replenishment threshold.

    Threshold order: ``reorder_point``, then ``min_stock_level``, then a
    fraction of ``max_stock_level`` — most catalogues fill in only one of them.
    """
    stmt = select(
        StockBalance.product_id, func.sum(StockBalance.quantity)
    ).where(StockBalance.status == StockStatus.AVAILABLE)

    if warehouse_id:
        stmt = stmt.where(StockBalance.warehouse_id == warehouse_id)
    elif not include_vehicles:
        stmt = stmt.join(Warehouse, Warehouse.id == StockBalance.warehouse_id).where(
            Warehouse.warehouse_type != WarehouseType.VEHICLE,
            Warehouse.is_deleted.is_(False),
        )
    stmt = stmt.group_by(StockBalance.product_id)

    on_hand = {pid: qty(value) for pid, value in db.execute(stmt).all()}

    products = db.execute(
        select(Product).where(Product.is_deleted.is_(False), Product.is_active.is_(True))
    ).scalars().all()

    out: list[dict[str, Any]] = []
    for product in products:
        threshold = ZERO
        if product.reorder_point is not None and D(product.reorder_point) > 0:
            threshold = qty(product.reorder_point)
        elif D(product.min_stock_level) > 0:
            threshold = qty(product.min_stock_level)
        elif product.max_stock_level is not None and D(product.max_stock_level) > 0:
            threshold = qty(D(product.max_stock_level) * D(settings.low_stock_ratio))
        if threshold <= 0:
            continue

        available = qty(on_hand.get(product.id, ZERO))
        if available > threshold:
            continue

        per_case = D(product.units_per_case) or Decimal("1")
        out.append(
            {
                "product_id": product.id,
                "sku": product.sku,
                "name": product.name,
                "uom": product.base_uom,
                "warehouse_id": warehouse_id,
                "on_hand": available,
                "case_qty": qty(available / per_case),
                "min_stock_level": qty(product.min_stock_level),
                "reorder_point": qty(product.reorder_point) if product.reorder_point is not None else None,
                "threshold": threshold,
                "shortage": qty(threshold - available),
            }
        )

    out.sort(key=lambda row: (-float(row["shortage"]), str(row["name"])))
    return out


# ===========================================================================
# Stock card
# ===========================================================================
def stock_card(
    db: Session,
    product_id: int,
    warehouse_id: int,
    start: date | None = None,
    end: date | None = None,
) -> dict[str, Any]:
    """
    Movement history of one product in one warehouse with a running balance.

    The opening balance is replayed from every movement before *start*, so a
    period view still reconciles to the ledger.
    """
    product = _product(db, product_id)
    warehouse = _warehouse(db, warehouse_id)

    base = select(StockMovement).where(
        StockMovement.product_id == product.id,
        StockMovement.warehouse_id == warehouse.id,
    )

    opening = ZERO
    if start is not None:
        rows = db.execute(
            select(StockMovement.quantity).where(
                StockMovement.product_id == product.id,
                StockMovement.warehouse_id == warehouse.id,
                StockMovement.moved_at < day_start(start),
            )
        ).scalars().all()
        for value in rows:
            opening += D(value)
        base = base.where(StockMovement.moved_at >= day_start(start))
    if end is not None:
        base = base.where(StockMovement.moved_at <= day_end(end))

    movements = db.execute(
        base.order_by(StockMovement.moved_at.asc(), StockMovement.id.asc())
    ).scalars().all()

    running = qty(opening)
    total_in = ZERO
    total_out = ZERO
    rows_out: list[dict[str, Any]] = []
    for mv in movements:
        change = qty(mv.quantity)
        running = qty(running + change)
        if change > 0:
            total_in += change
        else:
            total_out += -change
        rows_out.append(
            {
                "movement_id": mv.id,
                "moved_at": mv.moved_at,
                "movement_type": mv.movement_type,
                "status": mv.status,
                "lot_id": mv.lot_id,
                "lot_number": mv.lot.lot_number if mv.lot else None,
                "expiry_date": mv.lot.expiry_date if mv.lot else None,
                "quantity_in": change if change > 0 else ZERO,
                "quantity_out": -change if change < 0 else ZERO,
                "quantity": change,
                "unit_cost": money(mv.unit_cost),
                "total_cost": money(mv.total_cost),
                "balance": running,
                "reference_type": mv.reference_type,
                "reference_no": mv.reference_no,
                "counterparty_warehouse_id": mv.counterparty_warehouse_id,
                "notes": mv.notes,
            }
        )

    return {
        "product_id": product.id,
        "sku": product.sku,
        "product_name": product.name,
        "uom": product.base_uom,
        "warehouse_id": warehouse.id,
        "warehouse_code": warehouse.code,
        "opening_balance": qty(opening),
        "closing_balance": running,
        "total_in": qty(total_in),
        "total_out": qty(total_out),
        "rows": rows_out,
    }


def movements(
    db: Session,
    *,
    warehouse_id: int | None = None,
    product_id: int | None = None,
    movement_type: str | None = None,
    reference_type: str | None = None,
    reference_id: int | None = None,
    start: date | None = None,
    end: date | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[StockMovement]:
    """Filtered slice of the ledger — the generic reader other modules build on."""
    stmt = select(StockMovement)
    if warehouse_id:
        stmt = stmt.where(StockMovement.warehouse_id == warehouse_id)
    if product_id:
        stmt = stmt.where(StockMovement.product_id == product_id)
    if movement_type:
        stmt = stmt.where(StockMovement.movement_type == movement_type)
    if reference_type:
        stmt = stmt.where(StockMovement.reference_type == reference_type)
    if reference_id:
        stmt = stmt.where(StockMovement.reference_id == reference_id)
    if start is not None:
        stmt = stmt.where(StockMovement.moved_at >= day_start(start))
    if end is not None:
        stmt = stmt.where(StockMovement.moved_at <= day_end(end))
    stmt = stmt.order_by(StockMovement.moved_at.desc(), StockMovement.id.desc())
    stmt = stmt.limit(limit).offset(offset)
    return list(db.execute(stmt).scalars().all())

