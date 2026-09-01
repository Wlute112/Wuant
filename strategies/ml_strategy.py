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
from nautilus_trader.model.data import Bar, BarType, QuoteTick, TradeTick
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.events import (
    OrderAccepted,
    OrderCancelRejected,
    OrderCanceled,
    OrderDenied,
    OrderExpired,
    OrderFilled,
    OrderPendingCancel,
    OrderModifyRejected,
    OrderRejected,
    OrderSubmitted,
    OrderUpdated,
)
from nautilus_trader.model.identifiers import AccountId, ClientOrderId, InstrumentId
from nautilus_trader.model.objects import Quantity
from nautilus_trader.trading.strategy import Strategy

from quant.data.quality import BarQualityGate
from quant.models.prediction_engine import PredictionConfig, PredictionEngine
from quant.news.core import NewsFeatureReader, NewsFeatureSnapshot
from quant.ops.state import OperationsStore
from quant.run.nautilus_reconciliation import snapshot_from_nautilus_cache
from quant.run.reconciliation import ReconciliationConfig, reconcile, recover_ledger
from quant.run.telemetry import LiveTelemetryRecorder
from quant.strategies.execution_state import (
    ExecutionLedger,
    ExecutionSafetyController,
    ExecutionSafetyState,
    LifecycleStatus,
    OrderRole,
)
from quant.strategies.risk import RiskConfig, RiskManager, TradingState
from quant.strategies.sessions import (
    ExchangeSessionCalendar,
    SessionPhase,
    SessionPolicy,
    SessionPolicyMode,
)


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

    # 0 keeps the original expanding Huber fit. Positive values retain only
    # the newest labeled feature rows at each refit, allowing Optuna's purged
    # walk-forward objective to validate adaptation to changing regimes.
    training_window_bars: int = 0

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
    regime_bull_threshold: float = 0.02
    regime_bear_threshold: float = -0.02
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

    # --- causal live/historical news alpha -------------------------------
    # The reader sees only articles whose first received_at timestamp is no
    # later than the completed bar.  Raw mode contributes a tightly bounded
    # return adjustment immediately; fit mode lets Huber estimate its weight.
    use_news_features: bool = True
    news_source: str = "raw"           # "fit" | "raw"
    news_raw_scale: float = 0.001
    news_score_clip: float = 1.0
    news_data_path: str = ""
    news_half_life_hours: float = 12.0
    news_max_age_hours: float = 72.0
    news_direct_weight: float = 1.0
    news_industry_weight: float = 0.45
    news_commodity_weight: float = 0.55
    news_macro_weight: float = 0.20

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
    max_order_notional_pct: float = 0.10
    max_symbol_exposure_pct: float = 0.25
    max_sector_exposure_pct: float = 0.30
    max_gross_exposure_pct: float = 1.0
    max_concentration_pct: float = 1.0
    price_collar_pct: float = 0.05

    # --- live execution safety (runtime-owned; identical for paper/live) ---
    execution_mode: str = "backtest"  # backtest | paper | live
    asset_class: str = "crypto"
    entry_time_in_force: str = "GTC"
    stale_entry_bars: int = 1
    rejection_suspend_threshold: int = 1
    enable_broker_protection: bool = False
    protection_target_rr: float = 2.0
    risk_check_interval_secs: int = 0
    max_market_data_age_secs: int = 15
    startup_health_grace_secs: int = 30
    cancel_ack_timeout_secs: int = 30
    require_session_schedule: bool = False
    session_policy: str = "RTH_ONLY"
    opening_buffer_minutes: int = 5
    closing_buffer_minutes: int = 5
    no_new_entry_minutes_before_close: int = 15
    participate_opening_auction: bool = False
    participate_closing_auction: bool = False
    cancel_entries_at_session_end: bool = True
    # Live/paper startup requests enough historical bars to warm the alpha
    # without submitting orders for that history. Ignored by backtests.
    request_historical_bars: bool = False
    bootstrap_lookback_days: int = 400
    # Optional local paper/live telemetry bridge. Blank in backtests, so this
    # has zero effect on simulation performance or strategy behavior.
    telemetry_path: str = ""
    telemetry_asset_class: str = "crypto"
    telemetry_mode: str = "paper"
    telemetry_include_extended_hours: bool = False
    telemetry_max_points: int = 750
    telemetry_target_rr: float = 2.0
    # Shared SQLite operational control plane. Blank keeps backtests and
    # explicitly diagnostic broker sessions free of external dependencies.
    operations_db_path: str = ""
    operations_component_id: str = ""
    external_supervisor_component: str = ""
    require_external_supervisor: bool = False
    external_supervisor_max_age_secs: int = 15
    expected_bar_interval_secs: int = 86_400
    data_gap_max_intervals: float = 3.0
    data_quality_recovery_bars: int = 3
    # Adapter-specific order metadata is assembled by the runner. The strategy
    # merely forwards opaque tags, preserving its venue-agnostic boundary.
    order_tags: tuple[str, ...] = ()

    # Backtest-only fold controls. They let the optimizer warm the model on a
    # fold's training history, freeze it before the embargo, and enable orders
    # only when validation begins. Zero leaves ordinary backtests unchanged.
    backtest_model_fit_end_ns: int = 0
    backtest_trade_start_ns: int = 0


