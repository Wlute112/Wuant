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

from quant.models.cross_asset import PriceHistory
from quant.models.industry import industry_peers_for_symbol
from quant.models.prediction_engine import PredictionConfig, PredictionEngine
from quant.models.regime import build_regime_frame
from quant.news.core import NewsFeatureReader
from quant.run.asset_profiles import get_asset_profile
from quant.run.backtest_common import VENUE, infer_bar_interval_minutes_from_csv
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
    "training_window_bars",
    "cross_asset_lags",
    "spread_lags",
    "industry_correlation_window_bars",
    "industry_correlation_half_life_bars",
    "industry_minimum_observations",
    "industry_minimum_correlation",
    "industry_correlation_shrinkage",
    "industry_momentum_bars",
    "use_regime_features",
    "use_hmm_feature",
    "regime_source",
    "hmm_source",
    "regime_raw_scale",
    "hmm_raw_scale",
    "regime_window",
    "regime_bull_threshold",
    "regime_bear_threshold",
    "use_news_features",
    "news_source",
    "news_raw_scale",
    "news_score_clip",
)

_NEWS_READER_OVERRIDE_KEYS = {
    "news_half_life_hours": "half_life_hours",
    "news_max_age_hours": "max_age_hours",
    "news_direct_weight": "direct_weight",
    "news_industry_weight": "industry_weight",
    "news_commodity_weight": "commodity_weight",
    "news_macro_weight": "macro_weight",
}

# Caps on how many raw fill/position rows an artifact embeds -- keeps the JSON
# file bounded on long/high-turnover runs. Never a silent cap: callers see
# "truncated": true and "total" alongside the capped "rows".
MAX_FILLS_RECORDED = 2000
MAX_POSITIONS_RECORDED = 2000
MAX_MODEL_CHART_POINTS = 1500


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
    csv_path: str,
    tickers: list[str],
    overrides: dict | None,
    n_splits: int = 5,
    news_series: dict | None = None,
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
    if news_series is None:
        news_series = news_series_by_ticker(csv_path, tickers, overrides)
    cross_lags = int(overrides.get("cross_asset_lags", 0) or 0)
    spread_lags = int(overrides.get("spread_lags", 0) or 0)
    wants_peers = cross_lags > 0 or spread_lags > 0
    wants_industry = bool(overrides.get("use_industry_features", False))
    cfg_kwargs = {k: overrides[k] for k in _PREDICTION_OVERRIDE_KEYS if k in overrides}

    results = {}
    for tk in tickers:
        peer_symbols = tuple(t for t in tickers if t != tk) if wants_peers else ()
        industry_peers = (
            industry_peers_for_symbol(
                tk,
                tickers,
                industry_map=overrides.get("industry_map"),
                benchmark_map=overrides.get("industry_benchmark_map"),
            )
            if wants_industry
            else ()
        )
        cfg = PredictionConfig(
            peer_symbols=peer_symbols,
            use_industry_features=bool(industry_peers),
            industry_peer_symbols=industry_peers,
            **cfg_kwargs,
        )
        required_peers = cfg.required_peer_symbols
        peer_closes = {
            peer: PriceHistory(
                closes=closes_by_ticker[peer],
                timestamps=timestamps_by_ticker[peer],
            )
            for peer in required_peers
        } or None
        ticker_news = news_series.get(tk, [])
        news_features = (
            np.asarray([float(item.get("score", 0.0)) for item in ticker_news])
            if ticker_news
            else None
        )
        try:
            eng = PredictionEngine(cfg)
            result = eng.walk_forward(
                closes_by_ticker[tk],
                peer_closes,
                n_splits=n_splits,
                timestamps=timestamps_by_ticker[tk],
                news_features=news_features,
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


def news_series_by_ticker(
    csv_path: str,
    tickers: list[str],
    overrides: dict | None = None,
) -> dict:
    """Causal news score aligned to every completed bar in ``csv_path``."""
    overrides = overrides or {}
    if not overrides.get("use_news_features", False):
        return {ticker: [] for ticker in tickers}
    db_path = str(overrides.get("news_data_path") or "")
    if not db_path or not Path(db_path).expanduser().is_file():
        return {ticker: [] for ticker in tickers}
    reader_kwargs = {
        target: overrides[source]
        for source, target in _NEWS_READER_OVERRIDE_KEYS.items()
        if source in overrides
    }
    df = pd.read_csv(csv_path, usecols=["timestamp", "ticker"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], format="mixed", utc=True)
    reader = NewsFeatureReader(db_path, **reader_kwargs)
    try:
        results = {}
        for ticker in tickers:
            timestamps = (
                df[df["ticker"] == ticker]
                .sort_values("timestamp")["timestamp"]
                .tolist()
            )
            scores = reader.series(
                ticker,
                [pd.Timestamp(value).timestamp() for value in timestamps],
            )
            results[ticker] = [
                {
                    "ts": pd.Timestamp(ts).isoformat(),
                    "score": round(float(score), 8),
                }
                for ts, score in zip(timestamps, scores)
            ]
        return results
    finally:
        reader.close()


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
                "decision_ts": pd.Timestamp(timestamps[t]).isoformat(),
                "target_ts": pd.Timestamp(timestamps[target_i]).isoformat(),
                "decision_price": round(float(closes[t]), 6),
                "actual_price": round(float(closes[target_i]), 6),
                "predicted_price": round(float(closes[t] * np.exp(pred)), 6),
                "predicted_return": round(float(pred), 8),
                "actual_return": round(float(_actual), 8),
            }
        )
    return points


