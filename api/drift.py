"""Read-only research/execution agreement review with durable local alert history."""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock, Thread

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from quant.api import jobs_routes
from quant.api.jobs import JOBS_DIR
from quant.run.artifacts import load_run
from quant.run.telemetry import load_telemetry

router = APIRouter(prefix="/api/drift", tags=["drift"])
_lock = Lock()
_stop = Event()
_worker: Thread | None = None


class ReviewConfig(BaseModel):
    baseline_run_id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_-]+$")
    min_matched: int = Field(default=20, ge=1, le=10000)
    max_prediction_mae_bps: float = Field(default=20, ge=0, le=10000)
    max_prediction_magnitude_shift_bps: float = Field(default=20, ge=0, le=10000)
    max_signal_disagreement_pct: float = Field(default=20, ge=0, le=100)
    max_regime_disagreement_pct: float = Field(default=30, ge=0, le=100)
    max_bull_share_shift_pct: float = Field(default=30, ge=0, le=100)
    max_slippage_bps: float = Field(default=30, ge=0, le=10000)
    max_reject_rate_pct: float = Field(default=5, ge=0, le=100)
    max_data_age_minutes: float = Field(default=60, ge=1, le=10080)


def _job(job_id: str) -> dict:
    manager = jobs_routes.manager
    job = manager.get(job_id) if manager else None
    if job is None or job.get("kind") not in {"paper", "live"}:
        raise HTTPException(404, "paper/live job not found")
    return job


def _path(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}_drift.json"


def _read(job_id: str) -> dict:
    try:
        return json.loads(_path(job_id).read_text())
    except (OSError, ValueError):
        return {"config": None, "alerts": [], "active_alerts": []}


