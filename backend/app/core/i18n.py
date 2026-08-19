"""
Backend internationalisation (Turkish / English).

Message catalogues are JSON files under ``backend/app/locales/``.  Nothing
user-facing is hard-coded in Python: services raise i18n *keys* and the API
layer renders them in the caller's language.

Language resolution order:
    1. explicit ``lang`` argument
    2. ``?lang=`` query parameter        (handled in the API layer)
    3. ``Accept-Language`` header        (handled in the API layer)
    4. ``settings.default_language``
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from app.core.config import APP_ROOT, settings

LOCALES_DIR: Path = APP_ROOT / "locales"
SUPPORTED: tuple[str, ...] = ("tr", "en")

_catalogues: dict[str, dict[str, str]] = {}
_lock = threading.Lock()


def _flatten(obj: Any, prefix: str = "") -> dict[str, str]:
    """Turn nested JSON into dotted keys: {"a": {"b": "x"}} -> {"a.b": "x"}."""
    out: dict[str, str] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(_flatten(v, f"{prefix}{k}."))
    else:
        out[prefix.rstrip(".")] = str(obj)
    return out


def _load(lang: str) -> dict[str, str]:
    path = LOCALES_DIR / f"{lang}.json"
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            return _flatten(json.load(fh))
    except Exception:
        return {}


def load_catalogues(force: bool = False) -> None:
    """Load (or reload) every message catalogue into memory."""
    with _lock:
        if _catalogues and not force:
            return
        _catalogues.clear()
        for lang in SUPPORTED:
            _catalogues[lang] = _load(lang)


def normalize_language(lang: str | None) -> str:
    """Map any incoming language tag onto a supported catalogue."""
    if not lang:
        return settings.default_language
    tag = lang.strip().lower().replace("_", "-").split(",")[0].split(";")[0]
    base = tag.split("-")[0]
    if base in SUPPORTED:
        return base
    return settings.default_language


def t(key: str, lang: str | None = None, /, **params: Any) -> str:
    """
    Translate *key* into *lang*.

    Falls back through: requested language -> Turkish -> English -> the key
    itself, so a missing translation degrades to something diagnosable instead
    of crashing.
    """
    load_catalogues()
    language = normalize_language(lang)

    template = (
        _catalogues.get(language, {}).get(key)
        or _catalogues.get("tr", {}).get(key)
        or _catalogues.get("en", {}).get(key)
        or key
    )
    if params:
        try:
            return template.format(**params)
        except (KeyError, IndexError, ValueError):
            return template
    return template


def has_key(key: str, lang: str | None = None) -> bool:
    load_catalogues()
    return key in _catalogues.get(normalize_language(lang), {})


def catalogue(lang: str | None = None) -> dict[str, str]:
    """Full flattened catalogue — served to the frontend on demand."""
    load_catalogues()
    return dict(_catalogues.get(normalize_language(lang), {}))


def missing_keys() -> dict[str, list[str]]:
    """Keys present in one catalogue but absent from another (QA helper)."""
    load_catalogues()
    tr = set(_catalogues.get("tr", {}))
    en = set(_catalogues.get("en", {}))
    return {
        "missing_in_en": sorted(tr - en),
        "missing_in_tr": sorted(en - tr),
    }


__all__ = [
    "t",
    "has_key",
    "catalogue",
    "normalize_language",
    "load_catalogues",
    "missing_keys",
    "SUPPORTED",
]
