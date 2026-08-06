"""Hyperparameter optimization with Optuna -- replaces the old LLM loop.

Why Optuna, not an LLM: hyperparameter search is a numerical optimization
problem. Optuna's TPE sampler is reproducible (fixed seed), efficient, and
auditable. An LLM proposing floats is slow, non-deterministic, and gives no
convergence guarantees. (If you later want an LLM, use it for *higher-level*
reasoning -- e.g. choosing which features to add -- not the numeric search.)

Anti-overfitting discipline:
  * Time-ordered split: optimize on the IN-SAMPLE window only, then the README
    shows you re-running the chosen params on the held-out OUT-OF-SAMPLE window.
  * Objective = Sortino-like score net of IBKR-Pro fees, with a small penalty
    for excessive turnover so the optimizer can't win by overtrading. Sortino
    (not Sharpe) charges only DOWNSIDE volatility, so a strategy is not punished
    for upside dispersion -- see ``_sortino_from_curve``.
  * Unpromising trials are pruned early: each trial streams the backtest in
    time-slices and reports an intermediate Sortino to Optuna's MedianPruner,
    which abandons parameter sets already lagging the median before they burn
    the remaining bars -- see ``make_objective`` / the pruner in ``main``.

Reproducibility:
  * By DEFAULT the Optuna sampler now draws a FRESH random seed each run, so
    re-running genuinely re-explores the space. Pass ``--seed N`` to reproduce
    a specific search exactly. The seed used is always printed.

Usage:
    python -m quant.optimize.optimize --csv quant/data/sample_bars.csv \
        --trials 40 --train-frac 0.7
    python -m quant.optimize.optimize --seed 42   # reproducible search
    # Goal mode: search until a trial's score hits a target, then stop.
    python -m quant.optimize.optimize --score 1.5             # uncapped
    python -m quant.optimize.optimize --score 1.5 --trials 200  # with safety cap
"""
from __future__ import annotations

import argparse
import json
import secrets

import time

import numpy as np
import optuna
import pandas as pd

from quant.run.artifacts import save_optimize_artifact
from quant.run.backtest_common import ASSET_CLASSES, build_and_run, build_engine, VENUE

DEFAULT_TICKERS = ["BTC", "ETH", "SOL", "XRP", "DOGE"]  # crypto (24/7)
EQUITY_DEFAULT_TICKERS = ["SPY", "QQQ", "DIA", "IWM"]  # liquid index ETFs

# Number of intermediate checkpoints per trial (report + should_prune). More
# checkpoints = earlier pruning but more equity-report calls; 6 gives ~monthly-
# to-quarterly granularity on multi-year daily data. Trials with fewer bars than
# this simply use one checkpoint per bar-slice.
N_PRUNE_CHECKPOINTS = 6


def _split_csv(csv_path: str, train_frac: float) -> tuple[str, str]:
    """Write in-sample / out-of-sample CSVs split by time. Returns paths."""
    df = pd.read_csv(csv_path)
    # A CSV can contain date-only rows from the original daily file and
    # date-time rows added by ``--fetch-missing``.  Pandas otherwise infers the
    # first format and rejects the other representation.
    df["timestamp"] = pd.to_datetime(
        df["timestamp"], format="mixed", utc=True
    )
    cutoff = df["timestamp"].quantile(train_frac)
    is_df = df[df["timestamp"] <= cutoff]
    oos_df = df[df["timestamp"] > cutoff]
    is_path = csv_path.replace(".csv", "_is.csv")
    oos_path = csv_path.replace(".csv", "_oos.csv")
    is_df.to_csv(is_path, index=False)
    oos_df.to_csv(oos_path, index=False)
    return is_path, oos_path


SECONDS_PER_YEAR = 365.25 * 24 * 3600  # wall-clock; session gaps ~cancel in the ratio

def _equity_series(engine):
    """Pull (values, ts_seconds) for the account-equity series.

    Returns equity values AND their UTC timestamps (seconds) so the Sharpe can
    be annualized from REAL elapsed time instead of a hard-coded bar grid.
    Returns (np.array([]), np.array([])) when unavailable.
    """
    try:
        report = engine.trader.generate_account_report(VENUE)
    except Exception:  # noqa: BLE001
        report = None
    if report is None or len(report) == 0 or "total" not in report.columns:
        return np.array([]), np.array([])

    total = pd.to_numeric(report["total"], errors="coerce")
    if "ts_event" in report.columns:
        ts = pd.to_numeric(report["ts_event"], errors="coerce") / 1e9
    elif isinstance(report.index, pd.DatetimeIndex):
        ts = pd.Series(report.index.view("int64") / 1e9, index=report.index)
    else:
        ts = pd.Series(np.full(len(total), np.nan), index=total.index)

    mask = total.notna()
    return total[mask].to_numpy(), ts[mask].to_numpy(dtype=float)

