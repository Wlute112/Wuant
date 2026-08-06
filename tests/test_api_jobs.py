import json

import pytest
from fastapi import HTTPException

from quant.api import jobs_routes
from quant.api.schemas import LIVE_CONFIRM_PHRASE, LiveJobRequest, PaperJobRequest


class StubJobManager:
    def __init__(self):
        self.submitted = None

    def new_job_id(self, kind):
        return f"{kind}_test"

    def submit(self, kind, module, args, config=None, job_id=None):
        self.submitted = {
            "kind": kind,
            "module": module,
            "args": args,
            "config": config,
            "job_id": job_id,
        }
        return {"id": job_id, "kind": kind, "status": "running"}


def test_live_route_forwards_inline_params(monkeypatch, tmp_path):
    manager = StubJobManager()
    monkeypatch.setattr(jobs_routes, "manager", manager)
    monkeypatch.setattr(jobs_routes, "JOBS_DIR", tmp_path)

    result = jobs_routes.start_live(
        LiveJobRequest(
            tickers=["BTC"],
            host="127.0.0.1",
            port=4001,
            account_id="U123",
            params={"entry_threshold": 0.002},
            confirm=LIVE_CONFIRM_PHRASE,
        )
    )

    args = manager.submitted["args"]
    params_path = args[args.index("--params") + 1]
    assert json.loads((tmp_path / "live_test_params.json").read_text()) == {
        "entry_threshold": 0.002,
    }
    assert params_path == str(tmp_path / "live_test_params.json")
    assert "--live" in args
    assert result["warning"] == "LIVE TRADING ARMED - REAL CAPITAL AT RISK"


def test_paper_route_accepts_gateway_paper_port(monkeypatch, tmp_path):
    manager = StubJobManager()
    monkeypatch.setattr(jobs_routes, "manager", manager)
    monkeypatch.setattr(jobs_routes, "JOBS_DIR", tmp_path)

    jobs_routes.start_paper(
        PaperJobRequest(tickers=["BTC"], port=4002, account_id="DU123")
    )

    args = manager.submitted["args"]
    assert args[args.index("--port") + 1] == "4002"


@pytest.mark.parametrize("port", [7496, 4001])
def test_paper_route_rejects_known_live_ports(port):
    with pytest.raises(HTTPException, match="LIVE port"):
        jobs_routes.start_paper(PaperJobRequest(tickers=["BTC"], port=port))
