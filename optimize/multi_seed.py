"""Run independent Optuna studies with one invariant validation contract."""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

import optuna
from optuna.trial import TrialState

from quant.optimize.campaign import (
    atomic_write_json,
    contract_digest,
    load_manifest,
    new_manifest,
)


_MANAGED_FLAGS = {
    "--seed",
    "--evaluation-seed",
    "--trials",
    "--run-id",
    "--resume-run-id",
    "--out-params",
    "--storage",
    "--defer-final-test",
    "--refit-every-n-bars",
}
_PROHIBITED_FLAGS = {
    "--score": "goal mode can stop before the campaign trial count",
    "--fetch-missing": "fetch data once before starting a campaign",
    "--replace-bars": "replace data once before starting a campaign",
}


def _validate_optimizer_args(arguments: list[str]) -> None:
    conflicts = sorted(
        {
            flag
            for argument in arguments
            for flag in _MANAGED_FLAGS
            if argument == flag or argument.startswith(f"{flag}=")
        }
    )
    if conflicts:
        raise ValueError(
            "multi_seed manages these optimizer flags: " + ", ".join(conflicts)
        )
    prohibited = [
        (flag, reason)
        for flag, reason in _PROHIBITED_FLAGS.items()
        if any(argument == flag or argument.startswith(f"{flag}=") for argument in arguments)
    ]
    if prohibited:
        flag, reason = prohibited[0]
        raise ValueError(f"{flag} is not allowed in a stability campaign: {reason}")


def _campaign_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    if not slug:
        raise ValueError("campaign-id must contain at least one letter or number")
    return slug


