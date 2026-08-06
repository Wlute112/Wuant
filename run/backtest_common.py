"""Shared backtest assembly used by both the runnable example and Optuna.

Builds a BacktestEngine from a CSV of daily bars (sample or real IBKR export),
wires the SAME MLStrategy used in paper/live, applies an IBKR-Pro-like fee
model, and returns the engine after running.

Targets nautilus_trader 1.229.

Account model note
------------------
We use a MARGIN account with ``default_leverage=1.0``. This is deliberate:
  * A CASH account in Nautilus (and at a real broker) CANNOT short-sell, but
    this strategy goes both long and short (yhat < -threshold -> SELL/short).
  * MARGIN with leverage pinned to 1.0 gives long/short capability while still
    forbidding any actual leverage (notional <= equity), which is exactly what
    "leverage = 1" means for a long/short equity book. The RiskManager
    additionally caps notional at equity as a second line of defence.
"""
from __future__ import annotations

import warnings
from collections import deque
from decimal import Decimal

import pandas as pd

# nautilus_trader 1.229's compiled engine (backtest/engine.pyx) still calls the
# deprecated ``pandas.Timestamp.utcnow()`` on every ``run()`` / ``end()``. On
# newer pandas this raises a noisy ``Pandas4Warning`` (a DeprecationWarning
# subclass) that Python attributes to OUR call site (e.g. optimize.py's
# ``engine.run(streaming=True)``). It is not our bug and cannot be fixed without
# patching a compiled dependency, so silence exactly this one message here --
# in the single module both the one-shot and streaming run paths import -- while
# leaving every other deprecation warning intact.
warnings.filterwarnings(
    "ignore",
    message=r"Timestamp\.utcnow is deprecated.*",
    category=DeprecationWarning,
)

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.backtest.models import FeeModel, PerContractFeeModel
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments import CurrencyPair, Equity, Instrument
from nautilus_trader.model.objects import Currency, Money, Price, Quantity

from quant.strategies.ml_strategy import MLStrategy, MLStrategyConfig

VENUE = Venue("IBKR")
BAR_SUFFIX = "-1-DAY-LAST-EXTERNAL"
ASSET_CLASSES = ("crypto", "equity")

# IBKR crypto (spot, routed to Zero Hash / Paxos): commission is tiered by
# TRAILING 30-DAY account-wide crypto trade value -- not maker/taker -- per
# https://www.interactivebrokers.com/en/pricing/commissions-cryptocurrencies.php
# 0.18% up to $100k, 0.15% from $100k-$1M, 0.12% above $1M, with a $1.75
# minimum per order that is itself capped at 1% of that order's trade value
# (so small orders pay 1% of notional rather than a flat $1.75 floor).
# ``ZeroHashCryptoFeeModel`` below implements this schedule exactly.
ZEROHASH_TIERS = (
    # (trailing-volume floor this rate applies ABOVE, rate)
    (Decimal("1000000"), Decimal("0.0012")),
    (Decimal("100000"), Decimal("0.0015")),
    (Decimal("0"), Decimal("0.0018")),
)
ZEROHASH_MIN_FEE = Decimal("1.75")
ZEROHASH_MIN_FEE_CAP_PCT = Decimal("0.01")  # min fee capped at 1% of trade value
ZEROHASH_VOLUME_WINDOW_NS = 30 * 24 * 60 * 60 * 1_000_000_000  # trailing 30 days

# Crypto price/size precision. USD-quoted price to the cent; size to 1e-6 of a
# coin so fractional positions (e.g. 0.0003 BTC) are representable -- essential
# on a small account where a whole coin costs more than the whole book.
CRYPTO_PRICE_PRECISION = 2
CRYPTO_SIZE_PRECISION = 6

# IBKR equities: approximated as a flat per-share commission (~$0.005/share),
# applied via PerContractFeeModel (each unit filled = 1 share). This ignores
# IBKR's real per-order minimum/tiered schedule -- same class of simplification
# as the crypto maker/taker approximation above, just a different fee shape
# since equities aren't quoted with a maker/taker split.
EQUITY_COMMISSION_PER_SHARE = Decimal("0.005")
EQUITY_PRICE_PRECISION = 2

# Leverage = 1 (no leverage) but a MARGIN account so the book can go short.
DEFAULT_LEVERAGE = Decimal("1.0")


