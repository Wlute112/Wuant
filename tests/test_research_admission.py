"""Host contention and lifecycle tests; no broker connection or orders."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time

import pytest
from fastapi import FastAPI

from quant.api import job_worker, jobs, resources
from quant.run import admission
from quant.run.admission import AdmissionStore
from quant.run.compute import ComputePool, resolve_compute_plan
from quant.tests.test_operations_api import ASGIClient, TOKEN

HOST = {"compute_cores": 8, "logical_cpus": 8, "memory_gb": 32, "chip": "test"}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANT_ADMISSION_DB", str(tmp_path / "admission.sqlite3"))
    monkeypatch.delenv("QUANT_RESEARCH_LEASE", raising=False)
    monkeypatch.setattr(admission, "host_resources", lambda: HOST)
    return AdmissionStore()


def test_atomic_contention_fifo_memory_and_release(store):
    store.configure(4, 16, "test")
    first = store.enqueue(3, 12, "first")
    second = store.enqueue(2, 8, "second")
    third = store.enqueue(1, 4, "third")
    with ThreadPoolExecutor(3) as pool:
        results = list(pool.map(store.claim, [first["token"], second["token"], third["token"]]))
    assert [row["state"] for row in results] == ["running", "queued", "queued"]
    assert store.claim(third["token"])["queue_position"] == 2
    assert store.status()["used"] == {"cpu_slots": 3, "memory_gb": 12}
    store.release(first["token"])
    assert store.claim(second["token"])["state"] == "running"
    assert store.claim(third["token"])["state"] == "running"
    assert store.status()["used"] == {"cpu_slots": 3, "memory_gb": 12}


def test_memory_admission_independent_of_cpu_and_no_lease_expiry(store):
    store.configure(6, 16, "test")
    one = store.enqueue(1, 12, "one")
    two = store.enqueue(1, 8, "two")
    store.claim(one["token"])
    with store.connect() as db:
        row = store._rows(db)[0]
        row["queued_at"] = 0
        store._write(db, row)
    assert store.claim(two["token"])["state"] == "queued"


def test_policy_is_durable_audited_and_cannot_strand_work(store):
    store.configure(3, 12, "alice")
    other = AdmissionStore()
    assert other.status()["limits"] == {"cpu_slots": 3, "memory_gb": 12}
    queued = other.enqueue(3, 12, "queued")
    with pytest.raises(ValueError, match="queued"):
        store.configure(2, 8, "alice")
    other.claim(queued["token"])
    with pytest.raises(ValueError, match="active"):
        store.configure(2, 8, "alice")
    with store.connect() as db:
        assert db.execute("SELECT operator FROM policy_audit").fetchone()[0] == "alice"


@pytest.mark.parametrize("cpu,memory", [(0, 4), (7, 4), (2, 25), (2, float("nan"))])
def test_service_reserves_cannot_be_removed(store, cpu, memory):
    with pytest.raises(ValueError):
        store.configure(cpu, memory, "test")


def test_dead_owner_reclaimed_but_surviving_child_keeps_reservation(store):
    row = store.enqueue(2, 8, "crashed")
    store.claim(row["token"])
    with store.connect() as db:
        row = store._rows(db)[0]
        row["birth"] = "old process with reused PID"
        row["processes"] = [{"pid": os.getpid(), "birth": admission.process_snapshot()[os.getpid()]["birth"]}]
        store._write(db, row)
    assert len(store.status()["requests"]) == 1
    with store.connect() as db:
        row["processes"] = []
        store._write(db, row)
    assert store.status()["requests"] == []


def test_measurement_counts_tree_and_retains_peak(store, monkeypatch):
    row = store.enqueue(2, 8, "measured")
    store.claim(row["token"])
    monkeypatch.setattr(admission, "process_snapshot", lambda: {
        100: {"pid": 100, "parent": 1, "group": 100, "rss_gb": 2, "birth": "a"},
        101: {"pid": 101, "parent": 100, "group": 100, "rss_gb": 3, "birth": "b"},
        200: {"pid": 200, "parent": 1, "group": 200, "rss_gb": 10, "birth": "c"},
    })
    assert store.sample(row["token"], 100, 100)["peak_rss_gb"] == 5
    assert store.sample(row["token"], 101)["peak_rss_gb"] == 5
    assert store.sample(row["token"], 101)["rss_gb"] == 3


def test_inherited_reservation_identity_and_nested_pool_limits(store, monkeypatch):
    row = store.enqueue(2, 8, "parent")
    store.claim(row["token"])
    monkeypatch.setenv("QUANT_RESEARCH_LEASE", row["token"])
    with admission.research_slot(2, 4) as inherited:
        assert inherited["token"] == row["token"]
    assert len(store.status()["requests"]) == 1  # nested context cannot release parent
    with pytest.raises(ValueError, match="inherited"):
        with admission.research_slot(3, 4):
            pass
    monkeypatch.setenv("QUANT_RESEARCH_LEASE", "missing")
    with pytest.raises(RuntimeError, match="missing"):
        store.inherited()


def test_monitor_failure_blocks_capacity_reclamation(store, monkeypatch):
    row = store.enqueue(1, 4, "one")
    monkeypatch.setattr(admission, "process_snapshot", lambda: (_ for _ in ()).throw(OSError("ps unavailable")))
    with pytest.raises(OSError):
        store.claim(row["token"])


def test_parallel_command_pinned_and_execution_not_admitted(store):
    store.configure(3, 12, "test")
    command = [sys.executable, "-m", "quant.optimize.multi_seed", "--workers", "20"]
    workers, memory, pinned = job_worker.research_request("campaign_seeds", command, store)
    assert (workers, memory) == (3, 12)
    assert pinned[pinned.index("--workers") + 1] == "3"
    assert pinned[pinned.index("--memory-budget-gb") + 1] == "12.0"
    assert not {"paper", "live", "risk_supervisor"} & admission.RESEARCH_KINDS


def test_cli_reservation_spans_preparation_pool_and_holdout(store, monkeypatch):
    store.configure(1, 4, "test")
    monkeypatch.setattr(sys, "argv", ["optimize", "--workers", "1"])
    with admission.research_entry("CLI optimization"):
        competitor = store.enqueue(1, 4, "competitor")
        # Preparation is protected before a fold pool has been entered.
        assert store.claim(competitor["token"])["state"] == "queued"
        with ComputePool(resolve_compute_plan(1, tasks=2, host=HOST)):
            assert len(store.status()["requests"]) == 2
        # Final holdout/evidence remains protected after fold workers stop.
        assert store.claim(competitor["token"])["state"] == "queued"
    assert store.claim(competitor["token"])["state"] == "running"


def test_failed_pool_setup_releases_capacity_and_restores_threads(store, monkeypatch):
    import threadpoolctl
    def fail(*args, **kwargs):
        raise RuntimeError("native thread setup failed")
    monkeypatch.setattr(threadpoolctl, "threadpool_limits", fail)
    with pytest.raises(RuntimeError, match="native thread"):
        with ComputePool(resolve_compute_plan(1, tasks=2, host=HOST)):
            pass
    assert store.status()["requests"] == []
    assert "QUANT_RESEARCH_LEASE" not in os.environ


def test_resource_routes_auth_validation_and_readonly_visibility(store, monkeypatch):
    monkeypatch.setenv("QUANT_CONTROL_TOKEN", TOKEN)
    app = FastAPI()
    app.include_router(resources.router)
    client = ASGIClient(app)
    assert client.get("/api/research-resources").status_code == 200
    payload = {"cpu_slots": 3, "memory_gb": 12}
    assert client.request("PUT", "/api/research-resources", json=payload).status_code == 401
    auth = {"Authorization": f"Bearer {TOKEN}"}
    assert client.request("PUT", "/api/research-resources", json=payload, headers=auth).status_code == 200
    assert client.request("PUT", "/api/research-resources", json={**payload, "cpu_slots": 7}, headers=auth).status_code == 409
    remote = ASGIClient(app, client=("192.0.2.1", 4000))
    assert remote.request("PUT", "/api/research-resources", json=payload, headers=auth).status_code == 403


@pytest.fixture
def manager(tmp_path, monkeypatch):
    """Real Redis + detached worker, fully isolated from the user's registry."""
    executable = shutil.which("redis-server")
    if not executable:
        pytest.skip("redis-server required for detached supervisor integration")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = subprocess.Popen([executable, "--port", str(port), "--bind", "127.0.0.1",
                               "--save", "", "--appendonly", "no", "--dir", str(tmp_path)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    monkeypatch.setenv("QUANT_JOBS_REDIS_PORT", str(port))
    monkeypatch.setenv("QUANT_JOBS_REDIS_PREFIX", "test:admission")
    monkeypatch.setenv("QUANT_ADMISSION_DB", str(tmp_path / "host.sqlite3"))
    monkeypatch.delenv("QUANT_RESEARCH_LEASE", raising=False)
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path)
    registry = jobs.RedisJobStore()
    deadline = time.monotonic() + 5
    while True:
        try:
            registry.ping()
            break
        except jobs.JobStoreUnavailableError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)
    manager = jobs.JobManager(registry)
    yield manager
    for job in manager.list():
        if job["status"] in jobs._ACTIVE_STATUSES:
            manager.cancel(job["id"])
    server.terminate()
    server.wait(timeout=5)


