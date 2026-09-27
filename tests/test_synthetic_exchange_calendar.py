from datetime import datetime, timezone

from quant.data.generate_sample_bars import generate, inspect_equity_fixture_calendar
from quant.data.research_preflight import inspect_frame
from quant.api import jobs_routes
from quant.api.schemas import SyntheticFixtureRequest


def _equity(start, count):
    return generate(["SPY"], n_days=count, start=datetime.fromisoformat(start).replace(tzinfo=timezone.utc),
                    seed=42, asset_class="equity")


def test_equity_fixture_uses_nyse_holidays_and_early_close():
    frame = _equity("2026-11-23", 6)
    rows = frame.set_index("session_date")
    assert "2026-11-26" not in rows.index  # Thanksgiving
    assert rows.loc["2026-11-27", "session_minutes"] == 210
    assert bool(rows.loc["2026-11-27", "early_close"])
    assert rows.loc["2026-11-27", "timestamp"] == "2026-11-27T18:00:00+00:00"
    assert inspect_equity_fixture_calendar(frame)["errors"] == []


def test_equity_fixture_utc_open_tracks_dst_and_preflight_checks_schedule():
    frame = _equity("2026-03-06", 4)
    rows = frame.set_index("session_date")
    assert rows.loc["2026-03-06", "session_open_utc"].endswith("14:30:00+00:00")
    assert rows.loc["2026-03-09", "session_open_utc"].endswith("13:30:00+00:00")
    assert rows.loc["2026-03-09", "session_close_utc"].endswith("20:00:00+00:00")
    report = inspect_frame(frame, ["SPY"], "equity")
    assert report["execution_eligible"]
    assert report["tickers"][0]["fixture_calendar"]["utc_open_times"] == ["13:30", "14:30"]
    assert any("Synthetic fixture" in warning for warning in report["warnings"])
    assert any("150 warmup bars" in warning for warning in report["warnings"])

    revised = frame.copy()
    revised.loc[1, "session_open_utc"] = "2026-03-09T14:30:00+00:00"
    assert not inspect_frame(revised, ["SPY"], "equity")["execution_eligible"]


def test_crypto_fixture_remains_24_7():
    frame = generate(["BTC"], n_days=3, start=datetime(2026, 11, 26, tzinfo=timezone.utc),
                     asset_class="crypto")
    assert frame.timestamp.tolist() == ["2026-11-26", "2026-11-27", "2026-11-28"]
    assert frame.source.tolist() == ["synthetic_fixture"] * 3
    assert "calendar_version" not in frame.columns


def test_dashboard_fixture_job_is_scoped_and_reviewable(monkeypatch):
    class Manager:
        def new_job_id(self, kind):
            assert kind == "synthetic_fixture"
            return "synthetic_fixture_123"

        def submit(self, kind, module, args, *, config, job_id):
            assert kind == "synthetic_fixture"
            assert module == "quant.data.generate_sample_bars"
            assert config["csv"].endswith("synthetic_fixture_123.csv")
            assert config["seed"] == 42
            assert args[args.index("--progress-path") + 1].endswith("synthetic_fixture_123_progress.json")
            return {"id": job_id, "kind": kind, "config": config}

    monkeypatch.setattr(jobs_routes, "manager", Manager())
    result = jobs_routes.generate_synthetic_fixture(SyntheticFixtureRequest(
        tickers=["spy"], days=12, start="2026-11-23", seed=42,
    ))
    assert result["config"]["tickers"] == ["SPY"]