def _equity_curve(engine) -> np.ndarray:
    """Back-compat wrapper: equity values only."""
    vals, _ = _equity_series(engine)
    return vals if len(vals) > 2 else np.array([])

def _annualization_factor(ts_seconds: np.ndarray) -> float:
    """periods-per-year from the MEDIAN spacing of equity timestamps.

    Frequency-robust (1-min / 1-hour / daily), resists fill-burst clustering,
    falls back to 252 (daily) when timestamps are unusable. Replaces the
    hard-coded sqrt(252) that over-annualized intraday bars (~20x for 1-min).
    """
    if ts_seconds.size < 2:
        return 252.0
    dt = np.diff(ts_seconds)
    dt = dt[dt > 0]
    if dt.size == 0:
        return 252.0
    mean_dt = float(np.median(dt))
    if mean_dt <= 0:
        return 252.0
    return SECONDS_PER_YEAR / mean_dt

DOWNSIDE_EPS = 1e-6  # clamp for sigma_down on an all-winners (no-loss) curve


def _sortino_from_curve(curve: np.ndarray, ts: np.ndarray) -> float:
    """Annualized Sortino-like score net of a turnover penalty.

    Sortino replaces Sharpe's total-return standard deviation with DOWNSIDE
    deviation ``sigma_down`` -- the root-mean-square of the negative return
    steps only (``r_t < 0``), measured against a 0 minimum-acceptable-return.
    Upside dispersion no longer penalizes the score: a strategy is charged only
    for the losses it actually incurs.

    Annualization reuses the same dynamic ``_annualization_factor(ts)`` derived
    from real elapsed timestamps (frequency-robust), NOT a hard-coded 252/bar
    grid. ``curve`` must have length >= 3 (the caller guarantees this).
    """
    rets = np.diff(curve) / curve[:-1]
    downside = rets[rets < 0]
    # sigma_down = sqrt(mean(r^2)) over the LOSING steps only (MAR = 0).
    sigma_down = float(np.sqrt(np.mean(np.square(downside)))) if downside.size else 0.0
    if sigma_down == 0.0:
        # No losing steps at all. If the mean return is positive this is a
        # (small-sample) all-winners curve -> clamp to a tiny epsilon so the
        # ratio stays finite and large instead of dividing by zero. Otherwise
        # (flat or negative mean) there is no positive edge to reward.
        if rets.mean() > 0:
            sigma_down = DOWNSIDE_EPS
        else:
            return 0.0
    periods_per_year = _annualization_factor(ts)
    sortino = (rets.mean() / sigma_down) * np.sqrt(periods_per_year)
    turnover_penalty = 0.0005 * len(rets)  # discourage hyperactivity
    return float(sortino - turnover_penalty)


def score_engine(engine, starting_cash: float) -> float:
    """Sortino-like objective net of fees, with a turnover penalty.

    Downside-deviation based (see ``_sortino_from_curve``); annualization is
    derived from the ACTUAL elapsed time of the equity curve (was hard-coded
    sqrt(252)). Returns a large negative sentinel when the trial placed no
    trades so the optimizer strongly avoids do-nothing parameter sets.
    """
    curve, ts = _equity_series(engine)
    if len(curve) < 3:
        try:
            fills = engine.trader.generate_order_fills_report()
            n_trades = 0 if fills is None else len(fills)
        except Exception:  # noqa: BLE001
            n_trades = 0
        return -1e6 if n_trades == 0 else 0.0
    return _sortino_from_curve(curve, ts)

