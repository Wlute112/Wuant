"""Account/currency-specific IBKR cash evidence; never restored from persistence."""
from __future__ import annotations

import math
import threading
import time
import weakref
from decimal import Decimal, InvalidOperation

# Account-summary subscriptions update on IBKR's three-minute cadence.
MAX_AGE_SECONDS = 240.0
SOURCE = "IBKR accountSummary SettledCash"


class SettledCashEvidence:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._values: dict[tuple[str, str], tuple[str, float]] = {}

    def clear(self) -> None:
        with self._lock:
            self._values.clear()

    def observe(self, account: str, value: str, currency: str, *, now=None) -> None:
        with self._lock:
            self._values[(account, currency)] = (str(value), time.time() if now is None else now)

    def snapshot(self, account: str, currency: str, *, connected: bool, now=None) -> dict:
        now = time.time() if now is None else now
        with self._lock:
            reading = self._values.get((account, currency))
        result = dict(value=None, currency=currency, source=SOURCE, status="UNAVAILABLE",
                      observed_at=None, age_seconds=None, max_age_seconds=MAX_AGE_SECONDS)
        if not connected:
            result["status"] = "DISCONNECTED"
            return result
        if reading is None:
            return result
        value, observed = reading
        age = now - observed
        result.update(observed_at=observed, age_seconds=age)
        try:
            amount = Decimal(value)
            valid = amount.is_finite() and abs(amount) < Decimal("1e100")
        except (InvalidOperation, ValueError):
            valid = False
        if not valid or not math.isfinite(age) or age < 0:
            result["status"] = "INVALID"
        elif age > MAX_AGE_SECONDS:
            result["status"] = "STALE"
        else:
            result.update(value=str(amount), status="CURRENT")
        return result


# Weak references prevent retaining disposed execution clients. Keys include the
# cache identity so two nodes connected to the same account cannot share evidence.
_SOURCES: dict[tuple[int, str], weakref.ReferenceType] = {}


def register_account_source(client) -> None:
    from quant.run.broker_connectivity import register
    register(client)
    client._quant_settled_cash = SettledCashEvidence()
    _SOURCES[(id(client._cache), str(client.account_id))] = weakref.ref(client)


def invalidate_account_sources(ib_client) -> None:
    for key, ref in list(_SOURCES.items()):
        client = ref()
        if client is None:
            _SOURCES.pop(key, None)
        elif client._client is ib_client:
            client._quant_settled_cash.clear()


def settled_cash_from_cache(cache, account_id: str, currency: str = "USD", *, now=None) -> dict:
    ref = _SOURCES.get((id(cache), account_id))
    client = ref() if ref else None
    if client is None:
        return SettledCashEvidence().snapshot(account_id, currency, connected=True, now=now)
    connected = bool(client._client.is_ready and client._client._eclient.isConnected())
    if not connected:
        client._quant_settled_cash.clear()
    return client._quant_settled_cash.snapshot(account_id, currency, connected=connected, now=now)
