"""Run-artifact persistence for the reporting dashboard.

Backtest and Optuna runs already compute rich in-memory results (equity
curve, positions, fills, walk-forward ML metrics) but historically only ever
printed them to the console. This module is the one place that turns those
in-memory nautilus/Optuna objects into JSON-safe dicts and writes them to
``quant/runs/<run_id>.json``, so the dashboard API has something to read. It
is purely additive: it never changes what run_backtest.py / optimize.py
compute or trade, only what they persist afterwards.
"""
from __future__ import annotations

import json
import time
import uuid
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

from quant.models.prediction_engine import PredictionConfig, PredictionEngine
from quant.models.regime import build_regime_frame
from quant.run.backtest_common import VENUE
from quant.run.metrics import (
    _EQUITY_COLS,
    _fills_report,
    _first_col,
    _positions_report,
    _to_float_series,
    compute_metrics,
)

RUNS_DIR = Path("quant/runs")

# MLStrategyConfig override keys that map 1:1 onto PredictionConfig fields, so
# the dashboard's ML-performance panel scores the SAME alpha hyperparameters
# the run actually traded with, not PredictionConfig's bare defaults.
_PREDICTION_OVERRIDE_KEYS = (
    "n_lags",
    "horizon",
    "huber_alpha",
    "huber_epsilon",
    "cross_asset_lags",
    "spread_lags",
    "use_regime_features",
    "use_hmm_feature",
    "regime_source",
    "hmm_source",
    "regime_raw_scale",
    "hmm_raw_scale",
)

# Caps on how many raw fill/position rows an artifact embeds -- keeps the JSON
# file bounded on long/high-turnover runs. Never a silent cap: callers see
# "truncated": true and "total" alongside the capped "rows".
MAX_FILLS_RECORDED = 2000
MAX_POSITIONS_RECORDED = 2000


def _json_safe(value):
    """Recursively coerce numpy/pandas/Decimal values into JSON-safe types."""
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        v = float(value)
        return None if np.isnan(v) else v
    if isinstance(value, float) and np.isnan(value):
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, np.ndarray):
        return [_json_safe(v) for v in value.tolist()]
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _iso(ts: float) -> str:
    return pd.Timestamp(ts, unit="s", tz="UTC").isoformat()


def _dataframe_records(df: pd.DataFrame | None, limit: int) -> dict:
    """JSON-safe {"rows": [...], "total": N, "truncated": bool} for a report."""
    if df is None or len(df) == 0:
        return {"rows": [], "total": 0, "truncated": False}
    total = len(df)
    sliced = df.tail(limit) if total > limit else df
    rows = [
        {str(k): _json_safe(v) for k, v in row.items()}
        for row in sliced.reset_index().to_dict(orient="records")
    ]
    return {"rows": rows, "total": total, "truncated": total > limit}


def equity_curve_with_timestamps(engine, venue) -> list[dict]:
    """[{"ts": ISO8601, "equity": float}, ...] from the account report.

    Mirrors optimize.py's ``_equity_series()`` extraction (same report, same
    "total"-column probing) but returns a JSON-ready timestamped series
    instead of a bare numpy array -- the dashboard charts equity against real
    time, not bar index.
    """
    try:
        report = engine.trader.generate_account_report(venue)
    except Exception:  # noqa: BLE001
        return []
    if report is None or len(report) == 0:
        return []
    col = _first_col(report, _EQUITY_COLS)
    if col is None:
        return []
    values = _to_float_series(report[col])
    if "ts_event" in report.columns:
        ts_ns = pd.to_numeric(report["ts_event"], errors="coerce")
        timestamps = pd.to_datetime(ts_ns, unit="ns", utc=True)
    elif isinstance(report.index, pd.DatetimeIndex):
        timestamps = report.index
    else:
        timestamps = pd.date_range("1970-01-01", periods=len(report), freq="D", tz="UTC")
    points = []
    for ts, val in zip(timestamps, values):
        if np.isnan(val):
            continue
        points.append({"ts": pd.Timestamp(ts).isoformat(), "equity": round(float(val), 2)})
    return points


