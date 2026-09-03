"""Fail-closed IBKR controls for opening and supervising equity shorts."""
from __future__ import annotations

import json
import math
import ssl
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from ibapi.account_summary_tags import AccountSummaryTags
from ibapi.client import EClient
from ibapi.contract import Contract
from ibapi.order import Order
from ibapi.wrapper import EWrapper


_INFORMATIONAL_CODES = frozenset({2104, 2106, 2107, 2108, 2158})
_CONNECTION_LOSS_CODES = frozenset({502, 504, 1100, 1300})
_US_PRIMARY_EXCHANGES = frozenset(
    {
        "AMEX",
        "ARCA",
        "BATS",
        "CBOE",
        "EDGEA",
        "IEX",
        "ISLAND",
        "NASDAQ",
        "NASDAQ.NMS",
        "NYSE",
        "NYSEARCA",
    }
)
_INFRASTRUCTURE_FAILURE_CODES = frozenset(
    {
        "ACCOUNT_MISMATCH",
        "ACCOUNT_EQUITY_UNKNOWN",
        "ACCOUNT_STALE",
        "BORROW_ERROR",
        "BORROW_FEE_STALE",
        "BORROW_STALE",
        "MARGIN_CUSHION",
        "MARGIN_UNAVAILABLE",
    }
)


def _utc_timestamp() -> float:
    return time.time()


def _finite_float(value: Any) -> float | None:
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _portal_number(value: Any) -> float | None:
    text = str(value or "").strip().replace(",", "")
    if not text or text in {"-", "--", "N/A"}:
        return None
    multiplier = 1.0
    if text[-1:].upper() in {"K", "M", "B"}:
        multiplier = {"K": 1_000.0, "M": 1_000_000.0, "B": 1_000_000_000.0}[
            text[-1].upper()
        ]
        text = text[:-1]
    if text.endswith("%"):
        text = text[:-1]
    text = text.lstrip("<>")
    number = _finite_float(text)
    return None if number is None else number * multiplier


@dataclass(frozen=True)
class ShortControlConfig:
    account_id: str
    tickers: tuple[str, ...]
    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 29
    primary_exchange: str = ""
    include_extended_hours: bool = False
    borrow_api_url: str = "ftp://shortstock@ftp2.interactivebrokers.com/usa.txt"
    borrow_api_verify_tls: bool = False
    max_borrow_fee_pct: float = 5.0
    min_margin_cushion_pct: float = 20.0
    locate_buffer_ratio: float = 1.25
    snapshot_max_age_secs: float = 30.0
    account_max_age_secs: float = 190.0
    fee_max_age_secs: float = 1_200.0
    ssr_max_age_secs: float = 7_200.0
    what_if_timeout_secs: float = 8.0
    recall_grace_secs: float = 60.0
    max_commission_pct: float = 1.0

    def __post_init__(self) -> None:
        if not self.account_id:
            raise ValueError("short controls require an IBKR account id")
        if not self.tickers:
            raise ValueError("short controls require at least one equity ticker")
        if self.client_id < 0:
            raise ValueError("short-control client id must be non-negative")
        numeric_controls = (
            self.max_borrow_fee_pct,
            self.min_margin_cushion_pct,
            self.locate_buffer_ratio,
            self.snapshot_max_age_secs,
            self.account_max_age_secs,
            self.fee_max_age_secs,
            self.ssr_max_age_secs,
            self.what_if_timeout_secs,
            self.recall_grace_secs,
            self.max_commission_pct,
        )
        if not all(math.isfinite(float(value)) for value in numeric_controls):
            raise ValueError("short-control thresholds must be finite")
        if not 0 < self.max_borrow_fee_pct <= 100:
            raise ValueError("maximum borrow fee must be between 0 and 100 percent")
        if not 0 < self.min_margin_cushion_pct < 100:
            raise ValueError("minimum margin cushion must be between 0 and 100 percent")
        if not 1 <= self.locate_buffer_ratio <= 10:
            raise ValueError("locate buffer ratio must be between 1 and 10")
        if self.recall_grace_secs > 3_600:
            raise ValueError("short recall grace cannot exceed 3600 seconds")
        if not 0 < self.max_commission_pct <= 10:
            raise ValueError("maximum commission must be between 0 and 10 percent")
        if min(
            self.snapshot_max_age_secs,
            self.account_max_age_secs,
            self.fee_max_age_secs,
            self.ssr_max_age_secs,
            self.what_if_timeout_secs,
            self.recall_grace_secs,
        ) <= 0:
            raise ValueError("short-control timeouts must be positive")
        parsed = urlparse(self.borrow_api_url)
        if parsed.scheme not in {"ftp", "http", "https"} or not parsed.hostname:
            raise ValueError("borrow feed URL must be an FTP or HTTP(S) URL")
        if parsed.scheme == "ftp" and parsed.hostname not in {
            "ftp2.interactivebrokers.com",
            "ftp3.interactivebrokers.com",
        }:
            raise ValueError("FTP borrow feeds must use an Interactive Brokers host")
        if parsed.scheme in {"http", "https"} and not self.borrow_api_verify_tls and parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError("TLS verification may be disabled only for a loopback borrow API")


