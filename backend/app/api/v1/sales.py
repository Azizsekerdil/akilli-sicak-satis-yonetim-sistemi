"""
Sales & collections API: hot sale, orders, deliveries, invoices, payments and
returns.

Literal sub-paths are declared before ``/{sale_id}`` so ``/sales/invoices``
never resolves as a sale id.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import select

from app.core.deps import Ctx, Page, get_page, paginated, require, require_any
from app.core.enums import (
    DocumentType,
    InvoiceStatus,
    OrderStatus,
    OrderType,
    PaymentMethod,
    PaymentStatus,
    ReturnDisposition,
    ReturnReason,
)
from app.core.exceptions import AppError, PermissionDeniedError, ValidationError
from app.core.i18n import t
from app.core.utils import display_money
from app.models.sales import Invoice, Order, Payment, PaymentAllocation, ReturnDocument, Sale
from app.models.vehicle import Salesperson
from app.schemas.common import PagedResponse
from app.schemas.sales import (
    AllocationOut,
    CancelIn,
    CollectionsSummaryOut,
    DailySummaryOut,
    HotSaleIn,
    HotSaleOut,
    InvoiceListItem,
    InvoiceOut,
    OpenInvoiceOut,
    OrderCreate,
    OrderDeliverIn,
    OrderListItem,
    OrderOut,
    PaymentCreate,
    PaymentOut,
    ReturnCreate,
    ReturnListItem,
    ReturnOut,
    SaleListItem,
    SaleOut,
)
from app.services import (
    invoice_service,
    payment_service,
    return_service,
    sales_service,
)

router = APIRouter(prefix="/sales", tags=["sales"])


# ===========================================================================
# Helpers
# ===========================================================================
def _scope_ids(ctx: Ctx) -> list[int] | None:
    """
    Salesperson ids the caller may see, or None when unrestricted.

    A restricted caller with no salesperson profile gets an id that matches
    nothing rather than an empty filter — an empty list would silently widen a
    REGION/TEAM/OWN scope into "everything".
    """
    if ctx.unrestricted:
        return None
    return ctx.salesperson_ids or [-1]


def _assert_in_scope(ctx: Ctx, salesperson_id: int | None) -> None:
    allowed = _scope_ids(ctx)
    if salesperson_id is not None and allowed is not None and salesperson_id not in allowed:
        raise PermissionDeniedError(
            "auth.permission_denied",
            params={"resource": "sales", "action": "SCOPE"},
        )


def _acting_salesperson_id(ctx: Ctx, explicit: int | None) -> int:
    """Who is selling: the requested salesperson, or the caller's own profile."""
    if explicit:
        _assert_in_scope(ctx, explicit)
        return explicit
    own = ctx.db.execute(
        select(Salesperson.id).where(
            Salesperson.user_id == ctx.user.id, Salesperson.is_deleted.is_(False)
        )
    ).scalar_one_or_none()
    if own is None:
        raise ValidationError("salesperson.not_linked_to_user", params={"user": ctx.user.username})
    return int(own)


def _order_out(order: Order) -> OrderOut:
    data = OrderOut.model_validate(order)
    data.customer_name = order.customer.name if order.customer else None
    return data


def _order_list_item(order: Order) -> OrderListItem:
    data = OrderListItem.model_validate(order)
    data.customer_name = order.customer.name if order.customer else None
    return data


def _sale_out(sale: Sale) -> SaleOut:
    data = SaleOut.model_validate(sale)
    data.customer_name = sale.customer.name if sale.customer else None
    return data


def _sale_list_item(sale: Sale) -> SaleListItem:
    data = SaleListItem.model_validate(sale)
    data.customer_name = sale.customer.name if sale.customer else None
    return data


def _invoice_out(invoice: Invoice) -> InvoiceOut:
    data = InvoiceOut.model_validate(invoice)
    data.customer_name = invoice.customer.name if invoice.customer else None
    return data


def _invoice_list_item(invoice: Invoice) -> InvoiceListItem:
    data = InvoiceListItem.model_validate(invoice)
    data.customer_name = invoice.customer.name if invoice.customer else None
    return data


