"""Read-only research diagnostics and fail-closed finite-liquidity admission.

CSV metadata columns are declarations, not independent certification. Unknown
session/adjustment semantics are disclosed rather than inferred from prices.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path

import numpy as np
import pandas as pd

REQUIRED = {"timestamp", "ticker", "open", "high", "low", "close", "volume"}
METADATA = ("source", "session", "price_basis", "volume_basis")


def inspect_frame(frame: pd.DataFrame, tickers: list[str], asset_class: str) -> dict:
    report = {"asset_class": asset_class, "execution_eligible": False,
              "purpose": "Data diagnostics only; no orders or performance claims",
              "tickers": [], "errors": [], "warnings": []}
    missing = REQUIRED - set(frame.columns)
    if missing:
        report["errors"].append(f"Missing CSV columns: {', '.join(sorted(missing))}. Import timestamp,ticker,open,high,low,close,volume.")
        return report
    if not tickers:
        report["errors"].append("Select at least one ticker.")
        return report
    for ticker in dict.fromkeys(tickers):
        rows = frame[frame.ticker == ticker].copy()
        count = len(rows)
        item = {"ticker": ticker, "bars": count, "start": None, "end": None,
                "cadence_minutes": None, "zero_volume_bars": 0, "missing_volume_bars": 0,
                "invalid_volume_bars": 0, "possible_intraday_gaps": 0, "requested_bar_hours": None,
                "errors": [], "warnings": []}
        for column in METADATA:
            values = rows[column].fillna("unknown").astype(str).unique().tolist() if column in rows else []
            item[column] = ", ".join(sorted(values)) if values else "unknown"
        if "requested_bar_hours" in rows:
            widths = pd.to_numeric(rows.requested_bar_hours, errors="coerce").dropna().unique()
            if len(widths) == 1 and np.isfinite(widths[0]):
                item["requested_bar_hours"] = float(widths[0])
        if not count:
            item["errors"].append("No bars. Import this ticker or fetch it from IBKR.")
        else:
            timestamps = pd.to_datetime(rows.timestamp, format="mixed", utc=True, errors="coerce")
            valid_ts = timestamps.dropna().sort_values()
            if len(valid_ts):
                item.update(start=valid_ts.iloc[0].isoformat(), end=valid_ts.iloc[-1].isoformat())
                deltas = valid_ts.drop_duplicates().diff().dt.total_seconds().dropna() / 60
                intraday = deltas[(deltas > 0) & (deltas <= 720)]
                positive = deltas[deltas > 0]
                if len(positive):
                    cadence = float((intraday if len(intraday) else positive).median())
                    item["cadence_minutes"] = cadence
                    item["possible_intraday_gaps"] = int((intraday > cadence * 1.5).sum())
            if timestamps.isna().any():
                item["errors"].append("Invalid or missing timestamps. Supply ISO timestamps (naive timestamps are interpreted as UTC).")
            if timestamps.dropna().duplicated().any():
                item["errors"].append("Duplicate ticker/timestamp rows. Resolve revisions before research.")
            if not timestamps.dropna().is_monotonic_increasing:
                item["warnings"].append("Rows are out of order; execution sorts by timestamp.")
            prices = rows[["open", "high", "low", "close"]].apply(pd.to_numeric, errors="coerce")
            if not np.isfinite(prices.to_numpy()).all() or (prices <= 0).any().any():
                item["errors"].append("Missing, non-finite or non-positive OHLC prices. Repair source data.")
            if ((prices.high < prices[["open", "close", "low"]].max(axis=1)) |
                    (prices.low > prices[["open", "close", "high"]].min(axis=1))).any():
                item["errors"].append("Invalid OHLC envelope. Repair source data.")
            volume = pd.to_numeric(rows.volume, errors="coerce")
            item["missing_volume_bars"] = int(volume.isna().sum())
            item["invalid_volume_bars"] = int(((~np.isfinite(volume) & volume.notna()) | (volume < 0)).sum())
            item["zero_volume_bars"] = int((volume == 0).sum())
            if item["missing_volume_bars"] or item["invalid_volume_bars"]:
                item["errors"].append("Missing or invalid volume. Import observed volume; never fill it with invented liquidity.")
            if asset_class == "equity" and not (np.isfinite(volume) & (volume > 0)).any():
                item["errors"].append("No positive-volume bars. Fetch IBKR TRADES (LAST), or import observed trade volume; MIDPOINT has no trade volume.")
            elif item["zero_volume_bars"]:
                item["warnings"].append("Partial zero-volume coverage: these bars supply no equity execution liquidity.")
            if asset_class == "equity" and "unavailable_midpoint" in item["volume_basis"].split(", "):
                item["errors"].append("MIDPOINT volume is unavailable; use TRADES for execution studies.")
            if any(item[key] == "unknown" or "unknown" in item[key].split(", ") for key in METADATA):
                item["warnings"].append("Source/session/price or volume semantics are undeclared. Verify against the supplier; simulation assumptions do not certify the CSV.")
            if item["possible_intraday_gaps"]:
                item["warnings"].append("Possible intraday gaps detected. Exact missing-bar coverage is unavailable without a source calendar.")
        report["tickers"].append(item)
        report["errors"].extend(f"{ticker}: {message}" for message in item["errors"])
        report["warnings"].extend(f"{ticker}: {message}" for message in item["warnings"])
    report["execution_eligible"] = not report["errors"]
    report["coverage_note"] = "Cadence is observed spacing, not a certified bar width. Missing-bar coverage is unknown without source calendar metadata. Naive timestamps are interpreted as UTC."
    return report


def inspect_csv(csv_path: str | Path, tickers: list[str], asset_class: str) -> dict:
    path = Path(csv_path)
    try:
        content = path.read_bytes()
        frame = pd.read_csv(io.BytesIO(content))
    except (OSError, ValueError, pd.errors.ParserError) as exc:
        return {"execution_eligible": False, "asset_class": asset_class, "tickers": [],
                "errors": [f"Cannot read data CSV: {exc}. Select a valid CSV or fetch observed bars."],
                "warnings": [], "csv": str(path), "sha256": None}
    from quant.data.provenance import describe
    report = {**inspect_frame(frame, tickers, asset_class), "csv": str(path),
              "sha256": hashlib.sha256(content).hexdigest(),
              "provenance": describe(path, content)}
    if report["provenance"]["status"] == "integrity_error":
        report["execution_eligible"] = False
        report["errors"].append("Dataset provenance integrity error: " + report["provenance"]["error"])
    return report


def require_execution_data(report: dict) -> dict:
    if not report["execution_eligible"]:
        raise ValueError("Research preflight blocked execution: " + " ".join(report["errors"]))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True)
    parser.add_argument("--tickers", nargs="+", required=True)
    parser.add_argument("--asset-class", choices=("crypto", "equity"), default="equity")
    parser.add_argument("--repair", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7497)
    parser.add_argument("--client-id", type=int, default=71)
    parser.add_argument("--years", type=int, default=5)
    parser.add_argument("--bar-hours", type=int, default=4)
    parser.add_argument("--include-extended-hours", action="store_true")
    parser.add_argument("--progress-path")
    args = parser.parse_args()
    def progress(**values):
        if args.progress_path:
            path = Path(args.progress_path)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(values, allow_nan=False))
            temporary.replace(path)
    try:
        if args.repair:
            from quant.data.ibkr_fetch import replace_bars
            progress(phase="fetching", phase_label="Fetching observed IBKR trade bars; original CSV retained until validation passes")
            replace_bars(args.csv, args.tickers, args.asset_class, args.years,
                         args.host, args.port, args.client_id, bar_hours=args.bar_hours,
                         price_type="LAST", include_extended_hours=args.include_extended_hours)
        report = inspect_csv(args.csv, args.tickers, args.asset_class)
        print(json.dumps(report, indent=2, allow_nan=False))
        progress(phase="completed", phase_label="Data review ready", report=report)
        if args.repair:
            require_execution_data(report)
    except (Exception, SystemExit) as exc:
        progress(phase="failed", phase_label=str(exc), report=inspect_csv(args.csv, args.tickers, args.asset_class))
        raise


if __name__ == "__main__":
    main()
