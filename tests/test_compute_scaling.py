from dataclasses import fields
import multiprocessing
import os
import time

import numpy as np
import optuna
import pytest
from quant.run import compute

from quant.data.generate_sample_bars import generate
from quant.models.prediction_engine import PredictionConfig, make_features_targets
from quant.models.regime import RegimeConfig, RegimeFeatureEngine
from quant.optimize.optimize import _prepare_nested_walk_forward, make_objective
from quant.run.compute import ComputePool, THREAD_ENV, resolve_compute_plan


M4 = {"chip": "M4", "logical_cpus": 10, "compute_cores": 4, "memory_gb": 32}
ULTRA = {"chip": "M5 Ultra", "logical_cpus": 30, "compute_cores": 30, "memory_gb": 96}


def test_hardware_plan_reserves_services_and_caps_to_work_and_memory():
    assert resolve_compute_plan(tasks=10, host=M4).workers == 3
    plan = resolve_compute_plan(tasks=10, host=ULTRA)
    assert plan.workers == 10
    assert plan.memory_budget_gb == 72
    assert resolve_compute_plan(tasks=20, host=ULTRA).workers == 18
    assert resolve_compute_plan(100, 16, 4, tasks=20, host=ULTRA).workers == 4
    assert resolve_compute_plan(1, tasks=10, host=ULTRA).workers == 1
    assert resolve_compute_plan(tasks=10, host={**M4, "memory_gb": 0}).workers == 1


@pytest.mark.parametrize("kwargs", [
    {"workers": -1}, {"memory_budget_gb": -1}, {"memory_budget_gb": 80},
    {"worker_memory_gb": 0}, {"worker_memory_gb": float("nan")},
    {"memory_budget_gb": float("inf")},
    {"memory_budget_gb": 2, "worker_memory_gb": 4},
])
def test_invalid_resource_budgets_fail_before_spawning(kwargs):
    with pytest.raises(ValueError):
        resolve_compute_plan(tasks=10, host=ULTRA, **kwargs)


