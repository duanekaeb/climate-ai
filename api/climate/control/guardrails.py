"""Hard limits, enforced in code on every write whoever asked for it (blueprint §2.6).

``check`` never raises: it clamps what it can and reports violations as sentences, and sets
``blocked_reason`` when nothing may be written right now (rate limit, manual back-off,
stale data, humidity guard, mode off).
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any, get_args, get_origin

from pydantic import BaseModel, Field, ValidationError

from climate.control.policy import (
    CLAUDE_SIGNOFF_RANGES,
    OWNER_ONLY_PARAMS,
    PolicyParams,
    UnitStatus,
    UnitTarget,
)
from climate.store.app_settings import HardLimits
from climate.timeutil import to_local

STALE_AFTER = timedelta(minutes=15)
_EPS = 0.01


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
    tz: str | None = None,
) -> GuardResult:
    """Clamp to [min,max] heat/cool, keep cool - heat >= min_deadband_f (and the unit's
    heatCoolMinDelta setting if reported), limit each step to max_step_f from the current
    setpoint, block if the last write was < min_minutes_between_changes ago, block during
    manual_override_until, block raising cool_f while zone humidity > max_indoor_rh, block
    when the snapshot is older than 15 minutes or the unit is disconnected.

    Keyword options (all default to the strictest behaviour except ``mode``):
    - ``mode``: the controller mode; 'off' blocks. With 'act' and ``act_units`` given, a
      unit outside the list is blocked (it stays suggest-only).
    - ``enforce_rate_limit`` / ``enforce_manual_backoff``: False only for owner holds (the
      owner is the manual actor).
    - ``tz``: house time zone, only used to word times in sentences.

    Clamp order: round -> hard min/max -> step limit from the current setpoint -> humidity
    (never raise cool while humid) -> deadband (moves the setpoint that changed; when both
    or neither changed, moves heat down unless the thermostat is heating) -> hard limits again.
    Clamping is computed even when blocked, so the plan view shows what would be written."""
    name = _unit_name(unit)
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
    if snap is None:
        blocked.append(f"No live data from the {name} thermostat yet.")
    else:
        if not snap.connected:
            blocked.append(f"The {name} thermostat is disconnected.")
        age = unit.age_s if unit.age_s is not None else (now - snap.ts).total_seconds()
        if age > STALE_AFTER.total_seconds():
            blocked.append(f"The {name} thermostat's data is {int(age // 60)} min old (more than 15 min).")
    if enforce_manual_backoff and unit.manual_override_until is not None and unit.manual_override_until > now:
        blocked.append(
            f"Someone changed the {name} thermostat by hand; backing off until "
            f"{_clock(unit.manual_override_until, tz)}."
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

    heat, cool = _hard_limits(heat, cool, limits, violations)

    if cur_heat is not None and abs(heat - cur_heat) > limits.max_step_f + _EPS:
        stepped = _toward(cur_heat, heat, limits.max_step_f)
        violations.append(
            f"Heat moves at most {limits.max_step_f:g}°F per change: {stepped:g}°F now, on the way to {heat:g}°F."
        )
        heat = stepped
    if cur_cool is not None and abs(cool - cur_cool) > limits.max_step_f + _EPS:
        stepped = _toward(cur_cool, cool, limits.max_step_f)
        violations.append(
            f"Cool moves at most {limits.max_step_f:g}°F per change: {stepped:g}°F now, on the way to {cool:g}°F."
        )
        cool = stepped

    humid = (
        snap is not None and snap.zone_humidity is not None and snap.zone_humidity > limits.max_indoor_rh
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
        heat, cool = _fix_deadband(heat, cool, deadband, cur_heat, cur_cool, snap, cool_cap, limits)
        violations.append(f"Kept the {deadband:g}°F gap between heat and cool: {heat:g}–{cool:g}°F.")
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


def _clock(ts: datetime, tz: str | None) -> str:
    if tz:
        local = to_local(ts, tz)
        return f"{local.hour % 12 or 12}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"
    return ts.strftime("%H:%M UTC")


def _toward(current: float, wanted: float, step: float) -> float:
    """Move from ``current`` toward ``wanted`` by at most ``step``, on the 0.5°F grid, never
    overshooting the step (round toward the current setpoint)."""
    if wanted > current:
        return math.floor((current + step) * 2 + _EPS) / 2
    return math.ceil((current - step) * 2 - _EPS) / 2


def _hard_limits(heat: float, cool: float, limits: HardLimits, violations: list[str]) -> tuple[float, float]:
    if heat < limits.min_heat_f - _EPS:
        violations.append(f"Heat {heat:g}°F raised to the {limits.min_heat_f:g}°F minimum.")
        heat = limits.min_heat_f
    elif heat > limits.max_heat_f + _EPS:
        violations.append(f"Heat {heat:g}°F lowered to the {limits.max_heat_f:g}°F maximum.")
        heat = limits.max_heat_f
    if cool < limits.min_cool_f - _EPS:
        violations.append(f"Cool {cool:g}°F raised to the {limits.min_cool_f:g}°F minimum.")
        cool = limits.min_cool_f
    elif cool > limits.max_cool_f + _EPS:
        violations.append(f"Cool {cool:g}°F lowered to the {limits.max_cool_f:g}°F maximum.")
        cool = limits.max_cool_f
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
    snap: Any, cool_cap: float | None, limits: HardLimits,
) -> tuple[float, float]:
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
        cool = min(math.ceil((heat + deadband) * 2 - _EPS) / 2, limits.max_cool_f)
        if cool - heat < deadband - _EPS:
            heat = max(math.floor((cool - deadband) * 2 + _EPS) / 2, limits.min_heat_f)
    else:
        heat = max(math.floor((cool - deadband) * 2 + _EPS) / 2, limits.min_heat_f)
        if cool - heat < deadband - _EPS:
            ceiling = limits.max_cool_f if cool_cap is None else min(limits.max_cool_f, cool_cap)
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
