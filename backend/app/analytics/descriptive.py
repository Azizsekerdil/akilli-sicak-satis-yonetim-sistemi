"""
Descriptive statistics.

Every helper is total: an empty list, a single observation or a list full of
``None`` never raises.  Dashboards call these on whatever the database happens
to return, and a statistics screen that 500s because a salesperson sold nothing
yesterday is worse than one that shows zeros.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import Any

import numpy as np

__all__ = [
    "to_array",
    "summary",
    "histogram",
    "percentile",
    "mode",
    "SUMMARY_KEYS",
]

#: Order matters — the statistics screen renders the keys in this order.
SUMMARY_KEYS: tuple[str, ...] = (
    "count", "sum", "mean", "median", "mode", "std", "variance",
    "min", "max", "q1", "q3", "iqr", "p90", "p95", "cv",
)


def to_array(values: Iterable[Any] | None) -> np.ndarray:
    """
    Coerce anything iterable into a clean float array.

    Decimals, strings and ``None`` all arrive here from the ORM; non-finite and
    unparseable entries are dropped rather than poisoning every downstream
    statistic with ``nan``.
    """
    if values is None:
        return np.empty(0, dtype="float64")
    out: list[float] = []
    for raw in values:
        if raw is None or isinstance(raw, bool):
            continue
        try:
            num = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(num):
            out.append(num)
    return np.asarray(out, dtype="float64")


def _empty_summary() -> dict[str, float]:
    return {key: 0.0 for key in SUMMARY_KEYS} | {"count": 0}


def mode(values: Iterable[Any] | None, *, decimals: int = 4) -> float:
    """
    Most frequent value.

    Money and quantities are continuous, so exact equality almost never repeats;
    values are rounded to *decimals* before counting.  When nothing repeats at
    all the mode carries no information — the median is returned instead, which
    is what a reader actually wants to see in that cell.
    """
    arr = to_array(values)
    if arr.size == 0:
        return 0.0
    rounded = np.round(arr, decimals)
    uniques, counts = np.unique(rounded, return_counts=True)
    top = int(counts.max())
    if top <= 1:
        return float(np.median(arr))
    return float(uniques[int(counts.argmax())])


def percentile(values: Iterable[Any] | None, p: float) -> float:
    """Linear-interpolated percentile; *p* is 0-100 and is clamped into range."""
    arr = to_array(values)
    if arr.size == 0:
        return 0.0
    return float(np.percentile(arr, min(100.0, max(0.0, float(p)))))


def summary(values: Iterable[Any] | None) -> dict[str, float]:
    """
    Full descriptive summary of one series.

    ``std``/``variance`` are the *sample* statistics (ddof=1) because business
    series are samples of an ongoing process, not a closed population.  With a
    single observation both are 0.0 rather than ``nan``.

    ``cv`` is the coefficient of variation as a percentage — the key number for
    van loading: a SKU with cv > 100% is erratic and needs a safety buffer,
    while cv < 30% can be loaded close to its mean demand.
    """
    arr = to_array(values)
    if arr.size == 0:
        return _empty_summary()

    mean = float(arr.mean())
    if arr.size == 1:
        std = 0.0
        variance = 0.0
    else:
        std = float(arr.std(ddof=1))
        variance = float(arr.var(ddof=1))

    q1 = float(np.percentile(arr, 25))
    q3 = float(np.percentile(arr, 75))
    return {
        "count": int(arr.size),
        "sum": float(arr.sum()),
        "mean": mean,
        "median": float(np.median(arr)),
        "mode": mode(arr),
        "std": std,
        "variance": variance,
        "min": float(arr.min()),
        "max": float(arr.max()),
        "q1": q1,
        "q3": q3,
        "iqr": q3 - q1,
        "p90": float(np.percentile(arr, 90)),
        "p95": float(np.percentile(arr, 95)),
        "cv": (std / abs(mean) * 100.0) if mean else 0.0,
    }


def histogram(values: Iterable[Any] | None, bins: int = 10) -> dict[str, Any]:
    """
    Equal-width histogram ready for charting.

    Returns ``{"bins": [{lower, upper, count, label, share_percent}], "total",
    "bin_width"}``.  A degenerate series (all values identical) still produces
    one usable bin instead of a zero-width division.
    """
    arr = to_array(values)
    count = int(arr.size)
    if count == 0:
        return {"bins": [], "total": 0, "bin_width": 0.0}

    n_bins = max(1, min(int(bins or 10), 100))
    low = float(arr.min())
    high = float(arr.max())
    if math.isclose(low, high):
        high = low + 1.0
        n_bins = 1

    counts, edges = np.histogram(arr, bins=n_bins, range=(low, high))
    width = float(edges[1] - edges[0]) if len(edges) > 1 else 0.0
    out: list[dict[str, Any]] = []
    for i, c in enumerate(counts):
        lower = float(edges[i])
        upper = float(edges[i + 1])
        out.append(
            {
                "lower": lower,
                "upper": upper,
                "count": int(c),
                "label": f"{lower:,.2f} - {upper:,.2f}",
                "share_percent": round(int(c) / count * 100.0, 2),
            }
        )
    return {"bins": out, "total": count, "bin_width": width}


def zeros_share(values: Iterable[Any] | None) -> float:
    """
    Fraction of the series that is exactly zero, 0.0-1.0.

    Drives the intermittent-demand branch of the forecaster: FMCG SKUs that a
    route only sells twice a week must not be modelled with a smooth method.
    """
    arr = to_array(values)
    if arr.size == 0:
        return 0.0
    return float(np.count_nonzero(arr == 0.0) / arr.size)


def normalise_weights(values: Sequence[float]) -> list[float]:
    """Scale a non-negative vector so it sums to 1.0 (all-zero stays all-zero)."""
    arr = to_array(values)
    total = float(arr.sum())
    if arr.size == 0 or total <= 0:
        return [0.0] * len(list(values))
    return [float(v / total) for v in arr]
