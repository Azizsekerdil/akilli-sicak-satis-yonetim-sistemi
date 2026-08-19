"""
Pydantic contracts for warehouses, lots, balances, transfers and counts.

Quantities crossing this boundary are in the product's **base unit** unless the
payload carries its own ``uom`` field (transfer lines do), because the ledger
only ever speaks base units.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from app.core.enums import (
    AllocationStrategy,
    CountStatus,
    StockMovementType,
    StockStatus,
    TransferStatus,
    WarehouseType,
)
from app.schemas.common import ORMModel


# ===========================================================================
# Warehouses
# ===========================================================================
class WarehouseCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    name_en: str | None = Field(default=None, max_length=255)
    description: str | None = None
    warehouse_type: WarehouseType = WarehouseType.CENTRAL

    company_id: int | None = None
    region_id: int | None = None
    branch_id: int | None = None
    parent_id: int | None = None
    manager_id: int | None = None

    address: str | None = None
    city: str | None = Field(default=None, max_length=96)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)

    capacity_volume_l: float | None = Field(default=None, ge=0)
    capacity_weight_kg: float | None = Field(default=None, ge=0)
    allows_negative_stock: bool = False
    allocation_strategy: AllocationStrategy = AllocationStrategy.FEFO
    is_active: bool = True


class WarehouseUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    name_en: str | None = Field(default=None, max_length=255)
    description: str | None = None
    warehouse_type: WarehouseType | None = None

    company_id: int | None = None
    region_id: int | None = None
    branch_id: int | None = None
    parent_id: int | None = None
    manager_id: int | None = None

    address: str | None = None
    city: str | None = Field(default=None, max_length=96)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)

    capacity_volume_l: float | None = Field(default=None, ge=0)
    capacity_weight_kg: float | None = Field(default=None, ge=0)
    allows_negative_stock: bool | None = None
    allocation_strategy: AllocationStrategy | None = None
    is_active: bool | None = None


class WarehouseOut(ORMModel):
    id: int
    code: str
    name: str
    name_en: str | None = None
    description: str | None = None
    warehouse_type: str
    is_active: bool

    company_id: int | None = None
    region_id: int | None = None
    branch_id: int | None = None
    parent_id: int | None = None
    manager_id: int | None = None

    address: str | None = None
    city: str | None = None
    latitude: float | None = None
    longitude: float | None = None

    capacity_volume_l: float | None = None
    capacity_weight_kg: float | None = None
    allows_negative_stock: bool = False
    allocation_strategy: str = AllocationStrategy.FEFO

    created_at: datetime | None = None
    updated_at: datetime | None = None

    #: Filled in by the detail endpoint only — a list view must not run one
    #: valuation query per row.
    stock_value: Decimal | None = None
    product_count: int | None = None


class ValuationOut(BaseModel):
    warehouse_id: int
    warehouse_code: str
    warehouse_name: str
    currency: str = "TRY"
    total_value: Decimal = Decimal("0")
    total_quantity: Decimal = Decimal("0")
    product_count: int = 0
    lot_count: int = 0


# ===========================================================================
# Lots
# ===========================================================================
class LotCreate(BaseModel):
    product_id: int
    lot_number: str = Field(min_length=1, max_length=64)
    batch_number: str | None = Field(default=None, max_length=64)
    serial_number: str | None = Field(default=None, max_length=96)
    production_date: date | None = None
    expiry_date: date | None = None
    received_date: date | None = None
    supplier_name: str | None = Field(default=None, max_length=255)
    unit_cost: Decimal = Decimal("0")
    notes: str | None = None


class LotUpdate(BaseModel):
    batch_number: str | None = Field(default=None, max_length=64)
    serial_number: str | None = Field(default=None, max_length=96)
    production_date: date | None = None
    expiry_date: date | None = None
    received_date: date | None = None
    supplier_name: str | None = Field(default=None, max_length=255)
    unit_cost: Decimal | None = None
    notes: str | None = None


class LotBlockIn(BaseModel):
    blocked: bool = True
    reason: str | None = Field(default=None, max_length=255)


class LotOut(ORMModel):
    id: int
    product_id: int
    lot_number: str
    batch_number: str | None = None
    serial_number: str | None = None
    production_date: date | None = None
    expiry_date: date | None = None
    received_date: date | None = None
    supplier_name: str | None = None
    unit_cost: Decimal = Decimal("0")
    is_blocked: bool = False
    block_reason: str | None = None
    notes: str | None = None

    product_sku: str | None = None
    product_name: str | None = None
    days_to_expiry: int | None = None
    on_hand: Decimal | None = None


# ===========================================================================
# Balances & movements
# ===========================================================================
class StockBalanceOut(ORMModel):
    id: int
    warehouse_id: int
    product_id: int
    lot_id: int = 0
    status: str = StockStatus.AVAILABLE

    quantity: Decimal = Decimal("0")
    reserved_quantity: Decimal = Decimal("0")
    available: Decimal = Decimal("0")
    average_cost: Decimal = Decimal("0")
    value: Decimal = Decimal("0")
    last_movement_at: datetime | None = None

    product_sku: str | None = None
    product_name: str | None = None
    uom: str | None = None
    case_qty: Decimal | None = None
    lot_number: str | None = None
    expiry_date: date | None = None
    days_to_expiry: int | None = None
    is_blocked: bool = False


class StockMovementOut(ORMModel):
    id: int
    warehouse_id: int
    product_id: int
    lot_id: int | None = None
    movement_type: str
    status: str = StockStatus.AVAILABLE
    quantity: Decimal = Decimal("0")
    unit_cost: Decimal = Decimal("0")
    total_cost: Decimal = Decimal("0")
    balance_after: Decimal = Decimal("0")
    counterparty_warehouse_id: int | None = None
    reference_type: str | None = None
    reference_id: int | None = None
    reference_no: str | None = None
    salesperson_id: int | None = None
    customer_id: int | None = None
    day_session_id: int | None = None
    moved_at: datetime | None = None
    created_by_id: int | None = None
    notes: str | None = None


class StockCardRow(BaseModel):
    movement_id: int
    moved_at: datetime | None = None
    movement_type: str
    status: str = StockStatus.AVAILABLE
    lot_id: int | None = None
    lot_number: str | None = None
    expiry_date: date | None = None
    quantity_in: Decimal = Decimal("0")
    quantity_out: Decimal = Decimal("0")
    quantity: Decimal = Decimal("0")
    unit_cost: Decimal = Decimal("0")
    total_cost: Decimal = Decimal("0")
    balance: Decimal = Decimal("0")
    reference_type: str | None = None
    reference_no: str | None = None
    counterparty_warehouse_id: int | None = None
    notes: str | None = None


class StockCardOut(BaseModel):
    product_id: int
    sku: str
    product_name: str
    uom: str
    warehouse_id: int
    warehouse_code: str
    opening_balance: Decimal = Decimal("0")
    closing_balance: Decimal = Decimal("0")
    total_in: Decimal = Decimal("0")
    total_out: Decimal = Decimal("0")
    rows: list[StockCardRow] = Field(default_factory=list)


class AdjustmentIn(BaseModel):
    """
    Manual stock correction.

    *base_quantity* is signed: positive adds, negative removes.  A reason is
    mandatory because this is the only stock change with no source document.
    """

    warehouse_id: int
    product_id: int
    base_quantity: Decimal
    lot_id: int | None = None
    movement_type: StockMovementType = StockMovementType.ADJUSTMENT
    status: StockStatus = StockStatus.AVAILABLE
    unit_cost: Decimal = Decimal("0")
    reason: str = Field(min_length=3, max_length=255)


# ===========================================================================
# Transfers
# ===========================================================================
class TransferItem(ORMModel):
    """One transfer line.  ``quantity`` is expressed in ``uom``, not base units."""

    id: int | None = None
    product_id: int
    lot_id: int | None = None
    quantity: Decimal = Field(gt=0)
    received_quantity: Decimal = Decimal("0")
    uom: str | None = None
    unit_cost: Decimal | None = None

    product_sku: str | None = None
    product_name: str | None = None
    base_quantity: Decimal | None = None


class TransferCreate(BaseModel):
    source_warehouse_id: int
    target_warehouse_id: int
    transfer_date: date | None = None
    vehicle_id: int | None = None
    driver_id: int | None = None
    notes: str | None = None
    items: list[TransferItem] = Field(min_length=1)


class TransferUpdate(BaseModel):
    transfer_date: date | None = None
    vehicle_id: int | None = None
    driver_id: int | None = None
    notes: str | None = None
    items: list[TransferItem] | None = None


class TransferReceiveLine(BaseModel):
    item_id: int
    received_quantity: Decimal = Field(ge=0)


class TransferReceiveIn(BaseModel):
    """Empty ``lines`` means: everything shipped arrived intact."""

    lines: list[TransferReceiveLine] = Field(default_factory=list)
    notes: str | None = None


class TransferOut(ORMModel):
    id: int
    document_no: str
    source_warehouse_id: int
    target_warehouse_id: int
    status: str = TransferStatus.DRAFT
    transfer_date: date
    shipped_at: datetime | None = None
    received_at: datetime | None = None
    received_by_id: int | None = None
    vehicle_id: int | None = None
    driver_id: int | None = None
    notes: str | None = None
    created_at: datetime | None = None

    source_warehouse_name: str | None = None
    target_warehouse_name: str | None = None
    total_quantity: Decimal = Decimal("0")
    total_cost: Decimal = Decimal("0")
    items: list[TransferItem] = Field(default_factory=list)


# ===========================================================================
# Counts
# ===========================================================================
class CountItem(ORMModel):
    """One counted line.  Quantities are in the product's base unit."""

    id: int | None = None
    product_id: int
    lot_id: int | None = None
    system_quantity: Decimal = Decimal("0")
    counted_quantity: Decimal = Field(default=Decimal("0"), ge=0)
    variance_quantity: Decimal = Decimal("0")
    unit_cost: Decimal = Decimal("0")
    variance_value: Decimal = Decimal("0")
    reason: str | None = Field(default=None, max_length=255)

    product_sku: str | None = None
    product_name: str | None = None
    uom: str | None = None
    lot_number: str | None = None
    expiry_date: date | None = None


