"""The linked-floors policy (blueprint §2) and the house-state types it plans over.

``plan(state)`` is pure: given the current ``HouseState`` it returns the setpoints each unit
should hold now and why. The controller (``controller.py``) runs it every tick, passes each
target through ``guardrails.check`` and then suggests or writes it.

Policy parameters are versioned in ``policy_versions``; the models and Claude change them
only through the change gates (``changes.py``). Claude may sign off a model-proposed change
only when every parameter stays inside ``CLAUDE_SIGNOFF_RANGES``; anything else needs the
owner. Hard limits (``HardLimits``) are owner-only and enforced on every write.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from climate.sources.base import UnitSnapshot
from climate.store.app_settings import ControlSettings, OccupancySettings, UtilityEventSettings

RoomStateName = Literal["occupied", "asleep", "empty", "unknown", "no_target"]


class PolicyParams(BaseModel):
    """Tunable policy. Defaults are the blueprint's starting values."""

    linked_floors_enabled: bool = True
    # Cooling: when the main floor is empty by day but someone is upstairs, the main floor's
    # cool setpoint is tied to upstairs' cool setpoint minus this offset (main runs cooler).
    linked_offset_f: float = Field(default=1.0, ge=0.0, le=4.0)
    # Heating: the main floor may not sit more than this far below the upstairs heat setpoint.
    linked_heat_gap_f: float = Field(default=2.0, ge=0.0, le=6.0)
    # Whole house empty: both floors set back together, main still cooler by this gap.
    setback_gap_f: float = Field(default=2.0, ge=0.0, le=4.0)
    # Recovery: the main floor starts recovering this many minutes before the upstairs.
    recovery_lead_min: int = Field(default=20, ge=0, le=60)
    # Pre-cool on hot, sunny days (off until it earns its place).
    precool_enabled: bool = False
    precool_degrees_f: float = Field(default=1.0, ge=0.0, le=3.0)
    precool_start_hour: int = Field(default=13, ge=8, le=18)
    precool_min_forecast_high_f: float = Field(default=90.0, ge=70.0, le=115.0)
    # The bed/office wing stays independent unless data shows coupling to the main floor.
    bed_wing_independent: bool = True


# Ranges inside which Claude may sign off a model-queued change without the owner.
CLAUDE_SIGNOFF_RANGES: dict[str, tuple[float, float]] = {
    "linked_offset_f": (0.0, 3.0),
    "linked_heat_gap_f": (0.0, 4.0),
    "setback_gap_f": (0.0, 4.0),
    "recovery_lead_min": (0, 45),
    "precool_degrees_f": (0.0, 2.0),
    "precool_start_hour": (11, 16),
    "precool_min_forecast_high_f": (85.0, 105.0),
}
# Boolean switches (enable/disable a rule) always need the owner.
OWNER_ONLY_PARAMS = {"linked_floors_enabled", "precool_enabled", "bed_wing_independent"}


# --- house state (assembled by climate.state.load_house_state) -------------------------


class RoomStatus(BaseModel):
    room_key: str
    name: str
    unit_key: str
    floor: str
    has_sensor: bool
    is_sleep_room: bool
    has_comfort_target: bool
    temp_f: float | None = None  # mean of the room's live sensors; None when no sensor
    humidity: float | None = None
    state: RoomStateName = "unknown"
    confidence: float = 0.0
    reason: str = ""
    since: datetime | None = None
    sensor_keys: list[str] = Field(default_factory=list)
    seconds_since_motion: int | None = None
    stale: bool = False  # newest reading older than 15 minutes
    # Learned offset of this room from its thermostat's averaged temperature (°F), if fitted.
    offset_f: float | None = None
    is_priority: bool = False  # the room the unit is steering for right now


class PersonHold(BaseModel):
    """A hold a person set. It always wins: the controller writes nothing to the unit while
    it runs, however long that is (``climate.state`` detects it; guardrails blocks on it).

    ``by``: 'app' = the owner's hold from this app's hold form; 'thermostat' = anything else a
    person did (at the wall, in the ecobee app, Apple Home, Siri: ecobee does not say which)."""

    since: datetime  # when it was set: hold.start, else first seen
    first_seen: datetime  # when the controller first saw (and logged) it
    by: Literal["thermostat", "app"] = "thermostat"
    hold_type: str | None = None  # holdHours / nextTransition / indefinite / dateTime / quickSave
    until: datetime | None = None  # when it ends on its own; None = until someone changes it
    heat_f: float | None = None
    cool_f: float | None = None
    climate_ref: str | None = None  # a comfort setting picked by hand ('away', 'sleep', ...)
    detection_id: int | None = None  # the control_actions row that logged it


