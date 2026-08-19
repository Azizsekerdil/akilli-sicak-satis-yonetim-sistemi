"""
Product master data: catalogue, packaging, barcodes and price lists.

This module answers three questions about an article:

* *What is it?*      — product, category, brand (both hierarchies are trees).
* *How is it packed?* — base unit, cases, pallets, barcodes.
* *What does it cost?* — price-list resolution with quantity tiers.

**Unit convention.**  Every quantity in the database is stored in the product's
``base_uom``.  Field staff, however, speak in cases and pallets, so each
product carries conversion factors and this module owns the translation.  For
the same reason every monetary field on :class:`Product` (``cost_price``,
``sale_price``, …) is expressed **per base unit**; a price for a case is
derived by multiplying with that case's factor.  Keeping one convention means
margin and stock valuation never silently disagree.

Nothing here is ever hard-deleted: master data is referenced by historical
documents, so removal is always the ``is_deleted`` flag.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.enums import AuditAction, ProductStatus, StorageCondition, UnitOfMeasure
from app.core.exceptions import AppError, ConflictError, NotFoundError, ValidationError
from app.core.logging_config import get_logger
from app.core.utils import D, apply_percent, money, parse_date, qty, slugify
from app.models.base import utcnow
from app.models.product import (
    Barcode,
    Brand,
    PriceList,
    PriceListItem,
    Product,
    ProductCategory,
    ProductUnit,
)
from app.services import audit_service, numbering_service

log = get_logger("app.products")

ZERO = Decimal("0")
ONE = Decimal("1")

_VALID_UOMS: frozenset[str] = frozenset(str(u) for u in UnitOfMeasure)
_VALID_STATUS: frozenset[str] = frozenset(str(s) for s in ProductStatus)
_VALID_STORAGE: frozenset[str] = frozenset(str(s) for s in StorageCondition)

#: Scalar columns a caller may write on a product.
PRODUCT_FIELDS: tuple[str, ...] = (
    "sku", "code", "name", "name_en", "short_name", "description",
    "category_id", "brand_id", "status", "is_active",
    "base_uom", "sales_uom", "units_per_case",
    "unit_volume_l", "unit_weight_kg", "case_volume_l", "case_weight_kg",
    "storage_condition",
    "is_lot_tracked", "is_serial_tracked",
    "shelf_life_days", "min_remaining_shelf_life_days",
    "purchase_price", "cost_price", "sale_price", "recommended_retail_price",
    "currency", "vat_rate", "excise_rate", "excise_amount", "max_discount_percent",
    "min_stock_level", "max_stock_level", "reorder_point",
    "is_sellable", "is_returnable", "image_path", "tags",
)

_MONEY_FIELDS = frozenset(
    {"purchase_price", "cost_price", "sale_price", "recommended_retail_price", "excise_amount"}
)
_QTY_FIELDS = frozenset({"units_per_case", "min_stock_level", "max_stock_level", "reorder_point"})
#: Quantity columns that are NOT NULL — an explicit ``None`` means "zero".
_QTY_REQUIRED = frozenset({"units_per_case", "min_stock_level"})
_PERCENT_FIELDS = frozenset({"vat_rate", "excise_rate", "max_discount_percent"})
_UPPER_FIELDS = frozenset({"base_uom", "sales_uom", "storage_condition", "status", "currency"})

#: Column aliases accepted by :func:`bulk_import` so a Turkish CSV can be
#: forwarded without a mapping step in the UI.
IMPORT_ALIASES: dict[str, str] = {
    "stok_kodu": "sku",
    "stokkodu": "sku",
    "urun_kodu": "sku",
    "urun_adi": "name",
    "ad": "name",
    "isim": "name",
    "kisa_ad": "short_name",
    "barkod": "barcode",
    "kategori": "category_code",
    "marka": "brand_code",
    "kdv": "vat_rate",
    "kdv_orani": "vat_rate",
    "otv": "excise_rate",
    "fiyat": "sale_price",
    "satis_fiyati": "sale_price",
    "alis_fiyati": "purchase_price",
    "maliyet": "cost_price",
    "koli_ici": "units_per_case",
    "kolideki_adet": "units_per_case",
    "birim": "base_uom",
    "satis_birimi": "sales_uom",
    "para_birimi": "currency",
    "durum": "status",
    "aktif": "is_active",
}


# ===========================================================================
# Generic helpers
# ===========================================================================
def _audit(db: Session, action: str, *, user_id: int | None, audit_kwargs: dict[str, Any] | None, **fields: Any) -> None:
    """Write one audit row, merging the request-level fields the API supplies."""
    kwargs: dict[str, Any] = dict(audit_kwargs or {})
    kwargs.setdefault("user_id", user_id)
    audit_service.record(db, action, **kwargs, **fields)


def _like(term: str) -> str:
    """Portable case-insensitive contains pattern (no ILIKE on SQLite)."""
    return f"%{term.strip().lower()}%"


def _stamp_author(obj: Any, user_id: int | None, *, created: bool = False) -> None:
    if user_id is None:
        return
    if created and getattr(obj, "created_by_id", None) is None:
        obj.created_by_id = user_id
    obj.updated_by_id = user_id


def _soft_delete(obj: Any, user_id: int | None) -> None:
    obj.is_deleted = True
    obj.deleted_at = utcnow()
    obj.deleted_by_id = user_id
    obj.is_active = False


def _unique_code(db: Session, model: type, base: str, *, exclude_id: int | None = None) -> str:
    """Return *base* or ``base-2``, ``base-3``… until the code is free."""
    candidate = (base or "").strip().upper()[:60] or "ITEM"
    suffix = 1
    while True:
        stmt = select(model.id).where(func.lower(model.code) == candidate.lower())
        if exclude_id is not None:
            stmt = stmt.where(model.id != exclude_id)
        if db.execute(stmt.limit(1)).first() is None:
            return candidate
        suffix += 1
        candidate = f"{(base or 'ITEM').strip().upper()[:58]}-{suffix}"


def _norm_uom(value: Any, *, field: str = "uom") -> str:
    text = str(value or "").strip().upper()
    if text not in _VALID_UOMS:
        raise ValidationError("product.invalid_uom", params={"uom": value, "field": field})
    return text


# ===========================================================================
# Lookups
# ===========================================================================
def get_product(db: Session, product_id: int, *, include_deleted: bool = False) -> Product:
    stmt = select(Product).where(Product.id == product_id)
    if not include_deleted:
        stmt = stmt.where(Product.is_deleted.is_(False))
    row = db.execute(stmt).scalars().unique().one_or_none()
    if row is None:
        raise NotFoundError("product.not_found", params={"id": product_id})
    return row


def get_product_by_sku(db: Session, sku: str, *, include_deleted: bool = False) -> Product | None:
    stmt = select(Product).where(func.lower(Product.sku) == str(sku or "").strip().lower())
    if not include_deleted:
        stmt = stmt.where(Product.is_deleted.is_(False))
    return db.execute(stmt).scalars().unique().one_or_none()


def get_category(db: Session, category_id: int) -> ProductCategory:
    row = db.execute(
        select(ProductCategory).where(
            ProductCategory.id == category_id, ProductCategory.is_deleted.is_(False)
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("product.category_not_found", params={"id": category_id})
    return row


def get_brand(db: Session, brand_id: int) -> Brand:
    row = db.execute(
        select(Brand).where(Brand.id == brand_id, Brand.is_deleted.is_(False))
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("product.brand_not_found", params={"id": brand_id})
    return row


def get_price_list(db: Session, price_list_id: int) -> PriceList:
    row = db.execute(
        select(PriceList).where(PriceList.id == price_list_id, PriceList.is_deleted.is_(False))
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("product.price_list_not_found", params={"id": price_list_id})
    return row


# ===========================================================================
# Units of measure
# ===========================================================================
def uom_factor(product: Product, uom: str | None) -> Decimal:
    """
    How many base units one *uom* contains.

    Explicit :class:`ProductUnit` rows win over the shorthand ``units_per_case``
    because they are the record a warehouse actually maintains; the shorthand is
    only the fallback for products that never got a packaging table.
    """
    if uom is None:
        return ONE
    code = str(uom).strip().upper()
    if not code or code == str(product.base_uom or "").upper():
        return ONE

    for unit in product.units or ():
        if str(unit.uom or "").upper() == code:
            factor = D(unit.factor)
            if factor > 0:
                return factor

    if code in {str(product.sales_uom or "").upper(), str(UnitOfMeasure.CASE)}:
        factor = D(product.units_per_case)
        return factor if factor > 0 else ONE

    raise ValidationError("product.invalid_uom", params={"uom": uom, "sku": product.sku})


def to_base(product: Product, quantity: Any, uom: str | None) -> Decimal:
    """Convert a quantity expressed in *uom* into base units."""
    return qty(D(quantity) * uom_factor(product, uom))


def from_base(product: Product, base_quantity: Any, uom: str | None) -> Decimal:
    """Convert base units back into *uom* — the inverse of :func:`to_base`."""
    factor = uom_factor(product, uom)
    if factor <= 0:
        return ZERO
    return qty(D(base_quantity) / factor)


def available_uoms(product: Product) -> list[dict[str, Any]]:
    """Every unit this product can be traded in, ready for a UI dropdown."""
    base = str(product.base_uom or UnitOfMeasure.PIECE).upper()
    sales = str(product.sales_uom or base).upper()
    options: dict[str, dict[str, Any]] = {
        base: {
            "uom": base,
            "factor": ONE,
            "is_base": True,
            "is_sales_default": sales == base,
            "barcode": None,
            "price": None,
            "volume_l": product.unit_volume_l,
            "weight_kg": product.unit_weight_kg,
        }
    }

    for unit in sorted(product.units or (), key=lambda u: D(u.factor)):
        code = str(unit.uom or "").upper()
        if not code:
            continue
        factor = D(unit.factor)
        if factor <= 0:
            continue
        options[code] = {
            "uom": code,
            "factor": factor,
            "is_base": code == base,
            "is_sales_default": bool(unit.is_default_sales_unit) or code == sales,
            "barcode": unit.barcode,
            "price": unit.price,
            "volume_l": unit.volume_l,
            "weight_kg": unit.weight_kg,
        }

    if sales not in options:
        factor = D(product.units_per_case)
        options[sales] = {
            "uom": sales,
            "factor": factor if factor > 0 else ONE,
            "is_base": False,
            "is_sales_default": True,
            "barcode": None,
            "price": None,
            "volume_l": product.case_volume_l,
            "weight_kg": product.case_weight_kg,
        }

    return sorted(options.values(), key=lambda o: D(o["factor"]))


# ===========================================================================
# Product validation
# ===========================================================================
def _validate_product_data(data: dict[str, Any]) -> None:
    """Enforce the invariants the database cannot express itself."""
    for field in _PERCENT_FIELDS:
        if field in data and data[field] is not None:
            value = float(data[field])
            if not 0.0 <= value <= 100.0:
                key = "product.invalid_vat_rate" if field == "vat_rate" else "product.invalid_percent"
                raise ValidationError(key, params={"field": field, "value": value})

    for field in _MONEY_FIELDS:
        if field in data and data[field] is not None and D(data[field]) < 0:
            raise ValidationError("product.invalid_price", params={"field": field, "value": str(data[field])})

    if "units_per_case" in data and data["units_per_case"] is not None:
        if D(data["units_per_case"]) <= 0:
            raise ValidationError(
                "product.invalid_units_per_case", params={"value": str(data["units_per_case"])}
            )

    for field in ("min_stock_level", "max_stock_level", "reorder_point"):
        if field in data and data[field] is not None and D(data[field]) < 0:
            raise ValidationError("product.invalid_quantity", params={"field": field})

    for field in ("base_uom", "sales_uom"):
        if data.get(field):
            _norm_uom(data[field], field=field)

    if data.get("status") and str(data["status"]).upper() not in _VALID_STATUS:
        raise ValidationError("product.invalid_status", params={"status": data["status"]})

    if data.get("storage_condition") and str(data["storage_condition"]).upper() not in _VALID_STORAGE:
        raise ValidationError(
            "product.invalid_storage_condition", params={"value": data["storage_condition"]}
        )


def _assert_sku_free(db: Session, sku: str, *, exclude_id: int | None = None) -> None:
    """
    SKU uniqueness spans deleted rows too.

    The table's unique constraint does not know about soft deletion, so reusing
    a retired SKU would fail at flush time with an opaque IntegrityError; catch
    it here and return a translatable conflict instead.
    """
    stmt = select(Product.id).where(func.lower(Product.sku) == str(sku).strip().lower())
    if exclude_id is not None:
        stmt = stmt.where(Product.id != exclude_id)
    if db.execute(stmt.limit(1)).first() is not None:
        raise ConflictError("product.sku_taken", params={"sku": sku})


def _join_tags(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",")]
    elif isinstance(value, (list, tuple, set)):
        parts = [str(p).strip() for p in value]
    else:
        parts = [str(value).strip()]
    cleaned = [p for p in parts if p]
    return ",".join(cleaned) if cleaned else None


def _apply_product_fields(product: Product, data: dict[str, Any]) -> None:
    for field in PRODUCT_FIELDS:
        if field not in data:
            continue
        value = data[field]

        if field == "tags":
            value = _join_tags(value)
        elif field in _MONEY_FIELDS:
            value = money(value if value is not None else 0)
        elif field in _QTY_FIELDS:
            if value is None:
                value = ZERO if field in _QTY_REQUIRED else None
            else:
                value = qty(value)
        elif field in _UPPER_FIELDS and value is not None:
            value = str(value).strip().upper() or None
        elif field in _PERCENT_FIELDS and value is not None:
            value = float(value)
        elif field in ("sku", "code") and value is not None:
            value = str(value).strip()

        setattr(product, field, value)


def _product_snapshot(product: Product) -> dict[str, Any]:
    """The commercially meaningful slice of a product, for the audit trail."""
    return {
        "sku": product.sku,
        "name": product.name,
        "status": product.status,
        "is_active": product.is_active,
        "is_sellable": product.is_sellable,
        "category_id": product.category_id,
        "brand_id": product.brand_id,
        "base_uom": product.base_uom,
        "sales_uom": product.sales_uom,
        "units_per_case": str(product.units_per_case),
        "cost_price": str(product.cost_price),
        "sale_price": str(product.sale_price),
        "vat_rate": product.vat_rate,
        "max_discount_percent": product.max_discount_percent,
    }


# ===========================================================================
# Packaging & barcodes
# ===========================================================================
def set_units(db: Session, product: Product, units: Sequence[dict[str, Any]]) -> None:
    """
    Replace a product's packaging table.

    Rows are matched on ``uom`` and updated in place rather than deleted and
    re-inserted, because ``(product_id, uom)`` is unique and SQLAlchemy emits
    inserts before deletes within one flush.
    """
    wanted: dict[str, dict[str, Any]] = {}
    for raw in units or ():
        code = _norm_uom(raw.get("uom"))
        if code in wanted:
            raise ConflictError("product.duplicate_uom", params={"uom": code})
        factor = D(raw.get("factor"))
        if factor <= 0:
            raise ValidationError("product.invalid_unit_factor", params={"uom": code})
        if raw.get("price") is not None and D(raw["price"]) < 0:
            raise ValidationError("product.invalid_price", params={"field": "price", "uom": code})
        wanted[code] = raw

    existing = {str(u.uom or "").upper(): u for u in list(product.units or ())}
    for code, unit in existing.items():
        if code not in wanted:
            product.units.remove(unit)
    db.flush()

    for code, raw in wanted.items():
        unit = existing.get(code)
        if unit is None:
            unit = ProductUnit(uom=code, factor=D(raw["factor"]))
            product.units.append(unit)
        unit.factor = qty(raw["factor"])
        unit.barcode = (str(raw.get("barcode")).strip() or None) if raw.get("barcode") else None
        unit.price = money(raw["price"]) if raw.get("price") is not None else None
        unit.is_default_sales_unit = bool(raw.get("is_default_sales_unit"))
        unit.volume_l = raw.get("volume_l")
        unit.weight_kg = raw.get("weight_kg")
    db.flush()


def set_barcodes(db: Session, product: Product, barcodes: Sequence[dict[str, Any]]) -> None:
    """Replace a product's barcode list, refusing codes owned by another SKU."""
    wanted: dict[str, dict[str, Any]] = {}
    for raw in barcodes or ():
        code = str(raw.get("barcode") or "").strip()
        if not code:
            raise ValidationError("product.barcode_required")
        if code in wanted:
            raise ConflictError("product.barcode_taken", params={"barcode": code})
        wanted[code] = raw

    if wanted:
        stmt = select(Barcode.barcode).where(Barcode.barcode.in_(list(wanted)))
        if product.id is not None:
            stmt = stmt.where(Barcode.product_id != product.id)
        clash = db.execute(stmt.limit(1)).scalar_one_or_none()
        if clash:
            raise ConflictError("product.barcode_taken", params={"barcode": clash})

    existing = {b.barcode: b for b in list(product.barcodes or ())}
    for code, row in existing.items():
        if code not in wanted:
            product.barcodes.remove(row)
    db.flush()

    for code, raw in wanted.items():
        row = existing.get(code)
        if row is None:
            row = Barcode(barcode=code)
            product.barcodes.append(row)
        row.uom = _norm_uom(raw.get("uom") or product.base_uom)
        row.is_primary = bool(raw.get("is_primary"))
        row.label = raw.get("label")
    db.flush()