def _allocation_out(alloc: PaymentAllocation) -> AllocationOut:
    data = AllocationOut.model_validate(alloc)
    if alloc.invoice is not None:
        data.invoice_no = alloc.invoice.invoice_no
        data.invoice_date = alloc.invoice.invoice_date
    return data


def _payment_out(payment: Payment) -> PaymentOut:
    data = PaymentOut.model_validate(payment)
    data.customer_name = payment.customer.name if payment.customer else None
    data.allocations = [_allocation_out(a) for a in payment.allocations]
    return data


def _return_out(ctx: Ctx, doc: ReturnDocument) -> ReturnOut:
    data = ReturnOut.model_validate(doc)
    data.customer_name = doc.customer.name if doc.customer else None
    note = return_service.credit_note_for(ctx.db, doc.id)
    data.credit_note = _invoice_out(note) if note is not None else None
    return data


def _return_list_item(doc: ReturnDocument) -> ReturnListItem:
    data = ReturnListItem.model_validate(doc)
    data.customer_name = doc.customer.name if doc.customer else None
    return data


# ===========================================================================
# Hot sale — the flagship
# ===========================================================================
@router.post(
    "/hot-sale",
    response_model=HotSaleOut,
    status_code=status.HTTP_201_CREATED,
    summary="Sıcak satış / Hot sale (order + delivery + invoice + payment)",
)
def hot_sale(
    payload: HotSaleIn,
    ctx: Ctx = Depends(require("sales.hot_sale", "CREATE")),
) -> HotSaleOut:
    """Sell from the van in one atomic call and return the whole document chain."""
    salesperson_id = _acting_salesperson_id(ctx, payload.salesperson_id)
    vehicle_id = payload.vehicle_id or _resolve_vehicle_id(ctx, salesperson_id)

    result = sales_service.hot_sale(
        ctx.db,
        customer_id=payload.customer_id,
        lines=[line.to_line() for line in payload.lines],
        salesperson_id=salesperson_id,
        vehicle_id=vehicle_id,
        payment=(
            {
                "method": str(payload.payment.method),
                "amount": payload.payment.amount,
                "bank_name": payload.payment.bank_name,
                "document_number": payload.payment.document_number,
                "maturity_date": payload.payment.maturity_date,
                "drawer_name": payload.payment.drawer_name,
                "reference": payload.payment.reference,
                "notes": payload.payment.notes,
            }
            if payload.payment
            else None
        ),
        route_id=payload.route_id,
        visit_id=payload.visit_id,
        latitude=payload.latitude,
        longitude=payload.longitude,
        header_discount_percent=payload.header_discount_percent,
        notes=payload.notes,
        user_id=ctx.user.id,
    )
    invoice = result["invoice"]
    payment = result["payment"]
    return HotSaleOut(
        order=_order_out(result["order"]),
        sale=_sale_out(result["sale"]),
        invoice=_invoice_out(invoice) if invoice is not None else None,
        payment=_payment_out(payment) if payment is not None else None,
        stock_movements=int(result["stock_movements"]),
    )


def _resolve_vehicle_id(ctx: Ctx, salesperson_id: int) -> int:
    """Fall back to the open day session's van, then the default vehicle."""
    from app.services import day_session_service

    session = day_session_service.get_open_session(ctx.db, salesperson_id=salesperson_id)
    if session is not None and session.vehicle_id:
        return int(session.vehicle_id)

    salesperson = ctx.db.get(Salesperson, salesperson_id)
    if salesperson is not None and salesperson.default_vehicle_id:
        return int(salesperson.default_vehicle_id)
    raise ValidationError("vehicle.required", params={"salesperson_id": salesperson_id})


