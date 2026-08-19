"""
Campaign engine — targeting, condition evaluation, reward calculation and ROI.

A campaign is *conditions* + *reward*.  Conditions live in rows so a new
promotion never needs new code: the engine measures a basket against them
generically (``subject`` = what is measured, ``metric`` = how, ``min_value`` =
the threshold) and then pays out whatever the campaign's ``campaign_type``
prescribes.

Ordering rules that the field staff rely on:

* Campaigns fire in ``priority`` order, **lowest number first** — priority 10
  beats priority 100.
* A non-stackable campaign *claims* the lines it touched: no later campaign may
  discount those lines again.  Without this a basket could be discounted twice
  for the same reason and the margin would silently disappear.
* Every reward is capped by the line's remaining net amount and by the
  campaign's remaining budget, so a mis-configured promotion can never produce
  a negative invoice.

The engine is pure: it reads the campaign definitions and returns a
:class:`CampaignOutcome`.  Nothing is written until the document is posted and
the sales module calls :func:`record_application`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.enums import (
    CampaignScope,
    CampaignStatus,
    CampaignType,
    DiscountBasis,
)
from app.core.exceptions import (
    BusinessRuleError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from app.core.logging_config import get_logger
from app.core.utils import D, apply_percent, money, qty, weekday_code
from app.models.campaign import (
    Campaign,
    CampaignApplication,
    CampaignCondition,
    Discount,
)
from app.models.customer import Customer
from app.models.product import Product
from app.models.sales import Order, Sale
from app.models.vehicle import Salesperson

log = get_logger("app.campaigns")

ZERO = Decimal("0")

#: Scopes that select *lines* rather than whole baskets.
_LINE_SCOPES = frozenset(
    {CampaignScope.PRODUCT, CampaignScope.CATEGORY, CampaignScope.BRAND}
)

#: Campaign types whose reward is goods rather than money.
_GOODS_TYPES = frozenset({CampaignType.BUY_X_GET_Y, CampaignType.FREE_GOODS})

#: Statuses from which a campaign may be switched on.
_ACTIVATABLE = frozenset({CampaignStatus.DRAFT, CampaignStatus.PAUSED, CampaignStatus.ACTIVE})


# ===========================================================================
# Result types
# ===========================================================================
@dataclass
class CampaignOutcome:
    """
    What the engine decided for one basket.

    ``discount_by_line`` is keyed by the *index* the caller supplied, so the
    pricing service can post the money back onto its own line objects without
    guessing at identity.
    """

    discount_by_line: dict[int, Decimal] = field(default_factory=dict)
    free_goods: list[dict[str, Any]] = field(default_factory=list)
    applied: list[dict[str, Any]] = field(default_factory=list)
    total_discount: Decimal = ZERO

    def discount_for(self, index: int) -> Decimal:
        return self.discount_by_line.get(index, ZERO)


@dataclass
class _Reward:
    """Internal: one campaign's payout before it is merged into the outcome."""

    discounts: dict[int, Decimal] = field(default_factory=dict)
    free_goods: list[dict[str, Any]] = field(default_factory=list)
    times: int = 1
    touched: set[int] = field(default_factory=set)
    explanation: str = ""

    @property
    def total(self) -> Decimal:
        return money(sum(self.discounts.values(), ZERO))

    @property
    def free_quantity(self) -> Decimal:
        return qty(sum((D(g["base_quantity"]) for g in self.free_goods), ZERO))

    @property
    def free_cost(self) -> Decimal:
        return money(sum((D(g["cost"]) for g in self.free_goods), ZERO))


@dataclass
class StandingDiscount:
    """A resolved :class:`Discount` agreement — not time-boxed like a campaign."""

    discount_id: int
    code: str
    percent: float = 0.0
    amount: Decimal = ZERO


# ===========================================================================
# Small helpers
# ===========================================================================
def _today(on: date | None) -> date:
    return on or date.today()


def _csv_values(raw: str | None) -> list[str]:
    """Split a comma-separated scope/weekday list into trimmed upper tokens."""
    return [part.strip().upper() for part in (raw or "").split(",") if part.strip()]


def _int_values(raw: str | None) -> set[int]:
    out: set[int] = set()
    for token in _csv_values(raw):
        try:
            out.add(int(token))
        except ValueError:
            continue
    return out


def _line_net(line: dict[str, Any]) -> Decimal:
    """Money still on the line after its own manual discount."""
    if "net_after_line_discount" in line:
        return money(line["net_after_line_discount"])
    return money(line.get("gross_amount", ZERO))


def _line_unit_price(line: dict[str, Any]) -> Decimal:
    quantity = D(line.get("quantity", 0))
    if quantity <= 0:
        return ZERO
    return money(D(line.get("gross_amount", 0)) / quantity)


def _uom_factor(product: Product, uom: str | None) -> Decimal:
    """
    Base units per one *uom*.

    Imported lazily from the pricing service because that module imports this
    one — a module-level import either way would be circular.
    """
    from app.services.pricing_service import uom_factor

    return uom_factor(product, uom)