def make_crypto(symbol: str) -> CurrencyPair:
    """Define a spot crypto pair (BASE/USD) on the IBKR venue.

    ``symbol`` is the base-asset code (e.g. "BTC", "ETH", "SOL"). Nautilus
    resolves known crypto codes from its internal currency map and falls back to
    a precision-8 crypto currency for anything unknown, so any ticker works.
    CurrencyPair (unlike Equity) supports fractional ``size_precision``, which is
    what lets the risk-sized fractional-coin quantities actually trade.

    No ``maker_fee``/``taker_fee`` here -- Zero Hash/Paxos crypto commission is
    NOT a maker/taker split, it's tiered by trailing account volume, which
    ``ZeroHashCryptoFeeModel`` (the venue-wide fee model, see
    ``asset_class_fee_model``) computes directly from trade notional instead.
    """
    iid = InstrumentId.from_str(f"{symbol}.IBKR")
    base = Currency.from_str(symbol)  # BTC, ETH, ... (strict=False -> crypto)
    return CurrencyPair(
        instrument_id=iid,
        raw_symbol=Symbol(symbol),
        base_currency=base,
        quote_currency=USD,
        price_precision=CRYPTO_PRICE_PRECISION,
        size_precision=CRYPTO_SIZE_PRECISION,
        price_increment=Price(10 ** -CRYPTO_PRICE_PRECISION, CRYPTO_PRICE_PRECISION),
        size_increment=Quantity(10 ** -CRYPTO_SIZE_PRECISION, CRYPTO_SIZE_PRECISION),
        ts_event=0,
        ts_init=0,
    )


def make_equity(symbol: str) -> Equity:
    """Define a whole-share USD equity/ETF on the IBKR venue.

    ``Equity`` hardcodes ``size_precision=0`` / ``size_increment=1`` internally
    (Nautilus never allows fractional equity units here), so risk-sized
    quantities are automatically floored to whole shares via the strategy's
    existing ``instrument.make_qty(raw_qty)`` call -- no strategy-layer changes
    needed. Unlike ``CurrencyPair``, there's no maker/taker split; commission is
    applied venue-wide via ``PerContractFeeModel`` (see ``asset_class_fee_model``).
    """
    iid = InstrumentId.from_str(f"{symbol}.IBKR")
    return Equity(
        instrument_id=iid,
        raw_symbol=Symbol(symbol),
        currency=USD,
        price_precision=EQUITY_PRICE_PRECISION,
        price_increment=Price(10 ** -EQUITY_PRICE_PRECISION, EQUITY_PRICE_PRECISION),
        lot_size=Quantity.from_int(1),
        ts_event=0,
        ts_init=0,
    )


def make_instrument(symbol: str, asset_class: str) -> Instrument:
    """Dispatch to ``make_crypto``/``make_equity`` for the run's asset class."""
    if asset_class == "equity":
        return make_equity(symbol)
    return make_crypto(symbol)


class ZeroHashCryptoFeeModel(FeeModel):
    """IBKR crypto (Zero Hash/Paxos) commission: tiered by trailing 30-day
    account-wide crypto trade value, with a $1.75 minimum per order capped at
    1% of that order's trade value. See ``ZEROHASH_TIERS`` above for the exact
    schedule and source.

    One instance is shared across every crypto instrument on the venue for
    the life of a single engine (fresh instance per ``build_engine()`` call,
    so trailing volume never leaks between separate backtests/Optuna trials),
    because IBKR's tiering is account-wide, not per-symbol.
    """

    def __init__(self) -> None:
        super().__init__()
        self._fills: deque[tuple[int, Decimal]] = deque()
        self._window_notional = Decimal("0")

    def _trailing_volume(self, ts_ns: int) -> Decimal:
        cutoff = ts_ns - ZEROHASH_VOLUME_WINDOW_NS
        while self._fills and self._fills[0][0] < cutoff:
            _, old_notional = self._fills.popleft()
            self._window_notional -= old_notional
        return self._window_notional

    def _tier_rate(self, trailing_volume: Decimal) -> Decimal:
        for floor, rate in ZEROHASH_TIERS:
            if trailing_volume > floor:
                return rate
        return ZEROHASH_TIERS[-1][1]

    def get_commission(self, order, fill_qty, fill_px, instrument):
        notional = instrument.notional_value(
            quantity=fill_qty, price=fill_px, use_quote_for_inverse=False
        )
        notional_value = notional.as_decimal()
        ts_ns = order.ts_last or order.ts_init

        # Tier is based on volume BEFORE this fill, then this fill is folded
        # into the trailing window for the next one.
        rate = self._tier_rate(self._trailing_volume(ts_ns))
        self._fills.append((ts_ns, notional_value))
        self._window_notional += notional_value

        pct_fee = notional_value * rate
        min_fee = min(ZEROHASH_MIN_FEE, notional_value * ZEROHASH_MIN_FEE_CAP_PCT)
        commission_value = max(pct_fee, min_fee)

        currency = instrument.get_base_currency() if instrument.is_inverse else instrument.quote_currency
        return Money(commission_value, currency)


def asset_class_fee_model(asset_class: str) -> FeeModel:
    """Venue-wide fee model for the run's asset class.

    Crypto: ``ZeroHashCryptoFeeModel`` (real tiered IBKR/Zero Hash schedule).
    Equity: a flat per-share commission, since equities aren't quoted with a
    maker/taker split the way crypto is.
    """
    if asset_class == "equity":
        return PerContractFeeModel(commission=Money(EQUITY_COMMISSION_PER_SHARE, USD))
    return ZeroHashCryptoFeeModel()


