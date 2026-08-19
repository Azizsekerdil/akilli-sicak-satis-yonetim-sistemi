"""
Customer / CRM API: master data, contacts, notes, current account, ageing,
collection risk and the field-application lookups (nearby, visit plan).

Data scoping
------------
Every endpoint narrows its result set to ``ctx.salesperson_ids`` unless the
caller's role has unrestricted scope, so a salesperson only ever sees the
accounts they are responsible for.  Single-record endpoints raise 403 rather
than 404 for out-of-scope customers: the record exists, the caller simply may
not read it.
"""

from __future__ import annotations

import csv
import io
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query, Response

from app.core.deps import Ctx, Page, get_page, paginated, require
from app.core.enums import AuditAction, CustomerStatus, CustomerType, SalesChannel
from app.core.exceptions import PermissionDeniedError
from app.core.i18n import t
from app.core.utils import display_money
from app.models.customer import Customer
from app.schemas.common import Message, PagedResponse
from app.schemas.customer import (
    AgingCustomerRow,
    AgingOut,
    AgingSummaryOut,
    ChurnItem,
    ContactIn,
    ContactOut,
    CreditLimitUpdate,
    CustomerCreate,
    CustomerListItem,
    CustomerOut,
    CustomerProductItem,
    CustomerStatsOut,
    CustomerUpdate,
    DecliningItem,
    LedgerRow,
    NearbyItem,
    NoteIn,
    NoteOut,
    OverdueItemOut,
    RiskOut,
    SalesHistoryItem,
    StatementOut,
    StatusUpdate,
    TopDebtorOut,
    VisitPlanItem,
)
from app.services import audit_service, customer_service, ledger_service

router = APIRouter(prefix="/customers", tags=["crm"])

#: Excel on Windows only detects UTF-8 CSV when the file starts with a BOM.
_UTF8_BOM = chr(0xFEFF)


# ---------------------------------------------------------------------------
# Scoping helpers
# ---------------------------------------------------------------------------
def _scope_ids(ctx: Ctx) -> list[int] | None:
    """Salesperson ids the caller is limited to, or ``None`` for no limit."""
    if ctx.unrestricted:
        return None
    return ctx.salesperson_ids or None


def _load_scoped(ctx: Ctx, customer_id: int, *, include_deleted: bool = False) -> Customer:
    """Fetch a customer and refuse it when it falls outside the caller's scope."""
    customer = customer_service.get(ctx.db, customer_id, include_deleted=include_deleted)
    allowed = _scope_ids(ctx)
    if allowed is not None and customer.default_salesperson_id not in set(allowed):
        raise PermissionDeniedError(
            "auth.permission_denied",
            params={"resource": "crm.customers", "action": "VIEW"},
        )
    return customer


