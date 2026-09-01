from datetime import datetime, timedelta, timezone

import optuna
from optuna.trial import TrialState

from quant.scripts.cleanup_optuna_trials import cleanup_stale_trials


def test_cleanup_marks_only_stale_running_trials_failed_and_backs_up(tmp_path):
    database = tmp_path / "studies.db"
    storage_url = f"sqlite:///{database}"
    study = optuna.create_study(study_name="cleanup-test", storage=storage_url)
    running = study.ask()
    completed = study.ask()
    study.tell(completed, 1.0)

    dry_run = cleanup_stale_trials(
        storage_url,
        min_age_minutes=30,
        dry_run=True,
        now=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    assert [item.trial_number for item in dry_run["candidates"]] == [running.number]
    assert dry_run["failed"] == []
    assert dry_run["backup_path"] is None

    report = cleanup_stale_trials(
        storage_url,
        min_age_minutes=30,
        now=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    assert [item.trial_number for item in report["failed"]] == [running.number]
    assert report["backup_path"] is not None
    assert report["backup_path"].is_file()

    reloaded = optuna.load_study(study_name="cleanup-test", storage=storage_url)
    assert reloaded.trials[running.number].state == TrialState.FAIL
    assert reloaded.trials[completed.number].state == TrialState.COMPLETE