class CountCreate(BaseModel):
    warehouse_id: int
    count_date: date | None = None
    counted_by_id: int | None = None
    day_session_id: int | None = None
    is_van_end_of_day: bool = False
    #: Limit the sheet to these products; empty means every product on hand.
    product_ids: list[int] = Field(default_factory=list)
    prefill: bool = True
    notes: str | None = None


class CountSubmit(BaseModel):
    lines: list[CountItem] = Field(min_length=1)
    counted_by_id: int | None = None
    notes: str | None = None


class CountOut(ORMModel):
    id: int
    document_no: str
    warehouse_id: int
    status: str = CountStatus.DRAFT
    count_date: date
    counted_by_id: int | None = None
    approved_by_id: int | None = None
    approved_at: datetime | None = None
    day_session_id: int | None = None
    is_van_end_of_day: bool = False
    total_variance_qty: Decimal = Decimal("0")
    total_variance_value: Decimal = Decimal("0")
    notes: str | None = None
    created_at: datetime | None = None

    warehouse_code: str | None = None
    warehouse_name: str | None = None
    items: list[CountItem] = Field(default_factory=list)


class ReconcileLine(BaseModel):
    product_id: int
    sku: str
    name: str
    uom: str
    system_quantity: Decimal = Decimal("0")
    counted_quantity: Decimal = Decimal("0")
    variance_quantity: Decimal = Decimal("0")
    unit_cost: Decimal = Decimal("0")
    variance_value: Decimal = Decimal("0")


