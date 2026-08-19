"""
HTTP middleware: request id + timing, security headers, and a dependency-free
in-process rate limiter.

The rate limiter uses a sliding window per (client, route-class).  It is
deliberately in-process so the system needs no Redis; when ``VS_REDIS_URL`` is
configured the same interface can be backed by Redis without touching callers.
"""

from __future__ import annotations

import time
import uuid
from collections import defaultdict, deque
from threading import Lock

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.config import settings
from app.core.logging_config import get_logger

log = get_logger("app.http")
sec_log = get_logger("app.security")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attach a request id, measure duration, log slow/failed calls."""

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        request.state.request_id = rid
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            elapsed = (time.perf_counter() - started) * 1000
            log.exception(
                "request failed rid=%s %s %s after %.1fms",
                rid, request.method, request.url.path, elapsed,
            )
            raise
        elapsed = (time.perf_counter() - started) * 1000
        response.headers["X-Request-ID"] = rid
        response.headers["X-Response-Time"] = f"{elapsed:.1f}ms"
        if elapsed > 2000:
            log.warning(
                "slow request rid=%s %s %s %.0fms",
                rid, request.method, request.url.path, elapsed,
            )
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Baseline hardening headers on every response."""

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        response: Response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy", "geolocation=(self), camera=(self), microphone=()"
        )
        if settings.env == "production":
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response


class _SlidingWindow:
    """Thread-safe sliding-window counter."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def allow(self, key: str, limit: int, window_s: float = 60.0) -> tuple[bool, int]:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            cutoff = now - window_s
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= limit:
                return False, 0
            q.append(now)
            return True, limit - len(q)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


_window = _SlidingWindow()

#: Paths that get the stricter auth limit.
_AUTH_PATHS = ("/auth/login", "/auth/refresh", "/auth/password")


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-IP request throttling, stricter on authentication endpoints."""

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        path = request.url.path
        if request.method == "OPTIONS" or path.startswith(("/docs", "/redoc", "/openapi", "/static")):
            return await call_next(request)

        client = request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or (
            request.client.host if request.client else "unknown"
        )
        is_auth = any(p in path for p in _AUTH_PATHS)
        limit = settings.login_rate_limit_per_minute if is_auth else settings.rate_limit_per_minute
        bucket = f"{client}:{'auth' if is_auth else 'api'}"

        allowed, remaining = _window.allow(bucket, limit)
        if not allowed:
            sec_log.warning("rate limit exceeded client=%s path=%s limit=%d", client, path, limit)
            return JSONResponse(
                status_code=429,
                content={
                    "error": "rate_limited",
                    "message_key": "error.rate_limited",
                    "message": "Too many requests. Please slow down.",
                },
                headers={"Retry-After": "60"},
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response


def reset_rate_limits() -> None:
    """Test helper — clears all counters."""
    _window.reset()
