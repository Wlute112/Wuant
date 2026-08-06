"""Unit tests for cross-asset ARDL + spread features (models/prediction_engine.py).

Covers:
  * make_cross_asset_features column construction, lag alignment, and the
    right-alignment fallback for a shorter peer history.
  * The anti-lookahead contract: row i must be invariant to any change in
    target/peer closes at index > i.
  * PredictionEngine.refit_on_history / predict_move / walk_forward wired with
    peer_closes -- feature-vector length, backward compatibility (no peers),
    and the same no-lookahead invariant at the engine level.
"""
from __future__ import annotations

import numpy as np
import pytest

from quant.models.prediction_engine import (
    PredictionConfig,
    PredictionEngine,
    _log_returns,
    make_cross_asset_features,
    make_features_targets,
)

SEED = 7


def _random_walk(n: int, seed: int, start: float = 100.0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    steps = rng.normal(loc=0.0, scale=0.01, size=n)
    return start * np.exp(np.cumsum(steps))


# --------------------------------------------------------------------------- #
# make_cross_asset_features
# --------------------------------------------------------------------------- #

def test_returns_none_when_no_peer_symbols():
    cfg = PredictionConfig(cross_asset_lags=2, spread_lags=1, peer_symbols=())
    target = _random_walk(50, SEED)
    assert make_cross_asset_features(target, {}, cfg) is None


def test_returns_none_when_lags_both_zero():
    cfg = PredictionConfig(cross_asset_lags=0, spread_lags=0, peer_symbols=("PEER",))
    target = _random_walk(50, SEED)
    peer = _random_walk(50, SEED + 1)
    assert make_cross_asset_features(target, {"PEER": peer}, cfg) is None


def test_column_count_matches_lag_depths_times_peers():
    cfg = PredictionConfig(
        cross_asset_lags=2, spread_lags=3, peer_symbols=("A", "B")
    )
    n = 60
    target = _random_walk(n, SEED)
    peers = {"A": _random_walk(n, SEED + 1), "B": _random_walk(n, SEED + 2)}
    feats = make_cross_asset_features(target, peers, cfg)
    assert feats.shape == (n, len(cfg.peer_symbols) * (2 + 3))


def test_ardl_and_spread_lag_values_match_shifted_returns():
    cfg = PredictionConfig(cross_asset_lags=2, spread_lags=2, peer_symbols=("PEER",))
    n = 40
    target = _random_walk(n, SEED)
    peer = _random_walk(n, SEED + 1)
    feats = make_cross_asset_features(target, {"PEER": peer}, cfg)

    target_r = _log_returns(target)
    peer_r = _log_returns(peer)
    spread_r = target_r - peer_r

    # Column order: ardl_lag1, ardl_lag2, spread_lag1, spread_lag2.
    ardl1, ardl2, spread1, spread2 = feats[:, 0], feats[:, 1], feats[:, 2], feats[:, 3]
    for i in range(n):
        exp_ardl1 = peer_r[i - 1] if i >= 1 else 0.0
        exp_ardl2 = peer_r[i - 2] if i >= 2 else 0.0
        exp_spread1 = spread_r[i - 1] if i >= 1 else 0.0
        exp_spread2 = spread_r[i - 2] if i >= 2 else 0.0
        assert ardl1[i] == pytest.approx(exp_ardl1)
        assert ardl2[i] == pytest.approx(exp_ardl2)
        assert spread1[i] == pytest.approx(exp_spread1)
        assert spread2[i] == pytest.approx(exp_spread2)


def test_shorter_peer_history_right_aligns_with_leading_zeros():
    cfg = PredictionConfig(cross_asset_lags=1, spread_lags=0, peer_symbols=("PEER",))
    n = 30
    m = 10  # peer only has the most recent 10 bars
    target = _random_walk(n, SEED)
    peer_tail = _random_walk(m, SEED + 1)
    feats = make_cross_asset_features(target, {"PEER": peer_tail}, cfg)

    peer_r_full = _log_returns(peer_tail)
    # Rows before the peer's history starts (target index < n - m) must be 0;
    # the peer's own returns occupy the trailing m rows, lagged by 1.
    assert np.all(feats[: n - m - 1, 0] == 0.0)
    for j, i in enumerate(range(n - m, n)):
        expected = peer_r_full[j - 1] if j >= 1 else 0.0
        assert feats[i, 0] == pytest.approx(expected)


def test_missing_peer_symbol_treated_as_no_history():
    cfg = PredictionConfig(cross_asset_lags=1, spread_lags=1, peer_symbols=("GHOST",))
    n = 20
    target = _random_walk(n, SEED)
    feats = make_cross_asset_features(target, {}, cfg)
    assert feats.shape == (n, 2)
    # ARDL column: peer_r treated as all-zero -> the lagged peer-return column
    # is all zero.
    assert np.all(feats[:, 0] == 0.0)
    # Spread column: (target_r - 0) degenerates to target's own lagged return
    # -- correct given "no peer signal", not spuriously zero.
    target_r = _log_returns(target)
    expected_spread = np.concatenate([[0.0], target_r[:-1]])
    assert np.allclose(feats[:, 1], expected_spread)


def test_no_lookahead_row_invariant_to_future_target_and_peer_changes():
    cfg = PredictionConfig(cross_asset_lags=2, spread_lags=2, peer_symbols=("PEER",))
    n = 40
    target = _random_walk(n, SEED)
    peer = _random_walk(n, SEED + 1)
    feats_a = make_cross_asset_features(target, {"PEER": peer}, cfg)

    # Mutate every value strictly AFTER row i for both series (new array
    # objects; no in-place mutation of shared state) and confirm row i is
    # untouched -- the defining anti-lookahead property.
    check_row = 15
    target_b = target.copy()
    peer_b = peer.copy()
    target_b[check_row + 1 :] *= 1.5
    peer_b[check_row + 1 :] *= 0.7
    feats_b = make_cross_asset_features(target_b, {"PEER": peer_b}, cfg)

    assert np.allclose(feats_a[check_row], feats_b[check_row])
    assert np.allclose(feats_a[: check_row + 1], feats_b[: check_row + 1])


# --------------------------------------------------------------------------- #
# make_features_targets with cross_feats appended
# --------------------------------------------------------------------------- #

def test_make_features_targets_appends_cross_columns_after_regime():
    cfg = PredictionConfig(
        n_lags=3, horizon=1, cross_asset_lags=2, spread_lags=1, peer_symbols=("PEER",)
    )
    n = 60
    target = _random_walk(n, SEED)
    peer = _random_walk(n, SEED + 1)
    cross_feats = make_cross_asset_features(target, {"PEER": peer}, cfg)
    regime_feats = np.zeros((n, 2))  # stand-in, independent of regime.py

    X, y, idx = make_features_targets(target, cfg, regime_feats, cross_feats)
    # n_lags own-AR + 2 regime cols + (1 peer * (2 ardl + 1 spread)) cross cols.
    assert X.shape[1] == cfg.n_lags + 2 + 3
    assert len(X) == len(y) == len(idx)


# --------------------------------------------------------------------------- #
# PredictionEngine integration
# --------------------------------------------------------------------------- #

def _engine(**overrides) -> PredictionEngine:
    cfg = PredictionConfig(
        n_lags=3,
        horizon=1,
        min_train_bars=40,
        use_regime_features=False,  # isolate cross-asset behaviour from regime/HMM
        **overrides,
    )
    return PredictionEngine(cfg)


def test_refit_on_history_with_peers_grows_coef_vector():
    n = 80
    target = _random_walk(n, SEED)
    peer = _random_walk(n, SEED + 1)
    eng = _engine(cross_asset_lags=2, spread_lags=1, peer_symbols=("PEER",))

    assert eng.refit_on_history(target, {"PEER": peer}) is True
    coef, _ = eng.coef_intercept()
    assert len(coef) == 3 + 1 * (2 + 1)  # n_lags + peers * (ardl + spread)


def test_predict_move_with_peers_returns_float():
    n = 80
    target = _random_walk(n, SEED)
    peer = _random_walk(n, SEED + 1)
    eng = _engine(cross_asset_lags=2, spread_lags=1, peer_symbols=("PEER",))
    eng.refit_on_history(target, {"PEER": peer})

    yhat = eng.predict_move(target, {"PEER": peer})
    assert isinstance(yhat, float)


def test_no_peers_is_backward_compatible_with_positional_call():
    """An engine with peer_symbols=() (the default) must behave exactly like
    before this feature existed -- refit_on_history/predict_move callable
    without a peer_closes argument at all.
    """
    n = 80
    target = _random_walk(n, SEED)
    eng = _engine()  # peer_symbols=() by default
    assert eng.refit_on_history(target) is True
    coef, _ = eng.coef_intercept()
    assert len(coef) == 3  # n_lags only, no regime, no cross cols
    yhat = eng.predict_move(target)
    assert isinstance(yhat, float)


def test_walk_forward_with_peers_runs_and_scores():
    n = 200
    target = _random_walk(n, SEED)
    peer = _random_walk(n, SEED + 1)
    eng = _engine(cross_asset_lags=2, spread_lags=1, peer_symbols=("PEER",))

    metrics = eng.walk_forward(target, {"PEER": peer}, n_splits=4)
    assert metrics["oos_samples"] > 0
    assert "oos_r2" in metrics


def test_predict_move_invariant_to_prepended_older_history():
    """yhat is a function of the trailing window's tail only; prepending
    extra OLDER history (before what the engine previously saw) must not
    change it, since every cross-asset column is lagged off the window's
    own tail and log returns near the tail don't depend on what precedes
    the prepended segment.
    """
    n = 80
    target = _random_walk(n, SEED)
    peer = _random_walk(n, SEED + 1)
    eng = _engine(cross_asset_lags=2, spread_lags=1, peer_symbols=("PEER",))
    eng.refit_on_history(target, {"PEER": peer})

    yhat_a = eng.predict_move(target, {"PEER": peer})

    extra_history = _random_walk(20, SEED + 2, start=target[0])
    target_with_older_history = np.concatenate([extra_history, target])
    peer_with_older_history = np.concatenate([extra_history * 0.5, peer])
    yhat_b = eng.predict_move(
        target_with_older_history, {"PEER": peer_with_older_history}
    )
    assert yhat_a == pytest.approx(yhat_b)
