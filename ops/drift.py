"""Persistent advisory drift review. This module cannot trade, retune or promote."""
from contextlib import AbstractContextManager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from quant.ops.drift_statistics import summarize
from quant.run.drift_evidence import digest, timestamp

METRICS = {"prediction_mean": 0.002, "absolute_error": 0.002, "direction_accuracy": 0.1,
           "slippage_bps": 5.0, "reject_rate": 0.05, "feature_age_seconds": 3600.0}
LIMITATIONS = (
    "Advisory review only. No automatic model changes or promotion. "
    "95% intervals use normal mean standard errors; they are descriptive, assume independent samples, "
    "and do not correct for serial/cross-symbol dependence or multiple comparisons. "
    "Prediction errors use matured, non-overlapping targets per symbol. Slippage is adverse basis points "
    "per terminal entry order versus the last completed decision close, not an arrival quote; "
    "reject rates exclude pending orders. Missing observations are never treated as zero. "
    "Telemetry is a bounded window, not a complete broker execution archive."
)


def now():
    return datetime.now(timezone.utc).isoformat()


class Sample(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)
    id: str = Field(min_length=1, max_length=200)
    ts: str
    target_ts: str | None = None
    ticker: str = Field(min_length=1, max_length=32)
    regime: str = Field(default="Unknown", max_length=64)
    prediction: float | None = Field(default=None, ge=-100, le=100)
    actual: float | None = Field(default=None, ge=-100, le=100)
    slippage_bps: float | None = Field(default=None, ge=-1000000, le=1000000)
    rejected: Literal[0, 1] | None = None
    feature_age_seconds: float | None = Field(default=None, ge=0, le=1e9)

    @field_validator("rejected", mode="before")
    @classmethod
    def rejection_number(cls, value):
        if isinstance(value, bool):
            raise ValueError("Rejection must be 0 or 1, not boolean")
        return value

    @model_validator(mode="after")
    def dates(self):
        decision = timestamp(self.ts)
        if self.actual is not None and (self.prediction is None or self.target_ts is None):
            raise ValueError("Actual returns require prediction and target_ts")
        if self.target_ts and timestamp(self.target_ts) <= decision:
            raise ValueError("Target must follow decision time")
        return self


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    source_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    kind: Literal["research", "shadow", "paper"]
    reference: str = Field(min_length=5, max_length=1000)
    provenance: dict = Field(default_factory=dict)
    contract: dict
    as_of: str
    samples: list[Sample] = Field(max_length=10000)

    @model_validator(mode="after")
    def validate_evidence(self):
        contract = dict(self.contract)
        version = contract.pop("model_version", None)
        if version != digest(contract) or not all(k in contract for k in ("implementation", "params", "tickers", "asset_class", "bar_type", "extended_hours")):
            raise ValueError("Contract must carry a verified model_version hash and full data semantics")
        if not contract["tickers"] or contract["asset_class"] not in {"equity", "crypto"}:
            raise ValueError("Declare an asset class and ticker universe")
        tickers = contract["tickers"]
        if not isinstance(tickers, list) or any(not isinstance(t, str) for t in tickers) or len(set(tickers)) != len(tickers):
            raise ValueError("Ticker universe must be a unique list of symbols")
        if not isinstance(contract["params"], dict) or type(contract["params"].get("horizon")) is not int or not 1 <= contract["params"]["horizon"] <= 10000:
            raise ValueError("Contract requires a positive integer forecast horizon")
        as_of = timestamp(self.as_of)
        if as_of > datetime.now(timezone.utc):
            raise ValueError("Evidence is future dated")
        ids, forecasts = set(), set()
        for row in self.samples:
            if row.id in ids:
                raise ValueError("Duplicate observation IDs")
            ids.add(row.id)
            if row.prediction is not None:
                key = (row.ticker, timestamp(row.ts))
                if key in forecasts:
                    raise ValueError("Duplicate forecast decision timestamps")
                forecasts.add(key)
            if row.ticker not in contract["tickers"] or timestamp(row.target_ts or row.ts) > as_of:
                raise ValueError("Observation exceeds source time or universe")
        return self


class ReviewConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    baseline_id: str
    reason: str = Field(min_length=5, max_length=500)
    minimum_samples: int = Field(default=30, ge=2, le=10000)
    window_samples: int = Field(default=1000, ge=2, le=10000)
    stale_after_seconds: int = Field(default=300, ge=15, le=604800)
    enabled: bool = True
    thresholds: dict[str, float] = Field(default_factory=lambda: dict(METRICS))

    @model_validator(mode="after")
    def thresholds_valid(self):
        if set(self.thresholds) != set(METRICS) or any(v < 0 for v in self.thresholds.values()):
            raise ValueError("Supply nonnegative thresholds for all six metrics")
        if self.window_samples < self.minimum_samples:
            raise ValueError("Review window must be at least the minimum sample size")
        return self


class DriftStore(AbstractContextManager):
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS documents (sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT UNIQUE NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TRIGGER IF NOT EXISTS documents_no_update BEFORE UPDATE ON documents
                BEGIN SELECT RAISE(ABORT, 'Review history is immutable'); END;
            CREATE TRIGGER IF NOT EXISTS documents_no_delete BEFORE DELETE ON documents
                BEGIN SELECT RAISE(ABORT, 'Review history is immutable'); END;
            CREATE TABLE IF NOT EXISTS sources (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS observations (source_id TEXT, id TEXT, ts TEXT, payload TEXT,
                PRIMARY KEY(source_id,id));
        """)

    def __exit__(self, *args):
        self.db.close()

    def append(self, kind, payload):
        record = {**payload, "id": uuid4().hex, "recorded_at": now()}
        self.db.execute("INSERT INTO documents(id,kind,payload) VALUES (?,?,?)", (record["id"], kind, json.dumps(record, allow_nan=False)))
        return record

    def history(self, kind, limit=100):
        return [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM documents WHERE kind=? ORDER BY sequence DESC LIMIT ?", (kind, limit))]

    def config(self):
        rows = self.history("config", 1)
        return rows[0] if rows else None

    def sources(self):
        result = []
        for row in self.db.execute("SELECT payload FROM sources ORDER BY id"):
            source = json.loads(row[0])
            source["sample_count"] = self.db.execute("SELECT count(*) FROM observations WHERE source_id=?", (source["source_id"],)).fetchone()[0]
            result.append(source)
        return result

    def evidence(self, source_id):
        row = self.db.execute("SELECT payload FROM sources WHERE id=?", (source_id,)).fetchone()
        if not row:
            raise ValueError("Evidence source not found")
        result = json.loads(row[0])
        result["samples"] = [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM observations WHERE source_id=? ORDER BY ts,id", (source_id,))]
        return result

    def ingest(self, evidence, operator, *, automatic=False):
        data = Evidence.model_validate(evidence).model_dump(exclude_none=True)
        source_id = data["source_id"]
        previous = self.db.execute("SELECT payload FROM sources WHERE id=?", (source_id,)).fetchone()
        meta = {k: v for k, v in data.items() if k != "samples"}
        with self.db:
            if previous:
                old = json.loads(previous[0])
                if old["contract"] != meta["contract"] or old["kind"] != meta["kind"]:
                    raise ValueError("Source identity changed; use a new source ID and reviewed baseline")
                if not automatic:
                    if digest(self.evidence(source_id)) == digest(data):
                        return meta
                    raise ValueError("Imported evidence is immutable; choose a new source ID")
                if timestamp(meta["as_of"]) < timestamp(old["as_of"]):
                    raise ValueError("Out-of-order telemetry snapshot")
            self.db.execute("INSERT OR REPLACE INTO sources(id,payload) VALUES (?,?)", (source_id, json.dumps(meta)))
            for row in data["samples"]:
                prior = self.db.execute("SELECT payload FROM observations WHERE source_id=? AND id=?", (source_id, row["id"])).fetchone()
                merged = {**(json.loads(prior[0]) if prior else {}), **row}
                if automatic and row["id"].startswith("order:") and "slippage_bps" not in row:
                    merged.pop("slippage_bps", None)
                self.db.execute("INSERT OR REPLACE INTO observations VALUES (?,?,?,?)", (source_id, row["id"], timestamp(row["ts"]).isoformat(), json.dumps(merged)))
            # Explicit rolling retention; immutable baseline/report snapshots survive.
            self.db.execute("DELETE FROM observations WHERE source_id=? AND id NOT IN (SELECT id FROM observations WHERE source_id=? ORDER BY ts DESC,id DESC LIMIT 10000)", (source_id, source_id))
            if not automatic:
                self.append("import", {"operator": operator, "source_id": source_id, "sha256": digest(data)})
        return meta

    def baseline(self, source_id, reason, operator):
        evidence = self.evidence(source_id)
        if not evidence["samples"]:
            raise ValueError("Baseline requires observations")
        with self.db:
            return self.append("baseline", {"operator": operator, "reason": reason,
                "evidence": evidence, "sha256": digest(evidence)})

    def configure(self, config, operator):
        baseline = self.get_baseline(config.baseline_id)
        with self.db:
            return self.append("config", {**config.model_dump(), "operator": operator,
                                          "model_version": baseline["evidence"]["contract"]["model_version"]})

    def get_baseline(self, identifier):
        row = self.db.execute("SELECT payload FROM documents WHERE kind='baseline' AND id=?", (identifier,)).fetchone()
        if not row:
            raise ValueError("Baseline not found")
        return json.loads(row[0])

    def stop(self, operator):
        config = self.config()
        if not config:
            raise ValueError("No review is configured")
        return self.configure(ReviewConfig.model_validate({k: config[k] for k in ReviewConfig.model_fields} | {"enabled": False}), operator)

    def review(self, *, source_errors=None):
        config = self.config()
        if not config or not config["enabled"]:
            raise ValueError("Configure and enable review first")
        baseline = self.get_baseline(config["baseline_id"])
        reference = baseline["evidence"]
        results = []
        for meta in self.sources():
            source = self.evidence(meta["source_id"])
            result = {"source_id": source["source_id"], "kind": source["kind"], "as_of": source["as_of"],
                      "reference": source["reference"], "sha256": digest(source)}
            same_source = meta["source_id"] == reference["source_id"]
            if same_source:
                cutoff = max(timestamp(s.get("target_ts", s["ts"])) for s in reference["samples"])
                baseline_ids = {s["id"] for s in reference["samples"]}
                source["samples"] = [s for s in source["samples"] if s["id"] not in baseline_ids and timestamp(s["ts"]) >= cutoff]
                if not source["samples"]:
                    if source["kind"] != "research":
                        stale = (datetime.now(timezone.utc) - timestamp(source["as_of"])).total_seconds() > config["stale_after_seconds"]
                        result.update(status="stale" if stale else "awaiting", reason="No new observations after baseline cutoff" if not stale else "Evidence exceeds the configured freshness limit")
                        results.append(result)
                    continue
                result["baseline_cutoff"] = cutoff.isoformat()
            if source["contract"] != reference["contract"]:
                result.update(status="incompatible", reason="Model version, universe or data semantics differ")
            elif source["kind"] in {"paper", "shadow"} and (datetime.now(timezone.utc) - timestamp(source["as_of"])).total_seconds() > config["stale_after_seconds"]:
                result.update(status="stale", reason="Evidence exceeds the configured freshness limit")
            elif not same_source and source["kind"] == reference["kind"] and min((timestamp(s["ts"]) for s in source["samples"]), default=timestamp(source["as_of"])) < max(timestamp(s.get("target_ts", s["ts"])) for s in reference["samples"]):
                result.update(status="overlap", reason="Same-stage evidence overlaps the baseline period; import a later window")
            else:
                result.update(status="reviewed", groups=compare(reference["samples"], source["samples"], config))
            results.append(result)
        record = {"config_id": config["id"], "baseline_id": baseline["id"], "model_version": config["model_version"],
                  "sources": results, "source_errors": source_errors or [], "limitations": LIMITATIONS,
                  "stage_coverage": {kind: sum(r["kind"] == kind and r["status"] == "reviewed" for r in results) for kind in ("research", "shadow", "paper")}}
        # Alerts are transition based. Repeated polling does not manufacture alerts.
        states = {f"{r['source_id']}/{group}/{metric}": value["status"]
                  for r in results if r["status"] == "reviewed"
                  for group, metrics in r["groups"].items() for metric, value in metrics.items()}
        states.update({r["source_id"]: r["status"] for r in results if r["status"] != "reviewed"})
        states.update({f"collector/{e['source_id']}": "unavailable" for e in record["source_errors"]})
        prior = self.history("report", 1)
        previous = prior[0].get("states", {}) if prior and prior[0]["config_id"] == config["id"] else {}
        for key in previous:
            if key.startswith("collector/") and key not in states:
                states[key] = "recovered"
        with self.db:
            for key, status in states.items():
                if previous.get(key) != status and (status != "within_threshold" or previous.get(key) == "breach"):
                    self.append("alert", {"config_id": config["id"], "key": key, "status": status,
                        "previous": previous.get(key), "severity": "WARNING" if status == "breach" else "INFO"})
            report = self.append("report", {**record, "states": states})
        return report

    def status(self):
        return {"config": self.config(), "sources": self.sources(),
                "baselines": [{k: v for k, v in b.items() if k != "evidence"} | {
                    "source_id": b["evidence"]["source_id"], "model_version": b["evidence"]["contract"]["model_version"]}
                    for b in self.history("baseline")],
                "reports": self.history("report", 20), "alerts": self.history("alert"),
                "as_of": now(), "limitations": LIMITATIONS,
                "scheduler": "Every 15 seconds while review is enabled and the API is running"}


def _window(samples, size):
    # Non-overlapping matured target intervals per symbol; order/age-only
    # observations retain their own independent counts.
    result, ends = [], {}
    for row in sorted(samples, key=lambda r: (timestamp(r["ts"]), r["id"])):
        if row.get("prediction") is not None:
            if row.get("actual") is None:
                continue
            ticker = row["ticker"]
            if ticker in ends and timestamp(row["ts"]) < ends[ticker]:
                continue
            ends[ticker] = timestamp(row["target_ts"])
        result.append(row)
    return result[-size:]


def compare(baseline, current, config):
    baseline, current = _window(baseline, config["window_samples"]), _window(current, config["window_samples"])
    groups = {"All"} | {r["regime"] for r in baseline + current if r["regime"] != "Unknown"}
    results = {}
    for group in sorted(groups):
        a = summarize([r for r in baseline if group == "All" or r["regime"] == group], config["minimum_samples"])
        b = summarize([r for r in current if group == "All" or r["regime"] == group], config["minimum_samples"])
        metrics = {}
        for name, limit in config["thresholds"].items():
            delta = None if a[name]["mean"] is None or b[name]["mean"] is None else b[name]["mean"] - a[name]["mean"]
            ready = a[name]["status"] == b[name]["status"] == "ready"
            adverse = abs(delta) if delta is not None and name == "prediction_mean" else -delta if delta is not None and name == "direction_accuracy" else delta
            metrics[name] = {"baseline": a[name], "current": b[name], "delta": delta, "threshold": limit,
                "status": "insufficient" if not ready else "breach" if adverse > limit else "within_threshold"}
        results[group] = metrics
    return results
