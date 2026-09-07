"""Research-grade analytics derived from persisted run artifacts.

The trading engine remains the source of truth for fills, positions, and equity.
This module turns those records into a stable reporting contract for the web
research hub and adds an independent S&P 500 price-index comparator from FRED.
"""
from __future__ import annotations

from functools import lru_cache
from io import StringIO
import math
from pathlib import Path
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

FRED_SP500_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=SP500"
SP500_CACHE_PATH = Path(__file__).resolve().parents[1] / "data" / "benchmarks" / "sp500_fred.csv"


def _finite(value: float | int | None, digits: int = 6):
    if value is None or not np.isfinite(value):
        return None
    return round(float(value), digits)


def _equity_frame(run: dict) -> pd.DataFrame:
    raw = run.get("equity_curve") or run.get("oos_equity_curve") or []
    if not raw:
        return pd.DataFrame(columns=["value"])
    frame = pd.DataFrame(raw).rename(columns={"equity": "value"})
    frame["ts"] = pd.to_datetime(frame["ts"], format="mixed", utc=True)
    frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
    frame = frame.dropna(subset=["ts", "value"]).sort_values("ts")
    return frame.groupby("ts", as_index=True)["value"].last().to_frame()


def _daily_values(frame: pd.DataFrame) -> pd.Series:
    if frame.empty:
        return pd.Series(dtype=float)
    series = frame["value"].copy()
    series.index = series.index.floor("D")
    return series.groupby(level=0).last().sort_index()


@lru_cache(maxsize=1)
def _fred_sp500() -> pd.Series:
    if SP500_CACHE_PATH.is_file():
        frame = pd.read_csv(SP500_CACHE_PATH)
    else:
        request = Request(FRED_SP500_URL, headers={"User-Agent": "quant-research-hub/1.0"})
        with urlopen(request, timeout=20) as response:  # noqa: S310 - fixed trusted URL
            payload = response.read().decode("utf-8")
        frame = pd.read_csv(StringIO(payload))
    frame["observation_date"] = pd.to_datetime(frame["observation_date"], utc=True)
    frame["SP500"] = pd.to_numeric(frame["SP500"], errors="coerce")
    return frame.dropna().set_index("observation_date")["SP500"].sort_index()


def _benchmark_values(start: pd.Timestamp, end: pd.Timestamp) -> tuple[pd.Series, dict]:
    try:
        series = _fred_sp500().loc[start.floor("D"):end.floor("D")]
    except Exception as exc:  # network failure must not block local run review
        return pd.Series(dtype=float), {
            "status": "unavailable",
            "message": f"S&P 500 comparator unavailable: {exc}",
        }
    if len(series) < 2:
        return pd.Series(dtype=float), {
            "status": "unavailable",
            "message": "S&P 500 comparator has insufficient overlap with this run.",
        }
    return series, {
        "status": "ready",
        "symbol": "SP500",
        "label": "S&P 500 price index",
        "source": "Federal Reserve Bank of St. Louis (FRED)",
        "source_url": FRED_SP500_URL,
        "return_basis": "price return; dividends excluded",
        "coverage_start": series.index[0].isoformat(),
        "coverage_end": series.index[-1].isoformat(),
    }


def _drawdown(values: pd.Series) -> pd.Series:
    return values / values.cummax() - 1.0 if not values.empty else values


