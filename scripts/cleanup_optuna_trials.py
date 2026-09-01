#!/usr/bin/env python3
"""Fail stale Optuna RUNNING trials left behind by interrupted workers.

The default age threshold prevents this maintenance command from touching an
active optimization. SQLite storage is backed up transactionally before any
state changes unless ``--no-backup`` is explicitly supplied.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

import optuna
from optuna.storages import RDBStorage
from optuna.trial import TrialState
from sqlalchemy.engine import make_url


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = REPOSITORY_ROOT / "optimize" / "studies.db"
DEFAULT_STORAGE = f"sqlite:///{DEFAULT_DATABASE}"


@dataclass(frozen=True)
class StaleTrial:
    study_name: str
    trial_number: int
    trial_id: int
    started_at: datetime | None
    age_minutes: float


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def find_stale_trials(
    storage: RDBStorage,
    *,
    study_names: set[str] | None = None,
    min_age_minutes: float = 60.0,
    now: datetime | None = None,
) -> list[StaleTrial]:
    if min_age_minutes < 0:
        raise ValueError("min_age_minutes must be non-negative")
    current = _naive_utc(now or datetime.now(timezone.utc))
    stale: list[StaleTrial] = []
    summaries = optuna.study.get_all_study_summaries(storage=storage)
    available_names = {summary.study_name for summary in summaries}
    missing = (study_names or set()) - available_names
    if missing:
        raise ValueError(f"unknown Optuna study name(s): {', '.join(sorted(missing))}")

    for summary in summaries:
        if study_names and summary.study_name not in study_names:
            continue
        study = optuna.load_study(study_name=summary.study_name, storage=storage)
        for trial in study.get_trials(deepcopy=False, states=(TrialState.RUNNING,)):
            started = trial.datetime_start
            age_minutes = (
                float("inf")
                if started is None
                else max((current - _naive_utc(started)).total_seconds() / 60.0, 0.0)
            )
            if age_minutes >= min_age_minutes:
                # Optuna exposes the storage id only on FrozenTrial. The state
                # transition itself uses RDBStorage's public, row-locked API.
                stale.append(
                    StaleTrial(
                        study_name=summary.study_name,
                        trial_number=trial.number,
                        trial_id=trial._trial_id,
                        started_at=started,
                        age_minutes=age_minutes,
                    )
                )
    return stale


def backup_sqlite_storage(storage_url: str) -> Path | None:
    url = make_url(storage_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        return None
    source_path = Path(url.database).expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"Optuna SQLite database does not exist: {source_path}")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = source_path.with_name(f"{source_path.name}.backup-{stamp}")
    suffix = 1
    while backup_path.exists():
        backup_path = source_path.with_name(f"{source_path.name}.backup-{stamp}-{suffix}")
        suffix += 1
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    destination = sqlite3.connect(backup_path)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return backup_path


def cleanup_stale_trials(
    storage_url: str,
    *,
    study_names: set[str] | None = None,
    min_age_minutes: float = 60.0,
    dry_run: bool = False,
    create_backup: bool = True,
    now: datetime | None = None,
) -> dict:
    storage = RDBStorage(storage_url)
    candidates = find_stale_trials(
        storage,
        study_names=study_names,
        min_age_minutes=min_age_minutes,
        now=now,
    )
    backup_path = None
    if candidates and not dry_run and create_backup:
        backup_path = backup_sqlite_storage(storage_url)

    failed: list[StaleTrial] = []
    skipped: list[StaleTrial] = []
    if not dry_run:
        for candidate in candidates:
            current = storage.get_trial(candidate.trial_id)
            if current.state != TrialState.RUNNING:
                skipped.append(candidate)
                continue
            try:
                changed = storage.set_trial_state_values(
                    candidate.trial_id,
                    TrialState.FAIL,
                )
            except RuntimeError:
                # Another worker completed the trial after discovery. Never
                # overwrite an already-terminal trial.
                changed = False
            (failed if changed else skipped).append(candidate)

    return {
        "candidates": candidates,
        "failed": failed,
        "skipped": skipped,
        "backup_path": backup_path,
        "dry_run": dry_run,
    }


def _format_trial(trial: StaleTrial) -> str:
    age = "unknown" if trial.age_minutes == float("inf") else f"{trial.age_minutes:.1f}m"
    return (
        f"study={trial.study_name!r} trial={trial.trial_number} "
        f"storage_id={trial.trial_id} age={age}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mark stale Optuna RUNNING trials as FAILED",
    )
    parser.add_argument("--storage", default=DEFAULT_STORAGE)
    parser.add_argument(
        "--study-name",
        action="append",
        default=[],
        help="Limit cleanup to one study; repeat to select multiple studies",
    )
    parser.add_argument(
        "--min-age-minutes",
        type=float,
        default=60.0,
        help="Only fail RUNNING trials at least this old (default: 60)",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Do not create a transactional backup for SQLite storage",
    )
    args = parser.parse_args()

    report = cleanup_stale_trials(
        args.storage,
        study_names=set(args.study_name) or None,
        min_age_minutes=args.min_age_minutes,
        dry_run=args.dry_run,
        create_backup=not args.no_backup,
    )
    for candidate in report["candidates"]:
        prefix = "WOULD FAIL" if args.dry_run else (
            "FAILED" if candidate in report["failed"] else "SKIPPED"
        )
        print(f"{prefix}: {_format_trial(candidate)}")
    if report["backup_path"] is not None:
        print(f"SQLite backup: {report['backup_path']}")
    print(
        f"Candidates={len(report['candidates'])} "
        f"failed={len(report['failed'])} skipped={len(report['skipped'])}"
    )


if __name__ == "__main__":
    main()