def regime_series_by_ticker(
    csv_path: str,
    tickers: list[str],
    overrides: dict | None = None,
) -> dict:
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
    overrides = overrides or {}
    cfg_kwargs = {k: overrides[k] for k in _PREDICTION_OVERRIDE_KEYS if k in overrides}
    regime_cfg = PredictionConfig(**cfg_kwargs).to_regime_config()
    results = {}
    for tk in tickers:
        sub = df[df["ticker"] == tk].sort_values("timestamp")
        if sub.empty:
            results[tk] = {"error": f"no rows for ticker {tk!r}"}
            continue
        frame = build_regime_frame(
            sub["close"].to_numpy(),
            cfg=regime_cfg,
            timestamps=sub["timestamp"].to_numpy(),
        )
        results[tk] = [
            {
                "ts": pd.Timestamp(row.timestamp).isoformat(),
                "regime_score": round(float(row.regime_score), 4),
                "p_bull": round(float(row.p_bull), 4),
                "p_bear": round(float(row.p_bear), 4),
                "p_side": round(float(row.p_side), 4),
                "hmm_signed": int(row.hmm_signed),
                "hmm_label": row.hmm_label,
                "state_label": row.state_label,
            }
            for row in frame.itertuples(index=False)
        ]
    return results


def model_chart_by_ticker(
    csv_path: str,
    tickers: list[str],
    overrides: dict | None,
    ml_performance: dict,
    regimes: dict,
    news_series: dict | None = None,
) -> dict:
    """OHLC + model decision + regime diagnostics for the dashboard tape.

    Projected stop and target levels are visual risk references derived from
    the same ATR stop distance used for sizing. They are deliberately labeled
    as references in the UI because the strategy does not yet submit broker-
    side protective orders.
    """
    overrides = overrides or {}
    atr_period = max(1, int(overrides.get("atr_period", 14)))
    atr_mult = float(overrides.get("atr_stop_mult", 2.0))
    entry_threshold = float(overrides.get("entry_threshold", 0.001))
    target_rr = 2.0
    df = pd.read_csv(csv_path)
    df["timestamp"] = pd.to_datetime(df["timestamp"], format="mixed", utc=True)
    results = {}
    for ticker in tickers:
        sub = df[df["ticker"] == ticker].sort_values("timestamp").copy()
        if sub.empty:
            results[ticker] = []
            continue
        prev_close = sub["close"].shift(1)
        tr = pd.concat(
            [
                sub["high"] - sub["low"],
                (sub["high"] - prev_close).abs(),
                (sub["low"] - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        atr = tr.rolling(atr_period, min_periods=atr_period).mean()
        predictions = {
            item.get("decision_ts"): item
            for item in ml_performance.get(ticker, {}).get("price_series", [])
        }
        regime_map = {
            item.get("ts"): item
            for item in regimes.get(ticker, [])
            if isinstance(item, dict)
        }
        news_map = {
            item.get("ts"): item.get("score")
            for item in (news_series or {}).get(ticker, [])
            if isinstance(item, dict)
        }
        rows = []
        for idx, row in enumerate(sub.itertuples(index=False)):
            ts = pd.Timestamp(row.timestamp).isoformat()
            prediction = predictions.get(ts, {})
            regime = regime_map.get(ts, {})
            predicted_return = prediction.get("predicted_return")
            signal = "WARMUP"
            if predicted_return is not None:
                if predicted_return > entry_threshold:
                    signal = "BUY"
                elif predicted_return < -entry_threshold:
                    signal = "SELL"
                else:
                    signal = "HOLD"
            atr_value = atr.iloc[idx]
            atr_value = None if pd.isna(atr_value) else float(atr_value)
            risk_distance = atr_mult * atr_value if atr_value is not None else None
            direction = 1 if signal == "BUY" else -1 if signal == "SELL" else 0
            close = float(row.close)
            stop = close - direction * risk_distance if direction and risk_distance else None
            target = close + direction * risk_distance * target_rr if direction and risk_distance else None
            rows.append(
                {
                    "ts": ts,
                    "open": round(float(row.open), 6),
                    "high": round(float(row.high), 6),
                    "low": round(float(row.low), 6),
                    "close": round(close, 6),
                    "volume": round(float(row.volume), 6),
                    "predicted_price": prediction.get("predicted_price"),
                    "predicted_return": predicted_return,
                    "signal": signal,
                    "entry_threshold": entry_threshold,
                    "atr": round(atr_value, 6) if atr_value is not None else None,
                    "stop_loss": round(stop, 6) if stop is not None else None,
                    "take_profit": round(target, 6) if target is not None else None,
                    "target_rr": target_rr,
                    "news_score": news_map.get(ts),
                    **{
                        key: regime.get(key)
                        for key in (
                            "state_label",
                            "regime_score",
                            "p_bull",
                            "p_bear",
                            "p_side",
                            "hmm_label",
                            "hmm_signed",
                        )
                    },
                }
            )
        results[ticker] = rows[-MAX_MODEL_CHART_POINTS:]
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
    include_extended_hours: bool = False,
) -> dict:
    run_id = run_id or f"bt_{int(started_at)}_{uuid.uuid4().hex[:8]}"
    profile = get_asset_profile(asset_class)
    metrics = compute_metrics(engine, venue, starting_cash, asset_class)
    news_series = news_series_by_ticker(csv_path, tickers, overrides)
    ml_performance = ml_performance_by_ticker(
        csv_path, tickers, overrides, news_series=news_series
    )
    regimes = regime_series_by_ticker(csv_path, tickers, overrides)
    artifact = {
        "run_id": run_id,
        "kind": "backtest",
        "started_at": _iso(started_at),
        "finished_at": _iso(time.time()),
        "asset_class": asset_class,
        "asset_profile": profile,
        "objective_metric": profile["scoring"]["metric"],
        "include_extended_hours": include_extended_hours,
        "market_session": (
            "Regular + extended hours"
            if asset_class == "equity" and include_extended_hours
            else profile["market"]["session"]
        ),
        "bar_interval_minutes": infer_bar_interval_minutes_from_csv(csv_path, tickers),
        "tickers": tickers,
        "starting_cash": starting_cash,
        "params": overrides or {},
        "metrics": metrics.as_dict(),
        "equity_curve": equity_curve_with_timestamps(engine, venue),
        "positions": _dataframe_records(_positions_report(engine), MAX_POSITIONS_RECORDED),
        "fills": _dataframe_records(_fills_report(engine), MAX_FILLS_RECORDED),
        "ml_performance": ml_performance,
        "news": news_series,
        "regime": regimes,
        "model_chart": model_chart_by_ticker(
            csv_path, tickers, overrides, ml_performance, regimes, news_series
        ),
        "model_chart_meta": {
            "hmm_train_window": 750,
            "hmm_decode_window": 250,
            "stop_target_kind": "ATR_REFERENCE_NOT_BROKER_ORDER",
            "decision_source": "OFFLINE_WALK_FORWARD_RECONSTRUCTION",
        },
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
    include_extended_hours: bool = False,
    validation_metadata: dict | None = None,
) -> dict:
    run_id = run_id or f"opt_{int(started_at)}_{uuid.uuid4().hex[:8]}"
    trials = [
        {
            "number": t.number,
            "state": t.state.name,
            "value": t.value,
            "params": t.params,
            "user_attrs": t.user_attrs,
        }
        for t in study.get_trials(deepcopy=False)
    ]
    best_params = study.best_params
    scoring_params = {**best_params, **(structural_overrides or {})}
    profile = get_asset_profile(asset_class)
    news_series = news_series_by_ticker(oos_path, tickers, scoring_params)
    ml_performance = ml_performance_by_ticker(
        oos_path, tickers, scoring_params, news_series=news_series
    )
    regimes = regime_series_by_ticker(oos_path, tickers, scoring_params)
    oos_metrics = compute_metrics(oos_engine, VENUE, starting_cash, asset_class)
    # The persisted optimization score is authoritative. Keeping the report's
    # objective field identical prevents the UI from presenting the raw ratio
    # as if it already included the fill-activity penalty.
    oos_metrics.objective_score = round(float(oos_score), 6)
    artifact = {
        "run_id": run_id,
        "kind": "optimize",
        "started_at": _iso(started_at),
        "finished_at": _iso(time.time()),
        "asset_class": asset_class,
        "asset_profile": profile,
        "objective_metric": profile["scoring"]["metric"],
        "include_extended_hours": include_extended_hours,
        "market_session": (
            "Regular + extended hours"
            if asset_class == "equity" and include_extended_hours
            else profile["market"]["session"]
        ),
        "bar_interval_minutes": infer_bar_interval_minutes_from_csv(oos_path, tickers),
        "tickers": tickers,
        "starting_cash": starting_cash,
        "seed": seed,
        "n_trials_requested": n_trials_requested,
        "train_frac": train_frac,
        "validation": validation_metadata or {},
        "target_score": target_score,
        "resumed_from": resumed_from,
        # The live bar width this run's data was fetched at (see optimize.py's
        # --ibkr-bar-hours / --fetch-missing), so a later paper/live run
        # started from this run's params can subscribe to bars of the same
        # width instead of assuming daily. None when the run didn't fetch via
        # IBKR at a non-default cadence (data's real granularity isn't known).
        "ibkr_bar_hours": ibkr_bar_hours,
        # Structural alpha/risk settings remain top-level so run_live.py can
        # replay the complete winning contract, not only Optuna-tuned values.
        **(structural_overrides or {}),
        "trials": trials,
        "best_params": best_params,
        "in_sample_value": study.best_value,
        "oos_score": oos_score,
        "oos_metrics": oos_metrics.as_dict(),
        "oos_equity_curve": equity_curve_with_timestamps(oos_engine, VENUE),
        "ml_performance": ml_performance,
        "news": news_series,
        "regime": regimes,
        "model_chart": model_chart_by_ticker(
            oos_path, tickers, scoring_params, ml_performance, regimes, news_series
        ),
        "model_chart_meta": {
            "hmm_train_window": 750,
            "hmm_decode_window": 250,
            "stop_target_kind": "ATR_REFERENCE_NOT_BROKER_ORDER",
            "decision_source": "OFFLINE_WALK_FORWARD_RECONSTRUCTION",
        },
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
                "objective_metric": data.get("objective_metric"),
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
