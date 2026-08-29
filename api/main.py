"""Dashboard API entrypoint.

Run from the SAME working directory every other command in this project uses
(the directory ABOVE the quant/ package, e.g. .../Workspace):

    cd .../Workspace
    quant/.quant312/bin/python -m uvicorn quant.api.main:app --reload --port 8000
"""
from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from quant.api import broker_routes, jobs_routes, live_mock, news_routes, profiles, runs
from quant.api.jobs import JobManager, WORKDIR
from quant.run.readiness import live_readiness_status

# quant.run.artifacts resolves "quant/runs" relative to the process cwd. Pin
# it to WORKDIR regardless of where uvicorn was launched from, so it always
# matches where run_backtest.py / optimize.py subprocess jobs (pinned to the
# same WORKDIR in quant/api/jobs.py) write their artifacts.
os.chdir(WORKDIR)

app = FastAPI(title="Quant Dashboard API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
    allow_methods=["*"],
    allow_headers=["*"],
)

jobs_routes.manager = JobManager()

app.include_router(runs.router)
app.include_router(jobs_routes.router)
app.include_router(live_mock.router)
app.include_router(news_routes.router)
app.include_router(broker_routes.router)
app.include_router(profiles.router)


@app.on_event("startup")
def start_broker_monitor():
    broker_routes.monitor.start()


@app.on_event("shutdown")
def stop_broker_monitor():
    broker_routes.monitor.stop()


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/readiness/live")
def get_live_readiness():
    return live_readiness_status()
