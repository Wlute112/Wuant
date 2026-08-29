"""Runnable backtest example -- works TONIGHT on synthetic sample data.

    # 1. generate sample bars (no TWS needed)
    python -m quant.data.generate_sample_bars --out quant/data/sample_bars.csv
    # 2. run the backtest
    python -m quant.run.run_backtest --csv quant/data/sample_bars.csv

Later, point --csv at a real IBKR export produced by quant.data.ibkr_fetch.
The strategy, sizing, and risk rules are identical either way.
"""
from __future__ import annotations

import argparse
import json
import time

from quant.run.artifacts import save_backtest_artifact
from quant.run.asset_profiles import strategy_defaults_for_asset
from quant.run.backtest_common import (
    ASSET_CLASSES,
    VENUE,
    build_and_run,
    infer_bars_per_session,
)
from quant.run.metrics import print_metrics

DEFAULT_TICKERS = ["BTC", "ETH", "SOL", "XRP", "DOGE"]  # crypto (24/7)
EQUITY_DEFAULT_TICKERS = ["SPY", "QQQ", "DIA", "IWM"]  # liquid index ETFs

# The hyperparameters Optuna is allowed to tune (must match MLStrategyConfig
# fields). Anything else in the JSON is ignored so a stray key can't crash the
# strategy config.
TUNABLE_KEYS = {
    "n_lags",
    "horizon",
    "entry_threshold",
    "atr_period",
    "atr_stop_mult",
    "use_limit_orders",
    "limit_offset_bps",
    "use_kelly_sizing",
    "kelly_fraction",
    "max_open_positions",
    "cross_asset_lags",
    "spread_lags",
    "huber_alpha",
    "huber_epsilon",
    "training_window_bars",
}

# Structural settings (fixed, NOT Optuna-tuned) that optimize.py records as
# TOP-LEVEL keys in best_params.json -- siblings of "params", not inside it
# (see optimize.py's payload dict). Applied whenever present so a saved run's
# warmup/refit cadence actually gets replayed here too, not just its tuned
# hyperparameters -- e.g. on low-bar-count data (weekly bars) where the
# default warmup_bars=150/min_train_bars=120 alone can exceed the whole
# in-sample window and produce zero trades.
STRUCTURAL_KEYS = {
    "refit_every_n_bars",
    "warmup_bars",
    "min_train_bars",
    # --- feature toggles (dashboard-controlled, NOT Optuna-tuned) ---
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
    # --- risk rails (dashboard-editable, NOT Optuna-tuned) ---
    "risk_budget_pct",
    "max_trade_risk_pct",
    "max_leverage",
    "daily_loss_limit_pct",
    "kill_switch_pct",
    "kill_warn_pct",
    "kelly_max_fraction",
}


