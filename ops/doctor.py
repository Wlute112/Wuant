"""Preflight diagnostics for an unattended quant host."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import sys

from redis import Redis

from quant.api.jobs import RedisJobStore, _pid_is_alive
from quant.ops.state import OperationsStore


@dataclass(frozen=True)
class DoctorCheck:
    key: str
    passed: bool
    severity: str
    detail: str


def _sqlite_check(path: Path) -> DoctorCheck:
    if not path.exists():
        return DoctorCheck(f"sqlite:{path.name}", True, "INFO", "not created yet")
    try:
        with sqlite3.connect(path) as connection:
            result = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        return DoctorCheck(f"sqlite:{path.name}", result.lower() == "ok", "CRITICAL", result)
    except Exception as exc:  # noqa: BLE001
        return DoctorCheck(f"sqlite:{path.name}", False, "CRITICAL", f"{type(exc).__name__}: {exc}")


def run_doctor(
    root: Path,
    *,
    redis_host: str = "127.0.0.1",
    redis_port: int = 6379,
    skip_redis: bool = False,
    ibkr_host: str = "127.0.0.1",
    ibkr_port: int = 7497,
    require_ibkr: bool = False,
    operations_db: str = "",
) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    supported = sys.version_info[:2] == (3, 12)
    checks.append(DoctorCheck("python", supported, "CRITICAL", sys.version.split()[0]))
    lock = root / "requirements.lock"
    checks.append(DoctorCheck("dependency_lock", lock.exists(), "CRITICAL", str(lock)))
    if lock.exists():
        unpinned = [
            line.strip()
            for line in lock.read_text().splitlines()
            if line.strip()
            and not line.lstrip().startswith("#")
            and re.fullmatch(r"[A-Za-z0-9_.-]+==[^\s]+", line.strip()) is None
        ]
        checks.append(
            DoctorCheck(
                "dependency_lock_pinned",
                not unpinned,
                "CRITICAL",
                "all dependencies exact" if not unpinned else f"unlocked entries: {unpinned}",
            )
        )
    pip_check = subprocess.run(
        [sys.executable, "-m", "pip", "check"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    checks.append(
        DoctorCheck(
            "pip_check",
            pip_check.returncode == 0,
            "CRITICAL",
            (pip_check.stdout or pip_check.stderr).strip() or "ok",
        )
    )
    free = shutil.disk_usage(root).free
    checks.append(
        DoctorCheck(
            "disk_space",
            free >= 5 * 1024**3,
            "CRITICAL",
            f"{free / 1024**3:.2f} GiB free",
        )
    )
    for path in (root / "data" / "news.sqlite3", root / "optimize" / "studies.db"):
        checks.append(_sqlite_check(path))
    if operations_db:
        path = Path(operations_db).expanduser().resolve()
        checks.append(_sqlite_check(path))
        if path.exists():
            store = OperationsStore(str(path))
            ok, detail = store.verify_audit_chain()
            checks.append(DoctorCheck("audit_chain", ok, "CRITICAL", detail))
            store.close()
    if not skip_redis:
        try:
            client = Redis(host=redis_host, port=redis_port, decode_responses=True, socket_timeout=3)
            pong = bool(client.ping())
            checks.append(DoctorCheck("redis", pong, "CRITICAL", "PONG" if pong else "no response"))
            appendonly = str(client.config_get("appendonly").get("appendonly", "no")).lower()
            checks.append(DoctorCheck("redis_aof", appendonly == "yes", "CRITICAL", f"appendonly={appendonly}"))
            # Loading the registry also proves its configured namespace can be read.
            jobs = RedisJobStore(client=client).list()
            stale = [
                str(job.get("id"))
                for job in jobs
                if job.get("status") in {"starting", "running", "cancelling"}
                and not _pid_is_alive(int(job.get("pid") or 0))
            ]
            checks.append(
                DoctorCheck(
                    "job_registry",
                    not stale,
                    "CRITICAL",
                    f"{len(jobs)} durable job(s)" if not stale else f"dead active supervisors: {stale}",
                )
            )
        except Exception as exc:  # noqa: BLE001
            checks.append(DoctorCheck("redis", False, "CRITICAL", f"{type(exc).__name__}: {exc}"))
    try:
        with socket.create_connection((ibkr_host, ibkr_port), timeout=2):
            connected = True
    except OSError:
        connected = False
    checks.append(
        DoctorCheck(
            "ibkr_socket",
            connected or not require_ibkr,
            "CRITICAL" if require_ibkr else "WARNING",
            f"{'reachable' if connected else 'not reachable'} at {ibkr_host}:{ibkr_port}",
        )
    )
    clock_detail = datetime.now(timezone.utc).isoformat()
    if shutil.which("timedatectl"):
        result = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        ntp = result.stdout.strip().lower()
        checks.append(DoctorCheck("clock_sync", ntp == "yes", "CRITICAL", f"NTPSynchronized={ntp or 'unknown'}"))
    else:
        checks.append(DoctorCheck("clock_sync", True, "WARNING", f"verify macOS automatic time manually; UTC={clock_detail}"))
    return checks


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--redis-host", default="127.0.0.1")
    parser.add_argument("--redis-port", type=int, default=6379)
    parser.add_argument("--skip-redis", action="store_true")
    parser.add_argument("--ibkr-host", default="127.0.0.1")
    parser.add_argument("--ibkr-port", type=int, default=7497)
    parser.add_argument("--require-ibkr", action="store_true")
    parser.add_argument("--operations-db", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    checks = run_doctor(
        Path(args.root).resolve(),
        redis_host=args.redis_host,
        redis_port=args.redis_port,
        skip_redis=args.skip_redis,
        ibkr_host=args.ibkr_host,
        ibkr_port=args.ibkr_port,
        require_ibkr=args.require_ibkr,
        operations_db=args.operations_db,
    )
    if args.json:
        print(json.dumps({"checks": [asdict(item) for item in checks]}, indent=2))
    else:
        for item in checks:
            print(f"{'PASS' if item.passed else 'FAIL'} {item.key}: {item.detail}")
    raise SystemExit(1 if any(not item.passed and item.severity == "CRITICAL" for item in checks) else 0)


if __name__ == "__main__":
    main()
