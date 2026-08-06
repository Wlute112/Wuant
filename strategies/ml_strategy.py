"""The Strategy (execution) layer -- ONE class used for backtest, paper & live.

Nautilus does not require a different Strategy per environment: the *engine*
(BacktestEngine vs TradingNode + venue clients) differs, but this class is
identical across all three. That is the whole point of the design -- the logic
you backtest is byte-for-byte the logic that trades.

What it does on each closed bar:
  1. Append close to a rolling window (per instrument).
  2. Ask the PredictionEngine for yhat (forward-return forecast). The strategy
     NEVER computes yhat itself -- alpha is fully decoupled.
  3. Translate yhat -> intent (BUY / SELL / HOLD) via a threshold hyperparameter.
  4. Ask the RiskManager for size (1% target, 0.25% hard cap, leverage=1) using
     an ATR-based stop distance.
  5. Submit Nautilus order OBJECTS (LIMIT for maker, MARKET for taker) via the
     order factory -- never a raw "buy()".

Entries are resolved CROSS-SECTIONALLY: signals for all instruments at a given
timestamp are buffered, then the scarce `max_open_positions` slots are handed to
the highest-conviction (|yhat|) signals -- not to whichever instrument the
engine happened to deliver first (see `_resolve_batch`).
  6. Enforce daily-loss flatten+halt and the permanent drawdown kill-switch.

Targets nautilus_trader 1.229 API.
"""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
import json
from typing import NamedTuple

import numpy as np

from nautilus_trader.config import StrategyConfig
from nautilus_trader.core.message import Event
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.events import (
    OrderCanceled,
    OrderExpired,
    OrderFilled,
    OrderRejected,
)
from nautilus_trader.model.identifiers import AccountId, InstrumentId
from nautilus_trader.model.objects import Quantity
from nautilus_trader.trading.strategy import Strategy

from quant.models.prediction_engine import PredictionConfig, PredictionEngine
from quant.strategies.risk import RiskConfig, RiskManager, TradingState


class _PendingSignal(NamedTuple):
    """One instrument's buffered signal for a single timestamp.

    Signals are collected as each instrument's bar for a timestamp arrives, then
    resolved together (see ``MLStrategy._resolve_batch``) so scarce position
    slots go to the highest-CONVICTION signals rather than to whichever
    instrument the engine happened to deliver first.
    """
    close: float
    atr: float
    yhat: float  # signed forward-return forecast; |yhat| is the conviction


