from decimal import Decimal

import pytest

from quant.strategies.execution_state import (
    ExecutionLedger,
    ExecutionSafetyController,
    ExecutionSafetyState,
    LifecycleStatus,
    OrderRole,
)


def _ledger_with_order(quantity="10"):
    ledger = ExecutionLedger()
    ledger.register_order(
        client_order_id="O-1",
        instrument_id="QQQ.SMART",
        side="BUY",
        requested_quantity=quantity,
        role=OrderRole.ENTRY,
        signal_version="signal-1",
        ts_ns=1,
    )
    return ledger


def test_order_lifecycle_partial_fill_and_average_use_actual_executions():
    ledger = _ledger_with_order()
    ledger.apply_order_state("O-1", LifecycleStatus.SUBMITTED, event_id="E-1", ts_ns=2)
    ledger.apply_order_state(
        "O-1",
        LifecycleStatus.ACKNOWLEDGED,
        event_id="E-2",
        venue_order_id="101-9001",
        permanent_order_id="9001",
        ts_ns=3,
    )
    ledger.apply_fill(
        client_order_id="O-1",
        execution_id="X-1",
        instrument_id="QQQ.SMART",
        side="BUY",
        quantity="4",
        price="100",
        ts_ns=4,
        event_id="E-3",
    )
    order = ledger.orders["O-1"]
    assert order.status == LifecycleStatus.PARTIALLY_FILLED
    assert order.filled_quantity == Decimal("4")
    assert order.remaining_quantity == Decimal("6")
    assert order.average_fill_price == Decimal("100")

    ledger.apply_fill(
        client_order_id="O-1",
        execution_id="X-2",
        instrument_id="QQQ.SMART",
        side="BUY",
        quantity="6",
        price="102",
        ts_ns=5,
        event_id="E-4",
    )
    assert order.status == LifecycleStatus.FILLED
    assert order.average_fill_price == Decimal("101.2")
    position = ledger.position("QQQ.SMART")
    assert position.quantity == Decimal("10")
    assert position.average_entry_price == Decimal("101.2")


def test_duplicate_execution_callback_is_idempotent_even_with_new_event_id():
    ledger = _ledger_with_order()
    kwargs = dict(
        client_order_id="O-1",
        execution_id="X-1",
        instrument_id="QQQ.SMART",
        side="BUY",
        quantity="5",
        price="100",
        ts_ns=4,
    )
    assert ledger.apply_fill(**kwargs, event_id="E-1") is True
    assert ledger.apply_fill(**kwargs, event_id="E-2") is False
    assert ledger.orders["O-1"].filled_quantity == Decimal("5")
    assert ledger.position("QQQ.SMART").quantity == Decimal("5")


def test_conflicting_duplicate_execution_fails_closed():
    ledger = _ledger_with_order()
    ledger.apply_fill(
        client_order_id="O-1",
        execution_id="X-1",
        instrument_id="QQQ.SMART",
        side="BUY",
        quantity="5",
        price="100",
        ts_ns=4,
    )
    with pytest.raises(ValueError, match="conflicting duplicate execution"):
        ledger.apply_fill(
            client_order_id="O-1",
            execution_id="X-1",
            instrument_id="QQQ.SMART",
            side="BUY",
            quantity="5",
            price="101",
            ts_ns=5,
        )


def test_execution_correction_replaces_prior_fill_and_rebuilds_position():
    ledger = _ledger_with_order()
    ledger.apply_fill(
        client_order_id="O-1",
        execution_id="X-1",
        instrument_id="QQQ.SMART",
        side="BUY",
        quantity="10",
        price="100",
        ts_ns=4,
    )
    ledger.apply_fill(
        client_order_id="O-1",
        execution_id="X-1-CORRECTED",
        correction_of="X-1",
        instrument_id="QQQ.SMART",
        side="BUY",
        quantity="10",
        price="99.5",
        ts_ns=5,
    )
    assert "X-1" not in ledger.fills
    assert ledger.orders["O-1"].average_fill_price == Decimal("99.5")
    assert ledger.position("QQQ.SMART").average_entry_price == Decimal("99.5")


