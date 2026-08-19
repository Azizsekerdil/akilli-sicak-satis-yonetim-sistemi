"""
Least-squares regression.

Implemented on numpy alone — no statsmodels/scikit-learn dependency — so the
significance figure is deliberately called ``p_hint``: it is a normal
approximation to the t-test, good enough to separate "clearly meaningful" from
"could easily be noise", and not to be quoted as a published p-value.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.analytics.descriptive import to_array

__all__ = ["LinearFit", "MultipleFit", "linear_regression", "multiple_regression"]


def _normal_two_sided_p(t_stat: float) -> float:
    """Two-sided tail probability of a standard normal — the t-test stand-in."""
    if not math.isfinite(t_stat):
        return 1.0
    return round(float(math.erfc(abs(t_stat) / math.sqrt(2.0))), 6)


@dataclass(slots=True)
class LinearFit:
    """
    Result of a simple ``y = slope·x + intercept`` fit.

    Supports both attribute and mapping access so callers can treat it as the
    dict the API returns without a conversion step.
    """

    slope: float = 0.0
    intercept: float = 0.0
    r_squared: float = 0.0
    p_hint: float = 1.0
    std_error: float = 0.0
    n: int = 0

    def predict(self, x: Any) -> Any:
        """Predict one value, or a list of them when given a sequence."""
        if isinstance(x, (list, tuple, np.ndarray)):
            return [self.slope * float(v) + self.intercept for v in x]
        return self.slope * float(x) + self.intercept

    def __getitem__(self, key: str) -> Any:
        if key == "predict":
            return self.predict
        return getattr(self, key)

    def as_dict(self) -> dict[str, Any]:
        return {
            "slope": self.slope,
            "intercept": self.intercept,
            "r_squared": self.r_squared,
            "p_hint": self.p_hint,
            "std_error": self.std_error,
            "n": self.n,
            "is_significant": self.p_hint < 0.05,
        }


@dataclass(slots=True)
class MultipleFit:
    """Result of a multivariate fit: one coefficient per input column."""

    coefficients: list[float] = field(default_factory=list)
    intercept: float = 0.0
    r_squared: float = 0.0
    adjusted_r_squared: float = 0.0
    n: int = 0
    k: int = 0
    residual_std: float = 0.0
    rank_deficient: bool = False

    def predict(self, row: Sequence[Any]) -> float:
        values = to_array(row)
        if values.size != len(self.coefficients):
            return self.intercept
        return float(np.dot(values, np.asarray(self.coefficients)) + self.intercept)

    def __getitem__(self, key: str) -> Any:
        if key == "predict":
            return self.predict
        return getattr(self, key)

    def as_dict(self) -> dict[str, Any]:
        return {
            "coefficients": self.coefficients,
            "intercept": self.intercept,
            "r_squared": self.r_squared,
            "adjusted_r_squared": self.adjusted_r_squared,
            "n": self.n,
            "k": self.k,
            "residual_std": self.residual_std,
            "rank_deficient": self.rank_deficient,
        }


def linear_regression(x: Sequence[Any], y: Sequence[Any]) -> LinearFit:
    """
    Ordinary least squares on one predictor.

    With fewer than three usable pairs, or a constant predictor, an all-zero fit
    is returned instead of raising — the caller is a dashboard, not a notebook.
    """
    xs = to_array(x)
    ys = to_array(y)
    n = min(xs.size, ys.size)
    xs, ys = xs[:n], ys[:n]
    if n < 3:
        return LinearFit(n=int(n), intercept=float(ys.mean()) if n else 0.0)

    x_mean = float(xs.mean())
    y_mean = float(ys.mean())
    sxx = float(((xs - x_mean) ** 2).sum())
    if sxx == 0:
        return LinearFit(n=int(n), intercept=y_mean)

    slope = float(((xs - x_mean) * (ys - y_mean)).sum() / sxx)
    intercept = y_mean - slope * x_mean
    residuals = ys - (slope * xs + intercept)
    ss_res = float((residuals**2).sum())
    ss_tot = float(((ys - y_mean) ** 2).sum())
    r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    dof = n - 2
    resid_var = ss_res / dof if dof > 0 else 0.0
    std_error = math.sqrt(resid_var / sxx) if sxx > 0 and resid_var > 0 else 0.0
    p_hint = _normal_two_sided_p(slope / std_error) if std_error > 0 else (0.0 if slope else 1.0)

    return LinearFit(
        slope=round(slope, 6),
        intercept=round(intercept, 6),
        r_squared=round(max(0.0, min(1.0, r_squared)), 4),
        p_hint=p_hint,
        std_error=round(std_error, 6),
        n=int(n),
    )


def multiple_regression(x_matrix: Sequence[Sequence[Any]], y: Sequence[Any]) -> MultipleFit:
    """
    Multivariate OLS via ``np.linalg.lstsq``.

    ``lstsq`` returns the minimum-norm solution for singular/collinear designs
    instead of raising, which matters here: two sales drivers (visits and
    orders) are frequently near-collinear and a hard failure would take the
    statistics screen down.  Rank deficiency is reported rather than hidden.
    """
    rows = [to_array(row) for row in (x_matrix or [])]
    ys = to_array(y)
    n = min(len(rows), ys.size)
    if n == 0:
        return MultipleFit()

    width = min((row.size for row in rows[:n]), default=0)
    if width == 0:
        return MultipleFit(n=int(n), intercept=float(ys[:n].mean()))

    design = np.column_stack(
        [np.asarray([row[:width] for row in rows[:n]], dtype="float64"), np.ones(n)]
    )
    target = ys[:n]

    try:
        solution, _, rank, _ = np.linalg.lstsq(design, target, rcond=None)
    except np.linalg.LinAlgError:
        return MultipleFit(n=int(n), k=int(width), intercept=float(target.mean()), rank_deficient=True)

    coefficients = [round(float(c), 6) for c in solution[:-1]]
    intercept = float(solution[-1])
    predicted = design @ solution
    residuals = target - predicted
    ss_res = float((residuals**2).sum())
    ss_tot = float(((target - target.mean()) ** 2).sum())
    r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    dof = n - width - 1
    if dof > 0 and ss_tot > 0:
        adjusted = 1.0 - (1.0 - r_squared) * (n - 1) / dof
    else:
        adjusted = r_squared
    residual_std = math.sqrt(ss_res / dof) if dof > 0 and ss_res > 0 else 0.0

    return MultipleFit(
        coefficients=coefficients,
        intercept=round(intercept, 6),
        r_squared=round(max(0.0, min(1.0, r_squared)), 4),
        adjusted_r_squared=round(max(-1.0, min(1.0, adjusted)), 4),
        n=int(n),
        k=int(width),
        residual_std=round(residual_std, 6),
        rank_deficient=bool(rank < design.shape[1]),
    )
