import numpy as np
import pandas as pd

from quant.run import research


def _equity_points(values, start="2024-01-02"):
    dates = pd.bdate_range(start, periods=len(values), tz="UTC")
    return [
        {"ts": timestamp.isoformat(), "equity": float(value)}
        for timestamp, value in zip(dates, values)
    ]


def test_analyze_run_builds_same_period_benchmark_and_relative_metrics(monkeypatch):
    dates = pd.bdate_range("2024-01-02", periods=280, tz="UTC")
    benchmark = pd.Series(
        100 * np.cumprod(np.full(len(dates), 1.00025)),
        index=dates,
        name="SP500",
    )
    monkeypatch.setattr(research, "_fred_sp500", lambda: benchmark)
    values = 5_000 * np.cumprod(1 + 0.0004 + 0.001 * np.sin(np.arange(280) / 9))

    result = research.analyze_run({
        "run_id": "backtest_test",
        "kind": "backtest",
        "asset_class": "equity",
        "equity_curve": _equity_points(values),
        "positions": {"rows": []},
    })

    assert result["status"] == "ready"
    assert result["benchmark"]["status"] == "ready"
    assert result["benchmark"]["return_basis"] == "price return; dividends excluded"
    assert result["strategy"]["observations"] == 279
    assert result["relative"]["information_ratio"] is not None
    assert result["relative"]["probabilistic_sharpe_vs_benchmark_pct"] is not None
    assert len(result["series"]["strategy_index"]) == 280
    assert result["monthly_returns"]


def test_analyze_optimize_run_exposes_trials_sensitivity_and_cost_folds(monkeypatch):
    dates = pd.bdate_range("2024-01-02", periods=90, tz="UTC")
    monkeypatch.setattr(
        research,
        "_fred_sp500",
        lambda: pd.Series(np.linspace(100, 106, len(dates)), index=dates, name="SP500"),
    )
    trials = [
        {
            "number": number,
            "state": "COMPLETE",
            "value": value,
            "params": {"entry_threshold": threshold, "use_limit_orders": number % 2 == 0},
            "user_attrs": {
                "walk_forward_folds": [
                    {"fold": 0, "normal_ratio": value, "stressed_ratio": value - 0.1},
                    {"fold": 1, "normal_ratio": value + 0.05, "stressed_ratio": value - 0.05},
                ]
            },
        }
        for number, (value, threshold) in enumerate(((0.2, 0.003), (0.7, 0.002), (1.1, 0.001)))
    ]
    values = np.linspace(5_000, 5_450, len(dates))

    result = research.analyze_run({
        "run_id": "optimize_test",
        "kind": "optimize",
        "asset_class": "equity",
        "oos_equity_curve": _equity_points(values),
        "trials": trials,
        "best_params": trials[-1]["params"],
        "in_sample_value": 1.1,
        "oos_score": 0.8,
    })

    optimization = result["optimization"]
    assert optimization["best_trial_number"] == 2
    assert optimization["history"][-1]["incumbent"] == 1.1
    assert optimization["parameter_importance"][0]["parameter"] == "entry_threshold"
    assert optimization["folds"][0]["stressed_ratio"] == 1.0
    assert optimization["generalization_gap"] == 0.3