def eventually(fn, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = fn()
        if result:
            return result
        time.sleep(0.1)
    raise AssertionError("Condition did not become true")


def test_real_detached_queue_restart_cancel_and_execution_bypass(manager):
    store = AdmissionStore()
    store.configure(1, 4, "test")
    held = store.enqueue(1, 4, "CLI contention fixture")
    store.claim(held["token"])
    job = manager.submit("campaign_compare", "timeit", ["-n", "1", "-r", "1", "pass"])
    eventually(lambda: manager.get(job["id"]).get("resources", {}).get("queue_position") == 1)
    restored = jobs.JobManager(manager.store)
    assert restored.get(job["id"])["status"] == "queued"
    # Execution/risk work starts while every research slot is occupied.
    supervisor = restored.submit("risk_supervisor", "timeit", ["-n", "1", "-r", "1", "pass"])
    eventually(lambda: restored.get(supervisor["id"])["status"] == "completed")
    assert restored.cancel(job["id"])["status"] == "cancelled"
    assert [row["label"] for row in store.status()["requests"]] == ["CLI contention fixture"]
    next_job = restored.submit("campaign_compare", "timeit", ["-n", "1", "-r", "1", "pass"])
    eventually(lambda: restored.get(next_job["id"]).get("resources", {}).get("state") == "queued")
    store.release(held["token"])
    eventually(lambda: restored.get(next_job["id"])["status"] == "completed")
    assert restored.get(next_job["id"])["resources"]["peak_rss_gb"] is not None
    eventually(lambda: store.status()["requests"] == [])


def test_poll_during_supervisor_pid_handoff_preserves_queue_and_abandoned_launch_fails(manager):
    job = {"id": "queued-handoff", "kind": "campaign_compare", "status": "queued", "pid": None,
           "started_at": datetime.now(timezone.utc).isoformat()}
    manager.store.create(job)
    assert manager.get(job["id"])["status"] == "queued"
    manager.store.update(job["id"], started_at=(datetime.now(timezone.utc) - timedelta(seconds=11)).isoformat())
    assert manager.get(job["id"])["status"] == "failed"


def test_running_research_memory_and_cancel_release_budget(manager):
    store = AdmissionStore()
    store.configure(1, 4, "test")
    job = manager.submit("campaign_compare", "timeit", ["-n", "1", "-r", "1", "__import__('time').sleep(30)"])
    eventually(lambda: manager.get(job["id"])["status"] == "running")
    eventually(lambda: (manager.get(job["id"]).get("resources", {}).get("peak_rss_gb") or 0) > 0)
    assert manager.cancel(job["id"])["status"] == "cancelled"
    eventually(lambda: store.status()["requests"] == [])


def test_competing_cpu_research_preserves_independent_supervisor_heartbeat(manager, tmp_path):
    store = AdmissionStore()
    slots = min(3, store.ceiling()["cpu_slots"])
    store.configure(slots, 4 * slots, "test")
    work = "import time\nend=time.monotonic()+8\nwhile time.monotonic()<end: sum(range(50000))"
    studies = [manager.submit("campaign_compare", "timeit", ["-n", "1", "-r", "1", work]) for _ in range(slots)]
    eventually(lambda: all(manager.get(job["id"])["status"] == "running" for job in studies))
    queued = manager.submit("campaign_compare", "timeit", ["-n", "1", "-r", "1", "pass"])
    eventually(lambda: manager.get(queued["id"]).get("resources", {}).get("state") == "queued")
    heartbeats = tmp_path / "heartbeat.json"
    sentinel = ("import json,time,pathlib\n"
                "ticks=[]\nfor _ in range(20): ticks.append(time.monotonic()); time.sleep(0.05)\n"
                f"pathlib.Path({str(heartbeats)!r}).write_text(json.dumps(ticks))")
    supervisor = manager.submit("risk_supervisor", "timeit", ["-n", "1", "-r", "1", sentinel])
    eventually(lambda: manager.get(supervisor["id"])["status"] == "completed")
    ticks = json.loads(heartbeats.read_text())
    assert len(ticks) == 20
    assert max(right - left for left, right in zip(ticks, ticks[1:])) < 1.0
    assert manager.get(queued["id"])["status"] == "queued"
    manager.cancel(queued["id"])
    for job in studies:
        manager.cancel(job["id"])
    eventually(lambda: store.status()["requests"] == [])


def test_crashed_supervisor_cannot_leave_an_unreserved_research_child(manager):
    store = AdmissionStore()
    store.configure(1, 4, "test")
    job = manager.submit("campaign_compare", "timeit", ["-n", "1", "-r", "1", "__import__('time').sleep(30)"])
    eventually(lambda: manager.get(job["id"])["status"] == "running")
    record = manager.get(job["id"])
    os.kill(record["pid"], signal.SIGKILL)
    eventually(lambda: manager.get(job["id"])["status"] == "failed")
    eventually(lambda: record["child_pid"] not in admission.process_snapshot())
    eventually(lambda: store.status()["requests"] == [])


@pytest.mark.parametrize("failure", ["memory", "monitor"])
def test_resource_watchdog_kills_only_owned_research_and_records_failure(manager, tmp_path, monkeypatch, failure):
    store = AdmissionStore()
    store.configure(1, 4, "test")
    original_sample = store.sample
    calls = 0
    def sample(*args, **kwargs):
        nonlocal calls
        calls += 1
        measured = original_sample(*args, **kwargs)
        if calls > 1:
            if failure == "monitor":
                raise OSError("measurement source lost")
            measured["rss_gb"] = measured["memory_gb"] + 1
        return measured
    monkeypatch.setattr(store, "sample", sample)
    monkeypatch.setattr(job_worker, "AdmissionStore", lambda: store)
    monkeypatch.setattr(job_worker, "RedisJobStore", lambda **kwargs: manager.store)
    monkeypatch.setattr(job_worker.os, "nice", lambda *_: 0)
    job_id = f"watchdog-{failure}"
    manager.store.create({"id": job_id, "kind": "campaign_compare", "status": "queued"})
    spec = tmp_path / "watchdog.json"
    spec.write_text(json.dumps({"job_id": job_id, "kind": "campaign_compare",
        "command": [sys.executable, "-m", "timeit", "-n", "1", "-r", "1", "__import__('time').sleep(30)"],
        "cwd": str(tmp_path), "log_path": str(tmp_path / "watchdog.log"), "redis_prefix": "unused"}))
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        assert job_worker.supervise(spec) != 0
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
    result = manager.get(job_id)
    assert result["status"] == "failed"
    assert ("exceeded" if failure == "memory" else "monitoring unavailable") in result["failure_reason"]
    assert store.status()["requests"] == []
    assert not spec.exists()