# ===========================================================================
# Orders
# ===========================================================================
@router.get(
    "/orders",
    response_model=PagedResponse[OrderListItem],
    summary="Sipariş listesi / List orders",
)
def list_orders(
    ctx: Ctx = Depends(require("sales.orders", "VIEW")),
    page: Page = Depends(get_page),
    start: date | None = None,
    end: date | None = None,
    customer_id: int | None = None,
    salesperson_id: int | None = None,
    order_type: OrderType | None = None,
    order_status: OrderStatus | None = Query(default=None, alias="status"),
    open_only: bool = False,
    search: str | None = None,
) -> Any:
    _assert_in_scope(ctx, salesperson_id)
    rows, total = sales_service.list_orders(
        ctx.db,
        start=start,
        end=end,
        customer_id=customer_id,
        salesperson_id=salesperson_id,
        order_type=str(order_type) if order_type else None,
        status=str(order_status) if order_status else None,
        open_only=open_only,
        search=search,
        salesperson_ids=_scope_ids(ctx),
        offset=page.offset,
        limit=page.limit,
    )
    return paginated([_order_list_item(r) for r in rows], total, page)


@router.post(
    "/orders",
    response_model=OrderOut,
    status_code=status.HTTP_201_CREATED,
    summary="Sipariş oluştur / Create order",
)
def create_order(
    payload: OrderCreate,
    ctx: Ctx = Depends(require("sales.orders", "CREATE")),
) -> OrderOut:
    salesperson_id = payload.salesperson_id
    if salesperson_id is None and not ctx.unrestricted:
        salesperson_id = _acting_salesperson_id(ctx, None)
    _assert_in_scope(ctx, salesperson_id)

    order = sales_service.create_order(
        ctx.db,
        customer_id=payload.customer_id,
        lines=[line.to_line() for line in payload.lines],
        order_type=str(payload.order_type),
        salesperson_id=salesperson_id,
        vehicle_id=payload.vehicle_id,
        warehouse_id=payload.warehouse_id,
        route_id=payload.route_id,
        visit_id=payload.visit_id,
        day_session_id=payload.day_session_id,
        payment_method=str(payload.payment_method),
        header_discount_percent=payload.header_discount_percent,
        delivery_date=payload.delivery_date,
        notes=payload.notes,
        user_id=ctx.user.id,
    )
    ctx.db.commit()
    return _order_out(order)


@router.get("/orders/{order_id}", response_model=OrderOut, summary="Sipariş detayı / Order detail")
def get_order(
    order_id: int,
    ctx: Ctx = Depends(require("sales.orders", "VIEW")),
) -> OrderOut:
    order = sales_service.get_order(ctx.db, order_id)
    _assert_in_scope(ctx, order.salesperson_id)
    return _order_out(order)


@router.put("/orders/{order_id}", response_model=OrderOut, summary="Sipariş güncelle / Update order")
def update_order(
    order_id: int,
    payload: OrderCreate,
    ctx: Ctx = Depends(require("sales.orders", "UPDATE")),
) -> OrderOut:
    order = sales_service.get_order(ctx.db, order_id)
    _assert_in_scope(ctx, order.salesperson_id)
    sales_service.update_order(
        ctx.db,
        order,
        lines=[line.to_line() for line in payload.lines],
        payment_method=str(payload.payment_method),
        header_discount_percent=payload.header_discount_percent,
        delivery_date=payload.delivery_date,
        notes=payload.notes,
        user_id=ctx.user.id,
    )
    ctx.db.commit()
    return _order_out(order)


@router.delete(
    "/orders/{order_id}",
    response_model=OrderOut,
    summary="Sipariş iptal / Cancel order",
)
def cancel_order(
    order_id: int,
    reason: str = Query(min_length=3, max_length=255),
    ctx: Ctx = Depends(require("sales.orders", "DELETE")),
) -> OrderOut:
    order = sales_service.get_order(ctx.db, order_id)
    _assert_in_scope(ctx, order.salesperson_id)
    sales_service.cancel_order(ctx.db, order, reason=reason, user_id=ctx.user.id)
    ctx.db.commit()
    return _order_out(order)


