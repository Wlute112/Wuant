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
import threading
import time
from pathlib import Path

from quant.api.jobs import JobManager, RedisJobStore, _now_iso
from quant.run.admission import AdmissionStore, PARALLEL_KINDS, RESEARCH_KINDS
from quant.run.compute import THREAD_ENV, resolve_compute_plan


def research_request(kind, command, admission):
    """Resolve once and pin all nested seed/fold pools to this reservation."""
    if kind not in PARALLEL_KINDS:
        return 1, 4.0, command
    def option(flag, default):
        return command[command.index(flag) + 1] if flag in command else default
    plan = resolve_compute_plan(
        int(option("--workers", os.environ.get("QUANT_COMPUTE_WORKERS", 0))),
        float(option("--memory-budget-gb", os.environ.get("QUANT_COMPUTE_MEMORY_GB", 0))),
        float(option("--worker-memory-gb", 4)), tasks=20, host=admission.host,
    )
    limits = admission.status()["limits"]
    workers = min(plan.workers, limits["cpu_slots"], int(limits["memory_gb"] // plan.worker_memory_gb))
    if workers < 1:
        raise ValueError("Host research limits cannot fit one worker")
    command = list(command)
    for flag, value in (("--workers", workers), ("--memory-budget-gb", workers * plan.worker_memory_gb)):
        if flag in command:
            command[command.index(flag) + 1] = str(value)
        else:
            command.extend([flag, str(value)])
    return workers, workers * plan.worker_memory_gb, command


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
    reservation = None
    admission = None
    watcher = None
    watcher_stop = threading.Event()
    resource_failure = None

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
        environment = os.environ.copy()
        if kind in RESEARCH_KINDS:
            admission = AdmissionStore()
            workers, memory, command = research_request(kind, command, admission)
            reservation = admission.enqueue(workers, memory, job_id)
            token = reservation["token"]
            while True:
                current = store.get(job_id) or {}
                if current.get("cancel_requested_at") or stop_signal is not None:
                    store.transition(job_id, {"queued", "cancelling"}, status="cancelled", finished_at=_now_iso())
                    return 0
                reservation = admission.claim(token)
                store.update(job_id, resources=reservation)
                if reservation["state"] == "running":
                    break
                time.sleep(0.5)
            store.transition(job_id, {"queued"}, status="starting", admitted_at=_now_iso())
            environment["QUANT_RESEARCH_LEASE"] = token
            for key in THREAD_ENV:
                environment[key] = "1"
            # Research and all descendants yield scheduler priority to TWS,
            # Redis and the separately launched execution/risk supervisors.
            os.nice(10)
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
            env=environment,
        )
        if reservation:
            # Record the independently owned group before any log processing.
            reservation = admission.sample(token, child.pid, child.pid)
            store.update(job_id, resources=reservation)

            def measure():
                nonlocal resource_failure
                while not watcher_stop.wait(1):
                    if child.poll() is not None:
                        return
                    try:
                        sample = admission.sample(token, child.pid, child.pid)
                        store.update(job_id, resources=sample)
                        if sample["rss_gb"] <= sample["memory_gb"]:
                            continue
                        resource_failure = "Research process-tree RSS exceeded its reserved memory; increase the estimate or reduce the dataset"
                    except Exception as exc:
                        resource_failure = f"Research resource monitoring unavailable: {type(exc).__name__}"
                    # Bound termination without relying on buffered console output
                    # or allowing a worker to survive after releasing its budget.
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    return

            watcher = threading.Thread(target=measure, daemon=True)
            watcher.start()
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
            status="cancelled" if cancelled else ("completed" if return_code == 0 and not resource_failure else "failed"),
            return_code=return_code,
            finished_at=_now_iso(),
            **({"failure_reason": resource_failure} if resource_failure else {}),
        )
        return return_code
    except BaseException as exc:
        if child is not None and child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
        current = store.get(job_id) or {}
        store.transition(
            job_id,
            {"queued", "starting", "running", "cancelling"},
            status="cancelled" if current.get("cancel_requested_at") else "failed",
            return_code=child.returncode if child is not None else None,
            finished_at=_now_iso(),
            failure_reason=f"{type(exc).__name__}: {exc}",
        )
        return 1
    finally:
        watcher_stop.set()
        if watcher:
            watcher.join(timeout=5)
        if reservation:
            # Reap any surviving descendants before returning their capacity.
            if child is not None:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            admission.release(reservation["token"])
        spec_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one detached dashboard job")
    parser.add_argument("--spec", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(supervise(args.spec))


if __name__ == "__main__":
    main()