class UtilityEventState(BaseModel):
    """An announced or running utility event on one unit (a ``utility_events`` row), as the
    policy, guardrails and controller see it. Loaded by ``climate.utility.events.load_active``."""

    id: int
    unit_key: str
    event_key: str
    name: str | None = None
    status: Literal["announced", "running", "ended", "cancelled", "opted_out"]
    start_at: datetime | None = None
    end_at: datetime | None = None
    heat_f: float | None = None
    cool_f: float | None = None
    is_relative: bool = False
    heat_offset_f: float | None = None
    cool_offset_f: float | None = None
    is_optional: bool | None = None
    skip: Literal["requested", "done", "failed", "refused"] | None = None
    skip_by: Literal["owner", "rule"] | None = None
    # The event turns cooling / heating off (ecobee isCoolOff / isHeatOff; from the row's detail).
    is_cool_off: bool = False
    is_heat_off: bool = False

    def covers(self, now: datetime) -> bool:
        """The event's own window contains ``now`` (by the clock, whatever the last snapshot
        says) and nobody opted out of it."""
        if self.skip == "done" or self.status in ("cancelled", "opted_out", "ended"):
            return False
        if self.status == "running":
            return self.end_at is None or now < self.end_at
        return self.start_at is not None and self.start_at <= now and (self.end_at is None or now < self.end_at)


class UnitStatus(BaseModel):
    unit_key: str
    name: str
    snapshot: UnitSnapshot | None = None
    age_s: float | None = None  # seconds since snapshot.ts
    call: Literal["cool", "heat", "fan", "idle", "unknown"] = "unknown"
    last_change_at: datetime | None = None  # last control_action we wrote (status verified/sent)
    # A person's hold is running: the controller stands aside until it ends (no time limit).
    person_hold: PersonHold | None = None
    # Someone pressed Resume (thermostat, ecobee app, or this app's "Resume schedule"): the
    # ecobee schedule runs until this time (resume_backoff_hours from when it was first seen).
    resume_backoff_until: datetime | None = None
    resume_seen_at: datetime | None = None


class HouseState(BaseModel):
    now: datetime
    tz: str
    units: dict[str, UnitStatus]
    rooms: dict[str, RoomStatus]
    outdoor_temp_f: float | None = None
    forecast_high_f: float | None = None  # today's forecast high (local day)
    forecast_sunny: bool | None = None  # mean cloud cover < 40% for the afternoon
    house_empty: bool = False
    house_empty_reason: str = ""
    control: ControlSettings
    occupancy: OccupancySettings
    policy: PolicyParams
    policy_version_id: int | None = None
    # Announced and running utility events (all units), and what to do around them.
    utility_events: list[UtilityEventState] = Field(default_factory=list)
    utility: UtilityEventSettings = Field(default_factory=UtilityEventSettings)


class UnitTarget(BaseModel):
    unit_key: str
    heat_f: float
    cool_f: float
    rule: Literal[
        "comfort", "sleep", "linked_floors", "house_setback", "recovery", "precool", "independent", "hold_off",
        "event_prep",
    ]
    reason: str  # one human sentence, e.g. "Main floor empty, Toy Room occupied: main 1°F under upstairs"
    priority_room: str | None = None
    # 'program' = no hold needed (the ecobee schedule already matches); controller resumes or leaves it.
    desired: Literal["hold", "program"] = "hold"
    # The latest moment a hold written for this target may end ('event_prep': the utility
    # event's start minus precondition_end_gap_min). The controller picks holdHours so the
    # hold ends by then (it lapses on its own, never resumed), or writes nothing.
    hold_end_by: datetime | None = None


