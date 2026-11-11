"""Economic invariants exercised through real Nautilus order matching."""
import pandas as pd
import pytest

from quant.run import backtest_common
from quant.run.asset_profiles import strategy_defaults_for_asset
from quant.run.metrics import compute_metrics
from quant.strategies.ml_strategy import MLStrategy, MLStrategyConfig
from quant.models.prediction_engine import PredictionEngine
from quant.tests.test_equity_simulation import replay


def test_fee_stress_can_preserve_fill_count_while_reducing_native_equity():
    one, normal = replay(prices=(100, 110, 120), cost_multiplier=1)
    two, stress = replay(prices=(100, 110, 120), cost_multiplier=2)
    try:
        assert len(one.trader.generate_fills_report()) == len(two.trader.generate_fills_report()) == 1
        assert normal.commissions == 1 and stress.commissions == 2
        assert normal.equity() - stress.equity() == pytest.approx(1)
    finally:
        one.dispose()
        two.dispose()


def test_spread_stress_changes_execution_when_a_limit_is_marginal():
    one, normal = replay(prices=(100, 100, 100), limit=True, limit_price=100.04,
                         config={'spread_bps': 5}, cost_multiplier=1)
    two, stress = replay(prices=(100, 100, 100), limit=True, limit_price=100.04,
                         config={'spread_bps': 5}, cost_multiplier=2)
    try:
        assert len(one.trader.generate_order_fills_report()) == 1
        assert two.trader.generate_order_fills_report().empty
    finally:
        one.dispose()
        two.dispose()


def test_partial_fill_turnover_uses_executed_not_requested_notional():
    engine, runtime = replay(volume=1600, size=100, limit=True)
    try:
        report = engine.trader.generate_order_fills_report()
        assert float(report.iloc[0].filled_qty) < float(report.iloc[0].quantity)
        actual_notional = sum(float(row.last_qty) * float(row.last_px)
                              for row in engine.trader.generate_fills_report().itertuples())
        expected = round(actual_notional / ((5000 + runtime.equity()) / 2), 2)
        assert compute_metrics(engine, backtest_common.VENUE, 5000, 'equity').turnover_rate == expected
    finally:
        engine.dispose()


def test_research_default_matches_long_only_execution_policy():
    assert MLStrategyConfig(instrument_ids=['AAA.IBKR']).allow_short_positions is False
    assert strategy_defaults_for_asset('equity')['allow_short_positions'] is False
    assert strategy_defaults_for_asset('crypto')['allow_short_positions'] is False


@pytest.mark.parametrize('hour', [0, 21])
def test_costs_cross_daily_loss_limit_before_day_baseline_can_reset(tmp_path, monkeypatch, hour):
    class AuditStrategy(MLStrategy):
        def __init__(self, config):
            super().__init__(config)
            self.states = []

        def on_bar(self, bar):
            super().on_bar(bar)
            self.states.append((len(self._closes[bar.bar_type.instrument_id]), self._risk.state.value,
                                self._risk.telemetry(self._equity())['daily_pnl_pct']))

    monkeypatch.setattr(backtest_common, 'MLStrategy', AuditStrategy)
    monkeypatch.setattr(PredictionEngine, 'refit_on_history', lambda *a, **kw: True)
    monkeypatch.setattr(PredictionEngine, 'predict_move', lambda *a, **kw: .01)
    timestamps = pd.bdate_range('2026-01-05', periods=6, tz='UTC') + pd.Timedelta(hours=hour)
    path = tmp_path / 'daily_cost_fixture.csv'
    pd.DataFrame([dict(timestamp=ts, ticker='AAA', open=100, high=101, low=99, close=100,
                       volume=100000, source='execution_test', price_basis='synthetic_unadjusted',
                       volume_basis='synthetic', session='RTH') for ts in timestamps]).to_csv(path, index=False)
    outcomes = []
    for multiplier in (1, 2):
        engine = backtest_common.build_and_run(str(path), ['AAA'], starting_cash=5000,
            asset_class='equity', bypass_logging=True, cost_multiplier=multiplier,
            strategy_overrides={'allow_short_positions': False, 'warmup_bars': 4,
            'min_train_bars': 3, 'atr_period': 3, 'atr_stop_mult': 3.123,
            'daily_loss_limit_pct': .0003, 'use_limit_orders': False,
            'equity_simulation': {'spread_bps': 0, 'impact_bps': 0}})
        try:
            outcomes.append(next(row for row in engine.trader.strategies()[0].states if row[0] == 5))
        finally:
            engine.dispose()
    assert outcomes[0][1] == 'ACTIVE'
    assert outcomes[1][1] == 'HALTED_DAILY'
