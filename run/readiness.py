"""Fail-closed production-readiness controls for capital deployment.

There is deliberately no environment-variable override. Enabling live capital
requires a reviewed source change after every P0 gate is complete and validated
against the supported IBKR/TWS versions.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable

from quant.run.reconciliation import (
    ReconciliationReport,
    RecoveryActionType,
)


LIVE_CAPITAL_ENABLED = False
LIVE_GATE_CODE = "P0_PRODUCTION_READINESS_INCOMPLETE"


@dataclass(frozen=True)
class ReadinessGate:
    key: str
    title: str
    complete: bool


@dataclass(frozen=True)
class BrokerReadinessCheck:
    """Result of one broker-dependent startup safety check."""

    key: str
    title: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class BrokerReadinessContext:
    """Verified inputs produced after a connected broker startup snapshot."""

    reconciliation_report: ReconciliationReport | None = None
    market_data_report: Any | None = None
    recovery_verified: bool = False


P0_GATES = (
    ReadinessGate("order_lifecycle", "Order lifecycle and rejection policy", False),
    ReadinessGate("safe_shutdown", "Safe cancel/confirm shutdown", False),
    ReadinessGate("broker_protection", "Fill-based broker stop/target protection", False),
    ReadinessGate("kill_switch", "Confirmed cancel-all and emergency flatten", False),
    ReadinessGate("exchange_sessions", "IBKR exchange sessions and calendars", False),
    ReadinessGate("session_policies", "Validated order/session policies", False),
    ReadinessGate("session_risk", "Exchange-session daily risk accounting", False),
    ReadinessGate("realtime_risk", "Continuous broker/data risk supervision", False),
    ReadinessGate("broker_truth", "Deterministic broker-source reconciliation", False),
)


class LiveCapitalDisabledError(RuntimeError):
    """Raised whenever a caller attempts to arm real-capital execution."""


def reconcile_broker_positions(broker: Any | None = None) -> bool:
    """Reconcile broker positions against the durable local execution ledger.

    ``broker`` must be a :class:`BrokerReadinessContext` produced from a fresh,
    completed broker snapshot. Absence of that evidence fails closed.
    """
    report = getattr(broker, "reconciliation_report", None)
    return bool(
        report is not None
        and report.snapshot_complete
        and report.account_valid
        and report.positions_match
    )


def recover_uncertain_orders(broker: Any | None = None) -> bool:
    """Resolve orders whose final broker disposition is not locally known.

    Deterministic lifecycle/fill recovery is allowed only when a second
    reconciliation has verified the repaired ledger. Manual/foreign ownership,
    partial snapshots, or ambiguous terminal state fail closed.
    """
    report = getattr(broker, "reconciliation_report", None)
    if report is None or not report.orders_resolved or report.uncertain_order_ids:
        return False
    deterministic_recovery = any(
        action.action
        in {RecoveryActionType.APPLY_ORDER_STATE, RecoveryActionType.APPLY_EXECUTION}
        for action in report.actions
    )
    unsafe_recovery = any(
        action.action
        in {
            RecoveryActionType.ADOPT_ORDER,
            RecoveryActionType.CANCEL_ORDER,
            RecoveryActionType.FREEZE_AND_REVIEW,
        }
        for action in report.actions
    )
    return bool(
        not unsafe_recovery
        and (not deterministic_recovery or getattr(broker, "recovery_verified", False))
    )


def verify_market_data_freshness(broker: Any | None = None) -> bool:
    """Verify that every strategy instrument has current, coherent market data.

    The report covers completed-bar timestamps, exchange-session expectations,
    clock skew, coverage, and an asset/session-specific maximum age.
    """
    report = getattr(broker, "market_data_report", None)
    return bool(report is not None and getattr(report, "passed", False))


_BROKER_CHECK_SPECS: tuple[tuple[str, str, str], ...] = (
    (
        "broker_position_reconciliation",
        "Broker positions match durable strategy state",
        "reconcile_broker_positions",
    ),
    (
        "uncertain_order_recovery",
        "Uncertain broker orders have deterministic dispositions",
        "recover_uncertain_orders",
    ),
    (
        "market_data_freshness",
        "Market data is fresh for every active instrument",
        "verify_market_data_freshness",
    ),
)


def _run_broker_readiness_checks(broker: Any | None = None) -> list[BrokerReadinessCheck]:
    results: list[BrokerReadinessCheck] = []
    for key, title, function_name in _BROKER_CHECK_SPECS:
        hook: Callable[[Any | None], bool] = globals()[function_name]
        try:
            passed = bool(hook(broker))
            detail = "passed" if passed else "required broker evidence absent or failed"
        except NotImplementedError as exc:
            passed = False
            detail = str(exc) or "not implemented"
        except Exception as exc:  # noqa: BLE001 - a safety check must fail closed
            passed = False
            detail = f"{type(exc).__name__}: {exc}"
        results.append(BrokerReadinessCheck(key, title, passed, detail))
    return results


def live_readiness_status(broker: Any | None = None) -> dict:
    broker_checks = _run_broker_readiness_checks(broker)
    incomplete = [gate.key for gate in P0_GATES if not gate.complete]
    incomplete.extend(check.key for check in broker_checks if not check.passed)
    return {
        "live_capital_enabled": LIVE_CAPITAL_ENABLED and not incomplete,
        "code": LIVE_GATE_CODE,
        "gates": [asdict(gate) for gate in P0_GATES],
        "broker_checks": [asdict(check) for check in broker_checks],
        "incomplete": incomplete,
    }


def assert_live_capital_enabled(broker: Any | None = None) -> None:
    """Run static and broker-dependent gates before live execution can arm."""
    status = live_readiness_status(broker)
    if not status["live_capital_enabled"]:
        raise LiveCapitalDisabledError(
            "Live capital is disabled: P0 production-readiness gates are incomplete. "
            "Use IBKR equity paper trading only."
        )
