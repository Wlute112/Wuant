from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from quant.ops.state import OperationsStore
from quant.strategies.execution_state import ExecutionSafetyController, ExecutionSafetyState
from quant.strategies.ml_strategy import MLStrategy
from quant.strategies.risk import RiskManager
from quant.strategies.sessions import SessionPhase


@pytest.fixture
def strategy(tmp_path):
    now = datetime.now(timezone.utc)
    store = OperationsStore(str(tmp_path / "ops.sqlite3"))
    positions, orders, inflight, cancelled, exits = [], [], [], [], []
    safety = ExecutionSafetyController()
    safety.freeze("test")
    balance = SimpleNamespace(as_double=lambda: 5000)
    account = SimpleNamespace(balance_total=lambda **kwargs: balance,
                              last_event=lambda: SimpleNamespace(ts_event=int(now.timestamp() * 1e9)))
    obj = SimpleNamespace(
        id="ML", _operations=store, _operations_failed=False, _telemetry_failed=False,
        _risk=RiskManager(5000), _execution_safety=safety, _operations_entries_frozen=True,
        _reconciliation_state="BROKER_RECONCILED", _data_quality_blocked_instruments=set(),
        _staged_exit_active=False, _pending_exits={}, _startup_protection_pending=set(),
        _position_references={}, _bar_types={"SPY": "bars"},
        _corporate_registry=None,
        _session_calendars={"SPY": SimpleNamespace(phase_at=lambda when: SessionPhase.RTH)},
        _short_control=None, _external_supervisor_unhealthy=False, _started_at=now,
        _pending={"SPY": "old signal"},
        config=SimpleNamespace(require_external_supervisor=True, startup_health_grace_secs=30,
                               account_id="IB-DU1",
                               require_session_schedule=True, allow_short_positions=False,
                               execution_mode="paper"),
        clock=SimpleNamespace(utc_now=lambda: now, timestamp_ns=lambda: int(now.timestamp() * 1e9)),
        cache=SimpleNamespace(orders_open=lambda **kwargs: list(orders),
                              orders_inflight=lambda **kwargs: list(inflight),
                              positions_open=lambda **kwargs: list(positions)),
        _account=lambda: account, _external_supervisor_is_fresh=lambda: True,
        _operations_target=lambda: "strategy:test", is_exiting=lambda: False,
        _audit_event=lambda *args, **kwargs: True,
        _cancel_working_entry_orders=lambda **kwargs: cancelled.append("entries"),
        cancel_order=lambda order: cancelled.append(order.client_order_id),
        _begin_risk_exit=lambda reason, permanent: exits.append(permanent),
        _has_broker_exposure=lambda: bool(positions or orders or inflight),
        _reconcile_broker_cache_source_of_truth=lambda: None,
    )
    obj._resume_blockers = lambda: MLStrategy._resume_blockers(obj)
    obj._control_key = lambda kind: MLStrategy._control_key(obj, kind)
    obj._restore_operator_controls = lambda: MLStrategy._restore_operator_controls(obj)
    obj._check_corporate_actions = lambda: MLStrategy._check_corporate_actions(obj)
    yield obj, store, positions, orders, inflight, cancelled, exits
    store.close()


def command(strategy, action, **kwargs):
    obj, store, *_ = strategy
    result = store.request_command("strategy:test", action, "operator test", **kwargs)
    # Advancing the fake clock also proves request time is considered by the node.
    now = datetime.now(timezone.utc)
    obj.clock.utc_now = lambda: now
    obj.clock.timestamp_ns = lambda: int(now.timestamp() * 1e9)
    MLStrategy._process_operations_control(obj)
    return store.get_command(result.command_id)


def test_corporate_action_freezes_entries_and_blocks_resume_until_rebuilt(strategy):
    from quant.data.instrument_identity import EquityIdentity
    from quant.ops.corporate_actions import CorporateAction, CorporateActionRegistry

    obj, store, _, _, _, cancelled, _ = strategy
    account = obj.config.account_id
    registry = CorporateActionRegistry(store)
    instrument = EquityIdentity(123, "ABC", "NYSE", "COMMON")
    now = obj.clock.utc_now()
    registry.reconcile(account, [instrument], broker_flat=True, now=now)
    registry.register_event(account, CorporateAction(event_id="split-1", kind="SPLIT",
        con_id=123, effective_at=now, split_ratio=2, source_reference="issuer split notice"), "operator")
    obj._corporate_registry = registry
    obj._corporate_pending_ids = set()
    obj._operations_entries_frozen = False
    obj._check_corporate_actions()
    assert obj._operations_entries_frozen
    assert store.get_state(obj._control_key("operator-freeze")) is True
    assert obj._pending == {}
    assert cancelled == ["entries"]
    assert any("Corporate-action" in reason for reason in obj._resume_blockers())
    obj._check_corporate_actions()
    assert cancelled == ["entries"]
    registry.reconcile(account, [instrument], broker_flat=True, now=now)
    assert any("Corporate-action" in reason for reason in obj._resume_blockers())
    registry.finish_rebuild(account, {123})
    assert not any("Corporate-action" in reason for reason in obj._resume_blockers())
    assert obj._operations_entries_frozen  # Completion never implicitly resumes.


