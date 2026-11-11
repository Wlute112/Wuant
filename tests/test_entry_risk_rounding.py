from collections import defaultdict
from types import SimpleNamespace

import pytest
from nautilus_trader.model.enums import OrderSide, TimeInForce

from quant.run.backtest_common import make_equity
from quant.strategies.execution_state import ExecutionSafetyController
from quant.strategies.ml_strategy import MLStrategy, MLStrategyConfig
from quant.strategies.risk import RiskManager


def test_whole_share_rounding_cannot_increase_an_entry_above_its_risk_budget():
    instrument = make_equity('JPM')
    config = MLStrategyConfig(instrument_ids=[str(instrument.id)], enforce_portfolio_leverage=False)
    # $12.50 maximum risk / $20 stop distance permits only 0.625 shares.
    # Nearest rounding would incorrectly buy one; flooring must skip entry.
    obj = SimpleNamespace(config=config, _risk=RiskManager(5000),
        _execution_safety=ExecutionSafetyController(), _pending_exits={},
        cache=SimpleNamespace(instrument=lambda iid: instrument),
        _committed_notional=defaultdict(float), _sector_by_iid={},
        _last_mark={instrument.id: 300}, _pending_ts=None,
        _entry_tif=lambda: TimeInForce.GTC,
        order_factory=SimpleNamespace(market=lambda **kwargs: SimpleNamespace(**kwargs)),
        _register_and_submit_order=lambda *args, **kwargs: pytest.fail('Entry exceeded the risk-sized quantity'))
    MLStrategy._enter(obj, instrument.id, OrderSide.BUY, 300, 20 / config.atr_stop_mult, 5000, .01)
