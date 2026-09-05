"""Host-sized, spawned research workers; no broker or Optuna state is shared."""
from __future__ import annotations

import argparse
import json
import math
import multiprocessing
import os
import platform
import signal
import subprocess
from concurrent.futures import ProcessPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path


GIB = 1024 ** 3
THREAD_ENV = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
              "VECLIB_MAXIMUM_THREADS", "BLIS_NUM_THREADS", "NUMEXPR_NUM_THREADS")


def _sysctl(key: str) -> str:
    try:
        return subprocess.check_output(
            ["sysctl", "-n", key], stderr=subprocess.DEVNULL, text=True, timeout=2
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def host_resources() -> dict:
    cpus = os.cpu_count() or 1
    memory = 0
    fast_cores = cpus
    chip = platform.processor() or platform.machine()
    if platform.system() == "Darwin":
        chip = _sysctl("machdep.cpu.brand_string") or chip
        memory = int(_sysctl("hw.memsize") or 0)
        # M4 has performance/efficiency tiers; newer chips may instead expose
        # super/performance tiers. Include every tier except efficiency cores.
        levels = int(_sysctl("hw.nperflevels") or 0)
        counts = []
        for level in range(levels):
            name = _sysctl(f"hw.perflevel{level}.name").lower()
            count = int(_sysctl(f"hw.perflevel{level}.physicalcpu") or 0)
            if name and "efficiency" not in name:
                counts.append(count)
        fast_cores = sum(counts) or cpus
    else:
        if hasattr(os, "sched_getaffinity"):
            cpus = fast_cores = len(os.sched_getaffinity(0))
        try:
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemTotal:"):
                    memory = int(line.split()[1]) * 1024
        except OSError:
            pass
    return {"chip": chip, "logical_cpus": cpus, "compute_cores": fast_cores,
            "memory_gb": memory / GIB}


@dataclass(frozen=True)
class ComputePlan:
    workers: int
    memory_budget_gb: float
    worker_memory_gb: float
    host: dict
    native_threads: int = 1

    def as_dict(self) -> dict:
        return asdict(self)


def resolve_compute_plan(workers: int = 0, memory_budget_gb: float = 0,
                         worker_memory_gb: float = 4, *, tasks: int,
                         host: dict | None = None) -> ComputePlan:
    """Admission estimate, not an OS memory limit. Reserve RAM and CPU for services."""
    if workers < 0 or tasks < 1:
        raise ValueError("workers must be >= 0 and tasks must be >= 1")
    if not math.isfinite(memory_budget_gb) or memory_budget_gb < 0:
        raise ValueError("memory budget must be finite and >= 0")
    if not math.isfinite(worker_memory_gb) or worker_memory_gb <= 0:
        raise ValueError("worker memory estimate must be finite and > 0")
    host = host or host_resources()
    total = host["memory_gb"]
    available = max(0.0, total - max(8.0, total * 0.25)) if total else 0.0
    if memory_budget_gb and total and memory_budget_gb > available:
        raise ValueError(f"memory budget exceeds {available:g} GiB after the host reserve")
    budget = memory_budget_gb or available
    if budget and worker_memory_gb > budget:
        raise ValueError("memory budget cannot fit one worker at the requested memory estimate")
    memory_cap = max(1, int(budget // worker_memory_gb)) if budget else 1
    cores = host["compute_cores"]
    cpu_cap = max(1, cores - (2 if cores >= 8 else 1))
    count = min(workers or cpu_cap, cpu_cap, memory_cap, tasks)
    return ComputePlan(count, budget, worker_memory_gb, host)


def add_compute_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workers", type=int,
                        default=os.environ.get("QUANT_COMPUTE_WORKERS", "0"),
                        help="Independent backtest processes: 0=host-sized, 1=serial. "
                             "Clamped to CPU, memory and available fold/cost tasks.")
    parser.add_argument("--memory-budget-gb", type=float,
                        default=os.environ.get("QUANT_COMPUTE_MEMORY_GB", "0"),
                        help="Research RAM budget in GiB; 0 reserves max(8 GiB, 25%%) for services.")
    parser.add_argument("--worker-memory-gb", type=float, default=4,
                        help="Estimated peak GiB per worker, including caches (default 4). "
                             "Use the benchmark's RSS measurements for large datasets.")


_worker_threads = None


def _initialize_worker() -> None:
    global _worker_threads
    from threadpoolctl import threadpool_limits
    _worker_threads = threadpool_limits(limits=1)
    # Parent owns Ctrl-C and cleanup; SIGTERM remains available for killpg.
    signal.signal(signal.SIGINT, signal.SIG_IGN)


class ComputePool:
    """Reuse spawn workers across trials; return results in submitted order.

    Only the coordinator talks to Optuna/SQLite. Pruning cancels queued work
    and drains already-running work before the next trial can start.
    """

    def __init__(self, plan: ComputePlan):
        self.plan = plan
        self.executor = None

    def __enter__(self):
        from threadpoolctl import threadpool_limits
        self._environment = {key: os.environ.get(key) for key in THREAD_ENV}
        for key in THREAD_ENV:
            os.environ[key] = "1"  # inherited before spawn imports NumPy/Accelerate
        self._threads = threadpool_limits(limits=1)
        self._old_sigterm = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, self._interrupt)
        if self.plan.workers > 1:
            self.executor = ProcessPoolExecutor(
                max_workers=self.plan.workers,
                mp_context=multiprocessing.get_context("spawn"),
                initializer=_initialize_worker,
            )
        return self

    @staticmethod
    def _interrupt(_signum, _frame):
        raise KeyboardInterrupt("research cancelled")

    @contextmanager
    def results(self, function, tasks):
        if self.executor is None:
            yield map(function, tasks)
            return
        futures = [self.executor.submit(function, task) for task in tasks]
        try:
            yield (future.result() for future in futures)
        except (KeyboardInterrupt, SystemExit):
            self._terminate_workers()
            raise
        finally:
            for future in futures:
                future.cancel()
            # Bound outstanding work to this trial/scenario, including pruning.
            wait(futures)

    def _terminate_workers(self):
        # CPython 3.12 is pinned by the project and lacks 3.14's public
        # terminate_workers(). Keep the compatibility access isolated here.
        # These are this executor's owned Process handles, never a broad
        # process-group signal that could reach TWS or another research job.
        processes = list((getattr(self.executor, "_processes", None) or {}).values())
        for process in processes:
            if process.is_alive():
                process.terminate()

    def __exit__(self, exc_type, exc, tb):
        try:
            if self.executor is not None:
                if exc_type is not None:
                    self._terminate_workers()
                self.executor.shutdown(wait=True, cancel_futures=True)
        finally:
            signal.signal(signal.SIGTERM, self._old_sigterm)
            self._threads.restore_original_limits()
            for key, value in self._environment.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect local research worker sizing; starts no jobs.")
    add_compute_arguments(parser)
    parser.add_argument("--tasks", type=int, default=10,
                        help="Independent tasks per trial (2 x walk-forward folds).")
    args = parser.parse_args()
    try:
        plan = resolve_compute_plan(args.workers, args.memory_budget_gb,
                                    args.worker_memory_gb, tasks=args.tasks)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(plan.as_dict(), indent=2))


if __name__ == "__main__":
    main()