def plan(state: HouseState) -> list[UnitTarget]:
    """Return one target per unit for right now. Pure function; no I/O.

    Rules, in order (blueprint §2-§3):
    1. Per unit, the comfort band for the period: night (any of the unit's sleep rooms inside
       its sleep window) / day / away (house empty). Occupied and asleep rooms are protected;
       pick the setpoint so the unit's *priority* occupied room lands in band using its
       learned offset (cooling: cool_f = band.cool_f - offset of the warmest-running occupied
       priority room; heating mirrors it). Priorities: upstairs - Girls' Room at night, Toy
       Room by day; wing - Bedroom at night, Office in office hours; main - Hallway at night,
       School Room in school hours, Living Room in the evening.
    2. Linked floors: main floor empty by day and anyone upstairs -> main cool_f =
       min(main day cool, up cool_f - linked_offset_f); heating: main heat_f >= up heat_f -
       linked_heat_gap_f. Never let the main floor float to its away band in that case.
       At night the main floor is NOT empty (Twins'/Olive's rooms asleep): sleep comfort.
    3. Whole house empty: both floors use away bands, main kept setback_gap_f cooler than up.
       Recovery: main floor leaves setback recovery_lead_min before the upstairs.
    4. Pre-cool (only if enabled): hot (forecast_high_f >= threshold) and sunny -> from
       precool_start_hour lower cool_f by precool_degrees_f on occupied floors.
    5. Bed wing: independent (its own comfort) when bed_wing_independent.
    6. Pre-cool / pre-heat before a utility event (only if ``utility.precondition``): see
       ``_apply_event_prep``. A rule-4 pre-cool hold also ends before the next utility event
       (``_cap_precool_before_events``).
    Rooms without a sensor never contribute a temperature; unknown/stale data -> occupied.

    Implementation notes (see the helpers below):
    - "Keeps every occupied room in band": cool_f = band.cool_f - the largest learned offset
      among the unit's occupied sensored rooms (the warmest-running one), heat_f = band.heat_f
      - the smallest; offsets are clipped to +/-3°F and a missing offset counts as 0. Only
      when the rooms disagree by more than the deadband does the priority room alone decide.
      At night the main floor steers on the Hallway only (its night sensor set).
    - Recovery: when the house stops being empty (the earliest of an occupied room's
      ``since`` and the phones coming home) and the upstairs is still at its setback with
      nobody upstairs, the upstairs keeps its setback for recovery_lead_min while the main
      floor recovers first. Predicted arrivals ("Arriving") are not used yet.
    - Pre-cool runs from precool_start_hour until the schedule's evening_start.
    - A thermostat switched off gets rule 'hold_off' (no hold, current setpoints).
    - desired='program' when the rounded target equals what the ecobee program holds: the
      snapshot setpoints when no hold is active, or ``settings['program_heat_f'/'program_cool_f']``
      when the source reports them.
    """
    ctx = _Ctx.build(state)
    targets: dict[str, UnitTarget] = {}
    for unit_key in ctx.planning_order:
        targets[unit_key] = _plan_unit(ctx, unit_key, targets)
    _apply_recovery(ctx, targets)
    _apply_event_prep(ctx, targets)
    _cap_precool_before_events(ctx, targets)
    return [_finish(ctx, targets[k]) for k in ctx.unit_keys]


# ---------------------------------------------------------------------------------------
# plan helpers (private; climate.state reuses _unit_period / _priority_room)
# ---------------------------------------------------------------------------------------

# These imports sit below plan() on purpose: everything above is the shared contract.
from dataclasses import dataclass
from datetime import time, timedelta

from climate.house import ROOMS, UNIT_KEYS, UNITS
from climate.store.app_settings import DEFAULT_COMFORT, ComfortBand, Schedule
from climate.timeutil import in_window, parse_hhmm, to_local

Period = Literal["day", "night", "away"]
_OFFSET_CAP_F = 3.0  # learned offsets beyond this are treated as this (model noise guard)
_MATCH_F = 0.25  # setpoints within this are "the same" (thermostats show 0.5°F steps)
_LABELS: dict[str, tuple[str, str, str]] = {
    # subject, object, location
    "main": ("Main floor", "the main floor", "on the main floor"),
    "up": ("Upstairs", "upstairs", "upstairs"),
    "bed": ("Bed / Office wing", "the bed / office wing", "in the bed / office wing"),
}
_THERMOSTAT_ROOM = {u.key: u.thermostat_room_key for u in UNITS}


@dataclass
class _Ctx:
    state: HouseState
    local: datetime
    unit_keys: list[str]
    planning_order: list[str]
    periods: dict[str, Period]
    rooms_by_unit: dict[str, list[RoomStatus]]
    occupied: dict[str, list[RoomStatus]]

    @classmethod
    def build(cls, state: HouseState) -> _Ctx:
        local = to_local(state.now, state.tz)
        keys = [k for k in UNIT_KEYS if k in state.units] + [k for k in state.units if k not in UNIT_KEYS]
        if not keys:
            keys = list(UNIT_KEYS)
        # upstairs first: the main floor's linked and setback targets derive from it
        order = [k for k in ("up", "main") if k in keys] + [k for k in keys if k not in ("up", "main")]
        rooms_by_unit: dict[str, list[RoomStatus]] = {k: [] for k in keys}
        for r in state.rooms.values():
            rooms_by_unit.setdefault(r.unit_key, []).append(r)
        periods = {
            k: _unit_period(k, state.rooms, state.occupancy, local, state.house_empty) for k in rooms_by_unit
        }
        occupied = {k: [r for r in rs if _counts_as_occupied(r)] for k, rs in rooms_by_unit.items()}
        return cls(state, local, keys, order, periods, rooms_by_unit, occupied)


