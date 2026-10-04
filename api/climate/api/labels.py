"""One line for each Live unit card: whose hold is running on the thermostat and until when,
in house time (docs/specs/holds-and-utility-events.md, "API and web").

``hold_label`` is pure (no database, no clock): the status route passes the unit's
``UnitStatus`` (with its snapshot, ``person_hold`` as ``climate.state`` detected it), the
unit's utility events, the house time zone and ``now``. What the thermostat shows decides,
in this order:

1. its top running ecobee event: a utility event ("Utility event until 6:00 PM (cooling
   +2°F)"), a vacation ("Vacation until Oct 12, 9:00 AM"), Smart Away / Smart Home, or an
   event type the app doesn't know ("Unrecognised ecobee event: today");
2. a person's hold ("On your hold since 2:10 PM, until 4:00 PM", "On your hold since 2:10 PM
   (until you change it)", "Your hold from the app until 4:00 PM", "Quick Save"), including
   one carried over a HomeKit snapshot, which shows no ecobee holds;
3. a plain hold that is not a person's: the controller's ("Our hold until 3:40 PM"). When the
   state service is unavailable (no ``person_hold`` detection), a plain hold the source does
   not recognise as ours (``set_by_us``) is labelled a person's: the cautious reading;
4. no hold shown, but a utility event's own window covers now (the snapshot can lag the
   event's start): the utility event.

Nothing running gives (None, None); a resume back-off has its own field on ``UnitLive``.
Times on another local day than ``now`` carry the weekday ("since Mon 2:10 PM").
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from climate.api.schemas import HoldOwner
from climate.control.policy import UnitStatus, UtilityEventState
from climate.sources.base import AUTO_EVENTS, KNOWN_HOLD_TYPES, PLAIN_HOLD_TYPES, HoldInfo, ThermostatEvent
from climate.state import INDEFINITE_YEAR, person_until
from climate.timeutil import to_local
from climate.utility.events import change_label

UTILITY = "demandResponse"
VACATION = "vacation"
QUICK_SAVE = "quickSave"
AUTO_LABELS = {"autoAway": "Smart Away", "autoHome": "Smart Home"}


# ---------------------------------------------------------------------------------------
# time wording (house time)
# ---------------------------------------------------------------------------------------


def clock(ts: datetime, tz: str) -> str:
    """'2:10 PM' in house time."""
    local = to_local(ts, tz)
    return f"{local.hour % 12 or 12}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"


def when(ts: datetime, tz: str, now: datetime) -> str:
    """'4:00 PM' on the same local day as ``now``, else 'Tue 4:00 PM'."""
    text = clock(ts, tz)
    local = to_local(ts, tz)
    return text if local.date() == to_local(now, tz).date() else f"{local:%a} {text}"


def day_time(ts: datetime, tz: str) -> str:
    """'Oct 12, 9:00 AM' in house time (vacations span days, so the date is always shown)."""
    local = to_local(ts, tz)
    return f"{local:%b} {local.day}, {clock(ts, tz)}"


# ---------------------------------------------------------------------------------------
# the label
# ---------------------------------------------------------------------------------------


def hold_label(
    unit: UnitStatus, events: Sequence[UtilityEventState], tz: str, now: datetime,
) -> tuple[HoldOwner | None, str | None]:
    """(hold_owner, hold_label) for the unit's Live card (module docstring). ``events`` may
    hold other units' rows; they are ignored."""
    snap = unit.snapshot
    hold = snap.hold if snap is not None else None
    kind = hold.hold_type if hold is not None else None
    mine = [e for e in events if e.unit_key == unit.unit_key]

    if snap is not None and hold is not None and kind == UTILITY:
        running = _running_event(snap.events, hold)
        row = _covering(mine, now)
        end = _real_end(hold.end) or (running.end if running is not None else None) or (row.end_at if row else None)
        change = (_event_change(running) if running is not None else None) or _hold_change(hold) \
            or (_row_change(row) if row is not None else None)
        return "utility", _utility_text(end, change, tz, now)
    if hold is not None and kind == VACATION:
        end = _real_end(hold.end)
        return "vacation", f"Vacation until {day_time(end, tz)}" if end is not None else "Vacation"
    if kind in AUTO_EVENTS:
        return "ecobee_auto", AUTO_LABELS.get(str(kind), "Smart Away")
    if kind is not None and kind not in KNOWN_HOLD_TYPES:
        return "unknown_event", f"Unrecognised ecobee event: {kind}"

    ph = unit.person_hold
    if ph is not None:
        return _person(ph.by, ph.hold_type, ph.since, ph.until, tz, now)
    if hold is not None and (kind in PLAIN_HOLD_TYPES or kind == QUICK_SAVE or kind is None):
        if hold.set_by_us and kind != QUICK_SAVE:
            end = _real_end(hold.end)
            return "controller", f"Our hold until {when(end, tz, now)}" if end is not None else "Our hold"
        # Not ours, yet not a detected person's hold: the state service is unavailable, or a
        # timed hold just ended by the clock while the snapshot still shows it. Read it off
        # the snapshot.
        return _person("thermostat", kind, hold.start, person_until(hold), tz, now)

    row = _covering(mine, now)
    if row is not None:
        return "utility", _utility_text(row.end_at, _row_change(row), tz, now)
    return None, None


