"""
Correlation analysis with plain-language interpretation.

The interpretation strings exist because a coefficient alone is useless on a
sales manager's screen: "r = -0.62" means nothing, "the more days between
visits, the lower the collection rate" is an instruction.  Both Turkish and
English variants are produced so the frontend can pick without a round trip.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from app.analytics.descriptive import to_array

__all__ = [
    "pearson",
    "spearman",
    "correlation_matrix",
    "top_correlations",
    "strength_label",
]

#: (|r| threshold, tr label, en label) — checked from strongest to weakest.
_STRENGTH: tuple[tuple[float, str, str], ...] = (
    (0.90, "çok güçlü", "very strong"),
    (0.70, "güçlü", "strong"),
    (0.50, "orta düzeyde", "moderate"),
    (0.30, "zayıf", "weak"),
    (0.0, "çok zayıf", "negligible"),
)


def _pair(x: Sequence[Any], y: Sequence[Any]) -> tuple[np.ndarray, np.ndarray]:
    """Align two series to their common length and drop non-numeric rows."""
    xs = to_array(x)
    ys = to_array(y)
    n = min(xs.size, ys.size)
    return xs[:n], ys[:n]


def pearson(x: Sequence[Any], y: Sequence[Any]) -> float:
    """
    Pearson product-moment correlation, -1.0 to 1.0.

    Returns 0.0 for fewer than three pairs or a constant series: with a flat
    series the coefficient is undefined, and reporting 0 ("no relationship")
    is safer on a dashboard than reporting ``nan``.
    """
    xs, ys = _pair(x, y)
    if xs.size < 3:
        return 0.0
    if float(xs.std()) == 0.0 or float(ys.std()) == 0.0:
        return 0.0
    with np.errstate(invalid="ignore", divide="ignore"):
        r = float(np.corrcoef(xs, ys)[0, 1])
    return 0.0 if not np.isfinite(r) else round(max(-1.0, min(1.0, r)), 4)


def _ranks(values: np.ndarray) -> np.ndarray:
    """Ranks 1..n with ties averaged (the tie handling Spearman requires)."""
    order = values.argsort(kind="mergesort")
    ranks = np.empty(values.size, dtype="float64")
    ranks[order] = np.arange(1, values.size + 1, dtype="float64")

    sorted_values = values[order]
    start = 0
    for i in range(1, values.size + 1):
        if i == values.size or sorted_values[i] != sorted_values[start]:
            if i - start > 1:
                mean_rank = float(ranks[order[start:i]].mean())
                ranks[order[start:i]] = mean_rank
            start = i
    return ranks


def spearman(x: Sequence[Any], y: Sequence[Any]) -> float:
    """
    Spearman rank correlation.

    Preferred over Pearson for money series: one 200 000 TRY wholesale order
    would dominate a Pearson coefficient, while ranks keep it as a single
    observation.
    """
    xs, ys = _pair(x, y)
    if xs.size < 3:
        return 0.0
    return pearson(_ranks(xs), _ranks(ys))


def correlation_matrix(
    data: Mapping[str, Sequence[Any]], *, method: str = "pearson"
) -> dict[str, dict[str, float]]:
    """
    Full symmetric correlation matrix over the named series.

    Series of different lengths are truncated to their common length — with
    daily business series that means the shared date window.
    """
    fn = spearman if str(method).lower().startswith("s") else pearson
    keys = list(data.keys())
    matrix: dict[str, dict[str, float]] = {a: {} for a in keys}
    for i, a in enumerate(keys):
        matrix[a][a] = 1.0
        for b in keys[i + 1 :]:
            r = fn(data[a], data[b])
            matrix[a][b] = r
            matrix[b][a] = r
    return matrix


def strength_label(r: float) -> tuple[str, str]:
    """(tr, en) strength wording for a coefficient."""
    magnitude = abs(float(r))
    for threshold, tr_label, en_label in _STRENGTH:
        if magnitude >= threshold:
            return tr_label, en_label
    return _STRENGTH[-1][1], _STRENGTH[-1][2]


def interpret(name_a: str, name_b: str, r: float) -> tuple[str, str]:
    """Render one coefficient as a sentence a manager can act on."""
    tr_strength, en_strength = strength_label(r)
    if r >= 0:
        tr = (
            f"{name_a} ile {name_b} arasında {tr_strength} pozitif ilişki var "
            f"(r={r:.2f}): {name_a} arttıkça {name_b} de artma eğiliminde."
        )
        en = (
            f"{name_a} and {name_b} show a {en_strength} positive relationship "
            f"(r={r:.2f}): as {name_a} rises, {name_b} tends to rise too."
        )
    else:
        tr = (
            f"{name_a} ile {name_b} arasında {tr_strength} negatif ilişki var "
            f"(r={r:.2f}): {name_a} arttıkça {name_b} azalma eğiliminde."
        )
        en = (
            f"{name_a} and {name_b} show a {en_strength} negative relationship "
            f"(r={r:.2f}): as {name_a} rises, {name_b} tends to fall."
        )
    if abs(r) < 0.3:
        tr += " Bu ilişki karar almak için yeterince güçlü değil."
        en += " This link is not strong enough to act on by itself."
    return tr, en


def top_correlations(
    matrix: Mapping[str, Mapping[str, float]], limit: int = 10, *, min_abs: float = 0.0
) -> list[dict[str, Any]]:
    """
    Strongest relationships in a matrix, self-pairs and mirrors removed.

    Sorted by absolute strength — a strong negative link (visit gap vs revenue)
    is exactly as interesting as a strong positive one.
    """
    seen: set[tuple[str, str]] = set()
    rows: list[dict[str, Any]] = []
    for a, inner in matrix.items():
        for b, r in inner.items():
            if a == b:
                continue
            key = (a, b) if a < b else (b, a)
            if key in seen:
                continue
            seen.add(key)
            value = float(r)
            if abs(value) < float(min_abs):
                continue
            tr_strength, en_strength = strength_label(value)
            tr_text, en_text = interpret(key[0], key[1], value)
            rows.append(
                {
                    "subject_a": key[0],
                    "subject_b": key[1],
                    "coefficient": round(value, 4),
                    "strength": en_strength,
                    "strength_tr": tr_strength,
                    "direction": "positive" if value >= 0 else "negative",
                    "interpretation_tr": tr_text,
                    "interpretation_en": en_text,
                }
            )

    rows.sort(key=lambda row: abs(row["coefficient"]), reverse=True)
    return rows[: max(1, int(limit))]