@dataclass
class BorrowSnapshot:
    symbol: str
    con_id: int | None = None
    observed_at: float = 0.0
    fee_observed_at: float = 0.0
    shortable_tier: float | None = None
    shortable_shares: float | None = None
    borrow_fee_pct: float | None = None
    shortable_label: str = ""
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    prior_close: float | None = None
    session_low: float | None = None
    halted: bool | None = None
    primary_exchange: str = ""
    prior_session_ssr_triggered: bool | None = None
    ssr_history_observed_at: float = 0.0
    error: str = ""

    @property
    def current_session_ssr_triggered(self) -> bool | None:
        if not self.prior_close or self.prior_close <= 0:
            return None
        threshold = self.prior_close * 0.90
        if self.last and self.last > 0 and self.last <= threshold:
            return True
        if self.session_low and self.session_low > 0:
            return self.session_low <= threshold
        return None

    def ssr_active_at(self, now: float, max_history_age_secs: float) -> bool | None:
        current_triggered = self.current_session_ssr_triggered
        history_fresh = (
            self.ssr_history_observed_at > 0
            and now - self.ssr_history_observed_at <= max_history_age_secs
        )
        if current_triggered is True:
            return True
        if history_fresh and self.prior_session_ssr_triggered is True:
            return True
        if current_triggered is None or not history_fresh:
            return None
        return False

    @property
    def ssr_active(self) -> bool | None:
        return self.ssr_active_at(_utc_timestamp(), math.inf)


@dataclass
class AccountSnapshot:
    account_id: str
    observed_at: float = 0.0
    values: dict[str, float | str] = field(default_factory=dict)


@dataclass(frozen=True)
class WhatIfResult:
    allowed: bool
    status: str
    reason: str = ""
    init_margin_change: float | None = None
    maint_margin_change: float | None = None
    init_margin_after: float | None = None
    maint_margin_after: float | None = None
    equity_with_loan_after: float | None = None
    commission: float | None = None


@dataclass(frozen=True)
class ShortControlDecision:
    allowed: bool
    code: str
    reason: str
    limit_price: float | None = None
    ssr_active: bool | None = None
    snapshot: dict[str, Any] = field(default_factory=dict)
    what_if: dict[str, Any] = field(default_factory=dict)


