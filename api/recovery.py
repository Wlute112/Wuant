"""Local authenticated recovery configuration, durable jobs and restore review."""
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import threading
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from quant.api import jobs_routes
from quant.api.operations import authenticate
from quant.ops import recovery

router = APIRouter(prefix="/api/recovery", tags=["recovery"])
STATE = recovery.STATE
_mutex = threading.RLock()
_stop = threading.Event()
_thread = None


def _read(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def _jobs():
    if jobs_routes.manager is None:
        raise HTTPException(503, "Durable job registry unavailable")
    try:
        return jobs_routes.manager.list()
    except RuntimeError as exc:
        raise HTTPException(503, "Durable job registry unavailable") from exc


def _active():
    return [j for j in _jobs() if j.get("kind") == "recovery" and j.get("status") in recovery.ACTIVE]


@router.get("")
def status(operator: str = Depends(authenticate)):
    config = _read(STATE / "config.json")
    reports = []
    jobs = {j["id"]: j for j in _jobs() if j.get("kind") == "recovery"}
    for path in sorted((STATE / "requests").glob("*.json"), reverse=True):
        request = _read(path)
        report = _read(STATE / f"{path.stem}.json", {})
        job = jobs.get(request["job_id"])
        reports.append({**{k: request[k] for k in ("id", "action", "snapshot", "created_at", "operator", "job_id")},
                        **report, "job_status": job.get("status") if job else "unknown",
                        "status": report.get("status") if job and job.get("status") == "completed" else (job or {}).get("status", "unknown")})
    reports.sort(key=lambda r: r["created_at"], reverse=True)
    backups = [r for r in reports if r["action"] == "backup" and r["status"] == "completed"
               and r.get("repository") == (config or {}).get("repository")]
    age = None
    if backups:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(backups[0]["capture_started_at"])).total_seconds() / 3600
    inventory = _read(STATE / "inventory.json")
    if inventory and inventory.get("repository") != (config or {}).get("repository"):
        inventory = None
    return {"config": config, "reports": reports, "inventory": inventory,
            "dependencies": {name: bool(shutil.which(name)) for name in ("restic", "redis-cli", "redis-check-rdb")},
            "backup_age_hours": age, "rpo_status": "unknown" if age is None else "within target" if 0 <= age <= config["rpo_hours"] else "overdue",
            "as_of": recovery.now(), "scheduler": "Runs only while this API is running; retries at most hourly",
            "validation": "Off-host destination and clean-host drill are still required. Restores never authorize execution."}


@router.put("/config")
def configure(config: recovery.RecoveryConfig, operator: str = Depends(authenticate)):
    with _mutex:
        if _active():
            raise HTTPException(409, "Wait for or cancel the active recovery job before changing configuration")
        with recovery.exclusive(STATE):
            recovery.atomic_json(STATE / "config.json", config.model_dump())
            recovery.atomic_json(STATE / "config-audit" / f"{uuid4().hex}.json",
                                 {"operator": operator, "recorded_at": recovery.now(), "config": config.model_dump()})
    return config.model_dump()


class RecoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    action: Literal["initialize", "backup", "check", "restore"]
    snapshot: str | None = None
    confirmation: str = ""


@router.post("/jobs", status_code=202)
def launch(body: RecoveryRequest, operator: str = Depends(authenticate)):
    with _mutex:
        identifier = body.request_id.hex
        path = STATE / "requests" / f"{identifier}.json"
        payload = body.model_dump(mode="json")
        existing = _read(path)
        if existing:
            if existing["body"] != payload or existing["operator"] != operator:
                raise HTTPException(409, "Request ID already belongs to different content")
            job = jobs_routes.manager.get(existing["job_id"])
            if not job:
                raise HTTPException(409, "Submission outcome unknown; review the durable request before using a new ID")
            return job
        try:
            config = recovery.load_config(STATE)
        except (OSError, ValueError) as exc:
            raise HTTPException(409, "Save a valid recovery configuration first") from exc
        if body.action == "restore" and (not body.snapshot or not recovery.SNAPSHOT.fullmatch(body.snapshot)):
            raise HTTPException(422, "Select an exact snapshot ID")
        if body.action != "restore" and body.snapshot is not None:
            raise HTTPException(422, "Snapshot applies only to restore")
        expected = f"RESTORE ISOLATED {body.snapshot}" if body.action == "restore" else f"INITIALIZE {config.repository}"
        if body.action in {"initialize", "restore"} and body.confirmation != expected:
            raise HTTPException(422, f"Type {expected}")
        if _active():
            raise HTTPException(409, "A recovery job is already active")
        if not all(shutil.which(name) for name in ("restic", "redis-cli", "redis-check-rdb")):
            raise HTTPException(503, "Install restic and Redis command-line tools on the API host")
        job_id = f"recovery_{identifier}"
        with recovery.exclusive(STATE):
            recovery.atomic_json(path, {"id": identifier, "job_id": job_id, "body": payload,
                                 "action": body.action, "snapshot": body.snapshot, "config": config.model_dump(),
                                 "created_at": recovery.now(), "operator": operator})
        return jobs_routes.manager.submit("recovery", "quant.ops.recovery",
                    [body.action, "--request-id", identifier, *(["--snapshot", body.snapshot] if body.snapshot else [])],
                    config={"action": body.action, "snapshot": body.snapshot}, job_id=job_id)


@router.post("/jobs/{request_id}/cancel")
def cancel(request_id: UUID, operator: str = Depends(authenticate)):
    record = _read(STATE / "requests" / f"{request_id.hex}.json")
    if not record:
        raise HTTPException(404, "Recovery request not found")
    recovery.atomic_json(STATE / "cancellations" / f"{uuid4().hex}.json",
                         {"request_id": request_id.hex, "operator": operator, "at": recovery.now()})
    return jobs_routes.manager.cancel(record["job_id"])


def schedule_once():
    config = _read(STATE / "config.json")
    if not config or not config.get("scheduled") or _active():
        return
    requests = [_read(p) for p in (STATE / "requests").glob("*.json")]
    previous = [r for r in requests if r["action"] == "backup" and r["config"]["repository"] == config["repository"]]
    if previous:
        latest = max(previous, key=lambda r: r["created_at"])
        report = _read(STATE / f"{latest['id']}.json", {})
        delay = config["rpo_hours"] if report.get("status") == "completed" else 1
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(latest["created_at"])).total_seconds() / 3600
        if age < delay:
            return
    launch(RecoveryRequest(request_id=uuid4(), action="backup"), operator="configured-scheduler")


def start_scheduler():
    global _thread
    _stop.clear()
    def loop():
        while not _stop.wait(30):
            try:
                schedule_once()
            except Exception:
                # A missing dependency or unavailable registry is surfaced by
                # status. Never launch through an alternate persistence path.
                pass
    _thread = threading.Thread(target=loop, daemon=True, name="recovery-scheduler")
    _thread.start()


def stop_scheduler():
    _stop.set()
    if _thread:
        _thread.join(timeout=2)