def _prompt_refit_every_n_bars() -> int:
    """Interactively read the Huber refit cadence (in bars) from the terminal.

    This is a FIXED structural setting, deliberately NOT an Optuna-tuned
    parameter (see the CRITICAL GUARDRAIL below): refitting the Huber model
    every bar dominates backtest cost, so a larger cadence (e.g. 12 or 24) makes
    each of the many Optuna trials dramatically faster. The chosen value is
    bound identically into every trial and into the OOS validation.

    Robust parsing: any non-integer, non-positive, or unavailable input (e.g. a
    piped/non-interactive run raising EOFError) falls back safely to 1 (refit
    every bar), which reproduces the original behaviour.
    """
    prompt = "Enter the Huber regression refitting period in bars (e.g., 1, 12, 24): "
    try:
        raw = input(prompt)
    except EOFError:
        print("No input received; defaulting refit period to 1 (refit every bar).")
        return 1
    try:
        value = int(raw.strip())
    except (ValueError, TypeError):
        print(f"Invalid refit period {raw!r}; defaulting to 1 (refit every bar).")
        return 1
    if value < 1:
        print(f"Refit period must be >= 1 (got {value}); defaulting to 1.")
        return 1
    return value


def make_objective(
    csv_path: str,
    tickers: list[str],
    starting_cash: float,
    refit_every_n_bars: int = 1,
    asset_class: str = "crypto",
    warmup_bars: int = 150,
    min_train_bars: int = 120,
    structural_overrides: dict | None = None,
):
    def objective(trial: optuna.Trial) -> float:
        overrides = dict(
            n_lags=trial.suggest_int("n_lags", 3, 15),
            horizon=trial.suggest_int("horizon", 1, 5),
            entry_threshold=trial.suggest_float(
                "entry_threshold", 1e-4, 5e-3, log=True
            ),
            atr_period=trial.suggest_int("atr_period", 7, 21),
            atr_stop_mult=trial.suggest_float("atr_stop_mult", 1.0, 4.0),
            use_limit_orders=trial.suggest_categorical(
                "use_limit_orders", [True, False]
            ),
            # Maker passive offset: fill-probability vs price-improvement knob.
            # Was fixed at 2 bps; now searched (only used when use_limit_orders).
            limit_offset_bps=trial.suggest_float(
                "limit_offset_bps", 0.5, 10.0, log=True
            ),
            # --- fractional-Kelly conviction sizing ---
            # use_kelly_sizing gates it; kelly_fraction is the "percent of full
            # Kelly" the user asked to optimize. kelly_fraction is always
            # suggested (ignored when sizing is off) so the search space stays
            # reproducible across trials.
            use_kelly_sizing=trial.suggest_categorical(
                "use_kelly_sizing", [True, False]
            ),
            kelly_fraction=trial.suggest_float("kelly_fraction", 0.05, 1.0),
            # --- portfolio concentration ---
            # Max concurrent positions across the ticker universe. Upper bound is
            # the number of tickers (== unlimited relative to this run).
            max_open_positions=trial.suggest_int(
                "max_open_positions", 1, max(1, len(tickers))
            ),
            # --- cross-asset ARDL + spread features (models/prediction_engine.py) ---
            # Lag depths only; the peer UNIVERSE (cross_asset_symbols) is
            # structural, not searched -- it defaults to "every other ticker in
            # this run" (see MLStrategy.on_start). 0 disables a block, so the
            # search space naturally includes "cross-asset features off".
            cross_asset_lags=trial.suggest_int("cross_asset_lags", 0, 5),
            spread_lags=trial.suggest_int("spread_lags", 0, 5),
            # --- Huber L2 penalty (models/prediction_engine.py) ---
            # Was fixed at PredictionConfig's 1e-4 default for every trial. Log-
            # uniform range spans from that default up to a strongly-regularized
            # ridge-like fit, so Optuna can counteract whatever feature-count-vs-
            # training-window ill-conditioning the OTHER tuned knobs (n_lags,
            # cross_asset_lags, spread_lags) produce for a given run instead of
            # always fighting it with one fixed, possibly-too-weak value.
            huber_alpha=trial.suggest_float("huber_alpha", 1e-4, 10.0, log=True),
            # --- Huber loss transition point (models/prediction_engine.py) ---
            # Was fixed at PredictionConfig's own default (1.35, sklearn's
            # ~95%-OLS-efficiency default) for every trial. Searched range
            # stays comfortably above sklearn's epsilon > 1.0 requirement:
            # near 1.05 the loss is almost fully linear (max robustness to
            # fat-tailed crypto returns, least efficient on well-behaved
            # residuals); near 3.0 it is nearly OLS (max efficiency, least
            # robust to outliers/gaps). Linear, not log, scale -- the whole
            # range spans less than one order of magnitude.
            huber_epsilon=trial.suggest_float("huber_epsilon", 1.05, 3.0),
            # --- refit cadence (FIXED structural setting, NOT tuned) ---
            # Statically bound from the interactive terminal input; identical for
            # every trial. Deliberately NOT a trial.suggest_* call -- Optuna must
            # never search this. It only controls how often the Huber model is
            # refit (a speed knob), so leaving it out of the search space keeps
            # trials comparable and the optimum meaningful.
            refit_every_n_bars=refit_every_n_bars,
            # --- warmup / min-train bar counts (FIXED, NOT tuned) ---
            # Bound from --warmup-bars / --min-train-bars (default: the
            # MLStrategyConfig/PredictionConfig built-in defaults, 150/120).
            # These are counted in BARS, not calendar time, so the right value
            # depends on your bar cadence: 150 bars is ~5 months of daily data
            # but ~3 years of WEEKLY data. If every trial scores the -1e6
            # "zero trades" sentinel, check whether warmup_bars/min_train_bars
            # exceed your in-sample bar count per ticker before assuming the
            # search itself is broken.
            warmup_bars=warmup_bars,
            min_train_bars=min_train_bars,
        )
        # Feature toggles + risk-rail overrides (dashboard-controlled, NOT
        # searched by Optuna) applied identically to every trial -- same
        # precedent as refit_every_n_bars/warmup_bars/min_train_bars above.
        if structural_overrides:
            overrides.update(structural_overrides)
        # Build the engine but hold the data back so we can feed it in
        # time-slices and inspect the equity curve between slices.
        engine, all_bars = build_engine(
            csv_path=csv_path,
            tickers=tickers,
            strategy_overrides=overrides,
            starting_cash=starting_cash,
            log_level="ERROR",
            bypass_logging=True,
            asset_class=asset_class,
        )
        try:
            n = len(all_bars)
            if n == 0:
                return score_engine(engine, starting_cash)

            # Stream the bars in contiguous, time-ordered slices. After each
            # slice, score the equity curve SO FAR and hand it to Optuna. The
            # MedianPruner (configured in main) raises should_prune() once a
            # trial trails the median of prior trials at the same checkpoint --
            # so a losing / do-nothing parameter set is abandoned before it
            # burns the remaining bars. This is the compute saving.
            n_chunks = min(N_PRUNE_CHECKPOINTS, n)
            for step in range(n_chunks):
                lo = step * n // n_chunks
                hi = (step + 1) * n // n_chunks
                engine.add_data(all_bars[lo:hi])
                engine.run(streaming=True)  # pause at data exhaustion, don't finalize
                engine.clear_data()

                curve, ts = _equity_series(engine)
                if len(curve) < 3:
                    # Warmup: the model has not traded enough for a meaningful
                    # equity curve yet. Reporting a placeholder here would poison
                    # the MedianPruner -- for a MAXIMIZE study it compares the
                    # trial's BEST-so-far intermediate value against the median,
                    # and a 0.0 placeholder outranks every real (often negative)
                    # early Sortino, so nothing would ever prune. Skip instead;
                    # warmup length is deterministic so steps stay aligned across
                    # trials.
                    continue
                interim = _sortino_from_curve(curve, ts)
                trial.report(interim, step)
                if trial.should_prune():
                    raise optuna.TrialPruned()

            engine.end()  # finalize the streamed run (produces final results)
            return score_engine(engine, starting_cash)
        finally:
            engine.dispose()

    return objective


