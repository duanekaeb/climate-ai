"""Hard limits, enforced in code on every write whoever asked for it (blueprint §2.6).

``check`` never raises: it clamps what it can and reports violations as sentences, and sets
``blocked_reason`` when nothing may be written right now (rate limit, manual back-off,
stale data, humidity guard, mode off).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from climate.control.policy import UnitStatus, UnitTarget
from climate.store.app_settings import HardLimits


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


def check(target: UnitTarget, unit: UnitStatus, limits: HardLimits, now: datetime) -> GuardResult:
    """Clamp to [min,max] heat/cool, keep cool - heat >= min_deadband_f (and the unit's
    heatCoolMinDelta setting if reported), limit each step to max_step_f from the current
    setpoint, block if the last write was < min_minutes_between_changes ago, block during
    manual_override_until, block raising cool_f while zone humidity > max_indoor_rh, block
    when the snapshot is older than 15 minutes or the unit is disconnected."""
    raise NotImplementedError


def validate_policy_params(params: dict, *, actor: str) -> list[str]:
    """Return violations for a partial PolicyParams proposal: unknown keys, out-of-type or
    out-of-field-range values; for actor == 'claude' also keys outside CLAUDE_SIGNOFF_RANGES
    and any OWNER_ONLY_PARAMS. Empty list = acceptable."""
    raise NotImplementedError
