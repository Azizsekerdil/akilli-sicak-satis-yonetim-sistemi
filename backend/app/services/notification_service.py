"""
In-app notifications and the alert rules that raise them.

Alerts are *derived*, not pushed: :func:`run_checks` re-evaluates every rule
against the live database and raises a notification only when the situation is
new.  Each rule supplies a ``dedupe_key`` that includes the day (and the subject
id), so a low-stock condition that persists for a week produces one alert per
day per product rather than one per scheduler tick.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.enums import (
    AIRequestStatus,
    DaySessionStatus,
    LedgerEntryType,
    NotificationSeverity,
    NotificationType,
    RoleCode,
    StockStatus,
    StopStatus,
    WarehouseType,
)
from app.core.logging_config import get_logger
from app.core.utils import D, display_money, month_start, pct
from app.models.ai import AIProviderConfig, AIRequest, AIUsageDaily
from app.models.analytics import Target
from app.models.base import utcnow
from app.models.customer import Customer, CustomerLedger
from app.models.product import Product
from app.models.route import Route, RouteStop
from app.models.system import BackupRecord, Notification
from app.models.vehicle import DaySession, Salesperson, Vehicle
from app.models.warehouse import Lot, StockBalance, Warehouse
from app.services import setting_service

log = get_logger("app.notifications")

#: Roles that should see operational alerts when no specific user is targeted.
ROLE_STOCK = RoleCode.WAREHOUSE_MANAGER
ROLE_FINANCE = RoleCode.ACCOUNTING
ROLE_FIELD = RoleCode.SALES_MANAGER
ROLE_ADMIN = RoleCode.SYSTEM_ADMIN
ROLE_AI = RoleCode.AI_MANAGER


# ===========================================================================
# Core API
# ===========================================================================
def notify(
    db: Session,
    *,
    notification_type: str = NotificationType.SYSTEM,
    severity: str = NotificationSeverity.INFO,
    title_tr: str,
    title_en: str,
    body_tr: str | None = None,
    body_en: str | None = None,
    user_id: int | None = None,
    role_code: str | None = None,
    entity_type: str | None = None,
    entity_id: int | None = None,
    action_url: str | None = None,
    dedupe_key: str | None = None,
    expires_at: datetime | None = None,
    commit: bool = False,
) -> Notification | None:
    """
    Raise a notification, or return ``None`` if an identical live one exists.

    "Live" means not dismissed and not expired — a dismissed alert may be
    raised again, which is what makes an acknowledged-then-worsening condition
    reappear.
    """
    if dedupe_key:
        now = utcnow()
        existing = db.execute(
            select(Notification.id)
            .where(
                Notification.dedupe_key == dedupe_key,
                Notification.is_dismissed.is_(False),
                or_(Notification.expires_at.is_(None), Notification.expires_at > now),
            )
            .limit(1)
        ).scalar_one_or_none()
        if existing is not None:
            return None

    row = Notification(
        notification_type=str(notification_type),
        severity=str(severity),
        user_id=user_id,
        role_code=role_code,
        title_tr=title_tr[:255],
        title_en=(title_en or title_tr)[:255],
        body_tr=body_tr,
        body_en=body_en,
        entity_type=entity_type,
        entity_id=entity_id,
        action_url=action_url,
        dedupe_key=dedupe_key[:128] if dedupe_key else None,
        expires_at=expires_at,
    )
    db.add(row)
    db.flush()
    if commit:
        db.commit()
    return row


def _visibility_filter(user: Any) -> Any:
    role_code = getattr(getattr(user, "role", None), "code", None)
    clauses = [Notification.user_id == getattr(user, "id", 0)]
    if role_code:
        clauses.append(Notification.role_code == role_code)
    # Broadcasts (no user, no role) go to everyone.
    clauses.append(
        (Notification.user_id.is_(None)) & (Notification.role_code.is_(None))
    )
    return or_(*clauses)


def list_for(
    db: Session,
    user: Any,
    *,
    unread_only: bool = False,
    include_dismissed: bool = False,
    notification_type: str | None = None,
    severity: str | None = None,
    page: int = 1,
    size: int = 50,
) -> tuple[list[Notification], int]:
    """Notifications visible to *user*, newest first, with the total count."""
    now = utcnow()
    conds: list[Any] = [
        _visibility_filter(user),
        or_(Notification.expires_at.is_(None), Notification.expires_at > now),
    ]
    if not include_dismissed:
        conds.append(Notification.is_dismissed.is_(False))
    if unread_only:
        conds.append(Notification.is_read.is_(False))
    if notification_type:
        conds.append(Notification.notification_type == notification_type)
    if severity:
        conds.append(Notification.severity == severity)

    total = int(
        db.execute(select(func.count(Notification.id)).where(*conds)).scalar_one() or 0
    )
    rows = db.execute(
        select(Notification)
        .where(*conds)
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .offset(max(0, (page - 1) * size))
        .limit(size)
    ).scalars().all()
    return list(rows), total


def unread_count(db: Session, user: Any) -> int:
    now = utcnow()
    return int(
        db.execute(
            select(func.count(Notification.id)).where(
                _visibility_filter(user),
                Notification.is_read.is_(False),
                Notification.is_dismissed.is_(False),
                or_(Notification.expires_at.is_(None), Notification.expires_at > now),
            )
        ).scalar_one()
        or 0
    )


def mark_read(db: Session, user: Any, notification_id: int) -> Notification | None:
    row = db.execute(
        select(Notification).where(
            Notification.id == notification_id, _visibility_filter(user)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    if not row.is_read:
        row.is_read = True
        row.read_at = utcnow()
        db.commit()
    return row


def mark_all_read(db: Session, user: Any) -> int:
    rows = db.execute(
        select(Notification).where(
            _visibility_filter(user), Notification.is_read.is_(False)
        )
    ).scalars().all()
    now = utcnow()
    for row in rows:
        row.is_read = True
        row.read_at = now
    db.commit()
    return len(rows)


def dismiss(db: Session, user: Any, notification_id: int) -> Notification | None:
    row = db.execute(
        select(Notification).where(
            Notification.id == notification_id, _visibility_filter(user)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    row.is_dismissed = True
    row.is_read = True
    row.read_at = row.read_at or utcnow()
    db.commit()
    return row


def purge_expired(db: Session, *, older_than_days: int = 90) -> int:
    """Delete dismissed/expired notifications — they are transient by design."""
    cutoff = utcnow() - timedelta(days=older_than_days)
    rows = db.execute(
        select(Notification).where(
            or_(
                Notification.created_at < cutoff,
                (Notification.expires_at.isnot(None)) & (Notification.expires_at < utcnow()),
            )
        )
    ).scalars().all()
    for row in rows:
        db.delete(row)
    db.commit()
    return len(rows)


# ===========================================================================
# Alert rules
# ===========================================================================
def _salesperson_user_ids(db: Session) -> dict[int, int]:
    """salesperson_id -> user_id, so field alerts reach the right person."""
    return {
        int(sp_id): int(user_id)
        for sp_id, user_id in db.execute(
            select(Salesperson.id, Salesperson.user_id).where(Salesperson.user_id.isnot(None))
        ).all()
    }


def _check_low_stock(db: Session, today: date) -> list[Notification]:
    """Depot stock below the product's minimum level is a reorder signal."""
    rows = db.execute(
        select(
            Product.id,
            Product.sku,
            Product.name,
            Product.min_stock_level,
            func.sum(StockBalance.quantity).label("on_hand"),
        )
        .select_from(StockBalance)
        .join(Product, StockBalance.product_id == Product.id)
        .join(Warehouse, StockBalance.warehouse_id == Warehouse.id)
        .where(
            Warehouse.warehouse_type != WarehouseType.VEHICLE,
            Warehouse.is_deleted.is_(False),
            StockBalance.status == StockStatus.AVAILABLE,
            Product.min_stock_level > 0,
            Product.is_active.is_(True),
        )
        .group_by(Product.id, Product.sku, Product.name, Product.min_stock_level)
        .having(func.sum(StockBalance.quantity) < Product.min_stock_level)
    ).all()

    out: list[Notification] = []
    for r in rows:
        on_hand = D(r.on_hand)
        minimum = D(r.min_stock_level)
        ratio = pct(on_hand, minimum)
        severity = (
            NotificationSeverity.CRITICAL if on_hand <= 0
            else NotificationSeverity.WARNING if ratio < 50
            else NotificationSeverity.INFO
        )
        note = notify(
            db,
            notification_type=NotificationType.LOW_STOCK,
            severity=severity,
            title_tr=f"Düşük stok: {r.name}",
            title_en=f"Low stock: {r.name}",
            body_tr=f"{r.sku} — mevcut {on_hand}, minimum {minimum}. Tedarik planlayın.",
            body_en=f"{r.sku} — on hand {on_hand}, minimum {minimum}. Plan a replenishment.",
            role_code=ROLE_STOCK,
            entity_type="Product",
            entity_id=int(r.id),
            action_url=f"/stock/products/{int(r.id)}",
            dedupe_key=f"low_stock:{r.id}:{today.isoformat()}",
        )
        if note:
            out.append(note)
    return out