@router.post(
    "/orders/{order_id}/deliver",
    response_model=HotSaleOut,
    summary="Siparişi teslim et / Deliver order",
)
def deliver_order(
    order_id: int,
    payload: OrderDeliverIn,
    ctx: Ctx = Depends(require("sales.sales", "CREATE")),
) -> HotSaleOut:
    """Post the stock movements and raise the invoice for a pre-sale order."""
    order = sales_service.get_order(ctx.db, order_id)
    _assert_in_scope(ctx, order.salesperson_id)

    # A pre-sale order captured in the field carries no depot; delivering it
    # means handing goods over from the caller's own van.
    vehicle_id = payload.vehicle_id or order.vehicle_id
    if payload.warehouse_id is None and order.warehouse_id is None and vehicle_id is None:
        vehicle_id = _resolve_vehicle_id(ctx, _acting_salesperson_id(ctx, order.salesperson_id))

    result = sales_service.deliver_order(
        ctx.db,
        order,
        warehouse_id=payload.warehouse_id,
        vehicle_id=vehicle_id,
        create_invoice=payload.create_invoice,
        user_id=ctx.user.id,
    )
    invoice = result["invoice"]
    return HotSaleOut(
        order=_order_out(result["order"]),
        sale=_sale_out(result["sale"]),
        invoice=_invoice_out(invoice) if invoice is not None else None,
        payment=None,
        stock_movements=int(result["stock_movements"]),
    )


# ===========================================================================
# Invoices
# ===========================================================================
@router.get(
    "/invoices",
    response_model=PagedResponse[InvoiceListItem],
    summary="Fatura listesi / List invoices",
)
def list_invoices(
    ctx: Ctx = Depends(require("sales.invoices", "VIEW")),
    page: Page = Depends(get_page),
    start: date | None = None,
    end: date | None = None,
    customer_id: int | None = None,
    salesperson_id: int | None = None,
    sale_id: int | None = None,
    document_type: DocumentType | None = None,
    invoice_status: InvoiceStatus | None = Query(default=None, alias="status"),
    only_open: bool = False,
    search: str | None = None,
) -> Any:
    _assert_in_scope(ctx, salesperson_id)
    rows, total = invoice_service.list_invoices(
        ctx.db,
        customer_id=customer_id,
        salesperson_id=salesperson_id,
        sale_id=sale_id,
        document_type=str(document_type) if document_type else None,
        status=str(invoice_status) if invoice_status else None,
        start=start,
        end=end,
        only_open=only_open,
        search=search,
        salesperson_ids=_scope_ids(ctx),
        offset=page.offset,
        limit=page.limit,
    )
    return paginated([_invoice_list_item(r) for r in rows], total, page)


@router.get(
    "/invoices/{invoice_id}",
    response_model=InvoiceOut,
    summary="Fatura detayı / Invoice detail",
)
def get_invoice(
    invoice_id: int,
    ctx: Ctx = Depends(require("sales.invoices", "VIEW")),
) -> InvoiceOut:
    invoice = invoice_service.get(ctx.db, invoice_id)
    _assert_in_scope(ctx, invoice.salesperson_id)
    return _invoice_out(invoice)


@router.post(
    "/invoices/{invoice_id}/cancel",
    response_model=InvoiceOut,
    summary="Fatura iptal / Cancel invoice",
)
def cancel_invoice(
    invoice_id: int,
    payload: CancelIn,
    ctx: Ctx = Depends(require("sales.invoices", "UPDATE")),
) -> InvoiceOut:
    invoice = invoice_service.get(ctx.db, invoice_id)
    _assert_in_scope(ctx, invoice.salesperson_id)
    invoice_service.cancel(ctx.db, invoice, reason=payload.reason, user_id=ctx.user.id)
    ctx.db.commit()
    return _invoice_out(invoice)


@router.get(
    "/invoices/{invoice_id}/pdf",
    response_class=Response,
    summary="Fatura PDF / Invoice PDF",
)
def invoice_pdf(
    invoice_id: int,
    ctx: Ctx = Depends(require("sales.invoices", "VIEW")),
) -> Response:
    """
    Render the invoice through the shared report exporter.

    Gated on VIEW rather than EXPORT: this is one document the caller may
    already read in full, not a bulk data extract, and the salesperson has to
    be able to hand a printout over at the door.
    """
    invoice = invoice_service.get(ctx.db, invoice_id)
    _assert_in_scope(ctx, invoice.salesperson_id)

    try:
        from app.reports import exporters
    except ImportError as exc:  # reporting stack (reportlab) not installed
        raise AppError(
            "reports.pdf_unavailable",
            detail=str(exc),
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
        ) from exc

    pdf = exporters.to_pdf(
        _invoice_report(ctx, invoice),
        title=f"{invoice.document_type} {invoice.invoice_no}",
        subtitle=(invoice.customer.name if invoice.customer else ""),
        lang=ctx.lang,
    )
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{invoice.invoice_no}.pdf"',
        },
    )


