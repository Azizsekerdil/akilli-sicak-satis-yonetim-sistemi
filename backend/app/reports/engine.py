"""
Report engine.

A report is a :class:`ReportDef` — metadata (bilingual title, typed columns,
declared filters) plus a *runner* that performs a real aggregation against the
operational tables.  Keeping the definition and the query together means the
frontend, the exporters and the AI reporting agent all describe a report the
same way: one registry, one shape of result.

Every runner returns plain ``dict`` rows keyed by column key, so the exporters
(CSV / Excel / PDF / JSON) never need to know anything about the domain.

All SQL here is portable between SQLite and PostgreSQL: no JSONB, no ARRAY, no
ILIKE, and datetime columns are bucketed with ``func.date()`` which both
backends implement.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.enums import (
    LedgerEntryType,
    PaymentMethod,
    PaymentStatus,
    StockMovementType,
    StockStatus,
    TargetSubject,
    WarehouseType,
)
from app.core.exceptions import NotFoundError
from app.core.utils import D, money, month_start, parse_date, pct, qty
from app.models.analytics import Target
from app.models.campaign import Campaign, CampaignApplication
from app.models.customer import Customer, CustomerLedger
from app.models.organization import Region
from app.models.product import Brand, Product, ProductCategory
from app.models.route import Route, Visit
from app.models.sales import (
    Payment,
    ReturnDocument,
    ReturnItem,
    Sale,
    SaleItem,
)
from app.models.vehicle import Salesperson, Vehicle
from app.models.warehouse import Lot, StockBalance, StockMovement, Warehouse

# ===========================================================================
# Definition objects
# ===========================================================================
#: Column value types understood by every exporter.
COLUMN_TYPES = ("text", "integer", "number", "money", "quantity", "percent", "date")


@dataclass(frozen=True)
class ColumnDef:
    """One output column: how to label it, how to format it, how wide it is."""

    key: str
    label_tr: str
    label_en: str
    type: str = "text"
    width: int = 16
    align: str = "left"

    def label(self, lang: str = "tr") -> str:
        return self.label_en if lang == "en" else self.label_tr

    def as_dict(self, lang: str | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "key": self.key,
            "label_tr": self.label_tr,
            "label_en": self.label_en,
            "type": self.type,
            "width": self.width,
            "align": self.align,
        }
        if lang:
            out["label"] = self.label(lang)
        return out


@dataclass(frozen=True)
class FilterDef:
    """A parameter the UI should render before running the report."""

    key: str
    label_tr: str
    label_en: str
    type: str = "date"          # date | select | int | text | bool
    required: bool = False
    default: Any = None
    #: Endpoint/enum the UI can read option values from.
    source: str | None = None

    def as_dict(self, lang: str | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "key": self.key,
            "label_tr": self.label_tr,
            "label_en": self.label_en,
            "type": self.type,
            "required": self.required,
            "default": self.default,
            "source": self.source,
        }
        if lang:
            out["label"] = self.label_en if lang == "en" else self.label_tr
        return out


def _num(key: str, tr: str, en: str, kind: str = "money", width: int = 14) -> ColumnDef:
    return ColumnDef(key, tr, en, kind, width, "right")


def _txt(key: str, tr: str, en: str, width: int = 18) -> ColumnDef:
    return ColumnDef(key, tr, en, "text", width, "left")


def _dat(key: str, tr: str, en: str, width: int = 12) -> ColumnDef:
    return ColumnDef(key, tr, en, "date", width, "center")


# --- Standard filters ------------------------------------------------------
F_START = FilterDef("start", "Başlangıç Tarihi", "Start Date", "date", True)
F_END = FilterDef("end", "Bitiş Tarihi", "End Date", "date", True)
F_SALESPERSON = FilterDef("salesperson_id", "Plasiyer", "Salesperson", "select", False, None, "salespersons")
F_CUSTOMER = FilterDef("customer_id", "Müşteri", "Customer", "select", False, None, "customers")
F_REGION = FilterDef("region_id", "Bölge", "Region", "select", False, None, "regions")
F_ROUTE = FilterDef("route_id", "Rota", "Route", "select", False, None, "routes")
F_WAREHOUSE = FilterDef("warehouse_id", "Depo", "Warehouse", "select", False, None, "warehouses")
F_PRODUCT = FilterDef("product_id", "Ürün", "Product", "select", False, None, "products")
F_CATEGORY = FilterDef("category_id", "Kategori", "Category", "select", False, None, "categories")
F_BRAND = FilterDef("brand_id", "Marka", "Brand", "select", False, None, "brands")
F_LIMIT = FilterDef("limit", "Satır Sayısı", "Row Limit", "int", False, 200)


@dataclass(frozen=True)
class ReportScope:
    """
    The caller's data visibility, flattened into something a query can use.

    ``unrestricted`` short-circuits every filter; otherwise ``salesperson_ids``
    limits sales/collections and ``region_ids`` limits customer-anchored data.
    """

    unrestricted: bool = True
    salesperson_ids: tuple[int, ...] = ()
    region_ids: tuple[int, ...] = ()
    user_id: int | None = None

    def sales_filter(self) -> tuple[int, ...] | None:
        """Salesperson ids to restrict to, or None when unrestricted."""
        if self.unrestricted or not self.salesperson_ids:
            return None
        return self.salesperson_ids


def scope_from(obj: Any) -> ReportScope:
    """
    Build a :class:`ReportScope` from a ``Ctx``, a dict, or nothing.

    Accepting several shapes keeps the engine usable from the API layer, from
    the AI reporting agent and from scheduled jobs without a hard dependency
    on FastAPI's request context.
    """
    if obj is None:
        return ReportScope()
    if isinstance(obj, ReportScope):
        return obj
    if isinstance(obj, dict):
        return ReportScope(
            unrestricted=bool(obj.get("unrestricted", True)),
            salesperson_ids=tuple(obj.get("salesperson_ids") or ()),
            region_ids=tuple(obj.get("region_ids") or ()),
            user_id=obj.get("user_id"),
        )
    return ReportScope(
        unrestricted=bool(getattr(obj, "unrestricted", True)),
        salesperson_ids=tuple(getattr(obj, "salesperson_ids", ()) or ()),
        region_ids=tuple(getattr(obj, "region_ids", ()) or ()),
        user_id=getattr(obj, "user_id", None),
    )


class ReportParams:
    """Type-coercing accessor over the raw parameter dict sent by the client."""

    def __init__(self, raw: dict[str, Any] | None = None) -> None:
        self.raw: dict[str, Any] = {k: v for k, v in (raw or {}).items() if v not in ("", None)}

    def date(self, key: str, default: date | None = None) -> date | None:
        return parse_date(self.raw.get(key)) or default

    def int_(self, key: str, default: int | None = None) -> int | None:
        value = self.raw.get(key)
        if value is None:
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def str_(self, key: str, default: str | None = None) -> str | None:
        value = self.raw.get(key)
        return str(value).strip() if value is not None else default

    def bool_(self, key: str, default: bool = False) -> bool:
        value = self.raw.get(key)
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "evet", "on")

    def limit(self, default: int = 200, hard_max: int = 5000) -> int:
        return max(1, min(hard_max, self.int_("limit", default) or default))

    def range(self) -> tuple[date, date]:
        """Date window, defaulting to month-to-date; reversed input is corrected."""
        today = date.today()
        start = self.date("start") or month_start(today)
        end = self.date("end") or today
        return (end, start) if start > end else (start, end)


#: Runner contract shared by every report.
Runner = Callable[[Session, ReportParams, ReportScope], list[dict[str, Any]]]


@dataclass(frozen=True)
class ReportDef:
    """Everything needed to describe, authorise, run and export one report."""

    key: str
    title_tr: str
    title_en: str
    module: str
    columns: tuple[ColumnDef, ...]
    runner: Runner
    filters: tuple[FilterDef, ...] = ()
    group_by: str | None = None
    totals: tuple[str, ...] = ()
    #: ``{percent_key: (numerator_key, denominator_key)}`` — percentage totals
    #: must be recomputed from the summed components, never summed themselves.
    derived_totals: dict[str, tuple[str, str]] = field(default_factory=dict)
    permission: tuple[str, str] = ("analytics.reports", "VIEW")
    description_tr: str | None = None
    description_en: str | None = None

    def title(self, lang: str = "tr") -> str:
        return self.title_en if lang == "en" else self.title_tr

    def as_dict(self, lang: str = "tr") -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title(lang),
            "title_tr": self.title_tr,
            "title_en": self.title_en,
            "description": (self.description_en if lang == "en" else self.description_tr),
            "module": self.module,
            "columns": [c.as_dict(lang) for c in self.columns],
            "filters": [f.as_dict(lang) for f in self.filters],
            "group_by": self.group_by,
            "totals": list(self.totals),
            "permission": {"resource": self.permission[0], "action": self.permission[1]},
        }


# ===========================================================================
# Shared query helpers
# ===========================================================================
def _sale_conditions(p: ReportParams, scope: ReportScope) -> list[Any]:
    """
    Base predicate for every revenue report.

    Cancelled and soft-deleted sales never count; everything else that has been
    recorded in the field does, so a report run mid-day matches what the
    salesperson sees on the van.
    """
    start, end = p.range()
    conds: list[Any] = [
        Sale.sale_date >= start,
        Sale.sale_date <= end,
        Sale.is_cancelled.is_(False),
        Sale.is_deleted.is_(False),
    ]
    if (sp := p.int_("salesperson_id")) is not None:
        conds.append(Sale.salesperson_id == sp)
    if (cust := p.int_("customer_id")) is not None:
        conds.append(Sale.customer_id == cust)
    if (route := p.int_("route_id")) is not None:
        conds.append(Sale.route_id == route)
    if (wh := p.int_("warehouse_id")) is not None:
        conds.append(Sale.warehouse_id == wh)
    if (region := p.int_("region_id")) is not None:
        conds.append(Sale.customer_id.in_(select(Customer.id).where(Customer.region_id == region)))

    allowed = scope.sales_filter()
    if allowed is not None:
        conds.append(Sale.salesperson_id.in_(allowed))
    return conds


def _item_product_conditions(p: ReportParams) -> list[Any]:
    conds: list[Any] = []
    if (pid := p.int_("product_id")) is not None:
        conds.append(SaleItem.product_id == pid)
    if (cid := p.int_("category_id")) is not None:
        conds.append(Product.category_id == cid)
    if (bid := p.int_("brand_id")) is not None:
        conds.append(Product.brand_id == bid)
    return conds


def _item_select(*group_cols: Any) -> Select[Any]:
    """Item-level aggregation skeleton shared by SKU/brand/category/profit reports."""
    return (
        select(
            *group_cols,
            func.count(func.distinct(Sale.id)).label("sale_count"),
            func.count(func.distinct(Sale.customer_id)).label("customer_count"),
            func.sum(SaleItem.base_quantity).label("quantity"),
            func.sum(SaleItem.gross_amount).label("gross_amount"),
            func.sum(SaleItem.discount_amount + SaleItem.campaign_discount_amount).label("discount_amount"),
            func.sum(SaleItem.net_amount).label("net_amount"),
            func.sum(SaleItem.total_amount).label("total_amount"),
            func.sum(SaleItem.total_cost).label("total_cost"),
            func.sum(SaleItem.margin_amount).label("margin_amount"),
        )
        .select_from(SaleItem)
        .join(Sale, SaleItem.sale_id == Sale.id)
        .join(Product, SaleItem.product_id == Product.id)
    )


def _item_row(r: Any) -> dict[str, Any]:
    net = money(r.net_amount)
    cost = money(r.total_cost)
    margin = money(r.margin_amount)
    return {
        "sale_count": int(r.sale_count or 0),
        "customer_count": int(r.customer_count or 0),
        "quantity": qty(r.quantity),
        "gross_amount": money(r.gross_amount),
        "discount_amount": money(r.discount_amount),
        "net_amount": net,
        "total_amount": money(r.total_amount),
        "total_cost": cost,
        "margin_amount": margin,
        "margin_percent": pct(margin, net),
    }


def _share(rows: list[dict[str, Any]], value_key: str, share_key: str = "share_percent") -> None:
    """Add each row's share of the column total — mutates in place."""
    total = sum((D(r.get(value_key)) for r in rows), Decimal("0"))
    for r in rows:
        r[share_key] = pct(r.get(value_key), total)


