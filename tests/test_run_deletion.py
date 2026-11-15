"""Run deletion exercises actual filesystem cleanup and the job lifecycle."""
import json

import pytest
from fastapi import HTTPException

from quant.api import jobs, jobs_routes, runs
from quant.run import artifacts


@pytest.fixture
def deletion(tmp_path, monkeypatch):
    class Store:
        def __init__(self):
            self.records = {}

        def get(self, job_id):
            return self.records.get(job_id)

        def delete(self, job_id):
            return self.records.pop(job_id, None) is not None

    job_dir, run_dir = tmp_path / "jobs", tmp_path / "runs"
    run_dir.mkdir()
    monkeypatch.setattr(jobs, "JOBS_DIR", job_dir)
    monkeypatch.setattr(artifacts, "RUNS_DIR", run_dir)
    store = Store()
    manager = jobs.JobManager(store=store)
    monkeypatch.setattr(jobs_routes, "manager", manager)

    def create(job_id="optimize_test", status="completed", companion_ids=()):
        artifact = run_dir / f"{job_id}.json"
        artifact.write_text(json.dumps({"run_id": job_id, "kind": "optimize"}))
        store.records[job_id] = {"id": job_id, "status": status, "kind": "optimize",
                                 "companion_job_ids": list(companion_ids)}
        (job_dir / f"{job_id}.log").write_text("output")
        (job_dir / f".{job_id}.spec.json").write_text("{}")
        (job_dir / f"{job_id}_progress.json").write_text("{}")
        detail = job_dir / f"{job_id}_progress.evidence" / "nested"
        detail.mkdir(parents=True)
        (detail / "1.json").write_text("{}")
        return artifact

    return manager, store, job_dir, create


def test_deletion_removes_nested_evidence_files_and_registry_without_touching_other_runs(deletion):
    _, store, job_dir, create = deletion
    artifact = create()
    other = create("optimize_test2")
    result = runs.remove_run("optimize_test")
    assert result == {"run_id": "optimize_test", "deleted": True, "job_deleted": True}
    assert not artifact.exists() and "optimize_test" not in store.records
    assert not (job_dir / "optimize_test_progress.evidence").exists()
    assert not (job_dir / ".optimize_test.spec.json").exists()
    assert other.exists() and (job_dir / "optimize_test2.log").exists()
    assert store.get("optimize_test2") is not None


@pytest.mark.parametrize("artifact_missing,job_missing", [(True, False), (False, True), (True, True)])
def test_retry_completes_partial_deletion_even_if_artifact_and_registry_were_already_removed(deletion, artifact_missing, job_missing):
    _, store, job_dir, create = deletion
    artifact = create()
    if artifact_missing:
        artifact.unlink()
    if job_missing:
        store.delete("optimize_test")
    assert runs.remove_run("optimize_test")["deleted"] is True
    assert not artifact.exists() and not list(job_dir.iterdir())
    with pytest.raises(HTTPException) as error:
        runs.remove_run("optimize_test")
    assert error.value.status_code == 404


@pytest.mark.parametrize("active_companion", [False, True])
def test_active_job_or_companion_blocks_deletion_before_artifact_cleanup(deletion, monkeypatch, active_companion):
    manager, store, job_dir, create = deletion
    # Isolate cancellation-state protection from OS process liveness probing.
    monkeypatch.setattr(manager, "get", store.get)
    artifact = create(status="completed" if active_companion else "running",
                      companion_ids=["supervisor_test"] if active_companion else [])
    if active_companion:
        store.records["supervisor_test"] = {"id": "supervisor_test", "status": "running"}
    with pytest.raises(HTTPException) as error:
        runs.remove_run("optimize_test")
    assert error.value.status_code == 409
    assert artifact.exists() and (job_dir / "optimize_test.log").exists()
    assert store.get("optimize_test") is not None


def test_cleanup_failure_keeps_run_and_registry_retryable(deletion, monkeypatch):
    _, store, job_dir, create = deletion
    artifact = create()
    original = jobs.shutil.rmtree
    def blocked(path):
        raise PermissionError("fixture: cleanup denied")
    monkeypatch.setattr(jobs.shutil, "rmtree", blocked)
    with pytest.raises(PermissionError, match="cleanup denied"):
        runs.remove_run("optimize_test")
    assert artifact.exists() and store.get("optimize_test") is not None
    monkeypatch.setattr(jobs.shutil, "rmtree", original)
    assert runs.remove_run("optimize_test")["deleted"] is True
    assert not list(job_dir.iterdir())


def test_completed_companion_job_files_and_evidence_are_cleaned_up(deletion):
    _, store, job_dir, create = deletion
    create(companion_ids=["supervisor_test"])
    create("supervisor_test")
    result = runs.remove_run("optimize_test")
    assert result["job_deleted"] is True
    assert not store.records and not list(job_dir.iterdir())


def test_directory_symlink_is_unlinked_without_deleting_its_target(deletion, tmp_path):
    _, _, job_dir, create = deletion
    create()
    outside = tmp_path / "retained_data"
    outside.mkdir()
    (outside / "keep.json").write_text("retained")
    (job_dir / "optimize_test_external").symlink_to(outside, target_is_directory=True)
    runs.remove_run("optimize_test")
    assert (outside / "keep.json").read_text() == "retained"


def test_missing_run_and_job_returns_404(deletion):
    with pytest.raises(HTTPException) as error:
        runs.remove_run("missing")
    assert error.value.status_code == 404
