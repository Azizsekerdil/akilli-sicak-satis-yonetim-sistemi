"""CRM schemas: customers, contacts, notes, current account, ageing and risk."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import (
    CustomerStatus,
    CustomerType,
    PaymentMethod,
    SalesChannel,
    VisitFrequency,
)
from app.schemas.common import ORMModel


# ===========================================================================
# Customer
# ===========================================================================
class CustomerBase(BaseModel):
    """Fields a user may set on a customer card."""

    name: str = Field(min_length=1, max_length=255)
    trade_name: str | None = Field(default=None, max_length=255)

    customer_type: CustomerType = CustomerType.GROCERY
    channel: SalesChannel = SalesChannel.TRADITIONAL
    sub_channel: str | None = Field(default=None, max_length=32)
    status: CustomerStatus = CustomerStatus.ACTIVE

    tax_office: str | None = Field(default=None, max_length=128)
    tax_number: str | None = Field(default=None, max_length=32)
    national_id: str | None = Field(default=None, max_length=24)
    is_e_invoice: bool = False

    address: str | None = None
    city: str | None = Field(default=None, max_length=96)
    district: str | None = Field(default=None, max_length=96)
    neighbourhood: str | None = Field(default=None, max_length=96)
    postal_code: str | None = Field(default=None, max_length=16)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    region_id: int | None = None

    phone: str | None = Field(default=None, max_length=32)
    mobile: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=255)
    contact_person: str | None = Field(default=None, max_length=255)

    default_route_id: int | None = None
    default_salesperson_id: int | None = None
    visit_frequency: VisitFrequency = VisitFrequency.WEEKLY
    #: Comma-separated weekday codes, e.g. ``"MON,THU"``.
    visit_days: str | None = Field(default=None, max_length=32)
    visit_sequence: int = Field(default=0, ge=0)
    service_time_minutes: int = Field(default=10, ge=0, le=600)
    opening_time: str | None = Field(default=None, max_length=8)
    closing_time: str | None = Field(default=None, max_length=8)
    is_priority: bool = False

    price_list_id: int | None = None
    payment_method: PaymentMethod = PaymentMethod.CASH
    payment_term_days: int = Field(default=0, ge=0, le=365)
    risk_limit: Decimal = Decimal("0")
    discount_percent: float = Field(default=0.0, ge=0, le=100)
    currency: str = Field(default="TRY", max_length=8)

    image_path: str | None = Field(default=None, max_length=512)
    notes: str | None = None
    tags: str | None = None


class CustomerCreate(CustomerBase):
    """New customer.  ``code`` is allocated automatically when omitted."""

    code: str | None = Field(default=None, max_length=32)
    #: Requires ``crm.credit_limit:UPDATE`` when non-zero.
    credit_limit: Decimal = Decimal("0")


class CustomerUpdate(BaseModel):
    """Partial update — ``credit_limit`` has its own guarded endpoint."""

    model_config = ConfigDict(extra="ignore")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    trade_name: str | None = Field(default=None, max_length=255)
    customer_type: CustomerType | None = None
    channel: SalesChannel | None = None
    sub_channel: str | None = Field(default=None, max_length=32)
    status: CustomerStatus | None = None

    tax_office: str | None = Field(default=None, max_length=128)
    tax_number: str | None = Field(default=None, max_length=32)
    national_id: str | None = Field(default=None, max_length=24)
    is_e_invoice: bool | None = None

    address: str | None = None
    city: str | None = Field(default=None, max_length=96)
    district: str | None = Field(default=None, max_length=96)
    neighbourhood: str | None = Field(default=None, max_length=96)
    postal_code: str | None = Field(default=None, max_length=16)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    region_id: int | None = None

    phone: str | None = Field(default=None, max_length=32)
    mobile: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=255)
    contact_person: str | None = Field(default=None, max_length=255)

    default_route_id: int | None = None
    default_salesperson_id: int | None = None
    visit_frequency: VisitFrequency | None = None
    visit_days: str | None = Field(default=None, max_length=32)
    visit_sequence: int | None = Field(default=None, ge=0)
    service_time_minutes: int | None = Field(default=None, ge=0, le=600)
    opening_time: str | None = Field(default=None, max_length=8)
    closing_time: str | None = Field(default=None, max_length=8)
    is_priority: bool | None = None

    price_list_id: int | None = None
    payment_method: PaymentMethod | None = None
    payment_term_days: int | None = Field(default=None, ge=0, le=365)
    risk_limit: Decimal | None = None
    discount_percent: float | None = Field(default=None, ge=0, le=100)
    currency: str | None = Field(default=None, max_length=8)

    image_path: str | None = Field(default=None, max_length=512)
    notes: str | None = None
    tags: str | None = None


class CustomerListItem(ORMModel):
    """Compact row for grids and the field application's customer list."""

    id: int
    code: str
    name: str
    trade_name: str | None = None
    customer_type: str
    channel: str
    status: str
    city: str | None = None
    district: str | None = None
    phone: str | None = None
    mobile: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    default_salesperson_id: int | None = None
    default_route_id: int | None = None
    visit_days: str | None = None
    visit_sequence: int = 0
    balance: Decimal = Decimal("0")
    overdue_balance: Decimal = Decimal("0")
    credit_limit: Decimal = Decimal("0")
    total_sales_amount: Decimal = Decimal("0")
    order_count: int = 0
    last_order_date: date | None = None
    last_visit_date: date | None = None
    risk_score: float = 0.0
    is_priority: bool = False


