"""Daily-close intent reaches the next real session without bypassing safety."""
from datetime import datetime, timedelta, timezone
from types import MethodType, SimpleNamespace
from collections import defaultdict

import pytest

from quant.strategies.ml_strategy import MLStrategy, MLStrategyConfig, _PendingSignal
from quant.strategies.sessions import ExchangeSessionCalendar, SessionPolicy
from quant.strategies.session_signals import SessionSignal
from quant.strategies.execution_state import ExecutionLedger, ExecutionSafetyController
from quant.strategies.risk import RiskManager

UTC = timezone.utc
FRIDAY_CLOSE = datetime(2026, 3, 6, 21, tzinfo=UTC)
MONDAY_ENTRY = datetime(2026, 3, 9, 13, 35, tzinfo=UTC)  # DST weekend


def calendar(**policy):
    return ExchangeSessionCalendar(
        trading_hours="20260306:0400-2000;20260307:CLOSED;20260308:CLOSED;20260309:0400-2000;20260310:0400-2000",
        liquid_hours="20260306:0930-1600;20260307:CLOSED;20260308:CLOSED;20260309:0930-1600;20260310:0930-1600",
        timezone_id="US/Eastern", policy=SessionPolicy(**policy),
    )


def strategy():
    now = [FRIDAY_CLOSE]
    submitted, audit = [], []
    obj = SimpleNamespace(
        config=MLStrategyConfig(instrument_ids=["QQQ.SMART", "SPY.SMART"], execution_mode="paper", asset_class="equity"),
        clock=SimpleNamespace(utc_now=lambda: now[0]), _pending_ts=FRIDAY_CLOSE,
        _pending={}, _session_signals={}, _session_calendars={"QQQ.SMART": calendar(), "SPY.SMART": calendar()},
        _last_mark={"QQQ.SMART": 110, "SPY.SMART": 210}, _risk=RiskManager(5000),
        _execution_safety=ExecutionSafetyController(), _staged_exit_active=False,
        _execution=ExecutionLedger(), _pending_exits={}, _bar_index=defaultdict(int),
        _entry_submitted_bar={}, _committed_notional=defaultdict(float),
        is_exiting=lambda: False, _equity=lambda: 5000,
        _broker_position_state=lambda iid: (0, None), _can_open_new_position=lambda: True,
        _enter=lambda *args: submitted.append(args), _request_instrument_exit=lambda *args: None,
        _audit_event=lambda *args, **kwargs: audit.append(args),
        log=SimpleNamespace(warning=lambda *args: None),
    )
    for name in ("_uses_session_signals", "_schedule_session_signals", "_dispatch_session_signals",
                 "_resolve_batch", "_execute_signals", "_session_allows_entry", "_signal_contract"):
        setattr(obj, name, MethodType(getattr(MLStrategy, name), obj))
    return obj, now, submitted, audit


def queue(obj):
    obj._pending = {"QQQ.SMART": _PendingSignal(100, 2, .01), "SPY.SMART": _PendingSignal(200, 3, .02)}
    obj._resolve_batch()


def test_close_to_next_open_obeys_dst_buffer_and_uses_fresh_price_with_conviction_ranking():
    obj, now, submitted, _ = strategy()
    queue(obj)
    assert not submitted and len(obj._session_signals) == 2
    assert obj._session_signals["QQQ.SMART"].not_before == MONDAY_ENTRY
    now[0] = MONDAY_ENTRY - timedelta(minutes=1)
    for cal in obj._session_calendars.values():
        cal.record_market_data(now[0])
    obj._dispatch_session_signals(now[0])
    assert not submitted
    now[0] = MONDAY_ENTRY
    for cal in obj._session_calendars.values():
        cal.record_market_data(now[0])
    obj._dispatch_session_signals(now[0])
    assert [args[0] for args in submitted] == ["SPY.SMART", "QQQ.SMART"]
    assert [args[2] for args in submitted] == [210, 110]  # current, not Friday close
    assert [args[3] for args in submitted] == [3, 2]  # prior ATR, not recomputed with future bars
    obj._dispatch_session_signals(now[0])
    assert len(submitted) == 2 and not obj._session_signals


def test_frozen_stale_and_halted_state_cannot_dispatch_and_intent_expires():
    obj, now, submitted, audit = strategy()
    queue(obj)
    now[0] = MONDAY_ENTRY
    obj._execution_safety.freeze("operator freeze")
    obj._dispatch_session_signals(now[0])
    assert not submitted
    obj._execution_safety.resume("test")
    for cal in obj._session_calendars.values():
        cal.record_market_data(now[0] - timedelta(seconds=30))
    obj._dispatch_session_signals(now[0])
    assert not submitted
    for cal in obj._session_calendars.values():
        cal.record_market_data(now[0]); cal.set_halt(True)
    obj._dispatch_session_signals(now[0])
    assert not submitted
    now[0] = datetime(2026, 3, 9, 19, 45, tzinfo=UTC)
    obj._dispatch_session_signals(now[0])
    assert not obj._session_signals and not submitted
    assert any(row[0] == "SESSION_SIGNAL_EXPIRED" for row in audit)


def test_missing_or_exhausted_schedule_never_invents_a_next_session():
    obj, now, submitted, audit = strategy()
    now[0] = datetime(2026, 3, 11, 12, tzinfo=UTC)
    queue(obj)
    assert not obj._session_signals and not submitted
    assert any(row[0] in {"SESSION_SIGNAL_EXPIRED", "SESSION_SIGNAL_UNSCHEDULED"} for row in audit)


