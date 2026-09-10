"""Execution-adapter connectivity with a disconnect latch and callback listeners.

A socket reconnect is not reconciliation. Each loss invalidates the generation;
only a successful broker reconciliation of that generation can re-arm entries.
"""
from __future__ import annotations

import time
import weakref
import logging

_LISTENERS: dict[tuple[int, str], list] = {}


def watch(cache, account_id, callback):
    key = (id(cache), str(account_id))
    reference = weakref.WeakMethod(callback)
    _LISTENERS.setdefault(key, []).append(reference)
    def unsubscribe():
        refs = _LISTENERS.get(key, [])
        if reference in refs:
            refs.remove(reference)
        if not refs:
            _LISTENERS.pop(key, None)
    return unsubscribe


def register(client):
    client._quant_connectivity = dict(generation=0, reconciled_generation=None,
                                      event="STARTUP", observed_at=time.time())


def invalidate(client, reason):
    state = getattr(client, "_quant_connectivity", None)
    if state is None:
        register(client)
        state = client._quant_connectivity
    state.update(generation=state["generation"] + 1, reconciled_generation=None,
                 event=str(reason), observed_at=time.time())
    key = (id(client._cache), str(client.account_id))
    for ref in list(_LISTENERS.get(key, ())):
        callback = ref()
        if callback is None:
            _LISTENERS[key].remove(ref)
        else:
            try:
                callback(dict(state))
            except Exception:
                # The generation latch remains closed even if a listener's
                # cancellation/telemetry path fails. Never suppress IB cleanup.
                logging.getLogger(__name__).exception("Broker disconnect listener failed")


def invalidate_ib_client(ib_client, reason):
    from quant.run.account_evidence import _SOURCES
    for ref in list(_SOURCES.values()):
        client = ref()
        if client is not None and client._client is ib_client:
            invalidate(client, reason)


def _source(cache, account_id):
    from quant.run.account_evidence import _SOURCES
    ref = _SOURCES.get((id(cache), str(account_id)))
    return ref() if ref else None


def snapshot(cache, account_id):
    client = _source(cache, account_id)
    result = dict(healthy=False, connected=False, status="UNAVAILABLE", generation=None,
                  event="Execution adapter unavailable", observed_at=None,
                  recovery="Restore the broker connection, then restart for fresh reconciliation.")
    if client is None:
        return result
    state = getattr(client, "_quant_connectivity", {})
    try:
        connected = bool(client._client.is_ready and client._client._eclient.isConnected())
    except Exception:
        connected = False
    # Polling remains a fallback if an adapter version loses a callback.
    if not connected and state.get("reconciled_generation") is not None:
        invalidate(client, "SOCKET_NOT_READY")
        state = client._quant_connectivity
    healthy = connected and state.get("generation") is not None and state.get("reconciled_generation") == state.get("generation")
    return {**result, **state, "connected": connected, "healthy": healthy,
            "status": "RECONCILED" if healthy else "RECONCILIATION_REQUIRED" if connected else "DISCONNECTED"}


def acknowledge_reconciliation(cache, account_id, generation):
    client = _source(cache, account_id)
    current = snapshot(cache, account_id)
    if client is None or not current["connected"] or generation is None or current["generation"] != generation:
        return False
    client._quant_connectivity["reconciled_generation"] = generation
    return True
