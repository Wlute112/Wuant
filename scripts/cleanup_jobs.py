#!/usr/bin/env python3
"""List or remove old terminal dashboard jobs and their job-scoped files."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json

from quant.api.jobs import JobManager


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--older-than-days", type=int, default=30)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, args.older_than_days))
    manager = JobManager()
    candidates = []
    for job in manager.list():
        if job.get("status") not in {"completed", "failed", "cancelled"}:
            continue
        timestamp = job.get("finished_at") or job.get("started_at")
        try:
            finished = datetime.fromisoformat(str(timestamp))
            if finished.tzinfo is None:
                finished = finished.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        if finished.astimezone(timezone.utc) < cutoff:
            candidates.append(job)
    # Deleting a parent also removes its terminal companions.
    parents = [job for job in candidates if not job.get("parent_job_id")]
    result = {"apply": args.apply, "job_ids": [job["id"] for job in parents]}
    if args.apply:
        result["deleted"] = [manager.delete(job["id"]) for job in parents]
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
