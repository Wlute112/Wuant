"""Operational alert delivery with deduplication, cooldowns, and retries."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import smtplib
import time
from typing import Protocol
from urllib.request import Request, urlopen

from quant.ops.state import OperationsStore


@dataclass(frozen=True)
class Alert:
    code: str
    severity: str
    summary: str
    details: dict
    component: str
    occurred_at: str = ""

    def payload(self) -> dict:
        value = asdict(self)
        value["occurred_at"] = self.occurred_at or datetime.now(timezone.utc).isoformat()
        return value


class AlertSink(Protocol):
    name: str

    def send(self, alert: Alert) -> None: ...


class JsonlAlertSink:
    name = "jsonl"

    def __init__(self, path: str, *, max_bytes: int = 5_000_000, backups: int = 5) -> None:
        self.path = Path(path).expanduser().resolve()
        self.max_bytes = max(100_000, int(max_bytes))
        self.backups = max(1, int(backups))
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def send(self, alert: Alert) -> None:
        if self.path.exists() and self.path.stat().st_size >= self.max_bytes:
            oldest = self.path.with_name(f"{self.path.name}.{self.backups}")
            oldest.unlink(missing_ok=True)
            for index in range(self.backups - 1, 0, -1):
                source = self.path.with_name(f"{self.path.name}.{index}")
                if source.exists():
                    source.replace(self.path.with_name(f"{self.path.name}.{index + 1}"))
            self.path.replace(self.path.with_name(f"{self.path.name}.1"))
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(alert.payload(), sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


class WebhookAlertSink:
    name = "webhook"

    def __init__(self, url: str, timeout_seconds: float = 10.0) -> None:
        self.url = url
        self.timeout_seconds = timeout_seconds

    def send(self, alert: Alert) -> None:
        body = json.dumps(alert.payload(), separators=(",", ":")).encode("utf-8")
        request = Request(self.url, data=body, headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310 - operator-configured endpoint
            if not 200 <= int(response.status) < 300:
                raise RuntimeError(f"webhook returned HTTP {response.status}")


class SmtpAlertSink:
    name = "smtp"

    def __init__(self, host: str, port: int, sender: str, recipient: str) -> None:
        self.host, self.port, self.sender, self.recipient = host, int(port), sender, recipient

    def send(self, alert: Alert) -> None:
        username = os.environ.get("QUANT_ALERT_SMTP_USERNAME", "")
        password = os.environ.get("QUANT_ALERT_SMTP_PASSWORD", "")
        payload = alert.payload()
        message = (
            f"From: {self.sender}\r\nTo: {self.recipient}\r\n"
            f"Subject: [{alert.severity}] Quant {alert.code}\r\n"
            "Content-Type: application/json\r\n\r\n"
            f"{json.dumps(payload, indent=2)}"
        )
        with smtplib.SMTP(self.host, self.port, timeout=10) as smtp:
            smtp.starttls()
            if username:
                smtp.login(username, password)
            smtp.sendmail(self.sender, [self.recipient], message)


class AlertDispatcher:
    def __init__(
        self,
        store: OperationsStore,
        sinks: list[AlertSink],
        *,
        cooldown_seconds: float = 300.0,
        attempts: int = 3,
    ) -> None:
        self.store = store
        self.sinks = sinks
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self.attempts = max(1, int(attempts))

    def dispatch(self, alert: Alert, *, dedupe_key: str | None = None) -> dict[str, str]:
        key = dedupe_key or f"{alert.component}:{alert.code}"
        now = time.time()
        prior = self.store.get_state(f"alert:{key}", {})
        if now - float(prior.get("sent_at", 0.0)) < self.cooldown_seconds:
            return {"status": "suppressed"}
        outcomes: dict[str, str] = {}
        for sink in self.sinks:
            error = ""
            for attempt in range(self.attempts):
                try:
                    sink.send(alert)
                    outcomes[sink.name] = "sent"
                    break
                except Exception as exc:  # noqa: BLE001 - each sink is isolated
                    error = f"{type(exc).__name__}: {exc}"
                    if attempt + 1 < self.attempts:
                        time.sleep(min(0.25 * (2**attempt), 2.0))
            else:
                outcomes[sink.name] = error
        delivered = any(value == "sent" for value in outcomes.values())
        self.store.set_state(
            f"alert:{key}",
            {"sent_at": now if delivered else 0.0, "outcomes": outcomes},
        )
        self.store.append_event(
            alert.component,
            "ALERT_DISPATCHED" if delivered else "ALERT_DELIVERY_FAILED",
            {"alert": alert.payload(), "outcomes": outcomes},
            severity=alert.severity if delivered else "CRITICAL",
        )
        return outcomes


def sinks_from_environment(default_jsonl_path: str) -> list[AlertSink]:
    sinks: list[AlertSink] = [JsonlAlertSink(default_jsonl_path)]
    if url := os.environ.get("QUANT_ALERT_WEBHOOK_URL"):
        sinks.append(WebhookAlertSink(url))
    host = os.environ.get("QUANT_ALERT_SMTP_HOST")
    sender = os.environ.get("QUANT_ALERT_SMTP_FROM")
    recipient = os.environ.get("QUANT_ALERT_SMTP_TO")
    if host and sender and recipient:
        sinks.append(
            SmtpAlertSink(
                host,
                int(os.environ.get("QUANT_ALERT_SMTP_PORT", "587")),
                sender,
                recipient,
            )
        )
    return sinks
