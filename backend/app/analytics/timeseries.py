"""
Time-series utilities: bucketing, smoothing, growth, trend and decomposition.

Gap filling is the important part.  A route that sells nothing on Tuesday
produces *no* row in SQL, and if that missing Tuesday is silently skipped every
downstream statistic (mean demand, weekday seasonality, forecast) is biased
upwards.  :func:`resample` therefore materialises every bucket in the range and
fills the holes with zero.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import numpy as np

from app.analytics.descriptive import to_array
from app.core.utils import add_months

__all__ = [
    "Bucket",
    "GRANULARITIES",
    "bucket_start",
    "bucket_label",
    "next_bucket",
    "resample",
    "moving_average",
    "ewma",
    "cumulative",
    "growth",
    "period_over_period",
    "trend",
    "seasonality",
    "decompose",
    "seasonal_period_for",
]

GRANULARITIES: tuple[str, ...] = ("DAILY", "WEEKLY", "MONTHLY", "QUARTERLY", "YEARLY")

#: Natural seasonal cycle length per granularity (weekday cycle, month cycle…).
_DEFAULT_PERIOD: dict[str, int] = {
    "DAILY": 7,
    "WEEKLY": 4,
    "MONTHLY": 12,
    "QUARTERLY": 4,
    "YEARLY": 1,
}


@dataclass(slots=True)
class Bucket:
    """One resampled period: its start date, a display label and the total."""

    bucket_date: date
    label: str
    value: float

    def as_dict(self) -> dict[str, Any]:
        return {"bucket_date": self.bucket_date, "label": self.label, "value": self.value}


def _norm(granularity: str | None) -> str:
    g = (granularity or "DAILY").strip().upper()
    return g if g in GRANULARITIES else "DAILY"


def seasonal_period_for(granularity: str | None) -> int:
    """Cycle length to use when the caller does not specify one."""
    return _DEFAULT_PERIOD[_norm(granularity)]


def bucket_start(day: date, granularity: str = "DAILY") -> date:
    """Snap a date back to the first day of its bucket."""
    g = _norm(granularity)
    if g == "DAILY":
        return day
    if g == "WEEKLY":                      # ISO weeks start on Monday
        return day - timedelta(days=day.weekday())
    if g == "MONTHLY":
        return day.replace(day=1)
    if g == "QUARTERLY":
        return day.replace(month=((day.month - 1) // 3) * 3 + 1, day=1)
    return day.replace(month=1, day=1)


def bucket_label(day: date, granularity: str = "DAILY") -> str:
    """Human-readable bucket label ("2026-W07", "2026-Q1", …)."""
    g = _norm(granularity)
    if g == "DAILY":
        return day.isoformat()
    if g == "WEEKLY":
        return f"{day.isocalendar().year}-W{day.isocalendar().week:02d}"
    if g == "MONTHLY":
        return f"{day.year}-{day.month:02d}"
    if g == "QUARTERLY":
        return f"{day.year}-Q{(day.month - 1) // 3 + 1}"
    return str(day.year)


def next_bucket(day: date, granularity: str = "DAILY") -> date:
    """Start of the bucket following the one that *day* starts."""
    g = _norm(granularity)
    if g == "DAILY":
        return day + timedelta(days=1)
    if g == "WEEKLY":
        return day + timedelta(days=7)
    if g == "MONTHLY":
        return add_months(day.replace(day=1), 1)
    if g == "QUARTERLY":
        return add_months(day.replace(day=1), 3)
    return day.replace(month=1, day=1).replace(year=day.year + 1)


def resample(
    rows: Iterable[tuple[date, Any]],
    granularity: str = "DAILY",
    *,
    start: date | None = None,
    end: date | None = None,
) -> list[Bucket]:
    """
    Aggregate ``(date, value)`` pairs into ordered, gap-free buckets.

    *start*/*end* widen the range beyond the observed data — pass them when the
    report window is fixed (e.g. "last 30 days") so leading and trailing days
    with no sales still appear as zeros.
    """
    g = _norm(granularity)
    totals: dict[date, float] = {}
    for raw_day, raw_value in rows:
        if raw_day is None:
            continue
        day = raw_day.date() if hasattr(raw_day, "date") and not isinstance(raw_day, date) else raw_day
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        key = bucket_start(day, g)
        totals[key] = totals.get(key, 0.0) + value

    first = bucket_start(start, g) if start else (min(totals) if totals else None)
    last = bucket_start(end, g) if end else (max(totals) if totals else None)
    if first is None or last is None:
        return []
    if totals:
        first = min(first, min(totals))
        last = max(last, max(totals))

    out: list[Bucket] = []
    cursor = first
    # Hard stop: guards against a corrupt date making this loop unbounded.
    for _ in range(10_000):
        out.append(Bucket(cursor, bucket_label(cursor, g), totals.get(cursor, 0.0)))
        if cursor >= last:
            break
        cursor = next_bucket(cursor, g)
    return out


def moving_average(
    series: Sequence[float], window: int, *, center: bool = False
) -> list[float | None]:
    """
    Simple moving average; positions without a full window stay ``None``.

    With *center* and an even *window* the classic 2×M average is used (half
    weight on both end points) so the result stays aligned with the original
    index — that alignment is what makes :func:`seasonality` unbiased.
    """
    values = to_array(series)
    n = values.size
    w = max(1, int(window))
    if n == 0 or w > n:
        return [None] * n

    out: list[float | None] = [None] * n
    if not center:
        csum = np.cumsum(np.insert(values, 0, 0.0))
        for i in range(w - 1, n):
            out[i] = float((csum[i + 1] - csum[i + 1 - w]) / w)
        return out

    if w % 2 == 1:
        half = w // 2
        for i in range(half, n - half):
            out[i] = float(values[i - half : i + half + 1].mean())
        return out

    half = w // 2
    weights = np.ones(w + 1)
    weights[0] = weights[-1] = 0.5
    weights /= w
    for i in range(half, n - half):
        out[i] = float(np.dot(values[i - half : i + half + 1], weights))
    return out


def ewma(series: Sequence[float], alpha: float = 0.3) -> list[float]:
    """Exponentially weighted moving average; *alpha* is clamped to (0, 1]."""
    values = to_array(series)
    if values.size == 0:
        return []
    a = min(1.0, max(1e-6, float(alpha)))
    level = float(values[0])
    out = [level]
    for value in values[1:]:
        level = a * float(value) + (1 - a) * level
        out.append(level)
    return out


def cumulative(series: Sequence[float]) -> list[float]:
    """Running total."""
    values = to_array(series)
    return [float(v) for v in np.cumsum(values)] if values.size else []


def period_over_period(current: Any, previous: Any) -> float:
    """
    Percentage change between two periods (WoW / MoM / YoY).

    Growth from a zero base is reported as +100% rather than infinity, which is
    what a dashboard can actually render.
    """
    try:
        cur = float(current)
        prev = float(previous)
    except (TypeError, ValueError):
        return 0.0
    if prev == 0:
        if cur == 0:
            return 0.0
        return 100.0 if cur > 0 else -100.0
    return round((cur - prev) / abs(prev) * 100.0, 2)


def growth(series: Sequence[float], lag: int = 1) -> list[float | None]:
    """
    Percentage change against the bucket *lag* positions earlier.

    ``lag=1`` on a monthly series is MoM; ``lag=7`` on a daily series compares
    like weekdays; ``lag=12`` on a monthly series is YoY.
    """
    values = to_array(series)
    k = max(1, int(lag))
    out: list[float | None] = [None] * values.size
    for i in range(k, values.size):
        out[i] = period_over_period(values[i], values[i - k])
    return out


def trend(series: Sequence[float]) -> dict[str, Any]:
    """
    Least-squares straight line through the series.

    ``direction`` is derived from the slope *relative to the mean level* so a
    slope of +3 TRY/day on a 20 000 TRY/day route is correctly reported as flat
    instead of "rising".
    """
    values = to_array(series)
    n = values.size
    if n < 2:
        return {
            "slope": 0.0, "intercept": float(values[0]) if n else 0.0,
            "r_squared": 0.0, "direction": "flat", "n": int(n),
        }

    x = np.arange(n, dtype="float64")
    x_mean = float(x.mean())
    y_mean = float(values.mean())
    denom = float(((x - x_mean) ** 2).sum())
    if denom == 0:
        return {"slope": 0.0, "intercept": y_mean, "r_squared": 0.0, "direction": "flat", "n": int(n)}

    slope = float(((x - x_mean) * (values - y_mean)).sum() / denom)
    intercept = y_mean - slope * x_mean
    ss_tot = float(((values - y_mean) ** 2).sum())
    ss_res = float(((values - (slope * x + intercept)) ** 2).sum())
    r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    scale = abs(y_mean) if y_mean else (float(np.abs(values).max()) or 1.0)
    relative = slope * n / scale if scale else 0.0
    if relative > 0.05:
        direction = "up"
    elif relative < -0.05:
        direction = "down"
    else:
        direction = "flat"

    return {
        "slope": round(float(slope), 6),
        "intercept": round(float(intercept), 6),
        "r_squared": round(max(0.0, min(1.0, float(r_squared))), 4),
        "direction": direction,
        "n": int(n),
    }


def seasonality(series: Sequence[float], period: int = 7) -> list[float]:
    """
    Seasonal indices: the average ratio of each position to the centred moving
    average of its own cycle.

    An index of 1.30 for Friday means Fridays run 30% above the local trend —
    exactly the multiplier van loading needs.  Indices are normalised to average
    1.0 so applying them cannot inflate or deflate the overall level.
    """
    values = to_array(series)
    p = max(1, int(period))
    if p == 1 or values.size < 2 * p:
        return [1.0] * p

    centred = moving_average(values, p, center=True)
    ratios: list[list[float]] = [[] for _ in range(p)]
    for i, base in enumerate(centred):
        if base is None or base == 0:
            continue
        ratios[i % p].append(float(values[i]) / float(base))

    indices: list[float] = []
    for slot in ratios:
        indices.append(float(np.median(slot)) if slot else 1.0)

    mean_index = float(np.mean(indices)) if indices else 0.0
    if mean_index > 0:
        indices = [round(v / mean_index, 6) for v in indices]
    else:
        indices = [1.0] * p
    return indices


def decompose(series: Sequence[float], period: int = 7) -> dict[str, list[float | None]]:
    """
    Additive decomposition into trend, seasonal and residual components.

    Additive (not multiplicative) because FMCG daily series legitimately hit
    zero, and a multiplicative model is undefined there.  Trend is ``None`` at
    the edges where the centred window does not fit, and the residual follows.
    """
    values = to_array(series)
    n = values.size
    p = max(1, int(period))
    if n == 0:
        return {"trend": [], "seasonal": [], "residual": []}

    trend_line = moving_average(values, p, center=True) if n >= p else [None] * n

    # Seasonal component = average detrended deviation for each slot in the cycle.
    deviations: list[list[float]] = [[] for _ in range(p)]
    for i, base in enumerate(trend_line):
        if base is None:
            continue
        deviations[i % p].append(float(values[i]) - float(base))
    slot_means = [float(np.mean(d)) if d else 0.0 for d in deviations]
    overall = float(np.mean(slot_means)) if slot_means else 0.0
    slot_means = [m - overall for m in slot_means]          # centre on zero

    seasonal: list[float | None] = [slot_means[i % p] for i in range(n)]
    residual: list[float | None] = []
    for i in range(n):
        base = trend_line[i]
        if base is None:
            residual.append(None)
        else:
            residual.append(float(values[i]) - float(base) - float(seasonal[i] or 0.0))

    return {
        "trend": [None if v is None else round(float(v), 6) for v in trend_line],
        "seasonal": [None if v is None else round(float(v), 6) for v in seasonal],
        "residual": [None if v is None else round(float(v), 6) for v in residual],
    }