def _annualized_stats(values: pd.Series, periods: int) -> dict:
    returns = values.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    if len(values) < 2 or returns.empty:
        return {}
    elapsed_days = max((values.index[-1] - values.index[0]).total_seconds() / 86400, 1)
    years = elapsed_days / 365.2425
    total_return = values.iloc[-1] / values.iloc[0] - 1.0
    cagr = (values.iloc[-1] / values.iloc[0]) ** (1 / years) - 1.0
    volatility = returns.std(ddof=1) * math.sqrt(periods) if len(returns) > 1 else np.nan
    mean = returns.mean()
    std = returns.std(ddof=1)
    downside = returns[returns < 0].std(ddof=1)
    sharpe = mean / std * math.sqrt(periods) if std and np.isfinite(std) else np.nan
    sortino = mean / downside * math.sqrt(periods) if downside and np.isfinite(downside) else np.nan
    drawdown = _drawdown(values)
    max_drawdown = abs(drawdown.min())
    ulcer = math.sqrt(float(np.mean(np.square(drawdown))))
    return {
        "total_return_pct": _finite(total_return * 100, 3),
        "cagr_pct": _finite(cagr * 100, 3),
        "annual_volatility_pct": _finite(volatility * 100, 3),
        "sharpe": _finite(sharpe, 3),
        "sortino": _finite(sortino, 3),
        "max_drawdown_pct": _finite(max_drawdown * 100, 3),
        "calmar": _finite(cagr / max_drawdown, 3) if max_drawdown else None,
        "ulcer_index_pct": _finite(ulcer * 100, 3),
        "best_day_pct": _finite(returns.max() * 100, 3),
        "worst_day_pct": _finite(returns.min() * 100, 3),
        "skew": _finite(returns.skew(), 3),
        "excess_kurtosis": _finite(returns.kurt(), 3),
        "observations": int(len(returns)),
    }


def _probabilistic_sharpe(returns: pd.Series, benchmark_sharpe: float = 0.0) -> float | None:
    returns = returns.dropna()
    if len(returns) < 3:
        return None
    std = returns.std(ddof=1)
    if not std:
        return None
    sharpe = returns.mean() / std
    skew = returns.skew()
    kurt = returns.kurt() + 3.0
    denominator = 1 - skew * sharpe + ((kurt - 1) / 4) * sharpe**2
    if denominator <= 0:
        return None
    z_score = (sharpe - benchmark_sharpe) * math.sqrt(len(returns) - 1) / math.sqrt(denominator)
    return _finite(0.5 * (1 + math.erf(z_score / math.sqrt(2))) * 100, 2)


def _relative_stats(strategy: pd.Series, benchmark: pd.Series) -> dict:
    aligned = pd.concat(
        [strategy.rename("strategy"), benchmark.rename("benchmark")], axis=1
    ).sort_index().ffill().dropna()
    returns = aligned.pct_change().dropna()
    if len(returns) < 3:
        return {}
    strategy_returns = returns["strategy"]
    benchmark_returns = returns["benchmark"]
    variance = benchmark_returns.var(ddof=1)
    beta = strategy_returns.cov(benchmark_returns) / variance if variance else np.nan
    alpha_daily = strategy_returns.mean() - beta * benchmark_returns.mean() if np.isfinite(beta) else np.nan
    active = strategy_returns - benchmark_returns
    tracking_error = active.std(ddof=1) * math.sqrt(252)
    info_ratio = active.mean() / active.std(ddof=1) * math.sqrt(252) if active.std(ddof=1) else np.nan
    up = benchmark_returns > 0
    down = benchmark_returns < 0
    upside = strategy_returns[up].mean() / benchmark_returns[up].mean() if up.any() and benchmark_returns[up].mean() else np.nan
    downside = strategy_returns[down].mean() / benchmark_returns[down].mean() if down.any() and benchmark_returns[down].mean() else np.nan
    benchmark_daily_sharpe = benchmark_returns.mean() / benchmark_returns.std(ddof=1) if benchmark_returns.std(ddof=1) else 0.0
    return {
        "alpha_annual_pct": _finite(alpha_daily * 252 * 100, 3),
        "beta": _finite(beta, 3),
        "correlation": _finite(strategy_returns.corr(benchmark_returns), 3),
        "tracking_error_pct": _finite(tracking_error * 100, 3),
        "information_ratio": _finite(info_ratio, 3),
        "upside_capture_pct": _finite(upside * 100, 2),
        "downside_capture_pct": _finite(downside * 100, 2),
        "probabilistic_sharpe_vs_zero_pct": _probabilistic_sharpe(strategy_returns),
        "probabilistic_sharpe_vs_benchmark_pct": _probabilistic_sharpe(
            strategy_returns, benchmark_daily_sharpe
        ),
    }


