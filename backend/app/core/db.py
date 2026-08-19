"""
Database engine, session factory and declarative base.

Works against SQLite (zero-install default) and PostgreSQL (production) with
the same ORM code.  SQLite gets WAL journalling and enforced foreign keys so
its behaviour matches PostgreSQL as closely as practical.
"""

from __future__ import annotations

import logging
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from datetime import UTC
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, Engine, MetaData, Numeric, create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import QueuePool, StaticPool
from sqlalchemy.types import TypeDecorator

from app.core.config import settings

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Naming convention: deterministic constraint names so Alembic autogenerate
# can emit correct ALTER statements on both backends.
# ---------------------------------------------------------------------------
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for every ORM model."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# ---------------------------------------------------------------------------
# Money / Quantity column type
# ---------------------------------------------------------------------------
MONEY_PRECISION = 18
MONEY_SCALE = 4
QTY_SCALE = 3


class Money(TypeDecorator):
    """
    Decimal money column.

    PostgreSQL -> NUMERIC(18,4) natively.
    SQLite     -> stored as NUMERIC; SQLAlchemy round-trips through float, so
                  we re-quantize on the way out to guarantee exact 4-dp values.
    """

    impl = Numeric(MONEY_PRECISION, MONEY_SCALE)
    cache_ok = True
    _exp = Decimal(1).scaleb(-MONEY_SCALE)

    def process_bind_param(self, value: Any, dialect: Any) -> Any:  # noqa: ARG002
        if value is None:
            return None
        if not isinstance(value, Decimal):
            value = Decimal(str(value))
        return value.quantize(self._exp)

    def process_result_value(self, value: Any, dialect: Any) -> Decimal | None:  # noqa: ARG002
        if value is None:
            return None
        if not isinstance(value, Decimal):
            value = Decimal(str(value))
        return value.quantize(self._exp)


class Quantity(Money):
    """Decimal quantity column (3 decimal places — supports kg / litre / fraction)."""

    impl = Numeric(MONEY_PRECISION, QTY_SCALE)
    cache_ok = True
    _exp = Decimal(1).scaleb(-QTY_SCALE)


class UTCDateTime(TypeDecorator):
    """
    Timezone-aware UTC timestamp that behaves identically on SQLite and
    PostgreSQL.

    SQLite has no timestamp type — it stores an ISO string and hands back a
    **naive** ``datetime``.  PostgreSQL with ``TIMESTAMPTZ`` hands back an
    aware one.  Comparing the two raises ``TypeError``, which is exactly the
    kind of bug that only shows up once an account gets locked out at 2am.

    This decorator normalises both directions: values are converted to UTC on
    the way in and always come back tagged as UTC.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> Any:  # noqa: ARG002
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: Any, dialect: Any) -> Any:  # noqa: ARG002
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
def _build_engine() -> Engine:
    url = settings.effective_database_url
    kwargs: dict[str, Any] = {
        "echo": settings.db_echo,
        "future": True,
        "pool_pre_ping": True,
    }

    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        if ":memory:" in url:
            # A shared in-memory DB must use one connection for all sessions.
            kwargs["poolclass"] = StaticPool
        else:
            kwargs["poolclass"] = QueuePool
            kwargs["pool_size"] = settings.db_pool_size
            kwargs["max_overflow"] = settings.db_max_overflow
    else:
        kwargs["poolclass"] = QueuePool
        kwargs["pool_size"] = settings.db_pool_size
        kwargs["max_overflow"] = settings.db_max_overflow

    eng = create_engine(url, **kwargs)

    if url.startswith("sqlite"):

        @event.listens_for(eng, "connect")
        def _sqlite_pragmas(dbapi_conn, _rec):  # type: ignore[no-untyped-def]
            cur = dbapi_conn.cursor()
            try:
                cur.execute("PRAGMA foreign_keys=ON")
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.execute("PRAGMA busy_timeout=30000")
                cur.execute("PRAGMA cache_size=-64000")  # 64 MB page cache
                cur.execute("PRAGMA temp_store=MEMORY")
            finally:
                cur.close()

    return eng


engine: Engine = _build_engine()

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
    class_=Session,
)


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------
def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a request-scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional context manager for scripts, jobs and services."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def ping() -> bool:
    """Cheap connectivity probe used by the health-check endpoint."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # pragma: no cover - depends on environment
        log.error("Database ping failed: %s", exc)
        return False


def create_all() -> None:
    """Create every table (used by tests and first-run bootstrap)."""
    import app.models  # noqa: F401  — ensures all mappers are imported
    # Compliance/HSP layer lives in its own package with "cmp_" table names.
    # Imported here so a single create_all() builds the whole schema; the
    # layer is optional, so an import failure must not stop the core system.
    try:
        import app.compliance.models  # noqa: F401
    except Exception:  # pragma: no cover - optional layer
        log.exception("Compliance model layer could not be loaded")

    Base.metadata.create_all(bind=engine)


def drop_all() -> None:
    """Drop every table.  Test-suite use only."""
    import app.models  # noqa: F401

    try:
        import app.compliance.models  # noqa: F401
    except Exception:  # pragma: no cover - optional layer
        pass

    Base.metadata.drop_all(bind=engine)