def evaluate_short_snapshot(
    config: ShortControlConfig,
    borrow: BorrowSnapshot | None,
    account: AccountSnapshot | None,
    *,
    quantity: float,
    reference_price: float,
    now: float | None = None,
) -> ShortControlDecision:
    now = _utc_timestamp() if now is None else float(now)
    if (
        not math.isfinite(float(quantity))
        or float(quantity) < 0
        or not math.isfinite(float(reference_price))
        or float(reference_price) <= 0
    ):
        return ShortControlDecision(False, "INVALID_REQUEST", "Short quantity or price is invalid.")
    if borrow is None:
        return ShortControlDecision(False, "BORROW_UNAVAILABLE", "No IBKR borrow snapshot is available.")
    if now - borrow.observed_at > config.snapshot_max_age_secs:
        return ShortControlDecision(False, "BORROW_STALE", "IBKR shortability data is stale.")
    if borrow.error:
        return ShortControlDecision(False, "BORROW_ERROR", borrow.error)
    if borrow.halted is not False:
        return ShortControlDecision(False, "MARKET_HALTED_UNKNOWN", "The equity halt state is not clear.")
    ssr_active = borrow.ssr_active_at(now, config.ssr_max_age_secs)
    if ssr_active is None:
        return ShortControlDecision(
            False,
            "SSR_STATE_UNKNOWN",
            "Rule 201 state cannot be derived from current and prior-session prices.",
        )
    if borrow.shortable_tier is None or borrow.shortable_tier <= 2.5:
        return ShortControlDecision(
            False,
            "LOCATE_NOT_CONFIRMED",
            "IBKR has not confirmed immediately available short inventory.",
        )
    required_shares = float(quantity) * config.locate_buffer_ratio
    if (
        borrow.shortable_shares is None
        or borrow.shortable_shares <= 0
        or borrow.shortable_shares < required_shares
    ):
        return ShortControlDecision(
            False,
            "BORROW_INSUFFICIENT",
            f"Short inventory is below the {config.locate_buffer_ratio:.2f}x locate buffer.",
        )
    if borrow.borrow_fee_pct is None or now - borrow.fee_observed_at > config.fee_max_age_secs:
        return ShortControlDecision(False, "BORROW_FEE_STALE", "The IBKR borrow fee is unavailable or stale.")
    if borrow.borrow_fee_pct > config.max_borrow_fee_pct:
        return ShortControlDecision(
            False,
            "BORROW_FEE_LIMIT",
            f"Borrow fee {borrow.borrow_fee_pct:.2f}% exceeds the configured maximum.",
        )
    if account is None or now - account.observed_at > config.account_max_age_secs:
        return ShortControlDecision(False, "ACCOUNT_STALE", "IBKR margin data is unavailable or stale.")
    if account.account_id != config.account_id:
        return ShortControlDecision(False, "ACCOUNT_MISMATCH", "Short-control account does not match execution.")
    cushion = _finite_float(account.values.get("Cushion"))
    if cushion is None or cushion * 100.0 < config.min_margin_cushion_pct:
        return ShortControlDecision(
            False,
            "MARGIN_CUSHION",
            "Account margin cushion is below the configured minimum.",
        )
    available = _finite_float(account.values.get("AvailableFunds"))
    excess = _finite_float(account.values.get("ExcessLiquidity"))
    buying_power = _finite_float(account.values.get("BuyingPower"))
    notional = float(quantity) * float(reference_price)
    if any(value is None or value <= 0 for value in (available, excess, buying_power)):
        return ShortControlDecision(False, "MARGIN_UNAVAILABLE", "Authoritative margin balances are unavailable.")
    if buying_power < notional:
        return ShortControlDecision(False, "BUYING_POWER", "Buying power is below the proposed short notional.")
    day_trades = _finite_float(account.values.get("DayTradesRemaining"))
    net_liquidation = _finite_float(account.values.get("NetLiquidation"))
    if net_liquidation is None:
        return ShortControlDecision(False, "ACCOUNT_EQUITY_UNKNOWN", "Net liquidation value is unavailable.")
    if net_liquidation < 25_000:
        if day_trades is None:
            return ShortControlDecision(False, "PDT_UNKNOWN", "Pattern-day-trader capacity is unavailable.")
        if day_trades == 0:
            return ShortControlDecision(False, "PDT_LIMIT", "No pattern-day-trader capacity remains.")
    limit_price = None
    if ssr_active:
        if (
            borrow.bid
            and borrow.bid > 0
            and borrow.ask
            and borrow.ask > borrow.bid
        ):
            limit_price = borrow.ask
        else:
            return ShortControlDecision(
                False,
                "SSR_QUOTE_UNAVAILABLE",
                "Rule 201 is active and no unlocked current NBBO quote is available.",
                ssr_active=True,
            )
    return ShortControlDecision(
        True,
        "SNAPSHOT_APPROVED",
        "Borrow, fee, margin, PDT, halt, and Rule 201 snapshot checks passed.",
        limit_price=limit_price,
        ssr_active=ssr_active,
        snapshot={
            "shortable_shares": borrow.shortable_shares,
            "borrow_fee_pct": borrow.borrow_fee_pct,
            "shortable_tier": borrow.shortable_tier,
            "margin_cushion_pct": cushion * 100.0,
            "available_funds": available,
            "excess_liquidity": excess,
            "buying_power": buying_power,
        },
    )


class _ShortProbe(EWrapper, EClient):
    def __init__(self, owner: "IBKRShortControlService") -> None:
        EWrapper.__init__(self)
        EClient.__init__(self, self)
        self.owner = owner

    def nextValidId(self, orderId: int) -> None:  # noqa: N802
        self.owner._set_next_order_id(orderId)

    def managedAccounts(self, accountsList: str) -> None:  # noqa: N802
        self.owner._set_accounts(accountsList)

    def contractDetails(self, reqId, contractDetails) -> None:  # noqa: N802
        self.owner._contract_details(reqId, contractDetails)

    def contractDetailsEnd(self, reqId) -> None:  # noqa: N802
        self.owner._contract_details_end(reqId)

    def tickPrice(self, reqId, tickType, price, attrib) -> None:  # noqa: N802
        self.owner._tick(reqId, tickType, price)

    def tickSize(self, reqId, tickType, size) -> None:  # noqa: N802
        self.owner._tick(reqId, tickType, size)

    def tickGeneric(self, reqId, tickType, value) -> None:  # noqa: N802
        self.owner._tick(reqId, tickType, value)

    def accountSummary(self, reqId, account, tag, value, currency) -> None:  # noqa: N802
        self.owner._account_summary(account, tag, value)

    def historicalData(self, reqId, bar) -> None:  # noqa: N802
        self.owner._historical_bar(reqId, bar)

    def historicalDataEnd(self, reqId, start, end) -> None:  # noqa: N802
        self.owner._historical_end(reqId)

    def openOrder(self, orderId, contract, order, orderState) -> None:  # noqa: N802
        self.owner._what_if_result(orderId, orderState)

    def orderStatus(self, orderId, status, *args) -> None:  # noqa: N802
        self.owner._what_if_status(orderId, status)

    def error(self, reqId, *args) -> None:
        if len(args) >= 4:
            _, code, message, _ = args[:4]
        elif len(args) >= 2:
            code, message = args[:2]
        else:
            return
        self.owner._error(int(reqId), int(code), str(message))

    def connectionClosed(self) -> None:  # noqa: N802
        self.owner._connection_closed()


