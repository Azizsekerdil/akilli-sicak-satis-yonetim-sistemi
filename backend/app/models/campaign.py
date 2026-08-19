"""
Campaign engine model.

A campaign is *conditions* + *reward*.  Conditions are stored as rows
(``CampaignCondition``) so the engine can evaluate them generically instead of
hard-coding each promotion type, and every application is recorded
(``CampaignApplication``) so profitability can be measured afterwards.
"""

from __future__ import annotations

from datetime import date, datetime
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
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import CampaignScope, CampaignStatus, CampaignType, DiscountBasis
from app.models.base import (
    AuthorMixin,
    Base,
    Money,
    Quantity,
    SoftDeleteMixin,
    TimestampMixin,
    UTCDateTime,
    fk,
    pk,
)


class Campaign(Base, TimestampMixin, SoftDeleteMixin, AuthorMixin):
    """A promotion with a validity window, targeting rules and a reward."""

    __tablename__ = "campaigns"
    __table_args__ = (
        UniqueConstraint("code", name="uq_campaigns_code"),
        Index("ix_campaigns_status_dates", "status", "start_date", "end_date"),
    )

    id: Mapped[int] = pk()
    code: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    name_en: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)

    campaign_type: Mapped[str] = mapped_column(
        String(24), default=CampaignType.PERCENT_DISCOUNT, nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(16), default=CampaignStatus.DRAFT, nullable=False, index=True
    )

    start_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    end_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    #: Comma-separated Weekday codes; empty means every day.
    active_weekdays: Mapped[str | None] = mapped_column(String(32))

    # --- Targeting ---------------------------------------------------------
    scope: Mapped[str] = mapped_column(String(24), default=CampaignScope.ALL, nullable=False)
    #: Comma-separated ids/codes matching ``scope`` (e.g. "12,44,91").
    scope_values: Mapped[str | None] = mapped_column(Text)

    # --- Reward ------------------------------------------------------------
    discount_basis: Mapped[str] = mapped_column(
        String(16), default=DiscountBasis.PERCENT, nullable=False
    )
    discount_percent: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    discount_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    fixed_price: Mapped[Decimal | None] = mapped_column(Money)
    #: BUY_X_GET_Y — give this product free.
    free_product_id: Mapped[int | None] = fk("products.id", nullable=True, ondelete="SET NULL")
    free_quantity: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("0"), nullable=False)
    free_uom: Mapped[str | None] = mapped_column(String(16))

    # --- Limits ------------------------------------------------------------
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    is_stackable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    max_applications_per_order: Mapped[int | None] = mapped_column(Integer)
    max_applications_per_customer: Mapped[int | None] = mapped_column(Integer)
    max_total_applications: Mapped[int | None] = mapped_column(Integer)
    budget_amount: Mapped[Decimal | None] = mapped_column(Money)

    # --- Live counters -----------------------------------------------------
    application_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_discount_given: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    total_free_goods_cost: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    total_incremental_revenue: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)

    conditions: Mapped[list["CampaignCondition"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan", lazy="selectin"
    )

    def is_live(self, on: date | None = None) -> bool:
        ref = on or date.today()
        return (
            self.status == CampaignStatus.ACTIVE
            and not self.is_deleted
            and self.start_date <= ref <= self.end_date
        )


class CampaignCondition(Base):
    """
    One requirement a basket must satisfy.

    ``subject`` says what is measured (PRODUCT / CATEGORY / BRAND / ORDER),
    ``metric`` how it is measured (QUANTITY / AMOUNT / DISTINCT_PRODUCTS) and
    ``min_value`` the threshold.  All conditions on a campaign must hold.
    """

    __tablename__ = "campaign_conditions"

    id: Mapped[int] = pk()
    campaign_id: Mapped[int] = fk("campaigns.id", ondelete="CASCADE")
    subject: Mapped[str] = mapped_column(String(24), default="ORDER", nullable=False)
    subject_id: Mapped[int | None] = mapped_column(Integer, index=True)
    metric: Mapped[str] = mapped_column(String(24), default="AMOUNT", nullable=False)
    uom: Mapped[str | None] = mapped_column(String(16))
    min_value: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("0"), nullable=False)
    max_value: Mapped[Decimal | None] = mapped_column(Quantity)
    #: For BUY_X_GET_Y this is the "X" that must be bought per reward unit.
    step_value: Mapped[Decimal | None] = mapped_column(Quantity)

    campaign: Mapped["Campaign"] = relationship(back_populates="conditions")


class CampaignApplication(Base):
    """Audit record of a campaign firing on an order/sale — feeds ROI reports."""

    __tablename__ = "campaign_applications"
    __table_args__ = (
        Index("ix_campaign_applications_campaign_date", "campaign_id", "applied_on"),
        Index("ix_campaign_applications_ref", "reference_type", "reference_id"),
    )

    id: Mapped[int] = pk()
    campaign_id: Mapped[int] = fk("campaigns.id", ondelete="CASCADE")
    customer_id: Mapped[int | None] = mapped_column(Integer, index=True)
    salesperson_id: Mapped[int | None] = mapped_column(Integer, index=True)
    reference_type: Mapped[str] = mapped_column(String(24), default="ORDER", nullable=False)
    reference_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)

    applied_on: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    applied_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    times_applied: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    basket_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    discount_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    free_goods_quantity: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("0"), nullable=False)
    free_goods_cost: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    explanation: Mapped[str | None] = mapped_column(String(512))

    campaign: Mapped["Campaign"] = relationship(lazy="joined")


class Discount(Base, TimestampMixin, SoftDeleteMixin, AuthorMixin):
    """
    A standing discount agreement (not time-boxed like a campaign):
    customer-specific, channel-specific or product-specific.
    """

    __tablename__ = "discounts"
    __table_args__ = (
        Index("ix_discounts_scope", "scope", "scope_id"),
        Index("ix_discounts_active_dates", "is_active", "valid_from", "valid_to"),
    )

    id: Mapped[int] = pk()
    code: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    scope: Mapped[str] = mapped_column(String(24), default=CampaignScope.CUSTOMER, nullable=False)
    scope_id: Mapped[int | None] = mapped_column(Integer, index=True)
    product_id: Mapped[int | None] = fk("products.id", nullable=True, ondelete="CASCADE")
    category_id: Mapped[int | None] = fk("product_categories.id", nullable=True, ondelete="CASCADE")

    basis: Mapped[str] = mapped_column(String(16), default=DiscountBasis.PERCENT, nullable=False)
    percent: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    min_quantity: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("0"), nullable=False)
    min_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)

    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
