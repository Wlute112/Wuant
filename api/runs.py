"""Read-only routes over persisted run artifacts (see quant.run.artifacts)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from quant.api import jobs_routes
from quant.run.artifacts import delete_run, list_run_summaries, load_run

router = APIRouter(prefix="/api/runs", tags=["runs"])


@router.get("")
def get_runs(kind: str | None = Query(default=None)):
    summaries = list_run_summaries()
    if kind:
        summaries = [s for s in summaries if s.get("kind") == kind]
    return summaries


@router.get("/{run_id}")
def get_run(run_id: str):
    run = load_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run {run_id!r} not found")
    return run


@router.delete("/{run_id}")
def remove_run(run_id: str):
    """Delete a run artifact and the backend job that produced it. job_id ==
    run_id for backtest/optimize jobs (see quant.api.jobs.submit's --run-id
    wiring), so the same id cleans up both the artifact and the job's
    log/temp-override files.
    """
    if not delete_run(run_id):
        raise HTTPException(status_code=404, detail=f"run {run_id!r} not found")
    job_found = False
    if jobs_routes.manager is not None:
        try:
            job_found = jobs_routes.manager.delete(run_id)["job_found"]
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"run_id": run_id, "deleted": True, "job_deleted": job_found}
