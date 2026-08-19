"""
Real figures for AI prompts.

An agent that is allowed to invent numbers is worse than no agent at all, so
every prompt in this subsystem is built from a *data context* produced here:
either the analytics service's own answer, or — when that module is not
available or does not implement a given routine — an equivalent computed
directly from the ORM.

Nothing in this module talks to a model.  It only reads the business database
and returns plain, JSON-safe dictionaries.
"""

from __future__ import annotations

import importlib
import inspect
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.core.logging_config import get_logger
from app.core.utils import D, display_money, safe_div
from app.models.customer import Customer
from app.models.product import Product
from app.models.route import Route, RouteStop, Visit
from app.models.sales import Invoice, Payment, Sale, SaleItem
from app.models.vehicle import Salesperson, Vehicle
from app.models.warehouse import StockBalance

log = get_logger("app.ai.data")


# ===========================================================================
# Defensive cross-module calls
# ===========================================================================
def call_service(module_name: str, func_name: str, db: Session, **kwargs: Any) -> Any:
    """
    Call ``module.func(db, **kwargs)`` if it exists, otherwise return ``None``.

    Sibling service modules are delivered independently; an agent must degrade
    to its own computation rather than fail because a peer is not deployed yet.
    Only keyword arguments the target actually declares are passed, so a
    slightly different signature still works.
    """
    try:
        module = importlib.import_module(module_name)
    except Exception:
        return None
    func = getattr(module, func_name, None)
    if func is None or not callable(func):
        return None
    try:
        signature = inspect.signature(func)
        accepted = {k: v for k, v in kwargs.items() if k in signature.parameters}
        return func(db, **accepted)
    except Exception:
        log.exception("Delegation to %s.%s failed; using local fallback", module_name, func_name)
        return None


def _f(value: Any) -> float:
    """Money/quantity as a plain float for prompt JSON (never for arithmetic)."""
    return float(display_money(value))


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


# ===========================================================================
# Customers
# ===========================================================================
def get_customer(db: Session, customer_id: int) -> Customer:
    customer = db.get(Customer, customer_id)
    if customer is None or customer.is_deleted:
        raise NotFoundError("customer.not_found", params={"id": customer_id})
    return customer


def customer_snapshot(db: Session, customer_id: int) -> dict[str, Any]:
    """Identity, commercial terms and the headline balances for one customer."""
    customer = get_customer(db, customer_id)
    return {
        "customer_id": customer.id,
        "code": customer.code,
        "name": customer.name,
        "type": customer.customer_type,
        "channel": customer.channel,
        "status": customer.status,
        "city": customer.city,
        "district": customer.district,
        "visit_frequency": customer.visit_frequency,
        "payment_method": customer.payment_method,
        "payment_term_days": customer.payment_term_days,
        "credit_limit": _f(customer.credit_limit),
        "balance": _f(customer.balance),
        "overdue_balance": _f(customer.overdue_balance),
        "available_credit": _f(D(customer.credit_limit) - D(customer.balance)),
        "order_count": customer.order_count,
        "average_order_value": _f(customer.average_order_value),
        "total_sales_amount": _f(customer.total_sales_amount),
        "last_order_date": _iso(customer.last_order_date),
        "last_visit_date": _iso(customer.last_visit_date),
        "last_payment_date": _iso(customer.last_payment_date),
        "risk_score": customer.risk_score,
        "churn_score": customer.churn_score,
    }


