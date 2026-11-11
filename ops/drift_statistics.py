"""Descriptive uncertainty for drift review (local draft, cloud-corrected)."""
import math
import statistics


def summarize(samples: list[dict], minimum_samples: int) -> dict[str, dict]:
    values = {key: [] for key in (
        "prediction_mean", "absolute_error", "direction_accuracy", "slippage_bps",
        "reject_rate", "feature_age_seconds",
    )}

    def finite(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

    for sample in samples:
        pred, actual = sample.get("prediction"), sample.get("actual")
        if finite(pred):
            values["prediction_mean"].append(pred)
            if finite(actual):
                values["absolute_error"].append(abs(pred - actual))
                values["direction_accuracy"].append(int((pred > 0) - (pred < 0) == (actual > 0) - (actual < 0)))
        for name in ("slippage_bps", "feature_age_seconds"):
            value = sample.get(name)
            if finite(value) and (name != "feature_age_seconds" or value >= 0):
                values[name].append(value)
        rejected = sample.get("rejected")
        if finite(rejected) and rejected in (0, 1):
            values["reject_rate"].append(rejected)

    result = {}
    for name, data in values.items():
        n = len(data)
        mean = statistics.mean(data) if n else None
        interval = None
        if n > 1:
            margin = 1.96 * statistics.stdev(data) / math.sqrt(n)
            low, high = mean - margin, mean + margin
            if name in {"absolute_error", "feature_age_seconds", "direction_accuracy", "reject_rate"}:
                low = max(0, low)
            if name in {"direction_accuracy", "reject_rate"}:
                high = min(1, high)
            interval = [low, high]
        result[name] = {"n": n, "mean": mean, "ci95": interval,
                        "status": "ready" if n >= max(2, minimum_samples) else "insufficient"}
    return result
