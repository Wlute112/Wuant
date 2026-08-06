import asyncio

import pandas as pd
import pytest

from quant.data.ibkr_fetch import _infer_bar_hours, _fetch_and_merge


def _bars(timestamps):
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "ticker": ["BTC"] * len(timestamps),
            "open": [1.0] * len(timestamps),
            "high": [1.0] * len(timestamps),
            "low": [1.0] * len(timestamps),
            "close": [1.0] * len(timestamps),
            "volume": [1.0] * len(timestamps),
        }
    )


def test_infer_bar_hours_accepts_mixed_timestamp_formats():
    assert _infer_bar_hours(
        _bars(["2024-01-01 00:00:00", "2024-01-01 04:00:00"])
    ) == 4


def test_missing_merge_rejects_frequency_change(tmp_path):
    csv_path = tmp_path / "bars.csv"
    _bars(["2024-01-01 00:00:00", "2024-01-01 04:00:00"]).to_csv(
        csv_path, index=False
    )

    with pytest.raises(ValueError, match="Use replace_bars"):
        asyncio.run(_fetch_and_merge(
            str(csv_path), ["ETH"], 1, "127.0.0.1", 7497, 1, "ZEROHASH",
            "MID", 1, "REALTIME", 1,
        ))
