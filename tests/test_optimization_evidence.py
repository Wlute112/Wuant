import json
from types import SimpleNamespace
import optuna
import pandas as pd
import pytest
from quant.optimize import optimize, evidence
from quant.api import jobs_routes


def test_capture_bounds_sampling_and_order_times(monkeypatch):
    points = [{'ts': pd.Timestamp(i, unit='s', tz='UTC').isoformat(), 'equity': 100 + i} for i in range(1000)]
    monkeypatch.setattr(evidence, 'equity_curve_with_timestamps', lambda *a: points)
    monkeypatch.setattr(evidence, '_fills_report', lambda *a: pd.DataFrame({'ts_last': pd.to_datetime(range(1000), unit='s', utc=True), 'side': 'BUY'}))
    result = evidence.capture(None, 10 * 10**9, 900 * 10**9)
    assert len(result['equity']) == 500 and result['equity_total'] == 891
    assert result['equity'][0] == points[10] and result['equity'][-1] == points[900]
    assert result['fills']['total'] == 891
    assert len(result['fills']['rows']) == 200 and result['fills']['truncated']


@pytest.mark.parametrize('capture_failure', [False, True])
def test_worker_evidence_does_not_change_metrics_or_inputs(monkeypatch, capture_failure):
    disposed = []
    engine = SimpleNamespace(dispose=lambda: disposed.append(True))
    monkeypatch.setattr(optimize, 'build_and_run', lambda **kw: engine)
    result = optimize.FoldPerformance(1.2, .3, 8, 123)
    monkeypatch.setattr(optimize, '_engine_performance', lambda *a: result)
    def capture(*a):
        if capture_failure:
            raise RuntimeError('capture failed')
        return {'equity': []}
    monkeypatch.setattr(evidence, 'capture', capture)
    task = {'starting_cash': 1000, 'asset_class': 'crypto'}
    assert optimize.evaluate_backtest(task) == optimize.evaluate_backtest(task | {'evidence_bounds': (1, 2)})
    assert 'evidence_bounds' not in task and len(disposed) == 2


@pytest.mark.parametrize("prune", [False, True])
def test_objective_same_suggestions_scores_and_pruning_with_capture(monkeypatch, tmp_path, prune):
    monkeypatch.setattr(optimize, 'suggest_parameters', lambda trial, *a: {'horizon': trial.suggest_int('horizon', 1, 3)})
    folds = tuple(optimize.WalkForwardFold(i, 'unused', 10, 20, 9, tuple(range(1, 21))) for i in range(1, 4))
    def evaluate(folds, *a, **kw):
        for fold in folds:
            yield fold, optimize.FoldPerformance(1 + fold.number / 10, .1, 2), optimize.FoldPerformance(.8, .1, 2)
    monkeypatch.setattr(optimize, 'evaluate_folds', evaluate)
    def run(writer):
        study = optuna.create_study(direction='maximize', sampler=optuna.samplers.RandomSampler(seed=42), pruner=optuna.pruners.ThresholdPruner(upper=0) if prune else optuna.pruners.NopPruner())
        study.optimize(optimize.make_objective(folds, ['BTC'], 1000, evidence_writer=writer), n_trials=4)
        return [(t.params, t.value, t.state, t.intermediate_values) for t in study.trials]
    assert run(None) == run(evidence.EvidenceWriter(tmp_path / 'progress.json'))
    saved = json.loads((tmp_path / 'progress.evidence/1.json').read_text())
    assert len(saved['folds']) == (1 if prune else 3)
    assert saved['state'] == ('pruned' if prune else 'complete')
    assert saved['score_components']['turnover_penalty'] == pytest.approx(.001)


def test_evidence_api_isolated_and_survives_interruption(monkeypatch, tmp_path):
    monkeypatch.setattr(jobs_routes, 'JOBS_DIR', tmp_path)
    monkeypatch.setattr(jobs_routes, 'manager', SimpleNamespace(get=lambda job: {'status': 'cancelled'} if job == 'optimize_test' else None))
    writer = evidence.EvidenceWriter(tmp_path / 'optimize_test_progress.json')
    writer.write(1, {'trial': 1, 'state': 'running', 'params': {}, 'folds': []})
    assert jobs_routes.get_optimization_evidence('optimize_test', None)['trials'][0]['state'] == 'interrupted'
    assert jobs_routes.get_optimization_evidence('optimize_test', 1)['state'] == 'interrupted'
    with pytest.raises(Exception) as error:
        jobs_routes.get_optimization_evidence('../optimize_test', 1)
    assert error.value.status_code == 404
    with pytest.raises(Exception):
        jobs_routes.get_optimization_evidence('optimize_test', 2)


def test_writer_failure_is_observational(tmp_path):
    path = tmp_path / 'blocked'
    path.write_text('file')
    assert not evidence.EvidenceWriter(path / 'progress.json').write(1, {'folds': []})


def test_real_backtest_capture_parity(tmp_path):
    import numpy as np
    dates = pd.date_range('2024-01-01', periods=200, tz='UTC')
    prices = 100 + np.sin(np.arange(200) / 3) * 5 + np.arange(200) * .03
    source = tmp_path / 'bars.csv'
    pd.DataFrame(dict(timestamp=dates, ticker='BTC', open=prices, high=prices+2,
                      low=prices-2, close=prices, volume=10000)).to_csv(source, index=False)
    task = dict(csv_path=str(source), tickers=['BTC'], starting_cash=5000,
                asset_class='crypto', log_level='ERROR', bypass_logging=True,
                fill_model_seed=1729, strategy_overrides=dict(
                    use_regime_features=False, use_hmm_feature=False,
                    use_news_features=False, use_industry_features=False,
                    warmup_bars=40, min_train_bars=30, refit_every_n_bars=12,
                    entry_threshold=.001, backtest_model_fit_end_ns=dates[149].value,
                    backtest_trade_start_ns=dates[153].value))
    baseline = optimize.evaluate_backtest(task)
    captured = optimize.evaluate_backtest(task | {'evidence_bounds': (dates[153].value, dates[-1].value)})
    assert baseline == captured and captured.trades > 0
    assert captured.evidence['equity'] and captured.evidence['fills']['total'] > 0
    assert len(json.dumps(captured.evidence)) < 20000


@pytest.mark.parametrize('status', ['ready', 'partial', 'unavailable'])
def test_benchmark_coverage_is_explicit(monkeypatch, tmp_path, status):
    from quant.run import research
    monkeypatch.setattr(jobs_routes, 'JOBS_DIR', tmp_path)
    monkeypatch.setattr(jobs_routes, 'manager', SimpleNamespace(get=lambda job: {'status': 'running'}))
    series = pd.Series([100., 101.], index=pd.date_range('2024-01-01', periods=2, tz='UTC'))
    monkeypatch.setattr(research, '_benchmark_values', lambda *a: (series, {'status': status}))
    writer = evidence.EvidenceWriter(tmp_path / 'test_progress.json')
    writer.write(1, {'trial': 1, 'state': 'complete', 'folds': [
        {'validation_start': '2024-01-01', 'validation_end': '2024-01-02'}]})
    result = jobs_routes.get_optimization_evidence('test', 1)['folds'][0]['benchmark']
    assert result['status'] == status
    assert len(result['points']) == (2 if status == 'ready' else 0)
