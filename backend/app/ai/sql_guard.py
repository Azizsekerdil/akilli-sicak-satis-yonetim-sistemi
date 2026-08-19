"""
Read-only SQL gate for natural-language querying.

A language model writing SQL against a live business database is only safe if
something between the model and the database refuses everything except a single
read.  That is this module.

The policy is deliberately *allow-list first*: the statement must be one
``SELECT`` (optionally preceded by a ``WITH`` block) and nothing else.  Every
other check — no second statement, no comments, no DDL/DML verbs, no
credential-bearing tables — is a second line of defence behind that.

Analysis is performed on a copy of the statement with string literals blanked
out, so a legitimate ``WHERE name = 'a;b--c'`` is not mistaken for an injection
while an actual injection cannot hide inside quotes.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import UnsafeQueryError
from app.core.logging_config import get_logger

log = get_logger("app.ai.sql")

DEFAULT_MAX_ROWS = 1000
HARD_MAX_ROWS = 5000

#: Verbs that write, change structure, escalate privileges or reach the file
#: system.  None of them has any business in an analyst's question.
FORBIDDEN_KEYWORDS: tuple[str, ...] = (
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "TRUNCATE",
    "GRANT", "REVOKE", "ATTACH", "DETACH", "PRAGMA", "VACUUM", "REPLACE",
    "MERGE", "UPSERT", "REINDEX", "ANALYZE", "EXEC", "EXECUTE", "CALL",
    "COPY", "COMMIT", "ROLLBACK", "SAVEPOINT", "BEGIN", "SET", "LOCK",
    "INTO",  # blocks "SELECT ... INTO newtable"
)

#: Tables holding credentials, security history or provider configuration.
#: Reading them through an AI channel would turn a reporting feature into an
#: exfiltration channel.
FORBIDDEN_TABLES: frozenset[str] = frozenset(
    {
        "users",
        "user_sessions",
        "login_attempts",
        "audit_logs",
        "ai_provider_configs",
        "settings",
        "permissions",
        "role_permissions",
        "ai_terminal_commands",
        "ai_terminal_sessions",
        # Engine catalogues — schema spelunking is not a business question.
        "sqlite_master",
        "sqlite_temp_master",
        "sqlite_schema",
        "information_schema",
        "pg_catalog",
        "pg_shadow",
        "pg_authid",
        "pg_user",
        "pg_roles",
    }
)

#: Column names that carry secrets even when the table itself is harmless.
FORBIDDEN_COLUMNS: frozenset[str] = frozenset(
    {
        "password_hash",
        "password",
        "api_key",
        "api_key_ref",
        "secret_key",
        "access_token",
        "refresh_token",
        "token_hash",
        "private_key",
    }
)

#: File-system and extension-loading functions available in SQLite builds.
FORBIDDEN_FUNCTIONS: frozenset[str] = frozenset(
    {"load_extension", "readfile", "writefile", "edit", "fts3_tokenizer", "pg_read_file"}
)

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_LIMIT_RE = re.compile(r"\blimit\b", re.IGNORECASE)


def _blank_literals(sql: str) -> str:
    """
    Replace the *contents* of quoted literals with spaces.

    Quotes themselves are kept so the statement's shape is unchanged; doubled
    quotes (SQL's own escape) are handled, and a literal that never closes is
    treated as an unterminated string, which the validator rejects.
    """
    out: list[str] = []
    quote: str | None = None
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if quote is None:
            if char in ("'", '"', "`"):
                quote = char
                out.append(char)
            else:
                out.append(char)
            index += 1
            continue

        if char == quote:
            if index + 1 < length and sql[index + 1] == quote:  # escaped quote
                out.append("  ")
                index += 2
                continue
            quote = None
            out.append(char)
            index += 1
            continue

        out.append(" " if char != "\n" else "\n")
        index += 1

    if quote is not None:
        raise UnsafeQueryError(
            "ai.sql_forbidden", params={"reason": "unterminated string literal"}
        )
    return "".join(out)


def _reject(reason: str) -> None:
    log.warning("Rejected AI SQL: %s", reason)
    raise UnsafeQueryError("ai.sql_forbidden", params={"reason": reason})


def validate(sql: str) -> str:
    """
    Return the cleaned statement, or raise :class:`UnsafeQueryError`.

    The returned string has surrounding whitespace and a single trailing
    semicolon removed; nothing else about it is rewritten.
    """
    if not sql or not sql.strip():
        _reject("empty statement")

    cleaned = sql.strip().rstrip(";").strip()
    masked = _blank_literals(cleaned)

    if ";" in masked:
        _reject("multiple statements are not allowed")
    if "--" in masked or "/*" in masked or "*/" in masked:
        _reject("comments are not allowed")

    upper = masked.upper()
    head = upper.lstrip("( \t\r\n")
    if not (head.startswith("SELECT") or head.startswith("WITH")):
        _reject("only SELECT (or WITH ... SELECT) statements are allowed")
    if head.startswith("WITH") and not re.search(r"\bSELECT\b", upper):
        _reject("WITH block without a SELECT")

    for keyword in FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{keyword}\b", upper):
            _reject(f"keyword '{keyword}' is not allowed")

    identifiers = {m.group(0).lower() for m in _IDENT_RE.finditer(masked)}
    hit_tables = identifiers & FORBIDDEN_TABLES
    if hit_tables:
        _reject(f"table '{sorted(hit_tables)[0]}' is not readable through this channel")
    hit_columns = identifiers & FORBIDDEN_COLUMNS
    if hit_columns:
        _reject(f"column '{sorted(hit_columns)[0]}' is not readable through this channel")
    hit_functions = identifiers & FORBIDDEN_FUNCTIONS
    if hit_functions:
        _reject(f"function '{sorted(hit_functions)[0]}' is not allowed")

    return cleaned


def enforce_limit(sql: str, max_rows: int = DEFAULT_MAX_ROWS) -> str:
    """Append ``LIMIT`` when the statement does not already cap its own rows."""
    capped = max(1, min(int(max_rows or DEFAULT_MAX_ROWS), HARD_MAX_ROWS))
    cleaned = sql.strip().rstrip(";").strip()
    if _LIMIT_RE.search(_blank_literals(cleaned)):
        return cleaned
    return f"{cleaned} LIMIT {capped}"


def _jsonable(value: Any) -> Any:
    """Coerce a driver value into something the API can serialise."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(value))} bytes>"
    return value


def run_readonly(
    db: Session, sql: str, *, max_rows: int = DEFAULT_MAX_ROWS
) -> dict[str, Any]:
    """
    Validate, cap and execute *sql*, returning columns plus JSON-safe rows.

    On SQLite the connection is additionally put into ``query_only`` mode for
    the duration of the call, so even a validator bypass could not write.
    """
    capped = max(1, min(int(max_rows or DEFAULT_MAX_ROWS), HARD_MAX_ROWS))
    statement = enforce_limit(validate(sql), capped + 1)

    read_only_set = False
    try:
        if settings.is_sqlite:
            db.execute(text("PRAGMA query_only = ON"))
            read_only_set = True
        result = db.execute(text(statement))
        columns = list(result.keys())
        raw_rows = result.fetchall()
    except SQLAlchemyError as exc:
        db.rollback()
        message = str(getattr(exc, "orig", exc)).splitlines()[0][:200]
        raise UnsafeQueryError(
            "ai.sql_forbidden", params={"reason": f"query failed: {message}"}
        ) from exc
    finally:
        if read_only_set:
            try:
                db.execute(text("PRAGMA query_only = OFF"))
            except SQLAlchemyError:  # pragma: no cover - connection already gone
                log.warning("Could not restore PRAGMA query_only")

    truncated = len(raw_rows) > capped
    rows = [
        {col: _jsonable(value) for col, value in zip(columns, row)}
        for row in raw_rows[:capped]
    ]
    return {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "sql": statement,
    }


def schema_summary(*, max_tables: int = 80) -> str:
    """
    Compact ``table(col, col, …)`` listing of the readable schema.

    Fed to the model so it writes SQL against columns that actually exist —
    blocked tables are omitted entirely rather than mentioned and refused.
    """
    from app.core.db import Base
    import app.models  # noqa: F401  — registers every mapper

    lines: list[str] = []
    for table in sorted(Base.metadata.tables.values(), key=lambda t: t.name):
        if table.name in FORBIDDEN_TABLES:
            continue
        columns = [c.name for c in table.columns if c.name not in FORBIDDEN_COLUMNS]
        lines.append(f"{table.name}({', '.join(columns)})")
        if len(lines) >= max_tables:
            break
    return "\n".join(lines)


__all__ = [
    "DEFAULT_MAX_ROWS",
    "FORBIDDEN_COLUMNS",
    "FORBIDDEN_KEYWORDS",
    "FORBIDDEN_TABLES",
    "enforce_limit",
    "run_readonly",
    "schema_summary",
    "validate",
]