# ===========================================================================
# Collection & analysis lists
# (declared before "/{customer_id}" so the literal paths win the match)
# ===========================================================================
@router.get(
    "/export",
    response_class=Response,
    summary="Export customers as CSV / Müşterileri CSV olarak dışa aktar",
)
def export_customers(
    ctx: Ctx = Depends(require("crm.customers", "EXPORT")),
    term: str | None = Query(default=None, max_length=128),
    customer_type: CustomerType | None = None,
    channel: SalesChannel | None = None,
    status: CustomerStatus | None = None,
    region_id: int | None = None,
    route_id: int | None = None,
    salesperson_id: int | None = None,
    city: str | None = Query(default=None, max_length=96),
    has_debt: bool | None = None,
    limit: int = Query(default=5000, ge=1, le=50_000),
) -> Response:
    """Flat CSV of the filtered customer list, UTF-8 with BOM so Excel opens it correctly."""
    rows, total = customer_service.search(
        ctx.db,
        term=term,
        customer_type=customer_type,
        channel=channel,
        status=status,
        region_id=region_id,
        route_id=route_id,
        salesperson_id=salesperson_id,
        city=city,
        has_debt=has_debt,
        salesperson_ids=_scope_ids(ctx),
        page=1,
        size=limit,
    )

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(
        [
            "code", "name", "trade_name", "customer_type", "channel", "status",
            "tax_office", "tax_number", "city", "district", "address",
            "phone", "mobile", "email", "latitude", "longitude",
            "salesperson_id", "route_id", "visit_days", "payment_method",
            "payment_term_days", "credit_limit", "balance", "overdue_balance",
            "total_sales_amount", "order_count", "last_order_date", "risk_score",
        ]
    )
    for c in rows:
        writer.writerow(
            [
                c.code, c.name, c.trade_name or "", c.customer_type, c.channel, c.status,
                c.tax_office or "", c.tax_number or "", c.city or "", c.district or "",
                (c.address or "").replace("\n", " ").replace("\r", " "),
                c.phone or "", c.mobile or "", c.email or "",
                c.latitude if c.latitude is not None else "",
                c.longitude if c.longitude is not None else "",
                c.default_salesperson_id or "", c.default_route_id or "",
                c.visit_days or "", c.payment_method, c.payment_term_days,
                display_money(c.credit_limit), display_money(c.balance),
                display_money(c.overdue_balance), display_money(c.total_sales_amount),
                c.order_count,
                c.last_order_date.isoformat() if c.last_order_date else "",
                round(float(c.risk_score or 0.0), 2),
            ]
        )

    audit_service.record(
        ctx.db,
        AuditAction.EXPORT,
        entity_type="customer",
        entity_label="customer list",
        summary=f"exported {len(rows)} of {total} customers",
        **ctx.audit_kwargs(),
    )
    ctx.db.commit()

    return Response(
        content=_UTF8_BOM + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="customers-{date.today().isoformat()}.csv"'
            )
        },
    )


@router.get(
    "/nearby",
    response_model=list[NearbyItem],
    summary="Customers near a point / Yakındaki müşteriler",
)
def nearby_customers(
    latitude: float = Query(ge=-90, le=90),
    longitude: float = Query(ge=-180, le=180),
    radius_km: float = Query(default=5.0, gt=0, le=200),
    limit: int = Query(default=50, ge=1, le=500),
    ctx: Ctx = Depends(require("crm.customers", "VIEW")),
) -> list[NearbyItem]:
    """Nearest-first list used by the field application's map screen."""
    hits = customer_service.nearby(
        ctx.db,
        latitude,
        longitude,
        radius_km,
        salesperson_ids=_scope_ids(ctx),
        limit=limit,
    )
    return [
        NearbyItem(
            customer=CustomerListItem.model_validate(customer), distance_km=distance
        )
        for customer, distance in hits
    ]


@router.get(
    "/churn-risk",
    response_model=list[ChurnItem],
    summary="Customers that stopped ordering / Sipariş vermeyi bırakanlar",
)
def churn_risk(
    days: int = Query(default=90, ge=7, le=730),
    limit: int = Query(default=100, ge=1, le=500),
    ctx: Ctx = Depends(require("crm.customers", "VIEW")),
) -> list[ChurnItem]:
    rows = customer_service.churn_candidates(
        ctx.db, days=days, salesperson_ids=_scope_ids(ctx), limit=limit
    )
    return [
        ChurnItem(
            customer=CustomerListItem.model_validate(row["customer"]),
            days_since_last_order=row["days_since_last_order"],
            last_order_date=row["last_order_date"],
            total_sales_amount=row["total_sales_amount"],
            balance=row["balance"],
        )
        for row in rows
    ]


@router.get(
    "/declining",
    response_model=list[DecliningItem],
    summary="Customers with falling turnover / Cirosu düşen müşteriler",
)
def declining_customers(
    days: int = Query(default=30, ge=7, le=365),
    drop_percent: float = Query(default=20.0, ge=1, le=100),
    limit: int = Query(default=100, ge=1, le=500),
    ctx: Ctx = Depends(require("crm.customers", "VIEW")),
) -> list[DecliningItem]:
    rows = customer_service.declining(
        ctx.db,
        days=days,
        drop_percent=drop_percent,
        salesperson_ids=_scope_ids(ctx),
        limit=limit,
    )
    return [
        DecliningItem(
            customer=CustomerListItem.model_validate(row["customer"]),
            current_amount=row["current_amount"],
            previous_amount=row["previous_amount"],
            drop_percent=row["drop_percent"],
            days=row["days"],
        )
        for row in rows
    ]


