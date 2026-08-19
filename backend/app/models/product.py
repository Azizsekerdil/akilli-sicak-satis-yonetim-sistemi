"""Products, brands, categories, units of measure, barcodes and price lists."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import ProductStatus, StorageCondition, UnitOfMeasure
from app.models.base import (
    AuthorMixin,
    Base,
    CodeNameMixin,
    Money,
    Quantity,
    SoftDeleteMixin,
    TimestampMixin,
    fk,
    pk,
)


class Brand(Base, CodeNameMixin, TimestampMixin, SoftDeleteMixin, AuthorMixin):
    """Brand / sub-brand (self-referential for sub-brands)."""

    __tablename__ = "brands"
    __table_args__ = (UniqueConstraint("code", name="uq_brands_code"),)

    id: Mapped[int] = pk()
    parent_id: Mapped[int | None] = fk("brands.id", nullable=True, ondelete="SET NULL")
    logo_path: Mapped[str | None] = mapped_column(String(512))
    manufacturer: Mapped[str | None] = mapped_column(String(255))

    parent: Mapped["Brand | None"] = relationship("Brand", back_populates="children", remote_side="Brand.id")
    children: Mapped[list["Brand"]] = relationship("Brand", back_populates="parent")


class ProductCategory(Base, CodeNameMixin, TimestampMixin, SoftDeleteMixin, AuthorMixin):
    """Hierarchical product category."""

    __tablename__ = "product_categories"
    __table_args__ = (UniqueConstraint("code", name="uq_product_categories_code"),)

    id: Mapped[int] = pk()
    parent_id: Mapped[int | None] = fk("product_categories.id", nullable=True, ondelete="SET NULL")
    level: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    icon: Mapped[str | None] = mapped_column(String(64))
    color: Mapped[str | None] = mapped_column(String(16))

    parent: Mapped["ProductCategory | None"] = relationship(
        "ProductCategory", back_populates="children", remote_side="ProductCategory.id"
    )
    children: Mapped[list["ProductCategory"]] = relationship(
        "ProductCategory", back_populates="parent"
    )


class Product(Base, CodeNameMixin, TimestampMixin, SoftDeleteMixin, AuthorMixin):
    """
    A sellable SKU.

    Quantities are always stored in the **base unit** (``base_uom``).  The
    ``units`` collection defines conversions such as 1 CASE = 24 PIECE, which
    lets field staff work in cases while the ledger stays consistent.
    """

    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("sku", name="uq_products_sku"),
        Index("ix_products_category_brand", "category_id", "brand_id"),
        Index("ix_products_status_active", "status", "is_active"),
    )

    id: Mapped[int] = pk()
    sku: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    category_id: Mapped[int | None] = fk("product_categories.id", nullable=True, ondelete="SET NULL")
    brand_id: Mapped[int | None] = fk("brands.id", nullable=True, ondelete="SET NULL")

    short_name: Mapped[str | None] = mapped_column(String(96))
    #: ASCII-folded "sku name short_name" for Turkish-safe search — see
    #: Customer.search_key for why SQLite needs this.
    search_key: Mapped[str | None] = mapped_column(String(600), index=True)
    status: Mapped[str] = mapped_column(String(16), default=ProductStatus.ACTIVE, nullable=False, index=True)

    # --- Units -------------------------------------------------------------
    base_uom: Mapped[str] = mapped_column(String(16), default=UnitOfMeasure.PIECE, nullable=False)
    sales_uom: Mapped[str] = mapped_column(String(16), default=UnitOfMeasure.CASE, nullable=False)
    #: How many base units in one sales unit (e.g. 24 pieces per case).
    units_per_case: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("1"), nullable=False)

    # --- Physical ----------------------------------------------------------
    unit_volume_l: Mapped[float | None] = mapped_column(Float)      # per base unit
    unit_weight_kg: Mapped[float | None] = mapped_column(Float)     # per base unit
    case_volume_l: Mapped[float | None] = mapped_column(Float)
    case_weight_kg: Mapped[float | None] = mapped_column(Float)
    storage_condition: Mapped[str] = mapped_column(
        String(16), default=StorageCondition.AMBIENT, nullable=False
    )

    # --- Shelf life --------------------------------------------------------
    is_lot_tracked: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_serial_tracked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    shelf_life_days: Mapped[int | None] = mapped_column(Integer)
    min_remaining_shelf_life_days: Mapped[int | None] = mapped_column(Integer)

    # --- Commercial --------------------------------------------------------
    purchase_price: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    cost_price: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    sale_price: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    recommended_retail_price: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="TRY", nullable=False)
    vat_rate: Mapped[float] = mapped_column(Float, default=20.0, nullable=False)
    #: Special consumption tax (ÖTV) — infrastructure for regulated goods.
    excise_rate: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    excise_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    max_discount_percent: Mapped[float] = mapped_column(Float, default=100.0, nullable=False)

    # --- Planning ----------------------------------------------------------
    min_stock_level: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("0"), nullable=False)
    max_stock_level: Mapped[Decimal | None] = mapped_column(Quantity)
    reorder_point: Mapped[Decimal | None] = mapped_column(Quantity)
    is_sellable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_returnable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    image_path: Mapped[str | None] = mapped_column(String(512))
    tags: Mapped[str | None] = mapped_column(Text)  # comma-separated

    category: Mapped["ProductCategory | None"] = relationship(lazy="joined")
    brand: Mapped["Brand | None"] = relationship(lazy="joined")
    barcodes: Mapped[list["Barcode"]] = relationship(
        back_populates="product", cascade="all, delete-orphan", lazy="selectin"
    )
    units: Mapped[list["ProductUnit"]] = relationship(
        back_populates="product", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def margin_percent(self) -> float:
        """Gross margin on the list sale price."""
        if not self.sale_price or self.sale_price == 0:
            return 0.0
        return float((self.sale_price - self.cost_price) / self.sale_price * 100)


class ProductUnit(Base, TimestampMixin):
    """
    A purchasable/sellable packaging of a product.

    ``factor`` is how many **base units** one of this unit contains.
    Example: base=PIECE, unit=CASE, factor=24.
    """

    __tablename__ = "product_units"
    __table_args__ = (
        UniqueConstraint("product_id", "uom", name="uq_product_units_product_uom"),
    )

    id: Mapped[int] = pk()
    product_id: Mapped[int] = fk("products.id", ondelete="CASCADE")
    uom: Mapped[str] = mapped_column(String(16), nullable=False)
    factor: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    barcode: Mapped[str | None] = mapped_column(String(64), index=True)
    price: Mapped[Decimal | None] = mapped_column(Money)
    is_default_sales_unit: Mapped[bool] = mapped_column(Boolean, default=False)
    volume_l: Mapped[float | None] = mapped_column(Float)
    weight_kg: Mapped[float | None] = mapped_column(Float)

    product: Mapped["Product"] = relationship(back_populates="units")


class Barcode(Base, TimestampMixin):
    """EAN/UPC/case barcode.  A product may have several."""

    __tablename__ = "barcodes"
    __table_args__ = (UniqueConstraint("barcode", name="uq_barcodes_barcode"),)

    id: Mapped[int] = pk()
    product_id: Mapped[int] = fk("products.id", ondelete="CASCADE")
    barcode: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    uom: Mapped[str] = mapped_column(String(16), default=UnitOfMeasure.PIECE, nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    label: Mapped[str | None] = mapped_column(String(64))

    product: Mapped["Product"] = relationship(back_populates="barcodes")


class PriceList(Base, CodeNameMixin, TimestampMixin, SoftDeleteMixin, AuthorMixin):
    """A named set of prices, optionally scoped to a channel/region/customer type."""

    __tablename__ = "price_lists"
    __table_args__ = (UniqueConstraint("code", name="uq_price_lists_code"),)

    id: Mapped[int] = pk()
    currency: Mapped[str] = mapped_column(String(8), default="TRY", nullable=False)
    valid_from: Mapped[date | None] = mapped_column(Date, index=True)
    valid_to: Mapped[date | None] = mapped_column(Date, index=True)
    channel: Mapped[str | None] = mapped_column(String(32), index=True)
    customer_type: Mapped[str | None] = mapped_column(String(32), index=True)
    region_id: Mapped[int | None] = fk("regions.id", nullable=True, ondelete="SET NULL")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)

    items: Mapped[list["PriceListItem"]] = relationship(
        back_populates="price_list", cascade="all, delete-orphan"
    )


class PriceListItem(Base, TimestampMixin):
    """One product's price within a price list."""

    __tablename__ = "price_list_items"
    __table_args__ = (
        UniqueConstraint("price_list_id", "product_id", "uom", name="uq_price_list_items_pl_prod_uom"),
    )

    id: Mapped[int] = pk()
    price_list_id: Mapped[int] = fk("price_lists.id", ondelete="CASCADE")
    product_id: Mapped[int] = fk("products.id", ondelete="CASCADE")
    uom: Mapped[str] = mapped_column(String(16), default=UnitOfMeasure.CASE, nullable=False)
    price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    min_quantity: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("0"), nullable=False)
    discount_percent: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    price_list: Mapped["PriceList"] = relationship(back_populates="items")
    product: Mapped["Product"] = relationship(lazy="joined")


# ---------------------------------------------------------------------------
# search_key maintenance
# ---------------------------------------------------------------------------
@event.listens_for(Product, "before_insert")
@event.listens_for(Product, "before_update")
def _product_search_key(_mapper, _connection, target: Product) -> None:
    """Keep the ASCII-folded search column in step — see Customer.search_key."""
    from app.core.utils import slugify

    parts = [target.sku or "", target.code or "", target.name or "", target.short_name or ""]
    target.search_key = slugify(" ".join(p for p in parts if p), sep=" ")[:600]
