"""Numerical regressions for the high-dimensional optimizer feature space."""
import warnings

import numpy as np
import pytest
from sklearn.exceptions import ConvergenceWarning
from sklearn.linear_model import HuberRegressor

from quant.models.prediction_engine import PredictionConfig, PredictionEngine


def correlated_returns(seed=7):
    rng = np.random.default_rng(seed)
    peers = rng.normal(size=(140, 90))
    X = np.column_stack([
        peers, peers[:, :20] + rng.normal(scale=1e-5, size=(140, 20)),
    ])
    y = peers[:, 0] * 0.01 + rng.normal(scale=0.01, size=140)
    return X, y


def test_iteration_limited_fit_continues_to_a_converged_solution():
    X, y = correlated_returns()
    with pytest.warns(ConvergenceWarning):
        unfinished = HuberRegressor(alpha=0.0008, epsilon=1.05, max_iter=400).fit(X, y)
    reference = HuberRegressor(alpha=0.0008, epsilon=1.05, max_iter=5000).fit(X, y)
    engine = PredictionEngine(PredictionConfig(
        standardize=False, huber_alpha=0.0008, huber_epsilon=1.05,
    ))
    with warnings.catch_warnings():
        warnings.simplefilter("error", ConvergenceWarning)
        engine._fit_arrays(X, y)
    predicted = X @ engine._state.coef + engine._state.intercept
    assert np.max(np.abs(unfinished.predict(X) - predicted)) > 1e-4
    before_error = np.max(np.abs(unfinished.predict(X) - reference.predict(X)))
    after_error = np.max(np.abs(predicted - reference.predict(X)))
    assert after_error < before_error / 2


def test_converged_fit_is_unchanged_and_independent_of_previous_history():
    rng = np.random.default_rng(2)
    X = rng.normal(size=(100, 3))
    y = X[:, 0] * 0.02 + rng.normal(scale=0.01, size=100)
    engine = PredictionEngine(PredictionConfig())
    engine._fit_arrays(X, y)
    reference = HuberRegressor(max_iter=400).fit((X - X.mean(0)) / X.std(0), y)
    np.testing.assert_array_equal(engine._state.coef, reference.coef_)
    assert engine._state.intercept == reference.intercept_
    engine._fit_arrays(X, -y)
    engine._fit_arrays(X, y)
    np.testing.assert_array_equal(engine._state.coef, reference.coef_)


def test_exhausted_retry_still_warns():
    X, y = correlated_returns()
    engine = PredictionEngine(PredictionConfig(huber_max_iter=1))
    with pytest.warns(ConvergenceWarning):
        engine._fit_arrays(X, y)


def test_nonfinite_solution_does_not_replace_previous_fit(monkeypatch):
    X, y = correlated_returns()
    engine = PredictionEngine(PredictionConfig())
    previous = engine._state

    def invalid_fit(model, X, y):
        model.coef_ = np.full(X.shape[1], np.nan)
        model.intercept_ = 0.0
        return model

    monkeypatch.setattr(HuberRegressor, "fit", invalid_fit)
    with pytest.raises(ValueError, match="nonfinite"):
        engine._fit_arrays(X, y)
    assert engine._state is previous