class MLStrategyConfig(StrategyConfig, frozen=True):
    """All knobs Optuna tunes live here (plus fixed risk params)."""
    instrument_ids: list[str]
    bar_type_suffix: str = "-1-DAY-LAST-EXTERNAL"  # appended to each instrument id

    # --- alpha hyperparameters (Optuna-tuned) ---
    n_lags: int = 5
    horizon: int = 1
    entry_threshold: float = 0.0010   # |yhat| must exceed this to trade
    atr_period: int = 14
    atr_stop_mult: float = 2.0        # stop distance = atr_stop_mult * ATR
    use_limit_orders: bool = True     # maker (LIMIT) vs taker (MARKET)
    limit_offset_bps: float = 2.0     # passive offset for maker orders (Optuna-tuned)

    # --- Huber regression L2 penalty (Optuna-tuned; see models/prediction_engine.py) ---
    # Was fixed at PredictionConfig's own default (1e-4) for every run. Now
    # searchable so Optuna can trade off bias vs variance against whatever
    # feature count / training-window size the OTHER tuned knobs (n_lags,
    # cross_asset_lags, spread_lags, min_train_bars) land on for a given run --
    # a low-bar-count regime (e.g. weekly bars) with many features needs
    # stronger shrinkage than the 1e-4 default to keep the Huber fit
    # well-conditioned (see the HuberRegressor convergence-warning diagnosis).
    huber_alpha: float = 1e-4

    # --- Huber loss transition point (Optuna-tuned; see models/prediction_engine.py) ---
    # Was fixed at PredictionConfig's own default (1.35, sklearn's ~95%-OLS-
    # efficiency default). Now searchable so Optuna can trade off outlier
    # robustness (smaller epsilon -> more residuals treated as outliers ->
    # linear loss, more robust but less efficient) against OLS-like efficiency
    # (larger epsilon) for whatever return-distribution tails this run's
    # tickers/timeframe actually have. Must stay > 1.0 (sklearn requirement).
    huber_epsilon: float = 1.35

    # --- fractional-Kelly conviction sizing (Optuna-tuned) ---
    # When on, position size scales with the strength of the edge (|yhat|)
    # relative to its variance, times `kelly_fraction`. It only ever de-risks
    # BELOW the fixed 0.25% risk cap / leverage=1 rail (see RiskManager.
    # kelly_size_for_trade), so the hard risk limits stay binding.
    use_kelly_sizing: bool = False    # off -> original flat 0.25%-cap sizing
    kelly_fraction: float = 0.5       # fraction of full Kelly ("percent of f")
    kelly_vol_window: int = 30        # rolling window for the edge-variance est.

    # --- portfolio concentration (Optuna-tuned) ---
    # Max number of instruments held simultaneously. 0 = unlimited (original
    # behaviour). Caps how much of the book concentrates across the multi-crypto
    # universe and limits aggregate gross exposure when several signals fire at
    # once. Reversing/adjusting an ALREADY-held instrument is always allowed.
    max_open_positions: int = 0

    # --- regime-detection alpha features (see models/regime.py) ---
    # Structural (like the risk params), NOT Optuna-tuned: they give the Huber
    # model regime context so it is less fragile to bull->bear switches. The
    # SAME settings apply in backtest, paper, and live.
    use_regime_features: bool = True  # transition-matrix bull-minus-bear score
    use_hmm_feature: bool = True      # GaussianHMM latent-state feature
    regime_window: int = 20           # rolling-return lookback for labelling
    # --- fit vs raw source for each regime feature (see models/prediction_engine.py) ---
    # "fit": jointly weighted inside the Huber regression (original behaviour).
    # "raw": bypasses the Huber fit and contributes value*scale to yhat directly.
    regime_source: str = "fit"        # "fit" | "raw"
    hmm_source: str = "fit"           # "fit" | "raw"
    regime_raw_scale: float = 1.0
    hmm_raw_scale: float = 1.0

    # --- cross-asset ARDL + spread alpha features (see models/prediction_engine.py) ---
    # cross_asset_lags / spread_lags are Optuna-tunable (like n_lags/horizon):
    # 0 disables the corresponding feature block. cross_asset_symbols is
    # structural, NOT Optuna-tuned -- it fixes each instrument's peer universe
    # for the run; empty means "every OTHER instrument in instrument_ids".
    cross_asset_lags: int = 0
    spread_lags: int = 0
    cross_asset_symbols: tuple[str, ...] = ()

    # --- refit cadence (fixed structural setting; NOT Optuna-tuned) ---
    # How often (in post-warmup bars) the Huber model is refit via
    # PredictionEngine.refit_on_history(). 1 = refit every bar (highest fidelity,
    # the original behaviour). Refitting dominates backtest cost, so a larger
    # cadence (e.g. 12 or 24) makes Optuna's repeated backtests dramatically
    # faster. Like the risk/regime params this is STRUCTURAL, not part of the
    # Optuna search space -- it is passed in identically for every trial. The
    # very first available bar (and any bar before a baseline fit exists) always
    # refits, and skipped bars reuse the last PAST-ONLY fit, so no lookahead is
    # introduced -- only model staleness between refits.
    refit_every_n_bars: int = 1

    # --- risk params (fixed by your spec; not tuned, but dashboard-editable --
    # see strategies/risk.py's RiskConfig for the underlying rail semantics) ---
    starting_equity: float = 5000.0
    # Nautilus account id (for example "IB-DU1234567"). Empty in backtests,
    # where venue lookup resolves the simulated account.
    account_id: str = ""
    # Paper accounts commonly have a much larger simulated NAV than the
    # strategy allocation. When enabled, risk uses starting_equity plus the
    # account PnL delta from startup, rather than sizing from the full account.
    use_allocated_equity: bool = False
    # Spot crypto cannot open naked shorts at IBKR. Backtests retain their
    # original long/short behavior; the live runner forces this off.
    allow_short_positions: bool = True
    min_train_bars: int = 120
    warmup_bars: int = 150            # bars to collect before first prediction
    # Book-level leverage=1: cap TOTAL gross notional across all instruments at
    # max_leverage * equity, not just per-trade. Without it, holding N coins
    # each sized to the per-trade notional cap can push gross exposure to N*
    # equity. Structural (like the other risk rails), NOT Optuna-tuned.
    enforce_portfolio_leverage: bool = True
    # --- risk rails (fixed defaults match RiskConfig's; overridable per-run) ---
    risk_budget_pct: float = 0.01
    max_trade_risk_pct: float = 0.0025
    max_leverage: float = 1.0
    daily_loss_limit_pct: float = 0.02
    kill_switch_pct: float = 0.10
    kill_warn_pct: float = 0.05
    kelly_max_fraction: float = 0.5
    # Live/paper startup requests enough historical bars to warm the alpha
    # without submitting orders for that history. Ignored by backtests.
    request_historical_bars: bool = False
    bootstrap_lookback_days: int = 400


