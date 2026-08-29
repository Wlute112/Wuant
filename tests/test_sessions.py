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
