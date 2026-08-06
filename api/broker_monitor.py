"""Read-only Interactive Brokers API connection monitor.

This deliberately uses a separate client id from paper/live TradingNode jobs.
It performs the IB API handshake and waits for managedAccounts, but never
subscribes to market data or submits orders.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone

from ibapi.client import EClient
from ibapi.wrapper import EWrapper


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


class BrokerMonitor:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._probe: _Probe | None = None
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
        if probe is not None:
            try:
                probe.disconnect()
            except Exception:  # noqa: BLE001 - cleanup must continue
                pass

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