def find_by_barcode(db: Session, barcode: str) -> Product | None:
    """
    Resolve a scanned code to a product.

    Both barcode homes are consulted: the dedicated :class:`Barcode` table and
    the per-packaging ``ProductUnit.barcode`` a case label usually carries.
    """
    code = str(barcode or "").strip()
    if not code:
        return None

    product_id = db.execute(
        select(Barcode.product_id).where(Barcode.barcode == code).limit(1)
    ).scalar_one_or_none()
    if product_id is None:
        product_id = db.execute(
            select(ProductUnit.product_id).where(ProductUnit.barcode == code).limit(1)
        ).scalar_one_or_none()
    if product_id is None:
        lowered = code.lower()
        product_id = db.execute(
            select(Barcode.product_id).where(func.lower(Barcode.barcode) == lowered).limit(1)
        ).scalar_one_or_none()
    if product_id is None:
        return None

    return db.execute(
        select(Product).where(Product.id == product_id, Product.is_deleted.is_(False))
    ).scalars().unique().one_or_none()


def primary_barcode(product: Product) -> str | None:
    for row in product.barcodes or ():
        if row.is_primary:
            return row.barcode
    for row in product.barcodes or ():
        return row.barcode
    for unit in product.units or ():
        if unit.barcode:
            return unit.barcode
    return None


