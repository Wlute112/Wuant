from datetime import datetime, timezone

from quant.api import drift, jobs_routes


class _Manager:
    def get(self, job_id):
        return {"id": job_id, "kind": "paper", "config": {"asset_class": "equity"}}


def _fixtures():
    timestamp = "2026-01-02T15:00:00+00:00"
    baseline = {
        "asset_class": "equity", "bar_interval_minutes": 60,
        "model_chart": {"SPY": [
            {"ts": timestamp, "predicted_return": 0.001, "state_label": "Bull"},
            {"ts": "2026-01-02T16:00:00+00:00", "predicted_return": -0.002, "state_label": "Bear"},
        ]},
    }
    telemetry = {
        "asset_class": "equity", "bar_type": "SPY.SMART-1-HOUR-LAST-EXTERNAL",
        "as_of": datetime.now(timezone.utc).isoformat(),
        "series": {"SPY": [
            {"ts": timestamp, "close": 100, "predicted_return": 0.003, "state_label": "Bear"},
            {"ts": "2026-01-03T15:00:00+00:00", "close": 101, "predicted_return": -0.001, "state_label": "Bull"},
        ]},
        "risk": {"orders": [{"status": "rejected"}, {"status": "filled"}], "fills": []},
    }
    return baseline, telemetry


def test_review_distinguishes_paired_and_distribution_samples(monkeypatch):
    baseline, telemetry = _fixtures()
    monkeypatch.setattr(drift, "load_run", lambda _id: baseline)
    monkeypatch.setattr(drift, "load_telemetry", lambda _path: telemetry)
    config = drift.ReviewConfig(baseline_run_id="bt", min_matched=2)
    result = drift._review(_Manager().get("paper_1"), config)
    assert result["coverage"]["matched"] == 1
    assert result["metrics"]["prediction_mae_bps"]["status"] == "insufficient_sample"
    assert result["metrics"]["prediction_magnitude_shift_bps"]["status"] == "within_threshold"
    assert result["metrics"]["signal_disagreement_pct"]["uncertainty_95_pct"][1] > 0
    assert result["metrics"]["reject_rate_pct"]["value"] == 50


def test_alert_history_records_transition_only(monkeypatch, tmp_path):
    baseline, telemetry = _fixtures()
    monkeypatch.setattr(drift, "JOBS_DIR", tmp_path)
    monkeypatch.setattr(drift, "load_run", lambda _id: baseline)
    monkeypatch.setattr(drift, "load_telemetry", lambda _path: telemetry)
    monkeypatch.setattr(jobs_routes, "manager", _Manager())
    config = drift.ReviewConfig(baseline_run_id="bt", min_matched=1, max_reject_rate_pct=10)
    drift.save_config("paper_1", config)
    first = drift.get_review("paper_1")
    second = drift.get_review("paper_1")
    assert first["metrics"]["reject_rate_pct"]["status"] == "alert"
    assert len(second["alerts"]) == len(first["alerts"])
    telemetry["risk"]["orders"] = [{"status": "filled"}]
    cleared = drift.get_review("paper_1")
    assert any(item["metric"] == "reject_rate_pct" and item["state"] == "cleared" for item in cleared["alerts"])
