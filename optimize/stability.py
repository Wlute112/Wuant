"""Cross-study statistics, parameter consensus, and promotion rules."""
from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import asdict, dataclass
from itertools import combinations
from statistics import median
from typing import Any, Iterable

import numpy as np
import optuna
from optuna.distributions import CategoricalDistribution, FloatDistribution, IntDistribution
from optuna.trial import FrozenTrial, TrialState


CONDITIONAL_PARAMETERS = {
    "limit_offset_bps": ("use_limit_orders", True),
    "kelly_fraction": ("use_kelly_sizing", True),
}
FEATURE_ADOPTION_PARAMETERS = (
    "use_limit_orders",
    "use_kelly_sizing",
    "cross_asset_lags",
    "spread_lags",
)


def completed_trials(study: optuna.Study) -> list[FrozenTrial]:
    return sorted(
        (
            trial
            for trial in study.get_trials(deepcopy=False, states=(TrialState.COMPLETE,))
            if trial.value is not None and math.isfinite(float(trial.value))
        ),
        key=lambda trial: float(trial.value),
        reverse=True,
    )


def _folds(trial: FrozenTrial) -> list[dict[str, Any]]:
    value = trial.user_attrs.get("walk_forward_folds", [])
    return value if isinstance(value, list) else []


def _trial_fold_stat(trial: FrozenTrial, key: str, aggregate=np.median) -> float:
    values = [float(row[key]) for row in _folds(trial) if row.get(key) is not None]
    return float(aggregate(values)) if values else float("nan")


@dataclass(frozen=True)
class StudyComparison:
    rank: int
    study_name: str
    seed: int | None
    complete_trials: int
    best_objective: float
    top_n_median: float
    fold_dispersion: float
    positive_folds: int
    fold_count: int
    cost_degradation: float
    turnover: float
    trade_count: int
    robust_rank_score: float
    best_is_outlier: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def best_trial_is_outlier(
    trials: Iterable[FrozenTrial],
    *,
    top_n: int = 10,
    mad_multiplier: float = 3.0,
    relative_margin: float = 0.25,
) -> bool:
    ordered = sorted(
        (trial for trial in trials if trial.value is not None),
        key=lambda trial: float(trial.value),
        reverse=True,
    )[:top_n]
    if len(ordered) < 3:
        return True
    values = np.asarray([float(trial.value) for trial in ordered], dtype=float)
    center = float(np.median(values))
    mad = float(np.median(np.abs(values - center)))
    allowance = max(1.4826 * mad_multiplier * mad, abs(center) * relative_margin, 1e-9)
    return float(values[0]) > center + allowance


def summarize_study(study: optuna.Study, *, top_n: int = 10) -> StudyComparison:
    trials = completed_trials(study)
    if not trials:
        raise ValueError(f"study {study.study_name!r} has no completed trials")
    top = trials[:top_n]
    best = top[0]
    top_values = [float(trial.value) for trial in top]
    fold_dispersion = [
        _trial_fold_stat(trial, "normal_ratio", np.std)
        for trial in top
        if _folds(trial)
    ]
    degradation = []
    turnovers = []
    trade_counts = []
    for trial in top:
        rows = _folds(trial)
        degradation.extend(
            max(0.0, float(row["normal_ratio"]) - float(row["stressed_ratio"]))
            for row in rows
            if row.get("normal_ratio") is not None and row.get("stressed_ratio") is not None
        )
        turnovers.extend(
            float(row["turnover"])
            for row in rows
            if row.get("turnover") is not None
        )
        trade_counts.append(sum(int(row.get("trades", 0)) for row in rows))
    dispersion = float(np.median(fold_dispersion)) if fold_dispersion else float("nan")
    cost_degradation = float(np.median(degradation)) if degradation else float("nan")
    turnover = float(np.median(turnovers)) if turnovers else float("nan")
    trade_count = int(round(float(np.median(trade_counts)))) if trade_counts else 0
    top_median = float(np.median(top_values))
    robust_rank_score = top_median
    if math.isfinite(dispersion):
        robust_rank_score -= 0.5 * dispersion
    if math.isfinite(cost_degradation):
        robust_rank_score -= 0.5 * cost_degradation
    return StudyComparison(
        rank=0,
        study_name=study.study_name,
        seed=study.user_attrs.get("sampler_seed"),
        complete_trials=len(trials),
        best_objective=float(best.value),
        top_n_median=top_median,
        fold_dispersion=dispersion,
        positive_folds=int(best.user_attrs.get("positive_folds", 0)),
        fold_count=int(best.user_attrs.get("fold_count", len(_folds(best)))),
        cost_degradation=cost_degradation,
        turnover=turnover,
        trade_count=trade_count,
        robust_rank_score=robust_rank_score,
        best_is_outlier=best_trial_is_outlier(trials, top_n=top_n),
    )


