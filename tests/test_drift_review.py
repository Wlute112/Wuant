from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import math
from types import SimpleNamespace

from fastapi import FastAPI
import pytest
from pydantic import ValidationError

from quant.api import drift_routes, jobs_routes
from quant.ops.drift import DriftStore, Evidence, ReviewConfig, compare
from quant.ops.drift_statistics import summarize
from quant.run.drift_evidence import digest, research_evidence, review_contract, telemetry_evidence
from quant.strategies.ml_strategy import MLStrategyConfig
from quant.tests.test_operations_api import ASGIClient, TOKEN


def contract(**changes):
    return review_contract(MLStrategyConfig(instrument_ids=["QQQ.SIM"], asset_class="equity", **changes).dict())


def evidence(source="baseline", kind="research", prediction=0.01, actual=0.01, count=40, *, start=None, version=None):
    start = start or datetime(2026, 1, 1, tzinfo=timezone.utc)
    samples = [{"id": f"f{i}", "ticker": "QQQ", "regime": "Bull", "ts": (start + timedelta(days=i)).isoformat(),
        "target_ts": (start + timedelta(days=i + 1)).isoformat(), "prediction": prediction, "actual": actual,
        "slippage_bps": 1.0, "rejected": 0, "feature_age_seconds": 0.0} for i in range(count)]
    return {"source_id": source, "kind": kind, "reference": "Deterministic test fixture, not market evidence",
            "contract": version or contract(), "as_of": datetime.now(timezone.utc).isoformat(), "samples": samples}


def configured(db):
    db.ingest(evidence(), "test")
    baseline = db.baseline("baseline", "Reviewed test baseline", "test")
    db.configure(ReviewConfig(baseline_id=baseline["id"], reason="Test review policy"), "test")
    return baseline


def test_statistics_signed_intervals_and_counts():
    rows = [{"prediction": -4.0, "actual": 2.0, "slippage_bps": -20.0, "rejected": 1, "feature_age_seconds": 100.0},
            {"prediction": -2.0, "actual": -2.0, "slippage_bps": -10.0, "rejected": 0, "feature_age_seconds": 200.0}]
    stats = summarize(rows, 2)
    assert stats["prediction_mean"]["mean"] == -3
    assert stats["prediction_mean"]["ci95"] == pytest.approx([-4.96, -1.04])
    assert stats["slippage_bps"]["ci95"] == pytest.approx([-24.8, -5.2])
    assert stats["absolute_error"]["mean"] == 3
    assert stats["absolute_error"]["ci95"][1] > 1
    assert stats["feature_age_seconds"]["ci95"][1] > 200
    assert stats["direction_accuracy"]["mean"] == stats["reject_rate"]["mean"] == 0.5
    assert stats["reject_rate"]["ci95"] == [0, 1]


def test_statistics_partial_empty_boolean_nonfinite_and_no_mutation():
    rows = [{"prediction": 1.0, "actual": 0.0}, {"prediction": True, "actual": 1.0, "rejected": False},
            {"prediction": float("nan"), "slippage_bps": float("inf"), "feature_age_seconds": -1}, {}]
    stats = summarize(rows, 2)
    assert len(rows) == 4 and rows[1]["prediction"] is True
    assert stats["prediction_mean"] == {"n": 1, "mean": 1.0, "ci95": None, "status": "insufficient"}
    assert stats["reject_rate"]["n"] == 0
    assert all(v["status"] == "insufficient" for v in summarize([], 30).values())


def test_version_changes_for_model_universe_code_and_data_not_account():
    original = contract()
    assert original == contract(account_id="IB-DU123", execution_mode="paper", telemetry_path="/tmp/telemetry.json")
    assert original != contract(horizon=2)
    assert original != contract(huber_alpha=0.01)
    assert original != contract(bar_type_suffix="-1-HOUR-LAST-EXTERNAL")
    assert original != review_contract(MLStrategyConfig(instrument_ids=["SPY.SMART"], asset_class="equity").dict())
    assert original != review_contract(MLStrategyConfig(instrument_ids=["QQQ.SMART"], asset_class="equity").dict(), extended=True)


