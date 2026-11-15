"""Development activity and sizing must qualify before the holdout is touched."""
import json

import optuna
import pandas as pd
import pytest
from optuna.trial import create_trial

from quant.optimize import optimize
from quant.optimize.selection import (
    DEVELOPMENT_ELIGIBILITY_VERSION, NO_QUALIFYING_FOLDS_SCORE,
    development_assessment, qualifying_trial, search_diagnostics,
)
from quant.tests.test_optimize_outcomes import search


def folds(ratios=(1, 1, 1, 1, 1), normal=(2, 2, 2, 2, 2), stressed=(2, 2, 2, 2, 2)):
    return [dict(fold=i + 1, normal_ratio=ratio, stressed_ratio=ratio - .1,
                 trades=n, stressed_trades=s)
            for i, (ratio, n, s) in enumerate(zip(ratios, normal, stressed))]


@pytest.mark.parametrize("records,expected", [
    (folds(normal=(2, 2, 2, 0, 0)), "normal costs"),
    (folds(stressed=(2, 2, 2, 0, 0)), "stressed costs"),
    (folds(ratios=(1, 1, -1, -1, -1)), "2 of 5 positive folds; 3 required."),
    (folds(ratios=(1, float("nan"), 1, 1, 1)), "nonfinite"),
    ([], "No development folds"),
])
def test_activity_and_positive_return_requirements_are_independent(records, expected):
    assessment = development_assessment(records, .6)
    assert assessment["passed"] is False
    assert expected in " ".join(assessment["reasons"])


def test_assessment_reports_only_observed_whole_share_blockers():
    records = folds(normal=(2, 2, 2, 0, 0), stressed=(2, 2, 2, 0, 0))
    records[3]["sizing_diagnostics"] = {"normal": {"QQQ.IBKR": {
        "entry_attempts": 14, "below_one_share": 14,
        "max_raw_quantity": .637, "max_risk_cap_usd": 12.5}}}
    assessment = development_assessment(records, .6)
    reasons = " ".join(assessment["reasons"])
    assert "Fold 4: whole-share sizing blocked entries" in reasons
    assert "QQQ.IBKR: 14 attempts below one share" in reasons
    assert "0.637" in reasons and "$12.50" in reasons
    assert "Fold 5: whole-share" not in reasons
    json.dumps(assessment, allow_nan=False)


def test_active_mixed_returns_can_qualify_and_negative_penalties_are_allowed():
    records = folds(ratios=(.1, .1, .1, -1, -1))
    assert development_assessment(records, .6)["passed"]
    value = optimize.stability_aware_score(
        [f["normal_ratio"] for f in records], [f["stressed_ratio"] for f in records], [1] * 5)
    assert NO_QUALIFYING_FOLDS_SCORE < value < 0


@pytest.mark.parametrize("normal,stressed,ratios", [
    ((2, 2, 2, 0, 0), (2, 2, 2, 0, 0), (-1.65, .56, .36, 0, 0)),
    ((2, 2, 2, 0, 0), (2, 2, 2, 0, 0), (1, 1, 1, 0, 0)),
    ((2, 2, 2, 2, 2), (2, 2, 2, 0, 0), (1, 1, 1, 1, 1)),
    ((2, 2, 2, 2, 2), (2, 2, 2, 2, 2), (1, 1, -1, -1, -1)),
])
def test_real_objective_rejections_preserve_holdout_and_parameters(search, monkeypatch, normal, stressed, ratios):
    run, output, progress_path, original, directory = search
    objective_factory = optimize.make_objective
    def evaluate(actual_folds, *args, **kwargs):
        for fold, ratio, n, s in zip(actual_folds, ratios, normal, stressed):
            sizing = {"QQQ.IBKR": {"entry_attempts": 4, "below_one_share": 4,
                "max_raw_quantity": .637, "max_risk_cap_usd": 12.5}} if not n else {}
            yield fold, optimize.FoldPerformance(ratio, .1, n, sizing_diagnostics=sizing), optimize.FoldPerformance(ratio - .1, .1, s)
    monkeypatch.setattr(optimize, "evaluate_folds", evaluate)
    study = run([0], objective_factory=objective_factory)
    assert qualifying_trial(study) is None
    assert study.trials[0].value == NO_QUALIFYING_FOLDS_SCORE
    assert not study.trials[0].user_attrs["development_assessment"]["passed"]
    assert output.read_bytes() == original
    assert not study.user_attrs.get("final_test_evaluated")
    assert study.user_attrs["outer_holdout_status"] == "UNTOUCHED"
    progress = json.loads(progress_path.read_text())
    saved = json.loads((directory / "runs/search_test.json").read_text())
    assert progress["search_diagnostics"] == saved["search_diagnostics"]
    diagnostics = progress["search_diagnostics"]
    assert diagnostics["qualifying_trials"] == 0 and diagnostics["require_active_folds"]
    if 0 in normal:
        assert "whole-share sizing" in " ".join(diagnostics["rejected_trials"][0]["reasons"])
        from quant.api import runs
        from starlette.responses import JSONResponse
        report = runs.get_run_research("search_test")
        assert JSONResponse(report).status_code == 200
        assert report["optimization"]["search_diagnostics"] == diagnostics
    assert study.user_attrs["validation_contract"]["development_eligibility_version"] == DEVELOPMENT_ELIGIBILITY_VERSION


