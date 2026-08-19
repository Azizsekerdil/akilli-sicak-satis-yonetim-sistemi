"""Campaign, discount and pricing schemas."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.enums import (
    CampaignScope,
    CampaignStatus,
    CampaignType,
    DiscountBasis,
)
from app.schemas.common import ORMModel


def _to_csv(value: Any) -> str | None:
    """Accept either a list or an already-joined string for CSV-backed columns."""
    if value is None:
        return None
    if isinstance(value, (list, tuple, set)):
        joined = ",".join(str(item).strip() for item in value if str(item).strip())
        return joined or None
    text = str(value).strip()
    return text or None


def _split_csv(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


# ===========================================================================
# Conditions
# ===========================================================================
class ConditionIn(BaseModel):
    """One requirement the basket must satisfy for the campaign to pay out."""

    subject: str = Field(default="ORDER", max_length=24)
    subject_id: int | None = None
    metric: str = Field(default="AMOUNT", max_length=24)
    uom: str | None = Field(default=None, max_length=16)
    min_value: Decimal = Field(default=Decimal("0"), ge=0)
    max_value: Decimal | None = Field(default=None, ge=0)
    #: BUY_X_GET_Y: the "X" that must be bought per reward unit.
    step_value: Decimal | None = Field(default=None, ge=0)

    @field_validator("subject", "metric")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.strip().upper()


class ConditionOut(ORMModel):
    id: int
    subject: str
    subject_id: int | None = None
    metric: str
    uom: str | None = None
    min_value: Decimal = Decimal("0")
    max_value: Decimal | None = None
    step_value: Decimal | None = None


# ===========================================================================
# Campaigns
# ===========================================================================
class CampaignBase(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    name_en: str | None = Field(default=None, max_length=255)
    description: str | None = None

    campaign_type: CampaignType = CampaignType.PERCENT_DISCOUNT
    start_date: date
    end_date: date
    active_weekdays: str | None = Field(default=None, max_length=32)

    scope: CampaignScope = CampaignScope.ALL
    scope_values: str | None = None

    discount_basis: DiscountBasis = DiscountBasis.PERCENT
    discount_percent: float = Field(default=0.0, ge=0, le=100)
    discount_amount: Decimal = Field(default=Decimal("0"), ge=0)
    fixed_price: Decimal | None = Field(default=None, ge=0)
    free_product_id: int | None = None
    free_quantity: Decimal = Field(default=Decimal("0"), ge=0)
    free_uom: str | None = Field(default=None, max_length=16)

    priority: int = Field(default=100, ge=0, le=10_000)
    is_stackable: bool = False
    max_applications_per_order: int | None = Field(default=None, ge=1)
    max_applications_per_customer: int | None = Field(default=None, ge=1)
    max_total_applications: int | None = Field(default=None, ge=1)
    budget_amount: Decimal | None = Field(default=None, ge=0)

    @field_validator("scope_values", "active_weekdays", mode="before")
    @classmethod
    def _csv(cls, value: Any) -> str | None:
        return _to_csv(value)

    @model_validator(mode="after")
    def _window(self) -> "CampaignBase":
        if self.end_date < self.start_date:
            raise ValueError("end_date must not precede start_date")
        return self


class CampaignCreate(CampaignBase):
    code: str = Field(min_length=1, max_length=48)
    status: CampaignStatus = CampaignStatus.DRAFT
    conditions: list[ConditionIn] = Field(default_factory=list)

    @field_validator("code")
    @classmethod
    def _code(cls, value: str) -> str:
        return value.strip().upper()


class CampaignUpdate(BaseModel):
    """Every field optional — only what is sent is changed."""

    code: str | None = Field(default=None, max_length=48)
    name: str | None = Field(default=None, max_length=255)
    name_en: str | None = Field(default=None, max_length=255)
    description: str | None = None
    campaign_type: CampaignType | None = None
    start_date: date | None = None
    end_date: date | None = None
    active_weekdays: str | None = Field(default=None, max_length=32)
    scope: CampaignScope | None = None
    scope_values: str | None = None
    discount_basis: DiscountBasis | None = None
    discount_percent: float | None = Field(default=None, ge=0, le=100)
    discount_amount: Decimal | None = Field(default=None, ge=0)
    fixed_price: Decimal | None = Field(default=None, ge=0)
    free_product_id: int | None = None
    free_quantity: Decimal | None = Field(default=None, ge=0)
    free_uom: str | None = Field(default=None, max_length=16)
    priority: int | None = Field(default=None, ge=0, le=10_000)
    is_stackable: bool | None = None
    max_applications_per_order: int | None = Field(default=None, ge=1)
    max_applications_per_customer: int | None = Field(default=None, ge=1)
    max_total_applications: int | None = Field(default=None, ge=1)
    budget_amount: Decimal | None = Field(default=None, ge=0)
    conditions: list[ConditionIn] | None = None

    @field_validator("scope_values", "active_weekdays", mode="before")
    @classmethod
    def _csv(cls, value: Any) -> str | None:
        return _to_csv(value)

    @field_validator("code")
    @classmethod
    def _code(cls, value: str | None) -> str | None:
        return value.strip().upper() if value else value


class CampaignOut(ORMModel):
    id: int
    code: str
    name: str
    name_en: str | None = None
    description: str | None = None
    campaign_type: str
    status: str

    start_date: date
    end_date: date
    active_weekdays: str | None = None
    weekday_list: list[str] = Field(default_factory=list)

    scope: str
    scope_values: str | None = None
    scope_value_list: list[str] = Field(default_factory=list)

    discount_basis: str
    discount_percent: float = 0.0
    discount_amount: Decimal = Decimal("0")
    fixed_price: Decimal | None = None
    free_product_id: int | None = None
    free_quantity: Decimal = Decimal("0")
    free_uom: str | None = None

    priority: int = 100
    is_stackable: bool = False
    max_applications_per_order: int | None = None
    max_applications_per_customer: int | None = None
    max_total_applications: int | None = None
    budget_amount: Decimal | None = None

    application_count: int = 0
    total_discount_given: Decimal = Decimal("0")
    total_free_goods_cost: Decimal = Decimal("0")
    total_incremental_revenue: Decimal = Decimal("0")

    conditions: list[ConditionOut] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @model_validator(mode="after")
    def _explode_csv(self) -> "CampaignOut":
        if not self.scope_value_list:
            self.scope_value_list = _split_csv(self.scope_values)
        if not self.weekday_list:
            self.weekday_list = _split_csv(self.active_weekdays)
        return self


class CampaignStatusIn(BaseModel):
    """Optional body for activate / pause."""

    reason: str | None = Field(default=None, max_length=255)


# ===========================================================================
# Standing discounts
# ===========================================================================
class DiscountCreate(BaseModel):
    code: str = Field(min_length=1, max_length=48)
    name: str = Field(min_length=1, max_length=255)
    scope: CampaignScope = CampaignScope.CUSTOMER
    scope_id: int | None = None
    product_id: int | None = None
    category_id: int | None = None
    basis: DiscountBasis = DiscountBasis.PERCENT
    percent: float = Field(default=0.0, ge=0, le=100)
    amount: Decimal = Field(default=Decimal("0"), ge=0)
    min_quantity: Decimal = Field(default=Decimal("0"), ge=0)
    min_amount: Decimal = Field(default=Decimal("0"), ge=0)
    valid_from: date | None = None
    valid_to: date | None = None
    is_active: bool = True
    priority: int = Field(default=100, ge=0, le=10_000)

    @field_validator("code")
    @classmethod
    def _code(cls, value: str) -> str:
        return value.strip().upper()


class DiscountUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    scope: CampaignScope | None = None
    scope_id: int | None = None
    product_id: int | None = None
    category_id: int | None = None
    basis: DiscountBasis | None = None
    percent: float | None = Field(default=None, ge=0, le=100)
    amount: Decimal | None = Field(default=None, ge=0)
    min_quantity: Decimal | None = Field(default=None, ge=0)
    min_amount: Decimal | None = Field(default=None, ge=0)
    valid_from: date | None = None
    valid_to: date | None = None
    is_active: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=10_000)


class DiscountOut(ORMModel):
    id: int
    code: str
    name: str
    scope: str
    scope_id: int | None = None
    product_id: int | None = None
    category_id: int | None = None
    basis: str
    percent: float = 0.0
    amount: Decimal = Decimal("0")
    min_quantity: Decimal = Decimal("0")
    min_amount: Decimal = Decimal("0")
    valid_from: date | None = None
    valid_to: date | None = None
    is_active: bool = True
    priority: int = 100
    created_at: datetime | None = None


# ===========================================================================
# Pricing / preview
# ===========================================================================
class QuoteLineIn(BaseModel):
    product_id: int
    quantity: Decimal = Field(gt=0)
    uom: str = Field(default="CASE", max_length=16)
    discount_percent: float = Field(default=0.0, ge=0, le=100)
    unit_price_override: Decimal | None = Field(default=None, ge=0)

    @field_validator("uom")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.strip().upper()


class QuoteLineOut(BaseModel):
    line_no: int = 0
    product_id: int
    sku: str = ""
    product_name: str = ""
    quantity: Decimal = Decimal("0")
    uom: str = ""
    uom_factor: Decimal = Decimal("1")
    base_quantity: Decimal = Decimal("0")
    unit_price: Decimal = Decimal("0")
    list_price: Decimal = Decimal("0")
    gross_amount: Decimal = Decimal("0")
    discount_percent: float = 0.0
    discount_amount: Decimal = Decimal("0")
    campaign_discount_amount: Decimal = Decimal("0")
    header_discount_amount: Decimal = Decimal("0")
    net_amount: Decimal = Decimal("0")
    vat_rate: float = 0.0
    vat_amount: Decimal = Decimal("0")
    excise_amount: Decimal = Decimal("0")
    total_amount: Decimal = Decimal("0")
    unit_cost: Decimal = Decimal("0")
    total_cost: Decimal = Decimal("0")
    margin_amount: Decimal = Decimal("0")
    margin_percent: float = 0.0
    is_free_goods: bool = False
    campaign_id: int | None = None
    price_list_id: int | None = None


class AppliedCampaignOut(BaseModel):
    campaign_id: int
    code: str = ""
    name: str = ""
    campaign_type: str = ""
    priority: int = 100
    is_stackable: bool = False
    times_applied: int = 1
    discount_amount: Decimal = Decimal("0")
    free_goods_quantity: Decimal = Decimal("0")
    free_goods_cost: Decimal = Decimal("0")
    line_indexes: list[int] = Field(default_factory=list)
    explanation: str | None = None


class FreeGoodOut(BaseModel):
    campaign_id: int | None = None
    product_id: int
    sku: str = ""
    product_name: str = ""
    quantity: Decimal = Decimal("0")
    uom: str = ""
    base_quantity: Decimal = Decimal("0")
    unit_cost: Decimal = Decimal("0")
    cost: Decimal = Decimal("0")


class PriceQuoteIn(BaseModel):
    """Price a hypothetical basket — nothing is stored."""

    customer_id: int | None = None
    lines: list[QuoteLineIn] = Field(min_length=1)
    on_date: date | None = None
    salesperson_id: int | None = None
    price_list_id: int | None = None
    header_discount_percent: float = Field(default=0.0, ge=0, le=100)
    apply_campaigns: bool = True


class PriceQuoteOut(BaseModel):
    customer_id: int | None = None
    on_date: date | None = None
    currency: str = "TRY"
    lines: list[QuoteLineOut] = Field(default_factory=list)
    gross_amount: Decimal = Decimal("0")
    line_discount_amount: Decimal = Decimal("0")
    campaign_discount_amount: Decimal = Decimal("0")
    header_discount_percent: float = 0.0
    header_discount_amount: Decimal = Decimal("0")
    net_amount: Decimal = Decimal("0")
    vat_amount: Decimal = Decimal("0")
    excise_amount: Decimal = Decimal("0")
    total_amount: Decimal = Decimal("0")
    total_cost: Decimal = Decimal("0")
    margin_amount: Decimal = Decimal("0")
    margin_percent: float = 0.0
    total_volume_l: float = 0.0
    total_weight_kg: float = 0.0
    applied_campaigns: list[AppliedCampaignOut] = Field(default_factory=list)


class CampaignPreviewIn(PriceQuoteIn):
    """Same payload as a quote — the response focuses on the campaigns."""


class CampaignPreviewOut(BaseModel):
    customer_id: int | None = None
    on_date: date | None = None
    total_discount: Decimal = Decimal("0")
    #: Campaign discount keyed by the zero-based index of the submitted line.
    discount_by_line: dict[int, Decimal] = Field(default_factory=dict)
    free_goods: list[FreeGoodOut] = Field(default_factory=list)
    applied: list[AppliedCampaignOut] = Field(default_factory=list)
    quote: PriceQuoteOut


class CampaignProfitability(BaseModel):
    campaign_id: int
    code: str = ""
    name: str = ""
    campaign_type: str = ""
    status: str = ""
    applications: int = 0
    times_applied: int = 0
    discount_given: Decimal = Decimal("0")
    free_goods_cost: Decimal = Decimal("0")
    free_goods_quantity: Decimal = Decimal("0")
    revenue: Decimal = Decimal("0")
    promo_cost: Decimal = Decimal("0")
    incremental_margin: Decimal = Decimal("0")
    roi_percent: float = 0.0
    #: DOCUMENT when margin came from the posted sales/orders, ESTIMATED otherwise.
    margin_source: str = "ESTIMATED"
    budget_amount: Decimal | None = None
    budget_used_percent: float = 0.0
