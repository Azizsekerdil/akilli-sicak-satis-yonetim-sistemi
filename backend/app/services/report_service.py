"""
Report service: the layer between the API and the report engine.

Responsibilities the engine deliberately does not have: filtering the
catalogue by what the caller may see, choosing an export filename, resolving
the company name for PDF headers, and writing the ``EXPORT`` audit entry —
because "who took this data out of the system, and when" is exactly the
question an auditor asks first.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.enums import AuditAction
from app.core.exceptions import ValidationError
from app.core.logging_config import get_logger
from app.core.utils import slugify
from app.models.base import utcnow
from app.models.organization import Company
from app.reports import engine, exporters
from app.services import audit_service, setting_service

log = get_logger("app.reports")

#: Export formats offered to the UI, in the order it should show them.
EXPORT_FORMATS: tuple[str, ...] = ("excel", "pdf", "csv", "json")


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------
def list_reports(
    lang: str = "tr",
    *,
    allowed: Callable[[str, str], bool] | None = None,
) -> list[dict[str, Any]]:
    """
    Report definitions, optionally filtered by a permission predicate.

    Hiding a report the user cannot run is not security (the run endpoint
    enforces that) — it keeps the picker honest.
    """
    out: list[dict[str, Any]] = []
    for rdef in engine.REPORTS.values():
        if allowed is not None and not allowed(*rdef.permission):
            continue
        item = rdef.as_dict(lang)
        item["formats"] = list(EXPORT_FORMATS)
        out.append(item)
    return out


def get_definition(key: str) -> engine.ReportDef:
    return engine.get_report(key)


def module_groups(lang: str = "tr", *, allowed: Callable[[str, str], bool] | None = None) -> list[dict[str, Any]]:
    """The same catalogue grouped by module, for a sectioned report menu."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in list_reports(lang, allowed=allowed):
        grouped.setdefault(item["module"], []).append(item)
    return [
        {"module": module, "reports": reports}
        for module, reports in sorted(grouped.items())
    ]


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
def run_report(
    db: Session,
    key: str,
    params: dict[str, Any] | None = None,
    *,
    scope: Any = None,
    lang: str = "tr",
) -> dict[str, Any]:
    """Execute a report; ``scope`` may be a Ctx, a ReportScope or a dict."""
    return engine.run(db, key, params, ctx_scope=scope, lang=lang)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
def company_name(db: Session) -> str:
    """Company name for document headers: setting first, then the Company row."""
    configured = setting_service.get(db, "general", "company_name")
    if configured:
        return str(configured)
    company = db.execute(
        select(Company).where(Company.is_deleted.is_(False)).order_by(Company.id).limit(1)
    ).scalar_one_or_none()
    return (company.name if company else None) or settings.app_name


def build_filename(rdef: engine.ReportDef, fmt: str, *, lang: str = "tr", on: date | None = None) -> str:
    """
    ASCII-safe, sortable filename.

    Turkish characters are folded because ``Content-Disposition`` filenames
    travel through proxies and browsers that mangle non-ASCII.
    """
    stamp = (on or date.today()).strftime("%Y%m%d")
    time_part = utcnow().strftime("%H%M")
    return f"{slugify(rdef.title(lang))}_{stamp}_{time_part}.{exporters.extension_for(fmt)}"


def export_report(
    db: Session,
    key: str,
    params: dict[str, Any] | None = None,
    *,
    fmt: str = "excel",
    scope: Any = None,
    lang: str = "tr",
    audit: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
) -> tuple[bytes, str, str]:
    """
    Run (or reuse) a report and render it, returning ``(payload, media_type, filename)``.

    Every export is audited with :attr:`AuditAction.EXPORT` including the row
    count and the filters used, so a data extraction can always be traced back
    to a person, a moment and a query.
    """
    fmt_key = (fmt or "excel").lower()
    if fmt_key not in exporters.FORMATS:
        raise ValidationError("report.unsupported_format", params={"format": fmt})

    rdef = engine.get_report(key)
    payload_result = result if result is not None else run_report(
        db, key, params, scope=scope, lang=lang
    )

    subtitle_parts = [
        f"{payload_result['meta'].get('start', '')} - {payload_result['meta'].get('end', '')}"
    ]
    data, media_type = exporters.render(
        payload_result,
        fmt_key,
        title=rdef.title(lang),
        subtitle=" | ".join(x for x in subtitle_parts if x.strip(" -")),
        lang=lang,
        company=company_name(db),
    )
    filename = build_filename(rdef, fmt_key, lang=lang)

    audit_service.record(
        db,
        AuditAction.EXPORT,
        entity_type="Report",
        entity_label=rdef.key,
        summary=(
            f"exported {rdef.key} as {fmt_key} "
            f"({payload_result['meta'].get('row_count', 0)} rows, {len(data)} bytes)"
        ),
        new_values={
            "report": rdef.key,
            "format": fmt_key,
            "filename": filename,
            "row_count": payload_result["meta"].get("row_count", 0),
            "params": payload_result["meta"].get("params", {}),
        },
        commit=True,
        **(audit or {}),
    )
    log.info("Report %s exported as %s (%d bytes)", rdef.key, fmt_key, len(data))
    return data, media_type, filename


def content_disposition(filename: str) -> str:
    """
    Header value that survives non-ASCII names.

    ``filename`` stays ASCII for old clients; ``filename*`` carries the UTF-8
    original per RFC 5987 for everything modern.
    """
    from urllib.parse import quote

    ascii_name = filename.encode("ascii", "ignore").decode() or "report"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


__all__ = [
    "EXPORT_FORMATS",
    "build_filename",
    "company_name",
    "content_disposition",
    "export_report",
    "get_definition",
    "list_reports",
    "module_groups",
    "run_report",
]