@router.get(
    "/overdue",
    response_model=list[OverdueItemOut],
    summary="Overdue open items / Vadesi geçmiş açık kalemler",
)
def overdue_items(
    min_days: int = Query(default=1, ge=0, le=3650),
    limit: int = Query(default=200, ge=1, le=1000),
    ctx: Ctx = Depends(require("crm.ledger", "VIEW")),
) -> list[OverdueItemOut]:
    rows = ledger_service.overdue_list(
        ctx.db, min_days=min_days, salesperson_ids=_scope_ids(ctx), limit=limit
    )
    return [OverdueItemOut(**row) for row in rows]


@router.get(
    "/top-debtors",
    response_model=list[TopDebtorOut],
    summary="Largest open balances / En yüksek bakiyeler",
)
def top_debtors(
    limit: int = Query(default=20, ge=1, le=200),
    min_amount: Decimal = Query(default=Decimal("0"), ge=0),
    ctx: Ctx = Depends(require("crm.ledger", "VIEW")),
) -> list[TopDebtorOut]:
    rows = ledger_service.top_debtors(
        ctx.db, limit, salesperson_ids=_scope_ids(ctx), min_amount=min_amount
    )
    return [TopDebtorOut(**row) for row in rows]


@router.get(
    "/aging-summary",
    response_model=AgingSummaryOut,
    summary="Receivable ageing / Yaşlandırma özeti",
)
def aging_summary(
    as_of: date | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    ctx: Ctx = Depends(require("crm.ledger", "VIEW")),
) -> AgingSummaryOut:
    """Company (or scope) wide ageing, plus the per-customer breakdown."""
    reference = as_of or date.today()
    scope = _scope_ids(ctx)
    totals = ledger_service.aging(ctx.db, as_of=reference, salesperson_ids=scope)
    per_customer = ledger_service.aging_by_customer(
        ctx.db, as_of=reference, salesperson_ids=scope, limit=limit
    )
    return AgingSummaryOut(
        as_of=reference,
        totals=AgingOut(**totals),
        customers=[AgingCustomerRow(**row) for row in per_customer],
    )


@router.get(
    "/visit-plan",
    response_model=list[VisitPlanItem],
    summary="Customers due on a weekday / Gün planındaki müşteriler",
)
def visit_plan(
    weekday: str = Query(min_length=3, max_length=3, description="MON|TUE|WED|THU|FRI|SAT|SUN"),
    salesperson_id: int | None = None,
    route_id: int | None = None,
    ctx: Ctx = Depends(require("crm.customers", "VIEW")),
) -> list[VisitPlanItem]:
    rows = customer_service.visit_plan(
        ctx.db,
        weekday=weekday,
        salesperson_id=salesperson_id,
        salesperson_ids=_scope_ids(ctx),
        route_id=route_id,
    )
    return [VisitPlanItem.model_validate(row) for row in rows]


# ===========================================================================
# List & CRUD
# ===========================================================================
@router.get(
    "",
    response_model=PagedResponse[CustomerListItem],
    summary="Search customers / Müşteri ara",
)
def list_customers(
    ctx: Ctx = Depends(require("crm.customers", "VIEW")),
    page: Page = Depends(get_page),
    term: str | None = Query(default=None, max_length=128, description="name, code, phone, tax no"),
    customer_type: CustomerType | None = None,
    channel: SalesChannel | None = None,
    status: CustomerStatus | None = None,
    region_id: int | None = None,
    route_id: int | None = None,
    salesperson_id: int | None = None,
    city: str | None = Query(default=None, max_length=96),
    has_debt: bool | None = None,
    order_by: str = Query(default="name", pattern="^(name|code|balance|overdue|sales|last_order|risk|created)$"),
) -> Any:
    rows, total = customer_service.search(
        ctx.db,
        term=term,
        customer_type=customer_type,
        channel=channel,
        status=status,
        region_id=region_id,
        route_id=route_id,
        salesperson_id=salesperson_id,
        city=city,
        has_debt=has_debt,
        salesperson_ids=_scope_ids(ctx),
        order_by=order_by,
        page=page.page,
        size=page.size,
    )
    return paginated([CustomerListItem.model_validate(r) for r in rows], total, page)


