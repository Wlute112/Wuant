"""Purged, nested walk-forward hyperparameter optimization with Optuna.

Why Optuna, not an LLM: hyperparameter search is a numerical optimization
problem. Optuna's TPE sampler is reproducible (fixed seed), efficient, and
auditable. An LLM proposing floats is slow, non-deterministic, and gives no
convergence guarantees. (If you later want an LLM, use it for *higher-level*
reasoning -- e.g. choosing which features to add -- not the numeric search.)

Anti-overfitting discipline:
  * The newest 20% is an outer test set which Optuna never sees.
  * Every trial is evaluated over chronological expanding walk-forward folds.
  * At least ``horizon`` bars are embargoed between each fit and validation.
  * The model warms on fold history without trading, freezes before the
    embargo, and submits orders only in the validation interval.
  * Each fold is rerun with 2x commissions and slippage assumptions.
  * Selection rewards median net risk-adjusted performance and penalizes fold
    dispersion, turnover, and sensitivity to stressed costs. A trial must be
    positive in most folds to qualify.
  * The winning parameters are evaluated on the outer test once, after search.

Reproducibility:
  * By DEFAULT the Optuna sampler now draws a FRESH random seed each run, so
    re-running genuinely re-explores the space. Pass ``--seed N`` to reproduce
    a specific search exactly. The seed used is always printed.

Usage:
    python -m quant.optimize.optimize --csv quant/data/sample_bars.csv \
        --trials 40 --final-test-frac 0.2 --walk-forward-folds 5
    python -m quant.optimize.optimize --seed 42   # reproducible search
    # Goal mode: search until a trial's score hits a target, then stop.
    python -m quant.optimize.optimize --score 1.5             # uncapped
    python -m quant.optimize.optimize --score 1.5 --trials 200  # with safety cap
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import secrets
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import optuna
import pandas as pd

from quant.run.artifacts import save_optimize_artifact
from quant.run.asset_profiles import get_asset_profile, strategy_defaults_for_asset
from quant.run.backtest_common import (
    ASSET_CLASSES,
    VENUE,
    build_and_run,
    infer_bars_per_session,
    infer_bar_interval_minutes_from_csv,
)
from quant.run.scoring import (
    annualization_factor,
    primary_ratio_from_curve,
    sharpe_from_curve,
    sortino_from_curve,
)
from quant.run.metrics import compute_metrics

DEFAULT_TICKERS = ["BTC", "ETH", "SOL", "XRP", "DOGE"]  # crypto (24/7)
EQUITY_DEFAULT_TICKERS = ["SPY", "QQQ", "DIA", "IWM"]  # liquid index ETFs

NO_QUALIFYING_FOLDS_SCORE = -1_000_000.0


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class WalkForwardFold:
    number: int
    csv_path: str
    validation_start_ns: int
    validation_end_ns: int
    validation_start_index: int
    timestamps_ns: tuple[int, ...]

    def model_fit_end_ns(self, embargo_bars: int) -> int:
        fit_index = self.validation_start_index - embargo_bars - 1
        if fit_index < 0:
            raise ValueError(
                f"fold {self.number} has no training data before {embargo_bars}-bar embargo"
            )
        return self.timestamps_ns[fit_index]


@dataclass(frozen=True)
class NestedWalkForwardData:
    folds: tuple[WalkForwardFold, ...]
    final_context_path: str
    final_test_path: str
    final_test_start_ns: int
    final_test_end_ns: int
    final_test_start_index: int
    timestamps_ns: tuple[int, ...]
    development_fraction: float

    def final_model_fit_end_ns(self, embargo_bars: int) -> int:
        fit_index = self.final_test_start_index - embargo_bars - 1
        if fit_index < 0:
            raise ValueError("no development data before the final-test embargo")
        return self.timestamps_ns[fit_index]


def _split_csv(csv_path: str, train_frac: float) -> tuple[str, str]:
    """Backward-compatible one-shot splitter; the optimizer no longer uses it."""
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


def _prepare_nested_walk_forward(
    csv_path: str,
    tickers: list[str],
    output_dir: str,
    *,
    final_test_frac: float,
    n_folds: int,
    min_initial_train_bars: int,
    max_embargo_bars: int,
) -> NestedWalkForwardData:
    """Create reusable chronological fold CSVs and an untouched outer holdout."""
    if not 0.0 < final_test_frac < 0.5:
        raise ValueError("final_test_frac must be greater than 0 and less than 0.5")
    if not 2 <= n_folds <= 10:
        raise ValueError("walk-forward folds must be between 2 and 10")
    if max_embargo_bars < 1:
        raise ValueError("max_embargo_bars must be >= 1")

    df = pd.read_csv(csv_path)
    required = {"timestamp", "ticker", "open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"CSV missing required columns: {sorted(missing)}")
    df["timestamp"] = pd.to_datetime(df["timestamp"], format="mixed", utc=True)
    df = df[df["ticker"].isin(tickers)].sort_values(["timestamp", "ticker"])
    if df.empty:
        raise ValueError("CSV has no rows for the selected tickers")

    timestamps = pd.Index(df["timestamp"].drop_duplicates().sort_values())
    n_timestamps = len(timestamps)
    final_count = max(1, int(math.ceil(n_timestamps * final_test_frac)))
    development_count = n_timestamps - final_count
    usable_for_validation = (
        development_count - min_initial_train_bars - max_embargo_bars
    )
    validation_size = usable_for_validation // n_folds
    if validation_size < 2:
        required_count = (
            min_initial_train_bars + max_embargo_bars + 2 * n_folds + final_count
        )
        raise ValueError(
            "not enough chronological bars for purged walk-forward validation: "
            f"found {n_timestamps}, need at least {required_count} for "
            f"{n_folds} folds, {min_initial_train_bars} initial train bars, "
            f"{max_embargo_bars} embargo bars, and the final holdout"
        )

    validation_origin = development_count - validation_size * n_folds
    timestamps_ns = tuple(int(ts.value) for ts in timestamps)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    folds = []
    for number in range(1, n_folds + 1):
        val_start_index = validation_origin + (number - 1) * validation_size
        val_end_index = (
            development_count - 1
            if number == n_folds
            else val_start_index + validation_size - 1
        )
        val_start = timestamps[val_start_index]
        val_end = timestamps[val_end_index]
        fold_path = output / f"fold_{number}.csv"
        df[df["timestamp"] <= val_end].to_csv(fold_path, index=False)
        folds.append(
            WalkForwardFold(
                number=number,
                csv_path=str(fold_path),
                validation_start_ns=int(val_start.value),
                validation_end_ns=int(val_end.value),
                validation_start_index=val_start_index,
                timestamps_ns=timestamps_ns,
            )
        )

    final_start = timestamps[development_count]
    final_end = timestamps[-1]
    final_context_path = output / "final_context.csv"
    final_test_path = output / "final_test.csv"
    df.to_csv(final_context_path, index=False)
    df[df["timestamp"] >= final_start].to_csv(final_test_path, index=False)
    return NestedWalkForwardData(
        folds=tuple(folds),
        final_context_path=str(final_context_path),
        final_test_path=str(final_test_path),
        final_test_start_ns=int(final_start.value),
        final_test_end_ns=int(final_end.value),
        final_test_start_index=development_count,
        timestamps_ns=timestamps_ns,
        development_fraction=development_count / n_timestamps,
    )


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

def _annualization_factor(ts_seconds: np.ndarray, asset_class: str = "crypto") -> float:
    return annualization_factor(ts_seconds, asset_class)


def _sortino_from_curve(
    curve: np.ndarray, ts: np.ndarray, asset_class: str = "crypto"
) -> float:
    return sortino_from_curve(curve, ts, asset_class)


def _sharpe_from_curve(
    curve: np.ndarray, ts: np.ndarray, asset_class: str = "equity"
) -> float:
    return sharpe_from_curve(curve, ts, asset_class)


def score_engine(
    engine,
    starting_cash: float,
    asset_class: str = "crypto",
) -> float:
    """Asset-profile objective net of modeled fees and light activity cost."""
    curve, ts = _equity_series(engine)
    try:
        fills = engine.trader.generate_order_fills_report()
        n_trades = 0 if fills is None else len(fills)
    except Exception:  # noqa: BLE001
        n_trades = 0
    if len(curve) < 3:
        return -1e6 if n_trades == 0 else 0.0
    _metric, ratio = primary_ratio_from_curve(curve, ts, asset_class)
    penalty = float(get_asset_profile(asset_class)["scoring"]["trade_penalty"])
    return float(ratio - penalty * n_trades)


@dataclass(frozen=True)
class FoldPerformance:
    ratio: float
    turnover: float
    trades: int


def _engine_performance(
    engine,
    starting_cash: float,
    asset_class: str,
) -> FoldPerformance:
    """Net risk-adjusted performance and turnover from one validation run."""
    curve, ts = _equity_series(engine)
    metrics = compute_metrics(engine, VENUE, starting_cash, asset_class)
    if len(curve) < 3:
        ratio = NO_QUALIFYING_FOLDS_SCORE if metrics.total_trades == 0 else 0.0
    else:
        _metric, ratio = primary_ratio_from_curve(curve, ts, asset_class)
    return FoldPerformance(
        ratio=float(ratio),
        turnover=float(metrics.turnover_rate),
        trades=int(metrics.total_trades),
    )


def stability_aware_score(
    normal_ratios: list[float],
    stressed_ratios: list[float],
    turnovers: list[float],
    *,
    std_weight: float = 0.5,
    turnover_weight: float = 0.01,
    cost_sensitivity_weight: float = 0.5,
    min_positive_fraction: float = 0.6,
    require_positive_folds: bool = True,
) -> float:
    """Robust fold aggregation used as the Optuna objective."""
    if not normal_ratios or len(normal_ratios) != len(stressed_ratios):
        return NO_QUALIFYING_FOLDS_SCORE
    normal = np.asarray(normal_ratios, dtype=float)
    stressed = np.asarray(stressed_ratios, dtype=float)
    turnover = np.asarray(turnovers, dtype=float)
    if not np.all(np.isfinite(normal)) or not np.all(np.isfinite(stressed)):
        return NO_QUALIFYING_FOLDS_SCORE

    required_positive = max(1, math.ceil(len(normal) * min_positive_fraction))
    if require_positive_folds and int(np.sum(normal > 0.0)) < required_positive:
        return NO_QUALIFYING_FOLDS_SCORE

    stability = float(np.median(normal) - std_weight * np.std(normal))
    turnover_penalty = turnover_weight * float(np.median(turnover))
    degradation = np.maximum(0.0, normal - stressed)
    cost_penalty = cost_sensitivity_weight * float(np.median(degradation))
    return stability - turnover_penalty - cost_penalty

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
    folds: tuple[WalkForwardFold, ...],
    tickers: list[str],
    starting_cash: float,
    refit_every_n_bars: int = 1,
    asset_class: str = "crypto",
    warmup_bars: int = 150,
    min_train_bars: int = 120,
    structural_overrides: dict | None = None,
    *,
    embargo_bars: int = 0,
    std_weight: float = 0.5,
    turnover_weight: float = 0.01,
    cost_sensitivity_weight: float = 0.5,
    min_positive_fraction: float = 0.6,
    stress_cost_multiplier: float = 2.0,
    normal_slippage_probability: float = 0.05,
    seed: int = 0,
):
    window_floor = max(warmup_bars, min_train_bars)
    training_windows = tuple(
        sorted({0, window_floor, window_floor * 2, window_floor * 4, window_floor * 8})
    )

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
            # Expanding history (0) competes directly against several rolling
            # Huber fit windows. The same candidate is evaluated across every
            # purged fold, so a short recent-regime fit must generalize rather
            # than merely improving one period.
            training_window_bars=trial.suggest_categorical(
                "training_window_bars", training_windows
            ),
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

        horizon_embargo = max(int(overrides["horizon"]), int(embargo_bars))
        normal_results: list[FoldPerformance] = []
        stressed_results: list[FoldPerformance] = []
        fold_records = []
        for step, fold in enumerate(folds):
            fold_overrides = {
                **overrides,
                "backtest_model_fit_end_ns": fold.model_fit_end_ns(horizon_embargo),
                "backtest_trade_start_ns": fold.validation_start_ns,
            }
            fill_seed = seed + trial.number * 10_000 + fold.number
            normal_engine = build_and_run(
                csv_path=fold.csv_path,
                tickers=tickers,
                strategy_overrides=fold_overrides,
                starting_cash=starting_cash,
                log_level="ERROR",
                bypass_logging=True,
                asset_class=asset_class,
                cost_multiplier=1.0,
                slippage_probability=normal_slippage_probability,
                fill_model_seed=fill_seed,
            )
            try:
                normal = _engine_performance(
                    normal_engine, starting_cash, asset_class
                )
            finally:
                normal_engine.dispose()

            stressed_engine = build_and_run(
                csv_path=fold.csv_path,
                tickers=tickers,
                strategy_overrides=fold_overrides,
                starting_cash=starting_cash,
                log_level="ERROR",
                bypass_logging=True,
                asset_class=asset_class,
                cost_multiplier=stress_cost_multiplier,
                slippage_probability=min(1.0, normal_slippage_probability * 2.0),
                fill_model_seed=fill_seed,
            )
            try:
                stressed = _engine_performance(
                    stressed_engine, starting_cash, asset_class
                )
            finally:
                stressed_engine.dispose()

            normal_results.append(normal)
            stressed_results.append(stressed)
            fold_records.append(
                {
                    "fold": fold.number,
                    "validation_start": pd.Timestamp(
                        fold.validation_start_ns, unit="ns", tz="UTC"
                    ).isoformat(),
                    "validation_end": pd.Timestamp(
                        fold.validation_end_ns, unit="ns", tz="UTC"
                    ).isoformat(),
                    "embargo_bars": horizon_embargo,
                    "normal_ratio": normal.ratio,
                    "stressed_ratio": stressed.ratio,
                    "turnover": normal.turnover,
                    "trades": normal.trades,
                }
            )
            interim = stability_aware_score(
                [result.ratio for result in normal_results],
                [result.ratio for result in stressed_results],
                [result.turnover for result in normal_results],
                std_weight=std_weight,
                turnover_weight=turnover_weight,
                cost_sensitivity_weight=cost_sensitivity_weight,
                min_positive_fraction=min_positive_fraction,
                require_positive_folds=False,
            )
            trial.report(interim, step)
            if trial.should_prune():
                trial.set_user_attr("walk_forward_folds", fold_records)
                raise optuna.TrialPruned()

        final_score = stability_aware_score(
            [result.ratio for result in normal_results],
            [result.ratio for result in stressed_results],
            [result.turnover for result in normal_results],
            std_weight=std_weight,
            turnover_weight=turnover_weight,
            cost_sensitivity_weight=cost_sensitivity_weight,
            min_positive_fraction=min_positive_fraction,
        )
        trial.set_user_attr("walk_forward_folds", fold_records)
        trial.set_user_attr(
            "positive_folds", sum(result.ratio > 0 for result in normal_results)
        )
        trial.set_user_attr("fold_count", len(normal_results))
        trial.set_user_attr("median_fold_ratio", float(np.median(
            [result.ratio for result in normal_results]
        )))
        trial.set_user_attr("std_fold_ratio", float(np.std(
            [result.ratio for result in normal_results]
        )))
        return final_score

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
        "stability-aware walk-forward value reaches this target, then stop. Combine with "
        "--trials to bound the worst case; without it the search is uncapped "
        "(Ctrl-C to abort).",
    )
    p.add_argument(
        "--final-test-frac",
        type=float,
        default=0.20,
        help="Newest fraction reserved as the untouched outer test set (default 0.20).",
    )
    p.add_argument(
        "--train-frac",
        type=float,
        default=None,
        help=argparse.SUPPRESS,
    )
    p.add_argument(
        "--walk-forward-folds",
        type=int,
        default=5,
        help="Chronological purged validation folds inside the development set (default 5).",
    )
    p.add_argument(
        "--embargo-bars",
        type=int,
        default=0,
        help="Minimum train/validation embargo. Effective embargo is max(this, horizon).",
    )
    p.add_argument(
        "--stability-std-weight",
        type=float,
        default=0.50,
        help="Penalty multiplier for dispersion across fold ratios (default 0.50).",
    )
    p.add_argument(
        "--turnover-penalty-weight",
        type=float,
        default=0.01,
        help="Penalty per median fold turnover multiple (default 0.01).",
    )
    p.add_argument(
        "--cost-sensitivity-weight",
        type=float,
        default=0.50,
        help="Penalty multiplier for degradation under stressed costs (default 0.50).",
    )
    p.add_argument(
        "--min-positive-fold-fraction",
        type=float,
        default=0.60,
        help="Required fraction of folds with positive net primary ratio (default 0.60).",
    )
    p.add_argument(
        "--normal-slippage-probability",
        type=float,
        default=0.05,
        help="Normal probability of one-tick fill slippage; stress uses 2x (default 0.05).",
    )
    p.add_argument(
        "--stress-cost-multiplier",
        type=float,
        default=2.0,
        help="Commission multiplier for stressed fold/test reruns (default 2.0).",
    )
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
        "--include-extended-hours",
        action="store_true",
        help="For equity IBKR fetches, request all available sessions instead of RTH only.",
    )
    p.add_argument(
        "--structural-json",
        default=None,
        help="Path to a JSON file of feature-toggle / risk-rail overrides "
        "(use_regime_features, use_hmm_feature, regime_source, hmm_source, "
        "regime_raw_scale, hmm_raw_scale, risk_budget_pct, max_trade_risk_pct, "
        "max_leverage, daily_loss_limit_pct, kill_switch_pct, kill_warn_pct, "
        "kelly_max_fraction) applied identically to every trial and the final "
        "outer test -- NOT searched by Optuna, dashboard-controlled.",
    )
    p.add_argument(
        "--news-db",
        default=None,
        help="Captured RSS/IBKR news database. A content-addressed snapshot is "
        "used unchanged by every fold, cost rerun, and final holdout.",
    )
    p.add_argument(
        "--resume-run-id",
        default=None,
        help="Continue an INTERRUPTED nested sweep before its final test was "
        "evaluated. Data and validation settings must match exactly. A sweep "
        "which already consumed its outer holdout is intentionally immutable; "
        "start a new study instead of tuning further against a seen test set.",
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
    if args.train_frac is not None:
        if not 0.5 < args.train_frac < 1.0:
            p.error("--train-frac must be between 0.5 and 1.0")
        args.final_test_frac = 1.0 - args.train_frac
        print(
            "--train-frac is deprecated; interpreting it as "
            f"--final-test-frac {args.final_test_frac:.4f}"
        )
    if not 0.0 < args.final_test_frac < 0.5:
        p.error("--final-test-frac must be greater than 0 and less than 0.5")
    if not 2 <= args.walk_forward_folds <= 10:
        p.error("--walk-forward-folds must be between 2 and 10")
    if args.embargo_bars < 0:
        p.error("--embargo-bars must be >= 0")
    if not 0.0 < args.min_positive_fold_fraction <= 1.0:
        p.error("--min-positive-fold-fraction must be in (0, 1]")
    if not 0.0 <= args.normal_slippage_probability <= 0.5:
        p.error("--normal-slippage-probability must be between 0 and 0.5")
    if args.stress_cost_multiplier < 1.0:
        p.error("--stress-cost-multiplier must be >= 1")
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
                include_extended_hours=args.include_extended_hours,
            )
        else:
            from quant.data.ibkr_fetch import ensure_tickers
            fetched = ensure_tickers(
                csv_path=args.csv, tickers=tickers, asset_class=args.asset_class,
                years=args.ibkr_years, host=args.ibkr_host, port=args.ibkr_port,
                client_id=args.ibkr_client_id,
                include_extended_hours=args.include_extended_hours,
            )
            if fetched:
                print(f"Fetched missing tickers via IBKR at the CSV's existing frequency: {fetched}")

    # Asset profile defaults are structural and therefore identical across
    # every trial. Explicit JSON values win over the profile.
    structural_overrides = strategy_defaults_for_asset(args.asset_class)
    structural_overrides["regime_window"] = 20 * infer_bars_per_session(
        args.csv, tickers
    )
    if args.structural_json:
        with open(args.structural_json) as fh:
            structural_overrides.update(json.load(fh))
        print(f"Structural feature/risk overrides from {args.structural_json}: {structural_overrides}")
    if structural_overrides.get("regime_window") == 20:
        structural_overrides["regime_window"] = 20 * infer_bars_per_session(
            args.csv, tickers
        )

    started_at = time.time()
    seed = args.seed if args.seed is not None else secrets.randbelow(2**31)
    print(f"Optuna sampler seed = {seed}")
    score_profile = get_asset_profile(args.asset_class)["scoring"]
    print(
        f"Scoring profile = {score_profile['label']} "
        f"({args.asset_class}, primary objective)"
    )
    print(f"Huber refit cadence = every {refit_every_n_bars} bar(s) (fixed, not tuned)")
    print(f"warmup_bars = {warmup_bars}, min_train_bars = {min_train_bars} (fixed, not tuned)")

    split_workspace = tempfile.TemporaryDirectory(prefix="quant_nested_wf_")
    news_source = args.news_db or structural_overrides.get("news_data_path")
    news_snapshot_sha256 = None
    if news_source:
        from quant.news.core import snapshot_news_store

        news_snapshot, news_snapshot_sha256 = snapshot_news_store(
            str(news_source), "quant/optimize/news_snapshots"
        )
        structural_overrides["use_news_features"] = True
        structural_overrides["news_data_path"] = news_snapshot
    nested_data = _prepare_nested_walk_forward(
        args.csv,
        tickers,
        split_workspace.name,
        final_test_frac=args.final_test_frac,
        n_folds=args.walk_forward_folds,
        min_initial_train_bars=max(warmup_bars, min_train_bars),
        max_embargo_bars=max(5, args.embargo_bars),
    )
    print(
        f"Nested validation: {len(nested_data.folds)} purged walk-forward folds; "
        f"newest {args.final_test_frac:.1%} reserved for one final test"
    )

    sampler = optuna.samplers.TPESampler(seed=seed)
    # MedianPruner: after n_startup_trials have fully completed (seeding the
    # per-step medians) and past n_warmup_steps checkpoints within a trial,
    # prune any trial whose intermediate profile ratio trails the running median at
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
    validation_contract = {
        "scheme": "purged_nested_walk_forward_v1",
        "source_csv_sha256": _file_sha256(args.csv),
        "tickers": tickers,
        "asset_class": args.asset_class,
        "final_test_frac": args.final_test_frac,
        "walk_forward_folds": args.walk_forward_folds,
        "embargo_bars": args.embargo_bars,
        "stability_std_weight": args.stability_std_weight,
        "turnover_penalty_weight": args.turnover_penalty_weight,
        "cost_sensitivity_weight": args.cost_sensitivity_weight,
        "min_positive_fold_fraction": args.min_positive_fold_fraction,
        "normal_slippage_probability": args.normal_slippage_probability,
        "stress_cost_multiplier": args.stress_cost_multiplier,
        "warmup_bars": warmup_bars,
        "min_train_bars": min_train_bars,
        "refit_every_n_bars": refit_every_n_bars,
        "structural_overrides": structural_overrides,
        "news_snapshot_sha256": news_snapshot_sha256,
    }
    prior_contract = study.user_attrs.get("validation_contract")
    if study.trials and prior_contract is None:
        raise ValueError(
            f"study {study_name!r} predates purged nested validation and cannot be resumed"
        )
    if prior_contract is not None and prior_contract != validation_contract:
        raise ValueError(
            f"study {study_name!r} has a different data/validation contract; "
            "start a new sweep"
        )
    if study.user_attrs.get("final_test_evaluated"):
        raise ValueError(
            f"study {study_name!r} already consumed its outer test set and cannot "
            "accept more trials without contaminating that holdout"
        )
    study.set_user_attr("validation_contract", validation_contract)
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
            nested_data.folds, tickers, args.cash, refit_every_n_bars, args.asset_class,
            warmup_bars, min_train_bars, structural_overrides,
            embargo_bars=args.embargo_bars,
            std_weight=args.stability_std_weight,
            turnover_weight=args.turnover_penalty_weight,
            cost_sensitivity_weight=args.cost_sensitivity_weight,
            min_positive_fraction=args.min_positive_fold_fraction,
            stress_cost_multiplier=args.stress_cost_multiplier,
            normal_slippage_probability=args.normal_slippage_probability,
            seed=seed,
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

    print("\n=========== BEST (nested walk-forward) ===========")
    print("value:", study.best_value)
    print("params:", study.best_params)

    # Persist the winning params so run_backtest / live can load them directly.
    payload = {
        "params": study.best_params,
        "asset_class": args.asset_class,
        "objective_metric": score_profile["metric"],
        "trade_penalty": score_profile["trade_penalty"],
        "market_session": (
            "Regular + extended hours"
            if args.asset_class == "equity" and args.include_extended_hours
            else get_asset_profile(args.asset_class)["market"]["session"]
        ),
        "include_extended_hours": args.include_extended_hours,
        "bar_interval_minutes": infer_bar_interval_minutes_from_csv(args.csv, tickers),
        "in_sample_value": study.best_value,
        "selection_value": study.best_value,
        "seed": seed,
        "trials": args.trials,
        # train_frac remains as compatibility metadata for older dashboard
        # clients. It is no longer a one-shot optimization split.
        "train_frac": nested_data.development_fraction,
        "validation_scheme": "purged_nested_walk_forward",
        "final_test_frac": args.final_test_frac,
        "walk_forward_folds": args.walk_forward_folds,
        "embargo_bars": args.embargo_bars,
        "effective_embargo_bars": max(
            args.embargo_bars, int(study.best_params["horizon"])
        ),
        "stability_std_weight": args.stability_std_weight,
        "turnover_penalty_weight": args.turnover_penalty_weight,
        "cost_sensitivity_weight": args.cost_sensitivity_weight,
        "min_positive_fold_fraction": args.min_positive_fold_fraction,
        "normal_slippage_probability": args.normal_slippage_probability,
        "stress_cost_multiplier": args.stress_cost_multiplier,
        "best_fold_results": study.best_trial.user_attrs.get(
            "walk_forward_folds", []
        ),
        "promotion_status": "REQUIRES_SHADOW_AND_PAPER_AGREEMENT",
        "source_csv": args.csv,
        "news_snapshot_sha256": news_snapshot_sha256,
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

    # Evaluate the locked winner on the untouched outer period. This data was
    # not loaded by any Optuna trial. Normal and stressed assumptions each run
    # once; neither result can change study.best_params.
    print("\n=========== FINAL TEST (untouched outer holdout) ===========")
    final_embargo = max(args.embargo_bars, int(study.best_params["horizon"]))
    final_overrides = {
        **study.best_params,
        "refit_every_n_bars": refit_every_n_bars,
        "warmup_bars": warmup_bars,
        "min_train_bars": min_train_bars,
        **(structural_overrides or {}),
        "backtest_model_fit_end_ns": nested_data.final_model_fit_end_ns(
            final_embargo
        ),
        "backtest_trade_start_ns": nested_data.final_test_start_ns,
    }
    # Fail closed before touching the holdout: even a partial/failed final run
    # consumes information and must prevent later tuning in this study.
    study.set_user_attr("final_test_evaluated", True)
    study.set_user_attr("final_test_evaluated_at", time.time())
    engine = build_and_run(
        csv_path=nested_data.final_context_path,
        tickers=tickers,
        strategy_overrides=final_overrides,
        starting_cash=args.cash,
        log_level="ERROR",
        bypass_logging=True,
        asset_class=args.asset_class,
        cost_multiplier=1.0,
        slippage_probability=args.normal_slippage_probability,
        fill_model_seed=seed,
    )
    final_normal = _engine_performance(engine, args.cash, args.asset_class)
    stressed_engine = build_and_run(
        csv_path=nested_data.final_context_path,
        tickers=tickers,
        strategy_overrides=final_overrides,
        starting_cash=args.cash,
        log_level="ERROR",
        bypass_logging=True,
        asset_class=args.asset_class,
        cost_multiplier=args.stress_cost_multiplier,
        slippage_probability=min(
            1.0, args.normal_slippage_probability * 2.0
        ),
        fill_model_seed=seed,
    )
    final_stressed = _engine_performance(
        stressed_engine, args.cash, args.asset_class
    )
    oos_score = stability_aware_score(
        [final_normal.ratio],
        [final_stressed.ratio],
        [final_normal.turnover],
        std_weight=args.stability_std_weight,
        turnover_weight=args.turnover_penalty_weight,
        cost_sensitivity_weight=args.cost_sensitivity_weight,
        min_positive_fraction=args.min_positive_fold_fraction,
        require_positive_folds=False,
    )
    payload["final_test"] = {
        "start": pd.Timestamp(
            nested_data.final_test_start_ns, unit="ns", tz="UTC"
        ).isoformat(),
        "end": pd.Timestamp(
            nested_data.final_test_end_ns, unit="ns", tz="UTC"
        ).isoformat(),
        "normal_ratio": final_normal.ratio,
        "stressed_ratio": final_stressed.ratio,
        "turnover": final_normal.turnover,
        "trades": final_normal.trades,
        "stability_adjusted_score": oos_score,
    }
    with open(args.out_params, "w") as fh:
        json.dump(payload, fh, indent=2)
    print(
        f"final net {score_profile['metric']}: {final_normal.ratio:.4f}; "
        f"2x-cost stress: {final_stressed.ratio:.4f}; "
        f"stability-adjusted: {oos_score:.4f}"
    )

    # Persist a run artifact (trial history, OOS equity curve, ML-performance)
    # for the reporting dashboard. Best-effort: a failure here must never mask
    # the search result already printed and saved to --out-params above.
    try:
        artifact = save_optimize_artifact(
            study=study,
            oos_engine=engine,
            oos_score=oos_score,
            csv_path=args.csv,
            oos_path=nested_data.final_test_path,
            tickers=tickers,
            asset_class=args.asset_class,
            starting_cash=args.cash,
            seed=seed,
            n_trials_requested=args.trials,
            train_frac=nested_data.development_fraction,
            target_score=args.score,
            started_at=started_at,
            run_id=args.run_id,
            structural_overrides=structural_overrides,
            resumed_from=args.resume_run_id,
            ibkr_bar_hours=args.ibkr_bar_hours if args.replace_bars else None,
            include_extended_hours=args.include_extended_hours,
            validation_metadata={
                "scheme": "purged_nested_walk_forward",
                "final_test_frac": args.final_test_frac,
                "walk_forward_folds": args.walk_forward_folds,
                "embargo_bars": args.embargo_bars,
                "effective_embargo_bars": final_embargo,
                "stability_std_weight": args.stability_std_weight,
                "turnover_penalty_weight": args.turnover_penalty_weight,
                "cost_sensitivity_weight": args.cost_sensitivity_weight,
                "min_positive_fold_fraction": args.min_positive_fold_fraction,
                "normal_slippage_probability": args.normal_slippage_probability,
                "stress_cost_multiplier": args.stress_cost_multiplier,
                "best_fold_results": study.best_trial.user_attrs.get(
                    "walk_forward_folds", []
                ),
                "final_test": payload["final_test"],
                "promotion_status": "REQUIRES_SHADOW_AND_PAPER_AGREEMENT",
            },
        )
        print(f"Saved run artifact -> quant/runs/{artifact['run_id']}.json")
    except Exception as e:  # noqa: BLE001
        print(f"(run artifact unavailable: {e})")

    stressed_engine.dispose()
    engine.dispose()
    split_workspace.cleanup()


if __name__ == "__main__":
    # Interactive prompt at the very start of execution: the refit cadence is a
    # fixed structural setting supplied from the terminal, NOT an Optuna-tuned
    # parameter (see _prompt_refit_every_n_bars / the GUARDRAIL in make_objective).
    _refit_every_n_bars = _prompt_refit_every_n_bars()
    main(refit_every_n_bars=_refit_every_n_bars)