def rank_studies(studies: Iterable[optuna.Study], *, top_n: int = 10) -> list[StudyComparison]:
    summaries = sorted(
        (summarize_study(study, top_n=top_n) for study in studies),
        key=lambda item: (
            item.robust_rank_score,
            item.top_n_median,
            -item.fold_dispersion if math.isfinite(item.fold_dispersion) else -math.inf,
        ),
        reverse=True,
    )
    return [StudyComparison(**{**summary.to_dict(), "rank": rank}) for rank, summary in enumerate(summaries, 1)]


def top_trials(study: optuna.Study, top_n: int) -> list[FrozenTrial]:
    if not 5 <= top_n <= 10:
        raise ValueError("top_n must be between 5 and 10 for consensus extraction")
    return completed_trials(study)[:top_n]


def _adopted(parameter: str, value: Any) -> bool:
    if parameter in {"cross_asset_lags", "spread_lags"}:
        return int(value) > 0
    return bool(value)


def _categorical_mode(values: list[Any]) -> tuple[Any, float]:
    counts = Counter(json.dumps(value, sort_keys=True) for value in values)
    encoded, count = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0]
    return json.loads(encoded), count / len(values)


def extract_parameter_consensus(
    trials: Iterable[FrozenTrial],
    *,
    instability_threshold: float = 0.70,
) -> dict[str, Any]:
    selected = list(trials)
    if not selected:
        raise ValueError("cannot extract consensus from zero trials")
    parameter_names = sorted({name for trial in selected for name in trial.params})
    parameters: dict[str, Any] = {}
    confidence: dict[str, float] = {}
    unstable: list[dict[str, Any]] = []
    for name in parameter_names:
        values = [trial.params[name] for trial in selected if name in trial.params]
        distribution = next(
            trial.distributions[name]
            for trial in selected
            if name in trial.distributions
        )
        if isinstance(distribution, CategoricalDistribution):
            value, share = _categorical_mode(values)
            parameters[name] = value
            confidence[name] = share
            if share < instability_threshold:
                unstable.append(
                    {"parameter": name, "reason": "categorical_split", "majority_share": share}
                )
        elif isinstance(distribution, IntDistribution):
            parameters[name] = int(round(float(median(int(value) for value in values))))
        else:
            parameters[name] = float(median(float(value) for value in values))

    feature_adoption: dict[str, Any] = {}
    for name in FEATURE_ADOPTION_PARAMETERS:
        values = [trial.params[name] for trial in selected if name in trial.params]
        if not values:
            continue
        adopted = sum(_adopted(name, value) for value in values)
        share = adopted / len(values)
        feature_adoption[name] = {"adopted": adopted, "total": len(values), "share": share}
        if min(share, 1.0 - share) > (1.0 - instability_threshold):
            unstable.append(
                {"parameter": name, "reason": "feature_adoption_split", "adoption_share": share}
            )
    return {
        "trial_count": len(selected),
        "parameters": parameters,
        "categorical_confidence": confidence,
        "feature_adoption": feature_adoption,
        "unstable": unstable,
        "stable": not unstable,
    }


def study_consensus(study: optuna.Study, *, top_n: int = 10) -> dict[str, Any]:
    consensus = extract_parameter_consensus(top_trials(study, top_n))
    consensus.update(
        {
            "study_name": study.study_name,
            "seed": study.user_attrs.get("sampler_seed"),
        }
    )
    return consensus


def _numeric_coordinate(value: float, distribution: Any) -> float:
    low = float(distribution.low)
    high = float(distribution.high)
    current = float(value)
    if isinstance(distribution, FloatDistribution) and distribution.log:
        low, high, current = math.log(low), math.log(high), math.log(current)
    return 0.0 if high == low else (current - low) / (high - low)


