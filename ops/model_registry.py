"""Immutable model artifacts and evidence-gated operator promotion."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import uuid


APPROVAL_PHRASE_PREFIX = "APPROVE MODEL"


def _canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class PromotionPolicy:
    minimum_oos_score: float = 0.0
    minimum_stressed_ratio: float = 0.0
    minimum_profit_factor: float = 1.0
    minimum_trades: int = 20
    maximum_drawdown_pct: float = 10.0
    require_clean_revision: bool = True


class ModelRegistry:
    def __init__(self, db_path: str, artifact_dir: str | None = None) -> None:
        self.db_path = str(Path(db_path).expanduser().resolve())
        self.artifact_dir = Path(artifact_dir).expanduser().resolve() if artifact_dir else Path(self.db_path).with_suffix("").with_name("model_artifacts")
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.db_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS models (
                model_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 0,
                params_path TEXT NOT NULL,
                params_sha256 TEXT NOT NULL,
                optimization_path TEXT,
                optimization_sha256 TEXT,
                source_revision TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                evidence_json TEXT NOT NULL DEFAULT '{}',
                approved_at TEXT,
                approved_by TEXT,
                supersedes_model_id TEXT
            );
            CREATE TABLE IF NOT EXISTS model_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                occurred_at TEXT NOT NULL,
                model_id TEXT NOT NULL,
                action TEXT NOT NULL,
                operator TEXT NOT NULL,
                detail_json TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS model_events_no_update
            BEFORE UPDATE ON model_events BEGIN
                SELECT RAISE(ABORT, 'model events are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS model_events_no_delete
            BEFORE DELETE ON model_events BEGIN
                SELECT RAISE(ABORT, 'model events are immutable');
            END;
            CREATE TABLE IF NOT EXISTS promotion_evaluations (
                evaluation_id TEXT PRIMARY KEY,
                model_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                eligible INTEGER NOT NULL,
                evidence_json TEXT NOT NULL,
                evidence_sha256 TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS promotion_evaluations_no_update
            BEFORE UPDATE ON promotion_evaluations BEGIN
                SELECT RAISE(ABORT, 'promotion evaluations are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS promotion_evaluations_no_delete
            BEFORE DELETE ON promotion_evaluations BEGIN
                SELECT RAISE(ABORT, 'promotion evaluations are immutable');
            END;
            """
        )
        self.connection.commit()

    @staticmethod
    def _hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _revision() -> str:
        repository = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return "UNKNOWN"
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repository,
            capture_output=True,
            text=True,
            check=False,
        )
        suffix = "-DIRTY" if status.returncode != 0 or status.stdout.strip() else ""
        return result.stdout.strip() + suffix

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def register(self, params_path: str, *, optimization_path: str | None = None, metadata: dict | None = None) -> dict:
        source = Path(params_path).expanduser().resolve()
        payload = json.loads(source.read_text())
        if not isinstance(payload, dict) or not isinstance(payload.get("params", payload), dict):
            raise ValueError("params artifact must contain a JSON object")
        model_id = f"model_{uuid.uuid4().hex[:12]}"
        destination = self.artifact_dir / f"{model_id}_params.json"
        shutil.copy2(source, destination)
        destination.chmod(0o444)
        optimize_destination: Path | None = None
        if optimization_path:
            optimize_source = Path(optimization_path).expanduser().resolve()
            optimize_destination = self.artifact_dir / f"{model_id}_optimization.json"
            shutil.copy2(optimize_source, optimize_destination)
            optimize_destination.chmod(0o444)
        combined_metadata = {
            "asset_class": payload.get("asset_class"),
            "market_session": payload.get("market_session"),
            "bar_interval_minutes": payload.get("bar_interval_minutes"),
            "source_csv": payload.get("source_csv"),
            **(metadata or {}),
        }
        now = self._now()
        with self.connection:
            self.connection.execute(
                """INSERT INTO models
                (model_id, created_at, status, params_path, params_sha256,
                 optimization_path, optimization_sha256, source_revision, metadata_json)
                VALUES (?, ?, 'CANDIDATE', ?, ?, ?, ?, ?, ?)""",
                (
                    model_id,
                    now,
                    str(destination),
                    self._hash(destination),
                    str(optimize_destination) if optimize_destination else None,
                    self._hash(optimize_destination) if optimize_destination else None,
                    self._revision(),
                    json.dumps(combined_metadata, sort_keys=True),
                ),
            )
            self._event(model_id, "REGISTERED", "system", combined_metadata)
        return self.get(model_id)

    def _event(self, model_id: str, action: str, operator: str, detail: dict) -> None:
        self.connection.execute(
            "INSERT INTO model_events(occurred_at, model_id, action, operator, detail_json) VALUES (?, ?, ?, ?, ?)",
            (self._now(), model_id, action, operator, json.dumps(detail, sort_keys=True)),
        )

    def get(self, model_id: str) -> dict:
        row = self.connection.execute("SELECT * FROM models WHERE model_id = ?", (model_id,)).fetchone()
        if row is None:
            raise KeyError(model_id)
        value = dict(row)
        value["active"] = bool(value["active"])
        value["metadata"] = json.loads(value.pop("metadata_json"))
        value["evidence"] = json.loads(value.pop("evidence_json"))
        return value

    def list(self) -> list[dict]:
        rows = self.connection.execute("SELECT model_id FROM models ORDER BY created_at DESC")
        return [self.get(str(row[0])) for row in rows]

    def verify_artifacts(self, model_id: str) -> tuple[bool, str]:
        model = self.get(model_id)
        for path_key, digest_key in (("params_path", "params_sha256"), ("optimization_path", "optimization_sha256")):
            if not model[path_key]:
                continue
            path = Path(model[path_key])
            if not path.exists() or self._hash(path) != model[digest_key]:
                return False, f"artifact verification failed: {path}"
        return True, "ok"

    def evaluate(self, model_id: str, campaign_report: dict, policy: PromotionPolicy = PromotionPolicy()) -> dict:
        model = self.get(model_id)
        optimization = {}
        if model["optimization_path"]:
            optimization = json.loads(Path(model["optimization_path"]).read_text())
        else:
            optimization = json.loads(Path(model["params_path"]).read_text())
        final = optimization.get("final_test") or {}
        metrics = optimization.get("oos_metrics") or {}
        score_raw = optimization.get("oos_score", final.get("stability_adjusted_score"))
        stressed_raw = final.get("stressed_ratio", optimization.get("stressed_ratio"))
        profit_factor_raw = metrics.get("profit_factor")
        trades_raw = metrics.get("total_trades", final.get("trades"))
        drawdown_raw = metrics.get("max_drawdown_pct")
        score = float(score_raw) if score_raw is not None else None
        stressed = float(stressed_raw) if stressed_raw is not None else None
        profit_factor = float(profit_factor_raw) if profit_factor_raw is not None else None
        trades = int(trades_raw) if trades_raw is not None else None
        drawdown = float(drawdown_raw) if drawdown_raw is not None else None
        reasons: list[str] = []
        if score is None:
            reasons.append("OOS score evidence is missing")
        elif score < policy.minimum_oos_score:
            reasons.append(f"OOS score {score:.6f} < {policy.minimum_oos_score:.6f}")
        if stressed is None:
            reasons.append("stressed-ratio evidence is missing")
        elif stressed < policy.minimum_stressed_ratio:
            reasons.append(f"stressed ratio {stressed:.6f} < {policy.minimum_stressed_ratio:.6f}")
        if profit_factor is None:
            reasons.append("profit-factor evidence is missing")
        elif profit_factor < policy.minimum_profit_factor:
            reasons.append(f"profit factor {profit_factor:.6f} < {policy.minimum_profit_factor:.6f}")
        if trades is None:
            reasons.append("trade-count evidence is missing")
        elif trades < policy.minimum_trades:
            reasons.append(f"trades {trades} < {policy.minimum_trades}")
        if drawdown is None:
            reasons.append("drawdown evidence is missing")
        elif drawdown > policy.maximum_drawdown_pct:
            reasons.append(f"drawdown {drawdown:.6f}% > {policy.maximum_drawdown_pct:.6f}%")
        if not campaign_report.get("ready", False):
            reasons.append("paper validation campaign is not ready")
        if policy.require_clean_revision and (
            model["source_revision"] == "UNKNOWN"
            or model["source_revision"].endswith("-DIRTY")
        ):
            reasons.append("source revision is unknown or contains uncommitted changes")
        artifacts_ok, artifact_detail = self.verify_artifacts(model_id)
        if not artifacts_ok:
            reasons.append(artifact_detail)
        evaluation = {
            "evaluation_id": uuid.uuid4().hex,
            "model_id": model_id,
            "eligible": not reasons,
            "reasons": reasons,
            "offline": {"oos_score": score, "stressed_ratio": stressed, "profit_factor": profit_factor, "trades": trades, "max_drawdown_pct": drawdown},
            "paper": campaign_report,
            "policy": policy.__dict__,
        }
        rendered = _canonical(evaluation)
        with self.connection:
            self.connection.execute(
                "INSERT INTO promotion_evaluations(evaluation_id, model_id, created_at, eligible, evidence_json, evidence_sha256) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    evaluation["evaluation_id"],
                    model_id,
                    self._now(),
                    int(evaluation["eligible"]),
                    rendered,
                    hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
                ),
            )
            self._event(model_id, "EVALUATED", "system", {"evaluation_id": evaluation["evaluation_id"], "eligible": evaluation["eligible"]})
        return evaluation

    def approve(self, model_id: str, *, evidence: dict, operator: str, confirmation: str) -> dict:
        expected = f"{APPROVAL_PHRASE_PREFIX} {model_id} FOR LIVE"
        if confirmation != expected:
            raise ValueError(f"confirmation must exactly equal {expected!r}")
        if not operator.strip():
            raise ValueError("operator identity is required")
        evaluation_id = str(evidence.get("evaluation_id", ""))
        evaluation = self.connection.execute(
            "SELECT * FROM promotion_evaluations WHERE evaluation_id = ? AND model_id = ?",
            (evaluation_id, model_id),
        ).fetchone()
        rendered = _canonical(evidence)
        if (
            evaluation is None
            or not bool(evaluation["eligible"])
            or hashlib.sha256(rendered.encode("utf-8")).hexdigest()
            != evaluation["evidence_sha256"]
        ):
            raise ValueError("promotion evidence is missing, ineligible, or has been modified")
        artifacts_ok, detail = self.verify_artifacts(model_id)
        if not artifacts_ok:
            raise ValueError(detail)
        previous = self.connection.execute("SELECT model_id FROM models WHERE active = 1").fetchone()
        now = self._now()
        with self.connection:
            self.connection.execute("UPDATE models SET active = 0 WHERE active = 1")
            updated = self.connection.execute(
                "UPDATE models SET status = 'APPROVED', active = 1, evidence_json = ?, approved_at = ?, approved_by = ?, supersedes_model_id = ? WHERE model_id = ? AND status = 'CANDIDATE'",
                (json.dumps(evidence, sort_keys=True), now, operator, previous[0] if previous else None, model_id),
            ).rowcount
            if not updated:
                raise ValueError("only candidate or approved models can be promoted")
            self._event(model_id, "APPROVED", operator, {"supersedes": previous[0] if previous else None})
        return self.get(model_id)

    def rollback(self, *, operator: str, reason: str) -> dict:
        current = self.connection.execute("SELECT * FROM models WHERE active = 1").fetchone()
        if current is None or not current["supersedes_model_id"]:
            raise ValueError("no prior approved model is available for rollback")
        target = str(current["supersedes_model_id"])
        with self.connection:
            self.connection.execute("UPDATE models SET active = 0, status = 'ROLLED_BACK' WHERE model_id = ?", (current["model_id"],))
            self.connection.execute("UPDATE models SET active = 1, status = 'APPROVED' WHERE model_id = ?", (target,))
            self._event(str(current["model_id"]), "ROLLED_BACK", operator, {"reason": reason, "restored": target})
            self._event(target, "RESTORED", operator, {"reason": reason})
        return self.get(target)

    def reject(self, model_id: str, *, operator: str, reason: str) -> dict:
        if not operator.strip() or not reason.strip():
            raise ValueError("operator and reason are required")
        with self.connection:
            updated = self.connection.execute(
                "UPDATE models SET status = 'REJECTED', active = 0 WHERE model_id = ? AND status = 'CANDIDATE'",
                (model_id,),
            ).rowcount
            if not updated:
                raise ValueError("only candidate models can be rejected")
            self._event(model_id, "REJECTED", operator, {"reason": reason})
        return self.get(model_id)

    def params_path(self, model_id: str, *, require_approved: bool = False) -> str:
        model = self.get(model_id)
        if require_approved and (model["status"] != "APPROVED" or not model["active"]):
            raise ValueError(f"model {model_id} is not the active approved model")
        ok, detail = self.verify_artifacts(model_id)
        if not ok:
            raise ValueError(detail)
        return str(model["params_path"])

    def close(self) -> None:
        self.connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", default="quant/models/registry.sqlite3")
    subparsers = parser.add_subparsers(dest="command", required=True)
    register = subparsers.add_parser("register")
    register.add_argument("--params", required=True)
    register.add_argument("--optimization")
    listing = subparsers.add_parser("list")
    evaluate = subparsers.add_parser("evaluate")
    evaluate.add_argument("--model-id", required=True)
    evaluate.add_argument("--campaign-report", required=True)
    approve = subparsers.add_parser("approve")
    approve.add_argument("--model-id", required=True)
    approve.add_argument("--evidence", required=True)
    approve.add_argument("--operator", required=True)
    approve.add_argument("--confirm", required=True)
    rollback = subparsers.add_parser("rollback")
    rollback.add_argument("--operator", required=True)
    rollback.add_argument("--reason", required=True)
    reject = subparsers.add_parser("reject")
    reject.add_argument("--model-id", required=True)
    reject.add_argument("--operator", required=True)
    reject.add_argument("--reason", required=True)
    args = parser.parse_args()
    registry = ModelRegistry(args.registry)
    try:
        if args.command == "register":
            result = registry.register(args.params, optimization_path=args.optimization)
        elif args.command == "list":
            result = registry.list()
        elif args.command == "evaluate":
            result = registry.evaluate(args.model_id, json.loads(Path(args.campaign_report).read_text()))
        elif args.command == "approve":
            result = registry.approve(args.model_id, evidence=json.loads(Path(args.evidence).read_text()), operator=args.operator, confirmation=args.confirm)
        elif args.command == "rollback":
            result = registry.rollback(operator=args.operator, reason=args.reason)
        else:
            result = registry.reject(args.model_id, operator=args.operator, reason=args.reason)
        print(json.dumps(result, indent=2))
    finally:
        registry.close()


if __name__ == "__main__":
    main()