class IBKRShortControlService:
    """Separate TWS client for shortability, margin, what-if, and borrow fees."""

    def __init__(self, config: ShortControlConfig) -> None:
        self.config = config
        self._lock = threading.RLock()
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._probe = _ShortProbe(self)
        self._socket_thread: threading.Thread | None = None
        self._fee_thread: threading.Thread | None = None
        self._next_order_id: int | None = None
        self._accounts: set[str] = set()
        self._account = AccountSnapshot(config.account_id)
        self._borrow = {ticker: BorrowSnapshot(ticker) for ticker in config.tickers}
        self._contract_requests: dict[int, str] = {}
        self._market_requests: dict[int, str] = {}
        self._history_requests: dict[int, str] = {}
        self._history_rows: dict[int, list[tuple[str, float, float]]] = {}
        self._next_history_request_id = 9_500
        self._last_history_refresh = 0.0
        self._contracts: dict[str, Contract] = {}
        self._qualification_events: dict[str, threading.Event] = {
            ticker: threading.Event() for ticker in config.tickers
        }
        self._what_if_events: dict[int, threading.Event] = {}
        self._what_if_results: dict[int, WhatIfResult] = {}
        self._started_at = 0.0
        self._last_error = ""

    def start(self) -> None:
        self._started_at = _utc_timestamp()
        self._probe.connect(self.config.host, self.config.port, self.config.client_id)
        self._socket_thread = threading.Thread(
            target=self._probe.run,
            name="ibkr-short-controls",
            daemon=True,
        )
        self._socket_thread.start()
        if not self._ready.wait(timeout=10):
            self.stop()
            raise RuntimeError("IBKR short-control handshake timed out")
        if self.config.account_id not in self._accounts:
            self.stop()
            raise RuntimeError("IBKR short-control account was not found in managed accounts")
        self._probe.reqAccountSummary(9_100, "All", AccountSummaryTags.AllTags)
        for offset, ticker in enumerate(self.config.tickers):
            request_id = 9_200 + offset
            contract = Contract()
            contract.symbol = ticker
            contract.secType = "STK"
            contract.exchange = "SMART"
            contract.currency = "USD"
            if self.config.primary_exchange:
                contract.primaryExchange = self.config.primary_exchange
            self._contract_requests[request_id] = ticker
            self._probe.reqContractDetails(request_id, contract)
        deadline = time.monotonic() + 12
        for ticker, event in self._qualification_events.items():
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not event.wait(remaining) or ticker not in self._contracts:
                self.stop()
                raise RuntimeError(f"IBKR short controls could not qualify {ticker}")
        self._fee_thread = threading.Thread(
            target=self._fee_loop,
            name="ibkr-borrow-fees",
            daemon=True,
        )
        self._fee_thread.start()

    def stop(self) -> None:
        self._stop.set()
        for request_id in tuple(self._market_requests):
            try:
                self._probe.cancelMktData(request_id)
            except Exception:
                pass
        try:
            self._probe.cancelAccountSummary(9_100)
        except Exception:
            pass
        try:
            self._probe.disconnect()
        except Exception:
            pass
        for thread in (self._fee_thread, self._socket_thread):
            if thread and thread.is_alive():
                thread.join(timeout=3)

    def preflight(
        self,
        symbol: str,
        *,
        quantity: float,
        reference_price: float,
        proposed_limit_price: float | None,
    ) -> ShortControlDecision:
        normalized = symbol.upper()
        if (
            not math.isfinite(float(quantity))
            or float(quantity) <= 0
            or not math.isfinite(float(reference_price))
            or float(reference_price) <= 0
            or (
                proposed_limit_price is not None
                and (
                    not math.isfinite(float(proposed_limit_price))
                    or float(proposed_limit_price) <= 0
                )
            )
        ):
            return ShortControlDecision(
                False,
                "INVALID_REQUEST",
                "Short quantity, reference price, or limit price is invalid.",
            )
        with self._lock:
            borrow = self._copy_borrow(normalized)
            account = AccountSnapshot(
                self._account.account_id,
                self._account.observed_at,
                dict(self._account.values),
            )
            connected = self._probe.isConnected()
        if not connected:
            return ShortControlDecision(False, "SHORT_CONTROL_DISCONNECTED", "IBKR short controls are disconnected.")
        decision = evaluate_short_snapshot(
            self.config,
            borrow,
            account,
            quantity=quantity,
            reference_price=reference_price,
        )
        if not decision.allowed:
            return decision
        limit_price = max(
            value
            for value in (proposed_limit_price, decision.limit_price, reference_price)
            if value is not None
        )
        what_if = self._request_what_if(normalized, quantity, limit_price)
        if not what_if.allowed:
            return ShortControlDecision(
                False,
                "BROKER_WHAT_IF_REJECTED",
                what_if.reason or f"IBKR what-if returned {what_if.status or 'no status'}.",
                limit_price=limit_price,
                ssr_active=decision.ssr_active,
                snapshot=decision.snapshot,
                what_if=asdict(what_if),
            )
        post_equity = what_if.equity_with_loan_after
        post_maintenance = what_if.maint_margin_after
        if post_equity is None or post_maintenance is None or post_equity <= 0:
            return ShortControlDecision(
                False,
                "BROKER_MARGIN_UNKNOWN",
                "IBKR what-if omitted projected maintenance margin.",
                limit_price=limit_price,
                ssr_active=decision.ssr_active,
                snapshot=decision.snapshot,
                what_if=asdict(what_if),
            )
        projected_cushion_pct = (post_equity - post_maintenance) / post_equity * 100.0
        if projected_cushion_pct < self.config.min_margin_cushion_pct:
            return ShortControlDecision(
                False,
                "BROKER_MARGIN_CUSHION",
                "IBKR what-if projects a margin cushion below the configured minimum.",
                limit_price=limit_price,
                ssr_active=decision.ssr_active,
                snapshot={**decision.snapshot, "projected_margin_cushion_pct": projected_cushion_pct},
                what_if=asdict(what_if),
            )
        notional = quantity * limit_price
        if (
            what_if.commission is None
            or notional <= 0
            or what_if.commission / notional * 100.0 > self.config.max_commission_pct
        ):
            return ShortControlDecision(
                False,
                "BROKER_COMMISSION_LIMIT",
                "IBKR what-if commission is unavailable or above the configured limit.",
                limit_price=limit_price,
                ssr_active=decision.ssr_active,
                snapshot=decision.snapshot,
                what_if=asdict(what_if),
            )
        return ShortControlDecision(
            True,
            "SHORT_APPROVED",
            "IBKR borrow, fee, Rule 201, account, and what-if checks passed.",
            limit_price=limit_price,
            ssr_active=decision.ssr_active,
            snapshot=decision.snapshot,
            what_if=asdict(what_if),
        )

    def supervise(self, symbol: str, *, quantity: float = 0.0) -> ShortControlDecision:
        with self._lock:
            borrow = self._copy_borrow(symbol.upper())
            account = AccountSnapshot(
                self._account.account_id,
                self._account.observed_at,
                dict(self._account.values),
            )
            connected = self._probe.isConnected()
        if not connected:
            return ShortControlDecision(
                False,
                "SHORT_CONTROL_DISCONNECTED",
                "IBKR short controls are disconnected.",
            )
        decision = evaluate_short_snapshot(
            self.config,
            borrow,
            account,
            quantity=quantity,
            reference_price=1.0,
        )
        if decision.code in {"BUYING_POWER", "PDT_LIMIT"}:
            return ShortControlDecision(True, "SHORT_SUPERVISION_HEALTHY", "Existing short remains supervised.")
        return decision

    def snapshot(self) -> dict[str, Any]:
        now = _utc_timestamp()
        with self._lock:
            symbols = {}
            healthy = self._probe.isConnected()
            for ticker in self.config.tickers:
                borrow = self._copy_borrow(ticker)
                decision = evaluate_short_snapshot(
                    self.config,
                    borrow,
                    self._account,
                    quantity=0.0,
                    reference_price=1.0,
                    now=now,
                )
                symbols[ticker] = {
                    "state": "READY" if decision.allowed else "BLOCKED",
                    "code": decision.code,
                    "reason": decision.reason,
                    "shortable_shares": borrow.shortable_shares if borrow else None,
                    "borrow_fee_pct": borrow.borrow_fee_pct if borrow else None,
                    "shortable_tier": borrow.shortable_tier if borrow else None,
                    "ssr_active": (
                        borrow.ssr_active_at(now, self.config.ssr_max_age_secs)
                        if borrow
                        else None
                    ),
                    "halted": borrow.halted if borrow else None,
                    "age_seconds": max(now - borrow.observed_at, 0.0) if borrow else None,
                    "fee_age_seconds": max(now - borrow.fee_observed_at, 0.0) if borrow else None,
                }
                healthy = healthy and decision.code not in _INFRASTRUCTURE_FAILURE_CODES
            cushion = _finite_float(self._account.values.get("Cushion"))
            return {
                "enabled": True,
                "healthy": healthy,
                "state": "READY" if healthy else "BLOCKED",
                "account_id": "<redacted-account>",
                "margin_cushion_pct": cushion * 100.0 if cushion is not None else None,
                "max_borrow_fee_pct": self.config.max_borrow_fee_pct,
                "min_margin_cushion_pct": self.config.min_margin_cushion_pct,
                "locate_buffer_ratio": self.config.locate_buffer_ratio,
                "recall_grace_secs": self.config.recall_grace_secs,
                "last_error": self._last_error or None,
                "symbols": symbols,
            }

    def _copy_borrow(self, symbol: str) -> BorrowSnapshot | None:
        current = self._borrow.get(symbol)
        return BorrowSnapshot(**asdict(current)) if current is not None else None

    def _set_next_order_id(self, order_id: int) -> None:
        with self._lock:
            self._next_order_id = max(order_id, self._next_order_id or order_id)
            if self._accounts:
                self._ready.set()

    def _set_accounts(self, accounts: str) -> None:
        with self._lock:
            self._accounts = {value for value in accounts.split(",") if value}
            if self._next_order_id is not None:
                self._ready.set()

    def _contract_details(self, request_id: int, details) -> None:
        with self._lock:
            ticker = self._contract_requests.get(request_id)
            contract = details.contract
            if (
                ticker
                and str(contract.symbol).upper() == ticker
                and contract.secType == "STK"
                and contract.currency == "USD"
                and str(contract.primaryExchange or "").upper() in _US_PRIMARY_EXCHANGES
            ):
                self._contracts[ticker] = contract
                snapshot = self._borrow[ticker]
                snapshot.con_id = int(contract.conId)
                snapshot.primary_exchange = str(contract.primaryExchange or "")

    def _contract_details_end(self, request_id: int) -> None:
        with self._lock:
            ticker = self._contract_requests.pop(request_id, None)
            contract = self._contracts.get(ticker or "")
            if ticker and contract is not None:
                market_request_id = 9_300 + len(self._market_requests)
                self._market_requests[market_request_id] = ticker
                self._probe.reqMktData(market_request_id, contract, "165,236", False, False, [])
            if ticker:
                self._qualification_events[ticker].set()

    def _tick(self, request_id: int, tick_type: int, value: Any) -> None:
        number = _finite_float(value)
        if number is None:
            return
        with self._lock:
            ticker = self._market_requests.get(request_id)
            if ticker is None:
                return
            item = self._borrow[ticker]
            if tick_type == 46:
                item.shortable_tier = number
            elif tick_type == 89:
                item.shortable_shares = max(number, 0.0)
            elif tick_type == 1:
                item.bid = number if number > 0 else item.bid
            elif tick_type == 2:
                item.ask = number if number > 0 else item.ask
            elif tick_type == 4:
                item.last = number if number > 0 else item.last
            elif tick_type == 9:
                item.prior_close = number if number > 0 else item.prior_close
            elif tick_type == 7:
                item.session_low = number if number > 0 else item.session_low
            elif tick_type == 49:
                item.halted = None if number < 0 else number > 0
            item.observed_at = _utc_timestamp()

    def _refresh_ssr_history(self) -> None:
        with self._lock:
            if self._history_requests:
                return
            contracts = tuple(self._contracts.items())
            self._last_history_refresh = _utc_timestamp()
            requests = []
            for ticker, contract in contracts:
                request_id = self._next_history_request_id
                self._next_history_request_id += 1
                self._history_requests[request_id] = ticker
                self._history_rows[request_id] = []
                requests.append((request_id, contract))
        for request_id, contract in requests:
            try:
                self._probe.reqHistoricalData(
                    request_id,
                    contract,
                    "",
                    "5 D",
                    "1 day",
                    "TRADES",
                    1,
                    2,
                    False,
                    [],
                )
            except Exception as exc:
                with self._lock:
                    ticker = self._history_requests.pop(request_id, None)
                    self._history_rows.pop(request_id, None)
                    if ticker:
                        self._borrow[ticker].prior_session_ssr_triggered = None
                        self._borrow[ticker].ssr_history_observed_at = 0.0
                    self._last_error = f"IBKR Rule 201 history unavailable: {exc}"

    def _historical_bar(self, request_id: int, bar) -> None:
        low = _finite_float(getattr(bar, "low", None))
        close = _finite_float(getattr(bar, "close", None))
        if low is None or close is None or low <= 0 or close <= 0:
            return
        with self._lock:
            rows = self._history_rows.get(request_id)
            if rows is not None:
                rows.append((str(getattr(bar, "date", "")), low, close))

    @staticmethod
    def _historical_date(raw: str):
        value = str(raw).strip().split()[0]
        try:
            return datetime.strptime(value, "%Y%m%d").date()
        except ValueError:
            return None

    def _historical_end(self, request_id: int) -> None:
        with self._lock:
            ticker = self._history_requests.pop(request_id, None)
            rows = self._history_rows.pop(request_id, [])
            if ticker is None:
                return
            today = datetime.now(ZoneInfo("America/New_York")).date()
            completed = sorted(
                (
                    (session_date, low, close)
                    for raw_date, low, close in rows
                    if (session_date := self._historical_date(raw_date)) is not None
                    and session_date < today
                ),
                key=lambda value: value[0],
            )
            item = self._borrow[ticker]
            item.ssr_history_observed_at = _utc_timestamp()
            if len(completed) < 2:
                item.prior_session_ssr_triggered = None
                return
            previous_session = completed[-1]
            preceding_session = completed[-2]
            item.prior_session_ssr_triggered = (
                previous_session[1] <= preceding_session[2] * 0.90
            )

    def _account_summary(self, account: str, tag: str, value: str) -> None:
        if account != self.config.account_id:
            return
        parsed: float | str = _finite_float(value)
        if parsed is None:
            parsed = str(value)
        with self._lock:
            self._account.values[str(tag)] = parsed
            self._account.observed_at = _utc_timestamp()

    def _next_what_if_id(self) -> int:
        with self._lock:
            if self._next_order_id is None:
                raise RuntimeError("IBKR did not provide a next valid order id")
            order_id = self._next_order_id
            self._next_order_id += 1
            self._what_if_events[order_id] = threading.Event()
            return order_id

    def _request_what_if(self, symbol: str, quantity: float, limit_price: float) -> WhatIfResult:
        contract = self._contracts.get(symbol)
        if contract is None:
            return WhatIfResult(False, "Unavailable", "Qualified IBKR contract is unavailable.")
        order_id = self._next_what_if_id()
        order = Order()
        order.action = "SELL"
        order.totalQuantity = Decimal(str(quantity))
        order.orderType = "LMT"
        order.lmtPrice = float(limit_price)
        order.tif = "DAY"
        order.account = self.config.account_id
        order.outsideRth = self.config.include_extended_hours
        order.whatIf = True
        order.transmit = True
        order.orderRef = f"QSHORT-WHATIF-{order_id}"
        try:
            self._probe.placeOrder(order_id, contract, order)
            event = self._what_if_events[order_id]
            if not event.wait(self.config.what_if_timeout_secs):
                return WhatIfResult(False, "Timeout", "IBKR what-if margin check timed out.")
            return self._what_if_results.get(
                order_id,
                WhatIfResult(False, "Unavailable", "IBKR what-if returned no result."),
            )
        except Exception as exc:
            return WhatIfResult(False, "Error", f"IBKR what-if failed: {exc}")
        finally:
            with self._lock:
                self._what_if_events.pop(order_id, None)
                self._what_if_results.pop(order_id, None)

    def _what_if_result(self, order_id: int, state) -> None:
        with self._lock:
            if order_id not in self._what_if_events:
                return
            status = str(getattr(state, "status", ""))
            warning = str(getattr(state, "warningText", "") or "").strip()
            equity_after = _finite_float(getattr(state, "equityWithLoanAfter", None))
            maint_after = _finite_float(getattr(state, "maintMarginAfter", None))
            init_after = _finite_float(getattr(state, "initMarginAfter", None))
            allowed = status in {"PreSubmitted", "Submitted"} and not warning
            if equity_after is None or maint_after is None or init_after is None:
                allowed = False
                warning = warning or "IBKR what-if omitted authoritative post-trade margin values."
            elif min(equity_after - maint_after, equity_after - init_after) <= 0:
                allowed = False
                warning = warning or "The proposed short would exhaust the account margin buffer."
            self._what_if_results[order_id] = WhatIfResult(
                allowed,
                status,
                warning,
                _finite_float(getattr(state, "initMarginChange", None)),
                _finite_float(getattr(state, "maintMarginChange", None)),
                init_after,
                maint_after,
                equity_after,
                _finite_float(getattr(state, "commission", None)),
            )
            self._what_if_events[order_id].set()

    def _what_if_status(self, order_id: int, status: str) -> None:
        if status not in {"Inactive", "Cancelled", "ApiCancelled"}:
            return
        with self._lock:
            event = self._what_if_events.get(order_id)
            if event and not event.is_set():
                self._what_if_results[order_id] = WhatIfResult(
                    False,
                    status,
                    f"IBKR what-if order became {status}.",
                )
                event.set()

    def _what_if_error(self, request_id: int, code: int, message: str) -> bool:
        with self._lock:
            event = self._what_if_events.get(request_id)
            if event is None:
                return False
            self._what_if_results[request_id] = WhatIfResult(
                False,
                "Rejected",
                f"IBKR {code}: {message}",
            )
            event.set()
            return True

    def _error(self, request_id: int, code: int, message: str) -> None:
        if code in _INFORMATIONAL_CODES:
            return
        if self._what_if_error(request_id, code, message):
            return
        with self._lock:
            self._last_error = f"IBKR {code}: {message}"
            history_ticker = self._history_requests.pop(request_id, None)
            if history_ticker:
                self._history_rows.pop(request_id, None)
                item = self._borrow[history_ticker]
                item.prior_session_ssr_triggered = None
                item.ssr_history_observed_at = 0.0
                return
            ticker = self._contract_requests.get(request_id) or self._market_requests.get(request_id)
            if ticker:
                item = self._borrow[ticker]
                item.error = self._last_error
                item.observed_at = _utc_timestamp()
                self._qualification_events[ticker].set()
        if code in _CONNECTION_LOSS_CODES:
            self._ready.set()

    def _connection_closed(self) -> None:
        with self._lock:
            self._last_error = "IBKR short-control socket closed"

    def _fee_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._refresh_borrow_fees()
                if _utc_timestamp() - self._last_history_refresh >= 900:
                    self._refresh_ssr_history()
            except Exception as exc:
                with self._lock:
                    self._last_error = f"Short-control refresh failed: {exc}"
            self._stop.wait(60)

    def _refresh_borrow_fees(self) -> None:
        with self._lock:
            conids = {ticker: item.con_id for ticker, item in self._borrow.items() if item.con_id}
        if not conids:
            return
        parsed = urlparse(self.config.borrow_api_url)
        if parsed.scheme == "ftp":
            self._refresh_borrow_fees_from_ftp(conids)
            return
        base = self.config.borrow_api_url.rstrip("/")
        query = urlencode({"conids": ",".join(str(value) for value in conids.values()), "fields": "7636,7637,7644"})
        request = Request(
            f"{base}/iserver/marketdata/snapshot?{query}",
            headers={"Accept": "application/json", "User-Agent": "quant-short-controls/1"},
        )
        context = None
        if request.full_url.startswith("https://") and not self.config.borrow_api_verify_tls:
            context = ssl._create_unverified_context()
        try:
            with urlopen(request, timeout=5, context=context) as response:
                payload = json.load(response)
            if not isinstance(payload, list):
                raise ValueError("borrow API snapshot was not a list")
            by_conid = {int(row.get("conid")): row for row in payload if isinstance(row, dict) and row.get("conid")}
            observed_at = _utc_timestamp()
            with self._lock:
                for ticker, conid in conids.items():
                    row = by_conid.get(int(conid))
                    if not row:
                        continue
                    item = self._borrow[ticker]
                    fee = _portal_number(row.get("7637"))
                    shares = _portal_number(row.get("7636"))
                    if fee is not None:
                        item.borrow_fee_pct = fee
                        item.fee_observed_at = observed_at
                    if shares is not None:
                        item.shortable_shares = max(shares, 0.0)
                    item.shortable_label = str(row.get("7644") or "")
                    item.error = ""
        except Exception as exc:
            with self._lock:
                self._last_error = f"Borrow fee feed unavailable: {exc}"

    def _refresh_borrow_fees_from_ftp(self, conids: dict[str, int]) -> None:
        request = Request(
            self.config.borrow_api_url,
            headers={"User-Agent": "quant-short-controls/1"},
        )
        try:
            with urlopen(request, timeout=10) as response:
                text = response.read().decode("utf-8", errors="replace")
            rows: dict[int, dict[str, str]] = {}
            header: list[str] | None = None
            for raw_line in text.splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#BOF") or line.startswith("#EOF"):
                    continue
                parts = line.split("|")
                if line.startswith("#SYM"):
                    header = [value.lstrip("#") for value in parts]
                    continue
                if header is None:
                    continue
                row = dict(zip(header, parts, strict=False))
                con_id = _finite_float(row.get("CON"))
                if con_id is not None:
                    rows[int(con_id)] = row
            observed_at = _utc_timestamp()
            with self._lock:
                for ticker, conid in conids.items():
                    item = self._borrow[ticker]
                    row = rows.get(int(conid))
                    item.fee_observed_at = observed_at
                    if row is None:
                        item.borrow_fee_pct = None
                        item.shortable_shares = 0.0
                        continue
                    item.borrow_fee_pct = _portal_number(row.get("FEERATE"))
                    shares = _portal_number(row.get("AVAILABLE"))
                    if shares is not None:
                        item.shortable_shares = max(shares, 0.0)
                    item.error = ""
        except Exception as exc:
            with self._lock:
                self._last_error = f"Borrow fee feed unavailable: {exc}"


__all__ = [
    "AccountSnapshot",
    "BorrowSnapshot",
    "IBKRShortControlService",
    "ShortControlConfig",
    "ShortControlDecision",
    "WhatIfResult",
    "evaluate_short_snapshot",
]
