"""Fail-closed production-readiness controls for capital deployment.

There is deliberately no environment-variable override. Enabling live capital
requires a reviewed source change after every P0 gate is complete and validated
against the supported IBKR/TWS versions.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass


LIVE_CAPITAL_ENABLED = False
LIVE_GATE_CODE = "P0_PRODUCTION_READINESS_INCOMPLETE"


@dataclass(frozen=True)
class ReadinessGate:
    key: str
    title: str
    complete: bool


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


def live_readiness_status() -> dict:
    incomplete = [gate.key for gate in P0_GATES if not gate.complete]
    return {
        "live_capital_enabled": LIVE_CAPITAL_ENABLED and not incomplete,
        "code": LIVE_GATE_CODE,
        "gates": [asdict(gate) for gate in P0_GATES],
        "incomplete": incomplete,
    }


def assert_live_capital_enabled() -> None:
    status = live_readiness_status()
    if not status["live_capital_enabled"]:
        raise LiveCapitalDisabledError(
            "Live capital is disabled: P0 production-readiness gates are incomplete. "
            "Use IBKR equity paper trading only."
        )
