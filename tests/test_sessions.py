from datetime import datetime, timedelta, timezone

import pytest

from quant.strategies.risk import RiskManager, TradingState
from quant.strategies.sessions import (
    ExchangeSessionCalendar,
    OvernightPnlAssignment,
    SessionPhase,
    SessionPolicy,
    SessionPolicyMode,
    parse_ibkr_hours,
    resolve_session_policy,
)


UTC = timezone.utc


TRADING = (
    "20260306:0400-20260306:2000;"
    "20260309:0400-20260309:2000;"
    "20260703:CLOSED;"
    "20261127:0400-20261127:1300"
)
LIQUID = (
    "20260306:0930-20260306:1600;"
    "20260309:0930-20260309:1600;"
    "20260703:CLOSED;"
    "20261127:0930-20261127:1300"
)


def _calendar(policy=None, max_age=timedelta(seconds=15)):
    return ExchangeSessionCalendar(
        trading_hours=TRADING,
        liquid_hours=LIQUID,
        timezone_id="US/Eastern",
        policy=policy,
        max_market_data_age=max_age,
    )


def test_ibkr_hours_apply_dst_by_exchange_timezone():
    intervals = parse_ibkr_hours(
        "20260306:0930-1600;20260309:0930-1600",
        "US/Eastern",
    )
    before = intervals[datetime(2026, 3, 6).date()][0]
    after = intervals[datetime(2026, 3, 9).date()][0]
    assert before.start == datetime(2026, 3, 6, 14, 30, tzinfo=UTC)
    assert after.start == datetime(2026, 3, 9, 13, 30, tzinfo=UTC)


def test_holiday_and_early_close_are_broker_schedule_authoritative():
    calendar = _calendar()
    holiday = calendar.days[datetime(2026, 7, 3).date()]
    assert holiday.is_closed
    early_close = calendar.days[datetime(2026, 11, 27).date()].liquid[0]
    assert early_close.end == datetime(2026, 11, 27, 18, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "when,phase",
    [
        (datetime(2026, 3, 9, 12, 0, tzinfo=UTC), SessionPhase.PRE_MARKET),
        (datetime(2026, 3, 9, 13, 30, tzinfo=UTC), SessionPhase.OPENING_AUCTION),
        (datetime(2026, 3, 9, 15, 0, tzinfo=UTC), SessionPhase.RTH),
        (datetime(2026, 3, 9, 19, 59, 30, tzinfo=UTC), SessionPhase.CLOSING_AUCTION),
        (datetime(2026, 3, 9, 21, 0, tzinfo=UTC), SessionPhase.AFTER_HOURS),
        (datetime(2026, 3, 10, 0, 1, tzinfo=UTC), SessionPhase.CLOSED),
    ],
)
def test_session_phase_classification(when, phase):
    assert _calendar().phase_at(when) == phase


def test_rth_policy_applies_open_close_buffers_and_no_entry_period():
    calendar = _calendar(
        SessionPolicy(
            opening_buffer_minutes=5,
            closing_buffer_minutes=5,
            no_new_entry_minutes_before_close=15,
        )
    )
    assert calendar.allows_new_entry(datetime(2026, 3, 9, 13, 33, tzinfo=UTC)) == (
        False,
        "OPENING_BUFFER",
    )
    assert calendar.allows_new_entry(datetime(2026, 3, 9, 13, 36, tzinfo=UTC)) == (
        True,
        "ALLOWED",
    )
    assert calendar.allows_new_entry(datetime(2026, 3, 9, 19, 46, tzinfo=UTC)) == (
        False,
        "CLOSING_BUFFER",
    )


def test_extended_hours_requires_limit_not_market_orders():
    calendar = _calendar(
        SessionPolicy(
            mode=SessionPolicyMode.EXTENDED_HOURS,
            opening_buffer_minutes=0,
            closing_buffer_minutes=0,
            no_new_entry_minutes_before_close=0,
        )
    )
    premarket = datetime(2026, 3, 9, 12, 0, tzinfo=UTC)
    assert calendar.validates_order(
        premarket,
        order_type="MARKET",
        time_in_force="DAY",
    ) == (False, "MARKET_ORDER_OUTSIDE_RTH")
    assert calendar.validates_order(
        premarket,
        order_type="LIMIT",
        time_in_force="DAY",
    ) == (True, "ALLOWED")


def test_stale_data_and_halt_fail_closed():
    calendar = _calendar(max_age=timedelta(seconds=10))
    observed = datetime(2026, 3, 9, 15, 0, tzinfo=UTC)
    calendar.record_market_data(observed)
    assert calendar.phase_at(observed + timedelta(seconds=11)) == SessionPhase.STALE
    calendar.record_market_data(observed + timedelta(seconds=11))
    calendar.set_halt(True, "LULD")
    assert calendar.phase_at(observed + timedelta(seconds=11)) == SessionPhase.HALTED
    assert calendar.halt_reason == "LULD"


