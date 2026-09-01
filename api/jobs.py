"""Redis-backed, restart-safe dashboard job orchestration.

The API persists job metadata in Redis and launches a detached supervisor for
each CLI command.  The supervisor owns the actual trading/research process,
redacts its log stream, and writes terminal state back to Redis.  Consequently
an API restart neither loses the registry nor terminates an active job.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from redis import Redis
from redis.exceptions import RedisError, WatchError

# nautilus's console logger writes ANSI colour codes straight into stdout;
# strip them so the dashboard's log console renders plain, legible text.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_SENSITIVE_ARG_FLAGS = frozenset(
    {"--account-id", "--api-key", "--password", "--redis-password", "--token"}
)
_SENSITIVE_CONFIG_KEYS = frozenset(
    {"account_id", "api_key", "confirm", "password", "redis_password", "secret", "token"}
)

PYTHON_BIN = sys.executable
WORKDIR = Path(__file__).resolve().parents[2]
JOBS_DIR = WORKDIR / "quant" / "jobs"

_TERMINATE_GRACE_SECONDS = 5.0
_ARTIFACT_KINDS = ("backtest", "optimize")
_ACTIVE_STATUSES = frozenset({"starting", "running", "cancelling"})
_DEFAULT_REDIS_PREFIX = "quant:dashboard:jobs"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sensitive_arg_values(args: list[str]) -> tuple[str, ...]:
    values: list[str] = []
    for index, value in enumerate(args[:-1]):
        if value in _SENSITIVE_ARG_FLAGS and args[index + 1]:
            values.append(str(args[index + 1]))
    for env_name in (
        "TWS_ACCOUNT",
        "NAUTILUS_REDIS_PASSWORD",
        "QUANT_JOBS_REDIS_PASSWORD",
    ):
        env_value = os.environ.get(env_name)
        if env_value:
            values.append(env_value)
    return tuple(dict.fromkeys(values))


def _redact_text(value: str, sensitive_values: tuple[str, ...]) -> str:
    redacted = str(value)
    for secret in sensitive_values:
        redacted = redacted.replace(secret, "<redacted-account>")
    return redacted


def _redact_command(command: list[str]) -> list[str]:
    redacted = list(command)
    for index, value in enumerate(redacted[:-1]):
        if value in _SENSITIVE_ARG_FLAGS:
            redacted[index + 1] = "<redacted-account>"
    return redacted


def _redact_config(value):
    if isinstance(value, dict):
        return {
            key: (
                "<redacted>"
                if str(key).lower() in _SENSITIVE_CONFIG_KEYS
                and item is not None
                and item != ""
                else _redact_config(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_config(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_config(item) for item in value)
    return value


class JobStoreUnavailableError(RuntimeError):
    """Raised when the durable dashboard registry cannot reach Redis."""


class RedisJobStore:
    """Small Redis hash/zset repository for serializable job dictionaries."""

    def __init__(
        self,
        client: Redis | None = None,
        *,
        prefix: str | None = None,
    ) -> None:
        self.prefix = prefix or os.environ.get(
            "QUANT_JOBS_REDIS_PREFIX", _DEFAULT_REDIS_PREFIX
        )
        self.client = client or Redis(
            host=os.environ.get("QUANT_JOBS_REDIS_HOST", "127.0.0.1"),
            port=int(os.environ.get("QUANT_JOBS_REDIS_PORT", "6379")),
            db=int(os.environ.get("QUANT_JOBS_REDIS_DB", "0")),
            username=os.environ.get("QUANT_JOBS_REDIS_USERNAME")
            or os.environ.get("NAUTILUS_REDIS_USERNAME"),
            password=os.environ.get("QUANT_JOBS_REDIS_PASSWORD")
            or os.environ.get("NAUTILUS_REDIS_PASSWORD"),
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
        )

    @property
    def _index_key(self) -> str:
        return f"{self.prefix}:index"

    def _job_key(self, job_id: str) -> str:
        return f"{self.prefix}:job:{job_id}"

    @staticmethod
    def _encode(values: dict[str, Any]) -> dict[str, str]:
        return {
            str(key): json.dumps(value, separators=(",", ":"), default=str)
            for key, value in values.items()
        }

    @staticmethod
    def _decode(values: dict[str, str]) -> dict[str, Any]:
        return {key: json.loads(value) for key, value in values.items()}

    def ping(self) -> bool:
        try:
            return bool(self.client.ping())
        except RedisError as exc:
            raise JobStoreUnavailableError(f"Redis job registry unavailable: {exc}") from exc

    def create(self, job: dict[str, Any]) -> None:
        try:
            with self.client.pipeline(transaction=True) as pipe:
                pipe.hset(self._job_key(job["id"]), mapping=self._encode(job))
                pipe.zadd(self._index_key, {job["id"]: time.time()})
                pipe.execute()
        except RedisError as exc:
            raise JobStoreUnavailableError(f"could not persist job {job['id']}: {exc}") from exc

    def update(self, job_id: str, **changes: Any) -> dict[str, Any] | None:
        key = self._job_key(job_id)
        try:
            if not self.client.exists(key):
                return None
            if changes:
                self.client.hset(key, mapping=self._encode(changes))
            return self.get(job_id)
        except RedisError as exc:
            raise JobStoreUnavailableError(f"could not update job {job_id}: {exc}") from exc

    def transition(
        self,
        job_id: str,
        from_statuses: set[str] | frozenset[str],
        **changes: Any,
    ) -> tuple[dict[str, Any] | None, bool]:
        """Atomically update a job only while its status is expected."""
        key = self._job_key(job_id)
        try:
            while True:
                try:
                    with self.client.pipeline(transaction=True) as pipe:
                        pipe.watch(key)
                        raw_status = pipe.hget(key, "status")
                        if raw_status is None:
                            pipe.unwatch()
                            return None, False
                        if json.loads(raw_status) not in from_statuses:
                            pipe.unwatch()
                            return self.get(job_id), False
                        pipe.multi()
                        pipe.hset(key, mapping=self._encode(changes))
                        pipe.execute()
                        return self.get(job_id), True
                except WatchError:
                    continue
        except RedisError as exc:
            raise JobStoreUnavailableError(
                f"could not transition job {job_id}: {exc}"
            ) from exc

    def get(self, job_id: str) -> dict[str, Any] | None:
        try:
            raw = self.client.hgetall(self._job_key(job_id))
        except RedisError as exc:
            raise JobStoreUnavailableError(f"could not load job {job_id}: {exc}") from exc
        return self._decode(raw) if raw else None

    def list(self) -> list[dict[str, Any]]:
        try:
            job_ids = self.client.zrevrange(self._index_key, 0, -1)
            if not job_ids:
                return []
            with self.client.pipeline(transaction=False) as pipe:
                for job_id in job_ids:
                    pipe.hgetall(self._job_key(job_id))
                raw_jobs = pipe.execute()
        except RedisError as exc:
            raise JobStoreUnavailableError(f"could not list jobs: {exc}") from exc
        return [self._decode(raw) for raw in raw_jobs if raw]

    def delete(self, job_id: str) -> bool:
        try:
            with self.client.pipeline(transaction=True) as pipe:
                pipe.delete(self._job_key(job_id))
                pipe.zrem(self._index_key, job_id)
                deleted, _ = pipe.execute()
            return bool(deleted)
        except RedisError as exc:
            raise JobStoreUnavailableError(f"could not delete job {job_id}: {exc}") from exc


def _pid_is_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class JobManager:
    def __init__(self, store: RedisJobStore | None = None) -> None:
        self.store = store or RedisJobStore()
        JOBS_DIR.mkdir(parents=True, exist_ok=True)

    def new_job_id(self, kind: str) -> str:
        return f"{kind}_{uuid.uuid4().hex[:10]}"

    def submit(
        self,
        kind: str,
        module: str,
        args: list[str],
        config: dict | None = None,
        job_id: str | None = None,
        parent_job_id: str | None = None,
    ) -> dict:
        job_id = job_id or self.new_job_id(kind)
        extra = ["--run-id", job_id] if kind in _ARTIFACT_KINDS else []
        command = [PYTHON_BIN, "-u", "-m", module, *args, *extra]
        log_path = JOBS_DIR / f"{job_id}.log"
        spec_path = JOBS_DIR / f".{job_id}.spec.json"
        started_at = _now_iso()
        job = {
            "id": job_id,
            "kind": kind,
            "command": _redact_command(command),
            "status": "starting",
            "pid": None,
            "process_group_id": None,
            "child_pid": None,
            "child_process_group_id": None,
            "started_at": started_at,
            "finished_at": None,
            "return_code": None,
            "run_id": job_id if kind in _ARTIFACT_KINDS else None,
            "config": _redact_config(config or {}),
            "log_path": str(log_path),
            "cancel_requested_at": None,
            "parent_job_id": parent_job_id,
            "companion_job_ids": [],
        }
        self.store.create(job)

        spec = {
            "job_id": job_id,
            "kind": kind,
            "command": command,
            "cwd": str(WORKDIR),
            "log_path": str(log_path),
            "sensitive_values": _sensitive_arg_values(command),
            "redis_prefix": self.store.prefix,
        }
        file_descriptor = os.open(spec_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as spec_file:
                json.dump(spec, spec_file)
        except BaseException:
            spec_path.unlink(missing_ok=True)
            self.store.update(
                job_id,
                status="failed",
                finished_at=_now_iso(),
                return_code=None,
            )
            raise

        try:
            supervisor = subprocess.Popen(
                [
                    PYTHON_BIN,
                    "-u",
                    "-m",
                    "quant.api.job_worker",
                    "--spec",
                    str(spec_path),
                ],
                cwd=WORKDIR,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
        except BaseException:
            spec_path.unlink(missing_ok=True)
            self.store.update(
                job_id,
                status="failed",
                finished_at=_now_iso(),
                return_code=None,
            )
            raise

        self.store.update(
            job_id,
            pid=supervisor.pid,
            process_group_id=supervisor.pid,
        )
        # Reap the detached supervisor while this API instance lives. If the
        # API restarts, the OS adopts it and the Redis-backed lifecycle remains
        # authoritative.
        threading.Thread(target=supervisor.wait, daemon=True).start()
        return self.get(job_id) or job

    def link_companion(self, parent_job_id: str, companion_job_id: str) -> dict:
        parent = self.get(parent_job_id)
        companion = self.get(companion_job_id)
        if parent is None or companion is None:
            raise KeyError("parent and companion jobs must both exist")
        companions = list(dict.fromkeys([*(parent.get("companion_job_ids") or []), companion_job_id]))
        self.store.update(parent_job_id, companion_job_ids=companions)
        self.store.update(companion_job_id, parent_job_id=parent_job_id)
        return self.get(parent_job_id) or parent

    @staticmethod
    def _pump_logs(
        proc: subprocess.Popen,
        log_path: Path,
        sensitive_values: tuple[str, ...],
    ) -> None:
        stream = proc.stdout
        if stream is None:
            return
        with open(log_path, "w", encoding="utf-8") as log_fh:
            for line in stream:
                log_fh.write(_redact_text(line, sensitive_values))
                log_fh.flush()
        stream.close()

    def _refresh(self, job: dict[str, Any]) -> dict[str, Any]:
        if job.get("status") not in _ACTIVE_STATUSES:
            return job
        pid = job.get("pid")
        if pid is not None and _pid_is_alive(int(pid)):
            return job
        # A healthy supervisor commits terminal state before exiting. A dead
        # supervisor with an active record therefore represents a crash. Kill
        # any independently grouped child immediately: an unmanaged trading
        # process is more dangerous than losing its graceful shutdown path.
        child_process_group_id = int(job.get("child_process_group_id") or 0)
        if child_process_group_id:
            try:
                os.killpg(child_process_group_id, signal.SIGKILL)
            except ProcessLookupError:
                pass
        refreshed, _ = self.store.transition(
            job["id"],
            _ACTIVE_STATUSES,
            status="failed",
            finished_at=_now_iso(),
            return_code=job.get("return_code"),
            failure_reason="detached job supervisor exited without terminal state",
        )
        return refreshed or job

    def get(self, job_id: str) -> dict | None:
        job = self.store.get(job_id)
        return self._refresh(job) if job is not None else None

    def list(self) -> list[dict]:
        return [self._refresh(job) for job in self.store.list()]

    def logs(self, job_id: str, tail_lines: int = 200) -> dict | None:
        job = self.get(job_id)
        if job is None:
            return None
        log_path = Path(job["log_path"])
        if not log_path.exists():
            lines: list[str] = []
        else:
            with open(log_path, errors="replace") as log_file:
                all_lines = log_file.readlines()
            lines = [
                _ANSI_RE.sub("", line.rstrip("\n")) for line in all_lines[-tail_lines:]
            ]
        return {"job_id": job_id, "status": job["status"], "lines": lines}

    def delete(self, job_id: str) -> dict:
        job = self.get(job_id)
        if job is not None and job["status"] in _ACTIVE_STATUSES:
            raise ValueError(f"job {job_id!r} is still running; cancel it first")
        companion_ids = list(job.get("companion_job_ids") or []) if job else []
        for companion_id in companion_ids:
            companion = self.get(str(companion_id))
            if companion is not None and companion["status"] in _ACTIVE_STATUSES:
                raise ValueError(
                    f"companion job {companion_id!r} is still running; cancel it first"
                )
        deleted: list[str] = []
        for current_id in [job_id, *[str(value) for value in companion_ids]]:
            if self.store.delete(current_id):
                deleted.append(current_id)
            for pattern in (f"{current_id}*", f".{current_id}*"):
                for path in JOBS_DIR.glob(pattern):
                    path.unlink(missing_ok=True)
        return {"job_found": job_id in deleted, "deleted_job_ids": deleted}

    def cancel(self, job_id: str) -> dict | None:
        job = self.get(job_id)
        if job is None:
            return None
        if job["status"] not in _ACTIVE_STATUSES:
            return job

        for companion_id in job.get("companion_job_ids") or []:
            companion = self.get(str(companion_id))
            if companion is not None and companion.get("status") in _ACTIVE_STATUSES:
                self.cancel(str(companion_id))

        job, changed = self.store.transition(
            job_id,
            _ACTIVE_STATUSES,
            status="cancelling",
            cancel_requested_at=_now_iso(),
        )
        if job is None:
            return None
        if not changed:
            return job
        stop_signal = signal.SIGINT if job["kind"] in {"paper", "live"} else signal.SIGTERM
        process_group_id = int(
            job.get("child_process_group_id")
            or job.get("process_group_id")
            or job.get("pid")
            or 0
        )
        if process_group_id:
            try:
                os.killpg(process_group_id, stop_signal)
            except ProcessLookupError:
                pass

        grace_seconds = 30.0 if job["kind"] in {"paper", "live"} else _TERMINATE_GRACE_SECONDS
        deadline = time.monotonic() + grace_seconds
        pid = int(job.get("pid") or 0)
        while _pid_is_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        if _pid_is_alive(pid) and process_group_id:
            try:
                os.killpg(process_group_id, signal.SIGKILL)
            except ProcessLookupError:
                pass

        terminal, _ = self.store.transition(
            job_id,
            _ACTIVE_STATUSES,
            status="cancelled",
            finished_at=_now_iso(),
        )
        return terminal or job