# ===========================================================================
# Product CRUD
# ===========================================================================
def create_product(
    db: Session,
    data: dict[str, Any],
    *,
    user_id: int | None = None,
    audit_kwargs: dict[str, Any] | None = None,
) -> Product:
    """Create a product together with its packaging and barcodes."""
    payload = dict(data)
    units = payload.pop("units", None) or []
    barcodes = payload.pop("barcodes", None) or []

    sku = str(payload.get("sku") or "").strip()
    if not sku:
        raise ValidationError("product.sku_required")
    if not str(payload.get("name") or "").strip():
        raise ValidationError("product.name_required", params={"sku": sku})
    payload["sku"] = sku
    _validate_product_data(payload)
    _assert_sku_free(db, sku)

    if payload.get("category_id"):
        get_category(db, int(payload["category_id"]))
    if payload.get("brand_id"):
        get_brand(db, int(payload["brand_id"]))

    # Drawn last so a rejected payload never burns a number from the sequence.
    if not payload.get("code"):
        payload["code"] = numbering_service.next_number(db, "PRODUCT")

    product = Product()
    _apply_product_fields(product, payload)
    product.base_uom = product.base_uom or str(UnitOfMeasure.PIECE)
    product.sales_uom = product.sales_uom or product.base_uom
    if D(product.units_per_case) <= 0:
        product.units_per_case = ONE
    _stamp_author(product, user_id, created=True)

    db.add(product)
    db.flush()

    if units:
        set_units(db, product, units)
    if barcodes:
        set_barcodes(db, product, barcodes)

    _audit(
        db,
        AuditAction.CREATE,
        user_id=user_id,
        audit_kwargs=audit_kwargs,
        entity_type="Product",
        entity_id=product.id,
        entity_label=product.sku,
        summary=f"product.created sku={product.sku}",
        new_values=_product_snapshot(product),
    )
    return product


def update_product(
    db: Session,
    product_id: int,
    data: dict[str, Any],
    *,
    user_id: int | None = None,
    audit_kwargs: dict[str, Any] | None = None,
) -> Product:
    """Patch a product; only keys present in *data* are written."""
    product = get_product(db, product_id)
    payload = {k: v for k, v in data.items() if k in PRODUCT_FIELDS or k in ("units", "barcodes")}
    units = payload.pop("units", None)
    barcodes = payload.pop("barcodes", None)

    _validate_product_data(payload)

    if "sku" in payload and payload["sku"]:
        new_sku = str(payload["sku"]).strip()
        if new_sku.lower() != product.sku.lower():
            _assert_sku_free(db, new_sku, exclude_id=product.id)
    if payload.get("category_id"):
        get_category(db, int(payload["category_id"]))
    if payload.get("brand_id"):
        get_brand(db, int(payload["brand_id"]))

    before = _product_snapshot(product)
    old_price = D(product.sale_price)

    _apply_product_fields(product, payload)
    if D(product.units_per_case) <= 0:
        product.units_per_case = ONE
    _stamp_author(product, user_id)

    if units is not None:
        set_units(db, product, units)
    if barcodes is not None:
        set_barcodes(db, product, barcodes)
    db.flush()

    after = _product_snapshot(product)
    price_changed = D(product.sale_price) != old_price
    _audit(
        db,
        AuditAction.PRICE_CHANGE if price_changed else AuditAction.UPDATE,
        user_id=user_id,
        audit_kwargs=audit_kwargs,
        entity_type="Product",
        entity_id=product.id,
        entity_label=product.sku,
        summary=f"product.updated sku={product.sku}",
        old_values=before,
        new_values=after,
        amount=D(product.sale_price) if price_changed else None,
    )
    return product


def delete_product(
    db: Session,
    product_id: int,
    *,
    user_id: int | None = None,
    audit_kwargs: dict[str, Any] | None = None,
) -> Product:
    """Soft-delete: historical invoices and stock movements still point here."""
    product = get_product(db, product_id)
    before = _product_snapshot(product)
    _soft_delete(product, user_id)
    product.status = str(ProductStatus.PASSIVE)
    product.is_sellable = False
    _stamp_author(product, user_id)
    db.flush()

    _audit(
        db,
        AuditAction.DELETE,
        user_id=user_id,
        audit_kwargs=audit_kwargs,
        entity_type="Product",
        entity_id=product.id,
        entity_label=product.sku,
        summary=f"product.deleted sku={product.sku}",
        old_values=before,
    )
    return product


# ===========================================================================
# Search
# ===========================================================================
def _descendant_ids(db: Session, model: type, root_id: int) -> list[int]:
    """All ids in the subtree rooted at *root_id*, inclusive."""
    rows = db.execute(select(model.id, model.parent_id)).all()
    children: dict[int, list[int]] = {}
    for node_id, parent_id in rows:
        children.setdefault(parent_id, []).append(node_id)

    collected: list[int] = []
    stack: list[int] = [root_id]
    seen: set[int] = set()
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        collected.append(current)
        stack.extend(children.get(current, ()))
    return collected