@pytest.mark.parametrize("change", [
    lambda d: d["contract"].update(model_version="wrong"),
    lambda d: d["samples"][0].update(ts="2026-01-01"),
    lambda d: d["samples"][0].update(target_ts="2025-01-01T00:00:00Z"),
    lambda d: d["samples"][0].update(actual=float("nan")),
    lambda d: d["samples"][0].update(target_ts=None),
    lambda d: d["samples"][0].update(ticker="SPY"),
    lambda d: d["samples"].append(d["samples"][0]),
    lambda d: d.update(as_of="2099-01-01T00:00:00Z"),
])
def test_bad_evidence_rejected(change):
    data = evidence()
    change(data)
    with pytest.raises((ValidationError, ValueError)):
        Evidence.model_validate(data)


def test_persisted_review_breach_recovery_dedup_and_pause(tmp_path):
    path = tmp_path / "drift.sqlite3"
    with DriftStore(path) as db:
        configured(db)
        db.ingest(evidence("shadow", "shadow", prediction=-0.02), "test")
        report = db.review()
        metrics = report["sources"][0]["groups"]["All"]
        assert metrics["prediction_mean"]["status"] == "breach"
        assert metrics["direction_accuracy"]["status"] == "breach"
        assert metrics["absolute_error"]["status"] == "breach"
        assert metrics["slippage_bps"]["status"] == "within_threshold"
        assert metrics["prediction_mean"]["current"]["n"] == 40
        alerts = len(db.history("alert"))
        db.review()
        assert len(db.history("alert")) == alerts
        # Collector corrections replace existing IDs, never count duplicate polls.
        db.ingest(evidence("shadow", "shadow"), "collector", automatic=True)
        db.review()
        assert any(a["status"] == "within_threshold" and a["previous"] == "breach" for a in db.history("alert"))
        assert len(db.evidence("shadow")["samples"]) == 40
        db.stop("test")
        with pytest.raises(ValueError, match="enable"):
            db.review()
    with DriftStore(path) as reopened:
        assert reopened.config()["enabled"] is False
        assert len(reopened.history("report")) == 3
        assert reopened.history("baseline")[0]["evidence"]["samples"][0]["prediction"] == 0.01
        with pytest.raises(Exception, match="immutable"):
            reopened.db.execute("DELETE FROM documents")


def test_missing_mismatched_stale_overlapping_and_small_sources(tmp_path):
    with DriftStore(tmp_path / "review.db") as db:
        configured(db)
        assert db.review()["stage_coverage"] == {"research": 0, "shadow": 0, "paper": 0}
        db.ingest(evidence("mismatch", "paper", version=contract(horizon=2)), "test")
        stale = evidence("stale", "shadow")
        stale["as_of"] = "2026-06-01T00:00:00Z"
        db.ingest(stale, "test")
        db.ingest(evidence("overlap", "research"), "test")
        small = evidence("small", "paper", count=1)
        small["samples"][0].pop("slippage_bps")
        db.ingest(small, "test")
        states = {s["source_id"]: s for s in db.review()["sources"]}
        assert states["mismatch"]["status"] == "incompatible"
        assert states["stale"]["status"] == "stale"
        assert states["overlap"]["status"] == "overlap"
        stats = states["small"]["groups"]["All"]
        assert stats["prediction_mean"]["status"] == "insufficient"
        assert stats["slippage_bps"]["current"]["mean"] is None


def test_imports_immutable_and_changed_identity_requires_new_source(tmp_path):
    with DriftStore(tmp_path / "review.db") as db:
        data = evidence()
        db.ingest(data, "test")
        db.ingest(data, "test")
        data["samples"][0]["prediction"] = 0.02
        with pytest.raises(ValueError, match="immutable"):
            db.ingest(data, "test")
        data["contract"] = contract(horizon=2)
        with pytest.raises(ValueError, match="identity"):
            db.ingest(data, "collector", automatic=True)


def test_nonoverlapping_targets_and_regime_absence():
    data = evidence(count=10)["samples"]
    for i, row in enumerate(data):
        row["target_ts"] = (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=i + 3)).isoformat()
    config = ReviewConfig(baseline_id="test", reason="Test review config", minimum_samples=2).model_dump()
    result = compare(data, deepcopy(data), config)
    assert result["All"]["prediction_mean"]["current"]["n"] == 4
    bear = deepcopy(data)
    for row in bear:
        row["regime"] = "Bear"
    result = compare(data, bear, config)
    assert result["Bull"]["prediction_mean"]["current"]["n"] == 0
    assert result["Bear"]["prediction_mean"]["status"] == "insufficient"


