"""
Product catalogue endpoints: products, units of measure, barcodes, category
and brand trees, and price lists.

Route order matters.  FastAPI matches in declaration order, so every literal
path (``/categories``, ``/brands``, ``/price-lists``, ``/export``, ``/import``,
``/barcode/{code}``) is declared **before** ``/{product_id}`` — otherwise the
router would try to parse "categories" as an integer id and answer 422.
"""

from __future__ import annotations

import csv
import io
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from app.core.deps import Ctx, Page, get_page, paginated, require, require_any
from app.core.enums import AuditAction
from app.core.exceptions import NotFoundError
from app.core.i18n import t
from app.models.customer import Customer
from app.schemas.common import Message, PagedResponse
from app.schemas.product import (
    BrandCreate,
    BrandOut,
    BrandUpdate,
    CategoryCreate,
    CategoryOut,
    CategoryUpdate,
    PriceListCreate,
    PriceListItemOut,
    PriceListItemsReplace,
    PriceListItemsResult,
    PriceListOut,
    PriceListUpdate,
    ProductCreate,
    ProductImportRequest,
    ProductImportResult,
    ProductListItem,
    ProductOut,
    ProductSearchResult,
    ProductUpdate,
    ResolvedPrice,
    UomOption,
)
from app.services import audit_service, product_service

router = APIRouter(prefix="/products", tags=["products"])

#: Hard ceiling for a CSV export — the whole result is materialised in memory
#: before streaming, because the request-scoped session closes when the handler
#: returns and a lazy generator would then be reading from a dead session.
MAX_EXPORT_ROWS = 20_000

VIEW_PRICE_LISTS = require_any(("marketing.price_lists", "VIEW"), ("stock.products", "VIEW"))


# ===========================================================================
# Catalogue search
# ===========================================================================
@router.get("", response_model=PagedResponse[ProductListItem], summary="Search products")
def list_products(
    page: Page = Depends(get_page),
    ctx: Ctx = Depends(require("stock.products", "VIEW")),
    q: str | None = Query(default=None, description="SKU, name, tag or barcode"),
    category_id: int | None = Query(default=None),
    brand_id: int | None = Query(default=None),
    status: str | None = Query(default=None),
    is_active: bool | None = Query(default=None),
    only_sellable: bool = Query(default=False),
    include_deleted: bool = Query(default=False),
    order_by: str = Query(default="name", pattern="^(name|sku|price|created)$"),
) -> dict[str, Any]:
    """
    Paged catalogue.

    Product master data is company-wide, so no salesperson scoping applies —
    the only scope-sensitive switch is ``include_deleted``, which is reserved
    for users whose data scope is unrestricted.
    """
    items, total = product_service.search(
        ctx.db,
        term=q,
        category_id=category_id,
        brand_id=brand_id,
        status=status,
        is_active=is_active,
        only_sellable=only_sellable,
        page=page.page,
        size=page.size,
        include_deleted=include_deleted and ctx.unrestricted,
        order_by=order_by,
    )
    return paginated([ProductListItem.model_validate(p) for p in items], total, page)


