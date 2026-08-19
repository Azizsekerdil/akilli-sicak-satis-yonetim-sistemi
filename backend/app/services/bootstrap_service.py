"""
First-run bootstrap: create tables, seed the permission catalogue, the 19
roles, default settings, AI provider rows and the initial administrator.

Idempotent — safe to run on every startup.
"""

from __future__ import annotations

import os
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import create_all, session_scope
from app.core.enums import AIProvider, UserStatus
from app.core.logging_config import get_logger
from app.core.permissions import (
    RESOURCES,
    ROLES,
    permission_code,
    role_permissions,
)
from app.core.security import hash_password
from app.core.utils import dumps
from app.models.ai import AIProviderConfig
from app.models.auth import Permission, Role, RolePermission, User
from app.models.base import utcnow

log = get_logger("app.bootstrap")

DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "admin"


# ---------------------------------------------------------------------------
# Permissions & roles
# ---------------------------------------------------------------------------
def seed_permissions(db: Session) -> int:
    existing = {p.code for p in db.execute(select(Permission)).scalars()}
    created = 0
    for res in RESOURCES:
        for action in res.actions:
            code = permission_code(res.key, action)
            if code in existing:
                continue
            db.add(
                Permission(
                    code=code,
                    module=res.module,
                    resource=res.key,
                    action=str(action),
                    name_tr=f"{res.name_tr} — {action}",
                    name_en=f"{res.name_en} — {action}",
                    is_sensitive=res.sensitive,
                )
            )
            created += 1
    db.flush()
    return created


