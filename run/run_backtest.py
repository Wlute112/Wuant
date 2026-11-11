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
from datetime import datetime, timezone
import json
import time

from nautilus_trader.model.data import Bar

from quant.run.artifacts import equity_curve_with_timestamps, save_backtest_artifact
from quant.run.asset_profiles import strategy_defaults_for_asset
from quant.run.backtest_common import (
    ASSET_CLASSES,
    VENUE,
    build_and_run,
    build_engine,
    infer_bars_per_session,
    replay_batches,
)
from quant.run.metrics import print_metrics
from quant.run.metrics import compute_metrics
from quant.run.progress import ProgressWriter

DEFAULT_TICKERS = ["BTC", "ETH", "SOL", "XRP", "DOGE"]  # crypto (24/7)
EQUITY_DEFAULT_TICKERS = ["SPY", "QQQ", "DIA", "IWM"]  # liquid index ETFs

from quant.strategies.parameters import STRUCTURAL_KEYS, TUNABLE_KEYS



def load_best_params(path: str) -> dict:
    """Load Optuna's saved params JSON.

    Accepts optimizer wrappers, dashboard runs ("best_params"), and bare
    {param: value} dicts. Returns the recognised TUNABLE_KEYS from
    "params", merged with any STRUCTURAL_KEYS found at the top level.
    """
    with open(path) as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("--params must contain a JSON object")
    if any(key in data for key in ("strategy_id", "validation_scheme", "source_run_id")) and not isinstance(data.get("allow_short_positions"), bool):
        raise ValueError("Optimizer parameters must record their long/short policy; run a new sweep")
    params = data.get("params", data.get("best_params", data))
    if not isinstance(params, dict):
        raise ValueError("Nested strategy parameters must be a JSON object")
    overrides = {k: v for k, v in params.items() if k in TUNABLE_KEYS}
    overrides.update({k: data[k] for k in STRUCTURAL_KEYS if k in data})
    dropped = set(params) - TUNABLE_KEYS - STRUCTURAL_KEYS
    if dropped:
        print(f"(ignoring non-tunable keys in {path}: {sorted(dropped)})")
    return overrides


