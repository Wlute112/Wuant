"""Optuna-only public configuration and trusted winner replay."""
import json

import optuna
import pytest
from fastapi import HTTPException

from quant.api import jobs_routes, strategy_selection
from quant.api.schemas import BacktestJobRequest, OptimizeJobRequest, PaperJobRequest, LiveJobRequest
from quant.optimize import optimize
from quant.run import artifacts
from quant.run.run_backtest import load_best_params
from quant.run.run_live import load_params
from quant.strategies.parameters import STRATEGY_ID, TUNABLE_KEYS, suggest_parameters


@pytest.fixture
def winner(tmp_path, monkeypatch):
    study = optuna.create_study(sampler=optuna.samplers.RandomSampler(seed=17))
    trial = study.ask()
    params = suggest_parameters(trial, ["QQQ"])
    run = {"kind": "optimize", "strategy_id": STRATEGY_ID, "asset_class": "equity",
           "allow_short_positions": False,
           "tickers": ["QQQ"], "best_params": params, "in_sample_value": 1.5,
           "bar_interval_minutes": 240, "include_extended_hours": False,
           "refit_every_n_bars": 3, "warmup_bars": 80, "min_train_bars": 60,
           "regime_window": 40, "equity_simulation": {"config": {"spread_bps": 7}},
           "ibkr_bar_hours": 4}
    monkeypatch.setattr(artifacts, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(jobs_routes, "JOBS_DIR", tmp_path)
    def save(**updates):
        run.update(updates)
        (tmp_path / "optuna_fixture.json").write_text(json.dumps(run))
        return run
    save()
    return save


@pytest.mark.parametrize("model,required", [
    (BacktestJobRequest, {}), (OptimizeJobRequest, {}),
    (PaperJobRequest, {"tickers": ["QQQ"]}),
    (LiveJobRequest, {"tickers": ["QQQ"], "port": 7496, "confirm": ""}),
])
@pytest.mark.parametrize("override", [
    {"params": {"n_lags": 3}}, {"params_path": "/tmp/manual.json"},
    {"features": {"n_lags": 3}}, {"features": {"use_hmm_feature": False}},
    {"n_lags": 3}, {"warmup_bars": 12},
])
def test_public_requests_reject_manual_strategy_settings(model, required, override):
    with pytest.raises(ValueError, match="Extra inputs"):
        model(**required, **override)


def test_backtest_without_winner_starts_existing_optuna_lifecycle(monkeypatch):
    submitted = []
    monkeypatch.setattr(jobs_routes, "start_optimize", lambda req: submitted.append(req) or {"kind": "optimize"})
    result = jobs_routes.start_backtest(BacktestJobRequest(
        name="Automatic", csv="observed.csv", tickers=["QQQ"], asset_class="equity",
        cash=6200, risk={"max_leverage": 0.75}, equity_simulation={"spread_bps": 9},
    ))
    assert result == {"kind": "optimize"}
    assert submitted[0].csv == "observed.csv"
    assert submitted[0].cash == 6200
    assert submitted[0].risk.max_leverage == 0.75
    assert submitted[0].equity_simulation.spread_bps == 9
    assert submitted[0].final_test_frac == 0.2
    assert submitted[0].walk_forward_folds == 5


def test_saved_winner_replays_exact_tuned_and_fixed_values_in_both_runners(winner):
    req = PaperJobRequest(tickers=["QQQ"], asset_class="equity", source_run_id="optuna_fixture", bar_hours=4)
    payload = strategy_selection.selected_strategy(req)
    path = jobs_routes._execution_params_file("paper_fixture", req)
    backtest = load_best_params(path)
    live, hours = load_params(path)
    assert hours == 4
    assert set(payload["params"]) == TUNABLE_KEYS
    for key, value in payload["params"].items():
        assert backtest[key] == live[key] == value
    for key in ("refit_every_n_bars", "warmup_bars", "min_train_bars", "regime_window"):
        assert backtest[key] == live[key] == payload[key]
    assert backtest["equity_simulation"] == {"spread_bps": 7}


@pytest.mark.parametrize("updates,message", [
    ({"kind": "backtest"}, "optimization result"),
    ({"strategy_id": "other"}, "another strategy"),
    ({"asset_class": "crypto"}, "another asset class"),
    ({"allow_short_positions": None}, "does not record its long/short policy"),
    ({"allow_short_positions": True}, "Long/short policy must match"),
    ({"tickers": ["SPY"]}, "Ticker universe"),
    ({"best_params": {"n_lags": 7}}, "winning candidate"),
    ({"in_sample_value": float("nan")}, "winning candidate"),
    ({"in_sample_value": -1e6}, "winning candidate"),
    ({"bar_interval_minutes": 60}, "Bar cadence"),
    ({"include_extended_hours": True}, "Session hours"),
])
def test_invalid_or_incompatible_winner_is_rejected(winner, updates, message):
    winner(**updates)
    req = PaperJobRequest(tickers=["QQQ"], asset_class="equity", source_run_id="optuna_fixture", bar_hours=4)
    with pytest.raises(HTTPException, match=message):
        strategy_selection.selected_strategy(req)


@pytest.mark.parametrize("run_id,status", [(None, 422), ("../secret", 422), ("absent", 404)])
def test_missing_or_unsafe_winner_ids(winner, run_id, status):
    with pytest.raises(HTTPException) as error:
        strategy_selection.selected_strategy(PaperJobRequest(tickers=["QQQ"], asset_class="equity", source_run_id=run_id))
    assert error.value.status_code == status


def test_legacy_optimizer_artifacts_and_explicit_risk_limits_remain_supported(winner):
    run = winner()
    del run["strategy_id"]
    winner()
    req = PaperJobRequest(tickers=["QQQ"], asset_class="equity", source_run_id="optuna_fixture", risk={"max_leverage": 0.5})
    path = jobs_routes._execution_params_file("paper_fixture", req)
    loaded, _ = load_params(path)
    assert loaded["max_leverage"] == 0.5
    assert loaded["n_lags"] == run["best_params"]["n_lags"]


def test_short_policy_is_preserved_for_matching_execution_and_replay(winner):
    winner(allow_short_positions=True)
    req = PaperJobRequest(tickers=["QQQ"], asset_class="equity", source_run_id="optuna_fixture", allow_shorts=True)
    payload = strategy_selection.selected_strategy(req)
    path = jobs_routes._execution_params_file("short_fixture", req)
    assert payload["allow_short_positions"] is True
    assert load_best_params(path)["allow_short_positions"] is True
    from quant.run.run_live import load_params_metadata
    assert load_params_metadata(path)["allow_short_positions"] is True


def test_execution_refuses_optimizer_payload_with_unknown_direction(tmp_path):
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps({"strategy_id": STRATEGY_ID, "params": {"n_lags": 9}}))
    with pytest.raises(ValueError, match="long/short policy"):
        load_params(path)
    with pytest.raises(ValueError, match="long/short policy"):
        load_best_params(path)


