"""Canonical crypto/equity operating profiles shared by CLI, API, and UI.

An asset class is more than an instrument constructor.  It selects the market
calendar, execution assumptions, reporting vocabulary, and (for Optuna) the
primary risk-adjusted objective.  Keeping those choices in one serializable
profile prevents the dashboard and subprocess runners from drifting apart.
"""
from __future__ import annotations

from copy import deepcopy
import math


ASSET_PROFILES = {
    "crypto": {
        "asset_class": "crypto",
        "label": "Crypto spot",
        "short_label": "CRYPTO",
        "description": "Continuous 24/7 spot market with fractional coin sizing.",
        "scoring": {
            "metric": "sortino",
            "label": "Sortino ratio",
            "short_label": "SORTINO",
            "rationale": "Downside deviation remains the primary penalty for asymmetric, fat-tailed crypto returns.",
            "trade_penalty": 0.002,
            "fallback_periods_per_year": 365.25,
        },
        "market": {
            "calendar": "24/7 continuous",
            "session": "All available hours",
            "use_regular_trading_hours": False,
            "price_type": "MID",
            "venue": "ZEROHASH",
            "quantity": "Fractional coins",
            "fee_model": "Zero Hash trailing-volume tiers",
            "gap_risk": "Weekend trading remains open; venue outages can still gap.",
        },
        "defaults": {
            "tickers": ["BTC", "ETH", "SOL", "XRP", "DOGE"],
            "csv": "quant/data/ibkr_bars.csv",
            "bar_hours": 24,
            "allow_shorts": False,
            "primary_exchange": "",
            "regime_window": 20,
            "regime_bull_threshold": 0.02,
            "regime_bear_threshold": -0.02,
            "target_score": 1.5,
        },
        "warnings": [
            "IBKR paper accounts do not support spot-crypto execution.",
            "Live IBKR spot crypto is long-only in this strategy.",
        ],
    },
    "equity": {
        "asset_class": "equity",
        "label": "US equities / ETFs",
        "short_label": "EQUITY",
        "description": "SMART-routed whole-share instruments on exchange sessions.",
        "scoring": {
            "metric": "sharpe",
            "label": "Sharpe ratio",
            "short_label": "SHARPE",
            "rationale": "Total volatility is the primary portfolio-efficiency penalty for session-based equities.",
            "trade_penalty": 0.001,
            "fallback_periods_per_year": 252.0,
        },
        "market": {
            "calendar": "US exchange calendar",
            "session": "Regular trading hours",
            "use_regular_trading_hours": True,
            "price_type": "LAST",
            "venue": "SMART",
            "quantity": "Whole shares",
            "fee_model": "Per-share commission approximation",
            "gap_risk": "Positions can gap across overnight, weekend, and holiday closures.",
        },
        "defaults": {
            # Matches the repository's bundled equity_bars.csv so selecting
            # the profile can run immediately; operators can type or fetch a
            # broader SMART universe from the same form.
            "tickers": ["QQQ"],
            "csv": "quant/data/equity_bars.csv",
            "bar_hours": 24,
            "allow_shorts": False,
            "primary_exchange": "",
            "regime_window": 20,
            "regime_bull_threshold": 0.01,
            "regime_bear_threshold": -0.01,
            "target_score": 1.0,
        },
        "warnings": [
            "Whole-share rounding can suppress trades in small allocations.",
            "Extended-hours bars and orders are opt-in and have thinner liquidity.",
            "Overnight gaps can cross an ATR risk reference before the next bar.",
            "Shorts require live IBKR borrow, fee, margin, Rule-201, and what-if approval.",
        ],
    },
}


def get_asset_profile(asset_class: str) -> dict:
    """Return a defensive copy of one canonical profile."""
    try:
        return deepcopy(ASSET_PROFILES[asset_class])
    except KeyError as exc:
        raise ValueError(f"unsupported asset class: {asset_class!r}") from exc


def scoring_metric_for_asset(asset_class: str) -> str:
    return get_asset_profile(asset_class)["scoring"]["metric"]


def regime_window_for_bar_hours(
    asset_class: str,
    bar_hours: int | None,
    *,
    include_extended_hours: bool = False,
) -> int:
    """Translate the canonical 20-session lookback into completed bars."""
    hours = 24 if bar_hours is None else max(1, int(bar_hours))
    if hours >= 24:
        return 20
    if asset_class == "crypto":
        bars_per_session = math.ceil(24 / hours)
    elif asset_class == "equity":
        session_hours = 16.0 if include_extended_hours else 6.5
        bars_per_session = math.ceil(session_hours / hours)
    else:
        raise ValueError(f"unsupported asset class: {asset_class!r}")
    return 20 * bars_per_session


def strategy_defaults_for_asset(
    asset_class: str,
    bar_hours: int | None = None,
    *,
    include_extended_hours: bool = False,
) -> dict:
    """Structural alpha defaults applied unless the operator overrides them."""
    defaults = get_asset_profile(asset_class)["defaults"]
    regime_window = regime_window_for_bar_hours(
        asset_class,
        bar_hours,
        include_extended_hours=include_extended_hours,
    )
    bars_per_session = max(1, regime_window // 20)
    return {
        "regime_window": regime_window,
        "regime_bull_threshold": defaults["regime_bull_threshold"],
        "regime_bear_threshold": defaults["regime_bear_threshold"],
        "use_industry_features": asset_class == "equity",
        "industry_correlation_window_bars": 60 * bars_per_session,
        "industry_correlation_half_life_bars": 20 * bars_per_session,
        "industry_minimum_observations": 40 * bars_per_session,
        "industry_momentum_bars": 5 * bars_per_session,
    }
