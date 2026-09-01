"""Objective, resettable paper-trading validation campaigns."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path

from quant.ops.state import OperationsStore


@dataclass(frozen=True)
class CampaignPolicy:
    minimum_clean_days: int = 20
    minimum_runtime_hours: float = 120.0
    minimum_healthy_fraction: float = 0.995
    minimum_fills: int = 20
    maximum_rejections: int = 0
    maximum_drawdown_pct: float = 10.0


@dataclass(frozen=True)
class CampaignReport:
    campaign_id: str
    ready: bool
    reasons: tuple[str, ...]
    observation_count: int
    clean_days: int
    runtime_hours: float
    healthy_fraction: float
    fill_count: int
    rejection_count: int
    maximum_drawdown_pct: float
    clean_since: str | None
    evaluated_at: str

    def as_dict(self) -> dict:
        return asdict(self)


def evaluate_campaign(store: OperationsStore, campaign_id: str, policy: CampaignPolicy = CampaignPolicy()) -> CampaignReport:
    rows = store.paper_observations(campaign_id)
    evaluated_at = datetime.now(timezone.utc).isoformat()
    if not rows:
        return CampaignReport(campaign_id, False, ("no paper observations",), 0, 0, 0.0, 0.0, 0, 0, 0.0, None, evaluated_at)
    last_unhealthy = max((index for index, row in enumerate(rows) if not row["healthy"]), default=-1)
    clean = rows[last_unhealthy + 1 :]
    clean_since = clean[0]["observed_at"] if clean else None
    timestamps = [datetime.fromisoformat(str(row["observed_at"])) for row in clean]
    runtime_hours = max((timestamps[-1] - timestamps[0]).total_seconds() / 3600.0, 0.0) if len(timestamps) >= 2 else 0.0
    clean_days = len({value.astimezone(timezone.utc).date() for value in timestamps})
    healthy_fraction = (
        sum(int(bool(row["healthy"])) for row in clean) / len(clean)
        if clean
        else 0.0
    )
    per_job: dict[str, dict[str, int]] = {}
    for row in clean:
        job = per_job.setdefault(str(row["job_id"]), {"fills": 0, "rejections": 0})
        job["fills"] = max(job["fills"], int(row["fill_count"]))
        job["rejections"] = max(job["rejections"], int(row["rejection_count"]))
    fill_count = sum(value["fills"] for value in per_job.values())
    rejection_count = sum(value["rejections"] for value in per_job.values())
    max_drawdown = max((float(row["drawdown_pct"] or 0.0) for row in clean), default=0.0)
    reconciliation_bad = any(
        str(row["reconciliation_state"]) not in {"STRATEGY_CACHE_RECONCILED", "BROKER_RECONCILED"}
        for row in clean
    )
    reasons: list[str] = []
    if clean_days < policy.minimum_clean_days:
        reasons.append(f"clean days {clean_days} < {policy.minimum_clean_days}")
    if runtime_hours < policy.minimum_runtime_hours:
        reasons.append(f"runtime {runtime_hours:.2f}h < {policy.minimum_runtime_hours:.2f}h")
    if healthy_fraction < policy.minimum_healthy_fraction:
        reasons.append(f"healthy fraction {healthy_fraction:.5f} < {policy.minimum_healthy_fraction:.5f}")
    if fill_count < policy.minimum_fills:
        reasons.append(f"fills {fill_count} < {policy.minimum_fills}")
    if rejection_count > policy.maximum_rejections:
        reasons.append(f"rejections {rejection_count} > {policy.maximum_rejections}")
    if max_drawdown > policy.maximum_drawdown_pct:
        reasons.append(f"drawdown {max_drawdown:.4f}% > {policy.maximum_drawdown_pct:.4f}%")
    if reconciliation_bad:
        reasons.append("one or more clean-period reconciliation snapshots are uncertain")
    return CampaignReport(
        campaign_id,
        not reasons,
        tuple(reasons),
        len(rows),
        clean_days,
        runtime_hours,
        healthy_fraction,
        fill_count,
        rejection_count,
        max_drawdown,
        clean_since,
        evaluated_at,
    )


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operations-db", default="quant/jobs/operations.sqlite3")
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--minimum-clean-days", type=int, default=20)
    parser.add_argument("--minimum-runtime-hours", type=float, default=120.0)
    parser.add_argument("--minimum-fills", type=int, default=20)
    parser.add_argument("--out")
    args = parser.parse_args()
    store = OperationsStore(args.operations_db)
    report = evaluate_campaign(
        store,
        args.campaign_id,
        CampaignPolicy(
            minimum_clean_days=args.minimum_clean_days,
            minimum_runtime_hours=args.minimum_runtime_hours,
            minimum_fills=args.minimum_fills,
        ),
    )
    store.close()
    rendered = json.dumps(report.as_dict(), indent=2)
    print(rendered)
    if args.out:
        path = Path(args.out).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered + "\n")
    raise SystemExit(0 if report.ready else 2)


if __name__ == "__main__":
    main()