def _write(job_id: str, record: dict) -> None:
    path = _path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w") as stream:
            json.dump(record, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _finite(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def _stamp(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _bar_minutes(bar_type: str):
    import re
    match = re.search(r"(?:^|-)(\d+)-(HOUR|MINUTE|DAY)(?:-|$)", str(bar_type).upper())
    if not match:
        return None
    return int(match[1]) * {"MINUTE": 1, "HOUR": 60, "DAY": 1440}[match[2]]


def _metric(value, count, threshold, *, sample_ok=True, unit=""):
    if value is None or count == 0:
        status = "unavailable"
    elif not sample_ok:
        status = "insufficient_sample"
    else:
        status = "alert" if value > threshold else "within_threshold"
    return {"value": round(value, 3) if value is not None else None, "count": count,
            "threshold": threshold, "unit": unit, "status": status}


def _proportion_metric(successes, count, threshold, minimum):
    value = 100 * successes / count if count else None
    metric = _metric(value, count, threshold, sample_ok=count >= minimum, unit="%")
    if count:
        # Wilson 95% interval, including at zero and full observed rates.
        z = 1.96
        p = successes / count
        denominator = 1 + z * z / count
        center = (p + z * z / (2 * count)) / denominator
        radius = z * math.sqrt(p * (1 - p) / count + z * z / (4 * count * count)) / denominator
        metric["uncertainty_95_pct"] = [round(100 * (center - radius), 2), round(100 * (center + radius), 2)]
    return metric


def _review(job: dict, config: ReviewConfig):
    baseline = load_run(config.baseline_run_id)
    if baseline is None:
        raise HTTPException(404, "baseline run not found")
    telemetry = load_telemetry(JOBS_DIR / f"{job['id']}_telemetry.json")
    if telemetry is None or telemetry.get("mock"):
        return {"status": "awaiting_telemetry", "baseline_run_id": config.baseline_run_id,
                "metrics": {}, "coverage": {"matched": 0}, "notes": ["No observed strategy telemetry is available."]}
    asset_class = job.get("config", {}).get("asset_class", "crypto")
    if baseline.get("asset_class") != asset_class or telemetry.get("asset_class") != asset_class:
        raise HTTPException(409, "baseline and execution asset profiles differ")
    baseline_minutes = baseline.get("bar_interval_minutes")
    execution_minutes = _bar_minutes(telemetry.get("bar_type", ""))
    if baseline_minutes is not None and execution_minutes is not None and int(baseline_minutes) != execution_minutes:
        raise HTTPException(409, "baseline and execution bar cadences differ")

    baseline_by_ticker = {}
    for ticker, rows in (baseline.get("model_chart") or {}).items():
        baseline_by_ticker[ticker] = {_stamp(row["ts"]): row for row in rows if isinstance(row, dict) and row.get("ts")}
    errors = []
    signal_diffs = []
    regime_diffs = []
    matching = []
    observed = 0
    observed_predictions = []
    observed_regimes = []
    baseline_predictions = []
    baseline_regimes = []
    for rows in baseline_by_ticker.values():
        for row in rows.values():
            value = _finite(row.get("predicted_return"))
            if value is not None:
                baseline_predictions.append(value)
            if row.get("state_label") is not None:
                baseline_regimes.append(row["state_label"])
    for ticker, rows in (telemetry.get("series") or {}).items():
        for row in rows:
            if not isinstance(row, dict) or row.get("predicted_return") is None:
                continue
            observed += 1
            value = _finite(row.get("predicted_return"))
            if value is not None:
                observed_predictions.append(value)
            if row.get("state_label") is not None:
                observed_regimes.append(row["state_label"])
            reference = baseline_by_ticker.get(ticker, {}).get(_stamp(row.get("ts")))
            if not reference or reference.get("predicted_return") is None:
                continue
            actual = _finite(row.get("predicted_return"))
            expected = _finite(reference.get("predicted_return"))
            if actual is None or expected is None:
                continue
            matching.append({"ticker": ticker, "ts": row["ts"]})
            errors.append(abs(actual - expected) * 10000)
            # Compare alpha direction at the same timestamp. Execution policy may turn a sell into EXIT/HOLD.
            signal_diffs.append(int((actual > 0) != (expected > 0)))
            actual_regime = row.get("state_label")
            expected_regime = reference.get("state_label")
            if actual_regime is not None and expected_regime is not None:
                regime_diffs.append(int(actual_regime != expected_regime))

    risk = telemetry.get("risk") or {}
    orders = risk.get("orders") or []
    fills = risk.get("fills") or []
    rejected = sum(str(order.get("status", "")).lower() in {"rejected", "denied"} for order in orders)
    # Use the last observed decision close at or before the fill. This is an indicative
    # execution proxy; it does not claim arrival NBBO or broker statement reconciliation.
    closes = {}
    for ticker, rows in (telemetry.get("series") or {}).items():
        closes[ticker] = sorted(((_stamp(row.get("ts")), _finite(row.get("close"))) for row in rows
                                 if isinstance(row, dict)), key=lambda item: item[0] or datetime.min.replace(tzinfo=timezone.utc))
    slippage = []
    for fill in fills:
        ticker = str(fill.get("instrument_id", "")).split(".", 1)[0].split("/", 1)[0]
        fill_time = datetime.fromtimestamp(int(fill.get("ts_ns", 0)) / 1e9, tz=timezone.utc)
        prior = [price for ts, price in closes.get(ticker, []) if ts and ts <= fill_time and price and price > 0]
        price = _finite(fill.get("price"))
        if prior and price is not None:
            side = 1 if str(fill.get("side", "")).upper() in {"BUY", "B"} else -1
            slippage.append(side * (price / prior[-1] - 1) * 10000)
    age = None
    as_of = _stamp(telemetry.get("as_of"))
    if as_of:
        age = max(0, (datetime.now(timezone.utc) - as_of).total_seconds() / 60)
    enough = len(errors) >= config.min_matched
    distribution_ok = min(len(baseline_predictions), len(observed_predictions)) >= config.min_matched
    magnitude_shift = (
        abs(sum(abs(value) for value in observed_predictions) / len(observed_predictions)
            - sum(abs(value) for value in baseline_predictions) / len(baseline_predictions)) * 10000
        if observed_predictions and baseline_predictions else None
    )
    bull_shift = (
        abs(sum(value == "Bull" for value in observed_regimes) / len(observed_regimes)
            - sum(value == "Bull" for value in baseline_regimes) / len(baseline_regimes)) * 100
        if observed_regimes and baseline_regimes else None
    )
    metrics = {
        "prediction_mae_bps": _metric(sum(errors) / len(errors) if errors else None, len(errors), config.max_prediction_mae_bps, sample_ok=enough, unit="bps"),
        "prediction_magnitude_shift_bps": _metric(magnitude_shift, min(len(baseline_predictions), len(observed_predictions)), config.max_prediction_magnitude_shift_bps, sample_ok=distribution_ok, unit="bps"),
        "signal_disagreement_pct": _proportion_metric(sum(signal_diffs), len(signal_diffs), config.max_signal_disagreement_pct, config.min_matched),
        "regime_disagreement_pct": _proportion_metric(sum(regime_diffs), len(regime_diffs), config.max_regime_disagreement_pct, config.min_matched),
        "bull_share_shift_pct": _metric(bull_shift, min(len(baseline_regimes), len(observed_regimes)), config.max_bull_share_shift_pct, sample_ok=min(len(baseline_regimes), len(observed_regimes)) >= config.min_matched, unit="%"),
        "slippage_bps": _metric(sum(slippage) / len(slippage) if slippage else None, len(slippage), config.max_slippage_bps, sample_ok=len(slippage) >= config.min_matched, unit="bps"),
        "reject_rate_pct": _proportion_metric(rejected, len(orders), config.max_reject_rate_pct, config.min_matched),
        "data_age_minutes": _metric(age, 1 if age is not None else 0, config.max_data_age_minutes, unit="min"),
    }
    market_age_seconds = _finite((risk.get("session") or {}).get("data_age_seconds"))
    metrics["market_data_age_minutes"] = _metric(
        market_age_seconds / 60 if market_age_seconds is not None else None,
        1 if market_age_seconds is not None else 0,
        config.max_data_age_minutes,
        unit="min",
    )
    return {"status": "reviewed", "baseline_run_id": config.baseline_run_id,
            "telemetry_as_of": telemetry.get("as_of"), "metrics": metrics,
            "coverage": {"observed_predictions": observed, "baseline_predictions": len(baseline_predictions), "matched": len(matching),
                         "first_match": matching[0] if matching else None, "last_match": matching[-1] if matching else None,
                         "orders_retained": len(orders), "fills_retained": len(fills)},
            "notes": ["Decision and regime disagreement require exact ticker and bar timestamp overlap. Magnitude and bull-share shifts compare distributions across different periods; they do not measure paired forecast error.",
                      "Slippage uses the last recorded strategy close before each fill; NBBO and broker statement costs are unavailable.",
                      "Telemetry retains at most 750 bars per ticker and 100 recent orders/fills. Feature-level timestamps and shadow comparison are unavailable without their source feeds.",
                      "Different model parameters or data versions can explain distribution shifts; review the baseline contract before acting on alerts."]}


@router.get("/{job_id}")
def get_review(job_id: str):
    job = _job(job_id)
    with _lock:
        record = _read(job_id)
        if not record.get("config"):
            return {"status": "unconfigured", "config": None, "alerts": record.get("alerts", [])}
        config = ReviewConfig.model_validate(record["config"])
        result = _review(job, config)
        if result["status"] != "reviewed":
            return {**result, "config": record["config"], "alerts": record.get("alerts", [])}
        active = set(record.get("active_alerts", []))
        current = {key for key, metric in result["metrics"].items() if metric["status"] == "alert"}
        now = datetime.now(timezone.utc).isoformat()
        for key in sorted(current - active):
            record.setdefault("alerts", []).append({"ts": now, "baseline_run_id": config.baseline_run_id, "metric": key, "state": "triggered", "value": result["metrics"][key]["value"]})
        for key in sorted(active - current):
            record.setdefault("alerts", []).append({"ts": now, "baseline_run_id": config.baseline_run_id, "metric": key, "state": "cleared", "value": result["metrics"].get(key, {}).get("value")})
        record["active_alerts"] = sorted(current)
        record["alerts"] = record.get("alerts", [])[-200:]
        _write(job_id, record)
        return {**result, "config": record["config"], "alerts": record["alerts"]}


@router.put("/{job_id}/config")
def save_config(job_id: str, config: ReviewConfig):
    job = _job(job_id)
    baseline = load_run(config.baseline_run_id)
    if baseline is None:
        raise HTTPException(404, "baseline run not found")
    if baseline.get("asset_class") != job.get("config", {}).get("asset_class", "crypto"):
        raise HTTPException(409, "baseline and execution asset profiles differ")
    with _lock:
        record = _read(job_id)
        if (record.get("config") or {}).get("baseline_run_id") != config.baseline_run_id:
            record["active_alerts"] = []
        record["config"] = config.model_dump()
        _write(job_id, record)
    return {"config": record["config"]}


def _review_loop():
    while not _stop.wait(30):
        manager = jobs_routes.manager
        if manager is None:
            continue
        try:
            jobs = manager.list()
        except Exception:
            continue
        for job in jobs:
            if job.get("kind") not in {"paper", "live"} or job.get("status") not in {"starting", "running"}:
                continue
            if not _path(job["id"]).exists():
                continue
            try:
                get_review(job["id"])
            except Exception:
                # One malformed or incomplete snapshot cannot stop other reviews.
                continue


def start_scheduler():
    global _worker
    if _worker is not None and _worker.is_alive():
        return
    _stop.clear()
    _worker = Thread(target=_review_loop, name="quant-drift-review", daemon=True)
    _worker.start()


def stop_scheduler():
    _stop.set()
    if _worker is not None:
        _worker.join(timeout=2)
