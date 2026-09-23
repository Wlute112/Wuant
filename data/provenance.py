"""Locally observed, content-addressed dataset versions (not vendor PIT certification).

CSV declarations remain unverified. Observation time never substitutes for a
historical retrieval or universe-membership date. No filtering of old symbols.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile

import pandas as pd

DECLARATIONS = ("source", "retrieved_at", "session", "price_basis", "volume_basis",
                "con_id", "symbol_alias", "membership_start", "membership_end",
                "membership_source")
LIMITATIONS = [
    "Local observation history is not proof that these values were available at each historical bar.",
    "Universe dates and symbol/conId lineage are supplier declarations, not independently verified. Bar coverage is not membership history.",
    "Survivorship and delisted-instrument coverage are unknown. Retained versions preserve supplied symbols, including removed or renamed symbols; absent history cannot be reconstructed.",
    "Research uses the selected fixed universe; declared membership dates are review evidence and do not implement a dynamic historical universe.",
]


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _store(path: Path) -> Path:
    return path.parent if path.parent.name.endswith(".provenance") else path.with_name(path.name + ".provenance")


def _json(value: dict | list) -> bytes:
    return json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":")).encode()


def _atomic(path: Path, content: bytes) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


@contextmanager
def dataset_lock(path: str | Path):
    store = _store(Path(path).resolve())
    store.mkdir(parents=True, exist_ok=True)
    with (store / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _frame(content: bytes) -> pd.DataFrame:
    try:
        return pd.read_csv(io.BytesIO(content), dtype=str).fillna("")
    except (ValueError, pd.errors.ParserError):
        return pd.DataFrame()


def _universe(frame: pd.DataFrame) -> list[dict]:
    if "ticker" not in frame:
        return []
    result = []
    for ticker, rows in frame.groupby("ticker", sort=True):
        timestamps = pd.to_datetime(rows.get("timestamp", pd.Series(dtype=str)), format="mixed", utc=True, errors="coerce").dropna()
        declarations = rows.reindex(columns=DECLARATIONS, fill_value="").drop_duplicates()
        result.append({"ticker": ticker, "bars": len(rows),
                       "observed_start": timestamps.min().isoformat() if len(timestamps) else None,
                       "observed_end": timestamps.max().isoformat() if len(timestamps) else None,
                       "declarations": [{key: value or None for key, value in row.items()}
                                        for row in declarations.to_dict("records")]})
    return result


def _changes(before: bytes | None, after: bytes) -> dict | None:
    if before is None:
        return None
    old, new = _frame(before), _frame(after)
    old_symbols = set(old.get("ticker", []))
    new_symbols = set(new.get("ticker", []))
    result = {"added_symbols": sorted(new_symbols - old_symbols),
              "removed_symbols": sorted(old_symbols - new_symbols),
              "added_bars": None, "removed_bars": None, "revised_bars": None}
    keys = ["ticker", "timestamp"]
    if any(not set(keys).issubset(frame.columns) for frame in (old, new)):
        return result
    for frame in (old, new):
        frame["timestamp"] = pd.to_datetime(frame.timestamp, format="mixed", utc=True, errors="coerce")
        if frame.timestamp.isna().any() or frame.duplicated(keys).any():
            return result
    for frame in (old, new):
        for column in ("open", "high", "low", "close", "volume", "requested_bar_hours"):
            if column in frame:
                frame[column] = pd.to_numeric(frame[column], errors="coerce")
    old, new = old.set_index(keys), new.set_index(keys)
    shared = old.index.intersection(new.index)
    # Retrieval time can change without revising market data or its semantics.
    columns = sorted((set(old.columns) | set(new.columns)) - {"retrieved_at"})
    left = old.reindex(index=shared, columns=columns).fillna("")
    right = new.reindex(index=shared, columns=columns).fillna("")
    result.update(added_bars=len(new.index.difference(old.index)),
                  removed_bars=len(old.index.difference(new.index)),
                  revised_bars=int(left.ne(right).any(axis=1).sum()))
    return result


def _load(store: Path, digest: str) -> dict:
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("Invalid dataset snapshot identity")
    envelope = json.loads((store / f"{digest}.json").read_text())
    record = envelope["record"]
    if _digest(_json(record)) != envelope["manifest_sha256"] or record["sha256"] != digest:
        raise ValueError("Dataset provenance manifest failed its integrity check")
    if _digest((store / f"{digest}.csv").read_bytes()) != digest:
        raise ValueError("Dataset snapshot failed its integrity check")
    return {**record, "manifest_sha256": envelope["manifest_sha256"]}


def _journal(store: Path) -> list[dict]:
    path = store / "observations.json"
    if not path.exists():
        if any(store.glob("*.csv")):
            raise ValueError("Dataset observation journal is missing; restore the archive evidence")
        return []
    envelope = json.loads(path.read_text())
    events = envelope["events"]
    if _digest(_json(events)) != envelope["sha256"]:
        raise ValueError("Dataset observation journal failed its integrity check")
    return events


def describe(path: str | Path, content: bytes | None = None) -> dict:
    path = Path(path).resolve()
    content = path.read_bytes() if content is None else content
    digest = _digest(content)
    store = _store(path)
    try:
        pinned = path.parent == store
        if pinned and path.stem != digest:
            raise ValueError("Dataset snapshot bytes no longer match its filename")
        events = _journal(store) if not pinned else []
        parent = events[-1]["sha256"] if events else None
        before = None
        if parent:
            _load(store, parent)
            before = (store / f"{parent}.csv").read_bytes()
        if (store / f"{digest}.json").exists():
            record = _load(store, digest)
            checked = {digest}
            ancestor = record.get("parent_sha256")
            while ancestor:
                if ancestor in checked:
                    raise ValueError("Dataset provenance ancestry contains a cycle")
                checked.add(ancestor)
                ancestor = _load(store, ancestor).get("parent_sha256")
            for event in events:
                if event["sha256"] not in checked:
                    _load(store, event["sha256"])
                    checked.add(event["sha256"])
            # Snapshot evidence is immutable. Source observation history is a
            # separate journal: replaying a snapshot cannot move its head.
            result = {**record, "status": "archived", "snapshot_csv": str(store / f"{digest}.csv")}
            if not pinned:
                current = events[-1] if parent == digest else None
                result["changes"] = current["changes"] if current else _changes(before, content)
                result["publication_status"] = "recorded" if current else "unrecorded_revisit"
                result["history"] = list(reversed(events[:-1] if current else events))[:50]
                result["history_truncated"] = len(events) - bool(current) > 50
            return result
        if pinned:
            raise ValueError("Retained dataset is missing its provenance manifest")
        return {"schema_version": 1, "status": "unarchived", "sha256": digest,
                "observed_at": None, "parent_sha256": parent,
                "changes": _changes(before, content), "universe": _universe(_frame(content)),
                "limitations": LIMITATIONS}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"status": "integrity_error", "sha256": digest,
                "error": str(exc), "limitations": LIMITATIONS}


def archive(path: str | Path, *, content: bytes | None = None, operation="research", locked=False) -> dict:
    """Archive exact bytes before use/publication; retries reuse verified versions."""
    path = Path(path).resolve()
    if not locked:
        with dataset_lock(path):
            return archive(path, content=content, operation=operation, locked=True)
    content = path.read_bytes() if content is None else content
    record = describe(path, content)
    if record["status"] == "integrity_error":
        raise ValueError(record["error"])
    store = _store(path)
    digest = record["sha256"]
    if path.parent == store:
        if record["status"] != "archived":
            raise ValueError("Retained dataset is missing its provenance manifest")
        return record
    events = _journal(store)
    now = datetime.now(timezone.utc).isoformat()
    if record["status"] != "archived":
        record.pop("status")
        record.update(observed_at=now, source_path=str(path), operation=operation)
        _atomic(store / f"{digest}.csv", content)
        _atomic(store / f"{digest}.json", _json({"record": record, "manifest_sha256": _digest(_json(record))}))
    if not events or events[-1]["sha256"] != digest:
        events.append({"sha256": digest, "observed_at": now, "operation": operation,
                       "changes": record["changes"]})
        _atomic(store / "observations.json", _json({"events": events, "sha256": _digest(_json(events))}))
    return describe(path, content)


def pin_csv(path: str | Path) -> str:
    """Research always reads one retained version, even if its source is replaced."""
    return archive(path)["snapshot_csv"]


def verify_contract_dataset(contract: dict) -> None:
    """Validate locked evidence before robustness/holdout; legacy stays hash-only."""
    path = Path(contract["source_csv"])
    if _digest(path.read_bytes()) != contract["source_csv_sha256"]:
        raise ValueError("source CSV changed after optimization")
    expected = contract.get("dataset_manifest_sha256")
    if expected is not None:
        report = describe(path)
        if report["status"] != "archived" or report.get("manifest_sha256") != expected:
            raise ValueError("Locked dataset provenance is missing, changed or corrupt")
