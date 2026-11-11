"""Exercise the real search lifecycle with cheap, controlled Optuna objectives."""
import json
from contextlib import nullcontext
from pathlib import Path

import optuna
import pandas as pd
import pytest
from optuna.trial import TrialState, create_trial

from quant.optimize import optimize
from quant.optimize.selection import NO_QUALIFYING_CANDIDATE, NO_QUALIFYING_FOLDS_SCORE, qualifying_trial
from quant.run import artifacts, research


@pytest.fixture
def search(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "bars.csv"
    timestamps = pd.date_range("2022-01-01", periods=400, freq="D", tz="UTC")
    pd.DataFrame({"timestamp": timestamps, "ticker": "BTC", "open": 100.,
                  "high": 101., "low": 99., "close": 100., "volume": 1000.}).to_csv(source, index=False)
    output = tmp_path / "best_params.json"
    original = b'{"params":{"existing_winner":true}}\n'
    output.write_bytes(original)
    progress = tmp_path / "progress.json"
    storage = f"sqlite:///{tmp_path / 'study.db'}"
    monkeypatch.setattr(artifacts, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(optimize, "ComputePool", lambda plan: nullcontext(None))
    monkeypatch.setattr(optimize, "build_and_run", lambda **kwargs: pytest.fail("Rejected search touched the holdout"))
    monkeypatch.setattr(optimize, "save_optimize_artifact", lambda **kwargs: pytest.fail("Rejected search saved a winning result"))
    def run(values, *extra, run_id="search_test"):
        sequence = iter(values)
        def objective(trial):
            trial.suggest_int("parameter", 1, 3)
            trial.suggest_int("horizon", 1, 3)
            trial.set_user_attr("walk_forward_folds", [{"fold": 1, "normal_ratio": -1., "trades": 2}])
            value = next(sequence)
            if value == "prune":
                raise optuna.TrialPruned()
            return value
        monkeypatch.setattr(optimize, "make_objective", lambda *args, **kwargs: objective)
        argv = ["optimize", "--csv", str(source), "--tickers", "BTC", "--workers", "1",
                "--trials", str(len(values)), "--seed", "42", "--storage", storage,
                "--run-id", run_id, "--out-params", str(output), "--progress-path", str(progress), *extra]
        monkeypatch.setattr("sys.argv", argv)
        optimize.main()
        study = optuna.load_study(study_name="search_test", storage=storage)
        return study
    return run, output, progress, original, tmp_path


@pytest.mark.parametrize("values,extra", [
    ([NO_QUALIFYING_FOLDS_SCORE] * 2, ()),
    (["prune", "prune"], ()),
    ([], ()),
    ([NO_QUALIFYING_FOLDS_SCORE] * 2, ("--defer-final-test",)),
    ([NO_QUALIFYING_FOLDS_SCORE] * 2, ("--score", "-2000000")),
    ([float("inf")], ()),
])
def test_no_qualifier_preserves_parameters_and_holdout_and_reports_outcome(search, values, extra):
    run, output, progress_path, original, directory = search
    study = run(values, *extra)
    assert output.read_bytes() == original
    assert study.user_attrs["outer_holdout_status"] == "UNTOUCHED"
    assert not study.user_attrs.get("final_test_evaluated")
    assert study.user_attrs["locked_candidate"] is None
    assert study.user_attrs["optimization_complete"] is False
    assert study.user_attrs["search_outcome"] == NO_QUALIFYING_CANDIDATE
    assert len(study.trials) == len(values)
    saved = json.loads((directory / "runs/search_test.json").read_text(), parse_constant=lambda value: pytest.fail(f"Nonfinite JSON value: {value}"))
    assert saved["selection_status"] == NO_QUALIFYING_CANDIDATE
    assert saved["best_params"] == {} and saved["in_sample_value"] is None
    assert saved["oos_metrics"] is None and saved["validation"]["final_test"] is None
    assert len(saved["trials"]) == len(values)
    assert saved["can_resume"] is True and saved["resume_run_id"] == study.study_name
    progress = json.loads(progress_path.read_text())
    assert progress["phase"] == "complete" and progress["outcome"] == NO_QUALIFYING_CANDIDATE
    assert progress["best_score"] is None and progress["outer_holdout_status"] == "UNTOUCHED"
    summary = artifacts.list_run_summaries()[0]
    assert summary["selection_status"] == NO_QUALIFYING_CANDIDATE
    report = research.analyze_run(saved)
    assert "not evaluated" in report["message"]
    assert report["optimization"]["best_params"] == {}
    assert report["optimization"]["best_trial_number"] is None
    assert all(item["incumbent"] is None for item in report["optimization"]["history"])


def test_rejected_study_can_resume_and_only_a_qualifier_can_replace_saved_parameters(search):
    run, output, progress, original, directory = search
    study = run([NO_QUALIFYING_FOLDS_SCORE])
    study = run([.25], "--resume-run-id", study.study_name, "--defer-final-test", run_id="resumed_test")
    saved = json.loads(output.read_text())
    assert saved["in_sample_value"] == .25 and saved["params"]
    assert study.user_attrs["optimization_complete"] is True
    assert not study.user_attrs.get("final_test_evaluated")
    assert study.user_attrs["search_outcome"] == "DEVELOPMENT_SELECTED"
    assert len(study.trials) == 2


def test_qualifying_candidate_reaches_holdout_after_selection(search, monkeypatch):
    run, output, *_ = search
    # A qualifying candidate must reach the existing evaluation path. Stop at
    # that boundary so no simulator/market evidence is fabricated by this test.
    def holdout(**kwargs):
        assert output.read_bytes() == b'{"params":{"existing_winner":true}}\n'
        raise RuntimeError("QUALIFIED_HOLDOUT_BOUNDARY")
    monkeypatch.setattr(optimize, "build_and_run", holdout)
    with pytest.raises(RuntimeError, match="QUALIFIED_HOLDOUT_BOUNDARY"):
        run([.25])


def test_selection_ignores_pruned_rejected_and_nonfinite_trials_but_accepts_negative_penalized_scores():
    study = optuna.create_study(direction="maximize")
    for trial in [create_trial(value=NO_QUALIFYING_FOLDS_SCORE), create_trial(value=float("inf")),
                  create_trial(value=-.25), create_trial(value=4, state=TrialState.PRUNED)]:
        study.add_trial(trial)
    assert qualifying_trial(study).value == -.25


def test_no_qualifier_artifact_and_analysis_are_available_through_the_api(search):
    from starlette.responses import JSONResponse
    from quant.api import runs
    run, *_ = search
    run([float("inf")])
    # Exercise route handlers and the strict JSON serialization used by HTTP
    # responses, without an optional HTTP test-client dependency.
    report = runs.get_run_research('search_test')
    assert JSONResponse(report).status_code == 200
    assert report['optimization']['best_params'] == {}
    detail = runs.get_run('search_test')
    assert JSONResponse(detail).status_code == 200
    assert detail['trials'][0]['value'] is None
    listing = runs.get_runs(kind=None)
    assert JSONResponse(listing).status_code == 200
    assert listing[0]['selection_status'] == NO_QUALIFYING_CANDIDATE


def test_seed_campaign_does_not_mark_rejected_search_complete(search, monkeypatch):
    from types import SimpleNamespace
    from quant.optimize import multi_seed
    _, _, _, _, directory = search
    storage = f"sqlite:///{directory / 'campaign_studies.db'}"
    manifest = directory / 'campaign.json'
    def subprocess_run(command, **kwargs):
        name = command[command.index('--run-id') + 1]
        study = optuna.create_study(study_name=name, storage=storage)
        study.set_user_attr('search_outcome', NO_QUALIFYING_CANDIDATE)
        study.set_user_attr('validation_contract', {'scheme': 'test'})
        study.set_user_attr('outer_holdout_status', 'UNTOUCHED')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(multi_seed.subprocess, 'run', subprocess_run)
    # Avoid shell-based resource probing: this is a campaign result test.
    monkeypatch.setattr(multi_seed, 'resolve_compute_plan', lambda *args, **kwargs: None)
    monkeypatch.setattr('sys.argv', ['multi_seed', '--campaign-id', 'campaign_test',
        '--manifest', str(manifest), '--storage', storage, '--csv', str(directory/'bars.csv'),
        '--seeds', '42', '43', '44', '--trials', '100'])
    with pytest.raises(SystemExit, match='Failed studies'):
        multi_seed.main()
    record = json.loads(manifest.read_text())['studies'][0]
    assert record['status'] == NO_QUALIFYING_CANDIDATE
    assert 'holdout remains untouched' in record['error']
    assert not Path(record['out_params']).exists()


@pytest.mark.parametrize("final_flag", [True, False])
def test_consumed_holdout_studies_cannot_be_reopened_by_the_no_qualifier_path(search, final_flag):
    run, output, _, original, _ = search
    study = run([NO_QUALIFYING_FOLDS_SCORE])
    study.set_user_attr('final_test_evaluated', final_flag)
    study.set_user_attr('outer_holdout_status', 'CONSUMED')
    with pytest.raises(ValueError, match='already consumed'):
        run([.25], '--resume-run-id', study.study_name, run_id='forbidden_resume')
    assert output.read_bytes() == original
    study = optuna.load_study(study_name=study.study_name, storage=study._storage)
    assert study.user_attrs['outer_holdout_status'] == 'CONSUMED'
