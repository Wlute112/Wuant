"""Mock live positions/risk endpoints.

No paper/live trading has run yet (see PRODUCT.md). These endpoints return
realistic sample data in the shape the real live wiring will eventually
serve, clearly flagged "mock": true, so the dashboard's live panels can be
built and demoed today and swapped to a real data source later without a
frontend change.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter

router = APIRouter(prefix="/api/live", tags=["live"])

# Fixed, REAL risk-rail constants (see CLAUDE.md / strategies/risk.py) -- not
# mocked. Only the equity/pnl/drawdown/leverage/positions below are simulated
# until paper trading actually produces this state.
RISK_RAILS = {
    "risk_budget_pct": 1.0,
    "hard_cap_pct": 0.25,
    "leverage_max": 1.0,
    "daily_loss_limit_pct": 2.0,
    "drawdown_warn_pct": 5.0,
    "kill_switch_pct": 10.0,
}

_SAMPLE_POSITIONS = [
    {
        "symbol": "BTC",
        "side": "LONG",
        "qty": 0.0142,
        "avg_price": 61840.0,
        "mark_price": 62910.0,
        "unrealized_pnl": 15.19,
        "notional": 893.32,
    },
    {
        "symbol": "ETH",
        "side": "SHORT",
        "qty": 0.51,
        "avg_price": 3120.0,
        "mark_price": 3085.0,
        "unrealized_pnl": 17.85,
        "notional": 1573.35,
    },
    {
        "symbol": "SOL",
        "side": "LONG",
        "qty": 4.2,
        "avg_price": 138.5,
        "mark_price": 135.9,
        "unrealized_pnl": -10.92,
        "notional": 570.78,
    },
]


@router.get("/positions")
def get_positions():
    return {
        "mock": True,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "positions": _SAMPLE_POSITIONS,
    }


@router.get("/risk")
def get_risk():
    return {
        "mock": True,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "equity": 5123.45,
        "starting_equity": 5000.0,
        "daily_pnl_pct": -0.8,
        "drawdown_pct": 2.1,
        "gross_leverage": 0.42,
        "kill_switch_engaged": False,
        "rails": RISK_RAILS,
    }
