"""Asset-aware risk-adjusted scoring primitives.

Crypto optimizes Sortino (downside deviation); equities optimize Sharpe (total
volatility). Crypto uses elapsed calendar time; equities use the observed bars
per active session times 252 sessions/year. This avoids incorrectly treating a
weekday-only daily equity series as 365 independent trading periods.
"""
from __future__ import annotations

import numpy as np

from quant.run.asset_profiles import get_asset_profile

SECONDS_PER_YEAR = 365.25 * 24 * 3600
DOWNSIDE_EPS = 1e-6


def annualization_factor(ts_seconds: np.ndarray, asset_class: str) -> float:
    fallback = float(
        get_asset_profile(asset_class)["scoring"]["fallback_periods_per_year"]
    )
    ts = np.unique(np.asarray(ts_seconds, dtype=float))
    ts = ts[np.isfinite(ts)]
    if ts.size < 2:
        return fallback
    if asset_class == "equity":
        active_days, counts = np.unique(np.floor(ts / 86400.0), return_counts=True)
        if active_days.size >= 2:
            # The first/last session can be partial after an IS/OOS split; the
            # median stays representative of the run's normal session density.
            return 252.0 * float(np.median(counts))
        dt = np.diff(ts)
        dt = dt[dt > 0]
        if dt.size and float(np.median(dt)) < 18 * 3600:
            return 252.0 * 6.5 * 3600.0 / float(np.median(dt))
        return fallback
    dt = np.diff(ts)
    dt = dt[np.isfinite(dt) & (dt > 0)]
    if dt.size == 0:
        return fallback
    median_dt = float(np.median(dt))
    return SECONDS_PER_YEAR / median_dt if median_dt > 0 else fallback


def returns_from_curve(curve: np.ndarray) -> np.ndarray:
    values = np.asarray(curve, dtype=float)
    if values.size < 3:
        return np.array([], dtype=float)
    prior = values[:-1]
    valid = np.isfinite(values[1:]) & np.isfinite(prior) & (prior != 0)
    return (values[1:][valid] - prior[valid]) / prior[valid]


def sharpe_from_curve(curve: np.ndarray, ts: np.ndarray, asset_class: str) -> float:
    rets = returns_from_curve(curve)
    if rets.size < 2:
        return 0.0
    sigma = float(np.std(rets))
    if sigma <= 0:
        return 0.0
    return float(
        (float(np.mean(rets)) / sigma)
        * np.sqrt(annualization_factor(ts, asset_class))
    )


def sortino_from_curve(curve: np.ndarray, ts: np.ndarray, asset_class: str) -> float:
    rets = returns_from_curve(curve)
    if rets.size < 2:
        return 0.0
    downside = rets[rets < 0]
    sigma_down = float(np.sqrt(np.mean(np.square(downside)))) if downside.size else 0.0
    if sigma_down <= 0:
        if float(np.mean(rets)) <= 0:
            return 0.0
        sigma_down = DOWNSIDE_EPS
    return float(
        (float(np.mean(rets)) / sigma_down)
        * np.sqrt(annualization_factor(ts, asset_class))
    )


def primary_ratio_from_curve(
    curve: np.ndarray,
    ts: np.ndarray,
    asset_class: str,
) -> tuple[str, float]:
    metric = get_asset_profile(asset_class)["scoring"]["metric"]
    if metric == "sharpe":
        return metric, sharpe_from_curve(curve, ts, asset_class)
    return metric, sortino_from_curve(curve, ts, asset_class)
