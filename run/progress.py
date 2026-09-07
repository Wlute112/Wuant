"""Atomic, best-effort progress snapshots for dashboard-launched research jobs."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path


class ProgressWriter:
    def __init__(self, path: str | None, **initial) -> None:
        self.path = Path(path) if path else None
        self.state = dict(initial)

    def update(self, **values) -> None:
        if self.path is None:
            return
        self.state.update(values)
        self.state["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(self.state, stream, allow_nan=False)
            temporary.replace(self.path)
        except (OSError, TypeError, ValueError):
            temporary.unlink(missing_ok=True)

