"""Exercise risk exits against the actual queued, finite-depth exchange."""
from datetime import datetime, timezone

import pandas as pd
import pytest
from nautilus_trader.model.enums import OrderSide, TimeInForce

from quant.run import backtest_common
from quant.run.backtest_common import replay_batches
from quant.strategies.execution_state import ExecutionSafetyState, LifecycleStatus, OrderRole
from quant.strategies.ml_strategy import MLStrategy
from quant.strategies.risk import TradingState


class LiquidationScript(MLStrategy):
    """Replace model signals with deterministic entries; keep execution real."""

    permanent = True
    trigger = "risk"
    entry_side = OrderSide.BUY

    def __init__(self, config):
        super().__init__(config)
        self.started_entries = set()
        self.exit_observations = []
        self.submissions = []

    def _register_and_submit_order(self, order, **kwargs):
        self.submissions.append((self.clock.timestamp_ns(), order, kwargs["role"]))
        return super()._register_and_submit_order(order, **kwargs)

    def on_bar(self, bar):
        super().on_bar(bar)
        iid = bar.bar_type.instrument_id
        if iid not in self.started_entries:
            self.started_entries.add(iid)
            order = self.order_factory.limit(
                instrument_id=iid, order_side=self.entry_side,
                quantity=self.cache.instrument(iid).make_qty(5),
                price=self.cache.instrument(iid).make_price(100),
                time_in_force=TimeInForce.GTC,
            )
            self._register_and_submit_order(order, role=OrderRole.ENTRY)
            self._position_references[iid] = {"atr": 10.0}
        elif self.trigger == "manual" and len(self._closes[iid]) == 2:
            self._begin_risk_exit("test liquidation", permanent=self.permanent)
        if self._pending_exits:
            # Repeated equal-time checks reproduce the production failure.
            for _ in range(4):
                self._supervise_risk(datetime.fromtimestamp(bar.ts_event / 1e9, timezone.utc))
            self.exit_observations.append({
                "pending": set(self._pending_exits),
                "gross": sum(self._committed_notional.values()),
                "net": sum(abs(float(self.portfolio.net_position(i) or 0)) for i in self._bar_types),
            })


def run_script(tmp_path, monkeypatch, *, volumes=(100000,) * 6, streaming=False,
               permanent=True, trigger="risk", entry_side=OrderSide.BUY):
    class Script(LiquidationScript):
        pass
    Script.permanent, Script.trigger = permanent, trigger
    Script.entry_side = entry_side
    monkeypatch.setattr(backtest_common, "MLStrategy", Script)
    # Test only execution here; observed-data preflight has its own suite.
    monkeypatch.setattr("quant.data.research_preflight.require_execution_data", lambda report: report)
    path = tmp_path / "bars.csv"
    rows = [dict(timestamp=ts, ticker=ticker, open=100, high=100, low=100, close=100, volume=volume)
            for ts, volume in zip(pd.bdate_range("2026-01-05", periods=len(volumes), tz="UTC"), volumes)
            for ticker in ("AAA", "BBB", "CCC")]
    pd.DataFrame(rows).to_csv(path, index=False)
    engine, events = backtest_common.build_engine(
        str(path), ["AAA", "BBB", "CCC"], asset_class="equity", bypass_logging=True,
        strategy_overrides={"warmup_bars": 150, "use_regime_features": False,
                            "use_hmm_feature": False,
                            "max_gross_exposure_pct": 0.25 if trigger == "risk" else 1.0,
                            "equity_simulation": {"spread_bps": 0, "impact_bps": 0}},
    )
    try:
        if streaming:
            for batch in replay_batches(events, 1):
                engine.add_data(batch)
                engine.run(streaming=True)
                engine.clear_data()
            engine.end()
        else:
            engine.add_data(events)
            engine.run()
        return engine, engine.trader.strategies()[0]
    except BaseException:
        engine.dispose()
        raise


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("entry_side", [OrderSide.BUY, OrderSide.SELL])
def test_gross_breach_cancels_protection_and_confirms_flat_without_duplicate_exits(tmp_path, monkeypatch, streaming, entry_side):
    engine, strategy = run_script(tmp_path, monkeypatch, streaming=streaming, entry_side=entry_side)
    try:
        assert strategy._risk.state == TradingState.DISABLED_KILL
        assert strategy._execution_safety.state == ExecutionSafetyState.EMERGENCY
        assert strategy.exit_observations
        assert any(o["net"] > 0 and o["gross"] > 0 for o in strategy.exit_observations)
        assert not engine.cache.positions_open()
        assert not engine.cache.orders_open()
        assert not engine.cache.orders_inflight()
        assert not strategy._pending_exits
        assert sum(strategy._committed_notional.values()) == 0
        exits = [(ts, order) for ts, order, role in strategy.submissions if role == OrderRole.EMERGENCY_EXIT]
        assert len(exits) == 3
        assert all(order.is_reduce_only for _, order in exits)
        assert all(order.side != entry_side for _, order in exits)
        protection = [r for r in strategy._execution.orders.values()
                      if r.role in {OrderRole.STOP_LOSS, OrderRole.TAKE_PROFIT}]
        assert len(protection) == 6
        assert all(r.status == LifecycleStatus.CANCELED for r in protection)
        assert not any(r.status in {LifecycleStatus.REJECTED, LifecycleStatus.DENIED}
                       for r in strategy._execution.orders.values())
    finally:
        engine.dispose()


def test_zero_depth_and_partial_exits_retain_pending_exposure_until_liquidity_returns(tmp_path, monkeypatch):
    engine, strategy = run_script(tmp_path, monkeypatch, volumes=(100000, 100000, 0, 800, 100000, 100000))
    try:
        assert not engine.cache.positions_open()
        assert not strategy._pending_exits
        assert sum(strategy._committed_notional.values()) == 0
        assert any(o["pending"] and o["net"] > 0 and o["gross"] > 0 for o in strategy.exit_observations)
        exits = [(ts, str(order.instrument_id), order) for ts, order, role in strategy.submissions
                 if role == OrderRole.EMERGENCY_EXIT]
        assert len(exits) > 3
        assert len({(ts, iid) for ts, iid, _ in exits}) == len(exits)
        assert all(order.is_reduce_only for _, _, order in exits)
        assert all(float(p.signed_qty) >= 0 for p in engine.cache.positions())
        exit_records = [r for r in strategy._execution.orders.values() if r.role == OrderRole.EMERGENCY_EXIT]
        assert any(0 < r.filled_quantity < r.requested_quantity for r in exit_records)
        assert not any(r.status == LifecycleStatus.REJECTED for r in exit_records)
    finally:
        engine.dispose()


def test_temporary_exit_completes_without_engaging_permanent_kill(tmp_path, monkeypatch):
    engine, strategy = run_script(tmp_path, monkeypatch, permanent=False, trigger="manual")
    try:
        assert strategy._risk.state == TradingState.ACTIVE
        assert strategy._execution_safety.state == ExecutionSafetyState.ACTIVE
        assert not strategy._pending_exits
        assert not engine.cache.positions_open()
        assert not engine.cache.orders_open()
        assert any(role == OrderRole.SIGNAL_EXIT for _, _, role in strategy.submissions)
    finally:
        engine.dispose()
