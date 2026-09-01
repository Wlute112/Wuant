"""Job-orchestration routes: trigger backtest/optuna/paper/live as subprocess
jobs, poll their status/logs, and cancel them.

/api/jobs/live is the one route shaped to deploy real capital. It is currently
fail-closed by the production-readiness gate before confirmation, port, or
subprocess handling. The confirmation phrase and port checks remain as
independent defense-in-depth controls for a future reviewed activation.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Query

from quant.api.jobs import JOBS_DIR, JobManager
from quant.api.schemas import (
    LIVE_CONFIRM_PHRASE,
    BacktestJobRequest,
    LiveJobRequest,
    OptimizeJobRequest,
    PaperJobRequest,
)
from quant.run.asset_profiles import get_asset_profile
from quant.run.readiness import live_readiness_status

router = APIRouter(prefix="/api/jobs", tags=["jobs"])
manager: JobManager | None = None  # set by quant.api.main at startup


def _args_from(flag_value_pairs) -> list[str]:
    args: list[str] = []
    for flag, value in flag_value_pairs:
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            if value:
                args.extend([flag, *[str(v) for v in value]])
            continue
        args.extend([flag, str(value)])
    return args


def _safe_execution_config(req, *, redact_confirmation: bool = False) -> dict:
    config = req.model_dump()
    if config.get("account_id"):
        config["account_id"] = "<redacted-account>"
    if redact_confirmation and "confirm" in config:
        config["confirm"] = "<redacted>"
    return config


def _write_params_file(
    job_id: str, base_path: str | None, base_params: dict | None, overrides: dict
) -> str | None:
    """Merge feature/risk overrides on top of an optional base params source
    and write the combined dict to a job-scoped temp file. ``base_params``
    (e.g. an Optuna run's best_params, loaded client-side via GET
    /api/runs/{run_id}) takes precedence over ``base_path`` (an existing
    params JSON file) when both are given. Returns the path to pass as
    --params, or the untouched base_path when there's nothing to write.
    """
    merged: dict = {}
    if base_params:
        merged = dict(base_params)
    elif base_path:
        with open(base_path) as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict):
            merged = loaded
    if not merged and not overrides:
        return base_path
    merged.update(overrides)
    out_path = JOBS_DIR / f"{job_id}_params.json"
    with open(out_path, "w") as fh:
        json.dump(merged, fh)
    return str(out_path)


def _submit_execution_with_supervisor(
    kind: str,
    req,
    job_id: str,
    args: list[str],
    *,
    config: dict,
) -> dict:
    telemetry_path = JOBS_DIR / f"{job_id}_telemetry.json"
    operations_path = JOBS_DIR / "operations.sqlite3"
    strategy_component = f"strategy:{job_id}"
    supervisor_component = f"supervisor:{job_id}"
    news_component = f"news:{job_id}"
    args += [
        "--telemetry-path",
        str(telemetry_path),
        "--operations-db",
        str(operations_path),
        "--operations-component-id",
        strategy_component,
        "--external-supervisor-component",
        supervisor_component,
        "--require-external-supervisor",
        "--news-operations-component-id",
        news_component,
    ]
    execution = manager.submit(
        kind,
        "quant.run.run_live",
        args,
        config=config,
        job_id=job_id,
    )
    # Test doubles and older embedded callers may implement only the historical
    # JobManager surface. The production manager always launches the companion.
    if not hasattr(manager, "link_companion"):
        return execution
    campaign_id = "paper:" + ":".join(
        [str(req.asset_class), ",".join(sorted(req.tickers)), str(req.bar_hours)]
    )
    supervisor_id = manager.new_job_id("risk_supervisor")
    supervisor_args = [
        "--operations-db",
        str(operations_path),
        "--telemetry-path",
        str(telemetry_path),
        "--execution-job-id",
        job_id,
        "--strategy-target",
        strategy_component,
        "--component-id",
        supervisor_component,
        "--campaign-id",
        campaign_id,
        "--news-component",
        news_component,
    ]
    redis_prefix = getattr(getattr(manager, "store", None), "prefix", None)
    if redis_prefix:
        supervisor_args += ["--redis-prefix", str(redis_prefix)]
    supervisor = manager.submit(
        "risk_supervisor",
        "quant.ops.supervisor",
        supervisor_args,
        config={"execution_job_id": job_id, "campaign_id": campaign_id},
        job_id=supervisor_id,
        parent_job_id=job_id,
    )
    manager.link_companion(job_id, supervisor_id)
    return {**execution, "supervisor_job_id": supervisor["id"], "operations_db": str(operations_path)}


@router.post("/backtest", status_code=202)
def start_backtest(req: BacktestJobRequest):
    job_id = manager.new_job_id("backtest")
    overrides = {**req.features.as_overrides(), **req.risk.as_overrides()}
    params_path = _write_params_file(job_id, req.params_path, req.params, overrides)
    args = _args_from(
        [
            ("--csv", req.csv),
            ("--asset-class", req.asset_class),
            ("--tickers", req.tickers),
            ("--cash", req.cash),
            ("--params", params_path),
        ]
    )
    if req.ibkr.fetch_missing and req.ibkr.replace_bars:
        raise HTTPException(400, "fetch_missing and replace_bars are mutually exclusive")
    if req.ibkr.fetch_missing or req.ibkr.replace_bars:
        args += _args_from(
            [
                ("--ibkr-host", req.ibkr.ibkr_host),
                ("--ibkr-port", req.ibkr.ibkr_port),
                ("--ibkr-client-id", req.ibkr.ibkr_client_id),
                ("--ibkr-years", req.ibkr.ibkr_years),
            ]
        )
        if req.ibkr.replace_bars:
            bar_hours = req.ibkr.ibkr_bar_hours or get_asset_profile(req.asset_class)["defaults"]["bar_hours"]
            args += _args_from([("--ibkr-bar-hours", bar_hours)]) + ["--replace-bars"]
        else:
            args += ["--fetch-missing"]
        if req.ibkr.include_extended_hours:
            args += ["--include-extended-hours"]
    return manager.submit(
        "backtest", "quant.run.run_backtest", args, config=req.model_dump(), job_id=job_id
    )


@router.post("/optimize", status_code=202)
def start_optimize(req: OptimizeJobRequest):
    job_id = manager.new_job_id("optimize")
    overrides = {**req.features.as_overrides(), **req.risk.as_overrides()}
    structural_path = None
    if overrides:
        structural_path = JOBS_DIR / f"{job_id}_structural.json"
        with open(structural_path, "w") as fh:
            json.dump(overrides, fh)
    args = _args_from(
        [
            ("--csv", req.csv),
            ("--asset-class", req.asset_class),
            ("--tickers", req.tickers),
            ("--trials", req.trials),
            ("--score", req.score),
            ("--final-test-frac", req.final_test_frac),
            ("--walk-forward-folds", req.walk_forward_folds),
            ("--embargo-bars", req.embargo_bars),
            ("--stability-std-weight", req.stability_std_weight),
            ("--turnover-penalty-weight", req.turnover_penalty_weight),
            ("--cost-sensitivity-weight", req.cost_sensitivity_weight),
            ("--min-positive-fold-fraction", req.min_positive_fold_fraction),
            ("--normal-slippage-probability", req.normal_slippage_probability),
            ("--stress-cost-multiplier", req.stress_cost_multiplier),
            ("--cash", req.cash),
            ("--seed", req.seed),
            ("--warmup-bars", req.warmup_bars),
            ("--min-train-bars", req.min_train_bars),
            ("--structural-json", str(structural_path) if structural_path else None),
            ("--resume-run-id", req.resume_run_id),
        ]
    )
    if req.ibkr.fetch_missing and req.ibkr.replace_bars:
        raise HTTPException(400, "fetch_missing and replace_bars are mutually exclusive")
    if req.ibkr.fetch_missing or req.ibkr.replace_bars:
        args += _args_from(
            [
                ("--ibkr-host", req.ibkr.ibkr_host),
                ("--ibkr-port", req.ibkr.ibkr_port),
                ("--ibkr-client-id", req.ibkr.ibkr_client_id),
                ("--ibkr-years", req.ibkr.ibkr_years),
            ]
        )
        if req.ibkr.replace_bars:
            bar_hours = req.ibkr.ibkr_bar_hours or get_asset_profile(req.asset_class)["defaults"]["bar_hours"]
            args += _args_from([("--ibkr-bar-hours", bar_hours)]) + ["--replace-bars"]
        else:
            args += ["--fetch-missing"]
        if req.ibkr.include_extended_hours:
            args += ["--include-extended-hours"]
    return manager.submit(
        "optimize", "quant.optimize.optimize", args, config=req.model_dump(), job_id=job_id
    )


@router.post("/paper", status_code=202)
def start_paper(req: PaperJobRequest):
    if req.asset_class == "crypto":
        raise HTTPException(
            400,
            "IBKR paper accounts do not support spot-crypto execution. "
            "Use the crypto backtest/demo feed, or select Equity for broker paper trading.",
        )
    if req.port in {7496, 4001}:
        raise HTTPException(
            400,
            f"Refusing to start a paper-trading job on LIVE port {req.port}. "
            "Use /api/jobs/live for live trading.",
        )
    if req.allow_shorts:
        raise HTTPException(
            400,
            "Short selling is disabled until borrow, SSR, margin, recall, and "
            "forced-buy-in controls are implemented.",
        )
    job_id = manager.new_job_id("paper")
    params_path = _write_params_file(job_id, req.params_path, req.params, {})
    args = _args_from(
        [
            ("--tickers", req.tickers),
            ("--asset-class", req.asset_class),
            ("--primary-exchange", req.primary_exchange or None),
            ("--host", req.host),
            ("--port", req.port),
            ("--client-id", req.client_id),
            ("--account-id", req.account_id),
            ("--cash", req.cash),
            ("--params", params_path),
            ("--bar-hours", req.bar_hours),
            ("--redis-host", req.redis_host),
            ("--redis-port", req.redis_port),
        ]
    )
    if req.allow_shorts:
        args.append("--allow-shorts")
    if req.include_extended_hours:
        args.append("--include-extended-hours")
    return _submit_execution_with_supervisor(
        "paper",
        req,
        job_id,
        args,
        config=_safe_execution_config(req),
    )


@router.post("/live", status_code=202)
def start_live(req: LiveJobRequest):
    readiness = live_readiness_status()
    if not readiness["live_capital_enabled"]:
        raise HTTPException(
            503,
            {
                "code": readiness["code"],
                "message": "Live capital is disabled until every P0 production-readiness gate passes.",
                "incomplete": readiness["incomplete"],
            },
        )
    if req.allow_shorts:
        raise HTTPException(
            400,
            "Short selling is disabled until borrow, SSR, margin, recall, and "
            "forced-buy-in controls are implemented.",
        )
    if req.confirm != LIVE_CONFIRM_PHRASE:
        raise HTTPException(
            400,
            f"Confirmation phrase did not match. Live trading NOT started. "
            f"Expected exact phrase: {LIVE_CONFIRM_PHRASE!r}",
        )
    if req.port in {7497, 4002}:
        raise HTTPException(
            400,
            f"Refusing to run --live on paper port {req.port}. Pass the live "
            "TWS/Gateway port (e.g. 7496) explicitly.",
        )
    job_id = manager.new_job_id("live")
    params_path = _write_params_file(job_id, req.params_path, req.params, {})
    args = _args_from(
        [
            ("--tickers", req.tickers),
            ("--asset-class", req.asset_class),
            ("--primary-exchange", req.primary_exchange or None),
            ("--host", req.host),
            ("--port", req.port),
            ("--client-id", req.client_id),
            ("--account-id", req.account_id),
            ("--cash", req.cash),
            ("--params", params_path),
            ("--bar-hours", req.bar_hours),
            ("--redis-host", req.redis_host),
            ("--redis-port", req.redis_port),
        ]
    ) + ["--live"]
    if req.allow_shorts:
        args.append("--allow-shorts")
    if req.include_extended_hours:
        args.append("--include-extended-hours")
    safe_config = _safe_execution_config(req, redact_confirmation=True)
    job = _submit_execution_with_supervisor(
        "live",
        req,
        job_id,
        args,
        config=safe_config,
    )
    return {**job, "warning": "LIVE TRADING ARMED - REAL CAPITAL AT RISK"}


@router.get("")
def list_jobs():
    return manager.list()


@router.get("/{job_id}")
def get_job(job_id: str):
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(404, f"job {job_id!r} not found")
    return job


@router.get("/{job_id}/logs")
def get_job_logs(job_id: str, tail_lines: int = Query(default=200, le=5000)):
    result = manager.logs(job_id, tail_lines)
    if result is None:
        raise HTTPException(404, f"job {job_id!r} not found")
    return result


@router.post("/{job_id}/cancel")
def cancel_job(job_id: str):
    result = manager.cancel(job_id)
    if result is None:
        raise HTTPException(404, f"job {job_id!r} not found")
    return result