def test_paper_baseline_compares_only_later_observations_in_same_session(tmp_path):
    with DriftStore(tmp_path / "review.db") as db:
        original = evidence("paper_baseline", "paper")
        db.ingest(original, "test", automatic=True)
        baseline = db.baseline("paper_baseline", "Reviewed first window", "test")
        db.configure(ReviewConfig(baseline_id=baseline["id"], reason="Reviewed monitoring policy"), "test")
        assert db.review()["sources"][0]["status"] == "awaiting"
        later = evidence("paper_baseline", "paper", prediction=-0.02, start=datetime(2026, 3, 1, tzinfo=timezone.utc))
        for row in later["samples"]:
            row["id"] = "new-" + row["id"]
        db.ingest(later, "collector", automatic=True)
        current = db.review()["sources"][0]
        assert current["baseline_cutoff"] == "2026-02-10T00:00:00+00:00"
        assert current["groups"]["All"]["prediction_mean"]["current"]["n"] == 40
        assert current["groups"]["All"]["prediction_mean"]["status"] == "breach"
        assert db.get_baseline(baseline["id"])["evidence"]["samples"][0]["prediction"] == 0.01


def telemetry():
    return {"mock": False, "mode": "paper", "as_of": "2026-06-01T00:00:00Z",
        "model": {"review_contract": contract(horizon=2)}, "series": {"QQQ": [
            {"ts": f"2026-01-0{i+1}T21:00:00Z", "close": 100.0 + i, "predicted_return": 0.01, "state_label": "Bull", "feature_age_seconds": 60.0} for i in range(5)]},
        "risk": {"orders": [{"client_order_id": "order1", "instrument_id": "QQQ.SMART", "side": "SELL", "role": "ENTRY",
            "status": "FILLED", "filled_quantity": 1.0, "average_fill_price": 99.0,
            "submitted_ts_ns": int(datetime(2026, 1, 1, 21, 1, tzinfo=timezone.utc).timestamp() * 1e9)}]}}


def test_telemetry_horizon_maturation_sign_and_pending_orders():
    payload = telemetry()
    data = telemetry_evidence(payload, "paper_test")
    Evidence.model_validate(data)
    assert data["samples"][0]["actual"] == pytest.approx(math.log(102 / 100))
    assert "actual" not in data["samples"][-2]
    assert data["samples"][-1]["slippage_bps"] == pytest.approx(100)
    assert data["samples"][-1]["rejected"] == 0
    payload["risk"]["orders"][0]["status"] = "SUBMITTED"
    assert len(telemetry_evidence(payload, "paper_test")["samples"]) == 5
    payload["mock"] = True
    with pytest.raises(ValueError, match="non-demo"):
        telemetry_evidence(payload, "paper_test")


def test_research_import_excludes_holdout_and_old_runs():
    run = {"kind": "backtest", "run_id": "bt_test", "finished_at": "2026-06-01T00:00:00Z", "review_contract": contract(),
        "ml_performance": {"QQQ": {"price_series": [{"decision_ts": "2026-01-01T00:00:00Z", "target_ts": "2026-01-02T00:00:00Z",
            "predicted_return": 0.01, "actual_return": 0.02}]}}}
    Evidence.model_validate(research_evidence(run))
    run["kind"] = "optimize"
    with pytest.raises(ValueError, match="holdout"):
        research_evidence(run)
    run.pop("review_contract")
    with pytest.raises(ValueError, match="predates"):
        research_evidence(run)


