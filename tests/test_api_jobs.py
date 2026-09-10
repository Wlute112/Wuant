import json
import io
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from quant.api import jobs, jobs_routes
from quant.api.schemas import (
    BacktestJobRequest,
    CampaignSeedJobRequest,
    CampaignStageJobRequest,
    LIVE_CONFIRM_PHRASE,
    LiveJobRequest,
    OptimizeJobRequest,
    PaperJobRequest,
)


class StubJobManager:
    def __init__(self):
        self.submitted = None
        self.submissions = []
        self.links = []

    def new_job_id(self, kind):
        return f"{kind}_test"

    def submit(self, kind, module, args, config=None, job_id=None, parent_job_id=None):
        job_id = job_id or self.new_job_id(kind)
        submission = {
            "kind": kind,
            "module": module,
            "args": args,
            "config": config,
            "job_id": job_id,
            "parent_job_id": parent_job_id,
        }
        self.submissions.append(submission)
        if kind != "risk_supervisor":
            self.submitted = submission
        return {"id": job_id, "kind": kind, "status": "running"}

    def link_companion(self, parent_job_id, companion_job_id):
        self.links.append((parent_job_id, companion_job_id))
        return {"id": parent_job_id, "companion_job_ids": [companion_job_id]}


@pytest.mark.parametrize("route,request_model,flag", [
    (jobs_routes.start_backtest, BacktestJobRequest, "--params"),
    (jobs_routes.start_optimize, OptimizeJobRequest, "--structural-json"),
])
def test_equity_simulation_is_validated_and_forwarded_to_research_job(monkeypatch, tmp_path, route, request_model, flag):
    manager = StubJobManager()
    monkeypatch.setattr(jobs_routes, "manager", manager)
    monkeypatch.setattr(jobs_routes, "JOBS_DIR", tmp_path)
    csv = tmp_path / "bars.csv"
    csv.write_text("timestamp,ticker,open,high,low,close,volume\n2025-01-02,SPY,100,101,99,100,1000\n")
    request = request_model(csv=str(csv), asset_class="equity", tickers=["SPY"], equity_simulation={
        "spread_bps": 12, "participation": 0.02, "risk_free_rate": 0.04,
    })
    route(request)
    args = manager.submitted["args"]
    with open(args[args.index(flag) + 1]) as file:
        payload = json.load(file)
    assert payload["equity_simulation"] == request.equity_simulation.model_dump(mode="json")
    with pytest.raises(HTTPException) as error:
        route(request_model(asset_class="crypto", equity_simulation={}))
    assert error.value.status_code == 422
    with pytest.raises(ValueError):
        request_model(asset_class="equity", equity_simulation={"participation": 1})


def test_paper_session_policy_is_validated_forwarded_and_saved(monkeypatch, tmp_path):
    manager = StubJobManager()
    monkeypatch.setattr(jobs_routes, "manager", manager)
    monkeypatch.setattr(jobs_routes, "JOBS_DIR", tmp_path)
    req = PaperJobRequest(
        tickers=["QQQ"], asset_class="equity", include_extended_hours=True,
        session_policy={"mode": "CUSTOM", "custom_windows": [["10:00", "15:00"]],
                        "overnight_pnl_assignment": "PRIOR_SESSION"},
    )
    jobs_routes.start_paper(req)
    args = manager.submitted["args"]
    payload = json.loads(args[args.index("--session-policy-json") + 1])
    assert payload["custom_windows"] == [["10:00", "15:00"]]
    assert payload["overnight_pnl_assignment"] == "PRIOR_SESSION"
    assert payload["cancel_entries_at_session_end"] is True
    assert payload == json.loads(json.dumps(manager.submitted["config"]["session_policy"]))
    assert "--include-extended-hours" in args


@pytest.mark.parametrize("request_model,kwargs", [(PaperJobRequest, {}), (LiveJobRequest, {"port": 7496, "confirm": ""})])
def test_execution_api_rejects_conflicting_session_contract(request_model, kwargs):
    with pytest.raises(ValueError, match="include_extended_hours"):
        request_model(tickers=["QQQ"], asset_class="equity", session_policy={"mode": "EXTENDED_HOURS"}, **kwargs)


def test_optimize_route_forwards_nested_walk_forward_controls(monkeypatch, tmp_path):
    manager = StubJobManager()
    monkeypatch.setattr(jobs_routes, "manager", manager)
    monkeypatch.setattr(jobs_routes, "JOBS_DIR", tmp_path)

    jobs_routes.start_optimize(
        OptimizeJobRequest(
            tickers=["BTC", "ETH"],
            final_test_frac=0.15,
            walk_forward_folds=7,
            embargo_bars=4,
            workers=8,
            memory_budget_gb=48,
            worker_memory_gb=6,
        )
    )

    args = manager.submitted["args"]
    assert args[args.index("--final-test-frac") + 1] == "0.15"
    assert args[args.index("--walk-forward-folds") + 1] == "7"
    assert args[args.index("--embargo-bars") + 1] == "4"
    assert args[args.index("--workers") + 1] == "8"
    assert args[args.index("--memory-budget-gb") + 1] == "48.0"
    assert args[args.index("--worker-memory-gb") + 1] == "6.0"