@router.post(
    "",
    response_model=CustomerOut,
    status_code=201,
    summary="Create a customer / Müşteri oluştur",
)
def create_customer(
    payload: CustomerCreate,
    ctx: Ctx = Depends(require("crm.customers", "CREATE")),
) -> CustomerOut:
    """
    Register a new customer.

    Setting a non-zero credit limit at creation time is a credit decision, so
    it additionally requires ``crm.credit_limit:UPDATE``.
    """
    data = payload.model_dump(exclude={"code", "credit_limit"}, exclude_unset=False)
    if payload.credit_limit and payload.credit_limit > 0:
        ctx.check("crm.credit_limit", "UPDATE")

    customer = customer_service.create(
        ctx.db,
        data,
        code=payload.code,
        credit_limit=payload.credit_limit,
        user_id=ctx.user_id,
        audit_extra={
            k: v for k, v in ctx.audit_kwargs().items() if k != "user_id"
        },
    )
    return CustomerOut.model_validate(customer)


@router.get(
    "/{customer_id}",
    response_model=CustomerOut,
    summary="Customer detail / Müşteri kartı",
)
def get_customer(
    customer_id: int,
    ctx: Ctx = Depends(require("crm.customers", "VIEW")),
) -> CustomerOut:
    return CustomerOut.model_validate(_load_scoped(ctx, customer_id))


@router.put(
    "/{customer_id}",
    response_model=CustomerOut,
    summary="Update a customer / Müşteriyi güncelle",
)
def update_customer(
    customer_id: int,
    payload: CustomerUpdate,
    ctx: Ctx = Depends(require("crm.customers", "UPDATE")),
) -> CustomerOut:
    _load_scoped(ctx, customer_id)
    customer = customer_service.update(
        ctx.db,
        customer_id,
        payload.model_dump(exclude_unset=True),
        user_id=ctx.user_id,
        audit_extra={k: v for k, v in ctx.audit_kwargs().items() if k != "user_id"},
    )
    return CustomerOut.model_validate(customer)


@router.delete(
    "/{customer_id}",
    response_model=Message,
    summary="Delete a customer / Müşteriyi sil",
)
def delete_customer(
    customer_id: int,
    ctx: Ctx = Depends(require("crm.customers", "DELETE")),
) -> Message:
    """Soft delete — refused while the account still carries a balance."""
    _load_scoped(ctx, customer_id)
    customer_service.soft_delete(
        ctx.db,
        customer_id,
        user_id=ctx.user_id,
        audit_extra={k: v for k, v in ctx.audit_kwargs().items() if k != "user_id"},
    )
    return Message(message=t("common.deleted", ctx.lang), message_key="common.deleted")


@router.put(
    "/{customer_id}/credit-limit",
    response_model=CustomerOut,
    summary="Change the credit limit / Kredi limitini değiştir",
)
def update_credit_limit(
    customer_id: int,
    payload: CreditLimitUpdate,
    ctx: Ctx = Depends(require("crm.credit_limit", "UPDATE")),
) -> CustomerOut:
    _load_scoped(ctx, customer_id)
    customer = customer_service.set_credit_limit(
        ctx.db,
        customer_id,
        payload.credit_limit,
        risk_limit=payload.risk_limit,
        reason=payload.reason,
        user_id=ctx.user_id,
        audit_extra={k: v for k, v in ctx.audit_kwargs().items() if k != "user_id"},
    )
    return CustomerOut.model_validate(customer)


@router.put(
    "/{customer_id}/status",
    response_model=CustomerOut,
    summary="Block / unblock a customer / Müşteriyi blokla",
)
def update_status(
    customer_id: int,
    payload: StatusUpdate,
    ctx: Ctx = Depends(require("crm.customers", "UPDATE")),
) -> CustomerOut:
    """Blocking is what stops further credit sales, so it is audited separately."""
    _load_scoped(ctx, customer_id)
    customer = customer_service.set_status(
        ctx.db,
        customer_id,
        str(payload.status),
        user_id=ctx.user_id,
        audit_extra={k: v for k, v in ctx.audit_kwargs().items() if k != "user_id"},
    )
    return CustomerOut.model_validate(customer)


