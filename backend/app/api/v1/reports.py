"""
Report endpoints.

``GET /reports``                  — the catalogue the report picker renders
``POST /reports/{key}/run``       — execute and return JSON
``POST /reports/{key}/export``    — execute and stream Excel / PDF / CSV / JSON

Authorisation is per report: each definition declares the ``resource:action``
it needs, so the receivable ageing report is gated on ``crm.ledger`` while the
van stock report is gated on ``stock.vehicle_stock``.  Exports additionally
require the EXPORT action on ``analytics.reports``.
"""

from __future__ import annotations

import io
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from app.core.deps import Context, Ctx, require
from app.core.exceptions import PermissionDeniedError
from app.reports import engine
from app.schemas.report import (
    ExportIn,
    ReportDefOut,
    ReportModuleGroupOut,
    ReportResultOut,
    ReportRunIn,
)
from app.services import report_service

router = APIRouter(prefix="/reports", tags=["reports"])


def _authorise(ctx: Ctx, rdef: engine.ReportDef) -> None:
    """Enforce the report's own permission on top of the module gate."""
    resource, action = rdef.permission
    if not ctx.can(resource, action):
        raise PermissionDeniedError(
            "auth.permission_denied", params={"resource": resource, "action": action}
        )


def _scope(ctx: Ctx) -> engine.ReportScope:
    return engine.ReportScope(
        unrestricted=ctx.unrestricted,
        salesperson_ids=tuple(ctx.salesperson_ids),
        region_ids=tuple(ctx.region_ids),
        user_id=ctx.user_id,
    )


@router.get(
    "",
    response_model=list[ReportDefOut],
    summary="Report catalogue / Rapor listesi",
)
def list_reports(
    ctx: Ctx = Depends(require("analytics.reports", "VIEW")),
    module: str | None = Query(default=None, description="Filter by module"),
) -> list[ReportDefOut]:
    """Every report the caller is allowed to run, with columns and filters."""
    items = report_service.list_reports(ctx.lang, allowed=ctx.can)
    if module:
        items = [i for i in items if i["module"] == module]
    return [ReportDefOut.model_validate(i) for i in items]


@router.get(
    "/groups",
    response_model=list[ReportModuleGroupOut],
    summary="Report catalogue grouped by module",
)
def list_report_groups(
    ctx: Ctx = Depends(require("analytics.reports", "VIEW")),
) -> list[ReportModuleGroupOut]:
    return [
        ReportModuleGroupOut.model_validate(group)
        for group in report_service.module_groups(ctx.lang, allowed=ctx.can)
    ]


@router.get(
    "/{key}",
    response_model=ReportDefOut,
    summary="One report definition",
)
def get_report_definition(
    key: str,
    ctx: Ctx = Depends(require("analytics.reports", "VIEW")),
) -> ReportDefOut:
    rdef = report_service.get_definition(key)
    _authorise(ctx, rdef)
    item = rdef.as_dict(ctx.lang)
    item["formats"] = list(report_service.EXPORT_FORMATS)
    return ReportDefOut.model_validate(item)


@router.post(
    "/{key}/run",
    response_model=ReportResultOut,
    summary="Run a report / Raporu çalıştır",
)
def run_report(
    key: str,
    payload: ReportRunIn | None = None,
    ctx: Ctx = Depends(require("analytics.reports", "VIEW")),
) -> ReportResultOut:
    """Execute the report within the caller's data scope and return the rows."""
    rdef = report_service.get_definition(key)
    _authorise(ctx, rdef)
    result = report_service.run_report(
        ctx.db,
        key,
        (payload.params if payload else {}),
        scope=_scope(ctx),
        lang=ctx.lang,
    )
    return ReportResultOut.model_validate(result)


@router.post(
    "/{key}/export",
    summary="Export a report / Raporu dışa aktar",
    response_class=StreamingResponse,
)
def export_report(
    key: str,
    payload: ExportIn,
    ctx: Ctx = Depends(require("analytics.reports", "EXPORT")),
) -> StreamingResponse:
    """
    Stream the report as Excel, PDF, CSV or JSON.

    Streaming (rather than returning bytes) keeps memory flat for large
    exports and lets the browser start the download immediately.
    """
    rdef = report_service.get_definition(key)
    _authorise(ctx, rdef)

    data, media_type, filename = report_service.export_report(
        ctx.db,
        key,
        payload.params,
        fmt=payload.format,
        scope=_scope(ctx),
        lang=ctx.lang,
        audit=ctx.audit_kwargs(),
    )
    headers: dict[str, Any] = {
        "Content-Disposition": report_service.content_disposition(filename),
        "Content-Length": str(len(data)),
        "X-Report-Key": rdef.key,
    }
    return StreamingResponse(io.BytesIO(data), media_type=media_type, headers=headers)


@router.get(
    "/{key}/quick",
    response_model=ReportResultOut,
    summary="Run a report with query-string filters",
)
def run_report_quick(
    key: str,
    ctx: Context,
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
    salesperson_id: int | None = Query(default=None),
    customer_id: int | None = Query(default=None),
    region_id: int | None = Query(default=None),
    warehouse_id: int | None = Query(default=None),
    product_id: int | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=5000),
) -> ReportResultOut:
    """
    Convenience GET for dashboards and links that cannot POST a body.

    Same authorisation as ``/run``; only the common filters are exposed.
    """
    ctx.check("analytics.reports", "VIEW")
    rdef = report_service.get_definition(key)
    _authorise(ctx, rdef)

    params = {
        "start": start,
        "end": end,
        "salesperson_id": salesperson_id,
        "customer_id": customer_id,
        "region_id": region_id,
        "warehouse_id": warehouse_id,
        "product_id": product_id,
        "limit": limit,
    }
    result = report_service.run_report(
        ctx.db,
        key,
        {k: v for k, v in params.items() if v is not None},
        scope=_scope(ctx),
        lang=ctx.lang,
    )
    return ReportResultOut.model_validate(result)
