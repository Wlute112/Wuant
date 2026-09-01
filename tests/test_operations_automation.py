from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

import pytest

from quant.ops.alerts import Alert, AlertDispatcher
from quant.ops.backups import RESTORE_CONFIRMATION, create_backup, restore_backup, verify_backup
from quant.ops.model_registry import ModelRegistry, PromotionPolicy
from quant.ops.state import OperationsStore
from quant.ops.supervisor import evaluate_snapshot
from quant.ops.validation import CampaignPolicy, evaluate_campaign


class _MemorySink:
    name = "memory"

    def __init__(self):
        self.alerts = []

    def send(self, alert):
        self.alerts.append(alert)


def _telemetry(**risk_overrides):
    risk = {
        "drawdown_pct": 1.0,
        "daily_pnl_pct": 0.1,
        "gross_leverage": 0.5,
        "execution_state": "ACTIVE",
        "reconciliation_state": "STRATEGY_CACHE_RECONCILED",
        "rails": {"kill_switch_pct": 10, "daily_loss_limit_pct": 2, "leverage_max": 1},
        "data_quality": {"healthy": True},
    }
    risk.update(risk_overrides)
    return {"risk": risk}


def test_supervisor_decisions_are_fail_closed_and_escalate_by_rail():
    assert evaluate_snapshot(None, age_seconds=None, max_age_seconds=10).action == "FREEZE_ENTRIES"
    assert evaluate_snapshot(_telemetry(), age_seconds=20, max_age_seconds=10).code == "TELEMETRY_STALE"
    assert evaluate_snapshot(_telemetry(gross_leverage=1.1), age_seconds=1, max_age_seconds=10).action == "FLATTEN"
    assert evaluate_snapshot(_telemetry(drawdown_pct=10), age_seconds=1, max_age_seconds=10).action == "KILL"
    assert evaluate_snapshot(_telemetry(), age_seconds=1, max_age_seconds=10).healthy


def test_alert_dispatch_is_deduplicated(tmp_path):
    store = OperationsStore(str(tmp_path / "ops.sqlite3"))
    sink = _MemorySink()
    dispatcher = AlertDispatcher(store, [sink], cooldown_seconds=60, attempts=1)
    alert = Alert("TEST", "WARNING", "test", {}, "unit")
    assert dispatcher.dispatch(alert)["memory"] == "sent"
    assert dispatcher.dispatch(alert) == {"status": "suppressed"}
    assert len(sink.alerts) == 1
    store.close()


def test_campaign_resets_after_unhealthy_observation(tmp_path):
    store = OperationsStore(str(tmp_path / "ops.sqlite3"))
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    payload = {"risk": {"drawdown_pct": 1, "orders": [], "fills": [{"id": 1}], "operator_alerts": [], "reconciliation_state": "STRATEGY_CACHE_RECONCILED"}}
    store.record_paper_observation("campaign", "job1", payload, healthy=True, observed_at=base)
    store.record_paper_observation("campaign", "job1", payload, healthy=False, observed_at=base + timedelta(hours=1))
    store.record_paper_observation("campaign", "job2", payload, healthy=True, observed_at=base + timedelta(days=1))
    store.record_paper_observation("campaign", "job2", payload, healthy=True, observed_at=base + timedelta(days=2))
    report = evaluate_campaign(
        store,
        "campaign",
        CampaignPolicy(minimum_clean_days=2, minimum_runtime_hours=23, minimum_healthy_fraction=0.5, minimum_fills=1),
    )
    assert report.ready
    assert report.clean_since.startswith("2026-01-02")
    store.close()


def test_model_promotion_requires_offline_and_paper_evidence(tmp_path):
    params = tmp_path / "params.json"
    params.write_text(json.dumps({"params": {"n_lags": 2}, "asset_class": "equity"}))
    optimization = tmp_path / "optimization.json"
    optimization.write_text(json.dumps({
        "oos_score": 1.2,
        "final_test": {"stressed_ratio": 0.5, "trades": 25},
        "oos_metrics": {"profit_factor": 1.2, "total_trades": 25, "max_drawdown_pct": 4.0},
    }))
    registry = ModelRegistry(str(tmp_path / "registry.sqlite3"))
    model = registry.register(str(params), optimization_path=str(optimization))
    evidence = registry.evaluate(
        model["model_id"],
        {"ready": True},
        PromotionPolicy(require_clean_revision=False),
    )
    assert evidence["eligible"]
    with pytest.raises(ValueError, match="confirmation"):
        registry.approve(model["model_id"], evidence=evidence, operator="tester", confirmation="yes")
    tampered = {**evidence, "offline": {**evidence["offline"], "oos_score": 99}}
    with pytest.raises(ValueError, match="evidence"):
        registry.approve(
            model["model_id"],
            evidence=tampered,
            operator="tester",
            confirmation=f"APPROVE MODEL {model['model_id']} FOR LIVE",
        )
    approved = registry.approve(
        model["model_id"],
        evidence=evidence,
        operator="tester",
        confirmation=f"APPROVE MODEL {model['model_id']} FOR LIVE",
    )
    assert approved["active"] and approved["status"] == "APPROVED"
    assert registry.params_path(model["model_id"], require_approved=True).endswith("_params.json")
    registry.close()


def test_backup_verification_and_guarded_restore(tmp_path):
    source = tmp_path / "state.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE state(value TEXT)")
        connection.execute("INSERT INTO state VALUES ('original')")
    backup = create_backup(str(tmp_path / "backups"), sqlite_paths=[str(source)])
    assert verify_backup(str(backup)) == (True, [])
    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE state SET value = 'changed'")
    with pytest.raises(ValueError, match="confirmation"):
        restore_backup(str(backup), confirmation="wrong")
    restore_backup(str(backup), confirmation=RESTORE_CONFIRMATION)
    with sqlite3.connect(source) as connection:
        assert connection.execute("SELECT value FROM state").fetchone()[0] == "original"
    backed_up = next((backup / "sqlite").iterdir())
    backed_up.write_bytes(b"corrupt")
    valid, errors = verify_backup(str(backup))
    assert not valid and errors
