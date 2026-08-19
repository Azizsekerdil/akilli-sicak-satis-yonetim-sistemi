"""
API v1 router registry.

Each feature module exposes a ``router`` object.  They are wired up here so
``app.main`` has a single include point.  Modules are imported defensively and
a missing one is logged rather than crashing the whole API — that way a broken
feature never takes the rest of the system down with it.
"""

from __future__ import annotations

import importlib
from pathlib import Path

from fastapi import APIRouter

from app.core.config import settings
from app.core.logging_config import get_logger

log = get_logger("app.api")

api_router = APIRouter()

#: (module name, human label) — order defines the OpenAPI grouping order.
_MODULES: tuple[tuple[str, str], ...] = (
    ("auth", "Authentication"),
    ("system", "System"),
    ("products", "Products"),
    ("warehouses", "Warehouses & Stock"),
    ("customers", "Customers & CRM"),
    ("vehicles", "Vehicles & Field"),
    ("sales", "Sales & Collections"),
    ("campaigns", "Campaigns & Pricing"),
    ("routes", "Routes & Visits"),
    ("analytics", "Analytics & Forecasting"),
    ("reports", "Reports"),
    ("ai", "Artificial Intelligence"),
    ("compliance", "Compliance & Human Sovereignty"),
)

REGISTERED: list[str] = []
MISSING: list[str] = []

_HERE = Path(__file__).parent


def _module_exists(name: str) -> bool:
    return (_HERE / f"{name}.py").is_file() or (_HERE / name / "__init__.py").is_file()


for _name, _label in _MODULES:
    if not _module_exists(_name):
        MISSING.append(_name)
        log.warning("API module not present yet: %s", _name)
        continue
    try:
        _mod = importlib.import_module(f"app.api.v1.{_name}")
    except Exception:
        # The file is on disk but failed to import — that is a real defect,
        # never a "not built yet".  Surface it loudly outside production.
        log.exception("Failed to load API module '%s'", _name)
        if settings.env != "production":
            raise
        MISSING.append(_name)
        continue

    _router = getattr(_mod, "router", None)
    if _router is None:
        log.error("API module '%s' has no 'router' attribute", _name)
        MISSING.append(_name)
        continue

    api_router.include_router(_router)
    REGISTERED.append(_name)

log.info("API v1 ready: %d modules registered%s",
         len(REGISTERED),
         f", missing: {MISSING}" if MISSING else "")
