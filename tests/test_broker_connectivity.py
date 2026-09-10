import asyncio
from types import SimpleNamespace

import pytest

from quant.run import broker_connectivity as connectivity
from quant.run.account_evidence import register_account_source
from quant.strategies.execution_state import ExecutionSafetyController, ExecutionSafetyState
from quant.strategies.ml_strategy import MLStrategy


class Client:
    pass


def source():
    client = Client()
    client._cache = object()
    client.account_id = "IB-DU1"
    client._client = SimpleNamespace(is_ready=True, _eclient=SimpleNamespace(isConnected=lambda: True))
    register_account_source(client)
    return client


def test_startup_disconnect_and_reconnect_require_matching_generation_reconciliation():
    client = source()
    state = connectivity.snapshot(client._cache, client.account_id)
    assert state["status"] == "RECONCILIATION_REQUIRED"
    assert connectivity.acknowledge_reconciliation(client._cache, client.account_id, state["generation"])
    assert connectivity.snapshot(client._cache, client.account_id)["healthy"]
    connectivity.invalidate_ib_client(client._client, "IBKR_ERROR_1100")
    assert not connectivity.snapshot(client._cache, client.account_id)["healthy"]
    assert not connectivity.acknowledge_reconciliation(client._cache, client.account_id, state["generation"])
    current = connectivity.snapshot(client._cache, client.account_id)
    assert current["status"] == "RECONCILIATION_REQUIRED"  # socket can already be reconnected
    assert connectivity.acknowledge_reconciliation(client._cache, client.account_id, current["generation"])


def test_socket_poll_fallback_invalidates_generation_and_other_nodes_are_isolated():
    client, other = source(), source()
    for item in (client, other):
        assert connectivity.acknowledge_reconciliation(item._cache, item.account_id, 0)
    client._client.is_ready = False
    assert connectivity.snapshot(client._cache, client.account_id)["status"] == "DISCONNECTED"
    client._client.is_ready = True
    assert not connectivity.snapshot(client._cache, client.account_id)["healthy"]
    assert connectivity.snapshot(other._cache, other.account_id)["healthy"]
    assert not connectivity.snapshot(client._cache, "IB-DU2")["healthy"]


def test_disconnect_callback_immediately_freezes_clears_signals_and_audits():
    client = source()
    class Listener:
        def disconnected(self, event):
            MLStrategy._on_broker_disconnect(self, event)
    strategy = Listener()
    strategy._pending = {"AAA": "old signal"}
    strategy._execution_safety = ExecutionSafetyController()
    strategy.clock = SimpleNamespace(timestamp_ns=lambda: 100)
    events, cancelled = [], []
    strategy._audit_event = lambda *args, **kwargs: events.append((args, kwargs))
    strategy._cancel_working_entry_orders = lambda **kwargs: cancelled.append(kwargs)
    strategy._refresh_telemetry_state = lambda: None
    unsubscribe = connectivity.watch(client._cache, client.account_id, strategy.disconnected)
    connectivity.invalidate_ib_client(client._client, "IBKR_CONNECTION_CLOSED")
    assert strategy._execution_safety.state == ExecutionSafetyState.UNCERTAIN
    assert strategy._reconciliation_state == "UNCERTAIN"
    assert strategy._pending == {}
    assert cancelled
    assert events[0][0][0] == "BROKER_CONNECTIVITY_LOST"
    assert events[0][1]["severity"] == "CRITICAL"
    unsubscribe()
    connectivity.invalidate_ib_client(client._client, "repeat")
    assert len(events) == 1


def test_failing_listener_cannot_interrupt_adapter_cleanup_or_rearm(caplog):
    client = source()
    class Listener:
        def disconnected(self, event):
            raise RuntimeError("cancel rejected")
    listener = Listener()
    unsubscribe = connectivity.watch(client._cache, client.account_id, listener.disconnected)
    connectivity.invalidate_ib_client(client._client, "disconnect")
    assert not connectivity.snapshot(client._cache, client.account_id)["healthy"]
    assert "cancel rejected" in caplog.text
    unsubscribe()


@pytest.mark.parametrize("code", [1100, 1101, 1102, 1300])
def test_supported_adapter_error_callback_invalidates_before_delegating(monkeypatch, code):
    from quant.data import ib_compat
    from nautilus_trader.adapters.interactive_brokers.client.client import InteractiveBrokersClient as IB
    calls = []
    client = source()
    connectivity.acknowledge_reconciliation(client._cache, client.account_id, 0)
    async def original(self, **kwargs):
        calls.append(connectivity.snapshot(client._cache, client.account_id)["healthy"])
    monkeypatch.setattr(ib_compat, "_EXECUTION_FIX_REGISTERED", False)
    monkeypatch.setattr(IB, "_quant_invalidates_cash", False, raising=False)
    monkeypatch.setattr(IB, "process_error", original)
    ib_compat.register_ibkr_execution_fixes()
    asyncio.run(IB.process_error(client._client, error_code=code))
    assert calls == [False]
    assert connectivity.snapshot(client._cache, client.account_id)["event"] == f"IBKR_ERROR_{code}"


def test_no_adapter_cannot_pass_reconciliation_acknowledgement():
    assert not connectivity.acknowledge_reconciliation(object(), "IB-DU1", 0)