def _study_record(manifest: dict, study_name: str) -> dict | None:
    return next(
        (item for item in manifest.get("studies", []) if item["study_name"] == study_name),
        None,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run 3+ independent multivariate-TPE studies without touching the outer holdout."
    )
    parser.add_argument("--seeds", nargs="+", type=int, default=[42, 43, 44, 45])
    parser.add_argument("--trials", type=int, default=100)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--manifest", help="Defaults to quant/optimize/campaigns/<id>.json")
    parser.add_argument("--storage", default="sqlite:///quant/optimize/studies.db")
    parser.add_argument("--evaluation-seed", type=int, default=1729)
    parser.add_argument("--refit-every-n-bars", type=int, default=1)
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Run remaining seeds after one optimizer subprocess fails.",
    )
    args, optimizer_args = parser.parse_known_args()
    if len(set(args.seeds)) < 3:
        parser.error("at least three distinct seeds are required")
    if len(set(args.seeds)) != len(args.seeds):
        parser.error("seeds must be unique")
    if not 100 <= args.trials <= 150:
        parser.error("--trials must be between 100 and 150 for a stability campaign")
    if args.refit_every_n_bars < 1:
        parser.error("--refit-every-n-bars must be >= 1")
    try:
        _validate_optimizer_args(optimizer_args)
        campaign_id = _campaign_slug(args.campaign_id)
    except ValueError as error:
        parser.error(str(error))

    manifest_path = Path(
        args.manifest or f"quant/optimize/campaigns/{campaign_id}.json"
    )
    if manifest_path.exists():
        manifest = load_manifest(manifest_path)
        expected = {
            "campaign_id": campaign_id,
            "storage": args.storage,
            "seeds": args.seeds,
            "trials_per_seed": args.trials,
            "evaluation_seed": args.evaluation_seed,
            "optimizer_args": optimizer_args,
        }
        mismatches = [
            key for key, value in expected.items() if manifest.get(key) != value
        ]
        if mismatches:
            parser.error(
                "existing campaign manifest differs in: " + ", ".join(mismatches)
            )
        if manifest.get("outer_holdout", {}).get("status") != "UNTOUCHED":
            parser.error("campaign outer holdout is already consumed; no more trials are allowed")
    else:
        manifest = new_manifest(
            campaign_id=campaign_id,
            storage=args.storage,
            seeds=args.seeds,
            trials_per_seed=args.trials,
            optimizer_args=optimizer_args,
            evaluation_seed=args.evaluation_seed,
        )
        atomic_write_json(manifest_path, manifest)

    failures = []
    params_dir = manifest_path.parent / f"{campaign_id}_params"
    params_dir.mkdir(parents=True, exist_ok=True)
    for seed in args.seeds:
        study_name = f"{campaign_id}_seed_{seed}"
        existing = _study_record(manifest, study_name)
        prior_trial_count = 0
        try:
            prior_study = optuna.load_study(study_name=study_name, storage=args.storage)
        except KeyError:
            prior_study = None
        if prior_study is not None:
            running = prior_study.get_trials(
                deepcopy=False, states=(TrialState.RUNNING, TrialState.WAITING)
            )
            if running:
                raise SystemExit(
                    f"{study_name} has active/stale trials; clean them before resuming"
                )
            prior_contract = prior_study.user_attrs.get("validation_contract")
            expected_digest = manifest.get("validation_contract_sha256")
            if (
                prior_contract is not None
                and expected_digest is not None
                and contract_digest(prior_contract) != expected_digest
            ):
                raise SystemExit(f"{study_name} has a different validation contract")
            prior_trial_count = len(prior_study.trials)
        if existing and existing.get("status") == "COMPLETE":
            if prior_study is None or prior_trial_count < args.trials:
                raise SystemExit(
                    f"{study_name} is marked complete but its Optuna study is incomplete"
                )
            print(f"{study_name}: already complete")
            continue
        remaining_trials = max(0, args.trials - prior_trial_count)
        out_params = params_dir / f"seed_{seed}.json"
        command = [
            sys.executable,
            "-m",
            "quant.optimize.optimize",
            *optimizer_args,
            "--trials",
            str(remaining_trials),
            "--seed",
            str(seed),
            "--evaluation-seed",
            str(args.evaluation_seed),
            "--run-id",
            study_name,
            "--out-params",
            str(out_params),
            "--storage",
            args.storage,
            "--defer-final-test",
            "--refit-every-n-bars",
            str(args.refit_every_n_bars),
        ]
        started_at = time.time()
        result = subprocess.run(command, check=False)
        record = existing or {"seed": seed, "study_name": study_name}
        if existing is None:
            manifest["studies"].append(record)
        record.update(
            {
                "seed": seed,
                "study_name": study_name,
                "out_params": str(out_params),
                "return_code": result.returncode,
                "requested_total_trials": args.trials,
                "prior_trials": prior_trial_count,
                "added_trials": remaining_trials,
                "started_at": started_at,
                "finished_at": time.time(),
                "status": "FAILED" if result.returncode else "COMPLETE",
            }
        )
        if result.returncode == 0:
            study = optuna.load_study(study_name=study_name, storage=args.storage)
            contract = study.user_attrs.get("validation_contract")
            if contract is None:
                record["status"] = "FAILED"
                record["error"] = "study did not persist a validation contract"
                failures.append(study_name)
            else:
                digest = contract_digest(contract)
                expected_digest = manifest.get("validation_contract_sha256")
                if expected_digest is None:
                    manifest["validation_contract_sha256"] = digest
                    manifest["validation_contract"] = contract
                elif digest != expected_digest:
                    record["status"] = "FAILED"
                    record["error"] = "validation contract differs across seeds"
                    failures.append(study_name)
        else:
            failures.append(study_name)
        manifest["updated_at"] = time.time()
        atomic_write_json(manifest_path, manifest)
        if failures and not args.continue_on_error:
            break

    completed = sum(item.get("status") == "COMPLETE" for item in manifest["studies"])
    print(f"Campaign {campaign_id}: {completed}/{len(args.seeds)} studies complete")
    print(f"Manifest -> {manifest_path}")
    if failures:
        raise SystemExit(f"Failed studies: {', '.join(failures)}")


if __name__ == "__main__":
    main()
