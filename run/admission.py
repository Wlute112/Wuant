"""One local-user research budget shared by dashboard supervisors and CLI pools.

SQLite serializes admission across API instances/checkouts. Process identities,
not lease timeouts, reclaim crashed owners: a stalled research process must
never lose its reservation while it can still consume resources.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import threading
import time
import uuid

from quant.run.compute import GIB, host_resources

RESEARCH_KINDS = frozenset({"backtest", "optimize", "campaign_seeds",
                            "campaign_compare", "campaign_robustness", "campaign_promote"})
PARALLEL_KINDS = frozenset({"optimize", "campaign_seeds", "campaign_robustness"})


def process_snapshot() -> dict:
    output = subprocess.check_output(
        ["ps", "-axo", "pid=,ppid=,pgid=,rss=,stat=,lstart="], text=True, timeout=3,
        env={**os.environ, "LC_ALL": "C", "TZ": "UTC"},
    )
    result = {}
    for line in output.splitlines():
        parts = line.split(maxsplit=5)
        if len(parts) == 6 and not parts[4].startswith("Z"):
            pid, parent, group, rss = map(int, parts[:4])
            result[pid] = {"pid": pid, "parent": parent, "group": group,
                           "rss_gb": rss * 1024 / GIB, "birth": parts[5]}
    return result


def process_tree(snapshot: dict, root: int, group: int | None = None) -> list[dict]:
    members = {root} | {pid for pid, row in snapshot.items() if group and row["group"] == group}
    while True:
        expanded = members | {pid for pid, row in snapshot.items() if row["parent"] in members}
        if expanded == members:
            return [snapshot[pid] for pid in members if pid in snapshot]
        members = expanded


class AdmissionStore:
    def __init__(self, path: Path | str | None = None, *, host: dict | None = None):
        self.path = Path(path or os.environ.get(
            "QUANT_ADMISSION_DB", str(Path.home() / ".quant" / "research-admission.sqlite3")))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.host = host or host_resources()
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, value TEXT)")
            db.execute("""CREATE TABLE IF NOT EXISTS requests (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT UNIQUE,
                value TEXT NOT NULL)""")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def ceiling(self) -> dict:
        cores = self.host["compute_cores"]
        total = self.host["memory_gb"]
        return {"cpu_slots": max(0, cores - (2 if cores >= 8 else 1)),
                "memory_gb": max(0, total - max(8, total * 0.25))}

    def _policy(self, db) -> dict:
        saved = db.execute("SELECT value FROM settings WHERE id=1").fetchone()
        ceiling = self.ceiling()
        if not saved:
            return ceiling
        policy = json.loads(saved[0])
        return {key: min(policy[key], ceiling[key]) for key in ceiling}

    def configure(self, cpu_slots: int, memory_gb: float, operator: str) -> dict:
        ceiling = self.ceiling()
        if (isinstance(cpu_slots, bool) or not isinstance(cpu_slots, int)
                or not 1 <= cpu_slots <= ceiling["cpu_slots"]
                or not math.isfinite(memory_gb) or not 0 < memory_gb <= ceiling["memory_gb"]):
            raise ValueError("Research limits must be positive and within the host ceiling after service reserves")
        with self.connect() as db:
            self._clean(db)
            rows = self._rows(db)
            running = [row for row in rows if row["state"] == "running"]
            if (sum(row["cpu_slots"] for row in running) > cpu_slots
                    or sum(row["memory_gb"] for row in running) > memory_gb):
                raise ValueError("New limits cannot be below active reservations; cancel or finish those jobs first")
            policy = {"cpu_slots": cpu_slots, "memory_gb": memory_gb}
            # Existing queued requests retain their explicit requirement. Do
            # not strand them forever behind a limit they can no longer fit.
            if any(row["cpu_slots"] > cpu_slots or row["memory_gb"] > memory_gb for row in rows):
                raise ValueError("New limits cannot strand queued requests; cancel those jobs first")
            previous = self._policy(db)
            db.execute("CREATE TABLE IF NOT EXISTS policy_audit (at REAL, operator TEXT, previous TEXT, value TEXT)")
            db.execute("INSERT INTO policy_audit VALUES (?, ?, ?, ?)",
                       (time.time(), operator, json.dumps(previous), json.dumps(policy)))
            db.execute("INSERT OR REPLACE INTO settings VALUES (1, ?)", (json.dumps(policy),))
        return self.status()

    @staticmethod
    def _rows(db):
        return [json.loads(row[0]) for row in db.execute("SELECT value FROM requests ORDER BY sequence")]

    def _clean(self, db):
        rows = self._rows(db)
        if not rows:
            return
        snapshot = process_snapshot()  # Failure blocks admission, never invents free capacity.
        for row in rows:
            identities = row.get("processes", []) + [{"pid": row["pid"], "birth": row["birth"]}]
            alive = any(snapshot.get(item["pid"], {}).get("birth") == item["birth"] for item in identities)
            group_alive = row.get("child_group") and any(
                item["group"] == row["child_group"] for item in snapshot.values())
            if not alive and not group_alive:
                db.execute("DELETE FROM requests WHERE token=?", (row["token"],))

    def status(self) -> dict:
        with self.connect() as db:
            self._clean(db)
            policy, rows = self._policy(db), self._rows(db)
            running = [row for row in rows if row["state"] == "running"]
            position = 0
            for row in rows:
                if row["state"] == "queued":
                    position += 1
                    row["queue_position"] = position
            return {"host": self.host, "ceiling": self.ceiling(), "limits": policy,
                    "used": {key: sum(row[key] for row in running) for key in policy},
                    "requests": rows, "as_of": time.time(),
                    "measurement": "Sampled aggregate process-tree RSS; not an OS hard memory limit"}

    def enqueue(self, cpu_slots: int, memory_gb: float, label: str) -> dict:
        if (not isinstance(cpu_slots, int) or cpu_slots < 1
                or not math.isfinite(memory_gb) or memory_gb <= 0):
            raise ValueError("Invalid research reservation")
        owner = process_snapshot()[os.getpid()]
        row = {"token": uuid.uuid4().hex, "pid": os.getpid(), "birth": owner["birth"],
               "label": label, "state": "queued", "cpu_slots": cpu_slots,
               "memory_gb": memory_gb, "queued_at": time.time(), "peak_rss_gb": None}
        with self.connect() as db:
            limits = self._policy(db)
            if cpu_slots > limits["cpu_slots"] or memory_gb > limits["memory_gb"]:
                raise ValueError("Research request exceeds the host limits; reduce workers or the worker memory estimate")
            db.execute("INSERT INTO requests (token, value) VALUES (?, ?)", (row["token"], json.dumps(row)))
        return row

    def claim(self, token: str) -> dict:
        with self.connect() as db:
            self._clean(db)
            rows, limits = self._rows(db), self._policy(db)
            row = next(item for item in rows if item["token"] == token)
            queued = [item for item in rows if item["state"] == "queued"]
            running = [item for item in rows if item["state"] == "running"]
            if row["state"] == "queued" and queued[0]["token"] == token and all(
                sum(item[key] for item in running) + row[key] <= limits[key] for key in limits
            ):
                row.update(state="running", admitted_at=time.time())
                self._write(db, row)
            row["queue_position"] = next((i + 1 for i, item in enumerate(queued) if item["token"] == token), None) if row["state"] == "queued" else None
            return row

    @staticmethod
    def _write(db, row):
        db.execute("UPDATE requests SET value=? WHERE token=?", (json.dumps(row), row["token"]))

    def sample(self, token: str, root: int, group: int | None = None) -> dict:
        members = process_tree(process_snapshot(), root, group)
        rss = sum(item["rss_gb"] for item in members)
        with self.connect() as db:
            row = json.loads(db.execute("SELECT value FROM requests WHERE token=?", (token,)).fetchone()[0])
            row.update(child_group=group, processes=[{"pid": item["pid"], "birth": item["birth"]} for item in members],
                       rss_gb=rss, peak_rss_gb=max(row.get("peak_rss_gb") or 0, rss), sampled_at=time.time())
            self._write(db, row)
        return row

    def release(self, token: str):
        with self.connect() as db:
            value = db.execute("SELECT value FROM requests WHERE token=?", (token,)).fetchone()
            row = json.loads(value[0]) if value else {}
            if row.get("child_group") and any(item["group"] == row["child_group"]
                                             for item in process_snapshot().values()):
                # SIGKILL delivery is asynchronous. Retain the reservation
                # until all independently grouped descendants have exited.
                return
            db.execute("DELETE FROM requests WHERE token=?", (token,))

    def inherited(self) -> dict | None:
        token = os.environ.get("QUANT_RESEARCH_LEASE")
        if not token:
            return None
        with self.connect() as db:
            self._clean(db)
            row = next((row for row in self._rows(db) if row["token"] == token and row["state"] == "running"), None)
        if row and os.getpid() in {item["pid"] for item in process_tree(process_snapshot(), row["pid"])}:
            return row
        raise RuntimeError("Inherited research reservation is missing or does not belong to this process tree")


@contextmanager
def research_slot(workers=1, worker_memory_gb=4, *, label="CLI research"):
    """CLI admission, also respecting a reservation inherited from the dashboard."""
    store = AdmissionStore()
    inherited = store.inherited()
    if inherited:
        if workers > inherited["cpu_slots"] or workers * worker_memory_gb > inherited["memory_gb"]:
            raise ValueError("Research pool exceeds its inherited host reservation")
        yield inherited
        return
    row = store.enqueue(workers, workers * worker_memory_gb, label)
    token = row["token"]
    stop = threading.Event()
    old_handler = signal.getsignal(signal.SIGTERM)
    def interrupt(*_):
        raise KeyboardInterrupt("research cancelled or resource monitoring failed")
    signal.signal(signal.SIGTERM, interrupt)
    try:
        announced = False
        while (row := store.claim(token))["state"] == "queued":
            if not announced:
                print("Research queued for shared host CPU/RAM capacity", flush=True)
                announced = True
            time.sleep(0.5)
        os.environ["QUANT_RESEARCH_LEASE"] = token

        def monitor():
            while not stop.wait(1):
                try:
                    sample = store.sample(token, os.getpid())
                    if not stop.is_set() and sample["rss_gb"] > row["memory_gb"]:
                        os.kill(os.getpid(), signal.SIGTERM)
                        return
                except Exception:
                    if not stop.is_set():
                        os.kill(os.getpid(), signal.SIGTERM)
                    return

        watcher = threading.Thread(target=monitor, daemon=True)
        watcher.start()
        try:
            yield row
        finally:
            stop.set()
            watcher.join(timeout=15)
    finally:
        os.environ.pop("QUANT_RESEARCH_LEASE", None)
        signal.signal(signal.SIGTERM, old_handler)
        store.release(token)


@contextmanager
def research_entry(label: str):
    """Admit a CLI before CSV preparation or campaign/holdout side effects."""
    import argparse
    from quant.run.compute import add_compute_arguments, resolve_compute_plan, THREAD_ENV
    from threadpoolctl import threadpool_limits
    parser = argparse.ArgumentParser(add_help=False)
    add_compute_arguments(parser)
    args, _ = parser.parse_known_args()
    plan = resolve_compute_plan(args.workers, args.memory_budget_gb,
                                args.worker_memory_gb, tasks=20)
    previous = {key: os.environ.get(key) for key in THREAD_ENV}
    try:
        for key in THREAD_ENV:
            os.environ[key] = "1"
        with research_slot(plan.workers, plan.worker_memory_gb, label=label), threadpool_limits(limits=1):
            yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