# ---------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------


def _person(
    by: str, hold_type: str | None, since: datetime | None, until: datetime | None, tz: str, now: datetime,
) -> tuple[HoldOwner, str]:
    """A person's hold: ``until`` None means it runs until someone changes it."""
    if hold_type == QUICK_SAVE:
        return "person", "Quick Save"
    if by == "app":
        if until is not None:
            return "app", f"Your hold from the app until {when(until, tz, now)}"
        return "app", "Your hold from the app (until you change it)"
    started = f" since {when(since, tz, now)}" if since is not None else ""
    if until is not None:
        return "person", f"On your hold{started}, until {when(until, tz, now)}"
    return "person", f"On your hold{started} (until you change it)"


def _real_end(end: datetime | None) -> datetime | None:
    """An end time worth showing: ecobee reports "no end" as a date in 2035 or later."""
    return None if end is None or end.year >= INDEFINITE_YEAR else end


def _utility_text(end: datetime | None, change: str | None, tz: str, now: datetime) -> str:
    text = "Utility event"
    end = _real_end(end)
    if end is not None:
        text += f" until {when(end, tz, now)}"
    if change:
        text += f" ({change})"
    return text


def _covering(events: Sequence[UtilityEventState], now: datetime) -> UtilityEventState | None:
    """The unit's utility event whose own window contains now (running ones first)."""
    covering = [e for e in events if e.covers(now)]
    covering.sort(key=lambda e: (e.status != "running", e.start_at or now))
    return covering[0] if covering else None


def _running_event(events: Sequence[ThermostatEvent], hold: HoldInfo) -> ThermostatEvent | None:
    """The snapshot's running utility event behind ``hold`` (same linkRef, else same name,
    else the first running one)."""
    running = [e for e in events if e.event_type == UTILITY and e.running]
    for match in (
        lambda e: hold.link_ref is not None and e.link_ref == hold.link_ref,
        lambda e: hold.event_name is not None and e.name == hold.event_name,
    ):
        found = next((e for e in running if match(e)), None)
        if found is not None:
            return found
    return running[0] if running else None


def _event_change(ev: ThermostatEvent) -> str | None:
    capped = ev.duty_cycle_pct is not None and 0 <= ev.duty_cycle_pct < 100
    if not (ev.is_cool_off or ev.is_heat_off or capped or _has_setpoints(
            ev.is_relative, ev.heat_f, ev.cool_f, ev.heat_offset_f, ev.cool_offset_f)):
        return None
    return change_label(
        heat_f=ev.heat_f, cool_f=ev.cool_f, is_relative=ev.is_relative, heat_offset_f=ev.heat_offset_f,
        cool_offset_f=ev.cool_offset_f, is_cool_off=ev.is_cool_off, is_heat_off=ev.is_heat_off,
        duty_cycle_pct=ev.duty_cycle_pct,
    )


def _hold_change(hold: HoldInfo) -> str | None:
    if not _has_setpoints(hold.is_relative, hold.heat_f, hold.cool_f, hold.heat_offset_f, hold.cool_offset_f):
        return None
    return change_label(
        heat_f=hold.heat_f, cool_f=hold.cool_f, is_relative=hold.is_relative,
        heat_offset_f=hold.heat_offset_f, cool_offset_f=hold.cool_offset_f,
    )


def _row_change(row: UtilityEventState) -> str | None:
    if not _has_setpoints(row.is_relative, row.heat_f, row.cool_f, row.heat_offset_f, row.cool_offset_f):
        return None
    return change_label(
        heat_f=row.heat_f, cool_f=row.cool_f, is_relative=row.is_relative,
        heat_offset_f=row.heat_offset_f, cool_offset_f=row.cool_offset_f,
    )


def _has_setpoints(
    is_relative: bool, heat_f: float | None, cool_f: float | None, heat_offset_f: float | None,
    cool_offset_f: float | None,
) -> bool:
    """Whether ``change_label`` has a setpoint change to word (else it says "not reported")."""
    if is_relative:
        return bool(heat_offset_f) or bool(cool_offset_f)
    return heat_f is not None or cool_f is not None
