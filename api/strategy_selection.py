"""Resolve a saved Optuna winner on the server; clients select IDs, never values."""
from __future__ import annotations

import math
import re

from fastapi import HTTPException

from quant.run.artifacts import load_run
from quant.run.asset_profiles import get_asset_profile
from quant.optimize.selection import holdout_assessment, REJECTED_OUTER_HOLDOUT, DEVELOPMENT_SELECTED
from quant.strategies.parameters import STRATEGY_ID, STRUCTURAL_KEYS, TUNABLE_KEYS


def selected_strategy(req) -> dict:
    run_id = req.source_run_id
    if not run_id:
        raise HTTPException(422, "Run an Optuna sweep, then select its saved result before execution.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", run_id):
        raise HTTPException(422, "Invalid Optuna run ID")
    try:
        run = load_run(run_id)
    except (OSError, ValueError) as exc:
        raise HTTPException(422, "Cannot read the selected Optuna result") from exc
    if run is None:
        raise HTTPException(404, "Selected Optuna result was not found; run a new sweep.")
    if not isinstance(run, dict):
        raise HTTPException(422, "The selected Optuna artifact must be a result object")
    if run.get("kind") != "optimize":
        raise HTTPException(422, "Select an Optuna optimization result")
    assessment = holdout_assessment(run)
    if run.get("selection_status") == DEVELOPMENT_SELECTED:
        raise HTTPException(422, "The selected development candidate has not passed its outer holdout.")
    if run.get("selection_status") == REJECTED_OUTER_HOLDOUT or (assessment and assessment["status"] == REJECTED_OUTER_HOLDOUT):
        raise HTTPException(422, "The selected development candidate failed its outer holdout and cannot be used for execution.")
    # Existing artifacts predate strategy IDs and belong to this strategy.
    if run.get("strategy_id", STRATEGY_ID) != STRATEGY_ID:
        raise HTTPException(422, "The selected result belongs to another strategy")
    if run.get("asset_class", "crypto") != req.asset_class:
        raise HTTPException(422, "The selected Optuna result uses another asset class")
    policy = run.get("allow_short_positions")
    if not isinstance(policy, bool):
        raise HTTPException(422, "The selected result does not record its long/short policy; run a new sweep.")
    if policy != bool(getattr(req, "allow_shorts", False)):
        raise HTTPException(422, "Long/short policy must match the selected Optuna result")
    tickers = req.tickers or get_asset_profile(req.asset_class)["defaults"]["tickers"]
    if set(run.get("tickers") or []) != set(tickers):
        raise HTTPException(422, "Ticker universe must match the selected Optuna result")
    params = run.get("best_params")
    score = run.get("in_sample_value")
    if (not isinstance(params, dict) or set(params) != TUNABLE_KEYS
            or not isinstance(score, (int, float)) or not math.isfinite(score) or score <= -1e6
            or any(not isinstance(v, (int, float, bool)) or not math.isfinite(v) for v in params.values())):
        raise HTTPException(422, "The selected Optuna result has no complete, usable winning candidate")
    cadence = run.get("bar_interval_minutes")
    execution = hasattr(req, "bar_hours")
    if execution and cadence not in {60, 120, 180, 240, 480, 1440}:
        raise HTTPException(422, "The selected Optuna result has no supported execution bar cadence; run a new sweep.")
    if execution and req.bar_hours is not None:
        if cadence is None or cadence != req.bar_hours * 60:
            raise HTTPException(422, "Bar cadence must match the selected Optuna result")
    if hasattr(req, "include_extended_hours") and bool(run.get("include_extended_hours")) != req.include_extended_hours:
        raise HTTPException(422, "Session hours must match the selected Optuna result")
    fixed = {key: run[key] for key in STRUCTURAL_KEYS if key in run}
    # Artifacts contain a simulation report; runners take just its configuration.
    if "equity_simulation" in fixed and isinstance(fixed["equity_simulation"], dict):
        fixed["equity_simulation"] = fixed["equity_simulation"].get("config", fixed["equity_simulation"])
    if req.asset_class != "equity":
        fixed.pop("equity_simulation", None)
    return {**fixed, "params": dict(params), "strategy_id": STRATEGY_ID,
            "source_run_id": run_id,
            "asset_class": req.asset_class,
            "include_extended_hours": bool(run.get("include_extended_hours")),
            "ibkr_bar_hours": cadence // 60 if execution else run.get("ibkr_bar_hours"),
            "bar_interval_minutes": cadence}