def _check_expiry(db: Session, today: date) -> list[Notification]:
    warn_days = int(
        setting_service.get_typed(db, "stock", "expiry_warning_days", settings.expiry_warning_days)
        or settings.expiry_warning_days
    )
    cutoff = today + timedelta(days=warn_days)
    rows = db.execute(
        select(
            Lot.id,
            Lot.lot_number,
            Lot.expiry_date,
            Product.sku,
            Product.name,
            Warehouse.name.label("warehouse_name"),
            func.sum(StockBalance.quantity).label("quantity"),
        )
        .select_from(StockBalance)
        .join(Lot, StockBalance.lot_id == Lot.id)
        .join(Product, StockBalance.product_id == Product.id)
        .join(Warehouse, StockBalance.warehouse_id == Warehouse.id)
        .where(
            Lot.expiry_date.isnot(None),
            Lot.expiry_date <= cutoff,
            StockBalance.quantity > 0,
        )
        .group_by(
            Lot.id, Lot.lot_number, Lot.expiry_date, Product.sku, Product.name, Warehouse.name
        )
    ).all()

    out: list[Notification] = []
    for r in rows:
        days_left = (r.expiry_date - today).days
        severity = (
            NotificationSeverity.CRITICAL if days_left < 0
            else NotificationSeverity.WARNING if days_left <= max(1, warn_days // 3)
            else NotificationSeverity.INFO
        )
        state_tr = "SKT geçti" if days_left < 0 else f"{days_left} gün kaldı"
        state_en = "expired" if days_left < 0 else f"{days_left} days left"
        note = notify(
            db,
            notification_type=NotificationType.EXPIRY_WARNING,
            severity=severity,
            title_tr=f"SKT uyarısı: {r.name}",
            title_en=f"Expiry warning: {r.name}",
            body_tr=(
                f"{r.sku} / parti {r.lot_number} — {r.warehouse_name} deposunda "
                f"{D(r.quantity)} adet, {state_tr} ({r.expiry_date})."
            ),
            body_en=(
                f"{r.sku} / lot {r.lot_number} — {D(r.quantity)} units in "
                f"{r.warehouse_name}, {state_en} ({r.expiry_date})."
            ),
            role_code=ROLE_STOCK,
            entity_type="Lot",
            entity_id=int(r.id),
            action_url=f"/stock/lots/{int(r.id)}",
            dedupe_key=f"expiry:{r.id}:{today.isoformat()}",
        )
        if note:
            out.append(note)
    return out


def _check_overdue_receivables(db: Session, today: date) -> list[Notification]:
    rows = db.execute(
        select(
            Customer.id,
            Customer.code,
            Customer.name,
            Customer.default_salesperson_id,
            func.sum(CustomerLedger.open_amount).label("overdue"),
            func.min(CustomerLedger.due_date).label("oldest_due"),
        )
        .select_from(CustomerLedger)
        .join(Customer, CustomerLedger.customer_id == Customer.id)
        .where(
            CustomerLedger.is_settled.is_(False),
            CustomerLedger.open_amount > 0,
            CustomerLedger.due_date.isnot(None),
            CustomerLedger.due_date < today,
            CustomerLedger.entry_type.in_(
                [LedgerEntryType.INVOICE, LedgerEntryType.DEBIT_NOTE,
                 LedgerEntryType.OPENING_BALANCE]
            ),
        )
        .group_by(Customer.id, Customer.code, Customer.name, Customer.default_salesperson_id)
    ).all()

    sp_users = _salesperson_user_ids(db)
    out: list[Notification] = []
    for r in rows:
        overdue = display_money(r.overdue)
        days = (today - r.oldest_due).days if r.oldest_due else 0
        severity = (
            NotificationSeverity.CRITICAL if days > 90
            else NotificationSeverity.WARNING if days > 30
            else NotificationSeverity.INFO
        )
        note = notify(
            db,
            notification_type=NotificationType.OVERDUE_PAYMENT,
            severity=severity,
            title_tr=f"Vadesi geçen alacak: {r.name}",
            title_en=f"Overdue receivable: {r.name}",
            body_tr=f"{r.code} — {overdue} TL, en eski vade {days} gün geçmiş.",
            body_en=f"{r.code} — {overdue} TRY outstanding, oldest {days} days past due.",
            user_id=sp_users.get(int(r.default_salesperson_id or 0)),
            role_code=None if sp_users.get(int(r.default_salesperson_id or 0)) else ROLE_FINANCE,
            entity_type="Customer",
            entity_id=int(r.id),
            action_url=f"/crm/customers/{int(r.id)}/ledger",
            dedupe_key=f"overdue:{r.id}:{today.isoformat()}",
        )
        if note:
            out.append(note)
    return out


def _check_route_delays(db: Session, today: date) -> list[Notification]:
    """A stop running materially late puts the rest of the day's plan at risk."""
    threshold = int(
        setting_service.get_typed(db, "route", "delay_alert_minutes", 30) or 30
    )
    rows = db.execute(
        select(
            Route.id,
            Route.code,
            Route.name,
            Route.salesperson_id,
            func.count(RouteStop.id).label("late_stops"),
            func.max(RouteStop.delay_minutes).label("worst_delay"),
        )
        .select_from(RouteStop)
        .join(Route, RouteStop.route_id == Route.id)
        .where(
            Route.route_date == today,
            Route.is_template.is_(False),
            RouteStop.delay_minutes >= threshold,
            RouteStop.status != StopStatus.COMPLETED,
        )
        .group_by(Route.id, Route.code, Route.name, Route.salesperson_id)
    ).all()

    sp_users = _salesperson_user_ids(db)
    out: list[Notification] = []
    for r in rows:
        note = notify(
            db,
            notification_type=NotificationType.ROUTE_DELAY,
            severity=NotificationSeverity.WARNING,
            title_tr=f"Rota gecikmesi: {r.name}",
            title_en=f"Route delay: {r.name}",
            body_tr=f"{r.code} — {int(r.late_stops)} durak gecikmede, en fazla {int(r.worst_delay or 0)} dk.",
            body_en=f"{r.code} — {int(r.late_stops)} stops delayed, worst {int(r.worst_delay or 0)} min.",
            user_id=sp_users.get(int(r.salesperson_id or 0)),
            role_code=ROLE_FIELD,
            entity_type="Route",
            entity_id=int(r.id),
            action_url=f"/field/routes/{int(r.id)}",
            dedupe_key=f"route_delay:{r.id}:{today.isoformat()}",
        )
        if note:
            out.append(note)
    return out


def _check_stock_variance(db: Session, today: date) -> list[Notification]:
    since = today - timedelta(days=2)
    rows = db.execute(
        select(DaySession)
        .where(
            DaySession.has_variance.is_(True),
            DaySession.session_date >= since,
            DaySession.status.in_([DaySessionStatus.CLOSED, DaySessionStatus.DISPUTED]),
        )
    ).scalars().all()

    people = {s.id: s.full_name for s in db.execute(select(Salesperson)).scalars()}
    vehicles = {v.id: v.plate_number for v in db.execute(select(Vehicle)).scalars()}

    out: list[Notification] = []
    for session in rows:
        who = people.get(session.salesperson_id, str(session.salesperson_id))
        plate = vehicles.get(session.vehicle_id, str(session.vehicle_id))
        value = display_money(abs(D(session.variance_value)))
        note = notify(
            db,
            notification_type=NotificationType.STOCK_VARIANCE,
            severity=(
                NotificationSeverity.CRITICAL
                if abs(D(session.variance_value)) > Decimal("1000")
                else NotificationSeverity.WARNING
            ),
            title_tr=f"Araç sayım farkı: {plate}",
            title_en=f"Van count variance: {plate}",
            body_tr=(
                f"{session.session_date} — {who}: {D(session.variance_qty)} adet fark, "
                f"{value} TL değerinde."
            ),
            body_en=(
                f"{session.session_date} — {who}: variance of {D(session.variance_qty)} units, "
                f"worth {value} TRY."
            ),
            role_code=ROLE_STOCK,
            entity_type="DaySession",
            entity_id=session.id,
            action_url=f"/field/day-sessions/{session.id}",
            dedupe_key=f"variance:{session.id}",
        )
        if note:
            out.append(note)
    return out


def _check_target_risk(db: Session, today: date) -> list[Notification]:
    """Flag targets whose achievement is materially behind the elapsed period."""
    targets = db.execute(
        select(Target).where(
            Target.period_start <= today,
            Target.period_end >= today,
            Target.target_value > 0,
        )
    ).scalars().all()

    people = {s.id: s.full_name for s in db.execute(select(Salesperson)).scalars()}
    sp_users = _salesperson_user_ids(db)

    out: list[Notification] = []
    for t in targets:
        span = max(1, (t.period_end - t.period_start).days + 1)
        elapsed = max(1, (today - t.period_start).days + 1)
        expected = pct(elapsed, span)
        achieved = pct(t.actual_value, t.target_value)
        if achieved >= expected - 15:
            continue
        subject = people.get(t.subject_id, f"{t.subject_type} #{t.subject_id}")
        note = notify(
            db,
            notification_type=NotificationType.TARGET_RISK,
            severity=(
                NotificationSeverity.CRITICAL if achieved < expected - 35
                else NotificationSeverity.WARNING
            ),
            title_tr=f"Hedef riski: {subject}",
            title_en=f"Target at risk: {subject}",
            body_tr=(
                f"{t.metric} hedefi %{achieved} gerçekleşti, beklenen %{expected}. "
                f"Kalan {max(0, (t.period_end - today).days)} gün."
            ),
            body_en=(
                f"{t.metric} target at {achieved}% versus {expected}% expected. "
                f"{max(0, (t.period_end - today).days)} days remaining."
            ),
            user_id=sp_users.get(int(t.subject_id or 0)),
            role_code=ROLE_FIELD,
            entity_type="Target",
            entity_id=t.id,
            action_url=f"/analytics/targets/{t.id}",
            dedupe_key=f"target_risk:{t.id}:{today.isoformat()}",
        )
        if note:
            out.append(note)
    return out


def _check_backups(db: Session, today: date) -> list[Notification]:
    from app.core.enums import BackupStatus

    out: list[Notification] = []
    latest_failed = db.execute(
        select(BackupRecord)
        .where(BackupRecord.status == BackupStatus.FAILED)
        .order_by(BackupRecord.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest_failed and latest_failed.created_at.date() >= today - timedelta(days=2):
        note = notify(
            db,
            notification_type=NotificationType.BACKUP_FAILED,
            severity=NotificationSeverity.CRITICAL,
            title_tr="Yedekleme başarısız",
            title_en="Backup failed",
            body_tr=f"{latest_failed.file_name}: {latest_failed.error_message or '-'}",
            body_en=f"{latest_failed.file_name}: {latest_failed.error_message or '-'}",
            role_code=ROLE_ADMIN,
            entity_type="BackupRecord",
            entity_id=latest_failed.id,
            action_url="/system/backup",
            dedupe_key=f"backup_failed:{latest_failed.id}",
        )
        if note:
            out.append(note)

    auto_enabled = bool(
        setting_service.get_typed(db, "backup", "auto_enabled", settings.backup_auto_enabled)
    )
    if auto_enabled:
        last_ok = db.execute(
            select(BackupRecord)
            .where(BackupRecord.status.in_([BackupStatus.COMPLETED, BackupStatus.VERIFIED]))
            .order_by(BackupRecord.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        stale_days = (today - last_ok.created_at.date()).days if last_ok else 9999
        if stale_days > 2:
            note = notify(
                db,
                notification_type=NotificationType.BACKUP_FAILED,
                severity=NotificationSeverity.WARNING,
                title_tr="Güncel yedek yok",
                title_en="No recent backup",
                body_tr=(
                    "Son başarılı yedek bulunamadı."
                    if not last_ok
                    else f"Son başarılı yedek {stale_days} gün önce alındı."
                ),
                body_en=(
                    "No successful backup found."
                    if not last_ok
                    else f"Last successful backup was {stale_days} days ago."
                ),
                role_code=ROLE_ADMIN,
                action_url="/system/backup",
                dedupe_key=f"backup_stale:{today.isoformat()}",
            )
            if note:
                out.append(note)
    return out


def _check_ai(db: Session, today: date) -> list[Notification]:
    out: list[Notification] = []

    for provider in db.execute(
        select(AIProviderConfig).where(AIProviderConfig.is_enabled.is_(True))
    ).scalars():
        if provider.is_healthy:
            continue
        note = notify(
            db,
            notification_type=NotificationType.AI_SERVICE_ERROR,
            severity=NotificationSeverity.ERROR,
            title_tr=f"AI servisi hatalı: {provider.display_name}",
            title_en=f"AI service unhealthy: {provider.display_name}",
            body_tr=f"Son hata: {provider.last_error or '-'} (hata oranı %{provider.error_rate}).",
            body_en=f"Last error: {provider.last_error or '-'} (error rate {provider.error_rate}%).",
            role_code=ROLE_AI,
            entity_type="AIProviderConfig",
            entity_id=provider.id,
            action_url="/ai/providers",
            dedupe_key=f"ai_unhealthy:{provider.provider}:{today.isoformat()}",
        )
        if note:
            out.append(note)

    since = utcnow() - timedelta(hours=24)
    rate_limited = int(
        db.execute(
            select(func.count(AIRequest.id)).where(
                AIRequest.status == AIRequestStatus.RATE_LIMITED,
                AIRequest.created_at >= since,
            )
        ).scalar_one()
        or 0
    )
    if rate_limited:
        note = notify(
            db,
            notification_type=NotificationType.API_QUOTA,
            severity=NotificationSeverity.WARNING,
            title_tr="AI kota limiti",
            title_en="AI quota limit",
            body_tr=f"Son 24 saatte {rate_limited} istek kota nedeniyle reddedildi.",
            body_en=f"{rate_limited} requests were rate-limited in the last 24 hours.",
            role_code=ROLE_AI,
            action_url="/ai/usage",
            dedupe_key=f"api_quota:{today.isoformat()}",
        )
        if note:
            out.append(note)

    budget = float(
        setting_service.get_typed(db, "ai", "monthly_budget_usd", settings.ai_monthly_budget_usd)
        or settings.ai_monthly_budget_usd
    )
    warn_pct = float(
        setting_service.get_typed(db, "ai", "budget_warn_percent", settings.ai_budget_warn_pct)
        or settings.ai_budget_warn_pct
    )
    if budget > 0:
        spent = D(
            db.execute(
                select(func.sum(AIUsageDaily.estimated_cost)).where(
                    AIUsageDaily.usage_date >= month_start(today)
                )
            ).scalar_one()
            or 0
        )
        usage = pct(spent, D(str(budget)))
        if usage >= warn_pct:
            note = notify(
                db,
                notification_type=NotificationType.HIGH_AI_COST,
                severity=(
                    NotificationSeverity.CRITICAL if usage >= 100 else NotificationSeverity.WARNING
                ),
                title_tr="AI maliyeti yüksek",
                title_en="High AI cost",
                body_tr=f"Bu ay {display_money(spent)} USD harcandı — bütçenin %{usage}'i.",
                body_en=f"{display_money(spent)} USD spent this month — {usage}% of budget.",
                role_code=ROLE_AI,
                action_url="/ai/usage",
                dedupe_key=f"ai_cost:{today.strftime('%Y-%m')}:{int(usage // 10)}",
            )
            if note:
                out.append(note)
    return out


def run_checks(db: Session, *, today: date | None = None) -> list[Notification]:
    """
    Evaluate every alert rule and persist the new notifications.

    One rule failing must not silence the others, so each is isolated: a broken
    check is logged and the sweep continues.
    """
    ref = today or date.today()
    created: list[Notification] = []
    rules = (
        ("low_stock", _check_low_stock),
        ("expiry", _check_expiry),
        ("overdue", _check_overdue_receivables),
        ("route_delay", _check_route_delays),
        ("stock_variance", _check_stock_variance),
        ("target_risk", _check_target_risk),
        ("backup", _check_backups),
        ("ai", _check_ai),
    )
    for name, rule in rules:
        try:
            created.extend(rule(db, ref))
        except Exception:
            db.rollback()
            log.exception("Notification rule '%s' failed", name)
    db.commit()
    if created:
        log.info("run_checks raised %d notification(s)", len(created))
    return created


def broadcast(
    db: Session,
    *,
    title_tr: str,
    title_en: str,
    body_tr: str | None = None,
    body_en: str | None = None,
    severity: str = NotificationSeverity.INFO,
    role_code: str | None = None,
    expires_at: datetime | None = None,
) -> Notification | None:
    """System announcement to a role (or everyone when *role_code* is None)."""
    return notify(
        db,
        notification_type=NotificationType.SYSTEM,
        severity=severity,
        title_tr=title_tr,
        title_en=title_en,
        body_tr=body_tr,
        body_en=body_en,
        role_code=role_code,
        expires_at=expires_at,
        commit=True,
    )


def as_dict(row: Notification, lang: str = "tr") -> dict[str, Any]:
    return {
        "id": row.id,
        "notification_type": row.notification_type,
        "severity": row.severity,
        "title": row.title_en if lang == "en" and row.title_en else row.title_tr,
        "title_tr": row.title_tr,
        "title_en": row.title_en,
        "body": (row.body_en if lang == "en" and row.body_en else row.body_tr),
        "entity_type": row.entity_type,
        "entity_id": row.entity_id,
        "action_url": row.action_url,
        "is_read": row.is_read,
        "is_dismissed": row.is_dismissed,
        "created_at": row.created_at,
        "read_at": row.read_at,
        "expires_at": row.expires_at,
    }


__all__ = [
    "as_dict",
    "broadcast",
    "dismiss",
    "list_for",
    "mark_all_read",
    "mark_read",
    "notify",
    "purge_expired",
    "run_checks",
    "unread_count",
]
