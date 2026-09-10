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


def test_infer_bar_hours_rejects_non_whole_hour_csv():
    with pytest.raises(ValueError, match="2.5-hour bars"):
        _infer_bar_hours(
            _bars(["2024-01-01 13:30:00", "2024-01-01 16:00:00"])
        )


def test_declared_source_width_preserves_short_rth_tail():
    frame = _bars(["2025-01-02 14:30:00", "2025-01-02 17:00:00"])
    frame["requested_bar_hours"] = 4
    assert _infer_bar_hours(frame) == 4
    frame.loc[1, "requested_bar_hours"] = 2
    with pytest.raises(ValueError, match="Conflicting"):
        _infer_bar_hours(frame)
