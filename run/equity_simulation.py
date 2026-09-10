"""Finite OHLC book replay and explicit equity accounting assumptions.

OHLC paths are scenarios, not reconstructed ticks. All four auction stages
precede the completed bar; signals from that bar can trade only the next book.
Costs/calibrations are immutable inline data, so fold workers need no live feed.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
import math
from typing import Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator
from nautilus_trader.backtest.config import SimulationModuleConfig
from nautilus_trader.backtest.models import FeeModel
from nautilus_trader.backtest.modules import SimulationModule
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import Bar, BookOrder, OrderBookDelta, OrderBookDeltas
from nautilus_trader.model.enums import BookAction, OrderSide, OmsType, PositionAdjustmentType
from nautilus_trader.model.events import OrderFilled, PositionAdjusted, PositionChanged, PositionClosed
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.model.position import Position


class Assumptions(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    spread_bps: float = Field(default=5, ge=0, le=1000)
    impact_bps: float = Field(default=5, ge=0, le=1000)
    participation: float = Field(default=0.01, gt=0, le=0.25)
    extended_liquidity: float = Field(default=0.25, ge=0, le=1)
    extended_spread_multiplier: float = Field(default=2, ge=1, le=20)
    commission_per_share: float = Field(default=0.005, ge=0, le=1)
    minimum_commission: float = Field(default=1, ge=0, le=100)
    commission_cap_pct: float = Field(default=0.01, gt=0, le=1)
    sec_sell_rate: float = Field(default=0, ge=0, le=0.01)
    taf_per_share: float = Field(default=0, ge=0, le=1)
    taf_cap: float = Field(default=0, ge=0, le=100)
    cat_per_share: float = Field(default=0, ge=0, le=1)
    borrow_rate: float = Field(default=0.03, ge=0, le=10)
    debit_rate: float = Field(default=0.06, ge=0, le=1)
    cash_credit_rate: float = Field(default=0, ge=0, le=1)
    risk_free_rate: float = Field(default=0, ge=0, le=1)


class Calibration(Assumptions):
    """Complete assumption snapshot, usable strictly after its evidence cutoff."""
    ticker: str = "*"
    observed_through: datetime
    effective_at: datetime
    source: str = Field(min_length=5, max_length=500)

    @model_validator(mode="after")
    def causal(self):
        if self.observed_through.tzinfo is None or self.effective_at.tzinfo is None:
            raise ValueError("Calibration dates require timezones")
        if self.observed_through >= self.effective_at:
            raise ValueError("Calibration evidence must end before it becomes effective")
        return self


class ResearchAction(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    event_id: str = Field(min_length=1, max_length=100)
    ticker: str
    kind: Literal["SPLIT", "CASH_DIVIDEND", "SYMBOL_CHANGE"]
    effective_at: datetime
    source: str = Field(min_length=5, max_length=500)
    split_ratio: float | None = Field(default=None, gt=0, le=1000000)
    cash_per_share: float | None = Field(default=None, ge=0)
    pay_at: datetime | None = None
    new_symbol: str | None = None

    @model_validator(mode="after")
    def validate_action(self):
        if self.effective_at.tzinfo is None or (self.pay_at and self.pay_at.tzinfo is None):
            raise ValueError("Corporate action dates require timezones")
        if self.kind == "SPLIT" and (self.split_ratio is None or self.split_ratio == 1):
            raise ValueError("Split requires new shares / old shares ratio other than one")
        if self.kind == "CASH_DIVIDEND" and (self.cash_per_share is None or self.pay_at is None or self.pay_at < self.effective_at):
            raise ValueError("Dividend requires cash_per_share and pay_at >= effective_at")
        if self.kind == "SYMBOL_CHANGE" and not self.new_symbol:
            raise ValueError("Symbol change requires new_symbol")
        if self.kind != "SPLIT" and self.split_ratio is not None:
            raise ValueError("Only splits accept split_ratio")
        if self.kind != "CASH_DIVIDEND" and (self.cash_per_share is not None or self.pay_at is not None):
            raise ValueError("Only dividends accept cash_per_share/pay_at")
        if self.kind != "SYMBOL_CHANGE" and self.new_symbol is not None:
            raise ValueError("Only symbol changes accept new_symbol")
        return self


class EquitySimulationConfig(Assumptions):
    version: Literal[1] = 1
    price_basis: Literal["split_adjusted", "raw"] = "split_adjusted"
    path: Literal["open_high_low_close", "open_low_high_close"] = "open_low_high_close"
    source: str = Field(default="Uncalibrated conservative scenario; regulatory fees require dated inputs", min_length=5)
    calibrations: list[Calibration] = Field(default_factory=list, max_length=10000)
    corporate_actions: list[ResearchAction] = Field(default_factory=list, max_length=10000)

    @model_validator(mode="after")
    def unique(self):
        if len({a.event_id for a in self.corporate_actions}) != len(self.corporate_actions):
            raise ValueError("Corporate action IDs must be unique")
        if self.price_basis != "raw" and any(a.kind == "SPLIT" for a in self.corporate_actions):
            raise ValueError("Explicit splits require raw prices; adjusted prices would apply splits twice")
        keys = [(c.ticker, c.effective_at) for c in self.calibrations]
        if len(set(keys)) != len(keys):
            raise ValueError("Duplicate calibration ticker/effective date")
        return self

    def at(self, ticker, ts_ns):
        applicable = [c for c in self.calibrations if c.ticker in {"*", ticker}
                      and c.effective_at.timestamp() * 1e9 <= ts_ns]
        return max(applicable, key=lambda c: (c.effective_at, c.ticker != "*")) if applicable else self


def canonical_equity_frame(frame, config):
    """Keep one stable research ID across reviewed source-symbol changes."""
    cfg = EquitySimulationConfig.model_validate(config or {})
    frame = frame.copy()
    for action in sorted(cfg.corporate_actions, key=lambda a: a.effective_at, reverse=True):
        if action.kind != "SYMBOL_CHANGE":
            continue
        mask = (frame.ticker == action.new_symbol) & (frame.timestamp >= pd.Timestamp(action.effective_at))
        frame.loc[mask, "ticker"] = action.ticker
    if frame.duplicated(["ticker", "timestamp"]).any():
        raise ValueError("Corporate-action aliases create duplicate ticker/timestamp rows")
    return frame


def simulation_for(engine):
    try:
        return next((s._equity_simulation for s in engine.trader.strategies()
                     if getattr(s, "_equity_simulation", None) is not None), None)
    except AttributeError:
        return None


def simulation_report(engine):
    runtime = simulation_for(engine)
    return runtime.report() if runtime else None


def risk_free_returns(engine, timestamps):
    runtime = simulation_for(engine)
    if runtime is None:
        return None
    return risk_free_intervals(runtime.settings, timestamps)


def risk_free_intervals(config, timestamps):
    config = EquitySimulationConfig.model_validate(config)
    result = []
    for left, right in zip(timestamps, timestamps[1:]):
        boundaries = {float(left), float(right)}
        boundaries.update(c.effective_at.timestamp() for c in config.calibrations
                          if c.ticker == "*" and left < c.effective_at.timestamp() < right)
        points = sorted(boundaries)
        growth = 1.0
        for start, end in zip(points, points[1:]):
            rate = config.at("*", int(start * 1e9)).risk_free_rate
            growth *= (1 + rate) ** ((end - start) / (365.25 * 86400))
        result.append(growth - 1)
    return result


class EquityFeeModel(FeeModel):
    """Fixed plan cumulative order minimum/cap, including partial-fill top-ups."""
    def __init__(self, runtime):
        super().__init__()
        self.runtime = runtime
        self.orders = defaultdict(lambda: [Decimal(0), Decimal(0), Decimal(0)])

    def get_commission(self, order, fill_qty, fill_px, instrument):
        cfg = self.runtime.settings.at(str(instrument.raw_symbol), self.runtime.now)
        qty, notional, charged = self.orders[str(order.client_order_id)]
        qty += fill_qty.as_decimal()
        notional += fill_qty.as_decimal() * fill_px.as_decimal()
        d = lambda x: Decimal(str(x))
        fee = min(max(qty * d(cfg.commission_per_share), d(cfg.minimum_commission)), notional * d(cfg.commission_cap_pct))
        regulatory = qty * d(cfg.cat_per_share)
        if order.side == OrderSide.SELL:
            regulatory += notional * d(cfg.sec_sell_rate) + min(qty * d(cfg.taf_per_share), d(cfg.taf_cap))
        total = ((fee + regulatory) * d(self.runtime.cost_multiplier)).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
        delta = max(Decimal(0), total - charged)
        self.orders[str(order.client_order_id)] = [qty, notional, charged + delta]
        self.runtime.commissions += float(delta)
        return Money(delta, USD)


class EquitySimulation(SimulationModule):
    def __init__(self, config, cost_multiplier=1.0):
        super().__init__(SimulationModuleConfig())
        self.settings = EquitySimulationConfig.model_validate(config or {})
        self.cost_multiplier = cost_multiplier
        self.strategy = None
        self.now = 0
        self.marks = {}
        self.previous_time = None
        self.curve = []
        self.cashflows = []
        self.actions = []
        self.applied = set()
        self.receivables = []
        self.commissions = 0.0
        self.bar_times = set()
        self.openings = {}
        self.volume = 0
        self.seen_bars = set()
        self.zero_volume_bars = 0
        self.total_bars = 0
        self.calibrated_bars = 0

    def report(self):
        payload = self.settings.model_dump(mode="json")
        return {"version": 1, "config": payload,
                "data_preflight": getattr(self, "data_preflight", None),
                "contract_sha256": hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
                "cost_multiplier": self.cost_multiplier,
                "calibration_status": (
                    "dated evidence covers all replay bars"
                    if self.total_bars and self.calibrated_bars == self.total_bars
                    else "partial dated evidence; remaining bars use scenario defaults"
                    if self.calibrated_bars else "uncalibrated scenario"
                ),
                "commission_usd": round(self.commissions, 2), "cashflows": self.cashflows,
                "data_coverage": {"bars": self.total_bars, "zero_volume_bars": self.zero_volume_bars,
                                  "calibrated_bars": self.calibrated_bars},
                "corporate_actions": self.actions, "price_basis": self.settings.price_basis,
                "equity_basis": "cash balance + unrealized PnL + dividend receivables",
                "execution_basis": "finite synthetic L2 OHLC auctions; next observed book; shared liquidity consumption",
                "limitations": ["OHLC does not identify intrabar order; compare both paths.",
                    "Default rates are scenarios, not historical broker calibration.",
                    "No queue reconstruction or hidden liquidity; market remainder may be cancelled."]}

    def equity(self):
        account = self.exchange.get_account()
        total = float(account.balance_total(USD).as_double())
        for pos in self.exchange.cache.positions_open():
            mark = self.marks.get(str(pos.instrument_id))
            if mark is not None:
                total += float(pos.unrealized_pnl(Price(mark, 2)).as_double())
        return total + sum(r["amount"] for r in self.receivables)

    def cash(self, amount, kind, ts, **details):
        amount = round(amount, 2)
        if not amount:
            return
        if amount:
            self.exchange.adjust_account(Money(amount, USD))
        self.cashflows.append({"ts": pd.Timestamp(ts, unit="ns", tz="UTC").isoformat(),
                               "kind": kind, "amount": amount, **details})

    def accrue(self, ts):
        if self.previous_time is None:
            self.previous_time = ts
            return
        if ts <= self.previous_time:
            return
        # Split accrual at every rate change: weekend and holiday days count.
        boundaries = {self.previous_time, ts}
        boundaries.update(int(c.effective_at.timestamp() * 1e9) for c in self.settings.calibrations
                          if self.previous_time < c.effective_at.timestamp() * 1e9 < ts)
        points = sorted(boundaries)
        positions = list(self.exchange.cache.positions_open())
        for start, end in zip(points, points[1:]):
            days = (end - start) / 86400e9
            long_value = 0.0
            for pos in positions:
                price = self.marks.get(str(pos.instrument_id), pos.avg_px_open)
                value = pos.signed_qty * price
                cfg = self.settings.at(str(pos.instrument_id).split(".")[0], start)
                if value < 0:
                    # IBKR USD short collateral rounded up to whole dollars * 102%.
                    collateral = math.ceil(price * 1.02) * abs(pos.signed_qty)
                    self.cash(-collateral * cfg.borrow_rate * days / 360 * self.cost_multiplier,
                              "BORROW", end, instrument_id=str(pos.instrument_id), days=days)
                else:
                    # Financing follows the purchase principal. An unrealized
                    # gain cannot create a cash debit (or reduce idle cash).
                    long_value += pos.signed_qty * pos.avg_px_open
            cfg = self.settings.at("*", start)
            balance = float(self.exchange.get_account().balance_total(USD).as_double())
            debit = max(0, long_value - balance)
            self.cash(-debit * cfg.debit_rate * days / 360 * self.cost_multiplier, "DEBIT_INTEREST", end, days=days)
            credit = max(0, balance - long_value)
            self.cash(credit * cfg.cash_credit_rate * days / 360, "CASH_INTEREST", end, days=days)
        self.previous_time = ts

    def pre_process(self, data):
        self.now = int(data.ts_init)
        self.accrue(self.now)
        for item in list(self.receivables):
            if item["pay_ns"] <= self.now:
                self.cash(item["amount"], "DIVIDEND_PAYMENT", self.now, event_id=item["event_id"])
                self.receivables.remove(item)
        data_iid = data.bar_type.instrument_id if isinstance(data, Bar) else getattr(data, "instrument_id", None)
        opening = self.openings.get((self.now, str(data_iid)))
        if opening:
            ticker, price = opening
            iid = InstrumentId.from_str(f"{ticker}.IBKR")
            for action in sorted(self.settings.corporate_actions, key=lambda a: (a.effective_at, a.event_id)):
                if action.ticker != ticker or action.event_id in self.applied or action.effective_at.timestamp() * 1e9 > self.now + 8:
                    continue
                positions = list(self.exchange.cache.positions_open(instrument_id=iid))
                record = {**action.model_dump(mode="json"), "applied_at": self.now,
                          "quantity_before": sum(p.signed_qty for p in positions)}
                if action.kind == "CASH_DIVIDEND":
                    amount = record["quantity_before"] * action.cash_per_share
                    self.receivables.append({"amount": amount, "pay_ns": int(action.pay_at.timestamp() * 1e9), "event_id": action.event_id})
                    record["entitlement_usd"] = amount
                elif action.kind == "SPLIT":
                    self.strategy._research_adjusting = True
                    self.strategy.cancel_all_orders(iid)
                    self.exchange.process(self.now)
                    for pos in positions:
                        self.split_position(pos, action, price)
                    self.strategy._apply_research_split(iid, action.split_ratio)
                    if hasattr(self.strategy, "_execution"):
                        self.strategy._execution.apply_research_split(str(iid), action.split_ratio, price, self.now, action.event_id)
                    self.strategy._research_adjusting = False
                    if hasattr(self.strategy, "_ensure_broker_protection"):
                        self.strategy._ensure_broker_protection(iid)
                self.applied.add(action.event_id)
                self.actions.append(record)
            self.marks[str(iid)] = price
        if isinstance(data, Bar):
            self.marks[str(data.bar_type.instrument_id)] = float(data.close)
            self.seen_bars.add(self.now)

    def split_position(self, pos, action, price):
        """Rebase the open net position; original execution reports remain intact."""
        exact = pos.signed_qty * action.split_ratio
        whole = math.trunc(exact)
        basis = pos.avg_px_open / action.split_ratio
        fractional = exact - whole
        # Margin accounting stores realized PnL, not stock principal.
        self.cash(fractional * (price - basis), "SPLIT_FRACTIONAL_PNL", self.now,
                  event_id=action.event_id, shares=fractional, proceeds=fractional * price)
        if whole:
            values = OrderFilled.to_dict(pos.last_event)
            values.update(last_qty=str(abs(whole)), last_px=str(Price(basis, 2)),
                          order_side="BUY" if whole > 0 else "SELL", commission="0.00 USD",
                          event_id=str(UUID4()), ts_event=self.now, ts_init=self.now)
            replacement = Position(self.exchange.cache.instrument(pos.instrument_id), OrderFilled.from_dict(values))
            adjustment = PositionAdjusted(pos.trader_id, pos.strategy_id, pos.instrument_id, pos.id,
                pos.account_id, PositionAdjustmentType.COMMISSION, None, pos.realized_pnl,
                "Preserve pre-split realized PnL; original fills retained in order report", UUID4(), self.now, self.now)
            replacement.apply_adjustment(adjustment)
            self.exchange.cache.add_position(replacement, OmsType.NETTING)
            self.strategy.portfolio.update_position(PositionChanged.create(replacement, replacement.last_event, UUID4(), self.now))
            self.cash(whole * (replacement.avg_px_open - basis), "SPLIT_BASIS_ROUNDING", self.now, event_id=action.event_id)
        else:
            # A quantity adjustment alone leaves Nautilus without a closing
            # order ID/time. Record the issuer's cash-in-lieu disposition in
            # the position lifecycle; this is not submitted as a market order
            # and does not enter the execution report or consume liquidity.
            values = OrderFilled.to_dict(pos.last_event)
            disposition_id = "CA-" + str(UUID4()).replace("-", "")
            values.update(last_qty=str(pos.quantity), last_px=str(Price(price * action.split_ratio, 2)),
                          order_side="SELL" if pos.signed_qty > 0 else "BUY", commission="0.00 USD",
                          client_order_id=disposition_id, trade_id=disposition_id,
                          event_id=str(UUID4()), ts_event=self.now, ts_init=self.now)
            pos.apply(OrderFilled.from_dict(values))
            self.exchange.cache.update_position(pos)
            self.strategy.portfolio.update_position(PositionClosed.create(pos, pos.last_event, UUID4(), self.now))

    def process(self, ts_now):
        if ts_now in self.seen_bars and ts_now >= self.strategy.config.backtest_trade_start_ns:
            point = {"ts": pd.Timestamp(ts_now, unit="ns", tz="UTC").isoformat(), "equity": round(self.equity(), 6)}
            if self.curve and self.curve[-1]["ts"] == point["ts"]:
                self.curve[-1] = point
            else:
                self.curve.append(point)

    def reset(self):
        self.marks.clear()
        self.curve.clear()
        self.cashflows.clear()
        self.actions.clear()
        self.applied.clear()
        self.receivables.clear()
        self.seen_bars.clear()
        self.previous_time = None
        self.commissions = 0.0

    def log_diagnostics(self, logger):
        pass

    def replay(self, bars):
        stream = []
        for bar in bars:
            self.total_bars += 1
            self.zero_volume_bars += int(float(bar.volume) == 0)
            iid = bar.bar_type.instrument_id
            ticker = str(iid).removesuffix(".IBKR")
            ts = int(bar.ts_init)
            self.bar_times.add(ts)
            self.openings[(ts - 8, str(iid))] = (ticker, float(bar.open))
            cfg = self.settings.at(ticker, ts - 8)
            self.calibrated_bars += int(isinstance(cfg, Calibration))
            local = pd.Timestamp(ts, unit="ns", tz="UTC").tz_convert("America/New_York")
            # Daily bars represent an entire observed session; intraday rows
            # outside RTH get reduced depth and wider quotes.
            daily = "DAY" in str(bar.bar_type)
            extended = not daily and not (9 * 60 + 30 < local.hour * 60 + local.minute <= 16 * 60)
            depth = math.floor(float(bar.volume) * cfg.participation * (cfg.extended_liquidity if extended else 1))
            mids = [float(bar.open), float(bar.high), float(bar.low), float(bar.close)]
            if self.settings.path == "open_low_high_close":
                mids[1], mids[2] = mids[2], mids[1]
            for stage, mid in enumerate(mids):
                at = ts - 8 + stage
                deltas = [OrderBookDelta(iid, BookAction.CLEAR, None, 0, 0, at, at)]
                # Budget split across stages, sides and two price levels.
                budget = depth // 4 + (stage < depth % 4)
                for side, sign in ((OrderSide.BUY, -1), (OrderSide.SELL, 1)):
                    side_budget = budget // 2
                    for level in range(2):
                        size = side_budget // 2 + (level < side_budget % 2)
                        if not size:
                            continue
                        spread = cfg.spread_bps * (cfg.extended_spread_multiplier if extended else 1)
                        bps = (spread / 2 + cfg.impact_bps * level) * self.cost_multiplier
                        px = max(0.01, (math.floor(mid * (1 - bps / 10000) * 100) if sign < 0 else math.ceil(mid * (1 + bps / 10000) * 100)) / 100)
                        deltas.append(OrderBookDelta(iid, BookAction.ADD,
                            BookOrder(side, Price(px, 2), Quantity.from_int(size), 1 + level + (2 if sign > 0 else 0)),
                            0, 0, at, at))
                # F_LAST on the last delta publishes the complete snapshot.
                last = deltas[-1]
                deltas[-1] = OrderBookDelta(iid, last.action, last.order, 128, 0, at, at)
                stream.append(OrderBookDeltas(iid, deltas))
            # Remove previous-bar liquidity before the completed bar signal.
            stream.append(OrderBookDeltas(iid, [OrderBookDelta(iid, BookAction.CLEAR, None, 128, 0, ts - 1, ts - 1)]))
            stream.append(bar)
        return sorted(stream, key=lambda d: d.ts_init)
