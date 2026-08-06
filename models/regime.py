"""Regime-detection features for the ML / alpha layer.

The base Huber-regression alpha is a short-horizon mean-reversion model: it maps
lagged returns to a forward-return forecast. That kind of model is structurally
weak to *regime changes* -- the coefficients it learns in a bull trend are the
wrong coefficients in a bear crash, and it has no explicit notion of "which
regime am I in right now". This module adds that missing context as two extra
features the regression can lean on:

  1. ``regime_score``  -- a walk-forward Markov transition-matrix feature. We
     label every day into Bull / Bear / Sideways from its trailing 20-day
     return, build a 3x3 transition-probability matrix from the state history
     seen *so far*, and expose ``P(next=Bull | today) - P(next=Bear | today)``.
     A value near +1 means "from the state we are in, history says tomorrow is
     usually Bull"; near -1 the opposite. This is the continuous feature the
     spec asks for.

     The trailing-return signal feeding the label is smoothed (``smoothing_
     window``) and the label itself uses hysteresis (``hysteresis_ratio``): once
     in Bull/Bear, a wider *exit* band must be crossed before leaving that
     state, and the transition matrix is recency-weighted (``transition_decay``)
     rather than an ever-growing cumulative count. Without this, a single noisy
     bar near the threshold flips the label (and the transition probabilities
     that depend on it) back and forth -- see "Stability" below.

  2. ``hmm_signed`` -- a Gaussian Hidden Markov Model latent-state feature. A
     ``GaussianHMM`` (hmmlearn) is fit on smoothed (rolling mean-return,
     rolling volatility) pairs -- not raw single-bar returns -- and decodes the
     current *latent* state from its posterior probabilities. Hidden states are
     sorted by their fitted mean-return dimension so the label is comparable
     across refits (-1 = bear-like, 0 = sideways-like, +1 = bull-like), and the
     emitted label comes from an EWMA-smoothed belief over those posteriors
     (``hmm_belief_decay``), not the raw per-bar arg-max. This is the
     independent "secondary feature for comparison" -- the HMM discovers
     regimes from the data instead of from a hand-drawn 20-day rule.

Stability
---------
Raw single-bar signals are noisy, especially on high-frequency (e.g. 4h) bars:
a threshold-crossing rule or an HMM's arg-max state can flip on a fraction of a
percent of price noise. Both features here are deliberately damped against
that: smoothed inputs, hysteresis on the rule-based label, and a posterior
probability floor on the HMM label. All of the smoothing is causal (uses
``close[0..t]`` / ``returns[0..t]`` only), so it damps noise without leaking
the future.

No-lookahead contract (identical in spirit to prediction_engine.py)
-------------------------------------------------------------------
Every value at index ``t`` is a function of ``close[0 .. t]`` ONLY:
  * The 20-day state label at ``t`` uses ``close[t-window .. t]``.
  * The transition matrix at ``t`` counts only transitions ``s[k]->s[k+1]`` for
    ``k < t`` (all observed by the close of bar ``t``), then reads the row for
    today's state. Tomorrow's realised move never enters the estimate.
  * The HMM at ``t`` is fit on ``returns[0 .. t]`` (expanding window, periodic
    refit) and decodes the state *as of* ``t``.
So slicing the output to ``[0 : train_end]`` -- which is exactly what
walk_forward() does -- can never leak a future row into a fitted coefficient.

Performance
-----------
``RegimeFeatureEngine`` is *stateful and incremental*. In the backtest the
strategy calls ``refit_on_history(list(closes))`` on every bar with an
append-only, ever-growing close series. Recomputing the whole walk-forward from
scratch each bar would be O(n^2) in labels and would refit the HMM thousands of
times. Instead the engine caches per-index results and, when handed a pure
extension of the series it already saw, only computes the new tail (refitting
the HMM at most once per ``hmm_refit_every`` new bars).
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass

import numpy as np
import pandas as pd

# hmmlearn is an optional heavy dependency. Import lazily so the transition
# matrix feature (pure numpy) still works if the HMM extra isn't installed.
try:  # pragma: no cover - import guard
    import logging as _logging

    from hmmlearn.hmm import GaussianHMM

    # hmmlearn's ConvergenceMonitor logs "Model is not converging" via the
    # logging module (not warnings). On a walk-forward refit loop this is
    # expected and noisy; quiet it so backtest logs stay readable.
    _logging.getLogger("hmmlearn").setLevel(_logging.ERROR)
    _HMM_AVAILABLE = True
except Exception:  # noqa: BLE001
    GaussianHMM = None  # type: ignore[assignment]
    _HMM_AVAILABLE = False


# State encoding shared across this module: index into the 3x3 matrix.
BEAR, SIDEWAYS, BULL = 0, 1, 2
STATE_NAMES = {BEAR: "Bear", SIDEWAYS: "Sideways", BULL: "Bull"}


@dataclass(frozen=True)
class RegimeConfig:
    """Knobs for regime-feature construction (all no-lookahead safe)."""

    window: int = 20              # rolling-return lookback for state labelling
    bull_threshold: float = 0.05  # 20-day return >= +2%  -> Bull
    bear_threshold: float = -0.05  # 20-day return <= -2% -> Bear (else Sideways)
    # --- rule-based label stability (fixes jumpiness / flickering) ---
    # The raw trailing-window return is smoothed over `smoothing_window` bars
    # before it's compared to the bull/bear thresholds, and the label uses a
    # Schmitt-trigger (hysteresis): once in Bull/Bear, the signal must decay
    # back past `threshold * hysteresis_ratio` (a narrower, easier-to-cross
    # "exit" band) before the state can change. Without this a return sitting
    # right at the threshold flips the label -- and regime_score -- every bar.
    smoothing_window: int = 5
    hysteresis_ratio: float = 0.6
    # Exponential decay applied to the transition-count matrix each bar so
    # regime_score tracks the TRAILING dynamics of the state history instead of
    # an ever-growing cumulative count that ossifies (stops moving) after
    # enough bars. ~1/(1-decay) bars of effective memory; 1.0 disables decay
    # (old unbounded-cumulative behaviour).
    transition_decay: float = 0.98
    # --- Gaussian HMM (secondary latent-state feature) ---
    use_hmm: bool = True
    hmm_states: int = 3           # bull / bear / sideways latent regimes
    hmm_refit_every: int = 20     # walk-forward refit cadence (bars)
    hmm_min_samples: int = 60     # need this many returns before the first fit
    hmm_n_iter: int = 100         # EM iterations per fit
    hmm_seed: int = 42            # fixed for reproducible latent labels
    # A component can collapse onto near-zero data during EM (see _fit_hmm),
    # leaving it a numerically-degenerate, effectively unreachable regime. A
    # fit is rejected if any state's average posterior responsibility over
    # the training window falls below this floor; `hmm_fit_retries`
    # deterministic alternate seeds are tried before giving up and keeping
    # the prior model (same fallback already used for a raised exception).
    hmm_min_state_weight: float = 0.001
    hmm_fit_retries: int = 4
    # Rolling window used to smooth (mean-return, volatility) inputs fed to the
    # HMM. Raw single-bar returns are noisy on high-frequency (e.g. 4h) bars
    # and make the decoded state toggle almost every bar; smoothing the inputs
    # gives the HMM a steadier signal to condition on.
    hmm_feature_smoothing: int = 6
    # Each bar's posterior over bear/sideways/bull ranks is folded into an
    # exponentially-weighted running "belief" (belief = decay*belief +
    # (1-decay)*posterior) and the EMITTED label is the belief's arg-max, not
    # the raw per-bar posterior's. This is a transition-smoothing layer: a
    # single noisy bar can nudge the belief without being able to flip the
    # label outright, which is what a raw per-bar arg-max does. ~1/(1-decay)
    # bars of effective memory; 0.0 disables smoothing (raw per-bar arg-max).
    hmm_belief_decay: float = 0.9
    # --- HMM cost bounds (keep the walk-forward decode LINEAR, not O(n^2)) ---
    # The backtest calls compute() once per bar on an ever-growing series. If we
    # fit and Viterbi-decode over the ENTIRE history [0..t] every bar, per-bar
    # cost is O(t) and the whole backtest is O(n^2) -- which made a 5,500-bar
    # (4-hour) series take ~34s PER TICKER and stalled Optuna. Bounding both the
    # fit and the decode to a trailing window makes per-bar cost O(window) (i.e.
    # linear overall). It stays strictly no-lookahead (only past/current returns
    # are used) and, for a 3-state HMM, the current-state estimate is dominated
    # by recent observations, so a few hundred bars of context is indistinguish-
    # able from the full history. <= 0 restores the old expanding-window cost.
    hmm_train_window: int = 750   # trailing returns used to FIT the HMM
    hmm_decode_window: int = 250  # trailing returns used to DECODE current state


def log_returns(close: np.ndarray) -> np.ndarray:
    """Log returns with a leading 0 so len(out) == len(close)."""
    close = np.asarray(close, dtype=float)
    out = np.zeros_like(close)
    if close.size > 1:
        out[1:] = np.diff(np.log(close))
    return out


def _trailing_return(close: np.ndarray, window: int) -> np.ndarray:
    """``window``-bar simple return, 0.0 before a full window is available.

    ``out[t] = close[t] / close[t-window] - 1`` uses only past/current prices.
    """
    close = np.asarray(close, dtype=float)
    n = close.size
    out = np.zeros(n, dtype=float)
    if n > window:
        out[window:] = close[window:] / close[:-window] - 1.0
    return out


def _smoothed_trailing_return(close: np.ndarray, cfg: RegimeConfig) -> np.ndarray:
    """Causal rolling mean of the trailing return -- damps single-bar noise."""
    trailing = _trailing_return(close, cfg.window)
    span = max(1, cfg.smoothing_window)
    return pd.Series(trailing).rolling(span, min_periods=1).mean().to_numpy()


def _hysteresis_states(
    smoothed: np.ndarray, cfg: RegimeConfig, start: int, prev_state: int
) -> np.ndarray:
    """Schmitt-trigger state assignment for ``smoothed[start:]``.

    Once ``prev`` is Bull/Bear, the signal must cross back past a narrower
    *exit* band (``threshold * hysteresis_ratio``) -- not just the original
    entry threshold -- before the state can leave Bull/Bear. This is what
    stops a return sitting right at the threshold from flipping the label
    every bar. Returns an array covering ``[start, len(smoothed))`` only.
    """
    enter_bull, enter_bear = cfg.bull_threshold, cfg.bear_threshold
    exit_bull = enter_bull * cfg.hysteresis_ratio
    exit_bear = enter_bear * cfg.hysteresis_ratio
    out = np.empty(smoothed.size - start, dtype=int)
    prev = prev_state
    for i, t in enumerate(range(start, smoothed.size)):
        s = smoothed[t]
        if prev == BULL:
            nxt = BULL if s >= exit_bull else (BEAR if s <= enter_bear else SIDEWAYS)
        elif prev == BEAR:
            nxt = BEAR if s <= exit_bear else (BULL if s >= enter_bull else SIDEWAYS)
        else:
            nxt = BULL if s >= enter_bull else (BEAR if s <= enter_bear else SIDEWAYS)
        out[i] = nxt
        prev = nxt
    return out


def label_states(close: np.ndarray, cfg: RegimeConfig) -> np.ndarray:
    """Label each bar Bull(2) / Sideways(1) / Bear(0) from its trailing return.

    The trailing ``window``-bar return is smoothed (``smoothing_window``) and
    passed through a hysteresis state machine (``hysteresis_ratio``) so a
    single noisy bar near the threshold doesn't flip the label -- see
    ``_hysteresis_states``. Bars before a full window is available start
    Sideways (neutral) and only move once the smoothed signal clears a
    threshold.
    """
    smoothed = _smoothed_trailing_return(close, cfg)
    return _hysteresis_states(smoothed, cfg, start=0, prev_state=SIDEWAYS)


@dataclass
class RegimeFrame:
    """Aligned, per-index regime feature columns (length == len(close))."""

    states: np.ndarray          # rule-based state label per bar (0/1/2)
    p_bull: np.ndarray          # P(next=Bull | today's state), walk-forward
    p_bear: np.ndarray          # P(next=Bear | today's state), walk-forward
    p_side: np.ndarray          # P(next=Sideways | today's state)
    regime_score: np.ndarray    # p_bull - p_bear  (the continuous ML feature)
    hmm_state: np.ndarray       # HMM latent state, mean-return-ordered (0/1/2)
    hmm_signed: np.ndarray      # hmm_state - 1  ({-1, 0, +1}); ML feature

    def feature_matrix(self, use_hmm: bool) -> np.ndarray:
        """Column-stack the features fed to the regression, aligned by index.

        Column order is fixed so the live path and the training path build the
        same vector: [regime_score] or [regime_score, hmm_signed].
        """
        cols = [self.regime_score]
        if use_hmm:
            cols.append(self.hmm_signed)
        return np.column_stack(cols)


class RegimeFeatureEngine:
    """Incremental, no-lookahead regime-feature builder with a length cache.

    Handing it an append-only close series (the backtest case) makes every
    ``compute`` call after the first an O(new-bars) extension. Handing it an
    unrelated series safely triggers a full recompute.
    """

    def __init__(self, cfg: RegimeConfig | None = None):
        self.cfg = cfg or RegimeConfig()
        self._reset(np.empty(0, dtype=float))

    # ---- cache management ----------------------------------------------
    def _reset(self, close: np.ndarray) -> None:
        self._closes = np.asarray(close, dtype=float)
        n = self._closes.size
        self._states = np.full(n, SIDEWAYS, dtype=int)
        self._p_bull = np.zeros(n)
        self._p_bear = np.zeros(n)
        self._p_side = np.zeros(n)
        self._score = np.zeros(n)
        self._hmm_state = np.full(n, SIDEWAYS, dtype=int)
        # 3x3 running transition counts among states seen strictly up to the
        # last processed index.
        self._counts = np.zeros((3, 3), dtype=float)
        self._processed = 0          # number of leading bars already finalised
        self._hmm_model = None
        self._hmm_order = None       # raw-state-id -> mean-return rank
        self._hmm_feat_mean = None   # fit-window feature mean (standardization)
        self._hmm_feat_std = None    # fit-window feature std (standardization)
        self._hmm_last_fit = -1      # index of the most recent HMM fit
        # EWMA belief over bear/sideways/bull ranks, carried across bars (see
        # `hmm_belief_decay`); starts neutral (uniform).
        self._hmm_belief = np.full(3, 1.0 / 3.0)

    def _is_extension_of_cache(self, close: np.ndarray) -> bool:
        m = self._closes.size
        if close.size < m:
            return False
        if m == 0:
            return True
        # Compare the overlapping prefix; np.array_equal is O(m) which is fine
        # because we already do O(n) work per compute.
        return np.array_equal(close[:m], self._closes)

    # ---- the walk-forward core -----------------------------------------
    def compute(self, close: np.ndarray) -> RegimeFrame:
        close = np.asarray(close, dtype=float)
        if not self._is_extension_of_cache(close):
            self._reset(close)
        else:
            self._closes = close  # adopt the longer array

        n = close.size
        self._grow_arrays(n)
        # Extend the smoothed/hysteresis rule-based label from where we left
        # off. Only the [processed, n) tail is computed sequentially (O(new
        # bars)); the smoothing itself is a cheap vectorised pass over the
        # full array, same cost profile as the previous full-recompute label.
        self._extend_states(close, n)

        # Extend the transition-count walk-forward from where we left off.
        self._extend_transitions(n)
        # Extend the HMM latent-state decode from where we left off.
        if self.cfg.use_hmm and _HMM_AVAILABLE:
            self._extend_hmm(close, n)

        self._processed = n
        hmm_signed = self._hmm_state.astype(float) - 1.0
        return RegimeFrame(
            states=self._states.copy(),
            p_bull=self._p_bull.copy(),
            p_bear=self._p_bear.copy(),
            p_side=self._p_side.copy(),
            regime_score=self._score.copy(),
            hmm_state=self._hmm_state.copy(),
            hmm_signed=hmm_signed,
        )

    def _grow_arrays(self, n: int) -> None:
        """Grow cached per-index arrays to length n, preserving processed head."""
        def _grow(a: np.ndarray, fill) -> np.ndarray:
            if a.size >= n:
                return a
            pad = np.full(n - a.size, fill, dtype=a.dtype)
            return np.concatenate([a, pad])

        self._states = _grow(self._states, SIDEWAYS)
        self._p_bull = _grow(self._p_bull, 0.0)
        self._p_bear = _grow(self._p_bear, 0.0)
        self._p_side = _grow(self._p_side, 0.0)
        self._score = _grow(self._score, 0.0)
        self._hmm_state = _grow(self._hmm_state, SIDEWAYS)

    def _extend_states(self, close: np.ndarray, n: int) -> None:
        """Extend the rule-based hysteresis label from ``self._processed`` to ``n``."""
        smoothed = _smoothed_trailing_return(close, self.cfg)
        start = self._processed
        prev = int(self._states[start - 1]) if start > 0 else SIDEWAYS
        self._states[start:n] = _hysteresis_states(smoothed, self.cfg, start, prev)

    def _row_probs(self, state: int) -> tuple[float, float, float]:
        """Row-normalised next-state probabilities for ``state``.

        A state with no observed outgoing transitions yet falls back to a
        uniform 1/3 each -> a neutral regime_score of 0.
        """
        row = self._counts[state]
        total = row.sum()
        if total <= 0:
            return 1 / 3, 1 / 3, 1 / 3
        return row[BULL] / total, row[BEAR] / total, row[SIDEWAYS] / total

    def _extend_transitions(self, n: int) -> None:
        states = self._states
        start = self._processed
        decay = self.cfg.transition_decay
        for t in range(start, n):
            # Fold the transition INTO today (s[t-1] -> s[t]) first: it is fully
            # observed by the close of bar t, so including it is not lookahead.
            if t >= 1:
                if 0.0 < decay < 1.0:
                    # Recency-weight the running counts so regime_score tracks
                    # trailing dynamics instead of an ever-hardening cumulative
                    # frequency table (bug: "static / linear probability drift").
                    self._counts *= decay
                self._counts[states[t - 1], states[t]] += 1.0
            p_bull, p_bear, p_side = self._row_probs(states[t])
            self._p_bull[t] = p_bull
            self._p_bear[t] = p_bear
            self._p_side[t] = p_side
            self._score[t] = p_bull - p_bear

    # ---- Gaussian HMM latent-state feature -----------------------------
    def _hmm_inputs(self, returns: np.ndarray) -> np.ndarray:
        """(rolling mean-return, rolling volatility) pairs fed to the HMM.

        Raw single-bar returns are noisy on high-frequency (e.g. 4h) bars and
        make the decoded state toggle almost every bar. Smoothing both the
        level and the dispersion over `hmm_feature_smoothing` bars gives the
        HMM a steadier signal, while staying causal (row t uses only
        returns[0..t]).
        """
        span = max(1, self.cfg.hmm_feature_smoothing)
        s = pd.Series(returns)
        mean_ret = s.rolling(span, min_periods=1).mean().to_numpy()
        vol = s.rolling(span, min_periods=1).std(ddof=0).fillna(0.0).to_numpy()
        return np.column_stack([mean_ret, vol])

    def _fit_hmm(self, features_upto_t: np.ndarray):
        """Fit a GaussianHMM on features[0..t]; return (model, order, mean, std)
        or None.

        ``order`` maps a raw hidden-state id -> mean-return rank (0=lowest mean
        ~ bear, hmm_states-1=highest ~ bull), so the emitted label is stable
        across refits despite hmmlearn's arbitrary internal state numbering.
        Ranking uses ONLY the mean-return column (0) so a state's volatility
        can't perturb the bear/bull ordering.

        Features are z-scored with TRAIN-WINDOW-ONLY mean/std before fitting
        (same convention as PredictionEngine._fit_arrays) -- ``mean``/``std``
        are returned so the caller standardizes decode-time rows with the
        SAME stats the model was fit on. Raw (mean-return, volatility) pairs
        sit several orders of magnitude below hmmlearn's default
        regularization constants (`covars_prior=0.01`), which biases EM
        toward the classic Gaussian-mixture singularity: a component
        collapses onto near-zero responsibility, and the M-step's covariance
        floor-division fallback (`covars_prior / 1e-5`, ~1000) leaves it
        permanently unable to win the posterior again -- silently starving
        whichever regime label that component happened to land on.
        Standardizing removes the scale mismatch and, empirically, is enough
        on its own; the average-responsibility check plus a few deterministic
        alternate-seed retries below is a cheap defensive fallback for the
        rare window standardizing alone doesn't fix (returning None, same as
        a raised exception -- the caller keeps the prior model).
        """
        mean = features_upto_t.mean(axis=0)
        std = features_upto_t.std(axis=0)
        std[std == 0] = 1.0
        Xs = (features_upto_t - mean) / std
        for attempt in range(self.cfg.hmm_fit_retries + 1):
            seed = (
                self.cfg.hmm_seed
                if attempt == 0
                else self.cfg.hmm_seed + attempt * 1000
            )
            model = GaussianHMM(
                n_components=self.cfg.hmm_states,
                covariance_type="diag",
                n_iter=self.cfg.hmm_n_iter,
                random_state=seed,
            )
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")  # silence non-convergence chatter
                model.fit(Xs)
                weights = model.predict_proba(Xs).mean(axis=0)
            if weights.min() >= self.cfg.hmm_min_state_weight:
                means = model.means_[:, 0]
                # rank[state_id] = position of that state's mean among all means.
                order = np.argsort(np.argsort(means))
                return model, order, mean, std
        return None  # every seed produced a degenerate (dead-state) fit

    def _extend_hmm(self, close: np.ndarray, n: int) -> None:
        returns = log_returns(close)
        features = self._hmm_inputs(returns)
        start = self._processed
        # Trailing-window bounds keep per-bar cost O(window), not O(t): fitting
        # and decoding over the FULL history every bar is what made this O(n^2).
        # <= 0 means "unbounded" (original expanding-window behaviour).
        train_w = self.cfg.hmm_train_window
        decode_w = self.cfg.hmm_decode_window
        belief_decay = self.cfg.hmm_belief_decay
        n_states = self.cfg.hmm_states
        for t in range(start, n):
            have = t + 1  # samples available through bar t
            prev_rank = int(self._hmm_state[t - 1]) if t > 0 else SIDEWAYS
            if have < max(self.cfg.hmm_min_samples, n_states + 1):
                self._hmm_state[t] = SIDEWAYS
                continue
            need_refit = (
                self._hmm_model is None
                or (t - self._hmm_last_fit) >= self.cfg.hmm_refit_every
            )
            if need_refit:
                # Fit on the trailing `train_w` rows ending at t (past only).
                fit_lo = max(0, have - train_w) if train_w and train_w > 0 else 0
                try:
                    fitted = self._fit_hmm(features[fit_lo: t + 1])
                except Exception:  # noqa: BLE001 - keep prior model on failure
                    fitted = None
                # Advance on any ATTEMPT (healthy fit, degenerate fit, or a
                # raised exception) so a run of bad fits waits for the next
                # scheduled refit cycle instead of re-paying the (multi-seed)
                # fit cost every single bar.
                self._hmm_last_fit = t
                if fitted is not None:
                    self._hmm_model, self._hmm_order, self._hmm_feat_mean, self._hmm_feat_std = fitted
            if self._hmm_model is None:
                self._hmm_state[t] = SIDEWAYS
                continue
            try:
                # Decode only the trailing `decode_w` rows ending at t: we only
                # need the CURRENT (last) state's posterior, which for a
                # 3-state HMM is governed by recent observations, so a bounded
                # context matches full-history inference while making the
                # decode O(1) per bar. Standardize with the FIT-window's
                # mean/std (not this window's) -- the model's parameter space
                # is defined relative to those stats.
                dec_lo = max(0, have - decode_w) if decode_w and decode_w > 0 else 0
                dec_x = (features[dec_lo: t + 1] - self._hmm_feat_mean) / self._hmm_feat_std
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    proba = self._hmm_model.predict_proba(dec_x)[-1]
                # Fold each raw hidden-state's posterior mass onto its
                # mean-return rank (bear/sideways/bull), then blend it into the
                # running EWMA belief rather than emitting the raw per-bar
                # arg-max directly -- a transition-smoothing layer that damps
                # single-bar flicker on noisy, high-frequency bars while still
                # tracking genuine, sustained regime shifts.
                rank_proba = np.zeros(n_states)
                for raw_id, rank in enumerate(self._hmm_order):
                    rank_proba[rank] += proba[raw_id]
                if belief_decay > 0.0:
                    belief = belief_decay * self._hmm_belief + (1.0 - belief_decay) * rank_proba
                    self._hmm_belief = belief / belief.sum()
                else:
                    self._hmm_belief = rank_proba
                self._hmm_state[t] = int(np.argmax(self._hmm_belief))
            except Exception:  # noqa: BLE001
                self._hmm_state[t] = prev_rank


# --------------------------------------------------------------------------- #
# Standalone DataFrame output (the deliverable the spec asks for)
# --------------------------------------------------------------------------- #
def build_regime_frame(
    close: np.ndarray,
    cfg: RegimeConfig | None = None,
    timestamps: np.ndarray | None = None,
) -> pd.DataFrame:
    """Return a DataFrame of regime features aligned with the price series.

    Columns:
        timestamp (optional), close,
        state, state_label,                      # rule-based 20-day label
        p_bull, p_bear, p_side,                  # transition-matrix row probs
        regime_score,                            # p_bull - p_bear (ML feature)
        hmm_state, hmm_label, hmm_signed         # GaussianHMM latent state
    Every row uses only data up to and including that row (walk-forward).
    """
    cfg = cfg or RegimeConfig()
    frame = RegimeFeatureEngine(cfg).compute(np.asarray(close, dtype=float))
    data = {
        "close": np.asarray(close, dtype=float),
        "state": frame.states,
        "state_label": [STATE_NAMES[s] for s in frame.states],
        "p_bull": frame.p_bull,
        "p_bear": frame.p_bear,
        "p_side": frame.p_side,
        "regime_score": frame.regime_score,
        "hmm_state": frame.hmm_state,
        "hmm_label": [STATE_NAMES[s] for s in frame.hmm_state],
        "hmm_signed": frame.hmm_signed,
    }
    df = pd.DataFrame(data)
    if timestamps is not None:
        df.insert(0, "timestamp", np.asarray(timestamps))
    return df


if __name__ == "__main__":
    # Emit the regime-feature DataFrame for a ticker in a bar CSV. This is the
    # standalone "Output" deliverable: Transition-matrix probabilities and HMM
    # latent states as columns aligned with the original price series.
    import argparse
    from pathlib import Path

    p = argparse.ArgumentParser(
        description="Walk-forward regime features (transition matrix + GaussianHMM)."
    )
    p.add_argument("--csv", default="quant/data/sample_bars.csv")
    p.add_argument("--ticker", default=None, help="Single ticker; default = first.")
    p.add_argument("--window", type=int, default=20)
    p.add_argument("--bull", type=float, default=0.02)
    p.add_argument("--bear", type=float, default=-0.02)
    p.add_argument("--no-hmm", action="store_true", help="Skip the GaussianHMM feature.")
    p.add_argument("--out", default=None, help="Optional CSV path to write the frame.")
    args = p.parse_args()

    if not Path(args.csv).exists():
        raise SystemExit(f"CSV not found: {args.csv}")
    src = pd.read_csv(args.csv)
    tk = args.ticker or sorted(src["ticker"].unique())[0]
    sub = src[src["ticker"] == tk].sort_values("timestamp")
    if sub.empty:
        raise SystemExit(f"No rows for ticker {tk} in {args.csv}")

    cfg = RegimeConfig(
        window=args.window,
        bull_threshold=args.bull,
        bear_threshold=args.bear,
        use_hmm=not args.no_hmm,
    )
    out = build_regime_frame(
        sub["close"].to_numpy(),
        cfg,
        timestamps=sub["timestamp"].to_numpy(),
    )
    if not _HMM_AVAILABLE and not args.no_hmm:
        print("[warn] hmmlearn not installed -> HMM columns are neutral. "
              "pip install hmmlearn")

    hmm_on = cfg.use_hmm and _HMM_AVAILABLE
    print(f"[{tk}] {len(out):,} rows | HMM={'on' if hmm_on else 'off'}")
    print("state distribution:", out["state_label"].value_counts().to_dict())
    if hmm_on:
        print("hmm distribution:  ", out["hmm_label"].value_counts().to_dict())
    with pd.option_context("display.max_columns", None, "display.width", 160):
        print(out.tail(12).to_string(index=False))

    if args.out:
        out.to_csv(args.out, index=False)
        print(f"Wrote {len(out):,} rows -> {args.out}")
