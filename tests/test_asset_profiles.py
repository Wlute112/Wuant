import numpy as np

from quant.run.asset_profiles import (
    get_asset_profile,
    regime_window_for_bar_hours,
    strategy_defaults_for_asset,
)
from quant.run.scoring import annualization_factor, primary_ratio_from_curve


def test_profiles_select_distinct_objectives_and_market_contracts():
    crypto = get_asset_profile("crypto")
    equity = get_asset_profile("equity")

    assert crypto["scoring"]["metric"] == "sortino"
    assert crypto["market"]["calendar"] == "24/7 continuous"
    assert crypto["market"]["quantity"] == "Fractional coins"
    assert equity["scoring"]["metric"] == "sharpe"
    assert equity["market"]["use_regular_trading_hours"] is True
    assert equity["market"]["quantity"] == "Whole shares"
    assert equity["defaults"]["regime_bull_threshold"] < crypto["defaults"]["regime_bull_threshold"]


def test_profile_returns_are_defensive_copies():
    profile = get_asset_profile("crypto")
    profile["defaults"]["tickers"].append("MUTATED")
    assert "MUTATED" not in get_asset_profile("crypto")["defaults"]["tickers"]


def test_equity_annualizes_by_sessions_and_crypto_by_calendar_time():
    daily = np.array([0.0, 86400.0, 2 * 86400.0, 3 * 86400.0])
    assert annualization_factor(daily, "equity") == 252.0
    assert annualization_factor(daily, "crypto") == 365.25

    two_bars_per_session = np.array([
        9.5 * 3600,
        13.5 * 3600,
        86400 + 9.5 * 3600,
        86400 + 13.5 * 3600,
    ])
    assert annualization_factor(two_bars_per_session, "equity") == 504.0


def test_primary_ratio_follows_profile():
    curve = np.array([100.0, 102.0, 101.0, 104.0, 103.0, 106.0])
    ts = np.arange(len(curve), dtype=float) * 86400.0
    crypto_metric, crypto_score = primary_ratio_from_curve(curve, ts, "crypto")
    equity_metric, equity_score = primary_ratio_from_curve(curve, ts, "equity")

    assert crypto_metric == "sortino"
    assert equity_metric == "sharpe"
    assert crypto_score != equity_score


def test_strategy_defaults_include_equity_scaled_regime_thresholds():
    crypto = strategy_defaults_for_asset("crypto")
    equity = strategy_defaults_for_asset("equity")
    assert crypto["regime_bull_threshold"] == 0.02
    assert equity["regime_bull_threshold"] == 0.01
    assert equity["regime_bear_threshold"] == -0.01


def test_profile_defaults_preserve_twenty_sessions_at_intraday_cadences():
    assert get_asset_profile("crypto")["defaults"]["bar_hours"] == 24
    assert get_asset_profile("equity")["defaults"]["bar_hours"] == 24
    assert regime_window_for_bar_hours("crypto", 4) == 120
    assert regime_window_for_bar_hours("equity", 1) == 140
    assert regime_window_for_bar_hours("equity", 1, include_extended_hours=True) == 320