def seed_roles(db: Session) -> int:
    perms_by_code = {p.code: p for p in db.execute(select(Permission)).scalars()}
    created = 0
    for rd in ROLES:
        role = db.execute(select(Role).where(Role.code == rd.code)).scalar_one_or_none()
        if role is None:
            role = Role(
                code=rd.code,
                name_tr=rd.name_tr,
                name_en=rd.name_en,
                data_scope=rd.scope,
                rank=rd.rank,
                is_system=True,
                is_active=True,
            )
            db.add(role)
            db.flush()
            created += 1
        else:
            role.name_tr, role.name_en = rd.name_tr, rd.name_en
            role.data_scope, role.rank = rd.scope, rd.rank

        wanted = role_permissions(rd.code)
        current = {
            rp.permission.code: rp
            for rp in db.execute(
                select(RolePermission).where(RolePermission.role_id == role.id)
            ).scalars()
            if rp.permission
        }
        for code in wanted - set(current):
            perm = perms_by_code.get(code)
            if perm:
                db.add(
                    RolePermission(role_id=role.id, permission_id=perm.id, data_scope=rd.scope)
                )
        for code in set(current) - wanted:
            db.delete(current[code])
    db.flush()
    return created


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
DEFAULT_SETTINGS: list[dict[str, Any]] = [
    # category, key, value, type, label_tr, label_en
    {"category": "general", "key": "company_name", "value": "Demo Gıda ve İçecek A.Ş.",
     "value_type": "string", "label_tr": "Şirket Adı", "label_en": "Company Name"},
    {"category": "general", "key": "default_language", "value": "tr",
     "value_type": "string", "label_tr": "Varsayılan Dil", "label_en": "Default Language"},
    {"category": "general", "key": "currency", "value": "TRY",
     "value_type": "string", "label_tr": "Para Birimi", "label_en": "Currency"},
    {"category": "general", "key": "timezone", "value": "Europe/Istanbul",
     "value_type": "string", "label_tr": "Saat Dilimi", "label_en": "Timezone"},

    {"category": "sales", "key": "default_vat_rate", "value": "20",
     "value_type": "float", "label_tr": "Varsayılan KDV (%)", "label_en": "Default VAT (%)"},
    {"category": "sales", "key": "allow_negative_stock_sale", "value": "false",
     "value_type": "bool", "label_tr": "Eksi Stokla Satış", "label_en": "Allow Negative-Stock Sale"},
    {"category": "sales", "key": "enforce_credit_limit", "value": "true",
     "value_type": "bool", "label_tr": "Kredi Limiti Zorunlu", "label_en": "Enforce Credit Limit"},
    {"category": "sales", "key": "max_field_discount_percent", "value": "15",
     "value_type": "float", "label_tr": "Sahada Maks. İskonto (%)", "label_en": "Max Field Discount (%)"},

    {"category": "stock", "key": "allocation_strategy", "value": "FEFO",
     "value_type": "string", "label_tr": "Stok Çıkış Stratejisi", "label_en": "Allocation Strategy"},
    {"category": "stock", "key": "expiry_warning_days", "value": "30",
     "value_type": "int", "label_tr": "SKT Uyarı Günü", "label_en": "Expiry Warning Days"},
    {"category": "stock", "key": "block_expired_sale", "value": "true",
     "value_type": "bool", "label_tr": "SKT Geçmiş Ürün Satışını Engelle",
     "label_en": "Block Sale of Expired Stock"},
    {"category": "stock", "key": "variance_tolerance_percent", "value": "1",
     "value_type": "float", "label_tr": "Sayım Fark Toleransı (%)", "label_en": "Count Variance Tolerance (%)"},

    {"category": "route", "key": "avg_speed_kmh", "value": "30",
     "value_type": "float", "label_tr": "Ortalama Hız (km/s)", "label_en": "Average Speed (km/h)"},
    {"category": "route", "key": "road_detour_factor", "value": "1.35",
     "value_type": "float", "label_tr": "Yol Sapma Katsayısı", "label_en": "Road Detour Factor"},
    {"category": "route", "key": "geofence_radius_m", "value": "150",
     "value_type": "int", "label_tr": "Ziyaret Yarıçapı (m)", "label_en": "Geofence Radius (m)"},
    {"category": "route", "key": "workday_minutes", "value": "540",
     "value_type": "int", "label_tr": "Günlük Çalışma (dk)", "label_en": "Workday Minutes"},

    {"category": "ai", "key": "failover_order", "value": "lmstudio,nvidia,claude",
     "value_type": "string", "label_tr": "AI Yedekleme Sırası", "label_en": "AI Failover Order"},
    {"category": "ai", "key": "monthly_budget_usd", "value": "25",
     "value_type": "float", "label_tr": "Aylık AI Bütçesi (USD)", "label_en": "Monthly AI Budget (USD)"},
    {"category": "ai", "key": "budget_warn_percent", "value": "80",
     "value_type": "int", "label_tr": "Bütçe Uyarı Eşiği (%)", "label_en": "Budget Warning Threshold (%)"},
    {"category": "ai", "key": "terminal_default_level", "value": "READ_ONLY",
     "value_type": "string", "label_tr": "AI Terminal Varsayılan Yetki",
     "label_en": "AI Terminal Default Permission"},

    {"category": "backup", "key": "auto_enabled", "value": "true",
     "value_type": "bool", "label_tr": "Otomatik Yedekleme", "label_en": "Automatic Backup"},
    {"category": "backup", "key": "schedule", "value": "daily",
     "value_type": "string", "label_tr": "Yedekleme Sıklığı", "label_en": "Backup Schedule"},
    {"category": "backup", "key": "retention_days", "value": "30",
     "value_type": "int", "label_tr": "Saklama Süresi (gün)", "label_en": "Retention (days)"},
]


def seed_settings(db: Session) -> int:
    from app.models.system import Setting

    existing = {
        (s.category, s.key) for s in db.execute(select(Setting)).scalars()
    }
    created = 0
    for row in DEFAULT_SETTINGS:
        if (row["category"], row["key"]) in existing:
            continue
        db.add(Setting(default_value=row["value"], **row))
        created += 1
    db.flush()
    return created