@pytest.fixture
def api_setup(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANT_CONTROL_TOKEN", TOKEN)
    monkeypatch.setattr(drift_routes, "JOBS_DIR", tmp_path)
    monkeypatch.setattr(jobs_routes, "manager", SimpleNamespace(list=lambda: [], get=lambda _: None))
    app = FastAPI()
    app.include_router(drift_routes.router)
    client = ASGIClient(app)
    client.headers["Authorization"] = f"Bearer {TOKEN}"
    return client, tmp_path


def test_api_auth_import_declare_config_review_export_stop(api_setup):
    client, _ = api_setup
    assert client.get("/api/drift", headers={"Authorization": "wrong"}).status_code == 401
    assert client.post("/api/drift/evidence", json=evidence()).status_code == 200
    exported = client.get("/api/drift/evidence/baseline").json()
    assert len(exported["samples"]) == 40
    base = client.post("/api/drift/baselines", json={"source_id": "baseline", "reason": "Reviewed research evidence"}).json()
    config = ReviewConfig(baseline_id=base["id"], reason="Reviewed thresholds").model_dump()
    assert client.post("/api/drift/config", json=config).status_code == 200
    assert client.post("/api/drift/evidence", json=evidence("shadow", "shadow")).status_code == 200
    response = client.post("/api/drift/review")
    assert response.status_code == 200
    assert response.json()["sources"][0]["status"] == "reviewed"
    assert client.post("/api/drift/stop").json()["enabled"] is False
    assert client.post("/api/drift/review").status_code == 409
    assert client.get("/api/drift").json()["reports"]


def test_api_collector_scheduler_missing_then_recovers(api_setup, monkeypatch):
    client, path = api_setup
    with drift_routes.store() as db:
        configured(db)
    job = {"id": "paper_test", "kind": "paper", "status": "running"}
    monkeypatch.setattr(jobs_routes, "manager", SimpleNamespace(list=lambda: [job], get=lambda _: job))
    drift_routes.schedule_once()
    assert client.get("/api/drift").json()["reports"][0]["source_errors"][0]["source_id"] == "paper_test"
    payload = telemetry()
    payload["as_of"] = datetime.now(timezone.utc).isoformat()
    (path / "paper_test_telemetry.json").write_text(json.dumps(payload))
    assert client.post("/api/drift/paper/paper_test").status_code == 200
    drift_routes.schedule_once()
    state = client.get("/api/drift").json()
    assert not state["reports"][0]["source_errors"]
    assert state["sources"][-1]["sample_count"] == 6
    assert state["reports"][0]["sources"][0]["status"] == "incompatible"


def test_api_remote_and_bad_config_rejected(api_setup):
    client, _ = api_setup
    client.client = ("198.51.100.1", 50000)
    assert client.get("/api/drift").status_code == 403
    client.client = ("127.0.0.1", 50000)
    assert client.post("/api/drift/config", json={"baseline_id": "missing", "reason": "Reviewed test policy"}).status_code == 422
    assert client.post("/api/drift/config", json={"baseline_id": "missing", "reason": "Reviewed test policy", "thresholds": {"invented": 1}}).status_code == 422


def test_real_backtest_artifact_and_telemetry_share_resolved_identity(tmp_path, monkeypatch):
    import time
    import pandas as pd
    from quant.run import artifacts
    from quant.run.backtest_common import build_and_run, VENUE

    csv = tmp_path / "fixture.csv"
    dates = pd.date_range("2025-01-01", periods=175, tz="UTC")
    close = [100 + i * 0.02 + math.sin(i / 4) for i in range(len(dates))]
    pd.DataFrame({"timestamp": dates, "ticker": "BTC", "open": close,
        "close": close, "high": [v + 1 for v in close], "low": [v - 1 for v in close],
        "volume": 10000}).to_csv(csv, index=False)
    params = {"use_news_features": False, "use_hmm_feature": False, "use_regime_features": False,
        "telemetry_path": str(tmp_path / "telemetry.json"), "telemetry_mode": "paper", "entry_threshold": 1.0}
    engine = build_and_run(str(csv), ["BTC"], strategy_overrides=params, bypass_logging=True)
    monkeypatch.setattr(artifacts, "RUNS_DIR", tmp_path / "runs")
    try:
        artifacts.save_backtest_artifact(engine=engine, venue=VENUE, starting_cash=5000,
            csv_path=str(csv), tickers=["BTC"], asset_class="crypto", overrides=params, started_at=time.time(), run_id="bt_smoke")
        run = artifacts.load_run("bt_smoke")
        live = json.loads((tmp_path / "telemetry.json").read_text())
        assert run["review_contract"] == live["model"]["review_contract"]
        Evidence.model_validate(research_evidence(run))
        Evidence.model_validate(telemetry_evidence(live, "paper_smoke"))
        assert research_evidence(run)["samples"]
    finally:
        engine.dispose()
