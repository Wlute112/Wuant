import json
from types import SimpleNamespace

import pytest

from quant.optimize import optimize
from quant.optimize.selection import (
    holdout_assessment, REJECTED_OUTER_HOLDOUT, PASSED_OUTER_HOLDOUT,
)
from quant.run import artifacts
from quant.run.run_live import load_params
from quant.api.strategy_selection import selected_strategy
from quant.api.schemas import PaperJobRequest
from fastapi import HTTPException
from quant.tests.test_optimize_outcomes import search


@pytest.mark.parametrize('changes', [
    {'trades': 0}, {'stressed_trades': 0}, {'normal_ratio': 0},
    {'normal_ratio': float('nan')}, {'stressed_ratio': float('inf')},
    {'normal_net_profit_usd': 0}, {'stressed_net_profit_usd': -1},
])
def test_inactive_nonpositive_and_nonfinite_holdouts_are_rejected(changes):
    final = dict(trades=4, stressed_trades=4, normal_ratio=1, stressed_ratio=.5,
                 normal_net_profit_usd=20, stressed_net_profit_usd=10)
    assert holdout_assessment({'final_test': final})['status'] == PASSED_OUTER_HOLDOUT
    final.update(changes)
    result = holdout_assessment({'final_test': final})
    assert result['status'] == REJECTED_OUTER_HOLDOUT and result['reasons']


@pytest.mark.parametrize('normal,stressed,passed', [
    ((0., 0., 0, 0.), (0., 0., 0, 0.), False),
    ((1., 1., 4, 20.), (.5, 0., 0, 0.), False),
    ((1., 1., 4, -1.), (.5, 1., 4, 10.), False),
    ((1., 1., 4, 20.), (-.5, 1., 4, -10.), False),
    ((1., 1., 4, 20.), (.5, 1., 4, 10.), True),
])
def test_real_search_publishes_only_after_active_profitable_holdout(search, monkeypatch, normal, stressed, passed):
    run, output, progress_path, original, directory = search
    disposed = []
    monkeypatch.setattr(optimize, 'build_and_run', lambda **kwargs: SimpleNamespace(dispose=lambda: disposed.append(True)))
    performance = iter([optimize.FoldPerformance(*normal), optimize.FoldPerformance(*stressed)])
    monkeypatch.setattr(optimize, '_engine_performance', lambda *args: next(performance))
    monkeypatch.setattr(optimize, 'save_optimize_artifact', artifacts.save_optimize_artifact)
    for name in ('news_series_by_ticker', 'ml_performance_by_ticker', 'regime_series_by_ticker', 'model_chart_by_ticker'):
        monkeypatch.setattr(artifacts, name, lambda *args, **kwargs: {})
    monkeypatch.setattr(artifacts, 'equity_curve_with_timestamps', lambda *args: [])
    metrics = dict(total_trades=normal[2], net_profit_usd=normal[3], primary_score=normal[0])
    monkeypatch.setattr(artifacts, 'compute_metrics', lambda *args: SimpleNamespace(as_dict=lambda: dict(metrics)))
    monkeypatch.setattr('quant.run.equity_simulation.simulation_report', lambda *args: {})
    study = run([.25])
    status = PASSED_OUTER_HOLDOUT if passed else REJECTED_OUTER_HOLDOUT
    assert len(disposed) == 2
    assert study.user_attrs['outer_holdout_status'] == 'CONSUMED'
    assert study.user_attrs['final_test_evaluated'] is True
    assert study.user_attrs['search_outcome'] == status
    artifact = artifacts.load_run('search_test')
    assert artifact['selection_status'] == status
    assert artifact['best_params']  # diagnostic evidence survives rejection
    assert artifact['can_resume'] is False
    assert json.loads(progress_path.read_text())['outcome'] == status
    if passed:
        assert json.loads(output.read_text())['selection_status'] == status
    else:
        assert output.read_bytes() == original
        assert artifact['validation']['promotion_status'] == REJECTED_OUTER_HOLDOUT
    with pytest.raises(ValueError, match='already consumed'):
        run([.5], '--resume-run-id', study.study_name, run_id='forbidden_resume')


def test_legacy_zero_trade_result_is_rejected_on_read_and_execution(tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts, 'RUNS_DIR', tmp_path)
    record = {'run_id': 'old', 'kind': 'optimize', 'best_params': {'n_lags': 3},
              'validation': {'final_test': {'trades': 0, 'normal_ratio': 0, 'stressed_ratio': 0}},
              'oos_metrics': {'total_trades': 0, 'net_profit_usd': 0}}
    path = tmp_path / 'old.json'
    path.write_text(json.dumps(record))
    assert artifacts.load_run('old')['selection_status'] == REJECTED_OUTER_HOLDOUT
    assert artifacts.list_run_summaries()[0]['selection_status'] == REJECTED_OUTER_HOLDOUT
    with pytest.raises(HTTPException, match='failed its outer holdout'):
        selected_strategy(PaperJobRequest(source_run_id='old', asset_class='equity', tickers=['QQQ']))
    with pytest.raises(ValueError, match='failed their outer holdout'):
        load_params(str(path))
    # Annotation is derived; loading history never rewrites the original evidence.
    assert 'selection_status' not in json.loads(path.read_text())


def test_development_only_parameters_cannot_start_execution(tmp_path):
    path = tmp_path / 'development.json'
    path.write_text(json.dumps({'selection_status': 'DEVELOPMENT_SELECTED', 'params': {'n_lags': 3}}))
    with pytest.raises(ValueError, match='have not passed their outer holdout'):
        load_params(str(path))
