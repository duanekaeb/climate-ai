"""Days the analyses leave out: utility (demand-response) events and the pre-cooling before them.

During a utility event the utility, not the weather or the strategy, sets the thermostats, and
an ``event_prep`` hold shifts runtime into the hours before one. A day touched by either says
nothing about the weather baseline or a strategy, so it is left out of baseline fitting,
savings (both the baseline's training days and the reporting days), the weekly waterfall,
drift, experiments (``experiment_days.included`` false with the reason as its note), natural
experiments and the coupling regression. Every report that drops days says how many and why
(``left_out_note``).

A day is an event day when a utility event that ran overlaps it on ANY unit (the house total
is what is judged, and an event on one floor moves load onto the others): a ``utility_events``
row with ``started_at`` set or status running / ended / opted_out, from its start to the
earlier of its scheduled end and when it was seen over (an opt-out or an early end). Announced
events that never ran and cancelled ones do not count. A prep day is one overlapped by a
verified or sent ``event_prep`` hold, from its write to its end. Days are local calendar days
in the house time zone; an event that crosses midnight marks both days.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from climate.store.orm import ControlAction, UtilityEvent
from climate.timeutil import day_bounds_utc, local_date

EVENT = "utility event"
PREP_COOL = "pre-cooling before a utility event"
PREP_HEAT = "pre-heating before a utility event"
LABELS = (EVENT, PREP_COOL, PREP_HEAT)
PREP_RULE = "event_prep"
RAN_STATUSES = ("running", "ended", "opted_out")
PREP_STATUSES = ("verified", "sent")
DEFAULT_PREP_HOURS = 2.0  # a prep hold whose request carries no length (holds are 1-2 h)
_PREP_LOOKBACK = timedelta(hours=3)  # a prep hold written this long before the window can reach into it
_NOUNS = {
    EVENT: ("day with a utility event", "days with a utility event"),
    PREP_COOL: ("day of pre-cooling before a utility event", "days of pre-cooling before a utility event"),
    PREP_HEAT: ("day of pre-heating before a utility event", "days of pre-heating before a utility event"),
}


def _days(lo: datetime, hi: datetime, tz: str, start: date, end: date) -> list[date]:
    """Local days in [start, end] that [lo, hi) overlaps (the day of ``lo`` when hi <= lo)."""
    d0 = local_date(lo, tz)
    d1 = local_date(hi - timedelta(microseconds=1), tz) if hi > lo else d0
    d0, d1 = max(d0, start), min(d1, end)
    return [d0 + timedelta(days=i) for i in range((d1 - d0).days + 1)]


def _event_span(row: Any, window_end: datetime) -> tuple[datetime, datetime] | None:
    starts = [t for t in (row.start_at, row.started_at) if t is not None]
    lo = min(starts) if starts else row.first_seen_at
    if lo is None:
        return None
    ends = [t for t in (row.end_at, row.ended_at) if t is not None]
    if ends:
        hi = min(ends)
    elif row.status == "running":
        hi = window_end  # still running, no end reported: through the window
    else:
        hi = lo
    return lo, max(hi, lo)


def _prep_end(ts: datetime, request: Any) -> datetime:
    req = request if isinstance(request, dict) else {}
    until = req.get("until")
    if isinstance(until, str):
        try:
            parsed = datetime.fromisoformat(until)
            if parsed.tzinfo is not None:
                return parsed
        except ValueError:
            pass
    hours = req.get("hours")
    if isinstance(hours, (int, float)) and not isinstance(hours, bool) and 0 < hours <= 24:
        return ts + timedelta(hours=float(hours))
    return ts + timedelta(hours=DEFAULT_PREP_HOURS)


def event_days(session: Session, start: date, end: date, tz: str) -> dict[date, str]:
    """Local days in [start, end] to leave out, with why: "utility event" (an event that ran
    overlaps the day on any unit) or "pre-cooling before a utility event" / "pre-heating ..."
    (a verified or sent event_prep hold overlaps it). The event wins when a day has both.
    Ordered by day; empty when nothing applies (two small indexed reads)."""
    if end < start:
        return {}
    t0, t1 = day_bounds_utc(start, tz)[0], day_bounds_utc(end, tz)[1]
    out: dict[date, str] = {}
    lo_col = func.coalesce(UtilityEvent.start_at, UtilityEvent.started_at, UtilityEvent.first_seen_at)
    rows = session.execute(
        select(UtilityEvent.start_at, UtilityEvent.started_at, UtilityEvent.end_at, UtilityEvent.ended_at,
               UtilityEvent.first_seen_at, UtilityEvent.status)
        .where(
            or_(UtilityEvent.started_at.is_not(None), UtilityEvent.status.in_(RAN_STATUSES)),
            lo_col < t1,
            or_(func.coalesce(UtilityEvent.end_at, UtilityEvent.ended_at).is_(None),
                func.greatest(UtilityEvent.end_at, UtilityEvent.ended_at) >= t0),
        )
    ).all()
    for row in rows:
        span = _event_span(row, t1)
        if span is None or span[1] < t0 or (span[1] == t0 and span[0] < t0):
            continue
        for d in _days(*span, tz, start, end):
            out[d] = EVENT
    preps = session.execute(
        select(ControlAction.ts, ControlAction.request, ControlAction.reason)
        .where(ControlAction.rule == PREP_RULE, ControlAction.status.in_(PREP_STATUSES),
               ControlAction.ts < t1, ControlAction.ts >= t0 - _PREP_LOOKBACK)
    ).all()
    for ts, request, reason in preps:
        hi = _prep_end(ts, request)
        if hi <= t0:
            continue
        label = PREP_HEAT if "pre-heat" in (reason or "").lower() else PREP_COOL
        for d in _days(ts, hi, tz, start, end):
            out.setdefault(d, label)
    return dict(sorted(out.items()))


def label_of(note: str | None) -> str | None:
    """The exclusion label inside a free-text note (``experiment_days.note``), if any."""
    if not note:
        return None
    for label in (PREP_COOL, PREP_HEAT, EVENT):
        if label in note:
            return label
    return None


def day_noun(label: str) -> str:
    """'day with a utility event' / 'day of pre-cooling before a utility event'."""
    return _NOUNS.get(label, (f"day with a {label}",))[0]


def counts(excluded: Mapping[date, str]) -> dict[str, int]:
    """{label: number of days}, labels in a fixed order, zeros left out."""
    c = Counter(excluded.values())
    return {label: c[label] for label in LABELS if c[label]}


def days_phrase(excluded: Mapping[date, str]) -> str:
    """'2 days with a utility event and 1 day of pre-cooling before a utility event' ('' when
    none)."""
    parts = [f"{n} {_NOUNS[label][0 if n == 1 else 1]}" for label, n in counts(excluded).items()]
    if not parts:
        return ""
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def left_out_note(excluded: Mapping[date, str], *, prefix: str = " ") -> str:
    """' 2 days with a utility event were left out.' for appending to a report's note; '' when
    nothing was left out."""
    phrase = days_phrase(excluded)
    if not phrase:
        return ""
    return f"{prefix}{phrase[0].upper()}{phrase[1:]} {'was' if len(excluded) == 1 else 'were'} left out."
