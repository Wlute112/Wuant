from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError
from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.backtest.models import FillModel, LatencyModel
from nautilus_trader.config import LoggingConfig, StrategyConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import AccountType, BookType, OmsType, OrderSide, TimeInForce
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.trading.strategy import Strategy

from quant.run.backtest_common import VENUE, make_equity, _bars_from_df, replay_batches
from quant.run.equity_simulation import EquitySimulation, EquitySimulationConfig, EquityFeeModel
from quant.run.scoring import sharpe_from_curve


class ScriptConfig(StrategyConfig, frozen=True):
    backtest_trade_start_ns: int = 0


class Script(Strategy):
    def __init__(self, runtime, instrument, bars, side=OrderSide.BUY, size=10, limit=False):
        super().__init__(ScriptConfig())
        self.runtime, self.instrument, self.bars = runtime, instrument, bars
        self.side, self.size, self.limit = side, size, limit
        self.count = 0
        self.splits = []

    def on_start(self):
        self.subscribe_bars(self.bars[0].bar_type)

    def on_bar(self, bar):
        if self.count == 0:
            values = dict(instrument_id=self.instrument.id, order_side=self.side, quantity=Quantity.from_int(self.size))
            order = self.order_factory.limit(**values, price=Price(200, 2), time_in_force=TimeInForce.GTC) if self.limit else self.order_factory.market(**values)
            self.submit_order(order)
        self.count += 1

    def _apply_research_split(self, iid, ratio):
        self.splits.append(ratio)


def replay(prices=(100, 110, 120), volume=100000, *, config=None, size=10, side=OrderSide.BUY, limit=False,
           start="2026-01-02", streaming=False, cost_multiplier=1):
    runtime = EquitySimulation({"spread_bps": 0, "impact_bps": 0, "borrow_rate": 0, **(config or {})}, cost_multiplier)
    inst = make_equity("ABC")
    frame = pd.DataFrame([dict(timestamp=ts, ticker="ABC", open=px, high=px, low=px, close=px, volume=volume)
        for ts, px in zip(pd.date_range(start, periods=len(prices), freq="B", tz="UTC"), prices)])
    bars = _bars_from_df(frame, inst, "-1-DAY-LAST-EXTERNAL")
    engine = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    engine.add_venue(VENUE, oms_type=OmsType.NETTING, account_type=AccountType.MARGIN, base_currency=USD,
        starting_balances=[Money(5000, USD)], book_type=BookType.L2_MBP, bar_execution=False,
        liquidity_consumption=True, fill_model=FillModel(), fee_model=EquityFeeModel(runtime), modules=[runtime],
        latency_model=LatencyModel(base_latency_nanos=0, insert_latency_nanos=1))
    engine.add_instrument(inst)
    strategy = Script(runtime, inst, bars, side, size, limit)
    runtime.strategy = strategy
    engine.add_strategy(strategy)
    stream = runtime.replay(bars)
    if streaming:
        for batch in replay_batches(stream, 1):
            engine.add_data(batch)
            engine.run(streaming=True)
            engine.clear_data()
        engine.end()
    else:
        engine.add_data(stream)
        engine.run()
    return engine, runtime


def test_market_fills_next_open_and_equity_includes_unrealized_pnl():
    engine, runtime = replay()
    try:
        fills = engine.trader.generate_order_fills_report()
        assert float(fills.iloc[0].avg_px) == 110
        assert runtime.curve[-1]["equity"] == pytest.approx(5099)
        assert runtime.commissions == 1
    finally:
        engine.dispose()


def test_zero_volume_never_fabricates_liquidity():
    engine, runtime = replay(volume=0)
    try:
        assert engine.trader.generate_order_fills_report().empty
        assert runtime.curve[-1]["equity"] == 5000
    finally:
        engine.dispose()


def test_shared_volume_caps_partial_fills_and_minimum_is_not_repeated():
    engine, runtime = replay(volume=1600, size=100, limit=True)
    try:
        orders = engine.trader.generate_order_fills_report()
        assert 0 < float(orders.iloc[0].filled_qty) <= 16
        assert runtime.commissions == 1
    finally:
        engine.dispose()


def test_split_rebases_quantity_and_basis_without_inventing_pnl():
    engine, runtime = replay(prices=(100, 100, 50, 55), config={"price_basis": "raw", "corporate_actions": [{
        "event_id": "split", "ticker": "ABC", "kind": "SPLIT", "effective_at": "2026-01-06T00:00:00Z",
        "split_ratio": 2, "source": "issuer split notice"}]})
    try:
        pos = engine.cache.positions_open()[0]
        assert pos.signed_qty == 20
        assert pos.avg_px_open == 50
        assert runtime.curve[2]["equity"] == pytest.approx(4999)
        assert runtime.curve[-1]["equity"] == pytest.approx(5099)
        assert len(runtime.actions) == 1
    finally:
        engine.dispose()


@pytest.mark.parametrize("side,expected", [(OrderSide.BUY, 10), (OrderSide.SELL, -10)])
def test_dividend_entitlement_survives_until_pay_date(side, expected):
    engine, runtime = replay(prices=(100, 100, 99, 99), side=side, config={"corporate_actions": [{
        "event_id": "dividend", "ticker": "ABC", "kind": "CASH_DIVIDEND",
        "effective_at": "2026-01-06T00:00:00Z", "pay_at": "2026-01-07T00:00:00Z",
        "cash_per_share": 1, "source": "issuer dividend notice"}]})
    try:
        assert runtime.actions[0]["entitlement_usd"] == expected
        assert runtime.curve[2]["equity"] == pytest.approx(4999)
        assert runtime.curve[-1]["equity"] == pytest.approx(4999)
    finally:
        engine.dispose()