def _invoice_report(ctx: Ctx, invoice: Invoice) -> dict[str, Any]:
    """Shape an invoice as the generic tabular structure the exporters take."""
    columns = [
        {"key": "line_no", "label_tr": "Sıra", "label_en": "No", "type": "integer", "width": 6, "align": "right"},
        {"key": "description", "label_tr": "Ürün", "label_en": "Product", "type": "text", "width": 34},
        {"key": "quantity", "label_tr": "Miktar", "label_en": "Quantity", "type": "quantity", "width": 12, "align": "right"},
        {"key": "uom", "label_tr": "Birim", "label_en": "UoM", "type": "text", "width": 8},
        {"key": "unit_price", "label_tr": "Birim Fiyat", "label_en": "Unit Price", "type": "money", "width": 14, "align": "right"},
        {"key": "discount_amount", "label_tr": "İskonto", "label_en": "Discount", "type": "money", "width": 12, "align": "right"},
        {"key": "net_amount", "label_tr": "Net", "label_en": "Net", "type": "money", "width": 14, "align": "right"},
        {"key": "vat_rate", "label_tr": "KDV %", "label_en": "VAT %", "type": "percent", "width": 8, "align": "right"},
        {"key": "vat_amount", "label_tr": "KDV", "label_en": "VAT", "type": "money", "width": 12, "align": "right"},
        {"key": "total_amount", "label_tr": "Toplam", "label_en": "Total", "type": "money", "width": 14, "align": "right"},
    ]
    rows = [
        {
            "line_no": item.line_no,
            "description": item.description or str(item.product_id),
            "quantity": item.quantity,
            "uom": item.uom,
            "unit_price": display_money(item.unit_price),
            "discount_amount": display_money(item.discount_amount),
            "net_amount": display_money(item.net_amount),
            "vat_rate": item.vat_rate,
            "vat_amount": display_money(item.vat_amount),
            "total_amount": display_money(item.total_amount),
        }
        for item in sorted(invoice.items, key=lambda i: i.line_no)
    ]
    return {
        "columns": columns,
        "rows": rows,
        "totals": {
            "description": t("common.total", ctx.lang),
            "net_amount": display_money(invoice.net_amount),
            "vat_amount": display_money(invoice.vat_amount),
            "total_amount": display_money(invoice.total_amount),
        },
        "meta": {
            "title": f"{invoice.document_type} {invoice.invoice_no}",
            "start": invoice.invoice_date,
            "end": invoice.due_date or invoice.invoice_date,
            "generated_at": invoice.issued_at.isoformat(timespec="seconds")
            if invoice.issued_at
            else None,
        },
    }


# ===========================================================================
# Payments
# ===========================================================================
@router.get(
    "/payments",
    response_model=PagedResponse[PaymentOut],
    summary="Tahsilat listesi / List collections",
)
def list_payments(
    ctx: Ctx = Depends(require("sales.payments", "VIEW")),
    page: Page = Depends(get_page),
    start: date | None = None,
    end: date | None = None,
    customer_id: int | None = None,
    salesperson_id: int | None = None,
    sale_id: int | None = None,
    day_session_id: int | None = None,
    payment_method: PaymentMethod | None = None,
    payment_status: PaymentStatus | None = Query(default=None, alias="status"),
    search: str | None = None,
) -> Any:
    _assert_in_scope(ctx, salesperson_id)
    rows, total = payment_service.list_payments(
        ctx.db,
        customer_id=customer_id,
        salesperson_id=salesperson_id,
        sale_id=sale_id,
        day_session_id=day_session_id,
        payment_method=str(payment_method) if payment_method else None,
        status=str(payment_status) if payment_status else None,
        start=start,
        end=end,
        search=search,
        salesperson_ids=_scope_ids(ctx),
        offset=page.offset,
        limit=page.limit,
    )
    return paginated([_payment_out(r) for r in rows], total, page)