def _unit_period(
    unit_key: str, rooms: dict[str, RoomStatus], occupancy: OccupancySettings, local_now: datetime,
    house_empty: bool,
) -> Period:
    """'night' if any of the unit's sleep rooms is asleep or inside its sleep window, else
    'away' when the whole house is empty, else 'day'."""
    for room in ROOMS:
        if room.unit_key != unit_key or not room.is_sleep_room:
            continue
        status = rooms.get(room.key)
        if status is not None and status.state == "asleep":
            return "night"
        if _in_sleep_window(room.key, occupancy, local_now):
            return "night"
    return "away" if house_empty else "day"


def _in_sleep_window(room_key: str, occupancy: OccupancySettings, local_now: datetime) -> bool:
    return any(in_window(local_now, w.start, w.end, w.days) for w in occupancy.sleep_windows.get(room_key, []))


def _priority_room(unit_key: str, period: str, local_now: datetime, schedule: Schedule) -> tuple[str, str] | None:
    """(room_key, why) the unit steers for right now under blueprint §3, or None."""
    if period == "away":
        return None
    night = period == "night"
    if unit_key == "up":
        return ("girls_room", "night") if night else ("toy_room", "daytime")
    if unit_key == "bed":
        if night:
            return "bedroom", "night"
        if in_window(local_now, schedule.office_start, schedule.office_end, schedule.office_days):
            return "office", "office hours"
        return None
    if unit_key == "main":
        if night:
            return "hallway", "night"
        if in_window(local_now, schedule.school_start, schedule.school_end, schedule.school_days):
            return "school_room", "school hours"
        if in_window(local_now, schedule.evening_start, "00:00"):
            return "living_room", "evening"
    return None


def _counts_as_occupied(r: RoomStatus) -> bool:
    """Comfort rooms that must stay in band: occupied, asleep, or a sensored room whose data
    is unknown or stale (uncertain means occupied). Unsensored rooms outside their sleep
    window ('unknown') are not comfort targets."""
    if not r.has_comfort_target:
        return False
    if r.state in ("occupied", "asleep"):
        return True
    return r.has_sensor and (r.state == "unknown" or r.stale)


def _band(control: ControlSettings, unit_key: str, period: Period) -> ComfortBand:
    comfort = control.comfort.get(unit_key) or DEFAULT_COMFORT.get(unit_key) or DEFAULT_COMFORT["main"]
    return getattr(comfort, period)


def _labels(state: HouseState, unit_key: str) -> tuple[str, str, str]:
    if unit_key in _LABELS:
        return _LABELS[unit_key]
    name = state.units[unit_key].name if unit_key in state.units else unit_key
    return name, f"the {name.lower()}", f"in the {name.lower()}"


def _plan_unit(ctx: _Ctx, unit_key: str, done: dict[str, UnitTarget]) -> UnitTarget:
    st = ctx.state
    p = st.policy
    period = ctx.periods.get(unit_key, "day")
    band = _band(st.control, unit_key, period)
    subject, obj, loc = _labels(st, unit_key)
    occ = ctx.occupied.get(unit_key, [])
    prio = _priority_room(unit_key, period, ctx.local, st.control.schedule)
    unit = st.units.get(unit_key)
    snap = unit.snapshot if unit else None

    if snap is not None and snap.hvac_mode == "off":
        return UnitTarget(
            unit_key=unit_key,
            heat_f=snap.heat_sp_f if snap.heat_sp_f is not None else band.heat_f,
            cool_f=snap.cool_sp_f if snap.cool_sp_f is not None else band.cool_f,
            rule="hold_off",
            reason=f"The {subject.lower()} thermostat is switched off, so the controller leaves it alone.",
            desired="program",
        )

    if period == "away":
        return _setback_target(ctx, unit_key, band, done)

    deadband = st.control.limits.min_deadband_f
    if period == "night":
        if unit_key == "main":
            steer = [r for r in ctx.rooms_by_unit.get(unit_key, []) if r.room_key == _THERMOSTAT_ROOM["main"]]
        else:
            steer = [r for r in occ if r.has_sensor]
        heat, cool, notes, steered = _steer(band, steer, prio[0] if prio else None, deadband)
        asleep = [
            r.name for r in ctx.rooms_by_unit.get(unit_key, [])
            if r.is_sleep_room and (r.state == "asleep" or _in_sleep_window(r.room_key, st.occupancy, ctx.local))
        ]
        who = f"{_join(asleep)} asleep" if asleep else "sleep window"
        where = " on the Hallway" if unit_key == "main" else ""
        reason = f"Night ({who}): {obj} holds sleep comfort {_fmt_band(band.heat_f, band.cool_f)}{where}"
        return UnitTarget(
            unit_key=unit_key, heat_f=heat, cool_f=cool, rule="sleep", reason=_sentence(reason, notes),
            priority_room=prio[0] if prio else steered,
        )

    # day
    if unit_key == "main" and not occ and p.linked_floors_enabled and "up" in done and ctx.occupied.get("up"):
        return _linked_target(ctx, band, done["up"])

    steer = [r for r in occ if r.has_sensor]
    heat, cool, notes, steered = _steer(band, steer, prio[0] if prio and _is_in(prio[0], occ) else None, deadband)
    independent = unit_key == "bed" and p.bed_wing_independent
    rule: str = "independent" if independent else "comfort"
    lead = f"{subject} runs on its own: " if independent else ""
    if occ:
        names = _join([r.name for r in occ])
        why = f" ({prio[1]})" if prio and _is_in(prio[0], occ) and len(occ) == 1 else ""
        core = f"{names} occupied{why}; day comfort {_fmt_band(band.heat_f, band.cool_f)}"
        reason = (lead + core) if independent else f"{subject}: {core}"
    elif independent:
        reason = f"{lead}no one detected, but the house isn't empty, so it holds day comfort {_fmt_band(band.heat_f, band.cool_f)}"
    else:
        reason = (
            f"No one detected {loc}, but the house isn't empty: holding day comfort "
            f"{_fmt_band(band.heat_f, band.cool_f)}"
        )
        if unit_key == "main" and p.linked_floors_enabled:
            reason += " (nobody upstairs, so the floors are not linked)"
    priority_room = prio[0] if prio and _is_in(prio[0], occ) else steered

    if occ and _precool_applies(ctx):
        cool -= p.precool_degrees_f
        rule = "precool"
        notes.append(
            f"pre-cooling {p.precool_degrees_f:g}°F on a hot, sunny day (forecast high "
            f"{st.forecast_high_f:g}°F) until {_clock_hhmm(st.control.schedule.evening_start)}"
        )
    return UnitTarget(
        unit_key=unit_key, heat_f=heat, cool_f=cool, rule=rule, reason=_sentence(reason, notes),  # type: ignore[arg-type]
        priority_room=priority_room,
    )