def test_missing_stressed_activity_is_explicit_and_fails_closed():
    records = folds()
    records[0].pop("stressed_trades")
    assessment = development_assessment(records, .6)
    assert not assessment["passed"]
    assert assessment["reasons"] == ["Fold 1: executed-trade counts are missing or invalid under stressed costs."]


def test_real_objective_active_candidate_reaches_holdout(search, monkeypatch):
    run, *_ = search
    objective_factory = optimize.make_objective
    def evaluate(actual_folds, *args, **kwargs):
        for fold in actual_folds:
            yield fold, optimize.FoldPerformance(1, .1, 2), optimize.FoldPerformance(.8, .1, 2)
    monkeypatch.setattr(optimize, "evaluate_folds", evaluate)
    def holdout(**kwargs):
        raise RuntimeError("ACTIVE_QUALIFIED_HOLDOUT_BOUNDARY")
    monkeypatch.setattr(optimize, "build_and_run", holdout)
    with pytest.raises(RuntimeError, match="ACTIVE_QUALIFIED_HOLDOUT_BOUNDARY"):
        run([0], objective_factory=objective_factory)


def test_missing_or_failed_assessment_cannot_win_versioned_study():
    study = optuna.create_study(direction="maximize")
    study.set_user_attr("validation_contract", {"development_eligibility_version": DEVELOPMENT_ELIGIBILITY_VERSION})
    study.add_trial(create_trial(value=3))
    study.add_trial(create_trial(value=2, user_attrs={"development_assessment": {"passed": False}}))
    assert qualifying_trial(study) is None
    study.add_trial(create_trial(value=-.25, user_attrs={"development_assessment": {"passed": True}}))
    assert qualifying_trial(study).value == -.25


def test_resume_does_not_mix_eligibility_policies(search):
    run, *_ = search
    study = run([NO_QUALIFYING_FOLDS_SCORE])
    contract = study.user_attrs["validation_contract"]
    contract.pop("development_eligibility_version")
    contract.pop("require_active_folds")
    study.set_user_attr("validation_contract", contract)
    with pytest.raises(ValueError, match="different data/validation contract"):
        run([.25], "--resume-run-id", study.study_name, run_id="new_policy")
    assert len(study.trials) == 1


@pytest.mark.parametrize("asset,ticker", [("equity", "QQQ"), ("crypto", "BTC")])
def test_native_sizing_observations_preserve_execution_and_fractional_crypto(tmp_path, monkeypatch, asset, ticker):
    from quant.models.prediction_engine import PredictionEngine
    # Fixed synthetic forecast exercises execution; no market holdout is replayed.
    monkeypatch.setattr(PredictionEngine, "predict_move", lambda *args, **kwargs: .01)
    dates = pd.date_range("2024-01-01", periods=200, freq="B", tz="UTC")
    source = tmp_path / "bars.csv"
    pd.DataFrame(dict(timestamp=dates, ticker=ticker, open=100., high=105.,
                      low=95., close=100., volume=100000.)).to_csv(source, index=False)
    task = dict(csv_path=str(source), tickers=[ticker], starting_cash=5000,
        asset_class=asset, log_level="ERROR", bypass_logging=True,
        fill_model_seed=1729, strategy_overrides=dict(
            use_regime_features=False, use_hmm_feature=False, use_news_features=False,
            use_industry_features=False, warmup_bars=40, min_train_bars=30,
            refit_every_n_bars=12, entry_threshold=.003558, atr_period=17,
            atr_stop_mult=3.855197419778216, use_kelly_sizing=False,
            backtest_model_fit_end_ns=dates[149].value,
            backtest_trade_start_ns=dates[153].value))
    baseline = optimize.evaluate_backtest(task)
    captured = optimize.evaluate_backtest(task | {"evidence_bounds": (dates[153].value, dates[-1].value)})
    assert baseline == captured
    assert baseline.sizing_diagnostics == captured.sizing_diagnostics
    if asset == "equity":
        observation = baseline.sizing_diagnostics["QQQ.IBKR"]
        assert baseline.trades == 0
        assert observation["entry_attempts"] == observation["below_one_share"] > 0
        assert observation["max_raw_quantity"] == pytest.approx(12.5 / (10 * 3.855197419778216))
        assert observation["max_risk_cap_usd"] == 12.5
    else:
        assert baseline.trades > 0 and not baseline.sizing_diagnostics
