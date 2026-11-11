"""Persistable daily signal intent, bounded to one broker session window."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import math


@dataclass(frozen=True)
class SessionSignal:
    source_ts: datetime
    not_before: datetime
    expires_at: datetime
    close: float
    atr: float
    yhat: float

    def __post_init__(self):
        if any(value.tzinfo is None for value in (self.source_ts, self.not_before, self.expires_at)):
            raise ValueError("Scheduled signal times must include a timezone")
        if self.expires_at <= self.not_before or self.source_ts > self.expires_at:
            raise ValueError("Invalid scheduled signal window")
        if not all(math.isfinite(value) for value in (self.close, self.atr, self.yhat)) or self.close <= 0 or self.atr <= 0:
            raise ValueError("Invalid scheduled signal values")

    def snapshot(self):
        result = asdict(self)
        for key in ("source_ts", "not_before", "expires_at"):
            result[key] = result[key].isoformat()
        return result

    @classmethod
    def restore(cls, values):
        return cls(**{**values, **{
            key: datetime.fromisoformat(values[key])
            for key in ("source_ts", "not_before", "expires_at")
        }})
