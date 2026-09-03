"""Rank seed studies by robust top-trial statistics and extract consensus."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import optuna

from quant.optimize.campaign import (
    atomic_write_json,
    invariant_validation_contract,
    load_manifest,
    load_studies,
    study_snapshot,
)
from quant.optimize.stability import (
    rank_studies,
    select_finalists,
    similar_seed_clusters,
    study_consensus,
)


def build_comparison_report(
    studies: list[optuna.Study],
    *,
    top_n: int = 10,
    finalist_count: int = 5,
    max_cluster_distance: float = 0.20,
) -> dict[str, Any]:
    contract = invariant_validation_contract(studies)
    rankings = rank_studies(studies, top_n=top_n)
    return {
        "validation_contract": contract,
        "rankings": [item.to_dict() for item in rankings],
        "study_consensus": [
            study_consensus(study, top_n=top_n) for study in studies
        ],
        "similar_seed_clusters": similar_seed_clusters(
            studies, top_n=top_n, max_distance=max_cluster_distance
        ),
        "finalists": select_finalists(studies, count=finalist_count),
        "top_n": top_n,
        "finalist_count": finalist_count,
        "max_cluster_distance": max_cluster_distance,
        "study_snapshot": study_snapshot(studies),
    }


def _format_number(value: Any, digits: int = 4) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "n/a"
    return f"{number:.{digits}f}" if math.isfinite(number) else "n/a"


def comparison_table(report: dict[str, Any]) -> str:
    headers = (
        "rank",
        "study",
        "seed",
        "best",
        f"top{report['top_n']} med",
        "fold std",
        "+folds",
        "cost degr",
        "turnover",
        "trades",
        "robust",
    )
    rows = []
    for item in report["rankings"]:
        rows.append(
            (
                str(item["rank"]),
                str(item["study_name"]),
                str(item.get("seed", "n/a")),
                _format_number(item["best_objective"]),
                _format_number(item["top_n_median"]),
                _format_number(item["fold_dispersion"]),
                f"{item['positive_folds']}/{item['fold_count']}",
                _format_number(item["cost_degradation"]),
                _format_number(item["turnover"]),
                str(item["trade_count"]),
                _format_number(item["robust_rank_score"]),
            )
        )
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        for index in range(len(headers))
    ]
    line = "  ".join(value.ljust(widths[index]) for index, value in enumerate(headers))
    divider = "  ".join("-" * width for width in widths)
    body = [
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(row))
        for row in rows
    ]
    return "\n".join([line, divider, *body])


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare independent seed studies using robust top-trial metrics."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--campaign", help="Multi-seed campaign manifest JSON.")
    source.add_argument("--studies", nargs="+", help="Optuna study names.")
    parser.add_argument("--storage", default="sqlite:///quant/optimize/studies.db")
    parser.add_argument("--top-n", type=int, default=10, choices=range(5, 11))
    parser.add_argument("--finalists", type=int, default=5, choices=range(5, 11))
    parser.add_argument("--max-cluster-distance", type=float, default=0.20)
    parser.add_argument("--out", help="Optional JSON report path.")
    args = parser.parse_args()

    if args.campaign:
        studies = load_studies(load_manifest(args.campaign))
    else:
        studies = [
            optuna.load_study(study_name=name, storage=args.storage)
            for name in args.studies
        ]
    report = build_comparison_report(
        studies,
        top_n=args.top_n,
        finalist_count=args.finalists,
        max_cluster_distance=args.max_cluster_distance,
    )
    print(comparison_table(report))
    print("\nParameter consensus:")
    for item in report["study_consensus"]:
        status = "STABLE" if item["stable"] else "UNSTABLE"
        print(f"  {item['study_name']} (seed={item['seed']}): {status}")
        print(f"    central: {json.dumps(item['parameters'], sort_keys=True)}")
        print(f"    adoption: {json.dumps(item['feature_adoption'], sort_keys=True)}")
        if item["unstable"]:
            print(f"    flags: {json.dumps(item['unstable'], sort_keys=True)}")
    if args.out:
        atomic_write_json(Path(args.out), report)
        print(f"\nSaved comparison report -> {args.out}")


if __name__ == "__main__":
    main()
