"""
Audit logging.

Every entry is chained: ``checksum = sha256(previous_checksum + payload)``.
Because each row's checksum depends on the one before it, editing or deleting
any historical row breaks the chain and :func:`verify_chain` reports exactly
where.  The API exposes no update or delete path for these rows.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import AuditAction
from app.core.logging_config import get_logger, redact
from app.core.utils import dumps
from app.models.base import utcnow
from app.models.system import AuditLog

log = get_logger("app.audit")

#: Never persist these keys into before/after snapshots.
_SENSITIVE_KEYS = {
    "password", "password_hash", "new_password", "old_password",
    "api_key", "apikey", "secret", "token", "access_token", "refresh_token",
    "authorization", "private_key", "client_secret",
}


def _scrub(data: Any) -> Any:
    """Recursively drop credential-shaped values from a snapshot."""
    if isinstance(data, dict):
        return {
            k: ("***" if k.lower() in _SENSITIVE_KEYS else _scrub(v))
            for k, v in data.items()
            if not k.startswith("_")
        }
    if isinstance(data, (list, tuple)):
        return [_scrub(v) for v in data]
    if isinstance(data, str):
        return redact(data)
    return data


def _payload(entry: AuditLog) -> str:
    """
    Canonical, reproducible representation of an entry's content.

    Built only from **persisted** columns — no clock reads, no random values —
    so :func:`verify_chain` can recompute the hash later and detect a row whose
    fields were edited, not merely a broken link.
    """
    return dumps(
        {
            "a": entry.action,
            "et": entry.entity_type,
            "ei": entry.entity_id,
            "el": entry.entity_label,
            "u": entry.user_id,
            "un": entry.username,
            "s": entry.summary,
            "ov": entry.old_values,
            "nv": entry.new_values,
            "am": str(entry.amount) if entry.amount is not None else None,
            "ai": entry.is_ai_action,
            "t": entry.created_at.isoformat() if entry.created_at else None,
        }
    )


def _compute_checksum(previous: str | None, payload: str) -> str:
    return hashlib.sha256(f"{previous or ''}|{payload}".encode()).hexdigest()


def _last_checksum(db: Session) -> str | None:
    row = db.execute(
        select(AuditLog.checksum).order_by(AuditLog.id.desc()).limit(1)
    ).scalar_one_or_none()
    return row


def record(
    db: Session,
    action: str | AuditAction,
    *,
    entity_type: str | None = None,
    entity_id: int | None = None,
    entity_label: str | None = None,
    user_id: int | None = None,
    username: str | None = None,
    role_code: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    request_path: str | None = None,
    request_method: str | None = None,
    summary: str | None = None,
    old_values: Any = None,
    new_values: Any = None,
    amount: Decimal | None = None,
    is_ai_action: bool = False,
    ai_agent_kind: str | None = None,
    ai_request_id: str | None = None,
    commit: bool = False,
) -> AuditLog:
    """
    Append an audit entry.

    Does **not** commit by default — the caller's transaction owns it, so an
    action and its audit record either both land or neither does.
    """
    old_json = dumps(_scrub(old_values)) if old_values is not None else None
    new_json = dumps(_scrub(new_values)) if new_values is not None else None

    entry = AuditLog(
        action=str(action),
        entity_type=entity_type,
        entity_id=entity_id,
        entity_label=entity_label,
        user_id=user_id,
        username=username,
        role_code=role_code,
        ip_address=ip_address,
        user_agent=(user_agent or "")[:512] or None,
        request_path=(request_path or "")[:255] or None,
        request_method=request_method,
        summary=(summary or "")[:512] or None,
        old_values=old_json,
        new_values=new_json,
        amount=amount,
        is_ai_action=is_ai_action,
        ai_agent_kind=ai_agent_kind,
        ai_request_id=ai_request_id,
    )

    # created_at must exist before hashing — it is part of the signed payload.
    entry.created_at = utcnow()
    entry.previous_checksum = _last_checksum(db)
    entry.checksum = _compute_checksum(entry.previous_checksum, _payload(entry))

    db.add(entry)
    db.flush()

    log.info(
        "audit action=%s entity=%s#%s user=%s ai=%s summary=%s",
        entry.action, entry.entity_type, entry.entity_id,
        entry.username or entry.user_id, is_ai_action, entry.summary,
    )
    if commit:
        db.commit()
    return entry


def verify_chain(db: Session, *, limit: int | None = None) -> dict[str, Any]:
    """
    Walk the audit chain and report the first broken link, if any.

    Returns ``{"valid": bool, "checked": int, "broken_at": id|None, "reason": str|None}``.
    """
    stmt = select(AuditLog).order_by(AuditLog.id.asc())
    if limit:
        stmt = stmt.limit(limit)
    rows = db.execute(stmt).scalars().all()

    previous: str | None = None
    checked = 0
    for row in rows:
        checked += 1
        if not row.checksum:
            return _broken(checked, row.id, "missing_checksum")
        if row.previous_checksum != previous:
            return _broken(checked, row.id, "previous_checksum_mismatch")
        # Recompute from the stored content: catches an edited field even when
        # the attacker also rewrote the checksum column to keep the chain
        # superficially consistent.
        if _compute_checksum(previous, _payload(row)) != row.checksum:
            return _broken(checked, row.id, "content_checksum_mismatch")
        previous = row.checksum
    return {"valid": True, "checked": checked, "broken_at": None, "reason": None}


def _broken(checked: int, row_id: int, reason: str) -> dict[str, Any]:
    log.error("Audit chain broken at id=%s reason=%s", row_id, reason)
    return {"valid": False, "checked": checked, "broken_at": row_id, "reason": reason}
