"""Hard limits, enforced in code on every write whoever asked for it (blueprint §2.6).

``check`` never raises: it clamps what it can and reports violations as sentences, and sets
``blocked_reason`` when nothing may be written right now (rate limit, a person's hold, the
back-off after a Resume, stale data, humidity guard, mode off, a vacation, utility or
unrecognised ecobee event). Whatever it returns with ``ok`` is inside the hard limits, on the
0.5°F grid and keeps the deadband.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any, get_args, get_origin

from pydantic import BaseModel, Field, ValidationError

from climate.control.policy import (
    CLAUDE_SIGNOFF_RANGES,
    OWNER_ONLY_PARAMS,
    PersonHold,
    PolicyParams,
    UnitStatus,
    UnitTarget,
    UtilityEventState,
)
from climate.sources.base import KNOWN_HOLD_TYPES
from climate.store.app_settings import HardLimits
from climate.timeutil import to_local

STALE_AFTER = timedelta(minutes=15)
_EPS = 0.01
# Running ecobee events the controller never overrides (event type -> how it is worded).
PROTECTED_EVENTS = {"vacation": "vacation", "demandResponse": "utility demand-response"}


class GuardResult(BaseModel):
    ok: bool
    heat_f: float  # after clamping
    cool_f: float
    violations: list[str] = Field(default_factory=list)
    clamped: bool = False
    blocked_reason: str | None = None


def round_setpoint(value_f: float) -> float:
    """Round to the 0.5°F the thermostats display, BEFORE converting to tenths."""
    return round(value_f * 2) / 2


def check(
    target: UnitTarget,
    unit: UnitStatus,
    limits: HardLimits,
    now: datetime,
    *,
    mode: str | None = None,
    act_units: list[str] | None = None,
    enforce_rate_limit: bool = True,
    enforce_manual_backoff: bool = True,
    enforce_step: bool = True,
    enforce_humidity: bool = True,
    events: list[UtilityEventState] | None = None,
    homekit_data_ok: bool = False,
    tz: str | None = None,
) -> GuardResult:
    """Clamp to [min,max] heat/cool, keep cool - heat >= min_deadband_f (and the unit's
    heatCoolMinDelta setting if reported), limit each step to max_step_f from the current
    setpoint, block if the last write was < min_minutes_between_changes ago, block while a
    person's hold runs (``unit.person_hold``) and during the back-off after a Resume
    (``unit.resume_backoff_until``), block raising cool_f while zone humidity > max_indoor_rh,
    block when the snapshot is older than 15 minutes or the unit is disconnected, and block
    while a vacation, utility demand-response or unrecognised ecobee event runs (never
    overridden): by the snapshot's top event, or by the clock for a utility event in
    ``events`` that covers ``now`` for this unit (the snapshot may lag).

    Keyword options (all default to the strictest behaviour except ``mode``):
    - ``mode``: the controller mode; 'off' blocks. With 'act' and ``act_units`` given, a
      unit outside the list is blocked (it stays suggest-only).
    - ``enforce_rate_limit`` / ``enforce_manual_backoff`` / ``enforce_step`` /
      ``enforce_humidity``: False only for the owner's own actions (the owner is the person;
      an owner hold gets exactly what was typed within the hard envelope: min/max, deadband,
      grid). With ``enforce_manual_backoff`` False the event blocks still apply and say to
      skip the utility event first: an owner hold over a running event would be an opt-out
      ecobee may not record as one.
    - ``events``: the house's utility events (``HouseState.utility_events``); rows for other
      units are ignored.
    - ``homekit_data_ok``: True only while the ecobee cloud circuit is open and HomeKit has
      taken over; otherwise a snapshot that came over HomeKit (no ecobee hold or program
      data) blocks the write.
    - ``tz``: house time zone, only used to word times in sentences.

    Clamp order: round -> hard min/max -> step limit from the current setpoint -> humidity
    (never raise cool while humid) -> deadband (moves the setpoint that changed; when both
    or neither changed, moves heat down unless the thermostat is heating) -> hard limits
    LAST. The hard limits always win: when the current setpoint sits so far outside them
    that the step limit cannot reach them, the value is clamped into [min, max] anyway and
    the violation is recorded, so nothing outside the hard limits is ever written. The
    deadband is then re-checked inside the limits; when they leave no room for it the
    result is blocked. Every value is on the thermostat's 0.5°F grid (limits that are not on
    the grid are tightened to the nearest grid point inside them).
    Clamping is computed even when blocked, so the plan view shows what would be written."""
    name = _unit_name(unit)
    owner = not enforce_manual_backoff
    violations: list[str] = []
    snap = unit.snapshot
    cur_heat = snap.heat_sp_f if snap is not None else None
    cur_cool = snap.cool_sp_f if snap is not None else None

    # --- blocking conditions ----------------------------------------------------------
    blocked: list[str] = []
    if mode == "off":
        blocked.append("The controller is off.")
    elif mode == "act" and act_units is not None and unit.unit_key not in act_units:
        blocked.append(f"The {name} thermostat is suggest-only: it is not in the list of units the controller may write to.")
    event: str | None = None
    if snap is None:
        blocked.append(f"No live data from the {name} thermostat yet.")
    else:
        if not snap.connected:
            blocked.append(f"The {name} thermostat is disconnected.")
        age = unit.age_s if unit.age_s is not None else (now - snap.ts).total_seconds()
        if age > STALE_AFTER.total_seconds():
            blocked.append(f"The {name} thermostat's data is {int(age // 60)} min old (more than 15 min).")
        if snap.source == "homekit" and not homekit_data_ok:
            blocked.append(
                f"The {name} thermostat's latest data came over HomeKit, which shows no ecobee holds; "
                "waiting for fresh ecobee cloud data."
            )
        event = snap.hold.hold_type if snap.hold is not None else None
        if event in PROTECTED_EVENTS:
            skip = " Skip the event first." if owner and event == "demandResponse" else ""
            blocked.append(
                f"A {PROTECTED_EVENTS[event]} event is running on the {name} thermostat; the controller never "
                f"overrides it.{skip}"
            )
        elif event is not None and event not in KNOWN_HOLD_TYPES:
            blocked.append(
                f"An unrecognised ecobee event ({event}) is running on the {name} thermostat; the app is hands-off "
                "until it ends."
            )
    running = [e for e in events or () if e.unit_key == unit.unit_key and e.covers(now)]
    if running and event != "demandResponse":
        ev = running[0]
        what = f"The utility event{f' {ev.name!r}' if ev.name else ''}"
        until = f" until {_clock(ev.end_at, tz, now)}" if ev.end_at is not None else ""
        skip = " Skip the event first." if owner else ""
        blocked.append(
            f"{what} is on for the {name} thermostat{until}; the controller stands aside while it runs.{skip}"
        )
    if enforce_manual_backoff and unit.person_hold is not None and (
        unit.person_hold.until is None or unit.person_hold.until > now
    ):
        blocked.append(person_hold_sentence(unit.person_hold, tz, now))
    if enforce_manual_backoff and unit.resume_backoff_until is not None and unit.resume_backoff_until > now:
        pressed = f" at {_clock(unit.resume_seen_at, tz, now)}" if unit.resume_seen_at is not None else ""
        blocked.append(
            f"Resume was pressed{pressed}: the {name} thermostat follows the ecobee schedule until "
            f"{_clock(unit.resume_backoff_until, tz, now)}, or until you choose Back to automatic."
        )
    if enforce_rate_limit and unit.last_change_at is not None:
        since = now - unit.last_change_at
        window = timedelta(minutes=limits.min_minutes_between_changes)
        if since < window:
            nxt = unit.last_change_at + window
            blocked.append(
                f"The {name} thermostat was changed {int(since.total_seconds() // 60)} min ago; at most one change per "
                f"{limits.min_minutes_between_changes} min (next at {_clock(nxt, tz)})."
            )

    # --- clamping ---------------------------------------------------------------------
    want_heat, want_cool = round_setpoint(target.heat_f), round_setpoint(target.cool_f)
    heat, cool = want_heat, want_cool
    bounds = _bounds(limits)
    lo_heat, hi_heat, lo_cool, hi_cool = bounds
    if lo_heat > hi_heat or lo_cool > hi_cool:
        blocked.append(f"The hard limits leave no valid setpoint for the {name} thermostat (minimum above maximum).")

    heat, cool = _hard_limits(heat, cool, bounds, violations)

    if enforce_step and cur_heat is not None and abs(heat - cur_heat) > limits.max_step_f + _EPS:
        stepped = _toward(cur_heat, heat, limits.max_step_f)
        violations.append(
            f"Heat moves at most {limits.max_step_f:g}°F per change: {stepped:g}°F now, on the way to {heat:g}°F."
        )
        heat = stepped
    if enforce_step and cur_cool is not None and abs(cool - cur_cool) > limits.max_step_f + _EPS:
        stepped = _toward(cur_cool, cool, limits.max_step_f)
        violations.append(
            f"Cool moves at most {limits.max_step_f:g}°F per change: {stepped:g}°F now, on the way to {cool:g}°F."
        )
        cool = stepped

    humid = (
        enforce_humidity and snap is not None and snap.zone_humidity is not None
        and snap.zone_humidity > limits.max_indoor_rh
    )
    cool_cap: float | None = None
    if humid and cur_cool is not None:
        cool_cap = math.floor(cur_cool * 2 + _EPS) / 2
        if cool > cool_cap + _EPS:
            violations.append(
                f"Indoor humidity {snap.zone_humidity:g}% is above {limits.max_indoor_rh:g}%: not raising the "
                f"cooling setpoint above {cool_cap:g}°F."
            )
            cool = cool_cap

    deadband = _deadband(limits, snap.settings if snap is not None else {})
    if cool - heat < deadband - _EPS:
        heat, cool = _fix_deadband(heat, cool, deadband, cur_heat, cur_cool, snap, cool_cap, bounds)
        violations.append(f"Kept the {deadband:g}°F gap between heat and cool: {heat:g}–{cool:g}°F.")

    # Hard limits LAST: they win over the step limit and the humidity guard (a current
    # setpoint far outside them cannot be stepped back inside in one change).
    heat, cool = _final_limits(heat, cool, bounds, limits.max_step_f, violations)
    if cool - heat < deadband - _EPS:
        heat, cool = _fix_deadband(heat, cool, deadband, cur_heat, cur_cool, snap, cool_cap, bounds)
        heat, cool = _final_limits(heat, cool, bounds, limits.max_step_f, violations)
        violations.append(f"Kept the {deadband:g}°F gap between heat and cool inside the hard limits: {heat:g}–{cool:g}°F.")
    if cool - heat < deadband - _EPS:
        blocked.append(
            f"The hard limits leave no room for a {deadband:g}°F gap between heat and cool on the {name} thermostat."
        )

    clamped = abs(heat - want_heat) > _EPS or abs(cool - want_cool) > _EPS
    if (
        cool_cap is not None and want_cool > cool_cap + _EPS and cur_heat is not None and cur_cool is not None
        and abs(heat - cur_heat) <= _EPS and abs(cool - cur_cool) <= 0.5
    ):
        # The humidity guard took away the only change the target asked for.
        blocked.append(
            f"Indoor humidity {snap.zone_humidity:g}% is above {limits.max_indoor_rh:g}%, so the cooling "  # type: ignore[union-attr]
            "setpoint may not be raised and nothing else would change."
        )
    blocked_reason = " ".join(blocked) if blocked else None
    return GuardResult(
        ok=blocked_reason is None,
        heat_f=heat,
        cool_f=cool,
        violations=violations,
        clamped=clamped,
        blocked_reason=blocked_reason,
    )


def validate_policy_params(params: dict, *, actor: str) -> list[str]:
    """Return violations for a partial PolicyParams proposal: unknown keys, out-of-type or
    out-of-field-range values; for actor == 'claude' also keys outside CLAUDE_SIGNOFF_RANGES
    and any OWNER_ONLY_PARAMS. Empty list = acceptable.

    An empty dict is acceptable here (an experiment's control arm changes nothing); callers
    that need at least one key check that themselves."""
    if not isinstance(params, dict):
        return ["Parameters must be an object mapping PolicyParams fields to values."]
    out: list[str] = []
    fields = PolicyParams.model_fields
    base = PolicyParams().model_dump()
    for key, value in params.items():
        if key not in fields:
            out.append(f"Unknown parameter '{key}'.")
            continue
        kind = _field_kind(fields[key].annotation)
        problem = _type_problem(key, value, kind)
        if problem:
            out.append(problem)
            continue
        try:
            PolicyParams.model_validate({**base, key: value})
        except ValidationError as exc:
            msg = exc.errors()[0].get("msg", "invalid value")
            out.append(f"{key} = {value!r} is not allowed: {msg[:1].lower() + msg[1:]}.")
            continue
        if actor == "claude":
            if key in OWNER_ONLY_PARAMS:
                out.append(f"{key} switches a rule on or off; only the owner can change it.")
            elif key not in CLAUDE_SIGNOFF_RANGES:
                out.append(f"{key} is outside what Claude may change; only the owner can.")
            else:
                lo, hi = CLAUDE_SIGNOFF_RANGES[key]
                if not lo <= float(value) <= hi:
                    out.append(f"{key} = {value:g} is outside Claude's sign-off range {lo:g}–{hi:g}.")
    return out


def within_signoff_ranges(params: dict) -> bool:
    """True when every key is in CLAUDE_SIGNOFF_RANGES and every value inside its range."""
    if not params:
        return False
    for key, value in params.items():
        if key not in CLAUDE_SIGNOFF_RANGES or not isinstance(value, (int, float)) or isinstance(value, bool):
            return False
        lo, hi = CLAUDE_SIGNOFF_RANGES[key]
        if not lo <= float(value) <= hi:
            return False
    return True


# ---------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------


def _unit_name(unit: UnitStatus) -> str:
    return {"main": "main floor", "up": "upstairs", "bed": "bed / office wing"}.get(
        unit.unit_key, unit.name or unit.unit_key
    )


def _clock(ts: datetime, tz: str | None, now: datetime | None = None) -> str:
    """'2:10 PM' in house time; 'Mon 2:10 PM' when ``now`` is given and the day differs."""
    if tz:
        local = to_local(ts, tz)
        text = f"{local.hour % 12 or 12}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"
        if now is not None and to_local(now, tz).date() != local.date():
            text = f"{local:%a} {text}"
        return text
    text = ts.strftime("%H:%M UTC")
    return f"{ts:%a} {text}" if now is not None and now.date() != ts.date() else text


def person_hold_sentence(hold: PersonHold, tz: str | None, now: datetime | None = None) -> str:
    """Why the controller waits on a person's hold: "On your hold since 2:10 PM (until you
    change it); the controller waits until it ends or you choose Back to automatic." ("until
    4:00 PM" for a timed hold; "your hold from the app" when it came from this app)."""
    whose = "your hold from the app" if hold.by == "app" else "your hold"
    since = _clock(hold.since, tz, now)
    until = f", until {_clock(hold.until, tz, now)}" if hold.until is not None else " (until you change it)"
    return f"On {whose} since {since}{until}; the controller waits until it ends or you choose Back to automatic."


