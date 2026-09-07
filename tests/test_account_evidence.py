from decimal import Decimal
from types import SimpleNamespace

import pytest

from quant.run.account_evidence import (
    SettledCashEvidence, register_account_source, settled_cash_from_cache,
    invalidate_account_sources,
)
from quant.run.nautilus_reconciliation import snapshot_from_nautilus_cache
from quant.strategies.execution_state import ExecutionLedger
from quant.api.broker_monitor import BrokerMonitor


def test_account_currency_and_field_freshness_are_independent():
    evidence = SettledCashEvidence()
    evidence.observe("DU1", "123.45", "USD", now=1000)
    evidence.observe("DU1", "999", "EUR", now=1200)
    evidence.observe("DU2", "999", "USD", now=1200)
    assert evidence.snapshot("DU1", "USD", connected=True, now=1240)["value"] == "123.45"
    assert evidence.snapshot("DU1", "USD", connected=True, now=1241)["status"] == "STALE"
    assert evidence.snapshot("DU3", "USD", connected=True, now=1200)["value"] is None
    assert evidence.snapshot("DU1", "USD", connected=False, now=1200)["value"] is None


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "", "N/A", "1.7976931348623157e308"])
def test_invalid_broker_amount_overwrites_previous_evidence(value):
    evidence = SettledCashEvidence()
    evidence.observe("DU1", "100", "USD", now=1000)
    evidence.observe("DU1", value, "USD", now=1001)
    reading = evidence.snapshot("DU1", "USD", connected=True, now=1002)
    assert reading["status"] == "INVALID"
    assert reading["value"] is None


@pytest.mark.parametrize("value", ["0", "-12.34", "123.450000000000001"])
def test_cash_preserves_zero_negative_and_decimal_precision(value):
    evidence = SettledCashEvidence()
    evidence.observe("DU1", value, "USD", now=1000)
    assert evidence.snapshot("DU1", "USD", connected=True, now=1000)["value"] == value
    assert evidence.snapshot("DU1", "USD", connected=True, now=999)["status"] == "INVALID"


class Client:
    pass


def test_reconciliation_consumes_only_current_matching_execution_evidence():
    money = SimpleNamespace(as_double=lambda: 10000)
    account = SimpleNamespace(id="IB-DU1", base_currency="USD", balance_total=lambda: money,
                              balance_free=lambda: money)
    cache = SimpleNamespace(accounts=lambda: [account], orders=lambda: [], positions_open=lambda: [])
    client = Client()
    client._cache = cache
    client.account_id = "IB-DU1"
    client._client = SimpleNamespace(is_ready=True, _eclient=SimpleNamespace(isConnected=lambda: True))
    register_account_source(client)
    client._quant_settled_cash.observe("IB-DU1", "123.45", "USD", now=1000)
    snapshot = snapshot_from_nautilus_cache(cache, ExecutionLedger(), strategy_id="ML",
                                           expected_account_id="IB-DU1", captured_at_ns=1001_000_000_000)
    assert snapshot.account.settled_cash == Decimal("123.45")
    assert snapshot.account.available_funds == Decimal("10000")
    assert snapshot.account.settled_cash_evidence["source"] == "IBKR accountSummary SettledCash"
    account.base_currency = None
    account.balance_total = lambda *, currency: money if str(currency) == "USD" else None
    account.balance_free = account.balance_total
    snapshot = snapshot_from_nautilus_cache(cache, ExecutionLedger(), strategy_id="ML",
                                           expected_account_id="IB-DU1", captured_at_ns=1001_000_000_000)
    assert snapshot.account.base_currency == "USD"
    assert snapshot.account.settled_cash == Decimal("123.45")
    assert settled_cash_from_cache(cache, "IB-DU2", now=1001)["value"] is None
    invalidate_account_sources(client._client)
    assert settled_cash_from_cache(cache, "IB-DU1", now=1001)["value"] is None
    client._quant_settled_cash.observe("IB-DU1", "55", "USD", now=1001)
    client._client.is_ready = False
    assert settled_cash_from_cache(cache, "IB-DU1", now=1002)["status"] == "DISCONNECTED"
    client._client.is_ready = True
    assert settled_cash_from_cache(cache, "IB-DU1", now=1003)["value"] is None
    register_account_source(client)
    assert settled_cash_from_cache(cache, "IB-DU1", now=1003)["value"] is None


