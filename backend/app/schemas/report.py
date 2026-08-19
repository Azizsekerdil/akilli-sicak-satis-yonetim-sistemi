"""Pydantic schemas for the report catalogue, execution and export."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class ReportColumnOut(ORMModel):
    """One column of a report result — everything the UI needs to render it."""

    key: str
    label: str | None = None
    label_tr: str
    label_en: str
    type: str = "text"
    width: int = 16
    align: str = "left"


class ReportFilterOut(ORMModel):
    key: str
    label: str | None = None
    label_tr: str
    label_en: str
    type: str = "date"
    required: bool = False
    default: Any = None
    #: Option list the UI should load ("customers", "regions", …).
    source: str | None = None


class ReportPermissionOut(BaseModel):
    resource: str
    action: str


class ReportDefOut(ORMModel):
    key: str
    title: str
    title_tr: str
    title_en: str
    description: str | None = None
    module: str
    columns: list[ReportColumnOut] = Field(default_factory=list)
    filters: list[ReportFilterOut] = Field(default_factory=list)
    group_by: str | None = None
    totals: list[str] = Field(default_factory=list)
    permission: ReportPermissionOut
    formats: list[str] = Field(default_factory=list)


class ReportModuleGroupOut(BaseModel):
    module: str
    reports: list[ReportDefOut] = Field(default_factory=list)


class ReportRunIn(BaseModel):
    """Parameters for one run — keys must match the report's declared filters."""

    params: dict[str, Any] = Field(default_factory=dict)


class ReportMetaOut(BaseModel):
    key: str
    title: str
    module: str
    group_by: str | None = None
    row_count: int = 0
    generated_at: str
    language: str = "tr"
    currency: str = "TRY"
    params: dict[str, Any] = Field(default_factory=dict)
    start: str | None = None
    end: str | None = None
    #: True when the caller's data scope narrowed the result.
    restricted: bool = False


class ReportResultOut(BaseModel):
    columns: list[ReportColumnOut] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    totals: dict[str, Any] = Field(default_factory=dict)
    meta: ReportMetaOut


class ExportIn(BaseModel):
    format: Literal["excel", "xlsx", "pdf", "csv", "json"] = "excel"
    params: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "ExportIn",
    "ReportColumnOut",
    "ReportDefOut",
    "ReportFilterOut",
    "ReportMetaOut",
    "ReportModuleGroupOut",
    "ReportPermissionOut",
    "ReportResultOut",
    "ReportRunIn",
]
