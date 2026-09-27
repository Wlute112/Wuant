"""Generate synthetic daily OHLCV bars so the backtest runs without TWS/IBKR.

Output schema matches the existing data_loader.py expectation:
    timestamp,ticker,open,high,low,close,volume

Each ticker is a geometric brownian motion with a mild, lag-dependent
autoregressive drift component. The AR component is intentional: it gives the
Huber-regression prediction layer a *real* (if weak) signal to learn, so the
end-to-end pipeline demonstrates non-trivial behaviour. It is NOT financial
reality -- it exists only so you can exercise backtest -> optimize tonight.

Determinism
-----------
By DEFAULT this now draws a FRESH random seed every run, so regenerating the
file genuinely produces NEW data (different walk-forward / Optuna results).
Pass ``--seed N`` for a reproducible run. The seed actually used is always
printed so you can reproduce any run later.

Usage:
    1. python -m quant.data.generate_sample_bars --out quant/data/sample_bars.csv
    2. python -m quant.data.generate_sample_bars --seed 42   # reproducible
"""
from __future__ import annotations

import argparse
import json
import secrets
from datetime import datetime, timedelta, timezone
from importlib.metadata import version
from pathlib import Path

import exchange_calendars as xcals
import numpy as np
import pandas as pd

# Crypto base assets (traded against USD). Crypto trades 24/7, so unlike
# equities there are no market-closed days to skip.
DEFAULT_TICKERS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK", "LTC", "BCH"]
EQUITY_DEFAULT_TICKERS = ["SPY", "QQQ", "DIA", "IWM"]  # liquid index ETFs


def _calendar_days(start: datetime, n: int) -> list[datetime]:
    """Consecutive CALENDAR days -- crypto trades 24/7 (no weekend gaps).

    Continuous daily bars mean the optimizer's return-spacing annualization
    sees ~365 periods/year instead of ~252 (it derives that from the
    timestamps automatically, so nothing downstream is hard-coded to 252).
    """
    return [start + timedelta(days=i) for i in range(n)]


def _equity_sessions(start: datetime, n: int) -> pd.DataFrame:
    """NYSE sessions with authoritative UTC open/close, including half days."""
    if n < 1:
        raise ValueError("days must be positive")
    start_date = pd.Timestamp(start).date().isoformat()
    calendar = xcals.get_calendar("XNYS", start=start_date,
                                  end=(pd.Timestamp(start_date) + pd.Timedelta(days=max(365, n * 2))).date().isoformat())
    sessions = calendar.schedule.loc[start_date:]
    if len(sessions) < n:
        raise ValueError("requested days exceed the installed XNYS calendar range")
    return sessions.iloc[:n].copy()


