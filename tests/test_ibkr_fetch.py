import asyncio
from datetime import datetime, timezone
from functools import partial
from types import SimpleNamespace

import pandas as pd
import pytest

from quant.data.ibkr_fetch import _infer_bar_hours, _fetch_and_merge


@pytest.mark.parametrize("code,historical,ends_request", [
    (2188, True, False),
    (2188, False, True),
    (162, True, True),
    (200, True, True),
    (354, True, True),
])
def test_historical_warning_keeps_request_alive_without_masking_errors(monkeypatch, code, historical, ends_request):
    from quant.data.ib_compat import register_ibkr_historical_fixes
    from nautilus_trader.adapters.interactive_brokers.client.client import InteractiveBrokersClient as IB

    def reqHistoricalData():
        pass
    def reqContractDetails():
        pass
    ended, messages = [], []
    request = SimpleNamespace(handle=partial(reqHistoricalData if historical else reqContractDetails))
    client = SimpleNamespace(
        _requests=SimpleNamespace(get=lambda **kwargs: request),
        _log=SimpleNamespace(warning=messages.append, debug=messages.append),
        _end_request=lambda req_id, success: ended.append((req_id, success)),
    )
    # Keep registration isolated from other adapter tests, including repeat calls.
    monkeypatch.setattr(IB, "_handle_request_error", IB._handle_request_error)
    monkeypatch.setattr(IB, "_quant_preserves_historical_2188", False, raising=False)
    register_ibkr_historical_fixes()
    registered = IB._handle_request_error
    register_ibkr_historical_fixes()
    assert IB._handle_request_error is registered
    asyncio.run(IB._handle_request_error(client, 42, code, "broker diagnostic"))
    assert ended == ([(42, False)] if ends_request else [])
    assert messages


@pytest.mark.parametrize("asset_class,price_type,source,price_basis,volume_basis", [
    ("equity", "LAST", "IBKR_TRADES", "split_adjusted", "traded_shares"),
    ("crypto", "LAST", "IBKR_LAST", "trade_prices", "traded_units"),
    ("crypto", "MID", "IBKR_MID", "midpoint", "unavailable_midpoint"),
])
def test_fetch_uses_utc_wall_clock_and_preserves_trade_bar_metadata(monkeypatch, asset_class, price_type, source, price_basis, volume_basis):
    from quant.data import ibkr_fetch
    from nautilus_trader.adapters.interactive_brokers.historical import client

    observed = {}
    bar = SimpleNamespace(ts_event=pd.Timestamp("2026-01-02", tz="UTC").value,
                          open=100, high=101, low=99, close=100.5, volume=500)
    class HistoricClient:
        def __init__(self, **kwargs):
            pass
        async def connect(self):
            pass
        async def request_instruments(self, **kwargs):
            return [SimpleNamespace(id="AAPL.NASDAQ")]
        async def request_bars(self, **kwargs):
            observed.update(kwargs)
            return [bar]
    monkeypatch.setattr(client, "HistoricInteractiveBrokersClient", HistoricClient)
    monkeypatch.setattr(ibkr_fetch, "register_ibkr_historical_fixes", lambda: None)
    monkeypatch.setattr(ibkr_fetch, "register_zerohash_crypto", lambda *args: None)
    before = datetime.now(timezone.utc).replace(tzinfo=None)
    result = asyncio.run(ibkr_fetch._fetch(
        ["AAPL"], 1, "127.0.0.1", 4002, 23, "SMART", price_type, 24,
        "REALTIME", 15, asset_class=asset_class,
    ))
    after = datetime.now(timezone.utc).replace(tzinfo=None)
    assert observed["tz_name"] == "UTC"
    assert before <= observed["end_date_time"] <= after
    assert observed["end_date_time"].tzinfo is None
    assert (observed["end_date_time"] - observed["start_date_time"]).days == 366
    assert observed["use_rth"] is (asset_class == "equity")
    assert observed["instrument_ids"] == ["AAPL.NASDAQ"]
    assert "contracts" not in observed
    assert result.iloc[0]["source"] == source
    assert result.iloc[0]["price_basis"] == price_basis
    assert result.iloc[0]["volume_basis"] == volume_basis
    assert result.iloc[0]["volume"] == 500


@pytest.mark.parametrize("symbol,expected", [
    ("AAPL", "AAPL"), ("BRK-B", "BRK B"), ("BRK.B", "BRK B"),
    ("BRK B", "BRK B"), ("BF-B", "BF B"),
])
def test_share_class_symbols_match_ibkr_contract_notation(symbol, expected):
    from quant.data.ibkr_fetch import _equity_ib_symbol
    assert _equity_ib_symbol(symbol) == expected


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
