"""Shared eligibility rules for an Optuna study's usable incumbent."""
from __future__ import annotations

import math
from numbers import Real

NO_QUALIFYING_FOLDS_SCORE = -1_000_000.0
NO_QUALIFYING_CANDIDATE = "NO_QUALIFYING_CANDIDATE"
REJECTED_OUTER_HOLDOUT = "REJECTED_OUTER_HOLDOUT"
PASSED_OUTER_HOLDOUT = "PASSED_OUTER_HOLDOUT"
DEVELOPMENT_SELECTED = "DEVELOPMENT_SELECTED"


def is_qualifying_score(value) -> bool:
    # A qualifying stability score may be negative after penalties. The
    # rejection sentinel and nonfinite values must never become winners.
    return (isinstance(value, Real) and not isinstance(value, bool)
            and math.isfinite(value) and value > NO_QUALIFYING_FOLDS_SCORE)


def qualifying_trial(study):
    """Select a DEVELOPMENT candidate; this does not certify its outer holdout."""
    return max((trial for trial in study.get_trials(deepcopy=False)
                if trial.state.name == "COMPLETE" and is_qualifying_score(trial.value)),
               key=lambda trial: trial.value, default=None)


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