def _points(series: pd.Series, key: str, *, scale: float = 1.0) -> list[dict]:
    return [
        {"ts": timestamp.isoformat(), key: _finite(value * scale, 6)}
        for timestamp, value in series.items()
        if np.isfinite(value)
    ]


def _monthly_returns(values: pd.Series) -> list[dict]:
    monthly = values.resample("ME").last().pct_change().dropna()
    return [
        {"year": int(timestamp.year), "month": int(timestamp.month), "return_pct": _finite(value * 100, 3)}
        for timestamp, value in monthly.items()
    ]


def _rolling_sharpe(values: pd.Series, window: int = 63, risk_free=None) -> list[dict]:
    returns = values.pct_change()
    if risk_free is not None:
        returns = returns - risk_free
    minimum = max(20, window // 3)
    rolling = returns.rolling(window, min_periods=minimum).mean()
    rolling /= returns.rolling(window, min_periods=minimum).std(ddof=1)
    rolling *= math.sqrt(252)
    return _points(rolling.dropna(), "value")


def _trade_analysis(run: dict) -> dict:
    rows = (run.get("positions") or {}).get("rows") or []
    pnl_values = []
    for row in rows:
        raw = next(
            (row.get(key) for key in ("realized_pnl", "realised_pnl", "pnl_realized", "pnl") if row.get(key) is not None),
            None,
        )
        if raw is None:
            continue
        try:
            value = float("".join(char for char in str(raw) if char in "-+.0123456789eE"))
        except ValueError:
            continue
        if value != 0:
            pnl_values.append(value)
    if not pnl_values:
        return {"count": 0, "pnl": []}
    values = np.asarray(pnl_values, dtype=float)
    wins = values[values > 0]
    losses = values[values < 0]
    max_win_streak = max_loss_streak = win_streak = loss_streak = 0
    for value in values:
        win_streak = win_streak + 1 if value > 0 else 0
        loss_streak = loss_streak + 1 if value < 0 else 0
        max_win_streak = max(max_win_streak, win_streak)
        max_loss_streak = max(max_loss_streak, loss_streak)
    return {
        "count": int(len(values)),
        "expectancy_usd": _finite(values.mean(), 2),
        "median_usd": _finite(np.median(values), 2),
        "average_win_usd": _finite(wins.mean(), 2) if len(wins) else None,
        "average_loss_usd": _finite(losses.mean(), 2) if len(losses) else None,
        "payoff_ratio": _finite(abs(wins.mean() / losses.mean()), 3) if len(wins) and len(losses) else None,
        "largest_win_usd": _finite(values.max(), 2),
        "largest_loss_usd": _finite(values.min(), 2),
        "max_win_streak": max_win_streak,
        "max_loss_streak": max_loss_streak,
        "pnl": [_finite(value, 2) for value in values],
    }


def _optimization_analysis(run: dict) -> dict | None:
    if run.get("kind") != "optimize":
        return None
    trials = run.get("trials") or []
    complete = [
        trial for trial in trials
        if trial.get("state") == "COMPLETE" and isinstance(trial.get("value"), (int, float))
    ]
    incumbent = -math.inf
    history = []
    for trial in sorted(trials, key=lambda item: int(item.get("number", 0))):
        value = trial.get("value")
        if trial.get("state") == "COMPLETE" and isinstance(value, (int, float)):
            incumbent = max(incumbent, float(value))
        history.append({
            "number": int(trial.get("number", 0)),
            "state": trial.get("state", "UNKNOWN"),
            "value": _finite(value),
            "incumbent": _finite(incumbent) if incumbent > -math.inf else None,
        })
    importance = []
    if len(complete) >= 3:
        names = sorted({key for trial in complete for key in (trial.get("params") or {})})
        target = pd.Series([float(trial["value"]) for trial in complete])
        for name in names:
            raw = [(trial.get("params") or {}).get(name) for trial in complete]
            if all(isinstance(value, bool) for value in raw if value is not None):
                values = pd.Series([float(bool(value)) for value in raw])
            elif all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in raw if value is not None):
                values = pd.to_numeric(pd.Series(raw), errors="coerce")
            else:
                values = pd.Series(raw, dtype="category").cat.codes.replace(-1, np.nan)
            score = abs(values.corr(target, method="spearman"))
            importance.append({"parameter": name, "importance": _finite(score, 4) or 0.0})
    importance.sort(key=lambda item: item["importance"], reverse=True)
    best_trial = max(complete, key=lambda item: item["value"], default={})
    folds = (best_trial.get("user_attrs") or {}).get("walk_forward_folds") or []
    in_sample = run.get("in_sample_value")
    out_of_sample = run.get("oos_score")
    return {
        "history": history,
        "state_counts": {
            state: sum(trial.get("state") == state for trial in trials)
            for state in ("COMPLETE", "PRUNED", "FAIL", "RUNNING")
        },
        "parameter_importance": importance,
        "best_trial_number": best_trial.get("number"),
        "best_params": run.get("best_params") or best_trial.get("params") or {},
        "folds": folds,
        "validation": run.get("validation") or {},
        "generalization_gap": _finite(float(in_sample) - float(out_of_sample), 4)
        if isinstance(in_sample, (int, float)) and isinstance(out_of_sample, (int, float))
        else None,
    }