def _spread(total: Decimal, weights: dict[int, Decimal]) -> dict[int, Decimal]:
    """
    Split *total* across lines proportionally to *weights*.

    The rounding remainder is pushed onto the heaviest line so the parts always
    add back up to the whole — an invoice that is one kuruş off is a real
    accounting problem.
    """
    total = money(total)
    basis = sum(weights.values(), ZERO)
    if total <= 0 or basis <= 0:
        return {}
    out: dict[int, Decimal] = {}
    running = ZERO
    ordered = sorted(weights.items(), key=lambda kv: (-kv[1], kv[0]))
    for idx, weight in ordered[1:]:
        share = money(total * weight / basis)
        out[idx] = share
        running += share
    head_idx = ordered[0][0]
    out[head_idx] = money(total - running)
    return out


def _cap_to_net(
    discounts: dict[int, Decimal], remaining_net: dict[int, Decimal]
) -> dict[int, Decimal]:
    """No line may be discounted below zero, whatever the campaign says."""
    capped: dict[int, Decimal] = {}
    for idx, amount in discounts.items():
        allowed = money(remaining_net.get(idx, ZERO))
        value = money(min(money(amount), allowed))
        if value > 0:
            capped[idx] = value
    return capped


# ===========================================================================
# Targeting
# ===========================================================================
def _weekday_ok(campaign: Campaign, on: date) -> bool:
    days = _csv_values(campaign.active_weekdays)
    return not days or weekday_code(on) in days


def _customer_scope_ok(
    campaign: Campaign,
    customer: Customer | None,
    salesperson: Salesperson | None,
) -> bool:
    """
    Header-level targeting.

    Product/category/brand scopes are decided per line, so they always pass
    here and are filtered in :func:`_lines_in_scope`.
    """
    scope = campaign.scope or CampaignScope.ALL
    if scope == CampaignScope.ALL or scope in _LINE_SCOPES:
        return True
    if customer is None:
        return False

    if scope == CampaignScope.CUSTOMER:
        return customer.id in _int_values(campaign.scope_values)
    if scope == CampaignScope.CUSTOMER_TYPE:
        return (customer.customer_type or "").upper() in _csv_values(campaign.scope_values)
    if scope == CampaignScope.CHANNEL:
        return (customer.channel or "").upper() in _csv_values(campaign.scope_values)
    if scope == CampaignScope.REGION:
        return customer.region_id is not None and customer.region_id in _int_values(
            campaign.scope_values
        )
    if scope == CampaignScope.ROUTE:
        return customer.default_route_id is not None and customer.default_route_id in _int_values(
            campaign.scope_values
        )
    if scope == CampaignScope.SALESPERSON:
        ids = _int_values(campaign.scope_values)
        candidates = {
            sid
            for sid in (
                salesperson.id if salesperson is not None else None,
                customer.default_salesperson_id,
            )
            if sid is not None
        }
        return bool(candidates & ids)
    return False


