import optuna
import pytest
from optuna.distributions import CategoricalDistribution, FloatDistribution, IntDistribution
from optuna.trial import create_trial

from quant.optimize.campaign import invariant_validation_contract
from quant.optimize.compare import build_comparison_report, comparison_table
from quant.optimize.multi_seed import _validate_optimizer_args
from quant.optimize.stability import (
    PromotionRulesEngine,
    extract_parameter_consensus,
    similar_seed_clusters,
    summarize_study,
)


DISTRIBUTIONS = {
    "n_lags": IntDistribution(3, 15),
    "horizon": IntDistribution(1, 5),
    "entry_threshold": FloatDistribution(1e-4, 5e-3, log=True),
    "use_limit_orders": CategoricalDistribution([True, False]),
    "limit_offset_bps": FloatDistribution(0.5, 10.0, log=True),
    "use_kelly_sizing": CategoricalDistribution([True, False]),
    "kelly_fraction": FloatDistribution(0.05, 1.0),
    "cross_asset_lags": IntDistribution(0, 5),
    "spread_lags": IntDistribution(0, 5),
}


def _params(offset=0.0, use_kelly=True):
    return {
        "n_lags": 8,
        "horizon": 2,
        "entry_threshold": 0.001 + offset,
        "use_limit_orders": True,
        "limit_offset_bps": 2.0 + offset,
        "use_kelly_sizing": use_kelly,
        "kelly_fraction": 0.25 + offset,
        "cross_asset_lags": 2,
        "spread_lags": 1,
    }


def _folds(stress=0.6):
    return [
        {
            "fold": number,
            "normal_ratio": 0.8 + number * 0.05,
            "stressed_ratio": stress + number * 0.03,
            "turnover": 1.0 + number * 0.1,
            "trades": 4 + number,
        }
        for number in range(1, 6)
    ]


def _study(name, seed, *, offset=0.0, kelly_votes=None):
    study = optuna.create_study(study_name=name, direction="maximize")
    study.set_user_attr("sampler_seed", seed)
    study.set_user_attr(
        "validation_contract",
        {
            "source_csv_sha256": "same-data",
            "tickers": ["BTC", "ETH"],
            "fold_boundaries": [1, 2, 3, 4, 5],
            "embargo_bars": 5,
            "stress_cost_multiplier": 2.0,
        },
    )
    votes = kelly_votes or [True] * 6
    for index, use_kelly in enumerate(votes):
        folds = _folds()
        study.add_trial(
            create_trial(
                value=1.0 - index * 0.02,
                params=_params(offset + index * 1e-5, use_kelly),
                distributions=DISTRIBUTIONS,
                user_attrs={
                    "walk_forward_folds": folds,
                    "positive_folds": 5,
                    "fold_count": 5,
                },
            )
        )
    return study


def test_study_comparison_uses_top_trial_distribution_and_fold_metrics():
    summary = summarize_study(_study("seed-42", 42), top_n=5)

    assert summary.best_objective == 1.0
    assert summary.top_n_median == pytest.approx(0.96)
    assert summary.positive_folds == 5
    assert summary.fold_count == 5
    assert summary.cost_degradation > 0.0
    assert summary.trade_count == sum(4 + number for number in range(1, 6))
    assert not summary.best_is_outlier


def test_consensus_uses_medians_majorities_and_flags_split_feature_adoption():
    study = _study(
        "split-kelly",
        42,
        kelly_votes=[True, True, True, False, False, False],
    )

    consensus = extract_parameter_consensus(study.trials)

    assert consensus["parameters"]["n_lags"] == 8
    assert consensus["feature_adoption"]["use_kelly_sizing"]["share"] == 0.5
    assert not consensus["stable"]
    assert any(
        flag["reason"] == "feature_adoption_split"
        and flag["parameter"] == "use_kelly_sizing"
        for flag in consensus["unstable"]
    )


def test_comparison_validates_contract_and_finds_three_seed_cluster():
    studies = [
        _study("seed-42", 42, offset=0.0),
        _study("seed-43", 43, offset=0.00001),
        _study("seed-44", 44, offset=0.00002),
    ]

    report = build_comparison_report(studies, top_n=5, finalist_count=5)
    clusters = similar_seed_clusters(studies, top_n=5)

    assert report["rankings"][0]["top_n_median"] == pytest.approx(0.96)
    assert clusters[0]["size"] == 3
    assert set(clusters[0]["seeds"]) == {42, 43, 44}
    assert "cost degr" in comparison_table(report)
    assert invariant_validation_contract(studies)["tickers"] == ["BTC", "ETH"]


def test_invariant_contract_rejects_seed_specific_validation_changes():
    studies = [_study("seed-42", 42), _study("seed-43", 43)]
    studies[1].set_user_attr(
        "validation_contract",
        {**studies[1].user_attrs["validation_contract"], "embargo_bars": 6},
    )

    with pytest.raises(ValueError, match="invariant validation contract"):
        invariant_validation_contract(studies)


def test_promotion_rules_require_cluster_non_outlier_positive_stress_and_robustness():
    studies = [
        _study("seed-42", 42),
        _study("seed-43", 43, offset=0.00001),
        _study("seed-44", 44, offset=0.00002),
    ]
    candidate = {
        "study_name": "seed-42",
        "trial_number": 0,
        "params": studies[0].trials[0].params,
    }
    robustness = {"finalists": [{**candidate, "passed": True}]}

    decision = PromotionRulesEngine().evaluate(
        studies=studies,
        candidate=candidate,
        robustness_report=robustness,
        top_n=5,
    )

    assert decision.passed
    assert all(decision.checks.values())

    robustness["finalists"][0]["passed"] = False
    blocked = PromotionRulesEngine().evaluate(
        studies=studies,
        candidate=candidate,
        robustness_report=robustness,
        top_n=5,
    )
    assert not blocked.passed
    assert "robustness_suite_positive" in blocked.reasons

    tampered = {**candidate, "params": {**candidate["params"], "n_lags": 9}}
    tampered_report = {"finalists": [{**tampered, "passed": True}]}
    blocked = PromotionRulesEngine().evaluate(
        studies=studies,
        candidate=tampered,
        robustness_report=tampered_report,
        top_n=5,
    )
    assert not blocked.checks["candidate_parameters_locked"]


@pytest.mark.parametrize("flag", ["--seed", "--trials", "--defer-final-test"])
def test_multi_seed_rejects_runner_managed_optimizer_flags(flag):
    with pytest.raises(ValueError, match="manages"):
        _validate_optimizer_args([flag, "42"])


@pytest.mark.parametrize("flag", ["--score", "--fetch-missing", "--replace-bars"])
def test_multi_seed_rejects_non_invariant_or_early_stop_modes(flag):
    with pytest.raises(ValueError, match="not allowed"):
        _validate_optimizer_args([flag])