def _linked_target(ctx: _Ctx, main_day: ComfortBand, up: UnitTarget) -> UnitTarget:
    """Rule 2: main floor empty by day, someone upstairs -> tie main to the upstairs target."""
    st = ctx.state
    p = st.policy
    main_away = _band(st.control, "main", "away")
    cool = min(main_day.cool_f, up.cool_f - p.linked_offset_f)
    heat = max(main_away.heat_f, up.heat_f - p.linked_heat_gap_f)
    up_rooms = ctx.occupied.get("up", [])
    names = _join([_the(r.name) for r in up_rooms])
    verb = "is" if len(up_rooms) == 1 else "are"
    empties = [r.since for r in ctx.rooms_by_unit.get("main", []) if r.state == "empty" and r.since is not None]
    since = f" since {_clock(max(empties), st.tz)}" if empties else ""
    gap = _round_half(up.cool_f) - _round_half(cool)
    reason = (
        f"Main floor empty{since} and {names} {verb} occupied: main floor held {gap:g}°F under upstairs "
        f"({_round_half(cool):g}°F), heat no lower than {_round_half(heat):g}°F"
    )
    return UnitTarget(unit_key="main", heat_f=heat, cool_f=cool, rule="linked_floors", reason=reason + ".",
                      priority_room=None)


def _setback_target(ctx: _Ctx, unit_key: str, band: ComfortBand, done: dict[str, UnitTarget]) -> UnitTarget:
    """Rule 3: whole house empty -> away bands; the main floor stays setback_gap_f under upstairs
    (cooling) and no more than setback_gap_f below it (heating)."""
    st = ctx.state
    _, obj, _ = _labels(st, unit_key)
    why = _lower_first(st.house_empty_reason.rstrip(".")) or "nobody home"
    heat, cool = band.heat_f, band.cool_f
    extra = ""
    up = done.get("up")
    if unit_key == "main" and up is not None and up.rule == "house_setback":
        gap = st.policy.setback_gap_f
        cool = min(band.cool_f, up.cool_f - gap)
        heat = max(band.heat_f, up.heat_f - gap)
        extra = f", {_round_half(up.cool_f) - _round_half(cool):g}°F under upstairs"
    reason = f"House empty ({why}): {obj} sets back to {_fmt_band(heat, cool)}{extra}."
    return UnitTarget(unit_key=unit_key, heat_f=heat, cool_f=cool, rule="house_setback", reason=reason)