def test_session_key_assigns_extended_hours_to_exchange_session_not_utc_date():
    trading = "20260309:2000-20260310:0400"
    liquid = "20260309:2100-20260310:0300"
    calendar = ExchangeSessionCalendar(
        trading_hours=trading,
        liquid_hours=liquid,
        timezone_id="UTC",
        policy=SessionPolicy(
            mode=SessionPolicyMode.EXTENDED_HOURS,
            overnight_pnl_assignment=OvernightPnlAssignment.NEXT_SESSION,
            opening_buffer_minutes=0,
            closing_buffer_minutes=0,
            no_new_entry_minutes_before_close=0,
        ),
    )
    assert calendar.session_key(datetime(2026, 3, 10, 2, 0, tzinfo=UTC)) == "2026-03-09"


def test_daily_loss_baseline_resets_on_session_but_halt_lasts_full_duration():
    risk = RiskManager(5000)
    first = datetime(2026, 3, 9, 15, 0, tzinfo=UTC)
    risk.on_new_session("2026-03-09", first, 5000)
    assert risk.update_equity(first, 4899) == TradingState.HALTED_DAILY
    risk.on_new_session(
        "2026-03-09",
        datetime(2026, 3, 10, 1, 0, tzinfo=UTC),
        4899,
    )
    assert risk.state == TradingState.HALTED_DAILY
    risk.on_new_session(
        "2026-03-10",
        datetime(2026, 3, 10, 13, 30, tzinfo=UTC),
        4899,
    )
    assert risk.state == TradingState.HALTED_DAILY
    assert risk.telemetry(4899)["daily_pnl_pct"] == 0
    risk.on_new_session(
        "2026-03-10",
        datetime(2026, 3, 10, 15, 0, tzinfo=UTC),
        4899,
    )
    assert risk.state == TradingState.ACTIVE


def test_invalid_or_missing_ibkr_session_metadata_is_rejected():
    with pytest.raises(ValueError, match="missing"):
        ExchangeSessionCalendar.from_instrument_info({"timeZoneId": "US/Eastern"})


@pytest.mark.parametrize("payload", [
    {"mode": "TYPO"}, {"mode": "CUSTOM"},
    {"custom_windows": "09:30-16:00"},
    {"mode": "CUSTOM", "custom_windows": [["09:30", "09:30"]]},
    {"mode": "CUSTOM", "custom_windows": [["25:00", "16:00"]]},
    {"mode": "CUSTOM", "custom_windows": [["09:30"]]},
    {"opening_buffer_minutes": -1}, {"closing_buffer_minutes": 1.5},
    {"closing_buffer_minutes": True}, {"participate_opening_auction": "false"},
    {"cancel_entries_at_session_end": None}, {"overnight_pnl_assignment": "typo"},
    {"opening_bufer_minutes": 1},
])
def test_invalid_execution_session_contract_fails_closed(payload):
    with pytest.raises(ValueError):
        resolve_session_policy(payload, asset_class="equity", include_extended_hours=True)


def test_session_contract_requires_data_and_routing_for_extended_windows():
    with pytest.raises(ValueError, match="include_extended_hours"):
        resolve_session_policy({"mode": "EXTENDED_HOURS"}, asset_class="equity", include_extended_hours=False)
    with pytest.raises(ValueError, match="require equity"):
        resolve_session_policy({"mode": "RTH_ONLY"}, asset_class="crypto", include_extended_hours=False)


def test_schedule_horizon_does_not_invent_risk_resets_on_unobserved_dates():
    calendar = _calendar()
    assert calendar.session_key(datetime(2026, 11, 28, 12, tzinfo=UTC)) == "2026-11-27"
    assert calendar.session_key(datetime(2026, 11, 29, 12, tzinfo=UTC)) == "2026-11-27"
    closed = ExchangeSessionCalendar(
        trading_hours="20260703:CLOSED", liquid_hours="20260703:CLOSED",
        timezone_id="US/Eastern",
    )
    assert closed.session_key(datetime(2026, 7, 3, 12, tzinfo=UTC)) == "UNAVAILABLE"


def test_resting_entries_expire_at_policy_close_even_while_exchange_is_open():
    regular = _calendar()
    after_close = datetime(2026, 3, 9, 20, 0, tzinfo=UTC)
    assert regular.phase_at(after_close) == SessionPhase.AFTER_HOURS
    assert not regular.within_policy_window(after_close)
    custom = _calendar(SessionPolicy(
        mode=SessionPolicyMode.CUSTOM,
        custom_windows=(("09:30", "12:00"), ("13:00", "16:00")),
    ))
    assert custom.within_policy_window(datetime(2026, 3, 9, 15, 59, tzinfo=UTC))
    assert not custom.within_policy_window(datetime(2026, 3, 9, 16, 0, tzinfo=UTC))
    assert custom.within_policy_window(datetime(2026, 3, 9, 17, 0, tzinfo=UTC))
    # Custom windows cannot extend past an authoritative early close.
    assert not custom.within_policy_window(datetime(2026, 11, 27, 18, 0, tzinfo=UTC))


