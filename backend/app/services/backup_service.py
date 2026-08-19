"""
Backup, verification and restore.

Design decisions that matter
----------------------------
*   **SQLite is copied with the online backup API**, never with
    ``shutil.copy``.  With WAL journalling a plain file copy can capture a
    database whose committed transactions still live in the ``-wal`` sidecar,
    producing a backup that restores to a silently older state.
    ``sqlite3.Connection.backup()`` takes a transactionally consistent snapshot
    while the application keeps serving requests.
*   **PostgreSQL is dumped with ``pg_dump``** when the binary is available; we
    refuse loudly rather than writing something unrestorable.
*   **Every artefact is checksummed** (SHA-256) at creation and re-checksummed
    on verification, so bit-rot or a truncated copy is detected before someone
    relies on it in an emergency.
*   **Restore always takes a safety backup first** and always verifies the
    archive before touching the live database.  A restore that goes wrong must
    still leave a way back.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.config import PROJECT_ROOT, settings
from app.core.db import Base, engine
from app.core.enums import AuditAction, BackupStatus, BackupType, NotificationSeverity, NotificationType
from app.core.exceptions import BackupError, NotFoundError, RestoreError, ValidationError
from app.core.logging_config import get_logger
from app.core.utils import dumps, loads
from app.models.base import utcnow
from app.models.system import BackupRecord
from app.services import audit_service, notification_service, setting_service

log = get_logger("app.backup")

#: Name the database artefact always carries inside the archive.
DB_MEMBER_SQLITE = "database/van_sales.db"
DB_MEMBER_PG = "database/van_sales.sql"
MANIFEST_MEMBER = "manifest.json"
SETTINGS_MEMBER = "settings.json"

_CHUNK = 1024 * 1024


# ===========================================================================
# Helpers
# ===========================================================================
def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _backup_dir() -> Path:
    path = settings.backup_path
    path.mkdir(parents=True, exist_ok=True)
    return path


def _database_engine_name() -> str:
    return "sqlite" if settings.is_sqlite else "postgresql"


def _table_stats(db: Session) -> tuple[int, int]:
    """(table count, total row count) — cheap integrity signal stored on the record."""
    tables = list(Base.metadata.sorted_tables)
    total = 0
    for table in tables:
        try:
            total += int(db.execute(select(func.count()).select_from(table)).scalar_one() or 0)
        except Exception:  # pragma: no cover - a table may not exist yet
            continue
    return len(tables), total


def _sqlite_online_copy(source: Path, target: Path) -> None:
    """Transactionally consistent copy of a live SQLite database."""
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(source))
    try:
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
            dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            dst.close()
    finally:
        src.close()


def _pg_conn_parts() -> dict[str, str]:
    parsed = urlparse(settings.database_url.replace("postgresql+psycopg2", "postgresql"))
    return {
        "host": parsed.hostname or "localhost",
        "port": str(parsed.port or 5432),
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "dbname": (parsed.path or "/").lstrip("/"),
    }


def _pg_dump(target: Path) -> None:
    binary = shutil.which("pg_dump")
    if not binary:
        raise BackupError("backup.pg_dump_missing")
    parts = _pg_conn_parts()
    env = dict(os.environ)
    if parts["password"]:
        env["PGPASSWORD"] = parts["password"]
    target.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(  # noqa: S603 - fixed binary, arguments built from settings
        [
            binary,
            "--host", parts["host"],
            "--port", parts["port"],
            "--username", parts["user"],
            "--no-password",
            "--format", "plain",
            "--clean",
            "--if-exists",
            "--file", str(target),
            parts["dbname"],
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=3600,
    )
    if result.returncode != 0:
        raise BackupError("backup.pg_dump_failed", detail=(result.stderr or "")[:500])


def _pg_restore(source: Path) -> None:
    binary = shutil.which("psql")
    if not binary:
        raise RestoreError("backup.psql_missing")
    parts = _pg_conn_parts()
    env = dict(os.environ)
    if parts["password"]:
        env["PGPASSWORD"] = parts["password"]
    result = subprocess.run(  # noqa: S603 - fixed binary, arguments built from settings
        [
            binary,
            "--host", parts["host"],
            "--port", parts["port"],
            "--username", parts["user"],
            "--dbname", parts["dbname"],
            "--file", str(source),
            "--set", "ON_ERROR_STOP=on",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=7200,
    )
    if result.returncode != 0:
        raise RestoreError("backup.restore_failed", detail=(result.stderr or "")[:500])


def _add_tree(archive: zipfile.ZipFile, root: Path, prefix: str, *, max_bytes: int) -> int:
    """Add a directory tree to the archive, stopping at *max_bytes*."""
    written = 0
    if not root.is_dir():
        return 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        size = path.stat().st_size
        if written + size > max_bytes:
            log.warning("Upload bundle truncated at %d bytes (limit reached)", written)
            break
        archive.write(path, f"{prefix}/{path.relative_to(root).as_posix()}")
        written += size
    return written


# ===========================================================================
# Create
# ===========================================================================
def create_backup(
    db: Session,
    *,
    backup_type: str = BackupType.FULL,
    trigger: str = "MANUAL",
    include_files: bool = False,
    user_id: int | None = None,
    username: str | None = None,
    notes: str | None = None,
    max_upload_bytes: int = 512 * 1024 * 1024,
) -> BackupRecord:
    """
    Take a backup and register it.

    The record is written *before* the work starts so a crash mid-backup leaves
    a FAILED row rather than no evidence at all.
    """
    started = utcnow()
    stamp = started.strftime("%Y%m%d_%H%M%S")
    file_name = f"vansales_{str(backup_type).lower()}_{stamp}.zip"
    target = _backup_dir() / file_name

    record = BackupRecord(
        backup_type=str(backup_type),
        status=BackupStatus.RUNNING,
        trigger=trigger,
        file_path=str(target),
        file_name=file_name,
        database_engine=_database_engine_name(),
        app_version=settings.app_version,
        includes_files=include_files,
        started_at=started,
        created_by_id=user_id,
        notes=notes,
    )
    db.add(record)
    db.commit()

    monotonic = time.monotonic()
    work_dir = Path(tempfile.mkdtemp(prefix="vs_backup_"))
    try:
        # --- 1. Database artefact ----------------------------------------
        if settings.is_sqlite:
            source = settings.sqlite_file
            if source is None or not source.is_file():
                raise BackupError("backup.database_missing")
            db_artefact = work_dir / "database.db"
            _sqlite_online_copy(source, db_artefact)
            member_name = DB_MEMBER_SQLITE
        else:
            db_artefact = work_dir / "database.sql"
            _pg_dump(db_artefact)
            member_name = DB_MEMBER_PG

        table_count, row_count = _table_stats(db)

        # --- 2. Bundle ----------------------------------------------------
        manifest = {
            "app": settings.app_name,
            "app_version": settings.app_version,
            "backup_type": str(backup_type),
            "trigger": trigger,
            "database_engine": _database_engine_name(),
            "created_at": started.isoformat(),
            "table_count": table_count,
            "row_count": row_count,
            "includes_files": include_files,
            "database_member": member_name,
            "schema_tables": sorted(t.name for t in Base.metadata.sorted_tables),
        }

        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            archive.write(db_artefact, member_name)
            archive.writestr(MANIFEST_MEMBER, dumps(manifest, indent=2))
            archive.writestr(
                SETTINGS_MEMBER, dumps(setting_service.export_all(db), indent=2)
            )
            env_example = PROJECT_ROOT / ".env.example"
            if env_example.is_file():
                archive.write(env_example, "config/.env.example")
            if include_files:
                _add_tree(
                    archive,
                    PROJECT_ROOT / "data" / "uploads",
                    "uploads",
                    max_bytes=max_upload_bytes,
                )

        checksum = _sha256(target)
        record.status = BackupStatus.COMPLETED
        record.size_bytes = target.stat().st_size
        record.checksum_sha256 = checksum
        record.table_count = table_count
        record.row_count = row_count
        record.schema_version = str(len(Base.metadata.sorted_tables))
        record.completed_at = utcnow()
        record.duration_seconds = round(time.monotonic() - monotonic, 3)

        audit_service.record(
            db,
            AuditAction.BACKUP,
            entity_type="BackupRecord",
            entity_id=record.id,
            entity_label=file_name,
            user_id=user_id,
            username=username,
            summary=f"backup created ({record.size_bytes} bytes, {row_count} rows)",
            new_values={"file": file_name, "checksum": checksum, "trigger": trigger},
        )
        db.commit()
        log.info("Backup completed: %s (%d bytes)", file_name, record.size_bytes)
        return record

    except Exception as exc:
        db.rollback()
        record = db.get(BackupRecord, record.id) or record
        record.status = BackupStatus.FAILED
        record.error_message = f"{type(exc).__name__}: {exc}"[:2000]
        record.completed_at = utcnow()
        record.duration_seconds = round(time.monotonic() - monotonic, 3)
        db.commit()
        try:
            notification_service.notify(
                db,
                notification_type=NotificationType.BACKUP_FAILED,
                severity=NotificationSeverity.CRITICAL,
                title_tr="Yedekleme başarısız",
                title_en="Backup failed",
                body_tr=f"{file_name}: {record.error_message}",
                body_en=f"{file_name}: {record.error_message}",
                role_code="SYSTEM_ADMIN",
                entity_type="BackupRecord",
                entity_id=record.id,
                dedupe_key=f"backup_failed:{record.id}",
                commit=True,
            )
        except Exception:  # pragma: no cover - notification must never mask the cause
            log.exception("Could not raise backup-failure notification")
        log.exception("Backup failed: %s", file_name)
        if target.exists():
            target.unlink(missing_ok=True)
        if isinstance(exc, BackupError):
            raise
        raise BackupError("backup.failed", params={"error": str(exc)}) from exc
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ===========================================================================
# Verify
# ===========================================================================
def verify_backup(db: Session, record: BackupRecord, *, user_id: int | None = None) -> BackupRecord:
    """
    Re-checksum the archive, open it, and integrity-check the database inside.

    Sets the record to VERIFIED or CORRUPT — a backup nobody has verified is
    only a hope, not a recovery plan.
    """
    path = Path(record.file_path)
    messages: list[str] = []
    ok = True

    if not path.is_file():
        record.status = BackupStatus.CORRUPT
        record.verified_at = utcnow()
        record.verify_message = "file_missing"
        db.commit()
        raise NotFoundError("backup.file_missing", params={"file": record.file_name})

    checksum = _sha256(path)
    if record.checksum_sha256 and checksum != record.checksum_sha256:
        ok = False
        messages.append("checksum_mismatch")
    elif not record.checksum_sha256:
        record.checksum_sha256 = checksum
        messages.append("checksum_recorded")

    work_dir = Path(tempfile.mkdtemp(prefix="vs_verify_"))
    try:
        with zipfile.ZipFile(path) as archive:
            broken = archive.testzip()
            if broken:
                ok = False
                messages.append(f"corrupt_member:{broken}")
            names = set(archive.namelist())
            member = DB_MEMBER_SQLITE if DB_MEMBER_SQLITE in names else (
                DB_MEMBER_PG if DB_MEMBER_PG in names else None
            )
            if member is None:
                ok = False
                messages.append("database_member_missing")
            elif member == DB_MEMBER_SQLITE:
                extracted = Path(archive.extract(member, work_dir))
                conn = sqlite3.connect(str(extracted))
                try:
                    result = conn.execute("PRAGMA integrity_check").fetchone()
                    if not result or result[0] != "ok":
                        ok = False
                        messages.append(f"integrity_check:{result[0] if result else 'no_result'}")
                    else:
                        messages.append("integrity_check:ok")
                    tables = int(
                        conn.execute(
                            "SELECT count(*) FROM sqlite_master WHERE type='table'"
                        ).fetchone()[0]
                    )
                    messages.append(f"tables:{tables}")
                finally:
                    conn.close()
            else:
                # A plain-SQL dump cannot be integrity-checked without a server;
                # a readable, non-empty, checksum-matching dump is the strongest
                # statement we can make offline.
                info = archive.getinfo(member)
                if info.file_size <= 0:
                    ok = False
                    messages.append("dump_empty")
                else:
                    messages.append(f"dump_bytes:{info.file_size}")
    except zipfile.BadZipFile:
        ok = False
        messages.append("bad_zip")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    record.status = BackupStatus.VERIFIED if ok else BackupStatus.CORRUPT
    record.verified_at = utcnow()
    record.verify_message = ", ".join(messages)[:512]
    audit_service.record(
        db,
        AuditAction.BACKUP,
        entity_type="BackupRecord",
        entity_id=record.id,
        entity_label=record.file_name,
        user_id=user_id,
        summary=f"backup verify -> {record.status}",
        new_values={"result": record.verify_message},
    )
    db.commit()
    return record


# ===========================================================================
# Restore
# ===========================================================================
def restore_backup(
    db: Session,
    record: BackupRecord,
    *,
    user_id: int | None = None,
    username: str | None = None,
    confirm: bool = False,
) -> BackupRecord:
    """
    Replace the live database with the contents of *record*.

    Refuses without ``confirm``; verifies the archive first; and always takes a
    safety backup of the current database so an unwanted restore is itself
    reversible.
    """
    if not confirm:
        raise ValidationError("backup.confirm_required")

    verify_backup(db, record, user_id=user_id)
    if record.status == BackupStatus.CORRUPT:
        raise RestoreError("backup.checksum_mismatch", params={"file": record.file_name})

    path = Path(record.file_path)
    safety: BackupRecord | None = None
    try:
        safety = create_backup(
            db,
            backup_type=BackupType.DATABASE,
            trigger="PRE_RESTORE",
            user_id=user_id,
            username=username,
            notes=f"Automatic safety backup taken before restoring #{record.id}",
        )
    except BackupError:
        log.exception("Safety backup before restore failed")
        raise

    work_dir = Path(tempfile.mkdtemp(prefix="vs_restore_"))
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if settings.is_sqlite:
                if DB_MEMBER_SQLITE not in names:
                    raise RestoreError("backup.wrong_engine")
                extracted = Path(archive.extract(DB_MEMBER_SQLITE, work_dir))
            else:
                if DB_MEMBER_PG not in names:
                    raise RestoreError("backup.wrong_engine")
                extracted = Path(archive.extract(DB_MEMBER_PG, work_dir))

        if settings.is_sqlite:
            target = settings.sqlite_file
            if target is None:
                raise RestoreError("backup.database_missing")
            # Close every pooled connection first: on Windows an open handle
            # blocks the replacement, and on any OS a live reader would see a
            # half-swapped file.
            db.close()
            engine.dispose()
            for sidecar in (
                target.with_name(target.name + "-wal"),
                target.with_name(target.name + "-shm"),
            ):
                sidecar.unlink(missing_ok=True)
            shutil.copy2(extracted, target)
        else:
            _pg_restore(extracted)

        # Prove the restored database is reachable before declaring success.
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))

        record.status = BackupStatus.RESTORED
        record.restored_at = utcnow()
        record.restored_by_id = user_id
        audit_service.record(
            db,
            AuditAction.RESTORE,
            entity_type="BackupRecord",
            entity_id=record.id,
            entity_label=record.file_name,
            user_id=user_id,
            username=username,
            summary=f"database restored from {record.file_name}",
            new_values={
                "safety_backup": safety.file_name if safety else None,
                "checksum": record.checksum_sha256,
            },
        )
        db.commit()
        setting_service.invalidate_cache()
        log.warning("Database restored from %s by user %s", record.file_name, user_id)
        return record
    except Exception as exc:
        db.rollback()
        log.exception("Restore failed from %s", record.file_name)
        if isinstance(exc, RestoreError):
            raise
        raise RestoreError("backup.restore_failed", params={"error": str(exc)}) from exc
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ===========================================================================
# Housekeeping
# ===========================================================================
def list_backups(
    db: Session,
    *,
    status: str | None = None,
    backup_type: str | None = None,
    page: int = 1,
    size: int = 50,
) -> tuple[list[BackupRecord], int]:
    conds: list[Any] = []
    if status:
        conds.append(BackupRecord.status == status)
    if backup_type:
        conds.append(BackupRecord.backup_type == backup_type)

    total = int(
        db.execute(select(func.count(BackupRecord.id)).where(*conds)).scalar_one() or 0
    )
    rows = db.execute(
        select(BackupRecord)
        .where(*conds)
        .order_by(BackupRecord.id.desc())
        .offset(max(0, (page - 1) * size))
        .limit(size)
    ).scalars().all()
    return list(rows), total


def get_backup(db: Session, backup_id: int) -> BackupRecord:
    record = db.get(BackupRecord, backup_id)
    if record is None:
        raise NotFoundError("backup.not_found", params={"id": backup_id})
    return record


def delete_backup(
    db: Session, record: BackupRecord, *, user_id: int | None = None, username: str | None = None
) -> None:
    path = Path(record.file_path)
    path.unlink(missing_ok=True)
    audit_service.record(
        db,
        AuditAction.DELETE,
        entity_type="BackupRecord",
        entity_id=record.id,
        entity_label=record.file_name,
        user_id=user_id,
        username=username,
        summary="backup deleted",
        old_values={"file": record.file_name, "checksum": record.checksum_sha256},
    )
    db.delete(record)
    db.commit()


def prune(db: Session, retention_days: int | None = None, *, user_id: int | None = None) -> int:
    """
    Delete backups older than the retention window.

    The most recent successful backup is always kept, whatever its age — an
    aggressive retention setting must never leave the system with nothing.
    """
    days = int(
        retention_days
        if retention_days is not None
        else setting_service.get_typed(
            db, "backup", "retention_days", settings.backup_retention_days
        )
        or settings.backup_retention_days
    )
    cutoff = utcnow() - timedelta(days=max(1, days))

    newest_ok = db.execute(
        select(BackupRecord.id)
        .where(BackupRecord.status.in_([BackupStatus.COMPLETED, BackupStatus.VERIFIED]))
        .order_by(BackupRecord.id.desc())
        .limit(1)
    ).scalar_one_or_none()

    stale = db.execute(
        select(BackupRecord).where(BackupRecord.created_at < cutoff)
    ).scalars().all()

    removed = 0
    for record in stale:
        if newest_ok is not None and record.id == newest_ok:
            continue
        Path(record.file_path).unlink(missing_ok=True)
        db.delete(record)
        removed += 1

    if removed:
        audit_service.record(
            db,
            AuditAction.DELETE,
            entity_type="BackupRecord",
            user_id=user_id,
            summary=f"pruned {removed} backup(s) older than {days} days",
        )
    db.commit()
    return removed


def schedule_due(db: Session, *, now: datetime | None = None) -> bool:
    """
    Is an automatic backup due?

    Called by the scheduler; keeps the cron interpretation in one place so the
    UI, the health check and the job all agree on "due".
    """
    if not bool(
        setting_service.get_typed(db, "backup", "auto_enabled", settings.backup_auto_enabled)
    ):
        return False

    schedule = str(
        setting_service.get_typed(db, "backup", "schedule", settings.backup_auto_cron)
        or settings.backup_auto_cron
    ).lower()
    if schedule == "off":
        return False

    interval_days = {"daily": 1, "weekly": 7, "monthly": 30}.get(schedule, 1)
    reference = now or utcnow()
    last = db.execute(
        select(BackupRecord)
        .where(
            BackupRecord.status.in_([BackupStatus.COMPLETED, BackupStatus.VERIFIED]),
            BackupRecord.trigger == "SCHEDULED",
        )
        .order_by(BackupRecord.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if last is None or last.completed_at is None:
        return True
    return (reference - last.completed_at) >= timedelta(days=interval_days)


def run_scheduled(db: Session) -> BackupRecord | None:
    """Take the scheduled backup if one is due, then apply the retention policy."""
    if not schedule_due(db):
        return None
    record = create_backup(db, backup_type=BackupType.FULL, trigger="SCHEDULED")
    verify_backup(db, record)
    prune(db)
    return record


# ===========================================================================
# Settings export / import
# ===========================================================================
def export_settings(db: Session) -> dict[str, Any]:
    """Portable settings snapshot (secrets excluded — they stay in the env)."""
    return {
        "app_version": settings.app_version,
        "exported_at": utcnow().isoformat(),
        "settings": setting_service.export_all(db),
    }


def import_settings(
    db: Session, payload: dict[str, Any], *, user: Any = None
) -> int:
    """Apply a snapshot produced by :func:`export_settings`."""
    data = payload.get("settings") if isinstance(payload.get("settings"), dict) else payload
    if not isinstance(data, dict):
        raise ValidationError("setting.invalid_payload")
    applied = setting_service.import_settings(db, data, user=user)
    audit_service.record(
        db,
        AuditAction.SETTING_CHANGE,
        entity_type="Setting",
        user_id=getattr(user, "id", None),
        username=getattr(user, "username", None),
        summary=f"imported {applied} setting(s)",
        commit=True,
    )
    return applied


def read_manifest(record: BackupRecord) -> dict[str, Any]:
    """Manifest stored inside an archive — used by the restore confirmation UI."""
    path = Path(record.file_path)
    if not path.is_file():
        raise NotFoundError("backup.file_missing", params={"file": record.file_name})
    try:
        with zipfile.ZipFile(path) as archive:
            if MANIFEST_MEMBER not in archive.namelist():
                return {}
            return loads(archive.read(MANIFEST_MEMBER).decode("utf-8"), {}) or {}
    except zipfile.BadZipFile as exc:
        raise BackupError("backup.checksum_mismatch") from exc


def storage_summary(db: Session) -> dict[str, Any]:
    """Backup count, total size on disk and the age of the newest good backup."""
    rows = db.execute(select(BackupRecord)).scalars().all()
    total_bytes = sum(int(r.size_bytes or 0) for r in rows)
    newest = None
    for r in rows:
        if r.status in (BackupStatus.COMPLETED, BackupStatus.VERIFIED, BackupStatus.RESTORED):
            if newest is None or r.created_at > newest.created_at:
                newest = r
    usage = shutil.disk_usage(str(_backup_dir()))
    return {
        "count": len(rows),
        "total_bytes": total_bytes,
        "directory": str(_backup_dir()),
        "free_bytes": usage.free,
        "newest_file": newest.file_name if newest else None,
        "newest_at": newest.created_at.isoformat() if newest else None,
        "newest_age_days": (
            (date.today() - newest.created_at.date()).days if newest else None
        ),
        "newest_verified": bool(newest and newest.verified_at is not None),
    }


__all__ = [
    "create_backup",
    "delete_backup",
    "export_settings",
    "get_backup",
    "import_settings",
    "list_backups",
    "prune",
    "read_manifest",
    "restore_backup",
    "run_scheduled",
    "schedule_due",
    "storage_summary",
    "verify_backup",
]