def customer_product_history(
    db: Session, customer_id: int, *, days: int = 120, limit: int = 20
) -> list[dict[str, Any]]:
    """Per-product purchase history: how much, how often, how recently."""
    since = date.today() - timedelta(days=days)
    rows = db.execute(
        select(
            SaleItem.product_id,
            Product.code,
            Product.name,
            Product.sales_uom,
            func.sum(SaleItem.quantity).label("total_qty"),
            func.sum(SaleItem.total_amount).label("total_amount"),
            func.count(func.distinct(Sale.id)).label("orders"),
            func.max(Sale.sale_date).label("last_date"),
            func.min(Sale.sale_date).label("first_date"),
        )
        .join(Sale, Sale.id == SaleItem.sale_id)
        .join(Product, Product.id == SaleItem.product_id)
        .where(
            Sale.customer_id == customer_id,
            Sale.sale_date >= since,
            Sale.is_cancelled.is_(False),
            Sale.is_deleted.is_(False),
            SaleItem.is_free_goods.is_(False),
        )
        .group_by(SaleItem.product_id, Product.code, Product.name, Product.sales_uom)
        .order_by(func.sum(SaleItem.total_amount).desc())
        .limit(limit)
    ).all()

    today = date.today()
    history: list[dict[str, Any]] = []
    for row in rows:
        orders = int(row.orders or 0)
        span_days = max(1, ((row.last_date or today) - (row.first_date or today)).days)
        history.append(
            {
                "product_id": row.product_id,
                "sku": row.code,
                "name": row.name,
                "uom": row.sales_uom,
                "orders": orders,
                "total_quantity": _f(row.total_qty),
                "total_amount": _f(row.total_amount),
                "avg_quantity_per_order": round(safe_div(row.total_qty, orders), 2),
                "avg_days_between_orders": round(safe_div(span_days, max(1, orders - 1)), 1)
                if orders > 1
                else None,
                "last_purchase_date": _iso(row.last_date),
                "days_since_last_purchase": (today - row.last_date).days
                if row.last_date
                else None,
            }
        )
    return history


def fallback_order_suggestion(
    db: Session, customer_id: int, *, on_date: date | None = None
) -> dict[str, Any]:
    """
    Reorder proposal derived purely from this customer's own buying rhythm.

    A line is proposed when the customer is at or past their usual gap between
    purchases of that product; the quantity is their own average order size.
    This is intentionally conservative — it never invents a product the
    customer has not bought before.
    """
    reference = on_date or date.today()
    history = customer_product_history(db, customer_id, days=180, limit=30)
    lines: list[dict[str, Any]] = []
    for item in history:
        gap = item["avg_days_between_orders"]
        elapsed = item["days_since_last_purchase"]
        if elapsed is None:
            continue
        due = gap is None or elapsed >= gap * 0.8
        if not due:
            continue
        quantity = round(item["avg_quantity_per_order"] or 0, 2)
        if quantity <= 0:
            continue
        lines.append(
            {
                "product_id": item["product_id"],
                "sku": item["sku"],
                "name": item["name"],
                "uom": item["uom"],
                "suggested_quantity": quantity,
                "reason": "due_by_cycle" if gap else "single_purchase_history",
                "orders": item["orders"],
                "avg_days_between_orders": gap,
                "days_since_last_purchase": elapsed,
                "last_purchase_date": item["last_purchase_date"],
            }
        )

    confidence = min(0.9, 0.25 + 0.05 * len(lines)) if lines else 0.0
    return {
        "source": "fallback_purchase_cycle",
        "customer_id": customer_id,
        "for_date": _iso(reference),
        "lines": lines[:15],
        "line_count": len(lines[:15]),
        "confidence": round(confidence, 2),
        "history_considered": len(history),
    }


def order_suggestion(
    db: Session, customer_id: int, *, on_date: date | None = None
) -> dict[str, Any]:
    """Analytics' order proposal when available, our own cycle model otherwise."""
    result = call_service(
        "app.services.analytics_service",
        "suggest_order",
        db,
        customer_id=customer_id,
        on_date=on_date,
        on=on_date,
    )
    if result:
        payload = _as_dict(result)
        payload.setdefault("source", "analytics_service.suggest_order")
        payload.setdefault("customer_id", customer_id)
        return payload
    return fallback_order_suggestion(db, customer_id, on_date=on_date)


# ===========================================================================
# Demand & forecasting
# ===========================================================================
def weekly_demand(
    db: Session, product_id: int, *, weeks: int = 12, salesperson_id: int | None = None
) -> list[dict[str, Any]]:
    """Sold quantity per ISO week for one product, oldest bucket first."""
    since = date.today() - timedelta(weeks=weeks)
    conditions = [
        SaleItem.product_id == product_id,
        Sale.sale_date >= since,
        Sale.is_cancelled.is_(False),
        Sale.is_deleted.is_(False),
    ]
    if salesperson_id:
        conditions.append(Sale.salesperson_id == salesperson_id)

    rows = db.execute(
        select(Sale.sale_date, SaleItem.base_quantity, SaleItem.total_amount)
        .join(Sale, Sale.id == SaleItem.sale_id)
        .where(*conditions)
    ).all()

    buckets: dict[date, dict[str, Decimal]] = {}
    for sale_date, quantity, amount in rows:
        if sale_date is None:
            continue
        week_start = sale_date - timedelta(days=sale_date.weekday())
        bucket = buckets.setdefault(
            week_start, {"quantity": Decimal("0"), "amount": Decimal("0")}
        )
        bucket["quantity"] += D(quantity)
        bucket["amount"] += D(amount)

    return [
        {
            "week_start": _iso(week),
            "quantity": _f(values["quantity"]),
            "amount": _f(values["amount"]),
        }
        for week, values in sorted(buckets.items())
    ]


