"""Encrypted off-host recovery bundles. Restores are isolated, never activated.

Restic owns encryption, transport, repository locking and retention. No shell is
used. Credentials are server-side files; neither credentials nor command output
are written to public job logs. A bundle spans multiple stores, so it records a
capture interval, not a fictitious cross-store transaction.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time

from pydantic import BaseModel, ConfigDict, Field, field_validator

from quant.ops.backups import _sha256, _sqlite_backup

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "jobs" / "recovery"
TAG = "quant-recovery-v1"
SNAPSHOT = re.compile(r"[a-f0-9]{64}$")
ACTIVE = {"starting", "running", "cancelling"}


def now():
    return datetime.now(timezone.utc).isoformat()


class RecoveryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repository: str = Field(max_length=500)
    password_file: str = Field(max_length=1000)
    retain: int = Field(default=56, ge=2, le=1000)
    rpo_hours: int = Field(default=6, ge=1, le=168)
    rto_minutes: int = Field(default=120, ge=1, le=10080)
    scheduled: bool = False
    redis_host: str = Field(default="127.0.0.1", pattern=r"^[A-Za-z0-9_.:-]+$", max_length=255)
    redis_port: int = Field(default=6379, ge=1, le=65535)

    @field_validator("repository")
    @classmethod
    def remote_repository(cls, value):
        # Restrict transport to SFTP, absolute path, no shell metacharacters or
        # inline credentials. SSH config/agent supplies authentication.
        match = re.fullmatch(r"sftp:([A-Za-z0-9_][A-Za-z0-9_.-]*@)?([A-Za-z0-9][A-Za-z0-9.-]*):(/[A-Za-z0-9_./-]+)", value)
        if not match or match[2].lower() in {"localhost", "127.0.0.1", "0.0.0.0", socket.gethostname().lower()}:
            raise ValueError("Use sftp:user@remote-host:/absolute/dedicated/repository")
        if match[3] == "/" or ".." in match[3].split("/"):
            raise ValueError("Use a dedicated remote repository directory")
        return value

    @field_validator("password_file")
    @classmethod
    def external_password(cls, value):
        path = Path(value).expanduser()
        if not path.is_absolute() or path.resolve().is_relative_to(ROOT):
            raise ValueError("Password file must be an absolute path outside the project and backup bundle")
        return str(path)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".recovery-")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def exclusive(state=STATE):
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state / "worker.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another recovery operation is running") from exc
        yield


def load_config(state=STATE):
    return RecoveryConfig.model_validate_json((state / "config.json").read_text())


def run_command(args, *, cwd=None, env=None, timeout=3600):
    # Inherit the job's process group so dashboard cancellation also terminates
    # restic/SSH/Redis children. Do not print output: remote errors can contain
    # usernames, paths, or SSH configuration details.
    result = subprocess.run(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{Path(args[0]).name} failed (exit {result.returncode}); check credentials, host key, connectivity and repository integrity")
    return result.stdout.decode()


class Restic:
    def __init__(self, config):
        self.config = config
        self.binary = shutil.which("restic")
        if not self.binary:
            raise ValueError("Install restic on the API host before launching recovery jobs")
        path = Path(config.password_file)
        if not path.is_file() or path.is_symlink() or path.stat().st_mode & 0o077 or path.stat().st_uid != os.getuid():
            raise ValueError("Repository password file must be owned by the API user, regular, and mode 0600")
        if not path.read_bytes().strip():
            raise ValueError("Repository password file is empty")
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("RESTIC_")}
        self.env.update(RESTIC_REPOSITORY=config.repository, RESTIC_PASSWORD_FILE=str(path))

    def call(self, *args, cwd=None):
        return run_command([self.binary, "--json", "-o", "sftp.args=-oBatchMode=yes -oStrictHostKeyChecking=yes -oConnectTimeout=15", *args],
                           env=self.env, cwd=cwd)


def capture_bundle(destination, *, root=ROOT, redis_export=None, redis_host="127.0.0.1", redis_port=6379):
    """SQLite online snapshots, immutable file copies and a full atomic Redis RDB.

    All durable research/operations directories are included, including consumed
    holdout manifests and SQLite user attrs. Symlinks and changing files fail the
    capture rather than silently following paths or publishing partial evidence.
    """
    started = now()
    destination.mkdir(mode=0o700)
    files = []
    # Retain dependency locks for clean-host reconstruction without copying a
    # platform-specific virtualenv or node_modules tree.
    for name in ("requirements.txt", "requirements.lock", "web/package.json", "web/package-lock.json"):
        source = root / name
        if source.exists():
            if source.is_symlink() or not source.resolve().is_relative_to(root.resolve()):
                raise ValueError(f"Symlink is outside the supported recovery contract: {name}")
            target = destination / "files" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            files.append({"path": str(target.relative_to(destination)), "sha256": _sha256(target), "kind": "file"})
    for directory in ("data", "optimize", "models", "jobs", "runs", "evidence"):
        source_root = root / directory
        if source_root.is_symlink():
            raise ValueError(f"Symlink is outside the supported recovery contract: {directory}")
        if not source_root.exists():
            continue
        for source in sorted(source_root.rglob("*")):
            relative = source.relative_to(root)
            if "__pycache__" in relative.parts or relative.parts[:2] == ("jobs", "recovery"):
                continue
            if source.is_symlink():
                raise ValueError(f"Symlink is outside the supported recovery contract: {relative}")
            if not source.is_file() or source.name.endswith(("-wal", "-shm", ".lock", ".pyc")):
                continue
            if source.suffix == ".py" or source.name.startswith("."):
                continue
            target = destination / "files" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.suffix in {".sqlite3", ".sqlite", ".db"}:
                _sqlite_backup(source, target)
                kind = "sqlite"
            else:
                before = source.stat()
                shutil.copyfile(source, target)
                after = source.stat()
                if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
                    raise ValueError(f"File changed during capture; retry after writers stop: {relative}")
                kind = "file"
            files.append({"path": str(target.relative_to(destination)), "sha256": _sha256(target), "kind": kind})
    redis_path = destination / "redis.rdb"
    if redis_export is None:
        # redis-cli uses REDISCLI_AUTH for credentials; its output is suppressed.
        # Full RDB includes all DBs and Nautilus key prefixes, not just quant:*.
        env = dict(os.environ)
        password = os.environ.get("QUANT_RECOVERY_REDIS_PASSWORD") or os.environ.get("NAUTILUS_REDIS_PASSWORD")
        username = os.environ.get("QUANT_RECOVERY_REDIS_USERNAME") or os.environ.get("NAUTILUS_REDIS_USERNAME")
        if password:
            env["REDISCLI_AUTH"] = password
        run_command(["redis-cli", "-h", redis_host, "-p", str(redis_port),
                     *(["--user", username] if username else []), "--rdb", str(redis_path)], env=env)
    else:
        redis_export(redis_path)
    files.append({"path": "redis.rdb", "sha256": _sha256(redis_path), "kind": "redis"})
    manifest = {"schema_version": 1, "capture_started_at": started, "capture_finished_at": now(),
                "source_host": socket.gethostname(), "files": files,
                "python_version": sys.version, "platform": sys.platform,
                "coverage": "Project data, optimize, models, jobs, runs, evidence; full configured Redis instance; dependency locks. External file paths and code checkout require separate recovery.",
                "activation": "BLOCKED: isolate Redis; reconcile broker and review state before resuming",
                "consistency": "Individual SQLite and Redis snapshots; no cross-store atomicity. Reconciliation required."}
    atomic_json(destination / "manifest.json", manifest)
    verify_bundle(destination)
    return manifest


def verify_bundle(root):
    manifest = json.loads((root / "manifest.json").read_text())
    if manifest.get("schema_version") != 1 or not manifest.get("files"):
        raise ValueError("Invalid recovery manifest")
    seen = set()
    for item in manifest["files"]:
        name = item["path"]
        path = root / name
        if (name in seen or Path(name).is_absolute() or ".." in Path(name).parts
                or path.is_symlink() or not path.resolve().is_relative_to(root.resolve())):
            raise ValueError("Unsafe or duplicate recovery path")
        seen.add(name)
        if item["kind"] not in {"file", "sqlite", "redis"} or not path.is_file() or _sha256(path) != item["sha256"]:
            raise ValueError(f"Recovery integrity failed: {name}")
        if item["kind"] == "sqlite":
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
                if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError(f"SQLite integrity failed: {name}")
    if "redis.rdb" not in seen:
        raise ValueError("Redis state missing from recovery bundle")
    run_command(["redis-check-rdb", str(root / "redis.rdb")], timeout=120)
    actual = {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}
    if actual != seen | {"manifest.json"}:
        raise ValueError("Unmanifested files in recovery bundle")
    return manifest


def perform(action, config, request_id, *, snapshot=None, state=STATE, root=ROOT, restic_factory=Restic):
    report_path = state / f"{request_id}.json"
    report = {"id": request_id, "action": action, "started_at": now(), "status": "running",
              "repository": config.repository, "snapshot": snapshot, "activation_allowed": False}
    started = time.monotonic()

    def phase(value):
        report["phase"] = value
        atomic_json(report_path, report)
        print(value, flush=True)

    with exclusive(state):
        try:
            restic = restic_factory(config)
            if action == "initialize":
                phase("Initializing encrypted repository")
                restic.call("init")
            elif action in {"backup", "check", "restore"}:
                if action == "backup":
                    phase("Capturing durable research and execution state")
                    with tempfile.TemporaryDirectory(prefix="capture-", dir=state) as temporary:
                        staging = Path(temporary)
                        manifest = capture_bundle(staging / "bundle", root=root,
                                                  redis_host=config.redis_host, redis_port=config.redis_port)
                        phase("Uploading encrypted snapshot")
                        output = restic.call("backup", "--tag", TAG, "bundle", cwd=staging)
                        summaries = [json.loads(line) for line in output.splitlines() if line.strip()]
                        snapshot = next((item.get("snapshot_id") for item in reversed(summaries) if item.get("snapshot_id")), None)
                        if not snapshot or not SNAPSHOT.fullmatch(snapshot):
                            raise ValueError("Backup did not return a full snapshot ID")
                        report.update(snapshot=snapshot, capture_started_at=manifest["capture_started_at"])
                    phase("Checking all encrypted repository data")
                    restic.call("check", "--read-data")
                    # Only prune this application's tag, grouping across staging
                    # paths, after the new upload and full integrity check pass.
                    phase("Applying snapshot retention")
                    restic.call("forget", "--tag", TAG, "--group-by", "tags", "--keep-last", str(config.retain), "--prune")
                    restic.call("check", "--read-data")
                elif action == "check":
                    phase("Checking all encrypted repository data")
                    restic.call("check", "--read-data")
                else:
                    if not snapshot or not SNAPSHOT.fullmatch(snapshot):
                        raise ValueError("Select an exact full snapshot ID")
                    snapshots = json.loads(restic.call("snapshots", "--tag", TAG))
                    if not any(item.get("id") == snapshot for item in snapshots):
                        raise ValueError("Snapshot does not belong to the configured recovery repository")
                    phase("Restoring into an isolated directory")
                    target = state / "drills" / request_id
                    target.mkdir(parents=True, exist_ok=False, mode=0o700)
                    restic.call("restore", snapshot, "--target", str(target))
                    bundles = list(target.rglob("manifest.json"))
                    # Restic records its original absolute staging path. Locate
                    # the bundle without trusting manifest-provided destinations.
                    candidates = [p.parent for p in bundles if p.parent.name == "bundle" and (p.parent / "redis.rdb").is_file()]
                    if len(candidates) != 1:
                        raise ValueError("Expected exactly one restored recovery bundle")
                    phase("Verifying restored SQLite, Redis and file integrity")
                    manifest = verify_bundle(candidates[0])
                    report.update(restore_path=str(candidates[0]), manifest=manifest,
                                  different_hostname=manifest["source_host"] != socket.gethostname(),
                                  clean_host_validation="PENDING: hostname alone does not establish isolation",
                                  broker_reconciliation="REQUIRED", state_preservation="byte checksums and database integrity verified")
                phase("Refreshing remote snapshot inventory")
                inventory = json.loads(restic.call("snapshots", "--tag", TAG))
                atomic_json(state / "inventory.json", {"repository": config.repository, "checked_at": now(), "snapshots": inventory})
            else:
                raise ValueError("Unknown recovery action")
            report.update(status="completed", finished_at=now(), duration_seconds=round(time.monotonic() - started, 2))
            if action == "restore":
                report["within_rto"] = report["duration_seconds"] <= config.rto_minutes * 60
                report["rto_scope"] = "Download and integrity only; broker reconciliation and restart are still required"
            atomic_json(report_path, report)
        except BaseException as exc:
            report.update(status="interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "failed",
                          error=str(exc), finished_at=now())
            atomic_json(report_path, report)
            raise
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("initialize", "backup", "check", "restore"))
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--snapshot")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-f0-9]{32}", args.request_id):
        parser.error("request-id must be a UUID hex string")
    request = json.loads((STATE / "requests" / f"{args.request_id}.json").read_text())
    if (request["action"], request["snapshot"]) != (args.action, args.snapshot):
        parser.error("Command does not match the durable recovery request")
    perform(args.action, RecoveryConfig.model_validate(request["config"]), args.request_id, snapshot=args.snapshot)


if __name__ == "__main__":
    main()