def test_cancel_fill_race_keeps_actual_partial_position():
    ledger = _ledger_with_order()
    ledger.apply_order_state("O-1", LifecycleStatus.ACKNOWLEDGED)
    ledger.apply_order_state("O-1", LifecycleStatus.PENDING_CANCEL)
    ledger.apply_fill(
        client_order_id="O-1",
        execution_id="X-1",
        instrument_id="QQQ.SMART",
        side="BUY",
        quantity="3",
        price="100",
        ts_ns=5,
    )
    ledger.apply_order_state("O-1", LifecycleStatus.CANCELED, ts_ns=6)
    order = ledger.orders["O-1"]
    assert order.status == LifecycleStatus.CANCELED
    assert order.filled_quantity == Decimal("3")
    assert ledger.position("QQQ.SMART").quantity == Decimal("3")


def test_fill_replay_computes_realized_pnl_and_reversal_entry():
    ledger = _ledger_with_order(quantity="10")
    ledger.apply_fill(
        client_order_id="O-1",
        execution_id="BUY",
        instrument_id="QQQ.SMART",
        side="BUY",
        quantity="10",
        price="100",
        ts_ns=1,
    )
    ledger.register_order(
        client_order_id="O-2",
        instrument_id="QQQ.SMART",
        side="SELL",
        requested_quantity="15",
        role=OrderRole.SIGNAL_EXIT,
    )
    ledger.apply_fill(
        client_order_id="O-2",
        execution_id="SELL",
        instrument_id="QQQ.SMART",
        side="SELL",
        quantity="15",
        price="110",
        ts_ns=2,
    )
    position = ledger.position("QQQ.SMART")
    assert position.quantity == Decimal("-5")
    assert position.average_entry_price == Decimal("110")
    assert position.realized_pnl == Decimal("100")


def test_snapshot_preserves_ids_fills_and_materialized_position():
    ledger = _ledger_with_order()
    ledger.apply_order_state(
        "O-1",
        LifecycleStatus.ACKNOWLEDGED,
        venue_order_id="101-9001",
        permanent_order_id="9001",
    )
    ledger.apply_fill(
        client_order_id="O-1",
        execution_id="X-1",
        instrument_id="QQQ.SMART",
        side="BUY",
        quantity="2",
        price="100.25",
        ts_ns=4,
    )
    restored = ExecutionLedger.from_snapshot(ledger.snapshot())
    assert restored.orders["O-1"].permanent_order_id == "9001"
    assert restored.orders["O-1"].filled_quantity == Decimal("2")
    assert restored.position("QQQ.SMART").quantity == Decimal("2")


def test_rejection_and_cancel_rejection_suspend_entries_and_alert_operator():
    controller = ExecutionSafetyController(max_rejections=2)
    controller.on_rejection("first", client_order_id="O-1")
    assert controller.entries_allowed is True
    controller.on_rejection("second", client_order_id="O-2")
    assert controller.state == ExecutionSafetyState.SUSPENDED
    assert controller.entries_allowed is False
    assert {alert.code for alert in controller.alerts} >= {
        "ORDER_REJECTED",
        "REJECTION_THRESHOLD",
    }

    cancel_controller = ExecutionSafetyController()
    cancel_controller.on_cancel_rejected("still working", client_order_id="O-3")
    assert cancel_controller.state == ExecutionSafetyState.SUSPENDED
    assert cancel_controller.alerts[-1].code == "CANCEL_REJECTED"


def test_incomplete_shutdown_persists_uncertainty_across_restart():
    controller = ExecutionSafetyController()
    controller.begin_shutdown()
    restored = ExecutionSafetyController.from_snapshot(controller.snapshot())
    restored.on_restart()
    assert restored.state == ExecutionSafetyState.UNCERTAIN
    assert restored.entries_allowed is False