def consensus_distance(
    left: dict[str, Any],
    right: dict[str, Any],
    distributions: dict[str, Any],
) -> float:
    left_params = left["parameters"]
    right_params = right["parameters"]
    distances = []
    for name in sorted(set(left_params) & set(right_params)):
        condition = CONDITIONAL_PARAMETERS.get(name)
        if condition is not None:
            gate, active = condition
            if left_params.get(gate) != active and right_params.get(gate) != active:
                continue
        distribution = distributions.get(name)
        if isinstance(distribution, CategoricalDistribution):
            distances.append(0.0 if left_params[name] == right_params[name] else 1.0)
        elif isinstance(distribution, (FloatDistribution, IntDistribution)):
            distances.append(
                abs(
                    _numeric_coordinate(left_params[name], distribution)
                    - _numeric_coordinate(right_params[name], distribution)
                )
            )
    return float(np.mean(distances)) if distances else math.inf


def similar_seed_clusters(
    studies: Iterable[optuna.Study],
    *,
    top_n: int = 10,
    max_distance: float = 0.20,
) -> list[dict[str, Any]]:
    study_list = list(studies)
    consensuses = [study_consensus(study, top_n=top_n) for study in study_list]
    distributions: dict[str, Any] = {}
    for study in study_list:
        trials = completed_trials(study)
        if trials:
            distributions.update(trials[0].distributions)
    adjacency = {index: {index} for index in range(len(consensuses))}
    pairwise = []
    for left_index, right_index in combinations(range(len(consensuses)), 2):
        distance = consensus_distance(
            consensuses[left_index], consensuses[right_index], distributions
        )
        left_params = consensuses[left_index]["parameters"]
        right_params = consensuses[right_index]["parameters"]
        feature_agreement = all(
            _adopted(name, left_params[name]) == _adopted(name, right_params[name])
            for name in FEATURE_ADOPTION_PARAMETERS
            if name in left_params and name in right_params
        )
        similar = distance <= max_distance and feature_agreement
        pairwise.append(
            {
                "left": consensuses[left_index]["study_name"],
                "right": consensuses[right_index]["study_name"],
                "distance": distance,
                "feature_adoption_agreement": feature_agreement,
                "similar": similar,
            }
        )
        if similar:
            adjacency[left_index].add(right_index)
            adjacency[right_index].add(left_index)

    # Maximal cliques prevent transitive chaining from turning two dissimilar
    # endpoints into one allegedly stable parameter cluster.
    cliques: list[set[int]] = []
    candidates = [set([index]) for index in range(len(consensuses))]
    for size in range(2, len(consensuses) + 1):
        for indices in combinations(range(len(consensuses)), size):
            cluster = set(indices)
            if all(right in adjacency[left] for left, right in combinations(indices, 2)):
                candidates.append(cluster)
    for candidate in sorted(candidates, key=lambda value: (-len(value), sorted(value))):
        if not any(candidate < existing for existing in cliques):
            cliques.append(candidate)
    return [
        {
            "size": len(indices),
            "study_names": [consensuses[index]["study_name"] for index in sorted(indices)],
            "seeds": [consensuses[index]["seed"] for index in sorted(indices)],
            "all_consensus_stable": all(consensuses[index]["stable"] for index in indices),
            "consensus": extract_parameter_consensus(
                trial
                for index in indices
                for trial in top_trials(study_list[index], top_n)
            ),
            "pairwise": [
                row
                for row in pairwise
                if row["left"] in {consensuses[index]["study_name"] for index in indices}
                and row["right"] in {consensuses[index]["study_name"] for index in indices}
            ],
        }
        for indices in cliques
    ]


def select_finalists(studies: Iterable[optuna.Study], *, count: int = 5) -> list[dict[str, Any]]:
    if not 5 <= count <= 10:
        raise ValueError("finalist count must be between 5 and 10")
    study_list = list(studies)
    queues = [completed_trials(study) for study in study_list]
    finalists = []
    seen = set()
    depth = 0
    while len(finalists) < count and any(depth < len(queue) for queue in queues):
        for study, queue in zip(study_list, queues):
            if depth >= len(queue) or len(finalists) >= count:
                continue
            trial = queue[depth]
            digest = json.dumps(trial.params, sort_keys=True, separators=(",", ":"))
            if digest in seen:
                continue
            seen.add(digest)
            finalists.append(
                {
                    "study_name": study.study_name,
                    "seed": study.user_attrs.get("sampler_seed"),
                    "trial_number": trial.number,
                    "objective": float(trial.value),
                    "params": dict(trial.params),
                }
            )
        depth += 1
    if len(finalists) < count:
        raise ValueError(f"only {len(finalists)} unique completed parameter sets are available")
    return finalists