class CustomerOut(CustomerListItem):
    """Full customer card, including the derived commercial state."""

    national_id: str | None = None
    tax_office: str | None = None
    tax_number: str | None = None
    is_e_invoice: bool = False
    address: str | None = None
    neighbourhood: str | None = None
    postal_code: str | None = None
    region_id: int | None = None
    email: str | None = None
    contact_person: str | None = None
    sub_channel: str | None = None

    visit_frequency: str
    service_time_minutes: int = 10
    opening_time: str | None = None
    closing_time: str | None = None

    price_list_id: int | None = None
    payment_method: str
    payment_term_days: int = 0
    risk_limit: Decimal = Decimal("0")
    discount_percent: float = 0.0
    currency: str = "TRY"

    total_paid_amount: Decimal = Decimal("0")
    average_order_value: Decimal = Decimal("0")
    first_order_date: date | None = None
    last_payment_date: date | None = None
    churn_score: float = 0.0
    risk_updated_at: datetime | None = None

    image_path: str | None = None
    notes: str | None = None
    tags: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class CreditLimitUpdate(BaseModel):
    credit_limit: Decimal = Field(ge=0)
    risk_limit: Decimal | None = Field(default=None, ge=0)
    reason: str | None = Field(default=None, max_length=255)


class StatusUpdate(BaseModel):
    status: CustomerStatus
    reason: str | None = Field(default=None, max_length=255)


# ===========================================================================
# Contacts & notes
# ===========================================================================
class ContactIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    title: str | None = Field(default=None, max_length=128)
    phone: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=255)
    is_primary: bool = False
    notes: str | None = None


class ContactOut(ORMModel):
    id: int
    customer_id: int
    name: str
    title: str | None = None
    phone: str | None = None
    email: str | None = None
    is_primary: bool = False
    notes: str | None = None
    created_at: datetime | None = None


class NoteIn(BaseModel):
    body: str = Field(min_length=1)
    category: str | None = Field(default=None, max_length=32)
    visit_id: int | None = None
    is_pinned: bool = False


class NoteOut(ORMModel):
    id: int
    customer_id: int
    visit_id: int | None = None
    category: str | None = None
    body: str
    is_pinned: bool = False
    created_at: datetime | None = None
    created_by_id: int | None = None


# ===========================================================================
# Current account
# ===========================================================================
class LedgerRow(ORMModel):
    id: int
    customer_id: int
    entry_type: str
    entry_date: date
    due_date: date | None = None
    debit: Decimal = Decimal("0")
    credit: Decimal = Decimal("0")
    balance_after: Decimal = Decimal("0")
    open_amount: Decimal = Decimal("0")
    is_settled: bool = False
    currency: str = "TRY"
    reference_type: str | None = None
    reference_id: int | None = None
    reference_no: str | None = None
    salesperson_id: int | None = None
    description: str | None = None
    created_at: datetime | None = None


class StatementRow(BaseModel):
    """One statement line, carrying the running balance."""

    id: int
    entry_date: date
    due_date: date | None = None
    entry_type: str
    reference_type: str | None = None
    reference_id: int | None = None
    reference_no: str | None = None
    description: str | None = None
    debit: Decimal = Decimal("0")
    credit: Decimal = Decimal("0")
    balance: Decimal = Decimal("0")
    open_amount: Decimal = Decimal("0")
    is_settled: bool = False


class StatementTotals(BaseModel):
    debit: Decimal = Decimal("0")
    credit: Decimal = Decimal("0")
    movement: Decimal = Decimal("0")
    row_count: Decimal = Decimal("0")