class MLStrategy(Strategy):
    def __init__(self, config: MLStrategyConfig):
        super().__init__(config)
        self._engines: dict[InstrumentId, PredictionEngine] = {}
        # maxlen kept large so the in-backtest fit sees the SAME expanding
        # history as the offline walk_forward() (which never truncates). A small
        # cap here would silently drop old bars and desync the two paths.
        self._closes: dict[InstrumentId, deque] = defaultdict(
            lambda: deque(maxlen=100_000)
        )
        self._highs: dict[InstrumentId, deque] = defaultdict(lambda: deque(maxlen=64))
        self._lows: dict[InstrumentId, deque] = defaultdict(lambda: deque(maxlen=64))
        self._prev_close: dict[InstrumentId, float] = {}
        self._bar_types: dict[InstrumentId, BarType] = {}
        self._risk: RiskManager | None = None
        self._trained: dict[InstrumentId, bool] = defaultdict(bool)
        # Per-instrument count of POST-WARMUP bars processed, used purely to
        # schedule the Huber refit cadence (refit_every_n_bars). It gates ONLY
        # how often we retrain, never what data the fit sees, so it cannot
        # introduce lookahead.
        self._bar_index: dict[InstrumentId, int] = defaultdict(int)
        # Synchronous per-instrument committed gross notional (quote ccy, >= 0),
        # set the moment an order is SUBMITTED. The portfolio's net_position
        # lags order submission (a resting LIMIT may not fill for several bars),
        # so it is unreliable for BOTH the concurrency cap and the book-level
        # leverage guard; this map gives a deterministic, fill-independent view:
        #   * count of non-zero entries -> concurrent-position slots used
        #   * sum of entries            -> total gross notional deployed
        # Reset to flat whenever the book is flattened by a risk event.
        self._committed_notional: dict[InstrumentId, float] = defaultdict(float)
        # --- cross-sectional entry batching ---
        # Bars for a given timestamp arrive one instrument at a time. To allocate
        # the (scarce) position slots by signal conviction rather than by which
        # instrument was processed first, we buffer each instrument's signal for
        # the CURRENT timestamp here and resolve them together once the timestamp
        # is complete (all instruments reported, or the timestamp advances).
        self._pending: dict[InstrumentId, _PendingSignal] = {}
        self._pending_ts = None  # timestamp the current buffer belongs to
        self._n_instruments = 0  # set in on_start; == len(instrument_ids)
        # raw instrument-id string (as used in config.instrument_ids / a
        # PredictionEngine's cfg.peer_symbols) -> InstrumentId, so per-bar peer
        # lookups for cross-asset features don't re-parse strings every bar.
        self._iid_by_raw: dict[str, InstrumentId] = {}
        self._last_bar_ns: dict[InstrumentId, int] = defaultdict(int)
        # Nautilus loads strategy state before on_start constructs the risk
        # manager and instrument maps, so on_load stages the decoded payload.
        self._loaded_state: dict | None = None
        self._account_equity_baseline: float | None = None

    # ---- lifecycle ------------------------------------------------------
    def on_start(self) -> None:
        self._risk = RiskManager(
            self._equity(),
            RiskConfig(
                risk_budget_pct=self.config.risk_budget_pct,
                max_trade_risk_pct=self.config.max_trade_risk_pct,
                max_leverage=self.config.max_leverage,
                daily_loss_limit_pct=self.config.daily_loss_limit_pct,
                kill_switch_pct=self.config.kill_switch_pct,
                kill_warn_pct=self.config.kill_warn_pct,
                kelly_max_fraction=self.config.kelly_max_fraction,
            ),
        )
        self._n_instruments = len(self.config.instrument_ids)
        for raw in self.config.instrument_ids:
            iid = InstrumentId.from_str(raw)
            self._iid_by_raw[raw] = iid
            bt = BarType.from_str(f"{raw}{self.config.bar_type_suffix}")
            self._bar_types[iid] = bt
            # Each instrument's peer universe for cross-asset ARDL/spread
            # features: an explicit cross_asset_symbols list (minus itself), or
            # every OTHER instrument in this run's universe by default.
            if self.config.cross_asset_symbols:
                peers = tuple(s for s in self.config.cross_asset_symbols if s != raw)
            else:
                peers = tuple(r for r in self.config.instrument_ids if r != raw)
            self._engines[iid] = PredictionEngine(
                PredictionConfig(
                    n_lags=self.config.n_lags,
                    horizon=self.config.horizon,
                    min_train_bars=self.config.min_train_bars,
                    use_regime_features=self.config.use_regime_features,
                    use_hmm_feature=self.config.use_hmm_feature,
                    regime_window=self.config.regime_window,
                    regime_source=self.config.regime_source,
                    hmm_source=self.config.hmm_source,
                    regime_raw_scale=self.config.regime_raw_scale,
                    hmm_raw_scale=self.config.hmm_raw_scale,
                    cross_asset_lags=self.config.cross_asset_lags,
                    spread_lags=self.config.spread_lags,
                    peer_symbols=peers,
                    huber_alpha=self.config.huber_alpha,
                    huber_epsilon=self.config.huber_epsilon,
                )
            )
            self.subscribe_bars(bt)
            self.log.info(f"Subscribed {bt}")
        self._restore_loaded_state()
        for iid, bt in self._bar_types.items():
            if (
                self.config.request_historical_bars
                and len(self._closes[iid]) < self.config.warmup_bars
            ):
                self.request_bars(
                    bt,
                    start=self.clock.utc_now()
                    - timedelta(days=self.config.bootstrap_lookback_days),
                )
        self._reconcile_committed_notional()

    # ---- helpers --------------------------------------------------------
    def _equity(self) -> float:
        try:
            acct = None
            if self.config.account_id:
                acct = self.portfolio.account(
                    account_id=AccountId.from_str(self.config.account_id)
                )
            if acct is None and self._bar_types:
                acct = self.portfolio.account(self._venue_for_any())
            if acct is None:
                accounts = self.cache.accounts()
                acct = accounts[0] if len(accounts) == 1 else None
            if acct is not None:
                bal = acct.balance_total()
                if bal is not None:
                    raw_equity = float(bal.as_double())
                    if not self.config.use_allocated_equity:
                        return raw_equity
                    if self._account_equity_baseline is None:
                        self._account_equity_baseline = raw_equity
                    return max(
                        self.config.starting_equity
                        + raw_equity
                        - self._account_equity_baseline,
                        0.0,
                    )
        except Exception:  # noqa: BLE001 - fall back gracefully in backtest
            pass
        return self.config.starting_equity

    def _venue_for_any(self):
        first = next(iter(self._bar_types.values()))
        return first.instrument_id.venue

    def _atr(self, iid: InstrumentId) -> float | None:
        highs, lows, closes = self._highs[iid], self._lows[iid], self._closes[iid]
        n = self.config.atr_period
        if len(highs) <= n:
            return None
        trs = []
        h = list(highs)[-n:]
        l = list(lows)[-n:]
        c = list(closes)[-(n + 1):-1]
        for i in range(n):
            tr = max(h[i] - l[i], abs(h[i] - c[i]), abs(l[i] - c[i]))
            trs.append(tr)
        return sum(trs) / len(trs) if trs else None

    def _peer_closes(self, iid: InstrumentId) -> dict[str, np.ndarray]:
        """Gather this instrument's peer close series for cross-asset features.

        Returns {} when the engine has no peer_symbols (feature off). Every
        cross-asset column PredictionEngine builds from this dict is lagged
        (>= 1 bar), so a peer missing its bar for the CURRENT timestamp simply
        means that peer's most-recent-available close is one bar stale --
        never a future one. All feature construction happens inside
        PredictionEngine; this strategy only assembles the raw closes.
        """
        peers = self._engines[iid].cfg.peer_symbols
        if not peers:
            return {}
        out = {}
        for raw in peers:
            piid = self._iid_by_raw.get(raw)
            if piid is None:
                continue
            pc = self._closes.get(piid)
            if pc:
                out[raw] = np.asarray(pc, dtype=float)
        return out

    def _flatten_all(self, reason: str) -> None:
        self.log.warning(f"FLATTEN ALL -- {reason}")
        for iid in self._bar_types:
            pos = self.portfolio.net_position(iid)
            if pos is not None and pos != 0:
                self.close_all_positions(iid)
        # We are now flat: free every concurrency slot and reset committed gross
        # notional so entries can resume (after a temporary daily halt) clean.
        self._committed_notional.clear()

    def _append_bar(self, bar: Bar) -> bool:
        """Append a bar once, returning False for duplicates/out-of-order data."""
        iid = bar.bar_type.instrument_id
        ts_ns = int(bar.ts_event)
        if ts_ns <= self._last_bar_ns[iid]:
            return False
        self._last_bar_ns[iid] = ts_ns
        self._closes[iid].append(float(bar.close))
        self._highs[iid].append(float(bar.high))
        self._lows[iid].append(float(bar.low))
        self._prev_close[iid] = float(bar.close)
        return True

    def on_historical_data(self, data) -> None:
        """Warm model history without evaluating signals or placing orders."""
        if isinstance(data, Bar):
            self._append_bar(data)

    # ---- main event -----------------------------------------------------
    def on_bar(self, bar: Bar) -> None:
        iid = bar.bar_type.instrument_id
        close = float(bar.close)
        if not self._append_bar(bar):
            return
        ts = datetime.fromtimestamp(int(bar.ts_event) / 1_000_000_000, tz=timezone.utc)

        # --- risk state machine (runs every bar) ---
        equity = self._equity()
        self._risk.on_new_day(ts, equity)
        prev_state = self._risk.state
        state = self._risk.update_equity(ts, equity)
        if state != prev_state:
            if state == TradingState.HALTED_DAILY:
                self._flatten_all("daily 2% loss limit hit -> 24h halt")
            elif state == TradingState.DISABLED_KILL:
                self._flatten_all("max drawdown kill-switch -> permanent disable")
        if self._risk.drawdown_warning:
            self.log.warning("Drawdown >= 5% warning threshold.")

        # Timestamp boundary: a bar with a new timestamp means the previous
        # timestamp's buffered signals are complete, so resolve them as one
        # cross-sectional batch before we start collecting this timestamp.
        if self._pending_ts is not None and ts != self._pending_ts:
            self._resolve_batch()
        self._pending_ts = ts

        closes = self._closes[iid]
        if len(closes) < self.config.warmup_bars:
            return

        # --- (re)train prediction engine on history seen so far (past only) ---
        # Refit through PredictionEngine.refit_on_history(), which uses the SAME
        # windowing contract as walk_forward(): expanding fit on all past rows.
        # refit_on_history returns False until there is enough history to build a
        # feature row.
        #
        # Refit CADENCE (refit_every_n_bars): refitting the Huber model is the
        # dominant per-bar cost, so we only retrain every `refit_every_n_bars`
        # post-warmup bars (a fixed structural knob, NOT Optuna-tuned). The very
        # first available bar -- and any bar before a baseline fit exists -- is
        # ALWAYS refit so the model is never used untrained; on skipped bars we
        # reuse the last fit. Because every fit only ever sees PAST closes and
        # predict_move builds its feature row from past closes too, a stale-but-
        # past fit introduces NO lookahead -- only model staleness between
        # refits. cadence=1 reproduces the original refit-every-bar behaviour.
        eng = self._engines[iid]
        peer_closes = self._peer_closes(iid)
        cadence = max(1, int(self.config.refit_every_n_bars))
        bar_index = self._bar_index[iid]
        self._bar_index[iid] += 1
        needs_baseline = not self._trained[iid]
        if needs_baseline or bar_index % cadence == 0:
            if not eng.refit_on_history(list(closes), peer_closes):
                return
            self._trained[iid] = True

        # --- alpha: get yhat, then BUFFER (submission happens in the batch) ---
        yhat = eng.predict_move(list(closes), peer_closes)
        if yhat is None:
            return

        atr = self._atr(iid)
        if atr is None or atr <= 0:
            return

        # Do NOT submit here. Buffer this instrument's signal and let the
        # cross-sectional batch decide who gets the scarce slots. When every
        # subscribed instrument has reported for this timestamp we resolve
        # immediately (same-bar submission timing); otherwise the batch resolves
        # when the timestamp advances (handles instruments missing a bar).
        self._pending[iid] = _PendingSignal(close=close, atr=atr, yhat=float(yhat))
        if len(self._pending) >= self._n_instruments:
            self._resolve_batch()

    def _resolve_batch(self) -> None:
        """Resolve one timestamp's buffered signals as a cross-sectional batch.

        Reversals/adjustments of already-held instruments run first -- they
        replace their own slot and so never consume a NEW one. The remaining
        flat instruments that want to open then compete for the free slots, and
        those slots are handed out in descending order of CONVICTION (|yhat|):
        when `max_open_positions` binds, the strongest signals win rather than
        whichever instrument the engine delivered first. `max_open_positions=0`
        (unlimited) simply opens every qualifying signal, order-independent.
        """
        pending, self._pending = self._pending, {}
        if not pending or not self._risk.can_open:
            return

        thr = self.config.entry_threshold
        equity = self._equity()

        reversals: list[tuple[InstrumentId, OrderSide, _PendingSignal]] = []
        new_entries: list[tuple[InstrumentId, OrderSide, _PendingSignal]] = []
        exits: list[InstrumentId] = []
        for iid, sig in pending.items():
            net = self.portfolio.net_position(iid)
            net = float(net) if net is not None else 0.0
            if not self.config.allow_short_positions:
                if net > 0 and sig.yhat < -thr:
                    exits.append(iid)
                    continue
                if net < 0:
                    # Flatten any externally-created short; do not reverse it
                    # into a long in the same batch.
                    exits.append(iid)
                    continue
                if sig.yhat < -thr:
                    continue
            if sig.yhat > thr and net <= 0:
                side = OrderSide.BUY
            elif sig.yhat < -thr and net >= 0:
                side = OrderSide.SELL
            else:
                continue  # HOLD, or signal already agrees with the open position
            # A non-zero committed notional means the instrument already holds a
            # slot, so reversing/adjusting it does not consume a new one.
            if self._committed_notional[iid] != 0.0:
                reversals.append((iid, side, sig))
            else:
                new_entries.append((iid, side, sig))

        for iid in exits:
            self.close_all_positions(iid)
            self._committed_notional[iid] = 0.0
            self.log.info(f"CLOSE {iid} -- bearish signal in long-only spot mode")

        for iid, side, sig in reversals:
            self._enter(iid, side, sig.close, sig.atr, equity, sig.yhat)

        # Highest conviction first. The cap check inside the loop stops handing
        # out slots once they are full, so weaker signals are dropped this bar.
        new_entries.sort(key=lambda t: abs(t[2].yhat), reverse=True)
        for iid, side, sig in new_entries:
            if self._committed_notional[iid] == 0.0 and not self._can_open_new_position():
                continue
            self._enter(iid, side, sig.close, sig.atr, equity, sig.yhat)

    def _open_position_count(self) -> int:
        """Instruments with a non-zero committed position (see _committed_notional)."""
        return sum(1 for v in self._committed_notional.values() if v != 0.0)

    def _can_open_new_position(self) -> bool:
        cap = self.config.max_open_positions
        if cap <= 0:  # 0 (or negative) -> unlimited
            return True
        return self._open_position_count() < cap

    def _available_notional(self, iid: InstrumentId, equity: float) -> float:
        """Remaining book-level gross-exposure headroom for a trade on `iid`.

        Book leverage=1 means TOTAL gross notional across all instruments must
        stay <= max_leverage * equity. This returns that budget minus the gross
        notional already committed to OTHER instruments (the current instrument
        is excluded because a reversal/adjust on it replaces its own notional,
        it does not stack on top). Clamped at 0.
        """
        budget = float(self._risk.cfg.max_leverage) * float(equity)
        gross_other = sum(
            v for k, v in self._committed_notional.items() if k != iid
        )
        return max(budget - gross_other, 0.0)

    def _edge_variance(self, iid: InstrumentId) -> float | None:
        """Variance of the horizon forward return, for the Kelly denominator.

        yhat is a cumulative log return over `horizon` bars. Under an iid
        approximation the variance of that sum is horizon * var(per-bar return),
        estimated from the trailing `kelly_vol_window` bars the strategy already
        buffers. Returns None when there is not enough history to estimate it.
        """
        closes = self._closes[iid]
        win = self.config.kelly_vol_window
        if len(closes) < win + 2:
            return None
        arr = np.asarray(list(closes)[-(win + 1):], dtype=float)
        rets = np.diff(np.log(arr))
        var_bar = float(np.var(rets))
        if var_bar <= 0:
            return None
        return var_bar * float(self.config.horizon)

    # ---- order construction (objects, not raw buy()) -------------------
    def _enter(
        self,
        iid,
        side: OrderSide,
        price: float,
        atr: float,
        equity: float,
        yhat: float,
    ):
        stop_dist = self.config.atr_stop_mult * atr
        stop_price = price - stop_dist if side == OrderSide.BUY else price + stop_dist
        # Book-level leverage=1: cap this trade's notional to the gross-exposure
        # headroom left after other instruments' commitments. None -> guard off
        # (per-trade caps only).
        avail = (
            self._available_notional(iid, equity)
            if self.config.enforce_portfolio_leverage
            else None
        )
        # Kelly conviction sizing (if enabled) scales size by |yhat|/variance,
        # but RiskManager.kelly_size_for_trade floors it at the same 0.25% risk
        # cap / leverage rail, so it can only ever de-risk below the flat sizer.
        if self.config.use_kelly_sizing:
            variance = self._edge_variance(iid)
            if variance is None:
                raw_qty = self._risk.size_for_trade(
                    equity, price, stop_price, available_notional=avail
                )
            else:
                raw_qty = self._risk.kelly_size_for_trade(
                    equity,
                    price,
                    stop_price,
                    edge=abs(float(yhat)),
                    variance=variance,
                    kelly_fraction=self.config.kelly_fraction,
                    available_notional=avail,
                )
        else:
            raw_qty = self._risk.size_for_trade(
                equity, price, stop_price, available_notional=avail
            )
        if raw_qty <= 0:
            return

        instrument = self.cache.instrument(iid)
        if instrument is None:
            self.log.error(f"Instrument {iid} not in cache.")
            return
        # make_qty rounds the fractional risk-sized qty to the instrument's size
        # precision (crypto allows fractional coins; equities round to whole
        # shares). If it rounds to zero the position is too small to place --
        # for a whole-unit instrument (e.g. equities) Nautilus RAISES rather
        # than returning a zero Quantity, so treat that the same as "too small".
        try:
            target_qty = instrument.make_qty(raw_qty)
        except ValueError:
            return
        if target_qty.as_double() <= 0:
            return

        # A reversal order must first flatten the existing position and then
        # establish the new risk-sized target. The committed exposure remains
        # the target quantity, not the larger transition-order quantity.
        net = self.portfolio.net_position(iid)
        net = float(net) if net is not None else 0.0
        reversing = (side == OrderSide.BUY and net < 0) or (
            side == OrderSide.SELL and net > 0
        )
        try:
            qty = instrument.make_qty(
                target_qty.as_double() + abs(net) if reversing else target_qty.as_double()
            )
        except ValueError:
            return

        if self.config.use_limit_orders:
            off = price * (self.config.limit_offset_bps / 10_000.0)
            limit_px = price - off if side == OrderSide.BUY else price + off
            order = self.order_factory.limit(
                instrument_id=iid,
                order_side=side,
                quantity=qty,
                price=instrument.make_price(limit_px),
                time_in_force=TimeInForce.GTC,
            )
        else:
            # IBKR requires market BUYs for spot crypto to use a quote-currency
            # cash amount. The adapter represents CRYPTO as inverse, so opt in
            # to quote quantity for that one case; market SELLs and all LIMIT
            # orders remain fractional base-coin quantities.
            quote_quantity = bool(
                getattr(instrument, "is_inverse", False) and side == OrderSide.BUY
            )
            order_qty = qty
            if quote_quantity:
                try:
                    order_qty = instrument.make_qty(qty.as_double() * price)
                except ValueError:
                    return
            order = self.order_factory.market(
                instrument_id=iid,
                order_side=side,
                quantity=order_qty,
                time_in_force=(
                    TimeInForce.IOC
                    if getattr(instrument, "is_inverse", False)
                    else TimeInForce.GTC
                ),
                quote_quantity=quote_quantity,
            )
        self.submit_order(order)
        # Record the committed gross notional synchronously so BOTH the
        # concurrency cap and the book-level leverage guard see this holding
        # immediately, before the fill (and resulting net_position) lands. Using
        # the ordered qty * price is a conservative synchronous proxy; on a
        # reversal it replaces (not stacks on) the instrument's prior notional.
        self._committed_notional[iid] = target_qty.as_double() * price
        self.log.info(
            f"{side.name} {qty} {iid} @~{price:.2f} stop~{stop_price:.2f} "
            f"(risk-sized, leverage=1)"
        )

    def _reconcile_committed_notional(self, iid: InstrumentId | None = None) -> None:
        """Rebuild exposure commitments from reconciled positions/open orders."""
        iids = [iid] if iid is not None else list(self._bar_types)
        for current in iids:
            notional = 0.0
            fallback_px = self._closes[current][-1] if self._closes[current] else 0.0
            net = self.portfolio.net_position(current)
            if net is not None:
                notional += abs(float(net)) * fallback_px
            for order in self.cache.orders_open(
                instrument_id=current,
                strategy_id=self.id,
            ):
                qty = getattr(order, "leaves_qty", order.quantity).as_double()
                if order.is_quote_quantity:
                    notional += abs(qty)
                else:
                    order_px = getattr(order, "price", None)
                    px = order_px.as_double() if order_px is not None else fallback_px
                    notional += abs(qty * px)
            self._committed_notional[current] = notional

    def on_event(self, event: Event) -> None:
        if isinstance(event, (OrderFilled, OrderCanceled, OrderRejected, OrderExpired)):
            iid = getattr(event, "instrument_id", None)
            if iid in self._bar_types:
                self._reconcile_committed_notional(iid)

    def on_save(self) -> dict[str, bytes]:
        if self._risk is None:
            return {}
        payload = {
            "version": 1,
            "instrument_ids": sorted(self.config.instrument_ids),
            "risk": self._risk.snapshot(),
            "account_equity_baseline": self._account_equity_baseline,
            "instruments": {
                str(iid): {
                    "closes": list(self._closes[iid]),
                    "highs": list(self._highs[iid]),
                    "lows": list(self._lows[iid]),
                    "last_bar_ns": self._last_bar_ns[iid],
                    "bar_index": self._bar_index[iid],
                }
                for iid in self._bar_types
            },
        }
        return {"ml_strategy.json": json.dumps(payload).encode("utf-8")}

    def on_load(self, state: dict[str, bytes]) -> None:
        raw = state.get("ml_strategy.json")
        if raw is None:
            return
        payload = json.loads(raw.decode("utf-8"))
        if payload.get("version") != 1:
            raise ValueError(f"Unsupported MLStrategy state version: {payload.get('version')}")
        self._loaded_state = payload

    def _restore_loaded_state(self) -> None:
        payload, self._loaded_state = self._loaded_state, None
        if payload is None:
            return
        saved_ids = payload.get("instrument_ids")
        if saved_ids is not None and sorted(saved_ids) != sorted(self.config.instrument_ids):
            self.log.warning(
                "Ignoring persisted strategy state for a different instrument universe"
            )
            return
        raw_baseline = payload.get("account_equity_baseline")
        if self.config.use_allocated_equity and raw_baseline is None:
            # A pre-allocation-aware snapshot contains the broker's full paper
            # NAV as its risk peak. Restoring it against a $5k allocation would
            # immediately trigger a false ~99% drawdown.
            self.log.warning(
                "Ignoring legacy risk snapshot without an allocated-equity baseline"
            )
        else:
            self._risk.restore(payload["risk"])
        if raw_baseline is not None:
            self._account_equity_baseline = float(raw_baseline)
        for raw, values in payload.get("instruments", {}).items():
            iid = self._iid_by_raw.get(raw)
            if iid is None:
                continue
            self._closes[iid].extend(values.get("closes", ()))
            self._highs[iid].extend(values.get("highs", ()))
            self._lows[iid].extend(values.get("lows", ()))
            self._last_bar_ns[iid] = int(values.get("last_bar_ns", 0))
            self._bar_index[iid] = int(values.get("bar_index", 0))

    def on_stop(self) -> None:
        self._resolve_batch()
        for bt in self._bar_types.values():
            self.unsubscribe_bars(bt)