# ===========================================================================
# Sales by period (daily / weekly / monthly / yearly)
# ===========================================================================
_PERIOD_COLUMNS: tuple[ColumnDef, ...] = (
    _txt("period", "Dönem", "Period", 14),
    _dat("bucket_date", "Tarih", "Date"),
    _num("sale_count", "Satış Adedi", "Sales", "integer", 10),
    _num("customer_count", "Müşteri", "Customers", "integer", 10),
    _num("gross_amount", "Brüt Tutar", "Gross"),
    _num("discount_amount", "İskonto", "Discount"),
    _num("net_amount", "Net Tutar", "Net"),
    _num("vat_amount", "KDV", "VAT"),
    _num("total_amount", "Toplam", "Total"),
    _num("paid_amount", "Tahsil Edilen", "Collected"),
    _num("total_cost", "Maliyet", "Cost"),
    _num("margin_amount", "Kâr", "Margin"),
    _num("margin_percent", "Kâr %", "Margin %", "percent", 10),
    _num("avg_basket", "Ort. Sepet", "Avg Basket"),
)

_PERIOD_TOTALS = (
    "sale_count", "customer_count", "gross_amount", "discount_amount", "net_amount",
    "vat_amount", "total_amount", "paid_amount", "total_cost", "margin_amount",
    "margin_percent",
)


def _bucket_key(d: date, bucket: str) -> tuple[str, date]:
    """Map a date onto its (label, bucket start date) for the requested grain."""
    if bucket == "WEEKLY":
        monday = d - timedelta(days=d.weekday())
        iso = d.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}", monday
    if bucket == "MONTHLY":
        return f"{d.year}-{d.month:02d}", date(d.year, d.month, 1)
    if bucket == "YEARLY":
        return str(d.year), date(d.year, 1, 1)
    return d.isoformat(), d


def _run_sales_by_period(
    db: Session, p: ReportParams, scope: ReportScope, *, bucket: str
) -> list[dict[str, Any]]:
    conds = _sale_conditions(p, scope)
    rows = db.execute(
        select(
            Sale.sale_date,
            func.count(func.distinct(Sale.id)).label("sale_count"),
            func.sum(Sale.gross_amount).label("gross_amount"),
            func.sum(Sale.discount_amount + Sale.campaign_discount_amount).label("discount_amount"),
            func.sum(Sale.net_amount).label("net_amount"),
            func.sum(Sale.vat_amount).label("vat_amount"),
            func.sum(Sale.total_amount).label("total_amount"),
            func.sum(Sale.paid_amount).label("paid_amount"),
            func.sum(Sale.total_cost).label("total_cost"),
            func.sum(Sale.margin_amount).label("margin_amount"),
        )
        .where(*conds)
        .group_by(Sale.sale_date)
        .order_by(Sale.sale_date)
    ).all()

    # Distinct customers must be counted per bucket, not summed from daily
    # counts — a customer bought on Monday and Thursday is one weekly customer.
    pairs = db.execute(
        select(Sale.sale_date, Sale.customer_id).where(*conds).distinct()
    ).all()
    customers: dict[str, set[int]] = {}
    for sale_date, customer_id in pairs:
        label, _ = _bucket_key(sale_date, bucket)
        customers.setdefault(label, set()).add(int(customer_id))

    acc: dict[str, dict[str, Any]] = {}
    for r in rows:
        label, bucket_date = _bucket_key(r.sale_date, bucket)
        cell = acc.setdefault(
            label,
            {
                "period": label,
                "bucket_date": bucket_date,
                "sale_count": 0,
                "gross_amount": Decimal("0"),
                "discount_amount": Decimal("0"),
                "net_amount": Decimal("0"),
                "vat_amount": Decimal("0"),
                "total_amount": Decimal("0"),
                "paid_amount": Decimal("0"),
                "total_cost": Decimal("0"),
                "margin_amount": Decimal("0"),
            },
        )
        cell["sale_count"] += int(r.sale_count or 0)
        for key in (
            "gross_amount", "discount_amount", "net_amount", "vat_amount",
            "total_amount", "paid_amount", "total_cost", "margin_amount",
        ):
            cell[key] += D(getattr(r, key))

    out: list[dict[str, Any]] = []
    for label in sorted(acc):
        cell = acc[label]
        cell["customer_count"] = len(customers.get(label, ()))
        for key in (
            "gross_amount", "discount_amount", "net_amount", "vat_amount",
            "total_amount", "paid_amount", "total_cost", "margin_amount",
        ):
            cell[key] = money(cell[key])
        cell["margin_percent"] = pct(cell["margin_amount"], cell["net_amount"])
        cell["avg_basket"] = money(
            cell["total_amount"] / cell["sale_count"] if cell["sale_count"] else 0
        )
        out.append(cell)
    return out


# ===========================================================================
# Salesperson performance
# ===========================================================================
def _run_salesperson_performance(
    db: Session, p: ReportParams, scope: ReportScope
) -> list[dict[str, Any]]:
    start, end = p.range()
    conds = _sale_conditions(p, scope)

    sales = db.execute(
        select(
            Sale.salesperson_id,
            func.count(func.distinct(Sale.id)).label("sale_count"),
            func.count(func.distinct(Sale.customer_id)).label("customer_count"),
            func.sum(Sale.net_amount).label("net_amount"),
            func.sum(Sale.total_amount).label("total_amount"),
            func.sum(Sale.discount_amount + Sale.campaign_discount_amount).label("discount_amount"),
            func.sum(Sale.total_cost).label("total_cost"),
            func.sum(Sale.margin_amount).label("margin_amount"),
        )
        .where(*conds)
        .group_by(Sale.salesperson_id)
    ).all()

    acc: dict[int, dict[str, Any]] = {}
    for r in sales:
        sid = int(r.salesperson_id or 0)
        acc[sid] = {
            "salesperson_id": sid,
            "sale_count": int(r.sale_count or 0),
            "customer_count": int(r.customer_count or 0),
            "net_amount": money(r.net_amount),
            "total_amount": money(r.total_amount),
            "discount_amount": money(r.discount_amount),
            "total_cost": money(r.total_cost),
            "margin_amount": money(r.margin_amount),
        }

    # Collections
    pay_conds: list[Any] = [
        Payment.payment_date >= start,
        Payment.payment_date <= end,
        Payment.is_deleted.is_(False),
        Payment.status != PaymentStatus.CANCELLED,
    ]
    if (allowed := scope.sales_filter()) is not None:
        pay_conds.append(Payment.salesperson_id.in_(allowed))
    for r in db.execute(
        select(Payment.salesperson_id, func.sum(Payment.amount).label("collected"))
        .where(*pay_conds)
        .group_by(Payment.salesperson_id)
    ).all():
        sid = int(r.salesperson_id or 0)
        acc.setdefault(sid, {"salesperson_id": sid})["collected_amount"] = money(r.collected)

    # Visit productivity
    visit_conds: list[Any] = [Visit.visit_date >= start, Visit.visit_date <= end]
    if allowed is not None:
        visit_conds.append(Visit.salesperson_id.in_(allowed))
    for r in db.execute(
        select(
            Visit.salesperson_id,
            func.count(Visit.id).label("visits"),
            func.sum(Visit.sale_amount).label("visit_sales"),
        )
        .where(*visit_conds)
        .group_by(Visit.salesperson_id)
    ).all():
        sid = int(r.salesperson_id or 0)
        cell = acc.setdefault(sid, {"salesperson_id": sid})
        cell["visit_count"] = int(r.visits or 0)

    productive_conds = list(visit_conds) + [Visit.sale_amount > 0]
    for r in db.execute(
        select(Visit.salesperson_id, func.count(Visit.id).label("productive"))
        .where(*productive_conds)
        .group_by(Visit.salesperson_id)
    ).all():
        sid = int(r.salesperson_id or 0)
        acc.setdefault(sid, {"salesperson_id": sid})["productive_visits"] = int(r.productive or 0)

    names = {
        s.id: s
        for s in db.execute(
            select(Salesperson).where(Salesperson.id.in_([k for k in acc if k]))
        ).scalars()
    }

    rows: list[dict[str, Any]] = []
    for sid, cell in acc.items():
        sp = names.get(sid)
        net = D(cell.get("net_amount"))
        total = D(cell.get("total_amount"))
        margin = D(cell.get("margin_amount"))
        visits = int(cell.get("visit_count") or 0)
        productive = int(cell.get("productive_visits") or 0)
        sale_count = int(cell.get("sale_count") or 0)
        rows.append(
            {
                "salesperson_id": sid,
                "code": sp.code if sp else "-",
                "name": sp.full_name if sp else "-",
                "sale_count": sale_count,
                "customer_count": int(cell.get("customer_count") or 0),
                "visit_count": visits,
                "productive_visits": productive,
                "success_rate": pct(productive, visits),
                "net_amount": money(net),
                "discount_amount": money(cell.get("discount_amount")),
                "total_amount": money(total),
                "total_cost": money(cell.get("total_cost")),
                "margin_amount": money(margin),
                "margin_percent": pct(margin, net),
                "collected_amount": money(cell.get("collected_amount")),
                "avg_basket": money(total / sale_count if sale_count else 0),
            }
        )
    rows.sort(key=lambda r: D(r["total_amount"]), reverse=True)
    _share(rows, "total_amount")
    return rows[: p.limit(500)]