class StatementOut(BaseModel):
    customer_id: int
    customer_code: str
    customer_name: str
    currency: str = "TRY"
    start: date
    end: date
    opening: Decimal = Decimal("0")
    rows: list[StatementRow] = Field(default_factory=list)
    closing: Decimal = Decimal("0")
    totals: StatementTotals


class AgingOut(BaseModel):
    """Receivable split by days past due."""

    current: Decimal = Decimal("0")
    d1_30: Decimal = Decimal("0")
    d31_60: Decimal = Decimal("0")
    d61_90: Decimal = Decimal("0")
    d90_plus: Decimal = Decimal("0")
    total: Decimal = Decimal("0")
    overdue: Decimal = Decimal("0")


class AgingCustomerRow(AgingOut):
    customer_id: int
    customer_code: str
    customer_name: str


class AgingSummaryOut(BaseModel):
    as_of: date
    totals: AgingOut
    customers: list[AgingCustomerRow] = Field(default_factory=list)


class RiskOut(BaseModel):
    customer_id: int
    customer_code: str
    customer_name: str
    risk_score: float = 0.0
    risk_band: str = "LOW"
    balance: Decimal = Decimal("0")
    overdue_balance: Decimal = Decimal("0")
    credit_limit: Decimal = Decimal("0")
    credit_utilisation_percent: float = 0.0
    days_past_due: int = 0
    bounced_payments_180d: int = 0
    average_payment_interval_days: float | None = None
    last_payment_date: date | None = None
    aging: AgingOut


class TopDebtorOut(BaseModel):
    customer_id: int
    customer_code: str
    customer_name: str
    balance: Decimal = Decimal("0")
    overdue_balance: Decimal = Decimal("0")
    credit_limit: Decimal = Decimal("0")
    risk_score: float = 0.0
    last_payment_date: date | None = None
    salesperson_id: int | None = None
    phone: str | None = None


class OverdueItemOut(BaseModel):
    ledger_id: int
    customer_id: int
    customer_code: str
    customer_name: str
    phone: str | None = None
    salesperson_id: int | None = None
    entry_type: str
    entry_date: date
    due_date: date | None = None
    reference_no: str | None = None
    open_amount: Decimal = Decimal("0")
    days_past_due: int = 0
    bucket: str = "current"


# ===========================================================================
# Geo / analysis
# ===========================================================================
class NearbyQuery(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    radius_km: float = Field(default=5.0, gt=0, le=200)
    limit: int = Field(default=50, ge=1, le=500)


class NearbyItem(BaseModel):
    customer: CustomerListItem
    distance_km: float = 0.0


class ChurnItem(BaseModel):
    customer: CustomerListItem
    days_since_last_order: int = 0
    last_order_date: date | None = None
    total_sales_amount: Decimal = Decimal("0")
    balance: Decimal = Decimal("0")


class DecliningItem(BaseModel):
    customer: CustomerListItem
    current_amount: Decimal = Decimal("0")
    previous_amount: Decimal = Decimal("0")
    drop_percent: float = 0.0
    days: int = 30


class SalesHistoryItem(ORMModel):
    id: int
    sale_no: str
    sale_date: date
    salesperson_id: int | None = None
    total_amount: Decimal = Decimal("0")
    net_amount: Decimal = Decimal("0")
    vat_amount: Decimal = Decimal("0")
    paid_amount: Decimal = Decimal("0")
    due_amount: Decimal = Decimal("0")
    payment_method: str = "CASH"
    line_count: int = 0
    is_cancelled: bool = False


class CustomerProductItem(BaseModel):
    """A SKU the customer buys, with its purchase history."""

    product_id: int
    product_code: str
    product_name: str
    total_quantity: Decimal = Decimal("0")
    total_amount: Decimal = Decimal("0")
    order_count: int = 0
    last_purchase_date: date | None = None


class VisitPlanItem(ORMModel):
    id: int
    code: str
    name: str
    visit_sequence: int = 0
    address: str | None = None
    city: str | None = None
    district: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    phone: str | None = None
    service_time_minutes: int = 10
    balance: Decimal = Decimal("0")
    overdue_balance: Decimal = Decimal("0")
    is_priority: bool = False


class CustomerStatsOut(BaseModel):
    """Recomputed derived state, returned after a refresh."""

    customer_id: int
    balance: Decimal = Decimal("0")
    overdue_balance: Decimal = Decimal("0")
    order_count: int = 0
    total_sales_amount: Decimal = Decimal("0")
    average_order_value: Decimal = Decimal("0")
    first_order_date: date | None = None
    last_order_date: date | None = None
    extra: dict[str, Any] = Field(default_factory=dict)
