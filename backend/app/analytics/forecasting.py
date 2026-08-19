"""
Demand forecasting for intermittent FMCG series.

Why several methods instead of one good one
-------------------------------------------
A van's demand series is not one kind of series.  A cola SKU on a busy urban
route is smooth and strongly weekday-seasonal; a 5 kg catering pack on the same
route sells three times a month and is *intermittent* — its mean is a lie and
any smoothing model will happily predict 0.3 cases a day forever.  So each
method here targets one regime, :func:`back_test` measures them on a holdout the
model never saw, and :func:`ensemble` picks (or blends) the winner per series.

Everything is hand-implemented on numpy: no prophet, statsmodels or sklearn.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np

from app.analytics import timeseries as ts
from app.analytics.descriptive import to_array, zeros_share
from app.core.enums import ForecastMethod

__all__ = [
    "moving_average_forecast",
    "seasonal_naive",
    "holt_winters",
    "croston",
    "linear_trend_forecast",
    "back_test",
    "ensemble",
    "METHODS",
    "ForecastResult",
]

#: z multiplier for the prediction interval — 1.2816 ≈ an 80% two-sided band.
#: 95% bands on intermittent FMCG demand are so wide they stop being useful for
#: loading decisions, so the narrower operational band is the default.
_Z_80 = 1.2816

#: A series with more zero buckets than this is treated as intermittent.
_INTERMITTENT_ZERO_SHARE = 0.30


@dataclass(slots=True)
class ForecastResult:
    """Outcome of :func:`ensemble` — kept as a dataclass for typed access."""

    method: str
    points: list[dict[str, Any]]
    confidence: float
    mae: float
    explanation_tr: str
    explanation_en: str
    history_points: int
    candidates: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "points": self.points,
            "confidence": self.confidence,
            "mae": self.mae,
            "explanation_tr": self.explanation_tr,
            "explanation_en": self.explanation_en,
            "history_points": self.history_points,
            "candidates": self.candidates,
        }


def _clean(series: Sequence[Any]) -> np.ndarray:
    """Float array with negatives clipped — returned goods are modelled apart."""
    values = to_array(series)
    return np.clip(values, 0.0, None) if values.size else values


def _flat(value: float, horizon: int) -> list[float]:
    return [float(max(0.0, value))] * max(0, int(horizon))


# ===========================================================================
# Methods
# ===========================================================================
def moving_average_forecast(
    series: Sequence[Any], horizon: int, *, window: int = 4, **_: Any
) -> list[float]:
    """
    Flat forecast at the mean of the last *window* buckets.

    The baseline every other method has to beat.  On short or noisy history it
    very often wins, and a forecaster that cannot admit that is overfitting.
    """
    values = _clean(series)
    if values.size == 0:
        return _flat(0.0, horizon)
    w = max(1, min(int(window), values.size))
    return _flat(float(values[-w:].mean()), horizon)


def seasonal_naive(
    series: Sequence[Any], horizon: int, *, period: int = 7, **_: Any
) -> list[float]:
    """
    Repeat the last complete cycle.

    Brutally simple and surprisingly hard to beat on route sales, where "what
    happened last Tuesday" is the single best predictor of this Tuesday.
    """
    values = _clean(series)
    p = max(1, int(period))
    if values.size == 0:
        return _flat(0.0, horizon)
    if values.size < p:
        return moving_average_forecast(values, horizon)
    cycle = values[-p:]
    return [float(max(0.0, cycle[i % p])) for i in range(max(0, int(horizon)))]


def holt_winters(
    series: Sequence[Any],
    horizon: int,
    *,
    period: int = 7,
    alpha: float = 0.30,
    beta: float = 0.10,
    gamma: float = 0.20,
    **_: Any,
) -> list[float]:
    """
    Additive Holt-Winters (level + trend + season), implemented by hand.

    Additive rather than multiplicative because FMCG buckets legitimately hit
    zero and the multiplicative update would divide by it.  Needs two full
    cycles to initialise the seasonal figures; with less history it degrades to
    the moving-average baseline instead of inventing a season.
    """
    values = _clean(series)
    p = max(1, int(period))
    n = values.size
    h = max(0, int(horizon))
    if n < 2 * p or p == 1:
        return linear_trend_forecast(values, h) if n >= 3 else moving_average_forecast(values, h)

    a = min(0.95, max(0.01, float(alpha)))
    b = min(0.95, max(0.0, float(beta)))
    g = min(0.95, max(0.0, float(gamma)))

    first_cycle = values[:p]
    second_cycle = values[p : 2 * p]
    level = float(first_cycle.mean())
    slope = float((second_cycle.mean() - first_cycle.mean()) / p)
    seasonals = [float(v - level) for v in first_cycle]

    for i in range(n):
        observed = float(values[i])
        season = seasonals[i % p]
        previous_level = level
        level = a * (observed - season) + (1 - a) * (level + slope)
        slope = b * (level - previous_level) + (1 - b) * slope
        seasonals[i % p] = g * (observed - level) + (1 - g) * season

    out: list[float] = []
    for step in range(1, h + 1):
        prediction = level + step * slope + seasonals[(n + step - 1) % p]
        out.append(float(max(0.0, prediction)))
    return out


def croston(
    series: Sequence[Any], horizon: int, *, alpha: float = 0.20, **_: Any
) -> list[float]:
    """
    Croston's method for intermittent demand.

    Splits the series into *how much* is bought when a purchase happens and
    *how often* purchases happen, smooths each separately, and forecasts the
    rate ``size / interval``.  This is the method that stops a slow-moving SKU
    from being loaded onto the van every single morning.
    """
    values = _clean(series)
    h = max(0, int(horizon))
    nonzero_idx = np.flatnonzero(values > 0)
    if values.size == 0 or nonzero_idx.size == 0:
        return _flat(0.0, h)
    if nonzero_idx.size == 1:
        return _flat(float(values[nonzero_idx[0]]) / values.size, h)

    a = min(0.95, max(0.01, float(alpha)))
    size = float(values[nonzero_idx[0]])
    interval = float(nonzero_idx[0] + 1)
    gap = 0
    for i in range(int(nonzero_idx[0]) + 1, values.size):
        gap += 1
        if values[i] > 0:
            size = a * float(values[i]) + (1 - a) * size
            interval = a * gap + (1 - a) * interval
            gap = 0

    rate = size / interval if interval > 0 else size
    return _flat(max(0.0, rate), h)


def linear_trend_forecast(series: Sequence[Any], horizon: int, **_: Any) -> list[float]:
    """Extrapolate the least-squares trend line, floored at zero."""
    values = _clean(series)
    n = values.size
    h = max(0, int(horizon))
    if n < 3:
        return moving_average_forecast(values, h)

    fit = ts.trend(values)
    slope = float(fit["slope"])
    intercept = float(fit["intercept"])
    return [float(max(0.0, slope * (n + step) + intercept)) for step in range(h)]


def ewma_forecast(
    series: Sequence[Any], horizon: int, *, alpha: float = 0.35, **_: Any
) -> list[float]:
    """Flat forecast at the exponentially weighted level of the series."""
    values = _clean(series)
    if values.size == 0:
        return _flat(0.0, horizon)
    smoothed = ts.ewma(values, alpha)
    return _flat(smoothed[-1], horizon)


#: Method registry — the keys are :class:`ForecastMethod` values so a persisted
#: ``Forecast.method`` round-trips back to the callable that produced it.
METHODS: dict[str, Callable[..., list[float]]] = {
    ForecastMethod.MOVING_AVERAGE: moving_average_forecast,
    ForecastMethod.EWMA: ewma_forecast,
    ForecastMethod.SEASONAL_NAIVE: seasonal_naive,
    ForecastMethod.HOLT_WINTERS: holt_winters,
    ForecastMethod.CROSTON: croston,
    ForecastMethod.LINEAR_TREND: linear_trend_forecast,
}


# ===========================================================================
# Evaluation
# ===========================================================================
def back_test(
    series: Sequence[Any],
    method: str | Callable[..., list[float]],
    holdout: int = 7,
    *,
    period: int = 7,
) -> dict[str, Any]:
    """
    Fit on everything but the last *holdout* buckets and score against them.

    Returns ``{mae, mape, rmse, bias, holdout, errors}``.  ``bias`` is the mean
    signed error: positive means the method over-forecasts, which for van
    loading costs returns and expiry, while negative means lost sales.  MAPE
    skips zero actuals (division by zero) and is 0.0 when every actual is zero.
    """
    values = _clean(series)
    fn = METHODS.get(str(method)) if not callable(method) else method
    if fn is None:
        fn = moving_average_forecast

    h = max(1, int(holdout))
    if values.size < h + 2:
        return {"mae": 0.0, "mape": 0.0, "rmse": 0.0, "bias": 0.0, "holdout": 0, "errors": []}

    train = values[:-h]
    actual = values[-h:]
    predicted = np.asarray(fn(train, h, period=period), dtype="float64")
    if predicted.size < h:                       # method returned short — pad flat
        pad = predicted[-1] if predicted.size else 0.0
        predicted = np.concatenate([predicted, np.full(h - predicted.size, pad)])

    errors = predicted[:h] - actual
    absolute = np.abs(errors)
    nonzero = actual != 0
    mape = float((absolute[nonzero] / np.abs(actual[nonzero])).mean() * 100.0) if nonzero.any() else 0.0

    return {
        "mae": round(float(absolute.mean()), 6),
        "mape": round(mape, 4),
        "rmse": round(float(math.sqrt(float((errors**2).mean()))), 6),
        "bias": round(float(errors.mean()), 6),
        "holdout": int(h),
        "errors": [round(float(e), 6) for e in errors],
    }


def _candidate_methods(values: np.ndarray, period: int) -> list[str]:
    """Choose which methods are worth back-testing for this series' shape."""
    n = values.size
    methods = [ForecastMethod.MOVING_AVERAGE, ForecastMethod.EWMA]
    if n >= 6:
        methods.append(ForecastMethod.LINEAR_TREND)
    if period > 1 and n >= 2 * period:
        methods.append(ForecastMethod.SEASONAL_NAIVE)
        methods.append(ForecastMethod.HOLT_WINTERS)
    if zeros_share(values) >= _INTERMITTENT_ZERO_SHARE:
        methods.append(ForecastMethod.CROSTON)
    return methods


