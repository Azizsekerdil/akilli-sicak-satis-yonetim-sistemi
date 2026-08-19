"""Customers (CRM master data), contacts, notes and the current-account ledger."""

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
    event,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import (
    CustomerStatus,
    CustomerType,
    PaymentMethod,
    SalesChannel,
    VisitFrequency,
)
from app.models.base import (
    AuthorMixin,
    Base,
    Money,
    SoftDeleteMixin,
    TimestampMixin,
    UTCDateTime,
    fk,
    pk,
    utcnow,
)


class Customer(Base, TimestampMixin, SoftDeleteMixin, AuthorMixin):
    """Point of sale we deliver to: grocery, market, HoReCa, wholesaler…"""

    __tablename__ = "customers"
    __table_args__ = (
        UniqueConstraint("code", name="uq_customers_code"),
        Index("ix_customers_type_channel", "customer_type", "channel"),
        Index("ix_customers_region_status", "region_id", "status"),
        Index("ix_customers_geo", "latitude", "longitude"),
    )

    id: Mapped[int] = pk()
    code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)          # ünvan
    trade_name: Mapped[str | None] = mapped_column(String(255), index=True)             # ticari isim
    #: ASCII-folded "code name trade_name phone" for search.  SQLite's LOWER()
    #: is ASCII-only, so 'ŞİŞLİ' would never match 'şişli' without this.
    #: Maintained on write via app.core.utils.slugify(..., sep=" ").
    search_key: Mapped[str | None] = mapped_column(String(600), index=True)

    customer_type: Mapped[str] = mapped_column(
        String(24), default=CustomerType.GROCERY, nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(
        String(24), default=SalesChannel.TRADITIONAL, nullable=False, index=True
    )
    sub_channel: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(
        String(16), default=CustomerStatus.ACTIVE, nullable=False, index=True
    )

    # --- Tax / legal -------------------------------------------------------
    tax_office: Mapped[str | None] = mapped_column(String(128))
    tax_number: Mapped[str | None] = mapped_column(String(32), index=True)
    national_id: Mapped[str | None] = mapped_column(String(24))
    is_e_invoice: Mapped[bool] = mapped_column(Boolean, default=False)

    # --- Location ----------------------------------------------------------
    address: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(String(96), index=True)
    district: Mapped[str | None] = mapped_column(String(96), index=True)
    neighbourhood: Mapped[str | None] = mapped_column(String(96))
    postal_code: Mapped[str | None] = mapped_column(String(16))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    region_id: Mapped[int | None] = fk("regions.id", nullable=True, ondelete="SET NULL")

    # --- Contact -----------------------------------------------------------
    phone: Mapped[str | None] = mapped_column(String(32), index=True)
    mobile: Mapped[str | None] = mapped_column(String(32))
    email: Mapped[str | None] = mapped_column(String(255))
    contact_person: Mapped[str | None] = mapped_column(String(255))

    # --- Visit plan --------------------------------------------------------
    default_route_id: Mapped[int | None] = mapped_column(Integer, index=True)
    default_salesperson_id: Mapped[int | None] = fk("salespersons.id", nullable=True, ondelete="SET NULL")
    visit_frequency: Mapped[str] = mapped_column(
        String(16), default=VisitFrequency.WEEKLY, nullable=False
    )
    #: Comma-separated Weekday codes, e.g. "MON,THU"
    visit_days: Mapped[str | None] = mapped_column(String(32))
    visit_sequence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    service_time_minutes: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    opening_time: Mapped[str | None] = mapped_column(String(8))   # "08:30"
    closing_time: Mapped[str | None] = mapped_column(String(8))   # "19:00"
    is_priority: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # --- Commercial terms --------------------------------------------------
    price_list_id: Mapped[int | None] = fk("price_lists.id", nullable=True, ondelete="SET NULL")
    payment_method: Mapped[str] = mapped_column(
        String(24), default=PaymentMethod.CASH, nullable=False
    )
    payment_term_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    credit_limit: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    risk_limit: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    discount_percent: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="TRY", nullable=False)

    # --- Denormalised commercial state (kept current by services) ----------
    balance: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False, index=True)
    overdue_balance: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    total_sales_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    total_paid_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    order_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    average_order_value: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    last_order_date: Mapped[date | None] = mapped_column(Date, index=True)
    last_visit_date: Mapped[date | None] = mapped_column(Date, index=True)
    last_payment_date: Mapped[date | None] = mapped_column(Date)
    first_order_date: Mapped[date | None] = mapped_column(Date)

    # --- Risk scoring (written by the AI collection-risk agent) ------------
    risk_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    churn_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    risk_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    image_path: Mapped[str | None] = mapped_column(String(512))
    notes: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[str | None] = mapped_column(Text)

    salesperson: Mapped["Salesperson | None"] = relationship(lazy="joined")  # noqa: F821

    @property
    def available_credit(self) -> Decimal:
        return self.credit_limit - self.balance

    @property
    def has_coordinates(self) -> bool:
        return self.latitude is not None and self.longitude is not None

    def visit_day_list(self) -> list[str]:
        return [d.strip().upper() for d in (self.visit_days or "").split(",") if d.strip()]


