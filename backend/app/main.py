"""
FastAPI application factory.

Run with::

    uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import PROJECT_ROOT, settings
from app.core.exceptions import AppError
from app.core.i18n import load_catalogues, normalize_language, t
from app.core.logging_config import get_logger, setup_logging
from app.core.middleware import (
    RateLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)

setup_logging()
log = get_logger("app.main")


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    """Startup / shutdown."""
    load_catalogues(force=True)
    log.info(
        "%s v%s starting | env=%s | db=%s | lang=%s",
        settings.app_name,
        settings.app_version,
        settings.env,
        "sqlite" if settings.is_sqlite else "postgresql",
        settings.default_language,
    )

    from app.core.db import ping
    from app.services import bootstrap_service

    if not ping():
        log.error("Database is not reachable at startup")
    else:
        try:
            bootstrap_service.ensure_baseline()
        except Exception:
            log.exception("Baseline bootstrap failed")

    yield
    log.info("Shutting down")


def _request_language(request: Request) -> str:
    return normalize_language(
        request.query_params.get("lang") or request.headers.get("Accept-Language")
    )


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=(
            "Akıllı Sıcak Satış Yönetim Sistemi — yiyecek & içecek dağıtımı için "
            "uçtan uca van sales platformu.\n\n"
            "Smart Van Sales Management System — end-to-end direct-store-delivery "
            "platform for food & beverage distribution."
        ),
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    # --- Middleware (outermost first) ------------------------------------
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "X-Response-Time", "Content-Disposition"],
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(RequestContextMiddleware)

    # --- Error handlers ---------------------------------------------------
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        lang = _request_language(request)
        rendered = t(exc.message_key, lang, **exc.params)
        if exc.status_code >= 500:
            log.error("AppError %s: %s", exc.error_code, exc.detail or exc.message_key)
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict(rendered))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        lang = _request_language(request)
        return JSONResponse(
            status_code=422,
            content={
                "error": "validation_error",
                "message_key": "error.validation_error",
                "message": t("error.validation_error", lang),
                "fields": [
                    {
                        "field": ".".join(str(p) for p in e.get("loc", [])[1:]),
                        "message": e.get("msg", ""),
                        "type": e.get("type", ""),
                    }
                    for e in exc.errors()
                ],
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        lang = _request_language(request)
        key = {401: "error.unauthorized", 403: "error.forbidden", 404: "error.not_found"}.get(
            exc.status_code, "error.http_error"
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": key.split(".")[-1],
                "message_key": key,
                "message": exc.detail if isinstance(exc.detail, str) else t(key, lang),
            },
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        rid = getattr(request.state, "request_id", "-")
        log.exception("Unhandled error rid=%s on %s", rid, request.url.path)
        lang = _request_language(request)
        body = {
            "error": "internal_error",
            "message_key": "error.internal",
            "message": t("error.internal", lang),
            "request_id": rid,
        }
        if settings.debug:
            body["detail"] = f"{type(exc).__name__}: {exc}"
        return JSONResponse(status_code=500, content=body)

    # --- Routes -----------------------------------------------------------
    from app.api.v1 import api_router

    app.include_router(api_router, prefix=settings.api_prefix)

    @app.get("/health", tags=["system"], summary="Liveness probe")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": settings.app_version, "app": settings.app_name}

    # --- Static frontend (served when the SPA has been built) -------------
    dist = PROJECT_ROOT / "frontend" / "dist"
    if dist.is_dir():
        dist_kok = dist.resolve()
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/", include_in_schema=False)
        async def _spa_root() -> FileResponse:
            return FileResponse(dist / "index.html")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def _spa_fallback(full_path: str) -> FileResponse:
            # Kapsam kontrolu SART: bu rota kimlik dogrulamasizdir ve yolu
            # dogrudan istemciden alir. "dist / full_path" tek basina guvenli
            # DEGILDIR -- pathlib'de mutlak bir parca birlesimi tamamen
            # degistirir ("C:/Windows/win.ini" dist'i yok sayar), "../.."
            # ise agactan disari cikar. Kontrolsuz birakilirsa istatik dosya
            # sunucusu keyfi dosya okuma acigina donusur.
            try:
                candidate = (dist / full_path).resolve()
            except (OSError, ValueError):
                return FileResponse(dist / "index.html")
            if candidate.is_relative_to(dist_kok) and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(dist / "index.html")
    else:

        @app.get("/", tags=["system"], include_in_schema=False)
        async def _root() -> dict[str, str]:
            return {
                "app": settings.app_name,
                "version": settings.app_version,
                "docs": "/docs",
                "api": settings.api_prefix,
                "note": "Frontend not built yet — run 'npm run build' in frontend/",
            }

    return app


app = create_app()


def _uploads_dir() -> Path:
    p = PROJECT_ROOT / "data" / "uploads"
    p.mkdir(parents=True, exist_ok=True)
    return p
