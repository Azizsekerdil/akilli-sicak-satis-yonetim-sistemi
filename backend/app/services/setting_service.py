"""
Runtime settings service.

Settings live in the ``settings`` table so an operator can change business
behaviour (VAT rate, expiry warning window, backup schedule…) without editing
``.env`` and restarting.  Credentials never live here: a row flagged
``is_secret`` only ever leaves this module masked.

Reads are cached per-process with a version counter that every write bumps —
settings are read on nearly every request but changed a few times a year.
"""

from __future__ import annotations

import threading
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import AuditAction
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging_config import get_logger
from app.core.security import mask_secret
from app.core.utils import D, loads
from app.models.base import utcnow
from app.models.system import Setting
from app.services import audit_service

log = get_logger("app.settings")

_CACHE: dict[tuple[str, str], str | None] = {}
_CACHE_LOADED = False
_LOCK = threading.Lock()

#: Human-readable, bilingual category headings for the settings screen.
CATEGORY_LABELS: dict[str, tuple[str, str]] = {
    "general": ("Genel", "General"),
    "sales": ("Satış", "Sales"),
    "stock": ("Stok", "Stock"),
    "route": ("Rota", "Route"),
    "ai": ("Yapay Zeka", "Artificial Intelligence"),
    "backup": ("Yedekleme", "Backup"),
    "notification": ("Bildirim", "Notifications"),
    "security": ("Güvenlik", "Security"),
}

_TRUE = {"1", "true", "yes", "on", "evet", "t"}


def category_label(category: str, lang: str = "tr") -> str:
    tr, en = CATEGORY_LABELS.get(category, (category.title(), category.title()))
    return en if lang == "en" else tr


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
def invalidate_cache() -> None:
    global _CACHE_LOADED
    with _LOCK:
        _CACHE.clear()
        _CACHE_LOADED = False


def _load_cache(db: Session) -> None:
    global _CACHE_LOADED
    with _LOCK:
        if _CACHE_LOADED:
            return
        for row in db.execute(select(Setting)).scalars():
            _CACHE[(row.category, row.key)] = row.value
        _CACHE_LOADED = True


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def get(db: Session, category: str, key: str, default: Any = None) -> Any:
    """Raw string value, or *default* when the setting is absent or empty."""
    _load_cache(db)
    value = _CACHE.get((category, key))
    return default if value in (None, "") else value


def _coerce(value: str | None, value_type: str, default: Any) -> Any:
    if value in (None, ""):
        return default
    text = str(value).strip()
    try:
        if value_type == "int":
            return int(float(text))
        if value_type == "float":
            return float(text)
        if value_type == "decimal":
            return D(text)
        if value_type == "bool":
            return text.lower() in _TRUE
        if value_type == "json":
            return loads(text, default)
    except (TypeError, ValueError):
        return default
    return text


def get_typed(db: Session, category: str, key: str, default: Any = None) -> Any:
    """
    Value converted using the row's declared ``value_type``.

    The declared type is trusted over the shape of *default* so a setting that
    is documented as an int stays an int even if a caller passes ``None``.
    """
    row = find(db, category, key)
    if row is None:
        return default
    return _coerce(row.value, row.value_type, default)


def find(db: Session, category: str, key: str) -> Setting | None:
    return db.execute(
        select(Setting).where(Setting.category == category, Setting.key == key)
    ).scalar_one_or_none()


def public_value(row: Setting) -> str | None:
    """The value as the API is allowed to show it (secrets masked)."""
    return mask_secret(row.value) if row.is_secret else row.value


def as_dict(row: Setting, lang: str = "tr") -> dict[str, Any]:
    return {
        "id": row.id,
        "category": row.category,
        "key": row.key,
        "value": public_value(row),
        "value_type": row.value_type,
        "default_value": row.default_value,
        "label": (row.label_en if lang == "en" else row.label_tr) or row.key,
        "label_tr": row.label_tr,
        "label_en": row.label_en,
        "description": (row.description_en if lang == "en" else row.description_tr),
        "is_secret": row.is_secret,
        "is_editable": row.is_editable,
        "requires_restart": row.requires_restart,
        "sort_order": row.sort_order,
    }


def all_grouped(db: Session, lang: str = "tr") -> list[dict[str, Any]]:
    """Every setting, grouped by category — the shape the settings screen wants."""
    rows = db.execute(
        select(Setting).order_by(Setting.category, Setting.sort_order, Setting.key)
    ).scalars().all()

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row.category, []).append(as_dict(row, lang))

    return [
        {
            "category": category,
            "label": category_label(category, lang),
            "label_tr": category_label(category, "tr"),
            "label_en": category_label(category, "en"),
            "items": items,
        }
        for category, items in sorted(grouped.items())
    ]


