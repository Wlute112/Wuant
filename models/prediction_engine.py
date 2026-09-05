"""Prediction (alpha) layer -- fully decoupled from execution.

This is the "ML layer" in your two-layer design:

    input  x  = lagged feature vector built from past bars only
    output yhat = predicted forward return over `horizon` bars

Design guarantees (the anti-lookahead contract):
  * Features at time t use ONLY bars with index <= t. The target uses bars
    STRICTLY in the future (t+1 .. t+horizon) and is therefore never available
    to the model at prediction time.
  * Training uses a walk-forward split. The model is fit on past rows only,
    using either expanding history or a configured trailing training window,
    and predicts only later rows. No future row leaks into fitted coefficients.
  * `predict_move(window)` is the live/bar-by-bar entry point: it accepts the
    trailing window of closes a Strategy has accumulated and returns a single
    yhat. It recomputes nothing about the future and holds no global state.

The Strategy layer NEVER sees how yhat is produced -- it only consumes yhat.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from sklearn.linear_model import HuberRegressor

from quant.models.cross_asset import (
    INDUSTRY_FEATURE_NAMES,
    aligned_peer_returns,
    coerce_price_history,
    make_industry_correlation_feature_row,
    make_industry_correlation_features,
)
from quant.models.regime import RegimeConfig, RegimeFeatureEngine, STATE_NAMES


@dataclass
class PredictionConfig:
    n_lags: int = 5            # number of lagged return features (x dimension)
    horizon: int = 1           # forecast horizon in bars (predict fwd return)
    standardize: bool = True   # z-score features using TRAIN stats only
    min_train_bars: int = 120  # refuse to predict before enough history
    # Number of most-recent labeled feature rows used by each Huber refit.
    # 0 preserves the original expanding-history behavior. A positive value
    # bounds coefficient estimation to a rolling window while regime/cross-
    # asset features still use all information available at that timestamp.
    training_window_bars: int = 0
    # --- Huber-loss regression hyperparameters ---
    # The alpha layer fits with HUBER loss (not OLS): squared error for small
    # residuals, linear (absolute) error beyond `huber_epsilon` standardized
    # residuals. This makes yhat robust to fat-tailed return outliers / gaps,
    # which dominate financial data and would otherwise dominate an OLS fit.
    huber_epsilon: float = 1.35  # smaller -> more robust; 1.35 ~ 95% OLS efficiency
    huber_alpha: float = 1e-4    # L2 regularization strength on coefficients
    huber_max_iter: int = 400    # solver iterations
    # --- regime-detection features (see models/regime.py) ---
    # Appended AFTER the lagged returns so the Huber model can condition its
    # mean-reversion forecast on the prevailing regime. Both features are
    # walk-forward / no-lookahead, so they slot into make_features_targets and
    # walk_forward() without breaking the anti-lookahead contract.
    use_regime_features: bool = True   # add regime_score (transition-matrix)
    use_hmm_feature: bool = True       # also add the GaussianHMM latent state
    regime_window: int = 20            # rolling-return lookback for labelling
    regime_bull_threshold: float = 0.02
    regime_bear_threshold: float = -0.02
    # --- fit vs raw source for each regime feature ---
    # "fit": the feature is a column in the Huber regression's X matrix, jointly
    # weighted with every other fit-mode feature (original behaviour).
    # "raw": the feature BYPASSES the Huber fit entirely and contributes
    # `value * <feature>_raw_scale` directly to yhat instead (the Huber model,
    # if any fit-mode features remain, is fit on the RESIDUAL target after
    # subtracting every raw contribution -- see PredictionEngine._fit_arrays
    # callers). Lets you use e.g. just the HMM signal as a fraction of yhat
    # without it ever passing through the regression.
    regime_source: str = "fit"     # "fit" | "raw"; only meaningful when use_regime_features
    hmm_source: str = "fit"        # "fit" | "raw"; only meaningful when use_hmm_feature
    regime_raw_scale: float = 1.0  # yhat contribution = regime_score * this, when raw
    hmm_raw_scale: float = 1.0     # yhat contribution = hmm_signed * this, when raw
    # --- cross-asset ARDL + spread features (see make_cross_asset_features) ---
    # Appended AFTER the regime columns, one block per symbol in `peer_symbols`:
    #   ARDL lags:   peer_log_return[i-1] .. peer_log_return[i-cross_asset_lags]
    #   spread lags: (own_return - peer_return)[i-1] .. [i-spread_lags]
    # Both blocks are strictly lagged (lag >= 1, never the current bar), so a
    # peer's bar for "today" need not have arrived yet -- see
    # make_cross_asset_features for the no-lookahead argument. `cross_asset_lags`
    # / `spread_lags` are Optuna-tunable (like n_lags); `peer_symbols` is the
    # run's universe, set once (structural, NOT Optuna-tuned).
    cross_asset_lags: int = 0
    spread_lags: int = 0
    peer_symbols: tuple[str, ...] = ()
    # --- industry correlation factor -----------------------------------
    # ``industry_peer_symbols`` is resolved structurally before the engine is
    # created. The five resulting columns are a correlation-weighted peer
    # return, peer momentum, beta residual z-score, breadth, and average
    # correlation. They are fit-mode features and are never raw-added to yhat.
    use_industry_features: bool = False
    industry_peer_symbols: tuple[str, ...] = ()
    industry_correlation_window_bars: int = 60
    industry_correlation_half_life_bars: int = 20
    industry_minimum_observations: int = 40
    industry_minimum_correlation: float = 0.25
    industry_correlation_shrinkage: float = 0.20
    industry_momentum_bars: int = 5
    # --- live/historical news feature -----------------------------------
    # One bounded, source-weighted score aligned to each completed bar.  The
    # caller supplies the causal series; values are built only from articles
    # received no later than that bar (see quant.news.NewsFeatureReader).
    use_news_features: bool = False
    news_source: str = "raw"          # "fit" | "raw"
    news_raw_scale: float = 0.001     # max direct return contribution at score=1
    news_score_clip: float = 1.0

    def to_regime_config(self) -> RegimeConfig:
        """Project the regime-relevant knobs onto a RegimeConfig."""
        return RegimeConfig(
            window=self.regime_window,
            bull_threshold=self.regime_bull_threshold,
            bear_threshold=self.regime_bear_threshold,
            use_hmm=self.use_hmm_feature,
        )

    @property
    def n_regime_cols(self) -> int:
        """How many regime columns land in the Huber FIT vector.

        Columns whose source is "raw" bypass the fit entirely (see
        regime_source/hmm_source) and are excluded from this count -- it
        exists to slice a fitted model's coef_ array, which only ever
        contains fit-mode columns.
        """
        n = 0
        if self.use_regime_features and self.regime_source != "raw":
            n += 1
        if self.use_hmm_feature and self.hmm_source != "raw":
            n += 1
        return n

    @property
    def n_cross_cols(self) -> int:
        """How many cross-asset (ARDL + spread) columns get appended per row."""
        if not self.peer_symbols:
            return 0
        return len(self.peer_symbols) * (self.cross_asset_lags + self.spread_lags)

    @property
    def n_industry_cols(self) -> int:
        return len(INDUSTRY_FEATURE_NAMES) if self.use_industry_features else 0

    @property
    def required_peer_symbols(self) -> tuple[str, ...]:
        legacy = (
            self.peer_symbols
            if self.cross_asset_lags > 0 or self.spread_lags > 0
            else ()
        )
        industry = self.industry_peer_symbols if self.use_industry_features else ()
        return tuple(dict.fromkeys((*legacy, *industry)))

    @property
    def n_news_cols(self) -> int:
        return int(self.use_news_features and self.news_source != "raw")


@dataclass
class _FitState:
    coef: np.ndarray | None = None
    intercept: float = 0.0
    feat_mean: np.ndarray | None = None
    feat_std: np.ndarray | None = None
    fitted: bool = False
    meta: dict = field(default_factory=dict)


def _log_returns(close: np.ndarray) -> np.ndarray:
    close = np.asarray(close, dtype=float)
    out = np.zeros_like(close)
    out[1:] = np.diff(np.log(close))
    return out


def make_cross_asset_features(
    target_close: np.ndarray,
    peer_closes: dict[str, object],
    cfg: PredictionConfig,
    target_timestamps: np.ndarray | None = None,
) -> np.ndarray | None:
    """Build the ARDL + spread cross-asset feature matrix aligned to `target_close`.

    Column blocks, one per symbol in `cfg.peer_symbols` (fixed order, so the
    training path and the live predict_move path build identical vectors):
        ARDL lags:   peer_log_return[i-1] .. peer_log_return[i-cross_asset_lags]
        spread lags: (own_return - peer_return)[i-1] .. [i-spread_lags]
    where `own_return`/`peer_return` are scale-free, time-additive log returns
    (see `_log_returns`) so the spread is a stationary basis series regardless
    of the two assets' price levels.

    No-lookahead: every column is built with `pandas.Series.shift(lag)` for
    `lag >= 1`, so row i only ever reads target/peer returns at index <= i-1 --
    strictly the same "past only" contract `make_features_targets` already
    enforces for the own-AR lags. Using lag >= 1 (never the current bar, lag 0)
    is deliberately conservative: it means a peer instrument's bar for "today"
    need not have arrived yet for this row to be computable, which matters in
    live/backtest settings where bars for different instruments in the universe
    are not guaranteed to be delivered in lockstep.

    A peer whose history is SHORTER than `target_close` is right-aligned: its
    returns are treated as populating the most recent `len(peer_close)` rows,
    and the earlier rows (before that peer had any history) get a neutral 0.0
    -- "no cross-asset signal available yet" -- rather than raising. Returns
    None if `cfg.peer_symbols` is empty or both lag depths are 0 (feature off).
    """
    if not cfg.peer_symbols or (cfg.cross_asset_lags <= 0 and cfg.spread_lags <= 0):
        return None
    n = len(target_close)
    target_r = _log_returns(np.asarray(target_close, dtype=float))

    def shifted(values, lag):
        result = np.zeros(n, dtype=float)
        if lag < n:
            result[lag:] = values[:-lag]
        result[np.isnan(result)] = 0.0
        return result

    cols = []
    for sym in cfg.peer_symbols:
        peer_r, _ = aligned_peer_returns(
            n, target_timestamps, peer_closes.get(sym, ())
        )
        for lag in range(1, cfg.cross_asset_lags + 1):
            cols.append(shifted(peer_r, lag))
        if cfg.spread_lags > 0:
            spread = target_r - peer_r
            for lag in range(1, cfg.spread_lags + 1):
                cols.append(shifted(spread, lag))

    return np.column_stack(cols) if cols else None


def make_features_targets(
    close: np.ndarray,
    cfg: PredictionConfig,
    regime_feats: np.ndarray | None = None,
    cross_feats: np.ndarray | None = None,
    industry_feats: np.ndarray | None = None,
    news_feats: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Build (X, y, valid_index) with a strict no-lookahead alignment.

    X[i]  uses returns at i-n_lags .. i-1   (past only), followed by the regime
          feature columns for bar i (regime_feats[i]), followed by the
          cross-asset ARDL/spread columns for bar i (cross_feats[i]), industry
          correlation columns (industry_feats[i]), followed by the news score
          available at bar i (news_feats[i]).
    y[i]  = sum of returns at i+1 .. i+horizon (future only) -> fwd return

    `regime_feats`, when supplied, is an (n, k) array aligned to `close` indices
    where row i is a walk-forward regime feature vector using ONLY close[0..i]
    (see models/regime.py). `cross_feats`, when supplied, is an (n, j) array
    from `make_cross_asset_features` where row i uses ONLY target/peer returns
    at index <= i-1. Because each row depends only on its own past, both are
    safe to append here and to slice in walk_forward(). ``news_feats`` follows
    the same rule: the ingestion timestamp, not a later-recovered publication
    timestamp, controls when an article becomes visible. When None, the
    corresponding block is simply omitted (original behaviour).

    Rows without full past lags or full future horizon are dropped.
    """
    r = _log_returns(close)
    n = len(r)
    L, H = cfg.n_lags, cfg.horizon
    k_regime = 0 if regime_feats is None else regime_feats.shape[1]
    k_cross = 0 if cross_feats is None else cross_feats.shape[1]
    k_industry = 0 if industry_feats is None else industry_feats.shape[1]
    k_news = 0 if news_feats is None else news_feats.shape[1]

    count = n - H - L
    if count <= 0:
        return np.empty((0, L + k_regime + k_cross + k_industry + k_news)), np.empty((0,)), np.empty((0,), dtype=int)
    # Views replace per-row Python slicing/concatenation. Sum each future
    # window directly (not cumulative-sum subtraction), preserving rounding.
    lagged = (np.lib.stride_tricks.sliding_window_view(r, L)[:count]
              if L else np.empty((count, 0)))
    blocks = [lagged]
    blocks.extend(feats[L:n - H] for feats in
                  (regime_feats, cross_feats, industry_feats, news_feats)
                  if feats is not None)
    X = np.concatenate(blocks, axis=1)
    y = np.lib.stride_tricks.sliding_window_view(r, H)[L + 1:L + 1 + count].sum(axis=1)
    return X, y, np.arange(L, n - H, dtype=int)


