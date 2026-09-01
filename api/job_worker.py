"""Detached supervisor used by :mod:`quant.api.jobs`.

This module is intentionally executable in its own session. It owns the CLI
child, redacts logs, and commits lifecycle changes to Redis even when Uvicorn
is restarted.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
from pathlib import Path

from quant.api.jobs import JobManager, RedisJobStore, _now_iso


def supervise(spec_path: Path) -> int:
    with open(spec_path, encoding="utf-8") as spec_file:
        spec = json.load(spec_file)

    job_id = str(spec["job_id"])
    kind = str(spec["kind"])
    command = [str(value) for value in spec["command"]]
    workdir = Path(spec["cwd"])
    log_path = Path(spec["log_path"])
    sensitive_values = tuple(str(value) for value in spec.get("sensitive_values", ()))
    store = RedisJobStore(prefix=str(spec["redis_prefix"]))
    child: subprocess.Popen | None = None
    stop_signal: int | None = None

    # Repair the launch record even if Uvicorn exits after Popen succeeds but
    # before it can persist the detached supervisor PID.
    store.update(
        job_id,
        pid=os.getpid(),
        process_group_id=os.getpgrp(),
    )

    def _forward_signal(signum, _frame) -> None:
        nonlocal stop_signal
        stop_signal = int(signum)
        if child is not None and child.poll() is None:
            child.send_signal(signum)

    signal.signal(signal.SIGINT, _forward_signal)
    signal.signal(signal.SIGTERM, _forward_signal)

    try:
        child = subprocess.Popen(
            command,
            cwd=workdir,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            bufsize=1,
            start_new_session=True,
        )
        current = store.get(job_id) or {}
        cancel_requested = bool(current.get("cancel_requested_at")) or stop_signal is not None
        if cancel_requested:
            child.send_signal(
                stop_signal
                or (signal.SIGINT if kind in {"paper", "live"} else signal.SIGTERM)
            )
        store.update(
            job_id,
            child_pid=child.pid,
            child_process_group_id=child.pid,
        )
        if not cancel_requested:
            store.transition(job_id, {"starting"}, status="running")
        JobManager._pump_logs(child, log_path, sensitive_values)
        return_code = child.wait()
        current = store.get(job_id) or {}
        cancelled = bool(current.get("cancel_requested_at")) or stop_signal is not None
        store.transition(
            job_id,
            {"starting", "running", "cancelling"},
            status="cancelled" if cancelled else ("completed" if return_code == 0 else "failed"),
            return_code=return_code,
            finished_at=_now_iso(),
        )
        return return_code
    except BaseException as exc:
        if child is not None and child.poll() is None:
            child.kill()
            child.wait()
        current = store.get(job_id) or {}
        store.transition(
            job_id,
            {"starting", "running", "cancelling"},
            status="cancelled" if current.get("cancel_requested_at") else "failed",
            return_code=child.returncode if child is not None else None,
            finished_at=_now_iso(),
            failure_reason=f"{type(exc).__name__}: {exc}",
        )
        return 1
    finally:
        spec_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one detached dashboard job")
    parser.add_argument("--spec", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(supervise(args.spec))


if __name__ == "__main__":
    main()
