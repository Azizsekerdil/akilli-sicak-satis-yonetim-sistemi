"""Shared Pydantic schemas used across every module."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ORMModel(BaseModel):
    """Base for schemas read straight off SQLAlchemy objects."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class Message(BaseModel):
    """Simple acknowledgement payload."""

    success: bool = True
    message: str = ""
    message_key: str | None = None
    data: dict[str, Any] | None = None


class IdResponse(BaseModel):
    id: int
    code: str | None = None
    message: str = ""


class PageMeta(BaseModel):
    total: int
    page: int
    size: int
    pages: int
    has_next: bool
    has_prev: bool


class PagedResponse(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    size: int
    pages: int
    has_next: bool = False
    has_prev: bool = False


class DateRange(BaseModel):
    start: date
    end: date


class MoneyAmount(BaseModel):
    amount: Decimal = Decimal("0")
    currency: str = "TRY"


class GeoPoint(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class SelectOption(BaseModel):
    """A dropdown entry — value plus bilingual label."""

    value: str
    label_tr: str
    label_en: str

    def label(self, lang: str = "tr") -> str:
        return self.label_en if lang == "en" else self.label_tr


class KpiCard(BaseModel):
    """One dashboard tile."""

    key: str
    label_tr: str
    label_en: str
    value: Decimal | float | int = 0
    previous_value: Decimal | float | int | None = None
    change_percent: float | None = None
    unit: str | None = None
    format: str = "number"          # number | money | percent | integer
    trend: str | None = None        # up | down | flat
    severity: str | None = None     # ok | warning | critical
    icon: str | None = None


class SeriesPoint(BaseModel):
    label: str
    value: Decimal | float = 0
    secondary: Decimal | float | None = None
    #: Not named ``date`` — a field of that name would shadow the ``date`` type
    #: in its own annotation and break Pydantic's forward-reference resolution.
    bucket_date: date | None = None


class ChartSeries(BaseModel):
    key: str
    name_tr: str
    name_en: str
    chart_type: str = "line"        # line | bar | area | pie | donut
    points: list[SeriesPoint] = Field(default_factory=list)
    unit: str | None = None


class AuditStamp(ORMModel):
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by_id: int | None = None
    updated_by_id: int | None = None


class BulkResult(BaseModel):
    """Outcome of a batch operation."""

    requested: int = 0
    succeeded: int = 0
    failed: int = 0
    errors: list[dict[str, Any]] = Field(default_factory=list)