def search(
    db: Session,
    *,
    term: str | None = None,
    category_id: int | None = None,
    brand_id: int | None = None,
    status: str | None = None,
    is_active: bool | None = None,
    only_sellable: bool = False,
    page: int = 1,
    size: int = 50,
    include_deleted: bool = False,
    include_subcategories: bool = True,
    order_by: str = "name",
) -> tuple[list[Product], int]:
    """
    Paged catalogue search.

    Matches SKU, code, both name fields, the short name, free-text tags and —
    via sub-selects rather than joins, so a product never appears twice — every
    barcode recorded against the product or one of its packagings.
    """
    conds: list[Any] = []
    if not include_deleted:
        conds.append(Product.is_deleted.is_(False))

    if term and term.strip():
        pattern = _like(term)
        barcode_hits = select(Barcode.product_id).where(func.lower(Barcode.barcode).like(pattern))
        unit_hits = select(ProductUnit.product_id).where(func.lower(ProductUnit.barcode).like(pattern))
        conds.append(
            or_(
                func.lower(Product.sku).like(pattern),
                func.lower(Product.code).like(pattern),
                func.lower(Product.name).like(pattern),
                func.lower(func.coalesce(Product.name_en, "")).like(pattern),
                func.lower(func.coalesce(Product.short_name, "")).like(pattern),
                func.lower(func.coalesce(Product.tags, "")).like(pattern),
                Product.id.in_(barcode_hits),
                Product.id.in_(unit_hits),
            )
        )

    if category_id:
        if include_subcategories:
            ids = _descendant_ids(db, ProductCategory, int(category_id))
            conds.append(Product.category_id.in_(ids))
        else:
            conds.append(Product.category_id == int(category_id))
    if brand_id:
        if include_subcategories:
            conds.append(Product.brand_id.in_(_descendant_ids(db, Brand, int(brand_id))))
        else:
            conds.append(Product.brand_id == int(brand_id))
    if status:
        conds.append(Product.status == str(status).strip().upper())
    if is_active is not None:
        conds.append(Product.is_active.is_(bool(is_active)))
    if only_sellable:
        conds.append(Product.is_sellable.is_(True))
        conds.append(Product.status == str(ProductStatus.ACTIVE))

    total = int(
        db.execute(select(func.count()).select_from(Product).where(*conds)).scalar_one() or 0
    )

    ordering = {
        "name": (Product.name.asc(), Product.id.asc()),
        "sku": (Product.sku.asc(),),
        "price": (Product.sale_price.desc(), Product.name.asc()),
        "created": (Product.created_at.desc(), Product.id.desc()),
    }.get(order_by, (Product.name.asc(), Product.id.asc()))

    page = max(1, int(page))
    size = max(1, min(int(size), 500))
    rows = (
        db.execute(
            select(Product)
            .where(*conds)
            .order_by(*ordering)
            .offset((page - 1) * size)
            .limit(size)
        )
        .scalars()
        .unique()
        .all()
    )
    return list(rows), total


def search_result(product: Product) -> dict[str, Any]:
    """Compact projection used by the scanner and type-ahead endpoints."""
    return {
        "id": product.id,
        "sku": product.sku,
        "code": product.code,
        "name": product.name,
        "short_name": product.short_name,
        "barcode": primary_barcode(product),
        "category_name": product.category.name if product.category else None,
        "brand_name": product.brand.name if product.brand else None,
        "base_uom": product.base_uom,
        "sales_uom": product.sales_uom,
        "units_per_case": product.units_per_case,
        "sale_price": product.sale_price,
        "currency": product.currency,
        "vat_rate": product.vat_rate,
        "is_sellable": product.is_sellable,
        "image_path": product.image_path,
        "uoms": available_uoms(product),
    }


# ===========================================================================
# Categories & brands (both hierarchical)
# ===========================================================================
def _assert_no_cycle(db: Session, model: type, node_id: int, parent_id: int | None) -> None:
    """A node may not become its own ancestor — that would strand the subtree."""
    if parent_id is None:
        return
    if parent_id == node_id:
        raise ConflictError("product.parent_cycle", params={"id": node_id})
    seen: set[int] = set()
    current: int | None = parent_id
    while current is not None and current not in seen:
        seen.add(current)
        if current == node_id:
            raise ConflictError("product.parent_cycle", params={"id": node_id})
        current = db.execute(select(model.parent_id).where(model.id == current)).scalar_one_or_none()


def _category_level(db: Session, parent_id: int | None) -> int:
    if not parent_id:
        return 1
    parent = get_category(db, int(parent_id))
    return int(parent.level or 1) + 1


def _recalc_category_levels(db: Session, root: ProductCategory) -> None:
    """Re-stamp ``level`` down the subtree after a re-parent."""
    stack: list[ProductCategory] = [root]
    seen: set[int] = set()
    while stack:
        node = stack.pop()
        if node.id in seen:
            continue
        seen.add(node.id)
        children = db.execute(
            select(ProductCategory).where(ProductCategory.parent_id == node.id)
        ).scalars().all()
        for child in children:
            child.level = int(node.level or 1) + 1
            stack.append(child)


def create_category(
    db: Session,
    data: dict[str, Any],
    *,
    user_id: int | None = None,
    audit_kwargs: dict[str, Any] | None = None,
) -> ProductCategory:
    name = str(data.get("name") or "").strip()
    if not name:
        raise ValidationError("product.category_name_required")

    parent_id = data.get("parent_id") or None
    category = ProductCategory(
        code=_unique_code(db, ProductCategory, data.get("code") or slugify(name)),
        name=name,
        name_en=data.get("name_en"),
        description=data.get("description"),
        parent_id=parent_id,
        level=_category_level(db, parent_id),
        sort_order=int(data.get("sort_order") or 0),
        icon=data.get("icon"),
        color=data.get("color"),
        is_active=bool(data.get("is_active", True)),
    )
    _stamp_author(category, user_id, created=True)
    db.add(category)
    db.flush()

    _audit(
        db, AuditAction.CREATE, user_id=user_id, audit_kwargs=audit_kwargs,
        entity_type="ProductCategory", entity_id=category.id, entity_label=category.code,
        summary=f"category.created code={category.code}",
        new_values={"code": category.code, "name": category.name, "parent_id": category.parent_id},
    )
    return category


def update_category(
    db: Session,
    category_id: int,
    data: dict[str, Any],
    *,
    user_id: int | None = None,
    audit_kwargs: dict[str, Any] | None = None,
) -> ProductCategory:
    category = get_category(db, category_id)
    before = {"code": category.code, "name": category.name, "parent_id": category.parent_id}

    if "parent_id" in data and data["parent_id"] != category.parent_id:
        _assert_no_cycle(db, ProductCategory, category.id, data["parent_id"])
        category.parent_id = data["parent_id"] or None
        category.level = _category_level(db, category.parent_id)
        _recalc_category_levels(db, category)

    if data.get("code") and data["code"] != category.code:
        category.code = _unique_code(db, ProductCategory, data["code"], exclude_id=category.id)
    for field in ("name", "name_en", "description", "icon", "color"):
        if field in data and data[field] is not None:
            setattr(category, field, data[field])
    if data.get("sort_order") is not None:
        category.sort_order = int(data["sort_order"])
    if data.get("is_active") is not None:
        category.is_active = bool(data["is_active"])

    _stamp_author(category, user_id)
    db.flush()

    _audit(
        db, AuditAction.UPDATE, user_id=user_id, audit_kwargs=audit_kwargs,
        entity_type="ProductCategory", entity_id=category.id, entity_label=category.code,
        summary=f"category.updated code={category.code}",
        old_values=before,
        new_values={"code": category.code, "name": category.name, "parent_id": category.parent_id},
    )
    return category


def delete_category(
    db: Session,
    category_id: int,
    *,
    user_id: int | None = None,
    audit_kwargs: dict[str, Any] | None = None,
) -> ProductCategory:
    """Refuse while anything still hangs off the node — silent orphans hide stock."""
    category = get_category(db, category_id)

    child = db.execute(
        select(ProductCategory.id).where(
            ProductCategory.parent_id == category.id, ProductCategory.is_deleted.is_(False)
        ).limit(1)
    ).first()
    if child is not None:
        raise ConflictError("product.category_has_children", params={"id": category.id})

    used = db.execute(
        select(Product.id).where(
            Product.category_id == category.id, Product.is_deleted.is_(False)
        ).limit(1)
    ).first()
    if used is not None:
        raise ConflictError("product.category_in_use", params={"id": category.id})

    _soft_delete(category, user_id)
    db.flush()
    _audit(
        db, AuditAction.DELETE, user_id=user_id, audit_kwargs=audit_kwargs,
        entity_type="ProductCategory", entity_id=category.id, entity_label=category.code,
        summary=f"category.deleted code={category.code}",
    )
    return category


