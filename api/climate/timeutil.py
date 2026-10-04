"""Time helpers. Store UTC; reason about schedules in the house's local time zone."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

SLOT = timedelta(minutes=5)


def utcnow() -> datetime:
    return datetime.now(UTC)


def floor_slot(ts: datetime, minutes: int = 5) -> datetime:
    """Align a timestamp down to the start of its N-minute slot (keeps tzinfo)."""
    discard = timedelta(minutes=ts.minute % minutes, seconds=ts.second, microseconds=ts.microsecond)
    return ts - discard


def to_local(ts: datetime, tz: str) -> datetime:
    return ts.astimezone(ZoneInfo(tz))


def local_date(ts: datetime, tz: str) -> date:
    return to_local(ts, tz).date()


def parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def in_window(local_ts: datetime, start: str, end: str, days: list[int] | None = None) -> bool:
    """True if local_ts falls in [start, end). Windows may cross midnight.

    ``days`` are the weekdays (Monday=0) on which the window STARTS; for a window that
    crosses midnight the early-morning part belongs to the previous day's window.
    """
    t = local_ts.time()
    s, e = parse_hhmm(start), parse_hhmm(end)
    wd = local_ts.weekday()
    allowed = set(days) if days is not None else set(range(7))
    if s <= e:
        return s <= t < e and wd in allowed
    # crosses midnight
    if t >= s:
        return wd in allowed
    if t < e:
        return (wd - 1) % 7 in allowed
    return False


def day_bounds_utc(d: date, tz: str) -> tuple[datetime, datetime]:
    """UTC [start, end) of a local calendar day (handles DST: 23/25-hour days)."""
    z = ZoneInfo(tz)
    start = datetime.combine(d, time(0), z)
    end = datetime.combine(d + timedelta(days=1), time(0), z)
    return start.astimezone(UTC), end.astimezone(UTC)