def inspect_equity_fixture_calendar(rows: pd.DataFrame) -> dict:
    """Check fixture timestamps and session declarations against installed XNYS."""
    report = {"calendar": "XNYS", "version": None, "sessions": len(rows),
              "early_close_sessions": 0, "utc_open_times": [], "errors": []}
    required = {"calendar", "calendar_version", "session_date", "session_open_utc",
                "session_close_utc", "session_minutes", "early_close"}
    missing = required - set(rows.columns)
    if missing:
        report["errors"].append("Synthetic XNYS fixture is missing calendar metadata: " + ", ".join(sorted(missing)))
        return report
    if rows["calendar"].astype(str).nunique() != 1 or str(rows["calendar"].iloc[0]) != "XNYS":
        report["errors"].append("Synthetic equity fixture must declare XNYS for every bar.")
        return report
    versions = rows["calendar_version"].astype(str).unique()
    report["version"] = versions[0] if len(versions) == 1 else None
    if len(versions) != 1:
        report["errors"].append("Synthetic fixture has conflicting calendar versions.")
        return report
    if report["version"] != version("exchange_calendars"):
        report["errors"].append("Synthetic fixture calendar version differs from the installed version; regenerate before research.")
        return report
    dates = pd.to_datetime(rows["session_date"], errors="coerce")
    if dates.isna().any() or dates.duplicated().any() or not dates.is_monotonic_increasing:
        report["errors"].append("Synthetic fixture session dates must be valid, unique and ordered.")
        return report
    try:
        calendar = xcals.get_calendar("XNYS", start=dates.iloc[0], end=dates.iloc[-1])
        schedule = calendar.schedule.loc[dates.iloc[0]:dates.iloc[-1]]
    except (KeyError, ValueError, OverflowError) as exc:
        report["errors"].append(f"XNYS calendar cannot cover fixture dates: {exc}")
        return report
    if list(schedule.index) != list(dates):
        report["errors"].append("Synthetic fixture skips a session or includes an exchange holiday.")
        return report
    opens = pd.to_datetime(rows["session_open_utc"], utc=True, errors="coerce")
    closes = pd.to_datetime(rows["session_close_utc"], utc=True, errors="coerce")
    timestamps = pd.to_datetime(rows["timestamp"], utc=True, errors="coerce")
    minutes = pd.to_numeric(rows["session_minutes"], errors="coerce")
    early = rows["early_close"].astype(str).str.lower().map({"true": True, "false": False})
    expected_minutes = (schedule["close"].to_numpy() - schedule["open"].to_numpy()) / np.timedelta64(1, "m")
    if (opens.isna().any() or closes.isna().any() or timestamps.isna().any()
            or not np.array_equal(opens.to_numpy(), schedule["open"].to_numpy())
            or not np.array_equal(closes.to_numpy(), schedule["close"].to_numpy())
            or not np.array_equal(timestamps.to_numpy(), schedule["close"].to_numpy())
            or not np.array_equal(minutes.to_numpy(), expected_minutes)
            or early.isna().any() or not np.array_equal(early.to_numpy(), expected_minutes < 390)):
        report["errors"].append("Synthetic fixture bar times, duration or early-close flags disagree with XNYS.")
    report["early_close_sessions"] = int(sum(expected_minutes < 390))
    report["utc_open_times"] = sorted(opens.dt.strftime("%H:%M").dropna().unique().tolist())
    return report


