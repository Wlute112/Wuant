from dataclasses import replace
from io import BytesIO
import time
from types import SimpleNamespace

import quant.run.short_controls as short_controls
from quant.run.short_controls import (
    AccountSnapshot,
    BorrowSnapshot,
    IBKRShortControlService,
    ShortControlConfig,
    WhatIfResult,
    evaluate_short_snapshot,
)
from quant.strategies.ml_strategy import MLStrategy


def _config(**overrides):
    base = ShortControlConfig(account_id="DU123", tickers=("QQQ",))
    return replace(base, **overrides)


def _borrow(now=100.0, **overrides):
    values = {
        "symbol": "QQQ",
        "observed_at": now,
        "fee_observed_at": now,
        "shortable_tier": 3.0,
        "shortable_shares": 10_000,
        "borrow_fee_pct": 0.75,
        "bid": 500.0,
        "ask": 500.1,
        "last": 500.0,
        "prior_close": 505.0,
        "session_low": 498.0,
        "halted": False,
        "prior_session_ssr_triggered": False,
        "ssr_history_observed_at": now,
    }
    values.update(overrides)
    return BorrowSnapshot(**values)


def _account(now=100.0, **overrides):
    values = {
        "Cushion": 0.50,
        "AvailableFunds": 50_000,
        "ExcessLiquidity": 50_000,
        "BuyingPower": 100_000,
        "DayTradesRemaining": 3,
        "NetLiquidation": 100_000,
    }
    values.update(overrides)
    return AccountSnapshot("DU123", now, values)


def test_short_snapshot_requires_fresh_locate_fee_margin_and_capacity():
    decision = evaluate_short_snapshot(
        _config(),
        _borrow(),
        _account(),
        quantity=10,
        reference_price=500,
        now=100,
    )
    assert decision.allowed
    assert decision.code == "SNAPSHOT_APPROVED"


def test_short_snapshot_rejects_nonfinite_or_nonpositive_order_inputs():
    invalid_quantity = evaluate_short_snapshot(
        _config(),
        _borrow(),
        _account(),
        quantity=float("nan"),
        reference_price=500,
        now=100,
    )
    invalid_price = evaluate_short_snapshot(
        _config(),
        _borrow(),
        _account(),
        quantity=1,
        reference_price=0,
        now=100,
    )
    assert invalid_quantity.code == "INVALID_REQUEST"
    assert invalid_price.code == "INVALID_REQUEST"


def test_short_snapshot_rejects_locate_and_fee_failures():
    locate = evaluate_short_snapshot(
        _config(),
        _borrow(shortable_shares=10),
        _account(),
        quantity=10,
        reference_price=500,
        now=100,
    )
    fee = evaluate_short_snapshot(
        _config(max_borrow_fee_pct=2),
        _borrow(borrow_fee_pct=2.1),
        _account(),
        quantity=10,
        reference_price=500,
        now=100,
    )
    assert locate.code == "BORROW_INSUFFICIENT"
    assert fee.code == "BORROW_FEE_LIMIT"


def test_short_snapshot_rejects_stale_data_low_margin_and_pdt_limit():
    stale = evaluate_short_snapshot(
        _config(snapshot_max_age_secs=20),
        _borrow(now=50),
        _account(),
        quantity=1,
        reference_price=500,
        now=100,
    )
    margin = evaluate_short_snapshot(
        _config(),
        _borrow(),
        _account(Cushion=0.1),
        quantity=1,
        reference_price=500,
        now=100,
    )
    pdt = evaluate_short_snapshot(
        _config(),
        _borrow(),
        _account(DayTradesRemaining=0, NetLiquidation=20_000),
        quantity=1,
        reference_price=500,
        now=100,
    )
    assert stale.code == "BORROW_STALE"
    assert margin.code == "MARGIN_CUSHION"
    assert pdt.code == "PDT_LIMIT"


def test_short_snapshot_requires_pdt_capacity_below_25k():
    decision = evaluate_short_snapshot(
        _config(),
        _borrow(),
        _account(DayTradesRemaining="N/A", NetLiquidation=20_000),
        quantity=1,
        reference_price=500,
        now=100,
    )
    assert decision.code == "PDT_UNKNOWN"


def test_rule_201_requires_current_quote_and_raises_short_limit_to_ask():
    active = evaluate_short_snapshot(
        _config(),
        _borrow(prior_close=500, session_low=449, bid=451, ask=451.25),
        _account(),
        quantity=1,
        reference_price=451,
        now=100,
    )
    missing = evaluate_short_snapshot(
        _config(),
        _borrow(prior_close=500, session_low=449, bid=None, ask=None),
        _account(),
        quantity=1,
        reference_price=451,
        now=100,
    )
    assert active.allowed and active.ssr_active and active.limit_price == 451.25
    assert missing.code == "SSR_QUOTE_UNAVAILABLE"

    locked = evaluate_short_snapshot(
        _config(),
        _borrow(prior_close=500, session_low=449, bid=451, ask=451),
        _account(),
        quantity=1,
        reference_price=451,
        now=100,
    )
    assert locked.code == "SSR_QUOTE_UNAVAILABLE"


