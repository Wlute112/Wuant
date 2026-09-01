from decimal import Decimal
import sqlite3
from types import SimpleNamespace

import pytest

from quant.ops.state import OperationsStore
from quant.run.reconciliation import (
    BrokerAccount,
    BrokerExecution,
    BrokerOrder,
    BrokerPosition,
    BrokerSnapshot,
    ReconciliationConfig,
    reconcile,
    recover_ledger,
)
from quant.run.nautilus_reconciliation import snapshot_from_nautilus_cache
from quant.strategies.execution_state import (
    ExecutionLedger,
    LifecycleStatus,
    OrderRole,
)


def _account() -> BrokerAccount:
    return BrokerAccount.normalized(
        account_id="DU123",
        base_currency="USD",
        equity="10000",
        available_funds="9000",
        buying_power="9000",
        settled_cash="9000",
        snapshot_complete=True,
    )


def _ledger(*, filled: bool) -> ExecutionLedger:
    ledger = ExecutionLedger()
    ledger.register_order(
        client_order_id="O-1",
        instrument_id="SPY.SMART",
        side="BUY",
        requested_quantity="5",
        role=OrderRole.ENTRY,
    )
    ledger.apply_order_state("O-1", LifecycleStatus.ACKNOWLEDGED)
    if filled:
        ledger.apply_fill(
            client_order_id="O-1",
            execution_id="E-1",
            instrument_id="SPY.SMART",
            side="BUY",
            quantity="5",
            price="500",
            ts_ns=10,
        )
    return ledger


def _snapshot(*, with_execution: bool = True) -> BrokerSnapshot:
    return BrokerSnapshot(
        account=_account(),
        positions=(BrokerPosition.normalized("SPY.SMART", "5", "500", "DU123"),),
        orders=(
            BrokerOrder.normalized(
                client_order_id="O-1",
                instrument_id="SPY.SMART",
                side="BUY",
                quantity="5",
                filled_quantity="5",
                status="FILLED",
                owned_by_strategy=True,
            ),
        ),
        executions=(
            (
                BrokerExecution.normalized(
                    execution_id="E-1",
                    client_order_id="O-1",
                    instrument_id="SPY.SMART",
                    side="BUY",
                    quantity="5",
                    price="500",
                    ts_ns=10,
                ),
            )
            if with_execution
            else ()
        ),
        positions_complete=True,
        orders_complete=True,
        executions_complete=True,
    )


def test_complete_matching_broker_snapshot_passes():
    report = reconcile(
        _ledger(filled=True),
        _snapshot(),
        ReconciliationConfig(expected_account_id="DU123", require_settled_cash=True),
    )
    assert report.passed
    assert report.issues == ()


def test_missing_execution_is_recovered_then_reconciles_cleanly():
    ledger = _ledger(filled=False)
    snapshot = _snapshot()
    first = reconcile(ledger, snapshot, ReconciliationConfig(expected_account_id="DU123"))
    assert first.recovered_execution_ids == ("E-1",)
    assert not first.passed

    recovered = recover_ledger(ledger, snapshot, first)
    second = reconcile(
        recovered,
        snapshot,
        ReconciliationConfig(expected_account_id="DU123"),
    )
    assert second.passed
    assert recovered.position("SPY.SMART").quantity == Decimal("5")


def test_unmanaged_broker_position_and_incomplete_snapshot_fail_closed():
    snapshot = BrokerSnapshot(
        account=_account(),
        positions=(BrokerPosition.normalized("QQQ.SMART", "2", "400"),),
        positions_complete=False,
        orders_complete=True,
        executions_complete=True,
    )
    report = reconcile(ExecutionLedger(), snapshot)
    assert not report.passed
    assert {issue.code for issue in report.issues} >= {
        "INCOMPLETE_BROKER_SNAPSHOT",
        "UNMANAGED_BROKER_POSITION",
    }


def test_operations_audit_chain_is_immutable_and_commands_are_recoverable(tmp_path):
    store = OperationsStore(str(tmp_path / "operations.sqlite3"))
    try:
        first = store.append_event("strategy", "SIGNAL", {"yhat": 0.01})
        store.append_event("strategy", "ORDER", {"id": "O-1"})
        assert store.verify_audit_chain()[0]
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            with store._connection() as connection:
                connection.execute(
                    "UPDATE audit_events SET payload_json = '{}' WHERE sequence = ?",
                    (first.sequence,),
                )

        command = store.request_command(
            "strategy:paper",
            "FLATTEN",
            "risk rail",
            dedupe_key="risk-rail",
        )
        duplicate = store.request_command(
            "strategy:paper",
            "FLATTEN",
            "risk rail",
            dedupe_key="risk-rail",
        )
        assert duplicate.command_id == command.command_id
        claimed = store.claim_commands("strategy:paper", "strategy:paper")
        assert [item.command_id for item in claimed] == [command.command_id]
        store.acknowledge_command(command.command_id, "strategy:paper")

        store.close()
        recovered = OperationsStore(str(tmp_path / "operations.sqlite3"))
        assert recovered.acknowledged_commands("strategy:paper", "strategy:paper")
        recovered.complete_command(
            command.command_id,
            "strategy:paper",
            success=True,
            result={"broker_flat_confirmed": True},
        )
        assert recovered.get_command(command.command_id).status == "COMPLETED"
        assert recovered.integrity_check()[0]
        recovered.close()
    finally:
        store.close()


def test_nautilus_cache_snapshot_does_not_hide_foreign_active_orders():
    class Money:
        def __init__(self, value):
            self.value = value

        def as_double(self):
            return self.value

    account = SimpleNamespace(
        id="IB-DU123",
        base_currency="USD",
        balance_total=lambda: Money(10_000),
        balance_free=lambda: Money(9_000),
    )
    order = SimpleNamespace(
        client_order_id="MANUAL-1",
        instrument_id="SPY.SMART",
        side=SimpleNamespace(name="BUY"),
        quantity=Money(1),
        filled_qty=Money(0),
        status=SimpleNamespace(name="ACCEPTED"),
        venue_order_id="IB-42",
        strategy_id="OTHER-STRATEGY",
        events=[],
    )
    cache = SimpleNamespace(
        accounts=lambda: [account],
        positions_open=lambda: [],
        orders=lambda: [order],
    )
    snapshot = snapshot_from_nautilus_cache(
        cache,
        ExecutionLedger(),
        strategy_id="ML-STRATEGY",
        expected_account_id="IB-DU123",
        captured_at_ns=1,
    )
    report = reconcile(
        ExecutionLedger(),
        snapshot,
        ReconciliationConfig(expected_account_id="IB-DU123"),
    )
    assert not report.passed
    assert any(issue.code == "UNMANAGED_BROKER_ORDER" for issue in report.issues)