def test_mac_discovery_counts_super_and_performance_tiers(monkeypatch):
    values = {"machdep.cpu.brand_string": "Apple M5 Ultra", "hw.memsize": str(96 * 1024 ** 3),
              "hw.nperflevels": "2", "hw.perflevel0.name": "Super",
              "hw.perflevel0.physicalcpu": "10", "hw.perflevel1.name": "Performance",
              "hw.perflevel1.physicalcpu": "20"}
    monkeypatch.setattr(compute.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(compute.os, "cpu_count", lambda: 30)
    monkeypatch.setattr(compute, "_sysctl", lambda key: values.get(key, ""))
    host = compute.host_resources()
    assert host["compute_cores"] == 30
    assert host["memory_gb"] == 96
    assert resolve_compute_plan(tasks=10, host=host).workers == 10


@pytest.mark.parametrize("lags", [0, 1, 5, 15])
@pytest.mark.parametrize("horizon", [1, 2, 5])
@pytest.mark.parametrize("length", [0, 3, 100])
def test_vectorized_training_rows_match_original_loop_exactly(lags, horizon, length):
    rng = np.random.default_rng(42)
    close = np.exp(rng.normal(0, 0.03, length).cumsum())
    blocks = [rng.normal(size=(length, width)) for width in (2, 6, 5, 1)]
    cfg = PredictionConfig(n_lags=lags, horizon=horizon)
    actual_X, actual_y, actual_idx = make_features_targets(close, cfg, *blocks)
    r = np.zeros_like(close)
    r[1:] = np.diff(np.log(close))
    idx = np.arange(lags, max(lags, length - horizon))
    expected_X = np.asarray([
        np.concatenate([r[i - lags:i], *(block[i] for block in blocks)]) for i in idx
    ]).reshape(len(idx), lags + 14)
    expected_y = np.asarray([r[i + 1:i + 1 + horizon].sum() for i in idx])
    np.testing.assert_array_equal(actual_X, expected_X)
    np.testing.assert_array_equal(actual_y, expected_y)
    np.testing.assert_array_equal(actual_idx, idx)


def test_raw_only_training_keeps_zero_column_shape():
    X, y, idx = make_features_targets(np.arange(1, 20.0), PredictionConfig(n_lags=0))
    assert X.shape == (18, 0)
    assert y.shape == idx.shape == (18,)


def test_regime_cache_skips_unchanged_work_and_detects_caller_edits(monkeypatch):
    config = RegimeConfig(hmm_min_samples=10, hmm_n_iter=5, hmm_fit_retries=0)
    close = np.exp(np.random.default_rng(42).normal(0, 0.02, 40).cumsum())
    engine = RegimeFeatureEngine(config)
    original = engine.compute(close)
    with monkeypatch.context() as patch:
        def unexpected(*args):
            raise AssertionError("unchanged history should reuse the cached arrays")
        patch.setattr(engine, "_extend_states", unexpected)
        patch.setattr(engine, "_extend_hmm", unexpected)
        repeated = engine.compute(close.copy())
    for field in fields(original):
        np.testing.assert_array_equal(getattr(original, field.name), getattr(repeated, field.name))
    close[-10:] *= 1.2
    recomputed = engine.compute(close)
    fresh = RegimeFeatureEngine(config).compute(close)
    for field in fields(fresh):
        np.testing.assert_array_equal(getattr(recomputed, field.name), getattr(fresh, field.name))


def _worker_environment(value):
    return value, os.getpid(), {key: os.environ.get(key) for key in THREAD_ENV}


def test_spawn_pool_orders_results_limits_threads_and_reaps_children():
    environment = {key: os.environ.get(key) for key in THREAD_ENV}
    prior_children = {child.pid for child in multiprocessing.active_children()}
    with ComputePool(resolve_compute_plan(2, tasks=4, host=M4)) as pool:
        with pool.results(_worker_environment, range(4)) as results:
            measured = list(results)
        assert [row[0] for row in measured] == list(range(4))
        assert all(row[1] != os.getpid() for row in measured)
        assert all(set(row[2].values()) == {"1"} for row in measured)
        with pytest.raises(ValueError):
            with pool.results(int, ["bad"]) as results:
                list(results)
        with pool.results(int, ["4"]) as results:
            assert list(results) == [4]
    assert {key: os.environ.get(key) for key in THREAD_ENV} == environment
    assert {child.pid for child in multiprocessing.active_children()} == prior_children


def test_pool_cancellation_stops_its_workers_promptly():
    prior_children = {child.pid for child in multiprocessing.active_children()}
    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        with ComputePool(resolve_compute_plan(2, tasks=4, host=M4)) as pool:
            with pool.results(time.sleep, [30, 30, 30, 30]):
                raise KeyboardInterrupt
    assert time.monotonic() - started < 10
    assert {child.pid for child in multiprocessing.active_children()} == prior_children


@pytest.mark.parametrize("asset_class,tickers", [
    ("crypto", ["BTC", "ETH"]), ("equity", ["SPY", "QQQ"]),
])
def test_real_optuna_serial_parallel_preserve_suggestions_pruning_and_metrics(tmp_path, asset_class, tickers):
    source = tmp_path / "bars.csv"
    generate(tickers, n_days=130, seed=42, asset_class=asset_class).to_csv(source, index=False)
    nested = _prepare_nested_walk_forward(
        str(source), tickers, str(tmp_path / "folds"),
        final_test_frac=0.2, n_folds=3, min_initial_train_bars=30, max_embargo_bars=5,
    )
    studies = []
    for workers in (1, 2):
        with ComputePool(resolve_compute_plan(workers, tasks=6, host=M4)) as pool:
            study = optuna.create_study(
                direction="maximize",
                sampler=optuna.samplers.TPESampler(seed=42, n_startup_trials=2),
                pruner=optuna.pruners.MedianPruner(n_startup_trials=2, n_warmup_steps=0),
            )
            study.optimize(make_objective(
                nested.folds, tickers, 5000, warmup_bars=30, min_train_bars=20,
                asset_class=asset_class,
                structural_overrides={"use_hmm_feature": True}, pool=pool,
            ), n_trials=6)
            studies.append(study)
    assert any(trial.state == optuna.trial.TrialState.PRUNED for trial in studies[0].trials)
    for serial, parallel in zip(studies[0].trials, studies[1].trials):
        assert serial.params == parallel.params
        assert serial.state == parallel.state
        assert serial.value == parallel.value
        assert serial.intermediate_values == parallel.intermediate_values
        assert serial.user_attrs == parallel.user_attrs