def test_resume_rechecks_state_and_clears_old_signals(strategy):
    obj, *_ = strategy
    assert obj._resume_blockers() == []
    result = command(strategy, "RESUME_ENTRIES")
    assert result.status == "COMPLETED"
    assert obj._execution_safety.entries_allowed
    assert obj._pending == {}
    assert not obj._operations_entries_frozen


@pytest.mark.parametrize("state", [ExecutionSafetyState.UNCERTAIN, ExecutionSafetyState.SUSPENDED,
                                  ExecutionSafetyState.EMERGENCY, ExecutionSafetyState.STOPPING])
def test_resume_cannot_clear_uncertainty_or_emergency(strategy, state):
    obj, *_ = strategy
    obj._execution_safety.state = state
    assert command(strategy, "RESUME_ENTRIES").status == "FAILED"
    assert obj._execution_safety.state == state
    assert obj._operations_entries_frozen


def test_supervisor_cannot_release_operator_freeze(strategy):
    obj, *_ = strategy
    result = command(strategy, "RESUME_ENTRIES", payload={"supervisor": "supervisor:test"})
    assert result.status == "FAILED"
    assert obj._operations_entries_frozen


def test_expired_resume_cannot_execute_after_restart(strategy):
    obj, store, *_ = strategy
    cmd = store.request_command("strategy:test", "RESUME_ENTRIES", "old request")
    later = datetime.now(timezone.utc) + timedelta(seconds=31)
    obj.clock.utc_now = lambda: later
    MLStrategy._process_operations_control(obj)
    result = store.get_command(cmd.command_id)
    assert result.status == "FAILED"
    assert any("expired" in reason for reason in result.result["blockers"])


def test_resume_reconciles_again_before_deciding(strategy):
    obj, *_ = strategy
    def uncertain():
        obj._reconciliation_state = "UNCERTAIN"
    obj._reconcile_broker_cache_source_of_truth = uncertain
    assert command(strategy, "RESUME_ENTRIES").status == "FAILED"


def test_resume_health_interlocks(strategy):
    obj, _, positions, orders, inflight, *_ = strategy
    obj._account = lambda: None
    obj._external_supervisor_is_fresh = lambda: False
    obj._data_quality_blocked_instruments.add("SPY")
    obj._session_calendars["SPY"].phase_at = lambda now: SessionPhase.STALE
    positions.append(SimpleNamespace(instrument_id="SPY"))
    inflight.append(object())
    obj._risk.engage_kill_switch()
    assert len(obj._resume_blockers()) >= 6
    assert command(strategy, "RESUME_ENTRIES").status == "FAILED"


def test_cancel_all_is_not_flatten_and_protects_existing_positions(strategy):
    obj, _, positions, orders, inflight, cancelled, exits = strategy
    positions.append(SimpleNamespace(instrument_id="SPY"))
    assert command(strategy, "CANCEL_ALL").status == "FAILED"
    assert cancelled == [] and exits == []
    assert obj._operations_entries_frozen


def test_cancel_all_waits_for_ack_and_reports_fill_race(strategy):
    obj, store, positions, orders, inflight, cancelled, exits = strategy
    orders.append(SimpleNamespace(client_order_id="entry"))
    result = command(strategy, "CANCEL_ALL")
    assert result.status == "ACKNOWLEDGED"
    assert cancelled == ["entry"] and exits == []
    orders.clear()
    orders.append(SimpleNamespace(client_order_id="new-protective-stop"))
    positions.append(SimpleNamespace(instrument_id="SPY"))
    MLStrategy._process_operations_control(obj)
    assert store.get_command(result.command_id).status == "FAILED"
    assert cancelled == ["entry"]


def test_flatten_waits_for_broker_flat_and_remains_frozen(strategy):
    obj, store, positions, _, _, _, exits = strategy
    positions.append(SimpleNamespace(instrument_id="SPY"))
    result = command(strategy, "FLATTEN")
    assert exits == [False]
    assert result.status == "ACKNOWLEDGED"
    positions.clear()
    MLStrategy._process_operations_control(obj)
    assert store.get_command(result.command_id).status == "COMPLETED"
    assert obj._operations_entries_frozen


def test_kill_is_permanent_even_if_flat(strategy):
    obj, *_ = strategy
    assert command(strategy, "KILL").status == "COMPLETED"
    assert not obj._risk.can_open
    assert command(strategy, "RESUME_ENTRIES").status == "FAILED"


def test_kill_and_freeze_survive_without_a_shutdown_snapshot(strategy):
    obj, store, *_ = strategy
    command(strategy, "FREEZE_ENTRIES")
    obj._operations_entries_frozen = False
    obj._execution_safety = ExecutionSafetyController()
    obj._restore_operator_controls()
    assert obj._operations_entries_frozen
    assert obj._execution_safety.state == ExecutionSafetyState.FROZEN
    command(strategy, "KILL")
    obj._risk = RiskManager(5000)
    obj._execution_safety = ExecutionSafetyController()
    obj._operations_entries_frozen = False
    obj._restore_operator_controls()
    assert not obj._risk.can_open
    assert obj._execution_safety.state == ExecutionSafetyState.EMERGENCY
