"""Durable conId registry and reviewed corporate-action recovery journal.

Recovery requires a broker-confirmed flat account and fresh qualification.
It never rewrites broker fills, position quantities or cash balances.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from quant.data.instrument_identity import EquityIdentity, SYMBOL


class CorporateAction(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    event_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")
    kind: Literal["SPLIT", "CASH_DIVIDEND", "SYMBOL_CHANGE"]
    con_id: int = Field(strict=True, gt=0)
    effective_at: datetime
    source_reference: str = Field(min_length=5, max_length=500)
    split_ratio: Decimal | None = Field(default=None, gt=0, le=1000000)
    cash_per_share: Decimal | None = Field(default=None, ge=0, le=1000000)
    new_symbol: str | None = None
    new_con_id: int | None = Field(default=None, strict=True, gt=0)
    currency: Literal["USD"] = "USD"

    @field_validator("effective_at")
    @classmethod
    def timezone_required(cls, value):
        if value.tzinfo is None:
            raise ValueError("effective_at requires an explicit timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_fields(self):
        if self.kind == "SPLIT":
            if self.split_ratio is None or self.split_ratio == 1 or self.cash_per_share is not None or self.new_symbol or self.new_con_id:
                raise ValueError("SPLIT requires a new-shares/old-shares ratio other than 1 and no dividend/symbol fields")
        elif self.kind == "CASH_DIVIDEND":
            if self.cash_per_share is None or self.split_ratio is not None or self.new_symbol or self.new_con_id:
                raise ValueError("CASH_DIVIDEND requires cash_per_share and no split/symbol fields")
        elif not self.new_symbol or not SYMBOL.fullmatch(self.new_symbol) or self.split_ratio is not None or self.cash_per_share is not None:
            raise ValueError("SYMBOL_CHANGE requires a valid new_symbol and no split/dividend fields")
        return self


class CorporateActionRegistry:
    def __init__(self, store):
        self.store = store
        store._connection().executescript("""
            CREATE TABLE IF NOT EXISTS equity_identities (
                account TEXT NOT NULL, con_id INTEGER NOT NULL, identity_json TEXT NOT NULL,
                aliases_json TEXT NOT NULL, PRIMARY KEY(account, con_id));
            CREATE TABLE IF NOT EXISTS corporate_actions (
                account TEXT NOT NULL, event_id TEXT NOT NULL, payload_json TEXT NOT NULL,
                digest TEXT NOT NULL, operator TEXT NOT NULL, status TEXT NOT NULL,
                result_json TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(account, event_id));
            CREATE TRIGGER IF NOT EXISTS corporate_action_payload_immutable
                BEFORE UPDATE OF payload_json, digest, operator ON corporate_actions
                BEGIN SELECT RAISE(ABORT, 'corporate action evidence is immutable'); END;
        """)

    def identities(self, account: str) -> list[dict]:
        return [{**json.loads(row["identity_json"]), "aliases": json.loads(row["aliases_json"])}
                for row in self.store._connection().execute(
                    "SELECT * FROM equity_identities WHERE account = ? ORDER BY con_id", (account,))]

    def events(self, account: str) -> list[dict]:
        return [{**json.loads(row["payload_json"]), "status": row["status"],
                 "digest": row["digest"], "operator": row["operator"], "result": json.loads(row["result_json"])}
                for row in self.store._connection().execute(
                    "SELECT * FROM corporate_actions WHERE account = ? ORDER BY event_id", (account,))]

    def generation(self, account: str) -> str:
        applied = sorted((event["event_id"], event["digest"]) for event in self.events(account)
                         if event["status"] in {"REBUILDING", "APPLIED"})
        return hashlib.sha256(json.dumps(applied).encode()).hexdigest()

    def register_event(self, account: str, action: CorporateAction, operator: str) -> dict:
        payload = action.model_dump(mode="json")
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(serialized.encode()).hexdigest()
        connection = self.store._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT digest FROM corporate_actions WHERE account = ? AND event_id = ?",
                                          (account, action.event_id)).fetchone()
            if existing:
                if existing["digest"] != digest:
                    raise ValueError("Event ID conflicts with immutable evidence already recorded")
                connection.commit()
                return next(item for item in self.events(account) if item["event_id"] == action.event_id)
            identities = {item["con_id"]: item for item in self.identities(account)}
            if action.con_id not in identities:
                raise ValueError("Event conId has not been qualified for this account")
            if any(item["con_id"] == action.con_id and item["status"] in {"PENDING", "REBUILDING"}
                   for item in self.events(account)):
                raise ValueError("Finish or cancel the existing event for this conId before recording another")
            connection.execute("INSERT INTO corporate_actions(account,event_id,payload_json,digest,operator,status) VALUES (?,?,?,?,?,'PENDING')",
                               (account, action.event_id, serialized, digest, operator))
            self.store.append_event("corporate-actions", "CORPORATE_ACTION_RECORDED",
                                    {"account": account, "event": payload, "digest": digest, "operator": operator})
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        return next(item for item in self.events(account) if item["event_id"] == action.event_id)

    def cancel(self, account: str, event_id: str, operator: str) -> None:
        connection = self.store._connection()
        with connection:
            changed = connection.execute("UPDATE corporate_actions SET status = 'CANCELLED' WHERE account = ? AND event_id = ? AND status = 'PENDING'",
                                         (account, event_id)).rowcount
            if not changed:
                raise ValueError("Only a pending corporate action can be cancelled")
            self.store.append_event("corporate-actions", "CORPORATE_ACTION_CANCELLED",
                                    {"account": account, "event_id": event_id, "operator": operator})

    def resolve(self, account: str, symbol: str) -> dict | None:
        matches = [item for item in self.identities(account) if symbol in item["aliases"]]
        if len(matches) > 1:
            raise ValueError(f"Ambiguous instrument alias: {symbol}")
        if not matches:
            return None
        result = dict(matches[0])
        for event in self.events(account):
            if event["status"] == "PENDING" and event["kind"] == "SYMBOL_CHANGE" and event["con_id"] == result["con_id"]:
                result.update(symbol=event["new_symbol"], con_id=event["new_con_id"] or result["con_id"])
        return result

    def reconcile(self, account: str, identities: list[EquityIdentity], *, broker_flat: bool, now: datetime) -> dict:
        """Commit identity/event transitions only against freshly qualified contracts."""
        connection = self.store._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            known = {item["con_id"]: item for item in self.identities(account)}
            current = {item.con_id: item for item in identities}
            if len(current) != len(identities):
                raise ValueError("Two requested tickers resolved to the same broker conId")
            recovering = [item for item in self.events(account) if item["status"] in {"PENDING", "REBUILDING"}]
            pending = [item for item in recovering if item["status"] == "PENDING"]
            if recovering and not broker_flat:
                raise ValueError("Corporate-action recovery requires broker-confirmed zero positions and zero working/inflight orders")
            transitions = {}
            for event in recovering:
                if datetime.fromisoformat(event["effective_at"]) > now:
                    raise ValueError("Corporate action is not effective yet; remain stopped until its effective time")
                destination = event.get("new_con_id") or event["con_id"]
                identity = current.get(destination)
                if identity is None:
                    raise ValueError("Restart must include every affected corporate-action conId")
                if event["kind"] == "SYMBOL_CHANGE":
                    if identity.symbol != event["new_symbol"]:
                        raise ValueError("Broker-qualified symbol does not match the reviewed symbol change")
                    if event["status"] == "REBUILDING":
                        continue
                    if destination in transitions:
                        raise ValueError("Multiple symbol changes cannot share a replacement conId")
                    if destination != event["con_id"] and destination in known:
                        raise ValueError("Replacement conId already belongs to another registered identity")
                    transitions[destination] = event["con_id"]
            for identity in identities:
                prior_id = transitions.get(identity.con_id, identity.con_id)
                prior = known.get(prior_id)
                if any(other.con_id != identity.con_id and other.symbol == identity.symbol for other in identities):
                    raise ValueError("Ticker reuse or identity collision: duplicate qualified symbol")
                expected_change = any(item["kind"] == "SYMBOL_CHANGE" and item["con_id"] == prior_id for item in pending)
                if prior and prior["symbol"] != identity.symbol and not expected_change:
                    raise ValueError("Unreviewed broker symbol change; record its corporate action before recovery")
                for other in known.values():
                    if other["con_id"] != prior_id and identity.symbol in other["aliases"]:
                        raise ValueError("Ticker reuse or identity collision: conId review required")
                aliases = sorted(set((prior or {}).get("aliases", []) + [identity.symbol]))
                if prior_id != identity.con_id:
                    connection.execute("DELETE FROM equity_identities WHERE account = ? AND con_id = ?", (account, prior_id))
                connection.execute("INSERT INTO equity_identities VALUES (?,?,?,?) ON CONFLICT(account,con_id) DO UPDATE SET identity_json=excluded.identity_json, aliases_json=excluded.aliases_json",
                                   (account, identity.con_id, json.dumps(identity.as_dict()), json.dumps(aliases)))
            for event in pending:
                result = {"broker_flat_confirmed": True, "history_policy": "discard and refetch broker history",
                          "cash_policy": "broker account delta only; no synthetic dividend credit",
                          "qualified_con_ids": sorted(current)}
                connection.execute("UPDATE corporate_actions SET status='REBUILDING', result_json=? WHERE account=? AND event_id=?",
                                   (json.dumps(result), account, event["event_id"]))
                self.store.append_event("corporate-actions", "CORPORATE_ACTION_REBUILD_STARTED",
                                        {"account": account, "event_id": event["event_id"], "result": result})
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        return {"identities": [item.as_dict() for item in identities], "generation": self.generation(account),
                "rebuild_required": bool(recovering), "events": self.events(account)}

    def finish_rebuild(self, account: str, con_ids: set[int]) -> None:
        connection = self.store._connection()
        with connection:
            for event in self.events(account):
                if event["status"] == "REBUILDING" and (event.get("new_con_id") or event["con_id"]) in con_ids:
                    updated = connection.execute("UPDATE corporate_actions SET status='APPLIED' WHERE account=? AND event_id=? AND status='REBUILDING'", (account, event["event_id"])).rowcount
                    if updated:
                        self.store.append_event("corporate-actions", "CORPORATE_ACTION_REBUILD_COMPLETED",
                                                {"account": account, "event_id": event["event_id"], "model_warmup_confirmed": True})