# ===========================================================================
# Customer performance
# ===========================================================================
def _run_customer_performance(
    db: Session, p: ReportParams, scope: ReportScope
) -> list[dict[str, Any]]:
    start, end = p.range()
    conds = _sale_conditions(p, scope)

    sales = db.execute(
        select(
            Sale.customer_id,
            func.count(func.distinct(Sale.id)).label("sale_count"),
            func.sum(Sale.net_amount).label("net_amount"),
            func.sum(Sale.total_amount).label("total_amount"),
            func.sum(Sale.discount_amount + Sale.campaign_discount_amount).label("discount_amount"),
            func.sum(Sale.total_cost).label("total_cost"),
            func.sum(Sale.margin_amount).label("margin_amount"),
            func.max(Sale.sale_date).label("last_sale_date"),
        )
        .where(*conds)
        .group_by(Sale.customer_id)
    ).all()

    acc = {
        int(r.customer_id): {
            "customer_id": int(r.customer_id),
            "sale_count": int(r.sale_count or 0),
            "net_amount": money(r.net_amount),
            "total_amount": money(r.total_amount),
            "discount_amount": money(r.discount_amount),
            "total_cost": money(r.total_cost),
            "margin_amount": money(r.margin_amount),
            "last_sale_date": r.last_sale_date,
        }
        for r in sales
    }
    if not acc:
        return []

    ids = list(acc)
    for r in db.execute(
        select(
            ReturnDocument.customer_id,
            func.sum(ReturnDocument.total_amount).label("return_amount"),
        )
        .where(
            ReturnDocument.return_date >= start,
            ReturnDocument.return_date <= end,
            ReturnDocument.is_deleted.is_(False),
            ReturnDocument.customer_id.in_(ids),
        )
        .group_by(ReturnDocument.customer_id)
    ).all():
        acc[int(r.customer_id)]["return_amount"] = money(r.return_amount)

    for r in db.execute(
        select(Payment.customer_id, func.sum(Payment.amount).label("collected"))
        .where(
            Payment.payment_date >= start,
            Payment.payment_date <= end,
            Payment.is_deleted.is_(False),
            Payment.status != PaymentStatus.CANCELLED,
            Payment.customer_id.in_(ids),
        )
        .group_by(Payment.customer_id)
    ).all():
        acc[int(r.customer_id)]["collected_amount"] = money(r.collected)

    customers = {
        c.id: c for c in db.execute(select(Customer).where(Customer.id.in_(ids))).scalars()
    }
    regions = {r.id: r.name for r in db.execute(select(Region)).scalars()}

    rows: list[dict[str, Any]] = []
    for cid, cell in acc.items():
        c = customers.get(cid)
        net = D(cell["net_amount"])
        total = D(cell["total_amount"])
        margin = D(cell["margin_amount"])
        sale_count = cell["sale_count"]
        rows.append(
            {
                "customer_id": cid,
                "code": c.code if c else "-",
                "name": c.name if c else "-",
                "customer_type": c.customer_type if c else "",
                "channel": c.channel if c else "",
                "city": (c.city or "") if c else "",
                "region": regions.get(c.region_id, "") if c else "",
                "sale_count": sale_count,
                "net_amount": money(net),
                "discount_amount": cell["discount_amount"],
                "total_amount": money(total),
                "margin_amount": money(margin),
                "margin_percent": pct(margin, net),
                "return_amount": money(cell.get("return_amount")),
                "collected_amount": money(cell.get("collected_amount")),
                "balance": money(c.balance if c else 0),
                "avg_basket": money(total / sale_count if sale_count else 0),
                "last_sale_date": cell["last_sale_date"],
            }
        )
    rows.sort(key=lambda r: D(r["total_amount"]), reverse=True)
    _share(rows, "total_amount")
    return rows[: p.limit(500)]


# ===========================================================================
# Product / brand / category / region breakdowns
# ===========================================================================
def _run_sku_performance(db: Session, p: ReportParams, scope: ReportScope) -> list[dict[str, Any]]:
    conds = _sale_conditions(p, scope) + _item_product_conditions(p)
    stmt = (
        _item_select(
            Product.id.label("product_id"),
            Product.sku,
            Product.name,
            Product.base_uom,
            Brand.name.label("brand_name"),
            ProductCategory.name.label("category_name"),
        )
        # Outer joins: a product without a brand or category still sells.
        .outerjoin(Brand, Product.brand_id == Brand.id)
        .outerjoin(ProductCategory, Product.category_id == ProductCategory.id)
        .where(*conds)
        .group_by(
            Product.id, Product.sku, Product.name, Product.base_uom,
            Brand.name, ProductCategory.name,
        )
    )
    rows: list[dict[str, Any]] = []
    for r in db.execute(stmt).all():
        rows.append(
            {
                "product_id": int(r.product_id),
                "sku": r.sku,
                "name": r.name,
                "brand": r.brand_name or "",
                "category": r.category_name or "",
                "base_uom": r.base_uom,
                **_item_row(r),
            }
        )
    rows.sort(key=lambda r: D(r["total_amount"]), reverse=True)
    _share(rows, "total_amount")
    return rows[: p.limit(500)]


def _run_brand_performance(db: Session, p: ReportParams, scope: ReportScope) -> list[dict[str, Any]]:
    conds = _sale_conditions(p, scope) + _item_product_conditions(p)
    stmt = (
        _item_select(Brand.id.label("brand_id"), Brand.code, Brand.name)
        .join(Brand, Product.brand_id == Brand.id)
        .where(*conds)
        .group_by(Brand.id, Brand.code, Brand.name)
    )
    rows = [
        {
            "brand_id": int(r.brand_id),
            "code": r.code,
            "name": r.name,
            **_item_row(r),
        }
        for r in db.execute(stmt).all()
    ]
    rows.sort(key=lambda r: D(r["total_amount"]), reverse=True)
    _share(rows, "total_amount")
    return rows[: p.limit(500)]


def _run_category_performance(
    db: Session, p: ReportParams, scope: ReportScope
) -> list[dict[str, Any]]:
    conds = _sale_conditions(p, scope) + _item_product_conditions(p)
    stmt = (
        _item_select(
            ProductCategory.id.label("category_id"), ProductCategory.code, ProductCategory.name
        )
        .join(ProductCategory, Product.category_id == ProductCategory.id)
        .where(*conds)
        .group_by(ProductCategory.id, ProductCategory.code, ProductCategory.name)
    )
    rows = [
        {
            "category_id": int(r.category_id),
            "code": r.code,
            "name": r.name,
            **_item_row(r),
        }
        for r in db.execute(stmt).all()
    ]
    rows.sort(key=lambda r: D(r["total_amount"]), reverse=True)
    _share(rows, "total_amount")
    return rows[: p.limit(500)]


def _run_region_performance(
    db: Session, p: ReportParams, scope: ReportScope
) -> list[dict[str, Any]]:
    conds = _sale_conditions(p, scope)
    stmt = (
        select(
            Region.id.label("region_id"),
            Region.code,
            Region.name,
            func.count(func.distinct(Sale.id)).label("sale_count"),
            func.count(func.distinct(Sale.customer_id)).label("customer_count"),
            func.sum(Sale.net_amount).label("net_amount"),
            func.sum(Sale.total_amount).label("total_amount"),
            func.sum(Sale.discount_amount + Sale.campaign_discount_amount).label("discount_amount"),
            func.sum(Sale.total_cost).label("total_cost"),
            func.sum(Sale.margin_amount).label("margin_amount"),
        )
        .select_from(Sale)
        .join(Customer, Sale.customer_id == Customer.id)
        .join(Region, Customer.region_id == Region.id)
        .where(*conds)
        .group_by(Region.id, Region.code, Region.name)
    )
    rows: list[dict[str, Any]] = []
    for r in db.execute(stmt).all():
        net = money(r.net_amount)
        margin = money(r.margin_amount)
        sale_count = int(r.sale_count or 0)
        total = money(r.total_amount)
        rows.append(
            {
                "region_id": int(r.region_id),
                "code": r.code,
                "name": r.name,
                "sale_count": sale_count,
                "customer_count": int(r.customer_count or 0),
                "net_amount": net,
                "discount_amount": money(r.discount_amount),
                "total_amount": total,
                "total_cost": money(r.total_cost),
                "margin_amount": margin,
                "margin_percent": pct(margin, net),
                "avg_basket": money(total / sale_count if sale_count else 0),
            }
        )
    rows.sort(key=lambda r: D(r["total_amount"]), reverse=True)
    _share(rows, "total_amount")
    return rows


# ===========================================================================
# Route performance
# ===========================================================================
def _run_route_performance(
    db: Session, p: ReportParams, scope: ReportScope
) -> list[dict[str, Any]]:
    start, end = p.range()
    conds: list[Any] = [
        Route.is_template.is_(False),
        Route.is_deleted.is_(False),
        Route.route_date >= start,
        Route.route_date <= end,
    ]
    if (sp := p.int_("salesperson_id")) is not None:
        conds.append(Route.salesperson_id == sp)
    if (rid := p.int_("route_id")) is not None:
        conds.append(Route.id == rid)
    if (region := p.int_("region_id")) is not None:
        conds.append(Route.region_id == region)
    if (allowed := scope.sales_filter()) is not None:
        conds.append(Route.salesperson_id.in_(allowed))

    routes = db.execute(select(Route).where(*conds).order_by(Route.route_date.desc())).scalars().all()
    if not routes:
        return []

    route_ids = [r.id for r in routes]
    sales_by_route: dict[int, tuple[Decimal, int]] = {}
    for r in db.execute(
        select(
            Sale.route_id,
            func.sum(Sale.total_amount).label("amount"),
            func.count(func.distinct(Sale.id)).label("cnt"),
        )
        .where(
            Sale.route_id.in_(route_ids),
            Sale.is_cancelled.is_(False),
            Sale.is_deleted.is_(False),
        )
        .group_by(Sale.route_id)
    ).all():
        sales_by_route[int(r.route_id)] = (money(r.amount), int(r.cnt or 0))

    people = {s.id: s.full_name for s in db.execute(select(Salesperson)).scalars()}

    rows: list[dict[str, Any]] = []
    for route in routes:
        amount, sale_count = sales_by_route.get(route.id, (Decimal("0"), 0))
        planned = route.planned_stops or 0
        rows.append(
            {
                "route_id": route.id,
                "code": route.code,
                "name": route.name,
                "route_date": route.route_date,
                "salesperson": people.get(route.salesperson_id or 0, ""),
                "status": route.status,
                "planned_stops": planned,
                "completed_stops": route.completed_stops,
                "skipped_stops": route.skipped_stops,
                "completion_percent": pct(route.completed_stops, planned),
                "planned_distance_km": round(route.planned_distance_km, 2),
                "actual_distance_km": round(route.actual_distance_km, 2),
                "planned_duration_min": route.planned_duration_min,
                "actual_duration_min": route.actual_duration_min,
                "sale_count": sale_count,
                "total_amount": amount,
                "amount_per_stop": money(
                    amount / route.completed_stops if route.completed_stops else 0
                ),
                "amount_per_km": money(
                    amount / Decimal(str(route.actual_distance_km))
                    if route.actual_distance_km
                    else 0
                ),
            }
        )
    return rows[: p.limit(500)]


# ===========================================================================
# Collections
# ===========================================================================
_PAYMENT_BUCKETS: tuple[tuple[str, str], ...] = (
    (PaymentMethod.CASH, "cash"),
    (PaymentMethod.CREDIT_CARD, "credit_card"),
    (PaymentMethod.BANK_TRANSFER, "bank_transfer"),
    (PaymentMethod.CHEQUE, "cheque"),
    (PaymentMethod.PROMISSORY_NOTE, "promissory_note"),
)
_METHOD_TO_KEY = dict(_PAYMENT_BUCKETS)


