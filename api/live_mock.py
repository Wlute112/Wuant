"""Live paper/account telemetry endpoints with an explicit demo fallback.

When a paper/live TradingNode is active, the strategy writes an atomic JSON
snapshot after every completed bar.  The API serves that snapshot directly.
If no matching node exists, a deterministic, clearly labelled demo tape keeps
the dashboard useful without implying that simulated values came from IBKR.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from quant.api import jobs_routes
from quant.api.jobs import JOBS_DIR
from quant.run.asset_profiles import get_asset_profile
from quant.run.telemetry import load_telemetry

router = APIRouter(prefix="/api/live", tags=["live"])


def _matching_job(job_id: str | None, asset_class: str, mode: str) -> dict | None:
    manager = jobs_routes.manager
    if manager is None:
        return None
    if job_id:
        job = manager.get(job_id)
        if job is None:
            raise HTTPException(404, f"job {job_id!r} not found")
        if job.get("kind") not in {"paper", "live"}:
            raise HTTPException(400, "telemetry is available only for paper/live jobs")
        if job.get("kind") != mode:
            raise HTTPException(409, "job mode does not match the selected workflow")
        if job.get("config", {}).get("asset_class", "crypto") != asset_class:
            raise HTTPException(409, "job asset class does not match the selected profile")
        return job

    candidates = [
        job
        for job in manager.list()
        if job.get("kind") == mode
        and job.get("config", {}).get("asset_class", "crypto") == asset_class
    ]
    if not candidates:
        return None
    running = [job for job in candidates if job.get("status") == "running"]
    return (running or candidates)[0]


def _telemetry_path(job: dict) -> Path:
    return JOBS_DIR / f"{job['id']}_telemetry.json"


def _demo_series(asset_class: str) -> tuple[str, list[dict]]:
    is_equity = asset_class == "equity"
    symbol = "QQQ" if is_equity else "BTC"
    base = 558.0 if is_equity else 62900.0
    amplitude = 3.7 if is_equity else 1250.0
    entry_threshold = 0.0025 if is_equity else 0.005
    step = timedelta(hours=24)
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    points: list[dict] = []
    for index in range(96):
        phase = index / 8.0
        center = base + math.sin(phase) * amplitude + index * amplitude * 0.012
        open_price = center - math.sin(phase * 1.7) * amplitude * 0.12
        close = center + math.cos(phase * 1.3) * amplitude * 0.09
        high = max(open_price, close) + amplitude * (0.08 + (index % 3) * 0.015)
        low = min(open_price, close) - amplitude * (0.07 + (index % 4) * 0.012)
        regime = 1 if math.sin(phase / 2.2) > 0.25 else -1 if math.sin(phase / 2.2) < -0.25 else 0
        p_bull = 0.62 if regime == 1 else 0.18 if regime == -1 else 0.31
        p_bear = 0.62 if regime == -1 else 0.16 if regime == 1 else 0.30
        p_side = 1.0 - p_bull - p_bear
        yhat = math.sin(phase * 1.4) * (0.0035 if is_equity else 0.008)
        signal = "BUY" if yhat > entry_threshold else "SELL" if yhat < -entry_threshold else "HOLD"
        atr = amplitude * 0.28
        side = 1 if signal == "BUY" else -1 if signal == "SELL" else 0
        points.append(
            {
                "ts": (now - step * (95 - index)).isoformat(),
                "open": round(open_price, 4),
                "high": round(high, 4),
                "low": round(low, 4),
                "close": round(close, 4),
                "volume": round(800_000 + 140_000 * math.sin(phase), 2),
                "complete": index < 95,
                "yhat": yhat,
                "predicted_price": round(close * math.exp(yhat), 4),
                "forecast": {
                    "horizon_bars": 1,
                    "open": round(close, 4),
                    "close": round(close * math.exp(yhat), 4),
                    "high": round(max(close, close * math.exp(yhat)) + atr * 0.5, 4),
                    "low": round(min(close, close * math.exp(yhat)) - atr * 0.5, 4),
                    "basis": "huber_close_atr_envelope",
                },
                "signal": signal,
                "atr": atr,
                "stop_reference": round(close - side * atr * 2.0, 4) if side else None,
                "take_profit_reference": round(close + side * atr * 4.0, 4) if side else None,
                "regime_score": p_bull - p_bear,
                "state_label": "Bull" if regime == 1 else "Bear" if regime == -1 else "Sideways",
                "p_bull": p_bull,
                "p_bear": p_bear,
                "p_side": p_side,
                "hmm_signed": regime,
                "hmm_label": "Bull" if regime == 1 else "Bear" if regime == -1 else "Sideways",
            }
        )
    return symbol, points


def _demo_payload(asset_class: str) -> dict:
    profile = get_asset_profile(asset_class)
    symbol, points = _demo_series(asset_class)
    last = points[-1]
    starting = 5_000.0
    equity = 5_123.45 if asset_class == "crypto" else 5_071.80
    return {
        "schema_version": 2,
        "mock": True,
        "status": "demo",
        "asset_class": asset_class,
        "mode": "paper",
        "bar_type": f"{profile['defaults']['bar_hours']}h",
        "as_of": datetime.now(timezone.utc).isoformat(),
        "tickers": [symbol],
        "series": {symbol: points},
        "positions": [
            {
                "symbol": symbol,
                "side": "LONG",
                "qty": 8 if asset_class == "equity" else 0.0142,
                "avg_price": last["close"] * 0.985,
                "entry_ts": points[-8]["ts"],
                "mark_price": last["close"],
                "unrealized_pnl": 0.0,
                "notional": (8 if asset_class == "equity" else 0.0142) * last["close"],
                "stop_loss": last["close"] * 0.985 - last["atr"] * 2.0,
                "take_profit": last["close"] * 0.985 + last["atr"] * 4.0,
                "reference_status": "model_reference",
            }
        ],
        "risk": {
            "equity": equity,
            "starting_equity": starting,
            "daily_pnl_pct": -0.8,
            "drawdown_pct": 2.1,
            "gross_leverage": 0.42,
            "kill_switch_engaged": False,
            "state": "ACTIVE",
            "rails": {
                "risk_budget_pct": 1.0,
                "hard_cap_pct": 0.25,
                "leverage_max": 1.0,
                "daily_loss_limit_pct": 2.0,
                "drawdown_warn_pct": 5.0,
                "kill_switch_pct": 10.0,
            },
        },
        "model": {
            "entry_threshold": 0.0025 if asset_class == "equity" else 0.005,
            "regime_window": profile["defaults"]["regime_window"],
            "hmm_train_window": 750,
            "hmm_decode_window": 250,
            "protective_orders_submitted": False,
            "reference_reward_risk": 2.0,
            "forecast_bar_basis": "predicted close with a half-ATR display envelope",
        },
        "profile": profile,
    }


@router.get("/telemetry")
def get_telemetry(
    job_id: str | None = None,
    asset_class: str = Query(default="crypto", pattern="^(crypto|equity)$"),
    mode: str = Query(default="paper", pattern="^(paper|live)$"),
):
    if mode not in {"paper", "live"}:  # direct Python calls bypass FastAPI coercion
        mode = "paper"
    job = _matching_job(job_id, asset_class, mode)
    if job is None:
        return {**_demo_payload(asset_class), "mode": mode}
    payload = load_telemetry(_telemetry_path(job))
    if payload is not None:
        return {**payload, "job_id": job["id"], "job_status": job["status"]}
    return {
        "schema_version": 2,
        "mock": False,
        "status": "connecting" if job["status"] == "running" else job["status"],
        "asset_class": asset_class,
        "mode": job["kind"],
        "as_of": job.get("started_at"),
        "job_id": job["id"],
        "job_status": job["status"],
        "tickers": job.get("config", {}).get("tickers", []),
        "series": {},
        "positions": [],
        "risk": {},
        "model": {},
        "profile": get_asset_profile(asset_class),
    }


@router.get("/positions")
def get_positions(
    job_id: str | None = None,
    asset_class: str = Query(default="crypto", pattern="^(crypto|equity)$"),
    mode: str = Query(default="paper", pattern="^(paper|live)$"),
):
    payload = get_telemetry(job_id=job_id, asset_class=asset_class, mode=mode)
    return {
        "mock": payload.get("mock", False),
        "status": payload.get("status"),
        "as_of": payload.get("as_of"),
        "positions": payload.get("positions", []),
    }


@router.get("/risk")
def get_risk(
    job_id: str | None = None,
    asset_class: str = Query(default="crypto", pattern="^(crypto|equity)$"),
    mode: str = Query(default="paper", pattern="^(paper|live)$"),
):
    payload = get_telemetry(job_id=job_id, asset_class=asset_class, mode=mode)
    return {
        "mock": payload.get("mock", False),
        "status": payload.get("status"),
        "as_of": payload.get("as_of"),
        **payload.get("risk", {}),
    }