def ml_performance_by_ticker(
    csv_path: str, tickers: list[str], overrides: dict | None, n_splits: int = 5
) -> dict:
    """Per-ticker walk-forward ML performance -- the dashboard's "model loss"
    panel (see PRODUCT.md: no raw per-iteration Huber training-loss series is
    tracked today, so this walk-forward performance-over-time series is the
    confirmed stand-in). Uses the SAME data and alpha hyperparameters
    (n_lags/horizon/huber_*/cross_asset_lags/spread_lags) the run traded with.
    """
    overrides = overrides or {}
    df = pd.read_csv(csv_path)
    df["timestamp"] = pd.to_datetime(
        df["timestamp"], format="mixed", utc=True
    )
    closes_by_ticker = {
        tk: df[df["ticker"] == tk].sort_values("timestamp")["close"].to_numpy()
        for tk in tickers
    }
    timestamps_by_ticker = {
        tk: df[df["ticker"] == tk].sort_values("timestamp")["timestamp"].to_numpy()
        for tk in tickers
    }
    cross_lags = int(overrides.get("cross_asset_lags", 0) or 0)
    spread_lags = int(overrides.get("spread_lags", 0) or 0)
    wants_peers = cross_lags > 0 or spread_lags > 0
    cfg_kwargs = {k: overrides[k] for k in _PREDICTION_OVERRIDE_KEYS if k in overrides}

    results = {}
    for tk in tickers:
        peer_symbols = tuple(t for t in tickers if t != tk) if wants_peers else ()
        cfg = PredictionConfig(peer_symbols=peer_symbols, **cfg_kwargs)
        peer_closes = {p: closes_by_ticker[p] for p in peer_symbols} if peer_symbols else None
        try:
            eng = PredictionEngine(cfg)
            result = eng.walk_forward(
                closes_by_ticker[tk],
                peer_closes,
                n_splits=n_splits,
                return_folds=True,
                return_series=True,
            )
            series = result.pop("series", None)
            result["price_series"] = _reconstruct_price_series(
                series, closes_by_ticker[tk], timestamps_by_ticker[tk], cfg.horizon
            )
            results[tk] = result
        except ValueError as e:
            results[tk] = {"error": str(e)}
    return results


def _reconstruct_price_series(series, closes, timestamps, horizon: int) -> list[dict]:
    """Actual-vs-predicted PRICE overlay from walk_forward's raw OOS
    forward-log-return series: predicted_price = close[t] * exp(pred),
    actual_price = close[t+horizon] (the real close the prediction targeted).
    """
    if not series or not series.get("idx"):
        return []
    n = len(closes)
    points = []
    for t, pred, _actual in zip(series["idx"], series["pred"], series["actual"]):
        target_i = t + horizon
        if target_i >= n:
            continue
        points.append(
            {
                "ts": pd.Timestamp(timestamps[target_i]).isoformat(),
                "actual_price": round(float(closes[target_i]), 6),
                "predicted_price": round(float(closes[t] * np.exp(pred)), 6),
            }
        )
    return points


def regime_series_by_ticker(csv_path: str, tickers: list[str]) -> dict:
    """Per-ticker regime state over time -- the dashboard's regime channel
    (see DESIGN.md's Trace Violet). Walk-forward, no-lookahead (see
    models/regime.py); values are the SAME regime_score / hmm_signed columns
    the alpha layer itself conditions on, not a separate presentation-only
    computation.
    """
    df = pd.read_csv(csv_path)
    df["timestamp"] = pd.to_datetime(
        df["timestamp"], format="mixed", utc=True
    )
    results = {}
    for tk in tickers:
        sub = df[df["ticker"] == tk].sort_values("timestamp")
        if sub.empty:
            results[tk] = {"error": f"no rows for ticker {tk!r}"}
            continue
        frame = build_regime_frame(
            sub["close"].to_numpy(), timestamps=sub["timestamp"].to_numpy()
        )
        results[tk] = [
            {
                "ts": pd.Timestamp(row.timestamp).isoformat(),
                "regime_score": round(float(row.regime_score), 4),
                "hmm_signed": int(row.hmm_signed),
                "state_label": row.state_label,
            }
            for row in frame.itertuples(index=False)
        ]
    return results


def _write_artifact(run_id: str, artifact: dict) -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    path = RUNS_DIR / f"{run_id}.json"
    with open(path, "w") as fh:
        json.dump(_json_safe(artifact), fh, indent=2, default=str)