# ---------------------------------------------------------------------------
# AI providers
# ---------------------------------------------------------------------------
def seed_ai_providers(db: Session) -> int:
    """
    Register the three providers.

    Only a *reference* to the credential is stored (``api_key_ref``); the value
    itself stays in the environment / .env.
    """
    rows = [
        {
            "provider": AIProvider.LMSTUDIO,
            "display_name": "LM Studio (Yerel / Local)",
            "base_url": settings.lmstudio_base_url,
            "default_model": settings.lmstudio_model,
            "api_key_ref": None,
            "has_api_key": True,  # local server needs none
            "timeout_seconds": settings.lmstudio_timeout,
            "max_tokens": settings.lmstudio_max_tokens,
            "temperature": settings.lmstudio_temperature,
            "supports_vision": True,
            "supports_embeddings": True,
            "failover_priority": 10,
            "is_enabled": settings.lmstudio_enabled,
            "input_cost_per_1k": 0,
            "output_cost_per_1k": 0,
            "task_model_map": dumps(
                {
                    "GENERAL": settings.lmstudio_model,
                    "ANALYSIS": settings.lmstudio_model,
                    "REPORTING": settings.lmstudio_model,
                    "SQL": settings.lmstudio_model,
                    "VISION": "qwen/qwen3-vl-8b",
                    "MATH": "qwen2.5-math-7b-instruct",
                    "EMBEDDING": settings.lmstudio_embedding_model,
                }
            ),
        },
        {
            "provider": AIProvider.NVIDIA,
            "display_name": "NVIDIA NIM",
            "base_url": settings.nvidia_base_url,
            "default_model": settings.nvidia_model,
            "api_key_ref": "VS_NVIDIA_API_KEY",
            "has_api_key": bool(settings.nvidia_api_key or os.getenv("NVIDIA_API_KEY")),
            "timeout_seconds": settings.nvidia_timeout,
            "max_tokens": settings.nvidia_max_tokens,
            "temperature": settings.nvidia_temperature,
            "supports_vision": True,
            "supports_embeddings": True,
            "failover_priority": 20,
            "is_enabled": settings.nvidia_enabled,
            "input_cost_per_1k": 0.0002,
            "output_cost_per_1k": 0.0006,
            "task_model_map": dumps(
                {
                    "GENERAL": "meta/llama-3.3-70b-instruct",
                    "ANALYSIS": "meta/llama-3.3-70b-instruct",
                    "REPORTING": "meta/llama-3.3-70b-instruct",
                    "SQL": "meta/llama-3.3-70b-instruct",
                    "CODING": "mistralai/codestral-22b-instruct-v0.1",
                    "VISION": "meta/llama-3.2-90b-vision-instruct",
                    "LONG_CONTEXT": "meta/llama-3.1-70b-instruct",
                    "EMBEDDING": "baai/bge-m3",
                }
            ),
        },
        {
            "provider": AIProvider.CLAUDE,
            "display_name": "Anthropic Claude",
            "base_url": settings.claude_base_url,
            "default_model": settings.claude_model,
            "api_key_ref": "VS_CLAUDE_API_KEY",
            "has_api_key": bool(settings.claude_api_key),
            "timeout_seconds": settings.claude_timeout,
            "max_tokens": settings.claude_max_tokens,
            "temperature": settings.claude_temperature,
            "supports_vision": True,
            "supports_embeddings": False,
            "failover_priority": 30,
            "is_enabled": settings.claude_enabled,
            "input_cost_per_1k": 0.003,
            "output_cost_per_1k": 0.015,
            "task_model_map": dumps(
                {
                    "GENERAL": settings.claude_model,
                    "ANALYSIS": settings.claude_model,
                    "REPORTING": settings.claude_model,
                    "CODING": settings.claude_model,
                    "LONG_CONTEXT": settings.claude_model,
                    "SQL": settings.claude_model,
                }
            ),
        },
    ]

    created = 0
    for row in rows:
        existing = db.execute(
            select(AIProviderConfig).where(AIProviderConfig.provider == row["provider"])
        ).scalar_one_or_none()
        if existing is None:
            db.add(AIProviderConfig(**row))
            created += 1
        else:
            # Keep endpoint/capability metadata fresh without clobbering
            # anything the operator has customised in the UI.
            existing.base_url = row["base_url"]
            existing.has_api_key = row["has_api_key"]
            if not existing.task_model_map:
                existing.task_model_map = row["task_model_map"]
    db.flush()
    return created


# ---------------------------------------------------------------------------
# Training centre
# ---------------------------------------------------------------------------
def seed_training(db: Session) -> int:
    """
    Create (or refresh) the 14 training lessons.

    Idempotent: existing lessons keep their id — and therefore every user's
    progress — while their text is brought up to date.
    """
    from app.services import training_service

    try:
        return training_service.seed_lessons(db)
    except Exception:
        # A documentation feature must never stop the application booting.
        log.exception("Training lesson seeding failed")
        return 0


