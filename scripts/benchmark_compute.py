"""Measure real development-fold throughput and RSS without an Optuna study or holdout run.

Run from the directory containing quant:
    quant/.quant312/bin/python -m quant.scripts.benchmark_compute --workers 1 0
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import resource
import statistics
import sys
import tempfile
import time
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path

from quant.data.generate_sample_bars import generate
from quant.optimize.optimize import _prepare_nested_walk_forward, evaluate_backtest, fold_tasks
from quant.run.compute import ComputePool, host_resources, resolve_compute_plan


def measure_backtest(task):
    started = time.perf_counter()
    cpu = time.process_time()
    performance = evaluate_backtest(task)
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes; Linux reports KiB. This is a process high-water
    # mark (including imports and earlier tasks), not incremental task memory.
    peak_bytes = peak if platform.system() == "Darwin" else peak * 1024
    return {"performance": asdict(performance), "seconds": time.perf_counter() - started,
            "cpu_seconds": time.process_time() - cpu, "pid": os.getpid(),
            "process_peak_rss_gb": peak_bytes / 1024 ** 3}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, nargs="+", default=[1, 0])
    parser.add_argument("--memory-budget-gb", type=float, default=0)
    parser.add_argument("--worker-memory-gb", type=float, default=4)
    parser.add_argument("--csv", help="Optional representative input; only development folds run.")
    parser.add_argument("--asset-class", choices=["crypto", "equity"], default="crypto")
    parser.add_argument("--tickers", nargs="+", default=["BTC", "ETH", "SOL"])
    parser.add_argument("--bars", type=int, default=600)
    parser.add_argument("--folds", type=int, default=5)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be >= 1")
    host = host_resources()
    report = {"host": host, "asset_class": args.asset_class, "tickers": args.tickers,
              "source": args.csv or "synthetic_seed_42", "bars": args.bars if not args.csv else None,
              "folds": args.folds, "outer_holdout_evaluated": False, "runs": []}
    report["runtime"] = {
        "python": sys.version, "platform": platform.platform(),
        "packages": {name: version(name) for name in
                     ("numpy", "scipy", "scikit-learn", "hmmlearn", "optuna", "nautilus_trader")},
    }
    reference = None
    with tempfile.TemporaryDirectory(prefix="quant_compute_benchmark_") as temporary:
        csv = args.csv or str(Path(temporary) / "bars.csv")
        if not args.csv:
            generate(args.tickers, n_days=args.bars, seed=42,
                     asset_class=args.asset_class).to_csv(csv, index=False)
        data = _prepare_nested_walk_forward(
            csv, args.tickers, str(Path(temporary) / "folds"), final_test_frac=0.2,
            n_folds=args.folds, min_initial_train_bars=150, max_embargo_bars=5,
        )
        tasks = fold_tasks(
            data.folds, args.tickers,
            {"n_lags": 5, "horizon": 2, "cross_asset_lags": 2, "spread_lags": 2,
             "entry_threshold": 0.001, "use_limit_orders": False},
            5000.0, args.asset_class, 5, 1729, 0.05, 2.0,
        )
        for workers in args.workers:
            plan = resolve_compute_plan(workers, args.memory_budget_gb,
                                        args.worker_memory_gb, tasks=len(tasks), host=host)
            measurements = []
            with ComputePool(plan) as pool:
                for repeat in range(args.repeats):
                    started = time.perf_counter()
                    with pool.results(measure_backtest, tasks) as results:
                        measured = list(results)
                    elapsed = time.perf_counter() - started
                    performances = [item["performance"] for item in measured]
                    if reference is None:
                        reference = performances
                    if performances != reference:
                        raise RuntimeError("Serial/parallel fold metrics differ; do not use this configuration")
                    row = {"repeat": repeat, "seconds": elapsed, "tasks": measured,
                           "backtests_per_second": len(tasks) / elapsed}
                    measurements.append(row)
                    print(f"workers={plan.workers} repeat={repeat} seconds={elapsed:.3f} "
                          f"peak_worker_rss_gb={max(item['process_peak_rss_gb'] for item in measured):.3f}",
                          flush=True)
            report["runs"].append({
                "plan": plan.as_dict(), "measurements": measurements,
                "cold_seconds": measurements[0]["seconds"],
                "warm_median_seconds": statistics.median(
                    item["seconds"] for item in measurements[1:] or measurements
                ),
            })
    report["identical_fold_metrics"] = True
    report["fold_metrics"] = reference
    text = json.dumps(report, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n")
        print(f"Report -> {args.out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
