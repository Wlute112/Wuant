"""Strict one-shot outer-holdout gate for a robustness-tested candidate."""
from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import time
from pathlib import Path
from typing import Any

import pandas as pd

from quant.optimize.campaign import (
    atomic_write_json,
    canonical_json,
    invariant_validation_contract,
    load_manifest,
    load_studies,
    locked_manifest,
    study_snapshot,
)
from quant.optimize.optimize import (
    _engine_performance,
    _file_sha256,
    _prepare_nested_walk_forward,
    stability_aware_score,
)
from quant.optimize.stability import PromotionRulesEngine
from quant.run.asset_profiles import get_asset_profile
from quant.run.backtest_common import build_and_run, infer_bar_interval_minutes_from_csv


def _candidate_digest(candidate: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(candidate).encode()).hexdigest()


def _select_candidate(
    report: dict[str, Any],
    study_name: str | None,
    trial_number: int | None,
) -> dict[str, Any]:
    finalists = report.get("finalists", [])
    if study_name is None and trial_number is None:
        candidate = next((item for item in finalists if item.get("passed")), None)
        if candidate is None:
            raise ValueError("robustness report has no passing finalist")
        return candidate
    if study_name is None or trial_number is None:
        raise ValueError("--study and --trial must be supplied together")
    match = next(
        (
            item
            for item in finalists
            if item.get("study_name") == study_name
            and int(item.get("trial_number", -1)) == trial_number
        ),
        None,
    )
    if match is None:
        raise ValueError("requested candidate is absent from the robustness report")
    return match