# ---------------------------------------------------------------------------
# Admin user
# ---------------------------------------------------------------------------
def ensure_admin(db: Session) -> tuple[User, str | None]:
    """
    Ensure an administrator exists.

    The first-run credential contract, in full:

    * A new installation uses the one-time ``admin`` bootstrap password. It is
      never written to the log, the audit trail, the database in clear, a
      backup or a screenshot — only the bcrypt hash is stored.
    * The account is flagged ``must_change_password``, which
      :func:`app.core.deps.get_current_user` enforces: until the password is
      changed, every route outside the password-change flow answers 403.
    * The account is flagged ``is_bootstrap_credential``, which
      :func:`app.services.auth_service.authenticate` enforces: until the
      password is changed, sign-in is refused from anything but the local
      device, even with the correct password.
    * Both flags clear on the first successful password change and nothing
      sets them again — an administrative reset forces another change but does
      not restore first-run status, and re-running the bootstrap on an
      existing installation returns early without touching the account.
    """
    from app.core.enums import RoleCode

    user = db.execute(
        select(User).where(User.username == DEFAULT_ADMIN_USERNAME)
    ).scalar_one_or_none()
    if user is not None:
        # Idempotent and non-destructive: an existing administrator keeps its
        # password and its flags.  This is the branch that stops the bootstrap
        # from ever re-arming a first-run credential on a live system.
        return user, None

    role = db.execute(
        select(Role).where(Role.code == RoleCode.SYSTEM_ADMIN)
    ).scalar_one_or_none()
    if role is None:
        raise RuntimeError("SYSTEM_ADMIN role missing — seed roles first")

    password = DEFAULT_ADMIN_PASSWORD
    user = User(
        username=DEFAULT_ADMIN_USERNAME,
        password_hash=hash_password(password),
        full_name="Sistem Yöneticisi",
        email=None,
        role_id=role.id,
        status=UserStatus.ACTIVE,
        language=settings.default_language,
        must_change_password=True,
        is_bootstrap_credential=True,
        password_changed_at=utcnow(),
    )
    db.add(user)
    db.flush()
    log.info("Default administrator created (username=%s)", DEFAULT_ADMIN_USERNAME)
    return user, password


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------
def ensure_baseline() -> dict[str, Any]:
    """Create schema + seed reference data.  Called on every app startup."""
    create_all()
    result: dict[str, Any] = {}
    with session_scope() as db:
        result["permissions_created"] = seed_permissions(db)
        result["roles_created"] = seed_roles(db)
        result["settings_created"] = seed_settings(db)
        result["ai_providers_created"] = seed_ai_providers(db)
        result["lessons_created"] = seed_training(db)
        _, generated = ensure_admin(db)
        result["admin_password_generated"] = generated is not None
        if generated:
            result["_admin_password"] = generated  # consumed by the installer only
            # Written to stdout with print(), never through the logger, so the
            # one-time password cannot end up in logs/application.log.
            print(
                "\n"
                "  ==========================================================\n"
                "   ILK YONETICI HESABI / INITIAL ADMINISTRATOR ACCOUNT\n"
                "  ----------------------------------------------------------\n"
                f"   Kullanici / Username : {DEFAULT_ADMIN_USERNAME}\n"
                f"   Sifre     / Password : {generated}\n"
                "  ----------------------------------------------------------\n"
                "   Bu sifre yalnizca bir kez gosterilir ve DEGISTIRILENE\n"
                "   KADAR: (1) panele, musteri/personel/finans verisine,\n"
                "   AI ayarlarina, disa aktarima ve yedege erisilemez;\n"
                "   (2) giris YALNIZCA bu cihazdan (localhost) yapilabilir.\n"
                "  ----------------------------------------------------------\n"
                "   This password is shown once. UNTIL IT IS CHANGED:\n"
                "   (1) the dashboard, customer/staff/financial records, AI\n"
                "       settings, export and backup are all unreachable;\n"
                "   (2) sign-in is accepted ONLY from this device (localhost).\n"
                "  ==========================================================\n",
                flush=True,
            )
    if any(v for k, v in result.items() if k.endswith("_created")):
        log.info(
            "Baseline seeded: %s",
            {k: v for k, v in result.items() if not k.startswith("_")},
        )
    return result


def reset_and_seed() -> dict[str, Any]:
    """Drop everything and re-seed.  Development / test use only."""
    from app.core.db import drop_all

    if settings.env == "production":
        raise RuntimeError("reset_and_seed is not allowed in production")
    drop_all()
    return ensure_baseline()