def _lines_in_scope(campaign: Campaign, lines: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Lines a product/category/brand-scoped campaign is allowed to touch."""
    scope = campaign.scope or CampaignScope.ALL
    if scope not in _LINE_SCOPES:
        return list(lines)

    ids = _int_values(campaign.scope_values)
    if not ids:
        return []
    out: list[dict[str, Any]] = []
    for line in lines:
        product: Product = line["product"]
        if scope == CampaignScope.PRODUCT and product.id in ids:
            out.append(line)
        elif scope == CampaignScope.CATEGORY and (product.category_id in ids):
            out.append(line)
        elif scope == CampaignScope.BRAND and (product.brand_id in ids):
            out.append(line)
    return out


def _customer_application_count(db: Session, campaign_id: int, customer_id: int | None) -> int:
    """How many times this customer already consumed the campaign."""
    if customer_id is None:
        return 0
    total = db.execute(
        select(func.coalesce(func.sum(CampaignApplication.times_applied), 0)).where(
            CampaignApplication.campaign_id == campaign_id,
            CampaignApplication.customer_id == customer_id,
        )
    ).scalar_one()
    return int(total or 0)


def _limits_ok(db: Session, campaign: Campaign, customer: Customer | None) -> bool:
    """Total-application, per-customer and budget ceilings."""
    if campaign.max_total_applications is not None and (
        campaign.application_count >= campaign.max_total_applications
    ):
        return False
    if campaign.budget_amount is not None and (
        D(campaign.total_discount_given) >= D(campaign.budget_amount)
    ):
        return False
    if campaign.max_applications_per_customer is not None and customer is not None:
        used = _customer_application_count(db, campaign.id, customer.id)
        if used >= campaign.max_applications_per_customer:
            return False
    return True


def active_campaigns(
    db: Session,
    *,
    customer: Customer | None = None,
    on_date: date | None = None,
    salesperson: Salesperson | None = None,
) -> list[Campaign]:
    """
    Campaigns that could fire for this customer today, cheapest filters first.

    Date window and status are pushed into SQL; weekday, scope and consumption
    limits are cheap in-memory checks on the (small) surviving set.
    """
    on = _today(on_date)
    rows = (
        db.execute(
            select(Campaign)
            .where(
                Campaign.is_deleted.is_(False),
                Campaign.status == CampaignStatus.ACTIVE,
                Campaign.start_date <= on,
                Campaign.end_date >= on,
            )
            .order_by(Campaign.priority.asc(), Campaign.id.asc())
        )
        .scalars()
        .all()
    )
    return [
        c
        for c in rows
        if _weekday_ok(c, on)
        and _customer_scope_ok(c, customer, salesperson)
        and _limits_ok(db, c, customer)
    ]


# ===========================================================================
# Condition measurement
# ===========================================================================
def _condition_lines(
    condition: CampaignCondition, lines: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    subject = (condition.subject or "ORDER").upper()
    if subject == "ORDER" or condition.subject_id is None:
        return list(lines)
    out: list[dict[str, Any]] = []
    for line in lines:
        product: Product = line["product"]
        if subject == "PRODUCT" and product.id == condition.subject_id:
            out.append(line)
        elif subject == "CATEGORY" and product.category_id == condition.subject_id:
            out.append(line)
        elif subject == "BRAND" and product.brand_id == condition.subject_id:
            out.append(line)
    return out


def _measure(
    condition: CampaignCondition, lines: Sequence[dict[str, Any]]
) -> tuple[Decimal, list[dict[str, Any]]]:
    """Return (measured value, lines that fed the measurement)."""
    matched = _condition_lines(condition, lines)
    metric = (condition.metric or "AMOUNT").upper()

    if metric == "AMOUNT":
        return money(sum((_line_net(line) for line in matched), ZERO)), matched

    if metric == "DISTINCT_PRODUCTS":
        distinct = {line["product_id"] for line in matched if D(line.get("quantity", 0)) > 0}
        return D(len(distinct)), matched

    # QUANTITY (default): measured in base units unless the condition names a
    # UoM — "buy 5 cases" must not be satisfied by 5 pieces.  The conversion is
    # per line because a category-wide condition can span products with
    # different case sizes.
    total = ZERO
    for line in matched:
        base = D(line.get("base_quantity", 0))
        if condition.uom:
            factor = _uom_factor(line["product"], condition.uom)
            total += base / factor if factor > 0 else ZERO
        else:
            total += base
    return qty(total), matched


def _evaluate_conditions(
    campaign: Campaign, lines: Sequence[dict[str, Any]]
) -> tuple[bool, list[dict[str, Any]], int | None]:
    """
    Check every condition (they are ANDed) and derive the repeat count.

    The repeat count comes from ``step_value`` — "per each X bought" — and the
    tightest condition wins, so a campaign needing 10 cases *and* 500 TL pays
    out only as often as both are satisfied.
    """
    conditions = list(campaign.conditions or [])
    if not conditions:
        return True, list(lines), None

    matched_union: dict[int, dict[str, Any]] = {}
    times: int | None = None
    for condition in conditions:
        value, matched = _measure(condition, lines)
        if value < D(condition.min_value):
            return False, [], None
        if condition.max_value is not None and value > D(condition.max_value):
            return False, [], None
        for line in matched:
            matched_union[line["index"]] = line
        step = D(condition.step_value or 0)
        if step > 0:
            step_times = int(value // step)
            times = step_times if times is None else min(times, step_times)

    ordered = [matched_union[key] for key in sorted(matched_union)]
    return True, ordered, times


# ===========================================================================
# Rewards
# ===========================================================================
def _money_reward(
    campaign: Campaign,
    targets: Sequence[dict[str, Any]],
    remaining_net: dict[int, Decimal],
    *,
    times: int = 1,
) -> dict[int, Decimal]:
    """PERCENT basis discounts each line; AMOUNT basis is spread over them."""
    weights = {
        line["index"]: money(min(_line_net(line), remaining_net.get(line["index"], ZERO)))
        for line in targets
    }
    weights = {idx: value for idx, value in weights.items() if value > 0}
    if not weights:
        return {}

    if (campaign.discount_basis or DiscountBasis.PERCENT) == DiscountBasis.PERCENT:
        percent = float(campaign.discount_percent or 0.0)
        if percent <= 0:
            return {}
        return {idx: apply_percent(value, percent) for idx, value in weights.items()}

    amount = money(D(campaign.discount_amount) * max(1, times))
    if amount <= 0:
        return {}
    return _spread(amount, weights)


def _fixed_price_reward(
    campaign: Campaign,
    targets: Sequence[dict[str, Any]],
    remaining_net: dict[int, Decimal],
) -> dict[int, Decimal]:
    """Override the unit price: the discount is whatever the line is above it."""
    fixed = D(campaign.fixed_price)
    if campaign.fixed_price is None or fixed < 0:
        return {}
    out: dict[int, Decimal] = {}
    for line in targets:
        unit_price = _line_unit_price(line)
        if unit_price <= fixed:
            continue
        delta = money((unit_price - fixed) * D(line.get("quantity", 0)))
        allowed = money(remaining_net.get(line["index"], ZERO))
        value = money(min(delta, allowed))
        if value > 0:
            out[line["index"]] = value
    return out


def _free_goods_reward(
    db: Session,
    campaign: Campaign,
    targets: Sequence[dict[str, Any]],
    *,
    times: int,
) -> list[dict[str, Any]]:
    """
    Build the bedelsiz (zero-priced) lines a campaign gives away.

    When no ``free_product_id`` is configured the gift is the product that
    triggered the campaign — that is what "10 al 1 bedava" means in the field.
    """
    each = D(campaign.free_quantity)
    if times <= 0 or each <= 0:
        return []

    product: Product | None = None
    if campaign.free_product_id:
        product = db.get(Product, campaign.free_product_id)
    elif targets:
        product = targets[0]["product"]
    if product is None:
        return []

    uom = campaign.free_uom or product.base_uom
    factor = _uom_factor(product, uom)
    quantity = qty(each * times)
    base_quantity = qty(quantity * factor)
    unit_cost = money(product.cost_price)
    return [
        {
            "campaign_id": campaign.id,
            "campaign_code": campaign.code,
            "product_id": product.id,
            "product": product,
            "quantity": quantity,
            "uom": uom,
            "uom_factor": factor,
            "base_quantity": base_quantity,
            "unit_cost": unit_cost,
            "cost": money(unit_cost * base_quantity),
        }
    ]


def _explain(campaign: Campaign, reward: _Reward) -> str:
    """
    Compact machine-readable trace of why the campaign paid out.

    Deliberately *not* a sentence: user-facing text is rendered from i18n keys
    in the API layer, this string is for auditors and support.
    """
    parts = [f"{campaign.code}:{campaign.campaign_type}"]
    if reward.times > 1:
        parts.append(f"x{reward.times}")
    if reward.discounts:
        parts.append(f"disc={reward.total}")
    if reward.free_goods:
        gifts = ",".join(f"{g['product_id']}:{g['quantity']}{g['uom']}" for g in reward.free_goods)
        parts.append(f"free={gifts}")
    if reward.touched:
        parts.append("lines=" + ",".join(str(i) for i in sorted(reward.touched)))
    return " ".join(parts)[:512]


def _build_reward(
    db: Session,
    campaign: Campaign,
    lines: Sequence[dict[str, Any]],
    remaining_net: dict[int, Decimal],
) -> _Reward | None:
    """Run one campaign against the basket and return its payout, or None."""
    scoped = _lines_in_scope(campaign, lines)
    if not scoped:
        return None

    met, matched, step_times = _evaluate_conditions(campaign, scoped)
    if not met:
        return None
    targets = matched or list(scoped)
    if not targets:
        return None

    kind = campaign.campaign_type
    max_per_order = campaign.max_applications_per_order
    reward = _Reward()

    if kind in _GOODS_TYPES:
        # BUY_X_GET_Y repeats with the basket; FREE_GOODS pays out once unless a
        # step is configured.
        times = step_times if step_times is not None else (0 if kind == CampaignType.BUY_X_GET_Y else 1)
        if times <= 0:
            return None
        if max_per_order is not None:
            times = min(times, max_per_order)
        if times <= 0:
            return None
        reward.times = times
        reward.free_goods = _free_goods_reward(db, campaign, targets, times=times)
        if not reward.free_goods:
            return None
        reward.touched = {line["index"] for line in targets}
    elif kind == CampaignType.FIXED_PRICE:
        discounts = _fixed_price_reward(campaign, targets, remaining_net)
        if not discounts:
            return None
        reward.discounts = discounts
        reward.touched = set(discounts)
    else:
        times = step_times if step_times is not None else 1
        if max_per_order is not None:
            times = min(times, max_per_order)
        times = max(times, 1)
        discounts = _cap_to_net(
            _money_reward(campaign, targets, remaining_net, times=times), remaining_net
        )
        if not discounts:
            return None
        reward.times = times
        reward.discounts = discounts
        reward.touched = set(discounts)

    # Budget ceiling: never give away more than the campaign still has.
    if campaign.budget_amount is not None and reward.discounts:
        remaining_budget = money(D(campaign.budget_amount) - D(campaign.total_discount_given))
        if remaining_budget <= 0:
            return None
        if reward.total > remaining_budget:
            reward.discounts = _spread(remaining_budget, reward.discounts)
            reward.touched = set(reward.discounts)
            if not reward.discounts:
                return None

    reward.explanation = _explain(campaign, reward)
    return reward


def evaluate(
    db: Session,
    *,
    customer: Customer | None,
    lines: list[dict[str, Any]],
    on_date: date | None = None,
    salesperson: Salesperson | None = None,
) -> CampaignOutcome:
    """
    Decide every campaign for one basket.

    Each *line* dict must carry: ``index``, ``product_id``, ``product``,
    ``base_quantity``, ``quantity``, ``uom``, ``gross_amount`` and
    ``net_after_line_discount``.
    """
    outcome = CampaignOutcome()
    if not lines:
        return outcome

    campaigns = active_campaigns(
        db, customer=customer, on_date=on_date, salesperson=salesperson
    )
    if not campaigns:
        return outcome

    remaining_net: dict[int, Decimal] = {
        line["index"]: _line_net(line) for line in lines
    }
    locked: set[int] = set()

    for campaign in campaigns:
        available = [line for line in lines if line["index"] not in locked]
        if not available:
            break

        reward = _build_reward(db, campaign, available, remaining_net)
        if reward is None:
            continue

        for idx, amount in reward.discounts.items():
            outcome.discount_by_line[idx] = money(
                outcome.discount_by_line.get(idx, ZERO) + amount
            )
            remaining_net[idx] = money(max(ZERO, remaining_net.get(idx, ZERO) - amount))
        outcome.free_goods.extend(reward.free_goods)
        outcome.total_discount = money(outcome.total_discount + reward.total)

        outcome.applied.append(
            {
                "campaign_id": campaign.id,
                "code": campaign.code,
                "name": campaign.name,
                "campaign_type": campaign.campaign_type,
                "priority": campaign.priority,
                "is_stackable": campaign.is_stackable,
                "times_applied": reward.times,
                "discount_amount": reward.total,
                "free_goods_quantity": reward.free_quantity,
                "free_goods_cost": reward.free_cost,
                "line_indexes": sorted(reward.touched),
                "explanation": reward.explanation,
            }
        )

        if not campaign.is_stackable:
            locked |= reward.touched

    return outcome


# ===========================================================================
# Application recording & profitability
# ===========================================================================
def record_application(
    db: Session,
    campaign: Campaign,
    *,
    reference_type: str,
    reference_id: int,
    customer_id: int | None = None,
    salesperson_id: int | None = None,
    basket_amount: Decimal = ZERO,
    discount_amount: Decimal = ZERO,
    free_goods_quantity: Decimal = ZERO,
    free_goods_cost: Decimal = ZERO,
    times_applied: int = 1,
    explanation: str | None = None,
    on: date | None = None,
) -> CampaignApplication:
    """
    Persist one campaign firing and bump the live counters.

    Counters are denormalised onto :class:`Campaign` because the limit checks
    run on every basket priced in the field — an aggregate query per campaign
    per keystroke would not survive a busy morning.

    Does not commit: the sale that triggered it owns the transaction.
    """
    row = CampaignApplication(
        campaign_id=campaign.id,
        customer_id=customer_id,
        salesperson_id=salesperson_id,
        reference_type=reference_type,
        reference_id=reference_id,
        applied_on=_today(on),
        applied_at=datetime.now(),
        times_applied=max(1, int(times_applied)),
        basket_amount=money(basket_amount),
        discount_amount=money(discount_amount),
        free_goods_quantity=qty(free_goods_quantity),
        free_goods_cost=money(free_goods_cost),
        explanation=(explanation or "")[:512] or None,
    )
    db.add(row)

    campaign.application_count = int(campaign.application_count or 0) + row.times_applied
    campaign.total_discount_given = money(
        D(campaign.total_discount_given) + row.discount_amount
    )
    campaign.total_free_goods_cost = money(
        D(campaign.total_free_goods_cost) + row.free_goods_cost
    )
    campaign.total_incremental_revenue = money(
        D(campaign.total_incremental_revenue) + row.basket_amount
    )
    db.flush()
    return row


def _document_margin(db: Session, rows: Sequence[CampaignApplication]) -> tuple[Decimal, bool]:
    """
    Real margin of the documents the campaign fired on.

    Falls back to "not measurable" when the applications point at documents we
    cannot resolve (e.g. previews recorded against a synthetic reference).
    """
    sale_ids = [r.reference_id for r in rows if (r.reference_type or "").upper() == "SALE"]
    order_ids = [r.reference_id for r in rows if (r.reference_type or "").upper() == "ORDER"]
    total = ZERO
    matched = 0

    # The row count matters, not the sum: SUM over an empty set is 0, which is
    # indistinguishable from a genuine zero-margin campaign.
    if sale_ids:
        count, value = db.execute(
            select(func.count(Sale.id), func.coalesce(func.sum(Sale.margin_amount), 0)).where(
                Sale.id.in_(sale_ids), Sale.is_cancelled.is_(False)
            )
        ).one()
        matched += int(count or 0)
        total += D(value)
    if order_ids:
        count, value = db.execute(
            select(func.count(Order.id), func.coalesce(func.sum(Order.margin_amount), 0)).where(
                Order.id.in_(order_ids), Order.is_deleted.is_(False)
            )
        ).one()
        matched += int(count or 0)
        total += D(value)
    return money(total), matched > 0


def profitability(db: Session, campaign_id: int) -> dict[str, Any]:
    """
    Did the promotion pay for itself?

    ``roi_percent`` compares the margin earned on promoted baskets against what
    the promotion cost (discount given plus the cost of free goods).  100% means
    the campaign returned exactly what it spent.
    """
    campaign = get_campaign(db, campaign_id)
    rows = (
        db.execute(
            select(CampaignApplication).where(CampaignApplication.campaign_id == campaign_id)
        )
        .scalars()
        .all()
    )

    discount_given = money(sum((D(r.discount_amount) for r in rows), ZERO))
    free_goods_cost = money(sum((D(r.free_goods_cost) for r in rows), ZERO))
    revenue = money(sum((D(r.basket_amount) for r in rows), ZERO))
    free_goods_quantity = qty(sum((D(r.free_goods_quantity) for r in rows), ZERO))
    times_applied = sum(int(r.times_applied or 0) for r in rows)
    promo_cost = money(discount_given + free_goods_cost)

    margin, measured = _document_margin(db, rows)
    if measured:
        incremental_margin = margin
        margin_source = "DOCUMENT"
    else:
        # No resolvable documents — approximate with what the applications know.
        incremental_margin = money(revenue - promo_cost)
        margin_source = "ESTIMATED"

    roi_percent = (
        round(float(incremental_margin / promo_cost * 100), 2) if promo_cost > 0 else 0.0
    )
    budget = D(campaign.budget_amount) if campaign.budget_amount is not None else None
    return {
        "campaign_id": campaign.id,
        "code": campaign.code,
        "name": campaign.name,
        "campaign_type": campaign.campaign_type,
        "status": campaign.status,
        "applications": len(rows),
        "times_applied": times_applied,
        "discount_given": discount_given,
        "free_goods_cost": free_goods_cost,
        "free_goods_quantity": free_goods_quantity,
        "revenue": revenue,
        "promo_cost": promo_cost,
        "incremental_margin": incremental_margin,
        "roi_percent": roi_percent,
        "margin_source": margin_source,
        "budget_amount": budget,
        "budget_used_percent": (
            round(float(discount_given / budget * 100), 2) if budget and budget > 0 else 0.0
        ),
    }


# ===========================================================================
# Standing discount agreements
# ===========================================================================
def resolve_standing_discount(
    db: Session,
    *,
    customer: Customer | None,
    product: Product,
    quantity: Decimal = ZERO,
    amount: Decimal = ZERO,
    on_date: date | None = None,
    salesperson: Salesperson | None = None,
) -> StandingDiscount | None:
    """
    Best standing :class:`Discount` for one line.

    Unlike campaigns these are open-ended commercial agreements, so exactly one
    wins: the lowest ``priority`` number, then the most specific match.
    Priority leads because it is the knob the back office actually turns — a
    specificity-first rule would make a customer-wide agreement silently
    outrank the SKU-level exception someone just created to override it.

    ``CUSTOMER_TYPE`` and ``CHANNEL`` scoped rows are skipped — ``scope_id`` is
    an integer column and cannot hold a channel code, so such rows can only
    have been created in error.
    """
    on = _today(on_date)
    rows = (
        db.execute(
            select(Discount).where(
                Discount.is_deleted.is_(False),
                Discount.is_active.is_(True),
            )
        )
        .scalars()
        .all()
    )

    best: tuple[int, int, Discount] | None = None
    for row in rows:
        if row.valid_from and row.valid_from > on:
            continue
        if row.valid_to and row.valid_to < on:
            continue
        if D(row.min_quantity) > D(quantity):
            continue
        if D(row.min_amount) > D(amount):
            continue

        specificity = 0
        if row.product_id is not None:
            if row.product_id != product.id:
                continue
            specificity += 4
        elif row.category_id is not None:
            if row.category_id != product.category_id:
                continue
            specificity += 2

        scope = row.scope or CampaignScope.CUSTOMER
        if scope == CampaignScope.ALL:
            pass
        elif customer is None:
            continue
        elif scope == CampaignScope.CUSTOMER:
            if row.scope_id != customer.id:
                continue
            specificity += 8
        elif scope == CampaignScope.REGION:
            if row.scope_id is None or row.scope_id != customer.region_id:
                continue
            specificity += 3
        elif scope == CampaignScope.ROUTE:
            if row.scope_id is None or row.scope_id != customer.default_route_id:
                continue
            specificity += 3
        elif scope == CampaignScope.SALESPERSON:
            sp_id = salesperson.id if salesperson is not None else customer.default_salesperson_id
            if row.scope_id is None or row.scope_id != sp_id:
                continue
            specificity += 3
        else:
            continue

        key = (int(row.priority or 100), -specificity, row)
        if best is None or (key[0], key[1]) < (best[0], best[1]):
            best = key  # type: ignore[assignment]

    if best is None:
        return None
    row = best[2]
    if (row.basis or DiscountBasis.PERCENT) == DiscountBasis.PERCENT:
        return StandingDiscount(
            discount_id=row.id, code=row.code, percent=float(row.percent or 0.0)
        )
    return StandingDiscount(discount_id=row.id, code=row.code, amount=money(row.amount))


# ===========================================================================
# Campaign CRUD
# ===========================================================================
def get_campaign(db: Session, campaign_id: int, *, include_deleted: bool = False) -> Campaign:
    campaign = db.get(Campaign, campaign_id)
    if campaign is None or (campaign.is_deleted and not include_deleted):
        raise NotFoundError("campaign.not_found", params={"id": campaign_id})
    return campaign


def list_campaigns(
    db: Session,
    *,
    search: str | None = None,
    status: str | None = None,
    campaign_type: str | None = None,
    scope: str | None = None,
    active_on: date | None = None,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[Campaign], int]:
    """Filtered campaign list plus the unpaginated total."""
    stmt = select(Campaign).where(Campaign.is_deleted.is_(False))
    if status:
        stmt = stmt.where(Campaign.status == status)
    if campaign_type:
        stmt = stmt.where(Campaign.campaign_type == campaign_type)
    if scope:
        stmt = stmt.where(Campaign.scope == scope)
    if active_on:
        stmt = stmt.where(Campaign.start_date <= active_on, Campaign.end_date >= active_on)
    if search:
        term = f"%{search.lower()}%"
        stmt = stmt.where(
            func.lower(Campaign.code).like(term) | func.lower(Campaign.name).like(term)
        )

    total = db.execute(
        select(func.count()).select_from(stmt.subquery())
    ).scalar_one()
    rows = (
        db.execute(
            stmt.order_by(Campaign.priority.asc(), Campaign.start_date.desc(), Campaign.id.desc())
            .offset(offset)
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return list(rows), int(total)


_CAMPAIGN_FIELDS = (
    "name",
    "name_en",
    "description",
    "campaign_type",
    "start_date",
    "end_date",
    "active_weekdays",
    "scope",
    "scope_values",
    "discount_basis",
    "discount_percent",
    "discount_amount",
    "fixed_price",
    "free_product_id",
    "free_quantity",
    "free_uom",
    "priority",
    "is_stackable",
    "max_applications_per_order",
    "max_applications_per_customer",
    "max_total_applications",
    "budget_amount",
)


def _validate_window(start: date | None, end: date | None) -> None:
    if start and end and end < start:
        raise ValidationError("campaign.invalid_date_range")


def _validate_reward(campaign: Campaign) -> None:
    """
    A campaign that cannot pay anything out is a configuration bug — catching it
    at activation is far cheaper than a salesperson discovering it at a counter.
    """
    kind = campaign.campaign_type
    if kind in _GOODS_TYPES:
        if D(campaign.free_quantity) <= 0:
            raise ValidationError("campaign.reward_not_configured")
        if kind == CampaignType.BUY_X_GET_Y and not any(
            D(c.step_value or 0) > 0 for c in (campaign.conditions or [])
        ):
            raise ValidationError("campaign.reward_not_configured")
        return
    if kind == CampaignType.FIXED_PRICE:
        if campaign.fixed_price is None:
            raise ValidationError("campaign.reward_not_configured")
        return
    if (campaign.discount_basis or DiscountBasis.PERCENT) == DiscountBasis.PERCENT:
        if float(campaign.discount_percent or 0.0) <= 0:
            raise ValidationError("campaign.reward_not_configured")
    elif D(campaign.discount_amount) <= 0:
        raise ValidationError("campaign.reward_not_configured")


def replace_conditions(
    db: Session, campaign: Campaign, conditions: Iterable[dict[str, Any]]
) -> None:
    """Swap the whole condition set — partial edits would leave orphan rules."""
    campaign.conditions.clear()
    db.flush()
    for raw in conditions:
        campaign.conditions.append(
            CampaignCondition(
                subject=str(raw.get("subject") or "ORDER").upper(),
                subject_id=raw.get("subject_id"),
                metric=str(raw.get("metric") or "AMOUNT").upper(),
                uom=raw.get("uom"),
                min_value=qty(raw.get("min_value") or 0),
                max_value=qty(raw["max_value"]) if raw.get("max_value") is not None else None,
                step_value=qty(raw["step_value"]) if raw.get("step_value") is not None else None,
            )
        )
    db.flush()


def create_campaign(
    db: Session, data: dict[str, Any], *, user_id: int | None = None
) -> Campaign:
    code = str(data.get("code") or "").strip().upper()
    if not code:
        raise ValidationError("campaign.code_required")
    exists = db.execute(
        select(Campaign.id).where(Campaign.code == code, Campaign.is_deleted.is_(False))
    ).scalar_one_or_none()
    if exists:
        raise ConflictError("campaign.code_exists", params={"code": code})

    _validate_window(data.get("start_date"), data.get("end_date"))

    campaign = Campaign(code=code, created_by_id=user_id, updated_by_id=user_id)
    for key in _CAMPAIGN_FIELDS:
        if key in data and data[key] is not None:
            setattr(campaign, key, data[key])
    campaign.status = str(data.get("status") or CampaignStatus.DRAFT)
    db.add(campaign)
    db.flush()

    replace_conditions(db, campaign, data.get("conditions") or [])
    return campaign


def update_campaign(
    db: Session, campaign_id: int, data: dict[str, Any], *, user_id: int | None = None
) -> Campaign:
    campaign = get_campaign(db, campaign_id)
    start = data.get("start_date", campaign.start_date)
    end = data.get("end_date", campaign.end_date)
    _validate_window(start, end)

    for key in _CAMPAIGN_FIELDS:
        if key in data and data[key] is not None:
            setattr(campaign, key, data[key])
    if data.get("code"):
        code = str(data["code"]).strip().upper()
        clash = db.execute(
            select(Campaign.id).where(
                Campaign.code == code,
                Campaign.id != campaign.id,
                Campaign.is_deleted.is_(False),
            )
        ).scalar_one_or_none()
        if clash:
            raise ConflictError("campaign.code_exists", params={"code": code})
        campaign.code = code
    campaign.updated_by_id = user_id

    if data.get("conditions") is not None:
        replace_conditions(db, campaign, data["conditions"])
    db.flush()
    return campaign


def delete_campaign(db: Session, campaign_id: int, *, user_id: int | None = None) -> Campaign:
    """Soft delete — historical applications must keep pointing at a real row."""
    campaign = get_campaign(db, campaign_id)
    campaign.is_deleted = True
    campaign.deleted_at = datetime.now()
    campaign.deleted_by_id = user_id
    if campaign.status == CampaignStatus.ACTIVE:
        campaign.status = CampaignStatus.CANCELLED
    db.flush()
    return campaign


def set_status(
    db: Session, campaign_id: int, status: str, *, user_id: int | None = None
) -> Campaign:
    """Activate / pause / cancel, refusing transitions that make no sense."""
    campaign = get_campaign(db, campaign_id)
    target = str(status).upper()
    if target not in set(CampaignStatus):
        raise ValidationError("campaign.invalid_status", params={"status": target})

    if target == CampaignStatus.ACTIVE:
        if campaign.status not in _ACTIVATABLE:
            raise BusinessRuleError(
                "campaign.invalid_status", params={"status": campaign.status}
            )
        if campaign.end_date < date.today():
            raise BusinessRuleError("campaign.not_active")
        _validate_reward(campaign)
    elif target == CampaignStatus.PAUSED and campaign.status != CampaignStatus.ACTIVE:
        raise BusinessRuleError("campaign.invalid_status", params={"status": campaign.status})

    campaign.status = target
    campaign.updated_by_id = user_id
    db.flush()
    return campaign


# ===========================================================================
# Discount CRUD
# ===========================================================================
def get_discount(db: Session, discount_id: int) -> Discount:
    row = db.get(Discount, discount_id)
    if row is None or row.is_deleted:
        raise NotFoundError("discount.not_found", params={"id": discount_id})
    return row


def list_discounts(
    db: Session,
    *,
    search: str | None = None,
    scope: str | None = None,
    scope_id: int | None = None,
    product_id: int | None = None,
    is_active: bool | None = None,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[Discount], int]:
    stmt = select(Discount).where(Discount.is_deleted.is_(False))
    if scope:
        stmt = stmt.where(Discount.scope == scope)
    if scope_id is not None:
        stmt = stmt.where(Discount.scope_id == scope_id)
    if product_id is not None:
        stmt = stmt.where(Discount.product_id == product_id)
    if is_active is not None:
        stmt = stmt.where(Discount.is_active.is_(is_active))
    if search:
        term = f"%{search.lower()}%"
        stmt = stmt.where(
            func.lower(Discount.code).like(term) | func.lower(Discount.name).like(term)
        )

    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = (
        db.execute(
            stmt.order_by(Discount.priority.asc(), Discount.id.desc()).offset(offset).limit(limit)
        )
        .scalars()
        .all()
    )
    return list(rows), int(total)


_DISCOUNT_FIELDS = (
    "name",
    "scope",
    "scope_id",
    "product_id",
    "category_id",
    "basis",
    "percent",
    "amount",
    "min_quantity",
    "min_amount",
    "valid_from",
    "valid_to",
    "is_active",
    "priority",
)


def create_discount(
    db: Session, data: dict[str, Any], *, user_id: int | None = None
) -> Discount:
    code = str(data.get("code") or "").strip().upper()
    if not code:
        raise ValidationError("discount.code_required")
    exists = db.execute(
        select(Discount.id).where(Discount.code == code, Discount.is_deleted.is_(False))
    ).scalar_one_or_none()
    if exists:
        raise ConflictError("discount.code_exists", params={"code": code})
    _validate_window(data.get("valid_from"), data.get("valid_to"))

    row = Discount(code=code, created_by_id=user_id, updated_by_id=user_id)
    for key in _DISCOUNT_FIELDS:
        if key in data and data[key] is not None:
            setattr(row, key, data[key])
    db.add(row)
    db.flush()
    return row


def update_discount(
    db: Session, discount_id: int, data: dict[str, Any], *, user_id: int | None = None
) -> Discount:
    row = get_discount(db, discount_id)
    _validate_window(
        data.get("valid_from", row.valid_from), data.get("valid_to", row.valid_to)
    )
    for key in _DISCOUNT_FIELDS:
        if key in data and data[key] is not None:
            setattr(row, key, data[key])
    row.updated_by_id = user_id
    db.flush()
    return row


def delete_discount(db: Session, discount_id: int, *, user_id: int | None = None) -> Discount:
    row = get_discount(db, discount_id)
    row.is_deleted = True
    row.is_active = False
    row.deleted_at = datetime.now()
    row.deleted_by_id = user_id
    db.flush()
    return row