@pytest.mark.parametrize("assignment,before,after", [
    (OvernightPnlAssignment.NEXT_SESSION, "2026-07-02", "2026-07-06"),
    (OvernightPnlAssignment.PRIOR_SESSION, "2026-07-01", "2026-07-02"),
])
def test_overnight_pnl_uses_real_session_boundaries_and_skips_closed_dates(assignment, before, after):
    schedule = (
        "20260701:0400-2000;20260702:0400-2000;"
        "20260703:CLOSED;20260704:CLOSED;20260705:CLOSED;20260706:0400-2000"
    )
    calendar = ExchangeSessionCalendar(
        trading_hours=schedule, liquid_hours=schedule, timezone_id="US/Eastern",
        policy=SessionPolicy(overnight_pnl_assignment=assignment),
    )
    assert calendar.session_key(datetime(2026, 7, 2, 6, tzinfo=UTC)) == before
    assert calendar.session_key(datetime(2026, 7, 3, 1, tzinfo=UTC)) == after
    assert calendar.session_key(datetime(2026, 7, 4, 12, tzinfo=UTC)) == after


@pytest.mark.parametrize("cancel_at_end", [True, False])
@pytest.mark.parametrize("policy,when", [
    (SessionPolicy(), datetime(2026, 3, 9, 20, tzinfo=UTC)),
    (SessionPolicy(mode=SessionPolicyMode.CUSTOM, custom_windows=(("09:30", "12:00"), ("13:00", "16:00"))),
     datetime(2026, 3, 9, 16, tzinfo=UTC)),
])
def test_strategy_supervision_cancels_only_entries_at_configured_boundary(policy, when, cancel_at_end, monkeypatch):
    monkeypatch.setattr("quant.strategies.ml_strategy.broker_connectivity.snapshot", lambda *args: {"healthy": True})
    from types import SimpleNamespace
    from quant.strategies.ml_strategy import MLStrategy
    from quant.strategies.execution_state import (
        ExecutionLedger, ExecutionSafetyController, LifecycleStatus, OrderRole,
    )

    ledger = ExecutionLedger()
    orders = {}
    for order_id, role in [("entry", OrderRole.ENTRY), ("stop", OrderRole.STOP_LOSS)]:
        ledger.register_order(client_order_id=order_id, instrument_id="QQQ.SMART",
                              side="BUY", requested_quantity="1", role=role,
                              signal_version="test", ts_ns=1)
        ledger.apply_order_state(order_id, LifecycleStatus.ACKNOWLEDGED, event_id=order_id, ts_ns=2)
        orders[order_id] = SimpleNamespace(is_closed=False)
    canceled = []

    def cancel(order, record, *, reason):
        canceled.append(record.client_order_id)
        ledger.apply_order_state(record.client_order_id, LifecycleStatus.PENDING_CANCEL,
                                 event_id="cancel", ts_ns=3)
        return True

    strategy = SimpleNamespace(
        config=SimpleNamespace(execution_mode="paper", account_id="IB-DU1", asset_class="equity", startup_health_grace_secs=30,
                               cancel_entries_at_session_end=cancel_at_end, max_gross_exposure_pct=1),
        _risk=RiskManager(5000), _account=lambda: SimpleNamespace(balance_total=lambda: 5000, base_currency="USD"),
        _equity=lambda: 5000, _session_calendars={"QQQ.SMART": _calendar(policy)},
        _execution_safety=ExecutionSafetyController(), _started_at=None,
        _last_session_phase={}, _execution=ledger,
        clock=SimpleNamespace(timestamp_ns=lambda: 3),
        cache=SimpleNamespace(order=lambda key: orders[str(key)]),
        _cancel_order_safely=cancel, log=SimpleNamespace(warning=lambda message: None),
        _supervise_short_positions=lambda when: None, _reconcile_committed_notional=lambda: None,
        _committed_notional={}, _audit_safety_state_if_changed=lambda: None,
        _sector_risk_snapshot=lambda: {"healthy": True, "status": "CURRENT"},
    )
    strategy._cancel_working_entry_orders = lambda *args, **kwargs: MLStrategy._cancel_working_entry_orders(strategy, *args, **kwargs)
    MLStrategy._supervise_risk(strategy, when)
    MLStrategy._supervise_risk(strategy, when)
    assert canceled == (["entry"] if cancel_at_end else [])
    assert ledger.orders["stop"].status == LifecycleStatus.ACKNOWLEDGED
