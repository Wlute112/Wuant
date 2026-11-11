"""Huber strategy parameter contract. Optuna owns candidate selection.

The study lifecycle, folds, costs and scoring belong to optimize/, while this
module owns the current strategy's search space. No Nautilus or API imports.
"""
from __future__ import annotations

STRATEGY_ID = "huber_mean_reversion_v1"
TUNABLE_KEYS = frozenset({
    "n_lags", "horizon", "entry_threshold", "atr_period", "atr_stop_mult",
    "use_limit_orders", "limit_offset_bps", "use_kelly_sizing", "kelly_fraction",
    "max_open_positions", "cross_asset_lags", "spread_lags", "huber_alpha",
    "huber_epsilon", "training_window_bars",
})


# Structural settings (fixed, NOT Optuna-tuned) that optimize.py records as
# TOP-LEVEL keys in best_params.json -- siblings of "params", not inside it
# (see optimize.py's payload dict). Applied whenever present so a saved run's
# warmup/refit cadence actually gets replayed here too, not just its tuned
# hyperparameters -- e.g. on low-bar-count data (weekly bars) where the
# default warmup_bars=150/min_train_bars=120 alone can exceed the whole
# in-sample window and produce zero trades.
STRUCTURAL_KEYS = {
    "allow_short_positions",
    "equity_simulation",
    "refit_every_n_bars",
    "warmup_bars",
    "min_train_bars",
    # --- strategy-owned feature defaults (not manually tuned) ---
    "use_regime_features",
    "use_hmm_feature",
    "regime_source",
    "hmm_source",
    "regime_raw_scale",
    "hmm_raw_scale",
    "regime_window",
    "regime_bull_threshold",
    "regime_bear_threshold",
    # --- causal news factor (captured DB is frozen at run start) ---
    "use_news_features",
    "news_source",
    "news_raw_scale",
    "news_score_clip",
    "news_data_path",
    "news_half_life_hours",
    "news_max_age_hours",
    "news_direct_weight",
    "news_industry_weight",
    "news_commodity_weight",
    "news_macro_weight",
    # --- correlation-weighted same-industry factor ---------------------
    "use_industry_features",
    "industry_map",
    "industry_benchmark_map",
    "sector_map",
    "industry_correlation_window_bars",
    "industry_correlation_half_life_bars",
    "industry_minimum_observations",
    "industry_minimum_correlation",
    "industry_correlation_shrinkage",
    "industry_momentum_bars",
    # --- risk rails (dashboard-editable, NOT Optuna-tuned) ---
    "risk_budget_pct",
    "max_trade_risk_pct",
    "max_leverage",
    "daily_loss_limit_pct",
    "kill_switch_pct",
    "kill_warn_pct",
    "kelly_max_fraction",
    "max_order_notional_pct",
    "max_symbol_exposure_pct",
    "max_sector_exposure_pct",
    "max_gross_exposure_pct",
    "max_concentration_pct",
    "price_collar_pct",
}


def validate_fixed_overrides(overrides):
    """Fixed infrastructure settings cannot replace values sampled by Optuna."""
    conflicts = TUNABLE_KEYS.intersection(overrides or {})
    if conflicts:
        raise ValueError("Strategy parameters are selected by Optuna; remove fixed overrides: "
                         + ", ".join(sorted(conflicts)))


def suggest_parameters(trial, tickers, warmup_bars=150, min_train_bars=120):
    window_floor = max(warmup_bars, min_train_bars)
    training_windows = tuple(
        sorted({0, window_floor, window_floor * 2, window_floor * 4, window_floor * 8})
    )

    return dict(
        n_lags=trial.suggest_int("n_lags", 3, 15),
        horizon=trial.suggest_int("horizon", 1, 5),
        entry_threshold=trial.suggest_float(
            "entry_threshold", 1e-4, 5e-3, log=True
        ),
        atr_period=trial.suggest_int("atr_period", 7, 21),
        atr_stop_mult=trial.suggest_float("atr_stop_mult", 1.0, 4.0),
        use_limit_orders=trial.suggest_categorical(
            "use_limit_orders", [True, False]
        ),
        # Maker passive offset: fill-probability vs price-improvement knob.
        # Was fixed at 2 bps; now searched (only used when use_limit_orders).
        limit_offset_bps=trial.suggest_float(
            "limit_offset_bps", 0.5, 10.0, log=True
        ),
        # --- fractional-Kelly conviction sizing ---
        # use_kelly_sizing gates it; kelly_fraction is the "percent of full
        # Kelly" the user asked to optimize. kelly_fraction is always
        # suggested (ignored when sizing is off) so the search space stays
        # reproducible across trials.
        use_kelly_sizing=trial.suggest_categorical(
            "use_kelly_sizing", [True, False]
        ),
        kelly_fraction=trial.suggest_float("kelly_fraction", 0.05, 1.0),
        # --- portfolio concentration ---
        # Max concurrent positions across the ticker universe. Upper bound is
        # the number of tickers (== unlimited relative to this run).
        max_open_positions=trial.suggest_int(
            "max_open_positions", 1, max(1, len(tickers))
        ),
        # --- cross-asset ARDL + spread features (models/prediction_engine.py) ---
        # Lag depths only; the peer UNIVERSE (cross_asset_symbols) is
        # structural, not searched -- it defaults to "every other ticker in
        # this run" (see MLStrategy.on_start). 0 disables a block, so the
        # search space naturally includes "cross-asset features off".
        cross_asset_lags=trial.suggest_int("cross_asset_lags", 0, 5),
        spread_lags=trial.suggest_int("spread_lags", 0, 5),
        # --- Huber L2 penalty (models/prediction_engine.py) ---
        # Was fixed at PredictionConfig's 1e-4 default for every trial. Log-
        # uniform range spans from that default up to a strongly-regularized
        # ridge-like fit, so Optuna can counteract whatever feature-count-vs-
        # training-window ill-conditioning the OTHER tuned knobs (n_lags,
        # cross_asset_lags, spread_lags) produce for a given run instead of
        # always fighting it with one fixed, possibly-too-weak value.
        huber_alpha=trial.suggest_float("huber_alpha", 1e-4, 10.0, log=True),
        # --- Huber loss transition point (models/prediction_engine.py) ---
        # Was fixed at PredictionConfig's own default (1.35, sklearn's
        # ~95%-OLS-efficiency default) for every trial. Searched range
        # stays comfortably above sklearn's epsilon > 1.0 requirement:
        # near 1.05 the loss is almost fully linear (max robustness to
        # fat-tailed crypto returns, least efficient on well-behaved
        # residuals); near 3.0 it is nearly OLS (max efficiency, least
        # robust to outliers/gaps). Linear, not log, scale -- the whole
        # range spans less than one order of magnitude.
        huber_epsilon=trial.suggest_float("huber_epsilon", 1.05, 3.0),
        # Expanding history (0) competes directly against several rolling
        # Huber fit windows. The same candidate is evaluated across every
        # purged fold, so a short recent-regime fit must generalize rather
        # than merely improving one period.
        training_window_bars=trial.suggest_categorical(
            "training_window_bars", training_windows
        ),
    )