def generate(
    tickers: list[str],
    n_days: int = 1000,
    start: datetime | None = None,
    seed: int | None = 42,
    asset_class: str = "crypto",
) -> pd.DataFrame:
    """Synthetic OHLCV. If seed is None, a fresh random seed is drawn.

    ``asset_class`` selects the trading calendar (crypto: 24/7 consecutive
    calendar days; equity: XNYS sessions) and the daily-volatility range used to simulate
    returns (equities are far less volatile day-to-day than crypto).
    """
    if seed is None:
        seed = secrets.randbelow(2**31)
    rng = np.random.default_rng(seed)
    start = start or datetime(2021, 6, 28, tzinfo=timezone.utc)
    is_equity = asset_class == "equity"
    if n_days < 1:
        raise ValueError("days must be positive")
    sessions = _equity_sessions(start, n_days) if is_equity else None
    dates = _calendar_days(start, n_days) if not is_equity else None

    frames = []
    for i, ticker in enumerate(tickers):
        # Per-ticker params (deterministic given seed + index). Crypto is more
        # volatile than equities, so daily vol is drawn higher; prices stay in a
        # moderate band so 2-decimal USD pricing is fine for the synthetic
        # exerciser (real prices come from ibkr_fetch).
        if is_equity:
            price0 = float(rng.uniform(50, 600))    # typical liquid ETF range
            daily_vol = float(rng.uniform(0.005, 0.02))
        else:
            price0 = float(rng.uniform(20, 4000))
            daily_vol = float(rng.uniform(0.02, 0.06))
        ar_coef = float(rng.uniform(0.03, 0.10))  # weak momentum the model can find

        rets = np.zeros(n_days)
        eps = rng.normal(0.0, daily_vol, n_days)
        for t in range(1, n_days):
            # AR(1) on returns + noise: yesterday's return weakly predicts today.
            rets[t] = ar_coef * rets[t - 1] + eps[t]

        close = price0 * np.cumprod(1.0 + rets)
        # Build OHLC around close with intraday range proportional to vol.
        intraday = np.abs(rng.normal(0.0, daily_vol, n_days)) * close
        open_ = np.concatenate([[price0], close[:-1]])
        high = np.maximum(open_, close) + intraday * 0.5
        low = np.minimum(open_, close) - intraday * 0.5
        low = np.clip(low, 0.01, None)
        volume = rng.integers(1_000_000, 20_000_000, n_days)

        frames.append(
            pd.DataFrame(
                {
                    "timestamp": (
                        [value.isoformat() for value in sessions["close"]]
                        if is_equity else [d.strftime("%Y-%m-%d") for d in dates]
                    ),
                    "ticker": ticker,
                    "open": np.round(open_, 2),
                    "high": np.round(high, 2),
                    "low": np.round(low, 2),
                    "close": np.round(close, 2),
                    "volume": volume,
                }
            )
        )
        frame = frames[-1]
        frame["source"] = "synthetic_fixture"
        frame["session"] = "XNYS_RTH" if is_equity else "24/7"
        frame["price_basis"] = "synthetic_unadjusted"
        frame["volume_basis"] = "synthetic"
        if is_equity:
            frame["calendar"] = "XNYS"
            frame["calendar_version"] = version("exchange_calendars")
            frame["session_date"] = [day.date().isoformat() for day in sessions.index]
            frame["session_open_utc"] = [value.isoformat() for value in sessions["open"]]
            frame["session_close_utc"] = [value.isoformat() for value in sessions["close"]]
            frame["session_minutes"] = [
                int((close - opened).total_seconds() // 60)
                for opened, close in zip(sessions["open"], sessions["close"])
            ]
            frame["early_close"] = frame["session_minutes"] < 390
            frame["requested_bar_hours"] = 24

    df = pd.concat(frames, ignore_index=True)
    df = df.sort_values(["timestamp", "ticker"]).reset_index(drop=True)
    df.attrs["seed"] = seed
    return df


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="quant/data/sample_bars.csv")
    p.add_argument(
        "--asset-class",
        choices=["crypto", "equity"],
        default="crypto",
        help="Trading calendar (24/7 vs XNYS sessions) and vol range to simulate.",
    )
    p.add_argument(
        "--tickers",
        nargs="*",
        default=None,
        help="Defaults to a crypto or equity ticker list depending on "
        "--asset-class if omitted.",
    )
    p.add_argument("--days", type=int, default=1000)
    p.add_argument("--start", default="2021-06-28", help="First eligible session date (YYYY-MM-DD).")
    p.add_argument("--progress-path", default=None)
    p.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Fixed seed for reproducibility. Omit for a FRESH random run.",
    )
    args = p.parse_args()
    tickers = args.tickers or (
        EQUITY_DEFAULT_TICKERS if args.asset_class == "equity" else DEFAULT_TICKERS
    )

    try:
        start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        p.error(f"invalid --start date: {exc}")
    df = generate(tickers, n_days=args.days, start=start, seed=args.seed, asset_class=args.asset_class)
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    try:
        df.to_csv(temporary, index=False)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    summary = {
        "phase": "completed", "phase_label": "Synthetic fixture ready",
        "csv": str(output), "asset_class": args.asset_class, "tickers": tickers,
        "rows": len(df), "sessions": args.days, "seed": df.attrs["seed"],
        "start": str(df["timestamp"].iloc[0]), "end": str(df["timestamp"].iloc[-1]),
        "calendar": "XNYS" if args.asset_class == "equity" else "24/7",
        "calendar_version": version("exchange_calendars") if args.asset_class == "equity" else None,
        "early_close_sessions": int(df.loc[df.early_close, "session_date"].nunique()) if args.asset_class == "equity" else 0,
    }
    if args.progress_path:
        progress = Path(args.progress_path)
        progress.parent.mkdir(parents=True, exist_ok=True)
        partial = progress.with_suffix(".tmp")
        partial.write_text(json.dumps(summary, allow_nan=False))
        partial.replace(progress)
    print(
        f"Wrote {len(df):,} rows ({df.ticker.nunique()} tickers) -> {args.out} "
        f"[seed={df.attrs['seed']}, calendar={summary['calendar']}, version={summary['calendar_version']}]"
    )


if __name__ == "__main__":
    main()
