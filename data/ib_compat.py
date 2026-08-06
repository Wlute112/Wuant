"""Zero Hash crypto compatibility shim for the nautilus_trader 1.229 IB adapter.

Why this exists
---------------
IBKR migrated US spot-crypto trading from **Paxos** to **Zero Hash**. The
nautilus_trader 1.229 Interactive Brokers adapter, however, still hardcodes
``PAXOS`` as the *only* recognised crypto venue in three places:

  1. ``parsing.instruments.VENUES_CRYPTO`` / ``EXCHANGES_BY_SEC_TYPE["CRYPTO"]``
     -- used by ``_decode_crypto_contract`` to turn an instrument id like
     ``BTC/USD.<VENUE>`` into an ``IBContract``. Any non-PAXOS crypto venue
     decodes to ``None`` and the instrument fails to load (breaks run_live).
  2. ``determine_venue_from_contract`` -- resolves the venue from the exchange.
  3. ``parsing.data.what_to_show`` -- IBKR historical crypto bars must be
     requested with ``whatToShow=AGGTRADES`` for LAST-price bars, but the
     adapter only emits AGGTRADES when the venue is exactly ``PAXOS``. For any
     other venue a LAST request degrades to ``TRADES``, which IBKR **rejects**
     for crypto ("No historical market data ... TRADES").

Result: naively swapping the exchange string to ``ZEROHASH`` makes the adapter
silently mis-handle crypto. This module registers ``ZEROHASH`` as a first-class
crypto venue at runtime so both the historical fetch and the live node work.

``register_zerohash_crypto()`` is idempotent -- call it once before any adapter
use (import, client construction, node build).
"""
from __future__ import annotations

import asyncio

CRYPTO_VENUE = "ZEROHASH"

# Track whether we've already patched so re-invocation is a no-op.
_REGISTERED = False
_EXECUTION_FIX_REGISTERED = False


def _restore_zerohash_base_quantity(ib_order, order, instrument) -> None:
    """Keep base-coin quantities on non-quote Zero Hash orders.

    Nautilus 1.229 models IBKR crypto as an inverse perpetual and therefore
    rewrites every crypto order into ``cashQty``. That is only valid when the
    Nautilus order explicitly carries a quote-currency quantity (used for a
    market BUY). Limit orders and market SELLs carry fractional base-coin
    quantities and must remain in IBKR's ``totalQuantity`` field.
    """
    if (
        str(order.instrument_id.venue) != CRYPTO_VENUE
        or not getattr(instrument, "is_inverse", False)
    ):
        return

    if order.is_quote_quantity:
        # IBKR's cashQty is a floating-point USD amount. Nautilus 1.229 casts
        # it to int, silently shaving off cents (or reducing a sub-$1 order to
        # zero), so restore the exact requested quote quantity.
        ib_order.cashQty = order.quantity.as_double()
        return

    from ibapi.const import UNSET_DOUBLE

    ib_order.cashQty = UNSET_DOUBLE
    ib_order.totalQuantity = order.quantity.as_decimal()


def register_ibkr_execution_fixes() -> None:
    """Apply IBKR execution-client fixes needed by every asset class."""
    global _EXECUTION_FIX_REGISTERED
    if _EXECUTION_FIX_REGISTERED:
        return

    from nautilus_trader.adapters.interactive_brokers import execution as _execution

    # The adapter treats connectivity-farm messages (for example 2104) as
    # connection-ready before TWS has necessarily delivered managedAccounts.
    # On a warm Redis cache, instrument initialization can otherwise race the
    # account callback and fault reconciliation.
    exec_cls = _execution.InteractiveBrokersExecutionClient
    if not getattr(exec_cls, "_quant_waits_for_managed_accounts", False):
        _orig_connect = exec_cls._connect

        async def _connect_after_managed_accounts(self):
            loop = asyncio.get_running_loop()
            deadline = loop.time() + min(float(self._connection_timeout), 15.0)
            while not self._client.accounts() and loop.time() < deadline:
                await asyncio.sleep(0.05)
            return await _orig_connect(self)

        exec_cls._connect = _connect_after_managed_accounts
        exec_cls._quant_waits_for_managed_accounts = True

    # The adapter marks spot crypto as ``is_inverse=True`` and consequently
    # converts every order quantity to IBKR ``cashQty``. For the strategy's
    # normal fractional-coin LIMIT orders this both changes the unit from coin
    # to USD and truncates sub-one quantities to zero. Preserve base quantity
    # unless the order explicitly opts into quote quantity.
    if not getattr(exec_cls, "_quant_preserves_crypto_base_quantity", False):
        _orig_transform_order = exec_cls._transform_order_to_ib_order

        def _transform_order_with_crypto_quantity(self, order, params=None):
            ib_order = _orig_transform_order(self, order, params)
            instrument = self.instrument_provider.find(order.instrument_id)
            if instrument is not None:
                _restore_zerohash_base_quantity(ib_order, order, instrument)
            return ib_order

        exec_cls._transform_order_to_ib_order = _transform_order_with_crypto_quantity
        exec_cls._quant_preserves_crypto_base_quantity = True

    _EXECUTION_FIX_REGISTERED = True


def register_zerohash_crypto(venue: str = CRYPTO_VENUE) -> None:
    """Teach the nautilus 1.229 IB adapter that ``venue`` is a crypto venue.

    Adds ``venue`` alongside ``PAXOS`` in the adapter's crypto-venue tables and
    patches ``what_to_show`` so LAST-price crypto bars request ``AGGTRADES``
    (the value IBKR requires) instead of ``TRADES`` (which IBKR rejects).

    Safe to call multiple times.
    """
    global _REGISTERED
    if _REGISTERED:
        return

    from nautilus_trader.adapters.interactive_brokers.client import market_data as _md
    from nautilus_trader.adapters.interactive_brokers.parsing import data as _data
    from nautilus_trader.adapters.interactive_brokers.parsing import instruments as _instr
    from nautilus_trader.model.enums import PriceType

    # (1) Register the venue so exchange_supports_sec_type / _decode_crypto_contract
    #     and determine_venue_from_contract accept it.
    if venue not in _instr.VENUES_CRYPTO:
        _instr.VENUES_CRYPTO.append(venue)
    _instr.EXCHANGES_BY_SEC_TYPE["CRYPTO"] = frozenset(_instr.VENUES_CRYPTO)

    # (2) Patch what_to_show so LAST crypto bars map to AGGTRADES for our venue
    #     too. market_data.py binds the name at import time, so we must patch the
    #     binding in *that* module (not only in parsing.data) for it to take
    #     effect on real bar requests.
    _crypto_venues = {"PAXOS", venue}
    _orig_what_to_show = _data.what_to_show

    def _what_to_show(bar_type):
        if (
            str(bar_type.instrument_id.venue) in _crypto_venues
            and bar_type.spec.price_type == PriceType.LAST
        ):
            return "AGGTRADES"
        return _orig_what_to_show(bar_type)

    _data.what_to_show = _what_to_show
    _md.what_to_show = _what_to_show

    # (3) Apply the account-callback race fix shared by crypto and equities.
    register_ibkr_execution_fixes()

    _REGISTERED = True


def crypto_instrument_id(base: str, quote: str = "USD", venue: str = CRYPTO_VENUE) -> str:
    """Build the nautilus instrument-id string for a spot-crypto pair.

    The adapter's crypto regex (``RE_CRYPTO``) requires the ``BASE/QUOTE`` symbol
    form, e.g. ``BTC/USD.ZEROHASH`` -- *not* ``BTC.ZEROHASH``.
    """
    return f"{base.upper()}/{quote.upper()}.{venue}"