@pytest.mark.parametrize(
    ("route", "payload", "expected"),
    [
        (
            jobs_routes.start_backtest,
            BacktestJobRequest(name="  QQQ   baseline  "),
            "QQQ baseline",
        ),
        (
            jobs_routes.start_optimize,
            OptimizeJobRequest(name="  Crypto   stability sweep  "),
            "Crypto stability sweep",
        ),
    ],
)
def test_research_job_name_is_normalized_and_forwarded(
    monkeypatch, tmp_path, route, payload, expected
):
    manager = StubJobManager()
    monkeypatch.setattr(jobs_routes, "manager", manager)
    monkeypatch.setattr(jobs_routes, "JOBS_DIR", tmp_path)

    route(payload)

    assert manager.submitted["config"]["name"] == expected
    args = manager.submitted["args"]
    assert args[args.index("--run-name") + 1] == expected
    progress_path = args[args.index("--progress-path") + 1]
    assert progress_path == str(tmp_path / f"{manager.submitted['job_id']}_progress.json")


def test_csv_upload_writes_a_job_scoped_file(monkeypatch, tmp_path):
    class FakeRequest:
        headers = {"content-length": "18"}

        async def stream(self):
            yield b"timestamp,close\n"
            yield b"1,100\n"

    monkeypatch.setattr(jobs_routes, "JOBS_DIR", tmp_path)
    result = asyncio.run(jobs_routes.upload_csv(FakeRequest(), "../QQQ bars.csv"))

    uploaded = tmp_path / "uploads" / result["path"].split("/")[-1]
    assert uploaded.read_bytes() == b"timestamp,close\n1,100\n"
    assert result["filename"] == "QQQ bars.csv"
    assert uploaded.name.startswith("QQQ-bars-")


def test_ibkr_replacement_frequency_rejects_non_native_widths():
    with pytest.raises(ValueError):
        BacktestJobRequest(ibkr={"replace_bars": True, "ibkr_bar_hours": 12})


def test_campaign_seed_route_maps_validation_contract(monkeypatch, tmp_path):
    manager = StubJobManager()
    monkeypatch.setattr(jobs_routes, "manager", manager)
    monkeypatch.setattr(jobs_routes, "CAMPAIGNS_DIR", tmp_path)

    csv = tmp_path / "bars.csv"
    csv.write_text("timestamp,ticker,open,high,low,close,volume\n2025-01-02,SPY,100,101,99,100,1000\n2025-01-02,QQQ,100,101,99,100,1000\n")
    job = jobs_routes.start_campaign_seeds(CampaignSeedJobRequest(
        campaign_id="equity_daily_v1",
        asset_class="equity",
        csv=str(csv),
        tickers=["SPY", "QQQ"],
        seeds=[42, 43, 44],
        trials=100,
    ))

    assert job["id"] == "campaign_seeds_test"
    assert manager.submitted["module"] == "quant.optimize.multi_seed"
    args = manager.submitted["args"]
    assert args[args.index("--campaign-id") + 1] == "equity_daily_v1"
    assert args[args.index("--seeds") + 1:args.index("--trials")] == ["42", "43", "44"]
    assert args[args.index("--manifest") + 1] == str(tmp_path / "equity_daily_v1.json")


def test_campaign_promotion_requires_phrase_and_robustness_report(monkeypatch, tmp_path):
    manager = StubJobManager()
    monkeypatch.setattr(jobs_routes, "manager", manager)
    monkeypatch.setattr(jobs_routes, "CAMPAIGNS_DIR", tmp_path)
    request = CampaignStageJobRequest(campaign_id="crypto_daily_v1")

    with pytest.raises(HTTPException, match="CONSUME OUTER HOLDOUT"):
        jobs_routes.start_campaign_promote(request)

    (tmp_path / "crypto_daily_v1_robustness.json").write_text("{}")
    job = jobs_routes.start_campaign_promote(CampaignStageJobRequest(
        campaign_id="crypto_daily_v1",
        confirm="CONSUME OUTER HOLDOUT",
    ))

    assert job["id"] == "campaign_promote_test"
    assert manager.submitted["module"] == "quant.optimize.promote"
    assert manager.submitted["config"]["confirm"] == "<redacted>"


def test_live_route_is_disabled_until_p0_gates_pass(monkeypatch, tmp_path):
    manager = StubJobManager()
    monkeypatch.setattr(jobs_routes, "manager", manager)
    monkeypatch.setattr(jobs_routes, "JOBS_DIR", tmp_path)

    with pytest.raises(HTTPException) as exc_info:
        jobs_routes.start_live(
            LiveJobRequest(
                tickers=["BTC"],
                host="127.0.0.1",
                port=4001,
                account_id="U123",
                params={"entry_threshold": 0.002},
                confirm=LIVE_CONFIRM_PHRASE,
            )
        )
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["code"] == "P0_PRODUCTION_READINESS_INCOMPLETE"
    assert manager.submitted is None


