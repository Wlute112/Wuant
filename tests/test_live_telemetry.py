import pytest
from fastapi import HTTPException

from quant.api import jobs_routes, live_mock
from quant.run.telemetry import LiveTelemetryRecorder, load_telemetry


def test_live_telemetry_replaces_same_bar_and_writes_complete_snapshot(tmp_path):
    path = tmp_path / "paper_telemetry.json"
    recorder = LiveTelemetryRecorder(
        str(path),
        asset_class="equity",
        mode="paper",
        bar_type="-1-HOUR-LAST-EXTERNAL",
        max_points=50,
    )
    common = {
        "ts": "2026-01-02T15:00:00+00:00",
        "open": 100.0,
        "high": 102.0,
        "low": 99.0,
        "close": 101.0,
    }
    recorder.record("SPY", common, positions=[], risk={"equity": 5000.0}, model={})
    recorder.record(
        "SPY",
        {**common, "predicted_return": 0.01, "signal": "BUY"},
        positions=[{"symbol": "SPY", "qty": 2}],
        risk={"equity": 5001.0},
        model={"protective_orders_submitted": False},
    )

    payload = load_telemetry(path)
    assert payload["schema_version"] == 2
    assert payload["bar_update_policy"] == "completed_strategy_bars"
    assert payload["mock"] is False
    assert payload["asset_class"] == "equity"
    assert len(payload["series"]["SPY"]) == 1
    assert payload["series"]["SPY"][0]["signal"] == "BUY"
    assert payload["positions"][0]["qty"] == 2

    recorder.refresh(
        positions=[{"symbol": "SPY", "qty": 3}],
        risk={"equity": 5002.0},
        model={"protective_orders_submitted": False},
    )
    refreshed = load_telemetry(path)
    assert len(refreshed["series"]["SPY"]) == 1
    assert refreshed["positions"][0]["qty"] == 3

    restored_path = tmp_path / "restored.json"
    restored = LiveTelemetryRecorder(
        str(restored_path),
        asset_class="equity",
        mode="paper",
        bar_type="-1-HOUR-LAST-EXTERNAL",
    )
    restored.restore_series(refreshed["series"])
    restored.refresh(positions=[], risk={}, model={})
    assert load_telemetry(restored_path)["series"]["SPY"] == refreshed["series"]["SPY"]


def test_live_endpoint_demo_is_explicit_and_asset_specific(monkeypatch):
    monkeypatch.setattr(jobs_routes, "manager", None)

    equity = live_mock.get_telemetry(asset_class="equity")
    crypto = live_mock.get_telemetry(asset_class="crypto")

    assert equity["mock"] is True
    assert equity["tickers"] == ["QQQ"]
    assert equity["profile"]["scoring"]["metric"] == "sharpe"
    assert crypto["tickers"] == ["BTC"]
    assert crypto["profile"]["scoring"]["metric"] == "sortino"
    assert equity["model"]["protective_orders_submitted"] is False
    assert equity["schema_version"] == 2
    assert equity["positions"][0]["stop_loss"] is not None
    assert equity["positions"][0]["take_profit"] is not None
    assert equity["series"]["QQQ"][-1]["forecast"]["basis"] == "huber_close_atr_envelope"
    referenced = [point for point in equity["series"]["QQQ"] if point["stop_reference"] is not None]
    assert referenced
    assert all(point["signal"] in {"BUY", "SELL"} for point in referenced)


def test_live_job_matching_never_crosses_workflow_mode(monkeypatch):
    jobs = [
        {
            "id": "paper_equity",
            "kind": "paper",
            "status": "running",
            "config": {"asset_class": "equity"},
        },
        {
            "id": "live_equity",
            "kind": "live",
            "status": "running",
            "config": {"asset_class": "equity"},
        },
    ]

    class Manager:
        def list(self):
            return jobs

        def get(self, job_id):
            return next((job for job in jobs if job["id"] == job_id), None)

    monkeypatch.setattr(jobs_routes, "manager", Manager())
    assert live_mock._matching_job(None, "equity", "paper")["id"] == "paper_equity"
    assert live_mock._matching_job(None, "equity", "live")["id"] == "live_equity"
    with pytest.raises(HTTPException, match="mode"):
        live_mock._matching_job("live_equity", "equity", "paper")