# ===========================================================================
# Current account
# ===========================================================================
@router.get(
    "/{customer_id}/ledger",
    response_model=PagedResponse[LedgerRow],
    summary="Current-account movements / Cari hesap hareketleri",
)
def customer_ledger(
    customer_id: int,
    ctx: Ctx = Depends(require("crm.ledger", "VIEW")),
    page: Page = Depends(get_page),
    start: date | None = None,
    end: date | None = None,
    entry_type: str | None = Query(default=None, max_length=24),
    only_open: bool = False,
) -> Any:
    _load_scoped(ctx, customer_id, include_deleted=True)
    rows, total = ledger_service.entries(
        ctx.db,
        customer_id,
        start=start,
        end=end,
        entry_type=entry_type,
        only_open=only_open,
        page=page.page,
        size=page.size,
    )
    return paginated([LedgerRow.model_validate(r) for r in rows], total, page)


@router.get(
    "/{customer_id}/statement",
    response_model=StatementOut,
    summary="Account statement / Hesap ekstresi",
)
def customer_statement(
    customer_id: int,
    ctx: Ctx = Depends(require("crm.ledger", "VIEW")),
    start: date | None = Query(default=None),
    end: date | None = Query(default=None),
) -> StatementOut:
    """Defaults to the last 90 days when no period is given."""
    _load_scoped(ctx, customer_id, include_deleted=True)
    end_date = end or date.today()
    start_date = start or (end_date - timedelta(days=90))
    return StatementOut(
        **ledger_service.statement(ctx.db, customer_id, start_date, end_date)
    )


@router.get(
    "/{customer_id}/aging",
    response_model=AgingOut,
    summary="Customer ageing / Müşteri yaşlandırma",
)
def customer_aging(
    customer_id: int,
    ctx: Ctx = Depends(require("crm.ledger", "VIEW")),
    as_of: date | None = Query(default=None),
) -> AgingOut:
    _load_scoped(ctx, customer_id, include_deleted=True)
    return AgingOut(**ledger_service.aging(ctx.db, customer_id=customer_id, as_of=as_of))


@router.get(
    "/{customer_id}/risk",
    response_model=RiskOut,
    summary="Collection risk / Tahsilat riski",
)
def customer_risk(
    customer_id: int,
    ctx: Ctx = Depends(require("crm.risk", "VIEW")),
    refresh: bool = Query(default=True, description="persist the recomputed score"),
) -> RiskOut:
    """Explainable risk score with the inputs that produced it."""
    customer = _load_scoped(ctx, customer_id, include_deleted=True)
    detail = ledger_service.risk_detail(ctx.db, customer)
    if refresh:
        ledger_service.refresh_risk(ctx.db, customer, commit=True)
    return RiskOut(**{**detail, "aging": AgingOut(**detail["aging"])})