def _consume_holdout(
    *,
    contract: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    source_csv = str(contract["source_csv"])
    if _file_sha256(source_csv) != contract["source_csv_sha256"]:
        raise ValueError("source CSV changed after optimization")
    tickers = list(contract["tickers"])
    cash = float(contract["starting_cash"])
    asset_class = str(contract["asset_class"])
    params = dict(candidate["params"])
    embargo = max(int(contract["embargo_bars"]), int(params["horizon"]))
    structural = dict(contract.get("structural_overrides", {}))
    with tempfile.TemporaryDirectory(prefix="quant_outer_holdout_") as temporary:
        nested = _prepare_nested_walk_forward(
            source_csv,
            tickers,
            temporary,
            final_test_frac=float(contract["final_test_frac"]),
            n_folds=int(contract["walk_forward_folds"]),
            min_initial_train_bars=max(
                int(contract["warmup_bars"]), int(contract["min_train_bars"])
            ),
            max_embargo_bars=max(5, embargo),
        )
        if (
            nested.final_test_start_ns != int(contract["outer_holdout_start_ns"])
            or nested.final_test_end_ns != int(contract["outer_holdout_end_ns"])
        ):
            raise ValueError("reconstructed outer-holdout dates differ from the locked contract")
        overrides = {
            **params,
            "refit_every_n_bars": int(contract["refit_every_n_bars"]),
            "warmup_bars": int(contract["warmup_bars"]),
            "min_train_bars": int(contract["min_train_bars"]),
            **structural,
            "backtest_model_fit_end_ns": nested.final_model_fit_end_ns(embargo),
            "backtest_trade_start_ns": nested.final_test_start_ns,
        }
        normal_slippage = float(contract["normal_slippage_probability"])
        fill_seed = int(contract.get("evaluation_seed", 1729))
        normal_engine = build_and_run(
            csv_path=nested.final_context_path,
            tickers=tickers,
            strategy_overrides=overrides,
            starting_cash=cash,
            log_level="ERROR",
            bypass_logging=True,
            asset_class=asset_class,
            cost_multiplier=1.0,
            slippage_probability=normal_slippage,
            fill_model_seed=fill_seed,
        )
        try:
            normal = _engine_performance(normal_engine, cash, asset_class)
        finally:
            normal_engine.dispose()
        stressed_engine = build_and_run(
            csv_path=nested.final_context_path,
            tickers=tickers,
            strategy_overrides=overrides,
            starting_cash=cash,
            log_level="ERROR",
            bypass_logging=True,
            asset_class=asset_class,
            cost_multiplier=float(contract["stress_cost_multiplier"]),
            slippage_probability=min(1.0, normal_slippage * 2.0),
            fill_model_seed=fill_seed,
        )
        try:
            stressed = _engine_performance(stressed_engine, cash, asset_class)
        finally:
            stressed_engine.dispose()
        adjusted = stability_aware_score(
            [normal.ratio],
            [stressed.ratio],
            [normal.turnover],
            std_weight=float(contract["stability_std_weight"]),
            turnover_weight=float(contract["turnover_penalty_weight"]),
            cost_sensitivity_weight=float(contract["cost_sensitivity_weight"]),
            min_positive_fraction=float(contract["min_positive_fold_fraction"]),
            require_positive_folds=False,
        )
        return {
            "start": pd.Timestamp(
                nested.final_test_start_ns, unit="ns", tz="UTC"
            ).isoformat(),
            "end": pd.Timestamp(
                nested.final_test_end_ns, unit="ns", tz="UTC"
            ).isoformat(),
            "normal_ratio": normal.ratio,
            "stressed_ratio": stressed.ratio,
            "turnover": normal.turnover,
            "trades": normal.trades,
            "stability_adjusted_score": adjusted,
            "passed": normal.ratio > 0.0 and stressed.ratio > 0.0,
        }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Apply strict promotion rules, then consume the outer holdout once."
    )
    parser.add_argument("--campaign", required=True)
    parser.add_argument("--robustness", help="Defaults to the path recorded in the campaign.")
    parser.add_argument("--study")
    parser.add_argument("--trial", type=int)
    parser.add_argument("--top-n", type=int, default=10, choices=range(5, 11))
    parser.add_argument("--max-cluster-distance", type=float, default=0.20)
    parser.add_argument("--out-params", required=True)
    args = parser.parse_args()

    manifest = load_manifest(args.campaign)
    robustness_path = args.robustness or manifest.get("robustness_report")
    if not robustness_path:
        parser.error("a robustness report is required before promotion")
    with open(robustness_path) as handle:
        robustness_report = json.load(handle)
    if robustness_report.get("outer_holdout_status") != "UNTOUCHED":
        parser.error("robustness report was not produced before outer-holdout evaluation")
    studies = load_studies(manifest)
    if robustness_report.get("campaign_id") != manifest.get("campaign_id"):
        parser.error("robustness report belongs to a different campaign")
    if robustness_report.get("study_snapshot") != study_snapshot(studies):
        parser.error("seed studies changed after robustness evaluation")
    contract = invariant_validation_contract(studies)
    try:
        candidate = _select_candidate(robustness_report, args.study, args.trial)
    except ValueError as error:
        parser.error(str(error))
    decision = PromotionRulesEngine().evaluate(
        studies=studies,
        candidate=candidate,
        robustness_report=robustness_report,
        top_n=args.top_n,
        max_cluster_distance=args.max_cluster_distance,
    )
    print(json.dumps(decision.to_dict(), indent=2, sort_keys=True))
    if not decision.passed:
        raise SystemExit("Promotion blocked; outer holdout remains untouched")

    digest = _candidate_digest(candidate)
    now = time.time()
    with locked_manifest(args.campaign) as (_handle, locked):
        holdout = locked.setdefault("outer_holdout", {})
        if holdout.get("status") != "UNTOUCHED" or int(holdout.get("evaluations", 0)) != 0:
            raise SystemExit("Outer holdout has already been consumed")
        holdout.update(
            {
                "status": "CONSUMED_PENDING",
                "evaluations": 1,
                "candidate_sha256": digest,
                "candidate": candidate,
                "started_at": now,
            }
        )
        locked["updated_at"] = now
        # Mark every same-contract study immutable before the first holdout run.
        for study in studies:
            study.set_user_attr("final_test_evaluated", True)
            study.set_user_attr("final_test_evaluated_at", now)
            study.set_user_attr("outer_holdout_status", "CONSUMED_PENDING")
            study.set_user_attr("promoted_candidate_sha256", digest)

    try:
        final_test = _consume_holdout(contract=contract, candidate=candidate)
    except BaseException as error:
        with locked_manifest(args.campaign) as (_handle, locked):
            locked["outer_holdout"].update(
                {
                    "status": "CONSUMED_FAILED",
                    "finished_at": time.time(),
                    "error": f"{type(error).__name__}: {error}",
                }
            )
            locked["updated_at"] = time.time()
        raise

    status = "PROMOTED" if final_test["passed"] else "REJECTED_OUTER_HOLDOUT"
    finished_at = time.time()
    with locked_manifest(args.campaign) as (_handle, locked):
        locked["outer_holdout"].update(
            {
                "status": status,
                "finished_at": finished_at,
                "result": final_test,
            }
        )
        locked["updated_at"] = finished_at
    for study in studies:
        study.set_user_attr("outer_holdout_status", status)

    structural = dict(contract.get("structural_overrides", {}))
    asset_class = str(contract["asset_class"])
    payload = {
        "params": candidate["params"],
        "asset_class": asset_class,
        "objective_metric": get_asset_profile(asset_class)["scoring"]["metric"],
        "source_csv": contract["source_csv"],
        "bar_interval_minutes": infer_bar_interval_minutes_from_csv(
            contract["source_csv"], list(contract["tickers"])
        ),
        "study_name": candidate["study_name"],
        "trial_number": candidate["trial_number"],
        "sampler_seed": candidate.get("seed"),
        "evaluation_seed": contract["evaluation_seed"],
        "validation_contract": contract,
        "promotion_checks": decision.to_dict(),
        "robustness": {
            key: candidate[key]
            for key in ("robustness_score", "normal_median", "stressed_median", "passed")
            if key in candidate
        },
        "final_test": final_test,
        "promotion_status": status,
        "refit_every_n_bars": contract["refit_every_n_bars"],
        "warmup_bars": contract["warmup_bars"],
        "min_train_bars": contract["min_train_bars"],
        **structural,
    }
    atomic_write_json(Path(args.out_params), payload)
    print(f"Outer holdout evaluations: 1; status: {status}")
    print(f"Promoted params -> {args.out_params}")
    if status != "PROMOTED":
        raise SystemExit("Candidate rejected by the outer holdout")


if __name__ == "__main__":
    main()