def fallback_forecast(
    db: Session, product_id: int, *, horizon_days: int = 14, weeks: int = 12
) -> dict[str, Any]:
    """
    Moving-average demand estimate with a linear trend adjustment.

    Deliberately simple and explainable: the agent has to justify the number to
    a sales manager, and "average of the last four weeks, adjusted for trend"
    is a sentence a human can check.
    """
    series = weekly_demand(db, product_id, weeks=weeks)
    quantities = [row["quantity"] for row in series]
    recent = quantities[-4:] if quantities else []
    baseline = safe_div(sum(recent), len(recent)) if recent else 0.0

    earlier = quantities[-8:-4] if len(quantities) >= 8 else []
    previous = safe_div(sum(earlier), len(earlier)) if earlier else baseline
    trend_pct = round(safe_div(baseline - previous, previous) * 100, 1) if previous else 0.0

    weeks_ahead = horizon_days / 7.0
    predicted = round(baseline * weeks_ahead, 2)
    spread = 0.35 if len(recent) < 3 else 0.2
    product = db.get(Product, product_id)

    return {
        "source": "fallback_moving_average",
        "product_id": product_id,
        "sku": getattr(product, "code", None),
        "product_name": getattr(product, "name", None),
        "horizon_days": horizon_days,
        "method": "MOVING_AVERAGE",
        "weekly_baseline": round(baseline, 2),
        "trend_percent": trend_pct,
        "predicted_quantity": predicted,
        "lower_bound": round(predicted * (1 - spread), 2),
        "upper_bound": round(predicted * (1 + spread), 2),
        "confidence": round(min(0.85, 0.3 + 0.1 * len(recent)), 2),
        "weekly_history": series,
    }


def demand_forecast(
    db: Session,
    product_id: int,
    *,
    horizon_days: int = 14,
    customer_id: int | None = None,
) -> dict[str, Any]:
    """Analytics' forecast when available, moving average otherwise."""
    result = call_service(
        "app.services.analytics_service",
        "forecast_demand",
        db,
        product_id=product_id,
        horizon_days=horizon_days,
        horizon=horizon_days,
        customer_id=customer_id,
    )
    if result:
        payload = _as_dict(result)
        payload.setdefault("source", "analytics_service.forecast_demand")
        payload.setdefault("product_id", product_id)
        payload.setdefault("weekly_history", weekly_demand(db, product_id))
        return payload
    return fallback_forecast(db, product_id, horizon_days=horizon_days)


# ===========================================================================
# Routes
# ===========================================================================
def route_snapshot(db: Session, route_id: int) -> dict[str, Any]:
    """Planned versus actual performance of one route, with stop-level detail."""
    route = db.get(Route, route_id)
    if route is None or route.is_deleted:
        raise NotFoundError("route.not_found", params={"id": route_id})

    stops = list(
        db.execute(
            select(RouteStop).where(RouteStop.route_id == route_id).order_by(RouteStop.sequence)
        ).scalars()
    )
    visits = list(
        db.execute(select(Visit).where(Visit.route_id == route_id)).scalars()
    )

    completed = sum(1 for s in stops if s.status == "COMPLETED")
    skipped = sum(1 for s in stops if s.status == "SKIPPED")
    delays = [s.delay_minutes or 0 for s in stops if s.delay_minutes]
    sold = sum(D(v.sale_amount) for v in visits)
    collected = sum(D(v.collected_amount) for v in visits)
    productive = sum(1 for v in visits if D(v.sale_amount) > 0)

    return {
        "route_id": route.id,
        "code": route.code,
        "name": route.name,
        "route_date": _iso(route.route_date),
        "status": route.status,
        "salesperson_id": route.salesperson_id,
        "vehicle_id": route.vehicle_id,
        "is_optimized": route.is_optimized,
        "planned_stops": route.planned_stops,
        "actual_stops": route.actual_stops,
        "completed_stops": completed,
        "skipped_stops": skipped,
        "planned_distance_km": round(float(route.planned_distance_km or 0), 2),
        "actual_distance_km": round(float(route.actual_distance_km or 0), 2),
        "planned_duration_min": route.planned_duration_min,
        "actual_duration_min": route.actual_duration_min,
        "visit_count": len(visits),
        "productive_visits": productive,
        "strike_rate_percent": round(safe_div(productive, len(visits)) * 100, 1),
        "total_sales_amount": _f(sold),
        "total_collected_amount": _f(collected),
        "sales_per_stop": _f(D(sold) / len(visits)) if visits else 0.0,
        "avg_delay_minutes": round(safe_div(sum(delays), len(delays)), 1) if delays else 0.0,
        "skip_reasons": [
            {"customer_id": s.customer_id, "reason": s.skip_reason}
            for s in stops
            if s.status == "SKIPPED"
        ][:10],
        "km_per_stop": round(
            safe_div(float(route.actual_distance_km or route.planned_distance_km or 0), max(1, len(stops))),
            2,
        ),
    }