def _toward(current: float, wanted: float, step: float) -> float:
    """Move from ``current`` toward ``wanted`` by at most ``step``, on the 0.5°F grid, never
    overshooting the step (round toward the current setpoint)."""
    if wanted > current:
        return math.floor((current + step) * 2 + _EPS) / 2
    return math.ceil((current - step) * 2 - _EPS) / 2


def _bounds(limits: HardLimits) -> tuple[float, float, float, float]:
    """(min heat, max heat, min cool, max cool) tightened onto the 0.5°F grid, inside the limits."""
    tol = 1e-6  # float noise only: a limit 0.003°F off the grid must not round outward
    return (
        math.ceil(limits.min_heat_f * 2 - tol) / 2,
        math.floor(limits.max_heat_f * 2 + tol) / 2,
        math.ceil(limits.min_cool_f * 2 - tol) / 2,
        math.floor(limits.max_cool_f * 2 + tol) / 2,
    )


def _hard_limits(
    heat: float, cool: float, bounds: tuple[float, float, float, float], violations: list[str],
) -> tuple[float, float]:
    lo_heat, hi_heat, lo_cool, hi_cool = bounds
    if heat < lo_heat - _EPS:
        violations.append(f"Heat {heat:g}°F raised to the {lo_heat:g}°F minimum.")
        heat = lo_heat
    elif heat > hi_heat + _EPS:
        violations.append(f"Heat {heat:g}°F lowered to the {hi_heat:g}°F maximum.")
        heat = hi_heat
    if cool < lo_cool - _EPS:
        violations.append(f"Cool {cool:g}°F raised to the {lo_cool:g}°F minimum.")
        cool = lo_cool
    elif cool > hi_cool + _EPS:
        violations.append(f"Cool {cool:g}°F lowered to the {hi_cool:g}°F maximum.")
        cool = hi_cool
    return heat, cool


