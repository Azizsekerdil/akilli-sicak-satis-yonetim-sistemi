"""
Outlier and change-point detection.

Four complementary detectors, because business data breaks in four different
ways: a single freak value (z-score), a skewed distribution where the mean is
already contaminated (IQR), a permanent level shift such as a customer moving to
a competitor (CUSUM), and a value that is only odd *for that weekday*
(seasonal residuals).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import numpy as np

from app.analytics import timeseries as ts
from app.analytics.descriptive import to_array
from app.core.enums import AnomalySeverity

__all__ = [
    "zscore_outliers",
    "iqr_outliers",
    "change_point",
    "seasonal_residual_outliers",
    "classify",
    "classify_z",
]


def zscore_outliers(
    values: Sequence[Any], threshold: float = 3.0
) -> list[dict[str, Any]]:
    """
    Classic z-score detection against the series mean.

    Returns one dict per flagged point: ``{index, value, expected, deviation,
    z_score, direction, severity}``.  Needs at least three points and non-zero
    spread; a flat series has no outliers by definition.
    """
    arr = to_array(values)
    if arr.size < 3:
        return []
    mean = float(arr.mean())
    std = float(arr.std(ddof=1)) if arr.size > 1 else 0.0
    if std <= 0:
        return []

    limit = abs(float(threshold))
    out: list[dict[str, Any]] = []
    for i, raw in enumerate(arr):
        value = float(raw)
        z = (value - mean) / std
        if abs(z) < limit:
            continue
        out.append(
            {
                "index": i,
                "value": value,
                "expected": mean,
                "deviation": value - mean,
                "z_score": round(z, 4),
                "direction": "high" if z > 0 else "low",
                "method": "zscore",
                "severity": classify_z(z),
            }
        )
    return out


def iqr_outliers(values: Sequence[Any], k: float = 1.5) -> list[dict[str, Any]]:
    """
    Tukey fence detection: outside ``[Q1 - k·IQR, Q3 + k·IQR]``.

    Robust where the z-score is not — a single 200 000 TRY wholesale order
    inflates the standard deviation enough to hide every other outlier, but it
    barely moves the quartiles.
    """
    arr = to_array(values)
    if arr.size < 4:
        return []
    q1 = float(np.percentile(arr, 25))
    q3 = float(np.percentile(arr, 75))
    iqr = q3 - q1
    if iqr <= 0:
        return []

    factor = abs(float(k))
    low = q1 - factor * iqr
    high = q3 + factor * iqr
    median = float(np.median(arr))

    out: list[dict[str, Any]] = []
    for i, raw in enumerate(arr):
        value = float(raw)
        if low <= value <= high:
            continue
        out.append(
            {
                "index": i,
                "value": value,
                "expected": median,
                "deviation": value - median,
                "z_score": round((value - median) / iqr, 4),
                "direction": "high" if value > high else "low",
                "method": "iqr",
                "lower_fence": round(low, 4),
                "upper_fence": round(high, 4),
                "severity": classify(value, median),
            }
        )
    return out


def change_point(
    series: Sequence[Any], *, threshold: float = 4.0, drift: float = 0.5
) -> list[dict[str, Any]]:
    """
    Two-sided CUSUM — detects a sustained shift in the level of the series.

    A z-score sees a 30% sales drop on one day; CUSUM sees that the drop never
    recovered.  The cumulative sums accumulate deviations beyond a small *drift*
    allowance (in standard deviations) and fire once they cross *threshold*,
    then reset so a long series can report several shifts.
    """
    arr = to_array(series)
    if arr.size < 6:
        return []
    mean = float(arr.mean())
    std = float(arr.std(ddof=1))
    if std <= 0:
        return []

    allowance = abs(float(drift))
    limit = abs(float(threshold))
    pos = neg = 0.0
    out: list[dict[str, Any]] = []
    for i, raw in enumerate(arr):
        z = (float(raw) - mean) / std
        pos = max(0.0, pos + z - allowance)
        neg = max(0.0, neg - z - allowance)
        if pos > limit or neg > limit:
            rising = pos > limit
            before = float(arr[:i].mean()) if i else mean
            after = float(arr[i:].mean())
            out.append(
                {
                    "index": i,
                    "value": float(raw),
                    "expected": before,
                    "deviation": after - before,
                    "direction": "up" if rising else "down",
                    "cusum": round(pos if rising else neg, 4),
                    "mean_before": round(before, 4),
                    "mean_after": round(after, 4),
                    "method": "cusum",
                    "severity": classify(after, before),
                }
            )
            pos = neg = 0.0
    return out


def seasonal_residual_outliers(
    series: Sequence[Any], period: int = 7, *, threshold: float = 2.5
) -> list[dict[str, Any]]:
    """
    Outliers in the *deseasonalised* residual.

    Saturday selling triple a Tuesday is not an anomaly, it is Saturday.  This
    detector removes the weekday pattern first and only then asks whether the
    remainder is unusual, which is the only way to catch "a quiet Saturday".
    """
    arr = to_array(series)
    p = max(2, int(period))
    if arr.size < 2 * p:
        return []

    parts = ts.decompose(arr, p)
    residuals = parts["residual"]
    usable = [(i, float(r)) for i, r in enumerate(residuals) if r is not None]
    if len(usable) < 3:
        return []

    residual_values = np.asarray([r for _, r in usable], dtype="float64")
    std = float(residual_values.std(ddof=1))
    if std <= 0:
        return []
    mean = float(residual_values.mean())

    limit = abs(float(threshold))
    out: list[dict[str, Any]] = []
    for idx, residual in usable:
        z = (residual - mean) / std
        if abs(z) < limit:
            continue
        seasonal = float(parts["seasonal"][idx] or 0.0)
        trend_value = float(parts["trend"][idx] or 0.0)
        expected = trend_value + seasonal
        out.append(
            {
                "index": idx,
                "value": float(arr[idx]),
                "expected": round(expected, 4),
                "deviation": round(float(arr[idx]) - expected, 4),
                "z_score": round(z, 4),
                "direction": "high" if z > 0 else "low",
                "method": "seasonal_residual",
                "severity": classify_z(z),
            }
        )
    return out


def classify(observed: Any, expected: Any) -> str:
    """
    Severity from the relative gap between observed and expected.

    Thresholds are business-calibrated, not statistical: a 10% miss on a route's
    daily revenue is noise, 25% is worth a look, 50% needs a call, and a
    doubling or a total stop is a critical event.
    """
    try:
        obs = float(observed)
        exp = float(expected)
    except (TypeError, ValueError):
        return str(AnomalySeverity.INFO)

    base = abs(exp)
    if base < 1e-9:
        return str(AnomalySeverity.CRITICAL if abs(obs) > 0 else AnomalySeverity.INFO)

    ratio = abs(obs - exp) / base
    if ratio >= 1.0:
        return str(AnomalySeverity.CRITICAL)
    if ratio >= 0.50:
        return str(AnomalySeverity.HIGH)
    if ratio >= 0.25:
        return str(AnomalySeverity.MEDIUM)
    if ratio >= 0.10:
        return str(AnomalySeverity.LOW)
    return str(AnomalySeverity.INFO)


def classify_z(z_score: float) -> str:
    """Severity straight from a z-score, for detectors that produce one."""
    try:
        magnitude = abs(float(z_score))
    except (TypeError, ValueError):
        return str(AnomalySeverity.INFO)
    if not math.isfinite(magnitude):
        return str(AnomalySeverity.CRITICAL)
    if magnitude >= 4.0:
        return str(AnomalySeverity.CRITICAL)
    if magnitude >= 3.0:
        return str(AnomalySeverity.HIGH)
    if magnitude >= 2.0:
        return str(AnomalySeverity.MEDIUM)
    if magnitude >= 1.5:
        return str(AnomalySeverity.LOW)
    return str(AnomalySeverity.INFO)
