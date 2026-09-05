"""Pre-holdout robustness re-evaluation for locked seed-study finalists."""
from __future__ import annotations

import argparse
import json
import math
import tempfile
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from quant.optimize.campaign import (
    atomic_write_json,
    invariant_validation_contract,
    load_manifest,
    load_studies,
    study_snapshot,
)
from quant.optimize.compare import build_comparison_report
from quant.optimize.optimize import (
    FoldPerformance,
    WalkForwardFold,
    _file_sha256,
    _prepare_nested_walk_forward,
    stability_aware_score,
    evaluate_folds,
)
from quant.run.compute import ComputePool, add_compute_arguments, resolve_compute_plan


def evaluate_fixed_parameters(
    *,
    folds: tuple[WalkForwardFold, ...],
    tickers: list[str],
    params: dict[str, Any],
    contract: dict[str, Any],
    embargo_bars: int,
    scenario: str,
    training_window_override: int | None = None,
    pool: ComputePool | None = None,
) -> dict[str, Any]:
    """Evaluate one locked parameter set; no trial suggestions or holdout data."""
    structural = dict(contract.get("structural_overrides", {}))
    base_overrides = {
        **params,
        "refit_every_n_bars": int(contract["refit_every_n_bars"]),
        "warmup_bars": int(contract["warmup_bars"]),
        "min_train_bars": int(contract["min_train_bars"]),
        **structural,
    }
    if training_window_override is not None:
        base_overrides["training_window_bars"] = int(training_window_override)
    effective_embargo = max(int(params["horizon"]), int(embargo_bars))
    normal_results: list[FoldPerformance] = []
    stressed_results: list[FoldPerformance] = []
    rows = []
    evaluation_seed = int(contract.get("evaluation_seed", 1729))
    normal_slippage = float(contract["normal_slippage_probability"])
    stress_multiplier = float(contract["stress_cost_multiplier"])
    cash = float(contract["starting_cash"])
    asset_class = str(contract["asset_class"])
    with closing(evaluate_folds(
        folds, tickers, base_overrides, cash, asset_class, effective_embargo,
        evaluation_seed, normal_slippage, stress_multiplier, pool,
    )) as results:
        for fold, normal, stressed in results:
            normal_results.append(normal)
            stressed_results.append(stressed)
            rows.append(
                {
                    "fold": fold.number,
                    "validation_start": pd.Timestamp(
                        fold.validation_start_ns, unit="ns", tz="UTC"
                    ).isoformat(),
                    "validation_end": pd.Timestamp(
                        fold.validation_end_ns, unit="ns", tz="UTC"
                    ).isoformat(),
                    "normal_ratio": normal.ratio,
                    "stressed_ratio": stressed.ratio,
                    "turnover": normal.turnover,
                    "trades": normal.trades,
                }
            )
    normal = [item.ratio for item in normal_results]
    stressed = [item.ratio for item in stressed_results]
    turnovers = [item.turnover for item in normal_results]
    return {
        "scenario": scenario,
        "tickers": tickers,
        "embargo_bars": effective_embargo,
        "training_window_bars": int(base_overrides.get("training_window_bars", 0)),
        "training_mode": (
            "expanding"
            if int(base_overrides.get("training_window_bars", 0)) == 0
            else "rolling"
        ),
        "fold_count": len(folds),
        "normal_median": float(np.median(normal)),
        "stressed_median": float(np.median(stressed)),
        "fold_dispersion": float(np.std(normal)),
        "normal_to_stress_degradation": float(
            np.median([max(0.0, left - right) for left, right in zip(normal, stressed)])
        ),
        "positive_normal_folds": int(sum(value > 0.0 for value in normal)),
        "positive_stressed_folds": int(sum(value > 0.0 for value in stressed)),
        "turnover": float(np.median(turnovers)),
        "trades": int(sum(item.trades for item in normal_results)),
        "stability_score": stability_aware_score(
            normal,
            stressed,
            turnovers,
            std_weight=float(contract["stability_std_weight"]),
            turnover_weight=float(contract["turnover_penalty_weight"]),
            cost_sensitivity_weight=float(contract["cost_sensitivity_weight"]),
            min_positive_fraction=float(contract["min_positive_fold_fraction"]),
        ),
        "passed": all(value > 0.0 for value in normal)
        and all(value > 0.0 for value in stressed),
        "folds": rows,
    }


