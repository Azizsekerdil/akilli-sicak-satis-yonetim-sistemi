"""
Statistical engine.

Pure computation only: every function here takes plain numbers and returns
plain numbers.  Nothing in this package touches the database or the ORM, which
keeps the maths unit-testable and lets the service layer decide what a
"series" actually means (sales, quantities, collections, visits…).

Modules
-------
``descriptive``   summary statistics, histograms, percentiles
``timeseries``    resampling, smoothing, trend, seasonality, decomposition
``correlation``   Pearson / Spearman and plain-language interpretation
``regression``    simple and multiple least-squares regression
``forecasting``   demand forecasting for intermittent FMCG series
``anomaly``       outlier and change-point detection
"""

from __future__ import annotations

from app.analytics import (
    anomaly,
    correlation,
    descriptive,
    forecasting,
    regression,
    timeseries,
)

__all__ = [
    "anomaly",
    "correlation",
    "descriptive",
    "forecasting",
    "regression",
    "timeseries",
]
