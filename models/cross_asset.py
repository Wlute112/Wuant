"""Causal, timestamp-aligned industry correlation features.

Every feature row at index ``i`` uses returns with indices strictly below
``i``.  The builder is therefore safe to compute once and slice inside a
walk-forward evaluation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np


INDUSTRY_FEATURE_NAMES = (
    "industry_peer_return",
    "industry_peer_momentum",
    "industry_residual_zscore",
    "industry_breadth",
    "industry_average_correlation",
)


@dataclass(frozen=True)
class PriceHistory:
    """Close history with optional nanosecond or datetime-like timestamps."""

    closes: Any
    timestamps: Any | None = None


def _timestamp_keys(values: Any) -> np.ndarray:
    arr = np.asarray(values)
    if np.issubdtype(arr.dtype, np.datetime64):
        return arr.astype("datetime64[ns]").astype(np.int64)
    if arr.dtype == object:
        keys: list[int] = []
        for value in arr:
            if hasattr(value, "value"):
                keys.append(int(value.value))
            elif isinstance(value, (int, float, np.integer, np.floating)):
                keys.append(int(value))
            else:
                keys.append(int(np.datetime64(value, "ns").astype(np.int64)))
        return np.asarray(keys, dtype=np.int64)
    return arr.astype(np.int64, copy=False)


def _safe_log_returns(close: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    values = np.asarray(close, dtype=float)
    result = np.zeros(len(values), dtype=float)
    valid = np.zeros(len(values), dtype=bool)
    if len(values) <= 1:
        return result, valid
    usable = (
        np.isfinite(values[1:])
        & np.isfinite(values[:-1])
        & (values[1:] > 0)
        & (values[:-1] > 0)
    )
    result[1:][usable] = np.log(values[1:][usable] / values[:-1][usable])
    valid[1:] = usable
    return result, valid


def coerce_price_history(value: Any) -> PriceHistory:
    if isinstance(value, PriceHistory):
        return value
    if isinstance(value, Mapping):
        closes = value.get("closes", value.get("close", ()))
        return PriceHistory(closes=closes, timestamps=value.get("timestamps"))
    return PriceHistory(closes=value)


def aligned_peer_returns(
    target_length: int,
    target_timestamps: Any | None,
    history: Any,
) -> tuple[np.ndarray, np.ndarray]:
    """Align peer closes by timestamp, retaining the legacy right-align fallback."""
    peer = coerce_price_history(history)
    peer_close = np.asarray(peer.closes, dtype=float)
    if target_timestamps is not None and peer.timestamps is not None:
        target_keys = _timestamp_keys(target_timestamps)
        peer_keys = _timestamp_keys(peer.timestamps)
        aligned = np.full(target_length, np.nan, dtype=float)
        positions = {int(key): idx for idx, key in enumerate(target_keys)}
        for key, value in zip(peer_keys, peer_close):
            idx = positions.get(int(key))
            if idx is not None:
                aligned[idx] = value
        return _safe_log_returns(aligned)

    aligned = np.full(target_length, np.nan, dtype=float)
    count = min(target_length, len(peer_close))
    if count:
        aligned[-count:] = peer_close[-count:]
    return _safe_log_returns(aligned)


def _winsorize(values: np.ndarray) -> np.ndarray:
    if len(values) < 10:
        return values
    lower, upper = np.quantile(values, (0.01, 0.99))
    return np.clip(values, lower, upper)


def _ewma_correlation(x: np.ndarray, y: np.ndarray, half_life: float) -> float:
    if len(x) < 2:
        return 0.0
    x = _winsorize(np.asarray(x, dtype=float))
    y = _winsorize(np.asarray(y, dtype=float))
    age = np.arange(len(x) - 1, -1, -1, dtype=float)
    weights = np.exp2(-age / max(float(half_life), 1.0))
    weights /= weights.sum()
    x_centered = x - np.dot(weights, x)
    y_centered = y - np.dot(weights, y)
    covariance = np.dot(weights, x_centered * y_centered)
    variance = np.dot(weights, x_centered * x_centered) * np.dot(
        weights, y_centered * y_centered
    )
    if variance <= 1e-24:
        return 0.0
    return float(np.clip(covariance / np.sqrt(variance), -1.0, 1.0))


def _aligned_return_matrix(
    target_close: Any,
    peer_histories: Mapping[str, Any] | None,
    peer_symbols: tuple[str, ...],
    target_timestamps: Any | None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    target = np.asarray(target_close, dtype=float)
    n = len(target)
    peer_histories = peer_histories or {}
    target_returns, target_valid = _safe_log_returns(target)
    if not peer_symbols:
        empty = np.empty((n, 0), dtype=float)
        return target_returns, target_valid, empty, empty.astype(bool)
    peer_returns: list[np.ndarray] = []
    peer_valid: list[np.ndarray] = []
    for symbol in peer_symbols:
        values, valid = aligned_peer_returns(
            n, target_timestamps, peer_histories.get(symbol, ())
        )
        peer_returns.append(values)
        peer_valid.append(valid)
    peers = np.column_stack(peer_returns)
    peer_is_valid = np.column_stack(peer_valid)
    return target_returns, target_valid, peers, peer_is_valid


def _industry_feature_row(
    row: int,
    target_returns: np.ndarray,
    target_valid: np.ndarray,
    peers: np.ndarray,
    peer_is_valid: np.ndarray,
    *,
    correlation_window_bars: int,
    correlation_half_life_bars: int,
    minimum_observations: int,
    minimum_correlation: float,
    correlation_shrinkage: float,
    momentum_bars: int,
) -> np.ndarray:
    neutral = np.zeros(len(INDUSTRY_FEATURE_NAMES), dtype=float)
    neutral[3] = 0.5
    if row < 1 or peers.shape[1] == 0:
        return neutral

    window = max(int(correlation_window_bars), 2)
    min_obs = max(2, min(int(minimum_observations), window))
    half_life = max(int(correlation_half_life_bars), 1)
    momentum = max(int(momentum_bars), 1)
    threshold = float(np.clip(minimum_correlation, 0.0, 0.999999))
    shrinkage = float(np.clip(correlation_shrinkage, 0.0, 1.0))

    start = max(1, row - window)
    correlations = np.zeros(peers.shape[1], dtype=float)
    for column in range(peers.shape[1]):
        valid = target_valid[start:row] & peer_is_valid[start:row, column]
        if int(valid.sum()) < min_obs:
            continue
        correlation = _ewma_correlation(
            target_returns[start:row][valid],
            peers[start:row, column][valid],
            half_life,
        )
        correlations[column] = correlation * (1.0 - shrinkage)

    strength = np.maximum(correlations - threshold, 0.0)
    if strength.sum() <= 0:
        return neutral
    weights = strength / strength.sum()

    latest_available = peer_is_valid[row - 1]
    latest_weights = weights * latest_available
    if latest_weights.sum() <= 0:
        return neutral
    latest_weights /= latest_weights.sum()
    peer_return = float(np.dot(latest_weights, peers[row - 1]))

    momentum_start = max(1, row - momentum)
    peer_momenta = np.zeros(peers.shape[1], dtype=float)
    momentum_available = np.zeros(peers.shape[1], dtype=bool)
    for column in range(peers.shape[1]):
        valid = peer_is_valid[momentum_start:row, column]
        if valid.any():
            peer_momenta[column] = peers[momentum_start:row, column][valid].sum()
            momentum_available[column] = True
    momentum_weights = weights * momentum_available
    if momentum_weights.sum() > 0:
        momentum_weights /= momentum_weights.sum()
        peer_momentum = float(np.dot(momentum_weights, peer_momenta))
        direction = np.sign(peer_momentum)
        if direction == 0:
            breadth = 0.5
        else:
            breadth = float(
                np.mean(np.sign(peer_momenta[momentum_available]) == direction)
            )
    else:
        peer_momentum = 0.0
        breadth = 0.5

    factor_window = peers[start:row] @ weights
    factor_valid = target_valid[start:row] & np.any(
        peer_is_valid[start:row], axis=1
    )
    target_window = target_returns[start:row][factor_valid]
    factor_window = factor_window[factor_valid]
    if len(target_window) >= min_obs and np.var(factor_window) > 1e-24:
        beta = float(
            np.cov(target_window, factor_window, ddof=0)[0, 1]
            / np.var(factor_window)
        )
        beta = float(np.clip(beta, -3.0, 3.0))
        residuals = target_window - beta * factor_window
        residual = target_returns[row - 1] - beta * peer_return
        residual_std = float(np.std(residuals))
        residual_z = (
            (residual - float(np.mean(residuals))) / residual_std
            if residual_std > 1e-12
            else 0.0
        )
    else:
        residual_z = 0.0

    selected = correlations[strength > 0]
    return np.asarray(
        (
            peer_return,
            peer_momentum,
            float(np.clip(residual_z, -5.0, 5.0)),
            breadth,
            float(np.mean(selected)) if len(selected) else 0.0,
        ),
        dtype=float,
    )


def make_industry_correlation_feature_row(
    target_close: Any,
    peer_histories: Mapping[str, Any] | None,
    peer_symbols: tuple[str, ...],
    *,
    row: int | None = None,
    target_timestamps: Any | None = None,
    correlation_window_bars: int = 60,
    correlation_half_life_bars: int = 20,
    minimum_observations: int = 40,
    minimum_correlation: float = 0.25,
    correlation_shrinkage: float = 0.20,
    momentum_bars: int = 5,
) -> np.ndarray:
    """Compute one causal feature row without rebuilding the historical frame."""
    target = np.asarray(target_close, dtype=float)
    selected_row = len(target) - 1 if row is None else int(row)
    if selected_row < 0 or selected_row >= len(target):
        raise IndexError("industry feature row is outside target history")
    target_returns, target_valid, peers, peer_is_valid = _aligned_return_matrix(
        target, peer_histories, peer_symbols, target_timestamps
    )
    return _industry_feature_row(
        selected_row,
        target_returns,
        target_valid,
        peers,
        peer_is_valid,
        correlation_window_bars=correlation_window_bars,
        correlation_half_life_bars=correlation_half_life_bars,
        minimum_observations=minimum_observations,
        minimum_correlation=minimum_correlation,
        correlation_shrinkage=correlation_shrinkage,
        momentum_bars=momentum_bars,
    )


def make_industry_correlation_features(
    target_close: Any,
    peer_histories: Mapping[str, Any] | None,
    peer_symbols: tuple[str, ...],
    *,
    target_timestamps: Any | None = None,
    correlation_window_bars: int = 60,
    correlation_half_life_bars: int = 20,
    minimum_observations: int = 40,
    minimum_correlation: float = 0.25,
    correlation_shrinkage: float = 0.20,
    momentum_bars: int = 5,
) -> np.ndarray:
    """Return five causal industry-factor columns aligned to ``target_close``."""
    target = np.asarray(target_close, dtype=float)
    output = np.zeros((len(target), len(INDUSTRY_FEATURE_NAMES)), dtype=float)
    if len(target) == 0 or not peer_symbols:
        return output
    output[:, 3] = 0.5
    target_returns, target_valid, peers, peer_is_valid = _aligned_return_matrix(
        target, peer_histories, peer_symbols, target_timestamps
    )
    for row in range(1, len(target)):
        output[row] = _industry_feature_row(
            row,
            target_returns,
            target_valid,
            peers,
            peer_is_valid,
            correlation_window_bars=correlation_window_bars,
            correlation_half_life_bars=correlation_half_life_bars,
            minimum_observations=minimum_observations,
            minimum_correlation=minimum_correlation,
            correlation_shrinkage=correlation_shrinkage,
            momentum_bars=momentum_bars,
        )
    return output