def _rolling_context_folds(
    folds: tuple[WalkForwardFold, ...],
    output_dir: Path,
    *,
    context_bars: int,
) -> tuple[WalkForwardFold, ...]:
    output_dir.mkdir(parents=True, exist_ok=True)
    result = []
    for fold in folds:
        frame = pd.read_csv(fold.csv_path)
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], format="mixed", utc=True)
        context_start_index = max(0, fold.validation_start_index - context_bars)
        context_start_ns = fold.timestamps_ns[context_start_index]
        frame = frame[frame["timestamp"].astype("int64") >= context_start_ns]
        destination = output_dir / f"fold_{fold.number}.csv"
        frame.to_csv(destination, index=False)
        result.append(
            WalkForwardFold(
                number=fold.number,
                csv_path=str(destination),
                validation_start_ns=fold.validation_start_ns,
                validation_end_ns=fold.validation_end_ns,
                validation_start_index=fold.validation_start_index,
                timestamps_ns=fold.timestamps_ns,
            )
        )
    return tuple(result)


def _longest_true_interval(mask: pd.Series, eligible_start: int) -> tuple[int, int] | None:
    best = None
    start = None
    for index, active in enumerate(mask.fillna(False).tolist() + [False]):
        active = bool(active) and index >= eligible_start
        if active and start is None:
            start = index
        elif not active and start is not None:
            candidate = (start, index - 1)
            if best is None or candidate[1] - candidate[0] > best[1] - best[0]:
                best = candidate
            start = None
    return best