def _run_with_progress(
    *,
    csv_path: str,
    tickers: list[str],
    overrides: dict,
    starting_cash: float,
    log_level: str,
    asset_class: str,
    progress: ProgressWriter,
):
    progress.update(
        phase="loading",
        phase_label="Loading market history",
        percent=1,
        tickers=tickers,
        starting_cash=starting_cash,
        timeline=[],
    )
    engine, bars = build_engine(
        csv_path=csv_path,
        tickers=tickers,
        strategy_overrides=overrides,
        starting_cash=starting_cash,
        log_level=log_level,
        asset_class=asset_class,
    )
    if not bars:
        engine.dispose()
        raise ValueError("No bars matched the requested ticker universe.")

    total = len(bars)
    total_bars = sum(isinstance(item, Bar) for item in bars)
    processed_bars = 0
    target_batch_size = max(1, total // 80)
    timeline = []
    peak_equity = starting_cash
    index = 0
    try:
        for batch in replay_batches(bars, target_batch_size):
            end = index + len(batch)
            processed_bars += sum(isinstance(item, Bar) for item in batch)
            engine.add_data(batch)
            engine.run(streaming=True)
            engine.clear_data()

            metrics = compute_metrics(engine, VENUE, starting_cash, asset_class)
            curve = equity_curve_with_timestamps(engine, VENUE)
            equity = curve[-1]["equity"] if curve else starting_cash + metrics.net_profit_usd
            peak_equity = max(peak_equity, equity)
            drawdown = 100 * (equity / peak_equity - 1) if peak_equity else 0.0
            as_of = datetime.fromtimestamp(batch[-1].ts_init / 1_000_000_000, timezone.utc).isoformat()
            timeline.append({
                "ts": as_of,
                "equity": round(float(equity), 2),
                "net_pnl": round(float(equity - starting_cash), 2),
                "drawdown_pct": round(float(drawdown), 2),
                "trades": metrics.total_trades,
            })
            progress.update(
                phase="replay",
                phase_label="Replaying historical bars",
                percent=round(100 * end / total, 1),
                bars_processed=processed_bars,
                bars_total=total_bars,
                as_of=as_of,
                equity=round(float(equity), 2),
                net_pnl=round(float(equity - starting_cash), 2),
                max_drawdown_pct=metrics.max_drawdown_pct,
                trades=metrics.total_trades,
                timeline=timeline[-180:],
            )
            index = end
        engine.end()
        progress.update(phase="reporting", phase_label="Computing final evidence", percent=100)
    except BaseException:
        engine.dispose()
        raise
    return engine


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--allow-shorts", action=argparse.BooleanOptionalAction, default=None,
                   help="Explicit equity research direction; default is long-only.")
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
        "--run-name",
        default=None,
        help="Optional human-readable label stored with the run artifact.",
    )
    p.add_argument("--progress-path", default=None, help=argparse.SUPPRESS)
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
    progress = ProgressWriter(
        args.progress_path,
        kind="backtest",
        phases=["loading", "replay", "reporting", "saving"],
    )
    progress.update(phase="loading", phase_label="Preparing run", percent=0)

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

    from quant.data.provenance import archive
    dataset_provenance = archive(args.csv)
    args.csv = dataset_provenance["snapshot_csv"]

    if args.asset_class == "equity":
        from quant.data.research_preflight import inspect_csv, require_execution_data
        require_execution_data(inspect_csv(args.csv, tickers, args.asset_class))

    loaded_overrides = load_best_params(args.params) if args.params else {}
    overrides = strategy_defaults_for_asset(args.asset_class)
    bars_per_session = infer_bars_per_session(args.csv, tickers)
    if "regime_window" not in loaded_overrides or loaded_overrides.get("regime_window") == 20:
        overrides["regime_window"] = 20 * bars_per_session
    for key, sessions in {
        "industry_correlation_window_bars": 60,
        "industry_correlation_half_life_bars": 20,
        "industry_minimum_observations": 40,
        "industry_momentum_bars": 5,
    }.items():
        if key not in loaded_overrides or loaded_overrides.get(key) == sessions:
            overrides[key] = sessions * bars_per_session
    overrides.update(loaded_overrides)
    if args.allow_shorts is not None:
        if "allow_short_positions" in loaded_overrides and loaded_overrides["allow_short_positions"] != args.allow_shorts:
            p.error("Long/short policy must match the selected Optuna parameters")
        overrides["allow_short_positions"] = args.allow_shorts
    if args.asset_class != "equity" and overrides.get("allow_short_positions"):
        p.error("Crypto spot research does not support short entries")
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
    if args.progress_path:
        engine = _run_with_progress(
            csv_path=args.csv,
            tickers=tickers,
            overrides=overrides,
            starting_cash=args.cash,
            log_level=args.log_level,
            asset_class=args.asset_class,
            progress=progress,
        )
    else:
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
        progress.update(phase="saving", phase_label="Saving research artifact", percent=100)
        artifact = save_backtest_artifact(
            dataset_provenance=dataset_provenance,
            engine=engine,
            venue=VENUE,
            starting_cash=args.cash,
            csv_path=args.csv,
            tickers=tickers,
            asset_class=args.asset_class,
            overrides=overrides,
            started_at=started_at,
            run_id=args.run_id,
            run_name=args.run_name,
            include_extended_hours=args.include_extended_hours,
        )
        print(f"\nSaved run artifact -> quant/runs/{artifact['run_id']}.json")
        progress.update(phase="complete", phase_label="Research artifact ready", percent=100)
    except Exception as e:  # noqa: BLE001
        print(f"(run artifact unavailable: {e})")

    engine.dispose()


if __name__ == "__main__":
    main()