class ReconcileOut(BaseModel):
    warehouse_id: int
    lines: list[ReconcileLine] = Field(default_factory=list)
    total_variance_qty: Decimal = Decimal("0")
    total_variance_value: Decimal = Decimal("0")
    has_variance: bool = False


# ===========================================================================
# Reporting rows
# ===========================================================================
class VehicleLotRow(BaseModel):
    lot_id: int | None = None
    lot_number: str | None = None
    expiry_date: date | None = None
    qty: Decimal = Decimal("0")


class VehicleStockRow(BaseModel):
    product_id: int
    sku: str
    name: str
    uom: str
    base_qty: Decimal = Decimal("0")
    case_qty: Decimal = Decimal("0")
    lots: list[VehicleLotRow] = Field(default_factory=list)
    value: Decimal = Decimal("0")


class ExpiryRow(BaseModel):
    warehouse_id: int
    warehouse_code: str | None = None
    warehouse_name: str | None = None
    product_id: int
    sku: str
    name: str
    lot_id: int
    lot_number: str
    expiry_date: date | None = None
    days_to_expiry: int | None = None
    quantity: Decimal = Decimal("0")
    uom: str | None = None
    unit_cost: Decimal = Decimal("0")
    value: Decimal = Decimal("0")
    is_blocked: bool = False


class LowStockRow(BaseModel):
    product_id: int
    sku: str
    name: str
    uom: str
    warehouse_id: int | None = None
    on_hand: Decimal = Decimal("0")
    case_qty: Decimal = Decimal("0")
    min_stock_level: Decimal = Decimal("0")
    reorder_point: Decimal | None = None
    threshold: Decimal = Decimal("0")
    shortage: Decimal = Decimal("0")


__all__: list[str] = [
    "WarehouseCreate",
    "WarehouseUpdate",
    "WarehouseOut",
    "ValuationOut",
    "LotCreate",
    "LotUpdate",
    "LotBlockIn",
    "LotOut",
    "StockBalanceOut",
    "StockMovementOut",
    "StockCardRow",
    "StockCardOut",
    "AdjustmentIn",
    "TransferItem",
    "TransferCreate",
    "TransferUpdate",
    "TransferReceiveLine",
    "TransferReceiveIn",
    "TransferOut",
    "CountItem",
    "CountCreate",
    "CountSubmit",
    "CountOut",
    "ReconcileLine",
    "ReconcileOut",
    "VehicleLotRow",
    "VehicleStockRow",
    "ExpiryRow",
    "LowStockRow",
]