def _final_limits(
    heat: float, cool: float, bounds: tuple[float, float, float, float], step: float, violations: list[str],
) -> tuple[float, float]:
    """The last clamp: a value the step limit (or the humidity guard) left outside the hard
    limits is moved inside them, and the reason is recorded."""
    lo_heat, hi_heat, lo_cool, hi_cool = bounds
    for label in ("Heat", "Cool"):
        value, lo, hi = (heat, lo_heat, hi_heat) if label == "Heat" else (cool, lo_cool, hi_cool)
        if lo > hi:
            continue  # no valid value at all; check() blocks
        if value < lo - _EPS:
            new, word = lo, "minimum"
        elif value > hi + _EPS:
            new, word = hi, "maximum"
        else:
            continue
        violations.append(
            f"{label} {value:g}°F would be outside the hard limits; the {new:g}°F {word} wins over the "
            f"{step:g}°F step limit."
        )
        if label == "Heat":
            heat = new
        else:
            cool = new
    return heat, cool


def _deadband(limits: HardLimits, settings: dict[str, Any]) -> float:
    """The larger of our minimum and the thermostat's heatCoolMinDelta. ecobee reports that
    setting in tenths of °F (e.g. 50 = 5°F); values above 20 are read as tenths."""
    db = limits.min_deadband_f
    raw = settings.get("heatCoolMinDelta") if isinstance(settings, dict) else None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
        delta = raw / 10 if raw > 20 else float(raw)
        db = max(db, delta)
    return db


