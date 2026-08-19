"""
Logging configuration with rotation and automatic secret redaction.

Four sinks, as required by the specification:
    logs/application.log   — everything at INFO+
    logs/error.log         — WARNING+ only
    logs/ai.log            — AI subsystem (app.ai.*)
    logs/security.log      — auth / RBAC / audit (app.security, app.auth)

Every record passes through :class:`SecretRedactor`, so API keys, bearer
tokens and passwords can never reach disk even if a caller is careless.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys

from app.core.config import settings

# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------
_REDACTED = "***REDACTED***"

_SECRET_PATTERNS: list[re.Pattern[str]] = [
    # key/value pairs.  The separator group tolerates an optional closing quote
    # after the key and an optional opening quote before the value, so both
    #   api_key=abc123
    #   "password": "SuperSecret123"
    # are caught.
    re.compile(
        r"(?i)\b(api[_-]?key|apikey|secret|password|passwd|pwd|token|"
        r"access[_-]?token|refresh[_-]?token|authorization|auth|bearer|"
        r"client[_-]?secret|private[_-]?key)\b"
        r"(\"?\s*[:=]\s*[\"']?)"
        r"([^\s,;&\"'}\]]{4,})"
    ),
    # Bare provider-style keys
    re.compile(r"\bnvapi-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    # JWTs
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
]


def redact(text: str) -> str:
    """Strip anything that looks like a credential out of *text*."""
    if not text:
        return text
    out = _SECRET_PATTERNS[0].sub(lambda m: f"{m.group(1)}{m.group(2)}{_REDACTED}", text)
    for pat in _SECRET_PATTERNS[1:]:
        out = pat.sub(_REDACTED, out)
    return out


class SecretRedactor(logging.Filter):
    """Redacts credentials from the message and every positional argument."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = redact(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {
                        k: redact(v) if isinstance(v, str) else v
                        for k, v in record.args.items()
                    }
                elif isinstance(record.args, tuple):
                    record.args = tuple(
                        redact(a) if isinstance(a, str) else a for a in record.args
                    )
        except Exception:  # never let logging break the request
            pass
        return True


class _NameFilter(logging.Filter):
    """Passes only records whose logger name starts with one of *prefixes*."""

    def __init__(self, prefixes: tuple[str, ...]) -> None:
        super().__init__()
        self.prefixes = prefixes

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name.startswith(self.prefixes)


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
_FMT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

_configured = False


def _rotating(filename: str, level: int, *, filters: list[logging.Filter] | None = None):
    h = logging.handlers.RotatingFileHandler(
        settings.log_path / filename,
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
        encoding="utf-8",
    )
    h.setLevel(level)
    h.setFormatter(logging.Formatter(_FMT, _DATEFMT))
    h.addFilter(SecretRedactor())
    for f in filters or []:
        h.addFilter(f)
    return h


def setup_logging(force: bool = False) -> None:
    """Install handlers.  Idempotent — safe to call from multiple entrypoints."""
    global _configured
    if _configured and not force:
        return

    root = logging.getLogger()
    root.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
    for h in list(root.handlers):
        root.removeHandler(h)

    # Console — UTF-8 so Turkish characters render on Windows terminals.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(_FMT, _DATEFMT))
    console.addFilter(SecretRedactor())
    root.addHandler(console)

    root.addHandler(_rotating("application.log", logging.INFO))
    root.addHandler(_rotating("error.log", logging.WARNING))
    root.addHandler(
        _rotating("ai.log", logging.INFO, filters=[_NameFilter(("app.ai",))])
    )
    root.addHandler(
        _rotating(
            "security.log",
            logging.INFO,
            filters=[_NameFilter(("app.security", "app.auth", "app.audit"))],
        )
    )

    # Quiet down noisy third parties.
    for noisy, lvl in (
        ("uvicorn.access", logging.WARNING),
        ("httpx", logging.WARNING),
        ("httpcore", logging.WARNING),
        ("multipart", logging.WARNING),
        ("watchfiles", logging.WARNING),
    ):
        logging.getLogger(noisy).setLevel(lvl)

    if settings.db_echo:
        logging.getLogger("sqlalchemy.engine").setLevel(logging.INFO)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Convenience accessor that guarantees setup has run."""
    setup_logging()
    return logging.getLogger(name)


__all__ = ["setup_logging", "get_logger", "redact", "SecretRedactor"]
