"""Comparable, version-bound observations; never invent unavailable execution data."""
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


@lru_cache(maxsize=1)
def implementation_version():
    root = Path(__file__).resolve().parents[1]
    files = ("models/prediction_engine.py", "models/regime.py", "models/cross_asset.py",
             "models/industry.py", "strategies/ml_strategy.py")
    return digest({name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files})


def review_contract(config, *, extended=False):
    """Hash resolved alpha/strategy knobs, code, universe and data semantics.

    Runtime account IDs, paths, execution venue and monitoring flags are excluded.
    A new process is required after source edits, just as for the TradingNode.
    """
    keys = list(config)
    start, end = keys.index("n_lags"), keys.index("refit_every_n_bars")
    params = {k: config[k] for k in keys[start:end + 1] if k != "news_data_path"}
    params.update({k: config[k] for k in ("min_train_bars", "warmup_bars")})
    contract = {"implementation": implementation_version(), "params": params,
                "asset_class": config["asset_class"],
                "tickers": sorted(symbol(i) for i in config["instrument_ids"]),
                "bar_type": config["bar_type_suffix"], "extended_hours": bool(extended)}
    # Canonical JSON types keep tuple-valued config fields equal after disk I/O.
    return json.loads(json.dumps({**contract, "model_version": digest(contract)}))


def symbol(instrument):
    return str(instrument).rsplit(".", 1)[0].split("/", 1)[0]


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Evidence timestamps require a timezone")
    return parsed.astimezone(timezone.utc)


def telemetry_evidence(payload, source_id):
    if payload.get("mock") is not False or payload.get("mode") not in {"paper", "shadow"}:
        raise ValueError("Only explicit non-demo paper or shadow telemetry can be reviewed")
    contract = payload.get("model", {}).get("review_contract")
    if not contract:
        raise ValueError("Telemetry predates model version binding; restart the session")
    horizon = max(1, int(contract["params"]["horizon"]))
    samples = []
    series = payload.get("series", {})
    # Comparison thins by horizon within each symbol. Unmatured forecasts
    # but never receive a target from an earlier bar or a future timestamp.
    for ticker, points in series.items():
        points = sorted({p["ts"]: p for p in points}.values(), key=lambda p: timestamp(p["ts"]))
        for i, point in enumerate(points):
            if point.get("predicted_return") is None:
                continue
            row = {"id": f"forecast:{ticker}:{point['ts']}", "ts": point["ts"], "ticker": ticker,
                   "regime": point.get("state_label") or "Unknown",
                   "prediction": point["predicted_return"],
                   "feature_age_seconds": point.get("feature_age_seconds")}
            if i + horizon < len(points):
                target = points[i + horizon]
                if timestamp(target["ts"]) <= timestamp(payload["as_of"]) and point["close"] > 0 and target["close"] > 0:
                    row["actual"] = math.log(target["close"] / point["close"])
                    row["target_ts"] = target["ts"]
            samples.append(row)
    # One sample per terminal entry order. Reference is the last completed
    # decision close strictly before submission, not an NBBO/arrival quote.
    for order in payload.get("risk", {}).get("orders", []):
        if order.get("role") != "ENTRY" or order.get("status") not in {"FILLED", "CANCELED", "EXPIRED", "REJECTED", "DENIED"}:
            continue
        submitted = order.get("submitted_ts_ns")
        if not submitted:
            continue
        ts = datetime.fromtimestamp(submitted / 1e9, timezone.utc)
        ticker = symbol(order["instrument_id"])
        row = {"id": f"order:{order['client_order_id']}", "ts": ts.isoformat(), "ticker": ticker,
               "regime": "Unknown", "rejected": int(order["status"] in {"REJECTED", "DENIED"})}
        prior = [p for p in series.get(ticker, []) if timestamp(p["ts"]) <= ts]
        if prior and order.get("filled_quantity", 0) > 0:
            decision = max(prior, key=lambda p: timestamp(p["ts"]))
            row["regime"] = decision.get("state_label") or "Unknown"
            reference = decision["close"]
            if reference > 0 and order.get("average_fill_price", 0) > 0:
                row["slippage_bps"] = (order["average_fill_price"] / reference - 1) * 10000 * (1 if order["side"] == "BUY" else -1)
        samples.append(row)
    return {"source_id": source_id, "kind": payload["mode"], "contract": contract,
            "reference": f"Strategy telemetry {source_id}; completed bars and terminal entry orders",
            "provenance": {"basis": "strategy_telemetry", "status": payload.get("status"),
                           "bar_type": payload.get("bar_type"), "started_at": payload.get("started_at")},
            "as_of": payload["as_of"], "samples": samples}


def research_evidence(run):
    contract = run.get("review_contract")
    if not contract:
        raise ValueError("Run predates model version binding; create a fresh backtest")
    if run.get("kind") != "backtest":
        raise ValueError("Only standalone backtests may be imported; holdout artifacts are excluded")
    samples = []
    horizon = max(1, int(contract["params"]["horizon"]))
    for ticker, result in run.get("ml_performance", {}).items():
        points = result.get("price_series", [])
        regimes = {p["ts"]: p.get("state_label", "Unknown") for p in run.get("regime", {}).get(ticker, [])}
        last_target = None
        for point in sorted(points, key=lambda p: timestamp(p["decision_ts"])):
            decision = timestamp(point["decision_ts"])
            if last_target is not None and decision < last_target:
                continue
            last_target = timestamp(point["target_ts"])
            samples.append({"id": f"forecast:{ticker}:{point['decision_ts']}", "ts": point["decision_ts"],
                            "ticker": ticker, "regime": regimes.get(point["decision_ts"], "Unknown"),
                            "prediction": point["predicted_return"], "actual": point["actual_return"],
                            "target_ts": point["target_ts"]})
    return {"source_id": run["run_id"], "kind": "research", "contract": contract,
            "reference": f"Backtest {run['run_id']}; horizon {horizon}, non-overlapping OOS targets; execution/freshness unavailable",
            "provenance": {"dataset": run.get("dataset_provenance"), "source_csv": run.get("source_csv"),
                           "simulation_status": (run.get("equity_simulation") or {}).get("calibration_status")},
            "as_of": run["finished_at"], "samples": samples}