def create_brand(
    db: Session,
    data: dict[str, Any],
    *,
    user_id: int | None = None,
    audit_kwargs: dict[str, Any] | None = None,
) -> Brand:
    name = str(data.get("name") or "").strip()
    if not name:
        raise ValidationError("product.brand_name_required")

    brand = Brand(
        code=_unique_code(db, Brand, data.get("code") or slugify(name)),
        name=name,
        name_en=data.get("name_en"),
        description=data.get("description"),
        parent_id=data.get("parent_id") or None,
        logo_path=data.get("logo_path"),
        manufacturer=data.get("manufacturer"),
        is_active=bool(data.get("is_active", True)),
    )
    _stamp_author(brand, user_id, created=True)
    db.add(brand)
    db.flush()

    _audit(
        db, AuditAction.CREATE, user_id=user_id, audit_kwargs=audit_kwargs,
        entity_type="Brand", entity_id=brand.id, entity_label=brand.code,
        summary=f"brand.created code={brand.code}",
        new_values={"code": brand.code, "name": brand.name, "parent_id": brand.parent_id},
    )
    return brand


def update_brand(
    db: Session,
    brand_id: int,
    data: dict[str, Any],
    *,
    user_id: int | None = None,
    audit_kwargs: dict[str, Any] | None = None,
) -> Brand:
    brand = get_brand(db, brand_id)
    before = {"code": brand.code, "name": brand.name, "parent_id": brand.parent_id}

    if "parent_id" in data and data["parent_id"] != brand.parent_id:
        _assert_no_cycle(db, Brand, brand.id, data["parent_id"])
        brand.parent_id = data["parent_id"] or None

    if data.get("code") and data["code"] != brand.code:
        brand.code = _unique_code(db, Brand, data["code"], exclude_id=brand.id)
    for field in ("name", "name_en", "description", "logo_path", "manufacturer"):
        if field in data and data[field] is not None:
            setattr(brand, field, data[field])
    if data.get("is_active") is not None:
        brand.is_active = bool(data["is_active"])

    _stamp_author(brand, user_id)
    db.flush()

    _audit(
        db, AuditAction.UPDATE, user_id=user_id, audit_kwargs=audit_kwargs,
        entity_type="Brand", entity_id=brand.id, entity_label=brand.code,
        summary=f"brand.updated code={brand.code}",
        old_values=before,
        new_values={"code": brand.code, "name": brand.name, "parent_id": brand.parent_id},
    )
    return brand


def delete_brand(
    db: Session,
    brand_id: int,
    *,
    user_id: int | None = None,
    audit_kwargs: dict[str, Any] | None = None,
) -> Brand:
    brand = get_brand(db, brand_id)

    child = db.execute(
        select(Brand.id).where(Brand.parent_id == brand.id, Brand.is_deleted.is_(False)).limit(1)
    ).first()
    if child is not None:
        raise ConflictError("product.brand_has_children", params={"id": brand.id})

    used = db.execute(
        select(Product.id).where(Product.brand_id == brand.id, Product.is_deleted.is_(False)).limit(1)
    ).first()
    if used is not None:
        raise ConflictError("product.brand_in_use", params={"id": brand.id})

    _soft_delete(brand, user_id)
    db.flush()
    _audit(
        db, AuditAction.DELETE, user_id=user_id, audit_kwargs=audit_kwargs,
        entity_type="Brand", entity_id=brand.id, entity_label=brand.code,
        summary=f"brand.deleted code={brand.code}",
    )
    return brand


def _product_counts(db: Session, column: Any) -> dict[int, int]:
    rows = db.execute(
        select(column, func.count(Product.id))
        .where(Product.is_deleted.is_(False), column.is_not(None))
        .group_by(column)
    ).all()
    return {int(key): int(count) for key, count in rows if key is not None}


def _build_tree(nodes: Sequence[Any], payload: Any, counts: dict[int, int]) -> list[dict[str, Any]]:
    """Turn a flat parent_id list into nested dicts, roots first."""
    by_id: dict[int, dict[str, Any]] = {}
    for node in nodes:
        item = payload(node)
        item["product_count"] = counts.get(node.id, 0)
        item["children"] = []
        by_id[node.id] = item

    roots: list[dict[str, Any]] = []
    for node in nodes:
        item = by_id[node.id]
        parent = by_id.get(node.parent_id) if node.parent_id else None
        if parent is None:
            roots.append(item)
        else:
            parent["children"].append(item)
    return roots


def category_payload(category: ProductCategory) -> dict[str, Any]:
    return {
        "id": category.id,
        "code": category.code,
        "name": category.name,
        "name_en": category.name_en,
        "description": category.description,
        "parent_id": category.parent_id,
        "level": category.level,
        "sort_order": category.sort_order,
        "icon": category.icon,
        "color": category.color,
        "is_active": category.is_active,
        "product_count": 0,
        "children": [],
    }


def brand_payload(brand: Brand) -> dict[str, Any]:
    return {
        "id": brand.id,
        "code": brand.code,
        "name": brand.name,
        "name_en": brand.name_en,
        "description": brand.description,
        "parent_id": brand.parent_id,
        "logo_path": brand.logo_path,
        "manufacturer": brand.manufacturer,
        "is_active": brand.is_active,
        "product_count": 0,
        "children": [],
    }


def category_tree(db: Session, *, only_active: bool = False) -> list[dict[str, Any]]:
    """Nested category dicts with per-node product counts, ready for the UI."""
    stmt = select(ProductCategory).where(ProductCategory.is_deleted.is_(False))
    if only_active:
        stmt = stmt.where(ProductCategory.is_active.is_(True))
    nodes = db.execute(
        stmt.order_by(ProductCategory.sort_order.asc(), ProductCategory.name.asc())
    ).scalars().all()
    return _build_tree(nodes, category_payload, _product_counts(db, Product.category_id))


def brand_tree(db: Session, *, only_active: bool = False) -> list[dict[str, Any]]:
    stmt = select(Brand).where(Brand.is_deleted.is_(False))
    if only_active:
        stmt = stmt.where(Brand.is_active.is_(True))
    nodes = db.execute(stmt.order_by(Brand.name.asc())).scalars().all()
    return _build_tree(nodes, brand_payload, _product_counts(db, Product.brand_id))


# ===========================================================================
# Price lists
# ===========================================================================
def price_list_payload(db: Session, price_list: PriceList) -> dict[str, Any]:
    """Serialisable view of one price list, with its line count filled in."""
    count = db.execute(
        select(func.count())
        .select_from(PriceListItem)
        .where(PriceListItem.price_list_id == price_list.id)
    ).scalar_one()
    return {
        "id": price_list.id,
        "code": price_list.code,
        "name": price_list.name,
        "name_en": price_list.name_en,
        "description": price_list.description,
        "currency": price_list.currency,
        "valid_from": price_list.valid_from,
        "valid_to": price_list.valid_to,
        "channel": price_list.channel,
        "customer_type": price_list.customer_type,
        "region_id": price_list.region_id,
        "is_default": price_list.is_default,
        "priority": price_list.priority,
        "is_active": price_list.is_active,
        "item_count": int(count or 0),
        "created_at": price_list.created_at,
        "updated_at": price_list.updated_at,
    }