@dataclass(frozen=True)
class PromotionDecision:
    passed: bool
    checks: dict[str, bool]
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "checks": self.checks, "reasons": list(self.reasons)}


class PromotionRulesEngine:
    """Fail-closed pre-holdout promotion checks."""

    def __init__(self, *, minimum_similar_seeds: int = 3) -> None:
        self.minimum_similar_seeds = minimum_similar_seeds

    def evaluate(
        self,
        *,
        studies: Iterable[optuna.Study],
        candidate: dict[str, Any],
        robustness_report: dict[str, Any],
        top_n: int = 10,
        max_cluster_distance: float = 0.20,
    ) -> PromotionDecision:
        study_list = list(studies)
        clusters = similar_seed_clusters(
            study_list, top_n=top_n, max_distance=max_cluster_distance
        )
        candidate_study = str(candidate.get("study_name", ""))
        source = next(
            (study for study in study_list if study.study_name == candidate_study), None
        )
        trial = None
        if source is not None:
            trial = next(
                (
                    item
                    for item in completed_trials(source)
                    if item.number == int(candidate.get("trial_number", -1))
                ),
                None,
            )
        eligible_clusters = [
            cluster
            for cluster in clusters
            if candidate_study in cluster["study_names"]
            and cluster["size"] >= self.minimum_similar_seeds
        ]
        cluster_ok = bool(eligible_clusters)
        candidate_params = candidate.get("params", {})
        matching_clusters = []
        if trial is not None and isinstance(candidate_params, dict):
            for cluster in eligible_clusters:
                cluster_params = cluster["consensus"]["parameters"]
                feature_agreement = all(
                    _adopted(name, candidate_params[name])
                    == _adopted(name, cluster_params[name])
                    for name in FEATURE_ADOPTION_PARAMETERS
                    if name in candidate_params and name in cluster_params
                )
                distance = consensus_distance(
                    {"parameters": candidate_params},
                    cluster["consensus"],
                    trial.distributions,
                )
                if feature_agreement and distance <= max_cluster_distance:
                    matching_clusters.append(cluster)
        candidate_matches_cluster = bool(matching_clusters)
        relevant_cluster = matching_clusters or eligible_clusters
        relevant_names = (
            set(relevant_cluster[0]["study_names"])
            if relevant_cluster
            else {candidate_study}
        )
        outlier_ok = all(
            not best_trial_is_outlier(completed_trials(study), top_n=top_n)
            for study in study_list
            if study.study_name in relevant_names
        )
        fold_rows = _folds(trial) if trial is not None else []
        candidate_params_locked = bool(
            trial is not None and dict(candidate.get("params", {})) == dict(trial.params)
        )
        folds_positive = bool(fold_rows) and all(
            float(row.get("normal_ratio", 0.0)) > 0.0 for row in fold_rows
        )
        stressed_positive = bool(fold_rows) and all(
            float(row.get("stressed_ratio", 0.0)) > 0.0 for row in fold_rows
        )

        finalist_match = next(
            (
                item
                for item in robustness_report.get("finalists", [])
                if item.get("study_name") == candidate_study
                and int(item.get("trial_number", -1)) == int(candidate.get("trial_number", -2))
            ),
            None,
        )
        robustness_positive = bool(finalist_match and finalist_match.get("passed"))
        holdout_untouched = all(
            not bool(study.user_attrs.get("final_test_evaluated")) for study in study_list
        )
        checks = {
            "three_similar_seed_clusters": cluster_ok,
            "candidate_matches_seed_cluster": candidate_matches_cluster,
            "candidate_parameters_locked": candidate_params_locked,
            "top_trials_not_outliers": outlier_ok,
            "all_selection_folds_positive": folds_positive,
            "all_selection_stressed_folds_positive": stressed_positive,
            "robustness_suite_positive": robustness_positive,
            "outer_holdout_untouched": holdout_untouched,
        }
        reasons = tuple(name for name, passed in checks.items() if not passed)
        return PromotionDecision(not reasons, checks, reasons)