def _future_dates(
    last_date: date | None, horizon: int, granularity: str
) -> list[date | None]:
    if last_date is None:
        return [None] * horizon
    out: list[date | None] = []
    cursor = ts.bucket_start(last_date, granularity)
    for _ in range(horizon):
        cursor = ts.next_bucket(cursor, granularity)
        out.append(cursor)
    return out


def ensemble(
    series: Sequence[Any],
    horizon: int,
    period: int = 7,
    *,
    last_date: date | None = None,
    granularity: str = "DAILY",
    label_tr: str = "Talep",
    label_en: str = "Demand",
) -> dict[str, Any]:
    """
    Pick — or blend — the best method for this series by back-tested MAE.

    Selection rule: every plausible method is scored on a holdout, the lowest
    MAE wins, and if the runner-up is within 10% of the winner the two are
    averaged.  Blending near-ties is deliberate: on short business series the
    MAE gap between two methods is often noise, and the average of two
    reasonable models is more stable than repeatedly flip-flopping between them
    from one run to the next.

    Returns ``{method, points:[{date, value, lower, upper}], confidence, mae,
    explanation_tr, explanation_en, history_points, candidates}``.
    """
    values = _clean(series)
    h = max(1, int(horizon))
    p = max(1, int(period))
    n = int(values.size)
    dates = _future_dates(last_date, h, granularity)

    if n == 0:
        return ForecastResult(
            method=ForecastMethod.MOVING_AVERAGE,
            points=[
                {"date": dates[i], "index": i, "value": 0.0, "lower": 0.0, "upper": 0.0}
                for i in range(h)
            ],
            confidence=0.0,
            mae=0.0,
            explanation_tr=(
                f"{label_tr} için geçmiş satış verisi bulunamadı; tahmin sıfır olarak "
                "verildi. En az 2-3 haftalık satış geçmişi biriktikten sonra tekrar deneyin."
            ),
            explanation_en=(
                f"No sales history for {label_en}, so the forecast is zero. Retry once "
                "two to three weeks of history has accumulated."
            ),
            history_points=0,
            candidates=[],
        ).as_dict()

    # Holdout: a quarter of the history, at least one bucket, at most one cycle.
    holdout = int(min(max(1, n // 4), max(p, 7)))
    can_backtest = n >= holdout + 3

    scores: list[dict[str, Any]] = []
    for name in _candidate_methods(values, p):
        metrics = back_test(values, name, holdout, period=p) if can_backtest else {}
        scores.append(
            {
                "method": str(name),
                "mae": float(metrics.get("mae", 0.0)) if metrics.get("holdout") else float("inf"),
                "mape": float(metrics.get("mape", 0.0)),
                "rmse": float(metrics.get("rmse", 0.0)),
                "bias": float(metrics.get("bias", 0.0)),
            }
        )

    scored = [s for s in scores if math.isfinite(s["mae"])]
    scored.sort(key=lambda s: s["mae"])

    if scored:
        best = scored[0]
        chosen = [best["method"]]
        blended = False
        if len(scored) > 1:
            runner_up = scored[1]
            spread = runner_up["mae"] - best["mae"]
            tolerance = max(best["mae"] * 0.10, 1e-9)
            if spread <= tolerance:
                chosen.append(runner_up["method"])
                blended = True
        mae = float(best["mae"])
        residual_std = float(back_test(values, best["method"], holdout, period=p).get("rmse", 0.0))
    else:
        # Too little history to score anything — fall back to the safest model.
        chosen = [
            str(ForecastMethod.CROSTON)
            if zeros_share(values) >= _INTERMITTENT_ZERO_SHARE
            else str(ForecastMethod.MOVING_AVERAGE)
        ]
        blended = False
        mae = 0.0
        residual_std = float(values.std()) if n > 1 else 0.0

    predictions = [
        np.asarray(METHODS[m](values, h, period=p), dtype="float64") for m in chosen
    ]
    combined = np.mean(np.vstack(predictions), axis=0)
    method_name = str(ForecastMethod.ENSEMBLE) if blended else str(chosen[0])

    # Interval widens with the horizon: uncertainty compounds the further out we
    # look, so step 10 gets a visibly wider band than step 1.
    spread_base = residual_std if residual_std > 0 else (float(values.std()) if n > 1 else 0.0)
    points: list[dict[str, Any]] = []
    for i in range(h):
        value = float(max(0.0, combined[i]))
        spread = _Z_80 * spread_base * math.sqrt(1.0 + i / max(1.0, float(p)))
        points.append(
            {
                "date": dates[i],
                "index": i,
                "value": round(value, 4),
                "lower": round(max(0.0, value - spread), 4),
                "upper": round(value + spread, 4),
            }
        )

    mean_level = float(values.mean())
    accuracy = 1.0 - (mae / mean_level) if mean_level > 0 and mae > 0 else (0.5 if mae == 0 and not scored else 0.9)
    history_factor = min(1.0, n / (4.0 * p))     # short history caps confidence
    confidence = round(float(min(0.95, max(0.05, accuracy * (0.4 + 0.6 * history_factor)))), 4)

    tr, en = _explain(
        method_name=method_name,
        chosen=chosen,
        blended=blended,
        n=n,
        period=p,
        mae=mae,
        mean_level=mean_level,
        zero_share=zeros_share(values),
        confidence=confidence,
        horizon=h,
        label_tr=label_tr,
        label_en=label_en,
    )

    return ForecastResult(
        method=method_name,
        points=points,
        confidence=confidence,
        mae=round(mae, 4),
        explanation_tr=tr,
        explanation_en=en,
        history_points=n,
        candidates=[
            {k: (round(v, 4) if isinstance(v, float) and math.isfinite(v) else v) for k, v in s.items()}
            for s in scored
        ],
    ).as_dict()


_METHOD_WORDS: dict[str, tuple[str, str]] = {
    ForecastMethod.MOVING_AVERAGE: ("hareketli ortalama", "moving average"),
    ForecastMethod.EWMA: ("üstel ağırlıklı ortalama", "exponentially weighted average"),
    ForecastMethod.SEASONAL_NAIVE: ("mevsimsel tekrar", "seasonal naive"),
    ForecastMethod.HOLT_WINTERS: ("Holt-Winters (trend + mevsimsellik)", "Holt-Winters (trend + seasonality)"),
    ForecastMethod.CROSTON: ("Croston (aralıklı talep)", "Croston (intermittent demand)"),
    ForecastMethod.LINEAR_TREND: ("doğrusal trend", "linear trend"),
    ForecastMethod.ENSEMBLE: ("karma model", "blended model"),
}


def _explain(
    *,
    method_name: str,
    chosen: list[str],
    blended: bool,
    n: int,
    period: int,
    mae: float,
    mean_level: float,
    zero_share: float,
    confidence: float,
    horizon: int,
    label_tr: str,
    label_en: str,
) -> tuple[str, str]:
    """Build the bilingual rationale shown next to every forecast."""
    words = [_METHOD_WORDS.get(m, (m, m)) for m in chosen]
    tr_methods = " + ".join(w[0] for w in words)
    en_methods = " + ".join(w[1] for w in words)
    error_pct = (mae / mean_level * 100.0) if mean_level > 0 else 0.0

    tr = (
        f"{label_tr} için {n} dönemlik geçmiş kullanıldı. "
        f"Geriye dönük testte en düşük hatayı {tr_methods} yöntemi verdi"
        f"{' ve iki yöntem birbirine çok yakın olduğu için ortalamaları alındı' if blended else ''}. "
        f"Ortalama mutlak hata {mae:,.2f} (ortalama seviyenin ~%{error_pct:,.0f} kadarı); "
        f"önümüzdeki {horizon} dönem tahmin edildi. Güven skoru: %{confidence * 100:,.0f}."
    )
    en = (
        f"Used {n} historical periods for {label_en}. "
        f"{en_methods.capitalize()} produced the lowest back-tested error"
        f"{' and the top two methods were within noise of each other, so they were averaged' if blended else ''}. "
        f"Mean absolute error {mae:,.2f} (about {error_pct:,.0f}% of the average level); "
        f"forecast covers the next {horizon} periods. Confidence: {confidence * 100:,.0f}%."
    )

    if zero_share >= _INTERMITTENT_ZERO_SHARE:
        tr += (
            f" Dönemlerin %{zero_share * 100:,.0f}'ünde hiç satış yok — talep aralıklı, "
            "bu yüzden tahmin ortalama bir günlük hıza göre verildi."
        )
        en += (
            f" {zero_share * 100:,.0f}% of periods had no sales at all — demand is "
            "intermittent, so the forecast is expressed as an average daily rate."
        )
    if n < 3 * period:
        tr += " Geçmiş kısa olduğu için tahmin temkinli yorumlanmalı."
        en += " History is short, so treat the forecast as indicative."
    return tr, en