@router.post(
    "/payments",
    response_model=PaymentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Tahsilat kaydet / Record a collection",
)
def create_payment(
    payload: PaymentCreate,
    ctx: Ctx = Depends(require("sales.payments", "CREATE")),
) -> PaymentOut:
    salesperson_id = payload.salesperson_id
    if salesperson_id is None and not ctx.unrestricted:
        salesperson_id = _acting_salesperson_id(ctx, None)
    _assert_in_scope(ctx, salesperson_id)

    try:
        payment = payment_service.record_payment(
            ctx.db,
            customer_id=payload.customer_id,
            amount=payload.amount,
            payment_method=str(payload.payment_method),
            payment_date=payload.payment_date or date.today(),
            salesperson_id=salesperson_id,
            sale_id=payload.sale_id,
            visit_id=payload.visit_id,
            day_session_id=payload.day_session_id,
            invoice_ids=payload.invoice_ids,
            bank_name=payload.bank_name,
            document_number=payload.document_number,
            maturity_date=payload.maturity_date,
            drawer_name=payload.drawer_name,
            reference=payload.reference,
            latitude=payload.latitude,
            longitude=payload.longitude,
            notes=payload.notes,
            user_id=ctx.user.id,
        )
        ctx.db.commit()
    except Exception:
        ctx.db.rollback()
        raise
    return _payment_out(payment)


@router.post(
    "/payments/{payment_id}/clear",
    response_model=PaymentOut,
    summary="Çek/senet tahsil / Clear instrument",
)
def clear_payment(
    payment_id: int,
    ctx: Ctx = Depends(require("sales.payments", "UPDATE")),
) -> PaymentOut:
    payment = payment_service.get(ctx.db, payment_id)
    _assert_in_scope(ctx, payment.salesperson_id)
    try:
        payment_service.clear_payment(ctx.db, payment, user_id=ctx.user.id)
        ctx.db.commit()
    except Exception:
        ctx.db.rollback()
        raise
    return _payment_out(payment)


@router.post(
    "/payments/{payment_id}/bounce",
    response_model=PaymentOut,
    summary="Karşılıksız çek / Bounce instrument",
)
def bounce_payment(
    payment_id: int,
    payload: CancelIn,
    ctx: Ctx = Depends(require("sales.payments", "UPDATE")),
) -> PaymentOut:
    payment = payment_service.get(ctx.db, payment_id)
    _assert_in_scope(ctx, payment.salesperson_id)
    try:
        payment_service.bounce_payment(
            ctx.db, payment, reason=payload.reason, user_id=ctx.user.id
        )
        ctx.db.commit()
    except Exception:
        ctx.db.rollback()
        raise
    return _payment_out(payment)


@router.get(
    "/payments-summary",
    response_model=CollectionsSummaryOut,
    summary="Tahsilat özeti / Collections summary",
)
def collections_summary(
    start: date,
    end: date,
    ctx: Ctx = Depends(require("sales.payments", "VIEW")),
    salesperson_id: int | None = None,
) -> Any:
    _assert_in_scope(ctx, salesperson_id)
    return payment_service.collections_summary(
        ctx.db,
        start=start,
        end=end,
        salesperson_id=salesperson_id,
        salesperson_ids=_scope_ids(ctx),
    )


# ===========================================================================
# Returns
# ===========================================================================
@router.get(
    "/returns",
    response_model=PagedResponse[ReturnListItem],
    summary="İade listesi / List returns",
)
def list_returns(
    ctx: Ctx = Depends(require("sales.returns", "VIEW")),
    page: Page = Depends(get_page),
    start: date | None = None,
    end: date | None = None,
    customer_id: int | None = None,
    salesperson_id: int | None = None,
    sale_id: int | None = None,
    reason: ReturnReason | None = None,
    disposition: ReturnDisposition | None = None,
    is_posted: bool | None = None,
    search: str | None = None,
) -> Any:
    _assert_in_scope(ctx, salesperson_id)
    rows, total = return_service.list_returns(
        ctx.db,
        start=start,
        end=end,
        customer_id=customer_id,
        salesperson_id=salesperson_id,
        sale_id=sale_id,
        reason=str(reason) if reason else None,
        disposition=str(disposition) if disposition else None,
        is_posted=is_posted,
        search=search,
        salesperson_ids=_scope_ids(ctx),
        offset=page.offset,
        limit=page.limit,
    )
    return paginated([_return_list_item(r) for r in rows], total, page)


