"""Shared eligibility rules for an Optuna study's usable incumbent."""
from __future__ import annotations

import math
from numbers import Real

NO_QUALIFYING_FOLDS_SCORE = -1_000_000.0
NO_QUALIFYING_CANDIDATE = "NO_QUALIFYING_CANDIDATE"
REJECTED_OUTER_HOLDOUT = "REJECTED_OUTER_HOLDOUT"
PASSED_OUTER_HOLDOUT = "PASSED_OUTER_HOLDOUT"
DEVELOPMENT_SELECTED = "DEVELOPMENT_SELECTED"
DEVELOPMENT_ELIGIBILITY_VERSION = 1


def development_assessment(folds, min_positive_fraction):
    """Gate development evidence before a candidate can consume the holdout.

    Every fold must demonstrate execution under both cost assumptions. Positive
    normal-cost ratios remain a separate requirement; inactivity is not profit.
    Only observed sizing attempts are explained, never inferred from no fills.
    """
    reasons = []
    finite = lambda value: (isinstance(value, Real) and not isinstance(value, bool)
                            and math.isfinite(value))
    if not folds:
        reasons.append("No development folds were evaluated.")
    normal = [fold.get("normal_ratio") for fold in folds]
    stressed = [fold.get("stressed_ratio") for fold in folds]
    if not all(finite(value) for value in normal + stressed):
        reasons.append("Fold risk-adjusted returns are missing or nonfinite.")
    if folds and all(finite(value) for value in normal):
        positive = sum(value > 0 for value in normal)
        required = max(1, math.ceil(len(folds) * min_positive_fraction))
        if positive < required:
            reasons.append(f"{positive} of {len(folds)} positive folds; {required} required.")
    for index, fold in enumerate(folds, 1):
        inactive = []
        missing = []
        for label, key in (("normal", "trades"), ("stressed", "stressed_trades")):
            count = fold.get(key)
            if not finite(count) or count < 0:
                missing.append(label)
            elif count == 0:
                inactive.append(label)
        if not inactive and not missing:
            continue
        number = fold.get("fold", index)
        if inactive:
            reasons.append(f"Fold {number}: no executed trades under {' and '.join(inactive)} costs.")
        if missing:
            reasons.append(f"Fold {number}: executed-trade counts are missing or invalid under "
                           f"{' and '.join(missing)} costs.")
        # One compact summary per inactive fold; symbols/counts come from the
        # normal run when possible, otherwise from the stressed run.
        observations = (fold.get("sizing_diagnostics") or {}).get((inactive + missing)[0]) or {}
        blocked = []
        for symbol, sizing in observations.items():
            count = sizing.get("below_one_share", 0)
            quantity = sizing.get("max_raw_quantity")
            budget = sizing.get("max_risk_cap_usd")
            if finite(count) and count > 0 and finite(quantity) and finite(budget):
                blocked.append(f"{symbol}: {count} attempts below one share "
                               f"(largest allowed quantity {quantity:.3f}; risk cap up to ${budget:.2f})")
        if blocked:
            reasons.append(f"Fold {number}: whole-share sizing blocked entries. " + "; ".join(blocked) + ".")
    return {"version": DEVELOPMENT_ELIGIBILITY_VERSION, "passed": not reasons,
            "reasons": reasons}


def is_qualifying_score(value) -> bool:
    # A qualifying stability score may be negative after penalties. The
    # rejection sentinel and nonfinite values must never become winners.
    return (isinstance(value, Real) and not isinstance(value, bool)
            and math.isfinite(value) and value > NO_QUALIFYING_FOLDS_SCORE)


def qualifying_trial(study):
    """Select a DEVELOPMENT candidate; this does not certify its outer holdout."""
    requires_assessment = bool((study.user_attrs.get("validation_contract") or {}).get(
        "development_eligibility_version"))
    return max((trial for trial in study.get_trials(deepcopy=False)
                if trial.state.name == "COMPLETE" and is_qualifying_score(trial.value)
                and trial.user_attrs.get("development_assessment", {}).get("passed", not requires_assessment)),
               key=lambda trial: trial.value, default=None)


