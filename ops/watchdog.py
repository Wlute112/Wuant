"""Bounded-restart process watchdog for unattended local services."""
from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time
from urllib.request import urlopen

from quant.ops.alerts import Alert, AlertDispatcher, sinks_from_environment
from quant.ops.state import OperationsStore


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    command: tuple[str, ...]
    cwd: str
    log_path: str
    health_type: str = "process"
    health_target: str = ""
    check_interval_seconds: float = 5.0
    startup_grace_seconds: float = 15.0
    max_restarts: int = 5
    restart_window_seconds: float = 600.0
    base_backoff_seconds: float = 1.0
    max_log_bytes: int = 10_000_000
    log_backups: int = 5

    @classmethod
    def from_dict(cls, value: dict) -> "ServiceSpec":
        command = value.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
            raise ValueError("service command must be a non-empty JSON string array")
        return cls(
            name=str(value["name"]),
            command=tuple(command),
            cwd=str(Path(value["cwd"]).expanduser().resolve()),
            log_path=str(Path(value["log_path"]).expanduser().resolve()),
            health_type=str(value.get("health_type", "process")),
            health_target=str(value.get("health_target", "")),
            check_interval_seconds=float(value.get("check_interval_seconds", 5.0)),
            startup_grace_seconds=float(value.get("startup_grace_seconds", 15.0)),
            max_restarts=int(value.get("max_restarts", 5)),
            restart_window_seconds=float(value.get("restart_window_seconds", 600.0)),
            base_backoff_seconds=float(value.get("base_backoff_seconds", 1.0)),
            max_log_bytes=int(value.get("max_log_bytes", 10_000_000)),
            log_backups=int(value.get("log_backups", 5)),
        )


class ServiceWatchdog:
    def __init__(self, spec: ServiceSpec, operations_db: str) -> None:
        self.spec = spec
        self.store = OperationsStore(operations_db)
        self.component = f"watchdog:{spec.name}"
        self.restarts: deque[float] = deque()
        self.process: subprocess.Popen | None = None
        self.started_at = 0.0
        self.stop_requested = False
        self.alerts = AlertDispatcher(
            self.store,
            sinks_from_environment(f"{operations_db}.alerts.jsonl"),
        )

    def request_stop(self, *_args) -> None:
        self.stop_requested = True

    def _start(self) -> None:
        Path(self.spec.log_path).parent.mkdir(parents=True, exist_ok=True)
        self._rotate_log()
        log = open(self.spec.log_path, "ab", buffering=0)
        try:
            self.process = subprocess.Popen(
                list(self.spec.command),
                cwd=self.spec.cwd,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
                env=os.environ.copy(),
            )
        finally:
            log.close()
        self.started_at = time.monotonic()
        self.store.append_event(self.component, "SERVICE_STARTED", {"pid": self.process.pid, "command": list(self.spec.command)})

    def _rotate_log(self) -> None:
        path = Path(self.spec.log_path)
        if not path.exists() or path.stat().st_size < max(self.spec.max_log_bytes, 100_000):
            return
        backups = max(1, self.spec.log_backups)
        path.with_name(f"{path.name}.{backups}").unlink(missing_ok=True)
        for index in range(backups - 1, 0, -1):
            source = path.with_name(f"{path.name}.{index}")
            if source.exists():
                source.replace(path.with_name(f"{path.name}.{index + 1}"))
        path.replace(path.with_name(f"{path.name}.1"))

    def _healthy(self) -> bool:
        if self.process is None or self.process.poll() is not None:
            return False
        if time.monotonic() - self.started_at < self.spec.startup_grace_seconds:
            return True
        if self.spec.health_type == "process":
            return True
        if self.spec.health_type == "http":
            try:
                with urlopen(self.spec.health_target, timeout=3) as response:  # noqa: S310 - local operator config
                    return 200 <= int(response.status) < 500
            except OSError:
                return False
        if self.spec.health_type == "tcp":
            host, raw_port = self.spec.health_target.rsplit(":", 1)
            try:
                with socket.create_connection((host, int(raw_port)), timeout=3):
                    return True
            except OSError:
                return False
        raise ValueError(f"unsupported health_type {self.spec.health_type!r}")

    def _stop_process(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process.wait(timeout=5)

    def run(self) -> int:
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)
        self._start()
        try:
            while not self.stop_requested:
                healthy = self._healthy()
                self.store.heartbeat(
                    self.component,
                    str(os.getpid()),
                    status="HEALTHY" if healthy else "DEGRADED",
                    details={"service_pid": self.process.pid if self.process else None},
                )
                if healthy:
                    time.sleep(self.spec.check_interval_seconds)
                    continue
                now = time.monotonic()
                while self.restarts and now - self.restarts[0] > self.spec.restart_window_seconds:
                    self.restarts.popleft()
                self.store.append_event(self.component, "SERVICE_UNHEALTHY", {"return_code": self.process.poll() if self.process else None}, severity="CRITICAL")
                self._stop_process()
                if len(self.restarts) >= self.spec.max_restarts:
                    reason = f"restart budget exhausted ({self.spec.max_restarts}/{self.spec.restart_window_seconds:.0f}s)"
                    self.alerts.dispatch(Alert("RESTART_BUDGET_EXHAUSTED", "CRITICAL", reason, {"service": self.spec.name}, self.component))
                    self.store.heartbeat(self.component, str(os.getpid()), status="FAILED", details={"reason": reason})
                    return 2
                delay = min(self.spec.base_backoff_seconds * (2 ** len(self.restarts)), 60.0)
                self.restarts.append(now)
                time.sleep(delay)
                self._start()
            return 0
        finally:
            self._stop_process()
            self.store.heartbeat(self.component, str(os.getpid()), status="STOPPED")
            self.store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="JSON file containing one service object")
    parser.add_argument("--operations-db", required=True)
    args = parser.parse_args()
    with Path(args.config).expanduser().open() as handle:
        spec = ServiceSpec.from_dict(json.load(handle))
    raise SystemExit(ServiceWatchdog(spec, args.operations_db).run())


if __name__ == "__main__":
    main()
