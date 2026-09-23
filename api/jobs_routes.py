"""Job-orchestration routes: trigger backtest/optuna/paper/live as subprocess
jobs, poll their status/logs, and cancel them.

/api/jobs/live is the one route shaped to deploy real capital. It is currently
fail-closed by the production-readiness gate before confirmation, port, or
subprocess handling. The confirmation phrase and port checks remain as
independent defense-in-depth controls for a future reviewed activation.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
from urllib.parse import urlparse
import uuid

from fastapi import APIRouter, HTTPException, Query, Request

from quant.api.jobs import JOBS_DIR, WORKDIR, JobManager
from quant.api.schemas import (
    LIVE_CONFIRM_PHRASE,
    BacktestJobRequest,
    DataPreflightRequest,
    DataRepairRequest,
    CampaignSeedJobRequest,
    CampaignStageJobRequest,
    LiveJobRequest,
    OptimizeJobRequest,
    PaperJobRequest,
    SectorEvidenceRequest,
)
from quant.run.asset_profiles import get_asset_profile
from quant.run.readiness import live_readiness_status

router = APIRouter(prefix="/api/jobs", tags=["jobs"])
manager: JobManager | None = None  # set by quant.api.main at startup
CAMPAIGNS_DIR = JOBS_DIR.parent / "optimize" / "campaigns"
PROMOTION_CONFIRM_PHRASE = "CONSUME OUTER HOLDOUT"
MAX_CSV_UPLOAD_BYTES = 100 * 1024 * 1024


@router.post("/sector-evidence/validate")
def validate_sector_upload(req: SectorEvidenceRequest):
    from quant.run.sector_risk import validate_sector_evidence
    try:
        evidence = validate_sector_evidence(req.evidence, symbols=req.tickers)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"evidence": evidence, "status": "VALIDATED",
            "note": "Source declarations validated. Qualified broker conId/symbol matching is checked by the execution node."}


def _safe_csv_upload_name(filename: str) -> str:
    basename = Path(filename).name
    if Path(basename).suffix.lower() != ".csv":
        raise HTTPException(400, "Select a CSV file.")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(basename).stem).strip(".-") or "data"
    return f"{stem[:80]}-{uuid.uuid4().hex[:10]}.csv"


@router.post("/upload-csv", status_code=201)
async def upload_csv(
    request: Request,
    filename: str = Query(..., min_length=1, max_length=255),
):
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_CSV_UPLOAD_BYTES:
                raise HTTPException(413, "CSV files are limited to 100 MB.")
        except ValueError as exc:
            raise HTTPException(400, "Invalid upload size header.") from exc
    upload_dir = JOBS_DIR / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    target = upload_dir / _safe_csv_upload_name(filename)
    partial = target.with_suffix(".uploading")
    total = 0
    try:
        with partial.open("wb") as stream:
            async for chunk in request.stream():
                total += len(chunk)
                if total > MAX_CSV_UPLOAD_BYTES:
                    raise HTTPException(413, "CSV files are limited to 100 MB.")
                stream.write(chunk)
        if total == 0:
            raise HTTPException(400, "The selected CSV is empty.")
        from quant.data.provenance import archive
        archive(target, content=partial.read_bytes(), operation="csv_import")
        partial.replace(target)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    return {"path": str(target), "filename": Path(filename).name, "size_bytes": total}


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


def _research_report(req):
    from quant.data.research_preflight import inspect_csv
    path = Path(req.csv)
    if not path.is_absolute():
        path = WORKDIR / path
    tickers = req.tickers or get_asset_profile(req.asset_class)["defaults"]["tickers"]
    return inspect_csv(path, tickers, req.asset_class)


@router.post("/data/preflight")
def research_preflight(req: DataPreflightRequest):
    return _research_report(req)


def _admit_equity_research(req):
    if req.asset_class != "equity":
        return
    # A fetch-enabled worker must check the replacement data before any trials.
    fetch = getattr(req, "ibkr", None)
    if fetch and (fetch.fetch_missing or fetch.replace_bars):
        return
    report = _research_report(req)
    if not report["execution_eligible"]:
        raise HTTPException(422, {"code": "RESEARCH_DATA_UNUSABLE",
                                  "message": "Research data preflight blocked launch. " + " ".join(report["errors"]),
                                  "report": report})


@router.post("/data/repair", status_code=202)
def repair_research_data(req: DataRepairRequest):
    job_id = manager.new_job_id("data_repair")
    args = _args_from([
        ("--csv", req.csv), ("--asset-class", req.asset_class),
        ("--tickers", req.tickers or get_asset_profile(req.asset_class)["defaults"]["tickers"]),
        ("--host", req.ibkr.ibkr_host), ("--port", req.ibkr.ibkr_port),
        ("--client-id", req.ibkr.ibkr_client_id), ("--years", req.ibkr.ibkr_years),
        ("--bar-hours", req.ibkr.ibkr_bar_hours or 4),
        ("--progress-path", JOBS_DIR / f"{job_id}_progress.json"),
    ]) + ["--repair"]
    if req.ibkr.include_extended_hours:
        args.append("--include-extended-hours")
    return manager.submit("data_repair", "quant.data.research_preflight", args,
                          config=req.model_dump(), job_id=job_id)


def _safe_execution_config(req, *, redact_confirmation: bool = False) -> dict:
    config = req.model_dump()
    if config.get("account_id"):
        config["account_id"] = "<redacted-account>"
    if redact_confirmation and "confirm" in config:
        config["confirm"] = "<redacted>"
    return config


def _validate_short_controls(req) -> None:
    if not req.allow_shorts:
        return
    if req.asset_class != "equity":
        raise HTTPException(400, "Short selling is supported only for US equities and ETFs.")
    if req.short_controls.client_id in {req.client_id, 30}:
        raise HTTPException(
            400,
            "Short-control client ID must differ from the trading and news client IDs.",
        )
    parsed = urlparse(req.short_controls.borrow_api_url)
    if parsed.scheme not in {"ftp", "http", "https"} or not parsed.hostname:
        raise HTTPException(400, "Borrow feed URL must be an FTP or HTTP(S) URL.")
    if parsed.scheme == "ftp" and parsed.hostname not in {
        "ftp2.interactivebrokers.com",
        "ftp3.interactivebrokers.com",
    }:
        raise HTTPException(400, "FTP borrow feeds must use an Interactive Brokers host.")
    if parsed.scheme in {"http", "https"} and not req.short_controls.borrow_api_verify_tls and parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise HTTPException(
            400,
            "TLS verification may be disabled only for a loopback borrow API.",
        )


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


def _execution_params_file(job_id, req):
    from quant.run.sector_risk import validate_sector_evidence
    try:
        payload = dict(req.params or {})
        if not payload and req.params_path:
            payload = json.loads(Path(req.params_path).read_text())
        if not isinstance(payload, dict):
            raise ValueError("Execution params must be a JSON object")
        nested_key = next((key for key in ("params", "best_params") if payload.get(key) is not None), None)
        if nested_key and not isinstance(payload[nested_key], dict):
            raise ValueError("Nested execution params must be a JSON object")
        nested = payload[nested_key] if nested_key else payload
        evidence = req.sector_evidence if req.sector_evidence is not None else nested.get("sector_evidence", payload.get("sector_evidence"))
        if evidence is not None:
            evidence = validate_sector_evidence(evidence, symbols=req.tickers)
            payload["sector_evidence"] = evidence
            if nested_key:
                payload[nested_key] = {**nested, "sector_evidence": evidence}
        return _write_params_file(job_id, None, payload, {})
    except (ValueError, TypeError, OSError) as exc:
        raise HTTPException(400, f"Invalid execution sector evidence or params: {exc}") from exc


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
    _admit_equity_research(req)
    job_id = manager.new_job_id("backtest")
    progress_path = JOBS_DIR / f"{job_id}_progress.json"
    overrides = {**req.features.as_overrides(), **req.risk.as_overrides()}
    if req.equity_simulation is not None:
        if req.asset_class != "equity":
            raise HTTPException(422, "Equity simulation requires the equity profile")
        overrides["equity_simulation"] = req.equity_simulation.model_dump(mode="json")
    params_path = _write_params_file(job_id, req.params_path, req.params, overrides)
    args = _args_from(
        [
            ("--csv", req.csv),
            ("--asset-class", req.asset_class),
            ("--tickers", req.tickers),
            ("--cash", req.cash),
            ("--params", params_path),
            ("--run-name", req.name),
            ("--progress-path", progress_path),
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
    _admit_equity_research(req)
    job_id = manager.new_job_id("optimize")
    progress_path = JOBS_DIR / f"{job_id}_progress.json"
    overrides = {**req.features.as_overrides(), **req.risk.as_overrides()}
    if req.equity_simulation is not None:
        if req.asset_class != "equity":
            raise HTTPException(422, "Equity simulation requires the equity profile")
        overrides["equity_simulation"] = req.equity_simulation.model_dump(mode="json")
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
            ("--workers", req.workers),
            ("--memory-budget-gb", req.memory_budget_gb),
            ("--worker-memory-gb", req.worker_memory_gb),
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
            ("--run-name", req.name),
            ("--progress-path", progress_path),
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


def _campaign_paths(campaign_id: str) -> tuple[str, str, str, str]:
    base = CAMPAIGNS_DIR / f"{campaign_id}.json"
    comparison = CAMPAIGNS_DIR / f"{campaign_id}_comparison.json"
    robustness = CAMPAIGNS_DIR / f"{campaign_id}_robustness.json"
    promoted = CAMPAIGNS_DIR / f"{campaign_id}_promoted_params.json"
    return tuple(str(path) for path in (base, comparison, robustness, promoted))


@router.post("/campaign/seeds", status_code=202)
def start_campaign_seeds(req: CampaignSeedJobRequest):
    _admit_equity_research(req)
    if len(set(req.seeds)) != len(req.seeds):
        raise HTTPException(400, "Campaign seeds must be distinct.")
    manifest, _comparison, _robustness, _promoted = _campaign_paths(req.campaign_id)
    args = _args_from([
        ("--campaign-id", req.campaign_id),
        ("--manifest", manifest),
        ("--seeds", req.seeds),
        ("--trials", req.trials),
        ("--workers", req.workers),
        ("--memory-budget-gb", req.memory_budget_gb),
        ("--worker-memory-gb", req.worker_memory_gb),
        ("--csv", req.csv),
        ("--asset-class", req.asset_class),
        ("--tickers", req.tickers),
        ("--cash", req.cash),
        ("--final-test-frac", req.final_test_frac),
        ("--walk-forward-folds", req.walk_forward_folds),
        ("--embargo-bars", req.embargo_bars),
    ])
    return manager.submit(
        "campaign_seeds", "quant.optimize.multi_seed", args, config=req.model_dump()
    )


@router.post("/campaign/compare", status_code=202)
def start_campaign_compare(req: CampaignStageJobRequest):
    manifest, comparison, _robustness, _promoted = _campaign_paths(req.campaign_id)
    if not Path(manifest).is_file():
        raise HTTPException(404, f"Campaign {req.campaign_id!r} was not found.")
    args = _args_from([
        ("--campaign", manifest),
        ("--top-n", req.top_n),
        ("--finalists", req.finalists),
        ("--max-cluster-distance", req.max_cluster_distance),
        ("--out", comparison),
    ])
    return manager.submit(
        "campaign_compare", "quant.optimize.compare", args, config=req.model_dump()
    )


@router.post("/campaign/robustness", status_code=202)
def start_campaign_robustness(req: CampaignStageJobRequest):
    manifest, comparison, robustness, _promoted = _campaign_paths(req.campaign_id)
    if not Path(comparison).is_file():
        raise HTTPException(409, "Run campaign comparison before robustness testing.")
    args = _args_from([
        ("--campaign", manifest),
        ("--comparison", comparison),
        ("--finalists", req.finalists),
        ("--top-n", req.top_n),
        ("--workers", req.workers),
        ("--memory-budget-gb", req.memory_budget_gb),
        ("--worker-memory-gb", req.worker_memory_gb),
        ("--out", robustness),
    ])
    return manager.submit(
        "campaign_robustness", "quant.optimize.robustness", args, config=req.model_dump()
    )


@router.post("/campaign/promote", status_code=202)
def start_campaign_promote(req: CampaignStageJobRequest):
    if req.confirm != PROMOTION_CONFIRM_PHRASE:
        raise HTTPException(400, f"Expected exact confirmation phrase: {PROMOTION_CONFIRM_PHRASE!r}")
    manifest, _comparison, robustness, promoted = _campaign_paths(req.campaign_id)
    if not Path(robustness).is_file():
        raise HTTPException(409, "Run the robustness suite before consuming the outer holdout.")
    args = _args_from([
        ("--campaign", manifest),
        ("--robustness", robustness),
        ("--top-n", req.top_n),
        ("--max-cluster-distance", req.max_cluster_distance),
        ("--out-params", promoted),
    ])
    safe_config = {**req.model_dump(), "confirm": "<redacted>"}
    return manager.submit(
        "campaign_promote", "quant.optimize.promote", args, config=safe_config
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
    _validate_short_controls(req)
    job_id = manager.new_job_id("paper")
    params_path = _execution_params_file(job_id, req)
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
            ("--session-policy-json", json.dumps(req.session_policy) if req.session_policy is not None else None),
            ("--redis-host", req.redis_host),
            ("--redis-port", req.redis_port),
            ("--short-control-client-id", req.short_controls.client_id),
            ("--short-borrow-api-url", req.short_controls.borrow_api_url),
            ("--short-max-borrow-fee-pct", req.short_controls.max_borrow_fee_pct),
            ("--short-min-margin-cushion-pct", req.short_controls.min_margin_cushion_pct),
            ("--short-locate-buffer-ratio", req.short_controls.locate_buffer_ratio),
            ("--short-recall-grace-secs", req.short_controls.recall_grace_secs),
        ]
    )
    if req.allow_shorts:
        args.append("--allow-shorts")
    if req.short_controls.borrow_api_verify_tls:
        args.append("--short-borrow-api-verify-tls")
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
    _validate_short_controls(req)
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
    params_path = _execution_params_file(job_id, req)
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
            ("--session-policy-json", json.dumps(req.session_policy) if req.session_policy is not None else None),
            ("--redis-host", req.redis_host),
            ("--redis-port", req.redis_port),
            ("--short-control-client-id", req.short_controls.client_id),
            ("--short-borrow-api-url", req.short_controls.borrow_api_url),
            ("--short-max-borrow-fee-pct", req.short_controls.max_borrow_fee_pct),
            ("--short-min-margin-cushion-pct", req.short_controls.min_margin_cushion_pct),
            ("--short-locate-buffer-ratio", req.short_controls.locate_buffer_ratio),
            ("--short-recall-grace-secs", req.short_controls.recall_grace_secs),
        ]
    ) + ["--live"]
    if req.allow_shorts:
        args.append("--allow-shorts")
    if req.short_controls.borrow_api_verify_tls:
        args.append("--short-borrow-api-verify-tls")
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


@router.get("/{job_id}/progress")
def get_job_progress(job_id: str):
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(404, f"job {job_id!r} not found")
    progress_path = JOBS_DIR / f"{job_id}_progress.json"
    if progress_path.is_file():
        try:
            progress = json.loads(progress_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            progress = {}
    else:
        progress = {}
    return {
        "job_id": job_id,
        "kind": job["kind"],
        "status": job["status"],
        "started_at": job["started_at"],
        "finished_at": job.get("finished_at"),
        **progress,
    }


@router.post("/{job_id}/cancel")
def cancel_job(job_id: str):
    job = manager.get(job_id)
    if job and job.get("kind") == "recovery":
        raise HTTPException(403, "Use authenticated recovery controls to cancel this job")
    result = manager.cancel(job_id)
    if result is None:
        raise HTTPException(404, f"job {job_id!r} not found")
    return result
