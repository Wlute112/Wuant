"""Deterministic bar validation and market-data freshness gates."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import math
from typing import Iterable


@dataclass(frozen=True)
class BarQualityIssue:
    code: str
    severity: str
    instrument_id: str
    detail: str


@dataclass(frozen=True)
class BarQualityReport:
    accepted: bool
    issues: tuple[BarQualityIssue, ...] = ()

    @property
    def critical(self) -> bool:
        return any(issue.severity == "CRITICAL" for issue in self.issues)


@dataclass(frozen=True)
class MarketDataFreshnessReport:
    passed: bool
    checked_at: str
    ages_seconds: dict[str, float | None]
    stale_instruments: tuple[str, ...] = ()
    missing_instruments: tuple[str, ...] = ()
    clock_skew_instruments: tuple[str, ...] = ()
    details: dict = field(default_factory=dict)


class BarQualityGate:
    """Stateful validation for a completed OHLCV stream.

    Exact duplicate bars are ignored idempotently. A changed bar at an already
    observed timestamp is a revision and fails closed. Gaps in a continuous
    market are critical; gaps in session-based markets are reported as warnings
    because weekends, holidays, and overnight closures are expected.
    """

    def __init__(
        self,
        *,
        expected_interval_seconds: float,
        continuous_market: bool,
        max_gap_intervals: float = 3.0,
    ) -> None:
        if expected_interval_seconds <= 0:
            raise ValueError("expected_interval_seconds must be positive")
        self.expected_interval_seconds = float(expected_interval_seconds)
        self.continuous_market = bool(continuous_market)
        self.max_gap_intervals = max(float(max_gap_intervals), 1.0)
        self._last_timestamp_ns: dict[str, int] = {}
        self._fingerprints: dict[str, tuple[int, tuple[float, ...]]] = {}

    def validate(
        self,
        instrument_id: str,
        *,
        timestamp_ns: int,
        open_price: float,
        high_price: float,
        low_price: float,
        close_price: float,
        volume: float,
    ) -> BarQualityReport:
        instrument = str(instrument_id)
        values = tuple(
            float(value)
            for value in (open_price, high_price, low_price, close_price, volume)
        )
        issues: list[BarQualityIssue] = []
        prices = values[:4]
        if not all(math.isfinite(value) for value in values):
            issues.append(self._issue("NON_FINITE", "CRITICAL", instrument, "OHLCV contains NaN or infinity"))
        if any(value <= 0 for value in prices):
            issues.append(self._issue("NON_POSITIVE_PRICE", "CRITICAL", instrument, "OHLC prices must be positive"))
        if math.isfinite(values[4]) and values[4] < 0:
            issues.append(self._issue("NEGATIVE_VOLUME", "CRITICAL", instrument, "volume cannot be negative"))
        if all(math.isfinite(value) for value in prices):
            if values[1] < max(values[0], values[3]) or values[2] > min(values[0], values[3]) or values[1] < values[2]:
                issues.append(self._issue("INCOHERENT_OHLC", "CRITICAL", instrument, "high/low do not contain open and close"))

        prior = self._fingerprints.get(instrument)
        fingerprint = (int(timestamp_ns), values)
        if prior is not None and timestamp_ns == prior[0]:
            if fingerprint == prior:
                return BarQualityReport(False, (self._issue("DUPLICATE", "INFO", instrument, "exact duplicate bar ignored"),))
            issues.append(self._issue("REVISED_BAR", "CRITICAL", instrument, "bar changed at an accepted timestamp"))
        elif prior is not None and timestamp_ns < prior[0]:
            issues.append(self._issue("OUT_OF_ORDER", "CRITICAL", instrument, "bar timestamp moved backwards"))
        elif prior is not None:
            elapsed = (timestamp_ns - prior[0]) / 1_000_000_000
            threshold = self.expected_interval_seconds * self.max_gap_intervals
            if elapsed > threshold:
                issues.append(
                    self._issue(
                        "DATA_GAP",
                        "CRITICAL" if self.continuous_market else "WARNING",
                        instrument,
                        f"{elapsed:.1f}s since prior bar exceeds {threshold:.1f}s",
                    )
                )

        critical = any(issue.severity == "CRITICAL" for issue in issues)
        gap_only = critical and all(
            issue.severity != "CRITICAL" or issue.code == "DATA_GAP"
            for issue in issues
        )
        if not critical or gap_only:
            self._last_timestamp_ns[instrument] = int(timestamp_ns)
            self._fingerprints[instrument] = fingerprint
        return BarQualityReport(not critical, tuple(issues))

    @staticmethod
    def _issue(code: str, severity: str, instrument: str, detail: str) -> BarQualityIssue:
        return BarQualityIssue(code, severity, instrument, detail)

    def snapshot(self) -> dict:
        return {
            "last_timestamp_ns": dict(self._last_timestamp_ns),
            "fingerprints": {
                key: [timestamp, list(values)]
                for key, (timestamp, values) in self._fingerprints.items()
            },
        }

    def restore(self, state: dict) -> None:
        self._last_timestamp_ns = {
            str(key): int(value)
            for key, value in (state.get("last_timestamp_ns") or {}).items()
        }
        restored: dict[str, tuple[int, tuple[float, ...]]] = {}
        for key, raw in (state.get("fingerprints") or {}).items():
            if isinstance(raw, (list, tuple)) and len(raw) == 2:
                restored[str(key)] = (int(raw[0]), tuple(float(value) for value in raw[1]))
        self._fingerprints = restored


def verify_freshness(
    instruments: Iterable[str],
    latest_timestamp_ns: dict[str, int],
    *,
    max_age_seconds: float,
    now: datetime | None = None,
    session_open: dict[str, bool] | None = None,
    max_clock_skew_seconds: float = 5.0,
) -> MarketDataFreshnessReport:
    """Verify feed coverage, age, and future timestamp/clock skew."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current_ns = int(current.timestamp() * 1_000_000_000)
    ages: dict[str, float | None] = {}
    stale: list[str] = []
    missing: list[str] = []
    skewed: list[str] = []
    schedule = session_open or {}
    for raw in instruments:
        instrument = str(raw)
        timestamp = latest_timestamp_ns.get(instrument)
        if timestamp is None:
            ages[instrument] = None
            if schedule.get(instrument, True):
                missing.append(instrument)
            continue
        age = (current_ns - int(timestamp)) / 1_000_000_000
        ages[instrument] = age
        if age < -abs(float(max_clock_skew_seconds)):
            skewed.append(instrument)
        elif schedule.get(instrument, True) and age > float(max_age_seconds):
            stale.append(instrument)
    passed = not (stale or missing or skewed)
    return MarketDataFreshnessReport(
        passed=passed,
        checked_at=current.astimezone(timezone.utc).isoformat(),
        ages_seconds=ages,
        stale_instruments=tuple(sorted(stale)),
        missing_instruments=tuple(sorted(missing)),
        clock_skew_instruments=tuple(sorted(skewed)),
        details={"max_age_seconds": float(max_age_seconds)},
    )
