"""Authenticated, local-only safety controls for registered paper sessions."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import hmac
import ipaddress
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from quant.api import jobs_routes
from quant.api.jobs import JOBS_DIR
from quant.ops.state import OperationsStore
from quant.ops.corporate_actions import CorporateAction, CorporateActionRegistry

router = APIRouter(prefix="/api/operations", tags=["operations"])
ACTIONS = ("FREEZE_ENTRIES", "RESUME_ENTRIES", "CANCEL_ALL", "FLATTEN", "KILL")


def authenticate(request: Request) -> str:
    # Remote administration remains a separate readiness item. Proxy headers
    # are not consulted: the socket peer must be loopback and browser origins
    # must be the configured local dashboard origins.
    try:
        local = ipaddress.ip_address(request.client.host).is_loopback
    except (ValueError, AttributeError):
        local = False
    if not local:
        raise HTTPException(403, "Safety controls are available only on localhost")
    origin = request.headers.get("origin")
    if origin:
        parsed = urlparse(origin)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise HTTPException(403, "Dashboard origin is not authorized")
    secret = os.environ.get("QUANT_CONTROL_TOKEN", "")
    if len(secret) < 32:
        raise HTTPException(503, "Safety controls are locked: configure QUANT_CONTROL_TOKEN (at least 32 characters) on the API server")
    supplied = request.headers.get("authorization", "")
    if not hmac.compare_digest(supplied.encode(), f"Bearer {secret}".encode()):
        raise HTTPException(401, "Invalid operator token", headers={"WWW-Authenticate": "Bearer"})
    return os.environ.get("QUANT_CONTROL_OPERATOR", "local-operator")


class CommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    action: Literal["FREEZE_ENTRIES", "RESUME_ENTRIES", "CANCEL_ALL", "FLATTEN", "KILL"]
    reason: str = Field(min_length=3, max_length=500)
    request_id: UUID
    confirmation: str = Field(default="", max_length=160)


def _job(job_id: str) -> dict:
    manager = jobs_routes.manager
    job = manager.get(job_id) if manager is not None else None
    if job is None:
        raise HTTPException(404, "Execution job not found")
    if job.get("kind") != "paper":
        raise HTTPException(403, "Manual controls are enabled only for paper sessions; live readiness remains locked")
    if job.get("id") != job_id:
        raise HTTPException(409, "Execution job identity mismatch")
    return job


def _store() -> OperationsStore:
    path = Path(JOBS_DIR) / "operations.sqlite3"
    if not path.is_file():
        raise HTTPException(503, "Operations database is unavailable; start a supervised paper session first")
    return OperationsStore(str(path))


def _status(store, job) -> dict:
    target = f"strategy:{job['id']}"
    heartbeat = store.get_heartbeat(target)
    fresh = False
    if heartbeat:
        try:
            observed = datetime.fromisoformat(heartbeat["observed_at"])
            age = (datetime.now(timezone.utc) - observed).total_seconds()
            fresh = 0 <= age <= 10
        except (ValueError, TypeError):
            pass
    blockers = []
    if job.get("status") != "running":
        blockers.append("Session is not running.")
    if not fresh:
        blockers.append("Strategy heartbeat is missing or older than 10 seconds.")
    if (heartbeat or {}).get("status") != "FROZEN":
        blockers.append("Strategy is not in a resumable FROZEN state.")
    reported = (heartbeat or {}).get("details", {}).get("resume_blockers")
    if not isinstance(reported, list) or any(not isinstance(item, str) for item in reported):
        blockers.append("Strategy resume eligibility is unknown.")
    else:
        blockers.extend(reported)
    commands = [asdict(command) for command in store.commands_for_target(target)]
    pending = any(command["status"] in {"PENDING", "CLAIMED", "ACKNOWLEDGED"} for command in commands)
    if pending:
        blockers.append("Another control command is pending broker confirmation.")
    return {"job_id": job["id"], "target": target, "job_status": job["status"],
            "heartbeat_fresh": fresh, "heartbeat": heartbeat,
            "resume_blockers": blockers, "commands": commands,
            "actions": list(ACTIONS), "as_of": datetime.now(timezone.utc).isoformat(),
            "corporate_actions": _corporate_status(store, job)}


def _corporate_status(store, job) -> dict:
    binding = store.get_state(f"equity-identities:strategy:{job['id']}")
    if not binding:
        return {"available": False, "reason": "This session has no qualified equity identities yet", "identities": [], "events": []}
    registry = CorporateActionRegistry(store)
    account = binding["account_id"]
    return {"available": True, "account_id": account, "identities": registry.identities(account),
            "events": registry.events(account), "generation": registry.generation(account),
            "recovery_policy": "Freeze entries, flatten and stop; restart after effective time for fresh qualification and model warmup."}


class CorporateActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: CorporateAction
    confirmation: str


@router.post("/{job_id}/corporate-actions", status_code=202)
def record_corporate_action(job_id: str, body: CorporateActionRequest, operator: str = Depends(authenticate)):
    job = _job(job_id)
    if body.confirmation != f"RECORD CORPORATE ACTION {job_id}":
        raise HTTPException(422, "Corporate-action confirmation must match the selected job")
    store = _store()
    try:
        state = _corporate_status(store, job)
        if not state["available"]:
            raise HTTPException(409, state["reason"])
        return CorporateActionRegistry(store).register_event(state["account_id"], body.event, operator)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    finally:
        store.close()


@router.post("/{job_id}/corporate-actions/{event_id}/cancel")
def cancel_corporate_action(job_id: str, event_id: str, operator: str = Depends(authenticate)):
    job = _job(job_id)
    store = _store()
    try:
        state = _corporate_status(store, job)
        if not state["available"]:
            raise HTTPException(409, state["reason"])
        CorporateActionRegistry(store).cancel(state["account_id"], event_id, operator)
        return _corporate_status(store, job)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    finally:
        store.close()


@router.get("/{job_id}")
def status(job_id: str, operator: str = Depends(authenticate)):
    job = _job(job_id)
    store = _store()
    try:
        return {**_status(store, job), "operator": operator}
    finally:
        store.close()


@router.post("/{job_id}/commands", status_code=202)
def submit(job_id: str, body: CommandRequest, operator: str = Depends(authenticate)):
    job = _job(job_id)
    target = f"strategy:{job_id}"
    if body.action != "FREEZE_ENTRIES" and body.confirmation != f"{body.action} {target}":
        raise HTTPException(422, f"Confirmation must exactly equal {body.action} {target}")
    store = _store()
    try:
        identifier = body.request_id.hex
        payload = {"operator": operator, "source": "dashboard"}
        existing = store.get_command(identifier)
        if existing:
            if (existing.target, existing.action, existing.reason, existing.payload) != (target, body.action, body.reason, payload):
                raise HTTPException(409, "Request ID was already used for different content")
            return asdict(existing)
        if job.get("status") != "running":
            raise HTTPException(409, "Session is not running; no command was queued")
        state = _status(store, job)
        if body.action == "RESUME_ENTRIES" and state["resume_blockers"]:
            raise HTTPException(409, {"message": "Resume blocked", "blockers": state["resume_blockers"]})
        return asdict(store.request_command(target, body.action, body.reason,
                      payload=payload, command_id=identifier))
    finally:
        store.close()


@router.post("/{job_id}/commands/{command_id}/cancel")
def cancel(job_id: str, command_id: UUID, operator: str = Depends(authenticate)):
    job = _job(job_id)
    store = _store()
    try:
        return asdict(store.cancel_pending_command(command_id.hex, f"strategy:{job['id']}", operator))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    finally:
        store.close()