def test_backtest_accepts_dashboard_winner_format_without_losing_tuned_values(winner, tmp_path):
    run = winner()
    path = tmp_path / "dashboard_run.json"
    path.write_text(json.dumps(run))
    loaded = load_best_params(path)
    assert all(loaded[key] == value for key, value in run["best_params"].items())
    assert loaded["allow_short_positions"] is False


def test_optimizer_cannot_silently_replace_sampled_values():
    for key in TUNABLE_KEYS:
        with pytest.raises(ValueError, match="selected by Optuna"):
            optimize.make_objective((), ["QQQ"], 5000, structural_overrides={key: 0})


def test_real_optuna_trials_use_the_strategy_search_space(monkeypatch):
    observed = []
    def evaluate(*args):
        overrides = args[2]
        observed.append(overrides)
        yield type("Fold", (), {"number": 1, "validation_start_ns": 1, "validation_end_ns": 2})(), optimize.FoldPerformance(1.5, 1, 3), optimize.FoldPerformance(1.2, 1, 3)
    monkeypatch.setattr(optimize, "evaluate_folds", evaluate)
    study = optuna.create_study(direction="maximize", sampler=optuna.samplers.TPESampler(seed=42))
    study.optimize(optimize.make_objective((), ["QQQ"], 5000), n_trials=2)
    assert len(observed) == 2
    for trial, overrides in zip(study.trials, observed):
        assert set(trial.params) == TUNABLE_KEYS
        assert all(overrides[key] == value for key, value in trial.params.items())
        assert overrides["max_open_positions"] == 1
        assert trial.value > 0


def test_execution_inherits_observed_cadence_when_no_bar_width_is_given(winner):
    winner(ibkr_bar_hours=None)
    req = PaperJobRequest(tickers=["QQQ"], asset_class="equity", source_run_id="optuna_fixture")
    path = jobs_routes._execution_params_file("paper_fixture", req)
    _, hours = load_params(path)
    assert hours == 4


@pytest.mark.parametrize("cadence", [None, 30, 720])
def test_execution_requires_a_supported_observed_cadence(winner, cadence):
    winner(bar_interval_minutes=cadence)
    with pytest.raises(HTTPException, match="execution bar cadence"):
        strategy_selection.selected_strategy(PaperJobRequest(
            tickers=["QQQ"], asset_class="equity", source_run_id="optuna_fixture"))