class CustomerContact(Base, TimestampMixin):
    """Additional named contacts at a customer."""

    __tablename__ = "customer_contacts"

    id: Mapped[int] = pk()
    customer_id: Mapped[int] = fk("customers.id", ondelete="CASCADE")
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str | None] = mapped_column(String(128))
    phone: Mapped[str | None] = mapped_column(String(32))
    email: Mapped[str | None] = mapped_column(String(255))
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)


class CustomerNote(Base, TimestampMixin, AuthorMixin):
    """Free-form note or field observation about a customer."""

    __tablename__ = "customer_notes"
    __table_args__ = (Index("ix_customer_notes_cust_time", "customer_id", "created_at"),)

    id: Mapped[int] = pk()
    customer_id: Mapped[int] = fk("customers.id", ondelete="CASCADE")
    visit_id: Mapped[int | None] = mapped_column(Integer, index=True)
    category: Mapped[str | None] = mapped_column(String(32))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False)


class CustomerLedger(Base):
    """
    Current-account (cari hesap) entry.

    Append-only.  ``debit`` increases what the customer owes; ``credit``
    decreases it.  ``balance_after`` is the running balance for statements.
    """

    __tablename__ = "customer_ledger"
    __table_args__ = (
        Index("ix_customer_ledger_cust_date", "customer_id", "entry_date"),
        Index("ix_customer_ledger_ref", "reference_type", "reference_id"),
        Index("ix_customer_ledger_due", "due_date", "is_settled"),
    )

    id: Mapped[int] = pk()
    customer_id: Mapped[int] = fk("customers.id", ondelete="CASCADE")
    entry_type: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    entry_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    due_date: Mapped[date | None] = mapped_column(Date, index=True)

    debit: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    credit: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    balance_after: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="TRY", nullable=False)

    #: Remaining unpaid amount of this entry (for invoices).  Drives ageing.
    open_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    is_settled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)

    reference_type: Mapped[str | None] = mapped_column(String(32), index=True)
    reference_id: Mapped[int | None] = mapped_column(Integer, index=True)
    reference_no: Mapped[str | None] = mapped_column(String(64))
    salesperson_id: Mapped[int | None] = mapped_column(Integer, index=True)
    description: Mapped[str | None] = mapped_column(String(512))

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, nullable=False
    )
    created_by_id: Mapped[int | None] = mapped_column(Integer)

    customer: Mapped["Customer"] = relationship(lazy="joined")

    @property
    def amount(self) -> Decimal:
        return self.debit - self.credit


# ---------------------------------------------------------------------------
# search_key maintenance
# ---------------------------------------------------------------------------
@event.listens_for(Customer, "before_insert")
@event.listens_for(Customer, "before_update")
def _customer_search_key(_mapper, _connection, target: Customer) -> None:
    """
    Keep the ASCII-folded search column in step with the record.

    Done as a mapper event rather than in a service so *every* write path —
    API, importer, demo-data generator, AI agent — gets it for free.
    """
    from app.core.utils import slugify

    parts = [
        target.code or "",
        target.name or "",
        target.trade_name or "",
        target.phone or "",
        target.mobile or "",
        target.tax_number or "",
        target.city or "",
        target.district or "",
    ]
    target.search_key = slugify(" ".join(p for p in parts if p), sep=" ")[:600]
