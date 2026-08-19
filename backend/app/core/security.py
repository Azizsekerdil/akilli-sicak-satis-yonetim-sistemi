"""
Password hashing, JWT issuing/verification and secret handling.

Uses the ``bcrypt`` library directly rather than passlib: passlib 1.7.4 breaks
against bcrypt >= 4.1 (it reads the removed ``bcrypt.__about__``), and going
direct removes an entire class of dependency-version breakage.

A pure-Python PBKDF2-HMAC-SHA256 fallback is included so the system still runs
(and tests still pass) on a machine where the bcrypt wheel is unavailable.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt

from app.core.config import settings

# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------
try:  # pragma: no cover - depends on environment
    import bcrypt as _bcrypt

    _HAS_BCRYPT = True
except Exception:  # pragma: no cover
    _bcrypt = None  # type: ignore[assignment]
    _HAS_BCRYPT = False

_PBKDF2_ROUNDS = 260_000
_PBKDF2_PREFIX = "pbkdf2_sha256"

#: bcrypt silently truncates at 72 bytes — we pre-hash longer inputs so long
#: passphrases keep their full entropy instead of being clipped.
_BCRYPT_MAX_BYTES = 72


def _prehash(password: str) -> bytes:
    raw = password.encode("utf-8")
    if len(raw) > _BCRYPT_MAX_BYTES:
        return base64.b64encode(hashlib.sha256(raw).digest())
    return raw


def hash_password(password: str) -> str:
    """Hash a plaintext password for storage."""
    if not password:
        raise ValueError("password must not be empty")
    if _HAS_BCRYPT:
        return _bcrypt.hashpw(_prehash(password), _bcrypt.gensalt(rounds=12)).decode()
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ROUNDS)
    return f"{_PBKDF2_PREFIX}${_PBKDF2_ROUNDS}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, hashed: str) -> bool:
    """Constant-time verification of *password* against a stored hash."""
    if not password or not hashed:
        return False
    try:
        if hashed.startswith(_PBKDF2_PREFIX):
            _, rounds_s, salt_b64, dk_b64 = hashed.split("$", 3)
            salt = base64.b64decode(salt_b64)
            expected = base64.b64decode(dk_b64)
            dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(rounds_s))
            return hmac.compare_digest(dk, expected)
        if _HAS_BCRYPT:
            return _bcrypt.checkpw(_prehash(password), hashed.encode())
        return False
    except Exception:
        return False


def password_strength_errors(password: str) -> list[str]:
    """
    Return machine-readable i18n keys for each failed policy rule.
    Empty list means the password is acceptable.
    """
    errs: list[str] = []
    if len(password) < settings.password_min_length:
        errs.append("password.too_short")
    if not any(c.isupper() for c in password):
        errs.append("password.needs_uppercase")
    if not any(c.islower() for c in password):
        errs.append("password.needs_lowercase")
    if not any(c.isdigit() for c in password):
        errs.append("password.needs_digit")
    return errs


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------
TokenType = Literal["access", "refresh"]


def _create_token(
    subject: str | int,
    token_type: TokenType,
    expires: timedelta,
    extra: dict[str, Any] | None = None,
) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(subject),
        "typ": token_type,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int((now + expires).timestamp()),
        "jti": secrets.token_urlsafe(16),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


def create_access_token(
    subject: str | int,
    *,
    role: str | None = None,
    scope: str | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    claims: dict[str, Any] = dict(extra or {})
    if role:
        claims["role"] = role
    if scope:
        claims["scope"] = scope
    return _create_token(
        subject, "access", timedelta(minutes=settings.access_token_minutes), claims
    )


def create_refresh_token(subject: str | int) -> str:
    return _create_token(subject, "refresh", timedelta(days=settings.refresh_token_days))


class TokenError(Exception):
    """Raised when a token is missing, malformed, expired or of the wrong type."""


def decode_token(token: str, *, expected_type: TokenType | None = None) -> dict[str, Any]:
    """Decode and validate a JWT.  Raises :class:`TokenError` on any problem."""
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("token.expired") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("token.invalid") from exc

    if expected_type and payload.get("typ") != expected_type:
        raise TokenError("token.wrong_type")
    return payload


# ---------------------------------------------------------------------------
# Misc secret utilities
# ---------------------------------------------------------------------------
def generate_api_key(prefix: str = "vs") -> str:
    return f"{prefix}_{secrets.token_urlsafe(32)}"


#: Fixed width of the asterisk run in :func:`mask_secret`.  Constant on purpose:
#: a variable-width mask would disclose the length of the secret.
_MASK_WIDTH = 12


def mask_secret(value: str | None, *, keep: int = 4) -> str:
    """
    Render a secret safe for display.

    Only the **last** ``keep`` characters survive; everything before them is
    replaced by a fixed-width run of asterisks.

    An earlier version also kept the first four characters. That leaked the
    key's prefix — which identifies the vendor and the key class — into every
    screenshot, export and slide the settings screen appeared in. You do not
    need a prefix to recognise a key you pasted yourself, so it is gone.

        >>> mask_secret("0123456789abcdef")
        '************cdef'
        >>> mask_secret("shrt")
        '****'
    """
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return f"{'*' * _MASK_WIDTH}{value[-keep:]}"


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


__all__ = [
    "hash_password",
    "verify_password",
    "password_strength_errors",
    "create_access_token",
    "create_refresh_token",
    "decode_token",
    "TokenError",
    "generate_api_key",
    "mask_secret",
    "constant_time_equals",
]