def _fix_deadband(
    heat: float, cool: float, deadband: float, cur_heat: float | None, cur_cool: float | None,
    snap: Any, cool_cap: float | None, bounds: tuple[float, float, float, float],
) -> tuple[float, float]:
    lo_heat, _hi_heat, lo_cool, hi_cool = bounds
    heat_changed = cur_heat is None or abs(heat - cur_heat) > _EPS
    cool_changed = cur_cool is None or abs(cool - cur_cool) > _EPS
    heating = snap is not None and snap.hvac_mode in ("heat", "auxHeatOnly")
    if cool_changed and not heat_changed:
        move_cool = True
    elif heat_changed and not cool_changed:
        move_cool = False
    else:
        move_cool = heating
    if move_cool and cool_cap is not None:
        move_cool = False  # humid: cool may not go up, so heat gives way
    if move_cool:
        cool = min(math.ceil((heat + deadband) * 2 - _EPS) / 2, hi_cool)
        if cool - heat < deadband - _EPS:
            heat = max(math.floor((cool - deadband) * 2 + _EPS) / 2, lo_heat)
    else:
        heat = max(math.floor((cool - deadband) * 2 + _EPS) / 2, lo_heat)
        if cool - heat < deadband - _EPS:
            # humid: cool may rise only as far as the cap, unless the hard minimum forces more
            ceiling = hi_cool if cool_cap is None else max(lo_cool, min(hi_cool, cool_cap))
            cool = min(math.ceil((heat + deadband) * 2 - _EPS) / 2, ceiling)
    return heat, cool


def _field_kind(annotation: Any) -> type:
    if get_origin(annotation) is not None:
        args = [a for a in get_args(annotation) if a is not type(None)]
        annotation = args[0] if args else annotation
    return annotation if isinstance(annotation, type) else object


def _type_problem(key: str, value: Any, kind: type) -> str | None:
    if kind is bool:
        return None if isinstance(value, bool) else f"{key} must be true or false (got {value!r})."
    if kind in (int, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"{key} must be a number (got {value!r})."
        if not math.isfinite(float(value)):
            return f"{key} must be a finite number (got {value!r})."
        if kind is int and float(value) != int(value):
            return f"{key} must be a whole number (got {value!r})."
    return None
