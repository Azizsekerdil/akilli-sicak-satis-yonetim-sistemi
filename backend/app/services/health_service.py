"""
System health checks.

Each component reports a state, a human-readable message and a latency, and
the result is persisted (one row per component) so the health screen has
history even when a probe currently times out.

A component that is *not configured* reports ``UNKNOWN``, never ``ERROR``:
an operator who deliberately runs without Redis should not see a red light
every morning.  Only something configured-but-broken is an error.
"""

from __future__ import annotations

import shutil
import time
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.config import PROJECT_ROOT, settings
from app.core.enums import BackupStatus, HealthState
from app.core.logging_config import get_logger
from app.core.utils import dumps, loads
from app.models.ai import AIProviderConfig
from app.models.base import utcnow
from app.models.system import BackupRecord, HealthCheckResult
from app.services import setting_service

log = get_logger("app.health")

#: Probe timeout for remote AI providers — the health screen must stay snappy.
PROBE_TIMEOUT = 3.0

COMPONENTS: tuple[str, ...] = (
    "backend",
    "database",
    "redis",
    "lmstudio",
    "nvidia",
    "claude",
    "disk",
    "backup",
    "queue",
)

COMPONENT_LABELS: dict[str, tuple[str, str]] = {
    "backend": ("Uygulama Sunucusu", "Backend"),
    "database": ("Veritabanı", "Database"),
    "redis": ("Önbellek (Redis)", "Cache (Redis)"),
    "lmstudio": ("LM Studio (Yerel AI)", "LM Studio (Local AI)"),
    "nvidia": ("NVIDIA NIM", "NVIDIA NIM"),
    "claude": ("Anthropic Claude", "Anthropic Claude"),
    "disk": ("Disk Alanı", "Disk Space"),
    "backup": ("Yedekleme", "Backup"),
    "queue": ("Görev Kuyruğu", "Task Queue"),
}

_STARTED_AT = utcnow()