def test_calibration_cannot_see_future_and_adjusted_prices_reject_splits():
    with pytest.raises(ValidationError):
        EquitySimulationConfig(calibrations=[dict(source="test evidence", observed_through="2026-01-02T00:00:00Z", effective_at="2026-01-01T00:00:00Z")])
    cfg = EquitySimulationConfig(calibrations=[dict(source="test evidence", observed_through="2026-01-01T00:00:00Z", effective_at="2026-01-02T00:00:00Z", spread_bps=20)])
    assert cfg.at("ABC", pd.Timestamp("2026-01-01", tz="UTC").value).spread_bps == 5
    assert cfg.at("ABC", pd.Timestamp("2026-01-02", tz="UTC").value).spread_bps == 20
    with pytest.raises(ValidationError):
        EquitySimulationConfig(corporate_actions=[dict(event_id="x", ticker="ABC", kind="SPLIT", effective_at="2026-01-02T00:00:00Z", split_ratio=2, source="issuer notice")])


def test_excess_sharpe_uses_interval_matched_risk_free_returns():
    curve = np.array([100, 102, 101, 104])
    ts = np.arange(4) * 86400
    assert sharpe_from_curve(curve, ts, "equity", [0.01] * 3) < sharpe_from_curve(curve, ts, "equity")
    with pytest.raises(ValueError):
        sharpe_from_curve(curve, ts, "equity", [0.01])


def test_streaming_replay_matches_one_shot_across_auction_boundaries():
    one_shot, expected = replay(volume=1600, size=100, limit=True)
    streamed, actual = replay(volume=1600, size=100, limit=True, streaming=True)
    try:
        assert actual.curve == expected.curve
        assert actual.cashflows == expected.cashflows
        assert actual.commissions == expected.commissions
        assert streamed.cache.positions_open()[0].signed_qty == one_shot.cache.positions_open()[0].signed_qty
    finally:
        one_shot.dispose()
        streamed.dispose()


def test_unrealized_gains_do_not_change_cash_financing():
    config = {"cash_credit_rate": 0.36, "commission_per_share": 0, "minimum_commission": 0}
    flat_engine, flat = replay(prices=(100, 100, 100, 100), config=config)
    rising_engine, rising = replay(prices=(100, 100, 200, 200), config=config)
    try:
        assert rising.cashflows == flat.cashflows
        assert rising.curve[-1]["equity"] - flat.curve[-1]["equity"] == pytest.approx(1000)
    finally:
        flat_engine.dispose()
        rising_engine.dispose()


@pytest.mark.parametrize("multiplier", [1, 2])
def test_short_borrow_accrues_over_weekend_and_is_stressed(multiplier):
    engine, runtime = replay(prices=(100, 100, 100), start="2026-01-08", side=OrderSide.SELL,
                             config={"borrow_rate": 0.36}, cost_multiplier=multiplier)
    try:
        borrow = [flow for flow in runtime.cashflows if flow["kind"] == "BORROW"]
        assert sum(flow["amount"] for flow in borrow) == pytest.approx(-3.06 * multiplier)
        assert runtime.commissions == multiplier
    finally:
        engine.dispose()


def test_calibration_coverage_does_not_claim_uncovered_history():
    engine, runtime = replay(config={"calibrations": [{
        "ticker": "ABC", "source": "measured historical sample",
        "observed_through": "2026-01-03T00:00:00Z", "effective_at": "2026-01-04T00:00:00Z",
    }]})
    try:
        report = runtime.report()
        assert report["data_coverage"]["calibrated_bars"] == 2
        assert report["calibration_status"].startswith("partial")
    finally:
        engine.dispose()


@pytest.mark.parametrize("ratio,quantity", [(0.25, 2), (0.05, 0)])
@pytest.mark.parametrize("side", [OrderSide.BUY, OrderSide.SELL])
def test_reverse_split_cash_in_lieu_preserves_equity(ratio, quantity, side):
    engine, runtime = replay(prices=(100, 100, 100 / ratio), side=side, config={
        "price_basis": "raw", "corporate_actions": [{
            "event_id": "reverse", "ticker": "ABC", "kind": "SPLIT",
            "effective_at": "2026-01-06T00:00:00Z", "split_ratio": ratio, "source": "issuer notice",
        }],
    })
    try:
        assert sum(abs(pos.signed_qty) for pos in engine.cache.positions_open()) == quantity
        assert runtime.curve[-1]["equity"] == pytest.approx(4999)
    finally:
        engine.dispose()


def test_regulatory_fees_are_sell_specific_and_stressed():
    config = {"sec_sell_rate": 0.001, "taf_per_share": 0.01, "taf_cap": 1, "cat_per_share": 0.005}
    buy_engine, buy = replay(config=config, cost_multiplier=2)
    sell_engine, sell = replay(config=config, side=OrderSide.SELL, cost_multiplier=2)
    try:
        assert buy.commissions == pytest.approx(2.10)
        assert sell.commissions == pytest.approx(4.50)
    finally:
        buy_engine.dispose()
        sell_engine.dispose()
