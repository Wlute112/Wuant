import pytest

from quant.data.quality import MarketDataFreshnessReport
from quant.run import readiness
from quant.run.reconciliation import ReconciliationReport
from quant.run.readiness import (
    BrokerReadinessContext,
    LIVE_GATE_CODE,
    LiveCapitalDisabledError,
    assert_live_capital_enabled,
    live_readiness_status,
    reconcile_broker_positions,
    recover_uncertain_orders,
    verify_market_data_freshness,
)


def test_live_capital_gate_is_fail_closed_without_runtime_override():
    status = live_readiness_status()
    assert status["live_capital_enabled"] is False
    assert status["code"] == LIVE_GATE_CODE
    assert status["incomplete"]
    assert {check["key"] for check in status["broker_checks"]} == {
        "broker_position_reconciliation",
        "uncertain_order_recovery",
        "market_data_freshness",
    }
    assert all(not check["passed"] for check in status["broker_checks"])
    with pytest.raises(LiveCapitalDisabledError, match="Live capital is disabled"):
        assert_live_capital_enabled()


def test_broker_readiness_stubs_fail_closed():
    assert reconcile_broker_positions() is False
    assert recover_uncertain_orders() is False
    assert verify_market_data_freshness() is False


def test_live_readiness_requires_all_runtime_broker_checks(monkeypatch):
    monkeypatch.setattr(readiness, "LIVE_CAPITAL_ENABLED", True)
    monkeypatch.setattr(
        readiness,
        "P0_GATES",
        tuple(
            readiness.ReadinessGate(gate.key, gate.title, True)
            for gate in readiness.P0_GATES
        ),
    )
    monkeypatch.setattr(readiness, "reconcile_broker_positions", lambda broker=None: True)
    monkeypatch.setattr(readiness, "recover_uncertain_orders", lambda broker=None: True)
    monkeypatch.setattr(readiness, "verify_market_data_freshness", lambda broker=None: True)

    status = readiness.live_readiness_status(object())
    assert status["live_capital_enabled"] is True
    assert status["incomplete"] == []


def test_runtime_broker_evidence_drives_integrated_hooks():
    reconciliation = ReconciliationReport(
        snapshot_complete=True,
        positions_match=True,
        orders_resolved=True,
        account_valid=True,
    )
    freshness = MarketDataFreshnessReport(
        passed=True,
        checked_at="2026-01-01T00:00:00+00:00",
        ages_seconds={"QQQ.SMART": 1.0},
    )
    context = BrokerReadinessContext(
        reconciliation_report=reconciliation,
        market_data_report=freshness,
        recovery_verified=True,
    )
    assert reconcile_broker_positions(context)
    assert recover_uncertain_orders(context)
    assert verify_market_data_freshness(context)