def route_efficiency(db: Session, route_id: int) -> dict[str, Any]:
    """Analytics' route scoring when available, our own snapshot otherwise."""
    result = call_service(
        "app.services.analytics_service", "route_efficiency", db, route_id=route_id
    )
    snapshot = route_snapshot(db, route_id)
    if result:
        payload = _as_dict(result)
        snapshot.update({k: v for k, v in payload.items() if v is not None})
        snapshot["source"] = "analytics_service.route_efficiency"
    else:
        snapshot["source"] = "fallback_route_snapshot"
    return snapshot


# ===========================================================================
# Van load
# ===========================================================================
def fallback_van_load(
    db: Session,
    *,
    salesperson_id: int,
    vehicle_id: int | None = None,
    on_date: date | None = None,
    weeks: int = 4,
) -> dict[str, Any]:
    """
    Load proposal from the salesperson's own recent daily throughput.

    Each product's average sold-per-selling-day over the last few weeks, plus a
    20% safety buffer, capped by what the source warehouse actually holds.
    """
    reference = on_date or date.today()
    since = reference - timedelta(weeks=weeks)

    rows = db.execute(
        select(
            SaleItem.product_id,
            Product.code,
            Product.name,
            Product.sales_uom,
            Product.case_volume_l,
            Product.case_weight_kg,
            func.sum(SaleItem.quantity).label("total_qty"),
            func.count(func.distinct(Sale.sale_date)).label("selling_days"),
        )
        .join(Sale, Sale.id == SaleItem.sale_id)
        .join(Product, Product.id == SaleItem.product_id)
        .where(
            Sale.salesperson_id == salesperson_id,
            Sale.sale_date >= since,
            Sale.sale_date < reference,
            Sale.is_cancelled.is_(False),
            Sale.is_deleted.is_(False),
        )
        .group_by(
            SaleItem.product_id,
            Product.code,
            Product.name,
            Product.sales_uom,
            Product.case_volume_l,
            Product.case_weight_kg,
        )
        .order_by(func.sum(SaleItem.quantity).desc())
        .limit(40)
    ).all()

    salesperson = db.get(Salesperson, salesperson_id)
    vehicle = db.get(Vehicle, vehicle_id) if vehicle_id else None
    if vehicle is None and salesperson is not None and salesperson.default_vehicle_id:
        vehicle = db.get(Vehicle, salesperson.default_vehicle_id)
    warehouse_id = getattr(salesperson, "default_warehouse_id", None)

    available: dict[int, float] = {}
    if warehouse_id:
        for product_id, quantity in db.execute(
            select(StockBalance.product_id, func.sum(StockBalance.quantity))
            .where(
                StockBalance.warehouse_id == warehouse_id,
                StockBalance.status == "AVAILABLE",
            )
            .group_by(StockBalance.product_id)
        ).all():
            available[int(product_id)] = float(D(quantity))

    lines: list[dict[str, Any]] = []
    total_volume = 0.0
    total_weight = 0.0
    for row in rows:
        per_day = safe_div(row.total_qty, max(1, int(row.selling_days or 1)))
        suggested = round(per_day * 1.2, 2)
        if suggested <= 0:
            continue
        stock = available.get(int(row.product_id))
        if stock is not None:
            suggested = round(min(suggested, stock), 2)
        if suggested <= 0:
            continue
        volume = suggested * float(row.case_volume_l or 0)
        weight = suggested * float(row.case_weight_kg or 0)
        total_volume += volume
        total_weight += weight
        lines.append(
            {
                "product_id": row.product_id,
                "sku": row.code,
                "name": row.name,
                "uom": row.sales_uom,
                "suggested_quantity": suggested,
                "avg_daily_sales": round(per_day, 2),
                "selling_days_observed": int(row.selling_days or 0),
                "warehouse_available": stock,
                "volume_l": round(volume, 2),
                "weight_kg": round(weight, 2),
            }
        )

    capacity_volume = float(getattr(vehicle, "capacity_volume_l", 0) or 0)
    capacity_weight = float(getattr(vehicle, "capacity_weight_kg", 0) or 0)
    return {
        "source": "fallback_daily_throughput",
        "salesperson_id": salesperson_id,
        "salesperson_name": getattr(salesperson, "full_name", None),
        "vehicle_id": getattr(vehicle, "id", None),
        "vehicle_plate": getattr(vehicle, "plate_number", None),
        "source_warehouse_id": warehouse_id,
        "for_date": _iso(reference),
        "lines": lines[:25],
        "line_count": len(lines[:25]),
        "total_volume_l": round(total_volume, 2),
        "total_weight_kg": round(total_weight, 2),
        "capacity_volume_l": capacity_volume,
        "capacity_weight_kg": capacity_weight,
        "volume_utilisation_percent": round(safe_div(total_volume, capacity_volume) * 100, 1),
        "weight_utilisation_percent": round(safe_div(total_weight, capacity_weight) * 100, 1),
        "confidence": round(min(0.85, 0.3 + 0.05 * len(lines)), 2),
    }


