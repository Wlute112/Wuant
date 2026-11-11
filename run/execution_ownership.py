"""Exclusive broker execution for the supported single-host/local-user scope.

This is a kernel-held lock, not a time-expiring distributed lease. Paused
owners cannot be replaced. All accounts/profiles share the slot because the
legacy Nautilus cache namespaces are not yet account-isolated. Never unlink
the lock file or expose a force-unlock operation.
"""
from __future__ import annotations

from datetime import datetime, timezone
import fcntl
from functools import wraps
import hashlib
import inspect
import json
import os
from pathlib import Path
import stat
import uuid


OWNERSHIP_DIR = Path.home() / ".quant" / "execution-ownership"
SCOPE = "single-host-local-user-all-broker-runners"


class ExecutionOwnershipError(ValueError):
    """Broker execution cannot proceed without exclusive ownership."""


def _open_lock() -> tuple[Path, int]:
    OWNERSHIP_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = OWNERSHIP_DIR / "broker-execution.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    details = os.fstat(fd)
    if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid():
        os.close(fd)
        raise ExecutionOwnershipError("Execution ownership file must be a regular file owned by this local user")
    os.set_inheritable(fd, False)
    return path, fd


def _read(fd: int) -> dict:
    # Lock metadata is for review only. The kernel lock is authoritative.
    raw = os.pread(fd, 8192, 0)
    try:
        value = json.loads(raw) if raw else {}
        return value if isinstance(value, dict) else {}
    except (ValueError, UnicodeError):
        return {}


def ownership_status() -> dict:
    path, fd = _open_lock()
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            active = False
        except BlockingIOError:
            active = True
        record = _read(fd)
        return {"scope": SCOPE, "active": active,
                "status": "OWNED" if active else "AVAILABLE",
                "owner": record if active else None,
                "previous_owner": record if not active else None,
                "as_of": datetime.now(timezone.utc).isoformat(),
                "policy": "One broker runner across all local accounts, profiles and client IDs. Stop the owner and verify shutdown before restarting. No lease expiry or force unlock.",
                "limitations": "Local OS user only; not distributed fencing. Broker orders and positions still require fresh reconciliation after owner exit."}
    finally:
        os.close(fd)


class ExecutionOwnership:
    def __init__(self, *, account_id: str, mode: str, asset_class: str,
                 client_id: int, owner_id: str = ""):
        if not isinstance(account_id, str) or not account_id.strip() or any(ord(c) < 32 for c in account_id):
            raise ExecutionOwnershipError("A valid IBKR account ID is required for execution ownership")
        self.path, self.fd = _open_lock()
        self.pid = os.getpid()
        self.closed = False
        try:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ExecutionOwnershipError(
                    "Another broker runner owns local execution. Stop that session and wait for verified shutdown; changing account, profile or client ID does not create another execution slot."
                ) from exc
            self.record = {
                "owner_id": owner_id or f"cli:{uuid.uuid4().hex[:12]}",
                "generation": uuid.uuid4().hex,
                "pid": self.pid, "mode": mode, "asset_class": asset_class,
                "client_id": client_id,
                "account_fingerprint": hashlib.sha256(account_id.strip().upper().encode()).hexdigest(),
                "acquired_at": datetime.now(timezone.utc).isoformat(),
            }
            raw = json.dumps(self.record, separators=(",", ":")).encode()
            os.ftruncate(self.fd, 0)
            os.pwrite(self.fd, raw, 0)
            os.fsync(self.fd)
            self.identity = (os.fstat(self.fd).st_dev, os.fstat(self.fd).st_ino)
        except BaseException:
            os.close(self.fd)
            self.closed = True
            raise

    def assert_owned(self):
        try:
            if self.closed or os.getpid() != self.pid:
                raise ExecutionOwnershipError("Execution ownership is closed or belongs to another process")
            current = self.path.stat(follow_symlinks=False)
            if (current.st_dev, current.st_ino) != self.identity or not stat.S_ISREG(current.st_mode):
                raise ExecutionOwnershipError("Execution ownership file was replaced; execution is disabled")
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if _read(self.fd).get("generation") != self.record["generation"]:
                raise ExecutionOwnershipError("Execution ownership identity changed; execution is disabled")
        except (OSError, ValueError) as exc:
            raise ExecutionOwnershipError(f"Exclusive execution ownership cannot be verified: {exc}") from exc

    def snapshot(self) -> dict:
        try:
            self.assert_owned()
            return {"healthy": True, "status": "OWNED", "scope": SCOPE, **self.record}
        except ExecutionOwnershipError:
            return {"healthy": False, "status": "UNVERIFIED", "scope": SCOPE}

    def close(self):
        if self.closed:
            return
        # Closing a forked child's copy must not explicitly unlock the parent's
        # shared open-file description. No trading is permitted from that child.
        if os.getpid() == self.pid:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)
        self.closed = True


class OwnedTradingNode:
    """Keep ownership through run, stop, state saving and verified disposal."""
    def __init__(self, node, ownership: ExecutionOwnership):
        self._node = node
        self._ownership = ownership
        self._started = False
        self._disposed = False

    def __getattr__(self, name):
        return getattr(self._node, name)

    def run(self, *args, **kwargs):
        self._ownership.assert_owned()
        self._started = True
        return self._node.run(*args, **kwargs)

    async def run_async(self, *args, **kwargs):
        self._ownership.assert_owned()
        self._started = True
        return await self._node.run_async(*args, **kwargs)

    def dispose(self):
        if self._disposed:
            return
        self._node.dispose()
        if self._started:
            # Nautilus dispose logs some failures instead of raising. Do not
            # mistake a returned call for confirmed broker disconnection.
            kernel = self._node.kernel
            if (kernel.is_running() or not kernel.exec_engine.check_disconnected()
                    or not kernel.data_engine.check_disconnected()):
                raise ExecutionOwnershipError("Shutdown is unverified; execution ownership remains held until the owning process exits")
        self._ownership.close()
        self._disposed = True

    def __del__(self):
        # A built-but-never-started node has no broker connection. An abandoned
        # started node deliberately retains its FD until process exit.
        if not getattr(self, "_started", True):
            self._ownership.close()


def exclusive_execution_node(builder):
    """Acquire before adapter/cache construction for every public node build."""
    signature = inspect.signature(builder)

    @wraps(builder)
    def build(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        values = bound.arguments
        if values["is_live"]:
            from quant.run.readiness import assert_live_capital_enabled
            assert_live_capital_enabled()
        ownership = ExecutionOwnership(
            account_id=values["account_id"], mode="live" if values["is_live"] else "paper",
            asset_class=values["asset_class"], client_id=values["client_id"],
            owner_id=values["operations_component_id"],
        )
        try:
            node = builder(*args, **kwargs)
            node.quant_strategy.attach_execution_ownership(ownership)
            return OwnedTradingNode(node, ownership)
        except BaseException:
            ownership.close()
            raise

    return build
