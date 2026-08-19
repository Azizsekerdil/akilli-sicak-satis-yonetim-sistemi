"""
Price resolution and basket pricing.

Everything commercial funnels through here: the field app, the order screen and
the AI assistant all price a basket the same way, so a quote can never disagree
with the invoice that follows it.

Pricing order (each step feeds the next):

1. **Unit price** — price list tier, then the product's list price.
2. **Line discount** — the salesperson's percent, capped by
   ``Product.max_discount_percent``; falls back to a standing agreement or the
   customer's blanket percent when the line carries none.
3. **Campaigns** — one evaluation for the whole basket, so a promotion that
   needs three different products can actually see all three.
4. **Header discount** — spread proportionally over the lines.
5. **Tax** — VAT and excise are computed on what is *left*, never on the gross,
   because tax follows the money that actually changes hands.

Money is Decimal end to end and every stored amount is quantised with
:func:`app.core.utils.money`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import UnitOfMeasure
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging_config import get_logger
from app.core.utils import D, apply_percent, money, qty, vat_from_net
from app.models.customer import Customer
from app.models.product import PriceList, PriceListItem, Product
from app.models.vehicle import Salesperson
from app.services import campaign_service

log = get_logger("app.pricing")

ZERO = Decimal("0")
ONE = Decimal("1")


# ===========================================================================
# Contract dataclasses
# ===========================================================================
@dataclass
class LineInput:
    """One requested basket line, as the caller states it."""

    product_id: int
    quantity: Decimal
    uom: str
    discount_percent: float = 0.0
    unit_price_override: Decimal | None = None


@dataclass
class PricedLine:
    """A fully costed line — everything a SaleItem/OrderItem row needs."""

    product_id: int
    product: Product
    quantity: Decimal
    uom: str
    uom_factor: Decimal
    base_quantity: Decimal
    unit_price: Decimal
    list_price: Decimal
    gross_amount: Decimal
    discount_percent: float
    discount_amount: Decimal
    campaign_discount_amount: Decimal
    net_amount: Decimal
    vat_rate: float
    vat_amount: Decimal
    excise_amount: Decimal
    total_amount: Decimal
    unit_cost: Decimal
    total_cost: Decimal
    margin_amount: Decimal
    is_free_goods: bool = False
    campaign_id: int | None = None
    line_no: int = 0
    price_list_id: int | None = None
    header_discount_amount: Decimal = ZERO
    discount_id: int | None = None
    volume_l: float = 0.0
    weight_kg: float = 0.0

    @property
    def margin_percent(self) -> float:
        if self.net_amount <= 0:
            return 0.0
        return round(float(self.margin_amount / self.net_amount * 100), 2)


@dataclass
class PricedBasket:
    """The whole quote: lines plus the header totals an invoice needs."""

    lines: list[PricedLine] = field(default_factory=list)
    gross_amount: Decimal = ZERO
    line_discount_amount: Decimal = ZERO
    campaign_discount_amount: Decimal = ZERO
    net_amount: Decimal = ZERO
    vat_amount: Decimal = ZERO
    excise_amount: Decimal = ZERO
    total_amount: Decimal = ZERO
    total_cost: Decimal = ZERO
    margin_amount: Decimal = ZERO
    total_volume_l: float = 0.0
    total_weight_kg: float = 0.0
    applied_campaigns: list[dict[str, Any]] = field(default_factory=list)
    header_discount_percent: float = 0.0
    header_discount_amount: Decimal = ZERO
    currency: str = "TRY"

    @property
    def margin_percent(self) -> float:
        if self.net_amount <= 0:
            return 0.0
        return round(float(self.margin_amount / self.net_amount * 100), 2)

    @property
    def free_goods_count(self) -> int:
        return sum(1 for line in self.lines if line.is_free_goods)


# ===========================================================================
# Units of measure
# ===========================================================================
def uom_factor(product: Product, uom: str | None) -> Decimal:
    """
    How many **base units** one *uom* contains.

    Explicit ``ProductUnit`` rows win; otherwise the two units every product
    implicitly has (its base unit and its case) are honoured.  An unknown unit
    is a data error, not something to silently price at 1:1.
    """
    if not uom:
        return ONE
    code = str(uom).strip().upper()
    for unit in product.units or []:
        if (unit.uom or "").upper() == code:
            factor = D(unit.factor)
            return factor if factor > 0 else ONE
    if code == (product.base_uom or "").upper():
        return ONE
    if code in {(product.sales_uom or "").upper(), str(UnitOfMeasure.CASE)}:
        factor = D(product.units_per_case)
        return factor if factor > 0 else ONE
    raise ValidationError(
        "product.invalid_uom", params={"sku": product.sku, "uom": code}
    )


def to_base(product: Product, quantity: Decimal, uom: str) -> Decimal:
    """Convert a quantity expressed in *uom* into base units."""
    return qty(D(quantity) * uom_factor(product, uom))


def _per_base_volume(product: Product) -> float:
    if product.unit_volume_l:
        return float(product.unit_volume_l)
    if product.case_volume_l:
        per_case = D(product.units_per_case)
        if per_case > 0:
            return float(D(product.case_volume_l) / per_case)
    return 0.0


def _per_base_weight(product: Product) -> float:
    if product.unit_weight_kg:
        return float(product.unit_weight_kg)
    if product.case_weight_kg:
        per_case = D(product.units_per_case)
        if per_case > 0:
            return float(D(product.case_weight_kg) / per_case)
    return 0.0


# ===========================================================================
# Price list resolution
# ===========================================================================
def _list_is_valid(price_list: PriceList, on: date) -> bool:
    if price_list.valid_from and price_list.valid_from > on:
        return False
    if price_list.valid_to and price_list.valid_to < on:
        return False
    return True


def _list_match_score(price_list: PriceList, customer: Customer | None) -> int | None:
    """
    How well a list targets this customer, or None when it targets someone else.

    Channel is the strongest signal (a wholesale list must never leak onto a
    grocery), then customer type, then region.
    """
    score = 0
    if price_list.channel:
        if customer is None or (customer.channel or "").upper() != price_list.channel.upper():
            return None
        score += 4
    if price_list.customer_type:
        if (
            customer is None
            or (customer.customer_type or "").upper() != price_list.customer_type.upper()
        ):
            return None
        score += 2
    if price_list.region_id is not None:
        if customer is None or customer.region_id != price_list.region_id:
            return None
        score += 1
    return score


def candidate_price_lists(
    db: Session, customer: Customer | None, on: date
) -> list[PriceList]:
    """
    Every list that may serve this customer, best first.

    Ranking: most specific targeting, then the default list, then ``priority``
    ascending (lower number = stronger, matching the campaign engine).
    """
    rows = (
        db.execute(
            select(PriceList).where(
                PriceList.is_deleted.is_(False), PriceList.is_active.is_(True)
            )
        )
        .scalars()
        .all()
    )
    scored: list[tuple[int, int, int, int, PriceList]] = []
    for row in rows:
        if not _list_is_valid(row, on):
            continue
        score = _list_match_score(row, customer)
        if score is None:
            continue
        scored.append((-score, 0 if row.is_default else 1, int(row.priority or 100), -row.id, row))
    scored.sort(key=lambda item: item[:4])
    return [item[4] for item in scored]


def _item_net_price(item: PriceListItem) -> Decimal:
    """A price-list row may carry its own standing discount."""
    price = D(item.price)
    if item.discount_percent:
        price -= apply_percent(price, float(item.discount_percent))
    return money(max(ZERO, price))


def _price_from_list(
    db: Session,
    price_list_id: int,
    product: Product,
    uom: str,
    quantity: Decimal,
) -> Decimal | None:
    """
    Best applicable row for (list, product, uom), or None when the list cannot
    price this line.

    ``min_quantity`` is a *threshold*, not a hint: a row that says "170 TL from
    10 cases" must not price an order of three.  When no row is reached the list
    declines and the caller falls through to the next one — which is how volume
    tiers are modelled here, since a unique constraint allows only one row per
    (list, product, uom).

    A list that prices a different unit still counts: we convert through base
    units rather than pretending there is no price at all.
    """
    items = (
        db.execute(
            select(PriceListItem).where(
                PriceListItem.price_list_id == price_list_id,
                PriceListItem.product_id == product.id,
            )
        )
        .scalars()
        .all()
    )
    if not items:
        return None

    wanted = str(uom).strip().upper()
    reached = [
        item
        for item in items
        if (item.uom or "").upper() == wanted and D(item.min_quantity) <= D(quantity)
    ]
    if reached:
        return _item_net_price(max(reached, key=lambda i: (D(i.min_quantity), i.id)))

    target_factor = uom_factor(product, wanted)
    base_quantity = D(quantity) * target_factor
    converted: list[tuple[Decimal, Decimal]] = []  # (threshold in base units, per-base price)
    for item in items:
        try:
            factor = uom_factor(product, item.uom)
        except ValidationError:
            continue
        if factor <= 0:
            continue
        threshold = D(item.min_quantity) * factor
        if threshold <= base_quantity:
            converted.append((threshold, _item_net_price(item) / factor))
    if not converted:
        return None

    per_base = max(converted, key=lambda entry: entry[0])[1]
    return money(per_base * target_factor)


def resolve_unit_price(
    db: Session,
    product: Product,
    *,
    uom: str,
    customer: Customer | None = None,
    price_list_id: int | None = None,
    quantity: Decimal = ONE,
    on_date: date | None = None,
) -> tuple[Decimal, int | None]:
    """
    The price one *uom* of *product* costs this customer today.

    Precedence: an explicitly requested list, then the customer's own list, then
    the best-targeted valid list, then the default list, and finally the
    product's list price scaled by the UoM factor.  Returns
    ``(price, price_list_id)`` — the id is ``None`` when the fallback was used,
    which is what the order header records.
    """
    on = on_date or date.today()
    quantity = D(quantity)

    tried: list[int] = []
    if price_list_id:
        tried.append(int(price_list_id))
    if customer is not None and customer.price_list_id:
        # The customer's assigned list is honoured even when another list scores
        # higher — it is a negotiated agreement, not a default.
        if int(customer.price_list_id) not in tried:
            tried.append(int(customer.price_list_id))
    for candidate in candidate_price_lists(db, customer, on):
        if candidate.id not in tried:
            tried.append(candidate.id)

    for candidate_id in tried:
        price = _price_from_list(db, candidate_id, product, uom, quantity)
        if price is not None and price > 0:
            return money(price), candidate_id

    return money(D(product.sale_price) * uom_factor(product, uom)), None


# ===========================================================================
# Basket pricing
# ===========================================================================
def _load_products(db: Session, product_ids: Sequence[int]) -> dict[int, Product]:
    if not product_ids:
        return {}
    rows = (
        db.execute(select(Product).where(Product.id.in_(set(product_ids))))
        .scalars()
        .all()
    )
    return {row.id: row for row in rows}


def _resolve_line_discount(
    db: Session,
    *,
    product: Product,
    customer: Customer | None,
    salesperson: Salesperson | None,
    requested_percent: float,
    quantity: Decimal,
    gross_amount: Decimal,
    on_date: date,
    apply_standing_discounts: bool,
) -> tuple[float, Decimal, int | None]:
    """
    Effective (percent, extra amount, discount id) for one line.

    A percent the *caller* typed is validated hard — exceeding the product's
    ceiling is a policy breach and must be refused.  A percent that comes from
    stored master data is clamped instead: back-office misconfiguration should
    not block a sale at the counter.
    """
    ceiling = float(product.max_discount_percent if product.max_discount_percent is not None else 100.0)
    requested = float(requested_percent or 0.0)
    if requested < 0:
        raise ValidationError("product.discount_too_high", params={"sku": product.sku})
    if requested > 0:
        if requested > ceiling:
            raise ValidationError(
                "product.discount_too_high",
                params={"sku": product.sku, "requested": requested, "max": ceiling},
            )
        return requested, ZERO, None

    if not apply_standing_discounts:
        return 0.0, ZERO, None

    standing = campaign_service.resolve_standing_discount(
        db,
        customer=customer,
        product=product,
        quantity=quantity,
        amount=gross_amount,
        on_date=on_date,
        salesperson=salesperson,
    )
    if standing is not None:
        if standing.percent > 0:
            return min(standing.percent, ceiling), ZERO, standing.discount_id
        if standing.amount > 0:
            return 0.0, money(min(standing.amount, gross_amount)), standing.discount_id

    blanket = float(customer.discount_percent or 0.0) if customer is not None else 0.0
    return (min(blanket, ceiling) if blanket > 0 else 0.0), ZERO, None


def _finalise_line(line: PricedLine) -> None:
    """
    Compute tax, totals, cost and margin from the line's final net amount.

    Run last, after every discount, because VAT and excise follow the money that
    is actually invoiced.
    """
    product = line.product
    line.net_amount = money(max(ZERO, line.net_amount))

    if line.is_free_goods:
        # Bedelsiz goods carry no revenue, so there is nothing to tax — but the
        # stock really leaves the van, so the cost is real.
        line.vat_amount = ZERO
        line.excise_amount = ZERO
        line.total_amount = ZERO
    else:
        excise = money(D(product.excise_amount) * line.base_quantity)
        if product.excise_rate:
            excise += apply_percent(line.net_amount, float(product.excise_rate))
        line.excise_amount = money(excise)
        line.vat_amount = vat_from_net(
            line.net_amount + line.excise_amount, float(line.vat_rate)
        )
        line.total_amount = money(line.net_amount + line.excise_amount + line.vat_amount)

    line.unit_cost = money(product.cost_price)
    line.total_cost = money(line.unit_cost * line.base_quantity)
    line.margin_amount = money(line.net_amount - line.total_cost)
    line.volume_l = round(float(line.base_quantity) * _per_base_volume(product), 4)
    line.weight_kg = round(float(line.base_quantity) * _per_base_weight(product), 4)


def _apply_header_discount(lines: list[PricedLine], percent: float) -> Decimal:
    """
    Spread a header percent across the paying lines.

    Computed as a single amount and then distributed so the parts add back to
    the whole — per-line rounding would drift away from the printed header
    figure.
    """
    if percent <= 0:
        return ZERO
    payable = [line for line in lines if not line.is_free_goods and line.net_amount > 0]
    if not payable:
        return ZERO

    basis = money(sum((line.net_amount for line in payable), ZERO))
    total = apply_percent(basis, percent)
    if total <= 0:
        return ZERO
    total = money(min(total, basis))

    ordered = sorted(payable, key=lambda line: (-line.net_amount, line.line_no))
    running = ZERO
    for line in ordered[1:]:
        share = money(total * line.net_amount / basis)
        line.header_discount_amount = share
        line.net_amount = money(line.net_amount - share)
        running += share
    head = ordered[0]
    head.header_discount_amount = money(total - running)
    head.net_amount = money(max(ZERO, head.net_amount - head.header_discount_amount))
    return total


def price_basket(
    db: Session,
    *,
    customer: Customer | None,
    lines: list[LineInput],
    on_date: date | None = None,
    salesperson: Salesperson | None = None,
    apply_campaigns: bool = True,
    header_discount_percent: float = 0.0,
    price_list_id: int | None = None,
    apply_standing_discounts: bool = True,
) -> PricedBasket:
    """
    Price a whole basket in one pass.

    The basket — not the line — is the unit of evaluation: campaigns such as
    "three different brands earns 5%" can only be judged with every line in
    view, and the header discount can only be spread once the campaign
    discounts are known.
    """
    on = on_date or date.today()
    basket = PricedBasket(header_discount_percent=float(header_discount_percent or 0.0))
    if not lines:
        return basket
    if customer is not None and customer.currency:
        basket.currency = customer.currency

    products = _load_products(db, [line.product_id for line in lines])
    priced: list[PricedLine] = []

    for position, raw in enumerate(lines):
        product = products.get(raw.product_id)
        if product is None:
            raise NotFoundError("product.not_found", params={"id": raw.product_id})
        if not product.is_sellable:
            raise ValidationError("product.not_sellable", params={"sku": product.sku})

        quantity = qty(raw.quantity)
        if quantity <= 0:
            raise ValidationError("product.invalid_quantity", params={"sku": product.sku})

        factor = uom_factor(product, raw.uom)
        base_quantity = qty(quantity * factor)

        list_price, resolved_list_id = resolve_unit_price(
            db,
            product,
            uom=raw.uom,
            customer=customer,
            price_list_id=price_list_id,
            quantity=quantity,
            on_date=on,
        )
        unit_price = (
            money(raw.unit_price_override)
            if raw.unit_price_override is not None
            else list_price
        )
        gross = money(unit_price * quantity)

        percent, extra_amount, discount_id = _resolve_line_discount(
            db,
            product=product,
            customer=customer,
            salesperson=salesperson,
            requested_percent=raw.discount_percent,
            quantity=quantity,
            gross_amount=gross,
            on_date=on,
            apply_standing_discounts=apply_standing_discounts,
        )
        discount_amount = money(apply_percent(gross, percent) + extra_amount)
        discount_amount = money(min(discount_amount, gross))

        priced.append(
            PricedLine(
                product_id=product.id,
                product=product,
                quantity=quantity,
                uom=str(raw.uom).strip().upper(),
                uom_factor=factor,
                base_quantity=base_quantity,
                unit_price=unit_price,
                list_price=list_price,
                gross_amount=gross,
                discount_percent=percent,
                discount_amount=discount_amount,
                campaign_discount_amount=ZERO,
                net_amount=money(gross - discount_amount),
                vat_rate=float(product.vat_rate or 0.0),
                vat_amount=ZERO,
                excise_amount=ZERO,
                total_amount=ZERO,
                unit_cost=money(product.cost_price),
                total_cost=ZERO,
                margin_amount=ZERO,
                line_no=position + 1,
                price_list_id=resolved_list_id,
                discount_id=discount_id,
            )
        )

    # --- Campaigns -------------------------------------------------------
    if apply_campaigns:
        outcome = campaign_service.evaluate(
            db,
            customer=customer,
            lines=[
                {
                    "index": index,
                    "product_id": line.product_id,
                    "product": line.product,
                    "base_quantity": line.base_quantity,
                    "quantity": line.quantity,
                    "uom": line.uom,
                    "gross_amount": line.gross_amount,
                    "net_after_line_discount": line.net_amount,
                }
                for index, line in enumerate(priced)
            ],
            on_date=on,
            salesperson=salesperson,
        )
        for index, amount in outcome.discount_by_line.items():
            if 0 <= index < len(priced):
                line = priced[index]
                capped = money(min(amount, line.net_amount))
                line.campaign_discount_amount = money(line.campaign_discount_amount + capped)
                line.net_amount = money(line.net_amount - capped)
        for gift in outcome.free_goods:
            priced.append(_free_goods_line(gift, len(priced) + 1))
        basket.applied_campaigns = outcome.applied

    # --- Header discount, then tax --------------------------------------
    basket.header_discount_amount = _apply_header_discount(
        priced, basket.header_discount_percent
    )
    for line in priced:
        _finalise_line(line)

    basket.lines = priced
    _roll_up(basket)
    return basket


def _free_goods_line(gift: dict[str, Any], line_no: int) -> PricedLine:
    """Turn a campaign gift into a zero-priced basket line."""
    product: Product = gift["product"]
    return PricedLine(
        product_id=product.id,
        product=product,
        quantity=qty(gift["quantity"]),
        uom=str(gift["uom"]).upper(),
        uom_factor=D(gift["uom_factor"]),
        base_quantity=qty(gift["base_quantity"]),
        unit_price=ZERO,
        list_price=money(product.sale_price),
        gross_amount=ZERO,
        discount_percent=100.0,
        discount_amount=ZERO,
        campaign_discount_amount=ZERO,
        net_amount=ZERO,
        vat_rate=float(product.vat_rate or 0.0),
        vat_amount=ZERO,
        excise_amount=ZERO,
        total_amount=ZERO,
        unit_cost=money(gift["unit_cost"]),
        total_cost=money(gift["cost"]),
        margin_amount=ZERO,
        is_free_goods=True,
        campaign_id=gift.get("campaign_id"),
        line_no=line_no,
    )


def _roll_up(basket: PricedBasket) -> None:
    """Sum the finalised lines into the header totals."""
    basket.gross_amount = money(sum((line.gross_amount for line in basket.lines), ZERO))
    basket.line_discount_amount = money(
        sum((line.discount_amount for line in basket.lines), ZERO)
    )
    basket.campaign_discount_amount = money(
        sum((line.campaign_discount_amount for line in basket.lines), ZERO)
    )
    basket.header_discount_amount = money(
        sum((line.header_discount_amount for line in basket.lines), ZERO)
    )
    basket.net_amount = money(sum((line.net_amount for line in basket.lines), ZERO))
    basket.vat_amount = money(sum((line.vat_amount for line in basket.lines), ZERO))
    basket.excise_amount = money(sum((line.excise_amount for line in basket.lines), ZERO))
    basket.total_amount = money(sum((line.total_amount for line in basket.lines), ZERO))
    basket.total_cost = money(sum((line.total_cost for line in basket.lines), ZERO))
    basket.margin_amount = money(basket.net_amount - basket.total_cost)
    basket.total_volume_l = round(sum(line.volume_l for line in basket.lines), 4)
    basket.total_weight_kg = round(sum(line.weight_kg for line in basket.lines), 4)


def recompute_line(
    db: Session,
    *,
    customer: Customer | None,
    line: LineInput,
    on_date: date | None = None,
    salesperson: Salesperson | None = None,
    price_list_id: int | None = None,
    apply_campaigns: bool = False,
    apply_standing_discounts: bool = True,
) -> PricedLine:
    """
    Re-price a single line, e.g. after the salesperson edits a quantity.

    Campaigns are **off** by default: a basket-wide promotion cannot be judged
    from one line, and re-running it here would double-count against the
    discount the basket already carries.  Pass ``apply_campaigns=True`` only
    when this line is the entire basket.
    """
    basket = price_basket(
        db,
        customer=customer,
        lines=[line],
        on_date=on_date,
        salesperson=salesperson,
        apply_campaigns=apply_campaigns,
        price_list_id=price_list_id,
        apply_standing_discounts=apply_standing_discounts,
    )
    if not basket.lines:
        raise ValidationError("order.empty")
    return basket.lines[0]


def quote_dict(basket: PricedBasket) -> dict[str, Any]:
    """Flatten a basket for API responses and AI tool calls."""
    return {
        "lines": [
            {
                "line_no": line.line_no,
                "product_id": line.product_id,
                "sku": line.product.sku,
                "product_name": line.product.name,
                "quantity": line.quantity,
                "uom": line.uom,
                "uom_factor": line.uom_factor,
                "base_quantity": line.base_quantity,
                "unit_price": line.unit_price,
                "list_price": line.list_price,
                "gross_amount": line.gross_amount,
                "discount_percent": line.discount_percent,
                "discount_amount": line.discount_amount,
                "campaign_discount_amount": line.campaign_discount_amount,
                "header_discount_amount": line.header_discount_amount,
                "net_amount": line.net_amount,
                "vat_rate": line.vat_rate,
                "vat_amount": line.vat_amount,
                "excise_amount": line.excise_amount,
                "total_amount": line.total_amount,
                "unit_cost": line.unit_cost,
                "total_cost": line.total_cost,
                "margin_amount": line.margin_amount,
                "margin_percent": line.margin_percent,
                "is_free_goods": line.is_free_goods,
                "campaign_id": line.campaign_id,
                "price_list_id": line.price_list_id,
            }
            for line in basket.lines
        ],
        "gross_amount": basket.gross_amount,
        "line_discount_amount": basket.line_discount_amount,
        "campaign_discount_amount": basket.campaign_discount_amount,
        "header_discount_percent": basket.header_discount_percent,
        "header_discount_amount": basket.header_discount_amount,
        "net_amount": basket.net_amount,
        "vat_amount": basket.vat_amount,
        "excise_amount": basket.excise_amount,
        "total_amount": basket.total_amount,
        "total_cost": basket.total_cost,
        "margin_amount": basket.margin_amount,
        "margin_percent": basket.margin_percent,
        "total_volume_l": basket.total_volume_l,
        "total_weight_kg": basket.total_weight_kg,
        "currency": basket.currency,
        "applied_campaigns": basket.applied_campaigns,
    }