def _run_collections(db: Session, p: ReportParams, scope: ReportScope) -> list[dict[str, Any]]:
    start, end = p.range()
    conds: list[Any] = [
        Payment.payment_date >= start,
        Payment.payment_date <= end,
        Payment.is_deleted.is_(False),
        Payment.status != PaymentStatus.CANCELLED,
    ]
    if (sp := p.int_("salesperson_id")) is not None:
        conds.append(Payment.salesperson_id == sp)
    if (cust := p.int_("customer_id")) is not None:
        conds.append(Payment.customer_id == cust)
    if (method := p.str_("payment_method")) is not None:
        conds.append(Payment.payment_method == method)
    if (allowed := scope.sales_filter()) is not None:
        conds.append(Payment.salesperson_id.in_(allowed))

    rows_raw = db.execute(
        select(
            Payment.payment_date,
            Payment.payment_method,
            func.count(Payment.id).label("cnt"),
            func.sum(Payment.amount).label("amount"),
        )
        .where(*conds)
        .group_by(Payment.payment_date, Payment.payment_method)
        .order_by(Payment.payment_date)
    ).all()

    pairs = db.execute(
        select(Payment.payment_date, Payment.customer_id).where(*conds).distinct()
    ).all()
    customers_per_day: dict[date, set[int]] = {}
    for pay_date, customer_id in pairs:
        customers_per_day.setdefault(pay_date, set()).add(int(customer_id))

    acc: dict[date, dict[str, Any]] = {}
    for r in rows_raw:
        cell = acc.setdefault(
            r.payment_date,
            {
                "bucket_date": r.payment_date,
                "payment_count": 0,
                "cash": Decimal("0"),
                "credit_card": Decimal("0"),
                "bank_transfer": Decimal("0"),
                "cheque": Decimal("0"),
                "promissory_note": Decimal("0"),
                "other": Decimal("0"),
                "total_amount": Decimal("0"),
            },
        )
        key = _METHOD_TO_KEY.get(r.payment_method, "other")
        amount = D(r.amount)
        cell[key] += amount
        cell["total_amount"] += amount
        cell["payment_count"] += int(r.cnt or 0)

    out: list[dict[str, Any]] = []
    for day in sorted(acc):
        cell = acc[day]
        cell["customer_count"] = len(customers_per_day.get(day, ()))
        for key in ("cash", "credit_card", "bank_transfer", "cheque", "promissory_note", "other", "total_amount"):
            cell[key] = money(cell[key])
        out.append(cell)
    return out


# ===========================================================================
# Receivable ageing / risk
# ===========================================================================
_AGE_BUCKETS: tuple[tuple[str, int, int], ...] = (
    ("not_due", -10_000, 0),
    ("days_1_30", 1, 30),
    ("days_31_60", 31, 60),
    ("days_61_90", 61, 90),
    ("days_90_plus", 91, 10_000),
)


def _run_receivable_ageing(
    db: Session, p: ReportParams, scope: ReportScope
) -> list[dict[str, Any]]:
    as_of = p.date("as_of") or date.today()
    conds: list[Any] = [
        CustomerLedger.is_settled.is_(False),
        CustomerLedger.open_amount > 0,
        CustomerLedger.entry_type.in_(
            [
                LedgerEntryType.INVOICE,
                LedgerEntryType.DEBIT_NOTE,
                LedgerEntryType.OPENING_BALANCE,
            ]
        ),
        CustomerLedger.entry_date <= as_of,
    ]
    if (cust := p.int_("customer_id")) is not None:
        conds.append(CustomerLedger.customer_id == cust)
    if (allowed := scope.sales_filter()) is not None:
        conds.append(
            CustomerLedger.customer_id.in_(
                select(Customer.id).where(Customer.default_salesperson_id.in_(allowed))
            )
        )
    if (region := p.int_("region_id")) is not None:
        conds.append(
            CustomerLedger.customer_id.in_(
                select(Customer.id).where(Customer.region_id == region)
            )
        )

    entries = db.execute(
        select(
            CustomerLedger.customer_id,
            CustomerLedger.due_date,
            CustomerLedger.entry_date,
            CustomerLedger.open_amount,
        ).where(*conds)
    ).all()
    if not entries:
        return []

    acc: dict[int, dict[str, Any]] = {}
    for customer_id, due_date, entry_date, open_amount in entries:
        cid = int(customer_id)
        cell = acc.setdefault(
            cid,
            {
                "customer_id": cid,
                "not_due": Decimal("0"),
                "days_1_30": Decimal("0"),
                "days_31_60": Decimal("0"),
                "days_61_90": Decimal("0"),
                "days_90_plus": Decimal("0"),
                "open_amount": Decimal("0"),
                "overdue_amount": Decimal("0"),
                "oldest_days": 0,
                "document_count": 0,
            },
        )
        overdue_days = (as_of - (due_date or entry_date)).days
        amount = D(open_amount)
        bucket = "not_due"
        for name, low, high in _AGE_BUCKETS:
            if low <= overdue_days <= high:
                bucket = name
                break
        cell[bucket] += amount
        cell["open_amount"] += amount
        cell["document_count"] += 1
        if overdue_days > 0:
            cell["overdue_amount"] += amount
            cell["oldest_days"] = max(cell["oldest_days"], overdue_days)

    customers = {
        c.id: c for c in db.execute(select(Customer).where(Customer.id.in_(list(acc)))).scalars()
    }
    people = {s.id: s.full_name for s in db.execute(select(Salesperson)).scalars()}

    rows: list[dict[str, Any]] = []
    for cid, cell in acc.items():
        c = customers.get(cid)
        limit = D(c.credit_limit if c else 0)
        for key in ("not_due", "days_1_30", "days_31_60", "days_61_90", "days_90_plus",
                    "open_amount", "overdue_amount"):
            cell[key] = money(cell[key])
        rows.append(
            {
                **cell,
                "code": c.code if c else "-",
                "name": c.name if c else "-",
                "salesperson": people.get(c.default_salesperson_id or 0, "") if c else "",
                "phone": (c.phone or "") if c else "",
                "credit_limit": money(limit),
                "limit_usage_percent": pct(cell["open_amount"], limit) if limit else 0.0,
                "risk_score": round(c.risk_score, 1) if c else 0.0,
                "payment_term_days": c.payment_term_days if c else 0,
            }
        )
    rows.sort(key=lambda r: D(r["overdue_amount"]), reverse=True)
    return rows[: p.limit(1000)]


# ===========================================================================
# Stock reports
# ===========================================================================
def _stock_rows(
    db: Session, p: ReportParams, *, vehicle: bool
) -> list[Any]:
    conds: list[Any] = [
        Warehouse.is_deleted.is_(False),
        StockBalance.status == StockStatus.AVAILABLE,
    ]
    conds.append(
        Warehouse.warehouse_type == WarehouseType.VEHICLE
        if vehicle
        else Warehouse.warehouse_type != WarehouseType.VEHICLE
    )
    if not p.bool_("include_zero"):
        conds.append(StockBalance.quantity != 0)
    if (wh := p.int_("warehouse_id")) is not None:
        conds.append(StockBalance.warehouse_id == wh)
    if (pid := p.int_("product_id")) is not None:
        conds.append(StockBalance.product_id == pid)
    if (cid := p.int_("category_id")) is not None:
        conds.append(Product.category_id == cid)
    if (bid := p.int_("brand_id")) is not None:
        conds.append(Product.brand_id == bid)

    return db.execute(
        select(
            StockBalance.warehouse_id,
            Warehouse.code.label("warehouse_code"),
            Warehouse.name.label("warehouse_name"),
            Product.id.label("product_id"),
            Product.sku,
            Product.name.label("product_name"),
            Product.base_uom,
            Product.min_stock_level,
            Product.units_per_case,
            func.sum(StockBalance.quantity).label("quantity"),
            func.sum(StockBalance.reserved_quantity).label("reserved"),
            func.sum(StockBalance.quantity * StockBalance.average_cost).label("value"),
        )
        .select_from(StockBalance)
        .join(Warehouse, StockBalance.warehouse_id == Warehouse.id)
        .join(Product, StockBalance.product_id == Product.id)
        .where(*conds)
        .group_by(
            StockBalance.warehouse_id,
            Warehouse.code,
            Warehouse.name,
            Product.id,
            Product.sku,
            Product.name,
            Product.base_uom,
            Product.min_stock_level,
            Product.units_per_case,
        )
        .order_by(Warehouse.code, Product.sku)
    ).all()


def _run_warehouse_stock(db: Session, p: ReportParams, scope: ReportScope) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for r in _stock_rows(db, p, vehicle=False):
        quantity = qty(r.quantity)
        reserved = qty(r.reserved)
        minimum = qty(r.min_stock_level)
        per_case = D(r.units_per_case) or Decimal("1")
        rows.append(
            {
                "warehouse_code": r.warehouse_code,
                "warehouse_name": r.warehouse_name,
                "sku": r.sku,
                "product_name": r.product_name,
                "base_uom": r.base_uom,
                "quantity": quantity,
                "case_quantity": qty(quantity / per_case),
                "reserved_quantity": reserved,
                "available_quantity": qty(quantity - reserved),
                "min_stock_level": minimum,
                "stock_value": money(r.value),
                "status": "LOW" if minimum and quantity < minimum else "OK",
            }
        )
    _share(rows, "stock_value")
    return rows[: p.limit(2000)]


def _run_van_stock(db: Session, p: ReportParams, scope: ReportScope) -> list[dict[str, Any]]:
    vehicles = {
        v.warehouse_id: v
        for v in db.execute(select(Vehicle).where(Vehicle.warehouse_id.isnot(None))).scalars()
    }
    people = {s.id: s.full_name for s in db.execute(select(Salesperson)).scalars()}
    allowed = scope.sales_filter()

    rows: list[dict[str, Any]] = []
    for r in _stock_rows(db, p, vehicle=True):
        vehicle = vehicles.get(int(r.warehouse_id))
        sp_id = vehicle.default_salesperson_id if vehicle else None
        if allowed is not None and sp_id not in allowed:
            continue
        if (want := p.int_("vehicle_id")) is not None and (not vehicle or vehicle.id != want):
            continue
        quantity = qty(r.quantity)
        per_case = D(r.units_per_case) or Decimal("1")
        rows.append(
            {
                "vehicle_code": vehicle.code if vehicle else r.warehouse_code,
                "plate_number": vehicle.plate_number if vehicle else "",
                "salesperson": people.get(sp_id or 0, ""),
                "warehouse_name": r.warehouse_name,
                "sku": r.sku,
                "product_name": r.product_name,
                "base_uom": r.base_uom,
                "quantity": quantity,
                "case_quantity": qty(quantity / per_case),
                "stock_value": money(r.value),
            }
        )
    _share(rows, "stock_value")
    return rows[: p.limit(2000)]


