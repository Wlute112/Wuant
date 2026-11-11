import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
import pytest

from quant.api import execution_ownership as routes, jobs_routes
from quant.api.schemas import PaperJobRequest
from quant.run import execution_ownership as ownership, run_live
from quant.run.execution_ownership import ExecutionOwnership, ExecutionOwnershipError, OwnedTradingNode
from quant.strategies.execution_state import ExecutionSafetyState
from quant.strategies.ml_strategy import MLStrategy, MLStrategyConfig
from quant.tests.test_operations_api import ASGIClient


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(ownership, "OWNERSHIP_DIR", tmp_path / "owners")
    return tmp_path / "owners"


def acquire(**kwargs):
    return ExecutionOwnership(**{"account_id": "DU123", "mode": "paper", "asset_class": "equity",
                                 "client_id": 1, "owner_id": "strategy:paper-test", **kwargs})


def test_one_writer_across_accounts_clients_profiles_and_mode(isolated):
    first = acquire()
    try:
        assert first.snapshot()["healthy"]
        for fields in ({"client_id": 8}, {"account_id": "DU456"}, {"asset_class": "crypto"}, {"mode": "live"}):
            with pytest.raises(ExecutionOwnershipError, match="Another broker runner"):
                acquire(**fields)
        status = ownership.ownership_status()
        assert status["active"]
        assert status["owner"]["owner_id"] == "strategy:paper-test"
        assert "DU123" not in json.dumps(status)
    finally:
        first.close()
    assert ownership.ownership_status()["status"] == "AVAILABLE"
    second = acquire(client_id=8)
    assert second.record["generation"] != first.record["generation"]
    second.close()


def test_closed_or_replaced_identity_never_allows_mutation(isolated):
    first = acquire()
    first.path.unlink()
    first.path.write_text("{}")
    with pytest.raises(ExecutionOwnershipError, match="replaced"):
        first.assert_owned()
    first.close()
    assert not first.snapshot()["healthy"]


def test_metadata_corruption_does_not_unlock_the_owner(isolated):
    first = acquire()
    try:
        os.pwrite(first.fd, b"broken", 0)
        with pytest.raises(ExecutionOwnershipError, match="identity changed"):
            first.assert_owned()
        assert ownership.ownership_status()["active"]
        with pytest.raises(ExecutionOwnershipError):
            acquire()
    finally:
        first.close()


def test_symlink_is_rejected_instead_of_creating_another_lock_domain(isolated, tmp_path):
    isolated.mkdir()
    target = tmp_path / "elsewhere"
    target.write_text("")
    (isolated / "broker-execution.lock").symlink_to(target)
    with pytest.raises(OSError):
        acquire()


def test_paused_owner_is_not_expired_and_process_death_releases_kernel_lock(isolated):
    script = (
        "from pathlib import Path\nimport sys,time\n"
        "from quant.run import execution_ownership as m\n"
        "m.OWNERSHIP_DIR=Path(sys.argv[1])\n"
        "guard=m.ExecutionOwnership(account_id='DU999',mode='paper',asset_class='equity',client_id=1)\n"
        "print('owned',flush=True)\ntime.sleep(60)\n"
    )
    child = subprocess.Popen([sys.executable, "-c", script, str(isolated)], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "owned"
        os.kill(child.pid, signal.SIGSTOP)
        assert ownership.ownership_status()["active"]
        with pytest.raises(ExecutionOwnershipError):
            acquire(client_id=99)
        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=5)
        # Stale metadata is not a live owner and never asserts broker flatness.
        status = ownership.ownership_status()
        assert not status["active"]
        assert status["owner"] is None
        assert status["previous_owner"]["pid"] == child.pid
        replacement = acquire()
        replacement.close()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        child.stdout.close()


class FakeNode:
    def __init__(self):
        self.disconnected = False
        self.running = False
        self.disposal_failed = False
        engine = SimpleNamespace(check_disconnected=lambda: self.disconnected)
        self.kernel = SimpleNamespace(is_running=lambda: self.running, exec_engine=engine, data_engine=engine)

    def run(self):
        self.running = True

    async def run_async(self):
        self.running = True

    def stop(self):
        self.running = False

    def dispose(self):
        if self.disposal_failed:
            raise RuntimeError("failed disconnect")
        self.running = False