def van_load_suggestion(
    db: Session,
    *,
    salesperson_id: int,
    vehicle_id: int | None = None,
    on_date: date | None = None,
) -> dict[str, Any]:
    """Analytics' van-load proposal when available, throughput model otherwise."""
    result = call_service(
        "app.services.analytics_service",
        "suggest_van_load",
        db,
        salesperson_id=salesperson_id,
        vehicle_id=vehicle_id,
        on_date=on_date,
        on=on_date,
    )
    if result:
        payload = _as_dict(result)
        payload.setdefault("source", "analytics_service.suggest_van_load")
        payload.setdefault("salesperson_id", salesperson_id)
        return payload
    return fallback_van_load(
        db, salesperson_id=salesperson_id, vehicle_id=vehicle_id, on_date=on_date
    )


# ===========================================================================
# Collection risk
# ===========================================================================
def collection_snapshot(db: Session, customer_id: int) -> dict[str, Any]:
    """Ageing, payment behaviour and exposure for one customer."""
    snapshot = customer_snapshot(db, customer_id)
    today = date.today()

    invoices = list(
        db.execute(
            select(Invoice).where(
                Invoice.customer_id == customer_id,
                Invoice.is_deleted.is_(False),
                Invoice.open_amount > 0,
            )
        ).scalars()
    )

    buckets = {"current": Decimal("0"), "d1_30": Decimal("0"), "d31_60": Decimal("0"),
               "d61_90": Decimal("0"), "d90_plus": Decimal("0")}
    oldest_overdue_days = 0
    for invoice in invoices:
        amount = D(invoice.open_amount)
        due = invoice.due_date or invoice.invoice_date
        overdue_days = (today - due).days if due else 0
        oldest_overdue_days = max(oldest_overdue_days, overdue_days)
        if overdue_days <= 0:
            buckets["current"] += amount
        elif overdue_days <= 30:
            buckets["d1_30"] += amount
        elif overdue_days <= 60:
            buckets["d31_60"] += amount
        elif overdue_days <= 90:
            buckets["d61_90"] += amount
        else:
            buckets["d90_plus"] += amount

    payments = list(
        db.execute(
            select(Payment)
            .where(
                Payment.customer_id == customer_id,
                Payment.is_deleted.is_(False),
            )
            .order_by(Payment.payment_date.desc())
            .limit(12)
        ).scalars()
    )
    bounced = sum(1 for p in payments if p.status == "BOUNCED")

    aging = call_service(
        "app.services.ledger_service", "aging", db, customer_id=customer_id, as_of=today
    )

    snapshot.update(
        {
            "open_invoice_count": len(invoices),
            "open_amount": _f(sum(D(i.open_amount) for i in invoices)),
            "aging": {k: _f(v) for k, v in buckets.items()},
            "aging_source": "ledger_service.aging" if aging else "fallback_invoice_aging",
            "ledger_aging": _as_dict(aging) if aging else None,
            "oldest_overdue_days": oldest_overdue_days,
            "recent_payment_count": len(payments),
            "bounced_payment_count": bounced,
            "last_payments": [
                {
                    "date": _iso(p.payment_date),
                    "amount": _f(p.amount),
                    "method": p.payment_method,
                    "status": p.status,
                }
                for p in payments[:5]
            ],
            "credit_utilisation_percent": round(
                safe_div(float(D(snapshot["balance"])), float(D(snapshot["credit_limit"]))) * 100,
                1,
            ),
        }
    )
    return snapshot