def export_all(db: Session, *, include_secrets: bool = False) -> dict[str, Any]:
    """Flat ``{"category.key": value}`` map — used by the backup bundle."""
    out: dict[str, Any] = {}
    for row in db.execute(select(Setting).order_by(Setting.category, Setting.key)).scalars():
        if row.is_secret and not include_secrets:
            continue
        out[f"{row.category}.{row.key}"] = row.value
    return out


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------
def _stringify(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    if isinstance(value, (dict, list)):
        from app.core.utils import dumps

        return dumps(value)
    return str(value)


def set(  # noqa: A001 - the domain verb really is "set"
    db: Session,
    category: str,
    key: str,
    value: Any,
    *,
    user: Any = None,
    create_missing: bool = False,
    value_type: str = "string",
    label_tr: str | None = None,
    label_en: str | None = None,
    is_secret: bool = False,
    commit: bool = True,
) -> Setting:
    """
    Update one setting, auditing the before/after values.

    Secret values are recorded in the audit log as ``***`` (the audit service
    scrubs them) so a change is provable without leaking the credential.
    """
    row = find(db, category, key)
    if row is None:
        if not create_missing:
            raise NotFoundError("setting.not_found", params={"category": category, "key": key})
        row = Setting(
            category=category,
            key=key,
            value_type=value_type,
            label_tr=label_tr or key,
            label_en=label_en or key,
            is_secret=is_secret,
        )
        db.add(row)
        db.flush()

    if not row.is_editable:
        raise ValidationError("setting.not_editable", params={"key": f"{category}.{key}"})

    new_value = _stringify(value)
    if row.value_type == "bool" and new_value is not None:
        new_value = "true" if new_value.strip().lower() in _TRUE else "false"
    if row.value_type in ("int", "float", "decimal") and new_value not in (None, ""):
        try:
            float(new_value)
        except ValueError as exc:
            raise ValidationError(
                "setting.invalid_number", params={"key": f"{category}.{key}"}
            ) from exc

    old_value = row.value
    if old_value == new_value:
        return row

    row.value = new_value
    row.updated_at = utcnow()
    if user is not None:
        row.updated_by_id = getattr(user, "id", None)
    db.flush()

    audit_service.record(
        db,
        AuditAction.SETTING_CHANGE,
        entity_type="Setting",
        entity_id=row.id,
        entity_label=f"{category}.{key}",
        user_id=getattr(user, "id", None),
        username=getattr(user, "username", None),
        role_code=(getattr(getattr(user, "role", None), "code", None)),
        summary=f"setting {category}.{key} changed",
        old_values={"value": "***" if row.is_secret else old_value},
        new_values={"value": "***" if row.is_secret else new_value},
    )
    if commit:
        db.commit()
    invalidate_cache()
    return row


def bulk_update(
    db: Session, updates: list[dict[str, Any]], *, user: Any = None
) -> list[Setting]:
    """
    Apply several setting changes in one transaction.

    All-or-nothing: a single invalid value rolls the whole batch back, so the
    settings screen can never leave half a configuration applied.
    """
    changed: list[Setting] = []
    for item in updates:
        category = str(item.get("category") or "").strip()
        key = str(item.get("key") or "").strip()
        if not category or not key:
            raise ValidationError("setting.key_required")
        changed.append(
            set(db, category, key, item.get("value"), user=user, commit=False)
        )
    db.commit()
    invalidate_cache()
    return changed


def import_settings(
    db: Session, payload: dict[str, Any], *, user: Any = None, create_missing: bool = False
) -> int:
    """Restore a ``{"category.key": value}`` map produced by :func:`export_all`."""
    applied = 0
    for dotted, value in (payload or {}).items():
        if "." not in str(dotted):
            continue
        category, key = str(dotted).split(".", 1)
        row = find(db, category, key)
        if row is None and not create_missing:
            continue
        try:
            set(db, category, key, value, user=user, create_missing=create_missing, commit=False)
            applied += 1
        except (ValidationError, NotFoundError):
            log.warning("Skipping unimportable setting %s", dotted)
    db.commit()
    invalidate_cache()
    return applied


__all__ = [
    "CATEGORY_LABELS",
    "all_grouped",
    "as_dict",
    "bulk_update",
    "category_label",
    "export_all",
    "find",
    "get",
    "get_typed",
    "import_settings",
    "invalidate_cache",
    "public_value",
    "set",
]
