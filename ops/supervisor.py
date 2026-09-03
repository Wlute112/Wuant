"""Independent paper/live risk supervisor and durable safety-command issuer."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import signal
import time

from quant.api.jobs import RedisJobStore
from quant.ops.alerts import Alert, AlertDispatcher, sinks_from_environment
from quant.ops.state import OperationsStore
from quant.run.telemetry import load_telemetry


TERMINAL_JOB_STATES = {"completed", "failed", "cancelled"}


@dataclass(frozen=True)
class SupervisorDecision:
    healthy: bool
    action: str | None = None
    code: str = "HEALTHY"
    reason: str = "all independently checked rails are healthy"
    severity: str = "INFO"


def evaluate_snapshot(payload: dict | None, *, age_seconds: float | None, max_age_seconds: float) -> SupervisorDecision:
    if payload is None:
        return SupervisorDecision(False, "FREEZE_ENTRIES", "TELEMETRY_MISSING", "strategy telemetry is missing", "CRITICAL")
    if age_seconds is None or age_seconds > max_age_seconds:
        return SupervisorDecision(False, "FREEZE_ENTRIES", "TELEMETRY_STALE", f"strategy telemetry age exceeds {max_age_seconds:.1f}s", "CRITICAL")
    risk = payload.get("risk") or {}
    rails = risk.get("rails") or {}
    drawdown = float(risk.get("drawdown_pct") or 0.0)
    kill_limit = float(rails.get("kill_switch_pct") or 10.0)
    if risk.get("kill_switch_engaged") or drawdown >= kill_limit:
        return SupervisorDecision(False, "KILL", "DRAWDOWN_KILL", f"drawdown {drawdown:.4f}% reached kill rail {kill_limit:.4f}%", "CRITICAL")
    leverage = float(risk.get("gross_leverage") or 0.0)
    leverage_limit = float(rails.get("leverage_max") or 1.0)
    if leverage > leverage_limit + 1e-9:
        return SupervisorDecision(False, "FLATTEN", "LEVERAGE_BREACH", f"gross leverage {leverage:.6f} exceeds {leverage_limit:.6f}", "CRITICAL")
    daily_pnl = float(risk.get("daily_pnl_pct") or 0.0)
    daily_limit = float(rails.get("daily_loss_limit_pct") or 2.0)
    if daily_pnl <= -daily_limit:
        return SupervisorDecision(False, "FLATTEN", "DAILY_LOSS_BREACH", f"daily PnL {daily_pnl:.4f}% breached {-daily_limit:.4f}%", "CRITICAL")
    if str(risk.get("execution_state")) == "UNCERTAIN":
        return SupervisorDecision(False, "FLATTEN", "EXECUTION_UNCERTAIN", "strategy reports uncertain broker execution state", "CRITICAL")
    reconciliation = str(risk.get("reconciliation_state") or "UNKNOWN")
    if reconciliation in {"UNKNOWN", "UNCERTAIN", "NOT_STARTED"}:
        return SupervisorDecision(False, "FREEZE_ENTRIES", "RECONCILIATION_UNCERTAIN", f"broker reconciliation state is {reconciliation}", "CRITICAL")
    data_quality = risk.get("data_quality") or {}
    if data_quality and not bool(data_quality.get("healthy", False)):
        return SupervisorDecision(False, "FREEZE_ENTRIES", "DATA_QUALITY_FAILED", "strategy reports a critical market-data quality issue", "CRITICAL")
    short_controls = risk.get("short_controls") or {}
    if short_controls.get("enabled") and not bool(short_controls.get("healthy", False)):
        positions = payload.get("positions") or []
        has_short = any(str(position.get("side", "")).upper() == "SHORT" for position in positions)
        grace_active = short_controls.get("state") == "RECALL_GRACE"
        active_breach = short_controls.get("state") == "ACTIVE_BREACH"
        return SupervisorDecision(
            False,
            "FLATTEN" if has_short and active_breach else "FREEZE_ENTRIES",
            "SHORT_CONTROL_FAILED",
            (
                "an existing short is inside the configured control-loss grace period"
                if has_short and grace_active
                else "an existing short exceeded the configured control-loss grace period"
                if has_short and active_breach
                else "required borrow, fee, margin, or short-sale-restriction controls are not healthy"
            ),
            "CRITICAL",
        )
    session = risk.get("session") or {}
    data_age = session.get("data_age_seconds")
    if data_age is not None and float(data_age) > max_age_seconds:
        return SupervisorDecision(False, "FREEZE_ENTRIES", "MARKET_DATA_STALE", f"market data age is {float(data_age):.1f}s", "CRITICAL")
    return SupervisorDecision(True)


class RiskSupervisor:
    def __init__(
        self,
        *,
        operations_db: str,
        telemetry_path: str,
        execution_job_id: str,
        strategy_target: str,
        component_id: str,
        campaign_id: str,
        max_age_seconds: float = 20.0,
        startup_grace_seconds: float = 45.0,
        poll_seconds: float = 2.0,
        redis_prefix: str | None = None,
        news_component: str = "",
        observation_seconds: float = 60.0,
    ) -> None:
        self.store = OperationsStore(operations_db)
        self.telemetry_path = telemetry_path
        self.execution_job_id = execution_job_id
        self.strategy_target = strategy_target
        self.component_id = component_id
        self.campaign_id = campaign_id
        self.news_component = news_component
        self.observation_seconds = max(5.0, float(observation_seconds))
        self._last_observation_at = 0.0
        self._last_observation_healthy: bool | None = None
        self.max_age_seconds = float(max_age_seconds)
        self.startup_grace_seconds = float(startup_grace_seconds)
        self.poll_seconds = max(0.2, float(poll_seconds))
        self.started = time.monotonic()
        self.stop_requested = False
        self.job_store = RedisJobStore(prefix=redis_prefix)
        self.alerts = AlertDispatcher(
            self.store,
            sinks_from_environment(f"{operations_db}.alerts.jsonl"),
        )

    def request_stop(self, *_args) -> None:
        self.stop_requested = True

    @staticmethod
    def _telemetry_age(payload: dict | None) -> float | None:
        if not payload or not payload.get("as_of"):
            return None
        try:
            observed = datetime.fromisoformat(str(payload["as_of"]))
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=timezone.utc)
            return max((datetime.now(timezone.utc) - observed.astimezone(timezone.utc)).total_seconds(), 0.0)
        except (TypeError, ValueError):
            return None

    def step(self) -> bool:
        job = self.job_store.get(self.execution_job_id)
        payload = None
        if job is None:
            decision = SupervisorDecision(False, "FREEZE_ENTRIES", "EXECUTION_JOB_MISSING", "execution job disappeared from durable registry", "CRITICAL")
            terminal = True
        else:
            terminal = job.get("status") in TERMINAL_JOB_STATES
            payload = load_telemetry(self.telemetry_path)
            decision = (
                SupervisorDecision(True, code="EXECUTION_TERMINAL", reason="execution job is terminal")
                if terminal
                else evaluate_snapshot(
                    payload,
                    age_seconds=self._telemetry_age(payload),
                    max_age_seconds=self.max_age_seconds,
                )
            )
            model = (payload or {}).get("model") or {}
            if (
                not terminal
                and decision.healthy
                and model.get("use_news_features")
                and self.news_component
            ):
                heartbeat = self.store.get_heartbeat(self.news_component)
                heartbeat_age = None
                if heartbeat is not None:
                    try:
                        observed = datetime.fromisoformat(str(heartbeat["observed_at"]))
                        if observed.tzinfo is None:
                            observed = observed.replace(tzinfo=timezone.utc)
                        heartbeat_age = max(
                            (
                                datetime.now(timezone.utc)
                                - observed.astimezone(timezone.utc)
                            ).total_seconds(),
                            0.0,
                        )
                    except (TypeError, ValueError):
                        heartbeat_age = None
                if (
                    heartbeat is None
                    or heartbeat.get("status") not in {"HEALTHY", "DEGRADED"}
                    or heartbeat_age is None
                    or heartbeat_age > self.max_age_seconds
                ):
                    decision = SupervisorDecision(
                        False,
                        "FREEZE_ENTRIES",
                        "NEWS_SERVICE_UNHEALTHY",
                        "required news service heartbeat is missing, stale, or degraded",
                        "CRITICAL",
                    )
                elif heartbeat.get("status") == "DEGRADED":
                    self.alerts.dispatch(
                        Alert(
                            "NEWS_SERVICE_DEGRADED",
                            "WARNING",
                            "news service is operating with reduced source coverage",
                            {"component": self.news_component},
                            self.component_id,
                        ),
                        dedupe_key=f"{self.execution_job_id}:news-degraded",
                    )
        in_grace = time.monotonic() - self.started < self.startup_grace_seconds
        effective_healthy = decision.healthy or (
            in_grace
            and decision.code
            in {"TELEMETRY_MISSING", "TELEMETRY_STALE", "NEWS_SERVICE_UNHEALTHY"}
        )
        observation_now = time.monotonic()
        if (
            not terminal
            and payload is not None
            and payload.get("mode") == "paper"
            and (
                observation_now - self._last_observation_at
                >= self.observation_seconds
                or self._last_observation_healthy is None
                or self._last_observation_healthy != effective_healthy
            )
        ):
            self.store.record_paper_observation(
                self.campaign_id,
                self.execution_job_id,
                payload,
                healthy=effective_healthy,
            )
            self._last_observation_at = observation_now
            self._last_observation_healthy = effective_healthy
        self.store.heartbeat(
            self.component_id,
            self.component_id,
            status="HEALTHY" if effective_healthy else "DEGRADED",
            details={"execution_job_id": self.execution_job_id, "decision": decision.code, "in_startup_grace": in_grace},
        )
        state_key = f"supervisor:last-intervention:{self.execution_job_id}"
        if not effective_healthy and decision.action:
            command = self.store.request_command(
                self.strategy_target,
                decision.action,
                decision.reason,
                payload={"code": decision.code, "supervisor": self.component_id},
                correlation_id=f"supervisor:{self.execution_job_id}:{decision.code}",
                dedupe_key=decision.code,
            )
            self.store.set_state(
                state_key,
                {"action": decision.action, "code": decision.code, "command_id": command.command_id},
            )
            self.alerts.dispatch(
                Alert(decision.code, decision.severity, decision.reason, {"command_id": command.command_id, "job_id": self.execution_job_id}, self.component_id),
                dedupe_key=f"{self.execution_job_id}:{decision.code}",
            )
        elif effective_healthy and not terminal:
            previous = self.store.get_state(state_key, {})
            if previous.get("action") == "FREEZE_ENTRIES":
                self.store.request_command(
                    self.strategy_target,
                    "RESUME_ENTRIES",
                    f"Supervisor verified recovery from {previous.get('code', 'transient fault')}",
                    payload={"supervisor": self.component_id, "recovered": previous},
                    correlation_id=f"supervisor:{self.execution_job_id}:recovery",
                    dedupe_key=f"auto-recovery:{previous.get('command_id', '')}",
                )
                self.store.set_state(state_key, {"action": "RECOVERY_REQUESTED"})
        return not terminal

    def run(self) -> int:
        self.store.append_event(self.component_id, "SUPERVISOR_STARTED", {"execution_job_id": self.execution_job_id})
        try:
            while not self.stop_requested and self.step():
                time.sleep(self.poll_seconds)
            return 0
        finally:
            self.store.heartbeat(self.component_id, self.component_id, status="STOPPED", details={"execution_job_id": self.execution_job_id})
            self.store.append_event(self.component_id, "SUPERVISOR_STOPPED", {"execution_job_id": self.execution_job_id})
            self.store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operations-db", required=True)
    parser.add_argument("--telemetry-path", required=True)
    parser.add_argument("--execution-job-id", required=True)
    parser.add_argument("--strategy-target", required=True)
    parser.add_argument("--component-id", required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--max-age-seconds", type=float, default=20.0)
    parser.add_argument("--startup-grace-seconds", type=float, default=45.0)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--redis-prefix", default=None)
    parser.add_argument("--news-component", default="")
    parser.add_argument("--observation-seconds", type=float, default=60.0)
    args = parser.parse_args()
    supervisor = RiskSupervisor(**vars(args))
    signal.signal(signal.SIGINT, supervisor.request_stop)
    signal.signal(signal.SIGTERM, supervisor.request_stop)
    raise SystemExit(supervisor.run())


if __name__ == "__main__":
    main()