def test_paper_route_accepts_gateway_paper_port(monkeypatch, tmp_path):
    manager = StubJobManager()
    monkeypatch.setattr(jobs_routes, "manager", manager)
    monkeypatch.setattr(jobs_routes, "JOBS_DIR", tmp_path)

    jobs_routes.start_paper(
        PaperJobRequest(tickers=["QQQ"], asset_class="equity", port=4002, account_id="DU123")
    )

    args = manager.submitted["args"]
    assert args[args.index("--port") + 1] == "4002"
    assert manager.submitted["config"]["account_id"] == "<redacted-account>"


def test_equity_paper_route_forwards_session_and_telemetry_options(monkeypatch, tmp_path):
    manager = StubJobManager()
    monkeypatch.setattr(jobs_routes, "manager", manager)
    monkeypatch.setattr(jobs_routes, "JOBS_DIR", tmp_path)

    jobs_routes.start_paper(
        PaperJobRequest(
            tickers=["SPY"],
            asset_class="equity",
            port=7497,
            bar_hours=1,
            include_extended_hours=True,
        )
    )

    args = manager.submitted["args"]
    assert args[args.index("--bar-hours") + 1] == "1"
    assert "--include-extended-hours" in args
    assert args[args.index("--telemetry-path") + 1] == str(tmp_path / "paper_test_telemetry.json")
    assert "--require-external-supervisor" in args
    assert args[args.index("--operations-db") + 1] == str(tmp_path / "operations.sqlite3")
    supervisor = manager.submissions[1]
    assert supervisor["module"] == "quant.ops.supervisor"
    assert supervisor["parent_job_id"] == "paper_test"
    assert manager.links == [("paper_test", "risk_supervisor_test")]


@pytest.mark.parametrize("port", [7496, 4001])
def test_paper_route_rejects_known_live_ports(port):
    with pytest.raises(HTTPException, match="LIVE port"):
        jobs_routes.start_paper(PaperJobRequest(tickers=["QQQ"], asset_class="equity", port=port))


def test_paper_route_rejects_unsupported_crypto_paper():
    with pytest.raises(HTTPException, match="do not support spot-crypto"):
        jobs_routes.start_paper(PaperJobRequest(tickers=["BTC"], port=7497))


def test_paper_route_forwards_fail_closed_short_controls(monkeypatch, tmp_path):
    manager = StubJobManager()
    monkeypatch.setattr(jobs_routes, "manager", manager)
    monkeypatch.setattr(jobs_routes, "JOBS_DIR", tmp_path)

    jobs_routes.start_paper(
        PaperJobRequest(
            tickers=["QQQ"],
            asset_class="equity",
            port=7497,
            account_id="DU123",
            allow_shorts=True,
        )
    )

    args = manager.submitted["args"]
    assert "--allow-shorts" in args
    assert args[args.index("--short-control-client-id") + 1] == "29"
    assert args[args.index("--short-max-borrow-fee-pct") + 1] == "5.0"
    assert args[args.index("--short-min-margin-cushion-pct") + 1] == "20.0"


def test_paper_route_rejects_short_control_client_id_collision():
    with pytest.raises(HTTPException, match="client ID"):
        jobs_routes.start_paper(
            PaperJobRequest(
                tickers=["QQQ"],
                asset_class="equity",
                port=7497,
                client_id=29,
                allow_shorts=True,
            )
        )


def test_job_command_and_log_redaction_hide_account_identifiers(monkeypatch, tmp_path):
    command = ["python", "-m", "quant.run.run_live", "--account-id", "DU123456"]
    assert jobs._redact_command(command)[-1] == "<redacted-account>"
    assert "DU123456" not in jobs._redact_text(
        "Connected account DU123456\n",
        ("DU123456",),
    )

    monkeypatch.setenv("TWS_ACCOUNT", "U987654")
    monkeypatch.delenv("NAUTILUS_REDIS_PASSWORD", raising=False)
    assert jobs._sensitive_arg_values(["python", "-m", "module"]) == ("U987654",)
    assert jobs._redact_config(
        {
            "account_id": "DU123456",
            "nested": {"redis_password": "secret", "port": 6379},
        }
    ) == {
        "account_id": "<redacted>",
        "nested": {"redis_password": "<redacted>", "port": 6379},
    }
    log_path = tmp_path / "paper.log"
    jobs.JobManager._pump_logs(
        SimpleNamespace(stdout=io.StringIO("Connected account DU123456\n")),
        log_path,
        ("DU123456",),
    )
    assert log_path.read_text() == "Connected account <redacted-account>\n"