def search_diagnostics(trials, validation):
    """Explain recorded development rejections without selecting a winner.

    This also works on older artifacts. Missing fold evidence stays unknown;
    neither the outer holdout nor the simulator is consulted.
    """
    fraction = validation.get("min_positive_fold_fraction")
    if (not isinstance(fraction, Real) or isinstance(fraction, bool)
            or not math.isfinite(fraction) or not 0 < fraction <= 1):
        fraction = None
    completed = [trial for trial in trials if trial.get("state") == "COMPLETE"]
    rejected = []
    requires_assessment = bool(validation.get("development_eligibility_version"))
    for trial in completed:
        assessment = (trial.get("user_attrs") or {}).get("development_assessment") or {}
        if is_qualifying_score(trial.get("value")) and assessment.get("passed", not requires_assessment):
            continue
        folds = (trial.get("user_attrs") or {}).get("walk_forward_folds") or []
        normal = [fold.get("normal_ratio") for fold in folds]
        stressed = [fold.get("stressed_ratio") for fold in folds]
        finite = lambda value: (isinstance(value, Real) and not isinstance(value, bool)
                                and math.isfinite(value))
        ratios_known = bool(folds) and all(finite(value) for value in normal)
        positive = sum(value > 0 for value in normal) if ratios_known else None
        required = max(1, math.ceil(len(folds) * fraction)) if folds and fraction else None
        trades_known = bool(folds) and all(finite(fold.get("trades")) for fold in folds)
        reasons = []
        if requires_assessment and not assessment:
            reasons.append("Development eligibility assessment is missing.")
        if not finite(trial.get("value")):
            reasons.append("The trial score is missing or nonfinite.")
        if folds and not all(finite(value) for value in normal + stressed):
            reasons.append("Fold risk-adjusted returns are missing or nonfinite.")
        if positive is not None and required is not None and positive < required:
            reasons.append(f"{positive} of {len(folds)} positive folds; {required} required.")
        if trades_known and not any(fold["trades"] > 0 for fold in folds):
            reasons.append("No executed trades across the validation folds.")
        for reason in assessment.get("reasons", []):
            if reason not in reasons:
                reasons.append(reason)
        rejected.append({
            "trial_number": trial["number"] + 1,
            "positive_folds": positive, "fold_count": len(folds),
            "required_positive_folds": required,
            "trades": sum(fold["trades"] for fold in folds) if trades_known else None,
            "inactive_folds": sum(fold["trades"] == 0 for fold in folds) if trades_known else None,
            "reasons": reasons or ["The recorded score did not qualify; detailed rejection evidence is unavailable."],
        })
    return {
        "completed_trials": len(completed),
        "pruned_trials": sum(trial.get("state") == "PRUNED" for trial in trials),
        "failed_trials": sum(trial.get("state") == "FAIL" for trial in trials),
        "qualifying_trials": len(completed) - len(rejected),
        "min_positive_fold_fraction": fraction,
        "require_active_folds": validation.get("require_active_folds", False),
        "rejected_trials": rejected,
    }


def holdout_assessment(record):
    """Assess recorded activity and net performance without re-evaluating data.

    Legacy records without holdout evidence remain unassessed. Known failed
    evidence is rejected even if an older writer omitted a rejection status.
    New optimizer records include separate normal/stressed activity and PnL.
    """
    validation = record.get("validation") or {}
    final = record.get("final_test") or validation.get("final_test")
    metrics = record.get("oos_metrics")
    if final is None and metrics is None:
        return None
    final, metrics = final or {}, metrics or {}
    reasons = []

    def positive(value):
        return (isinstance(value, Real) and not isinstance(value, bool)
                and math.isfinite(value) and value > 0)

    trades = final.get("trades", metrics.get("total_trades"))
    if not positive(trades):
        reasons.append("Normal-cost holdout has no executed trades.")
    if "stressed_trades" in final and not positive(final["stressed_trades"]):
        reasons.append("Stressed-cost holdout has no executed trades.")
    normal_ratio = final.get("normal_ratio", metrics.get("primary_score"))
    if not positive(normal_ratio):
        reasons.append("Normal-cost holdout risk-adjusted return is not positive and finite.")
    if not positive(final.get("stressed_ratio")):
        reasons.append("Stressed-cost holdout risk-adjusted return is not positive and finite.")
    for label, value, present in (
        ("Normal-cost", final.get("normal_net_profit_usd", metrics.get("net_profit_usd")),
         "normal_net_profit_usd" in final or "net_profit_usd" in metrics),
        ("Stressed-cost", final.get("stressed_net_profit_usd"), "stressed_net_profit_usd" in final),
    ):
        if present and not positive(value):
            reasons.append(f"{label} holdout net profit is not positive and finite.")
    return {"status": REJECTED_OUTER_HOLDOUT if reasons else PASSED_OUTER_HOLDOUT,
            "reasons": reasons}


def annotate_holdout(record):
    """Keep rejected parameters as diagnostic evidence, never as an eligible winner."""
    assessment = holdout_assessment(record)
    if assessment is None:
        return record
    record["holdout_assessment"] = assessment
    record["selection_status"] = assessment["status"]
    record["can_resume"] = False
    if "validation" in record:
        record["validation"]["holdout_assessment"] = assessment
        record["validation"]["outer_holdout_status"] = "CONSUMED"
        if assessment["status"] == REJECTED_OUTER_HOLDOUT:
            record["validation"]["promotion_status"] = REJECTED_OUTER_HOLDOUT
    if assessment["status"] == REJECTED_OUTER_HOLDOUT:
        record["promotion_status"] = REJECTED_OUTER_HOLDOUT
    return record