@pytest.mark.parametrize("asynchronous", [False, True])
def test_ownership_spans_stop_dispose_and_unverified_shutdown(isolated, asynchronous):
    guard = acquire()
    raw = FakeNode()
    node = OwnedTradingNode(raw, guard)
    try:
        asyncio.run(node.run_async()) if asynchronous else node.run()
        node.stop()
        assert ownership.ownership_status()["active"]
        raw.disposal_failed = True
        with pytest.raises(RuntimeError):
            node.dispose()
        assert guard.snapshot()["healthy"]
        raw.disposal_failed = False
        with pytest.raises(ExecutionOwnershipError, match="Shutdown is unverified"):
            node.dispose()
        assert guard.snapshot()["healthy"]
        raw.disconnected = True
        node.dispose()
        node.dispose()
        assert not ownership.ownership_status()["active"]
        with pytest.raises(ExecutionOwnershipError):
            node.run()
    finally:
        guard.close()


def test_builder_blocks_duplicates_before_adapter_and_without_persistence(isolated, monkeypatch):
    guard = acquire()
    monkeypatch.setattr(run_live, "register_ibkr_execution_fixes", lambda: pytest.fail("adapter was constructed"))
    try:
        with pytest.raises(ExecutionOwnershipError):
            run_live.build_node(["QQQ"], "127.0.0.1", 7497, 44, False, {}, account_id="DU456",
                                asset_class="equity", persistence=False)
    finally:
        guard.close()


def test_failed_build_releases_ownership_without_resetting_persistence(isolated, monkeypatch):
    def fail():
        raise RuntimeError("adapter assembly failed")
    monkeypatch.setattr(run_live, "register_ibkr_execution_fixes", fail)
    with pytest.raises(RuntimeError, match="assembly failed"):
        run_live.build_node(["QQQ"], "127.0.0.1", 7497, 1, False, {}, account_id="DU123", asset_class="equity")
    assert not ownership.ownership_status()["active"]


@pytest.mark.parametrize("method", ["submit_order", "submit_order_list", "modify_order", "cancel_order",
                                    "cancel_all_orders", "close_position", "close_all_positions"])
def test_every_public_broker_mutation_fails_before_base_method_when_unowned(isolated, method):
    strategy = MLStrategy(MLStrategyConfig(instrument_ids=["QQQ.SMART"], execution_mode="paper"))
    strategy._pending["QQQ"] = "old signal"
    with pytest.raises(ExecutionOwnershipError, match="No exclusive"):
        getattr(strategy, method)(object())
    assert not strategy._pending
    assert strategy._operations_entries_frozen
    assert strategy._execution_safety.state == ExecutionSafetyState.UNCERTAIN


def test_strategy_owner_loss_is_detected_and_recorded(isolated):
    guard = acquire()
    strategy = MLStrategy(MLStrategyConfig(instrument_ids=["QQQ.SMART"], execution_mode="paper"))
    strategy.attach_execution_ownership(guard)
    strategy._assert_execution_ownership()
    guard.close()
    with pytest.raises(ExecutionOwnershipError):
        strategy.cancel_all_orders(object())
    assert strategy._execution_safety.state == ExecutionSafetyState.UNCERTAIN


def test_ownership_api_review_and_launch_conflict_precede_job_creation(isolated, monkeypatch):
    app = FastAPI()
    app.include_router(routes.router)
    client = ASGIClient(app)
    assert client.get("/api/execution-ownership").json()["status"] == "AVAILABLE"
    guard = acquire()
    try:
        assert client.get("/api/execution-ownership").json()["status"] == "OWNED"
        monkeypatch.setattr(jobs_routes, "manager", SimpleNamespace(new_job_id=lambda *_: pytest.fail("job created")))
        with pytest.raises(HTTPException) as error:
            jobs_routes.start_paper(PaperJobRequest(asset_class="equity", tickers=["QQQ"], client_id=44))
        assert error.value.status_code == 409
    finally:
        guard.close()


def test_ownership_read_failure_is_unavailable_not_free(isolated, monkeypatch):
    def fail():
        raise OSError("ownership store unavailable")
    monkeypatch.setattr(routes, "ownership_status", fail)
    with pytest.raises(HTTPException) as error:
        routes.status()
    assert error.value.status_code == 503
