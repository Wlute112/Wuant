"""Atomic paper/live model telemetry snapshots for the dashboard.

The TradingNode runs in a subprocess, so a small atomic JSON snapshot is the
most dependable local bridge to FastAPI: readers see either the previous full
snapshot or the next full snapshot, never a partially-written file.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path


class LiveTelemetryRecorder:
    def __init__(
        self,
        path: str,
        *,
        asset_class: str,
        mode: str,
        bar_type: str,
        max_points: int = 750,
        include_extended_hours: bool = False,
    ) -> None:
        self.path = Path(path)
        self.asset_class = asset_class
        self.mode = mode
        self.bar_type = bar_type
        self.max_points = max(50, int(max_points))
        self.include_extended_hours = bool(include_extended_hours)
        self.points: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=self.max_points)
        )
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.status = "running"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        ticker: str,
        point: dict,
        *,
        positions: list[dict],
        risk: dict,
        model: dict,
        flush: bool = True,
    ) -> None:
        series = self.points[ticker]
        if series and series[-1].get("ts") == point.get("ts"):
            series[-1] = point
        else:
            series.append(point)
        if flush:
            self._write(positions=positions, risk=risk, model=model)

    def stop(self, *, positions: list[dict], risk: dict, model: dict) -> None:
        self.status = "stopped"
        self._write(positions=positions, risk=risk, model=model)

    def refresh(self, *, positions: list[dict], risk: dict, model: dict) -> None:
        """Publish execution/risk changes without fabricating a market bar."""
        self._write(positions=positions, risk=risk, model=model)

    def restore_series(self, series_by_ticker: dict[str, list[dict]]) -> None:
        """Restore persisted chart context before the first new broker bar."""
        for ticker, points in series_by_ticker.items():
            if not isinstance(points, list):
                continue
            self.points[ticker].extend(points[-self.max_points :])

    def _payload(self, *, positions: list[dict], risk: dict, model: dict) -> dict:
        return {
            "schema_version": 2,
            "mock": False,
            "status": self.status,
            "asset_class": self.asset_class,
            "mode": self.mode,
            "bar_type": self.bar_type,
            "bar_update_policy": "completed_strategy_bars",
            "include_extended_hours": self.include_extended_hours,
            "started_at": self.started_at,
            "as_of": datetime.now(timezone.utc).isoformat(),
            "tickers": sorted(self.points),
            "series": {ticker: list(points) for ticker, points in self.points.items()},
            "positions": positions,
            "risk": risk,
            "model": model,
        }

    def _write(self, *, positions: list[dict], risk: dict, model: dict) -> None:
        payload = self._payload(positions=positions, risk=risk, model=model)
        tmp_path = self.path.with_name(f".{self.path.name}.tmp")
        with tmp_path.open("w") as fh:
            json.dump(payload, fh, separators=(",", ":"), allow_nan=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, self.path)


def load_telemetry(path: str | Path) -> dict | None:
    try:
        with Path(path).open() as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None
