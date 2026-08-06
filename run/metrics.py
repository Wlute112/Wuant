"""Performance-metric visuals for the backtest result.

Ported from the legacy ``run_backtest.py`` rich "STRATEGY METRICS" panel, but
adapted to the new MLStrategy / nautilus 1.229 engine: the legacy version read
bespoke ``strategy.wins / gross_profit / ...`` attributes that the lean
MLStrategy does not expose. Here we reconstruct the SAME metrics purely from the
engine's standard reports:

    * account report   -> equity curve  -> Sharpe, max drawdown, net profit
    * positions report -> realized PnL  -> wins/losses, profit factor, capacity
    * fills report      -> notional traded -> turnover

So the metrics layer stays fully decoupled from the strategy internals and works
for backtest, paper, and live identically.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd

# Nautilus reports denominate equity columns differently across builds; we probe
# a small set of likely column names rather than hard-coding one.
_EQUITY_COLS = ("total", "balance_total", "free", "equity")
_PNL_COLS = ("realized_pnl", "realised_pnl", "pnl_realized", "pnl")
_NOTIONAL_COLS = ("notional", "quote_qty", "value")


def _first_col(df: pd.DataFrame, candidates) -> str | None:
    for c in candidates:
        if c in df.columns:
            return c
    return None


def _to_float_series(series: pd.Series) -> np.ndarray:
    """Coerce a column that may contain '1234.50 USD' style strings to floats."""
    if series.dtype.kind in "fi":
        return series.to_numpy(dtype=float)
    cleaned = (
        series.astype(str)
        .str.replace(r"[^0-9.\-eE]", "", regex=True)
        .replace("", np.nan)
    )
    return pd.to_numeric(cleaned, errors="coerce").to_numpy(dtype=float)


@dataclass
class StrategyMetrics:
    net_profit_usd: float = 0.0
    sharpe_ratio: float = 0.0
    sortino_ratio: float | str = 0.0
    max_drawdown_pct: float = 0.0
    total_trades: int = 0
    closed_positions: int = 0
    wins: int = 0
    losses: int = 0
    win_loss_ratio: float = 0.0
    win_rate_pct: float = 0.0
    profit_factor: float | str = 0.0
    turnover_rate: float = 0.0
    capacity_score: float = 0.0

    def as_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- #
# extraction helpers
# --------------------------------------------------------------------------- #
def _equity_curve(engine, venue) -> np.ndarray:
    try:
        report = engine.trader.generate_account_report(venue)
    except Exception:  # noqa: BLE001
        return np.array([])
    if report is None or len(report) == 0:
        return np.array([])
    col = _first_col(report, _EQUITY_COLS)
    if col is None:
        return np.array([])
    vals = _to_float_series(report[col])
    vals = vals[~np.isnan(vals)]
    return vals


def _positions_report(engine) -> pd.DataFrame | None:
    try:
        rep = engine.trader.generate_positions_report()
    except Exception:  # noqa: BLE001
        return None
    if rep is None or len(rep) == 0:
        return None
    return rep


def _fills_report(engine) -> pd.DataFrame | None:
    try:
        rep = engine.trader.generate_order_fills_report()
    except Exception:  # noqa: BLE001
        return None
    if rep is None or len(rep) == 0:
        return None
    return rep


def _sharpe(curve: np.ndarray, periods_per_year: int = 252) -> float:
    if len(curve) < 3:
        return 0.0
    rets = np.diff(curve) / curve[:-1]
    if rets.std() == 0:
        return 0.0
    return float((rets.mean() / rets.std()) * np.sqrt(periods_per_year))


def _sortino(curve: np.ndarray, periods_per_year: int = 252) -> float | str:
    """Sharpe's downside-only counterpart: charges volatility from LOSING
    steps only (MAR = 0), so upside dispersion never penalizes the score.
    Mirrors optimize.py's own Sortino-based objective (_sortino_from_curve),
    but without its turnover penalty -- this is the raw ratio for reporting,
    not a search objective. Returns the string "inf" (JSON-safe, matching
    profit_factor's own convention below) for a no-losing-step curve, rather
    than a literal float("inf").
    """
    if len(curve) < 3:
        return 0.0
    rets = np.diff(curve) / curve[:-1]
    downside = rets[rets < 0]
    if downside.size == 0:
        return 0.0 if rets.mean() <= 0 else "inf"
    sigma_down = float(np.sqrt(np.mean(np.square(downside))))
    if sigma_down == 0:
        return 0.0
    return float((rets.mean() / sigma_down) * np.sqrt(periods_per_year))


def _max_drawdown_pct(curve: np.ndarray) -> float:
    if len(curve) < 2:
        return 0.0
    peak = np.maximum.accumulate(curve)
    dd = (curve - peak) / peak
    return float(-dd.min() * 100.0)


def compute_metrics(engine, venue, starting_cash: float) -> StrategyMetrics:
    """Reconstruct the rich strategy-metrics panel from engine reports."""
    m = StrategyMetrics()

    # ---- equity-curve derived: net profit, Sharpe, drawdown ----
    curve = _equity_curve(engine, venue)
    if len(curve):
        ending = float(curve[-1])
        m.net_profit_usd = round(ending - starting_cash, 2)
        m.sharpe_ratio = round(_sharpe(curve), 2)
        sortino = _sortino(curve)
        m.sortino_ratio = sortino if isinstance(sortino, str) else round(sortino, 2)
        m.max_drawdown_pct = round(_max_drawdown_pct(curve), 2)
        avg_capital = (starting_cash + ending) / 2.0
    else:
        avg_capital = starting_cash

    # ---- fills derived: total trades + traded notional (turnover) ----
    gross_traded_notional = 0.0
    fills = _fills_report(engine)
    if fills is not None:
        m.total_trades = int(len(fills))
        ncol = _first_col(fills, _NOTIONAL_COLS)
        if ncol is not None:
            notional = np.abs(_to_float_series(fills[ncol]))
            gross_traded_notional = float(np.nansum(notional))
        else:
            # Reconstruct notional = |qty| * avg_px when no notional column.
            qcol = _first_col(fills, ("quantity", "filled_qty", "last_qty"))
            pcol = _first_col(fills, ("avg_px", "price", "last_px"))
            if qcol is not None and pcol is not None:
                q = np.abs(_to_float_series(fills[qcol]))
                p = np.abs(_to_float_series(fills[pcol]))
                gross_traded_notional = float(np.nansum(q * p))

    if avg_capital:
        m.turnover_rate = round(gross_traded_notional / avg_capital, 2)

    # ---- positions derived: wins/losses, profit factor, capacity ----
    positions = _positions_report(engine)
    gross_profit = gross_loss = 0.0
    if positions is not None:
        pcol = _first_col(positions, _PNL_COLS)
        if pcol is not None:
            # Nautilus emits per-close-event "snapshot" rows carrying the
            # realized PnL of each (partial) close, plus one final flat summary
            # row per instrument (is_snapshot=False, pnl=0). Count realized
            # PnL events from the snapshots; if the column is absent, fall back
            # to all rows.
            pos = positions
            if "is_snapshot" in pos.columns and bool(pos["is_snapshot"].any()):
                pos = pos[pos["is_snapshot"] == True]  # noqa: E712
            pnls = _to_float_series(pos[pcol])
            pnls = pnls[~np.isnan(pnls)]
            m.closed_positions = int(len(pnls))
            m.wins = int((pnls > 0).sum())
            m.losses = int((pnls < 0).sum())
            gross_profit = float(pnls[pnls > 0].sum())
            gross_loss = float(-pnls[pnls < 0].sum())

    closed = m.wins + m.losses
    m.win_loss_ratio = round((m.wins / m.losses) if m.losses else float(m.wins), 2)
    m.win_rate_pct = round((100.0 * m.wins / closed) if closed else 0.0, 1)
    if gross_loss > 0:
        m.profit_factor = round(gross_profit / gross_loss, 2)
    elif gross_profit > 0:
        m.profit_factor = "inf"
    else:
        m.profit_factor = 0.0
    if gross_traded_notional > 0:
        m.capacity_score = round(m.net_profit_usd / gross_traded_notional, 4)

    return m


def render_panel(m: StrategyMetrics) -> str:
    """Format the metrics into the legacy rich console panel."""
    lines = [
        "",
        "📈 ─── STRATEGY METRICS ────────────────────────────────",
        f"   Net Profit       : ${m.net_profit_usd:,.2f}",
        f"   Sharpe Ratio     : {m.sharpe_ratio}",
        f"   Sortino Ratio    : {m.sortino_ratio}",
        f"   Max Drawdown     : {m.max_drawdown_pct}%",
        f"   Trades (fills)   : {m.total_trades}  |  Closed: {m.closed_positions}",
        f"   Win/Loss Ratio   : {m.win_loss_ratio}  ({m.win_rate_pct}% win rate)",
        f"   Profit Factor    : {m.profit_factor}",
        f"   Turnover Rate    : {m.turnover_rate}x",
        f"   Capacity Score   : {m.capacity_score}  (profit per $ traded)",
        "───────────────────────────────────────────────────────",
    ]
    return "\n".join(lines)


def print_metrics(engine, venue, starting_cash: float) -> StrategyMetrics:
    """Compute + print the panel; return the metrics object for further use."""
    m = compute_metrics(engine, venue, starting_cash)
    print(render_panel(m))
    return m
