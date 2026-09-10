import asyncio
import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest
from fastapi import HTTPException

from quant.api import jobs_routes
from quant.api.schemas import BacktestJobRequest, OptimizeJobRequest, CampaignSeedJobRequest, DataPreflightRequest, DataRepairRequest
from quant.data import ibkr_fetch
from quant.data.research_preflight import inspect_csv, inspect_frame, require_execution_data
from quant.run.backtest_common import build_engine


def bars(volume=(100, 0), ticker="QQQ"):
    return pd.DataFrame({"timestamp": ["2025-01-02T14:30:00Z", "2025-01-02T18:30:00Z"],
                         "ticker": ticker, "open": 100, "high": 102, "low": 98,
                         "close": 101, "volume": list(volume)})


def test_zero_volume_blocks_execution_but_report_remains_reviewable(tmp_path):
    path = tmp_path / "bars.csv"
    bars((0, 0)).to_csv(path, index=False)
    report = inspect_csv(path, ["QQQ"], "equity")
    assert not report["execution_eligible"]
    assert report["tickers"][0]["zero_volume_bars"] == 2
    assert "TRADES" in report["errors"][0]
    assert report["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    json.dumps(report, allow_nan=False)
    with pytest.raises(ValueError, match="No positive-volume"):
        build_engine(str(path), ["QQQ"], asset_class="equity")


def test_partial_liquidity_and_unknown_metadata_are_explicit():
    report = inspect_frame(bars(), ["QQQ"], "equity")
    assert report["execution_eligible"]
    row = report["tickers"][0]
    assert row["zero_volume_bars"] == 1
    assert row["cadence_minutes"] == 240
    assert row["price_basis"] == row["session"] == "unknown"
    assert report["warnings"]


@pytest.mark.parametrize("column,value", [("volume", None), ("volume", -1), ("volume", float("inf")),
                                            ("open", 0), ("close", float("nan")), ("high", 90),
                                            ("timestamp", "bad")])
def test_invalid_rows_fail_closed(column, value):
    frame = bars().astype({column: "object"})
    frame.loc[0, column] = value
    report = inspect_frame(frame, ["QQQ"], "equity")
    assert not report["execution_eligible"]
    json.dumps(report, allow_nan=False)


def test_missing_ticker_and_duplicate_timestamp_fail_closed():
    assert not inspect_frame(bars(), ["SPY"], "equity")["execution_eligible"]
    frame = bars()
    frame.loc[1, "timestamp"] = frame.loc[0, "timestamp"]
    assert "Duplicate" in inspect_frame(frame, ["QQQ"], "equity")["errors"][0]


def test_midpoint_metadata_cannot_claim_positive_liquidity():
    frame = bars()
    frame["volume_basis"] = "unavailable_midpoint"
    assert not inspect_frame(frame, ["QQQ"], "equity")["execution_eligible"]


def test_unreadable_and_empty_csv_have_actionable_reports(tmp_path):
    path = tmp_path / "data.csv"
    assert not inspect_csv(path, ["QQQ"], "equity")["execution_eligible"]
    path.write_text("")
    assert "Cannot read" in inspect_csv(path, ["QQQ"], "equity")["errors"][0]
    path.write_text("ticker,close\nQQQ,100\n")
    assert "Missing CSV columns" in inspect_csv(path, ["QQQ"], "equity")["errors"][0]


@pytest.mark.parametrize("route,model,extra", [
    (jobs_routes.start_backtest, BacktestJobRequest, {}),
    (jobs_routes.start_optimize, OptimizeJobRequest, {}),
    (jobs_routes.start_campaign_seeds, CampaignSeedJobRequest, {"campaign_id": "test"}),
])
def test_api_rechecks_dataset_before_submission(tmp_path, route, model, extra):
    path = tmp_path / "data.csv"
    bars().to_csv(path, index=False)
    request = model(csv=str(path), tickers=["QQQ"], asset_class="equity", **extra)
    assert jobs_routes.research_preflight(DataPreflightRequest(csv=str(path), tickers=["QQQ"]))["execution_eligible"]
    bars((0, 0)).to_csv(path, index=False)
    with pytest.raises(HTTPException) as error:
        route(request)
    assert error.value.status_code == 422
    assert error.value.detail["report"]["tickers"][0]["zero_volume_bars"] == 2


@pytest.mark.parametrize("replacement", [bars((0, 0)), bars(ticker="SPY")])
def test_invalid_or_partial_replacement_preserves_original(monkeypatch, tmp_path, replacement):
    path = tmp_path / "bars.csv"
    original = b"original data retained byte for byte\n"
    path.write_bytes(original)
    async def fetch(*args, **kwargs):
        return replacement
    monkeypatch.setattr(ibkr_fetch, "_fetch", fetch)
    with pytest.raises(ValueError, match="preflight blocked"):
        ibkr_fetch.replace_bars(str(path), ["QQQ"], asset_class="equity")
    assert path.read_bytes() == original
    assert not list(tmp_path.glob("*.tmp"))


def test_valid_repair_preserves_volume_and_original_backup(monkeypatch, tmp_path):
    path = tmp_path / "bars.csv"
    original = b"broken source data\n"
    path.write_bytes(original)
    async def fetch(*args, **kwargs):
        return bars()
    monkeypatch.setattr(ibkr_fetch, "_fetch", fetch)
    assert ibkr_fetch.replace_bars(str(path), ["QQQ"], asset_class="equity") == 2
    assert pd.read_csv(path).volume.tolist() == [100, 0]
    assert list(tmp_path.glob("*.bak"))[0].read_bytes() == original


def test_repair_fetch_failure_preserves_original(monkeypatch, tmp_path):
    path = tmp_path / "bars.csv"
    path.write_bytes(b"original")
    async def fetch(*args, **kwargs):
        raise TimeoutError("broker unavailable")
    monkeypatch.setattr(ibkr_fetch, "_fetch", fetch)
    with pytest.raises(TimeoutError):
        ibkr_fetch.replace_bars(str(path), ["QQQ"], asset_class="equity")
    assert path.read_bytes() == b"original"


def test_equity_and_crypto_default_historical_price_types(monkeypatch):
    from nautilus_trader.adapters.interactive_brokers.historical import client
    requested = []
    class FakeClient:
        def __init__(self, **kwargs):
            pass
        async def connect(self):
            pass
        async def request_bars(self, **kwargs):
            requested.append(kwargs["bar_specifications"])
            return []
    monkeypatch.setattr(client, "HistoricInteractiveBrokersClient", FakeClient)
    monkeypatch.setattr(ibkr_fetch, "register_zerohash_crypto", lambda *args: None)
    for asset in ["equity", "crypto"]:
        with pytest.raises(SystemExit, match="No bars"):
            asyncio.run(ibkr_fetch._fetch(["QQQ"], 1, "localhost", 7497, 71, "SMART", None, 4,
                                         "REALTIME", 1, asset_class=asset))
    assert requested == [["4-HOUR-LAST"], ["4-HOUR-MID"]]


def test_native_daily_fetch_does_not_resample_intraday_volume():
    assert ibkr_fetch._plan_bars(24, "LAST") == ("1-DAY-LAST", None)


def test_invalid_fetched_missing_equity_does_not_modify_existing(monkeypatch, tmp_path):
    path = tmp_path / "bars.csv"
    bars().to_csv(path, index=False)
    original = path.read_bytes()
    async def fetch(*args, **kwargs):
        return bars((0, 0), "SPY")
    monkeypatch.setattr(ibkr_fetch, "_fetch", fetch)
    with pytest.raises(ValueError, match="No positive-volume"):
        asyncio.run(ibkr_fetch._fetch_and_merge(path, ["QQQ", "SPY"], 1, "localhost", 7497,
                    71, "SMART", None, 4, "REALTIME", 1, asset_class="equity"))
    assert path.read_bytes() == original


def test_repair_route_uses_managed_job_with_reviewable_progress(monkeypatch, tmp_path):
    class Manager:
        def new_job_id(self, kind):
            return "repair_test"
        def submit(self, kind, module, args, **kwargs):
            return {"kind": kind, "module": module, "args": args, **kwargs}
    monkeypatch.setattr(jobs_routes, "manager", Manager())
    monkeypatch.setattr(jobs_routes, "JOBS_DIR", tmp_path)
    job = jobs_routes.repair_research_data(DataRepairRequest(csv="data.csv", tickers=["QQQ"],
        ibkr={"ibkr_port": 4002, "ibkr_bar_hours": 24, "include_extended_hours": True}))
    assert job["kind"] == "data_repair"
    assert job["module"] == "quant.data.research_preflight"
    assert "--repair" in job["args"] and "--include-extended-hours" in job["args"]
    assert job["args"][job["args"].index("--progress-path") + 1] == str(tmp_path / "repair_test_progress.json")
    assert "4002" in job["args"]


@pytest.mark.parametrize("module", ["quant.run.run_backtest", "quant.optimize.optimize"])
def test_cli_blocks_before_simulation_or_optimizer_trials(tmp_path, module):
    import subprocess
    import sys
    path = tmp_path / "zero.csv"
    bars((0, 0)).to_csv(path, index=False)
    result = subprocess.run([sys.executable, "-m", module, "--csv", str(path),
                             "--asset-class", "equity", "--tickers", "QQQ"],
                            cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert "Research preflight blocked execution" in result.stderr