def _steer(
    band: ComfortBand, rooms: list[RoomStatus], priority_key: str | None, deadband: float,
) -> tuple[float, float, list[str], str | None]:
    """Setpoints that keep every steering room in band given its learned offset from the
    thermostat's average. Returns (heat, cool, notes, room steered for)."""
    offs = {r.room_key: _clip(r.offset_f) for r in rooms}
    if not offs:
        return band.heat_f, band.cool_f, [], None
    names = {r.room_key: r.name for r in rooms}
    warm = max(offs, key=lambda k: offs[k])
    cold = min(offs, key=lambda k: offs[k])
    cool_adj, heat_adj = offs[warm], offs[cold]
    notes: list[str] = []
    if band.cool_f - cool_adj - (band.heat_f - heat_adj) < deadband and priority_key in offs:
        cool_adj = heat_adj = offs[priority_key]
        warm = cold = priority_key
        notes.append(f"the rooms disagree by more than the deadband, so it steers for {_the(names[priority_key])}")
    if abs(cool_adj) >= 0.05:
        if cool_adj > 0:
            notes.append(f"cooling {cool_adj:.1f}°F lower because {_the(names[warm])} runs warm")
        else:
            notes.append(f"cooling {-cool_adj:.1f}°F higher because every occupied room runs cooler than the thermostat")
    if abs(heat_adj) >= 0.05:
        if heat_adj < 0:
            notes.append(f"heating {-heat_adj:.1f}°F higher because {_the(names[cold])} runs cool")
        else:
            notes.append(f"heating {heat_adj:.1f}°F lower because every occupied room runs warmer than the thermostat")
    return band.heat_f - heat_adj, band.cool_f - cool_adj, notes, (warm if cool_adj >= -heat_adj else cold)


def _precool_applies(ctx: _Ctx) -> bool:
    st = ctx.state
    p = st.policy
    if not p.precool_enabled or st.forecast_sunny is not True or st.forecast_high_f is None:
        return False
    if st.forecast_high_f < p.precool_min_forecast_high_f:
        return False
    return time(p.precool_start_hour, 0) <= ctx.local.time() < parse_hhmm(st.control.schedule.evening_start)


def _apply_recovery(ctx: _Ctx, targets: dict[str, UnitTarget]) -> None:
    """Rule 3 recovery: the main floor leaves setback first; the upstairs follows
    recovery_lead_min after the house stopped being empty, unless someone is upstairs."""
    st = ctx.state
    lead = st.policy.recovery_lead_min
    if st.house_empty or lead <= 0 or "up" not in targets or "main" not in targets:
        return
    if ctx.periods.get("up") != "day" or ctx.occupied.get("up") or targets["up"].rule == "hold_off":
        return
    up = st.units.get("up")
    away = _band(st.control, "up", "away")
    snap = up.snapshot if up else None
    if snap is None or snap.cool_sp_f is None or snap.heat_sp_f is None:
        return
    if not (snap.cool_sp_f >= away.cool_f - 0.5 and snap.heat_sp_f <= away.heat_f + 0.5):
        return  # upstairs is not sitting at its setback, nothing to stagger
    start = _presence_start(st)
    if start is None:
        return
    until = start + timedelta(minutes=lead)
    if st.now >= until:
        return
    t0, t1 = _clock(start, st.tz), _clock(until, st.tz)
    targets["up"] = UnitTarget(
        unit_key="up", heat_f=away.heat_f, cool_f=away.cool_f, rule="recovery",
        reason=(f"House occupied again at {t0}: the main floor recovers first; upstairs stays set back "
                f"({_fmt_band(away.heat_f, away.cool_f)}) until {t1} with nobody up there."),
    )
    main = targets["main"]
    if main.rule == "comfort":
        targets["main"] = main.model_copy(update={
            "rule": "recovery",
            "reason": (f"House occupied again at {t0}: the main floor recovers first, to "
                       f"{_fmt_band(_round_half(main.heat_f), _round_half(main.cool_f))}; upstairs follows at {t1}."),
        })


PREP_LEAD = timedelta(minutes=15)  # the pre-conditioning window opens this long before its hold could start
PREP_MIN_HOLD = timedelta(hours=1)  # the shortest hold (holdHours 1) must fit before the event
PREP_END_SLACK = timedelta(minutes=5)  # a running hold of ours that ends this close to end_by is the prep hold
AUTO_COOLING_OUTDOOR_F = 65.0  # 'auto' mode with no direction from the event: cooling at or above this


