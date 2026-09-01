"""Normalize a reconciled Nautilus cache into broker-source-of-truth records."""
from __future__ import annotations

from decimal import Decimal

from nautilus_trader.model.events import OrderFilled

from quant.run.reconciliation import (
    BrokerAccount,
    BrokerExecution,
    BrokerOrder,
    BrokerPosition,
    BrokerSnapshot,
)
from quant.strategies.execution_state import ExecutionLedger, OrderRole


def _money_value(value) -> Decimal:
    if value is None:
        return Decimal("0")
    return Decimal(str(value.as_double()))


def snapshot_from_nautilus_cache(
    cache,
    ledger: ExecutionLedger,
    *,
    strategy_id,
    expected_account_id: str,
    captured_at_ns: int,
) -> BrokerSnapshot:
    """Capture the post-engine-reconciliation account cache without filtering.

    No strategy filter is applied: manual TWS positions, orders from other API
    client IDs, and other strategy IDs must be visible to the safety check.
    Nautilus's live execution engine supplies the snapshot-end synchronization;
    this function is called only after that engine has started the strategy.
    """
    accounts = list(cache.accounts())
    selected = next((item for item in accounts if str(item.id) == expected_account_id), None)
    if selected is None and len(accounts) == 1:
        selected = accounts[0]
    base_currency = str(getattr(selected, "base_currency", "") or "")
    total = selected.balance_total() if selected is not None else None
    free = selected.balance_free() if selected is not None else None
    broker_account = BrokerAccount.normalized(
        account_id=str(selected.id) if selected is not None else "",
        base_currency=base_currency,
        equity=_money_value(total),
        available_funds=_money_value(free),
        # Nautilus 1.229 exposes IBKR FullAvailableFunds as the free balance but
        # does not retain a separate BuyingPower field in Account. Use the same
        # conservative immediately available amount; settled cash remains absent.
        buying_power=_money_value(free),
        settled_cash=None,
        snapshot_complete=selected is not None,
    )
    positions = tuple(
        BrokerPosition.normalized(
            str(position.instrument_id),
            position.signed_qty,
            getattr(position, "avg_px_open", 0),
            str(getattr(position, "account_id", "")),
        )
        for position in cache.positions_open()
    )
    orders: list[BrokerOrder] = []
    executions: list[BrokerExecution] = []
    for order in cache.orders():
        order_id = str(order.client_order_id)
        local = ledger.orders.get(order_id)
        venue_order_id = str(getattr(order, "venue_order_id", "") or "")
        permanent_order_id = ""
        if "-" in venue_order_id:
            candidate = venue_order_id.rsplit("-", 1)[-1]
            if candidate.isdigit() and candidate != "0":
                permanent_order_id = candidate
        orders.append(
            BrokerOrder.normalized(
                client_order_id=order_id,
                instrument_id=str(order.instrument_id),
                side=order.side.name,
                quantity=order.quantity.as_double(),
                filled_quantity=order.filled_qty.as_double(),
                status=order.status.name,
                venue_order_id=venue_order_id,
                permanent_order_id=permanent_order_id,
                owned_by_strategy=str(order.strategy_id) == str(strategy_id),
                role=local.role if local is not None else OrderRole.UNKNOWN,
            )
        )
        for event in order.events:
            if not isinstance(event, OrderFilled):
                continue
            info = getattr(event, "info", None) or {}
            executions.append(
                BrokerExecution.normalized(
                    execution_id=str(event.trade_id),
                    client_order_id=order_id,
                    instrument_id=str(event.instrument_id),
                    side=event.order_side.name,
                    quantity=event.last_qty.as_double(),
                    price=event.last_px.as_double(),
                    ts_ns=int(event.ts_event),
                    correction_of=str(info.get("correction_of", "")),
                )
            )
    return BrokerSnapshot(
        account=broker_account,
        positions=positions,
        orders=tuple(orders),
        executions=tuple(executions),
        positions_complete=True,
        orders_complete=True,
        executions_complete=True,
        captured_at_ns=int(captured_at_ns),
    )
