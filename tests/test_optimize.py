import pandas as pd

from quant.optimize.optimize import _split_csv


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
