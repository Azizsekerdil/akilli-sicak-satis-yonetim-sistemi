"""
Day sessions: the salesperson's working day on one van, and the end-of-day
reconciliation that proves the van is empty of surprises.

The reconciliation identity, per product and in **base units**::

    opening + loaded + reloaded - sold + returned - wastage = theoretical
    theoretical - counted                                   = variance

``opening`` is derived, not stored: it is the current ledger balance minus the
net of every movement attributed to the session, i.e. a true snapshot of what
the van held when the day started.  Deriving it means the report survives
back-dated corrections instead of freezing a stale number at open time.

Note the deliberate split at close time: ``variance`` is the *business* figure
(what the day's paperwork says should be there versus what was physically
counted), while the ``COUNT_ADJUSTMENT`` movement posted to the ledger is
``counted - on_hand``.  In a clean day those are the same number with opposite
signs; when someone has posted a movement outside the day flow they are not,
and only the second one leaves the ledger agreeing with reality.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, and_, func, or_, select, update
from sqlalchemy.orm import Session

from app.core.enums import (
    AuditAction,
    CountStatus,
    DaySessionStatus,
    NotificationSeverity,
    NotificationType,
    PaymentMethod,
    PaymentStatus,
    RoleCode,
    StockMovementType,
    StopStatus,
)
from app.core.exceptions import DaySessionError, InsufficientStockError, NotFoundError
from app.core.i18n import t
from app.core.utils import D, display_money, money, qty
from app.models.base import utcnow
from app.models.product import Product
from app.models.route import RouteStop, Visit
from app.models.sales import Invoice, Payment, Sale
from app.models.system import Notification
from app.models.vehicle import DaySession, VanLoad, Vehicle
from app.models.warehouse import StockBalance, StockCount, StockCountItem, StockMovement
from app.services import audit_service, numbering_service, stock_service, vehicle_service

#: Movement types that consume van stock as spoilage rather than as a sale.
WASTAGE_TYPES: tuple[str, ...] = (
    StockMovementType.WASTAGE,
    StockMovementType.DAMAGE,
    StockMovementType.EXPIRY,
)

#: Reference type stamped on movements produced by a van load-out.
VAN_LOAD_REFERENCE = "VAN_LOAD"

#: Variance value (TRY) above which the closing alert is escalated.
VARIANCE_ALERT_THRESHOLD = Decimal("100")


# ===========================================================================
# Lookup
# ===========================================================================
def get_session(db: Session, session_id: int) -> DaySession:
    session = db.get(DaySession, session_id)
    if session is None:
        raise NotFoundError("day.not_open", params={"id": session_id})
    return session


def get_open_session(
    db: Session, *, salesperson_id: int, on: date | None = None
) -> DaySession | None:
    """The salesperson's currently open session, optionally pinned to a date."""
    stmt = select(DaySession).where(
        DaySession.salesperson_id == salesperson_id,
        DaySession.status == DaySessionStatus.OPEN,
    )
    if on is not None:
        stmt = stmt.where(DaySession.session_date == on)
    return db.execute(
        stmt.order_by(DaySession.session_date.desc(), DaySession.id.desc()).limit(1)
    ).scalar_one_or_none()


def require_open_session(
    db: Session, *, salesperson_id: int, vehicle_id: int | None = None
) -> DaySession:
    """
    The open session, or an error.

    Selling from a van with no open day would leave stock movements that no
    reconciliation ever accounts for, so the sales flow calls this first.
    """
    session = get_open_session(db, salesperson_id=salesperson_id)
    if session is None:
        raise DaySessionError("day.not_open", params={"salesperson_id": salesperson_id})
    if vehicle_id is not None and session.vehicle_id != vehicle_id:
        raise DaySessionError(
            "day.not_open", params={"salesperson_id": salesperson_id, "vehicle_id": vehicle_id}
        )
    return session