@router.get("/export", summary="Export the catalogue as CSV")
def export_products(
    ctx: Ctx = Depends(require("stock.products", "EXPORT")),
    q: str | None = Query(default=None),
    category_id: int | None = Query(default=None),
    brand_id: int | None = Query(default=None),
    status: str | None = Query(default=None),
    is_active: bool | None = Query(default=None),
    only_sellable: bool = Query(default=False),
) -> StreamingResponse:
    items, total = product_service.search(
        ctx.db,
        term=q,
        category_id=category_id,
        brand_id=brand_id,
        status=status,
        is_active=is_active,
        only_sellable=only_sellable,
        page=1,
        size=MAX_EXPORT_ROWS,
        order_by="sku",
    )
    rows: list[list[str]] = [list(product_service.EXPORT_COLUMNS)]
    rows.extend(product_service.export_row(p) for p in items)

    audit_service.record(
        ctx.db,
        AuditAction.EXPORT,
        entity_type="Product",
        entity_label="catalogue",
        summary=f"product.export rows={len(items)} matched={total}",
        **ctx.audit_kwargs(),
    )
    ctx.db.commit()

    def stream() -> Any:
        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
        # Byte-order mark: without it Excel renders Turkish characters as mojibake.
        yield "\ufeff"
        for row in rows:
            writer.writerow(row)
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)

    filename = f"products-{date.today().isoformat()}.csv"
    return StreamingResponse(
        stream(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/import", response_model=ProductImportResult, summary="Bulk import / upsert products")
def import_products(
    payload: ProductImportRequest,
    ctx: Ctx = Depends(require("stock.products", "CREATE")),
) -> ProductImportResult:
    """Upsert by SKU.  Rows are independent: a bad line is reported, not fatal."""
    ctx.check("stock.products", "UPDATE")
    result = product_service.bulk_import(
        ctx.db, payload.rows, user_id=ctx.user_id, audit_kwargs=ctx.audit_kwargs()
    )
    ctx.db.commit()
    return ProductImportResult(**result)


# ===========================================================================
# Categories
# ===========================================================================
@router.get("/categories", response_model=list[CategoryOut], summary="Category tree")
def category_tree(
    ctx: Ctx = Depends(require("stock.products", "VIEW")),
    only_active: bool = Query(default=False),
) -> list[CategoryOut]:
    return [
        CategoryOut.model_validate(node)
        for node in product_service.category_tree(ctx.db, only_active=only_active)
    ]


@router.post("/categories", response_model=CategoryOut, status_code=201, summary="Create a category")
def create_category(
    payload: CategoryCreate,
    ctx: Ctx = Depends(require("stock.products", "CREATE")),
) -> CategoryOut:
    category = product_service.create_category(
        ctx.db, payload.model_dump(exclude_unset=True),
        user_id=ctx.user_id, audit_kwargs=ctx.audit_kwargs(),
    )
    ctx.db.commit()
    return CategoryOut.model_validate(product_service.category_payload(category))


@router.put("/categories/{category_id}", response_model=CategoryOut, summary="Update a category")
def update_category(
    category_id: int,
    payload: CategoryUpdate,
    ctx: Ctx = Depends(require("stock.products", "UPDATE")),
) -> CategoryOut:
    category = product_service.update_category(
        ctx.db, category_id, payload.model_dump(exclude_unset=True),
        user_id=ctx.user_id, audit_kwargs=ctx.audit_kwargs(),
    )
    ctx.db.commit()
    return CategoryOut.model_validate(product_service.category_payload(category))


@router.delete("/categories/{category_id}", response_model=Message, summary="Delete a category")
def delete_category(
    category_id: int,
    ctx: Ctx = Depends(require("stock.products", "DELETE")),
) -> Message:
    product_service.delete_category(
        ctx.db, category_id, user_id=ctx.user_id, audit_kwargs=ctx.audit_kwargs()
    )
    ctx.db.commit()
    return Message(message=t("common.deleted", ctx.lang), message_key="common.deleted")


# ===========================================================================
# Brands
# ===========================================================================
@router.get("/brands", response_model=list[BrandOut], summary="Brand tree")
def brand_tree(
    ctx: Ctx = Depends(require("stock.products", "VIEW")),
    only_active: bool = Query(default=False),
) -> list[BrandOut]:
    return [
        BrandOut.model_validate(node)
        for node in product_service.brand_tree(ctx.db, only_active=only_active)
    ]


@router.post("/brands", response_model=BrandOut, status_code=201, summary="Create a brand")
def create_brand(
    payload: BrandCreate,
    ctx: Ctx = Depends(require("stock.products", "CREATE")),
) -> BrandOut:
    brand = product_service.create_brand(
        ctx.db, payload.model_dump(exclude_unset=True),
        user_id=ctx.user_id, audit_kwargs=ctx.audit_kwargs(),
    )
    ctx.db.commit()
    return BrandOut.model_validate(product_service.brand_payload(brand))


@router.put("/brands/{brand_id}", response_model=BrandOut, summary="Update a brand")
def update_brand(
    brand_id: int,
    payload: BrandUpdate,
    ctx: Ctx = Depends(require("stock.products", "UPDATE")),
) -> BrandOut:
    brand = product_service.update_brand(
        ctx.db, brand_id, payload.model_dump(exclude_unset=True),
        user_id=ctx.user_id, audit_kwargs=ctx.audit_kwargs(),
    )
    ctx.db.commit()
    return BrandOut.model_validate(product_service.brand_payload(brand))


@router.delete("/brands/{brand_id}", response_model=Message, summary="Delete a brand")
def delete_brand(
    brand_id: int,
    ctx: Ctx = Depends(require("stock.products", "DELETE")),
) -> Message:
    product_service.delete_brand(
        ctx.db, brand_id, user_id=ctx.user_id, audit_kwargs=ctx.audit_kwargs()
    )
    ctx.db.commit()
    return Message(message=t("common.deleted", ctx.lang), message_key="common.deleted")


# ===========================================================================
# Price lists
# ===========================================================================
@router.get("/price-lists", response_model=list[PriceListOut], summary="Price lists")
def list_price_lists(
    ctx: Ctx = Depends(VIEW_PRICE_LISTS),
    is_active: bool | None = Query(default=None),
    channel: str | None = Query(default=None),
    customer_type: str | None = Query(default=None),
    region_id: int | None = Query(default=None),
    on_date: date | None = Query(default=None, description="Only lists valid on this date"),
) -> list[PriceListOut]:
    rows = product_service.list_price_lists(
        ctx.db,
        is_active=is_active,
        channel=channel,
        customer_type=customer_type,
        region_id=region_id,
        on_date=on_date,
    )
    return [PriceListOut.model_validate(row) for row in rows]


@router.post(
    "/price-lists", response_model=PriceListOut, status_code=201, summary="Create a price list"
)
def create_price_list(
    payload: PriceListCreate,
    ctx: Ctx = Depends(require("marketing.price_lists", "CREATE")),
) -> PriceListOut:
    price_list = product_service.create_price_list(
        ctx.db, payload.model_dump(exclude_unset=True),
        user_id=ctx.user_id, audit_kwargs=ctx.audit_kwargs(),
    )
    ctx.db.commit()
    return PriceListOut.model_validate(product_service.price_list_payload(ctx.db, price_list))


@router.put(
    "/price-lists/{price_list_id}", response_model=PriceListOut, summary="Update a price list"
)
def update_price_list(
    price_list_id: int,
    payload: PriceListUpdate,
    ctx: Ctx = Depends(require("marketing.price_lists", "UPDATE")),
) -> PriceListOut:
    price_list = product_service.update_price_list(
        ctx.db, price_list_id, payload.model_dump(exclude_unset=True),
        user_id=ctx.user_id, audit_kwargs=ctx.audit_kwargs(),
    )
    ctx.db.commit()
    return PriceListOut.model_validate(product_service.price_list_payload(ctx.db, price_list))


@router.delete(
    "/price-lists/{price_list_id}", response_model=Message, summary="Delete a price list"
)
def delete_price_list(
    price_list_id: int,
    ctx: Ctx = Depends(require("marketing.price_lists", "DELETE")),
) -> Message:
    product_service.delete_price_list(
        ctx.db, price_list_id, user_id=ctx.user_id, audit_kwargs=ctx.audit_kwargs()
    )
    ctx.db.commit()
    return Message(message=t("common.deleted", ctx.lang), message_key="common.deleted")


@router.get(
    "/price-lists/{price_list_id}/items",
    response_model=PagedResponse[PriceListItemOut],
    summary="Lines of a price list",
)
def get_price_list_items(
    price_list_id: int,
    page: Page = Depends(get_page),
    ctx: Ctx = Depends(VIEW_PRICE_LISTS),
    q: str | None = Query(default=None, description="Filter by product SKU or name"),
    product_id: int | None = Query(default=None),
) -> dict[str, Any]:
    items, total = product_service.price_list_items(
        ctx.db, price_list_id, term=q, product_id=product_id, page=page.page, size=page.size
    )
    return paginated([PriceListItemOut.model_validate(row) for row in items], total, page)


@router.put(
    "/price-lists/{price_list_id}/items",
    response_model=PriceListItemsResult,
    summary="Write the lines of a price list",
)
def put_price_list_items(
    price_list_id: int,
    payload: PriceListItemsReplace,
    ctx: Ctx = Depends(require("marketing.price_lists", "UPDATE")),
) -> PriceListItemsResult:
    result = product_service.upsert_price_list_items(
        ctx.db,
        price_list_id,
        [item.model_dump() for item in payload.items],
        replace=payload.replace,
        user_id=ctx.user_id,
        audit_kwargs=ctx.audit_kwargs(),
    )
    ctx.db.commit()
    return PriceListItemsResult(**result)


# ===========================================================================
# Barcode lookup
# ===========================================================================
@router.get(
    "/barcode/{barcode}", response_model=ProductSearchResult, summary="Resolve a scanned barcode"
)
def get_by_barcode(
    barcode: str,
    ctx: Ctx = Depends(require("stock.products", "VIEW")),
) -> ProductSearchResult:
    product = product_service.find_by_barcode(ctx.db, barcode)
    if product is None:
        raise NotFoundError("product.not_found", params={"barcode": barcode})
    payload = product_service.search_result(product)
    payload["barcode"] = barcode
    return ProductSearchResult.model_validate(payload)


# ===========================================================================
# Single product
# ===========================================================================
@router.post("", response_model=ProductOut, status_code=201, summary="Create a product")
def create_product(
    payload: ProductCreate,
    ctx: Ctx = Depends(require("stock.products", "CREATE")),
) -> ProductOut:
    product = product_service.create_product(
        ctx.db, payload.model_dump(exclude_unset=True),
        user_id=ctx.user_id, audit_kwargs=ctx.audit_kwargs(),
    )
    ctx.db.commit()
    return ProductOut.model_validate(product)


@router.get("/{product_id}", response_model=ProductOut, summary="Product detail")
def get_product(
    product_id: int,
    ctx: Ctx = Depends(require("stock.products", "VIEW")),
) -> ProductOut:
    return ProductOut.model_validate(product_service.get_product(ctx.db, product_id))


@router.get(
    "/{product_id}/uoms", response_model=list[UomOption], summary="Units this product is sold in"
)
def get_product_uoms(
    product_id: int,
    ctx: Ctx = Depends(require("stock.products", "VIEW")),
) -> list[UomOption]:
    product = product_service.get_product(ctx.db, product_id)
    return [UomOption.model_validate(option) for option in product_service.available_uoms(product)]


@router.get("/{product_id}/price", response_model=ResolvedPrice, summary="Resolve a unit price")
def get_product_price(
    product_id: int,
    ctx: Ctx = Depends(require("stock.products", "VIEW")),
    uom: str | None = Query(default=None),
    price_list_id: int | None = Query(default=None),
    customer_id: int | None = Query(default=None),
    quantity: Decimal = Query(default=Decimal("1"), gt=0),
    on_date: date | None = Query(default=None),
) -> ResolvedPrice:
    """What one unit costs for this customer today — the price the van shows."""
    product = product_service.get_product(ctx.db, product_id)
    customer = ctx.db.get(Customer, customer_id) if customer_id else None
    if customer_id and (customer is None or customer.is_deleted):
        raise NotFoundError("customer.not_found", params={"id": customer_id})

    unit_price, used_list_id = product_service.resolve_price(
        ctx.db,
        product,
        uom=uom,
        price_list_id=price_list_id,
        customer=customer,
        quantity=quantity,
        on_date=on_date,
    )
    return ResolvedPrice(
        product_id=product.id,
        uom=(uom or product.sales_uom or product.base_uom).upper(),
        quantity=quantity,
        unit_price=unit_price,
        price_list_id=used_list_id,
        currency=product.currency,
    )


@router.put("/{product_id}", response_model=ProductOut, summary="Update a product")
def update_product(
    product_id: int,
    payload: ProductUpdate,
    ctx: Ctx = Depends(require("stock.products", "UPDATE")),
) -> ProductOut:
    data = payload.model_dump(exclude_unset=True)
    if "units" in data and data["units"] is None:
        data.pop("units")
    if "barcodes" in data and data["barcodes"] is None:
        data.pop("barcodes")
    product = product_service.update_product(
        ctx.db, product_id, data, user_id=ctx.user_id, audit_kwargs=ctx.audit_kwargs()
    )
    ctx.db.commit()
    return ProductOut.model_validate(product)


@router.delete("/{product_id}", response_model=Message, summary="Delete (deactivate) a product")
def delete_product(
    product_id: int,
    ctx: Ctx = Depends(require("stock.products", "DELETE")),
) -> Message:
    product_service.delete_product(
        ctx.db, product_id, user_id=ctx.user_id, audit_kwargs=ctx.audit_kwargs()
    )
    ctx.db.commit()
    return Message(message=t("common.deleted", ctx.lang), message_key="common.deleted")