@router.post(
    "/returns",
    response_model=ReturnOut,
    status_code=status.HTTP_201_CREATED,
    summary="İade oluştur / Create return",
)
def create_return(
    payload: ReturnCreate,
    ctx: Ctx = Depends(require("sales.returns", "CREATE")),
) -> ReturnOut:
    salesperson_id = payload.salesperson_id
    if salesperson_id is None and not ctx.unrestricted:
        salesperson_id = _acting_salesperson_id(ctx, None)
    _assert_in_scope(ctx, salesperson_id)

    warehouse_id = payload.warehouse_id
    if warehouse_id is None:
        from app.services import stock_service

        vehicle_id = payload.vehicle_id
        if vehicle_id is None and salesperson_id is not None:
            vehicle_id = _resolve_vehicle_id(ctx, salesperson_id)
        if vehicle_id is None:
            raise ValidationError("return.warehouse_required")
        warehouse_id = stock_service.vehicle_warehouse_id(ctx.db, vehicle_id)

    try:
        doc = return_service.create_return(
            ctx.db,
            customer_id=payload.customer_id,
            lines=[line.to_line() for line in payload.lines],
            warehouse_id=warehouse_id,
            reason=str(payload.reason),
            disposition=str(payload.disposition),
            sale_id=payload.sale_id,
            salesperson_id=salesperson_id,
            vehicle_id=payload.vehicle_id,
            visit_id=payload.visit_id,
            day_session_id=payload.day_session_id,
            creates_credit_note=payload.creates_credit_note,
            notes=payload.notes,
            user_id=ctx.user.id,
        )
        if payload.post_now:
            return_service.post_return(ctx.db, doc, user_id=ctx.user.id)
        ctx.db.commit()
    except Exception:
        ctx.db.rollback()
        raise
    return _return_out(ctx, doc)


@router.get("/returns/{return_id}", response_model=ReturnOut, summary="İade detayı / Return detail")
def get_return(
    return_id: int,
    ctx: Ctx = Depends(require("sales.returns", "VIEW")),
) -> ReturnOut:
    doc = return_service.get(ctx.db, return_id)
    _assert_in_scope(ctx, doc.salesperson_id)
    return _return_out(ctx, doc)


@router.post(
    "/returns/{return_id}/post",
    response_model=ReturnOut,
    summary="İadeyi işle / Post return to stock",
)
def post_return(
    return_id: int,
    ctx: Ctx = Depends(
        # Field staff take the goods back onto the van themselves, so CREATE is
        # enough here; APPROVE covers supervisors posting someone else's draft.
        require_any(("sales.returns", "APPROVE"), ("sales.returns", "CREATE"))
    ),
) -> ReturnOut:
    doc = return_service.get(ctx.db, return_id)
    _assert_in_scope(ctx, doc.salesperson_id)
    return_service.post_return(ctx.db, doc, user_id=ctx.user.id, commit=True)
    return _return_out(ctx, doc)


# ===========================================================================
# Field-app summaries
# ===========================================================================
@router.get(
    "/daily-summary",
    response_model=DailySummaryOut,
    summary="Günlük özet / Daily summary",
)
def daily_summary(
    ctx: Ctx = Depends(require("sales.sales", "VIEW")),
    on: date | None = None,
    salesperson_id: int | None = None,
) -> Any:
    _assert_in_scope(ctx, salesperson_id)
    return sales_service.daily_summary(
        ctx.db,
        on=on or date.today(),
        salesperson_id=salesperson_id,
        salesperson_ids=_scope_ids(ctx),
    )


