"""IBKR exchange-session parsing, classification, and entry policies."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


UTC = timezone.utc


class SessionPhase(str, Enum):
    UNKNOWN = "UNKNOWN"
    CLOSED = "CLOSED"
    PRE_MARKET = "PRE_MARKET"
    OPENING_AUCTION = "OPENING_AUCTION"
    RTH = "RTH"
    CLOSING_AUCTION = "CLOSING_AUCTION"
    AFTER_HOURS = "AFTER_HOURS"
    HALTED = "HALTED"
    STALE = "STALE"


class SessionPolicyMode(str, Enum):
    RTH_ONLY = "RTH_ONLY"
    EXTENDED_HOURS = "EXTENDED_HOURS"
    CUSTOM = "CUSTOM"


class OvernightPnlAssignment(str, Enum):
    PRIOR_SESSION = "PRIOR_SESSION"
    NEXT_SESSION = "NEXT_SESSION"


@dataclass(frozen=True, order=True)
class MarketInterval:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("market intervals must be timezone-aware")
        if self.end <= self.start:
            raise ValueError("market interval end must be after start")

    def contains(self, when: datetime) -> bool:
        return self.start <= when < self.end


@dataclass(frozen=True)
class SessionDay:
    session_date: date
    trading: tuple[MarketInterval, ...] = ()
    liquid: tuple[MarketInterval, ...] = ()

    @property
    def is_closed(self) -> bool:
        return not self.trading and not self.liquid


@dataclass(frozen=True)
class SessionPolicy:
    mode: SessionPolicyMode = SessionPolicyMode.RTH_ONLY
    opening_buffer_minutes: int = 5
    closing_buffer_minutes: int = 5
    no_new_entry_minutes_before_close: int = 15
    participate_opening_auction: bool = False
    participate_closing_auction: bool = False
    cancel_entries_at_session_end: bool = True
    custom_windows: tuple[tuple[str, str], ...] = ()
    overnight_pnl_assignment: OvernightPnlAssignment = OvernightPnlAssignment.NEXT_SESSION

    def __post_init__(self) -> None:
        for field_name in (
            "opening_buffer_minutes",
            "closing_buffer_minutes",
            "no_new_entry_minutes_before_close",
        ):
            if getattr(self, field_name) < 0:
                raise ValueError(f"{field_name} must not be negative")
        if self.mode == SessionPolicyMode.CUSTOM and not self.custom_windows:
            raise ValueError("CUSTOM session policy requires at least one custom window")
        for start, end in self.custom_windows:
            _parse_clock_time(start)
            _parse_clock_time(end)


_TZ_ALIASES = {
    "EST5EDT": "America/New_York",
    "US/Eastern": "America/New_York",
    "US/Central": "America/Chicago",
    "US/Mountain": "America/Denver",
    "US/Pacific": "America/Los_Angeles",
}


def resolve_ibkr_timezone(value: str) -> ZoneInfo:
    name = _TZ_ALIASES.get(str(value).strip(), str(value).strip())
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unsupported IBKR timeZoneId {value!r}") from exc


def _parse_clock_time(value: str) -> time:
    compact = str(value).replace(":", "").strip()
    if len(compact) != 4 or not compact.isdigit():
        raise ValueError(f"invalid clock time {value!r}")
    hour, minute = int(compact[:2]), int(compact[2:])
    if hour > 23 or minute > 59:
        raise ValueError(f"invalid clock time {value!r}")
    return time(hour, minute)


def _parse_endpoint(value: str, default_date: date, tz: ZoneInfo) -> tuple[datetime, bool]:
    token = value.strip()
    explicit_date = ":" in token
    if explicit_date:
        raw_date, raw_time = token.split(":", 1)
        endpoint_date = datetime.strptime(raw_date, "%Y%m%d").date()
    else:
        endpoint_date, raw_time = default_date, token
    clock = _parse_clock_time(raw_time)
    return datetime.combine(endpoint_date, clock, tzinfo=tz), explicit_date


def parse_ibkr_hours(value: str, timezone_id: str) -> dict[date, tuple[MarketInterval, ...]]:
    """Parse IBKR ``TradingHours`` or ``LiquidHours`` into UTC intervals.

    IBKR prefixes each semicolon-delimited trading date and may emit multiple
    comma-delimited ranges, explicit dates on either endpoint, overnight ranges,
    or ``CLOSED`` holiday entries.
    """
    tz = resolve_ibkr_timezone(timezone_id)
    days: dict[date, list[MarketInterval]] = {}
    if not value:
        return {}
    for raw_day in str(value).split(";"):
        raw_day = raw_day.strip()
        if not raw_day:
            continue
        try:
            date_token, ranges = raw_day.split(":", 1)
            session_date = datetime.strptime(date_token, "%Y%m%d").date()
        except ValueError as exc:
            raise ValueError(f"invalid IBKR hours segment {raw_day!r}") from exc
        days.setdefault(session_date, [])
        if ranges.strip().upper() == "CLOSED":
            continue
        for raw_range in ranges.split(","):
            try:
                start_token, end_token = raw_range.strip().split("-", 1)
                start, _ = _parse_endpoint(start_token, session_date, tz)
                end, end_explicit = _parse_endpoint(end_token, session_date, tz)
            except ValueError as exc:
                raise ValueError(f"invalid IBKR hours range {raw_range!r}") from exc
            if end <= start and not end_explicit:
                end += timedelta(days=1)
            if end <= start:
                raise ValueError(f"IBKR hours range ends before it starts: {raw_range!r}")
            days[session_date].append(
                MarketInterval(start.astimezone(UTC), end.astimezone(UTC))
            )
    return {
        session_date: tuple(sorted(intervals))
        for session_date, intervals in days.items()
    }


class ExchangeSessionCalendar:
    """One instrument's authoritative broker-provided session calendar."""

    def __init__(
        self,
        *,
        trading_hours: str,
        liquid_hours: str,
        timezone_id: str,
        policy: SessionPolicy | None = None,
        auction_window_minutes: int = 1,
        max_market_data_age: timedelta = timedelta(seconds=15),
    ) -> None:
        if auction_window_minutes < 0:
            raise ValueError("auction_window_minutes must not be negative")
        if max_market_data_age <= timedelta(0):
            raise ValueError("max_market_data_age must be positive")
        self.timezone = resolve_ibkr_timezone(timezone_id)
        self.timezone_id = str(self.timezone.key)
        self.policy = policy or SessionPolicy()
        self.auction_window = timedelta(minutes=auction_window_minutes)
        self.max_market_data_age = max_market_data_age
        trading = parse_ibkr_hours(trading_hours, timezone_id)
        liquid = parse_ibkr_hours(liquid_hours, timezone_id)
        self.days = {
            key: SessionDay(key, trading.get(key, ()), liquid.get(key, ()))
            for key in sorted(set(trading) | set(liquid))
        }
        self._halted = False
        self._halt_reason = ""
        self._last_market_data_at: datetime | None = None

    @classmethod
    def from_instrument_info(
        cls,
        info: dict,
        *,
        policy: SessionPolicy | None = None,
        max_market_data_age: timedelta = timedelta(seconds=15),
    ) -> "ExchangeSessionCalendar":
        if not isinstance(info, dict):
            raise ValueError("instrument info must be a dictionary")
        missing = [
            key
            for key in ("tradingHours", "liquidHours", "timeZoneId")
            if not info.get(key)
        ]
        if missing:
            raise ValueError(f"IBKR contract details missing {', '.join(missing)}")
        return cls(
            trading_hours=info["tradingHours"],
            liquid_hours=info["liquidHours"],
            timezone_id=info["timeZoneId"],
            policy=policy,
            max_market_data_age=max_market_data_age,
        )

    def record_market_data(self, when: datetime) -> None:
        self._last_market_data_at = _aware_utc(when)

    def set_halt(self, halted: bool, reason: str = "") -> None:
        self._halted = bool(halted)
        self._halt_reason = str(reason) if halted else ""

    @property
    def halt_reason(self) -> str:
        return self._halt_reason

    @property
    def last_market_data_at(self) -> datetime | None:
        return self._last_market_data_at

    def _day_containing(self, when: datetime) -> SessionDay | None:
        for day in self.days.values():
            if any(interval.contains(when) for interval in (*day.trading, *day.liquid)):
                return day
        return None

    def _liquid_interval(self, when: datetime) -> MarketInterval | None:
        day = self._day_containing(when)
        if day is None:
            return None
        return next((interval for interval in day.liquid if interval.contains(when)), None)

    def _trading_interval(self, when: datetime) -> MarketInterval | None:
        day = self._day_containing(when)
        if day is None:
            return None
        return next((interval for interval in day.trading if interval.contains(when)), None)

    def phase_at(self, when: datetime, *, enforce_data_health: bool = True) -> SessionPhase:
        when = _aware_utc(when)
        if self._halted:
            return SessionPhase.HALTED
        trading = self._trading_interval(when)
        liquid = self._liquid_interval(when)
        if trading is None and liquid is None:
            return SessionPhase.CLOSED
        if (
            enforce_data_health
            and self._last_market_data_at is not None
            and when - self._last_market_data_at > self.max_market_data_age
        ):
            return SessionPhase.STALE
        if liquid is not None:
            if when < liquid.start + self.auction_window:
                return SessionPhase.OPENING_AUCTION
            if when >= liquid.end - self.auction_window:
                return SessionPhase.CLOSING_AUCTION
            return SessionPhase.RTH
        day = self._day_containing(when)
        if day is None or trading is None:
            return SessionPhase.UNKNOWN
        first_liquid = min((interval.start for interval in day.liquid), default=None)
        last_liquid = max((interval.end for interval in day.liquid), default=None)
        if first_liquid is not None and when < first_liquid:
            return SessionPhase.PRE_MARKET
        if last_liquid is not None and when >= last_liquid:
            return SessionPhase.AFTER_HOURS
        return SessionPhase.CLOSED

    def policy_intervals(self, session_date: date) -> tuple[MarketInterval, ...]:
        day = self.days.get(session_date)
        if day is None:
            return ()
        if self.policy.mode == SessionPolicyMode.RTH_ONLY:
            return day.liquid
        if self.policy.mode == SessionPolicyMode.EXTENDED_HOURS:
            return day.trading
        intervals: list[MarketInterval] = []
        for raw_start, raw_end in self.policy.custom_windows:
            start = datetime.combine(
                session_date,
                _parse_clock_time(raw_start),
                tzinfo=self.timezone,
            )
            end = datetime.combine(
                session_date,
                _parse_clock_time(raw_end),
                tzinfo=self.timezone,
            )
            if end <= start:
                end += timedelta(days=1)
            custom = MarketInterval(start.astimezone(UTC), end.astimezone(UTC))
            for trading in day.trading:
                clipped_start = max(custom.start, trading.start)
                clipped_end = min(custom.end, trading.end)
                if clipped_end > clipped_start:
                    intervals.append(MarketInterval(clipped_start, clipped_end))
        return tuple(sorted(intervals))

    def allows_new_entry(self, when: datetime) -> tuple[bool, str]:
        when = _aware_utc(when)
        phase = self.phase_at(when)
        if phase in {SessionPhase.HALTED, SessionPhase.STALE, SessionPhase.UNKNOWN}:
            return False, phase.value
        day = self._day_containing(when)
        if day is None:
            return False, SessionPhase.CLOSED.value
        interval = next(
            (item for item in self.policy_intervals(day.session_date) if item.contains(when)),
            None,
        )
        if interval is None:
            return False, "OUTSIDE_POLICY_WINDOW"
        if phase == SessionPhase.OPENING_AUCTION and not self.policy.participate_opening_auction:
            return False, "OPENING_AUCTION_DISABLED"
        if phase == SessionPhase.CLOSING_AUCTION and not self.policy.participate_closing_auction:
            return False, "CLOSING_AUCTION_DISABLED"
        open_after = interval.start + timedelta(minutes=self.policy.opening_buffer_minutes)
        close_before = interval.end - timedelta(minutes=self.policy.closing_buffer_minutes)
        no_entry_after = interval.end - timedelta(
            minutes=self.policy.no_new_entry_minutes_before_close
        )
        if when < open_after:
            return False, "OPENING_BUFFER"
        if when >= min(close_before, no_entry_after):
            return False, "CLOSING_BUFFER"
        return True, "ALLOWED"

    def validates_order(
        self,
        when: datetime,
        *,
        order_type: str,
        time_in_force: str,
        is_entry: bool = True,
    ) -> tuple[bool, str]:
        phase = self.phase_at(when)
        normalized_type = str(order_type).upper()
        normalized_tif = str(time_in_force).upper()
        if is_entry:
            allowed, reason = self.allows_new_entry(when)
            if not allowed:
                return False, reason
        if phase in {SessionPhase.PRE_MARKET, SessionPhase.AFTER_HOURS}:
            if normalized_type == "MARKET":
                return False, "MARKET_ORDER_OUTSIDE_RTH"
            if normalized_tif in {"AT_THE_OPEN", "AT_THE_CLOSE"}:
                return False, "AUCTION_TIF_OUTSIDE_AUCTION"
        if phase == SessionPhase.OPENING_AUCTION and normalized_tif not in {
            "AT_THE_OPEN",
            "DAY",
        }:
            return False, "UNSUPPORTED_OPENING_AUCTION_TIF"
        if phase == SessionPhase.CLOSING_AUCTION and normalized_tif not in {
            "AT_THE_CLOSE",
            "DAY",
        }:
            return False, "UNSUPPORTED_CLOSING_AUCTION_TIF"
        return True, "ALLOWED"

    def session_key(self, when: datetime) -> str:
        when = _aware_utc(when)
        day = self._day_containing(when)
        if day is not None:
            return day.session_date.isoformat()
        ordered = sorted(self.days)
        local_date = when.astimezone(self.timezone).date()
        if self.policy.overnight_pnl_assignment == OvernightPnlAssignment.NEXT_SESSION:
            candidate = next((value for value in ordered if value >= local_date), None)
        else:
            candidate = next((value for value in reversed(ordered) if value <= local_date), None)
        return (candidate or local_date).isoformat()

    def next_open_close(self, when: datetime) -> tuple[datetime | None, datetime | None]:
        when = _aware_utc(when)
        intervals = [
            interval
            for session_date in sorted(self.days)
            for interval in self.policy_intervals(session_date)
        ]
        current = next((interval for interval in intervals if interval.contains(when)), None)
        if current is not None:
            next_open = next((interval.start for interval in intervals if interval.start > when), None)
            return next_open, current.end
        future = next((interval for interval in intervals if interval.start > when), None)
        return (future.start, future.end) if future is not None else (None, None)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)