# ===========================================================================
# Company pulse (general questions)
# ===========================================================================
def company_pulse(
    db: Session, *, days: int = 30, salesperson_ids: list[int] | None = None
) -> dict[str, Any]:
    """Headline trading figures for the recent period, scoped where required."""
    since = date.today() - timedelta(days=days)
    conditions = [
        Sale.sale_date >= since,
        Sale.is_cancelled.is_(False),
        Sale.is_deleted.is_(False),
    ]
    if salesperson_ids:
        conditions.append(Sale.salesperson_id.in_(salesperson_ids))

    totals = db.execute(
        select(
            func.count(Sale.id),
            func.sum(Sale.total_amount),
            func.sum(Sale.margin_amount),
            func.count(func.distinct(Sale.customer_id)),
        ).where(*conditions)
    ).one()

    top_products = db.execute(
        select(
            Product.code,
            Product.name,
            func.sum(SaleItem.total_amount).label("amount"),
            func.sum(SaleItem.quantity).label("quantity"),
        )
        .join(Sale, Sale.id == SaleItem.sale_id)
        .join(Product, Product.id == SaleItem.product_id)
        .where(*conditions)
        .group_by(Product.code, Product.name)
        .order_by(func.sum(SaleItem.total_amount).desc())
        .limit(5)
    ).all()

    top_customers = db.execute(
        select(
            Customer.code,
            Customer.name,
            func.sum(Sale.total_amount).label("amount"),
        )
        .join(Customer, Customer.id == Sale.customer_id)
        .where(*conditions)
        .group_by(Customer.code, Customer.name)
        .order_by(func.sum(Sale.total_amount).desc())
        .limit(5)
    ).all()

    sale_count, revenue, margin, customer_count = totals
    return {
        "period_days": days,
        "period_start": _iso(since),
        "period_end": _iso(date.today()),
        "scoped_to_salespersons": salesperson_ids or "all",
        "sale_count": int(sale_count or 0),
        "revenue": _f(revenue),
        "margin": _f(margin),
        "margin_percent": round(safe_div(float(D(margin)), float(D(revenue))) * 100, 1),
        "active_customers": int(customer_count or 0),
        "avg_sale_value": _f(D(revenue) / int(sale_count)) if sale_count else 0.0,
        "top_products": [
            {"sku": r.code, "name": r.name, "amount": _f(r.amount), "quantity": _f(r.quantity)}
            for r in top_products
        ],
        "top_customers": [
            {"code": r.code, "name": r.name, "amount": _f(r.amount)} for r in top_customers
        ],
    }


def _as_dict(value: Any) -> dict[str, Any]:
    """Normalise a peer service's return value into a plain dictionary."""
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "__dataclass_fields__"):
        from dataclasses import asdict

        return asdict(value)
    if isinstance(value, list):
        return {"items": value, "count": len(value)}
    if hasattr(value, "model_dump"):
        return dict(value.model_dump())
    return {"value": value}


__all__ = [
    "call_service",
    "collection_snapshot",
    "company_pulse",
    "customer_product_history",
    "customer_snapshot",
    "demand_forecast",
    "fallback_forecast",
    "fallback_order_suggestion",
    "fallback_van_load",
    "get_customer",
    "order_suggestion",
    "route_efficiency",
    "route_snapshot",
    "van_load_suggestion",
    "weekly_demand",
]
