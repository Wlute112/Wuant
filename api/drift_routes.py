"""Authenticated review configuration, collection, history and bounded scheduling."""
import logging
from pathlib import Path
import threading

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from quant.api import jobs_routes
from quant.api.jobs import JOBS_DIR
from quant.api.operations import authenticate
from quant.ops.drift import DriftStore, Evidence, ReviewConfig
from quant.run.artifacts import load_run
from quant.run.drift_evidence import research_evidence, telemetry_evidence
from quant.run.telemetry import load_telemetry

router = APIRouter(prefix="/api/drift", tags=["drift-review"])
_mutex = threading.RLock()
_stop = threading.Event()
_thread = None
logger = logging.getLogger(__name__)


def store():
    return DriftStore(Path(JOBS_DIR) / "drift.sqlite3")


class BaselineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str
    reason: str = Field(min_length=5, max_length=500)


def _job_evidence(identifier):
    manager = jobs_routes.manager
    job = manager.get(identifier) if manager else None
    if not job or job.get("kind") != "paper" or job.get("id") != identifier:
        raise ValueError("Select a registered paper session")
    # Identifier comes from the durable registry, never an arbitrary file path.
    payload = load_telemetry(Path(JOBS_DIR) / f"{job['id']}_telemetry.json")
    if not payload:
        raise ValueError("Session telemetry is unavailable")
    return telemetry_evidence(payload, job["id"])


@router.get("")
def status(operator: str = Depends(authenticate)):
    with _mutex, store() as db:
        return db.status()


@router.post("/evidence")
def import_evidence(body: Evidence, operator: str = Depends(authenticate)):
    with _mutex, store() as db:
        try:
            return db.ingest(body.model_dump(exclude_none=True), operator)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc


@router.get("/evidence/{source_id}")
def export_evidence(source_id: str, operator: str = Depends(authenticate)):
    with _mutex, store() as db:
        try:
            return db.evidence(source_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc


@router.post("/research/{run_id}")
def import_research(run_id: str, operator: str = Depends(authenticate)):
    if not run_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in run_id):
        raise HTTPException(422, "Invalid run ID")
    run = load_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    with _mutex, store() as db:
        try:
            return db.ingest(research_evidence(run), operator)
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from exc


@router.post("/paper/{job_id}")
def import_paper(job_id: str, operator: str = Depends(authenticate)):
    with _mutex, store() as db:
        try:
            return db.ingest(_job_evidence(job_id), operator, automatic=True)
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from exc


@router.post("/baselines")
def declare_baseline(body: BaselineRequest, operator: str = Depends(authenticate)):
    with _mutex, store() as db:
        try:
            record = db.baseline(body.source_id, body.reason, operator)
            return {k: v for k, v in record.items() if k != "evidence"}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc


@router.post("/config")
def configure(body: ReviewConfig, operator: str = Depends(authenticate)):
    with _mutex, store() as db:
        try:
            return db.configure(body, operator)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc


def _review(db):
    config = db.config()
    if not config or not config["enabled"]:
        raise ValueError("Configure and enable review first")
    errors = []
    try:
        if jobs_routes.manager is None:
            raise RuntimeError("Durable job registry unavailable")
        jobs = jobs_routes.manager.list()
    except RuntimeError as exc:
        jobs = []
        errors.append({"source_id": "job_registry", "reason": str(exc)})
    for job in jobs:
        if job.get("kind") == "paper" and job.get("status") == "running":
            try:
                db.ingest(_job_evidence(job["id"]), "automatic-collector", automatic=True)
            except (ValueError, KeyError, TypeError, OSError) as exc:
                errors.append({"source_id": job["id"], "reason": str(exc)})
    return db.review(source_errors=errors)


@router.post("/review")
def review(operator: str = Depends(authenticate)):
    with _mutex, store() as db:
        try:
            return _review(db)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc


@router.post("/stop")
def stop(operator: str = Depends(authenticate)):
    with _mutex, store() as db:
        try:
            return db.stop(operator)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc


def schedule_once():
    with _mutex, store() as db:
        config = db.config()
        if config and config["enabled"]:
            _review(db)


def start_scheduler():
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()

    def loop():
        while not _stop.wait(15):
            try:
                schedule_once()
            except Exception as exc:
                logger.exception("Drift review failed")
                try:
                    with _mutex, store() as db, db.db:
                        db.append("alert", {"key": "scheduler", "status": "unavailable", "reason": str(exc), "severity": "WARNING"})
                except Exception:
                    logger.exception("Could not persist drift scheduler failure")
    _thread = threading.Thread(target=loop, daemon=True, name="drift-review")
    _thread.start()


def stop_scheduler():
    _stop.set()
    if _thread:
        _thread.join(timeout=2)