# ===========================================================================
# History
# ===========================================================================
@router.get(
    "/{customer_id}/sales-history",
    response_model=list[SalesHistoryItem],
    summary="Sales history / Satış geçmişi",
)
def customer_sales_history(
    customer_id: int,
    ctx: Ctx = Depends(require("crm.customers", "VIEW")),
    start: date | None = None,
    end: date | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[SalesHistoryItem]:
    _load_scoped(ctx, customer_id, include_deleted=True)
    rows = customer_service.sales_history(
        ctx.db, customer_id, start=start, end=end, limit=limit
    )
    return [SalesHistoryItem.model_validate(r) for r in rows]


@router.get(
    "/{customer_id}/products",
    response_model=list[CustomerProductItem],
    summary="Products bought / Aldığı ürünler",
)
def customer_products(
    customer_id: int,
    ctx: Ctx = Depends(require("crm.customers", "VIEW")),
    start: date | None = None,
    end: date | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[CustomerProductItem]:
    """The customer's SKU basket — feeds the 'usual order' suggestion."""
    _load_scoped(ctx, customer_id, include_deleted=True)
    return [
        CustomerProductItem(**row)
        for row in customer_service.purchased_products(
            ctx.db, customer_id, start=start, end=end, limit=limit
        )
    ]


@router.post(
    "/{customer_id}/refresh-stats",
    response_model=CustomerStatsOut,
    summary="Recompute derived state / Türetilmiş alanları yenile",
)
def refresh_stats(
    customer_id: int,
    ctx: Ctx = Depends(require("crm.customers", "UPDATE")),
) -> CustomerStatsOut:
    """Rebuild balance and sales counters from the source rows."""
    _load_scoped(ctx, customer_id, include_deleted=True)
    customer = customer_service.refresh_all_stats(ctx.db, customer_id)
    ctx.db.commit()
    return CustomerStatsOut(
        customer_id=customer.id,
        balance=customer.balance,
        overdue_balance=customer.overdue_balance,
        order_count=customer.order_count,
        total_sales_amount=customer.total_sales_amount,
        average_order_value=customer.average_order_value,
        first_order_date=customer.first_order_date,
        last_order_date=customer.last_order_date,
        extra={"last_payment_date": customer.last_payment_date},
    )


# ===========================================================================
# Contacts & notes
# ===========================================================================
@router.get(
    "/{customer_id}/contacts",
    response_model=list[ContactOut],
    summary="Contacts / İlgili kişiler",
)
def list_contacts(
    customer_id: int,
    ctx: Ctx = Depends(require("crm.customers", "VIEW")),
) -> list[ContactOut]:
    _load_scoped(ctx, customer_id, include_deleted=True)
    return [
        ContactOut.model_validate(c) for c in customer_service.list_contacts(ctx.db, customer_id)
    ]


@router.post(
    "/{customer_id}/contacts",
    response_model=ContactOut,
    status_code=201,
    summary="Add a contact / İlgili kişi ekle",
)
def add_contact(
    customer_id: int,
    payload: ContactIn,
    ctx: Ctx = Depends(require("crm.customers", "UPDATE")),
) -> ContactOut:
    _load_scoped(ctx, customer_id)
    contact = customer_service.add_contact(
        ctx.db, customer_id, payload.model_dump(), user_id=ctx.user_id
    )
    return ContactOut.model_validate(contact)


@router.delete(
    "/{customer_id}/contacts/{contact_id}",
    response_model=Message,
    summary="Remove a contact / İlgili kişiyi sil",
)
def delete_contact(
    customer_id: int,
    contact_id: int,
    ctx: Ctx = Depends(require("crm.customers", "UPDATE")),
) -> Message:
    _load_scoped(ctx, customer_id)
    customer_service.delete_contact(ctx.db, contact_id, customer_id=customer_id)
    return Message(message=t("common.deleted", ctx.lang), message_key="common.deleted")


@router.get(
    "/{customer_id}/notes",
    response_model=list[NoteOut],
    summary="Notes / Notlar",
)
def list_notes(
    customer_id: int,
    ctx: Ctx = Depends(require("crm.customers", "VIEW")),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[NoteOut]:
    _load_scoped(ctx, customer_id, include_deleted=True)
    return [
        NoteOut.model_validate(n)
        for n in customer_service.list_notes(ctx.db, customer_id, limit=limit)
    ]


@router.post(
    "/{customer_id}/notes",
    response_model=NoteOut,
    status_code=201,
    summary="Add a note / Not ekle",
)
def add_note(
    customer_id: int,
    payload: NoteIn,
    ctx: Ctx = Depends(require("crm.customers", "UPDATE")),
) -> NoteOut:
    _load_scoped(ctx, customer_id)
    note = customer_service.add_note(
        ctx.db, customer_id, payload.model_dump(), user_id=ctx.user_id
    )
    return NoteOut.model_validate(note)


@router.delete(
    "/{customer_id}/notes/{note_id}",
    response_model=Message,
    summary="Remove a note / Notu sil",
)
def delete_note(
    customer_id: int,
    note_id: int,
    ctx: Ctx = Depends(require("crm.customers", "UPDATE")),
) -> Message:
    _load_scoped(ctx, customer_id)
    customer_service.delete_note(ctx.db, note_id, customer_id=customer_id)
    return Message(message=t("common.deleted", ctx.lang), message_key="common.deleted")