def _result(
    component: str,
    state: str,
    message: str,
    *,
    latency_ms: int | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tr, en = COMPONENT_LABELS.get(component, (component, component))
    return {
        "component": component,
        "label_tr": tr,
        "label_en": en,
        "state": str(state),
        "message": message,
        "latency_ms": latency_ms,
        "details": details or {},
    }


# ===========================================================================
# Individual probes
# ===========================================================================
def check_backend() -> dict[str, Any]:
    uptime = (utcnow() - _STARTED_AT).total_seconds()
    return _result(
        "backend",
        HealthState.OK,
        f"{settings.app_name} v{settings.app_version} ({settings.env})",
        latency_ms=0,
        details={
            "version": settings.app_version,
            "environment": settings.env,
            "uptime_seconds": int(uptime),
            "debug": settings.debug,
        },
    )


def check_database(db: Session) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        db.execute(text("SELECT 1")).scalar_one()
        latency = int((time.perf_counter() - started) * 1000)
    except Exception as exc:
        return _result("database", HealthState.ERROR, f"{type(exc).__name__}: {exc}"[:400])

    details: dict[str, Any] = {"engine": "sqlite" if settings.is_sqlite else "postgresql"}
    if settings.is_sqlite and settings.sqlite_file and settings.sqlite_file.is_file():
        details["file"] = str(settings.sqlite_file)
        details["size_bytes"] = settings.sqlite_file.stat().st_size
        try:
            details["journal_mode"] = db.execute(text("PRAGMA journal_mode")).scalar_one()
        except Exception:  # pragma: no cover - PRAGMA support is sqlite-only
            pass

    state = HealthState.OK if latency < 500 else HealthState.WARNING
    return _result(
        "database",
        state,
        f"{details['engine']} — {latency} ms",
        latency_ms=latency,
        details=details,
    )


def check_redis() -> dict[str, Any]:
    if not settings.redis_url:
        return _result(
            "redis",
            HealthState.UNKNOWN,
            "not_configured",
            details={"configured": False},
        )
    try:
        import redis  # type: ignore[import-not-found]
    except ImportError:
        return _result(
            "redis",
            HealthState.WARNING,
            "redis_client_not_installed",
            details={"configured": True},
        )
    started = time.perf_counter()
    try:
        client = redis.Redis.from_url(settings.redis_url, socket_timeout=PROBE_TIMEOUT)
        client.ping()
        latency = int((time.perf_counter() - started) * 1000)
        return _result("redis", HealthState.OK, f"pong — {latency} ms", latency_ms=latency)
    except Exception as exc:
        return _result("redis", HealthState.ERROR, f"{type(exc).__name__}: {exc}"[:400])


def _probe_http(url: str, *, headers: dict[str, str] | None = None) -> tuple[bool, int, str]:
    """GET *url* with a short timeout; returns ``(ok, latency_ms, message)``."""
    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx is a hard dependency
        return False, 0, "httpx_not_installed"

    started = time.perf_counter()
    try:
        response = httpx.get(url, headers=headers or {}, timeout=PROBE_TIMEOUT)
        latency = int((time.perf_counter() - started) * 1000)
        # 401/403 still proves the endpoint is alive; it is a credential issue.
        if response.status_code < 400:
            return True, latency, f"HTTP {response.status_code}"
        return False, latency, f"HTTP {response.status_code}"
    except Exception as exc:
        latency = int((time.perf_counter() - started) * 1000)
        return False, latency, f"{type(exc).__name__}: {exc}"[:200]


def check_lmstudio() -> dict[str, Any]:
    if not settings.lmstudio_enabled:
        return _result("lmstudio", HealthState.UNKNOWN, "disabled", details={"enabled": False})
    url = settings.lmstudio_base_url.rstrip("/") + "/models"
    ok, latency, message = _probe_http(url)
    return _result(
        "lmstudio",
        HealthState.OK if ok else HealthState.WARNING,
        message,
        latency_ms=latency,
        details={"base_url": settings.lmstudio_base_url, "model": settings.lmstudio_model},
    )


def check_nvidia() -> dict[str, Any]:
    if not settings.nvidia_enabled:
        return _result("nvidia", HealthState.UNKNOWN, "disabled", details={"enabled": False})
    if not settings.nvidia_api_key:
        return _result(
            "nvidia", HealthState.UNKNOWN, "no_api_key", details={"has_api_key": False}
        )
    url = settings.nvidia_base_url.rstrip("/") + "/models"
    ok, latency, message = _probe_http(
        url, headers={"Authorization": f"Bearer {settings.nvidia_api_key}"}
    )
    return _result(
        "nvidia",
        HealthState.OK if ok else HealthState.ERROR,
        message,
        latency_ms=latency,
        details={"base_url": settings.nvidia_base_url, "model": settings.nvidia_model},
    )


def check_claude() -> dict[str, Any]:
    if not settings.claude_enabled:
        return _result("claude", HealthState.UNKNOWN, "disabled", details={"enabled": False})
    if not settings.claude_api_key:
        return _result(
            "claude", HealthState.UNKNOWN, "no_api_key", details={"has_api_key": False}
        )
    url = settings.claude_base_url.rstrip("/") + "/models"
    ok, latency, message = _probe_http(
        url,
        headers={
            "x-api-key": settings.claude_api_key,
            "anthropic-version": "2023-06-01",
        },
    )
    return _result(
        "claude",
        HealthState.OK if ok else HealthState.ERROR,
        message,
        latency_ms=latency,
        details={"base_url": settings.claude_base_url, "model": settings.claude_model},
    )


def check_disk() -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(str(PROJECT_ROOT))
    except OSError as exc:
        return _result("disk", HealthState.UNKNOWN, str(exc)[:200])

    free_gb = usage.free / 1024**3
    total_gb = usage.total / 1024**3
    used_percent = round((usage.used / usage.total) * 100, 1) if usage.total else 0.0
    state = (
        HealthState.ERROR if free_gb < 1
        else HealthState.WARNING if free_gb < 5
        else HealthState.OK
    )
    return _result(
        "disk",
        state,
        f"{free_gb:.1f} GB free / {total_gb:.1f} GB",
        details={
            "free_gb": round(free_gb, 2),
            "total_gb": round(total_gb, 2),
            "used_percent": used_percent,
            "path": str(PROJECT_ROOT),
        },
    )


def check_backup(db: Session) -> dict[str, Any]:
    newest = db.execute(
        select(BackupRecord)
        .where(
            BackupRecord.status.in_(
                [BackupStatus.COMPLETED, BackupStatus.VERIFIED, BackupStatus.RESTORED]
            )
        )
        .order_by(BackupRecord.id.desc())
        .limit(1)
    ).scalar_one_or_none()

    auto_enabled = bool(
        setting_service.get_typed(db, "backup", "auto_enabled", settings.backup_auto_enabled)
    )
    if newest is None:
        return _result(
            "backup",
            HealthState.WARNING if auto_enabled else HealthState.UNKNOWN,
            "no_backup_yet",
            details={"auto_enabled": auto_enabled, "count": 0},
        )

    age_days = (date.today() - newest.created_at.date()).days
    verified = newest.status == BackupStatus.VERIFIED
    state = (
        HealthState.ERROR if age_days > 7
        else HealthState.WARNING if age_days > 2 or not verified
        else HealthState.OK
    )
    total = int(db.execute(select(func.count(BackupRecord.id))).scalar_one() or 0)
    return _result(
        "backup",
        state,
        f"{newest.file_name} — {age_days} day(s) old"
        + ("" if verified else ", not verified"),
        details={
            "auto_enabled": auto_enabled,
            "count": total,
            "newest_file": newest.file_name,
            "newest_age_days": age_days,
            "verified": verified,
            "size_bytes": newest.size_bytes,
        },
    )


def check_queue(db: Session) -> dict[str, Any]:
    """
    Background work status.

    There is no external broker in the default single-node deployment: work is
    run in-process, so "queue" reports whether the scheduled jobs are keeping
    up rather than whether a broker is alive.
    """
    if not settings.redis_url:
        pending = db.execute(
            select(func.count(AIProviderConfig.id)).where(
                AIProviderConfig.is_enabled.is_(True), AIProviderConfig.is_healthy.is_(False)
            )
        ).scalar_one()
        return _result(
            "queue",
            HealthState.UNKNOWN,
            "in_process_scheduler",
            details={"broker": None, "unhealthy_providers": int(pending or 0)},
        )

    try:
        import redis  # type: ignore[import-not-found]

        client = redis.Redis.from_url(settings.redis_url, socket_timeout=PROBE_TIMEOUT)
        length = int(client.llen("vansales:jobs") or 0)
        state = HealthState.WARNING if length > 1000 else HealthState.OK
        return _result(
            "queue", state, f"{length} job(s) queued", details={"broker": "redis", "depth": length}
        )
    except Exception as exc:
        return _result("queue", HealthState.ERROR, f"{type(exc).__name__}: {exc}"[:400])


# ===========================================================================
# Aggregate
# ===========================================================================
def check_all(db: Session, *, persist: bool = True) -> list[dict[str, Any]]:
    """Run every probe, optionally upserting the results into the database."""
    results: list[dict[str, Any]] = []
    probes: tuple[tuple[str, Any], ...] = (
        ("backend", lambda: check_backend()),
        ("database", lambda: check_database(db)),
        ("redis", lambda: check_redis()),
        ("lmstudio", lambda: check_lmstudio()),
        ("nvidia", lambda: check_nvidia()),
        ("claude", lambda: check_claude()),
        ("disk", lambda: check_disk()),
        ("backup", lambda: check_backup(db)),
        ("queue", lambda: check_queue(db)),
    )
    for name, probe in probes:
        try:
            results.append(probe())
        except Exception as exc:
            log.exception("Health probe '%s' crashed", name)
            results.append(_result(name, HealthState.ERROR, f"{type(exc).__name__}: {exc}"[:400]))

    if persist:
        _persist(db, results)
    return results


def _persist(db: Session, results: list[dict[str, Any]]) -> None:
    """Upsert one row per component — the screen shows the latest state only."""
    existing = {
        row.component: row for row in db.execute(select(HealthCheckResult)).scalars()
    }
    now = utcnow()
    for item in results:
        row = existing.get(item["component"])
        if row is None:
            row = HealthCheckResult(component=item["component"])
            db.add(row)
        row.state = item["state"]
        row.message = (item.get("message") or "")[:512]
        row.latency_ms = item.get("latency_ms")
        row.details = dumps(item.get("details") or {})
        row.checked_at = now
        row.checked_on = now.date()
    db.commit()


def overall_state(results: list[dict[str, Any]]) -> str:
    """Worst state across components — UNKNOWN never degrades the overall light."""
    states = {str(r["state"]) for r in results}
    if str(HealthState.ERROR) in states:
        return str(HealthState.ERROR)
    if str(HealthState.WARNING) in states:
        return str(HealthState.WARNING)
    if str(HealthState.OK) in states:
        return str(HealthState.OK)
    return str(HealthState.UNKNOWN)


def last_results(db: Session) -> list[dict[str, Any]]:
    """Persisted results without re-probing — cheap enough for a dashboard poll."""
    rows = db.execute(
        select(HealthCheckResult).order_by(HealthCheckResult.component)
    ).scalars().all()
    out: list[dict[str, Any]] = []
    for row in rows:
        tr, en = COMPONENT_LABELS.get(row.component, (row.component, row.component))
        out.append(
            {
                "component": row.component,
                "label_tr": tr,
                "label_en": en,
                "state": row.state,
                "message": row.message or "",
                "latency_ms": row.latency_ms,
                "details": loads(row.details, {}) or {},
                "checked_at": row.checked_at,
            }
        )
    return out


def summary(db: Session, *, refresh: bool = True) -> dict[str, Any]:
    """``{state, checked_at, components:[...]}`` — what ``GET /system/health`` returns."""
    results = check_all(db) if refresh else last_results(db)
    return {
        "state": overall_state(results),
        "checked_at": utcnow(),
        "app_version": settings.app_version,
        "environment": settings.env,
        "components": results,
    }


def stale_components(db: Session, *, older_than_minutes: int = 30) -> list[str]:
    """Components whose last probe is older than the freshness window."""
    cutoff = utcnow() - timedelta(minutes=older_than_minutes)
    return [
        row.component
        for row in db.execute(select(HealthCheckResult)).scalars()
        if row.checked_at < cutoff
    ]


__all__ = [
    "COMPONENTS",
    "COMPONENT_LABELS",
    "check_all",
    "check_backend",
    "check_backup",
    "check_claude",
    "check_database",
    "check_disk",
    "check_lmstudio",
    "check_nvidia",
    "check_queue",
    "check_redis",
    "last_results",
    "overall_state",
    "stale_components",
    "summary",
]
