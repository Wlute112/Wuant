from types import SimpleNamespace

import pytest
from ibapi.contract import Contract

from quant.api.broker_monitor import BrokerMonitor


class FakeProbe:
    def __init__(self):
        self.connected = True
        self.contract_requests = []
        self.bar_requests = []
        self.cancelled = []

    def isConnected(self):  # noqa: N802 - mirrors IB API
        return self.connected

    def reqContractDetails(self, request_id, contract):  # noqa: N802
        self.contract_requests.append((request_id, contract))

    def reqHistoricalData(self, *args):  # noqa: N802
        self.bar_requests.append(args)

    def cancelHistoricalData(self, request_id):  # noqa: N802
        self.cancelled.append(request_id)


def _connected_monitor():
    monitor = BrokerMonitor()
    probe = FakeProbe()
    monitor._probe = probe
    monitor._state["status"] = "connected"
    return monitor, probe


def _qualify_latest(monitor, probe, *, symbol, security_type, exchange):
    request_id, requested = probe.contract_requests[-1]
    assert requested.symbol == symbol
    assert requested.secType == security_type
    assert requested.exchange == exchange
    qualified = Contract()
    qualified.symbol = symbol
    qualified.secType = security_type
    qualified.exchange = exchange
    qualified.currency = "USD"
    qualified.primaryExchange = "ARCA" if security_type == "STK" else ""
    monitor.add_contract_details(request_id, SimpleNamespace(contract=qualified))
    monitor.complete_contract_details(request_id)


def _bar(timestamp, *, close=101.0):
    return SimpleNamespace(
        date=timestamp,
        open=100.0,
        high=max(102.0, close),
        low=min(99.0, close),
        close=close,
        volume=1_250.0,
    )


def test_equity_subscription_backfills_and_keeps_forming_bar_current():
    monitor, probe = _connected_monitor()

    pending = monitor.subscribe_bars("spy", asset_class="equity", bar_hours=1)
    assert pending["status"] == "qualifying"
    _qualify_latest(
        monitor,
        probe,
        symbol="SPY",
        security_type="STK",
        exchange="SMART",
    )

    request = probe.bar_requests[-1]
    bar_request_id = request[0]
    assert request[2:] == ("", "30 D", "1 hour", "TRADES", 1, 2, True, [])

    monitor.receive_historical_bar(bar_request_id, _bar(1_787_900_400), forming=False)
    monitor.complete_historical_backfill(bar_request_id)
    monitor.receive_historical_bar(
        bar_request_id,
        _bar(1_787_900_400, close=101.75),
        forming=True,
    )
    current = monitor.bar_snapshot("SPY", asset_class="equity", bar_hours=1)
    assert current["status"] == "streaming"
    assert current["source"] == "ib_gateway"
    assert current["primary_exchange"] == "ARCA"
    assert current["session_scope"] == "rth"
    assert current["price_adjustment"] == "split_adjusted_dividend_unadjusted"
    assert current["bars"][-1]["close"] == 101.75
    assert current["bars"][-1]["complete"] is False

    monitor.receive_historical_bar(
        bar_request_id,
        _bar(1_787_904_000, close=102.25),
        forming=True,
    )
    advanced = monitor.bar_snapshot("SPY", asset_class="equity", bar_hours=1)
    assert advanced["bars"][-2]["complete"] is True
    assert advanced["bars"][-1]["complete"] is False


def test_crypto_subscription_uses_zero_hash_midpoint_and_calendar_hours():
    monitor, probe = _connected_monitor()

    monitor.subscribe_bars("btc", asset_class="crypto", bar_hours=24)
    _qualify_latest(
        monitor,
        probe,
        symbol="BTC",
        security_type="CRYPTO",
        exchange="ZEROHASH",
    )

    request = probe.bar_requests[-1]
    assert request[2:] == ("", "1 Y", "1 day", "MIDPOINT", 0, 2, True, [])


def test_daily_bar_preserves_exchange_session_date_without_local_timezone_shift():
    monitor, probe = _connected_monitor()
    monitor.subscribe_bars("QQQ", asset_class="equity", bar_hours=24)
    _qualify_latest(
        monitor,
        probe,
        symbol="QQQ",
        security_type="STK",
        exchange="SMART",
    )
    bar_request_id = probe.bar_requests[-1][0]

    monitor.receive_historical_bar(
        bar_request_id,
        _bar("20260821", close=713.44),
        forming=False,
    )

    point = monitor.bar_snapshot("QQQ", asset_class="equity", bar_hours=24)["bars"][0]
    assert point["session_date"] == "2026-08-21"
    assert point["ts"] == "2026-08-21T00:00:00+00:00"


@pytest.mark.parametrize("symbol", ["", "../SPY", "SPY USD", "A" * 16])
def test_bar_subscription_rejects_invalid_tickers(symbol):
    monitor, _ = _connected_monitor()
    with pytest.raises(ValueError, match="ticker"):
        monitor.subscribe_bars(symbol, asset_class="equity", bar_hours=1)


def test_crypto_rejects_equity_extended_hours_setting():
    monitor, _ = _connected_monitor()
    with pytest.raises(ValueError, match="extended-hours"):
        monitor.subscribe_bars(
            "BTC",
            asset_class="crypto",
            bar_hours=1,
            include_extended_hours=True,
        )
