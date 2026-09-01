"""Durable operational state, control commands, heartbeats, and audit events."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Iterable
import uuid


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True)
class AuditEvent:
    sequence: int
    event_id: str
    occurred_at: str
    component: str
    event_type: str
    severity: str
    correlation_id: str
    payload: dict
    previous_hash: str
    event_hash: str


@dataclass(frozen=True)
class ControlCommand:
    command_id: str
    target: str
    action: str
    reason: str
    requested_at: str
    status: str
    correlation_id: str
    payload: dict
    claimed_at: str | None = None
    claimed_by: str = ""
    completed_at: str | None = None
    result: dict | None = None


class OperationsStore:
    """SQLite control plane shared by the strategy and external supervisors.

    Audit rows are protected by database triggers against update/delete and
    chained with SHA-256. Commands use transactional leasing, allowing a
    restarted strategy to recover pending safety actions exactly once.
    """

    def __init__(self, path: str) -> None:
        self.path = str(Path(path).expanduser().resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._initialize()

    def _connection(self) -> sqlite3.Connection:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = sqlite3.connect(self.path, timeout=15.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=15000")
            self._local.connection = connection
        return connection

    def _initialize(self) -> None:
        connection = self._connection()
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS audit_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                occurred_at TEXT NOT NULL,
                component TEXT NOT NULL,
                event_type TEXT NOT NULL,
                severity TEXT NOT NULL,
                correlation_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                previous_hash TEXT NOT NULL,
                event_hash TEXT NOT NULL UNIQUE
            );
            CREATE INDEX IF NOT EXISTS idx_audit_time
                ON audit_events(occurred_at, sequence);
            CREATE INDEX IF NOT EXISTS idx_audit_type
                ON audit_events(event_type, severity);
            CREATE TRIGGER IF NOT EXISTS audit_events_no_update
            BEFORE UPDATE ON audit_events BEGIN
                SELECT RAISE(ABORT, 'audit events are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
            BEFORE DELETE ON audit_events BEGIN
                SELECT RAISE(ABORT, 'audit events are immutable');
            END;

            CREATE TABLE IF NOT EXISTS service_heartbeats (
                component TEXT PRIMARY KEY,
                instance_id TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                status TEXT NOT NULL,
                details_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS control_commands (
                command_id TEXT PRIMARY KEY,
                target TEXT NOT NULL,
                action TEXT NOT NULL,
                reason TEXT NOT NULL,
                requested_at TEXT NOT NULL,
                status TEXT NOT NULL,
                correlation_id TEXT NOT NULL,
                dedupe_key TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                claimed_at TEXT,
                claimed_by TEXT NOT NULL DEFAULT '',
                completed_at TEXT,
                result_json TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_control_pending
                ON control_commands(target, status, requested_at);

            CREATE TABLE IF NOT EXISTS supervisor_state (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS paper_observations (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                observed_at TEXT NOT NULL,
                campaign_id TEXT NOT NULL,
                job_id TEXT NOT NULL,
                healthy INTEGER NOT NULL,
                equity REAL,
                drawdown_pct REAL,
                gross_leverage REAL,
                data_age_seconds REAL,
                order_count INTEGER NOT NULL,
                fill_count INTEGER NOT NULL,
                rejection_count INTEGER NOT NULL,
                reconciliation_state TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_paper_campaign_time
                ON paper_observations(campaign_id, observed_at);
            """
        )
        connection.commit()

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> AuditEvent:
        return AuditEvent(
            sequence=int(row["sequence"]),
            event_id=str(row["event_id"]),
            occurred_at=str(row["occurred_at"]),
            component=str(row["component"]),
            event_type=str(row["event_type"]),
            severity=str(row["severity"]),
            correlation_id=str(row["correlation_id"]),
            payload=json.loads(row["payload_json"]),
            previous_hash=str(row["previous_hash"]),
            event_hash=str(row["event_hash"]),
        )

    @staticmethod
    def _command_from_row(row: sqlite3.Row) -> ControlCommand:
        return ControlCommand(
            command_id=str(row["command_id"]),
            target=str(row["target"]),
            action=str(row["action"]),
            reason=str(row["reason"]),
            requested_at=str(row["requested_at"]),
            status=str(row["status"]),
            correlation_id=str(row["correlation_id"]),
            payload=json.loads(row["payload_json"]),
            claimed_at=row["claimed_at"],
            claimed_by=str(row["claimed_by"]),
            completed_at=row["completed_at"],
            result=(json.loads(row["result_json"]) if row["result_json"] else None),
        )

    def append_event(
        self,
        component: str,
        event_type: str,
        payload: dict | None = None,
        *,
        severity: str = "INFO",
        correlation_id: str = "",
        event_id: str | None = None,
        occurred_at: datetime | None = None,
    ) -> AuditEvent:
        identifier = event_id or uuid.uuid4().hex
        timestamp = _utc_iso(occurred_at)
        payload_json = _canonical_json(payload or {})
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM audit_events WHERE event_id = ?", (identifier,)
            ).fetchone()
            if existing is not None:
                connection.commit()
                return self._event_from_row(existing)
            prior = connection.execute(
                "SELECT event_hash FROM audit_events ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            previous_hash = str(prior[0]) if prior is not None else "0" * 64
            digest_input = "\x1f".join(
                (
                    previous_hash,
                    identifier,
                    timestamp,
                    str(component),
                    str(event_type),
                    str(severity).upper(),
                    str(correlation_id),
                    payload_json,
                )
            )
            event_hash = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()
            cursor = connection.execute(
                """
                INSERT INTO audit_events
                    (event_id, occurred_at, component, event_type, severity,
                     correlation_id, payload_json, previous_hash, event_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    timestamp,
                    str(component),
                    str(event_type),
                    str(severity).upper(),
                    str(correlation_id),
                    payload_json,
                    previous_hash,
                    event_hash,
                ),
            )
            row = connection.execute(
                "SELECT * FROM audit_events WHERE sequence = ?", (cursor.lastrowid,)
            ).fetchone()
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        return self._event_from_row(row)

    def audit_events(
        self,
        *,
        since: str | None = None,
        severities: Iterable[str] | None = None,
        limit: int = 1000,
    ) -> list[AuditEvent]:
        clauses: list[str] = []
        values: list[Any] = []
        if since:
            clauses.append("occurred_at >= ?")
            values.append(str(since))
        normalized = tuple(str(item).upper() for item in (severities or ()))
        if normalized:
            clauses.append(f"severity IN ({','.join('?' for _ in normalized)})")
            values.extend(normalized)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(max(1, int(limit)))
        rows = self._connection().execute(
            f"SELECT * FROM audit_events {where} ORDER BY sequence LIMIT ?",
            values,
        )
        return [self._event_from_row(row) for row in rows]

    def verify_audit_chain(self) -> tuple[bool, str]:
        previous_hash = "0" * 64
        rows = self._connection().execute(
            "SELECT * FROM audit_events ORDER BY sequence"
        )
        for row in rows:
            if row["previous_hash"] != previous_hash:
                return False, f"audit sequence {row['sequence']} has a broken predecessor"
            digest_input = "\x1f".join(
                (
                    previous_hash,
                    str(row["event_id"]),
                    str(row["occurred_at"]),
                    str(row["component"]),
                    str(row["event_type"]),
                    str(row["severity"]),
                    str(row["correlation_id"]),
                    str(row["payload_json"]),
                )
            )
            expected = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()
            if row["event_hash"] != expected:
                return False, f"audit sequence {row['sequence']} failed hash verification"
            previous_hash = expected
        return True, previous_hash

    def heartbeat(
        self,
        component: str,
        instance_id: str,
        *,
        status: str = "RUNNING",
        details: dict | None = None,
        observed_at: datetime | None = None,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO service_heartbeats
                    (component, instance_id, observed_at, status, details_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(component) DO UPDATE SET
                    instance_id = excluded.instance_id,
                    observed_at = excluded.observed_at,
                    status = excluded.status,
                    details_json = excluded.details_json
                """,
                (
                    str(component),
                    str(instance_id),
                    _utc_iso(observed_at),
                    str(status).upper(),
                    _canonical_json(details or {}),
                ),
            )

    def get_heartbeat(self, component: str) -> dict | None:
        row = self._connection().execute(
            "SELECT * FROM service_heartbeats WHERE component = ?", (str(component),)
        ).fetchone()
        if row is None:
            return None
        return {
            "component": row["component"],
            "instance_id": row["instance_id"],
            "observed_at": row["observed_at"],
            "status": row["status"],
            "details": json.loads(row["details_json"]),
        }

    def request_command(
        self,
        target: str,
        action: str,
        reason: str,
        *,
        payload: dict | None = None,
        correlation_id: str = "",
        dedupe_key: str = "",
    ) -> ControlCommand:
        connection = self._connection()
        normalized_action = str(action).upper()
        if dedupe_key:
            existing = connection.execute(
                """
                SELECT * FROM control_commands
                WHERE target = ? AND action = ? AND dedupe_key = ?
                  AND status IN ('PENDING', 'CLAIMED', 'ACKNOWLEDGED')
                ORDER BY requested_at DESC LIMIT 1
                """,
                (str(target), normalized_action, str(dedupe_key)),
            ).fetchone()
            if existing is not None:
                return self._command_from_row(existing)
        command_id = uuid.uuid4().hex
        requested_at = _utc_iso()
        with connection:
            connection.execute(
                """
                INSERT INTO control_commands
                    (command_id, target, action, reason, requested_at, status,
                     correlation_id, dedupe_key, payload_json)
                VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?, ?)
                """,
                (
                    command_id,
                    str(target),
                    normalized_action,
                    str(reason),
                    requested_at,
                    str(correlation_id),
                    str(dedupe_key),
                    _canonical_json(payload or {}),
                ),
            )
        self.append_event(
            "operations",
            "CONTROL_COMMAND_REQUESTED",
            {
                "command_id": command_id,
                "target": target,
                "action": normalized_action,
                "reason": reason,
                "payload": payload or {},
            },
            severity="CRITICAL" if normalized_action in {"FLATTEN", "KILL"} else "WARNING",
            correlation_id=correlation_id or command_id,
            event_id=f"command-requested:{command_id}",
        )
        return self.get_command(command_id)

    def get_command(self, command_id: str) -> ControlCommand | None:
        row = self._connection().execute(
            "SELECT * FROM control_commands WHERE command_id = ?", (str(command_id),)
        ).fetchone()
        return self._command_from_row(row) if row is not None else None

    def claim_commands(
        self,
        target: str,
        claimant: str,
        *,
        limit: int = 20,
        lease_seconds: float = 60.0,
    ) -> list[ControlCommand]:
        now = datetime.now(timezone.utc)
        expired = _utc_iso(
            datetime.fromtimestamp(now.timestamp() - max(float(lease_seconds), 1.0), timezone.utc)
        )
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = list(
                connection.execute(
                    """
                    SELECT * FROM control_commands
                    WHERE target IN (?, '*')
                      AND (
                        status = 'PENDING'
                        OR (status = 'CLAIMED' AND claimed_at <= ?)
                      )
                    ORDER BY requested_at
                    LIMIT ?
                    """,
                    (str(target), expired, max(1, int(limit))),
                )
            )
            claimed_at = _utc_iso(now)
            for row in rows:
                connection.execute(
                    """
                    UPDATE control_commands
                    SET status = 'CLAIMED', claimed_at = ?, claimed_by = ?
                    WHERE command_id = ?
                    """,
                    (claimed_at, str(claimant), row["command_id"]),
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        return [self.get_command(str(row["command_id"])) for row in rows]

    def complete_command(
        self,
        command_id: str,
        claimant: str,
        *,
        success: bool,
        result: dict | None = None,
    ) -> ControlCommand:
        status = "COMPLETED" if success else "FAILED"
        connection = self._connection()
        with connection:
            updated = connection.execute(
                """
                UPDATE control_commands
                SET status = ?, completed_at = ?, result_json = ?
                WHERE command_id = ? AND status IN ('CLAIMED', 'ACKNOWLEDGED')
                  AND claimed_by = ?
                """,
                (
                    status,
                    _utc_iso(),
                    _canonical_json(result or {}),
                    str(command_id),
                    str(claimant),
                ),
            ).rowcount
        if not updated:
            raise RuntimeError("control command is not leased by this claimant")
        command = self.get_command(command_id)
        self.append_event(
            "operations",
            "CONTROL_COMMAND_COMPLETED" if success else "CONTROL_COMMAND_FAILED",
            {"command_id": command_id, "result": result or {}},
            severity="INFO" if success else "CRITICAL",
            correlation_id=command.correlation_id or command_id,
            event_id=f"command-terminal:{command_id}",
        )
        return command

    def acknowledge_command(
        self,
        command_id: str,
        claimant: str,
        *,
        result: dict | None = None,
    ) -> ControlCommand:
        with self._connection() as connection:
            updated = connection.execute(
                """
                UPDATE control_commands
                SET status = 'ACKNOWLEDGED', result_json = ?
                WHERE command_id = ? AND status = 'CLAIMED' AND claimed_by = ?
                """,
                (_canonical_json(result or {}), str(command_id), str(claimant)),
            ).rowcount
        if not updated:
            raise RuntimeError("control command is not leased by this claimant")
        command = self.get_command(command_id)
        self.append_event(
            "operations",
            "CONTROL_COMMAND_ACKNOWLEDGED",
            {"command_id": command_id, "result": result or {}},
            severity="WARNING",
            correlation_id=command.correlation_id or command_id,
            event_id=f"command-acknowledged:{command_id}",
        )
        return command

    def acknowledged_commands(self, target: str, claimant: str) -> list[ControlCommand]:
        rows = self._connection().execute(
            """
            SELECT * FROM control_commands
            WHERE target IN (?, '*') AND status = 'ACKNOWLEDGED' AND claimed_by = ?
            ORDER BY requested_at
            """,
            (str(target), str(claimant)),
        )
        return [self._command_from_row(row) for row in rows]

    def set_state(self, key: str, value: Any) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO supervisor_state(key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (str(key), _canonical_json(value), _utc_iso()),
            )

    def get_state(self, key: str, default: Any = None) -> Any:
        row = self._connection().execute(
            "SELECT value_json FROM supervisor_state WHERE key = ?", (str(key),)
        ).fetchone()
        return json.loads(row[0]) if row is not None else default

    def record_paper_observation(
        self,
        campaign_id: str,
        job_id: str,
        payload: dict,
        *,
        healthy: bool,
        observed_at: datetime | None = None,
    ) -> None:
        risk = payload.get("risk") or {}
        orders = risk.get("orders") or []
        fills = risk.get("fills") or []
        alerts = risk.get("operator_alerts") or []
        session = risk.get("session") or {}
        compact_risk = {
            key: value
            for key, value in risk.items()
            if key not in {"orders", "fills", "operator_alerts"}
        }
        if isinstance(compact_risk.get("data_quality"), dict):
            compact_risk["data_quality"] = {
                **compact_risk["data_quality"],
                "issues": list(compact_risk["data_quality"].get("issues") or [])[-10:],
            }
        compact_payload = {
            "schema_version": payload.get("schema_version"),
            "status": payload.get("status"),
            "mode": payload.get("mode"),
            "asset_class": payload.get("asset_class"),
            "bar_type": payload.get("bar_type"),
            "as_of": payload.get("as_of"),
            "tickers": payload.get("tickers"),
            "risk": compact_risk,
            "model": payload.get("model") or {},
        }
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO paper_observations
                    (observed_at, campaign_id, job_id, healthy, equity,
                     drawdown_pct, gross_leverage, data_age_seconds, order_count,
                     fill_count, rejection_count, reconciliation_state, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utc_iso(observed_at),
                    str(campaign_id),
                    str(job_id),
                    int(bool(healthy)),
                    risk.get("equity"),
                    risk.get("drawdown_pct"),
                    risk.get("gross_leverage"),
                    session.get("data_age_seconds"),
                    len(orders),
                    len(fills),
                    sum(1 for item in alerts if item.get("code") == "ORDER_REJECTED"),
                    str(risk.get("reconciliation_state") or "UNKNOWN"),
                    _canonical_json(compact_payload),
                ),
            )

    def paper_observations(self, campaign_id: str) -> list[dict]:
        rows = self._connection().execute(
            """
            SELECT * FROM paper_observations
            WHERE campaign_id = ? ORDER BY observed_at, sequence
            """,
            (str(campaign_id),),
        )
        return [
            {**dict(row), "payload": json.loads(row["payload_json"])}
            for row in rows
        ]

    def backup_to(self, path: str) -> str:
        target_path = str(Path(path).expanduser().resolve())
        Path(target_path).parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(target_path)
        try:
            self._connection().backup(target)
        finally:
            target.close()
        return target_path

    def integrity_check(self) -> tuple[bool, str]:
        result = str(self._connection().execute("PRAGMA integrity_check").fetchone()[0])
        chain_ok, chain_detail = self.verify_audit_chain()
        if result.lower() != "ok":
            return False, result
        return chain_ok, chain_detail if not chain_ok else "ok"

    def close(self) -> None:
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            connection.close()
            self._local.connection = None
