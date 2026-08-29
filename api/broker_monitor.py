"""Read-only Interactive Brokers account and live-bar monitor.

This deliberately uses a separate client id from paper/live TradingNode jobs.
It performs the IB API handshake, monitors account health, and maintains a
small LRU set of ``keepUpToDate`` historical-bar subscriptions for dashboard
charts. It never submits or modifies orders.
"""
from __future__ import annotations

import math
import os
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone

from ibapi.client import EClient
from ibapi.contract import Contract
from ibapi.wrapper import EWrapper


_BAR_HOURS = {1, 2, 3, 4, 8, 24}
_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.-]{0,14}$")
_MAX_BAR_SUBSCRIPTIONS = 8
_MAX_BARS = 750


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_symbol(symbol: str) -> str:
    normalized = str(symbol or "").strip().upper()
    if not _SYMBOL_PATTERN.fullmatch(normalized):
        raise ValueError("ticker must contain 1-15 letters, numbers, dots, or hyphens")
    return normalized


def _bar_contract(symbol: str, asset_class: str) -> Contract:
    contract = Contract()
    contract.symbol = symbol
    contract.currency = "USD"
    if asset_class == "crypto":
        contract.secType = "CRYPTO"
        contract.exchange = "ZEROHASH"
    elif asset_class == "equity":
        contract.secType = "STK"
        contract.exchange = "SMART"
    else:
        raise ValueError("asset_class must be 'crypto' or 'equity'")
    return contract


def _bar_request_settings(asset_class: str, bar_hours: int) -> tuple[str, str, str, int]:
    hours = int(bar_hours)
    if hours not in _BAR_HOURS:
        raise ValueError("bar_hours must be one of 1, 2, 3, 4, 8, or 24")
    bar_size = "1 day" if hours == 24 else f"{hours} hour" + ("" if hours == 1 else "s")
    duration = "1 Y" if hours == 24 else "30 D"
    what_to_show = "MIDPOINT" if asset_class == "crypto" else "TRADES"
    return bar_size, duration, what_to_show, hours


def _bar_timestamp(value) -> str:
    text = str(value).strip()
    if text.isdigit() and len(text) == 8:
        parsed = datetime.strptime(text, "%Y%m%d").replace(tzinfo=timezone.utc)
    elif text.isdigit():
        parsed = datetime.fromtimestamp(int(text), tz=timezone.utc)
    else:
        normalized = text.replace("  ", " ")
        try:
            parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        except ValueError:
            parsed = datetime.strptime(normalized[:17], "%Y%m%d %H:%M:%S").replace(
                tzinfo=timezone.utc,
            )
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _bar_session_date(value) -> str | None:
    """Return IB's exchange-session date without applying a timezone shift."""
    text = str(value).strip()
    if not (text.isdigit() and len(text) == 8):
        return None
    try:
        return datetime.strptime(text, "%Y%m%d").date().isoformat()
    except ValueError:
        return None


