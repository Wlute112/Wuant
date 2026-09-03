"""Shared multi-seed campaign manifest and Optuna loading helpers."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import optuna


SCHEMA_VERSION = 1


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def contract_digest(contract: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(contract).encode()).hexdigest()


def load_manifest(path: str | Path) -> dict[str, Any]:
    manifest_path = Path(path)
    with manifest_path.open() as handle:
        manifest = json.load(handle)
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported campaign manifest schema in {manifest_path}")
    return manifest


def atomic_write_json(path: str | Path, payload: dict[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent)
    )
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def locked_manifest(path: str | Path) -> Iterator[tuple[Any, dict[str, Any]]]:
    """Exclusive manifest transaction used by the one-shot holdout gate."""
    manifest_path = Path(path)
    with manifest_path.open("r+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        manifest = json.load(handle)
        if manifest.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported campaign manifest schema")
        yield handle, manifest
        handle.seek(0)
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.truncate()
        handle.flush()
        os.fsync(handle.fileno())
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def study_names(manifest: dict[str, Any]) -> list[str]:
    return [str(item["study_name"]) for item in manifest.get("studies", [])]


def load_studies(manifest: dict[str, Any]) -> list[optuna.Study]:
    storage = str(manifest["storage"])
    studies = [
        optuna.load_study(study_name=name, storage=storage)
        for name in study_names(manifest)
    ]
    if not studies:
        raise ValueError("campaign has no studies")
    return studies


def invariant_validation_contract(studies: list[optuna.Study]) -> dict[str, Any]:
    contracts = [study.user_attrs.get("validation_contract") for study in studies]
    if any(contract is None for contract in contracts):
        raise ValueError("every study must have a validation_contract")
    digests = {contract_digest(contract) for contract in contracts}
    if len(digests) != 1:
        raise ValueError("seed studies do not share an invariant validation contract")
    return dict(contracts[0])


def study_snapshot(studies: list[optuna.Study]) -> dict[str, Any]:
    """Content fingerprint proving no trials changed after robustness testing."""
    rows = []
    for study in sorted(studies, key=lambda item: item.study_name):
        trials = []
        for trial in study.get_trials(deepcopy=False):
            trials.append(
                {
                    "number": trial.number,
                    "state": trial.state.name,
                    "value": trial.value,
                    "params": trial.params,
                    "walk_forward_folds": trial.user_attrs.get("walk_forward_folds", []),
                }
            )
        rows.append(
            {
                "study_name": study.study_name,
                "sampler_seed": study.user_attrs.get("sampler_seed"),
                "trials": trials,
            }
        )
    return {
        "studies": [
            {
                "study_name": row["study_name"],
                "sampler_seed": row["sampler_seed"],
                "trial_count": len(row["trials"]),
            }
            for row in rows
        ],
        "sha256": hashlib.sha256(canonical_json(rows).encode()).hexdigest(),
    }


def new_manifest(
    *,
    campaign_id: str,
    storage: str,
    seeds: list[int],
    trials_per_seed: int,
    optimizer_args: list[str],
    evaluation_seed: int,
) -> dict[str, Any]:
    now = time.time()
    return {
        "schema_version": SCHEMA_VERSION,
        "campaign_id": campaign_id,
        "created_at": now,
        "updated_at": now,
        "storage": storage,
        "seeds": seeds,
        "trials_per_seed": trials_per_seed,
        "evaluation_seed": evaluation_seed,
        "optimizer_args": optimizer_args,
        "studies": [],
        "validation_contract_sha256": None,
        "outer_holdout": {"status": "UNTOUCHED", "evaluations": 0},
    }