class PredictionEngine:
    """Huber-regression forecaster with walk-forward evaluation."""

    def __init__(self, cfg: PredictionConfig | None = None):
        self.cfg = cfg or PredictionConfig()
        if self.cfg.training_window_bars < 0:
            raise ValueError("training_window_bars must be >= 0")
        if 0 < self.cfg.training_window_bars < self.cfg.min_train_bars:
            raise ValueError(
                "training_window_bars must be 0 (expanding) or at least min_train_bars"
            )
        if self.cfg.news_source not in {"fit", "raw"}:
            raise ValueError("news_source must be 'fit' or 'raw'")
        if self.cfg.news_score_clip <= 0:
            raise ValueError("news_score_clip must be > 0")
        if self.cfg.industry_correlation_window_bars < 2:
            raise ValueError("industry_correlation_window_bars must be >= 2")
        if self.cfg.industry_correlation_half_life_bars < 1:
            raise ValueError("industry_correlation_half_life_bars must be >= 1")
        if self.cfg.industry_minimum_observations < 2:
            raise ValueError("industry_minimum_observations must be >= 2")
        if not 0.0 <= self.cfg.industry_minimum_correlation < 1.0:
            raise ValueError("industry_minimum_correlation must be in [0, 1)")
        if not 0.0 <= self.cfg.industry_correlation_shrinkage <= 1.0:
            raise ValueError("industry_correlation_shrinkage must be in [0, 1]")
        if self.cfg.industry_momentum_bars < 1:
            raise ValueError("industry_momentum_bars must be >= 1")
        self._state = _FitState()
        self._industry_cache: np.ndarray | None = None
        self._industry_cache_target_state: (
            tuple[int, float | None, object | None] | None
        ) = None
        self._industry_cache_peer_state: dict[str, tuple[int, float | None, int, object | None]] = {}
        # One cached, incremental regime engine per PredictionEngine instance.
        # It is stateful so the per-bar refit in the backtest only computes the
        # new tail (see models/regime.py). None when regime features are off.
        self._regime = (
            RegimeFeatureEngine(self.cfg.to_regime_config())
            if self.cfg.use_regime_features
            else None
        )

    def _regime_feats(self, close: np.ndarray) -> np.ndarray | None:
        """Walk-forward regime FIT-mode feature matrix aligned to `close`, or
        None. Only includes columns whose source is "fit" -- a column set to
        "raw" bypasses the Huber fit entirely (see `_regime_raw`).

        Column order (regime_score, then optional hmm_signed) is fixed so the
        training path and the live predict_move path build identical vectors.
        """
        if self._regime is None:
            return None
        frame = self._regime.compute(np.asarray(close, float))
        cols = []
        if self.cfg.use_regime_features and self.cfg.regime_source != "raw":
            cols.append(frame.regime_score)
        if self.cfg.use_hmm_feature and self.cfg.hmm_source != "raw":
            cols.append(frame.hmm_signed)
        return np.column_stack(cols) if cols else None

    def _regime_raw(self, close: np.ndarray) -> np.ndarray | None:
        """Walk-forward RAW (non-fit) regime contribution to yhat, or None.

        Each block whose source is "raw" contributes `value * <feature>_raw_scale`
        directly, added to the Huber fit's prediction by the caller (fit(),
        refit_on_history(), walk_forward(), predict_move()) -- never entering
        the regression itself.
        """
        if self._regime is None:
            return None
        frame = self._regime.compute(np.asarray(close, float))
        total = None
        if self.cfg.use_regime_features and self.cfg.regime_source == "raw":
            total = frame.regime_score * self.cfg.regime_raw_scale
        if self.cfg.use_hmm_feature and self.cfg.hmm_source == "raw":
            c = frame.hmm_signed * self.cfg.hmm_raw_scale
            total = c if total is None else total + c
        return total

    def _cross_feats(
        self,
        close: np.ndarray,
        peer_closes: dict[str, object] | None,
        timestamps: np.ndarray | None = None,
    ) -> np.ndarray | None:
        """Walk-forward ARDL/spread cross-asset feature matrix, or None.

        `peer_closes` maps peer symbol -> that peer's close series, keyed by the
        SAME symbols as `cfg.peer_symbols` (see make_cross_asset_features). None
        or an empty dict simply disables the block (same as no peer_symbols).
        """
        if not self.cfg.peer_symbols or not peer_closes:
            return None
        return make_cross_asset_features(
            np.asarray(close, float), peer_closes, self.cfg, timestamps
        )

    def _industry_feats(
        self,
        close: np.ndarray,
        peer_closes: dict[str, object] | None,
        timestamps: np.ndarray | None = None,
    ) -> np.ndarray | None:
        if not self.cfg.use_industry_features:
            return None
        target = np.asarray(close, dtype=float)
        peer_closes = peer_closes or {}
        target_state = self._target_history_state(target, timestamps)
        peer_state = self._peer_history_state(peer_closes)
        if (
            self._industry_cache is not None
            and target_state == self._industry_cache_target_state
            and peer_state == self._industry_cache_peer_state
        ):
            return self._industry_cache

        can_append = (
            self._industry_cache is not None
            and len(target) == len(self._industry_cache) + 1
            and self._target_history_extends(target, timestamps)
            and self._peer_histories_extend(peer_closes)
        )
        kwargs = self._industry_feature_kwargs()
        if can_append:
            latest = make_industry_correlation_feature_row(
                target,
                peer_closes,
                self.cfg.industry_peer_symbols,
                target_timestamps=timestamps,
                **kwargs,
            )
            result = np.vstack([self._industry_cache, latest])
        else:
            result = make_industry_correlation_features(
                target,
                peer_closes,
                self.cfg.industry_peer_symbols,
                target_timestamps=timestamps,
                **kwargs,
            )
        self._industry_cache = result
        self._industry_cache_target_state = target_state
        self._industry_cache_peer_state = peer_state
        return result

    def _industry_feature_kwargs(self) -> dict:
        return dict(
            correlation_window_bars=self.cfg.industry_correlation_window_bars,
            correlation_half_life_bars=self.cfg.industry_correlation_half_life_bars,
            minimum_observations=self.cfg.industry_minimum_observations,
            minimum_correlation=self.cfg.industry_minimum_correlation,
            correlation_shrinkage=self.cfg.industry_correlation_shrinkage,
            momentum_bars=self.cfg.industry_momentum_bars,
        )

    @staticmethod
    def _last_timestamp(timestamps: np.ndarray | None, index: int = -1) -> object | None:
        if timestamps is None or len(timestamps) == 0:
            return None
        value = np.asarray(timestamps)[index]
        return int(value.value) if hasattr(value, "value") else str(value)

    @classmethod
    def _target_history_state(
        cls, target: np.ndarray, timestamps: np.ndarray | None
    ) -> tuple[int, float | None, object | None]:
        return (
            len(target),
            float(target[-1]) if len(target) else None,
            cls._last_timestamp(timestamps),
        )

    def _peer_history_state(
        self, peer_closes: dict[str, object]
    ) -> dict[str, tuple[int, float | None, int, object | None]]:
        state = {}
        for symbol in self.cfg.industry_peer_symbols:
            history = coerce_price_history(peer_closes.get(symbol, ()))
            closes = np.asarray(history.closes, dtype=float)
            timestamp_count = 0 if history.timestamps is None else len(history.timestamps)
            state[symbol] = (
                len(closes),
                float(closes[-1]) if len(closes) else None,
                timestamp_count,
                self._last_timestamp(history.timestamps),
            )
        return state

    def _target_history_extends(
        self, target: np.ndarray, timestamps: np.ndarray | None
    ) -> bool:
        prior = self._industry_cache_target_state
        if prior is None or prior[0] <= 0:
            return prior is not None and prior[0] == 0
        prior_length, prior_close, prior_timestamp = prior
        return (
            len(target) > prior_length
            and float(target[prior_length - 1]) == prior_close
            and self._last_timestamp(timestamps, prior_length - 1) == prior_timestamp
        )

    def _peer_histories_extend(self, peer_closes: dict[str, object]) -> bool:
        for symbol, prior in self._industry_cache_peer_state.items():
            history = coerce_price_history(peer_closes.get(symbol, ()))
            closes = np.asarray(history.closes, dtype=float)
            prior_close_count, prior_close, prior_ts_count, prior_ts = prior
            if len(closes) < prior_close_count:
                return False
            if prior_close_count and float(closes[prior_close_count - 1]) != prior_close:
                return False
            if history.timestamps is None:
                if prior_ts_count:
                    return False
            else:
                if len(history.timestamps) < prior_ts_count:
                    return False
                if prior_ts_count and self._last_timestamp(
                    history.timestamps, prior_ts_count - 1
                ) != prior_ts:
                    return False
        return True

    def _news_full(
        self, close: np.ndarray, news_features: np.ndarray | None
    ) -> np.ndarray | None:
        """Right-align and bound the causal score series to ``close``."""
        if not self.cfg.use_news_features:
            return None
        n = len(close)
        aligned = np.zeros(n, dtype=float)
        if news_features is not None:
            supplied = np.asarray(news_features, dtype=float).reshape(-1)
            supplied = np.nan_to_num(supplied, nan=0.0, posinf=0.0, neginf=0.0)
            m = min(n, supplied.size)
            if m:
                aligned[n - m:] = supplied[-m:]
        return np.clip(aligned, -self.cfg.news_score_clip, self.cfg.news_score_clip)

    def _news_fit(
        self, close: np.ndarray, news_features: np.ndarray | None
    ) -> np.ndarray | None:
        full = self._news_full(close, news_features)
        if full is None or self.cfg.news_source == "raw":
            return None
        return full.reshape(-1, 1)

    def _news_raw(
        self, close: np.ndarray, news_features: np.ndarray | None
    ) -> np.ndarray | None:
        full = self._news_full(close, news_features)
        if full is None or self.cfg.news_source != "raw":
            return None
        return full * float(self.cfg.news_raw_scale)

    def _raw_full(
        self, close: np.ndarray, news_features: np.ndarray | None
    ) -> np.ndarray | None:
        regime = self._regime_raw(close)
        news = self._news_raw(close, news_features)
        if regime is None:
            return news
        if news is None:
            return regime
        return regime + news

    def _residualize(self, y: np.ndarray, idx: np.ndarray, raw_full: np.ndarray | None) -> np.ndarray:
        """Subtract each row's raw (non-fit) contribution from the target so
        the Huber fit only ever has to explain what raw features don't."""
        if raw_full is None:
            return y
        return y - raw_full[idx]

    def _training_tail(self, X: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Apply the configured rolling fit window to aligned training rows."""
        window = int(self.cfg.training_window_bars)
        if window <= 0 or len(X) <= window:
            return X, y
        return X[-window:], y[-window:]

    # ---- training -------------------------------------------------------
    def fit(
        self,
        close: np.ndarray,
        peer_closes: dict[str, object] | None = None,
        news_features: np.ndarray | None = None,
        *,
        timestamps: np.ndarray | None = None,
    ) -> "PredictionEngine":
        close = np.asarray(close, float)
        X, y, idx = make_features_targets(
            close,
            self.cfg,
            self._regime_feats(close),
            self._cross_feats(close, peer_closes, timestamps),
            self._industry_feats(close, peer_closes, timestamps),
            self._news_fit(close, news_features),
        )
        if len(X) == 0:
            raise ValueError("Not enough data to build features/targets.")
        y_fit = self._residualize(y, idx, self._raw_full(close, news_features))
        X, y_fit = self._training_tail(X, y_fit)
        self._fit_arrays(X, y_fit)
        return self

    def refit_on_history(
        self,
        close: np.ndarray,
        peer_closes: dict[str, object] | None = None,
        news_features: np.ndarray | None = None,
        *,
        timestamps: np.ndarray | None = None,
    ) -> bool:
        """Fit on PAST-ONLY history using the SAME windowing contract as
        walk_forward(): build (X, y) via make_features_targets, then fit on all
        available rows or the configured trailing training window. This is the
        single, shared definition of "how the model sees history", used by BOTH
        the offline OOS evaluation and the live/backtest strategy -- so the
        model the strategy trades with matches the model walk_forward() scores.
        No future row ever enters the fit
        because make_features_targets aligns each X[i] to past returns only.

        `peer_closes`, when the universe has peers (`cfg.peer_symbols`), maps
        peer symbol -> that peer's close series seen so far -- the caller (e.g.
        MLStrategy) is responsible only for gathering this dict; all
        cross-asset feature construction happens inside this engine.

        Returns True if a fit happened, False if there is not yet enough history
        to build a single feature row (caller should skip trading this bar).
        """
        close = np.asarray(close, float)
        X, y, idx = make_features_targets(
            close,
            self.cfg,
            self._regime_feats(close),
            self._cross_feats(close, peer_closes, timestamps),
            self._industry_feats(close, peer_closes, timestamps),
            self._news_fit(close, news_features),
        )
        if len(X) == 0:
            return False
        y_fit = self._residualize(y, idx, self._raw_full(close, news_features))
        X, y_fit = self._training_tail(X, y_fit)
        self._fit_arrays(X, y_fit)
        return True

    def _fit_arrays(self, X: np.ndarray, y: np.ndarray) -> None:
        if X.shape[1] == 0:
            # No fit-mode features at all (e.g. every block set to "raw" and
            # n_lags=0) -- there is nothing for Huber to learn; yhat comes
            # entirely from the raw contribution the caller adds afterwards
            # (predict_move/walk_forward). A degenerate zero-coefficient
            # "fit" keeps _predict_x uniform for every caller.
            self._state = _FitState(
                coef=np.zeros(0),
                intercept=0.0,
                feat_mean=np.zeros(0),
                feat_std=np.ones(0),
                fitted=True,
            )
            return
        if self.cfg.standardize:
            mean = X.mean(axis=0)
            std = X.std(axis=0)
            std[std == 0] = 1.0
            Xs = (X - mean) / std
        else:
            mean = np.zeros(X.shape[1])
            std = np.ones(X.shape[1])
            Xs = X
        # Huber loss: robust to outlier forward-returns (fat tails / gaps).
        model = HuberRegressor(
            epsilon=self.cfg.huber_epsilon,
            alpha=self.cfg.huber_alpha,
            max_iter=self.cfg.huber_max_iter,
        )
        model.fit(Xs, y)
        self._state = _FitState(
            coef=model.coef_.copy(),
            intercept=float(model.intercept_),
            feat_mean=mean,
            feat_std=std,
            fitted=True,
        )

    # ---- introspection ---------------------------------------------------
    def coef_intercept(self) -> tuple[np.ndarray, float]:
        """Return the currently-fitted Huber linear equation:

            yhat = coef . x_standardized + intercept

        `coef` is one weight per lagged-return feature (length n_lags), in the
        SAME standardized-feature space the model was trained on (see
        `_fit_arrays`: features are z-scored with train-set mean/std before
        fitting). Raises if the engine has not been fit yet.
        """
        s = self._state
        if not s.fitted:
            raise RuntimeError("PredictionEngine.coef_intercept called before fit().")
        return s.coef.copy(), s.intercept

    def current_diagnostics(self, close: np.ndarray) -> dict:
        """Return the latest no-lookahead regime/model state for telemetry."""
        closes = np.asarray(close, dtype=float)
        diagnostics = {
            "trained": bool(self._state.fitted),
            "regime_window": self.cfg.regime_window,
            "hmm_train_window": 0,
            "hmm_decode_window": 0,
            "regime_score": None,
            "state_label": "Unavailable",
            "p_bull": None,
            "p_bear": None,
            "p_side": None,
            "hmm_signed": None,
            "hmm_label": "Unavailable",
        }
        if self._regime is None or closes.size == 0:
            return diagnostics
        frame = self._regime.compute(closes)
        diagnostics.update(
            {
                "hmm_train_window": self._regime.cfg.hmm_train_window,
                "hmm_decode_window": self._regime.cfg.hmm_decode_window,
                "regime_score": float(frame.regime_score[-1]),
                "state_label": STATE_NAMES[int(frame.states[-1])],
                "p_bull": float(frame.p_bull[-1]),
                "p_bear": float(frame.p_bear[-1]),
                "p_side": float(frame.p_side[-1]),
                "hmm_signed": int(frame.hmm_signed[-1]),
                "hmm_label": STATE_NAMES[int(frame.hmm_state[-1])],
            }
        )
        return diagnostics

    # ---- inference ------------------------------------------------------
    def _predict_x(self, x: np.ndarray) -> float:
        s = self._state
        if not s.fitted:
            raise RuntimeError("PredictionEngine.predict called before fit().")
        if s.coef.shape[0] == 0:
            return float(s.intercept)
        xs = (x - s.feat_mean) / s.feat_std
        return float(np.dot(xs, s.coef) + s.intercept)

    def predict_move(
        self,
        recent_closes: np.ndarray,
        peer_closes: dict[str, object] | None = None,
        news_features: np.ndarray | None = None,
        *,
        timestamps: np.ndarray | None = None,
    ) -> float | None:
        """Live entry point. Returns yhat (forward return) or None if not ready.

        `recent_closes` is the trailing window the Strategy has accumulated.
        We use the most recent `n_lags+1` closes to form one feature row from
        PAST returns only -- nothing about the future is referenced. Same for
        `peer_closes` (maps peer symbol -> that peer's trailing window).
        """
        closes = np.asarray(recent_closes, dtype=float)
        if len(closes) < self.cfg.n_lags + 1 or len(closes) < self.cfg.min_train_bars:
            return None
        r = _log_returns(closes)
        # n_lags=0 disables the AR block entirely (see make_features_targets'
        # r[i-0:i] == empty slice) -- r[-0:] would otherwise return the WHOLE
        # array (Python's -0 == 0 slicing quirk), so guard it explicitly.
        x = r[-self.cfg.n_lags:] if self.cfg.n_lags > 0 else np.empty(0)
        # Append the CURRENT bar's regime feature row (regime_feats[-1]), which
        # uses only close[0..now] -- same columns, same order as training.
        rf = self._regime_feats(closes)
        if rf is not None:
            x = np.concatenate([x, rf[-1]])
        # Append the CURRENT bar's cross-asset row (cross_feats[-1]); every
        # column in it is lagged >= 1 (see make_cross_asset_features), so it
        # never references a peer's return for "now" -- only its past.
        cf = self._cross_feats(closes, peer_closes, timestamps)
        if cf is not None:
            x = np.concatenate([x, cf[-1]])
        industry = self._industry_feats(closes, peer_closes, timestamps)
        if industry is not None:
            x = np.concatenate([x, industry[-1]])
        nf = self._news_fit(closes, news_features)
        if nf is not None:
            x = np.concatenate([x, nf[-1]])
        raw = self._raw_full(closes, news_features)
        raw_contribution = float(raw[-1]) if raw is not None else 0.0
        return self._predict_x(x) + raw_contribution

    # ---- evaluation -----------------------------------------------------
    def walk_forward(
        self,
        close: np.ndarray,
        peer_closes: dict[str, object] | None = None,
        n_splits: int = 5,
        *,
        timestamps: np.ndarray | None = None,
        news_features: np.ndarray | None = None,
        return_folds: bool = False,
        return_series: bool = False,
    ) -> dict:
        """Walk-forward evaluation. Trains on the past, scores the future.

        ``training_window_bars=0`` uses expanding folds; a positive value uses
        that many latest labeled rows within each chronological training fold.

        Returns out-of-sample metrics. This is the honest performance estimate;
        an in-sample R^2 would be optimistic and is deliberately not reported.

        ``return_folds=True`` additionally reports each fold's OWN (non-
        cumulative) oos_r2/dir_acc/ic under the ``"folds"`` key, so callers can
        plot model performance as a series over time instead of one aggregate
        scalar (used by the dashboard's ML-performance panel).

        ``return_series=True`` additionally reports the full per-bar OOS
        predicted/actual forward-return series under ``"series"`` as
        ``{"idx": [...], "pred": [...], "actual": [...]}``, where ``idx[j]``
        is the row's position in the ORIGINAL `close` array (the bar the
        prediction was made FROM, not the bar it's predicting). Callers
        reconstruct an actual-vs-predicted PRICE overlay from this via
        ``close[idx[j] + horizon] * ...`` -- see run/artifacts.py. Both flags
        default False, keeping every existing caller's return shape unchanged.
        """
        close = np.asarray(close, float)
        # Regime/cross-asset columns are computed ONCE over the full series
        # here; each row is walk-forward (uses only its own past), so slicing
        # X[:train_end] below keeps the fold's fit free of any future leak.
        X, y_true, idx = make_features_targets(
            close,
            self.cfg,
            self._regime_feats(close),
            self._cross_feats(close, peer_closes, timestamps),
            self._industry_feats(close, peer_closes, timestamps),
            self._news_fit(close, news_features),
        )
        n = len(X)
        if n < self.cfg.min_train_bars + n_splits:
            raise ValueError("Not enough samples for the requested walk-forward.")

        # Raw (non-fit) blocks contribute directly to yhat and are excluded
        # from the Huber fit's target -- the fit only has to explain whatever
        # variance the raw signal(s) don't (see _residualize).
        raw_full = self._raw_full(close, news_features)
        raw_at_idx = raw_full[idx] if raw_full is not None else np.zeros(len(idx))
        y_fit = y_true - raw_at_idx

        fold = n // (n_splits + 1)
        preds, actuals, pred_idx = [], [], []
        fold_records = []
        for k in range(1, n_splits + 1):
            train_end = fold * k
            test_end = fold * (k + 1) if k < n_splits else n
            if train_end < self.cfg.min_train_bars:
                continue
            train_X, train_y = self._training_tail(
                X[:train_end], y_fit[:train_end]
            )
            self._fit_arrays(train_X, train_y)
            fold_preds, fold_actuals = [], []
            for i in range(train_end, test_end):
                p = self._predict_x(X[i]) + raw_at_idx[i]
                fold_preds.append(p)
                fold_actuals.append(y_true[i])
                pred_idx.append(int(idx[i]))
            preds.extend(fold_preds)
            actuals.extend(fold_actuals)
            if return_folds and fold_preds:
                fold_records.append(
                    _fold_metrics(k, train_end, test_end, fold_preds, fold_actuals)
                )

        preds = np.asarray(preds)
        actuals = np.asarray(actuals)
        if len(preds) == 0:
            empty = {"oos_samples": 0}
            if return_folds:
                empty["folds"] = []
            if return_series:
                empty["series"] = {"idx": [], "pred": [], "actual": []}
            return empty

        ss_res = float(((actuals - preds) ** 2).sum())
        ss_tot = float(((actuals - actuals.mean()) ** 2).sum()) or 1.0
        # Directional accuracy: did we get the sign right?
        dir_acc = float((np.sign(preds) == np.sign(actuals)).mean())
        # Information coefficient: corr(pred, actual).
        ic = float(np.corrcoef(preds, actuals)[0, 1]) if len(preds) > 1 else 0.0

        # Re-fit on all data so the engine is ready for live use afterwards.
        final_X, final_y = self._training_tail(X, y_fit)
        self._fit_arrays(final_X, final_y)
        result = {
            "oos_samples": int(len(preds)),
            "oos_r2": 1.0 - ss_res / ss_tot,
            "directional_accuracy": dir_acc,
            "information_coefficient": ic,
        }
        if return_folds:
            result["folds"] = fold_records
        if return_series:
            result["series"] = {
                "idx": pred_idx,
                "pred": [float(p) for p in preds],
                "actual": [float(a) for a in actuals],
            }
        return result


def _fold_metrics(fold_index, train_end, test_end, preds, actuals) -> dict:
    """Non-cumulative oos_r2/dir_acc/ic for a single walk-forward fold."""
    preds_arr = np.asarray(preds)
    actuals_arr = np.asarray(actuals)
    ss_res = float(((actuals_arr - preds_arr) ** 2).sum())
    ss_tot = float(((actuals_arr - actuals_arr.mean()) ** 2).sum()) or 1.0
    dir_acc = float((np.sign(preds_arr) == np.sign(actuals_arr)).mean())
    ic = float(np.corrcoef(preds_arr, actuals_arr)[0, 1]) if len(preds_arr) > 1 else 0.0
    return {
        "fold": fold_index,
        "train_end": int(train_end),
        "test_end": int(test_end),
        "n": int(len(preds_arr)),
        "oos_r2": 1.0 - ss_res / ss_tot,
        "directional_accuracy": dir_acc,
        "information_coefficient": ic,
    }


if __name__ == "__main__":
    # OOS walk-forward report on the ACTUAL data you generated/fetched.
    #
    # NOTE: this used to call generate() with its own fixed-seed series and
    # IGNORED your CSV -- so OOS metrics were byte-identical no matter what data
    # you regenerated. It now reads --csv (defaulting to the bars you produce),
    # so OOS metrics actually track the data.
    import argparse
    from pathlib import Path

    p = argparse.ArgumentParser(description="Walk-forward OOS metrics per ticker.")
    p.add_argument(
        "--csv",
        default="quant/data/sample_bars.csv",
        help="Bar CSV (timestamp,ticker,open,high,low,close,volume).",
    )
    p.add_argument("--ticker", default=None, help="Single ticker; default = all.")
    p.add_argument("--n-lags", type=int, default=5)
    p.add_argument("--horizon", type=int, default=1)
    p.add_argument("--splits", type=int, default=5)
    p.add_argument(
        "--peers",
        nargs="*",
        default=[],
        help="Other tickers in --csv to condition on via cross-asset ARDL/spread "
        "features (e.g. --peers ETH SOL). Requires --cross-lags and/or "
        "--spread-lags > 0 to actually add columns.",
    )
    p.add_argument(
        "--cross-lags",
        type=int,
        default=0,
        help="ARDL lag depth per peer (peer_log_return[i-1..i-cross_lags]).",
    )
    p.add_argument(
        "--spread-lags",
        type=int,
        default=0,
        help="Spread lag depth per peer ((own-peer)_log_return[i-1..i-spread_lags]).",
    )
    args = p.parse_args()

    cfg = PredictionConfig(
        n_lags=args.n_lags,
        horizon=args.horizon,
        cross_asset_lags=args.cross_lags,
        spread_lags=args.spread_lags,
        peer_symbols=tuple(args.peers),
    )

    if Path(args.csv).exists():
        df = pd.read_csv(args.csv)
        print(f"[data] {args.csv}  rows={len(df):,}")
    else:
        # Fallback: fresh RANDOM synthetic data (no fixed seed) so reruns differ.
        from quant.data.generate_sample_bars import generate

        df = generate(["BTC"], n_days=800, seed=None)
        print(f"[data] {args.csv} not found -> synthetic seed={df.attrs['seed']}")

    def _peer_closes_for(tk: str) -> dict[str, np.ndarray]:
        return {
            sym: df[df["ticker"] == sym].sort_values("timestamp")["close"].to_numpy()
            for sym in cfg.peer_symbols
            if sym != tk and sym in set(df["ticker"])
        }

    tickers = [args.ticker] if args.ticker else sorted(df["ticker"].unique())
    for tk in tickers:
        closes = (
            df[df["ticker"] == tk].sort_values("timestamp")["close"].to_numpy()
        )
        if len(closes) < cfg.min_train_bars + args.splits:
            print(f"{tk}: not enough bars ({len(closes)})")
            continue
        peer_closes = _peer_closes_for(tk)
        eng = PredictionEngine(cfg)
        m = eng.walk_forward(closes, peer_closes, n_splits=args.splits)
        if m.get("oos_samples", 0) == 0:
            print(f"{tk}: no OOS samples produced")
            continue
        print(
            f"{tk}: oos_r2={m['oos_r2']:+.4f}  "
            f"dir_acc={m['directional_accuracy']:.3f}  "
            f"ic={m['information_coefficient']:+.4f}  n={m['oos_samples']}"
        )
        print(f"{tk}: next yhat={eng.predict_move(closes[-200:], peer_closes)}")
        coef, intercept = eng.coef_intercept()
        lag_coef = coef[: cfg.n_lags]
        regime_coef = coef[cfg.n_lags: cfg.n_lags + cfg.n_regime_cols]
        cross_coef = coef[cfg.n_lags + cfg.n_regime_cols:]
        lag_terms = "  ".join(
            f"w{lag}={c:+.6f}" for lag, c in zip(range(cfg.n_lags, 0, -1), lag_coef)
        )
        print(f"{tk}: huber lag weights (standardized-feature space): {lag_terms}")
        if len(regime_coef):
            # Only fit-mode columns are ever in coef_ -- a "raw" column
            # bypasses the fit and never appears here (see n_regime_cols).
            names = []
            if cfg.use_regime_features and cfg.regime_source != "raw":
                names.append("regime_score")
            if cfg.use_hmm_feature and cfg.hmm_source != "raw":
                names.append("hmm_signed")
            regime_terms = "  ".join(
                f"{nm}={c:+.6f}" for nm, c in zip(names, regime_coef)
            )
            print(f"{tk}: huber regime weights: {regime_terms}")
        raw_bits = []
        if cfg.use_regime_features and cfg.regime_source == "raw":
            raw_bits.append(f"regime_score*{cfg.regime_raw_scale}")
        if cfg.use_hmm_feature and cfg.hmm_source == "raw":
            raw_bits.append(f"hmm_signed*{cfg.hmm_raw_scale}")
        if raw_bits:
            print(f"{tk}: raw (non-fit) yhat terms: {' + '.join(raw_bits)}")
        if len(cross_coef):
            names = []
            for sym in cfg.peer_symbols:
                names += [f"ardl_{sym}_lag{lag}" for lag in range(1, cfg.cross_asset_lags + 1)]
                names += [f"spread_{sym}_lag{lag}" for lag in range(1, cfg.spread_lags + 1)]
            cross_terms = "  ".join(
                f"{nm}={c:+.6f}" for nm, c in zip(names, cross_coef)
            )
            print(f"{tk}: huber cross-asset weights: {cross_terms}")
        print(f"{tk}: huber bias (intercept)={intercept:+.6f}")