def _finite_number(value, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


class _Probe(EWrapper, EClient):
    def __init__(self, monitor: "BrokerMonitor") -> None:
        EWrapper.__init__(self)
        EClient.__init__(self, self)
        self.monitor = monitor
        self.ready = threading.Event()

    def nextValidId(self, orderId: int) -> None:  # noqa: N802 - IB API callback
        self.ready.set()

    def managedAccounts(self, accountsList: str) -> None:  # noqa: N802 - IB API callback
        self.monitor.set_accounts([item for item in accountsList.split(",") if item])
        self.ready.set()

    def accountSummary(self, reqId, account, tag, value, currency) -> None:  # noqa: N802
        self.monitor.set_account_value(account, tag, value, currency)

    def updateAccountValue(self, key, value, currency, accountName) -> None:  # noqa: N802
        self.monitor.set_account_value(accountName, key, value, currency)

    def error(self, reqId, *args) -> None:
        # ibapi uses the legacy (reqId, code, message, json) callback for
        # older servers and the protobuf (reqId, errorTime, code, message,
        # json) callback for newer Gateway builds.
        if len(args) >= 4:
            _, errorCode, errorString, _ = args[:4]
        elif len(args) >= 2:
            errorCode, errorString = args[:2]
        else:
            return
        if self.monitor.set_request_error(reqId, errorCode, errorString):
            return
        # Informational IB messages are not connection loss.
        if errorCode not in {2104, 2106, 2107, 2158}:
            self.monitor.set_error(f"IBKR {errorCode}: {errorString}")
        # A reachable socket can still reject the API handshake. Wake the
        # connection attempt immediately so auto-discovery can try the next
        # configured Gateway/TWS port instead of waiting for the timeout.
        if errorCode in {502, 326}:
            self.ready.set()

    def connectionClosed(self) -> None:  # noqa: N802 - IB API callback
        self.monitor.set_disconnected("IBKR socket closed")

    def contractDetails(self, reqId, contractDetails) -> None:  # noqa: N802
        self.monitor.add_contract_details(reqId, contractDetails)

    def contractDetailsEnd(self, reqId) -> None:  # noqa: N802
        self.monitor.complete_contract_details(reqId)

    def historicalData(self, reqId, bar) -> None:  # noqa: N802
        self.monitor.receive_historical_bar(reqId, bar, forming=False)

    def historicalDataUpdate(self, reqId, bar) -> None:  # noqa: N802
        self.monitor.receive_historical_bar(reqId, bar, forming=True)

    def historicalDataEnd(self, reqId, start, end) -> None:  # noqa: N802
        self.monitor.complete_historical_backfill(reqId)


class BrokerMonitor:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._probe: _Probe | None = None
        self._next_request_id = 8100
        self._bar_subscriptions: dict[str, dict] = {}
        self._contract_requests: dict[int, str] = {}
        self._bar_requests: dict[int, str] = {}
        self._accounts: list[str] = []
        self._account_values: dict[str, dict[str, dict[str, str]]] = {}
        explicit_port = os.environ.get("IBKR_PORT", os.environ.get("TWS_PORT"))
        self._auto_discover = explicit_port is None
        self._auto_ports = [7497, 4002, 7496, 4001]
        self._auto_index = 0
        self._config = {
            "host": os.environ.get("IBKR_HOST", os.environ.get("TWS_HOST", "127.0.0.1")),
            "port": int(explicit_port or "7497"),
            # Keep the health probe separate from the configured TradingNode id.
            # IBKR installations commonly restrict client IDs to 0-31. Keep
            # the monitor on 31 by default, separate from the app's usual 1.
            "client_id": int(os.environ.get("IBKR_MONITOR_CLIENT_ID", "31")),
            "account_id": os.environ.get("TWS_ACCOUNT", ""),
            "mode": "paper",
        }
        self._state = {
            "status": "connecting",
            "message": "Starting IBKR monitor",
            "last_connected_at": None,
            "last_error": None,
            "attempts": 0,
        }

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="ibkr-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._disconnect()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    def configure(self, payload: dict) -> dict:
        self._auto_discover = False
        with self._lock:
            for key in ("host", "port", "account_id", "mode"):
                if key in payload and payload[key] is not None:
                    self._config[key] = payload[key]
            self._config["port"] = int(self._config["port"])
        self._set_state("connecting", "Reconnecting to IBKR with updated settings")
        self._disconnect()
        self._wake.set()
        return self.status()

    def set_accounts(self, accounts: list[str]) -> None:
        with self._lock:
            self._accounts = accounts

    def set_account_value(self, account: str, tag: str, value: str, currency: str) -> None:
        with self._lock:
            self._account_values.setdefault(account, {})[tag] = {
                "value": value,
                "currency": currency,
            }

    def set_error(self, message: str) -> None:
        with self._lock:
            self._state["last_error"] = message

    def set_request_error(self, req_id: int, code: int, message: str) -> bool:
        with self._lock:
            key = self._contract_requests.pop(req_id, None)
            if key is None:
                key = self._bar_requests.pop(req_id, None)
            if key is None:
                return False
            entry = self._bar_subscriptions.get(key)
            if entry is not None:
                entry["status"] = "error"
                entry["error"] = f"IBKR {code}: {message}"
                entry["updated_at"] = _now()
            return True

    def set_disconnected(self, message: str) -> None:
        if not self._stop.is_set():
            self._set_state("disconnected", message)

    def _set_state(self, status: str, message: str) -> None:
        with self._lock:
            self._state["status"] = status
            self._state["message"] = message

    def _disconnect(self) -> None:
        probe = self._probe
        self._probe = None
        with self._lock:
            bar_request_ids = list(self._bar_requests)
            self._bar_requests.clear()
            self._contract_requests.clear()
            for entry in self._bar_subscriptions.values():
                if entry["status"] not in {"error", "idle"}:
                    entry["status"] = "stale"
                    entry["error"] = "IBKR connection unavailable; showing the last received bars"
        if probe is not None:
            for req_id in bar_request_ids:
                try:
                    probe.cancelHistoricalData(req_id)
                except Exception:  # noqa: BLE001 - disconnect must continue
                    pass
            try:
                probe.disconnect()
            except Exception:  # noqa: BLE001 - cleanup must continue
                pass

    def _request_id_locked(self) -> int:
        request_id = self._next_request_id
        self._next_request_id += 1
        return request_id

    @staticmethod
    def _subscription_key(
        symbol: str,
        asset_class: str,
        bar_hours: int,
        include_extended_hours: bool,
    ) -> str:
        return ":".join(
            (
                asset_class,
                symbol,
                str(bar_hours),
                "extended" if include_extended_hours else "rth",
            )
        )

    def subscribe_bars(
        self,
        symbol: str,
        *,
        asset_class: str,
        bar_hours: int,
        include_extended_hours: bool = False,
    ) -> dict:
        normalized = _normalize_symbol(symbol)
        bar_size, duration, what_to_show, hours = _bar_request_settings(
            asset_class,
            bar_hours,
        )
        if asset_class != "equity" and include_extended_hours:
            raise ValueError("extended-hours selection applies only to equities")
        key = self._subscription_key(
            normalized,
            asset_class,
            hours,
            include_extended_hours,
        )
        evicted_request_id = None
        with self._lock:
            entry = self._bar_subscriptions.get(key)
            probe = self._probe
            connected = bool(probe is not None and probe.isConnected())
            if entry is None:
                if len(self._bar_subscriptions) >= _MAX_BAR_SUBSCRIPTIONS:
                    old_key = min(
                        self._bar_subscriptions,
                        key=lambda item: self._bar_subscriptions[item]["last_accessed"],
                    )
                    old_entry = self._bar_subscriptions.pop(old_key)
                    evicted_request_id = old_entry.get("bar_request_id")
                    if evicted_request_id is not None:
                        self._bar_requests.pop(evicted_request_id, None)
                entry = {
                    "key": key,
                    "symbol": normalized,
                    "asset_class": asset_class,
                    "bar_hours": hours,
                    "bar_size": bar_size,
                    "duration": duration,
                    "what_to_show": what_to_show,
                    "include_extended_hours": bool(include_extended_hours),
                    "use_rth": asset_class == "equity" and not include_extended_hours,
                    "status": "idle",
                    "error": None,
                    "bars": OrderedDict(),
                    "created_at": _now(),
                    "updated_at": None,
                    "last_accessed": time.monotonic(),
                    "contract_candidates": [],
                    "qualified_contract": None,
                    "contract_request_id": None,
                    "bar_request_id": None,
                }
                self._bar_subscriptions[key] = entry
            else:
                entry["last_accessed"] = time.monotonic()
            should_start = connected and entry["status"] in {"idle", "error", "stale"}
            if not connected and not entry["bars"]:
                entry["status"] = "disconnected"
                entry["error"] = "IB Gateway or TWS is not connected"

        if evicted_request_id is not None and probe is not None:
            try:
                probe.cancelHistoricalData(evicted_request_id)
            except Exception:  # noqa: BLE001 - subscription replacement continues
                pass
        if should_start:
            self._begin_contract_resolution(key, probe)
        return self.bar_snapshot(
            normalized,
            asset_class=asset_class,
            bar_hours=hours,
            include_extended_hours=include_extended_hours,
        )

    def _begin_contract_resolution(self, key: str, probe: _Probe) -> None:
        with self._lock:
            entry = self._bar_subscriptions.get(key)
            if entry is None:
                return
            request_id = self._request_id_locked()
            entry["status"] = "qualifying"
            entry["error"] = None
            entry["contract_candidates"] = []
            entry["contract_request_id"] = request_id
            self._contract_requests[request_id] = key
            contract = _bar_contract(entry["symbol"], entry["asset_class"])
        try:
            probe.reqContractDetails(request_id, contract)
        except Exception as exc:  # noqa: BLE001 - report through snapshot
            self.set_request_error(request_id, 0, str(exc))

    def add_contract_details(self, request_id: int, details) -> None:
        with self._lock:
            key = self._contract_requests.get(request_id)
            entry = self._bar_subscriptions.get(key) if key is not None else None
            if entry is not None:
                entry["contract_candidates"].append(details)

    def complete_contract_details(self, request_id: int) -> None:
        with self._lock:
            key = self._contract_requests.pop(request_id, None)
            entry = self._bar_subscriptions.get(key) if key is not None else None
            if entry is None:
                return
            candidates = entry.pop("contract_candidates", [])
            expected_type = "CRYPTO" if entry["asset_class"] == "crypto" else "STK"
            exact = [
                item
                for item in candidates
                if str(getattr(item.contract, "symbol", "")).upper() == entry["symbol"]
                and getattr(item.contract, "secType", "") == expected_type
                and getattr(item.contract, "currency", "USD") == "USD"
            ]
            if not exact:
                entry["status"] = "error"
                entry["error"] = f"No IBKR {expected_type} contract matched {entry['symbol']}"
                entry["updated_at"] = _now()
                return
            details = exact[0]
            contract = details.contract
            entry["qualified_contract"] = contract
            entry["primary_exchange"] = getattr(contract, "primaryExchange", "")
            bar_request_id = self._request_id_locked()
            entry["bar_request_id"] = bar_request_id
            entry["status"] = "backfilling"
            entry["error"] = None
            self._bar_requests[bar_request_id] = key
            probe = self._probe
            request = (
                bar_request_id,
                contract,
                "",
                entry["duration"],
                entry["bar_size"],
                entry["what_to_show"],
                int(entry["use_rth"]),
                2,
                True,
                [],
            )
        if probe is None or not probe.isConnected():
            self.set_request_error(bar_request_id, 0, "IBKR disconnected during contract qualification")
            return
        try:
            probe.reqHistoricalData(*request)
        except Exception as exc:  # noqa: BLE001 - report through snapshot
            self.set_request_error(bar_request_id, 0, str(exc))

    def receive_historical_bar(self, request_id: int, bar, *, forming: bool) -> None:
        try:
            timestamp = _bar_timestamp(bar.date)
        except (TypeError, ValueError, OverflowError):
            return
        point = {
            "ts": timestamp,
            "open": _finite_number(bar.open),
            "high": _finite_number(bar.high),
            "low": _finite_number(bar.low),
            "close": _finite_number(bar.close),
            "volume": max(0.0, _finite_number(bar.volume)),
            "complete": not forming,
        }
        session_date = _bar_session_date(bar.date)
        if session_date is not None:
            point["session_date"] = session_date
        with self._lock:
            key = self._bar_requests.get(request_id)
            entry = self._bar_subscriptions.get(key) if key is not None else None
            if entry is None:
                return
            bars = entry["bars"]
            if forming and bars and timestamp not in bars:
                last_key = next(reversed(bars))
                bars[last_key]["complete"] = True
            bars[timestamp] = point
            bars.move_to_end(timestamp)
            while len(bars) > _MAX_BARS:
                bars.popitem(last=False)
            entry["status"] = "streaming" if forming else entry["status"]
            entry["error"] = None
            entry["updated_at"] = _now()

    def complete_historical_backfill(self, request_id: int) -> None:
        with self._lock:
            key = self._bar_requests.get(request_id)
            entry = self._bar_subscriptions.get(key) if key is not None else None
            if entry is not None:
                entry["status"] = "streaming"
                entry["error"] = None
                entry["updated_at"] = _now()

    def _restart_bar_subscriptions(self, probe: _Probe) -> None:
        with self._lock:
            keys = list(self._bar_subscriptions)
            self._contract_requests.clear()
            self._bar_requests.clear()
            for entry in self._bar_subscriptions.values():
                entry["status"] = "reconnecting"
                entry["bar_request_id"] = None
                entry["contract_request_id"] = None
        for key in keys:
            self._begin_contract_resolution(key, probe)

    def bar_snapshot(
        self,
        symbol: str,
        *,
        asset_class: str,
        bar_hours: int,
        include_extended_hours: bool = False,
    ) -> dict:
        normalized = _normalize_symbol(symbol)
        _, _, _, hours = _bar_request_settings(asset_class, bar_hours)
        key = self._subscription_key(
            normalized,
            asset_class,
            hours,
            include_extended_hours,
        )
        with self._lock:
            entry = self._bar_subscriptions.get(key)
            if entry is None:
                return {
                    "source": "ib_gateway",
                    "status": "not_subscribed",
                    "symbol": normalized,
                    "asset_class": asset_class,
                    "bar_hours": hours,
                    "bars": [],
                    "as_of": None,
                    "error": None,
                }
            entry["last_accessed"] = time.monotonic()
            return {
                "source": "ib_gateway",
                "status": entry["status"],
                "symbol": entry["symbol"],
                "asset_class": entry["asset_class"],
                "bar_hours": entry["bar_hours"],
                "bar_size": entry["bar_size"],
                "what_to_show": entry["what_to_show"],
                "include_extended_hours": entry["include_extended_hours"],
                "session_scope": "rth" if entry["use_rth"] else "all_hours",
                "price_adjustment": (
                    "split_adjusted_dividend_unadjusted"
                    if entry["asset_class"] == "equity"
                    else "not_applicable"
                ),
                "primary_exchange": entry.get("primary_exchange", ""),
                "bars": list(entry["bars"].values()),
                "as_of": entry["updated_at"],
                "error": entry["error"],
            }

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                config = dict(self._config)
                self._state["attempts"] += 1
            candidate_port = config["port"]
            self._set_state("connecting", f"Connecting to {config['host']}:{config['port']}")
            with self._lock:
                self._state["last_error"] = None
            probe = _Probe(self)
            self._probe = probe
            try:
                probe.connect(config["host"], candidate_port, self._config["client_id"])
                reader = threading.Thread(target=probe.run, name="ibkr-monitor-reader", daemon=True)
                reader.start()
                if not probe.ready.wait(timeout=12):
                    raise TimeoutError("IBKR handshake timed out")
                with self._lock:
                    handshake_error = self._state["last_error"]
                if handshake_error and any(code in handshake_error for code in ("IBKR 502:", "IBKR 326:")):
                    raise ConnectionError(handshake_error)
                probe.reqAccountSummary(
                    7001,
                    "All",
                    "AccountType,TotalCashValue,AvailableFunds,NetLiquidation,BuyingPower",
                )
                account = self._accounts[0] if self._accounts else ""
                if account:
                    probe.reqAccountUpdates(True, account)
                with self._lock:
                    expected = config.get("account_id")
                    if expected and self._accounts and expected not in self._accounts:
                        self._state["status"] = "error"
                        self._state["message"] = f"Connected, but account {expected} was not reported"
                    else:
                        self._state["status"] = "connected"
                        self._state["message"] = "IBKR API connected"
                        self._state["last_connected_at"] = _now()
                        self._state["last_error"] = None
                        self._config["port"] = candidate_port
                self._restart_bar_subscriptions(probe)
                while not self._stop.is_set() and probe.isConnected():
                    self._wake.wait(timeout=2)
                    self._wake.clear()
                    if self._wake.is_set():
                        break
            except Exception as exc:  # noqa: BLE001 - monitor must reconnect
                self._set_state("disconnected", str(exc))
                with self._lock:
                    if not self._state["last_error"]:
                        self._state["last_error"] = str(exc)
                if self._auto_discover:
                    self._auto_index = (self._auto_index + 1) % len(self._auto_ports)
                    self._config["port"] = self._auto_ports[self._auto_index]
            finally:
                try:
                    probe.cancelAccountSummary(7001)
                except Exception:  # noqa: BLE001 - cleanup must continue
                    pass
                if self._accounts:
                    try:
                        probe.reqAccountUpdates(False, self._accounts[0])
                    except Exception:  # noqa: BLE001 - cleanup must continue
                        pass
                self._disconnect()
            self._wake.wait(timeout=5)
            self._wake.clear()

    def status(self) -> dict:
        with self._lock:
            account_id = self._accounts[0] if self._accounts else None
            values = self._account_values.get(account_id or "", {})

            def value_for(tag: str):
                item = values.get(tag)
                if not item or item["value"] in {"", "N/A"}:
                    return None
                try:
                    return float(item["value"])
                except (TypeError, ValueError):
                    return None

            return {
                **self._state,
                "config": dict(self._config),
                "accounts": list(self._accounts),
                "account": {
                    "id": account_id,
                    "cash": value_for("TotalCashValue"),
                    "available_funds": value_for("AvailableFunds"),
                    "net_liquidation": value_for("NetLiquidation"),
                    "buying_power": value_for("BuyingPower"),
                    "currency": values.get("TotalCashValue", {}).get("currency", "USD"),
                },
                "read_only": True,
            }
