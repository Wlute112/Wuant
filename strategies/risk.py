"""Risk manager -- venue-agnostic, framework-agnostic.

Encodes your exact rules so they can be unit-tested in isolation and reused
identically across backtest, paper, and live (the Strategy just calls into it).

Rules implemented
-----------------
1. Position sizing target: risk ~1% of CURRENT account equity per trade
   (the "risk budget"). Sizing is computed from the stop distance so that
   (entry - stop) * qty ~= 1% of equity.
2. Per-trade hard cap: a single trade may never put more than 0.25% of equity
   at risk. If 1% sizing would exceed the cap, qty is clamped down to 0.25%.
   (So 0.25% is the binding ceiling; 1% is the target you can raise later.)
3. Leverage = 1: notional of a new position may not exceed available equity.
4. Daily loss limit: if equity drops >= 2% from the day's starting equity,
   flatten everything and halt new entries for 24 hours.
5. Max drawdown kill-switch: if peak-to-trough equity drawdown reaches the
   kill threshold (default 10%; warn at 5%), permanently disable automated
   execution.

All percentages are configurable; defaults match your spec.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum


class TradingState(str, Enum):
    ACTIVE = "ACTIVE"
    HALTED_DAILY = "HALTED_DAILY"      # temporary 24h halt
    DISABLED_KILL = "DISABLED_KILL"    # permanent kill-switch


@dataclass
class RiskConfig:
    risk_budget_pct: float = 0.01       # 1% target risk per trade
    max_trade_risk_pct: float = 0.0025  # 0.25% hard cap per trade
    max_leverage: float = 1.0           # leverage = 1
    daily_loss_limit_pct: float = 0.02  # 2% daily loss -> flatten + 24h halt
    kill_switch_pct: float = 0.10       # 10% peak-to-trough -> permanent disable
    kill_warn_pct: float = 0.05         # 5% -> warn (logged, not disabled)
    halt_hours: int = 24
    # Fixed rail for the Kelly sizer (see kelly_size_for_trade). Belt-and-
    # suspenders ceiling on the *notional* fraction of equity a single Kelly-
    # sized trade may target, applied BEFORE the ATR 0.25% risk cap / leverage
    # cap. It is deliberately a FIXED risk rail, NOT an Optuna knob: the
    # optimizer tunes the fractional-Kelly multiplier, never the safety ceiling.
    kelly_max_fraction: float = 0.5     # <= 50% of equity notional per trade
    max_order_notional_pct: float = 0.10
    max_symbol_exposure_pct: float = 0.25
    max_sector_exposure_pct: float = 0.30
    max_gross_exposure_pct: float = 1.0
    max_concentration_pct: float = 1.0
    price_collar_pct: float = 0.05


class RiskManager:
    def __init__(self, starting_equity: float, cfg: RiskConfig | None = None):
        self.cfg = cfg or RiskConfig()
        self.state = TradingState.ACTIVE
        self.peak_equity = float(starting_equity)
        self._day: datetime.date | None = None
        self._session_key: str | None = None
        self._day_start_equity = float(starting_equity)
        self._halt_until: datetime | None = None
        self._warned_drawdown = False

    # ---- daily bookkeeping ---------------------------------------------
    def on_new_day(self, now: datetime, equity: float) -> None:
        """Call at the first event of each trading day."""
        d = now.astimezone(timezone.utc).date()
        if self._day != d:
            self._day = d
            self._day_start_equity = float(equity)
        # Auto-release a temporary daily halt once the window has passed.
        if (
            self.state == TradingState.HALTED_DAILY
            and self._halt_until is not None
            and now >= self._halt_until
        ):
            self.state = TradingState.ACTIVE
            self._halt_until = None

    def on_new_session(self, session_key: str, now: datetime, equity: float) -> None:
        """Reset daily risk at the broker exchange-session boundary.

        ``session_key`` is supplied by the exchange calendar (for example
        ``2026-11-02`` for the US equity session) and therefore remains stable
        across UTC midnight, extended hours, DST transitions, and overnight
        segments. The PnL baseline resets with the exchange session, while a
        temporary halt remains in force for its full configured duration; the
        permanent drawdown kill-switch never releases.
        """
        key = str(session_key).strip()
        if not key:
            raise ValueError("session_key must not be empty")
        if self._session_key != key:
            self._session_key = key
            self._day_start_equity = float(equity)
        if (
            self.state == TradingState.HALTED_DAILY
            and self._halt_until is not None
            and now >= self._halt_until
        ):
            self.state = TradingState.ACTIVE
            self._halt_until = None

    # ---- per-event equity check ----------------------------------------
    def update_equity(self, now: datetime, equity: float) -> TradingState:
        """Update drawdown / daily-loss state. Returns current TradingState.

        If this returns HALTED_DAILY or DISABLED_KILL after being ACTIVE, the
        caller must FLATTEN all positions.
        """
        equity = float(equity)
        self.peak_equity = max(self.peak_equity, equity)

        if self.state == TradingState.DISABLED_KILL:
            return self.state

        # --- max drawdown kill-switch (permanent) ---
        dd = (self.peak_equity - equity) / self.peak_equity if self.peak_equity else 0.0
        if dd >= self.cfg.kill_warn_pct and not self._warned_drawdown:
            self._warned_drawdown = True  # caller can log on transition
        if dd >= self.cfg.kill_switch_pct:
            self.state = TradingState.DISABLED_KILL
            self._halt_until = None
            return self.state

        # --- daily loss limit (temporary 24h) ---
        if self.state == TradingState.ACTIVE and self._day_start_equity > 0:
            day_loss = (self._day_start_equity - equity) / self._day_start_equity
            if day_loss >= self.cfg.daily_loss_limit_pct:
                self.state = TradingState.HALTED_DAILY
                self._halt_until = now + timedelta(hours=self.cfg.halt_hours)
        return self.state

    def telemetry(self, equity: float) -> dict:
        """Current risk readings for the read-only dashboard feed."""
        equity = float(equity)
        drawdown = (
            (self.peak_equity - equity) / self.peak_equity
            if self.peak_equity > 0
            else 0.0
        )
        daily_pnl = (
            (equity - self._day_start_equity) / self._day_start_equity
            if self._day_start_equity > 0
            else 0.0
        )
        return {
            "equity": equity,
            "peak_equity": self.peak_equity,
            "day_start_equity": self._day_start_equity,
            "daily_pnl_pct": daily_pnl * 100.0,
            "drawdown_pct": drawdown * 100.0,
            "state": self.state.value,
            "session_key": self._session_key,
            "kill_switch_engaged": self.state == TradingState.DISABLED_KILL,
            "halt_until": (
                self._halt_until.astimezone(timezone.utc).isoformat()
                if self._halt_until is not None
                else None
            ),
        }

    @property
    def can_open(self) -> bool:
        return self.state == TradingState.ACTIVE

    @property
    def drawdown_warning(self) -> bool:
        return self._warned_drawdown and self.state != TradingState.DISABLED_KILL

    def engage_kill_switch(self) -> TradingState:
        """Permanently disable automated execution on an external safety command."""
        self.state = TradingState.DISABLED_KILL
        self._halt_until = None
        return self.state

    # ---- persistence ---------------------------------------------------
    def snapshot(self) -> dict:
        """Return JSON-serializable risk state.

        The kill-switch is deliberately persisted: restarting the process must
        never re-enable automation after a drawdown disable.
        """
        return {
            "state": self.state.value,
            "peak_equity": self.peak_equity,
            "day": self._day.isoformat() if self._day is not None else None,
            "session_key": self._session_key,
            "day_start_equity": self._day_start_equity,
            "halt_until": (
                self._halt_until.astimezone(timezone.utc).isoformat()
                if self._halt_until is not None
                else None
            ),
            "warned_drawdown": self._warned_drawdown,
        }

    def restore(self, state: dict) -> None:
        """Restore a state produced by :meth:`snapshot`."""
        self.state = TradingState(state["state"])
        self.peak_equity = float(state["peak_equity"])
        raw_day = state.get("day")
        self._day = date.fromisoformat(raw_day) if raw_day else None
        self._session_key = state.get("session_key")
        self._day_start_equity = float(state["day_start_equity"])
        raw_halt = state.get("halt_until")
        self._halt_until = datetime.fromisoformat(raw_halt) if raw_halt else None
        if self._halt_until is not None and self._halt_until.tzinfo is None:
            self._halt_until = self._halt_until.replace(tzinfo=timezone.utc)
        self._warned_drawdown = bool(state.get("warned_drawdown", False))

    # ---- pre-trade risk validation ------------------------------------
    def pretrade_violations(
        self,
        *,
        equity: float,
        order_notional: float,
        symbol_exposure_after: float,
        gross_exposure_after: float,
        sector_exposure_after: float | None = None,
        order_price: float | None = None,
        reference_price: float | None = None,
    ) -> list[str]:
        """Return every hard-limit violation for a proposed broker order."""
        equity = float(equity)
        if equity <= 0:
            return ["ACCOUNT_EQUITY_UNAVAILABLE"]
        violations: list[str] = []
        order_notional = abs(float(order_notional))
        symbol_exposure_after = abs(float(symbol_exposure_after))
        gross_exposure_after = abs(float(gross_exposure_after))
        if order_notional > self.cfg.max_order_notional_pct * equity:
            violations.append("MAX_ORDER_NOTIONAL")
        if symbol_exposure_after > self.cfg.max_symbol_exposure_pct * equity:
            violations.append("MAX_SYMBOL_EXPOSURE")
        if gross_exposure_after > self.cfg.max_gross_exposure_pct * equity:
            violations.append("MAX_GROSS_EXPOSURE")
        if gross_exposure_after > 0:
            concentration = symbol_exposure_after / gross_exposure_after
            if concentration > self.cfg.max_concentration_pct:
                violations.append("MAX_CONCENTRATION")
        if sector_exposure_after is not None:
            if abs(float(sector_exposure_after)) > self.cfg.max_sector_exposure_pct * equity:
                violations.append("MAX_SECTOR_EXPOSURE")
        if order_price is not None and reference_price is not None:
            order_price = float(order_price)
            reference_price = float(reference_price)
            if reference_price <= 0 or order_price <= 0:
                violations.append("INVALID_PRICE")
            elif abs(order_price / reference_price - 1.0) > self.cfg.price_collar_pct:
                violations.append("PRICE_COLLAR")
        return violations

    # ---- position sizing -----------------------------------------------
    def size_for_trade(
        self,
        equity: float,
        entry_price: float,
        stop_price: float,
        available_notional: float | None = None,
    ) -> float:
        """Return a FRACTIONAL share/contract/coin qty respecting the risk
        budget, the 0.25% hard cap, and leverage=1. Returns 0.0 if not allowed.

        The quantity is intentionally a float, NOT an int: crypto trades in
        fractional units (e.g. 0.03 BTC). Flooring to whole units -- as an
        equities-only sizer would -- makes every high-priced-coin position round
        to zero on a small account, so no trades ever fire. The caller rounds
        this to the instrument's actual size precision via `make_qty`, which is
        what enforces the venue's minimum lot / size increment.

        Sizing logic:
          risk_per_unit = |entry - stop|
          qty_budget = (risk_budget_pct * equity) / risk_per_unit
          qty_cap    = (max_trade_risk_pct * equity) / risk_per_unit
          qty = min(qty_budget, qty_cap)            # 0.25% cap binds
          qty = min(qty, equity / entry_price)      # leverage = 1 (notional cap)

        ``available_notional`` (optional) is the PORTFOLIO-level gross-exposure
        headroom, in quote currency, that the caller still has to spend on THIS
        instrument after accounting for notional already committed elsewhere.
        When provided, the position is additionally capped so its notional never
        consumes more than that headroom. This turns the per-trade leverage=1
        cap above into a BOOK-level leverage=1 cap: summed gross notional across
        all instruments stays <= max_leverage * equity. ``None`` (default)
        leaves the original per-trade-only behaviour untouched.
        """
        if not self.can_open:
            return 0.0
        equity = float(equity)
        risk_per_unit = abs(float(entry_price) - float(stop_price))
        if risk_per_unit <= 0 or entry_price <= 0 or equity <= 0:
            return 0.0

        qty_budget = (self.cfg.risk_budget_pct * equity) / risk_per_unit
        qty_cap = (self.cfg.max_trade_risk_pct * equity) / risk_per_unit
        qty = min(qty_budget, qty_cap)

        # Leverage = 1 (per trade): notional cannot exceed equity.
        max_notional_qty = (self.cfg.max_leverage * equity) / float(entry_price)
        qty = min(qty, max_notional_qty)

        # Leverage = 1 (book level): notional cannot exceed the remaining
        # portfolio gross-exposure headroom. No headroom -> no new size.
        if available_notional is not None:
            headroom_qty = max(float(available_notional), 0.0) / float(entry_price)
            qty = min(qty, headroom_qty)
        return max(float(qty), 0.0)

    # ---- fractional-Kelly (conviction) sizing --------------------------
    def kelly_size_for_trade(
        self,
        equity: float,
        entry_price: float,
        stop_price: float,
        edge: float,
        variance: float,
        kelly_fraction: float,
        available_notional: float | None = None,
    ) -> float:
        """Conviction-scaled qty: fractional Kelly, floored by the hard caps.

        Fractional Kelly turns the alpha's edge into a *target* notional
        fraction of equity:

            f_star = edge / variance          # full-Kelly fraction (Gaussian)
            f      = kelly_fraction * f_star   # fractional Kelly
            f      = clamp(f, 0, kelly_max_fraction)
            kelly_qty = f * equity / entry_price

        where ``edge`` is the MAGNITUDE of the forward-return forecast (|yhat|)
        and ``variance`` is the variance of that same forward return. Direction
        (long/short) is chosen by the caller, so only the magnitude matters here.

        The hard rails stay hard: this returns
            min(kelly_qty, size_for_trade(...))
        so Kelly can only ever size the position DOWN from the existing
        1%-budget / 0.25%-cap / leverage=1 quantity. High conviction saturates
        at the cap; low conviction (or noisy, high-variance) signals size well
        below it. Falls back to the plain risk-cap qty when the Kelly inputs are
        unusable (non-positive edge/variance), never up-sizing past the cap.

        ``available_notional`` is threaded straight into ``size_for_trade`` so
        the portfolio-level gross-exposure headroom bounds the cap too; because
        the result is floored at that cap, Kelly can never breach the book-level
        leverage=1 limit either.
        """
        cap_qty = self.size_for_trade(
            equity, entry_price, stop_price, available_notional=available_notional
        )
        if cap_qty <= 0:
            return 0.0
        edge = float(edge)
        variance = float(variance)
        kelly_fraction = float(kelly_fraction)
        if edge <= 0 or variance <= 0 or kelly_fraction <= 0 or entry_price <= 0:
            # No usable conviction signal -> defer to the hard-cap sizing.
            return cap_qty
        f = kelly_fraction * (edge / variance)
        f = min(max(f, 0.0), self.cfg.kelly_max_fraction)
        kelly_qty = (f * float(equity)) / float(entry_price)
        return max(min(kelly_qty, cap_qty), 0.0)


if __name__ == "__main__":
    rm = RiskManager(5000.0)
    # entry 100, stop 98 -> risk/unit = 2. 0.25% of 5000 = 12.5 -> qty = 6.25
    # (fractional; the instrument's size precision rounds it at order time).
    print("qty:", rm.size_for_trade(5000.0, 100.0, 98.0))
    # Fractional Kelly: strong edge vs low variance -> saturates at the 0.25%
    # cap qty above (Kelly only ever de-risks, never breaches the hard cap).
    print("kelly qty (strong):", rm.kelly_size_for_trade(
        5000.0, 100.0, 98.0, edge=0.02, variance=0.0004, kelly_fraction=0.5))
    # Weak edge / high variance -> f* small -> sizes BELOW the cap (6.25).
    print("kelly qty (weak):", rm.kelly_size_for_trade(
        5000.0, 100.0, 98.0, edge=0.0005, variance=0.01, kelly_fraction=0.5))
    now = datetime(2026, 6, 29, 14, 0, tzinfo=timezone.utc)
    rm.on_new_day(now, 5000.0)
    print("state after 2% loss:", rm.update_equity(now, 4899.0))  # HALTED_DAILY
