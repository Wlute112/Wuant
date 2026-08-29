import pandas as pd
import pytest

from quant.models.prediction_engine import PredictionConfig, PredictionEngine
from quant.optimize.optimize import (
    NO_QUALIFYING_FOLDS_SCORE,
    _prepare_nested_walk_forward,
    _split_csv,
    stability_aware_score,
)


def test_split_csv_accepts_mixed_date_and_datetime_timestamps(tmp_path):
    source = tmp_path / "bars.csv"
    pd.DataFrame(
        {
            "timestamp": ["2024-01-01", "2024-01-01 04:00:00"],
            "ticker": ["BTC", "BTC"],
            "open": [1.0, 1.0],
            "high": [1.0, 1.0],
            "low": [1.0, 1.0],
            "close": [1.0, 1.0],
            "volume": [1.0, 1.0],
        }
    ).to_csv(source, index=False)

    is_path, oos_path = _split_csv(str(source), 0.5)

    assert len(pd.read_csv(is_path)) == 1
    assert len(pd.read_csv(oos_path)) == 1


def _bars_frame(n=100, tickers=("BTC", "ETH")):
    rows = []
    for i, ts in enumerate(pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")):
        for offset, ticker in enumerate(tickers):
            price = 100.0 + i + offset
            rows.append(
                {
                    "timestamp": ts,
                    "ticker": ticker,
                    "open": price,
                    "high": price + 1,
                    "low": price - 1,
                    "close": price,
                    "volume": 10.0,
                }
            )
    return pd.DataFrame(rows)


def test_nested_walk_forward_reserves_outer_test_and_purges_each_fold(tmp_path):
    source = tmp_path / "bars.csv"
    _bars_frame().to_csv(source, index=False)

    prepared = _prepare_nested_walk_forward(
        str(source),
        ["BTC", "ETH"],
        str(tmp_path / "splits"),
        final_test_frac=0.2,
        n_folds=5,
        min_initial_train_bars=10,
        max_embargo_bars=5,
    )

    assert len(prepared.folds) == 5
    assert len(pd.read_csv(prepared.final_test_path)["timestamp"].unique()) == 20
    assert prepared.final_test_start_index == 80
    for fold in prepared.folds:
        fit_end = fold.model_fit_end_ns(3)
        fit_index = fold.timestamps_ns.index(fit_end)
        assert fold.validation_start_index - fit_index - 1 == 3
        fold_frame = pd.read_csv(fold.csv_path)
        assert pd.to_datetime(fold_frame["timestamp"], utc=True).max().value == fold.validation_end_ns


def test_stability_objective_penalizes_dispersion_turnover_and_cost_sensitivity():
    score = stability_aware_score(
        [1.0, 2.0, 3.0],
        [0.8, 1.4, 2.2],
        [1.0, 2.0, 3.0],
        std_weight=0.5,
        turnover_weight=0.1,
        cost_sensitivity_weight=0.5,
        min_positive_fraction=2 / 3,
    )
    # median 2 - .5*std([1,2,3]) - .1*median(turnover)
    # - .5*median([.2,.6,.8])
    assert score == pytest.approx(2 - 0.5 * (2 / 3) ** 0.5 - 0.2 - 0.3)


def test_stability_objective_requires_positive_performance_in_most_folds():
    assert stability_aware_score(
        [1.0, -0.1, -0.2, -0.3, -0.4],
        [0.9, -0.2, -0.3, -0.4, -0.5],
        [1, 1, 1, 1, 1],
        min_positive_fraction=0.6,
    ) == NO_QUALIFYING_FOLDS_SCORE


def test_prediction_engine_training_window_keeps_only_recent_rows():
    engine = PredictionEngine(
        PredictionConfig(training_window_bars=3, min_train_bars=3)
    )
    X = pd.DataFrame({"x": range(8)}).to_numpy()
    y = pd.Series(range(8), dtype=float).to_numpy()

    train_X, train_y = engine._training_tail(X, y)

    assert train_X[:, 0].tolist() == [5, 6, 7]
    assert train_y.tolist() == [5.0, 6.0, 7.0]


def test_prediction_engine_rejects_undersized_rolling_window():
    with pytest.raises(ValueError, match="at least min_train_bars"):
        PredictionEngine(
            PredictionConfig(training_window_bars=19, min_train_bars=20)
        )