def save_backtest_artifact(
    *,
    engine,
    venue,
    starting_cash: float,
    csv_path: str,
    tickers: list[str],
    asset_class: str,
    overrides: dict | None,
    started_at: float,
    run_id: str | None = None,
) -> dict:
    run_id = run_id or f"bt_{int(started_at)}_{uuid.uuid4().hex[:8]}"
    metrics = compute_metrics(engine, venue, starting_cash)
    artifact = {
        "run_id": run_id,
        "kind": "backtest",
        "started_at": _iso(started_at),
        "finished_at": _iso(time.time()),
        "asset_class": asset_class,
        "tickers": tickers,
        "starting_cash": starting_cash,
        "params": overrides or {},
        "metrics": metrics.as_dict(),
        "equity_curve": equity_curve_with_timestamps(engine, venue),
        "positions": _dataframe_records(_positions_report(engine), MAX_POSITIONS_RECORDED),
        "fills": _dataframe_records(_fills_report(engine), MAX_FILLS_RECORDED),
        "ml_performance": ml_performance_by_ticker(csv_path, tickers, overrides),
        "regime": regime_series_by_ticker(csv_path, tickers),
    }
    _write_artifact(run_id, artifact)
    return artifact


def save_optimize_artifact(
    *,
    study,
    oos_engine,
    oos_score: float,
    csv_path: str,
    oos_path: str,
    tickers: list[str],
    asset_class: str,
    starting_cash: float,
    seed: int,
    n_trials_requested: int | None,
    train_frac: float,
    target_score: float | None,
    started_at: float,
    run_id: str | None = None,
    structural_overrides: dict | None = None,
    resumed_from: str | None = None,
    ibkr_bar_hours: int | None = None,
) -> dict:
    run_id = run_id or f"opt_{int(started_at)}_{uuid.uuid4().hex[:8]}"
    trials = [
        {
            "number": t.number,
            "state": t.state.name,
            "value": t.value,
            "params": t.params,
        }
        for t in study.get_trials(deepcopy=False)
    ]
    best_params = study.best_params
    scoring_params = {**best_params, **(structural_overrides or {})}
    artifact = {
        "run_id": run_id,
        "kind": "optimize",
        "started_at": _iso(started_at),
        "finished_at": _iso(time.time()),
        "asset_class": asset_class,
        "tickers": tickers,
        "starting_cash": starting_cash,
        "seed": seed,
        "n_trials_requested": n_trials_requested,
        "train_frac": train_frac,
        "target_score": target_score,
        "resumed_from": resumed_from,
        # The live bar width this run's data was fetched at (see optimize.py's
        # --ibkr-bar-hours / --fetch-missing), so a later paper/live run
        # started from this run's params can subscribe to bars of the same
        # width instead of assuming daily. None when the run didn't fetch via
        # IBKR at a non-default cadence (data's real granularity isn't known).
        "ibkr_bar_hours": ibkr_bar_hours,
        "trials": trials,
        "best_params": best_params,
        "in_sample_value": study.best_value,
        "oos_score": oos_score,
        "oos_metrics": compute_metrics(oos_engine, VENUE, starting_cash).as_dict(),
        "oos_equity_curve": equity_curve_with_timestamps(oos_engine, VENUE),
        "ml_performance": ml_performance_by_ticker(oos_path, tickers, scoring_params),
        "regime": regime_series_by_ticker(oos_path, tickers),
    }
    _write_artifact(run_id, artifact)
    return artifact


def list_run_summaries() -> list[dict]:
    """Lightweight summaries for the run list (newest first)."""
    if not RUNS_DIR.exists():
        return []
    summaries = []
    for path in RUNS_DIR.glob("*.json"):
        try:
            with open(path) as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            continue
        summaries.append(
            {
                "run_id": data.get("run_id", path.stem),
                "kind": data.get("kind"),
                "started_at": data.get("started_at"),
                "finished_at": data.get("finished_at"),
                "asset_class": data.get("asset_class"),
                "tickers": data.get("tickers"),
                "metrics": data.get("metrics") or data.get("oos_metrics"),
                "in_sample_value": data.get("in_sample_value"),
                "oos_score": data.get("oos_score"),
            }
        )
    summaries.sort(key=lambda s: s.get("finished_at") or "", reverse=True)
    return summaries


def load_run(run_id: str) -> dict | None:
    path = RUNS_DIR / f"{run_id}.json"
    if not path.exists():
        return None
    with open(path) as fh:
        return json.load(fh)


def delete_run(run_id: str) -> bool:
    """Delete a run artifact file. Returns False if it didn't exist."""
    path = RUNS_DIR / f"{run_id}.json"
    if not path.exists():
        return False
    path.unlink()
    return True