def _make_target_callback(target_score: float):
    """Optuna callback that stops the study once the best COMPLETED trial's
    value reaches ``target_score``.

    ``study.stop()`` requests a graceful stop after the current trial, so the
    search halts as soon as the goal is met instead of exhausting a trial count.
    ``study.best_value`` raises until at least one trial has completed (all early
    ones may be pruned), so that case is swallowed.
    """
    def callback(study: optuna.Study, trial: optuna.trial.FrozenTrial) -> None:
        try:
            best = study.best_value
        except ValueError:
            return  # no completed trial yet
        if best >= target_score:
            print(f"\nTarget score {target_score} reached (best={best:.4f}); stopping.")
            study.stop()

    return callback


def main(refit_every_n_bars: int = 1) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--csv", default="quant/data/sample_bars.csv")
    p.add_argument(
        "--asset-class",
        choices=ASSET_CLASSES,
        default="crypto",
        help="Instrument type + fee model for every ticker in this run "
        "(one class per run; see backtest_common.py).",
    )
    p.add_argument(
        "--tickers",
        nargs="*",
        default=None,
        help="Defaults to a crypto or equity ticker list depending on "
        "--asset-class if omitted.",
    )
    p.add_argument(
        "--trials",
        type=int,
        default=None,
        help="Number of trials to run. Omit to use the default of 40. When "
        "--score is set, this instead acts as a SAFETY CAP (max trials); omit "
        "it there to search with no cap until the score is reached.",
    )
    p.add_argument(
        "--score",
        type=float,
        default=None,
        help="Goal mode: keep running trials until a COMPLETED trial's "
        "score_engine value reaches this target, then stop. Combine with "
        "--trials to bound the worst case; without it the search is uncapped "
        "(Ctrl-C to abort).",
    )
    p.add_argument("--train-frac", type=float, default=0.7)
    p.add_argument("--cash", type=float, default=5000.0)
    p.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Fixed Optuna sampler seed. Omit for a FRESH random search.",
    )
    p.add_argument(
        "--out-params",
        default="quant/optimize/best_params.json",
        help="Where to save the best params so run_backtest can load them.",
    )
    p.add_argument(
        "--run-id",
        default=None,
        help="Explicit run artifact id (e.g. supplied by the dashboard API's "
        "job runner). Defaults to an auto-generated opt_<timestamp>_<hex> id.",
    )
    p.add_argument(
        "--warmup-bars",
        type=int,
        default=None,
        help="Override MLStrategyConfig.warmup_bars (default: 150). Counted in "
        "BARS, not calendar time -- lower this for low-bar-count data (e.g. "
        "weekly bars), where 150 bars can exceed your whole in-sample window. "
        "Every trial scoring the -1e6 'zero trades' sentinel is the symptom.",
    )
    p.add_argument(
        "--min-train-bars",
        type=int,
        default=None,
        help="Override PredictionConfig.min_train_bars (default: 120). Same "
        "bar-count-vs-calendar-time caveat as --warmup-bars.",
    )
    p.add_argument(
        "--fetch-missing",
        action="store_true",
        help="Before running, fetch any --tickers missing from --csv via a live "
        "IBKR TWS/Gateway connection and merge them in (existing tickers' rows "
        "are left untouched). Requires TWS/Gateway running -- see "
        "data/ibkr_fetch.py; unverified against a live TWS in this environment.",
    )
    p.add_argument(
        "--replace-bars",
        action="store_true",
        help="Fetch the requested universe at --ibkr-bar-hours and replace the "
        "CSV completely. Mutually exclusive with --fetch-missing.",
    )
    p.add_argument("--ibkr-host", default="127.0.0.1")
    p.add_argument("--ibkr-port", type=int, default=7497)
    p.add_argument("--ibkr-client-id", type=int, default=1)
    p.add_argument("--ibkr-years", type=int, default=5)
    p.add_argument(
        "--ibkr-bar-hours",
        type=int,
        default=4,
        help="Target bar width in hours for --replace-bars (default 4). "
        "--fetch-missing always uses the CSV's existing frequency.",
    )
    p.add_argument(
        "--structural-json",
        default=None,
        help="Path to a JSON file of feature-toggle / risk-rail overrides "
        "(use_regime_features, use_hmm_feature, regime_source, hmm_source, "
        "regime_raw_scale, hmm_raw_scale, risk_budget_pct, max_trade_risk_pct, "
        "max_leverage, daily_loss_limit_pct, kill_switch_pct, kill_warn_pct, "
        "kelly_max_fraction) applied identically to every trial and the final "
        "OOS validation -- NOT searched by Optuna, dashboard-controlled.",
    )
    p.add_argument(
        "--resume-run-id",
        default=None,
        help="Continue a prior sweep instead of starting fresh: reattaches to "
        "that run's Optuna study (by run_id) via persistent storage and runs "
        "--trials MORE trials on top of what it already completed, with the "
        "TPE sampler conditioning on the full combined history. The new run "
        "gets its own --run-id/artifact; the original run's artifact is left "
        "untouched. Use the SAME --tickers/--asset-class/--csv/structural "
        "settings as the original run -- changing the search space mid-study "
        "mixes incompatible trials into one 'best' comparison.",
    )
    p.add_argument(
        "--storage",
        default="sqlite:///quant/optimize/studies.db",
        help="Optuna persistent storage URL. Every sweep's study is saved "
        "here under a study_name (its own run_id, or --resume-run-id's when "
        "resuming) so a later run can pick it back up with --resume-run-id.",
    )
    args = p.parse_args()
    if args.fetch_missing and args.replace_bars:
        p.error("--fetch-missing and --replace-bars are mutually exclusive")
    tickers = args.tickers or (
        EQUITY_DEFAULT_TICKERS if args.asset_class == "equity" else DEFAULT_TICKERS
    )
    warmup_bars = args.warmup_bars if args.warmup_bars is not None else 150
    min_train_bars = args.min_train_bars if args.min_train_bars is not None else 120

    if args.fetch_missing or args.replace_bars:
        if args.replace_bars:
            from quant.data.ibkr_fetch import replace_bars
            replace_bars(
                csv_path=args.csv, tickers=tickers, asset_class=args.asset_class,
                years=args.ibkr_years, host=args.ibkr_host, port=args.ibkr_port,
                client_id=args.ibkr_client_id, bar_hours=args.ibkr_bar_hours,
            )
        else:
            from quant.data.ibkr_fetch import ensure_tickers
            fetched = ensure_tickers(
                csv_path=args.csv, tickers=tickers, asset_class=args.asset_class,
                years=args.ibkr_years, host=args.ibkr_host, port=args.ibkr_port,
                client_id=args.ibkr_client_id,
            )
            if fetched:
                print(f"Fetched missing tickers via IBKR at the CSV's existing frequency: {fetched}")

    structural_overrides = None
    if args.structural_json:
        with open(args.structural_json) as fh:
            structural_overrides = json.load(fh)
        print(f"Structural feature/risk overrides from {args.structural_json}: {structural_overrides}")

    started_at = time.time()
    seed = args.seed if args.seed is not None else secrets.randbelow(2**31)
    print(f"Optuna sampler seed = {seed}")
    print(f"Huber refit cadence = every {refit_every_n_bars} bar(s) (fixed, not tuned)")
    print(f"warmup_bars = {warmup_bars}, min_train_bars = {min_train_bars} (fixed, not tuned)")

    is_path, oos_path = _split_csv(args.csv, args.train_frac)
    print(f"In-sample:  {is_path}\nOut-of-sample: {oos_path}")

    sampler = optuna.samplers.TPESampler(seed=seed)
    # MedianPruner: after n_startup_trials have fully completed (seeding the
    # per-step medians) and past n_warmup_steps checkpoints within a trial,
    # prune any trial whose intermediate Sortino trails the running median at
    # the same step. Startup/warmup keep it from killing trials on noise before
    # there is anything to compare against.
    pruner = optuna.pruners.MedianPruner(n_startup_trials=5, n_warmup_steps=1)
    # study_name defaults to this run's own --run-id so ANY run can later be
    # resumed via --resume-run-id; resuming reattaches to the OLD run's name
    # instead, so the new run's trials land in that same persistent study.
    study_name = args.resume_run_id or args.run_id or f"opt_{int(started_at)}_{secrets.token_hex(4)}"
    study = optuna.create_study(
        study_name=study_name,
        storage=args.storage,
        load_if_exists=True,
        direction="maximize",
        sampler=sampler,
        pruner=pruner,
    )
    if args.resume_run_id:
        print(f"Resuming study {study_name!r}: {len(study.trials)} trial(s) already "
              f"recorded; adding more on top.")
    # Run mode: fixed trial count, or "goal mode" that runs until a completed
    # trial reaches --score (with --trials as an optional safety cap).
    callbacks = []
    if args.score is not None:
        callbacks.append(_make_target_callback(args.score))
        n_trials = args.trials  # None -> uncapped; else a safety maximum
        cap = f"max {n_trials} trials" if n_trials is not None else "no trial cap"
        print(f"Goal mode: running until score >= {args.score} ({cap}).")
    else:
        n_trials = args.trials if args.trials is not None else 40
        print(f"Running {n_trials} trials.")

    study.optimize(
        make_objective(
            is_path, tickers, args.cash, refit_every_n_bars, args.asset_class,
            warmup_bars, min_train_bars, structural_overrides,
        ),
        n_trials=n_trials,
        callbacks=callbacks,
        # A determinate progress bar needs a known trial count; skip it when the
        # goal-mode search is uncapped (n_trials is None).
        show_progress_bar=n_trials is not None,
    )

    n_pruned = len(study.get_trials(states=(optuna.trial.TrialState.PRUNED,)))
    n_complete = len(study.get_trials(states=(optuna.trial.TrialState.COMPLETE,)))
    print(f"\nTrials: {n_complete} complete, {n_pruned} pruned early "
          f"({n_complete + n_pruned} run)")
    if args.score is not None:
        reached = n_complete > 0 and study.best_value >= args.score
        best_txt = f"{study.best_value:.4f}" if n_complete > 0 else "n/a"
        print(f"Target score {args.score}: "
              f"{'REACHED' if reached else 'NOT reached'} (best = {best_txt})")

    print("\n=========== BEST (in-sample) ===========")
    print("value:", study.best_value)
    print("params:", study.best_params)

    # Persist the winning params so run_backtest / live can load them directly.
    payload = {
        "params": study.best_params,
        "in_sample_value": study.best_value,
        "seed": seed,
        "trials": args.trials,
        "train_frac": args.train_frac,
        "source_csv": args.csv,
        # Recorded OUTSIDE "params" (which holds only Optuna-tuned keys) because
        # these are fixed structural settings, not searched hyperparameters.
        "refit_every_n_bars": refit_every_n_bars,
        "warmup_bars": warmup_bars,
        "min_train_bars": min_train_bars,
        **(structural_overrides or {}),
    }
    if args.replace_bars:
        # Record the replacement cadence. Missing-ticker fetches inherit the
        # existing CSV cadence and do not claim a new one here.
        # run_live.py reads this to pick the same live bar width for paper/
        # live trading instead of assuming daily bars.
        payload["ibkr_bar_hours"] = args.ibkr_bar_hours
    with open(args.out_params, "w") as fh:
        json.dump(payload, fh, indent=2)
    print(f"\nSaved best params -> {args.out_params}")

    # Validate the chosen params on the held-out OOS window.
    print("\n=========== VALIDATION (out-of-sample) ===========")
    engine = build_and_run(
        csv_path=oos_path,
        tickers=tickers,
        # Merge the FIXED structural settings onto the tuned params so OOS
        # validation runs the same configuration the in-sample trials used.
        strategy_overrides={
            **study.best_params,
            "refit_every_n_bars": refit_every_n_bars,
            "warmup_bars": warmup_bars,
            "min_train_bars": min_train_bars,
            **(structural_overrides or {}),
        },
        starting_cash=args.cash,
        log_level="ERROR",
        bypass_logging=True,
        asset_class=args.asset_class,
    )
    oos_score = score_engine(engine, args.cash)
    print("oos score:", oos_score)

    # Persist a run artifact (trial history, OOS equity curve, ML-performance)
    # for the reporting dashboard. Best-effort: a failure here must never mask
    # the search result already printed and saved to --out-params above.
    try:
        artifact = save_optimize_artifact(
            study=study,
            oos_engine=engine,
            oos_score=oos_score,
            csv_path=args.csv,
            oos_path=oos_path,
            tickers=tickers,
            asset_class=args.asset_class,
            starting_cash=args.cash,
            seed=seed,
            n_trials_requested=args.trials,
            train_frac=args.train_frac,
            target_score=args.score,
            started_at=started_at,
            run_id=args.run_id,
            structural_overrides=structural_overrides,
            resumed_from=args.resume_run_id,
            ibkr_bar_hours=args.ibkr_bar_hours if args.replace_bars else None,
        )
        print(f"Saved run artifact -> quant/runs/{artifact['run_id']}.json")
    except Exception as e:  # noqa: BLE001
        print(f"(run artifact unavailable: {e})")

    engine.dispose()


if __name__ == "__main__":
    # Interactive prompt at the very start of execution: the refit cadence is a
    # fixed structural setting supplied from the terminal, NOT an Optuna-tuned
    # parameter (see _prompt_refit_every_n_bars / the GUARDRAIL in make_objective).
    _refit_every_n_bars = _prompt_refit_every_n_bars()
    main(refit_every_n_bars=_refit_every_n_bars)