def _bars_from_df(df: pd.DataFrame, instrument: Instrument) -> list[Bar]:
    bar_type = BarType.from_str(f"{instrument.id}{BAR_SUFFIX}")
    pp = instrument.price_precision
    # Nautilus requires bar.volume.precision == instrument.size_precision, which
    # for a fractional-size crypto pair is 6 (not the equities 0).
    sp = instrument.size_precision
    bars = []
    for row in df.itertuples(index=False):
        ts = row.timestamp
        if not isinstance(ts, pd.Timestamp):
            ts = pd.Timestamp(ts, tz="UTC")
        ns = int(ts.value)
        bars.append(
            Bar(
                bar_type=bar_type,
                open=Price(float(row.open), pp),
                high=Price(float(row.high), pp),
                low=Price(float(row.low), pp),
                close=Price(float(row.close), pp),
                volume=Quantity(float(row.volume), sp),
                ts_event=ns,
                ts_init=ns,
            )
        )
    return bars


def build_engine(
    csv_path: str,
    tickers: list[str],
    strategy_overrides: dict | None = None,
    starting_cash: float = 5000.0,
    log_level: str = "ERROR",
    bypass_logging: bool = False,
    asset_class: str = "crypto",
) -> tuple[BacktestEngine, list[Bar]]:
    """Assemble the venue, instruments and strategy WITHOUT loading data or running.

    Returns the engine plus the full time-sorted list of ``Bar`` objects (across
    all tickers, interleaved by ``ts_init``). The caller decides how to feed them:
      * one shot -- ``engine.add_data(bars); engine.run()`` (see ``build_and_run``),
      * streaming -- add contiguous time-slices via ``engine.run(streaming=True)``
        so intermediate state can be inspected mid-backtest (used by the Optuna
        pruner to abandon hopeless trials early).

    Splitting construction from execution keeps a single source of truth for the
    account model / fee wiring so the streamed and one-shot paths stay identical.

    ``asset_class`` is ``"crypto"`` (default, backward compatible) or
    ``"equity"``. One asset class per call -- every ticker in ``tickers`` is
    built as the SAME instrument type on the SAME venue, since Nautilus binds
    one fee model per venue/account and this run's tickers are assumed to all
    belong to the same market.
    """
    if asset_class not in ASSET_CLASSES:
        raise ValueError(f"asset_class must be one of {ASSET_CLASSES}, got {asset_class!r}")
    df = pd.read_csv(csv_path)
    df["timestamp"] = pd.to_datetime(
        df["timestamp"], format="mixed", utc=True
    )
    df = df[df["ticker"].isin(tickers)].sort_values("timestamp")

    engine = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id="BACKTEST-001",
            logging=LoggingConfig(
                log_level=log_level,
                bypass_logging=bypass_logging,
            ),
        )
    )
    # MARGIN account with leverage pinned to 1.0 -> short-selling allowed,
    # but no actual leverage (notional capped at equity by both the venue and
    # the RiskManager). A CASH account would raise "cash accounts cannot short".
    engine.add_venue(
        venue=VENUE,
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        base_currency=USD,
        starting_balances=[Money(starting_cash, USD)],
        default_leverage=DEFAULT_LEVERAGE,
        fee_model=asset_class_fee_model(asset_class),
    )

    instrument_ids = []
    all_bars: list[Bar] = []
    for sym in tickers:
        inst = make_instrument(sym, asset_class)
        engine.add_instrument(inst)
        sub = df[df["ticker"] == sym].copy()
        all_bars.extend(_bars_from_df(sub, inst))
        instrument_ids.append(str(inst.id))

    # One combined, time-ordered stream. Streaming batches MUST be globally
    # time-sorted across instruments (each batch strictly after the previous),
    # so sort the merged list once here rather than per ticker.
    all_bars.sort(key=lambda b: b.ts_init)

    cfg_kwargs = dict(
        instrument_ids=instrument_ids,
        bar_type_suffix=BAR_SUFFIX,
        starting_equity=starting_cash,
    )
    if strategy_overrides:
        cfg_kwargs.update(strategy_overrides)

    engine.add_strategy(MLStrategy(MLStrategyConfig(**cfg_kwargs)))
    return engine, all_bars


def build_and_run(
    csv_path: str,
    tickers: list[str],
    strategy_overrides: dict | None = None,
    starting_cash: float = 5000.0,
    log_level: str = "ERROR",
    bypass_logging: bool = False,
    asset_class: str = "crypto",
) -> BacktestEngine:
    """One-shot backtest: build, load all data, run to completion, return engine."""
    engine, all_bars = build_engine(
        csv_path=csv_path,
        tickers=tickers,
        strategy_overrides=strategy_overrides,
        starting_cash=starting_cash,
        log_level=log_level,
        bypass_logging=bypass_logging,
        asset_class=asset_class,
    )
    engine.add_data(all_bars)
    engine.run()
    return engine
