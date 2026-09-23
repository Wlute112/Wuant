"""Recovery integrity, admission, retries and an actual encrypted local drill."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
import pytest
from redis import Redis

from quant.ops import recovery
from quant.api import recovery as routes, jobs_routes
from quant.tests.test_operations_api import ASGIClient, TOKEN


def config(tmp_path, **changes):
    password = tmp_path / "password"
    password.write_text("fixture-encryption-secret")
    password.chmod(0o600)
    return recovery.RecoveryConfig(repository="sftp:backup@recovery.example:/quant", password_file=str(password), **changes)


@pytest.mark.parametrize("repository", ["/tmp/local", "sftp:localhost:/backup", "sftp:a@127.0.0.1:/backup",
    "sftp:host:/", "sftp:host:/../etc", "sftp:host:/backup;touch-bad", "sftp:-oProxyCommand=x:/a"])
def test_reject_unsafe_or_local_destinations(tmp_path, repository):
    with pytest.raises(ValueError):
        recovery.RecoveryConfig(repository=repository, password_file=str(tmp_path / "secret"))


@pytest.fixture
def redis_dump(tmp_path):
    if not shutil.which("redis-server"):
        pytest.skip("Redis tools required")
    # Use a short /tmp socket path (macOS UNIX_PATH_MAX), no network listener.
    import tempfile
    with tempfile.TemporaryDirectory(prefix="qr-") as directory:
        socket = str(Path(directory) / "redis.sock")
        proc = subprocess.Popen(["redis-server", "--port", "0", "--unixsocket", socket,
                                 "--save", "", "--appendonly", "no", "--dir", directory],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        client = Redis(unix_socket_path=socket)
        try:
            for _ in range(100):
                try:
                    if client.ping():
                        break
                except Exception:
                    time.sleep(.02)
            client.set("trader-001:allocation", "5000")
            client.hset("unanticipated-nautilus-prefix", mapping={"kill_switch": "true", "peak": "5250"})
            client.save()
            yield Path(directory) / "dump.rdb"
        finally:
            client.close()
            proc.terminate()
            proc.wait(timeout=5)


def fixture_root(tmp_path):
    root = tmp_path / "source"
    (root / "optimize" / "campaigns").mkdir(parents=True)
    (root / "jobs").mkdir()
    (root / "optimize" / "campaigns" / "locked.json").write_text(json.dumps({"outer_holdout": {"status": "CONSUMED_PENDING", "evaluations": 1}}))
    with sqlite3.connect(root / "jobs" / "operations.sqlite3") as db:
        db.execute("CREATE TABLE state (key TEXT, value TEXT)")
        db.execute("INSERT INTO state VALUES ('permanent-kill', 'true')")
        db.execute("INSERT INTO state VALUES ('allocation', '5000')")
        db.execute("CREATE TABLE audit (event TEXT)")
        db.execute("INSERT INTO audit VALUES ('kill engaged')")
    return root


def test_capture_preserves_holdout_audit_and_all_redis_prefixes(tmp_path, redis_dump):
    root = fixture_root(tmp_path)
    target = tmp_path / "bundle"
    manifest = recovery.capture_bundle(target, root=root, redis_export=lambda p: shutil.copyfile(redis_dump, p))
    assert recovery.verify_bundle(target) == manifest
    assert json.loads((target / "files/optimize/campaigns/locked.json").read_text())["outer_holdout"]["status"] == "CONSUMED_PENDING"
    assert (target / "redis.rdb").read_bytes() == redis_dump.read_bytes()
    with sqlite3.connect(target / "files/jobs/operations.sqlite3") as db:
        assert db.execute("SELECT value FROM state WHERE key='permanent-kill'").fetchone()[0] == "true"
    (target / "redis.rdb").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="integrity"):
        recovery.verify_bundle(target)


def test_manifest_path_escape_and_symlink_rejected(tmp_path, redis_dump):
    target = tmp_path / "bundle"
    recovery.capture_bundle(target, root=fixture_root(tmp_path), redis_export=lambda p: shutil.copyfile(redis_dump, p))
    manifest = json.loads((target / "manifest.json").read_text())
    manifest["files"][0]["path"] = "../outside"
    recovery.atomic_json(target / "manifest.json", manifest)
    with pytest.raises(ValueError, match="Unsafe"):
        recovery.verify_bundle(target)
    root = tmp_path / "symlinks"
    (root / "jobs").mkdir(parents=True)
    (root / "jobs" / "external").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="Symlink"):
        recovery.capture_bundle(tmp_path / "bad", root=root)


def test_interruption_never_claims_success_or_prunes(tmp_path, monkeypatch):
    class Interrupted:
        def __init__(self, config):
            pass
        def call(self, *args, **kwargs):
            raise KeyboardInterrupt()
    identifier = uuid4().hex
    with pytest.raises(KeyboardInterrupt):
        recovery.perform("check", config(tmp_path), identifier, state=tmp_path / "state", restic_factory=Interrupted)
    report = json.loads((tmp_path / "state" / f"{identifier}.json").read_text())
    assert report["status"] == "interrupted"
    assert report["activation_allowed"] is False


@pytest.fixture
def api_setup(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANT_CONTROL_TOKEN", TOKEN)
    monkeypatch.setattr(routes, "STATE", tmp_path / "state")
    monkeypatch.setattr(routes.shutil, "which", lambda name: f"/test/{name}")
    jobs = {}
    def submit(kind, module, args, config, job_id):
        jobs[job_id] = {"id": job_id, "kind": kind, "status": "running", "config": config}
        return jobs[job_id]
    def cancel(identifier):
        jobs[identifier]["status"] = "cancelling"
        return jobs[identifier]
    manager = SimpleNamespace(list=lambda: list(jobs.values()), get=jobs.get, submit=submit, cancel=cancel)
    monkeypatch.setattr(jobs_routes, "manager", manager)
    app = FastAPI()
    app.include_router(routes.router)
    app.include_router(jobs_routes.router)
    client = ASGIClient(app)
    client.headers["Authorization"] = f"Bearer {TOKEN}"
    conf = config(tmp_path).model_dump()
    assert client.request("PUT", "/api/recovery/config", json=conf).status_code == 200
    return client, jobs, conf


def test_api_auth_retries_conflict_cancel_and_public_bypass(api_setup):
    client, jobs, conf = api_setup
    body = {"action": "backup", "request_id": str(uuid4())}
    assert client.get("/api/recovery", headers={"Authorization": "Bearer wrong"}).status_code == 401
    first = client.post("/api/recovery/jobs", json=body)
    assert first.status_code == 202
    assert client.post("/api/recovery/jobs", json=body).json() == first.json()
    assert len(jobs) == 1
    assert client.post("/api/recovery/jobs", json={**body, "action": "check"}).status_code == 409
    assert client.request("PUT", "/api/recovery/config", json=conf).status_code == 409
    assert client.post(f"/api/jobs/{first.json()['id']}/cancel").status_code == 403
    assert client.post(f"/api/recovery/jobs/{body['request_id']}/cancel").status_code == 200
    assert client.get("/api/recovery").json()["reports"][0]["status"] == "cancelling"


def test_restore_confirmation_and_missing_inventory_are_fail_closed(api_setup):
    client, _, _ = api_setup
    assert client.post("/api/recovery/jobs", json={"action": "restore", "request_id": str(uuid4()), "snapshot": "latest"}).status_code == 422
    assert client.post("/api/recovery/jobs", json={"action": "restore", "request_id": str(uuid4()), "snapshot": "a" * 64}).status_code == 422
    state = client.get("/api/recovery").json()
    assert state["inventory"] is None and state["rpo_status"] == "unknown"


def test_scheduler_is_opt_in_and_failed_attempts_are_bounded(api_setup):
    client, jobs, conf = api_setup
    routes.schedule_once()
    assert not jobs
    assert client.request("PUT", "/api/recovery/config", json={**conf, "scheduled": True}).status_code == 200
    routes.schedule_once()
    assert len(jobs) == 1
    next(iter(jobs.values()))["status"] = "failed"
    routes.schedule_once()
    assert len(jobs) == 1


def test_failed_repository_integrity_never_applies_retention(tmp_path, monkeypatch):
    calls = []
    class BrokenCheck:
        def __init__(self, config):
            pass
        def call(self, *args, **kwargs):
            calls.append(args)
            if args[0] == "backup":
                return json.dumps({"snapshot_id": "a" * 64})
            raise RuntimeError("fixture repository corruption")
    monkeypatch.setattr(recovery, "capture_bundle", lambda *a, **kw: {"capture_started_at": recovery.now()})
    with pytest.raises(RuntimeError, match="corruption"):
        recovery.perform("backup", config(tmp_path), uuid4().hex, state=tmp_path / "state", restic_factory=BrokenCheck)
    assert not any(c[0] == "forget" for c in calls)


def test_actual_encrypted_local_backup_check_restore(tmp_path, redis_dump, monkeypatch):
    binary = shutil.which("restic")
    if not binary:
        pytest.skip("Install restic to run encrypted integration drill")
    repository = tmp_path / "encrypted-repo"
    conf = config(tmp_path)
    original_capture = recovery.capture_bundle
    def capture(destination, **kwargs):
        return original_capture(destination, root=kwargs["root"], redis_export=lambda p: shutil.copyfile(redis_dump, p))
    monkeypatch.setattr(recovery, "capture_bundle", capture)
    class LocalRestic(recovery.Restic):
        # Local transport is test-only. Production config refuses local targets.
        def __init__(self, config):
            super().__init__(config)
            self.env["RESTIC_REPOSITORY"] = str(repository)
    state = tmp_path / "state"
    root = fixture_root(tmp_path)
    recovery.perform("initialize", conf, uuid4().hex, state=state, restic_factory=LocalRestic)
    backup = recovery.perform("backup", conf, uuid4().hex, root=root, state=state, restic_factory=LocalRestic)
    assert backup["status"] == "completed"
    result = recovery.perform("restore", conf, uuid4().hex, snapshot=backup["snapshot"], state=state, restic_factory=LocalRestic)
    assert result["status"] == "completed" and result["activation_allowed"] is False
    assert result["broker_reconciliation"] == "REQUIRED"
    assert recovery.verify_bundle(Path(result["restore_path"]))["files"]
    assert (root / "optimize/campaigns/locked.json").read_bytes() == (Path(result["restore_path"]) / "files/optimize/campaigns/locked.json").read_bytes()
    # Load only the restored fixture RDB into an isolated, network-disabled
    # Redis instance, and confirm actual allocation/kill payloads survived.
    import tempfile
    with tempfile.TemporaryDirectory(prefix="qr-restored-") as directory:
        shutil.copyfile(Path(result["restore_path"]) / "redis.rdb", Path(directory) / "dump.rdb")
        sock = str(Path(directory) / "s")
        proc = subprocess.Popen(["redis-server", "--port", "0", "--unixsocket", sock,
                                 "--save", "", "--appendonly", "no", "--dir", directory],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        client = Redis(unix_socket_path=sock)
        try:
            for _ in range(100):
                try:
                    if client.ping():
                        break
                except Exception:
                    time.sleep(.02)
            assert client.get("trader-001:allocation") == b"5000"
            assert client.hget("unanticipated-nautilus-prefix", "kill_switch") == b"true"
        finally:
            client.close()
            proc.terminate()
            proc.wait(timeout=5)