def _apply_event_prep(ctx: _Ctx, targets: dict[str, UnitTarget]) -> None:
    """Rule 6, after the normal rules: pre-cool (cooling) or pre-heat (heating) a unit before
    an announced utility event, so the house starts the event cooler (warmer) while the event
    itself is applied to the normal setpoint.

    Only for the unit's NEXT event (``next_utility_event``: the earliest announced one that has
    not started, a pending skip included), never a later one: a hold sized for a later event
    would still run when the earlier one starts. ``end_by = e.start_at -
    precondition_end_gap_min``; inside the window
    ``[end_by - precondition_hours h - 15 min, end_by)`` the target becomes rule 'event_prep',
    ``desired='hold'``, ``hold_end_by=end_by``, with cool_f lowered (heat_f raised) by
    ``precondition_degrees_f``. The controller sizes the hold so it ends by ``end_by`` (it
    lapses on its own, never relied on to be resumed). No prep when that event has a skip
    requested (it may still run if the opt-out fails, and no later event is prepped for in
    the meantime), when a one-hour hold no longer fits before ``end_by``, unless our prep hold
    is already running (it is then left to lapse, instead of being replaced by a normal hold
    that would run into the event). A unit whose thermostat is off, or whose direction cannot
    be told (``_prep_direction``), is left as is. A prep hold of ours that would still run
    when the next event starts (the event was announced or moved earlier) is the controller's
    to pull back (``controller._evaluate``)."""
    st = ctx.state
    cfg = st.utility
    if not cfg.precondition:
        return
    for unit_key, target in list(targets.items()):
        if target.rule == "hold_off":
            continue
        ev = next_utility_event(st, unit_key)
        if ev is None or ev.skip == "requested":
            continue
        end_by = ev.start_at - timedelta(minutes=cfg.precondition_end_gap_min)  # type: ignore[operator]
        start = end_by - timedelta(hours=cfg.precondition_hours) - PREP_LEAD
        if not start <= st.now < end_by:
            continue
        if end_by - st.now < PREP_MIN_HOLD and not _prep_hold_running(st, unit_key, start, end_by):
            continue
        direction = _prep_direction(st, unit_key, ev)
        if direction is None:
            continue
        deg = cfg.precondition_degrees_f
        heat, cool = target.heat_f, target.cool_f
        if direction == "cool":
            cool -= deg
        else:
            heat += deg
        verb = "Pre-cooling" if direction == "cool" else "Pre-heating"
        reason = (f"{verb} {deg:g}°F before the utility event at {_clock(ev.start_at, st.tz)}; this hold ends by "  # type: ignore[arg-type]
                  f"{_clock(end_by, st.tz)}, before the event starts.")
        targets[unit_key] = target.model_copy(update={
            "heat_f": heat, "cool_f": cool, "rule": "event_prep", "reason": reason, "desired": "hold",
            "hold_end_by": end_by,
        })


def _cap_precool_before_events(ctx: _Ctx, targets: dict[str, UnitTarget]) -> None:
    """The hot-day pre-cool (rule 4) lowers the cooling setpoint too, so a hold written for it
    must also end before the unit's next utility event (``precondition_end_gap_min`` before
    its start), whether or not pre-conditioning is on: no lowered hold of the controller's runs
    into an event. The controller sizes the hold to fit or writes nothing, and pulls back one
    that would still run (``controller._prep_pull_back``)."""
    st = ctx.state
    for unit_key, target in list(targets.items()):
        if target.rule != "precool" or target.hold_end_by is not None:
            continue
        ev = next_utility_event(st, unit_key)
        if ev is None or ev.start_at is None:
            continue
        end_by = ev.start_at - timedelta(minutes=st.utility.precondition_end_gap_min)
        reason = (f"{target.reason} This hold ends by {_clock(end_by, st.tz)}, before the utility event at "
                  f"{_clock(ev.start_at, st.tz)}.")
        targets[unit_key] = target.model_copy(update={"hold_end_by": end_by, "reason": reason})


def next_utility_event(state: HouseState, unit_key: str) -> UtilityEventState | None:
    """The unit's earliest announced utility event that has not started yet (start after now),
    unless it was opted out of. One with a skip requested counts: the opt-out can still fail
    or be refused, and the event then runs."""
    evs = [
        e for e in state.utility_events
        if e.unit_key == unit_key and e.status == "announced" and e.start_at is not None
        and e.start_at > state.now and e.skip != "done"
    ]
    return min(evs, key=lambda e: e.start_at, default=None)  # type: ignore[arg-type, return-value]


def _prep_hold_running(state: HouseState, unit_key: str, start: datetime, end_by: datetime) -> bool:
    """Our hold is running and was written for this prep: it started inside the window and
    ends by ``end_by``."""
    unit = state.units.get(unit_key)
    hold = unit.snapshot.hold if unit is not None and unit.snapshot is not None else None
    if hold is None or not hold.set_by_us or hold.end is None:
        return False
    if hold.start is not None and hold.start < start - PREP_END_SLACK:
        return False
    return state.now < hold.end <= end_by + PREP_END_SLACK