def build_regime_folds(
    csv_path: str,
    tickers: list[str],
    output_dir: Path,
    *,
    final_test_frac: float,
    minimum_history: int,
    embargo_bars: int,
) -> tuple[dict[str, tuple[WalkForwardFold, ...]], list[str]]:
    """Select longest development-only bull/bear/sideways/high-vol episodes."""
    frame = pd.read_csv(csv_path)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], format="mixed", utc=True)
    frame = frame[frame["ticker"].isin(tickers)].sort_values(["timestamp", "ticker"])
    timestamps = pd.Index(frame["timestamp"].drop_duplicates().sort_values())
    final_count = max(1, int(math.ceil(len(timestamps) * final_test_frac)))
    development_count = len(timestamps) - final_count
    development_timestamps = timestamps[:development_count]
    prices = (
        frame[frame["timestamp"].isin(development_timestamps)]
        .pivot_table(index="timestamp", columns="ticker", values="close", aggfunc="last")
        .sort_index()
        .ffill()
    )
    market_return = np.log(prices).diff().median(axis=1).fillna(0.0)
    lookback = min(20, max(5, development_count // 10))
    trailing = market_return.rolling(lookback, min_periods=lookback).sum()
    volatility = market_return.rolling(lookback, min_periods=lookback).std()
    lower, upper = trailing.dropna().quantile([1 / 3, 2 / 3]).tolist()
    high_vol_threshold = float(volatility.dropna().quantile(0.75))
    masks = {
        "bull": trailing > upper,
        "bear": trailing < lower,
        "sideways": trailing.between(lower, upper, inclusive="both"),
        "high_volatility": volatility >= high_vol_threshold,
    }
    timestamps_ns = tuple(int(value.value) for value in timestamps)
    eligible_start = minimum_history + embargo_bars
    output_dir.mkdir(parents=True, exist_ok=True)
    folds: dict[str, tuple[WalkForwardFold, ...]] = {}
    unavailable = []
    for number, (label, mask) in enumerate(masks.items(), 1001):
        interval = _longest_true_interval(mask.reset_index(drop=True), eligible_start)
        if interval is None or interval[1] - interval[0] + 1 < 2:
            unavailable.append(label)
            continue
        start, end = interval
        destination = output_dir / f"{label}.csv"
        frame[frame["timestamp"] <= development_timestamps[end]].to_csv(
            destination, index=False
        )
        folds[label] = (
            WalkForwardFold(
                number=number,
                csv_path=str(destination),
                validation_start_ns=int(development_timestamps[start].value),
                validation_end_ns=int(development_timestamps[end].value),
                validation_start_index=start,
                timestamps_ns=timestamps_ns,
            ),
        )
    return folds, unavailable


def run_robustness_suite(
    *,
    finalists: list[dict[str, Any]],
    contract: dict[str, Any],
    workspace: Path,
    pool: ComputePool | None = None,
) -> list[dict[str, Any]]:
    source_csv = str(contract["source_csv"])
    if _file_sha256(source_csv) != contract["source_csv_sha256"]:
        raise ValueError("source CSV changed after the seed studies were created")
    tickers = list(contract["tickers"])
    base_fold_count = int(contract["walk_forward_folds"])
    warmup = max(int(contract["warmup_bars"]), int(contract["min_train_bars"]))
    base_embargo = int(contract["embargo_bars"])
    embargoes = sorted({base_embargo, max(5, base_embargo * 2)})
    fold_counts = sorted(
        {
            max(2, base_fold_count - 1),
            min(10, base_fold_count + 1),
        }
    )
    expanding_schemes: dict[str, tuple[WalkForwardFold, ...]] = {}
    unavailable_schemes = []
    for fold_count in fold_counts:
        try:
            prepared = _prepare_nested_walk_forward(
                source_csv,
                tickers,
                str(workspace / f"boundaries_{fold_count}"),
                final_test_frac=float(contract["final_test_frac"]),
                n_folds=fold_count,
                min_initial_train_bars=warmup,
                max_embargo_bars=max(5, max(embargoes)),
            )
            expanding_schemes[f"expanding_boundaries_{fold_count}"] = prepared.folds
        except ValueError as error:
            unavailable_schemes.append(
                {"scenario": f"expanding_boundaries_{fold_count}", "error": str(error)}
            )
    base_prepared = _prepare_nested_walk_forward(
        source_csv,
        tickers,
        str(workspace / "base"),
        final_test_frac=float(contract["final_test_frac"]),
        n_folds=base_fold_count,
        min_initial_train_bars=warmup,
        max_embargo_bars=max(5, max(embargoes)),
    )
    regime_folds, unavailable_regimes = build_regime_folds(
        source_csv,
        tickers,
        workspace / "regimes",
        final_test_frac=float(contract["final_test_frac"]),
        minimum_history=warmup,
        embargo_bars=max(embargoes),
    )

    reports = []
    for finalist_index, finalist in enumerate(finalists):
        params = dict(finalist["params"])
        rolling_context = max(
            warmup * 2,
            int(params.get("training_window_bars", 0) or 0),
        )
        rolling_folds = _rolling_context_folds(
            base_prepared.folds,
            workspace / f"rolling_{finalist_index}",
            context_bars=rolling_context,
        )
        results = []
        schemes = {
            name: (folds, 0) for name, folds in expanding_schemes.items()
        }
        schemes[f"rolling_context_{rolling_context}"] = (
            rolling_folds,
            rolling_context,
        )
        for scheme_name, (folds, training_window_override) in schemes.items():
            for embargo in embargoes:
                results.append(
                    evaluate_fixed_parameters(
                        folds=folds,
                        tickers=tickers,
                        params=params,
                        contract=contract,
                        embargo_bars=embargo,
                        scenario=f"{scheme_name}_embargo_{embargo}",
                        training_window_override=training_window_override,
                        pool=pool,
                    )
                )
        for ticker in tickers:
            results.append(
                evaluate_fixed_parameters(
                    folds=base_prepared.folds,
                    tickers=[ticker],
                    params=params,
                    contract=contract,
                    embargo_bars=max(embargoes),
                    scenario=f"individual_ticker_{ticker}",
                    pool=pool,
                )
            )
        for label, folds in regime_folds.items():
            results.append(
                evaluate_fixed_parameters(
                    folds=folds,
                    tickers=tickers,
                    params=params,
                    contract=contract,
                    embargo_bars=max(embargoes),
                    scenario=f"market_regime_{label}",
                    pool=pool,
                )
            )
        normal_values = [item["normal_median"] for item in results]
        stressed_values = [item["stressed_median"] for item in results]
        degradation = [item["normal_to_stress_degradation"] for item in results]
        all_required_available = not unavailable_schemes and not unavailable_regimes
        passed = all_required_available and bool(results) and all(
            item["passed"] for item in results
        )
        reports.append(
            {
                **finalist,
                "robustness_score": float(
                    np.median(normal_values)
                    - 0.5 * np.std(normal_values)
                    - 0.5 * np.median(degradation)
                ),
                "normal_median": float(np.median(normal_values)),
                "stressed_median": float(np.median(stressed_values)),
                "passed": passed,
                "unavailable_schemes": unavailable_schemes,
                "unavailable_regimes": unavailable_regimes,
                "scenarios": results,
            }
        )
    return sorted(reports, key=lambda item: item["robustness_score"], reverse=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Re-evaluate locked finalists on development-only alternative schemes."
    )
    parser.add_argument("--campaign", required=True)
    parser.add_argument("--comparison", help="Existing compare.py JSON report.")
    parser.add_argument("--finalists", type=int, default=5, choices=range(5, 11))
    parser.add_argument("--top-n", type=int, default=10, choices=range(5, 11))
    parser.add_argument("--out", help="Defaults beside the campaign manifest.")
    add_compute_arguments(parser)
    args = parser.parse_args()
    try:
        compute_plan = resolve_compute_plan(
            args.workers, args.memory_budget_gb, args.worker_memory_gb, tasks=20,
        )
    except ValueError as error:
        parser.error(str(error))

    manifest = load_manifest(args.campaign)
    if manifest.get("outer_holdout", {}).get("status") != "UNTOUCHED":
        parser.error("outer holdout is not untouched")
    studies = load_studies(manifest)
    expected_studies = {
        item["study_name"]
        for item in manifest.get("studies", [])
        if item.get("status") == "COMPLETE"
    }
    if expected_studies != {study.study_name for study in studies}:
        parser.error("every campaign seed study must complete before robustness testing")
    if len(studies) < 3:
        parser.error("at least three completed seed studies are required")
    expected_trials = int(manifest["trials_per_seed"])
    if any(len(study.trials) != expected_trials for study in studies):
        parser.error("each seed study must have exactly the campaign trial count")
    if any(study.user_attrs.get("final_test_evaluated") for study in studies):
        parser.error("at least one seed study already evaluated the outer holdout")
    contract = invariant_validation_contract(studies)
    if args.comparison:
        with open(args.comparison) as handle:
            comparison = json.load(handle)
        if comparison.get("study_snapshot") != study_snapshot(studies):
            parser.error("comparison report is stale for the current seed studies")
        if comparison.get("validation_contract") != contract:
            parser.error("comparison report uses a different validation contract")
        finalists = comparison["finalists"][: args.finalists]
    else:
        comparison = build_comparison_report(
            studies, top_n=args.top_n, finalist_count=args.finalists
        )
        finalists = comparison["finalists"]
    print(f"Research compute: {json.dumps(compute_plan.as_dict(), sort_keys=True)}")
    with (
        tempfile.TemporaryDirectory(prefix="quant_robustness_") as temporary,
        ComputePool(compute_plan) as pool,
    ):
        evaluated = run_robustness_suite(
            finalists=finalists,
            contract=contract,
            workspace=Path(temporary),
            pool=pool,
        )
    report = {
        "compute_plan": compute_plan.as_dict(),
        "campaign_id": manifest["campaign_id"],
        "created_at": time.time(),
        "outer_holdout_status": "UNTOUCHED",
        "finalists": evaluated,
        "comparison": comparison,
        "study_snapshot": study_snapshot(studies),
    }
    output = Path(
        args.out
        or Path(args.campaign).with_name(
            f"{manifest['campaign_id']}_robustness.json"
        )
    )
    atomic_write_json(output, report)
    manifest["robustness_report"] = str(output)
    manifest["updated_at"] = time.time()
    atomic_write_json(args.campaign, manifest)
    print("rank  study  trial  robust  normal  stressed  passed")
    for rank, item in enumerate(evaluated, 1):
        print(
            f"{rank:<4}  {item['study_name']}  {item['trial_number']:<5}  "
            f"{item['robustness_score']:.4f}  {item['normal_median']:.4f}  "
            f"{item['stressed_median']:.4f}  {item['passed']}"
        )
    print(f"Robustness report -> {output}")


if __name__ == "__main__":
    main()