def test_short_snapshot_fails_closed_when_rule_201_state_is_unknown():
    decision = evaluate_short_snapshot(
        _config(),
        _borrow(prior_close=None, session_low=None, last=None),
        _account(),
        quantity=1,
        reference_price=500,
        now=100,
    )
    assert decision.code == "SSR_STATE_UNKNOWN"


def test_rule_201_remains_active_after_a_prior_session_trigger():
    decision = evaluate_short_snapshot(
        _config(),
        _borrow(
            prior_close=500,
            session_low=495,
            last=499,
            prior_session_ssr_triggered=True,
        ),
        _account(),
        quantity=1,
        reference_price=499,
        now=100,
    )
    assert decision.allowed
    assert decision.ssr_active
    assert decision.limit_price == 500.1


def test_ibkr_ftp_feed_is_matched_by_contract_id(monkeypatch):
    payload = b"\n".join(
        [
            b"#BOF|20260901|",
            b"#SYM|CUR|NAME|CON|ISIN|REBATERATE|FEERATE|AVAILABLE|FIGI|",
            b"QQQ|USD|INVESCO QQQ TRUST|320227571|US46090E1038|-0.10|0.35|125000|BBG000BSWKH7|",
            b"#EOF|1|",
        ]
    )

    class _Response(BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    monkeypatch.setattr(short_controls, "urlopen", lambda *_args, **_kwargs: _Response(payload))
    service = IBKRShortControlService(_config())
    service._refresh_borrow_fees_from_ftp({"QQQ": 320227571})

    snapshot = service._borrow["QQQ"]
    assert snapshot.borrow_fee_pct == 0.35
    assert snapshot.shortable_shares == 125_000
    assert snapshot.fee_observed_at > 0


def test_prior_session_rule_201_trigger_is_derived_from_completed_daily_bars():
    service = IBKRShortControlService(_config())
    service._history_requests[9_500] = "QQQ"
    service._history_rows[9_500] = [
        ("20260827", 100.0, 100.0),
        ("20260828", 89.5, 95.0),
    ]

    service._historical_end(9_500)

    snapshot = service._borrow["QQQ"]
    assert snapshot.prior_session_ssr_triggered is True
    assert snapshot.ssr_history_observed_at > 0


def test_preflight_requires_passing_order_specific_what_if(monkeypatch):
    service = IBKRShortControlService(_config())
    now = time.time()
    service._borrow["QQQ"] = _borrow(now=now)
    service._account = _account(now=now)
    monkeypatch.setattr(service._probe, "isConnected", lambda: True)
    monkeypatch.setattr(
        service,
        "_request_what_if",
        lambda *_args: WhatIfResult(
            allowed=True,
            status="PreSubmitted",
            init_margin_after=55_000,
            maint_margin_after=50_000,
            equity_with_loan_after=100_000,
            commission=1.0,
        ),
    )

    decision = service.preflight(
        "QQQ",
        quantity=10,
        reference_price=500,
        proposed_limit_price=None,
    )

    assert decision.allowed
    assert decision.code == "SHORT_APPROVED"
    assert decision.limit_price == 500


def test_preflight_rejects_low_projected_margin_cushion(monkeypatch):
    service = IBKRShortControlService(_config())
    now = time.time()
    service._borrow["QQQ"] = _borrow(now=now)
    service._account = _account(now=now)
    monkeypatch.setattr(service._probe, "isConnected", lambda: True)
    monkeypatch.setattr(
        service,
        "_request_what_if",
        lambda *_args: WhatIfResult(
            allowed=True,
            status="PreSubmitted",
            init_margin_after=95_000,
            maint_margin_after=90_000,
            equity_with_loan_after=100_000,
            commission=1.0,
        ),
    )

    decision = service.preflight(
        "QQQ",
        quantity=10,
        reference_price=500,
        proposed_limit_price=None,
    )

    assert decision.code == "BROKER_MARGIN_CUSHION"


def test_existing_short_supervision_detects_inventory_deterioration(monkeypatch):
    service = IBKRShortControlService(_config())
    now = time.time()
    service._borrow["QQQ"] = _borrow(now=now, shortable_shares=5)
    service._account = _account(now=now)
    monkeypatch.setattr(service._probe, "isConnected", lambda: True)

    decision = service.supervise("QQQ", quantity=10)

    assert not decision.allowed
    assert decision.code == "BORROW_INSUFFICIENT"


def test_strategy_short_control_attachment_has_no_lifecycle_side_effects():
    target = SimpleNamespace(_short_control=None)
    controller = object()

    MLStrategy.attach_short_control(target, controller)

    assert target._short_control is controller
