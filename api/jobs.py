"""Subprocess-based job orchestration for the dashboard's action buttons.

Backtest/Optuna/paper/live are all long-lived CLI entrypoints today
(run_backtest.py, optimize.py, run_live.py). Rather than re-implement any of
that logic in-process (nautilus's BacktestEngine and TradingNode are not
meant to share a process with FastAPI's event loop, and a long Optuna search
would block it), the API spawns the SAME CLI commands as subprocesses and
tracks their lifecycle. Backtest/optimize subprocesses run to completion and
write a run artifact (see quant.run.artifacts); paper/live subprocesses are
long-running daemons (TradingNode.run() loops forever) that stay "running"
until they exit on their own or are cancelled.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

# nautilus's console logger writes ANSI colour codes straight into stdout;
# strip them so the dashboard's log console renders plain, legible text.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_SENSITIVE_ARG_FLAGS = frozenset(
    {"--account-id", "--api-key", "--password", "--redis-password", "--token"}
)
_SENSITIVE_CONFIG_KEYS = frozenset(
    {"account_id", "api_key", "confirm", "password", "redis_password", "secret", "token"}
)

# The interpreter running THIS api process already has nautilus_trader/optuna/
# scikit-learn/hmmlearn installed (the project's .quant312 venv) -- reuse it
# for spawned jobs instead of guessing a system "python".
PYTHON_BIN = sys.executable

# quant/api/jobs.py -> parents[0]=api, [1]=quant, [2]=the directory ABOVE the
# quant package (e.g. .../Workspace). Every existing command in this project
# (run_backtest.py, optimize.py, CLAUDE.md's examples) is invoked with THIS
# directory as cwd, using relative paths like "quant/data/...", "quant/runs/
# ..." -- jobs must run with the identical cwd or those paths land elsewhere.
WORKDIR = Path(__file__).resolve().parents[2]
JOBS_DIR = WORKDIR / "quant" / "jobs"

_TERMINATE_GRACE_SECONDS = 5.0

# backtest/optimize run to completion and produce a run artifact; paper/live
# are long-running daemons with no run-artifact concept. They publish a
# job-scoped atomic telemetry snapshot instead (see run/telemetry.py and
# api/live_mock.py).
_ARTIFACT_KINDS = ("backtest", "optimize")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sensitive_arg_values(args: list[str]) -> tuple[str, ...]:
    values: list[str] = []
    for index, value in enumerate(args[:-1]):
        if value in _SENSITIVE_ARG_FLAGS and args[index + 1]:
            values.append(str(args[index + 1]))
    for env_name in ("TWS_ACCOUNT", "NAUTILUS_REDIS_PASSWORD"):
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


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, dict] = {}
        self._procs: dict[str, subprocess.Popen] = {}
        self._log_threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        JOBS_DIR.mkdir(parents=True, exist_ok=True)

    def new_job_id(self, kind: str) -> str:
        """Generate an id up front, e.g. so a caller can write a temp file
        keyed by it (structural-overrides JSON) before calling submit()."""
        return f"{kind}_{uuid.uuid4().hex[:10]}"

    def submit(
        self, kind: str, module: str, args: list[str], config: dict | None = None,
        job_id: str | None = None,
    ) -> dict:
        job_id = job_id or self.new_job_id(kind)
        # backtest/optimize accept --run-id so the artifact this job produces
        # is named identically to the job, letting the frontend go straight
        # from "job finished" to GET /api/runs/{job_id}.
        extra = ["--run-id", job_id] if kind in _ARTIFACT_KINDS else []
        command = [PYTHON_BIN, "-u", "-m", module, *args, *extra]
        log_path = JOBS_DIR / f"{job_id}.log"
        sensitive_values = _sensitive_arg_values(command)
        proc = subprocess.Popen(
            command,
            cwd=WORKDIR,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            bufsize=1,
        )
        log_thread = threading.Thread(
            target=self._pump_logs,
            args=(proc, log_path, sensitive_values),
            name=f"quant-job-log-{job_id}",
            daemon=True,
        )
        log_thread.start()
        job = {
            "id": job_id,
            "kind": kind,
            "command": _redact_command(command),
            "status": "running",
            "pid": proc.pid,
            "started_at": _now_iso(),
            "finished_at": None,
            "return_code": None,
            "run_id": job_id if kind in _ARTIFACT_KINDS else None,
            "config": _redact_config(config or {}),
            "log_path": str(log_path),
        }
        with self._lock:
            self._jobs[job_id] = job
            self._procs[job_id] = proc
            self._log_threads[job_id] = log_thread
        return dict(job)

    @staticmethod
    def _pump_logs(
        proc: subprocess.Popen,
        log_path: Path,
        sensitive_values: tuple[str, ...],
    ) -> None:
        stream = proc.stdout
        if stream is None:
            return
        with open(log_path, "w") as log_fh:
            for line in stream:
                log_fh.write(_redact_text(line, sensitive_values))
                log_fh.flush()
        stream.close()

    def _refresh_locked(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        proc = self._procs.get(job_id)
        if job is None or proc is None or job["status"] != "running":
            return
        rc = proc.poll()
        if rc is None:
            return
        log_thread = self._log_threads.get(job_id)
        if log_thread is not None:
            log_thread.join(timeout=1.0)
        job["return_code"] = rc
        job["finished_at"] = _now_iso()
        job["status"] = "completed" if rc == 0 else "failed"

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            if job_id not in self._jobs:
                return None
            self._refresh_locked(job_id)
            return dict(self._jobs[job_id])

    def list(self) -> list[dict]:
        with self._lock:
            for job_id in self._jobs:
                self._refresh_locked(job_id)
            jobs = [dict(j) for j in self._jobs.values()]
        jobs.sort(key=lambda j: j["started_at"], reverse=True)
        return jobs

    def logs(self, job_id: str, tail_lines: int = 200) -> dict | None:
        job = self.get(job_id)
        if job is None:
            return None
        log_path = Path(job["log_path"])
        if not log_path.exists():
            lines: list[str] = []
        else:
            with open(log_path, errors="replace") as fh:
                all_lines = fh.readlines()
            lines = [
                _ANSI_RE.sub("", line.rstrip("\n")) for line in all_lines[-tail_lines:]
            ]
        return {"job_id": job_id, "status": job["status"], "lines": lines}

    def delete(self, job_id: str) -> dict:
        """Remove a job's in-memory record (if any) and its log/temp-override
        files under JOBS_DIR. Used by DELETE /api/runs/{run_id} to clean up
        the job that produced a run artifact (job_id == run_id for
        backtest/optimize -- see submit()'s --run-id wiring above).

        Raises ValueError if the job is still running -- cancel it first.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                self._refresh_locked(job_id)
                if job["status"] == "running":
                    raise ValueError(f"job {job_id!r} is still running; cancel it first")
                del self._jobs[job_id]
                self._procs.pop(job_id, None)
                self._log_threads.pop(job_id, None)
            job_found = job is not None
        for path in JOBS_DIR.glob(f"{job_id}*"):
            path.unlink(missing_ok=True)
        return {"job_found": job_found}

    def cancel(self, job_id: str) -> dict | None:
        with self._lock:
            proc = self._procs.get(job_id)
            job = self._jobs.get(job_id)
            if proc is None or job is None:
                return None
            self._refresh_locked(job_id)
            if job["status"] != "running":
                return dict(job)
            # Long-running Nautilus nodes need a KeyboardInterrupt path so
            # TradingNode.run() reaches its shutdown/save-state sequence.
            # SIGTERM leaves the node alive until the grace timeout and then
            # forces SIGKILL, losing the strategy snapshot.
            stop_signal = signal.SIGINT if job["kind"] in {"paper", "live"} else signal.SIGTERM
            proc.send_signal(stop_signal)
        grace_seconds = 30.0 if job["kind"] in {"paper", "live"} else _TERMINATE_GRACE_SECONDS
        try:
            proc.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=_TERMINATE_GRACE_SECONDS)
        log_thread = self._log_threads.get(job_id)
        if log_thread is not None:
            log_thread.join(timeout=1.0)
        with self._lock:
            job["return_code"] = proc.returncode
            job["finished_at"] = _now_iso()
            job["status"] = "cancelled"
            return dict(job)
