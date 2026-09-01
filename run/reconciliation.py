"""Broker-source-of-truth reconciliation independent of IBKR/Nautilus types."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Iterable, Protocol

from quant.strategies.execution_state import (
    ExecutionLedger,
    LifecycleStatus,
    OrderRole,
)


ZERO = Decimal("0")


def _decimal(value) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"invalid broker decimal {value!r}") from exc
    if not result.is_finite():
        raise ValueError(f"non-finite broker decimal {value!r}")
    return result


class ReconciliationSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class RecoveryActionType(str, Enum):
    APPLY_ORDER_STATE = "APPLY_ORDER_STATE"
    APPLY_EXECUTION = "APPLY_EXECUTION"
    ADOPT_ORDER = "ADOPT_ORDER"
    CANCEL_ORDER = "CANCEL_ORDER"
    FREEZE_AND_REVIEW = "FREEZE_AND_REVIEW"


@dataclass(frozen=True)
class BrokerPosition:
    instrument_id: str
    quantity: Decimal
    average_price: Decimal = ZERO
    account_id: str = ""

    @classmethod
    def normalized(
        cls,
        instrument_id: str,
        quantity,
        average_price=0,
        account_id: str = "",
    ) -> "BrokerPosition":
        return cls(
            str(instrument_id),
            _decimal(quantity),
            _decimal(average_price),
            str(account_id),
        )


@dataclass(frozen=True)
class BrokerOrder:
    client_order_id: str
    instrument_id: str
    side: str
    quantity: Decimal
    filled_quantity: Decimal = ZERO
    status: str = "SUBMITTED"
    venue_order_id: str = ""
    permanent_order_id: str = ""
    owned_by_strategy: bool = False
    role: OrderRole = OrderRole.UNKNOWN

    @classmethod
    def normalized(cls, **values) -> "BrokerOrder":
        side = str(values.get("side", "")).upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError(f"invalid broker order side {side!r}")
        return cls(
            client_order_id=str(values.get("client_order_id", "")),
            instrument_id=str(values["instrument_id"]),
            side=side,
            quantity=_decimal(values["quantity"]),
            filled_quantity=_decimal(values.get("filled_quantity", 0)),
            status=str(values.get("status", "SUBMITTED")).upper(),
            venue_order_id=str(values.get("venue_order_id", "")),
            permanent_order_id=str(values.get("permanent_order_id", "")),
            owned_by_strategy=bool(values.get("owned_by_strategy", False)),
            role=OrderRole(values.get("role", OrderRole.UNKNOWN)),
        )

    @property
    def is_active(self) -> bool:
        return self.status in {
            "INITIALIZED",
            "PRESUBMITTED",
            "PRE_SUBMITTED",
            "SUBMITTED",
            "ACKNOWLEDGED",
            "ACCEPTED",
            "RELEASED",
            "EMULATED",
            "TRIGGERED",
            "PENDING_UPDATE",
            "PARTIALLY_FILLED",
            "PENDING_CANCEL",
        }


@dataclass(frozen=True)
class BrokerExecution:
    execution_id: str
    client_order_id: str
    instrument_id: str
    side: str
    quantity: Decimal
    price: Decimal
    ts_ns: int
    correction_of: str = ""

    @classmethod
    def normalized(cls, **values) -> "BrokerExecution":
        side = str(values.get("side", "")).upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError(f"invalid broker execution side {side!r}")
        return cls(
            execution_id=str(values["execution_id"]),
            client_order_id=str(values.get("client_order_id", "")),
            instrument_id=str(values["instrument_id"]),
            side=side,
            quantity=_decimal(values["quantity"]),
            price=_decimal(values["price"]),
            ts_ns=int(values.get("ts_ns", 0)),
            correction_of=str(values.get("correction_of", "")),
        )


@dataclass(frozen=True)
class BrokerAccount:
    account_id: str
    base_currency: str
    equity: Decimal
    available_funds: Decimal
    buying_power: Decimal
    settled_cash: Decimal | None = None
    snapshot_complete: bool = False

    @classmethod
    def normalized(cls, **values) -> "BrokerAccount":
        settled = values.get("settled_cash")
        return cls(
            account_id=str(values["account_id"]),
            base_currency=str(values.get("base_currency", "")).upper(),
            equity=_decimal(values.get("equity", 0)),
            available_funds=_decimal(values.get("available_funds", 0)),
            buying_power=_decimal(values.get("buying_power", 0)),
            settled_cash=_decimal(settled) if settled is not None else None,
            snapshot_complete=bool(values.get("snapshot_complete", False)),
        )


@dataclass(frozen=True)
class BrokerSnapshot:
    account: BrokerAccount
    positions: tuple[BrokerPosition, ...] = ()
    orders: tuple[BrokerOrder, ...] = ()
    executions: tuple[BrokerExecution, ...] = ()
    positions_complete: bool = False
    orders_complete: bool = False
    executions_complete: bool = False
    captured_at_ns: int = 0

    @property
    def complete(self) -> bool:
        return bool(
            self.account.snapshot_complete
            and self.positions_complete
            and self.orders_complete
            and self.executions_complete
        )


class BrokerSnapshotProvider(Protocol):
    def broker_snapshot(self) -> BrokerSnapshot: ...


@dataclass(frozen=True)
class ReconciliationIssue:
    code: str
    severity: ReconciliationSeverity
    message: str
    instrument_id: str = ""
    client_order_id: str = ""


@dataclass(frozen=True)
class RecoveryAction:
    action: RecoveryActionType
    reason: str
    client_order_id: str = ""
    execution_id: str = ""
    broker_status: str = ""


@dataclass(frozen=True)
class ReconciliationReport:
    snapshot_complete: bool
    positions_match: bool
    orders_resolved: bool
    account_valid: bool
    issues: tuple[ReconciliationIssue, ...] = ()
    actions: tuple[RecoveryAction, ...] = ()
    uncertain_order_ids: tuple[str, ...] = ()
    recovered_execution_ids: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return bool(
            self.snapshot_complete
            and self.positions_match
            and self.orders_resolved
            and self.account_valid
            and not any(
                issue.severity == ReconciliationSeverity.CRITICAL
                for issue in self.issues
            )
        )

    def as_dict(self) -> dict:
        return {
            "passed": self.passed,
            "snapshot_complete": self.snapshot_complete,
            "positions_match": self.positions_match,
            "orders_resolved": self.orders_resolved,
            "account_valid": self.account_valid,
            "issues": [
                {**asdict(issue), "severity": issue.severity.value}
                for issue in self.issues
            ],
            "actions": [
                {**asdict(action), "action": action.action.value}
                for action in self.actions
            ],
            "uncertain_order_ids": list(self.uncertain_order_ids),
            "recovered_execution_ids": list(self.recovered_execution_ids),
        }


@dataclass(frozen=True)
class ReconciliationConfig:
    expected_account_id: str = ""
    required_base_currency: str = "USD"
    quantity_tolerance: Decimal = Decimal("0.00000001")
    require_settled_cash: bool = False
    allow_unmanaged_positions: bool = False
    allow_unmanaged_orders: bool = False


_BROKER_TO_LOCAL_STATUS = {
    "INITIALIZED": LifecycleStatus.INITIALIZED,
    "EMULATED": LifecycleStatus.INITIALIZED,
    "RELEASED": LifecycleStatus.SUBMITTED,
    "PRESUBMITTED": LifecycleStatus.SUBMITTED,
    "PRE_SUBMITTED": LifecycleStatus.SUBMITTED,
    "SUBMITTED": LifecycleStatus.SUBMITTED,
    "ACKNOWLEDGED": LifecycleStatus.ACKNOWLEDGED,
    "ACCEPTED": LifecycleStatus.ACKNOWLEDGED,
    "TRIGGERED": LifecycleStatus.ACKNOWLEDGED,
    "PENDING_UPDATE": LifecycleStatus.ACKNOWLEDGED,
    "PARTIALLY_FILLED": LifecycleStatus.PARTIALLY_FILLED,
    "PENDING_CANCEL": LifecycleStatus.PENDING_CANCEL,
    "FILLED": LifecycleStatus.FILLED,
    "CANCELED": LifecycleStatus.CANCELED,
    "CANCELLED": LifecycleStatus.CANCELED,
    "EXPIRED": LifecycleStatus.EXPIRED,
    "REJECTED": LifecycleStatus.REJECTED,
    "DENIED": LifecycleStatus.DENIED,
    "INACTIVE": LifecycleStatus.DENIED,
}


def _broker_order_for_local(local_order, broker_orders: Iterable[BrokerOrder]):
    for broker_order in broker_orders:
        if broker_order.client_order_id and (
            broker_order.client_order_id == local_order.client_order_id
        ):
            return broker_order
        if local_order.permanent_order_id and (
            broker_order.permanent_order_id == local_order.permanent_order_id
        ):
            return broker_order
        if local_order.venue_order_id and broker_order.venue_order_id == local_order.venue_order_id:
            return broker_order
    return None


def reconcile(
    ledger: ExecutionLedger,
    snapshot: BrokerSnapshot,
    config: ReconciliationConfig | None = None,
) -> ReconciliationReport:
    cfg = config or ReconciliationConfig()
    issues: list[ReconciliationIssue] = []
    actions: list[RecoveryAction] = []
    uncertain: set[str] = set()
    recovered_executions: list[str] = []

    if not snapshot.complete:
        issues.append(
            ReconciliationIssue(
                "INCOMPLETE_BROKER_SNAPSHOT",
                ReconciliationSeverity.CRITICAL,
                "Broker snapshot did not receive every required end marker.",
            )
        )

    account = snapshot.account
    if cfg.expected_account_id and account.account_id != cfg.expected_account_id:
        issues.append(
            ReconciliationIssue(
                "ACCOUNT_ID_MISMATCH",
                ReconciliationSeverity.CRITICAL,
                f"Expected account {cfg.expected_account_id}, received {account.account_id}.",
            )
        )
    if account.base_currency != cfg.required_base_currency:
        issues.append(
            ReconciliationIssue(
                "ACCOUNT_CURRENCY_MISMATCH",
                ReconciliationSeverity.CRITICAL,
                f"Account currency {account.base_currency or 'UNKNOWN'} is unsupported.",
            )
        )
    for code, value in (
        ("INVALID_ACCOUNT_EQUITY", account.equity),
        ("INVALID_AVAILABLE_FUNDS", account.available_funds),
        ("INVALID_BUYING_POWER", account.buying_power),
    ):
        if value < ZERO or (code == "INVALID_ACCOUNT_EQUITY" and value == ZERO):
            issues.append(
                ReconciliationIssue(
                    code,
                    ReconciliationSeverity.CRITICAL,
                    f"Broker supplied unsafe account value {value}.",
                )
            )
    if cfg.require_settled_cash and account.settled_cash is None:
        issues.append(
            ReconciliationIssue(
                "SETTLED_CASH_UNAVAILABLE",
                ReconciliationSeverity.CRITICAL,
                "Settled cash is required but absent from the broker snapshot.",
            )
        )

    local_positions = {
        instrument_id: position.quantity
        for instrument_id, position in ledger.positions.items()
        if position.quantity != ZERO
    }
    broker_positions = {
        position.instrument_id: position.quantity
        for position in snapshot.positions
        if position.quantity != ZERO
    }
    for instrument_id in sorted(set(local_positions) | set(broker_positions)):
        local_quantity = local_positions.get(instrument_id, ZERO)
        broker_quantity = broker_positions.get(instrument_id, ZERO)
        if abs(local_quantity - broker_quantity) <= cfg.quantity_tolerance:
            continue
        unmanaged = local_quantity == ZERO and broker_quantity != ZERO
        severity = (
            ReconciliationSeverity.WARNING
            if unmanaged and cfg.allow_unmanaged_positions
            else ReconciliationSeverity.CRITICAL
        )
        issues.append(
            ReconciliationIssue(
                "UNMANAGED_BROKER_POSITION" if unmanaged else "POSITION_QUANTITY_MISMATCH",
                severity,
                f"Local quantity {local_quantity} differs from broker quantity {broker_quantity}.",
                instrument_id=instrument_id,
            )
        )

    matched_broker_orders: set[int] = set()
    for local_order in ledger.orders.values():
        broker_order = _broker_order_for_local(local_order, snapshot.orders)
        if broker_order is not None:
            matched_broker_orders.add(id(broker_order))
            if (
                broker_order.instrument_id != local_order.instrument_id
                or broker_order.side != local_order.side
                or broker_order.quantity != local_order.requested_quantity
            ):
                uncertain.add(local_order.client_order_id)
                issues.append(
                    ReconciliationIssue(
                        "ORDER_IDENTITY_MISMATCH",
                        ReconciliationSeverity.CRITICAL,
                        "Broker order identity conflicts with the durable local order.",
                        instrument_id=local_order.instrument_id,
                        client_order_id=local_order.client_order_id,
                    )
                )
                continue
            mapped_status = _BROKER_TO_LOCAL_STATUS.get(broker_order.status)
            if mapped_status is None:
                uncertain.add(local_order.client_order_id)
                issues.append(
                    ReconciliationIssue(
                        "UNKNOWN_BROKER_ORDER_STATUS",
                        ReconciliationSeverity.CRITICAL,
                        f"Unsupported broker status {broker_order.status}.",
                        client_order_id=local_order.client_order_id,
                    )
                )
            elif mapped_status != local_order.status:
                actions.append(
                    RecoveryAction(
                        RecoveryActionType.APPLY_ORDER_STATE,
                        "Broker state supersedes the stale local lifecycle state.",
                        client_order_id=local_order.client_order_id,
                        broker_status=mapped_status.value,
                    )
                )
        elif local_order.is_working:
            uncertain.add(local_order.client_order_id)
            issues.append(
                ReconciliationIssue(
                    "LOCAL_WORKING_ORDER_MISSING_AT_BROKER",
                    ReconciliationSeverity.CRITICAL,
                    "A locally working order was absent from the complete broker snapshot.",
                    instrument_id=local_order.instrument_id,
                    client_order_id=local_order.client_order_id,
                )
            )

    for broker_order in snapshot.orders:
        if id(broker_order) in matched_broker_orders or not broker_order.is_active:
            continue
        if broker_order.owned_by_strategy:
            actions.append(
                RecoveryAction(
                    RecoveryActionType.ADOPT_ORDER,
                    "Strategy-owned broker order is missing locally and must be adopted.",
                    client_order_id=broker_order.client_order_id,
                    broker_status=broker_order.status,
                )
            )
            issues.append(
                ReconciliationIssue(
                    "UNADOPTED_STRATEGY_ORDER",
                    ReconciliationSeverity.CRITICAL,
                    "Strategy-owned active broker order is absent from the local ledger.",
                    instrument_id=broker_order.instrument_id,
                    client_order_id=broker_order.client_order_id,
                )
            )
        elif not cfg.allow_unmanaged_orders:
            actions.append(
                RecoveryAction(
                    RecoveryActionType.FREEZE_AND_REVIEW,
                    "Manual or foreign broker order requires operator review.",
                    client_order_id=broker_order.client_order_id,
                    broker_status=broker_order.status,
                )
            )
            issues.append(
                ReconciliationIssue(
                    "UNMANAGED_BROKER_ORDER",
                    ReconciliationSeverity.CRITICAL,
                    "Active broker order is not owned by this strategy.",
                    instrument_id=broker_order.instrument_id,
                    client_order_id=broker_order.client_order_id,
                )
            )

    for execution in snapshot.executions:
        if execution.execution_id in ledger.fills:
            continue
        if execution.client_order_id not in ledger.orders:
            parent = next(
                (
                    order
                    for order in snapshot.orders
                    if order.client_order_id == execution.client_order_id
                ),
                None,
            )
            if parent is not None and not parent.is_active and not parent.owned_by_strategy:
                issues.append(
                    ReconciliationIssue(
                        "UNMANAGED_HISTORICAL_EXECUTION",
                        ReconciliationSeverity.INFO,
                        "Historical execution belongs to a terminal foreign order.",
                        instrument_id=execution.instrument_id,
                        client_order_id=execution.client_order_id,
                    )
                )
                continue
            issues.append(
                ReconciliationIssue(
                    "UNCLAIMED_BROKER_EXECUTION",
                    ReconciliationSeverity.CRITICAL,
                    "Broker execution cannot be associated with a local order.",
                    instrument_id=execution.instrument_id,
                    client_order_id=execution.client_order_id,
                )
            )
            continue
        actions.append(
            RecoveryAction(
                RecoveryActionType.APPLY_EXECUTION,
                "Broker execution is missing from the durable local ledger.",
                client_order_id=execution.client_order_id,
                execution_id=execution.execution_id,
            )
        )
        recovered_executions.append(execution.execution_id)

    positions_match = not any(
        issue.severity == ReconciliationSeverity.CRITICAL
        and issue.code in {"UNMANAGED_BROKER_POSITION", "POSITION_QUANTITY_MISMATCH"}
        for issue in issues
    )
    orders_resolved = not uncertain and not any(
        issue.severity == ReconciliationSeverity.CRITICAL
        and issue.code
        in {
            "ORDER_IDENTITY_MISMATCH",
            "UNKNOWN_BROKER_ORDER_STATUS",
            "UNADOPTED_STRATEGY_ORDER",
            "UNMANAGED_BROKER_ORDER",
            "UNCLAIMED_BROKER_EXECUTION",
        }
        for issue in issues
    )
    account_valid = not any(
        issue.severity == ReconciliationSeverity.CRITICAL
        and issue.code
        in {
            "ACCOUNT_ID_MISMATCH",
            "ACCOUNT_CURRENCY_MISMATCH",
            "INVALID_ACCOUNT_EQUITY",
            "INVALID_AVAILABLE_FUNDS",
            "INVALID_BUYING_POWER",
            "SETTLED_CASH_UNAVAILABLE",
        }
        for issue in issues
    )
    return ReconciliationReport(
        snapshot_complete=snapshot.complete,
        positions_match=positions_match,
        orders_resolved=orders_resolved,
        account_valid=account_valid,
        issues=tuple(issues),
        actions=tuple(actions),
        uncertain_order_ids=tuple(sorted(uncertain)),
        recovered_execution_ids=tuple(recovered_executions),
    )


def recover_ledger(
    ledger: ExecutionLedger,
    snapshot: BrokerSnapshot,
    report: ReconciliationReport,
) -> ExecutionLedger:
    """Apply only deterministic broker recovery actions to a copied ledger."""
    recovered = ExecutionLedger.from_snapshot(ledger.snapshot())
    orders_by_id = {order.client_order_id: order for order in snapshot.orders}
    executions_by_id = {item.execution_id: item for item in snapshot.executions}
    for action in report.actions:
        if action.action == RecoveryActionType.APPLY_ORDER_STATE:
            broker_order = orders_by_id.get(action.client_order_id)
            if broker_order is None:
                continue
            recovered.apply_order_state(
                action.client_order_id,
                _BROKER_TO_LOCAL_STATUS[broker_order.status],
                event_id=f"reconcile:order:{action.client_order_id}:{broker_order.status}",
                venue_order_id=broker_order.venue_order_id,
                permanent_order_id=broker_order.permanent_order_id,
            )
        elif action.action == RecoveryActionType.APPLY_EXECUTION:
            execution = executions_by_id.get(action.execution_id)
            if execution is None:
                continue
            recovered.apply_fill(
                client_order_id=execution.client_order_id,
                execution_id=execution.execution_id,
                instrument_id=execution.instrument_id,
                side=execution.side,
                quantity=execution.quantity,
                price=execution.price,
                ts_ns=execution.ts_ns,
                event_id=f"reconcile:execution:{execution.execution_id}",
                correction_of=execution.correction_of,
            )
    return recovered


def reconcile_provider(
    provider: BrokerSnapshotProvider,
    ledger: ExecutionLedger,
    config: ReconciliationConfig | None = None,
) -> tuple[BrokerSnapshot, ReconciliationReport]:
    snapshot = provider.broker_snapshot()
    return snapshot, reconcile(ledger, snapshot, config)