def analyze_run(run: dict) -> dict:
    equity_frame = _equity_frame(run)
    if equity_frame.empty:
        return {
            "run_id": run.get("run_id"),
            "status": "insufficient_data",
            "message": "This run has no recorded equity curve.",
        }
    strategy = _daily_values(equity_frame)
    benchmark, benchmark_meta = _benchmark_values(strategy.index[0], strategy.index[-1])
    strategy_stats = _annualized_stats(strategy, 365 if run.get("asset_class") == "crypto" else 252)
    excess_basis = None
    daily_rf = None
    simulation = run.get("equity_simulation")
    if simulation and simulation.get("config"):
        from quant.run.equity_simulation import risk_free_intervals
        rates = risk_free_intervals(simulation["config"], [date.timestamp() for date in strategy.index])
        daily_rf = pd.Series([np.nan, *rates], index=strategy.index)
        excess = (strategy.pct_change() - daily_rf).dropna()
        sigma = excess.std(ddof=1)
        strategy_stats["sharpe"] = _finite(excess.mean() / sigma * math.sqrt(252), 3) if sigma else None
        excess_basis = "Strategy Sharpe: daily returns minus configured risk-free accrual; benchmark Sharpe: price returns, zero risk-free rate"
    benchmark_stats = _annualized_stats(benchmark, 252) if not benchmark.empty else {}
    relative = _relative_stats(strategy, benchmark) if not benchmark.empty else {}
    strategy_index = strategy / strategy.iloc[0] * 100
    benchmark_index = benchmark / benchmark.iloc[0] * 100 if not benchmark.empty else benchmark
    return {
        "run_id": run.get("run_id"),
        "status": "ready",
        "period": {
            "start": strategy.index[0].isoformat(),
            "end": strategy.index[-1].isoformat(),
            "calendar_days": int((strategy.index[-1] - strategy.index[0]).days),
        },
        "strategy": strategy_stats,
        "benchmark": {**benchmark_meta, "metrics": benchmark_stats},
        "relative": relative,
        "sharpe_basis": excess_basis or "Zero risk-free rate assumed",
        "series": {
            "strategy_index": _points(strategy_index, "value"),
            "benchmark_index": _points(benchmark_index, "value"),
            "strategy_drawdown": _points(_drawdown(strategy) * 100, "value"),
            "benchmark_drawdown": _points(_drawdown(benchmark) * 100, "value") if not benchmark.empty else [],
            "rolling_sharpe_63d": _rolling_sharpe(strategy, risk_free=daily_rf),
        },
        "monthly_returns": _monthly_returns(strategy),
        "trades": _trade_analysis(run),
        "optimization": _optimization_analysis(run),
    }
