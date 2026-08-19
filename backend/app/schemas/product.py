"""
Product master-data schemas.

Covers the catalogue (products, categories, brands), packaging (units of
measure, barcodes) and commercial pricing (price lists and their items).
Read models inherit :class:`ORMModel` so they can be built straight off the
SQLAlchemy objects the services return.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, Field

from app.core.enums import ProductStatus, StorageCondition, UnitOfMeasure
from app.schemas.common import BulkResult, ORMModel

_UOMS: frozenset[str] = frozenset(str(u) for u in UnitOfMeasure)


def _normalise_uom(value: Any) -> Any:
    """Accept lower-case input from the UI but store the canonical enum value."""
    if value is None:
        return value
    text = str(value).strip().upper()
    if text not in _UOMS:
        raise ValueError(f"unknown unit of measure: {value}")
    return text


def _tags_to_list(value: Any) -> Any:
    """The column stores a comma-separated string; the API speaks lists."""
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


#: A unit-of-measure code, normalised and checked against the enum.
Uom = Annotated[str, BeforeValidator(_normalise_uom)]
#: Tags arrive from the ORM as ``"a,b,c"`` and leave as ``["a", "b", "c"]``.
TagList = Annotated[list[str], BeforeValidator(_tags_to_list)]


# ===========================================================================
# Units of measure & barcodes
# ===========================================================================
class ProductUnitIn(BaseModel):
    """One packaging level, e.g. CASE = 24 base units."""

    uom: Uom
    factor: Decimal = Field(gt=0, description="How many base units this packaging holds")
    barcode: str | None = Field(default=None, max_length=64)
    price: Decimal | None = Field(default=None, ge=0)
    is_default_sales_unit: bool = False
    volume_l: float | None = Field(default=None, ge=0)
    weight_kg: float | None = Field(default=None, ge=0)


class ProductUnitOut(ORMModel):
    id: int
    product_id: int
    uom: str
    factor: Decimal
    barcode: str | None = None
    price: Decimal | None = None
    is_default_sales_unit: bool = False
    volume_l: float | None = None
    weight_kg: float | None = None


class BarcodeIn(BaseModel):
    barcode: str = Field(min_length=1, max_length=64)
    uom: Uom = str(UnitOfMeasure.PIECE)
    is_primary: bool = False
    label: str | None = Field(default=None, max_length=64)


class BarcodeOut(ORMModel):
    id: int
    product_id: int
    barcode: str
    uom: str
    is_primary: bool = False
    label: str | None = None


class UomOption(BaseModel):
    """One entry of the unit dropdown shown next to a quantity field."""

    uom: str
    factor: Decimal
    is_base: bool = False
    is_sales_default: bool = False
    barcode: str | None = None
    price: Decimal | None = None
    volume_l: float | None = None
    weight_kg: float | None = None


# ===========================================================================
# Categories & brands
# ===========================================================================
class CategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    code: str | None = Field(default=None, max_length=64)
    name_en: str | None = Field(default=None, max_length=255)
    description: str | None = None
    parent_id: int | None = None
    sort_order: int = 0
    icon: str | None = Field(default=None, max_length=64)
    color: str | None = Field(default=None, max_length=16)
    is_active: bool = True


class CategoryUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    code: str | None = Field(default=None, max_length=64)
    name_en: str | None = Field(default=None, max_length=255)
    description: str | None = None
    parent_id: int | None = None
    sort_order: int | None = None
    icon: str | None = Field(default=None, max_length=64)
    color: str | None = Field(default=None, max_length=16)
    is_active: bool | None = None


class CategoryOut(ORMModel):
    id: int
    code: str
    name: str
    name_en: str | None = None
    description: str | None = None
    parent_id: int | None = None
    level: int = 1
    sort_order: int = 0
    icon: str | None = None
    color: str | None = None
    is_active: bool = True
    product_count: int = 0
    children: list["CategoryOut"] = Field(default_factory=list)


class BrandCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    code: str | None = Field(default=None, max_length=64)
    name_en: str | None = Field(default=None, max_length=255)
    description: str | None = None
    parent_id: int | None = None
    logo_path: str | None = Field(default=None, max_length=512)
    manufacturer: str | None = Field(default=None, max_length=255)
    is_active: bool = True


class BrandUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    code: str | None = Field(default=None, max_length=64)
    name_en: str | None = Field(default=None, max_length=255)
    description: str | None = None
    parent_id: int | None = None
    logo_path: str | None = Field(default=None, max_length=512)
    manufacturer: str | None = Field(default=None, max_length=255)
    is_active: bool | None = None


class BrandOut(ORMModel):
    id: int
    code: str
    name: str
    name_en: str | None = None
    description: str | None = None
    parent_id: int | None = None
    logo_path: str | None = None
    manufacturer: str | None = None
    is_active: bool = True
    product_count: int = 0
    children: list["BrandOut"] = Field(default_factory=list)


class CategoryRef(ORMModel):
    id: int
    code: str
    name: str
    name_en: str | None = None


class BrandRef(ORMModel):
    id: int
    code: str
    name: str
    name_en: str | None = None


# ===========================================================================
# Products
# ===========================================================================
class ProductCreate(BaseModel):
    sku: str = Field(min_length=1, max_length=64)
    #: Business code — drawn from the PRODUCT number sequence when omitted.
    code: str | None = Field(default=None, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    name_en: str | None = Field(default=None, max_length=255)
    short_name: str | None = Field(default=None, max_length=96)
    description: str | None = None

    category_id: int | None = None
    brand_id: int | None = None
    status: str = str(ProductStatus.ACTIVE)
    is_active: bool = True

    base_uom: Uom = str(UnitOfMeasure.PIECE)
    sales_uom: Uom = str(UnitOfMeasure.CASE)
    units_per_case: Decimal = Field(default=Decimal("1"), gt=0)

    unit_volume_l: float | None = Field(default=None, ge=0)
    unit_weight_kg: float | None = Field(default=None, ge=0)
    case_volume_l: float | None = Field(default=None, ge=0)
    case_weight_kg: float | None = Field(default=None, ge=0)
    storage_condition: str = str(StorageCondition.AMBIENT)

    is_lot_tracked: bool = True
    is_serial_tracked: bool = False
    shelf_life_days: int | None = Field(default=None, ge=0)
    min_remaining_shelf_life_days: int | None = Field(default=None, ge=0)

    purchase_price: Decimal = Field(default=Decimal("0"), ge=0)
    cost_price: Decimal = Field(default=Decimal("0"), ge=0)
    sale_price: Decimal = Field(default=Decimal("0"), ge=0)
    recommended_retail_price: Decimal = Field(default=Decimal("0"), ge=0)
    currency: str = Field(default="TRY", max_length=8)
    vat_rate: float = Field(default=20.0, ge=0, le=100)
    excise_rate: float = Field(default=0.0, ge=0, le=100)
    excise_amount: Decimal = Field(default=Decimal("0"), ge=0)
    max_discount_percent: float = Field(default=100.0, ge=0, le=100)

    min_stock_level: Decimal = Field(default=Decimal("0"), ge=0)
    max_stock_level: Decimal | None = Field(default=None, ge=0)
    reorder_point: Decimal | None = Field(default=None, ge=0)
    is_sellable: bool = True
    is_returnable: bool = True

    image_path: str | None = Field(default=None, max_length=512)
    tags: TagList = Field(default_factory=list)

    units: list[ProductUnitIn] = Field(default_factory=list)
    barcodes: list[BarcodeIn] = Field(default_factory=list)


class ProductUpdate(BaseModel):
    """Every field optional — only what is sent is written."""

    sku: str | None = Field(default=None, min_length=1, max_length=64)
    code: str | None = Field(default=None, max_length=64)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    name_en: str | None = Field(default=None, max_length=255)
    short_name: str | None = Field(default=None, max_length=96)
    description: str | None = None

    category_id: int | None = None
    brand_id: int | None = None
    status: str | None = None
    is_active: bool | None = None

    base_uom: Uom | None = None
    sales_uom: Uom | None = None
    units_per_case: Decimal | None = Field(default=None, gt=0)

    unit_volume_l: float | None = Field(default=None, ge=0)
    unit_weight_kg: float | None = Field(default=None, ge=0)
    case_volume_l: float | None = Field(default=None, ge=0)
    case_weight_kg: float | None = Field(default=None, ge=0)
    storage_condition: str | None = None

    is_lot_tracked: bool | None = None
    is_serial_tracked: bool | None = None
    shelf_life_days: int | None = Field(default=None, ge=0)
    min_remaining_shelf_life_days: int | None = Field(default=None, ge=0)

    purchase_price: Decimal | None = Field(default=None, ge=0)
    cost_price: Decimal | None = Field(default=None, ge=0)
    sale_price: Decimal | None = Field(default=None, ge=0)
    recommended_retail_price: Decimal | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, max_length=8)
    vat_rate: float | None = Field(default=None, ge=0, le=100)
    excise_rate: float | None = Field(default=None, ge=0, le=100)
    excise_amount: Decimal | None = Field(default=None, ge=0)
    max_discount_percent: float | None = Field(default=None, ge=0, le=100)

    min_stock_level: Decimal | None = Field(default=None, ge=0)
    max_stock_level: Decimal | None = Field(default=None, ge=0)
    reorder_point: Decimal | None = Field(default=None, ge=0)
    is_sellable: bool | None = None
    is_returnable: bool | None = None

    image_path: str | None = Field(default=None, max_length=512)
    tags: list[str] | None = None
    #: Sending either collection replaces it wholesale; omitting it leaves it alone.
    units: list[ProductUnitIn] | None = None
    barcodes: list[BarcodeIn] | None = None


class ProductListItem(ORMModel):
    """Row shape for the product grid — deliberately lighter than ProductOut."""

    id: int
    sku: str
    code: str
    name: str
    name_en: str | None = None
    short_name: str | None = None
    status: str
    is_active: bool = True
    is_sellable: bool = True
    category_id: int | None = None
    brand_id: int | None = None
    category: CategoryRef | None = None
    brand: BrandRef | None = None
    base_uom: str
    sales_uom: str
    units_per_case: Decimal
    sale_price: Decimal
    cost_price: Decimal
    currency: str = "TRY"
    vat_rate: float = 0.0
    min_stock_level: Decimal = Decimal("0")
    image_path: str | None = None
    tags: TagList = Field(default_factory=list)


class ProductOut(ORMModel):
    id: int
    sku: str
    code: str
    name: str
    name_en: str | None = None
    short_name: str | None = None
    description: str | None = None

    category_id: int | None = None
    brand_id: int | None = None
    category: CategoryRef | None = None
    brand: BrandRef | None = None
    status: str
    is_active: bool = True

    base_uom: str
    sales_uom: str
    units_per_case: Decimal

    unit_volume_l: float | None = None
    unit_weight_kg: float | None = None
    case_volume_l: float | None = None
    case_weight_kg: float | None = None
    storage_condition: str

    is_lot_tracked: bool = True
    is_serial_tracked: bool = False
    shelf_life_days: int | None = None
    min_remaining_shelf_life_days: int | None = None

    purchase_price: Decimal
    cost_price: Decimal
    sale_price: Decimal
    recommended_retail_price: Decimal
    currency: str
    vat_rate: float
    excise_rate: float
    excise_amount: Decimal
    max_discount_percent: float
    margin_percent: float = 0.0

    min_stock_level: Decimal
    max_stock_level: Decimal | None = None
    reorder_point: Decimal | None = None
    is_sellable: bool = True
    is_returnable: bool = True

    image_path: str | None = None
    tags: TagList = Field(default_factory=list)

    units: list[ProductUnitOut] = Field(default_factory=list)
    barcodes: list[BarcodeOut] = Field(default_factory=list)

    is_deleted: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ProductSearchResult(BaseModel):
    """Compact hit used by barcode scanning and type-ahead pickers."""

    id: int
    sku: str
    code: str = ""
    name: str
    short_name: str | None = None
    barcode: str | None = None
    category_name: str | None = None
    brand_name: str | None = None
    base_uom: str
    sales_uom: str
    units_per_case: Decimal = Decimal("1")
    sale_price: Decimal = Decimal("0")
    currency: str = "TRY"
    vat_rate: float = 0.0
    is_sellable: bool = True
    image_path: str | None = None
    uoms: list[UomOption] = Field(default_factory=list)


# ===========================================================================
# Price lists
# ===========================================================================
class PriceListItemIn(BaseModel):
    product_id: int
    uom: Uom = str(UnitOfMeasure.CASE)
    price: Decimal = Field(ge=0)
    min_quantity: Decimal = Field(default=Decimal("0"), ge=0)
    discount_percent: float = Field(default=0.0, ge=0, le=100)


class PriceListItemOut(ORMModel):
    id: int
    price_list_id: int
    product_id: int
    product_sku: str | None = None
    product_name: str | None = None
    uom: str
    price: Decimal
    min_quantity: Decimal = Decimal("0")
    discount_percent: float = 0.0


class PriceListCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    code: str | None = Field(default=None, max_length=64)
    name_en: str | None = Field(default=None, max_length=255)
    description: str | None = None
    currency: str = Field(default="TRY", max_length=8)
    valid_from: date | None = None
    valid_to: date | None = None
    channel: str | None = Field(default=None, max_length=32)
    customer_type: str | None = Field(default=None, max_length=32)
    region_id: int | None = None
    is_default: bool = False
    priority: int = Field(default=100, ge=0, le=10_000)
    is_active: bool = True
    items: list[PriceListItemIn] = Field(default_factory=list)


class PriceListUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    code: str | None = Field(default=None, max_length=64)
    name_en: str | None = Field(default=None, max_length=255)
    description: str | None = None
    currency: str | None = Field(default=None, max_length=8)
    valid_from: date | None = None
    valid_to: date | None = None
    channel: str | None = Field(default=None, max_length=32)
    customer_type: str | None = Field(default=None, max_length=32)
    region_id: int | None = None
    is_default: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=10_000)
    is_active: bool | None = None


class PriceListOut(ORMModel):
    id: int
    code: str
    name: str
    name_en: str | None = None
    description: str | None = None
    currency: str = "TRY"
    valid_from: date | None = None
    valid_to: date | None = None
    channel: str | None = None
    customer_type: str | None = None
    region_id: int | None = None
    is_default: bool = False
    priority: int = 100
    is_active: bool = True
    item_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class PriceListItemsReplace(BaseModel):
    """Write payload for a price list's lines."""

    items: list[PriceListItemIn] = Field(default_factory=list, max_length=20_000)
    replace: bool = Field(
        default=False,
        description="True deletes lines absent from the payload; False upserts only.",
    )


class PriceListItemsResult(BaseModel):
    """Outcome of a price-list write."""

    created: int = 0
    updated: int = 0
    removed: int = 0


class ResolvedPrice(BaseModel):
    product_id: int
    uom: str
    quantity: Decimal = Decimal("1")
    unit_price: Decimal
    price_list_id: int | None = None
    currency: str = "TRY"


# ===========================================================================
# Bulk import
# ===========================================================================
class ProductImportRequest(BaseModel):
    """Rows are loose dicts so a CSV/Excel upload can be forwarded unchanged."""

    rows: list[dict[str, Any]] = Field(default_factory=list, max_length=10_000)


class ProductImportResult(BulkResult):
    """:class:`BulkResult` plus the create/update split the operator wants to see."""

    created: int = 0
    updated: int = 0


CategoryOut.model_rebuild()
BrandOut.model_rebuild()