def list_price_lists(
    db: Session,
    *,
    is_active: bool | None = None,
    channel: str | None = None,
    customer_type: str | None = None,
    region_id: int | None = None,
    on_date: date | None = None,
) -> list[dict[str, Any]]:
    """Price lists plus their line counts, ordered strongest-first."""
    conds: list[Any] = [PriceList.is_deleted.is_(False)]
    if is_active is not None:
        conds.append(PriceList.is_active.is_(bool(is_active)))
    if channel:
        conds.append(or_(PriceList.channel.is_(None), PriceList.channel == channel))
    if customer_type:
        conds.append(or_(PriceList.customer_type.is_(None), PriceList.customer_type == customer_type))
    if region_id:
        conds.append(or_(PriceList.region_id.is_(None), PriceList.region_id == region_id))
    if on_date:
        conds.append(or_(PriceList.valid_from.is_(None), PriceList.valid_from <= on_date))
        conds.append(or_(PriceList.valid_to.is_(None), PriceList.valid_to >= on_date))

    rows = db.execute(
        select(PriceList).where(*conds).order_by(
            PriceList.priority.desc(), PriceList.is_default.desc(), PriceList.name.asc()
        )
    ).scalars().all()

    counts = dict(
        db.execute(
            select(PriceListItem.price_list_id, func.count(PriceListItem.id))
            .group_by(PriceListItem.price_list_id)
        ).all()
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "id": row.id,
                "code": row.code,
                "name": row.name,
                "name_en": row.name_en,
                "description": row.description,
                "currency": row.currency,
                "valid_from": row.valid_from,
                "valid_to": row.valid_to,
                "channel": row.channel,
                "customer_type": row.customer_type,
                "region_id": row.region_id,
                "is_default": row.is_default,
                "priority": row.priority,
                "is_active": row.is_active,
                "item_count": int(counts.get(row.id, 0)),
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            }
        )
    return out


def create_price_list(
    db: Session,
    data: dict[str, Any],
    *,
    user_id: int | None = None,
    audit_kwargs: dict[str, Any] | None = None,
) -> PriceList:
    name = str(data.get("name") or "").strip()
    if not name:
        raise ValidationError("product.price_list_name_required")

    valid_from = parse_date(data.get("valid_from"))
    valid_to = parse_date(data.get("valid_to"))
    if valid_from and valid_to and valid_to < valid_from:
        raise ValidationError("product.invalid_validity_range")

    price_list = PriceList(
        code=_unique_code(db, PriceList, data.get("code") or slugify(name)),
        name=name,
        name_en=data.get("name_en"),
        description=data.get("description"),
        currency=str(data.get("currency") or "TRY").upper(),
        valid_from=valid_from,
        valid_to=valid_to,
        channel=data.get("channel"),
        customer_type=data.get("customer_type"),
        region_id=data.get("region_id"),
        is_default=bool(data.get("is_default", False)),
        priority=int(data.get("priority") or 100),
        is_active=bool(data.get("is_active", True)),
    )
    _stamp_author(price_list, user_id, created=True)
    db.add(price_list)
    db.flush()

    if data.get("is_default"):
        _clear_other_defaults(db, price_list.id)
    items = data.get("items") or []
    if items:
        upsert_price_list_items(db, price_list.id, items, user_id=user_id, audit_kwargs=audit_kwargs)

    _audit(
        db, AuditAction.CREATE, user_id=user_id, audit_kwargs=audit_kwargs,
        entity_type="PriceList", entity_id=price_list.id, entity_label=price_list.code,
        summary=f"price_list.created code={price_list.code} items={len(items)}",
        new_values={"code": price_list.code, "name": price_list.name, "priority": price_list.priority},
    )
    return price_list


def _clear_other_defaults(db: Session, keep_id: int) -> None:
    """Only one list may be the fallback, otherwise resolution is ambiguous."""
    others = db.execute(
        select(PriceList).where(PriceList.is_default.is_(True), PriceList.id != keep_id)
    ).scalars().all()
    for row in others:
        row.is_default = False
    db.flush()


def update_price_list(
    db: Session,
    price_list_id: int,
    data: dict[str, Any],
    *,
    user_id: int | None = None,
    audit_kwargs: dict[str, Any] | None = None,
) -> PriceList:
    price_list = get_price_list(db, price_list_id)
    before = {
        "name": price_list.name,
        "priority": price_list.priority,
        "is_default": price_list.is_default,
        "is_active": price_list.is_active,
    }

    if data.get("code") and data["code"] != price_list.code:
        price_list.code = _unique_code(db, PriceList, data["code"], exclude_id=price_list.id)
    for field in ("name", "name_en", "description", "channel", "customer_type"):
        if field in data and data[field] is not None:
            setattr(price_list, field, data[field])
    if data.get("currency"):
        price_list.currency = str(data["currency"]).upper()
    if "valid_from" in data:
        price_list.valid_from = parse_date(data["valid_from"])
    if "valid_to" in data:
        price_list.valid_to = parse_date(data["valid_to"])
    if price_list.valid_from and price_list.valid_to and price_list.valid_to < price_list.valid_from:
        raise ValidationError("product.invalid_validity_range")
    if "region_id" in data:
        price_list.region_id = data["region_id"]
    if data.get("priority") is not None:
        price_list.priority = int(data["priority"])
    if data.get("is_active") is not None:
        price_list.is_active = bool(data["is_active"])
    if data.get("is_default") is not None:
        price_list.is_default = bool(data["is_default"])
        if price_list.is_default:
            _clear_other_defaults(db, price_list.id)

    _stamp_author(price_list, user_id)
    db.flush()

    _audit(
        db, AuditAction.UPDATE, user_id=user_id, audit_kwargs=audit_kwargs,
        entity_type="PriceList", entity_id=price_list.id, entity_label=price_list.code,
        summary=f"price_list.updated code={price_list.code}",
        old_values=before,
        new_values={
            "name": price_list.name,
            "priority": price_list.priority,
            "is_default": price_list.is_default,
            "is_active": price_list.is_active,
        },
    )
    return price_list


def delete_price_list(
    db: Session,
    price_list_id: int,
    *,
    user_id: int | None = None,
    audit_kwargs: dict[str, Any] | None = None,
) -> PriceList:
    price_list = get_price_list(db, price_list_id)
    _soft_delete(price_list, user_id)
    db.flush()
    _audit(
        db, AuditAction.DELETE, user_id=user_id, audit_kwargs=audit_kwargs,
        entity_type="PriceList", entity_id=price_list.id, entity_label=price_list.code,
        summary=f"price_list.deleted code={price_list.code}",
    )
    return price_list


def price_list_items(
    db: Session,
    price_list_id: int,
    *,
    term: str | None = None,
    product_id: int | None = None,
    page: int = 1,
    size: int = 100,
) -> tuple[list[dict[str, Any]], int]:
    """Lines of a price list, joined to the product for a usable grid."""
    get_price_list(db, price_list_id)

    conds: list[Any] = [PriceListItem.price_list_id == price_list_id]
    if product_id:
        conds.append(PriceListItem.product_id == int(product_id))
    if term and term.strip():
        pattern = _like(term)
        hits = select(Product.id).where(
            or_(func.lower(Product.sku).like(pattern), func.lower(Product.name).like(pattern))
        )
        conds.append(PriceListItem.product_id.in_(hits))

    total = int(
        db.execute(select(func.count()).select_from(PriceListItem).where(*conds)).scalar_one() or 0
    )
    page = max(1, int(page))
    size = max(1, min(int(size), 500))

    rows = db.execute(
        select(PriceListItem, Product.sku, Product.name)
        .join(Product, Product.id == PriceListItem.product_id)
        .where(*conds)
        .order_by(Product.name.asc(), PriceListItem.min_quantity.asc())
        .offset((page - 1) * size)
        .limit(size)
    ).all()

    items = [
        {
            "id": item.id,
            "price_list_id": item.price_list_id,
            "product_id": item.product_id,
            "product_sku": sku,
            "product_name": name,
            "uom": item.uom,
            "price": item.price,
            "min_quantity": item.min_quantity,
            "discount_percent": item.discount_percent,
        }
        for item, sku, name in rows
    ]
    return items, total


