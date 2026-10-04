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
from climate.store.app_settings import ControlSettings, OccupancySettings

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


class UnitStatus(BaseModel):
    unit_key: str
    name: str
    snapshot: UnitSnapshot | None = None
    age_s: float | None = None  # seconds since snapshot.ts
    call: Literal["cool", "heat", "fan", "idle", "unknown"] = "unknown"
    last_change_at: datetime | None = None  # last control_action we wrote (status verified/sent)
    manual_override_until: datetime | None = None  # back-off after someone changed it by hand


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


class UnitTarget(BaseModel):
    unit_key: str
    heat_f: float
    cool_f: float
    rule: Literal[
        "comfort", "sleep", "linked_floors", "house_setback", "recovery", "precool", "independent", "hold_off"
    ]
    reason: str  # one human sentence, e.g. "Main floor empty, Toy Room occupied: main 1°F under upstairs"
    priority_room: str | None = None
    # 'program' = no hold needed (the ecobee schedule already matches); controller resumes or leaves it.
    desired: Literal["hold", "program"] = "hold"


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
    Rooms without a sensor never contribute a temperature; unknown/stale data -> occupied.
    """
    raise NotImplementedError