def load_best_params(path: str) -> dict:
    """Load Optuna's saved params JSON.

    Accepts both the wrapper written by optimize.py ({"params": {...}, ...})
    and a bare {param: value} dict. Returns the recognised TUNABLE_KEYS from
    "params", merged with any STRUCTURAL_KEYS found at the top level.
    """
    with open(path) as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        return {}
    params = data.get("params", data)
    overrides = {k: v for k, v in params.items() if k in TUNABLE_KEYS}
    overrides.update({k: data[k] for k in STRUCTURAL_KEYS if k in data})
    dropped = set(params) - TUNABLE_KEYS - STRUCTURAL_KEYS
    if dropped:
        print(f"(ignoring non-tunable keys in {path}: {sorted(dropped)})")
    return overrides


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--csv", default="quant/data/sample_bars.csv")
    p.add_argument(
        "--asset-class",
        choices=ASSET_CLASSES,
        default="crypto",
        help="Instrument type + fee model for every ticker in this run "
        "(one class per run; see backtest_common.py).",
    )
    p.add_argument(
        "--tickers",
        nargs="*",
        default=None,
        help="Defaults to a crypto or equity ticker list depending on "
        "--asset-class if omitted.",
    )
    p.add_argument("--cash", type=float, default=5000.0)
    p.add_argument("--log-level", default="INFO")
    p.add_argument(
        "--news-db",
        default=None,
        help="Captured RSS/IBKR SQLite database. It is frozen to a content-"
        "addressed snapshot before replay so every bar sees only news that was "
        "received and analyzed by that timestamp.",
    )
    p.add_argument(
        "--no-news",
        action="store_true",
        help="Disable the news factor even when the params file enables it.",
    )
    p.add_argument(
        "--params",
        default=None,
        help="Path to Optuna's best_params.json. If given, those tuned "
        "hyperparameters override the MLStrategyConfig defaults.",
    )
    p.add_argument(
        "--run-id",
        default=None,
        help="Explicit run artifact id (e.g. supplied by the dashboard API's "
        "job runner). Defaults to an auto-generated bt_<timestamp>_<hex> id.",
    )
    p.add_argument(
        "--fetch-missing",
        action="store_true",
        help="Before running, fetch any --tickers missing from --csv via a live "
        "IBKR TWS/Gateway connection and merge them in (existing tickers' rows "
        "are left untouched). Requires TWS/Gateway running -- see "
        "data/ibkr_fetch.py; unverified against a live TWS in this environment.",
    )
    p.add_argument(
        "--replace-bars",
        action="store_true",
        help="Fetch the requested universe at --ibkr-bar-hours and replace the "
        "CSV completely. Mutually exclusive with --fetch-missing.",
    )
    p.add_argument("--ibkr-host", default="127.0.0.1")
    p.add_argument("--ibkr-port", type=int, default=7497)
    p.add_argument("--ibkr-client-id", type=int, default=1)
    p.add_argument("--ibkr-years", type=int, default=5)
    p.add_argument(
        "--ibkr-bar-hours",
        type=int,
        default=4,
        help="Target bar width in hours for --replace-bars (default 4). "
        "--fetch-missing always uses the CSV's existing frequency.",
    )
    p.add_argument(
        "--include-extended-hours",
        action="store_true",
        help="For equity IBKR fetches, request all available sessions instead of RTH only.",
    )
    args = p.parse_args()
    if args.fetch_missing and args.replace_bars:
        p.error("--fetch-missing and --replace-bars are mutually exclusive")
    tickers = args.tickers or (
        EQUITY_DEFAULT_TICKERS if args.asset_class == "equity" else DEFAULT_TICKERS
    )

    if args.fetch_missing or args.replace_bars:
        from quant.data.ibkr_fetch import ensure_tickers
        if args.replace_bars:
            from quant.data.ibkr_fetch import replace_bars
            replace_bars(
                csv_path=args.csv, tickers=tickers, asset_class=args.asset_class,
                years=args.ibkr_years, host=args.ibkr_host, port=args.ibkr_port,
                client_id=args.ibkr_client_id, bar_hours=args.ibkr_bar_hours,
                include_extended_hours=args.include_extended_hours,
            )
        else:
            fetched = ensure_tickers(
                csv_path=args.csv, tickers=tickers, asset_class=args.asset_class,
                years=args.ibkr_years, host=args.ibkr_host, port=args.ibkr_port,
                client_id=args.ibkr_client_id,
                include_extended_hours=args.include_extended_hours,
            )
            if fetched:
                print(f"Fetched missing tickers via IBKR at the CSV's existing frequency: {fetched}")

    loaded_overrides = load_best_params(args.params) if args.params else {}
    overrides = strategy_defaults_for_asset(args.asset_class)
    bars_per_session = infer_bars_per_session(args.csv, tickers)
    if "regime_window" not in loaded_overrides or loaded_overrides.get("regime_window") == 20:
        overrides["regime_window"] = 20 * bars_per_session
    overrides.update(loaded_overrides)
    if args.no_news:
        overrides["use_news_features"] = False
        overrides["news_data_path"] = ""
    elif args.news_db:
        from quant.news.core import snapshot_news_store

        news_snapshot, _news_digest = snapshot_news_store(args.news_db)
        overrides["use_news_features"] = True
        overrides["news_data_path"] = news_snapshot
    if args.params:
        print(f"Using tuned params from {args.params}: {overrides}")
    else:
        print(f"Using {args.asset_class} profile defaults (no --params).")

    started_at = time.time()
    engine = build_and_run(
        csv_path=args.csv,
        tickers=tickers,
        strategy_overrides=overrides,
        starting_cash=args.cash,
        log_level=args.log_level,
        asset_class=args.asset_class,
    )

    print("\n================ BACKTEST RESULT ================")
    try:
        print(engine.trader.generate_order_fills_report())
    except Exception as e:  # noqa: BLE001
        print(f"(fills report unavailable: {e})")
    try:
        print(engine.trader.generate_positions_report())
    except Exception as e:  # noqa: BLE001
        print(f"(positions report unavailable: {e})")

    # Rich performance-metric visuals (Net Profit, Sharpe, Max Drawdown,
    # Win/Loss, Profit Factor, Turnover, Capacity) ported from the legacy
    # run_backtest.py and reconstructed from the engine's standard reports.
    try:
        print_metrics(
            engine,
            VENUE,
            starting_cash=args.cash,
            asset_class=args.asset_class,
        )
    except Exception as e:  # noqa: BLE001
        print(f"(metrics panel unavailable: {e})")

    # Persist a run artifact (equity curve, positions, fills, ML-performance)
    # for the reporting dashboard. Best-effort: a failure here must never mask
    # the backtest result the rest of this command already printed above.
    try:
        artifact = save_backtest_artifact(
            engine=engine,
            venue=VENUE,
            starting_cash=args.cash,
            csv_path=args.csv,
            tickers=tickers,
            asset_class=args.asset_class,
            overrides=overrides,
            started_at=started_at,
            run_id=args.run_id,
            include_extended_hours=args.include_extended_hours,
        )
        print(f"\nSaved run artifact -> quant/runs/{artifact['run_id']}.json")
    except Exception as e:  # noqa: BLE001
        print(f"(run artifact unavailable: {e})")

    engine.dispose()


if __name__ == "__main__":
    main()