class MLStrategy(Strategy):
    def __init__(self, config: MLStrategyConfig):
        super().__init__(config)
        self._engines: dict[InstrumentId, PredictionEngine] = {}
        # maxlen stays large even when Huber fitting uses a rolling window:
        # regime/cross-asset features still need the full past, while
        # PredictionEngine applies training_window_bars only to labeled fit rows.
        self._closes: dict[InstrumentId, deque] = defaultdict(
            lambda: deque(maxlen=100_000)
        )
        self._bar_times: dict[InstrumentId, deque] = defaultdict(
            lambda: deque(maxlen=100_000)
        )
        self._news_scores: dict[InstrumentId, deque] = defaultdict(
            lambda: deque(maxlen=100_000)
        )
        self._news_meta: dict[InstrumentId, NewsFeatureSnapshot] = {}
        self._news_reader: NewsFeatureReader | None = None
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
        self._raw_by_iid: dict[InstrumentId, str] = {}
        self._last_bar_ns: dict[InstrumentId, int] = defaultdict(int)
        self._data_quality = BarQualityGate(
            expected_interval_seconds=max(1, config.expected_bar_interval_secs),
            continuous_market=config.asset_class == "crypto",
            max_gap_intervals=config.data_gap_max_intervals,
        )
        self._data_quality_issues: deque[dict] = deque(maxlen=100)
        self._data_quality_blocked_instruments: set[str] = set()
        self._data_quality_good_bars: dict[str, int] = defaultdict(int)
        # Nautilus loads strategy state before on_start constructs the risk
        # manager and instrument maps, so on_load stages the decoded payload.
        self._loaded_state: dict | None = None
        self._account_equity_baseline: float | None = None
        self._telemetry: LiveTelemetryRecorder | None = None
        self._telemetry_failed = False
        self._historical_telemetry_count = 0
        self._operations: OperationsStore | None = None
        self._operations_failed = False
        self._external_supervisor_unhealthy = False
        self._last_audited_safety_state: tuple[str, str] | None = None
        # Model-derived reference levels for positions/orders. Telemetry marks
        # them broker-guaranteed only after both OCA protection legs are
        # acknowledged for the actual filled position.
        self._position_references: dict[InstrumentId, dict] = {}
        self._execution = ExecutionLedger()
        self._execution_safety = ExecutionSafetyController(
            max_rejections=max(1, int(config.rejection_suspend_threshold))
        )
        self._session_calendars: dict[InstrumentId, ExchangeSessionCalendar] = {}
        self._last_session_phase: dict[InstrumentId, SessionPhase] = {}
        self._last_mark: dict[InstrumentId, float] = {}
        self._entry_submitted_bar: dict[str, int] = {}
        self._pending_exits: dict[InstrumentId, str] = {}
        self._protection_ids: dict[InstrumentId, dict[str, str]] = {}
        self._startup_protection_pending: set[InstrumentId] = set()
        self._started_at = None
        self._risk_timer_name = f"RISK_SUPERVISOR-{self.id}"
        self._staged_exit_timer_name = f"STAGED-EXIT-{self.id}"
        self._staged_exit_started_at = None
        self._staged_exit_active = False
        self._reconciliation_state = (
            "NOT_STARTED"
            if config.execution_mode in {"paper", "live"}
            else "NOT_APPLICABLE"
        )

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
                max_order_notional_pct=self.config.max_order_notional_pct,
                max_symbol_exposure_pct=self.config.max_symbol_exposure_pct,
                max_sector_exposure_pct=self.config.max_sector_exposure_pct,
                max_gross_exposure_pct=self.config.max_gross_exposure_pct,
                max_concentration_pct=self.config.max_concentration_pct,
                price_collar_pct=self.config.price_collar_pct,
            ),
        )
        self._started_at = self.clock.utc_now()
        if self.config.operations_db_path:
            try:
                self._operations = OperationsStore(self.config.operations_db_path)
            except Exception as exc:  # noqa: BLE001 - broker execution fails closed below
                self._operations_failed = True
                if self.config.execution_mode in {"paper", "live"}:
                    self._execution_safety.mark_uncertain(
                        f"Operational control database unavailable: {exc}",
                        ts_ns=self.clock.timestamp_ns(),
                    )
                self.log.error(f"Operational control database unavailable: {exc}")
        self._n_instruments = len(self.config.instrument_ids)
        if self.config.use_news_features and self.config.news_data_path:
            try:
                self._news_reader = NewsFeatureReader(
                    self.config.news_data_path,
                    half_life_hours=self.config.news_half_life_hours,
                    max_age_hours=self.config.news_max_age_hours,
                    direct_weight=self.config.news_direct_weight,
                    industry_weight=self.config.news_industry_weight,
                    commodity_weight=self.config.news_commodity_weight,
                    macro_weight=self.config.news_macro_weight,
                )
            except Exception as exc:  # noqa: BLE001 - news is additive, risk stays active
                self.log.error(f"News feature reader unavailable; using neutral scores: {exc}")
        if self.config.telemetry_path:
            self._telemetry = LiveTelemetryRecorder(
                self.config.telemetry_path,
                asset_class=self.config.telemetry_asset_class,
                mode=self.config.telemetry_mode,
                bar_type=self.config.bar_type_suffix,
                max_points=self.config.telemetry_max_points,
                include_extended_hours=self.config.telemetry_include_extended_hours,
            )
        for raw in self.config.instrument_ids:
            iid = InstrumentId.from_str(raw)
            self._iid_by_raw[raw] = iid
            self._raw_by_iid[iid] = raw
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
                    training_window_bars=self.config.training_window_bars,
                    use_regime_features=self.config.use_regime_features,
                    use_hmm_feature=self.config.use_hmm_feature,
                    regime_window=self.config.regime_window,
                    regime_bull_threshold=self.config.regime_bull_threshold,
                    regime_bear_threshold=self.config.regime_bear_threshold,
                    regime_source=self.config.regime_source,
                    hmm_source=self.config.hmm_source,
                    regime_raw_scale=self.config.regime_raw_scale,
                    hmm_raw_scale=self.config.hmm_raw_scale,
                    cross_asset_lags=self.config.cross_asset_lags,
                    spread_lags=self.config.spread_lags,
                    peer_symbols=peers,
                    use_news_features=self.config.use_news_features,
                    news_source=self.config.news_source,
                    news_raw_scale=self.config.news_raw_scale,
                    news_score_clip=self.config.news_score_clip,
                    huber_alpha=self.config.huber_alpha,
                    huber_epsilon=self.config.huber_epsilon,
                )
            )
            self.subscribe_bars(bt)
            if self.config.execution_mode in {"paper", "live"}:
                self.subscribe_quote_ticks(iid)
                self.subscribe_trade_ticks(iid)
                self.subscribe_instrument_status(iid)
                self._initialize_session_calendar(iid)
            self.log.info(f"Subscribed {bt}")
        self._restore_loaded_state()
        if self.config.require_session_schedule:
            missing_schedules = [
                str(iid) for iid in self._bar_types if iid not in self._session_calendars
            ]
            if missing_schedules:
                self._execution_safety.mark_uncertain(
                    "Required IBKR exchange sessions unavailable after state restore: "
                    + ", ".join(missing_schedules),
                    ts_ns=self.clock.timestamp_ns(),
                )
        self._adopt_reconciled_broker_orders()
        if self.config.execution_mode in {"paper", "live"}:
            self._reconcile_broker_cache_source_of_truth()
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
        self._reconcile_protection_after_restart()
        self._audit_event(
            "STRATEGY_STARTED",
            {
                "execution_mode": self.config.execution_mode,
                "instrument_ids": sorted(self.config.instrument_ids),
                "account_id": self.config.account_id,
                "reconciliation_state": self._reconciliation_state,
            },
            severity="INFO",
            event_id=f"strategy-started:{self._operations_target()}:{self.clock.timestamp_ns()}",
        )
        self._operations_heartbeat()
        if self.config.risk_check_interval_secs > 0:
            self.clock.set_timer(
                name=self._risk_timer_name,
                interval=timedelta(seconds=self.config.risk_check_interval_secs),
                callback=self.on_time_event,
            )

    # ---- helpers --------------------------------------------------------
    def _operations_target(self) -> str:
        return self.config.operations_component_id or f"strategy:{self.id}"

    def _audit_event(
        self,
        event_type: str,
        payload: dict,
        *,
        severity: str = "INFO",
        correlation_id: str = "",
        event_id: str | None = None,
    ) -> bool:
        if self._operations is None or self._operations_failed:
            return not bool(self.config.operations_db_path)
        try:
            self._operations.append_event(
                self._operations_target(),
                event_type,
                payload,
                severity=severity,
                correlation_id=correlation_id,
                event_id=event_id,
            )
            return True
        except Exception as exc:  # noqa: BLE001 - an unavailable audit trail is unsafe
            self._operations_failed = True
            self.log.error(f"Operational audit failed; execution frozen: {exc}")
            if self.config.execution_mode in {"paper", "live"}:
                self._execution_safety.mark_uncertain(
                    f"Required operational audit failed: {exc}",
                    ts_ns=self.clock.timestamp_ns(),
                )
            return False

    def _operations_heartbeat(self) -> None:
        if self._operations is None or self._operations_failed or self._risk is None:
            return
        try:
            self._operations.heartbeat(
                self._operations_target(),
                str(self.id),
                status=self._execution_safety.state.value,
                details={
                    "execution_mode": self.config.execution_mode,
                    "risk_state": self._risk.state.value,
                    "entries_allowed": (
                        self._risk.can_open and self._execution_safety.entries_allowed
                    ),
                    "open_orders": len(self.cache.orders_open(strategy_id=self.id)),
                    "inflight_orders": len(self.cache.orders_inflight(strategy_id=self.id)),
                    "open_positions": len(self.cache.positions_open(strategy_id=self.id)),
                },
                observed_at=self.clock.utc_now(),
            )
        except Exception as exc:  # noqa: BLE001
            self._audit_event(
                "HEARTBEAT_WRITE_FAILED",
                {"error": f"{type(exc).__name__}: {exc}"},
                severity="CRITICAL",
            )

    def _external_supervisor_is_fresh(self) -> bool:
        if not self.config.require_external_supervisor:
            return True
        if self._operations is None or not self.config.external_supervisor_component:
            return False
        heartbeat = self._operations.get_heartbeat(
            self.config.external_supervisor_component
        )
        if heartbeat is None or heartbeat.get("status") not in {"RUNNING", "HEALTHY"}:
            return False
        try:
            observed = datetime.fromisoformat(str(heartbeat["observed_at"]))
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return False
        return self.clock.utc_now() - observed.astimezone(timezone.utc) <= timedelta(
            seconds=max(1, self.config.external_supervisor_max_age_secs)
        )

    def _process_operations_control(self) -> None:
        if self._operations is None or self._operations_failed or self._risk is None:
            return
        target = self._operations_target()
        supervisor_fresh = self._external_supervisor_is_fresh()
        grace_elapsed = (
            self._started_at is not None
            and self.clock.utc_now() - self._started_at
            >= timedelta(seconds=max(0, self.config.startup_health_grace_secs))
        )
        if self.config.require_external_supervisor and grace_elapsed and not supervisor_fresh:
            if not self._external_supervisor_unhealthy:
                self._external_supervisor_unhealthy = True
                self._execution_safety.freeze(
                    "Required external risk-supervisor heartbeat is stale or missing.",
                    ts_ns=self.clock.timestamp_ns(),
                )
                self._cancel_working_entry_orders(
                    reason="external risk supervisor unavailable"
                )
                self._audit_event(
                    "EXTERNAL_SUPERVISOR_UNAVAILABLE",
                    {"component": self.config.external_supervisor_component},
                    severity="CRITICAL",
                )
        elif supervisor_fresh and self._external_supervisor_unhealthy:
            self._external_supervisor_unhealthy = False
            self._audit_event(
                "EXTERNAL_SUPERVISOR_RECOVERED",
                {"component": self.config.external_supervisor_component},
                severity="WARNING",
            )

        try:
            commands = self._operations.claim_commands(target, target)
        except Exception as exc:  # noqa: BLE001
            self._audit_event(
                "CONTROL_COMMAND_POLL_FAILED",
                {"error": f"{type(exc).__name__}: {exc}"},
                severity="CRITICAL",
            )
            return
        for command in commands:
            try:
                if command.action == "FREEZE_ENTRIES":
                    self._execution_safety.freeze(
                        command.reason,
                        ts_ns=self.clock.timestamp_ns(),
                    )
                    self._cancel_working_entry_orders(reason=command.reason)
                    self._operations.complete_command(
                        command.command_id,
                        target,
                        success=True,
                        result={"entries_allowed": False},
                    )
                elif command.action == "RESUME_ENTRIES":
                    allowed = bool(
                        supervisor_fresh
                        and self._risk.state == TradingState.ACTIVE
                        and not self._external_supervisor_unhealthy
                    )
                    if allowed:
                        self._execution_safety.resume(
                            command.reason,
                            ts_ns=self.clock.timestamp_ns(),
                        )
                    self._operations.complete_command(
                        command.command_id,
                        target,
                        success=allowed,
                        result={"entries_allowed": self._execution_safety.entries_allowed},
                    )
                elif command.action in {"CANCEL_ALL", "FLATTEN", "KILL"}:
                    permanent = command.action == "KILL"
                    if permanent:
                        self._risk.engage_kill_switch()
                    self._begin_risk_exit(command.reason, permanent=permanent)
                    self._operations.acknowledge_command(
                        command.command_id,
                        target,
                        result={"broker_confirmation_pending": self._has_broker_exposure()},
                    )
                else:
                    self._operations.complete_command(
                        command.command_id,
                        target,
                        success=False,
                        result={"error": f"unsupported action {command.action}"},
                    )
            except Exception as exc:  # noqa: BLE001
                self._operations.complete_command(
                    command.command_id,
                    target,
                    success=False,
                    result={"error": f"{type(exc).__name__}: {exc}"},
                )
                self._execution_safety.mark_uncertain(
                    f"Operational command {command.action} failed: {exc}",
                    ts_ns=self.clock.timestamp_ns(),
                )

        for command in self._operations.acknowledged_commands(target, target):
            if command.action not in {"CANCEL_ALL", "FLATTEN", "KILL"}:
                continue
            if not self._has_broker_exposure() and not self.is_exiting():
                self._operations.complete_command(
                    command.command_id,
                    target,
                    success=True,
                    result={"broker_flat_confirmed": True},
                )

    def _audit_safety_state_if_changed(self) -> None:
        if self._risk is None:
            return
        current = (self._risk.state.value, self._execution_safety.state.value)
        if current == self._last_audited_safety_state:
            return
        previous = self._last_audited_safety_state
        self._last_audited_safety_state = current
        self._audit_event(
            "RISK_STATE_CHANGED",
            {
                "previous": previous,
                "risk_state": current[0],
                "execution_state": current[1],
                "entries_allowed": (
                    self._risk.can_open and self._execution_safety.entries_allowed
                ),
            },
            severity=("CRITICAL" if current[0] == TradingState.DISABLED_KILL.value else "WARNING"),
        )

    def _equity(self) -> float:
        try:
            acct = self._account()
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

    def _account(self):
        try:
            if self.config.account_id:
                account = self.portfolio.account(
                    account_id=AccountId.from_str(self.config.account_id)
                )
                if account is not None:
                    return account
            if self._bar_types:
                account = self.portfolio.account(self._venue_for_any())
                if account is not None:
                    return account
            accounts = self.cache.accounts()
            return accounts[0] if len(accounts) == 1 else None
        except Exception:  # noqa: BLE001 - callers fail closed in broker modes
            return None

    def _initialize_session_calendar(self, iid: InstrumentId) -> None:
        if self.config.asset_class != "equity":
            return
        instrument = self.cache.instrument(iid)
        info = getattr(instrument, "info", None) if instrument is not None else None
        try:
            policy = SessionPolicy(
                mode=SessionPolicyMode(self.config.session_policy),
                opening_buffer_minutes=self.config.opening_buffer_minutes,
                closing_buffer_minutes=self.config.closing_buffer_minutes,
                no_new_entry_minutes_before_close=(
                    self.config.no_new_entry_minutes_before_close
                ),
                participate_opening_auction=self.config.participate_opening_auction,
                participate_closing_auction=self.config.participate_closing_auction,
                cancel_entries_at_session_end=self.config.cancel_entries_at_session_end,
            )
            calendar = ExchangeSessionCalendar.from_instrument_info(
                info,
                policy=policy,
                max_market_data_age=timedelta(
                    seconds=max(1, self.config.max_market_data_age_secs)
                ),
            )
        except (TypeError, ValueError) as exc:
            if self.config.require_session_schedule:
                self._execution_safety.mark_uncertain(
                    f"Cannot establish IBKR exchange session for {iid}: {exc}",
                    ts_ns=self.clock.timestamp_ns(),
                )
                self.log.error(
                    f"Trading suspended: cannot establish IBKR exchange session for {iid}: {exc}"
                )
            return
        self._session_calendars[iid] = calendar
        self._last_session_phase[iid] = calendar.phase_at(
            self.clock.utc_now(),
            enforce_data_health=False,
        )

    def _entry_tif(self) -> TimeInForce:
        try:
            return TimeInForce[self.config.entry_time_in_force.upper()]
        except KeyError as exc:
            raise ValueError(
                f"unsupported entry_time_in_force {self.config.entry_time_in_force!r}"
            ) from exc

    def _broker_position_state(self, iid: InstrumentId) -> tuple[float, float | None]:
        positions = self.cache.positions_open(
            instrument_id=iid,
            strategy_id=self.id,
        )
        if not positions:
            net = self.portfolio.net_position(iid)
            return (float(net) if net is not None else 0.0), None
        signed = sum(float(position.signed_qty) for position in positions)
        total = sum(abs(float(position.signed_qty)) for position in positions)
        average = (
            sum(
                abs(float(position.signed_qty)) * float(position.avg_px_open)
                for position in positions
            )
            / total
            if total > 0
            else None
        )
        return signed, average

    def _adopt_reconciled_broker_orders(self) -> None:
        if self.config.execution_mode not in {"paper", "live"}:
            return
        unknown: list[str] = []
        protection_roles = {
            values.get("stop"): OrderRole.STOP_LOSS
            for values in self._protection_ids.values()
            if values.get("stop")
        }
        protection_roles.update(
            {
                values.get("target"): OrderRole.TAKE_PROFIT
                for values in self._protection_ids.values()
                if values.get("target")
            }
        )
        for order in self.cache.orders(strategy_id=self.id):
            order_id = str(order.client_order_id)
            if order_id in self._execution.orders:
                continue
            role = (
                OrderRole.EMERGENCY_EXIT
                if "MARKET_EXIT" in (order.tags or [])
                else protection_roles.get(order_id, OrderRole.UNKNOWN)
            )
            self._execution.register_order(
                client_order_id=order_id,
                instrument_id=str(order.instrument_id),
                side=order.side.name,
                requested_quantity=order.quantity.as_double(),
                role=role,
                signal_version="RECONCILED_EXTERNAL",
                ts_ns=self.clock.timestamp_ns(),
            )
            if role == OrderRole.UNKNOWN and not order.is_closed:
                unknown.append(order_id)
        if unknown:
            self._execution_safety.mark_uncertain(
                f"Reconciliation found {len(unknown)} unclassified broker order(s): "
                f"{', '.join(unknown)}",
                ts_ns=self.clock.timestamp_ns(),
            )

    def _reconcile_broker_cache_source_of_truth(self) -> None:
        """Recover deterministic broker events and fail on unresolved account state."""
        try:
            snapshot = snapshot_from_nautilus_cache(
                self.cache,
                self._execution,
                strategy_id=self.id,
                expected_account_id=self.config.account_id,
                captured_at_ns=self.clock.timestamp_ns(),
            )
            config = ReconciliationConfig(
                expected_account_id=self.config.account_id,
                required_base_currency="USD",
                allow_unmanaged_positions=False,
                allow_unmanaged_orders=False,
            )
            initial = reconcile(self._execution, snapshot, config)
            recovered = recover_ledger(self._execution, snapshot, initial)
            final = reconcile(recovered, snapshot, config)
            self._execution = recovered
            self._reconciliation_state = (
                "BROKER_RECONCILED" if final.passed else "UNCERTAIN"
            )
            self._audit_event(
                "BROKER_RECONCILIATION_COMPLETED",
                {
                    "initial": initial.as_dict(),
                    "final": final.as_dict(),
                    "deterministic_recoveries": len(initial.actions),
                },
                severity="INFO" if final.passed else "CRITICAL",
            )
            if not final.passed:
                reasons = "; ".join(
                    issue.message
                    for issue in final.issues
                    if issue.severity.value == "CRITICAL"
                ) or "broker reconciliation did not pass"
                self._execution_safety.mark_uncertain(
                    reasons,
                    ts_ns=self.clock.timestamp_ns(),
                )
        except Exception as exc:  # noqa: BLE001 - startup reconciliation fails closed
            self._reconciliation_state = "UNCERTAIN"
            self._execution_safety.mark_uncertain(
                f"Broker source-of-truth reconciliation failed: {exc}",
                ts_ns=self.clock.timestamp_ns(),
            )
            self._audit_event(
                "BROKER_RECONCILIATION_FAILED",
                {"error": f"{type(exc).__name__}: {exc}"},
                severity="CRITICAL",
            )

    def _session_allows_entry(self, iid: InstrumentId, when: datetime) -> tuple[bool, str]:
        calendar = self._session_calendars.get(iid)
        if calendar is None:
            if self.config.require_session_schedule:
                return False, "SESSION_SCHEDULE_UNAVAILABLE"
            return True, "NOT_REQUIRED"
        allowed, reason = calendar.allows_new_entry(when)
        if not allowed:
            return False, reason
        order_type = "LIMIT" if self.config.use_limit_orders else "MARKET"
        return calendar.validates_order(
            when,
            order_type=order_type,
            time_in_force=self.config.entry_time_in_force,
            is_entry=True,
        )

    def _cancel_working_entry_orders(
        self,
        iid: InstrumentId | None = None,
        *,
        reason: str,
    ) -> int:
        canceled = 0
        for record in self._execution.working_orders(
            instrument_id=str(iid) if iid is not None else None,
            roles={OrderRole.ENTRY},
        ):
            order = self.cache.order(ClientOrderId(record.client_order_id))
            if order is None or order.is_closed:
                continue
            if record.status != LifecycleStatus.PENDING_CANCEL:
                if self._cancel_order_safely(order, record, reason=reason):
                    canceled += 1
                    self.log.warning(
                        f"Canceling working entry {record.client_order_id} for {record.instrument_id}: {reason}"
                    )
        return canceled

    def _cancel_order_safely(self, order, record=None, *, reason: str) -> bool:
        try:
            self.cancel_order(order)
        except Exception as exc:
            self._execution_safety.suspend(
                f"Local cancellation failed for {order.client_order_id}: {exc}",
                code="LOCAL_CANCEL_FAILED",
                ts_ns=self.clock.timestamp_ns(),
                client_order_id=str(order.client_order_id),
                instrument_id=str(order.instrument_id),
            )
            self.log.error(
                f"Cancellation failed for {order.client_order_id} ({reason}): {exc}"
            )
            return False
        if record is not None:
            self._execution.apply_order_state(
                record.client_order_id,
                LifecycleStatus.PENDING_CANCEL,
                ts_ns=self.clock.timestamp_ns(),
            )
        return True

    def _supervise_risk(self, when: datetime) -> None:
        if self._risk is None:
            return
        broker_mode = self.config.execution_mode in {"paper", "live"}
        if broker_mode:
            account = self._account()
            try:
                total_balance = account.balance_total() if account is not None else None
                base_currency = getattr(account, "base_currency", None)
            except Exception:  # noqa: BLE001 - fail closed below
                total_balance = None
                base_currency = None
            account_failure = None
            if account is None:
                account_failure = "Broker account state is unavailable."
            elif total_balance is None:
                account_failure = "Broker total-equity data is unavailable."
            elif base_currency is not None and str(base_currency) != "USD":
                account_failure = (
                    f"Unsupported broker base currency {base_currency}; USD is required."
                )
            if account_failure is not None:
                if self._execution_safety.state != ExecutionSafetyState.UNCERTAIN:
                    self._execution_safety.mark_uncertain(
                        account_failure,
                        ts_ns=self.clock.timestamp_ns(),
                    )
                    self._cancel_working_entry_orders(
                        reason="broker account data unavailable or unsupported"
                    )
                self._audit_safety_state_if_changed()
                return

        equity = self._equity()
        if self._session_calendars:
            first_calendar = next(iter(self._session_calendars.values()))
            previous_risk_state = self._risk.state
            self._risk.on_new_session(first_calendar.session_key(when), when, equity)
            if (
                previous_risk_state == TradingState.HALTED_DAILY
                and self._risk.state == TradingState.ACTIVE
            ):
                self._execution_safety.resume(
                    "The next exchange session opened after the daily halt.",
                    ts_ns=self.clock.timestamp_ns(),
                )
        else:
            self._risk.on_new_day(when, equity)

        prior_state = self._risk.state
        state = self._risk.update_equity(when, equity)
        if state != prior_state:
            if state == TradingState.HALTED_DAILY:
                self._begin_risk_exit("daily loss limit", permanent=False)
            elif state == TradingState.DISABLED_KILL:
                self._begin_risk_exit("max drawdown kill-switch", permanent=True)
        elif state in {TradingState.HALTED_DAILY, TradingState.DISABLED_KILL}:
            if (
                state == TradingState.DISABLED_KILL
                and self._execution_safety.state
                not in {ExecutionSafetyState.EMERGENCY, ExecutionSafetyState.UNCERTAIN}
            ):
                self._execution_safety.begin_emergency(
                    "Persisted max-drawdown kill-switch remains engaged.",
                    ts_ns=self.clock.timestamp_ns(),
                )
            elif (
                state == TradingState.HALTED_DAILY
                and self._execution_safety.state == ExecutionSafetyState.ACTIVE
            ):
                self._execution_safety.freeze(
                    "Persisted daily-loss halt remains engaged.",
                    ts_ns=self.clock.timestamp_ns(),
                )
            if broker_mode and self._has_broker_exposure() and not (
                self._staged_exit_active or self.is_exiting()
            ):
                self.market_exit()

        unhealthy: list[str] = []
        now_ns = self.clock.timestamp_ns()
        grace_elapsed = (
            self._started_at is not None
            and when - self._started_at
            >= timedelta(seconds=max(0, self.config.startup_health_grace_secs))
        )
        for iid, calendar in self._session_calendars.items():
            phase = calendar.phase_at(when)
            previous = self._last_session_phase.get(iid)
            self._last_session_phase[iid] = phase
            if (
                self.config.cancel_entries_at_session_end
                and previous not in {None, SessionPhase.CLOSED}
                and phase == SessionPhase.CLOSED
            ):
                self._cancel_working_entry_orders(iid, reason="exchange session ended")
            scheduled_phase = calendar.phase_at(when, enforce_data_health=False)
            if phase in {SessionPhase.HALTED, SessionPhase.STALE}:
                unhealthy.append(f"{iid}:{phase.value}")
            elif (
                grace_elapsed
                and calendar.last_market_data_at is None
                and scheduled_phase != SessionPhase.CLOSED
            ):
                unhealthy.append(f"{iid}:NO_MARKET_DATA")
        if unhealthy:
            if self._execution_safety.state == ExecutionSafetyState.ACTIVE:
                self._execution_safety.freeze(
                    f"Market-data/session health failed: {', '.join(unhealthy)}",
                    ts_ns=now_ns,
                )
            self._cancel_working_entry_orders(reason="market-data/session health failure")
        elif (
            self._execution_safety.state == ExecutionSafetyState.FROZEN
            and self._risk.state == TradingState.ACTIVE
        ):
            self._execution_safety.resume(
                "Market-data/session health recovered.",
                ts_ns=now_ns,
            )

        self._reconcile_committed_notional()
        gross = sum(self._committed_notional.values())
        if gross > self.config.max_gross_exposure_pct * equity + 1e-9:
            self._begin_risk_exit("gross exposure limit breached", permanent=True)
        self._audit_safety_state_if_changed()

    def _has_broker_exposure(self) -> bool:
        return bool(
            self.cache.orders_open(strategy_id=self.id)
            or self.cache.orders_inflight(strategy_id=self.id)
            or self.cache.positions_open(strategy_id=self.id)
        )

    def on_time_event(self, _event) -> None:
        self._process_operations_control()
        self._supervise_risk(self.clock.utc_now())
        self._activate_startup_protection()
        self._refresh_telemetry_state()
        self._operations_heartbeat()

    def _activate_startup_protection(self) -> None:
        if (
            not self._startup_protection_pending
            or self._risk.state != TradingState.ACTIVE
            or self._execution_safety.state
            not in {ExecutionSafetyState.ACTIVE, ExecutionSafetyState.FROZEN}
            or self._staged_exit_active
            or self.is_exiting()
        ):
            return
        pending = tuple(self._startup_protection_pending)
        self._startup_protection_pending.clear()
        for iid in pending:
            self._ensure_broker_protection(iid)

    def _refresh_telemetry_state(self) -> None:
        if self._telemetry is None or self._telemetry_failed:
            return
        try:
            self._telemetry.refresh(
                positions=self._telemetry_positions(),
                risk=self._telemetry_risk(),
                model=self._telemetry_model(),
            )
        except Exception as exc:  # noqa: BLE001
            self._telemetry_failed = True
            self.log.error(f"Live telemetry disabled after state write failure: {exc}")

    def on_quote_tick(self, tick: QuoteTick) -> None:
        iid = tick.instrument_id
        calendar = self._session_calendars.get(iid)
        when = datetime.fromtimestamp(int(tick.ts_event) / 1_000_000_000, tz=timezone.utc)
        if calendar is not None:
            calendar.record_market_data(when)
        self._last_mark[iid] = (float(tick.bid_price) + float(tick.ask_price)) / 2.0
        self._supervise_risk(when)

    def on_trade_tick(self, tick: TradeTick) -> None:
        iid = tick.instrument_id
        when = datetime.fromtimestamp(int(tick.ts_event) / 1_000_000_000, tz=timezone.utc)
        calendar = self._session_calendars.get(iid)
        if calendar is not None:
            calendar.record_market_data(when)
        self._last_mark[iid] = float(tick.price)
        self._supervise_risk(when)

    def on_instrument_status(self, status) -> None:
        calendar = self._session_calendars.get(status.instrument_id)
        if calendar is None:
            return
        action = getattr(getattr(status, "action", None), "name", "")
        halted = action in {
            "HALT",
            "PAUSE",
            "SUSPEND",
            "NOT_AVAILABLE_FOR_TRADING",
        } or not bool(getattr(status, "is_trading", True))
        calendar.set_halt(halted, getattr(status, "reason", "") or action)
        if halted:
            self._cancel_working_entry_orders(
                status.instrument_id,
                reason=f"instrument status {action or 'not trading'}",
            )
        self._supervise_risk(self.clock.utc_now())

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
        """Synchronous backtest-only liquidation path."""
        self.log.warning(f"FLATTEN ALL -- {reason}")
        for iid in self._bar_types:
            self.cancel_all_orders(iid)
            pos = self.portfolio.net_position(iid)
            if pos is not None and pos != 0:
                self.close_all_positions(iid)
        # We are now flat: free every concurrency slot and reset committed gross
        # notional so entries can resume (after a temporary daily halt) clean.
        self._committed_notional.clear()

    def _begin_risk_exit(self, reason: str, *, permanent: bool) -> None:
        self._pending.clear()
        if permanent:
            self._execution_safety.begin_emergency(
                reason,
                ts_ns=self.clock.timestamp_ns(),
            )
        else:
            self._execution_safety.freeze(reason, ts_ns=self.clock.timestamp_ns())
        if self.config.execution_mode in {"paper", "live"}:
            if not self.is_exiting():
                self.market_exit()
        else:
            self._flatten_all(reason)

    def market_exit(self) -> None:
        """Cancel-confirm before invoking Nautilus's flatten-confirm sequence."""
        if self.config.execution_mode not in {"paper", "live"}:
            super().market_exit()
            return
        if self._staged_exit_active or self.is_exiting():
            return
        self._pending.clear()
        self._staged_exit_active = True
        self._staged_exit_started_at = self.clock.utc_now()
        if self._execution_safety.state not in {
            ExecutionSafetyState.EMERGENCY,
            ExecutionSafetyState.FROZEN,
            ExecutionSafetyState.SUSPENDED,
            ExecutionSafetyState.UNCERTAIN,
        }:
            self._execution_safety.begin_shutdown(ts_ns=self.clock.timestamp_ns())
        instruments = {
            order.instrument_id
            for order in [
                *self.cache.orders_open(strategy_id=self.id),
                *self.cache.orders_inflight(strategy_id=self.id),
            ]
        }
        for instrument_id in instruments:
            try:
                self.cancel_all_orders(instrument_id)
            except Exception as exc:
                self._execution_safety.mark_uncertain(
                    f"Cancel-all submission failed for {instrument_id}: {exc}",
                    ts_ns=self.clock.timestamp_ns(),
                )
                self.log.error(f"Cancel-all failed for {instrument_id}: {exc}")
        self._advance_staged_market_exit()
        if self._staged_exit_active:
            self.clock.set_timer(
                name=self._staged_exit_timer_name,
                interval=timedelta(milliseconds=100),
                callback=self._on_staged_exit_timer,
            )

    def _on_staged_exit_timer(self, _event) -> None:
        self._advance_staged_market_exit()

    def _advance_staged_market_exit(self) -> None:
        if not self._staged_exit_active:
            return
        open_orders = self.cache.orders_open(strategy_id=self.id)
        inflight_orders = self.cache.orders_inflight(strategy_id=self.id)
        if open_orders or inflight_orders:
            if (
                self._staged_exit_started_at is not None
                and self.clock.utc_now() - self._staged_exit_started_at
                >= timedelta(seconds=max(1, self.config.cancel_ack_timeout_secs))
                and self._execution_safety.state != ExecutionSafetyState.UNCERTAIN
            ):
                self._execution_safety.mark_uncertain(
                    f"Cancellation acknowledgement timed out with "
                    f"{len(open_orders)} open and {len(inflight_orders)} inflight orders.",
                    ts_ns=self.clock.timestamp_ns(),
                )
            return
        if self._staged_exit_timer_name in self.clock.timer_names:
            self.clock.cancel_timer(self._staged_exit_timer_name)
        self._staged_exit_active = False
        self._staged_exit_started_at = None
        # No working order remains. The framework sequence now submits only
        # emergency exits and waits for their fills before completing shutdown.
        super().market_exit()

    def on_market_exit(self) -> None:
        self._pending.clear()
        if self._execution_safety.state not in {
            ExecutionSafetyState.EMERGENCY,
            ExecutionSafetyState.FROZEN,
        }:
            self._execution_safety.begin_shutdown(ts_ns=self.clock.timestamp_ns())

    def post_market_exit(self) -> None:
        open_orders = self.cache.orders_open(strategy_id=self.id)
        inflight_orders = self.cache.orders_inflight(strategy_id=self.id)
        positions = self.cache.positions_open(strategy_id=self.id)
        if open_orders or inflight_orders or positions:
            details = (
                f"Market exit unresolved: {len(open_orders)} open orders, "
                f"{len(inflight_orders)} inflight orders, {len(positions)} positions."
            )
            self._execution_safety.mark_uncertain(
                details,
                ts_ns=self.clock.timestamp_ns(),
            )
            self.log.error(details)
            return
        self._committed_notional.clear()
        self._protection_ids.clear()
        if self._execution_safety.state == ExecutionSafetyState.STOPPING:
            self._execution_safety.finish_shutdown(
                clean=True,
                ts_ns=self.clock.timestamp_ns(),
            )

    def _telemetry_positions(self) -> list[dict]:
        positions = []
        for iid, raw in self._raw_by_iid.items():
            net = self.portfolio.net_position(iid)
            qty = float(net) if net is not None else 0.0
            if qty == 0.0 and self._committed_notional[iid] == 0.0:
                continue
            mark = float(self._closes[iid][-1]) if self._closes[iid] else 0.0
            reference = self._position_references.get(iid, {})
            avg_price = reference.get("entry_price")
            entry_ts = reference.get("entry_ts")
            unrealized_pnl = None
            open_positions = self.cache.positions_open(
                instrument_id=iid,
                strategy_id=self.id,
            )
            if open_positions:
                position = open_positions[0]
                avg_price = float(position.avg_px_open)
                entry_ts = datetime.fromtimestamp(
                    int(position.ts_opened) / 1_000_000_000,
                    tz=timezone.utc,
                ).isoformat()
                instrument = self.cache.instrument(iid)
                if instrument is not None and mark > 0:
                    try:
                        unrealized_pnl = position.unrealized_pnl(
                            instrument.make_price(mark)
                        ).as_double()
                    except Exception:  # noqa: BLE001 - optional display value
                        unrealized_pnl = None
            symbol = raw.split(".", 1)[0].split("/", 1)[0]
            positions.append(
                {
                    "symbol": symbol,
                    "side": "LONG" if qty > 0 else "SHORT" if qty < 0 else "PENDING",
                    "qty": abs(qty),
                    "avg_price": avg_price,
                    "entry_ts": entry_ts,
                    "mark_price": mark,
                    "market_value": abs(qty) * mark,
                    "notional": self._committed_notional[iid],
                    "unrealized_pnl": unrealized_pnl,
                    "stop_loss": reference.get("stop_loss"),
                    "take_profit": reference.get("take_profit"),
                    "reference_status": reference.get("status", "active"),
                    "reference_signal": reference.get("signal"),
                    "protection_guaranteed": bool(
                        reference.get("protection_guaranteed", False)
                    ),
                    "protection_quantity": reference.get("protection_quantity"),
                }
            )
        return positions

    def _telemetry_risk(self) -> dict:
        equity = self._equity()
        readings = self._risk.telemetry(equity)
        gross = sum(abs(value) for value in self._committed_notional.values())
        readings.update(
            {
                "gross_leverage": gross / equity if equity > 0 else 0.0,
                "rails": {
                    "risk_budget_pct": self.config.risk_budget_pct * 100.0,
                    "hard_cap_pct": self.config.max_trade_risk_pct * 100.0,
                    "leverage_max": self.config.max_leverage,
                    "daily_loss_limit_pct": self.config.daily_loss_limit_pct * 100.0,
                    "drawdown_warn_pct": self.config.kill_warn_pct * 100.0,
                    "kill_switch_pct": self.config.kill_switch_pct * 100.0,
                    "max_order_notional_pct": self.config.max_order_notional_pct
                    * 100.0,
                    "max_symbol_exposure_pct": self.config.max_symbol_exposure_pct
                    * 100.0,
                    "max_sector_exposure_pct": self.config.max_sector_exposure_pct
                    * 100.0,
                    "max_gross_exposure_pct": self.config.max_gross_exposure_pct
                    * 100.0,
                    "price_collar_pct": self.config.price_collar_pct * 100.0,
                },
                "execution_state": self._execution_safety.state.value,
                "reconciliation_state": (
                    "UNCERTAIN"
                    if self._execution_safety.state == ExecutionSafetyState.UNCERTAIN
                    else self._reconciliation_state
                ),
                "entries_allowed": (
                    self._execution_safety.entries_allowed and self._risk.can_open
                ),
                "operator_alerts": [
                    {
                        "severity": alert.severity,
                        "code": alert.code,
                        "message": alert.message,
                        "ts_ns": alert.ts_ns,
                        "client_order_id": alert.client_order_id,
                        "instrument_id": alert.instrument_id,
                    }
                    for alert in self._execution_safety.alerts[-20:]
                ],
                "orders": [
                    {
                        "client_order_id": record.client_order_id,
                        "venue_order_id": record.venue_order_id,
                        "permanent_order_id": record.permanent_order_id,
                        "instrument_id": record.instrument_id,
                        "side": record.side,
                        "role": record.role.value,
                        "status": record.status.value,
                        "quantity": float(record.requested_quantity),
                        "filled_quantity": float(record.filled_quantity),
                        "remaining_quantity": float(record.remaining_quantity),
                        "average_fill_price": float(record.average_fill_price),
                        "rejection_reason": record.rejection_reason,
                    }
                    for record in self._execution.orders.values()
                ][-100:],
                "fills": [
                    {
                        "execution_id": fill.execution_id,
                        "client_order_id": fill.client_order_id,
                        "instrument_id": fill.instrument_id,
                        "side": fill.side,
                        "quantity": float(fill.quantity),
                        "price": float(fill.price),
                        "ts_ns": fill.ts_ns,
                        "correction_of": fill.correction_of,
                    }
                    for fill in sorted(
                        self._execution.fills.values(),
                        key=lambda item: item.sequence,
                    )
                ][-100:],
                "data_quality": {
                    "healthy": not self._data_quality_blocked_instruments,
                    "blocked_instruments": sorted(self._data_quality_blocked_instruments),
                    "recovery_progress_bars": dict(self._data_quality_good_bars),
                    "recovery_required_bars": self.config.data_quality_recovery_bars,
                    "issues": list(self._data_quality_issues),
                },
            }
        )
        if self._session_calendars:
            calendar = next(iter(self._session_calendars.values()))
            now = self.clock.utc_now()
            next_open, next_close = calendar.next_open_close(now)
            last_data = calendar.last_market_data_at
            readings["session"] = {
                "phase": calendar.phase_at(now).value,
                "session_key": calendar.session_key(now),
                "timezone": calendar.timezone_id,
                "next_open": next_open.isoformat() if next_open else None,
                "next_close": next_close.isoformat() if next_close else None,
                "data_age_seconds": (
                    max((now - last_data).total_seconds(), 0.0)
                    if last_data is not None
                    else None
                ),
            }
        return readings

    def _telemetry_model(self) -> dict:
        return {
            "entry_threshold": self.config.entry_threshold,
            "horizon": self.config.horizon,
            "n_lags": self.config.n_lags,
            "atr_period": self.config.atr_period,
            "atr_stop_mult": self.config.atr_stop_mult,
            "regime_window": self.config.regime_window,
            "regime_bull_threshold": self.config.regime_bull_threshold,
            "regime_bear_threshold": self.config.regime_bear_threshold,
            "use_news_features": self.config.use_news_features,
            "news_data_path": self.config.news_data_path,
            "news_source": self.config.news_source,
            "news_raw_scale": self.config.news_raw_scale,
            "news_score_clip": self.config.news_score_clip,
            "news_half_life_hours": self.config.news_half_life_hours,
            "news_max_age_hours": self.config.news_max_age_hours,
            "news_direct_weight": self.config.news_direct_weight,
            "news_industry_weight": self.config.news_industry_weight,
            "news_commodity_weight": self.config.news_commodity_weight,
            "news_macro_weight": self.config.news_macro_weight,
            "target_rr": self.config.telemetry_target_rr,
            "protective_orders_submitted": any(
                values.get("protection_guaranteed", False)
                for values in self._position_references.values()
            ),
            "forecast_bar_basis": "predicted close with a half-ATR display envelope",
        }

    def _record_telemetry(
        self,
        bar: Bar,
        ts: datetime,
        *,
        yhat: float | None,
        atr: float | None,
        flush: bool = True,
    ) -> bool:
        if self._telemetry is None or self._telemetry_failed:
            return
        iid = bar.bar_type.instrument_id
        close = float(bar.close)
        diagnostics = self._engines[iid].current_diagnostics(list(self._closes[iid]))
        news = self._news_meta.get(iid, NewsFeatureSnapshot())
        signal = "WARMUP" if yhat is None else "HOLD"
        direction = 0
        if yhat is not None and yhat > self.config.entry_threshold:
            signal, direction = "BUY", 1
        elif yhat is not None and yhat < -self.config.entry_threshold:
            if self.config.allow_short_positions:
                signal, direction = "SELL", -1
            else:
                net = self.portfolio.net_position(iid)
                signal = "EXIT" if net is not None and float(net) > 0 else "HOLD"
                direction = -1 if signal == "EXIT" else 0
        risk_distance = (
            self.config.atr_stop_mult * float(atr)
            if atr is not None and atr > 0 and direction
            else None
        )
        stop = close - direction * risk_distance if risk_distance else None
        target = (
            close + direction * risk_distance * self.config.telemetry_target_rr
            if risk_distance
            else None
        )
        reference = self._position_references.get(iid)
        if stop is None and reference is not None:
            stop = reference.get("stop_loss")
            target = reference.get("take_profit")
        predicted_price = close * float(np.exp(yhat)) if yhat is not None else None
        forecast = None
        if predicted_price is not None:
            envelope = max(float(atr or 0.0) * 0.5, 0.0)
            forecast = {
                "horizon_bars": self.config.horizon,
                "open": close,
                "close": predicted_price,
                "high": max(close, predicted_price) + envelope,
                "low": min(close, predicted_price) - envelope,
                "basis": "huber_close_atr_envelope",
            }
        raw = self._raw_by_iid.get(iid, str(iid))
        ticker = raw.split(".", 1)[0].split("/", 1)[0]
        point = {
            "ts": ts.isoformat(),
            "open": float(bar.open),
            "high": float(bar.high),
            "low": float(bar.low),
            "close": close,
            "volume": float(bar.volume),
            "predicted_price": predicted_price,
            "forecast": forecast,
            "predicted_return": yhat,
            "signal": signal,
            "entry_threshold": self.config.entry_threshold,
            "atr": atr,
            "stop_loss": stop,
            "take_profit": target,
            "position_reference": reference is not None,
            "target_rr": self.config.telemetry_target_rr,
            "news_score": news.score,
            "news_article_count": news.article_count,
            "news_top_headlines": list(news.top_headlines),
            **diagnostics,
        }
        try:
            self._telemetry.record(
                ticker,
                point,
                positions=self._telemetry_positions(),
                risk=self._telemetry_risk(),
                model=self._telemetry_model(),
                flush=flush,
            )
        except Exception as exc:  # noqa: BLE001 - telemetry must never stop trading
            self._telemetry_failed = True
            self.log.error(f"Live telemetry disabled after write failure: {exc}")

    def _append_bar(self, bar: Bar) -> bool:
        """Append a bar once, returning False for duplicates/out-of-order data."""
        iid = bar.bar_type.instrument_id
        ts_ns = int(bar.ts_event)
        quality = self._data_quality.validate(
            str(iid),
            timestamp_ns=ts_ns,
            open_price=float(bar.open),
            high_price=float(bar.high),
            low_price=float(bar.low),
            close_price=float(bar.close),
            volume=float(bar.volume),
        )
        for issue in quality.issues:
            item = {
                "ts_ns": ts_ns,
                "instrument_id": issue.instrument_id,
                "code": issue.code,
                "severity": issue.severity,
                "detail": issue.detail,
            }
            self._data_quality_issues.append(item)
            if issue.severity in {"WARNING", "CRITICAL"}:
                self._audit_event(
                    "MARKET_DATA_QUALITY_ISSUE",
                    item,
                    severity=issue.severity,
                    event_id=f"data-quality:{issue.instrument_id}:{ts_ns}:{issue.code}",
                )
        if quality.critical:
            instrument_key = str(iid)
            self._data_quality_blocked_instruments.add(instrument_key)
            self._data_quality_good_bars[instrument_key] = 0
            self._execution_safety.freeze(
                f"Critical market-data quality failure for {iid}",
                ts_ns=self.clock.timestamp_ns(),
            )
            if self.config.execution_mode in {"paper", "live"}:
                self._cancel_working_entry_orders(reason="market-data quality failure")
            return False
        if not quality.accepted:
            return False
        if ts_ns <= self._last_bar_ns[iid]:
            return False
        instrument_key = str(iid)
        if instrument_key in self._data_quality_blocked_instruments:
            self._data_quality_good_bars[instrument_key] += 1
            if self._data_quality_good_bars[instrument_key] >= max(
                1, self.config.data_quality_recovery_bars
            ):
                recovery_bars = self._data_quality_good_bars.pop(instrument_key)
                self._data_quality_blocked_instruments.discard(instrument_key)
                self._audit_event(
                    "MARKET_DATA_QUALITY_RECOVERED",
                    {"instrument_id": instrument_key, "recovery_bars": recovery_bars},
                    severity="WARNING",
                )
                if (
                    self.config.execution_mode == "backtest"
                    and not self._data_quality_blocked_instruments
                ):
                    self._execution_safety.resume(
                        "Historical data-quality recovery window completed.",
                        ts_ns=self.clock.timestamp_ns(),
                    )
        self._last_bar_ns[iid] = ts_ns
        self._closes[iid].append(float(bar.close))
        self._bar_times[iid].append(ts_ns)
        snapshot = NewsFeatureSnapshot()
        if self._news_reader is not None:
            raw = self._raw_by_iid.get(iid, str(iid))
            try:
                snapshot = self._news_reader.snapshot_at(raw, ts_ns / 1_000_000_000)
            except Exception as exc:  # noqa: BLE001 - neutral news is fail-open
                self.log.error(f"News feature read failed for {raw}; using neutral score: {exc}")
        self._news_scores[iid].append(snapshot.score)
        self._news_meta[iid] = snapshot
        self._highs[iid].append(float(bar.high))
        self._lows[iid].append(float(bar.low))
        self._prev_close[iid] = float(bar.close)
        return True

    def _cross_asset_data_is_fresh(self, iid: InstrumentId, ts_ns: int) -> bool:
        if (
            self.config.execution_mode not in {"paper", "live"}
            or (self.config.cross_asset_lags <= 0 and self.config.spread_lags <= 0)
        ):
            return True
        stale: list[str] = []
        # Current peer bars need not have arrived: every peer feature is lagged
        # by at least one bar. The last peer bar must therefore be no older than
        # this target's immediately preceding bar, independent of overnight,
        # weekend, holiday, or DST wall-clock gaps.
        prior_target_ns = (
            int(self._bar_times[iid][-2])
            if len(self._bar_times[iid]) >= 2
            else int(ts_ns)
        )
        for raw in self._engines[iid].cfg.peer_symbols:
            peer = self._iid_by_raw.get(raw)
            latest = self._last_bar_ns.get(peer, 0) if peer is not None else 0
            if latest <= 0 or latest < prior_target_ns:
                stale.append(str(raw))
        if not stale:
            return True
        instrument_key = str(iid)
        self._data_quality_blocked_instruments.add(instrument_key)
        self._data_quality_good_bars[instrument_key] = 0
        item = {
            "ts_ns": ts_ns,
            "instrument_id": instrument_key,
            "code": "CROSS_ASSET_DATA_STALE",
            "severity": "CRITICAL",
            "detail": f"required peer bars are stale or missing: {', '.join(stale)}",
        }
        self._data_quality_issues.append(item)
        self._execution_safety.freeze(
            item["detail"],
            ts_ns=self.clock.timestamp_ns(),
        )
        self._cancel_working_entry_orders(reason="cross-asset market data stale")
        self._audit_event(
            "MARKET_DATA_QUALITY_ISSUE",
            item,
            severity="CRITICAL",
            event_id=f"cross-asset-quality:{instrument_key}:{ts_ns}",
        )
        return False

    def on_historical_data(self, data) -> None:
        """Warm model and chart history without evaluating or trading it."""
        if isinstance(data, Bar) and self._append_bar(data):
            self._historical_telemetry_count += 1
            ts = datetime.fromtimestamp(
                int(data.ts_event) / 1_000_000_000,
                tz=timezone.utc,
            )
            # Atomic fsync on every bootstrap bar is needlessly expensive.
            # Flush each small batch; the first real-time bar always flushes
            # the complete accumulated context again.
            self._record_telemetry(
                data,
                ts,
                yhat=None,
                atr=self._atr(data.bar_type.instrument_id),
                flush=(
                    self._historical_telemetry_count == 1
                    or self._historical_telemetry_count % 10 == 0
                ),
            )

    # ---- main event -----------------------------------------------------
    def on_bar(self, bar: Bar) -> None:
        iid = bar.bar_type.instrument_id
        close = float(bar.close)
        if not self._append_bar(bar):
            return
        ts = datetime.fromtimestamp(int(bar.ts_event) / 1_000_000_000, tz=timezone.utc)

        calendar = self._session_calendars.get(iid)
        if calendar is not None:
            calendar.record_market_data(ts)
        self._last_mark[iid] = close
        self._supervise_risk(ts)
        if self._risk.drawdown_warning:
            self.log.warning("Drawdown >= 5% warning threshold.")

        # Publish the completed market bar immediately, even while the model is
        # warming up. A second write below replaces this timestamp once yhat is
        # available, so the dashboard never waits for training to see prices.
        self._record_telemetry(bar, ts, yhat=None, atr=self._atr(iid))

        # Timestamp boundary: a bar with a new timestamp means the previous
        # timestamp's buffered signals are complete, so resolve them as one
        # cross-sectional batch before we start collecting this timestamp.
        if self._pending_ts is not None and ts != self._pending_ts:
            self._resolve_batch()
        self._pending_ts = ts

        closes = self._closes[iid]
        if len(closes) < self.config.warmup_bars:
            return
        if not self._cross_asset_data_is_fresh(iid, int(bar.ts_event)):
            return

        # --- (re)train prediction engine on history seen so far (past only) ---
        # Refit through PredictionEngine.refit_on_history(), which uses the SAME
        # windowing contract as walk_forward(): expanding or configured rolling
        # fit over past-only labeled rows.
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
        news_features = list(self._news_scores[iid])
        cadence = max(1, int(self.config.refit_every_n_bars))
        bar_index = self._bar_index[iid]
        self._bar_index[iid] += 1
        needs_baseline = not self._trained[iid]
        fit_allowed = (
            self.config.backtest_model_fit_end_ns <= 0
            or int(bar.ts_event) <= self.config.backtest_model_fit_end_ns
        )
        if fit_allowed and (needs_baseline or bar_index % cadence == 0):
            if not eng.refit_on_history(
                list(closes), peer_closes, news_features=news_features
            ):
                return
            self._trained[iid] = True
        elif needs_baseline:
            # A correctly planned fold always establishes a fit before its
            # embargo. Fail closed if sparse/misaligned ticker history did not.
            return

        # --- alpha: get yhat, then BUFFER (submission happens in the batch) ---
        yhat = eng.predict_move(
            list(closes), peer_closes, news_features=news_features
        )
        if yhat is None:
            return

        atr = self._atr(iid)
        if atr is None or atr <= 0:
            return

        self._record_telemetry(bar, ts, yhat=float(yhat), atr=atr)
        audited = self._audit_event(
            "SIGNAL_EVALUATED",
            {
                "instrument_id": str(iid),
                "bar_ts": ts.isoformat(),
                "close": close,
                "yhat": float(yhat),
                "entry_threshold": self.config.entry_threshold,
                "intent": (
                    "BUY"
                    if yhat > self.config.entry_threshold
                    else "SELL"
                    if yhat < -self.config.entry_threshold
                    else "HOLD"
                ),
            },
            event_id=f"signal:{self._operations_target()}:{iid}:{int(bar.ts_event)}",
        )

        if (
            self.config.backtest_trade_start_ns > 0
            and int(bar.ts_event) < self.config.backtest_trade_start_ns
        ):
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
        if (
            not pending
            or not self._risk.can_open
            or not self._execution_safety.entries_allowed
        ):
            return

        thr = self.config.entry_threshold
        equity = self._equity()

        new_entries: list[tuple[InstrumentId, OrderSide, _PendingSignal]] = []
        for iid, sig in pending.items():
            net, _ = self._broker_position_state(iid)
            desired_side: OrderSide | None = None
            if not self.config.allow_short_positions:
                if net > 0 and sig.yhat < -thr:
                    self._request_instrument_exit(iid, "bearish signal in long-only mode")
                    continue
                if net < 0:
                    self._request_instrument_exit(iid, "unauthorized short position")
                    continue
                if sig.yhat > thr:
                    desired_side = OrderSide.BUY
            elif sig.yhat > thr:
                desired_side = OrderSide.BUY
            elif sig.yhat < -thr:
                desired_side = OrderSide.SELL

            working_entries = self._execution.working_orders(
                instrument_id=str(iid),
                roles={OrderRole.ENTRY},
            )
            if working_entries:
                stale = any(
                    self._bar_index[iid]
                    - self._entry_submitted_bar.get(record.client_order_id, 0)
                    >= max(1, self.config.stale_entry_bars)
                    for record in working_entries
                )
                changed = desired_side is None or any(
                    record.side != desired_side.name for record in working_entries
                )
                if stale or changed:
                    self._cancel_working_entry_orders(
                        iid,
                        reason="signal changed" if changed else "entry became stale",
                    )
                # A replacement is never submitted until every cancel is
                # acknowledged, preventing duplicated exposure in cancel/fill races.
                continue

            if desired_side is None:
                continue
            if net != 0:
                agrees = (net > 0 and desired_side == OrderSide.BUY) or (
                    net < 0 and desired_side == OrderSide.SELL
                )
                if agrees:
                    continue
                self._request_instrument_exit(iid, "signal reversal")
                continue
            if iid in self._pending_exits:
                continue
            when = self._pending_ts or self.clock.utc_now()
            session_allowed, session_reason = self._session_allows_entry(iid, when)
            if not session_allowed:
                self.log.warning(f"Entry blocked for {iid}: {session_reason}")
                continue
            new_entries.append((iid, desired_side, sig))

        # Highest conviction first. The cap check inside the loop stops handing
        # out slots once they are full, so weaker signals are dropped this bar.
        new_entries.sort(key=lambda t: abs(t[2].yhat), reverse=True)
        for iid, side, sig in new_entries:
            if self._committed_notional[iid] == 0.0 and not self._can_open_new_position():
                continue
            self._enter(iid, side, sig.close, sig.atr, equity, sig.yhat)

    def _request_instrument_exit(self, iid: InstrumentId, reason: str) -> None:
        reference = self._position_references.get(iid)
        if reference is not None:
            reference["protection_guaranteed"] = False
            reference["status"] = f"exit_pending:{reason}"
        if iid not in self._pending_exits:
            self._pending_exits[iid] = reason
            self.log.warning(f"Safe exit requested for {iid}: {reason}")
        if self.config.execution_mode == "backtest":
            self.cancel_all_orders(iid)
            self.close_all_positions(iid)
            self._pending_exits.pop(iid, None)
            self._committed_notional[iid] = 0.0
            return
        for order in [
            *self.cache.orders_open(instrument_id=iid, strategy_id=self.id),
            *self.cache.orders_inflight(instrument_id=iid, strategy_id=self.id),
        ]:
            if not order.is_closed:
                record = self._execution.orders.get(str(order.client_order_id))
                self._cancel_order_safely(order, record, reason=reason)
        self._continue_pending_exit(iid)

    def _continue_pending_exit(self, iid: InstrumentId) -> None:
        reason = self._pending_exits.get(iid)
        if reason is None:
            return
        working = [
            *self.cache.orders_open(instrument_id=iid, strategy_id=self.id),
            *self.cache.orders_inflight(instrument_id=iid, strategy_id=self.id),
        ]
        if any(not order.is_closed for order in working):
            return
        net, _ = self._broker_position_state(iid)
        if net == 0:
            self._pending_exits.pop(iid, None)
            self._protection_ids.pop(iid, None)
            self._committed_notional[iid] = 0.0
            return
        instrument = self.cache.instrument(iid)
        if instrument is None:
            self._execution_safety.mark_uncertain(
                f"Cannot exit {iid}: instrument missing from cache.",
                ts_ns=self.clock.timestamp_ns(),
            )
            return
        side = OrderSide.SELL if net > 0 else OrderSide.BUY
        try:
            quantity = instrument.make_qty(abs(net))
        except ValueError:
            self._execution_safety.mark_uncertain(
                f"Cannot exit {iid}: broker position quantity is invalid.",
                ts_ns=self.clock.timestamp_ns(),
            )
            return
        order = self.order_factory.market(
            instrument_id=iid,
            order_side=side,
            quantity=quantity,
            time_in_force=TimeInForce.IOC,
            reduce_only=True,
            tags=list(self.config.order_tags) or None,
        )
        self._register_and_submit_order(
            order,
            role=OrderRole.SIGNAL_EXIT,
            signal_version=reason,
        )

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

    def _register_and_submit_order(
        self,
        order,
        *,
        role: OrderRole,
        signal_version: str = "",
        requested_quantity: float | None = None,
    ) -> None:
        order_id = str(order.client_order_id)
        quantity = (
            float(requested_quantity)
            if requested_quantity is not None
            else order.quantity.as_double()
        )
        self._execution.register_order(
            client_order_id=order_id,
            instrument_id=str(order.instrument_id),
            side=order.side.name,
            requested_quantity=quantity,
            role=role,
            signal_version=signal_version,
            ts_ns=self.clock.timestamp_ns(),
        )
        self._audit_event(
            "ORDER_REGISTERED",
            {
                "client_order_id": order_id,
                "instrument_id": str(order.instrument_id),
                "side": order.side.name,
                "quantity": quantity,
                "role": role.value,
                "signal_version": signal_version,
            },
            correlation_id=order_id,
            event_id=f"order-registered:{order_id}",
        )
        if not audited and role == OrderRole.ENTRY:
            self._execution.apply_order_state(
                order_id,
                LifecycleStatus.DENIED,
                ts_ns=self.clock.timestamp_ns(),
                reason="required operational audit unavailable",
            )
            return False
        try:
            self.submit_order(order)
        except Exception as exc:
            self._execution.apply_order_state(
                order_id,
                LifecycleStatus.REJECTED,
                ts_ns=self.clock.timestamp_ns(),
                reason=str(exc),
            )
            self._execution_safety.on_rejection(
                f"Local order submission failed: {exc}",
                ts_ns=self.clock.timestamp_ns(),
                client_order_id=order_id,
                instrument_id=str(order.instrument_id),
            )
            self._audit_event(
                "ORDER_SUBMIT_FAILED",
                {
                    "client_order_id": order_id,
                    "error": f"{type(exc).__name__}: {exc}",
                },
                severity="CRITICAL",
                correlation_id=order_id,
                event_id=f"order-submit-failed:{order_id}",
            )
            raise
        self._execution.apply_order_state(
            order_id,
            LifecycleStatus.SUBMITTED,
            ts_ns=self.clock.timestamp_ns(),
        )
        self._audit_event(
            "ORDER_SUBMITTED",
            {"client_order_id": order_id, "instrument_id": str(order.instrument_id)},
            correlation_id=order_id,
            event_id=f"order-submitted:{order_id}",
        )
        return True

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
        if not self._risk.can_open or not self._execution_safety.entries_allowed:
            return
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

        qty = target_qty

        limit_px = None
        submission_price = price
        if self.config.use_limit_orders:
            off = price * (self.config.limit_offset_bps / 10_000.0)
            limit_px = price - off if side == OrderSide.BUY else price + off
            submission_price = limit_px

        target_notional = target_qty.as_double() * submission_price
        gross_after = sum(
            value for current, value in self._committed_notional.items() if current != iid
        ) + target_notional
        violations = self._risk.pretrade_violations(
            equity=equity,
            order_notional=target_notional,
            symbol_exposure_after=target_notional,
            gross_exposure_after=gross_after,
            order_price=submission_price,
            reference_price=self._last_mark.get(iid, price),
        )
        if violations:
            self.log.error(f"Entry rejected by pre-trade risk for {iid}: {', '.join(violations)}")
            return

        if self.config.execution_mode in {"paper", "live"}:
            account = self._account()
            base_currency = getattr(account, "base_currency", None) if account else None
            if base_currency is not None and str(base_currency) != "USD":
                self._execution_safety.suspend(
                    f"Unsupported account base currency {base_currency}; USD is required.",
                    code="ACCOUNT_CURRENCY_MISMATCH",
                    ts_ns=self.clock.timestamp_ns(),
                    instrument_id=str(iid),
                )
                return
            try:
                free_balance = account.balance_free() if account is not None else None
                free_funds = free_balance.as_double() if free_balance is not None else None
            except Exception:  # noqa: BLE001 - fail closed below
                free_funds = None
            if side == OrderSide.BUY and (
                free_funds is None or float(free_funds) < target_notional
            ):
                self.log.error(f"Entry rejected for {iid}: available funds unavailable/insufficient")
                return

        if self.config.use_limit_orders:
            order = self.order_factory.limit(
                instrument_id=iid,
                order_side=side,
                quantity=qty,
                price=instrument.make_price(limit_px),
                time_in_force=self._entry_tif(),
                tags=list(self.config.order_tags) or None,
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
                    else self._entry_tif()
                ),
                quote_quantity=quote_quantity,
                tags=list(self.config.order_tags) or None,
            )
        signal_version = (
            f"{self._pending_ts.isoformat()}:{side.name}"
            if self._pending_ts is not None
            else side.name
        )
        submitted = self._register_and_submit_order(
            order,
            role=OrderRole.ENTRY,
            signal_version=signal_version,
            requested_quantity=target_qty.as_double(),
        )
        if not submitted:
            return
        self._entry_submitted_bar[str(order.client_order_id)] = self._bar_index[iid]
        # Record the committed gross notional synchronously so BOTH the
        # concurrency cap and the book-level leverage guard see this holding
        # immediately, before the fill (and resulting net_position) lands. Using
        # the ordered qty * price is a conservative synchronous proxy; on a
        # reversal it replaces (not stacks on) the instrument's prior notional.
        self._committed_notional[iid] = target_qty.as_double() * price
        direction = 1 if side == OrderSide.BUY else -1
        self._position_references[iid] = {
            "side": "LONG" if direction > 0 else "SHORT",
            "signal": side.name,
            "entry_price": price,
            "entry_ts": self._pending_ts.isoformat() if self._pending_ts is not None else None,
            "stop_loss": stop_price,
            "take_profit": price
            + direction * stop_dist * self.config.telemetry_target_rr,
            "atr": atr,
            "status": (
                "broker_protection_pending_fill"
                if self.config.enable_broker_protection
                else "model_reference"
            ),
        }
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
                record = self._execution.orders.get(str(order.client_order_id))
                if record is not None and record.role in {
                    OrderRole.STOP_LOSS,
                    OrderRole.TAKE_PROFIT,
                    OrderRole.SIGNAL_EXIT,
                    OrderRole.EMERGENCY_EXIT,
                }:
                    continue
                qty = getattr(order, "leaves_qty", order.quantity).as_double()
                if order.is_quote_quantity:
                    notional += abs(qty)
                else:
                    order_px = getattr(order, "price", None)
                    px = order_px.as_double() if order_px is not None else fallback_px
                    notional += abs(qty * px)
            self._committed_notional[current] = notional
            if notional == 0.0:
                self._position_references.pop(current, None)

    def _oca_tags(self, group: str) -> list[str]:
        tags = list(self.config.order_tags)
        payload = {}
        retained: list[str] = []
        for tag in tags:
            if tag.startswith("IBOrderTags:") and not payload:
                try:
                    payload = json.loads(tag.removeprefix("IBOrderTags:"))
                except json.JSONDecodeError:
                    retained.append(tag)
            else:
                retained.append(tag)
        payload.update({"ocaGroup": group, "ocaType": 1})
        retained.append(f"IBOrderTags:{json.dumps(payload, separators=(',', ':'))}")
        return retained

    def _cancel_protection(self, iid: InstrumentId, reason: str) -> None:
        for record in self._execution.working_orders(
            instrument_id=str(iid),
            roles={OrderRole.STOP_LOSS, OrderRole.TAKE_PROFIT},
        ):
            order = self.cache.order(ClientOrderId(record.client_order_id))
            if order is not None and not order.is_closed:
                self._cancel_order_safely(order, record, reason=reason)
        reference = self._position_references.get(iid)
        if reference is not None:
            reference["protection_guaranteed"] = False
            reference["status"] = f"broker_protection_canceling:{reason}"

    def _ensure_broker_protection(self, iid: InstrumentId) -> None:
        if not self.config.enable_broker_protection:
            return
        net, actual_average = self._broker_position_state(iid)
        if net == 0:
            self._cancel_protection(iid, "position flat")
            self._protection_ids.pop(iid, None)
            return
        reference = self._position_references.get(iid)
        if reference is None or not reference.get("atr"):
            self._execution_safety.suspend(
                f"Position {iid} has no persisted ATR context for broker protection.",
                code="MISSING_PROTECTION_CONTEXT",
                ts_ns=self.clock.timestamp_ns(),
                instrument_id=str(iid),
            )
            self._request_instrument_exit(iid, "missing protection context")
            return
        ledger_position = self._execution.position(str(iid))
        average = actual_average or float(ledger_position.average_entry_price)
        if average <= 0:
            self._execution_safety.suspend(
                f"Position {iid} has no authoritative average fill price.",
                code="MISSING_AVERAGE_FILL",
                ts_ns=self.clock.timestamp_ns(),
                instrument_id=str(iid),
            )
            self._request_instrument_exit(iid, "missing average fill")
            return
        instrument = self.cache.instrument(iid)
        if instrument is None:
            self._execution_safety.mark_uncertain(
                f"Cannot protect {iid}: instrument missing from cache.",
                ts_ns=self.clock.timestamp_ns(),
            )
            self._request_instrument_exit(iid, "instrument missing during protection")
            return
        distance = self.config.atr_stop_mult * float(reference["atr"])
        direction = 1 if net > 0 else -1
        stop_price = average - direction * distance
        target_price = average + direction * distance * self.config.protection_target_rr
        if stop_price <= 0 or target_price <= 0:
            self._execution_safety.suspend(
                f"Invalid protection prices for {iid}.",
                code="INVALID_PROTECTION_PRICE",
                ts_ns=self.clock.timestamp_ns(),
                instrument_id=str(iid),
            )
            self._request_instrument_exit(iid, "invalid protection prices")
            return
        try:
            quantity = instrument.make_qty(abs(net))
            stop_trigger = instrument.make_price(stop_price)
            target_limit = instrument.make_price(target_price)
        except ValueError as exc:
            self._execution_safety.suspend(
                f"Cannot size protection for {iid}: {exc}",
                code="INVALID_PROTECTION_QUANTITY",
                ts_ns=self.clock.timestamp_ns(),
                instrument_id=str(iid),
            )
            self._request_instrument_exit(iid, "invalid protection quantity")
            return
        exit_side = OrderSide.SELL if net > 0 else OrderSide.BUY
        ids = self._protection_ids.get(iid, {})
        stop_order = (
            self.cache.order(ClientOrderId(ids["stop"])) if ids.get("stop") else None
        )
        target_order = (
            self.cache.order(ClientOrderId(ids["target"])) if ids.get("target") else None
        )
        existing = [order for order in (stop_order, target_order) if order is not None]
        if existing and len(existing) != 2:
            self._execution_safety.suspend(
                f"Incomplete broker protection pair for {iid}; flattening safely.",
                code="INCOMPLETE_PROTECTION",
                ts_ns=self.clock.timestamp_ns(),
                instrument_id=str(iid),
            )
            self._request_instrument_exit(iid, "incomplete protection pair")
            return
        if len(existing) == 2 and any(order.is_closed for order in existing) and not all(
            order.is_closed for order in existing
        ):
            self._execution_safety.suspend(
                f"Only one protection order remains working for {iid}; flattening safely.",
                code="ORPHANED_PROTECTION",
                ts_ns=self.clock.timestamp_ns(),
                instrument_id=str(iid),
            )
            self._request_instrument_exit(iid, "orphaned protection order")
            return
        if len(existing) == 2 and all(not order.is_closed for order in existing):
            try:
                self.modify_order(stop_order, quantity=quantity, trigger_price=stop_trigger)
                self.modify_order(target_order, quantity=quantity, price=target_limit)
                self._execution.amend_order(
                    str(stop_order.client_order_id),
                    requested_quantity=quantity.as_double(),
                    ts_ns=self.clock.timestamp_ns(),
                )
                self._execution.amend_order(
                    str(target_order.client_order_id),
                    requested_quantity=quantity.as_double(),
                    ts_ns=self.clock.timestamp_ns(),
                )
            except Exception as exc:
                self._execution_safety.suspend(
                    f"Protection resize failed for {iid}: {exc}",
                    code="PROTECTION_RESIZE_FAILED",
                    ts_ns=self.clock.timestamp_ns(),
                    instrument_id=str(iid),
                )
                self._request_instrument_exit(iid, "protection resize failed")
                return
        else:
            group = f"QOCA-{str(iid).replace('/', '_')}-{self.clock.timestamp_ns()}"
            tags = self._oca_tags(group)
            stop_order = self.order_factory.stop_market(
                instrument_id=iid,
                order_side=exit_side,
                quantity=quantity,
                trigger_price=stop_trigger,
                time_in_force=TimeInForce.GTC,
                reduce_only=True,
                tags=tags,
            )
            target_order = self.order_factory.limit(
                instrument_id=iid,
                order_side=exit_side,
                quantity=quantity,
                price=target_limit,
                time_in_force=TimeInForce.GTC,
                reduce_only=True,
                tags=tags,
            )
            for order, role in (
                (stop_order, OrderRole.STOP_LOSS),
                (target_order, OrderRole.TAKE_PROFIT),
            ):
                self._execution.register_order(
                    client_order_id=str(order.client_order_id),
                    instrument_id=str(iid),
                    side=exit_side.name,
                    requested_quantity=quantity.as_double(),
                    role=role,
                    signal_version=group,
                    ts_ns=self.clock.timestamp_ns(),
                )
            order_list = self.order_factory.create_list([stop_order, target_order])
            try:
                self.submit_order_list(order_list)
            except Exception as exc:
                for order in (stop_order, target_order):
                    self._execution.apply_order_state(
                        str(order.client_order_id),
                        LifecycleStatus.REJECTED,
                        ts_ns=self.clock.timestamp_ns(),
                        reason=str(exc),
                    )
                self._execution_safety.suspend(
                    f"Broker protection submission failed for {iid}: {exc}",
                    code="PROTECTION_SUBMIT_FAILED",
                    ts_ns=self.clock.timestamp_ns(),
                    instrument_id=str(iid),
                )
                self._request_instrument_exit(iid, "protection submission failed")
                return
            for order in (stop_order, target_order):
                self._execution.apply_order_state(
                    str(order.client_order_id),
                    LifecycleStatus.SUBMITTED,
                    ts_ns=self.clock.timestamp_ns(),
                )
            self._protection_ids[iid] = {
                "stop": str(stop_order.client_order_id),
                "target": str(target_order.client_order_id),
                "oca_group": group,
            }
        reference.update(
            {
                "entry_price": average,
                "stop_loss": float(stop_trigger),
                "take_profit": float(target_limit),
                "status": "broker_protection_pending_ack",
                "protection_guaranteed": False,
                "protection_quantity": abs(net),
            }
        )

    def _refresh_protection_status(self, iid: InstrumentId) -> None:
        ids = self._protection_ids.get(iid)
        reference = self._position_references.get(iid)
        if not ids or reference is None:
            return
        records = [
            self._execution.orders.get(ids.get("stop", "")),
            self._execution.orders.get(ids.get("target", "")),
        ]
        if all(
            record is not None
            and record.status
            in {
                LifecycleStatus.ACKNOWLEDGED,
                LifecycleStatus.PARTIALLY_FILLED,
                LifecycleStatus.FILLED,
            }
            for record in records
        ):
            reference["status"] = "broker_protected"
            reference["protection_guaranteed"] = True

    def _reconcile_protection_after_restart(self) -> None:
        if not self.config.enable_broker_protection:
            return
        for iid in self._bar_types:
            working = self._execution.working_orders(
                instrument_id=str(iid),
                roles={OrderRole.STOP_LOSS, OrderRole.TAKE_PROFIT},
            )
            stop = next((item for item in working if item.role == OrderRole.STOP_LOSS), None)
            target = next(
                (item for item in working if item.role == OrderRole.TAKE_PROFIT),
                None,
            )
            if stop and target:
                self._protection_ids[iid] = {
                    "stop": stop.client_order_id,
                    "target": target.client_order_id,
                    "oca_group": stop.signal_version,
                }
            net, _ = self._broker_position_state(iid)
            if net != 0:
                # on_start may run before the Strategy reaches RUNNING. Defer
                # broker submissions/modifications to the first supervisor
                # timer after startup reconciliation completes.
                self._startup_protection_pending.add(iid)

    def on_event(self, event: Event) -> None:
        tracked_types = (
            OrderSubmitted,
            OrderAccepted,
            OrderUpdated,
            OrderPendingCancel,
            OrderFilled,
            OrderCanceled,
            OrderExpired,
            OrderRejected,
            OrderDenied,
            OrderCancelRejected,
            OrderModifyRejected,
        )
        if not isinstance(event, tracked_types):
            return
        iid = getattr(event, "instrument_id", None)
        client_order_id = getattr(event, "client_order_id", None)
        if iid not in self._bar_types or client_order_id is None:
            return
        order_id = str(client_order_id)
        record = self._execution.orders.get(order_id)
        if record is None:
            cached_order = self.cache.order(client_order_id)
            if cached_order is not None and "MARKET_EXIT" in (cached_order.tags or []):
                record = self._execution.register_order(
                    client_order_id=order_id,
                    instrument_id=str(iid),
                    side=cached_order.side.name,
                    requested_quantity=cached_order.quantity.as_double(),
                    role=OrderRole.EMERGENCY_EXIT,
                    signal_version="MARKET_EXIT",
                    ts_ns=int(getattr(event, "ts_event", self.clock.timestamp_ns())),
                )
        if record is None:
            if self.config.execution_mode in {"paper", "live"}:
                self._execution_safety.mark_uncertain(
                    f"Received broker event for unclaimed order {order_id} on {iid}.",
                    ts_ns=int(getattr(event, "ts_event", self.clock.timestamp_ns())),
                )
                self._cancel_working_entry_orders(reason="unclaimed broker order")
            self._reconcile_committed_notional(iid)
            return

        event_id = str(getattr(event, "event_id", ""))
        ts_ns = int(getattr(event, "ts_event", self.clock.timestamp_ns()))
        venue_order_id = str(getattr(event, "venue_order_id", "") or "")
        permanent_order_id = ""
        if "-" in venue_order_id:
            candidate = venue_order_id.rsplit("-", 1)[-1]
            if candidate.isdigit() and candidate != "0":
                permanent_order_id = candidate
        reason = str(getattr(event, "reason", "") or "")
        self._audit_event(
            "BROKER_ORDER_EVENT",
            {
                "event_class": type(event).__name__,
                "event_id": event_id,
                "client_order_id": order_id,
                "instrument_id": str(iid),
                "venue_order_id": venue_order_id,
                "permanent_order_id": permanent_order_id,
                "reason": reason,
            },
            severity=(
                "CRITICAL"
                if isinstance(
                    event,
                    (OrderRejected, OrderDenied, OrderCancelRejected, OrderModifyRejected),
                )
                else "INFO"
            ),
            correlation_id=order_id,
            event_id=(f"broker-event:{event_id}" if event_id else None),
        )
        try:
            if isinstance(event, OrderSubmitted):
                self._execution.apply_order_state(
                    order_id,
                    LifecycleStatus.SUBMITTED,
                    event_id=event_id,
                    ts_ns=ts_ns,
                    venue_order_id=venue_order_id,
                    permanent_order_id=permanent_order_id,
                )
            elif isinstance(event, OrderAccepted):
                self._execution.apply_order_state(
                    order_id,
                    LifecycleStatus.ACKNOWLEDGED,
                    event_id=event_id,
                    ts_ns=ts_ns,
                    venue_order_id=venue_order_id,
                    permanent_order_id=permanent_order_id,
                )
            elif isinstance(event, OrderUpdated):
                self._execution.amend_order(
                    order_id,
                    requested_quantity=event.quantity.as_double(),
                    ts_ns=ts_ns,
                )
            elif isinstance(event, OrderPendingCancel):
                self._execution.apply_order_state(
                    order_id,
                    LifecycleStatus.PENDING_CANCEL,
                    event_id=event_id,
                    ts_ns=ts_ns,
                )
            elif isinstance(event, OrderFilled):
                info = getattr(event, "info", None) or {}
                correction_of = str(info.get("correction_of", ""))
                self._execution.apply_fill(
                    client_order_id=order_id,
                    execution_id=str(event.trade_id),
                    instrument_id=str(iid),
                    side=event.order_side.name,
                    quantity=event.last_qty.as_double(),
                    price=event.last_px.as_double(),
                    ts_ns=ts_ns,
                    event_id=event_id,
                    correction_of=correction_of,
                )
            elif isinstance(event, OrderCanceled):
                self._execution.apply_order_state(
                    order_id,
                    LifecycleStatus.CANCELED,
                    event_id=event_id,
                    ts_ns=ts_ns,
                )
            elif isinstance(event, OrderExpired):
                self._execution.apply_order_state(
                    order_id,
                    LifecycleStatus.EXPIRED,
                    event_id=event_id,
                    ts_ns=ts_ns,
                )
            elif isinstance(event, (OrderRejected, OrderDenied)):
                status = (
                    LifecycleStatus.DENIED
                    if isinstance(event, OrderDenied)
                    else LifecycleStatus.REJECTED
                )
                self._execution.apply_order_state(
                    order_id,
                    status,
                    event_id=event_id,
                    ts_ns=ts_ns,
                    reason=reason,
                )
                self._execution_safety.on_rejection(
                    reason or f"{type(event).__name__} without reason",
                    ts_ns=ts_ns,
                    client_order_id=order_id,
                    instrument_id=str(iid),
                )
                if not self._execution_safety.entries_allowed:
                    self._cancel_working_entry_orders(
                        reason="order rejection suspended strategy"
                    )
                if record.role in {OrderRole.STOP_LOSS, OrderRole.TAKE_PROFIT}:
                    self._request_instrument_exit(iid, "broker protection rejected")
            elif isinstance(event, OrderCancelRejected):
                self._execution.apply_cancel_rejected(
                    order_id,
                    event_id=event_id,
                    ts_ns=ts_ns,
                    reason=reason,
                )
                self._execution_safety.on_cancel_rejected(
                    reason or "Broker rejected order cancellation.",
                    ts_ns=ts_ns,
                    client_order_id=order_id,
                    instrument_id=str(iid),
                )
            elif isinstance(event, OrderModifyRejected):
                self._execution_safety.suspend(
                    reason or "Broker rejected order modification.",
                    code="ORDER_MODIFY_REJECTED",
                    ts_ns=ts_ns,
                    client_order_id=order_id,
                    instrument_id=str(iid),
                )
                if record.role in {OrderRole.STOP_LOSS, OrderRole.TAKE_PROFIT}:
                    self._request_instrument_exit(iid, "protection modification rejected")
        except (KeyError, ValueError) as exc:
            self._execution_safety.mark_uncertain(
                f"Execution event could not be applied idempotently: {exc}",
                ts_ns=ts_ns,
            )
            self.log.error(f"Execution ledger failure: {exc}")

        self._reconcile_committed_notional(iid)
        if isinstance(event, OrderFilled):
            if record.role == OrderRole.ENTRY:
                self._ensure_broker_protection(iid)
            elif record.role in {
                OrderRole.STOP_LOSS,
                OrderRole.TAKE_PROFIT,
                OrderRole.SIGNAL_EXIT,
                OrderRole.EMERGENCY_EXIT,
            }:
                net, _ = self._broker_position_state(iid)
                if net == 0:
                    self._cancel_protection(iid, "exit filled")
                    self._pending_exits.pop(iid, None)
                elif record.role in {OrderRole.STOP_LOSS, OrderRole.TAKE_PROFIT}:
                    self._ensure_broker_protection(iid)
        if isinstance(event, (OrderAccepted, OrderUpdated)):
            self._refresh_protection_status(iid)
        if iid in self._pending_exits and isinstance(
            event,
            (OrderCanceled, OrderExpired, OrderRejected, OrderDenied, OrderFilled),
        ):
            self._continue_pending_exit(iid)
        if self._staged_exit_active and isinstance(
            event,
            (
                OrderCanceled,
                OrderExpired,
                OrderRejected,
                OrderDenied,
                OrderFilled,
                OrderCancelRejected,
            ),
        ):
            self._advance_staged_market_exit()
        if self._telemetry is not None and not self._telemetry_failed:
            try:
                self._telemetry.refresh(
                    positions=self._telemetry_positions(),
                    risk=self._telemetry_risk(),
                    model=self._telemetry_model(),
                )
            except Exception as exc:  # noqa: BLE001
                self._telemetry_failed = True
                self.log.error(
                    f"Live telemetry disabled after execution-event write failure: {exc}"
                )

    def on_save(self) -> dict[str, bytes]:
        if self._risk is None:
            return {}
        payload = {
            "version": 6,
            "instrument_ids": sorted(self.config.instrument_ids),
            "risk": self._risk.snapshot(),
            "account_equity_baseline": self._account_equity_baseline,
            "execution": self._execution.snapshot(),
            "execution_safety": self._execution_safety.snapshot(),
            "data_quality": self._data_quality.snapshot(),
            "data_quality_issues": list(self._data_quality_issues),
            "data_quality_blocked_instruments": sorted(self._data_quality_blocked_instruments),
            "data_quality_good_bars": dict(self._data_quality_good_bars),
            "protection_ids": {
                self._raw_by_iid[iid]: values
                for iid, values in self._protection_ids.items()
                if iid in self._raw_by_iid
            },
            "entry_submitted_bar": dict(self._entry_submitted_bar),
            "pending_exits": {
                self._raw_by_iid[iid]: reason
                for iid, reason in self._pending_exits.items()
                if iid in self._raw_by_iid
            },
            "instruments": {
                str(iid): {
                    "closes": list(self._closes[iid]),
                    "bar_times": list(self._bar_times[iid]),
                    "news_scores": list(self._news_scores[iid]),
                    "highs": list(self._highs[iid]),
                    "lows": list(self._lows[iid]),
                    "last_bar_ns": self._last_bar_ns[iid],
                    "bar_index": self._bar_index[iid],
                }
                for iid in self._bar_types
            },
            "telemetry_series": (
                {
                    ticker: list(points)
                    for ticker, points in self._telemetry.points.items()
                }
                if self._telemetry is not None
                else {}
            ),
            "position_references": {
                self._raw_by_iid[iid]: values
                for iid, values in self._position_references.items()
                if iid in self._raw_by_iid
            },
        }
        return {"ml_strategy.json": json.dumps(payload).encode("utf-8")}

    def on_load(self, state: dict[str, bytes]) -> None:
        raw = state.get("ml_strategy.json")
        if raw is None:
            return
        payload = json.loads(raw.decode("utf-8"))
        if payload.get("version") not in {1, 2, 3, 4, 5, 6}:
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
        if payload.get("execution"):
            self._execution = ExecutionLedger.from_snapshot(payload["execution"])
        if payload.get("execution_safety"):
            self._execution_safety = ExecutionSafetyController.from_snapshot(
                payload["execution_safety"]
            )
            self._execution_safety.on_restart(ts_ns=self.clock.timestamp_ns())
        if payload.get("data_quality"):
            self._data_quality.restore(payload["data_quality"])
        self._data_quality_issues.extend(payload.get("data_quality_issues", ()))
        self._data_quality_blocked_instruments = {
            str(value)
            for value in payload.get("data_quality_blocked_instruments", ())
        }
        self._data_quality_good_bars.update(
            {
                str(key): int(value)
                for key, value in payload.get("data_quality_good_bars", {}).items()
            }
        )
        self._entry_submitted_bar.update(
            {
                str(order_id): int(bar_index)
                for order_id, bar_index in payload.get("entry_submitted_bar", {}).items()
            }
        )
        for raw, values in payload.get("instruments", {}).items():
            iid = self._iid_by_raw.get(raw)
            if iid is None:
                continue
            self._closes[iid].extend(values.get("closes", ()))
            self._bar_times[iid].extend(values.get("bar_times", ()))
            saved_news = list(values.get("news_scores", ()))
            if len(saved_news) < len(self._closes[iid]):
                saved_news = [0.0] * (len(self._closes[iid]) - len(saved_news)) + saved_news
            self._news_scores[iid].extend(saved_news[-len(self._closes[iid]):])
            if self._news_scores[iid]:
                self._news_meta[iid] = NewsFeatureSnapshot(score=self._news_scores[iid][-1])
            self._highs[iid].extend(values.get("highs", ()))
            self._lows[iid].extend(values.get("lows", ()))
            self._last_bar_ns[iid] = int(values.get("last_bar_ns", 0))
            self._bar_index[iid] = int(values.get("bar_index", 0))
        for raw, values in payload.get("position_references", {}).items():
            iid = self._iid_by_raw.get(raw)
            if iid is not None and isinstance(values, dict):
                self._position_references[iid] = values
        for raw, values in payload.get("protection_ids", {}).items():
            iid = self._iid_by_raw.get(raw)
            if iid is not None and isinstance(values, dict):
                self._protection_ids[iid] = {
                    str(key): str(value) for key, value in values.items()
                }
        for raw, reason in payload.get("pending_exits", {}).items():
            iid = self._iid_by_raw.get(raw)
            if iid is not None:
                self._pending_exits[iid] = str(reason)
        if self._telemetry is not None:
            self._telemetry.restore_series(payload.get("telemetry_series", {}))
            if self._telemetry.points:
                self._telemetry.refresh(
                    positions=self._telemetry_positions(),
                    risk=self._telemetry_risk(),
                    model=self._telemetry_model(),
                )

    def on_stop(self) -> None:
        # Order resolution/submission is forbidden during on_stop. In broker
        # modes StrategyConfig.manage_stop runs Nautilus's cancel-confirm-
        # flatten-confirm market-exit sequence before this hook.
        self._pending.clear()
        if self._risk_timer_name in self.clock.timer_names:
            self.clock.cancel_timer(self._risk_timer_name)
        if self._staged_exit_timer_name in self.clock.timer_names:
            self.clock.cancel_timer(self._staged_exit_timer_name)
        if self.config.execution_mode in {"paper", "live"}:
            open_orders = self.cache.orders_open(strategy_id=self.id)
            inflight_orders = self.cache.orders_inflight(strategy_id=self.id)
            positions = self.cache.positions_open(strategy_id=self.id)
            if open_orders or inflight_orders or positions:
                details = (
                    f"Shutdown left {len(open_orders)} open broker orders, "
                    f"{len(inflight_orders)} inflight orders, and "
                    f"{len(positions)} residual positions."
                )
                self._execution_safety.finish_shutdown(
                    clean=False,
                    reason=details,
                    ts_ns=self.clock.timestamp_ns(),
                )
                self.log.error(details)
        if self._telemetry is not None and not self._telemetry_failed:
            try:
                self._telemetry.stop(
                    positions=self._telemetry_positions(),
                    risk=self._telemetry_risk(),
                    model=self._telemetry_model(),
                )
            except Exception as exc:  # noqa: BLE001
                self.log.error(f"Could not finalize live telemetry: {exc}")
        if self._news_reader is not None:
            self._news_reader.close()
        for iid, bt in self._bar_types.items():
            self.unsubscribe_bars(bt)
            if self.config.execution_mode in {"paper", "live"}:
                self.unsubscribe_quote_ticks(iid)
                self.unsubscribe_trade_ticks(iid)
                self.unsubscribe_instrument_status(iid)
        self._audit_event(
            "STRATEGY_STOPPED",
            {
                "risk_state": self._risk.state.value if self._risk is not None else None,
                "execution_state": self._execution_safety.state.value,
                "broker_exposure_remaining": self._has_broker_exposure(),
            },
            severity=(
                "CRITICAL" if self._has_broker_exposure() else "INFO"
            ),
        )
        if self._operations is not None and not self._operations_failed:
            try:
                self._operations.heartbeat(
                    self._operations_target(),
                    str(self.id),
                    status="STOPPED",
                    details={"broker_exposure_remaining": self._has_broker_exposure()},
                    observed_at=self.clock.utc_now(),
                )
            finally:
                self._operations.close()