@router.get(
    "/open-invoices",
    response_model=list[OpenInvoiceOut],
    summary="Açık faturalar / Open receivables",
)
def open_invoices(
    ctx: Ctx = Depends(require("sales.invoices", "VIEW")),
    customer_id: int | None = None,
    overdue_only: bool = False,
    as_of: date | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[OpenInvoiceOut]:
    """The collection worklist: what is still owed, oldest first."""
    ref = as_of or date.today()
    scope = _scope_ids(ctx)
    if overdue_only:
        rows = invoice_service.overdue(
            ctx.db, as_of=ref, customer_id=customer_id, salesperson_ids=scope
        )
    elif customer_id:
        rows = invoice_service.outstanding(ctx.db, customer_id, as_of=ref)
    else:
        rows, _ = invoice_service.list_invoices(
            ctx.db,
            only_open=True,
            end=ref,
            salesperson_ids=scope,
            offset=0,
            limit=limit,
        )
    return [
        OpenInvoiceOut(
            invoice_id=inv.id,
            invoice_no=inv.invoice_no,
            invoice_date=inv.invoice_date,
            due_date=inv.due_date,
            customer_id=inv.customer_id,
            customer_name=inv.customer.name if inv.customer else None,
            total_amount=inv.total_amount,
            paid_amount=inv.paid_amount,
            open_amount=inv.open_amount,
            days_overdue=invoice_service.days_overdue(inv, as_of=ref),
            status=inv.status,
        )
        for inv in rows[:limit]
    ]


# ===========================================================================
# Sales — parameterised routes last so literals above always win
# ===========================================================================
@router.get("", response_model=PagedResponse[SaleListItem], summary="Satış listesi / List sales")
def list_sales(
    ctx: Ctx = Depends(require("sales.sales", "VIEW")),
    page: Page = Depends(get_page),
    start: date | None = None,
    end: date | None = None,
    customer_id: int | None = None,
    salesperson_id: int | None = None,
    vehicle_id: int | None = None,
    route_id: int | None = None,
    day_session_id: int | None = None,
    include_cancelled: bool = False,
    search: str | None = None,
) -> Any:
    _assert_in_scope(ctx, salesperson_id)
    rows, total = sales_service.list_sales(
        ctx.db,
        start=start,
        end=end,
        customer_id=customer_id,
        salesperson_id=salesperson_id,
        vehicle_id=vehicle_id,
        route_id=route_id,
        day_session_id=day_session_id,
        include_cancelled=include_cancelled,
        search=search,
        salesperson_ids=_scope_ids(ctx),
        offset=page.offset,
        limit=page.limit,
    )
    return paginated([_sale_list_item(r) for r in rows], total, page)


@router.get("/{sale_id}", response_model=SaleOut, summary="Satış detayı / Sale detail")
def get_sale(
    sale_id: int,
    ctx: Ctx = Depends(require("sales.sales", "VIEW")),
) -> SaleOut:
    sale = sales_service.get_sale(ctx.db, sale_id)
    _assert_in_scope(ctx, sale.salesperson_id)
    return _sale_out(sale)


@router.post(
    "/{sale_id}/cancel",
    response_model=SaleOut,
    summary="Satış iptal / Cancel sale",
)
def cancel_sale(
    sale_id: int,
    payload: CancelIn,
    ctx: Ctx = Depends(require("sales.sales", "DELETE")),
) -> SaleOut:
    """Reverse a delivery: stock back in, ledger reversed, invoice cancelled."""
    sale = sales_service.get_sale(ctx.db, sale_id)
    _assert_in_scope(ctx, sale.salesperson_id)
    try:
        sales_service.cancel_sale(ctx.db, sale, reason=payload.reason, user_id=ctx.user.id)
        ctx.db.commit()
    except Exception:
        ctx.db.rollback()
        raise
    return _sale_out(sale)


@router.get(
    "/{sale_id}/invoices",
    response_model=list[InvoiceOut],
    summary="Satışın faturaları / Invoices of a sale",
)
def sale_invoices(
    sale_id: int,
    ctx: Ctx = Depends(require("sales.invoices", "VIEW")),
) -> list[InvoiceOut]:
    sale = sales_service.get_sale(ctx.db, sale_id)
    _assert_in_scope(ctx, sale.salesperson_id)
    rows, _ = invoice_service.list_invoices(ctx.db, sale_id=sale.id, offset=0, limit=100)
    return [_invoice_out(r) for r in rows]