def _run_expiry(db: Session, p: ReportParams, scope: ReportScope) -> list[dict[str, Any]]:
    horizon_days = p.int_("days", settings.expiry_warning_days) or settings.expiry_warning_days
    today = date.today()
    cutoff = today + timedelta(days=horizon_days)

    conds: list[Any] = [
        Lot.expiry_date.isnot(None),
        Lot.expiry_date <= cutoff,
        StockBalance.quantity > 0,
        Warehouse.is_deleted.is_(False),
    ]
    if (wh := p.int_("warehouse_id")) is not None:
        conds.append(StockBalance.warehouse_id == wh)
    if (pid := p.int_("product_id")) is not None:
        conds.append(StockBalance.product_id == pid)

    rows_raw = db.execute(
        select(
            Warehouse.code.label("warehouse_code"),
            Warehouse.name.label("warehouse_name"),
            Warehouse.warehouse_type,
            Product.sku,
            Product.name.label("product_name"),
            Lot.lot_number,
            Lot.expiry_date,
            StockBalance.quantity,
            StockBalance.average_cost,
            StockBalance.status,
        )
        .select_from(StockBalance)
        .join(Warehouse, StockBalance.warehouse_id == Warehouse.id)
        .join(Product, StockBalance.product_id == Product.id)
        .join(Lot, StockBalance.lot_id == Lot.id)
        .where(*conds)
        .order_by(Lot.expiry_date)
    ).all()

    rows: list[dict[str, Any]] = []
    for r in rows_raw:
        days_left = (r.expiry_date - today).days
        if days_left < 0:
            severity = "EXPIRED"
        elif days_left <= max(1, horizon_days // 3):
            severity = "CRITICAL"
        else:
            severity = "WARNING"
        quantity = qty(r.quantity)
        rows.append(
            {
                "warehouse_code": r.warehouse_code,
                "warehouse_name": r.warehouse_name,
                "warehouse_type": r.warehouse_type,
                "sku": r.sku,
                "product_name": r.product_name,
                "lot_number": r.lot_number,
                "expiry_date": r.expiry_date,
                "days_to_expiry": days_left,
                "quantity": quantity,
                "stock_status": r.status,
                "unit_cost": money(r.average_cost),
                "stock_value": money(quantity * D(r.average_cost)),
                "severity": severity,
            }
        )
    return rows[: p.limit(2000)]


_WASTAGE_TYPES = (
    StockMovementType.WASTAGE,
    StockMovementType.DAMAGE,
    StockMovementType.EXPIRY,
)


def _run_wastage(db: Session, p: ReportParams, scope: ReportScope) -> list[dict[str, Any]]:
    start, end = p.range()
    conds: list[Any] = [
        StockMovement.movement_type.in_(list(_WASTAGE_TYPES)),
        func.date(StockMovement.moved_at) >= start,
        func.date(StockMovement.moved_at) <= end,
    ]
    if (wh := p.int_("warehouse_id")) is not None:
        conds.append(StockMovement.warehouse_id == wh)
    if (pid := p.int_("product_id")) is not None:
        conds.append(StockMovement.product_id == pid)
    if (allowed := scope.sales_filter()) is not None:
        conds.append(StockMovement.salesperson_id.in_(allowed))

    rows_raw = db.execute(
        select(
            Warehouse.code.label("warehouse_code"),
            Warehouse.name.label("warehouse_name"),
            Product.sku,
            Product.name.label("product_name"),
            StockMovement.movement_type,
            func.count(StockMovement.id).label("movement_count"),
            func.sum(StockMovement.quantity).label("quantity"),
            func.sum(StockMovement.total_cost).label("cost"),
        )
        .select_from(StockMovement)
        .join(Warehouse, StockMovement.warehouse_id == Warehouse.id)
        .join(Product, StockMovement.product_id == Product.id)
        .where(*conds)
        .group_by(
            Warehouse.code, Warehouse.name, Product.sku, Product.name,
            StockMovement.movement_type,
        )
    ).all()

    rows = [
        {
            "warehouse_code": r.warehouse_code,
            "warehouse_name": r.warehouse_name,
            "sku": r.sku,
            "product_name": r.product_name,
            "movement_type": r.movement_type,
            "movement_count": int(r.movement_count or 0),
            # Wastage movements are stored negative (stock out); report the
            # magnitude so the number reads as "how much was lost".
            "quantity": qty(abs(D(r.quantity))),
            "cost_amount": money(abs(D(r.cost))),
        }
        for r in rows_raw
    ]
    rows.sort(key=lambda r: D(r["cost_amount"]), reverse=True)
    _share(rows, "cost_amount")
    return rows[: p.limit(1000)]


# ===========================================================================
# Returns
# ===========================================================================
def _run_returns(db: Session, p: ReportParams, scope: ReportScope) -> list[dict[str, Any]]:
    start, end = p.range()
    conds: list[Any] = [
        ReturnDocument.return_date >= start,
        ReturnDocument.return_date <= end,
        ReturnDocument.is_deleted.is_(False),
    ]
    if (sp := p.int_("salesperson_id")) is not None:
        conds.append(ReturnDocument.salesperson_id == sp)
    if (cust := p.int_("customer_id")) is not None:
        conds.append(ReturnDocument.customer_id == cust)
    if (reason := p.str_("reason")) is not None:
        conds.append(ReturnItem.reason == reason)
    if (pid := p.int_("product_id")) is not None:
        conds.append(ReturnItem.product_id == pid)
    if (allowed := scope.sales_filter()) is not None:
        conds.append(ReturnDocument.salesperson_id.in_(allowed))

    rows_raw = db.execute(
        select(
            Product.id.label("product_id"),
            Product.sku,
            Product.name.label("product_name"),
            ReturnItem.reason,
            ReturnItem.disposition,
            func.count(func.distinct(ReturnDocument.id)).label("document_count"),
            func.sum(ReturnItem.base_quantity).label("quantity"),
            func.sum(ReturnItem.net_amount).label("net_amount"),
            func.sum(ReturnItem.total_amount).label("total_amount"),
            func.sum(ReturnItem.base_quantity * ReturnItem.unit_cost).label("cost_amount"),
        )
        .select_from(ReturnItem)
        .join(ReturnDocument, ReturnItem.return_id == ReturnDocument.id)
        .join(Product, ReturnItem.product_id == Product.id)
        .where(*conds)
        .group_by(
            Product.id, Product.sku, Product.name, ReturnItem.reason, ReturnItem.disposition
        )
    ).all()
    if not rows_raw:
        return []

    # Return rate needs the matching sales volume for the same products/window.
    product_ids = [int(r.product_id) for r in rows_raw]
    sale_conds = _sale_conditions(p, scope) + [SaleItem.product_id.in_(product_ids)]
    sold = {
        int(r.product_id): (qty(r.quantity), money(r.amount))
        for r in db.execute(
            select(
                SaleItem.product_id,
                func.sum(SaleItem.base_quantity).label("quantity"),
                func.sum(SaleItem.total_amount).label("amount"),
            )
            .select_from(SaleItem)
            .join(Sale, SaleItem.sale_id == Sale.id)
            .where(*sale_conds)
            .group_by(SaleItem.product_id)
        ).all()
    }

    rows: list[dict[str, Any]] = []
    for r in rows_raw:
        sold_qty, sold_amount = sold.get(int(r.product_id), (Decimal("0"), Decimal("0")))
        quantity = qty(r.quantity)
        rows.append(
            {
                "sku": r.sku,
                "product_name": r.product_name,
                "reason": r.reason,
                "disposition": r.disposition,
                "document_count": int(r.document_count or 0),
                "quantity": quantity,
                "net_amount": money(r.net_amount),
                "total_amount": money(r.total_amount),
                "cost_amount": money(r.cost_amount),
                "sold_quantity": sold_qty,
                "sold_amount": sold_amount,
                "return_rate_percent": pct(quantity, sold_qty),
            }
        )
    rows.sort(key=lambda r: D(r["total_amount"]), reverse=True)
    return rows[: p.limit(1000)]


# ===========================================================================
# Campaign performance
# ===========================================================================
def _run_campaign_performance(
    db: Session, p: ReportParams, scope: ReportScope
) -> list[dict[str, Any]]:
    start, end = p.range()
    conds: list[Any] = [
        CampaignApplication.applied_on >= start,
        CampaignApplication.applied_on <= end,
    ]
    if (cid := p.int_("campaign_id")) is not None:
        conds.append(CampaignApplication.campaign_id == cid)
    if (allowed := scope.sales_filter()) is not None:
        conds.append(CampaignApplication.salesperson_id.in_(allowed))

    rows_raw = db.execute(
        select(
            Campaign.id.label("campaign_id"),
            Campaign.code,
            Campaign.name,
            Campaign.campaign_type,
            Campaign.status,
            Campaign.start_date,
            Campaign.end_date,
            Campaign.budget_amount,
            func.count(CampaignApplication.id).label("application_count"),
            func.count(func.distinct(CampaignApplication.customer_id)).label("customer_count"),
            func.sum(CampaignApplication.basket_amount).label("basket_amount"),
            func.sum(CampaignApplication.discount_amount).label("discount_amount"),
            func.sum(CampaignApplication.free_goods_quantity).label("free_goods_quantity"),
            func.sum(CampaignApplication.free_goods_cost).label("free_goods_cost"),
        )
        .select_from(CampaignApplication)
        .join(Campaign, CampaignApplication.campaign_id == Campaign.id)
        .where(*conds)
        .group_by(
            Campaign.id, Campaign.code, Campaign.name, Campaign.campaign_type,
            Campaign.status, Campaign.start_date, Campaign.end_date, Campaign.budget_amount,
        )
    ).all()

    rows: list[dict[str, Any]] = []
    for r in rows_raw:
        basket = money(r.basket_amount)
        discount = money(r.discount_amount)
        free_cost = money(r.free_goods_cost)
        investment = money(discount + free_cost)
        budget = money(r.budget_amount)
        rows.append(
            {
                "campaign_id": int(r.campaign_id),
                "code": r.code,
                "name": r.name,
                "campaign_type": r.campaign_type,
                "status": r.status,
                "start_date": r.start_date,
                "end_date": r.end_date,
                "application_count": int(r.application_count or 0),
                "customer_count": int(r.customer_count or 0),
                "basket_amount": basket,
                "discount_amount": discount,
                "free_goods_quantity": qty(r.free_goods_quantity),
                "free_goods_cost": free_cost,
                "investment_amount": investment,
                # How much basket value each lira of promotion bought.
                "roi_percent": pct(basket - investment, investment) if investment else 0.0,
                "cost_percent": pct(investment, basket),
                "budget_amount": budget,
                "budget_usage_percent": pct(investment, budget) if budget else 0.0,
            }
        )
    rows.sort(key=lambda r: D(r["basket_amount"]), reverse=True)
    return rows[: p.limit(500)]


# ===========================================================================
# Profitability
# ===========================================================================
_PROFIT_DIMENSIONS = {
    "PRODUCT": (Product.sku, Product.name),
    "CATEGORY": (ProductCategory.code, ProductCategory.name),
    "BRAND": (Brand.code, Brand.name),
}


def _run_profitability(db: Session, p: ReportParams, scope: ReportScope) -> list[dict[str, Any]]:
    dimension = (p.str_("dimension", "PRODUCT") or "PRODUCT").upper()
    conds = _sale_conditions(p, scope) + _item_product_conditions(p)

    if dimension == "CATEGORY":
        stmt = (
            _item_select(ProductCategory.code.label("dim_code"), ProductCategory.name.label("dim_name"))
            .join(ProductCategory, Product.category_id == ProductCategory.id)
            .where(*conds)
            .group_by(ProductCategory.code, ProductCategory.name)
        )
    elif dimension == "BRAND":
        stmt = (
            _item_select(Brand.code.label("dim_code"), Brand.name.label("dim_name"))
            .join(Brand, Product.brand_id == Brand.id)
            .where(*conds)
            .group_by(Brand.code, Brand.name)
        )
    elif dimension == "CUSTOMER":
        stmt = (
            _item_select(Customer.code.label("dim_code"), Customer.name.label("dim_name"))
            .join(Customer, Sale.customer_id == Customer.id)
            .where(*conds)
            .group_by(Customer.code, Customer.name)
        )
    elif dimension == "SALESPERSON":
        stmt = (
            _item_select(Salesperson.code.label("dim_code"), Salesperson.full_name.label("dim_name"))
            .join(Salesperson, Sale.salesperson_id == Salesperson.id)
            .where(*conds)
            .group_by(Salesperson.code, Salesperson.full_name)
        )
    else:
        dimension = "PRODUCT"
        stmt = (
            _item_select(Product.sku.label("dim_code"), Product.name.label("dim_name"))
            .where(*conds)
            .group_by(Product.sku, Product.name)
        )

    rows: list[dict[str, Any]] = []
    for r in db.execute(stmt).all():
        base = _item_row(r)
        gross = D(base["gross_amount"])
        rows.append(
            {
                "dimension": dimension,
                "code": r.dim_code,
                "name": r.dim_name,
                **base,
                "discount_percent": pct(base["discount_amount"], gross),
                "cost_percent": pct(base["total_cost"], base["net_amount"]),
            }
        )
    rows.sort(key=lambda r: D(r["margin_amount"]), reverse=True)
    _share(rows, "margin_amount", "margin_share_percent")
    return rows[: p.limit(500)]


# ===========================================================================
# Target achievement
# ===========================================================================
def _run_target_achievement(
    db: Session, p: ReportParams, scope: ReportScope
) -> list[dict[str, Any]]:
    start, end = p.range()
    conds: list[Any] = [Target.period_end >= start, Target.period_start <= end]
    if (subject := p.str_("subject_type")) is not None:
        conds.append(Target.subject_type == subject)
    if (metric := p.str_("metric")) is not None:
        conds.append(Target.metric == metric)
    if (allowed := scope.sales_filter()) is not None:
        conds.append(
            (Target.subject_type != TargetSubject.SALESPERSON)
            | (Target.subject_id.in_(allowed))
        )

    targets = db.execute(
        select(Target).where(*conds).order_by(Target.period_start.desc())
    ).scalars().all()
    if not targets:
        return []

    # Resolve subject labels in one pass per type instead of per row.
    labels: dict[tuple[str, int], str] = {}
    wanted: dict[str, set[int]] = {}
    for t in targets:
        wanted.setdefault(t.subject_type, set()).add(t.subject_id)

    loaders: dict[str, tuple[Any, Any, Any]] = {
        TargetSubject.SALESPERSON: (Salesperson, Salesperson.id, Salesperson.full_name),
        TargetSubject.REGION: (Region, Region.id, Region.name),
        TargetSubject.ROUTE: (Route, Route.id, Route.name),
        TargetSubject.PRODUCT: (Product, Product.id, Product.name),
        TargetSubject.CATEGORY: (ProductCategory, ProductCategory.id, ProductCategory.name),
        TargetSubject.BRAND: (Brand, Brand.id, Brand.name),
        TargetSubject.CUSTOMER: (Customer, Customer.id, Customer.name),
    }
    for subject_type, ids in wanted.items():
        loader = loaders.get(subject_type)
        if not loader or not ids:
            continue
        _model, id_col, name_col = loader
        for row_id, row_name in db.execute(
            select(id_col, name_col).where(id_col.in_(list(ids)))
        ).all():
            labels[(subject_type, int(row_id))] = row_name

    today = date.today()
    rows: list[dict[str, Any]] = []
    for t in targets:
        target_value = money(t.target_value)
        actual = money(t.actual_value)
        span = max(1, (t.period_end - t.period_start).days + 1)
        elapsed = min(span, max(0, (min(today, t.period_end) - t.period_start).days + 1))
        expected_pace = pct(elapsed, span)
        achievement = pct(actual, target_value)
        rows.append(
            {
                "target_id": t.id,
                "subject_type": t.subject_type,
                "subject_name": labels.get((t.subject_type, t.subject_id), "-"),
                "metric": t.metric,
                "period": t.period,
                "period_start": t.period_start,
                "period_end": t.period_end,
                "target_value": target_value,
                "actual_value": actual,
                "gap_amount": money(target_value - actual),
                "achievement_percent": achievement,
                "expected_percent": expected_pace,
                # Behind pace by more than 10 points is what supervisors chase.
                "pace_status": (
                    "AHEAD" if achievement >= expected_pace
                    else "AT_RISK" if achievement >= expected_pace - 10
                    else "BEHIND"
                ),
                "projected_value": money(t.projected_value),
                "risk_score": round(t.risk_score, 1),
                "days_remaining": max(0, (t.period_end - today).days),
            }
        )
    return rows[: p.limit(1000)]


# ===========================================================================
# Registry
# ===========================================================================
def _period_report(key: str, tr: str, en: str, bucket: str) -> ReportDef:
    return ReportDef(
        key=key,
        title_tr=tr,
        title_en=en,
        module="sales",
        columns=_PERIOD_COLUMNS,
        runner=lambda db, p, s, _b=bucket: _run_sales_by_period(db, p, s, bucket=_b),
        filters=(F_START, F_END, F_SALESPERSON, F_CUSTOMER, F_REGION, F_ROUTE, F_WAREHOUSE),
        group_by="period",
        totals=_PERIOD_TOTALS,
        derived_totals={"margin_percent": ("margin_amount", "net_amount")},
    )


REPORTS: dict[str, ReportDef] = {}


def _register(rdef: ReportDef) -> ReportDef:
    REPORTS[rdef.key] = rdef
    return rdef


_register(_period_report("sales_daily", "Günlük Satış Raporu", "Daily Sales Report", "DAILY"))
_register(_period_report("sales_weekly", "Haftalık Satış Raporu", "Weekly Sales Report", "WEEKLY"))
_register(_period_report("sales_monthly", "Aylık Satış Raporu", "Monthly Sales Report", "MONTHLY"))
_register(_period_report("sales_yearly", "Yıllık Satış Raporu", "Yearly Sales Report", "YEARLY"))

_register(
    ReportDef(
        key="salesperson_performance",
        title_tr="Plasiyer Performans Raporu",
        title_en="Salesperson Performance Report",
        module="sales",
        columns=(
            _txt("code", "Kod", "Code", 12),
            _txt("name", "Plasiyer", "Salesperson", 24),
            _num("sale_count", "Satış", "Sales", "integer", 10),
            _num("customer_count", "Müşteri", "Customers", "integer", 10),
            _num("visit_count", "Ziyaret", "Visits", "integer", 10),
            _num("productive_visits", "Verimli Ziyaret", "Productive Visits", "integer", 12),
            _num("success_rate", "Başarı %", "Success %", "percent", 10),
            _num("net_amount", "Net Tutar", "Net"),
            _num("discount_amount", "İskonto", "Discount"),
            _num("total_amount", "Toplam", "Total"),
            _num("total_cost", "Maliyet", "Cost"),
            _num("margin_amount", "Kâr", "Margin"),
            _num("margin_percent", "Kâr %", "Margin %", "percent", 10),
            _num("collected_amount", "Tahsilat", "Collections"),
            _num("avg_basket", "Ort. Sepet", "Avg Basket"),
            _num("share_percent", "Pay %", "Share %", "percent", 10),
        ),
        runner=_run_salesperson_performance,
        filters=(F_START, F_END, F_SALESPERSON, F_REGION, F_LIMIT),
        group_by="salesperson_id",
        totals=(
            "sale_count", "customer_count", "visit_count", "productive_visits",
            "net_amount", "discount_amount", "total_amount", "total_cost",
            "margin_amount", "collected_amount", "margin_percent", "success_rate",
        ),
        derived_totals={
            "margin_percent": ("margin_amount", "net_amount"),
            "success_rate": ("productive_visits", "visit_count"),
        },
    )
)

_register(
    ReportDef(
        key="customer_performance",
        title_tr="Müşteri Performans Raporu",
        title_en="Customer Performance Report",
        module="crm",
        columns=(
            _txt("code", "Kod", "Code", 12),
            _txt("name", "Müşteri", "Customer", 28),
            _txt("customer_type", "Tip", "Type", 14),
            _txt("channel", "Kanal", "Channel", 14),
            _txt("city", "Şehir", "City", 14),
            _txt("region", "Bölge", "Region", 14),
            _num("sale_count", "Satış", "Sales", "integer", 10),
            _num("net_amount", "Net Tutar", "Net"),
            _num("discount_amount", "İskonto", "Discount"),
            _num("total_amount", "Toplam", "Total"),
            _num("margin_amount", "Kâr", "Margin"),
            _num("margin_percent", "Kâr %", "Margin %", "percent", 10),
            _num("return_amount", "İade", "Returns"),
            _num("collected_amount", "Tahsilat", "Collections"),
            _num("balance", "Bakiye", "Balance"),
            _num("avg_basket", "Ort. Sepet", "Avg Basket"),
            _dat("last_sale_date", "Son Satış", "Last Sale"),
            _num("share_percent", "Pay %", "Share %", "percent", 10),
        ),
        runner=_run_customer_performance,
        filters=(F_START, F_END, F_SALESPERSON, F_CUSTOMER, F_REGION, F_LIMIT),
        group_by="customer_id",
        totals=(
            "sale_count", "net_amount", "discount_amount", "total_amount",
            "margin_amount", "return_amount", "collected_amount", "balance",
            "margin_percent",
        ),
        derived_totals={"margin_percent": ("margin_amount", "net_amount")},
    )
)

_SKU_COLUMNS = (
    _num("sale_count", "Satış", "Sales", "integer", 10),
    _num("customer_count", "Müşteri", "Customers", "integer", 10),
    _num("quantity", "Miktar", "Quantity", "quantity", 12),
    _num("gross_amount", "Brüt", "Gross"),
    _num("discount_amount", "İskonto", "Discount"),
    _num("net_amount", "Net Tutar", "Net"),
    _num("total_amount", "Toplam", "Total"),
    _num("total_cost", "Maliyet", "Cost"),
    _num("margin_amount", "Kâr", "Margin"),
    _num("margin_percent", "Kâr %", "Margin %", "percent", 10),
    _num("share_percent", "Pay %", "Share %", "percent", 10),
)
_SKU_TOTALS = (
    "sale_count", "quantity", "gross_amount", "discount_amount", "net_amount",
    "total_amount", "total_cost", "margin_amount", "margin_percent",
)

_register(
    ReportDef(
        key="sku_performance",
        title_tr="Ürün (SKU) Performans Raporu",
        title_en="SKU Performance Report",
        module="stock",
        columns=(
            _txt("sku", "Stok Kodu", "SKU", 16),
            _txt("name", "Ürün", "Product", 30),
            _txt("brand", "Marka", "Brand", 16),
            _txt("category", "Kategori", "Category", 18),
            _txt("base_uom", "Birim", "Unit", 10),
            *_SKU_COLUMNS,
        ),
        runner=_run_sku_performance,
        filters=(F_START, F_END, F_SALESPERSON, F_CATEGORY, F_BRAND, F_PRODUCT, F_LIMIT),
        group_by="product_id",
        totals=_SKU_TOTALS,
        derived_totals={"margin_percent": ("margin_amount", "net_amount")},
    )
)

_register(
    ReportDef(
        key="brand_performance",
        title_tr="Marka Performans Raporu",
        title_en="Brand Performance Report",
        module="stock",
        columns=(_txt("code", "Kod", "Code", 12), _txt("name", "Marka", "Brand", 26), *_SKU_COLUMNS),
        runner=_run_brand_performance,
        filters=(F_START, F_END, F_SALESPERSON, F_BRAND, F_REGION, F_LIMIT),
        group_by="brand_id",
        totals=_SKU_TOTALS,
        derived_totals={"margin_percent": ("margin_amount", "net_amount")},
    )
)

_register(
    ReportDef(
        key="category_performance",
        title_tr="Kategori Performans Raporu",
        title_en="Category Performance Report",
        module="stock",
        columns=(_txt("code", "Kod", "Code", 12), _txt("name", "Kategori", "Category", 26), *_SKU_COLUMNS),
        runner=_run_category_performance,
        filters=(F_START, F_END, F_SALESPERSON, F_CATEGORY, F_REGION, F_LIMIT),
        group_by="category_id",
        totals=_SKU_TOTALS,
        derived_totals={"margin_percent": ("margin_amount", "net_amount")},
    )
)

_register(
    ReportDef(
        key="region_performance",
        title_tr="Bölge Performans Raporu",
        title_en="Region Performance Report",
        module="sales",
        columns=(
            _txt("code", "Kod", "Code", 12),
            _txt("name", "Bölge", "Region", 24),
            _num("sale_count", "Satış", "Sales", "integer", 10),
            _num("customer_count", "Müşteri", "Customers", "integer", 10),
            _num("net_amount", "Net Tutar", "Net"),
            _num("discount_amount", "İskonto", "Discount"),
            _num("total_amount", "Toplam", "Total"),
            _num("total_cost", "Maliyet", "Cost"),
            _num("margin_amount", "Kâr", "Margin"),
            _num("margin_percent", "Kâr %", "Margin %", "percent", 10),
            _num("avg_basket", "Ort. Sepet", "Avg Basket"),
            _num("share_percent", "Pay %", "Share %", "percent", 10),
        ),
        runner=_run_region_performance,
        filters=(F_START, F_END, F_REGION, F_SALESPERSON),
        group_by="region_id",
        totals=(
            "sale_count", "customer_count", "net_amount", "discount_amount",
            "total_amount", "total_cost", "margin_amount", "margin_percent",
        ),
        derived_totals={"margin_percent": ("margin_amount", "net_amount")},
    )
)

_register(
    ReportDef(
        key="route_performance",
        title_tr="Rota Performans Raporu",
        title_en="Route Performance Report",
        module="field",
        columns=(
            _txt("code", "Rota Kodu", "Route Code", 14),
            _txt("name", "Rota", "Route", 24),
            _dat("route_date", "Tarih", "Date"),
            _txt("salesperson", "Plasiyer", "Salesperson", 22),
            _txt("status", "Durum", "Status", 14),
            _num("planned_stops", "Planlanan Durak", "Planned Stops", "integer", 12),
            _num("completed_stops", "Tamamlanan", "Completed", "integer", 12),
            _num("skipped_stops", "Atlanan", "Skipped", "integer", 10),
            _num("completion_percent", "Tamamlama %", "Completion %", "percent", 12),
            _num("planned_distance_km", "Plan km", "Planned km", "number", 10),
            _num("actual_distance_km", "Gerçek km", "Actual km", "number", 10),
            _num("actual_duration_min", "Süre (dk)", "Duration (min)", "integer", 12),
            _num("sale_count", "Satış", "Sales", "integer", 10),
            _num("total_amount", "Ciro", "Revenue"),
            _num("amount_per_stop", "Durak Başına", "Per Stop"),
            _num("amount_per_km", "km Başına", "Per km"),
        ),
        runner=_run_route_performance,
        filters=(F_START, F_END, F_SALESPERSON, F_ROUTE, F_REGION, F_LIMIT),
        group_by="route_id",
        totals=(
            "planned_stops", "completed_stops", "skipped_stops", "planned_distance_km",
            "actual_distance_km", "actual_duration_min", "sale_count", "total_amount",
            "completion_percent",
        ),
        derived_totals={"completion_percent": ("completed_stops", "planned_stops")},
        permission=("field.routes", "VIEW"),
    )
)

_register(
    ReportDef(
        key="collections",
        title_tr="Tahsilat Raporu",
        title_en="Collections Report",
        module="finance",
        columns=(
            _dat("bucket_date", "Tarih", "Date"),
            _num("payment_count", "Tahsilat Adedi", "Payments", "integer", 12),
            _num("customer_count", "Müşteri", "Customers", "integer", 10),
            _num("cash", "Nakit", "Cash"),
            _num("credit_card", "Kredi Kartı", "Credit Card"),
            _num("bank_transfer", "Havale/EFT", "Bank Transfer"),
            _num("cheque", "Çek", "Cheque"),
            _num("promissory_note", "Senet", "Promissory Note"),
            _num("other", "Diğer", "Other"),
            _num("total_amount", "Toplam", "Total"),
        ),
        runner=_run_collections,
        filters=(
            F_START, F_END, F_SALESPERSON, F_CUSTOMER,
            FilterDef("payment_method", "Ödeme Tipi", "Payment Method", "select", False, None, "payment_methods"),
        ),
        group_by="bucket_date",
        totals=(
            "payment_count", "customer_count", "cash", "credit_card", "bank_transfer",
            "cheque", "promissory_note", "other", "total_amount",
        ),
        permission=("sales.payments", "VIEW"),
    )
)

_register(
    ReportDef(
        key="receivable_ageing",
        title_tr="Alacak Yaşlandırma ve Risk Raporu",
        title_en="Receivable Ageing & Risk Report",
        module="finance",
        columns=(
            _txt("code", "Kod", "Code", 12),
            _txt("name", "Müşteri", "Customer", 28),
            _txt("salesperson", "Plasiyer", "Salesperson", 20),
            _txt("phone", "Telefon", "Phone", 16),
            _num("document_count", "Belge", "Documents", "integer", 10),
            _num("not_due", "Vadesi Gelmemiş", "Not Due"),
            _num("days_1_30", "1-30 Gün", "1-30 Days"),
            _num("days_31_60", "31-60 Gün", "31-60 Days"),
            _num("days_61_90", "61-90 Gün", "61-90 Days"),
            _num("days_90_plus", "90+ Gün", "90+ Days"),
            _num("overdue_amount", "Vadesi Geçen", "Overdue"),
            _num("open_amount", "Toplam Açık", "Total Open"),
            _num("credit_limit", "Kredi Limiti", "Credit Limit"),
            _num("limit_usage_percent", "Limit Kull. %", "Limit Used %", "percent", 12),
            _num("oldest_days", "En Eski (gün)", "Oldest (days)", "integer", 12),
            _num("risk_score", "Risk Skoru", "Risk Score", "number", 10),
        ),
        runner=_run_receivable_ageing,
        filters=(
            FilterDef("as_of", "Referans Tarihi", "As Of", "date", False),
            F_CUSTOMER, F_REGION, F_LIMIT,
        ),
        group_by="customer_id",
        totals=(
            "document_count", "not_due", "days_1_30", "days_31_60", "days_61_90",
            "days_90_plus", "overdue_amount", "open_amount", "credit_limit",
        ),
        permission=("crm.ledger", "VIEW"),
    )
)

_register(
    ReportDef(
        key="warehouse_stock",
        title_tr="Depo Stok Raporu",
        title_en="Warehouse Stock Report",
        module="stock",
        columns=(
            _txt("warehouse_code", "Depo Kodu", "Warehouse Code", 14),
            _txt("warehouse_name", "Depo", "Warehouse", 22),
            _txt("sku", "Stok Kodu", "SKU", 16),
            _txt("product_name", "Ürün", "Product", 30),
            _txt("base_uom", "Birim", "Unit", 10),
            _num("quantity", "Miktar", "Quantity", "quantity", 12),
            _num("case_quantity", "Koli", "Cases", "quantity", 10),
            _num("reserved_quantity", "Rezerve", "Reserved", "quantity", 10),
            _num("available_quantity", "Kullanılabilir", "Available", "quantity", 12),
            _num("min_stock_level", "Min. Stok", "Min Level", "quantity", 10),
            _num("stock_value", "Stok Değeri", "Stock Value"),
            _txt("status", "Durum", "Status", 10),
            _num("share_percent", "Pay %", "Share %", "percent", 10),
        ),
        runner=_run_warehouse_stock,
        filters=(
            F_WAREHOUSE, F_PRODUCT, F_CATEGORY, F_BRAND,
            FilterDef("include_zero", "Sıfır Stokları Göster", "Include Zero Stock", "bool", False, False),
            F_LIMIT,
        ),
        group_by="warehouse_code",
        totals=("quantity", "case_quantity", "reserved_quantity", "available_quantity", "stock_value"),
        permission=("stock.warehouses", "VIEW"),
    )
)

_register(
    ReportDef(
        key="van_stock",
        title_tr="Araç Stok Raporu",
        title_en="Van Stock Report",
        module="stock",
        columns=(
            _txt("vehicle_code", "Araç Kodu", "Vehicle Code", 14),
            _txt("plate_number", "Plaka", "Plate", 14),
            _txt("salesperson", "Plasiyer", "Salesperson", 22),
            _txt("sku", "Stok Kodu", "SKU", 16),
            _txt("product_name", "Ürün", "Product", 30),
            _txt("base_uom", "Birim", "Unit", 10),
            _num("quantity", "Miktar", "Quantity", "quantity", 12),
            _num("case_quantity", "Koli", "Cases", "quantity", 10),
            _num("stock_value", "Stok Değeri", "Stock Value"),
            _num("share_percent", "Pay %", "Share %", "percent", 10),
        ),
        runner=_run_van_stock,
        filters=(
            FilterDef("vehicle_id", "Araç", "Vehicle", "select", False, None, "vehicles"),
            F_PRODUCT, F_CATEGORY, F_BRAND, F_LIMIT,
        ),
        group_by="vehicle_code",
        totals=("quantity", "case_quantity", "stock_value"),
        permission=("stock.vehicle_stock", "VIEW"),
    )
)

_register(
    ReportDef(
        key="expiry",
        title_tr="SKT (Son Kullanma Tarihi) Raporu",
        title_en="Expiry (Shelf Life) Report",
        module="stock",
        columns=(
            _txt("warehouse_code", "Depo Kodu", "Warehouse Code", 14),
            _txt("warehouse_name", "Depo", "Warehouse", 22),
            _txt("warehouse_type", "Depo Tipi", "Warehouse Type", 14),
            _txt("sku", "Stok Kodu", "SKU", 16),
            _txt("product_name", "Ürün", "Product", 30),
            _txt("lot_number", "Parti No", "Lot No", 16),
            _dat("expiry_date", "SKT", "Expiry"),
            _num("days_to_expiry", "Kalan Gün", "Days Left", "integer", 10),
            _num("quantity", "Miktar", "Quantity", "quantity", 12),
            _num("unit_cost", "Birim Maliyet", "Unit Cost"),
            _num("stock_value", "Risk Tutarı", "Value at Risk"),
            _txt("severity", "Önem", "Severity", 12),
        ),
        runner=_run_expiry,
        filters=(
            FilterDef("days", "Uyarı Ufku (gün)", "Warning Horizon (days)", "int", False, settings.expiry_warning_days),
            F_WAREHOUSE, F_PRODUCT, F_LIMIT,
        ),
        group_by="warehouse_code",
        totals=("quantity", "stock_value"),
        permission=("stock.lots", "VIEW"),
    )
)

_register(
    ReportDef(
        key="wastage",
        title_tr="Fire ve Zayi Raporu",
        title_en="Wastage & Damage Report",
        module="stock",
        columns=(
            _txt("warehouse_code", "Depo Kodu", "Warehouse Code", 14),
            _txt("warehouse_name", "Depo", "Warehouse", 22),
            _txt("sku", "Stok Kodu", "SKU", 16),
            _txt("product_name", "Ürün", "Product", 30),
            _txt("movement_type", "Hareket Tipi", "Movement Type", 16),
            _num("movement_count", "Hareket", "Movements", "integer", 10),
            _num("quantity", "Miktar", "Quantity", "quantity", 12),
            _num("cost_amount", "Maliyet Tutarı", "Cost Amount"),
            _num("share_percent", "Pay %", "Share %", "percent", 10),
        ),
        runner=_run_wastage,
        filters=(F_START, F_END, F_WAREHOUSE, F_PRODUCT, F_LIMIT),
        group_by="warehouse_code",
        totals=("movement_count", "quantity", "cost_amount"),
        permission=("stock.adjustments", "VIEW"),
    )
)

_register(
    ReportDef(
        key="returns",
        title_tr="İade Raporu",
        title_en="Returns Report",
        module="sales",
        columns=(
            _txt("sku", "Stok Kodu", "SKU", 16),
            _txt("product_name", "Ürün", "Product", 30),
            _txt("reason", "İade Nedeni", "Reason", 18),
            _txt("disposition", "Karar", "Disposition", 14),
            _num("document_count", "Belge", "Documents", "integer", 10),
            _num("quantity", "İade Miktarı", "Returned Qty", "quantity", 12),
            _num("net_amount", "Net Tutar", "Net"),
            _num("total_amount", "Toplam", "Total"),
            _num("cost_amount", "Maliyet", "Cost"),
            _num("sold_quantity", "Satış Miktarı", "Sold Qty", "quantity", 12),
            _num("return_rate_percent", "İade Oranı %", "Return Rate %", "percent", 12),
        ),
        runner=_run_returns,
        filters=(
            F_START, F_END, F_SALESPERSON, F_CUSTOMER, F_PRODUCT,
            FilterDef(
                "reason", "İade Nedeni", "Return Reason", "select", False, None, "return_reasons"
            ),
            F_LIMIT,
        ),
        group_by="sku",
        totals=(
            "document_count", "quantity", "net_amount", "total_amount",
            "cost_amount", "sold_quantity", "return_rate_percent",
        ),
        derived_totals={"return_rate_percent": ("quantity", "sold_quantity")},
        permission=("sales.returns", "VIEW"),
    )
)

_register(
    ReportDef(
        key="campaign_performance",
        title_tr="Kampanya Performans Raporu",
        title_en="Campaign Performance Report",
        module="marketing",
        columns=(
            _txt("code", "Kod", "Code", 14),
            _txt("name", "Kampanya", "Campaign", 28),
            _txt("campaign_type", "Tip", "Type", 18),
            _txt("status", "Durum", "Status", 12),
            _dat("start_date", "Başlangıç", "Start"),
            _dat("end_date", "Bitiş", "End"),
            _num("application_count", "Uygulama", "Applications", "integer", 12),
            _num("customer_count", "Müşteri", "Customers", "integer", 10),
            _num("basket_amount", "Sepet Tutarı", "Basket Amount"),
            _num("discount_amount", "İskonto", "Discount"),
            _num("free_goods_quantity", "Bedelsiz Miktar", "Free Goods Qty", "quantity", 14),
            _num("free_goods_cost", "Bedelsiz Maliyet", "Free Goods Cost"),
            _num("investment_amount", "Toplam Yatırım", "Total Investment"),
            _num("cost_percent", "Maliyet %", "Cost %", "percent", 10),
            _num("roi_percent", "ROI %", "ROI %", "percent", 10),
            _num("budget_usage_percent", "Bütçe Kull. %", "Budget Used %", "percent", 12),
        ),
        runner=_run_campaign_performance,
        filters=(
            F_START, F_END,
            FilterDef("campaign_id", "Kampanya", "Campaign", "select", False, None, "campaigns"),
            F_LIMIT,
        ),
        group_by="campaign_id",
        totals=(
            "application_count", "customer_count", "basket_amount", "discount_amount",
            "free_goods_quantity", "free_goods_cost", "investment_amount", "cost_percent",
        ),
        derived_totals={"cost_percent": ("investment_amount", "basket_amount")},
        permission=("marketing.campaigns", "VIEW"),
    )
)

_register(
    ReportDef(
        key="profitability",
        title_tr="Kârlılık Raporu",
        title_en="Profitability Report",
        module="finance",
        columns=(
            _txt("code", "Kod", "Code", 16),
            _txt("name", "Ad", "Name", 30),
            _num("sale_count", "Satış", "Sales", "integer", 10),
            _num("quantity", "Miktar", "Quantity", "quantity", 12),
            _num("gross_amount", "Brüt", "Gross"),
            _num("discount_amount", "İskonto", "Discount"),
            _num("discount_percent", "İskonto %", "Discount %", "percent", 10),
            _num("net_amount", "Net Tutar", "Net"),
            _num("total_cost", "Maliyet", "Cost"),
            _num("cost_percent", "Maliyet %", "Cost %", "percent", 10),
            _num("margin_amount", "Kâr", "Margin"),
            _num("margin_percent", "Kâr %", "Margin %", "percent", 10),
            _num("margin_share_percent", "Kâr Payı %", "Margin Share %", "percent", 12),
        ),
        runner=_run_profitability,
        filters=(
            F_START, F_END,
            FilterDef(
                "dimension", "Kırılım", "Dimension", "select", False, "PRODUCT", "profit_dimensions"
            ),
            F_SALESPERSON, F_CATEGORY, F_BRAND, F_LIMIT,
        ),
        group_by="code",
        totals=(
            "sale_count", "quantity", "gross_amount", "discount_amount", "net_amount",
            "total_cost", "margin_amount", "margin_percent", "discount_percent",
        ),
        derived_totals={
            "margin_percent": ("margin_amount", "net_amount"),
            "discount_percent": ("discount_amount", "gross_amount"),
        },
        permission=("dashboard.financial", "VIEW"),
    )
)

_register(
    ReportDef(
        key="target_achievement",
        title_tr="Hedef Gerçekleşme Raporu",
        title_en="Target Achievement Report",
        module="analytics",
        columns=(
            _txt("subject_type", "Hedef Tipi", "Subject Type", 16),
            _txt("subject_name", "Hedef", "Subject", 26),
            _txt("metric", "Metrik", "Metric", 14),
            _txt("period", "Periyot", "Period", 12),
            _dat("period_start", "Başlangıç", "Start"),
            _dat("period_end", "Bitiş", "End"),
            _num("target_value", "Hedef", "Target"),
            _num("actual_value", "Gerçekleşen", "Actual"),
            _num("gap_amount", "Fark", "Gap"),
            _num("achievement_percent", "Gerçekleşme %", "Achievement %", "percent", 14),
            _num("expected_percent", "Beklenen %", "Expected %", "percent", 12),
            _txt("pace_status", "Durum", "Pace", 12),
            _num("projected_value", "Projeksiyon", "Projected"),
            _num("risk_score", "Risk", "Risk", "number", 10),
            _num("days_remaining", "Kalan Gün", "Days Left", "integer", 10),
        ),
        runner=_run_target_achievement,
        filters=(
            F_START, F_END,
            FilterDef("subject_type", "Hedef Tipi", "Subject Type", "select", False, None, "target_subjects"),
            FilterDef("metric", "Metrik", "Metric", "select", False, None, "target_metrics"),
            F_LIMIT,
        ),
        group_by="subject_type",
        totals=("target_value", "actual_value", "gap_amount", "achievement_percent"),
        derived_totals={"achievement_percent": ("actual_value", "target_value")},
        permission=("analytics.targets", "VIEW"),
    )
)


# ===========================================================================
# Execution
# ===========================================================================
def get_report(key: str) -> ReportDef:
    rdef = REPORTS.get(key)
    if rdef is None:
        raise NotFoundError("report.not_found", params={"key": key})
    return rdef


def list_definitions(lang: str = "tr") -> list[dict[str, Any]]:
    return [r.as_dict(lang) for r in REPORTS.values()]


def _compute_totals(rdef: ReportDef, rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    totals: dict[str, Any] = {}
    by_key = {c.key: c for c in rdef.columns}
    for key in rdef.totals:
        if key in rdef.derived_totals:
            continue
        column = by_key.get(key)
        kind = column.type if column else "number"
        if kind in ("integer",):
            totals[key] = sum(int(D(r.get(key))) for r in rows)
        elif kind in ("money",):
            totals[key] = money(sum((D(r.get(key)) for r in rows), Decimal("0")))
        elif kind in ("quantity",):
            totals[key] = qty(sum((D(r.get(key)) for r in rows), Decimal("0")))
        elif kind == "percent":
            totals[key] = 0.0
        else:
            totals[key] = round(float(sum(D(r.get(key)) for r in rows)), 4)
    for key, (num_key, den_key) in rdef.derived_totals.items():
        numerator = sum((D(r.get(num_key)) for r in rows), Decimal("0"))
        denominator = sum((D(r.get(den_key)) for r in rows), Decimal("0"))
        totals[key] = pct(numerator, denominator)
    return totals


def run(
    db: Session,
    key: str,
    params: dict[str, Any] | None = None,
    *,
    ctx_scope: Any = None,
    lang: str = "tr",
) -> dict[str, Any]:
    """
    Execute a report and return ``{columns, rows, totals, meta}``.

    ``ctx_scope`` accepts a :class:`ReportScope`, a request ``Ctx`` or a plain
    dict — whichever the caller happens to hold.
    """
    from app.models.base import utcnow

    rdef = get_report(key)
    p = ReportParams(params)
    scope = scope_from(ctx_scope)

    rows = rdef.runner(db, p, scope)
    totals = _compute_totals(rdef, rows)
    start, end = p.range()

    return {
        "columns": [c.as_dict(lang) for c in rdef.columns],
        "rows": rows,
        "totals": totals,
        "meta": {
            "key": rdef.key,
            "title": rdef.title(lang),
            "module": rdef.module,
            "group_by": rdef.group_by,
            "row_count": len(rows),
            "generated_at": utcnow().isoformat(timespec="seconds"),
            "language": lang,
            "currency": settings.default_currency,
            "params": dict(p.raw),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "restricted": not scope.unrestricted,
        },
    }


__all__ = [
    "ColumnDef",
    "FilterDef",
    "ReportDef",
    "ReportParams",
    "ReportScope",
    "REPORTS",
    "get_report",
    "list_definitions",
    "run",
    "scope_from",
]