def upsert_price_list_items(
    db: Session,
    price_list_id: int,
    items: Iterable[dict[str, Any]],
    *,
    replace: bool = False,
    user_id: int | None = None,
    audit_kwargs: dict[str, Any] | None = None,
) -> dict[str, int]:
    """
    Write price lines, keyed on ``(product_id, uom)``.

    That key is the table's own unique constraint, so one list holds at most
    one line per article and unit.  Quantity ladders ("10+ cases are cheaper")
    are therefore modelled as a second, higher-priority list carrying a
    ``min_quantity`` threshold rather than as extra rows here.
    """
    price_list = get_price_list(db, price_list_id)
    payload = list(items or [])

    existing = db.execute(
        select(PriceListItem).where(PriceListItem.price_list_id == price_list.id)
    ).scalars().all()
    by_key: dict[tuple[int, str], PriceListItem] = {
        (row.product_id, str(row.uom).upper()): row for row in existing
    }

    created = updated = 0
    touched: set[tuple[int, str]] = set()
    seen_products: set[int] = set()

    for raw in payload:
        product_id = int(raw.get("product_id") or 0)
        if not product_id:
            raise ValidationError("product.price_item_product_required")
        if product_id not in seen_products:
            get_product(db, product_id)
            seen_products.add(product_id)

        uom = _norm_uom(raw.get("uom") or UnitOfMeasure.CASE)
        price = money(raw.get("price"))
        if price < 0:
            raise ValidationError("product.invalid_price", params={"field": "price"})
        min_quantity = qty(raw.get("min_quantity") or 0)
        if min_quantity < 0:
            raise ValidationError("product.invalid_quantity", params={"field": "min_quantity"})
        discount = float(raw.get("discount_percent") or 0.0)
        if not 0.0 <= discount <= 100.0:
            raise ValidationError("product.invalid_percent", params={"field": "discount_percent"})

        key = (product_id, uom)
        if key in touched:
            raise ConflictError(
                "product.duplicate_price_line", params={"product_id": product_id, "uom": uom}
            )
        touched.add(key)
        row = by_key.get(key)
        if row is None:
            row = PriceListItem(
                price_list_id=price_list.id,
                product_id=product_id,
                uom=uom,
                price=price,
                min_quantity=min_quantity,
                discount_percent=discount,
            )
            db.add(row)
            by_key[key] = row
            created += 1
        else:
            row.price = price
            row.min_quantity = min_quantity
            row.discount_percent = discount
            updated += 1

    removed = 0
    if replace:
        for key, row in list(by_key.items()):
            if key not in touched:
                db.delete(row)
                removed += 1
    db.flush()

    _audit(
        db, AuditAction.PRICE_CHANGE, user_id=user_id, audit_kwargs=audit_kwargs,
        entity_type="PriceList", entity_id=price_list.id, entity_label=price_list.code,
        summary=(
            f"price_list.items code={price_list.code} "
            f"created={created} updated={updated} removed={removed}"
        ),
        new_values={"created": created, "updated": updated, "removed": removed},
    )
    return {"created": created, "updated": updated, "removed": removed}


# ---------------------------------------------------------------------------
# Price resolution
# ---------------------------------------------------------------------------
def _specificity(price_list: PriceList) -> int:
    return sum(
        1 for value in (price_list.channel, price_list.customer_type, price_list.region_id) if value
    )


def _scoped_price_lists(db: Session, customer: Any, on_date: date) -> list[PriceList]:
    """
    Every list whose scope covers this customer, strongest first.

    Ordering: explicit ``priority`` (higher wins) → more specific scope →
    non-default before the catch-all default → newest.
    """
    channel = getattr(customer, "channel", None)
    customer_type = getattr(customer, "customer_type", None)
    region_id = getattr(customer, "region_id", None)

    rows = db.execute(
        select(PriceList).where(
            PriceList.is_deleted.is_(False),
            PriceList.is_active.is_(True),
            or_(PriceList.valid_from.is_(None), PriceList.valid_from <= on_date),
            or_(PriceList.valid_to.is_(None), PriceList.valid_to >= on_date),
            or_(PriceList.channel.is_(None), PriceList.channel == channel),
            or_(PriceList.customer_type.is_(None), PriceList.customer_type == customer_type),
            or_(PriceList.region_id.is_(None), PriceList.region_id == region_id),
        )
    ).scalars().all()

    return sorted(
        rows,
        key=lambda pl: (-int(pl.priority or 0), -_specificity(pl), bool(pl.is_default), -pl.id),
    )


def _item_unit_price(
    product: Product, item: PriceListItem, target_uom: str, target_factor: Decimal
) -> Decimal | None:
    """Net price of one *target_uom*, converting through base units if needed."""
    gross = D(item.price)
    if item.discount_percent:
        gross = gross - apply_percent(gross, float(item.discount_percent))
    if str(item.uom).upper() == target_uom:
        return money(gross)
    try:
        source_factor = uom_factor(product, item.uom)
    except ValidationError:
        return None
    if source_factor <= 0:
        return None
    return money(gross / source_factor * target_factor)


def _price_from_list(
    db: Session,
    price_list_id: int,
    product: Product,
    target_uom: str,
    target_factor: Decimal,
    quantity: Decimal,
) -> Decimal | None:
    """
    Best applicable line of one price list, or ``None`` if none applies.

    A line whose ``min_quantity`` the order does not reach simply does not
    apply, so resolution moves on to the next list — that is how a
    high-priority "bulk" list acts as a quantity tier without breaking the
    one-line-per-(product, uom) rule the table enforces.  Thresholds are
    compared in base units so a CASE line and a PIECE line can be judged
    against the same order.
    """
    rows = db.execute(
        select(PriceListItem).where(
            PriceListItem.price_list_id == price_list_id,
            PriceListItem.product_id == product.id,
        )
    ).scalars().all()
    if not rows:
        return None

    ordered_base = D(quantity) * target_factor
    eligible: list[tuple[int, Decimal, int, PriceListItem]] = []
    for row in rows:
        try:
            factor = uom_factor(product, row.uom)
        except ValidationError:
            continue  # the line names a unit this product no longer defines
        if factor <= 0:
            continue
        threshold_base = D(row.min_quantity) * factor
        if ordered_base < threshold_base:
            continue
        is_exact = 1 if str(row.uom).upper() == target_uom else 0
        eligible.append((is_exact, threshold_base, row.id, row))

    if not eligible:
        return None
    best = max(eligible, key=lambda entry: (entry[0], entry[1], entry[2]))[3]
    return _item_unit_price(product, best, target_uom, target_factor)


def resolve_price(
    db: Session,
    product: Product,
    *,
    uom: str | None = None,
    price_list_id: int | None = None,
    customer: Any = None,
    quantity: Decimal | None = None,
    on_date: date | None = None,
) -> tuple[Decimal, int | None]:
    """
    Unit price for one *uom* of *product*, and the price list it came from.

    Precedence, first hit wins:

    1. the explicitly requested price list (an operator override),
    2. the customer's contractually agreed list,
    3. the strongest list whose channel / customer type / region scope covers
       the customer and whose validity window contains *on_date*,
    4. the product's own ``sale_price`` scaled to the requested unit.

    A list that simply has no line for this article does not block the chain —
    resolution continues downwards, which is what makes narrow "promo" lists
    usable alongside a full catalogue list.
    """
    target_uom = str(uom or product.sales_uom or product.base_uom).strip().upper()
    target_factor = uom_factor(product, target_uom)
    want = D(quantity) if quantity is not None else ONE
    when = parse_date(on_date) or date.today()

    candidates: list[int] = []
    if price_list_id:
        explicit = db.execute(
            select(PriceList).where(
                PriceList.id == int(price_list_id), PriceList.is_deleted.is_(False)
            )
        ).scalar_one_or_none()
        if explicit is None:
            raise NotFoundError("product.price_list_not_found", params={"id": price_list_id})
        candidates.append(explicit.id)

    customer_list_id = getattr(customer, "price_list_id", None)
    if customer_list_id and customer_list_id not in candidates:
        candidates.append(int(customer_list_id))

    for scoped in _scoped_price_lists(db, customer, when):
        if scoped.id not in candidates:
            candidates.append(scoped.id)

    for candidate_id in candidates:
        price = _price_from_list(db, candidate_id, product, target_uom, target_factor, want)
        if price is not None:
            return price, candidate_id

    return money(D(product.sale_price) * target_factor), None


# ===========================================================================
# Bulk import
# ===========================================================================
_NUMERIC_IMPORT_FIELDS = _MONEY_FIELDS | _QTY_FIELDS | _PERCENT_FIELDS


