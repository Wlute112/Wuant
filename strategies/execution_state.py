"""Deterministic order, fill, position, and execution-safety state.

The objects in this module have no Nautilus or IBKR dependency.  Broker events
are normalized at the strategy boundary and applied here, which makes duplicate
callbacks, partial fills, corrections, cancel/fill races, persistence, and
replay independently testable.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Iterable


ZERO = Decimal("0")


def _decimal(value) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"invalid decimal value {value!r}") from exc
    if not result.is_finite():
        raise ValueError(f"non-finite decimal value {value!r}")
    return result


class OrderRole(str, Enum):
    ENTRY = "ENTRY"
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    SIGNAL_EXIT = "SIGNAL_EXIT"
    EMERGENCY_EXIT = "EMERGENCY_EXIT"
    UNKNOWN = "UNKNOWN"


class LifecycleStatus(str, Enum):
    INITIALIZED = "INITIALIZED"
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    PENDING_CANCEL = "PENDING_CANCEL"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    DENIED = "DENIED"


TERMINAL_ORDER_STATES = frozenset(
    {
        LifecycleStatus.FILLED,
        LifecycleStatus.CANCELED,
        LifecycleStatus.EXPIRED,
        LifecycleStatus.REJECTED,
        LifecycleStatus.DENIED,
    }
)


class ExecutionSafetyState(str, Enum):
    ACTIVE = "ACTIVE"
    FROZEN = "FROZEN"
    SUSPENDED = "SUSPENDED"
    EMERGENCY = "EMERGENCY"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    UNCERTAIN = "UNCERTAIN"


@dataclass(frozen=True)
class FillRecord:
    execution_id: str
    client_order_id: str
    instrument_id: str
    side: str
    quantity: Decimal
    price: Decimal
    ts_ns: int
    sequence: int
    event_id: str = ""
    correction_of: str = ""

    @property
    def signed_quantity(self) -> Decimal:
        return self.quantity if self.side == "BUY" else -self.quantity


@dataclass
class OrderRecord:
    client_order_id: str
    instrument_id: str
    side: str
    requested_quantity: Decimal
    role: OrderRole = OrderRole.UNKNOWN
    status: LifecycleStatus = LifecycleStatus.INITIALIZED
    venue_order_id: str = ""
    permanent_order_id: str = ""
    submitted_ts_ns: int = 0
    updated_ts_ns: int = 0
    signal_version: str = ""
    rejection_reason: str = ""
    fill_ids: list[str] = field(default_factory=list)
    filled_quantity: Decimal = ZERO
    average_fill_price: Decimal = ZERO
    _pre_cancel_status: LifecycleStatus | None = field(default=None, repr=False)

    @property
    def remaining_quantity(self) -> Decimal:
        return max(self.requested_quantity - self.filled_quantity, ZERO)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_ORDER_STATES

    @property
    def is_working(self) -> bool:
        return self.status in {
            LifecycleStatus.SUBMITTED,
            LifecycleStatus.ACKNOWLEDGED,
            LifecycleStatus.PARTIALLY_FILLED,
            LifecycleStatus.PENDING_CANCEL,
        }


@dataclass(frozen=True)
class PositionState:
    instrument_id: str
    quantity: Decimal = ZERO
    average_entry_price: Decimal = ZERO
    realized_pnl: Decimal = ZERO


@dataclass(frozen=True)
class OperatorAlert:
    sequence: int
    severity: str
    code: str
    message: str
    ts_ns: int
    client_order_id: str = ""
    instrument_id: str = ""


class ExecutionLedger:
    """Idempotent materialized view of broker order and execution events."""

    def __init__(self) -> None:
        self.orders: dict[str, OrderRecord] = {}
        self.fills: dict[str, FillRecord] = {}
        self.positions: dict[str, PositionState] = {}
        self._event_ids: set[str] = set()
        self._sequence = 0

    def register_order(
        self,
        *,
        client_order_id: str,
        instrument_id: str,
        side: str,
        requested_quantity,
        role: OrderRole = OrderRole.UNKNOWN,
        signal_version: str = "",
        ts_ns: int = 0,
    ) -> OrderRecord:
        order_id = str(client_order_id)
        quantity = _decimal(requested_quantity)
        if quantity <= ZERO:
            raise ValueError("requested quantity must be positive")
        normalized_side = str(side).upper()
        if normalized_side not in {"BUY", "SELL"}:
            raise ValueError(f"invalid order side {side!r}")
        existing = self.orders.get(order_id)
        if existing is not None:
            identity = (
                existing.instrument_id,
                existing.side,
                existing.requested_quantity,
                existing.role,
            )
            incoming = (str(instrument_id), normalized_side, quantity, role)
            if identity != incoming:
                raise ValueError(f"conflicting duplicate order registration: {order_id}")
            return existing
        record = OrderRecord(
            client_order_id=order_id,
            instrument_id=str(instrument_id),
            side=normalized_side,
            requested_quantity=quantity,
            role=role,
            signal_version=str(signal_version),
            submitted_ts_ns=int(ts_ns),
            updated_ts_ns=int(ts_ns),
        )
        self.orders[order_id] = record
        return record

    def apply_order_state(
        self,
        client_order_id: str,
        status: LifecycleStatus,
        *,
        event_id: str = "",
        ts_ns: int = 0,
        venue_order_id: str = "",
        permanent_order_id: str = "",
        reason: str = "",
    ) -> bool:
        if event_id and event_id in self._event_ids:
            return False
        order = self.orders.get(str(client_order_id))
        if order is None:
            raise KeyError(f"unknown client order id {client_order_id!r}")
        if event_id:
            self._event_ids.add(event_id)

        if venue_order_id:
            if order.venue_order_id and order.venue_order_id != venue_order_id:
                raise ValueError(
                    f"venue order id changed for {client_order_id}: "
                    f"{order.venue_order_id!r} -> {venue_order_id!r}"
                )
            order.venue_order_id = venue_order_id
        if permanent_order_id:
            if order.permanent_order_id and order.permanent_order_id != permanent_order_id:
                raise ValueError(
                    f"permanent order id changed for {client_order_id}: "
                    f"{order.permanent_order_id!r} -> {permanent_order_id!r}"
                )
            order.permanent_order_id = permanent_order_id

        if status == LifecycleStatus.PENDING_CANCEL:
            if order.status not in TERMINAL_ORDER_STATES:
                order._pre_cancel_status = order.status
                order.status = status
        elif status in {LifecycleStatus.SUBMITTED, LifecycleStatus.ACKNOWLEDGED}:
            if order.status in {
                LifecycleStatus.INITIALIZED,
                LifecycleStatus.SUBMITTED,
                LifecycleStatus.ACKNOWLEDGED,
            }:
                order.status = status
        elif status in {
            LifecycleStatus.CANCELED,
            LifecycleStatus.EXPIRED,
            LifecycleStatus.REJECTED,
            LifecycleStatus.DENIED,
        }:
            if order.status != LifecycleStatus.FILLED:
                order.status = status
        elif status in {LifecycleStatus.PARTIALLY_FILLED, LifecycleStatus.FILLED}:
            order.status = status
        else:
            order.status = status
        if reason:
            order.rejection_reason = str(reason)
        order.updated_ts_ns = max(order.updated_ts_ns, int(ts_ns))
        return True

    def amend_order(
        self,
        client_order_id: str,
        *,
        requested_quantity=None,
        ts_ns: int = 0,
    ) -> OrderRecord:
        order = self.orders.get(str(client_order_id))
        if order is None:
            raise KeyError(f"unknown client order id {client_order_id!r}")
        if requested_quantity is not None:
            quantity = _decimal(requested_quantity)
            if quantity <= ZERO or quantity < order.filled_quantity:
                raise ValueError("amended quantity must cover existing fills")
            order.requested_quantity = quantity
            self._rebuild_order(order)
        order.updated_ts_ns = max(order.updated_ts_ns, int(ts_ns))
        return order

    def apply_cancel_rejected(
        self,
        client_order_id: str,
        *,
        event_id: str = "",
        ts_ns: int = 0,
        reason: str = "",
    ) -> bool:
        if event_id and event_id in self._event_ids:
            return False
        order = self.orders.get(str(client_order_id))
        if order is None:
            raise KeyError(f"unknown client order id {client_order_id!r}")
        if event_id:
            self._event_ids.add(event_id)
        if order.status == LifecycleStatus.PENDING_CANCEL:
            order.status = order._pre_cancel_status or (
                LifecycleStatus.PARTIALLY_FILLED
                if order.filled_quantity > ZERO
                else LifecycleStatus.ACKNOWLEDGED
            )
        order.rejection_reason = str(reason)
        order.updated_ts_ns = max(order.updated_ts_ns, int(ts_ns))
        return True

    def apply_fill(
        self,
        *,
        client_order_id: str,
        execution_id: str,
        instrument_id: str,
        side: str,
        quantity,
        price,
        ts_ns: int,
        event_id: str = "",
        correction_of: str = "",
    ) -> bool:
        if event_id and event_id in self._event_ids:
            return False
        order = self.orders.get(str(client_order_id))
        if order is None:
            raise KeyError(f"unknown client order id {client_order_id!r}")
        execution_id = str(execution_id)
        quantity = _decimal(quantity)
        price = _decimal(price)
        normalized_side = str(side).upper()
        if quantity <= ZERO or price <= ZERO:
            raise ValueError("fill quantity and price must be positive")
        if normalized_side not in {"BUY", "SELL"}:
            raise ValueError(f"invalid fill side {side!r}")
        if str(instrument_id) != order.instrument_id:
            raise ValueError("fill instrument does not match its order")

        existing = self.fills.get(execution_id)
        if existing is not None and not correction_of:
            duplicate = (
                existing.client_order_id == str(client_order_id)
                and existing.instrument_id == str(instrument_id)
                and existing.side == normalized_side
                and existing.quantity == quantity
                and existing.price == price
            )
            if duplicate:
                if event_id:
                    self._event_ids.add(event_id)
                return False
            raise ValueError(f"conflicting duplicate execution id {execution_id!r}")

        if correction_of:
            corrected = self.fills.get(str(correction_of))
            if corrected is None:
                raise ValueError(f"execution correction references unknown fill {correction_of!r}")
            if corrected.client_order_id != str(client_order_id):
                raise ValueError("execution correction changed client order id")
            del self.fills[str(correction_of)]
            if str(correction_of) in order.fill_ids:
                order.fill_ids.remove(str(correction_of))

        if event_id:
            self._event_ids.add(event_id)
        self._sequence += 1
        fill = FillRecord(
            execution_id=execution_id,
            client_order_id=str(client_order_id),
            instrument_id=str(instrument_id),
            side=normalized_side,
            quantity=quantity,
            price=price,
            ts_ns=int(ts_ns),
            sequence=self._sequence,
            event_id=str(event_id),
            correction_of=str(correction_of),
        )
        self.fills[execution_id] = fill
        if execution_id not in order.fill_ids:
            order.fill_ids.append(execution_id)
        self._rebuild_order(order)
        self._rebuild_position(fill.instrument_id)
        order.updated_ts_ns = max(order.updated_ts_ns, int(ts_ns))
        return True

    def _rebuild_order(self, order: OrderRecord) -> None:
        fills = [self.fills[fill_id] for fill_id in order.fill_ids if fill_id in self.fills]
        total = sum((fill.quantity for fill in fills), ZERO)
        notional = sum((fill.quantity * fill.price for fill in fills), ZERO)
        order.filled_quantity = total
        order.average_fill_price = notional / total if total > ZERO else ZERO
        if total >= order.requested_quantity:
            order.status = LifecycleStatus.FILLED
        elif total > ZERO:
            order.status = LifecycleStatus.PARTIALLY_FILLED

    def _rebuild_position(self, instrument_id: str) -> None:
        fills = sorted(
            (fill for fill in self.fills.values() if fill.instrument_id == instrument_id),
            key=lambda fill: (fill.ts_ns, fill.sequence),
        )
        quantity = ZERO
        average = ZERO
        realized = ZERO
        for fill in fills:
            delta = fill.signed_quantity
            if quantity == ZERO or quantity * delta > ZERO:
                total_abs = abs(quantity) + abs(delta)
                average = (
                    (abs(quantity) * average + abs(delta) * fill.price) / total_abs
                    if total_abs > ZERO
                    else ZERO
                )
                quantity += delta
                continue

            closing = min(abs(quantity), abs(delta))
            position_sign = Decimal("1") if quantity > ZERO else Decimal("-1")
            realized += closing * (fill.price - average) * position_sign
            next_quantity = quantity + delta
            if next_quantity == ZERO:
                average = ZERO
            elif quantity * next_quantity < ZERO:
                average = fill.price
            quantity = next_quantity
        self.positions[instrument_id] = PositionState(
            instrument_id=instrument_id,
            quantity=quantity,
            average_entry_price=average,
            realized_pnl=realized,
        )

    def position(self, instrument_id: str) -> PositionState:
        return self.positions.get(str(instrument_id), PositionState(str(instrument_id)))

    def working_orders(
        self,
        *,
        instrument_id: str | None = None,
        roles: Iterable[OrderRole] | None = None,
    ) -> list[OrderRecord]:
        role_set = set(roles) if roles is not None else None
        return [
            order
            for order in self.orders.values()
            if order.is_working
            and (instrument_id is None or order.instrument_id == str(instrument_id))
            and (role_set is None or order.role in role_set)
        ]

    def snapshot(self) -> dict:
        return {
            "version": 1,
            "sequence": self._sequence,
            "event_ids": sorted(self._event_ids),
            "orders": [
                {
                    **asdict(order),
                    "requested_quantity": str(order.requested_quantity),
                    "filled_quantity": str(order.filled_quantity),
                    "average_fill_price": str(order.average_fill_price),
                    "role": order.role.value,
                    "status": order.status.value,
                    "_pre_cancel_status": (
                        order._pre_cancel_status.value if order._pre_cancel_status else None
                    ),
                }
                for order in self.orders.values()
            ],
            "fills": [
                {
                    **asdict(fill),
                    "quantity": str(fill.quantity),
                    "price": str(fill.price),
                }
                for fill in self.fills.values()
            ],
        }

    @classmethod
    def from_snapshot(cls, payload: dict) -> "ExecutionLedger":
        if payload.get("version") != 1:
            raise ValueError(f"unsupported execution ledger version {payload.get('version')!r}")
        ledger = cls()
        ledger._sequence = int(payload.get("sequence", 0))
        ledger._event_ids = {str(value) for value in payload.get("event_ids", ())}
        for values in payload.get("orders", ()):
            record = OrderRecord(
                client_order_id=str(values["client_order_id"]),
                instrument_id=str(values["instrument_id"]),
                side=str(values["side"]),
                requested_quantity=_decimal(values["requested_quantity"]),
                role=OrderRole(values["role"]),
                status=LifecycleStatus(values["status"]),
                venue_order_id=str(values.get("venue_order_id", "")),
                permanent_order_id=str(values.get("permanent_order_id", "")),
                submitted_ts_ns=int(values.get("submitted_ts_ns", 0)),
                updated_ts_ns=int(values.get("updated_ts_ns", 0)),
                signal_version=str(values.get("signal_version", "")),
                rejection_reason=str(values.get("rejection_reason", "")),
                fill_ids=[str(value) for value in values.get("fill_ids", ())],
                filled_quantity=_decimal(values.get("filled_quantity", 0)),
                average_fill_price=_decimal(values.get("average_fill_price", 0)),
                _pre_cancel_status=(
                    LifecycleStatus(values["_pre_cancel_status"])
                    if values.get("_pre_cancel_status")
                    else None
                ),
            )
            ledger.orders[record.client_order_id] = record
        for values in payload.get("fills", ()):
            fill = FillRecord(
                execution_id=str(values["execution_id"]),
                client_order_id=str(values["client_order_id"]),
                instrument_id=str(values["instrument_id"]),
                side=str(values["side"]),
                quantity=_decimal(values["quantity"]),
                price=_decimal(values["price"]),
                ts_ns=int(values["ts_ns"]),
                sequence=int(values["sequence"]),
                event_id=str(values.get("event_id", "")),
                correction_of=str(values.get("correction_of", "")),
            )
            ledger.fills[fill.execution_id] = fill
        for order in ledger.orders.values():
            ledger._rebuild_order(order)
        for instrument_id in {fill.instrument_id for fill in ledger.fills.values()}:
            ledger._rebuild_position(instrument_id)
        return ledger


class ExecutionSafetyController:
    """Fail-closed entry gate and structured operator-alert accumulator."""

    def __init__(self, max_rejections: int = 1) -> None:
        if max_rejections < 1:
            raise ValueError("max_rejections must be at least one")
        self.max_rejections = int(max_rejections)
        self.state = ExecutionSafetyState.ACTIVE
        self.rejection_count = 0
        self.alerts: list[OperatorAlert] = []
        self._alert_sequence = 0

    @property
    def entries_allowed(self) -> bool:
        return self.state == ExecutionSafetyState.ACTIVE

    def alert(
        self,
        severity: str,
        code: str,
        message: str,
        *,
        ts_ns: int = 0,
        client_order_id: str = "",
        instrument_id: str = "",
    ) -> OperatorAlert:
        self._alert_sequence += 1
        alert = OperatorAlert(
            sequence=self._alert_sequence,
            severity=str(severity).upper(),
            code=str(code),
            message=str(message),
            ts_ns=int(ts_ns),
            client_order_id=str(client_order_id),
            instrument_id=str(instrument_id),
        )
        self.alerts.append(alert)
        return alert

    def freeze(self, reason: str, *, ts_ns: int = 0) -> None:
        if self.state == ExecutionSafetyState.ACTIVE:
            self.state = ExecutionSafetyState.FROZEN
        self.alert("WARNING", "ENTRIES_FROZEN", reason, ts_ns=ts_ns)

    def resume(self, reason: str, *, ts_ns: int = 0) -> None:
        if self.state != ExecutionSafetyState.FROZEN:
            return
        self.state = ExecutionSafetyState.ACTIVE
        self.alert("INFO", "ENTRIES_RESUMED", reason, ts_ns=ts_ns)

    def suspend(
        self,
        reason: str,
        *,
        code: str = "EXECUTION_SUSPENDED",
        ts_ns: int = 0,
        client_order_id: str = "",
        instrument_id: str = "",
    ) -> None:
        self.state = ExecutionSafetyState.SUSPENDED
        self.alert(
            "CRITICAL",
            code,
            reason,
            ts_ns=ts_ns,
            client_order_id=client_order_id,
            instrument_id=instrument_id,
        )

    def on_rejection(
        self,
        reason: str,
        *,
        ts_ns: int = 0,
        client_order_id: str = "",
        instrument_id: str = "",
    ) -> None:
        self.rejection_count += 1
        self.alert(
            "ERROR",
            "ORDER_REJECTED",
            reason,
            ts_ns=ts_ns,
            client_order_id=client_order_id,
            instrument_id=instrument_id,
        )
        if self.rejection_count >= self.max_rejections:
            self.suspend(
                f"Order rejection threshold reached ({self.rejection_count}).",
                code="REJECTION_THRESHOLD",
                ts_ns=ts_ns,
                client_order_id=client_order_id,
                instrument_id=instrument_id,
            )

    def on_cancel_rejected(
        self,
        reason: str,
        *,
        ts_ns: int = 0,
        client_order_id: str = "",
        instrument_id: str = "",
    ) -> None:
        self.suspend(
            reason,
            code="CANCEL_REJECTED",
            ts_ns=ts_ns,
            client_order_id=client_order_id,
            instrument_id=instrument_id,
        )

    def mark_uncertain(self, reason: str, *, ts_ns: int = 0) -> None:
        self.state = ExecutionSafetyState.UNCERTAIN
        self.alert("CRITICAL", "BROKER_STATE_UNCERTAIN", reason, ts_ns=ts_ns)

    def begin_emergency(self, reason: str, *, ts_ns: int = 0) -> None:
        self.state = ExecutionSafetyState.EMERGENCY
        self.alert("CRITICAL", "EMERGENCY_EXIT", reason, ts_ns=ts_ns)

    def begin_shutdown(self, *, ts_ns: int = 0) -> None:
        self.state = ExecutionSafetyState.STOPPING
        self.alert("INFO", "SHUTDOWN_STARTED", "New entries frozen.", ts_ns=ts_ns)

    def on_restart(self, *, ts_ns: int = 0) -> None:
        if self.state == ExecutionSafetyState.STOPPED:
            self.state = ExecutionSafetyState.ACTIVE
            self.alert("INFO", "CLEAN_RESTART", "Clean shutdown state re-armed.", ts_ns=ts_ns)
        elif self.state == ExecutionSafetyState.STOPPING:
            self.mark_uncertain("Process restarted during an incomplete shutdown.", ts_ns=ts_ns)

    def finish_shutdown(self, *, clean: bool, reason: str = "", ts_ns: int = 0) -> None:
        if clean:
            self.state = ExecutionSafetyState.STOPPED
            self.alert("INFO", "SHUTDOWN_COMPLETE", "Shutdown confirmed.", ts_ns=ts_ns)
        else:
            self.mark_uncertain(reason or "Shutdown left broker state unresolved.", ts_ns=ts_ns)

    def snapshot(self) -> dict:
        return {
            "version": 1,
            "state": self.state.value,
            "max_rejections": self.max_rejections,
            "rejection_count": self.rejection_count,
            "alert_sequence": self._alert_sequence,
            "alerts": [asdict(alert) for alert in self.alerts],
        }

    @classmethod
    def from_snapshot(cls, payload: dict) -> "ExecutionSafetyController":
        if payload.get("version") != 1:
            raise ValueError(f"unsupported execution safety version {payload.get('version')!r}")
        controller = cls(max_rejections=int(payload.get("max_rejections", 1)))
        controller.state = ExecutionSafetyState(payload["state"])
        controller.rejection_count = int(payload.get("rejection_count", 0))
        controller._alert_sequence = int(payload.get("alert_sequence", 0))
        controller.alerts = [OperatorAlert(**values) for values in payload.get("alerts", ())]
        return controller