def test_older_or_duplicate_signals_cannot_replace_newer_intent():
    obj, now, _, _ = strategy()
    queue(obj)
    first = obj._session_signals["QQQ.SMART"]
    obj._pending_ts -= timedelta(days=1)
    queue(obj)
    assert obj._session_signals["QQQ.SMART"] == first


def test_delayed_signal_from_an_older_completed_session_is_discarded():
    obj, now, submitted, _ = strategy()
    now[0] = datetime(2026, 3, 9, 21, tzinfo=UTC)
    queue(obj)
    assert not obj._session_signals and not submitted


def test_snapshot_is_timezone_aware_and_configuration_change_invalidates_intent():
    obj, _, _, _ = strategy()
    queue(obj)
    queued = obj._session_signals["QQQ.SMART"]
    assert SessionSignal.restore(queued.snapshot()) == queued
    old = obj._signal_contract()
    obj.config = MLStrategyConfig(instrument_ids=["QQQ.SMART", "SPY.SMART"], execution_mode="paper", asset_class="equity", n_lags=10)
    assert obj._signal_contract() != old
    bad = {**queued.snapshot(), "not_before": "2026-03-09T13:35:00"}
    with pytest.raises(ValueError, match="timezone"):
        SessionSignal.restore(bad)


def test_backtests_and_intraday_strategies_keep_immediate_batch_execution():
    obj, _, submitted, _ = strategy()
    obj.config = MLStrategyConfig(instrument_ids=["QQQ.SMART"], execution_mode="backtest", asset_class="equity")
    obj._session_calendars.clear()
    obj._pending = {"QQQ.SMART": _PendingSignal(100, 2, .01)}
    obj._resolve_batch()
    assert len(submitted) == 1 and not obj._session_signals


def test_holiday_early_close_and_custom_break_planning():
    cal = ExchangeSessionCalendar(
        trading_hours="20261126:CLOSED;20261127:0400-1300;20261130:0400-2000",
        liquid_hours="20261126:CLOSED;20261127:0930-1300;20261130:0930-1600", timezone_id="US/Eastern")
    window = cal.next_entry_window(datetime(2026, 11, 26, 12, tzinfo=UTC))
    assert window.start == datetime(2026, 11, 27, 14, 35, tzinfo=UTC)
    assert window.end == datetime(2026, 11, 27, 17, 45, tzinfo=UTC)
    assert cal.next_entry_window(window.end).start == datetime(2026, 11, 30, 14, 35, tzinfo=UTC)
    custom = calendar(mode="CUSTOM", custom_windows=(("09:30", "11:00"), ("13:00", "16:00")))
    window = custom.next_entry_window(datetime(2026, 3, 9, 15, tzinfo=UTC))
    assert window.start == datetime(2026, 3, 9, 17, 5, tzinfo=UTC)


def test_real_strategy_snapshot_restores_queued_intent_only_for_matching_configuration():
    from nautilus_trader.backtest.engine import BacktestEngine
    from nautilus_trader.model.identifiers import InstrumentId
    config = MLStrategyConfig(instrument_ids=["QQQ.SMART"], execution_mode="paper", asset_class="equity")
    iid = InstrumentId.from_str("QQQ.SMART")
    first = MLStrategy(config)
    first._risk = RiskManager(5000)
    first._session_signals[iid] = SessionSignal(FRIDAY_CLOSE, MONDAY_ENTRY, MONDAY_ENTRY + timedelta(hours=5), 100, 2, .01)
    saved = first.on_save()
    from nautilus_trader.backtest.config import BacktestEngineConfig
    from nautilus_trader.config import LoggingConfig
    engine = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    try:
        restored = MLStrategy(config)
        restored._risk = RiskManager(5000)
        restored._iid_by_raw[str(iid)] = iid
        engine.add_strategy(restored)
        restored.on_load(saved)
        restored._restore_loaded_state()
        assert restored._session_signals[iid] == first._session_signals[iid]
        changed = MLStrategy(MLStrategyConfig(instrument_ids=["QQQ.SMART"], execution_mode="paper", asset_class="equity", n_lags=10))
        changed._risk = RiskManager(5000)
        changed._iid_by_raw[str(iid)] = iid
        engine.add_strategy(changed)
        changed.on_load(saved)
        changed._restore_loaded_state()
        assert not changed._session_signals
    finally:
        engine.dispose()


def test_restored_intent_cannot_resubmit_a_signal_already_in_execution_ledger():
    from quant.strategies.execution_state import OrderRole
    obj, now, submitted, audit = strategy()
    obj._pending = {"QQQ.SMART": _PendingSignal(100, 2, .01)}
    obj._resolve_batch()
    obj._execution.register_order(client_order_id="original", instrument_id="QQQ.SMART",
        side="BUY", requested_quantity="1", role=OrderRole.ENTRY,
        signal_version=f"{FRIDAY_CLOSE.isoformat()}:BUY", ts_ns=1)
    now[0] = MONDAY_ENTRY
    obj._session_calendars["QQQ.SMART"].record_market_data(now[0])
    obj._dispatch_session_signals(now[0])
    assert not submitted and not obj._session_signals
    assert any(row[0] == "SESSION_SIGNAL_ALREADY_SUBMITTED" for row in audit)