def _coerce_number(value: Any) -> Any:
    """
    Accept Turkish decimal notation.

    Exports from Turkish Excel write ``1.234,56``; feeding that straight into
    ``Decimal`` silently yields zero, which would quietly wipe a price list.
    """
    if not isinstance(value, str):
        return value
    text = value.strip().replace(" ", "")
    if not text:
        return None
    if "," in text:
        text = text.replace(".", "").replace(",", ".") if "." in text else text.replace(",", ".")
    return text


def _normalise_import_row(raw: dict[str, Any]) -> dict[str, Any]:
    """Lower-case the headers, translate aliases and repair numeric notation."""
    row: dict[str, Any] = {}
    for key, value in (raw or {}).items():
        name = str(key or "").strip().lower().replace(" ", "_").replace("-", "_")
        name = IMPORT_ALIASES.get(name, name)
        if isinstance(value, str):
            value = value.strip()
            if value == "":
                value = None
        if name in _NUMERIC_IMPORT_FIELDS:
            value = _coerce_number(value)
        row[name] = value
    return row


_BOOL_TRUE = {"1", "true", "yes", "y", "evet", "e", "aktif", "x"}
_BOOL_FALSE = {"0", "false", "no", "n", "hayir", "hayır", "h", "pasif"}
#: Column -> value assumed when a row carries an unparseable flag.
_FLAG_DEFAULTS: dict[str, bool] = {
    "is_active": True,
    "is_sellable": True,
    "is_returnable": True,
    "is_lot_tracked": True,
    "is_serial_tracked": False,
}


def _coerce_bool(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _BOOL_TRUE:
        return True
    if text in _BOOL_FALSE:
        return False
    return default


def _resolve_reference(
    db: Session, model: type, *, code: Any, name: Any, explicit_id: Any
) -> int | None:
    """Find a category/brand by id, then code, then name — CSVs use all three."""
    if explicit_id:
        return int(explicit_id)
    for column, value in ((model.code, code), (model.name, name)):
        if not value:
            continue
        found = db.execute(
            select(model.id).where(
                func.lower(column) == str(value).strip().lower(), model.is_deleted.is_(False)
            ).limit(1)
        ).scalar_one_or_none()
        if found:
            return int(found)
    return None


def bulk_import(
    db: Session,
    rows: Sequence[dict[str, Any]],
    *,
    user_id: int | None = None,
    audit_kwargs: dict[str, Any] | None = None,
    create_missing_references: bool = True,
) -> dict[str, Any]:
    """
    Upsert products from loosely typed rows (CSV / Excel upload).

    Each row runs inside its own SAVEPOINT so a single bad line is reported and
    skipped instead of poisoning the whole batch — an import of 5 000 articles
    must not fail because row 4 712 has a typo in its VAT rate.
    """
    requested = len(rows or [])
    succeeded = 0
    failed = 0
    created = 0
    updated = 0
    errors: list[dict[str, Any]] = []

    for index, original in enumerate(rows or [], start=1):
        row = _normalise_import_row(original)
        sku = str(row.get("sku") or "").strip()
        try:
            if not sku:
                raise ValidationError("product.sku_required", params={"row": index})

            with db.begin_nested():
                payload: dict[str, Any] = {"sku": sku}
                for field in PRODUCT_FIELDS:
                    if field in row and row[field] is not None:
                        payload[field] = row[field]

                for flag, flag_default in _FLAG_DEFAULTS.items():
                    if flag in row:
                        payload[flag] = _coerce_bool(row[flag], flag_default)

                category_id = _resolve_reference(
                    db, ProductCategory,
                    code=row.get("category_code"), name=row.get("category_name"),
                    explicit_id=row.get("category_id"),
                )
                if category_id is None and create_missing_references and row.get("category_code"):
                    category_id = create_category(
                        db, {"name": str(row.get("category_name") or row["category_code"]),
                             "code": str(row["category_code"])},
                        user_id=user_id, audit_kwargs=audit_kwargs,
                    ).id
                if category_id:
                    payload["category_id"] = category_id

                brand_id = _resolve_reference(
                    db, Brand,
                    code=row.get("brand_code"), name=row.get("brand_name"),
                    explicit_id=row.get("brand_id"),
                )
                if brand_id is None and create_missing_references and row.get("brand_code"):
                    brand_id = create_brand(
                        db, {"name": str(row.get("brand_name") or row["brand_code"]),
                             "code": str(row["brand_code"])},
                        user_id=user_id, audit_kwargs=audit_kwargs,
                    ).id
                if brand_id:
                    payload["brand_id"] = brand_id

                barcode = str(row.get("barcode") or "").strip()
                existing = get_product_by_sku(db, sku, include_deleted=True)

                if existing is None:
                    payload.setdefault("name", sku)
                    if barcode:
                        payload["barcodes"] = [
                            {"barcode": barcode, "uom": payload.get("base_uom") or str(UnitOfMeasure.PIECE),
                             "is_primary": True}
                        ]
                    create_product(db, payload, user_id=user_id, audit_kwargs=audit_kwargs)
                    created += 1
                else:
                    if existing.is_deleted:
                        existing.is_deleted = False
                        existing.deleted_at = None
                        existing.deleted_by_id = None
                    if barcode and barcode not in {b.barcode for b in existing.barcodes or ()}:
                        payload["barcodes"] = [
                            {"barcode": b.barcode, "uom": b.uom, "is_primary": b.is_primary, "label": b.label}
                            for b in existing.barcodes or ()
                        ] + [{"barcode": barcode, "uom": existing.base_uom, "is_primary": not existing.barcodes}]
                    payload.pop("sku", None)
                    update_product(db, existing.id, payload, user_id=user_id, audit_kwargs=audit_kwargs)
                    updated += 1
            succeeded += 1
        except AppError as exc:
            failed += 1
            errors.append(
                {"row": index, "sku": sku, "error": exc.message_key, "params": exc.params}
            )
        except Exception as exc:  # noqa: BLE001 — one bad row must not abort the batch
            failed += 1
            log.warning("product import row %s failed: %s", index, exc)
            errors.append(
                {"row": index, "sku": sku, "error": "product.import_failed",
                 "params": {"detail": str(exc)[:200]}}
            )

    _audit(
        db,
        AuditAction.CREATE,
        user_id=user_id,
        audit_kwargs=audit_kwargs,
        entity_type="Product",
        entity_label="bulk-import",
        summary=(
            f"product.bulk_import requested={requested} created={created} "
            f"updated={updated} failed={failed}"
        ),
        new_values={"requested": requested, "created": created, "updated": updated, "failed": failed},
    )

    return {
        "requested": requested,
        "succeeded": succeeded,
        "failed": failed,
        "created": created,
        "updated": updated,
        "errors": errors[:200],
    }


# ===========================================================================
# Export
# ===========================================================================
EXPORT_COLUMNS: tuple[str, ...] = (
    "sku", "code", "name", "name_en", "short_name", "category_code", "brand_code",
    "status", "is_active", "base_uom", "sales_uom", "units_per_case",
    "purchase_price", "cost_price", "sale_price", "recommended_retail_price",
    "currency", "vat_rate", "excise_rate", "max_discount_percent",
    "min_stock_level", "is_sellable", "is_lot_tracked", "shelf_life_days",
    "storage_condition", "barcode", "tags",
)


def export_row(product: Product) -> list[str]:
    """One CSV line, in :data:`EXPORT_COLUMNS` order."""
    def text(value: Any) -> str:
        return "" if value is None else str(value)

    return [
        text(product.sku),
        text(product.code),
        text(product.name),
        text(product.name_en),
        text(product.short_name),
        text(product.category.code if product.category else None),
        text(product.brand.code if product.brand else None),
        text(product.status),
        "1" if product.is_active else "0",
        text(product.base_uom),
        text(product.sales_uom),
        text(product.units_per_case),
        text(product.purchase_price),
        text(product.cost_price),
        text(product.sale_price),
        text(product.recommended_retail_price),
        text(product.currency),
        text(product.vat_rate),
        text(product.excise_rate),
        text(product.max_discount_percent),
        text(product.min_stock_level),
        "1" if product.is_sellable else "0",
        "1" if product.is_lot_tracked else "0",
        text(product.shelf_life_days),
        text(product.storage_condition),
        text(primary_barcode(product)),
        text(product.tags),
    ]
