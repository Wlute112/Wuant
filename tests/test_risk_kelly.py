"""Unit tests for fractional-Kelly conviction sizing (RiskManager).

These are pure-Python (no Nautilus engine): they exercise the sizing math and
the invariant the user asked for -- Kelly may de-risk BELOW the hard 0.25% cap
but can NEVER breach it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from quant.strategies.risk import RiskConfig, RiskManager, TradingState


ENTRY = 100.0
STOP = 98.0  # risk_per_unit = 2.0


def _rm(**cfg) -> RiskManager:
    return RiskManager(5000.0, RiskConfig(**cfg))


def test_kelly_never_exceeds_hard_cap():
    # Arrange: a huge edge would want a massive position...
    rm = _rm()
    cap_qty = rm.size_for_trade(5000.0, ENTRY, STOP)

    # Act
    kelly_qty = rm.kelly_size_for_trade(
        5000.0, ENTRY, STOP, edge=0.10, variance=1e-6, kelly_fraction=1.0
    )

    # Assert: Kelly saturates at the 0.25% cap, never above it.
    assert kelly_qty == pytest.approx(cap_qty)


def test_weak_edge_sizes_below_cap():
    # Arrange
    rm = _rm()
    cap_qty = rm.size_for_trade(5000.0, ENTRY, STOP)

    # Act: small edge, high variance -> tiny Kelly fraction.
    kelly_qty = rm.kelly_size_for_trade(
        5000.0, ENTRY, STOP, edge=0.0005, variance=0.01, kelly_fraction=0.5
    )

    # Assert: strictly de-risked below the cap, still positive.
    assert 0.0 < kelly_qty < cap_qty


def test_kelly_fraction_scales_size_monotonically():
    rm = _rm()
    common = dict(edge=0.001, variance=0.01)
    small = rm.kelly_size_for_trade(5000.0, ENTRY, STOP, kelly_fraction=0.1, **common)
    large = rm.kelly_size_for_trade(5000.0, ENTRY, STOP, kelly_fraction=0.9, **common)
    assert large > small > 0.0


def test_kelly_max_fraction_ceiling_binds_before_cap():
    # With a very close stop the 0.25% risk cap allows a large notional, so the
    # kelly_max_fraction notional ceiling is the binding safety rail instead.
    rm = _rm(kelly_max_fraction=0.5)
    entry, stop = 100.0, 99.9999  # risk_per_unit ~ 1e-4 -> cap qty is enormous
    kelly_qty = rm.kelly_size_for_trade(
        5000.0, entry, stop, edge=0.10, variance=1e-6, kelly_fraction=1.0
    )
    # Ceiling: f <= 0.5 -> notional <= 0.5 * 5000 = 2500 -> qty <= 25.
    assert kelly_qty == pytest.approx(25.0)


@pytest.mark.parametrize("edge,var", [(0.0, 0.01), (-0.01, 0.01), (0.01, 0.0)])
def test_unusable_inputs_fall_back_to_cap(edge, var):
    # Non-positive edge/variance -> defer to the flat hard-cap sizing, never up.
    rm = _rm()
    cap_qty = rm.size_for_trade(5000.0, ENTRY, STOP)
    kelly_qty = rm.kelly_size_for_trade(
        5000.0, ENTRY, STOP, edge=edge, variance=var, kelly_fraction=0.5
    )
    assert kelly_qty == pytest.approx(cap_qty)


def test_kelly_returns_zero_when_halted():
    rm = _rm()
    rm.state = TradingState.DISABLED_KILL
    kelly_qty = rm.kelly_size_for_trade(
        5000.0, ENTRY, STOP, edge=0.01, variance=0.001, kelly_fraction=0.5
    )
    assert kelly_qty == 0.0


# --- portfolio-level gross-exposure guard (available_notional) --------------

def test_available_notional_caps_qty_below_percent_cap():
    # With a very close stop the 0.25% risk cap would allow a big notional, but
    # only $50 of book headroom remains -> qty capped at 50/entry = 0.5.
    rm = _rm()
    qty = rm.size_for_trade(5000.0, 100.0, 99.99, available_notional=50.0)
    assert qty == pytest.approx(0.5)


def test_zero_headroom_blocks_trade():
    rm = _rm()
    assert rm.size_for_trade(5000.0, ENTRY, STOP, available_notional=0.0) == 0.0


def test_ample_headroom_leaves_percent_cap_binding():
    # Headroom far exceeds the 0.25% cap qty -> the cap (6.25) still binds.
    rm = _rm()
    capped = rm.size_for_trade(5000.0, ENTRY, STOP)
    with_room = rm.size_for_trade(5000.0, ENTRY, STOP, available_notional=1e9)
    assert with_room == pytest.approx(capped)


def test_kelly_respects_available_notional():
    # Strong edge would saturate the per-trade cap, but headroom binds lower.
    rm = _rm()
    qty = rm.kelly_size_for_trade(
        5000.0, 100.0, 99.99, edge=0.10, variance=1e-6,
        kelly_fraction=1.0, available_notional=40.0,
    )
    assert qty == pytest.approx(0.4)


def test_risk_state_round_trip_preserves_kill_switch():
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    original = RiskManager(5000.0)
    original.on_new_day(now, 5000.0)
    assert original.update_equity(now, 4400.0) == TradingState.DISABLED_KILL

    restored = RiskManager(9999.0)
    restored.restore(original.snapshot())

    assert restored.state == TradingState.DISABLED_KILL
    assert restored.can_open is False
    assert restored.peak_equity == 5000.0


def test_risk_state_round_trip_preserves_daily_halt_deadline():
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    original = RiskManager(5000.0)
    original.on_new_day(now, 5000.0)
    assert original.update_equity(now, 4899.0) == TradingState.HALTED_DAILY

    restored = RiskManager(5000.0)
    restored.restore(original.snapshot())
    restored.on_new_day(now + timedelta(hours=23), 5000.0)
    assert restored.state == TradingState.HALTED_DAILY
    restored.on_new_day(now + timedelta(hours=25), 5000.0)
    assert restored.state == TradingState.ACTIVE
