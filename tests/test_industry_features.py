from __future__ import annotations

import numpy as np
import pandas as pd

from quant.models.cross_asset import (
    INDUSTRY_FEATURE_NAMES,
    PriceHistory,
    aligned_peer_returns,
    make_industry_correlation_features,
)
from quant.models.industry import (
    industry_peers_for_symbol,
    sector_for_symbol,
)
from quant.models.prediction_engine import PredictionConfig, PredictionEngine


def _correlated_prices(n: int = 180) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(91)
    common = rng.normal(0.0005, 0.01, n)
    target_returns = common + rng.normal(0, 0.001, n)
    peer_returns = common + rng.normal(0, 0.001, n)
    return (
        100 * np.exp(np.cumsum(target_returns)),
        80 * np.exp(np.cumsum(peer_returns)),
    )


def test_default_classification_selects_only_same_industry_peers():
    universe = ("NVDA.SMART", "AMD.SMART", "JPM.SMART", "SMH.SMART")
    assert industry_peers_for_symbol("NVDA.SMART", universe) == (
        "AMD.SMART",
        "SMH.SMART",
    )
    assert sector_for_symbol("NVDA.SMART") == "technology"
    assert sector_for_symbol("JPM.SMART") == "financials"


def test_custom_classification_and_benchmark_are_structural():
    universe = ("AAA.SMART", "BBB.SMART", "ETF.SMART", "OTHER.SMART")
    peers = industry_peers_for_symbol(
        "AAA.SMART",
        universe,
        industry_map={"AAA": "custom", "BBB": "custom"},
        benchmark_map={"custom": "ETF"},
    )
    assert peers == ("BBB.SMART", "ETF.SMART")


def test_industry_features_capture_correlated_peer_factor():
    target, peer = _correlated_prices()
    features = make_industry_correlation_features(
        target,
        {"PEER": peer},
        ("PEER",),
        correlation_window_bars=60,
        correlation_half_life_bars=20,
        minimum_observations=20,
        minimum_correlation=0.10,
        correlation_shrinkage=0.10,
        momentum_bars=5,
    )
    assert features.shape == (len(target), len(INDUSTRY_FEATURE_NAMES))
    assert features[-1, 4] > 0.7
    assert np.isfinite(features).all()


def test_industry_feature_rows_do_not_change_when_future_prices_change():
    target, peer = _correlated_prices(120)
    kwargs = dict(
        peer_symbols=("PEER",),
        correlation_window_bars=40,
        minimum_observations=15,
        minimum_correlation=0.05,
    )
    original = make_industry_correlation_features(
        target, {"PEER": peer}, **kwargs
    )
    row = 75
    changed_target = target.copy()
    changed_peer = peer.copy()
    changed_target[row + 1 :] *= 1.4
    changed_peer[row + 1 :] *= 0.7
    changed = make_industry_correlation_features(
        changed_target, {"PEER": changed_peer}, **kwargs
    )
    assert np.allclose(original[: row + 1], changed[: row + 1])


def test_timestamp_alignment_neutralizes_returns_across_missing_peer_bar():
    timestamps = pd.Series(
        pd.date_range("2024-01-01", periods=10, freq="D", tz="UTC")
    ).to_numpy()
    peer_close = np.linspace(100, 109, 10)
    keep = np.arange(10) != 5
    returns, valid = aligned_peer_returns(
        10,
        timestamps,
        PriceHistory(peer_close[keep], timestamps[keep]),
    )
    assert valid[4]
    assert not valid[5]
    assert not valid[6]
    assert valid[7]
    assert returns[5] == 0.0
    assert returns[6] == 0.0


def test_prediction_engine_fits_and_predicts_with_industry_columns():
    target, peer = _correlated_prices()
    timestamps = np.arange(len(target), dtype=np.int64) * 86_400_000_000_000
    cfg = PredictionConfig(
        n_lags=3,
        min_train_bars=30,
        use_regime_features=False,
        use_hmm_feature=False,
        use_industry_features=True,
        industry_peer_symbols=("PEER",),
        industry_correlation_window_bars=40,
        industry_correlation_half_life_bars=15,
        industry_minimum_observations=15,
        industry_minimum_correlation=0.05,
    )
    histories = {"PEER": PriceHistory(peer, timestamps)}
    engine = PredictionEngine(cfg)
    assert engine.refit_on_history(target, histories, timestamps=timestamps)
    coefficients, _ = engine.coef_intercept()
    assert len(coefficients) == cfg.n_lags + len(INDUSTRY_FEATURE_NAMES)
    prediction = engine.predict_move(target, histories, timestamps=timestamps)
    assert prediction is not None
    assert np.isfinite(prediction)


def test_prediction_engine_incremental_industry_cache_matches_full_frame():
    target, peer = _correlated_prices(100)
    cfg = PredictionConfig(
        use_regime_features=False,
        use_hmm_feature=False,
        use_industry_features=True,
        industry_peer_symbols=("PEER",),
        industry_correlation_window_bars=30,
        industry_minimum_observations=10,
        industry_minimum_correlation=0.05,
    )
    engine = PredictionEngine(cfg)
    for end in range(40, len(target) + 1):
        incremental = engine._industry_feats(
            target[:end], {"PEER": peer[:end]}
        )
    expected = make_industry_correlation_features(
        target,
        {"PEER": peer},
        ("PEER",),
        correlation_window_bars=cfg.industry_correlation_window_bars,
        correlation_half_life_bars=cfg.industry_correlation_half_life_bars,
        minimum_observations=cfg.industry_minimum_observations,
        minimum_correlation=cfg.industry_minimum_correlation,
        correlation_shrinkage=cfg.industry_correlation_shrinkage,
        momentum_bars=cfg.industry_momentum_bars,
    )
    assert np.allclose(incremental, expected)
    assert engine._industry_feats(target, {"PEER": peer}) is incremental