def list_sessions_query(
    db: Session,
    *,
    salesperson_id: int | None = None,
    vehicle_id: int | None = None,
    status: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    has_variance: bool | None = None,
    salesperson_ids: list[int] | None = None,
) -> Select[tuple[DaySession]]:
    stmt = select(DaySession)
    if salesperson_id is not None:
        stmt = stmt.where(DaySession.salesperson_id == salesperson_id)
    if vehicle_id is not None:
        stmt = stmt.where(DaySession.vehicle_id == vehicle_id)
    if status:
        stmt = stmt.where(DaySession.status == status)
    if date_from is not None:
        stmt = stmt.where(DaySession.session_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(DaySession.session_date <= date_to)
    if has_variance is not None:
        stmt = stmt.where(DaySession.has_variance.is_(has_variance))
    if salesperson_ids:
        stmt = stmt.where(DaySession.salesperson_id.in_(salesperson_ids))
    return stmt


# ===========================================================================
# Opening the day
# ===========================================================================
def open_day(
    db: Session,
    *,
    salesperson_id: int,
    vehicle_id: int,
    route_id: int | None = None,
    start_odometer: float | None = None,
    session_date: date | None = None,
    user_id: int | None = None,
    notes: str | None = None,
    commit: bool = True,
) -> DaySession:
    """
    Start a working day for one salesperson on one van.

    A salesperson may only have one day open at a time — two open days would
    make every van movement ambiguous at reconciliation.
    """
    person = vehicle_service.get_salesperson(db, salesperson_id)
    vehicle = vehicle_service.get_vehicle(db, vehicle_id)
    vehicle_service.require_usable(vehicle)
    warehouse = vehicle_service.ensure_warehouse(db, vehicle, user_id=user_id)

    on = session_date or date.today()

    existing = db.execute(
        select(DaySession).where(
            DaySession.salesperson_id == person.id,
            DaySession.vehicle_id == vehicle.id,
            DaySession.session_date == on,
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.status == DaySessionStatus.CLOSED:
            raise DaySessionError("day.already_closed", params={"session_id": existing.id})
        raise DaySessionError("day.already_open", params={"session_id": existing.id})

    elsewhere = get_open_session(db, salesperson_id=person.id)
    if elsewhere is not None:
        raise DaySessionError("day.already_open", params={"session_id": elsewhere.id})

    busy_van = db.execute(
        select(DaySession.id).where(
            DaySession.vehicle_id == vehicle.id,
            DaySession.status == DaySessionStatus.OPEN,
        )
    ).scalar_one_or_none()
    if busy_van:
        raise DaySessionError("day.already_open", params={"session_id": busy_van})

    session = DaySession(
        session_date=on,
        salesperson_id=person.id,
        vehicle_id=vehicle.id,
        route_id=route_id,
        warehouse_id=warehouse.id,
        status=DaySessionStatus.OPEN,
        opened_at=utcnow(),
        opened_by_id=user_id,
        start_odometer_km=start_odometer if start_odometer is not None else vehicle.odometer_km,
        notes=notes,
        created_by_id=user_id,
    )
    db.add(session)
    db.flush()

    # Load-outs prepared before the day was opened belong to this day.
    db.execute(
        update(VanLoad)
        .where(
            VanLoad.vehicle_id == vehicle.id,
            VanLoad.load_date == on,
            VanLoad.day_session_id.is_(None),
        )
        .values(day_session_id=session.id)
        .execution_options(synchronize_session=False)
    )

    recalculate(db, session)

    audit_service.record(
        db,
        AuditAction.CREATE,
        entity_type="DaySession",
        entity_id=session.id,
        entity_label=f"{person.code}/{vehicle.plate_number}/{on.isoformat()}",
        user_id=user_id,
        summary=f"day.opened:{person.code}",
        new_values={
            "salesperson_id": person.id,
            "vehicle_id": vehicle.id,
            "route_id": route_id,
            "start_odometer_km": session.start_odometer_km,
        },
    )
    if commit:
        db.commit()
        db.refresh(session)
    return session


# ===========================================================================
# Ledger aggregation
# ===========================================================================
def _session_window(session: DaySession) -> tuple[datetime, datetime]:
    """UTC bounds of the session's calendar day, used for unstamped movements."""
    start = datetime.combine(session.session_date, time.min, tzinfo=UTC)
    return start, start + timedelta(days=1)


def _movement_scope(session: DaySession, warehouse_id: int) -> Any:
    """
    Which ledger rows belong to this session.

    Primary key is ``day_session_id``.  Movements posted against the van on the
    session's date without a session stamp (a load-out prepared before the day
    was opened, an offline sync) are picked up by the date window so they are
    never silently dropped from the reconciliation.
    """
    start, end = _session_window(session)
    return and_(
        StockMovement.warehouse_id == warehouse_id,
        or_(
            StockMovement.day_session_id == session.id,
            and_(
                StockMovement.day_session_id.is_(None),
                StockMovement.moved_at >= start,
                StockMovement.moved_at < end,
                or_(
                    StockMovement.salesperson_id == session.salesperson_id,
                    StockMovement.salesperson_id.is_(None),
                ),
            ),
        ),
    )


def _empty_row(product_id: int) -> dict[str, Any]:
    return {
        "product_id": product_id,
        "product": None,
        "sku": None,
        "product_name": None,
        "base_uom": None,
        "opening": D(0),
        "loaded": D(0),
        "reloaded": D(0),
        "sold": D(0),
        "returned": D(0),
        "wastage": D(0),
        "other": D(0),
        "on_hand": D(0),
        "theoretical": D(0),
        "counted": None,
        "variance": None,
        "unit_cost": D(0),
        "variance_value": None,
    }


def _ledger_rows(db: Session, session: DaySession) -> dict[int, dict[str, Any]]:
    """Per-product movement buckets plus the van's current on-hand balance."""
    warehouse_id = session.warehouse_id or vehicle_service.warehouse_for(db, session.vehicle_id).id
    rows: dict[int, dict[str, Any]] = {}

    movements = db.execute(
        select(
            StockMovement.product_id,
            StockMovement.movement_type,
            VanLoad.is_reload,
            func.sum(StockMovement.quantity).label("total"),
        )
        .select_from(StockMovement)
        .outerjoin(
            VanLoad,
            and_(
                StockMovement.reference_type == VAN_LOAD_REFERENCE,
                StockMovement.reference_id == VanLoad.id,
            ),
        )
        .where(_movement_scope(session, warehouse_id))
        .group_by(StockMovement.product_id, StockMovement.movement_type, VanLoad.is_reload)
    ).all()

    for product_id, movement_type, is_reload, total in movements:
        row = rows.setdefault(product_id, _empty_row(product_id))
        amount = D(total)
        if movement_type == StockMovementType.VEHICLE_LOAD:
            row["reloaded" if is_reload else "loaded"] += amount
        elif movement_type == StockMovementType.SALE:
            row["sold"] += -amount
        elif movement_type == StockMovementType.SALE_RETURN:
            row["returned"] += amount
        elif movement_type in WASTAGE_TYPES:
            row["wastage"] += -amount
        else:
            # Transfers, manual adjustments, promotions: kept visible instead of
            # being folded into a bucket that would misdescribe them.
            row["other"] += amount

    balances = db.execute(
        select(
            StockBalance.product_id,
            func.sum(StockBalance.quantity).label("quantity"),
            func.sum(StockBalance.quantity * StockBalance.average_cost).label("value"),
        )
        .where(StockBalance.warehouse_id == warehouse_id)
        .group_by(StockBalance.product_id)
    ).all()
    for product_id, quantity, value in balances:
        row = rows.setdefault(product_id, _empty_row(product_id))
        row["on_hand"] = D(quantity)
        on_hand = D(quantity)
        # Cost the variance at the van's own weighted average, not the price
        # list: a shortfall costs what those units were carried at.
        row["unit_cost"] = money(D(value) / on_hand) if on_hand else D(0)

    return rows


def _decorate(db: Session, rows: dict[int, dict[str, Any]]) -> None:
    """Attach product master data and fall back to the standard cost."""
    if not rows:
        return
    products = db.execute(
        select(Product).where(Product.id.in_(list(rows)))
    ).scalars().all()
    by_id = {p.id: p for p in products}
    for product_id, row in rows.items():
        product = by_id.get(product_id)
        if product is None:
            continue
        row["product"] = product
        row["sku"] = product.sku
        row["product_name"] = product.name
        row["base_uom"] = product.base_uom
        if row["unit_cost"] <= 0:
            row["unit_cost"] = money(product.cost_price)


def reconciliation_report(
    db: Session,
    session: DaySession,
    *,
    counted: dict[int, Decimal] | None = None,
) -> list[dict[str, Any]]:
    """
    Per-product reconciliation rows for the session.

    ``counted`` defaults to the session's end-of-day van count when one has
    already been recorded; without either, the counted/variance columns are
    ``None`` rather than a misleading zero.
    """
    rows = _ledger_rows(db, session)

    if counted is None:
        counted = _counted_from_stock_count(db, session)

    for product_id in counted or {}:
        rows.setdefault(product_id, _empty_row(product_id))

    _decorate(db, rows)

    for product_id, row in rows.items():
        row["opening"] = qty(
            row["on_hand"]
            - (row["loaded"] + row["reloaded"] - row["sold"] + row["returned"] - row["wastage"] + row["other"])
        )
        row["theoretical"] = qty(
            row["opening"]
            + row["loaded"]
            + row["reloaded"]
            - row["sold"]
            + row["returned"]
            - row["wastage"]
        )
        for key in ("loaded", "reloaded", "sold", "returned", "wastage", "other", "on_hand"):
            row[key] = qty(row[key])

        if counted is not None and product_id in counted:
            row["counted"] = qty(counted[product_id])
            row["variance"] = qty(row["theoretical"] - row["counted"])
            row["variance_value"] = money(row["variance"] * row["unit_cost"])

    return sorted(
        rows.values(), key=lambda r: (r["product_name"] or "", r["product_id"])
    )


def _counted_from_stock_count(db: Session, session: DaySession) -> dict[int, Decimal] | None:
    """Counted quantities from the session's van count, if one exists."""
    count = db.execute(
        select(StockCount)
        .where(
            StockCount.day_session_id == session.id,
            StockCount.is_van_end_of_day.is_(True),
            StockCount.status != CountStatus.CANCELLED,
        )
        .order_by(StockCount.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if count is None:
        return None
    totals: dict[int, Decimal] = {}
    for item in count.items:
        totals[item.product_id] = totals.get(item.product_id, D(0)) + D(item.counted_quantity)
    return totals


# ===========================================================================
# Commercial totals
# ===========================================================================
def _commercial_totals(db: Session, session: DaySession) -> dict[str, Any]:
    """Sales, collections, visits and invoice count for the session."""
    sale_ids = select(Sale.id).where(
        Sale.day_session_id == session.id,
        Sale.is_cancelled.is_(False),
        Sale.is_deleted.is_(False),
    )

    total_sales = db.execute(
        select(func.coalesce(func.sum(Sale.total_amount), 0)).where(
            Sale.day_session_id == session.id,
            Sale.is_cancelled.is_(False),
            Sale.is_deleted.is_(False),
        )
    ).scalar_one()

    cash = db.execute(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.day_session_id == session.id,
            Payment.payment_method == PaymentMethod.CASH,
            Payment.status != PaymentStatus.CANCELLED,
            Payment.is_deleted.is_(False),
        )
    ).scalar_one()

    other = db.execute(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.day_session_id == session.id,
            Payment.payment_method != PaymentMethod.CASH,
            Payment.status != PaymentStatus.CANCELLED,
            Payment.is_deleted.is_(False),
        )
    ).scalar_one()

    invoices = db.execute(
        select(func.count(Invoice.id)).where(
            Invoice.sale_id.in_(sale_ids),
            Invoice.is_deleted.is_(False),
        )
    ).scalar_one()

    visits_done = db.execute(
        select(func.count(Visit.id)).where(Visit.day_session_id == session.id)
    ).scalar_one()

    visits_planned = 0
    if session.route_id:
        visits_planned = db.execute(
            select(func.count(RouteStop.id)).where(RouteStop.route_id == session.route_id)
        ).scalar_one()
        if not visits_done:
            visits_done = db.execute(
                select(func.count(RouteStop.id)).where(
                    RouteStop.route_id == session.route_id,
                    RouteStop.status == StopStatus.COMPLETED,
                )
            ).scalar_one()

    return {
        "total_sales_amount": money(total_sales),
        "total_collected_cash": money(cash),
        "total_collected_other": money(other),
        "invoices_count": int(invoices or 0),
        "visits_done": int(visits_done or 0),
        "visits_planned": int(visits_planned or 0),
    }


def recalculate(
    db: Session,
    session: DaySession,
    *,
    counted: dict[int, Decimal] | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """
    Recompute the session's summary from the ledger and the sales documents.

    Everything here is derived — nothing is incremented in place — so the
    numbers are reproducible from the ledger at any later date.
    """
    rows = reconciliation_report(db, session, counted=counted)

    totals = {
        "loaded_qty": D(0),
        "reloaded_qty": D(0),
        "sold_qty": D(0),
        "returned_qty": D(0),
        "wastage_qty": D(0),
        "theoretical_qty": D(0),
        "counted_qty": D(0),
        "variance_qty": D(0),
        "variance_value": D(0),
    }
    has_count = False
    for row in rows:
        totals["loaded_qty"] += row["loaded"]
        totals["reloaded_qty"] += row["reloaded"]
        totals["sold_qty"] += row["sold"]
        totals["returned_qty"] += row["returned"]
        totals["wastage_qty"] += row["wastage"]
        totals["theoretical_qty"] += row["theoretical"]
        if row["counted"] is not None:
            has_count = True
            totals["counted_qty"] += row["counted"]
            totals["variance_qty"] += row["variance"]
            totals["variance_value"] += row["variance_value"]

    session.loaded_qty = qty(totals["loaded_qty"])
    session.reloaded_qty = qty(totals["reloaded_qty"])
    session.sold_qty = qty(totals["sold_qty"])
    session.returned_qty = qty(totals["returned_qty"])
    session.wastage_qty = qty(totals["wastage_qty"])
    session.theoretical_qty = qty(totals["theoretical_qty"])
    session.counted_qty = qty(totals["counted_qty"])
    session.variance_qty = qty(totals["variance_qty"])
    session.variance_value = money(totals["variance_value"])
    session.has_variance = has_count and (
        session.variance_qty != 0 or session.variance_value != 0
    )

    commercial = _commercial_totals(db, session)
    session.total_sales_amount = commercial["total_sales_amount"]
    session.total_collected_cash = commercial["total_collected_cash"]
    session.total_collected_other = commercial["total_collected_other"]
    session.invoices_count = commercial["invoices_count"]
    session.visits_done = commercial["visits_done"]
    session.visits_planned = commercial["visits_planned"]

    db.flush()
    if commit:
        db.commit()

    return {
        "session_id": session.id,
        "rows": rows,
        **{k: getattr(session, k) for k in (
            "loaded_qty", "reloaded_qty", "sold_qty", "returned_qty", "wastage_qty",
            "theoretical_qty", "counted_qty", "variance_qty", "variance_value",
            "total_sales_amount", "total_collected_cash", "total_collected_other",
            "invoices_count", "visits_done", "visits_planned", "has_variance",
        )},
    }


# ===========================================================================
# Closing the day
# ===========================================================================
def _record_van_count(
    db: Session,
    session: DaySession,
    rows: list[dict[str, Any]],
    *,
    warehouse_id: int,
    user_id: int | None,
) -> StockCount:
    """Persist the physical count that the close is based on."""
    count = StockCount(
        document_no=numbering_service.next_number(db, "COUNT", on=session.session_date),
        warehouse_id=warehouse_id,
        status=CountStatus.APPROVED,
        count_date=session.session_date,
        counted_by_id=user_id,
        approved_by_id=user_id,
        approved_at=utcnow(),
        day_session_id=session.id,
        is_van_end_of_day=True,
        created_by_id=user_id,
    )
    db.add(count)
    db.flush()

    total_variance_qty = D(0)
    total_variance_value = D(0)
    for row in rows:
        if row["counted"] is None:
            continue
        count.items.append(
            StockCountItem(
                product_id=row["product_id"],
                system_quantity=row["theoretical"],
                counted_quantity=row["counted"],
                variance_quantity=row["variance"],
                unit_cost=row["unit_cost"],
                variance_value=row["variance_value"],
            )
        )
        total_variance_qty += row["variance"]
        total_variance_value += row["variance_value"]

    count.total_variance_qty = qty(total_variance_qty)
    count.total_variance_value = money(total_variance_value)
    db.flush()
    return count


def _post_count_adjustments(
    db: Session,
    session: DaySession,
    rows: list[dict[str, Any]],
    *,
    count: StockCount,
    warehouse_id: int,
    user_id: int | None,
) -> int:
    """
    Bring the van's ledger onto the counted figure, lot by lot.

    The delta is measured against **on-hand**, not against theoretical, so the
    balance ends up matching what was physically found even when something was
    posted outside the day flow.  A shortfall is issued through the normal
    FEFO allocation (the missing units are presumed to be the oldest ones);
    a surplus arrives without a lot, because stock nobody recorded has no
    traceable batch.
    """
    reference = {
        "reference_type": "STOCK_COUNT",
        "reference_id": count.id,
        "reference_no": count.document_no,
        "salesperson_id": session.salesperson_id,
        "day_session_id": session.id,
        "user_id": user_id,
        "notes": f"day_session:{session.id}",
    }
    posted = 0

    for row in rows:
        if row["counted"] is None:
            continue
        delta = qty(D(row["counted"]) - D(row["on_hand"]))
        if delta == 0:
            continue

        if delta > 0:
            stock_service.receive_stock(
                db,
                warehouse_id=warehouse_id,
                product_id=row["product_id"],
                base_quantity=delta,
                movement_type=StockMovementType.COUNT_ADJUSTMENT,
                unit_cost=row["unit_cost"],
                **reference,
            )
        else:
            try:
                stock_service.issue_stock(
                    db,
                    warehouse_id=warehouse_id,
                    product_id=row["product_id"],
                    base_quantity=-delta,
                    movement_type=StockMovementType.COUNT_ADJUSTMENT,
                    allow_expired=True,
                    **reference,
                )
            except InsufficientStockError:
                # Reserved or blocked lots must not stop the ledger from
                # matching what is physically on the van: the count wins.
                stock_service.post_movement(
                    db,
                    warehouse_id=warehouse_id,
                    product_id=row["product_id"],
                    movement_type=StockMovementType.COUNT_ADJUSTMENT,
                    base_quantity=delta,
                    unit_cost=row["unit_cost"],
                    **reference,
                )
        posted += 1

    db.flush()
    return posted


def _notify_variance(
    db: Session, session: DaySession, vehicle: Vehicle, count: StockCount
) -> Notification:
    """Raise the stock-variance alert supervisors act on the next morning."""
    value = abs(D(session.variance_value))
    severity = (
        NotificationSeverity.CRITICAL
        if value >= VARIANCE_ALERT_THRESHOLD * 5
        else NotificationSeverity.WARNING
        if value >= VARIANCE_ALERT_THRESHOLD
        else NotificationSeverity.INFO
    )
    label = f"{vehicle.plate_number} {session.session_date.isoformat()}"
    notification = Notification(
        notification_type=NotificationType.STOCK_VARIANCE,
        severity=severity,
        role_code=RoleCode.FIELD_SALES_SUPERVISOR,
        title_tr=t("stock.variance_detected", "tr", variance=display_money(session.variance_qty)),
        title_en=t("stock.variance_detected", "en", variance=display_money(session.variance_qty)),
        body_tr=f"{label} — {display_money(session.variance_value)}",
        body_en=f"{label} — {display_money(session.variance_value)}",
        entity_type="DaySession",
        entity_id=session.id,
        action_url=f"/field/day-sessions/{session.id}",
        dedupe_key=f"day_variance:{session.id}:{count.id}",
    )
    db.add(notification)
    db.flush()
    return notification


def close_day(
    db: Session,
    session: DaySession,
    *,
    counted: dict[int, Decimal] | None,
    declared_cash: Decimal | None = None,
    end_odometer: float | None = None,
    notes: str | None = None,
    user_id: int | None = None,
    commit: bool = True,
) -> DaySession:
    """
    Close the day against a physical van count.

    The count is mandatory: closing without one would let the van's book stock
    drift away from reality with nobody accountable for the difference.
    """
    if session.status == DaySessionStatus.CLOSED:
        raise DaySessionError("day.already_closed", params={"session_id": session.id})
    if counted is None:
        raise DaySessionError("day.count_required", params={"session_id": session.id})

    vehicle = vehicle_service.get_vehicle(db, session.vehicle_id)
    warehouse_id = session.warehouse_id or vehicle_service.warehouse_for(db, vehicle.id).id
    counted = {int(pid): qty(value) for pid, value in counted.items()}

    session.status = DaySessionStatus.RECONCILING
    db.flush()

    summary = recalculate(db, session, counted=counted)
    rows: list[dict[str, Any]] = summary["rows"]

    count = _record_van_count(db, session, rows, warehouse_id=warehouse_id, user_id=user_id)

    _post_count_adjustments(
        db, session, rows, count=count, warehouse_id=warehouse_id, user_id=user_id
    )

    session.declared_cash = money(declared_cash or 0)
    session.cash_variance = money(session.declared_cash - session.total_collected_cash)
    if end_odometer is not None:
        session.end_odometer_km = float(end_odometer)
        if end_odometer > (vehicle.odometer_km or 0.0):
            vehicle.odometer_km = float(end_odometer)
    if notes:
        session.notes = notes
    session.status = DaySessionStatus.CLOSED
    session.closed_at = utcnow()
    session.closed_by_id = user_id
    session.updated_by_id = user_id
    db.flush()

    has_variance = bool(session.has_variance)
    audit_service.record(
        db,
        AuditAction.STOCK_VARIANCE if has_variance else AuditAction.UPDATE,
        entity_type="DaySession",
        entity_id=session.id,
        entity_label=f"{vehicle.plate_number}/{session.session_date.isoformat()}",
        user_id=user_id,
        summary=f"day.closed:{session.id}",
        amount=session.variance_value,
        new_values={
            "count_id": count.id,
            "theoretical_qty": session.theoretical_qty,
            "counted_qty": session.counted_qty,
            "variance_qty": session.variance_qty,
            "variance_value": session.variance_value,
            "declared_cash": session.declared_cash,
            "cash_variance": session.cash_variance,
        },
    )
    if has_variance:
        _notify_variance(db, session, vehicle, count)

    if commit:
        db.commit()
        db.refresh(session)
    return session