def _prep_direction(state: HouseState, unit_key: str, ev: UtilityEventState) -> Literal["cool", "heat"] | None:
    """Cooling or heating, from what the unit is doing now: its mode (cool -> cooling; heat /
    auxHeatOnly -> heating; auto -> cooling at or above 65°F outdoors, heating below). The
    event's own direction (a positive cool offset, a cool setpoint or the AC off -> cooling; a
    negative heat offset, a heat setpoint or the heat off -> heating), when it names exactly
    one, must agree: a cooling event on a unit that is heating gets no pre-cooling (it would
    change nothing and say something untrue). With the unit's direction unknown the event's
    decides; None when neither tells."""
    cooling = ev.is_cool_off or (
        (ev.cool_offset_f or 0) > 0 if ev.is_relative else ev.cool_f is not None
    )
    heating = ev.is_heat_off or (
        (ev.heat_offset_f or 0) < 0 if ev.is_relative else ev.heat_f is not None
    )
    event_dir: Literal["cool", "heat"] | None = ("cool" if cooling else "heat") if cooling != heating else None
    unit_dir = _unit_direction(state, unit_key)
    if unit_dir is None:
        return event_dir
    if event_dir is not None and event_dir != unit_dir:
        return None
    return unit_dir


def _unit_direction(state: HouseState, unit_key: str) -> Literal["cool", "heat"] | None:
    unit = state.units.get(unit_key)
    snap = unit.snapshot if unit is not None else None
    mode = snap.hvac_mode if snap is not None else None
    if mode == "cool":
        return "cool"
    if mode in ("heat", "auxHeatOnly"):
        return "heat"
    if mode == "auto":
        outdoor = state.outdoor_temp_f
        if outdoor is None and snap is not None:
            outdoor = snap.outdoor_temp_f
        if outdoor is not None:
            return "cool" if outdoor >= AUTO_COOLING_OUTDOOR_F else "heat"
    return None


def _presence_start(state: HouseState) -> datetime | None:
    """When the house stopped being empty: the earliest of an occupied sensored room's
    ``since`` and the moment the phones came home."""
    starts = [
        r.since for r in state.rooms.values()
        if r.state == "occupied" and r.has_sensor and r.since is not None
    ]
    occ = state.occupancy
    if occ.phones_away is False and occ.phones_updated_at is not None:
        starts.append(occ.phones_updated_at)
    return min(starts) if starts else None


def _finish(ctx: _Ctx, t: UnitTarget) -> UnitTarget:
    """Round to the thermostat's 0.5°F and decide whether a hold is needed at all."""
    heat, cool = _round_half(t.heat_f), _round_half(t.cool_f)
    t = t.model_copy(update={"heat_f": heat, "cool_f": cool})
    if t.desired == "program":
        return t
    prog = _program_setpoints(ctx.state.units.get(t.unit_key))
    if prog is not None and abs(prog[0] - heat) <= _MATCH_F and abs(prog[1] - cool) <= _MATCH_F:
        t = t.model_copy(update={"desired": "program"})
    return t


def _program_setpoints(unit: UnitStatus | None) -> tuple[float, float] | None:
    """What the ecobee program holds right now, when we can tell."""
    snap = unit.snapshot if unit else None
    if snap is None:
        return None
    ph, pc = snap.settings.get("program_heat_f"), snap.settings.get("program_cool_f")
    if _is_number(ph) and _is_number(pc):
        return float(ph), float(pc)  # type: ignore[arg-type]
    if snap.hold is None and snap.heat_sp_f is not None and snap.cool_sp_f is not None:
        return snap.heat_sp_f, snap.cool_sp_f
    return None


def _is_number(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_in(room_key: str, rooms: list[RoomStatus]) -> bool:
    return any(r.room_key == room_key for r in rooms)


def _clip(offset: float | None) -> float:
    if offset is None:
        return 0.0
    return max(-_OFFSET_CAP_F, min(_OFFSET_CAP_F, float(offset)))


def _round_half(v: float) -> float:
    return round(v * 2) / 2


def _fmt_band(heat: float, cool: float) -> str:
    return f"{_round_half(heat):g}–{_round_half(cool):g}°F"


def _clock(ts: datetime, tz: str) -> str:
    local = to_local(ts, tz)
    return f"{local.hour % 12 or 12}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"


def _clock_hhmm(hhmm: str) -> str:
    t = parse_hhmm(hhmm)
    return f"{t.hour % 12 or 12}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


def _the(name: str) -> str:
    """'the Toy Room', 'the Girls' Room', but "Olive's Room"."""
    return name if "'s " in name else f"the {name}"


def _join(items: list[str]) -> str:
    items = list(dict.fromkeys(items))
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _lower_first(s: str) -> str:
    return s[:1].lower() + s[1:] if s else s


def _sentence(core: str, notes: list[str]) -> str:
    core = core.rstrip(".")
    return (f"{core}; {'; '.join(notes)}." if notes else f"{core}.")
