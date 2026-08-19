"""
Campaign, discount and pricing endpoints.

The two most-used routes here are ``/campaigns/quote`` and
``/campaigns/preview``: the field application calls them on every basket change
so the salesperson sees the promotion *before* committing the order, and both
run the exact same engine the posted sale will use.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.core.deps import Ctx, Page, get_page, paginated, require, require_any
from app.core.enums import AuditAction, CampaignStatus
from app.core.exceptions import NotFoundError, PermissionDeniedError
from app.core.i18n import t
from app.models.campaign import Campaign, CampaignApplication
from app.models.customer import Customer
from app.models.vehicle import Salesperson
from app.schemas.campaign import (
    AppliedCampaignOut,
    CampaignCreate,
    CampaignOut,
    CampaignPreviewIn,
    CampaignPreviewOut,
    CampaignProfitability,
    CampaignStatusIn,
    CampaignUpdate,
    DiscountCreate,
    DiscountOut,
    DiscountUpdate,
    FreeGoodOut,
    PriceQuoteIn,
    PriceQuoteOut,
    QuoteLineOut,
)
from app.schemas.common import Message, PagedResponse
from app.services import audit_service, campaign_service, pricing_service

router = APIRouter(prefix="/campaigns", tags=["marketing"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _customer(ctx: Ctx, customer_id: int | None) -> Customer | None:
    """Load the customer a quote is for, honouring soft deletion."""
    if customer_id is None:
        return None
    row = ctx.db.get(Customer, customer_id)
    if row is None or row.is_deleted:
        raise NotFoundError("customer.not_found", params={"id": customer_id})
    return row


def _salesperson(ctx: Ctx, salesperson_id: int | None) -> Salesperson | None:
    """
    Resolve the salesperson a quote is priced for.

    Restricted users may only quote as themselves (or their team) — campaigns
    can be salesperson-scoped, so letting anyone pass any id would leak another
    territory's promotions.
    """
    if salesperson_id is None:
        own = ctx.db.execute(
            select(Salesperson).where(Salesperson.user_id == ctx.user_id)
        ).scalar_one_or_none()
        return own
    if not ctx.unrestricted and ctx.salesperson_ids and salesperson_id not in ctx.salesperson_ids:
        raise PermissionDeniedError(
            "auth.permission_denied",
            params={"resource": "field.salespersons", "action": "VIEW"},
        )
    row = ctx.db.get(Salesperson, salesperson_id)
    if row is None or row.is_deleted:
        raise NotFoundError("customer.not_found", params={"id": salesperson_id})
    return row


def _quote(ctx: Ctx, payload: PriceQuoteIn) -> tuple[Any, PriceQuoteOut]:
    """Price a payload once and shape it for the API."""
    customer = _customer(ctx, payload.customer_id)
    salesperson = _salesperson(ctx, payload.salesperson_id)
    on = payload.on_date or date.today()

    basket = pricing_service.price_basket(
        ctx.db,
        customer=customer,
        lines=[
            pricing_service.LineInput(
                product_id=line.product_id,
                quantity=line.quantity,
                uom=line.uom,
                discount_percent=line.discount_percent,
                unit_price_override=line.unit_price_override,
            )
            for line in payload.lines
        ],
        on_date=on,
        salesperson=salesperson,
        apply_campaigns=payload.apply_campaigns,
        header_discount_percent=payload.header_discount_percent,
        price_list_id=payload.price_list_id,
    )

    data = pricing_service.quote_dict(basket)
    lines = [QuoteLineOut(**line) for line in data.pop("lines")]
    return basket, PriceQuoteOut(
        customer_id=payload.customer_id,
        on_date=on,
        lines=lines,
        **data,
    )


def _campaign_payload(model: CampaignCreate | CampaignUpdate) -> dict[str, Any]:
    data = model.model_dump(exclude_unset=True, exclude_none=True)
    if "conditions" in data:
        data["conditions"] = [
            condition if isinstance(condition, dict) else condition.model_dump()
            for condition in data["conditions"]
        ]
    return data


def _audit_campaign(ctx: Ctx, action: str, campaign: Campaign, summary: str, **extra: Any) -> None:
    audit_service.record(
        ctx.db,
        action,
        entity_type="Campaign",
        entity_id=campaign.id,
        entity_label=campaign.code,
        summary=summary,
        **extra,
        **ctx.audit_kwargs(),
    )


# ---------------------------------------------------------------------------
# Campaign list & create
# ---------------------------------------------------------------------------
@router.get(
    "",
    response_model=PagedResponse[CampaignOut],
    summary="List campaigns / Kampanyaları listele",
)
def list_campaigns(
    page: Page = Depends(get_page),
    search: str | None = Query(default=None, max_length=128),
    status: str | None = Query(default=None, max_length=16),
    campaign_type: str | None = Query(default=None, max_length=24),
    scope: str | None = Query(default=None, max_length=24),
    active_on: date | None = Query(default=None),
    ctx: Ctx = Depends(require("marketing.campaigns", "VIEW")),
) -> dict[str, Any]:
    rows, total = campaign_service.list_campaigns(
        ctx.db,
        search=search,
        status=status,
        campaign_type=campaign_type,
        scope=scope,
        active_on=active_on,
        offset=page.offset,
        limit=page.limit,
    )
    return paginated([CampaignOut.model_validate(row) for row in rows], total, page)


@router.post(
    "",
    response_model=CampaignOut,
    status_code=201,
    summary="Create campaign / Kampanya oluştur",
)
def create_campaign(
    payload: CampaignCreate,
    ctx: Ctx = Depends(require("marketing.campaigns", "CREATE")),
) -> CampaignOut:
    campaign = campaign_service.create_campaign(
        ctx.db, _campaign_payload(payload), user_id=ctx.user_id
    )
    _audit_campaign(
        ctx,
        AuditAction.CREATE,
        campaign,
        f"campaign.created {campaign.code}",
        new_values={
            "code": campaign.code,
            "type": campaign.campaign_type,
            "discount_percent": campaign.discount_percent,
            "discount_amount": campaign.discount_amount,
            "budget_amount": campaign.budget_amount,
        },
    )
    ctx.db.commit()
    ctx.db.refresh(campaign)
    return CampaignOut.model_validate(campaign)


# ---------------------------------------------------------------------------
# Static routes — declared before /{campaign_id} so they are not swallowed
# ---------------------------------------------------------------------------
@router.get(
    "/applicable",
    response_model=list[CampaignOut],
    summary="Campaigns available to a customer / Müşteriye uygun kampanyalar",
)
def applicable_campaigns(
    customer_id: int | None = Query(default=None),
    on_date: date | None = Query(default=None),
    salesperson_id: int | None = Query(default=None),
    ctx: Ctx = Depends(require("marketing.campaigns", "VIEW")),
) -> list[CampaignOut]:
    """Everything that would fire today, before any basket exists."""
    rows = campaign_service.active_campaigns(
        ctx.db,
        customer=_customer(ctx, customer_id),
        on_date=on_date,
        salesperson=_salesperson(ctx, salesperson_id),
    )
    return [CampaignOut.model_validate(row) for row in rows]


@router.post(
    "/preview",
    response_model=CampaignPreviewOut,
    summary="Preview campaigns on a basket / Sepet kampanya önizleme",
)
def preview_campaigns(
    payload: CampaignPreviewIn,
    ctx: Ctx = Depends(
        require_any(("marketing.campaigns", "VIEW"), ("sales.orders", "VIEW"))
    ),
) -> CampaignPreviewOut:
    """
    What would the campaigns do to this basket?

    Nothing is written — this is the screen the salesperson looks at while the
    customer is still deciding.
    """
    basket, quote = _quote(ctx, payload)

    discount_by_line: dict[int, Decimal] = {}
    free_goods: list[FreeGoodOut] = []
    for line in basket.lines:
        if line.is_free_goods:
            free_goods.append(
                FreeGoodOut(
                    campaign_id=line.campaign_id,
                    product_id=line.product_id,
                    sku=line.product.sku,
                    product_name=line.product.name,
                    quantity=line.quantity,
                    uom=line.uom,
                    base_quantity=line.base_quantity,
                    unit_cost=line.unit_cost,
                    cost=line.total_cost,
                )
            )
        elif line.campaign_discount_amount > 0:
            discount_by_line[line.line_no - 1] = line.campaign_discount_amount

    return CampaignPreviewOut(
        customer_id=payload.customer_id,
        on_date=payload.on_date or date.today(),
        total_discount=basket.campaign_discount_amount,
        discount_by_line=discount_by_line,
        free_goods=free_goods,
        applied=[AppliedCampaignOut(**item) for item in basket.applied_campaigns],
        quote=quote,
    )


@router.post(
    "/quote",
    response_model=PriceQuoteOut,
    summary="Price a basket / Sepeti fiyatlandır",
)
def quote_basket(
    payload: PriceQuoteIn,
    ctx: Ctx = Depends(
        require_any(
            ("marketing.price_lists", "VIEW"),
            ("marketing.campaigns", "VIEW"),
            ("sales.orders", "VIEW"),
        )
    ),
) -> PriceQuoteOut:
    """Full pricing — price list, discounts, campaigns, VAT — without saving."""
    _, quote = _quote(ctx, payload)
    return quote


# ---------------------------------------------------------------------------
# Standing discounts
# ---------------------------------------------------------------------------
@router.get(
    "/discounts",
    response_model=PagedResponse[DiscountOut],
    summary="List discounts / İskontoları listele",
)
def list_discounts(
    page: Page = Depends(get_page),
    search: str | None = Query(default=None, max_length=128),
    scope: str | None = Query(default=None, max_length=24),
    scope_id: int | None = Query(default=None),
    product_id: int | None = Query(default=None),
    is_active: bool | None = Query(default=None),
    ctx: Ctx = Depends(require("marketing.discounts", "VIEW")),
) -> dict[str, Any]:
    rows, total = campaign_service.list_discounts(
        ctx.db,
        search=search,
        scope=scope,
        scope_id=scope_id,
        product_id=product_id,
        is_active=is_active,
        offset=page.offset,
        limit=page.limit,
    )
    return paginated([DiscountOut.model_validate(row) for row in rows], total, page)


@router.post(
    "/discounts",
    response_model=DiscountOut,
    status_code=201,
    summary="Create discount / İskonto oluştur",
)
def create_discount(
    payload: DiscountCreate,
    ctx: Ctx = Depends(require("marketing.discounts", "CREATE")),
) -> DiscountOut:
    row = campaign_service.create_discount(
        ctx.db, payload.model_dump(exclude_unset=True), user_id=ctx.user_id
    )
    audit_service.record(
        ctx.db,
        AuditAction.CREATE,
        entity_type="Discount",
        entity_id=row.id,
        entity_label=row.code,
        summary=f"discount.created {row.code}",
        new_values=payload.model_dump(mode="json"),
        **ctx.audit_kwargs(),
    )
    ctx.db.commit()
    ctx.db.refresh(row)
    return DiscountOut.model_validate(row)


@router.get(
    "/discounts/{discount_id}",
    response_model=DiscountOut,
    summary="Discount detail / İskonto detayı",
)
def get_discount(
    discount_id: int,
    ctx: Ctx = Depends(require("marketing.discounts", "VIEW")),
) -> DiscountOut:
    return DiscountOut.model_validate(campaign_service.get_discount(ctx.db, discount_id))


@router.put(
    "/discounts/{discount_id}",
    response_model=DiscountOut,
    summary="Update discount / İskonto güncelle",
)
def update_discount(
    discount_id: int,
    payload: DiscountUpdate,
    ctx: Ctx = Depends(require("marketing.discounts", "UPDATE")),
) -> DiscountOut:
    before = campaign_service.get_discount(ctx.db, discount_id)
    old = {"percent": before.percent, "amount": before.amount, "is_active": before.is_active}
    row = campaign_service.update_discount(
        ctx.db, discount_id, payload.model_dump(exclude_unset=True), user_id=ctx.user_id
    )
    audit_service.record(
        ctx.db,
        AuditAction.UPDATE,
        entity_type="Discount",
        entity_id=row.id,
        entity_label=row.code,
        summary=f"discount.updated {row.code}",
        old_values=old,
        new_values=payload.model_dump(mode="json", exclude_unset=True),
        **ctx.audit_kwargs(),
    )
    ctx.db.commit()
    ctx.db.refresh(row)
    return DiscountOut.model_validate(row)


@router.delete(
    "/discounts/{discount_id}",
    response_model=Message,
    summary="Delete discount / İskonto sil",
)
def delete_discount(
    discount_id: int,
    ctx: Ctx = Depends(require("marketing.discounts", "DELETE")),
) -> Message:
    row = campaign_service.delete_discount(ctx.db, discount_id, user_id=ctx.user_id)
    audit_service.record(
        ctx.db,
        AuditAction.DELETE,
        entity_type="Discount",
        entity_id=row.id,
        entity_label=row.code,
        summary=f"discount.deleted {row.code}",
        **ctx.audit_kwargs(),
    )
    ctx.db.commit()
    return Message(message=t("common.deleted", ctx.lang), message_key="common.deleted")


# ---------------------------------------------------------------------------
# Single campaign
# ---------------------------------------------------------------------------
@router.get(
    "/{campaign_id}",
    response_model=CampaignOut,
    summary="Campaign detail / Kampanya detayı",
)
def get_campaign(
    campaign_id: int,
    ctx: Ctx = Depends(require("marketing.campaigns", "VIEW")),
) -> CampaignOut:
    return CampaignOut.model_validate(campaign_service.get_campaign(ctx.db, campaign_id))


@router.put(
    "/{campaign_id}",
    response_model=CampaignOut,
    summary="Update campaign / Kampanya güncelle",
)
def update_campaign(
    campaign_id: int,
    payload: CampaignUpdate,
    ctx: Ctx = Depends(require("marketing.campaigns", "UPDATE")),
) -> CampaignOut:
    before = campaign_service.get_campaign(ctx.db, campaign_id)
    old = {
        "discount_percent": before.discount_percent,
        "discount_amount": before.discount_amount,
        "fixed_price": before.fixed_price,
        "budget_amount": before.budget_amount,
        "priority": before.priority,
        "status": before.status,
    }
    campaign = campaign_service.update_campaign(
        ctx.db, campaign_id, _campaign_payload(payload), user_id=ctx.user_id
    )
    _audit_campaign(
        ctx,
        AuditAction.UPDATE,
        campaign,
        f"campaign.updated {campaign.code}",
        old_values=old,
        new_values=payload.model_dump(mode="json", exclude_unset=True),
    )
    ctx.db.commit()
    ctx.db.refresh(campaign)
    return CampaignOut.model_validate(campaign)


@router.delete(
    "/{campaign_id}",
    response_model=Message,
    summary="Delete campaign / Kampanya sil",
)
def delete_campaign(
    campaign_id: int,
    ctx: Ctx = Depends(require("marketing.campaigns", "DELETE")),
) -> Message:
    campaign = campaign_service.delete_campaign(ctx.db, campaign_id, user_id=ctx.user_id)
    _audit_campaign(ctx, AuditAction.DELETE, campaign, f"campaign.deleted {campaign.code}")
    ctx.db.commit()
    return Message(message=t("common.deleted", ctx.lang), message_key="common.deleted")


@router.post(
    "/{campaign_id}/activate",
    response_model=CampaignOut,
    summary="Activate campaign / Kampanyayı başlat",
)
def activate_campaign(
    campaign_id: int,
    payload: CampaignStatusIn | None = None,
    ctx: Ctx = Depends(require("marketing.campaigns", "APPROVE")),
) -> CampaignOut:
    """Switching a campaign on gives money away — hence the APPROVE right."""
    campaign = campaign_service.set_status(
        ctx.db, campaign_id, CampaignStatus.ACTIVE, user_id=ctx.user_id
    )
    _audit_campaign(
        ctx,
        AuditAction.DISCOUNT_APPLIED,
        campaign,
        f"campaign.activated {campaign.code}",
        new_values={
            "status": campaign.status,
            "reason": payload.reason if payload else None,
        },
        amount=campaign.budget_amount,
    )
    ctx.db.commit()
    ctx.db.refresh(campaign)
    return CampaignOut.model_validate(campaign)


@router.post(
    "/{campaign_id}/pause",
    response_model=CampaignOut,
    summary="Pause campaign / Kampanyayı durdur",
)
def pause_campaign(
    campaign_id: int,
    payload: CampaignStatusIn | None = None,
    ctx: Ctx = Depends(require("marketing.campaigns", "UPDATE")),
) -> CampaignOut:
    campaign = campaign_service.set_status(
        ctx.db, campaign_id, CampaignStatus.PAUSED, user_id=ctx.user_id
    )
    _audit_campaign(
        ctx,
        AuditAction.UPDATE,
        campaign,
        f"campaign.paused {campaign.code}",
        new_values={
            "status": campaign.status,
            "reason": payload.reason if payload else None,
        },
    )
    ctx.db.commit()
    ctx.db.refresh(campaign)
    return CampaignOut.model_validate(campaign)


@router.get(
    "/{campaign_id}/profitability",
    response_model=CampaignProfitability,
    summary="Campaign ROI / Kampanya kârlılığı",
)
def campaign_profitability(
    campaign_id: int,
    ctx: Ctx = Depends(require("marketing.campaigns", "VIEW")),
) -> CampaignProfitability:
    return CampaignProfitability(**campaign_service.profitability(ctx.db, campaign_id))


@router.get(
    "/{campaign_id}/applications",
    response_model=PagedResponse[dict[str, Any]],
    summary="Campaign applications / Kampanya kullanımları",
)
def campaign_applications(
    campaign_id: int,
    page: Page = Depends(get_page),
    ctx: Ctx = Depends(require("marketing.campaigns", "VIEW")),
) -> dict[str, Any]:
    """Raw firing history — the audit trail behind the ROI figures."""
    campaign_service.get_campaign(ctx.db, campaign_id)
    stmt = select(CampaignApplication).where(CampaignApplication.campaign_id == campaign_id)
    total = ctx.db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = (
        ctx.db.execute(
            stmt.order_by(CampaignApplication.id.desc()).offset(page.offset).limit(page.limit)
        )
        .scalars()
        .all()
    )
    items = [
        {
            "id": row.id,
            "customer_id": row.customer_id,
            "salesperson_id": row.salesperson_id,
            "reference_type": row.reference_type,
            "reference_id": row.reference_id,
            "applied_on": row.applied_on,
            "times_applied": row.times_applied,
            "basket_amount": row.basket_amount,
            "discount_amount": row.discount_amount,
            "free_goods_quantity": row.free_goods_quantity,
            "free_goods_cost": row.free_goods_cost,
            "explanation": row.explanation,
        }
        for row in rows
    ]
    return paginated(items, int(total), page)


__all__ = ["router"]
