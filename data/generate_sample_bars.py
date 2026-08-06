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
import secrets
from datetime import datetime, timedelta, timezone

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


def _business_days(start: datetime, n: int) -> list[datetime]:
    """``n`` weekday-only (Mon-Fri) dates starting from ``start``.

    A documented simplification for equities/ETFs: skips weekends but does NOT
    model a real exchange holiday calendar. Like the crypto generator, this is
    a pipeline exerciser, not market reality -- it exists so the walk-forward /
    backtest / Optuna pipeline can be run end-to-end on equity-shaped (real
    trading-day gaps) data without a live IBKR connection.
    """
    dates = []
    d = start
    while len(dates) < n:
        if d.weekday() < 5:  # 0=Mon .. 4=Fri
            dates.append(d)
        d = d + timedelta(days=1)
    return dates


def generate(
    tickers: list[str],
    n_days: int = 1000,
    start: datetime | None = None,
    seed: int | None = 42,
    asset_class: str = "crypto",
) -> pd.DataFrame:
    """Synthetic OHLCV. If seed is None, a fresh random seed is drawn.

    ``asset_class`` selects the trading calendar (crypto: 24/7 consecutive
    calendar days; equity: weekdays-only business days -- see
    ``_business_days``) and the daily-volatility range used to simulate
    returns (equities are far less volatile day-to-day than crypto).
    """
    if seed is None:
        seed = secrets.randbelow(2**31)
    rng = np.random.default_rng(seed)
    start = start or datetime(2021, 6, 28, tzinfo=timezone.utc)
    is_equity = asset_class == "equity"
    dates = _business_days(start, n_days) if is_equity else _calendar_days(start, n_days)

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
                    "timestamp": [d.strftime("%Y-%m-%d") for d in dates],
                    "ticker": ticker,
                    "open": np.round(open_, 2),
                    "high": np.round(high, 2),
                    "low": np.round(low, 2),
                    "close": np.round(close, 2),
                    "volume": volume,
                }
            )
        )

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
        help="Trading calendar (24/7 vs weekdays-only) and vol range to simulate.",
    )
    p.add_argument(
        "--tickers",
        nargs="*",
        default=None,
        help="Defaults to a crypto or equity ticker list depending on "
        "--asset-class if omitted.",
    )
    p.add_argument("--days", type=int, default=1000)
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

    df = generate(tickers, n_days=args.days, seed=args.seed, asset_class=args.asset_class)
    df.to_csv(args.out, index=False)
    print(
        f"Wrote {len(df):,} rows ({df.ticker.nunique()} tickers) -> {args.out} "
        f"[seed={df.attrs['seed']}]"
    )


if __name__ == "__main__":
    main()
