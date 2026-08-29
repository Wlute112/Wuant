import pandas as pd

from quant.run.backtest_common import (
    infer_bar_interval_minutes,
    infer_bar_type_suffix,
    infer_bars_per_session,
)


def test_intraday_csv_uses_matching_nautilus_bar_type(tmp_path):
    frame = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                [
                    "2026-01-05T14:30:00Z",
                    "2026-01-05T17:00:00Z",
                    "2026-01-06T14:30:00Z",
                    "2026-01-06T17:00:00Z",
                ],
                utc=True,
            ),
            "ticker": ["QQQ"] * 4,
        }
    )
    path = tmp_path / "bars.csv"
    frame.to_csv(path, index=False)

    assert infer_bar_type_suffix(frame) == "-3-HOUR-LAST-EXTERNAL"
    assert infer_bar_interval_minutes(frame) == 150
    assert infer_bars_per_session(str(path), ["QQQ"]) == 2


def test_daily_csv_keeps_daily_bar_type():
    frame = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                ["2026-01-05T00:00:00Z", "2026-01-06T00:00:00Z"],
                utc=True,
            ),
            "ticker": ["BTC", "BTC"],
        }
    )
    assert infer_bar_type_suffix(frame) == "-1-DAY-LAST-EXTERNAL"
