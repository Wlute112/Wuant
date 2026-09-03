import json
import io
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from quant.api import jobs, jobs_routes
from quant.api.schemas import (
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
        )
    )

    args = manager.submitted["args"]
    assert args[args.index("--final-test-frac") + 1] == "0.15"
    assert args[args.index("--walk-forward-folds") + 1] == "7"
    assert args[args.index("--embargo-bars") + 1] == "4"


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
