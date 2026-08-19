"""
Alembic environment.

The database URL always comes from application settings (``VS_DATABASE_URL``),
never from alembic.ini, so migrations and the app can never disagree about
which database they are talking to.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make the "app" package importable when alembic runs from backend/
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import settings  # noqa: E402
from app.core.db import Base  # noqa: E402
import app.models  # noqa: F401,E402  — registers every mapper
import app.compliance.models  # noqa: F401,E402  — compliance/HSP layer

config = context.config
config.set_main_option("sqlalchemy.url", settings.effective_database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _include_object(obj, name, type_, reflected, compare_to):  # noqa: ARG001
    """Never let autogenerate try to drop SQLite's internal tables."""
    if type_ == "table" and name.startswith("sqlite_"):
        return False
    return True


def _render_item(type_, obj, autogen_context):
    """
    Render our custom column types as plain SQLAlchemy types.

    A migration must remain valid forever, so it must not import application
    code — ``app.core.db.Money`` could be renamed or removed and every old
    migration would break.  Emitting ``sa.Numeric(18, 4)`` freezes the actual
    column definition into the migration, which is the point of migrations.
    """
    if type_ == "type":
        from app.core.db import Money, Quantity, UTCDateTime
        from app.models.base import JSONText

        autogen_context.imports.add("import sqlalchemy as sa")
        # Quantity, Money'den turedigi icin ONCE kontrol edilmeli; sirasi
        # ters olursa 3 ondalikli miktar kolonlari 4 ondalikli para olarak
        # yazilir ve migration modelle sessizce ayrisir.
        if isinstance(obj, Quantity):
            return "sa.Numeric(precision=18, scale=3)"
        if isinstance(obj, Money):
            return "sa.Numeric(precision=18, scale=4)"
        if isinstance(obj, UTCDateTime):
            return "sa.DateTime(timezone=True)"
        if isinstance(obj, JSONText):
            return "sa.Text()"
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=settings.effective_database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_object=_include_object,
        render_item=_render_item,
        render_as_batch=settings.is_sqlite,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            include_object=_include_object,
        render_item=_render_item,
            # SQLite cannot ALTER most things — batch mode rebuilds the table.
            render_as_batch=settings.is_sqlite,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
