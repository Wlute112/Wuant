"""Per-regime breakdown of the alpha layer's walk-forward OOS accuracy.

`PredictionEngine.walk_forward()` (see prediction_engine.py) reports dir_acc /
ic / oos_r2 POOLED across every out-of-sample bar. That pools together bars
where the market was trending, ranging, crashing, etc., which can hide an edge
that is real but concentrated in one regime (or a fake pooled edge that's
actually just one regime's noise).

This module reruns the EXACT SAME expanding walk-forward split -- same folds,
same no-lookahead X/y alignment -- but additionally tags every OOS prediction
with the rule-based (Bull/Bear/Sideways) and HMM regime label in effect at
that bar, then reports the metrics per regime bucket instead of pooled.

Read-only analysis: does not modify PredictionEngine, MLStrategy, or their
public signatures.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from quant.models.prediction_engine import PredictionConfig, PredictionEngine, make_features_targets
from quant.models.regime import RegimeFeatureEngine, STATE_NAMES

MIN_RELIABLE_SAMPLES = 30  # below this, flag the bucket's metrics as noisy


def _metrics(preds: np.ndarray, actuals: np.ndarray) -> dict:
    n = len(preds)
    if n == 0:
        return {"n": 0, "dir_acc": float("nan"), "ic": float("nan"), "r2": float("nan")}
    dir_acc = float((np.sign(preds) == np.sign(actuals)).mean())
    ic = float(np.corrcoef(preds, actuals)[0, 1]) if n > 1 and preds.std() > 0 else float("nan")
    ss_res = float(((actuals - preds) ** 2).sum())
    ss_tot = float(((actuals - actuals.mean()) ** 2).sum()) or 1.0
    return {"n": n, "dir_acc": dir_acc, "ic": ic, "r2": 1.0 - ss_res / ss_tot}


def walk_forward_by_regime(close: np.ndarray, cfg: PredictionConfig, n_splits: int = 5) -> pd.DataFrame:
    """Same expanding walk-forward as PredictionEngine.walk_forward(), with
    each OOS prediction additionally tagged by its rule-based/HMM regime.
    """
    close = np.asarray(close, dtype=float)
    eng = PredictionEngine(cfg)
    # Mirrors walk_forward()'s own internals exactly (same private helpers it
    # uses) so the pooled row below reproduces the same numbers the plain
    # `python -m quant.models.prediction_engine` command reports.
    X, y, idx = make_features_targets(close, cfg, eng._regime_feats(close))
    n = len(X)
    if n < cfg.min_train_bars + n_splits:
        raise ValueError("Not enough samples for the requested walk-forward.")

    regime_frame = (
        RegimeFeatureEngine(cfg.to_regime_config()).compute(close)
        if cfg.use_regime_features
        else None
    )

    fold = n // (n_splits + 1)
    rows = []
    for k in range(1, n_splits + 1):
        train_end = fold * k
        test_end = fold * (k + 1) if k < n_splits else n
        if train_end < cfg.min_train_bars:
            continue
        eng._fit_arrays(X[:train_end], y[:train_end])
        for i in range(train_end, test_end):
            rows.append((int(idx[i]), eng._predict_x(X[i]), float(y[i])))

    df = pd.DataFrame(rows, columns=["bar_index", "pred", "actual"])
    if regime_frame is not None:
        df["rule_state"] = [STATE_NAMES[regime_frame.states[i]] for i in df["bar_index"]]
        df["hmm_state"] = [STATE_NAMES[regime_frame.hmm_state[i]] for i in df["bar_index"]]

    out = [{"regime": "ALL (pooled)", **_metrics(df["pred"].to_numpy(), df["actual"].to_numpy())}]
    if regime_frame is not None:
        for label, sub in df.groupby("rule_state"):
            out.append({"regime": f"rule={label}", **_metrics(sub["pred"].to_numpy(), sub["actual"].to_numpy())})
        for label, sub in df.groupby("hmm_state"):
            out.append({"regime": f"hmm={label}", **_metrics(sub["pred"].to_numpy(), sub["actual"].to_numpy())})
    return pd.DataFrame(out)


if __name__ == "__main__":
    import argparse
    from pathlib import Path

    p = argparse.ArgumentParser(description="Per-regime walk-forward OOS accuracy breakdown.")
    p.add_argument("--csv", default="quant/data/sample_bars.csv")
    p.add_argument("--ticker", default=None, help="Single ticker; default = all.")
    p.add_argument("--n-lags", type=int, default=5)
    p.add_argument("--horizon", type=int, default=1)
    p.add_argument("--splits", type=int, default=5)
    args = p.parse_args()

    if not Path(args.csv).exists():
        raise SystemExit(f"CSV not found: {args.csv}")
    df = pd.read_csv(args.csv)
    tickers = [args.ticker] if args.ticker else sorted(df["ticker"].unique())
    cfg = PredictionConfig(n_lags=args.n_lags, horizon=args.horizon)

    for tk in tickers:
        closes = df[df["ticker"] == tk].sort_values("timestamp")["close"].to_numpy()
        if len(closes) < cfg.min_train_bars + args.splits:
            print(f"{tk}: not enough bars ({len(closes)})")
            continue
        breakdown = walk_forward_by_regime(closes, cfg, n_splits=args.splits)
        print(f"\n=== {tk} ===")
        with pd.option_context("display.float_format", "{:+.4f}".format):
            print(breakdown.to_string(index=False))
        small = breakdown[(breakdown["n"] > 0) & (breakdown["n"] < MIN_RELIABLE_SAMPLES)]
        if not small.empty:
            names = ", ".join(small["regime"])
            print(f"  [note] fewer than {MIN_RELIABLE_SAMPLES} samples -- noisy: {names}")