def test_dashboard_cash_uses_selected_account_and_explicit_usd():
    monitor = BrokerMonitor()
    monitor._probe = SimpleNamespace(isConnected=lambda: True)
    monitor._state["status"] = "connected"
    monitor.set_accounts(["DU1", "DU2"])
    monitor._config["account_id"] = "DU2"
    monitor.set_account_value("DU1", "SettledCash", "1000", "USD")
    monitor.set_account_value("DU2", "SettledCash", "0", "USD")
    monitor.set_account_value("DU2", "SettledCash", "5000", "EUR")
    reading = monitor.status()["account"]
    assert reading["id"] == "DU2"
    assert reading["settled_cash"]["value"] == "0"
    monitor.set_disconnected("test")
    assert monitor.status()["account"]["settled_cash"]["status"] == "DISCONNECTED"
    monitor._state["status"] = "connected"
    assert monitor.status()["account"]["settled_cash"]["status"] == "UNAVAILABLE"


def test_installed_adapter_callback_capture_and_invalidation(monkeypatch):
    import asyncio
    from quant.data import ib_compat
    from nautilus_trader.adapters.interactive_brokers.execution import InteractiveBrokersExecutionClient as Exec
    from nautilus_trader.adapters.interactive_brokers.client.client import InteractiveBrokersClient as IB

    calls = []
    async def noop(self, **kwargs):
        calls.append(kwargs)
    monkeypatch.setattr(ib_compat, "_EXECUTION_FIX_REGISTERED", False)
    for cls, name in [(Exec, "_quant_captures_settled_cash"), (IB, "_quant_invalidates_cash")]:
        monkeypatch.setattr(cls, name, False, raising=False)
    monkeypatch.setattr(Exec, "_on_account_summary", lambda self, *args: calls.append(args))
    monkeypatch.setattr(Exec, "_disconnect", noop)
    monkeypatch.setattr(IB, "process_connection_closed", lambda self: None)
    monkeypatch.setattr(IB, "subscribe_account_summary", lambda self: None)
    monkeypatch.setattr(IB, "process_error", noop)
    ib_compat.register_ibkr_execution_fixes()
    client = Client()
    client._cache = object()
    client.account_id = "IB-DU1"
    client._client = SimpleNamespace(is_ready=True, _eclient=SimpleNamespace(isConnected=lambda: True))
    register_account_source(client)
    Exec._on_account_summary(client, "SettledCash", "42", "USD")
    assert settled_cash_from_cache(client._cache, "IB-DU1")["value"] == "42"
    assert calls[-1] == ("SettledCash", "42", "USD")
    IB.process_connection_closed(client._client)
    assert settled_cash_from_cache(client._cache, "IB-DU1")["value"] is None
    Exec._on_account_summary(client, "SettledCash", "43", "USD")
    IB.subscribe_account_summary(client._client)
    assert settled_cash_from_cache(client._cache, "IB-DU1")["value"] is None
    Exec._on_account_summary(client, "SettledCash", "44", "USD")
    asyncio.run(IB.process_error(client._client, error_code=1100))
    assert settled_cash_from_cache(client._cache, "IB-DU1")["value"] is None
    assert calls[-1] == {"error_code": 1100}
    Exec._on_account_summary(client, "SettledCash", "45", "USD")
    asyncio.run(Exec._disconnect(client))
    assert settled_cash_from_cache(client._cache, "IB-DU1")["value"] is None
