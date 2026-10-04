"""Typed runtime settings stored as JSONB rows in ``app_settings``.

Every key has a Pydantic model with defaults, so a missing row reads as the defaults.
The owner edits these from the web app; the models and Claude never write them directly
(their changes go through ``changes`` and ``policy_versions``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, TypeVar

from pydantic import AliasChoices, BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from climate.store.orm import AppSetting

HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"

# ---------------------------------------------------------------------------------------
# control
# ---------------------------------------------------------------------------------------


class ComfortBand(BaseModel):
    """Setpoints for one period. heat_f < cool_f by at least the thermostat's min delta."""

    heat_f: float = Field(ge=45, le=80)
    cool_f: float = Field(ge=65, le=92)


class UnitComfort(BaseModel):
    day: ComfortBand
    night: ComfortBand  # sleep comfort; applies inside the unit's sleep rooms' windows
    away: ComfortBand  # whole house empty (subject to linked setback)


class HardLimits(BaseModel):
    """Enforced in code on every write, whoever asked for it. Owner-editable only."""

    min_heat_f: float = 60.0
    max_heat_f: float = 74.0
    min_cool_f: float = 70.0
    max_cool_f: float = 84.0
    min_deadband_f: float = 3.0  # cool - heat; ecobee's heatCoolMinDelta must also hold
    max_step_f: float = 2.0  # largest change per write vs the current setpoint
    min_minutes_between_changes: int = 30  # per unit
    min_hold_hours: int = 1
    max_hold_hours: int = 2
    max_indoor_rh: float = 58.0  # don't raise cooling setpoints while humidity is above this
    min_run_minutes: int = 5  # short-cycle guard (tracked; ecobee enforces its own too)


DEFAULT_COMFORT: dict[str, UnitComfort] = {
    "main": UnitComfort(
        day=ComfortBand(heat_f=68, cool_f=76), night=ComfortBand(heat_f=67, cool_f=74), away=ComfortBand(heat_f=62, cool_f=80)
    ),
    "up": UnitComfort(
        day=ComfortBand(heat_f=68, cool_f=77), night=ComfortBand(heat_f=67, cool_f=74), away=ComfortBand(heat_f=62, cool_f=82)
    ),
    "bed": UnitComfort(
        day=ComfortBand(heat_f=68, cool_f=76), night=ComfortBand(heat_f=66, cool_f=73), away=ComfortBand(heat_f=62, cool_f=80)
    ),
}


class Schedule(BaseModel):
    """Household rhythm used for room priorities (blueprint §3). Days: Monday=0 .. Sunday=6."""

    school_days: list[int] = [0, 1, 2, 3, 4]
    school_start: str = Field(default="08:00", pattern=HHMM)
    school_end: str = Field(default="15:00", pattern=HHMM)
    office_days: list[int] = [0, 1, 2, 3, 4]
    office_start: str = Field(default="08:00", pattern=HHMM)
    office_end: str = Field(default="17:00", pattern=HHMM)
    evening_start: str = Field(default="17:00", pattern=HHMM)


class ControlSettings(BaseModel):
    mode: Literal["off", "suggest", "act"] = "suggest"
    comfort: dict[str, UnitComfort] = Field(default_factory=lambda: dict(DEFAULT_COMFORT))
    limits: HardLimits = Field(default_factory=HardLimits)
    schedule: Schedule = Field(default_factory=Schedule)
    hold_hours: int = Field(default=2, ge=1, le=2)
    # A hold a person set (at the thermostat, in the ecobee app, Apple Home, or this app's
    # hold form) always wins for as long as it runs; nothing here limits it. This back-off
    # applies only after someone presses Resume (at the thermostat, in the ecobee app, or
    # this app's "Resume schedule"): the ecobee schedule then runs this long before the
    # controller writes again. Counted from when the resume was first seen. (Stored rows from
    # before the rename carry it as ``manual_backoff_hours``.)
    resume_backoff_hours: float = Field(
        default=4.0, ge=0, le=24, validation_alias=AliasChoices("resume_backoff_hours", "manual_backoff_hours")
    )
    # Push a reminder when a person's hold has run this long (0 = never). It only reminds;
    # the controller still waits for the hold to end or for "Back to automatic".
    manual_hold_reminder_hours: float = Field(default=0.0, ge=0, le=72)
    # Units the controller may write to in "act" mode (others stay suggest-only).
    act_units: list[str] = ["main", "up", "bed"]


# ---------------------------------------------------------------------------------------
# utility (demand-response) events
# ---------------------------------------------------------------------------------------


class UtilityEventSettings(BaseModel):
    """What the app does around utility energy-saving (demand-response) events.

    The app never counteracts an event while staying in it: during a running event the
    controller stands down. The only ways out are honest opt-outs (ecobee records them and
    the utility sees them): the owner's "Skip this event", or a skip rule below. Pre-
    conditioning happens only BEFORE an announced event, and its hold always ends at least
    ``precondition_end_gap_min`` before the event starts, so the event is applied to the
    normal setpoint, never to a lowered (or raised) one."""

    alerts: bool = True  # push when an event is announced, starts, ends or is skipped
    # Automatic skip rules (honest opt-outs). In 'suggest' mode a matching rule only alerts.
    auto_skip: bool = False
    skip_when_asleep: bool = True  # someone is asleep in a sleep room on that unit
    skip_above_f: float | None = Field(default=None, ge=72, le=90)  # an occupied/asleep room reaches this (cooling)
    skip_below_f: float | None = Field(default=None, ge=55, le=70)  # an occupied/asleep room falls to this (heating)
    # Pre-cool (cooling) / pre-heat (heating) before an announced event.
    precondition: bool = False
    precondition_degrees_f: float = Field(default=2.0, ge=0.5, le=3.0)
    precondition_hours: int = Field(default=2, ge=1, le=2)  # one holdHours hold of this length
    precondition_end_gap_min: int = Field(default=10, ge=10, le=60)  # the hold ends this long before the start


# ---------------------------------------------------------------------------------------
# occupancy
# ---------------------------------------------------------------------------------------


class SleepWindow(BaseModel):
    start: str = Field(pattern=HHMM)  # local time the window starts
    end: str = Field(pattern=HHMM)  # local time it ends (may be next morning)
    days: list[int] = [0, 1, 2, 3, 4, 5, 6]  # weekday the window STARTS on

    @field_validator("days")
    @classmethod
    def _days(cls, v: list[int]) -> list[int]:
        if not v or any(d < 0 or d > 6 for d in v):
            raise ValueError("days must be 0..6 (Monday=0)")
        return sorted(set(v))


# Placeholder bedtimes until the owner confirms them (open question 9).
DEFAULT_SLEEP_WINDOWS: dict[str, list[SleepWindow]] = {
    "girls_room": [SleepWindow(start="20:30", end="07:00")],
    "twins_room": [SleepWindow(start="20:00", end="07:00")],
    "olive_room": [SleepWindow(start="20:00", end="07:00")],
    "bedroom": [SleepWindow(start="22:00", end="06:30")],
}


class OccupancySettings(BaseModel):
    empty_after_min: int = Field(default=30, ge=5, le=240)  # no signal this long -> empty
    house_empty_after_min: int = Field(default=45, ge=15, le=480)
    sleep_windows: dict[str, list[SleepWindow]] = Field(default_factory=lambda: dict(DEFAULT_SLEEP_WINDOWS))
    # Adults' phones: True = all away, False = someone home, None = unknown (treated as home).
    phones_away: bool | None = None
    phones_updated_at: datetime | None = None


# ---------------------------------------------------------------------------------------
# location / source / agent / owner
# ---------------------------------------------------------------------------------------


class LocationSettings(BaseModel):
    lat: float | None = None
    lon: float | None = None
    tz: str = "America/Chicago"
    zip: str | None = None
    label: str | None = None
    confirmed: bool = False


class EcobeeOriginal(BaseModel):
    """A unit's ecobee settings as they were before the controller first changed them, so
    "Hand back to ecobee" can restore them. Captured once per unit (first ecobee snapshot,
    and again just before any first write that would change one of them if still missing)."""

    captured_at: datetime
    auto_away: bool | None = None  # settings.autoAway (Smart Away)
    follow_me: bool | None = None  # settings.followMeComfort (Follow Me)
    home_sensors: list[str] | None = None  # sensor keys in the Home comfort setting


class SourceSettings(BaseModel):
    kind: Literal["simulator", "ecobee"] = "simulator"
    homekit_enabled: bool = False
    # Circuit breaker: while set and in the future, cloud writes fall back to HomeKit.
    cloud_circuit_open_until: datetime | None = None


class AgentSettings(BaseModel):
    enabled: bool = True
    nightly_time: str = Field(default="03:30", pattern=HHMM)
    weekly_day: int = Field(default=6, ge=0, le=6)  # Sunday
    weekly_time: str = Field(default="05:00", pattern=HHMM)
    max_triggered_per_day: int = Field(default=3, ge=0, le=3)


class OwnerSettings(BaseModel):
    password_hash: str | None = None
    # Bumped on password change and "sign out everywhere"; part of every session cookie.
    session_epoch: int = 0


class Heartbeat(BaseModel):
    """Written by long-running services ("worker", "homekit", "agent") every loop."""

    at: datetime
    ok: bool = True
    detail: dict[str, Any] = Field(default_factory=dict)


SETTINGS_MODELS: dict[str, type[BaseModel]] = {
    "control": ControlSettings,
    "occupancy": OccupancySettings,
    "location": LocationSettings,
    "source": SourceSettings,
    "agent": AgentSettings,
    "owner": OwnerSettings,
    "utility_events": UtilityEventSettings,
}
# Non-settings rows (not owner-editable): 'ecobee_original' = {unit_key: EcobeeOriginal},
# 'ecobee_holds' (the ecobee adapter's record of our last hold), 'heartbeat:<service>'.
ECOBEE_ORIGINAL_KEY = "ecobee_original"

M = TypeVar("M", bound=BaseModel)


def get_setting(session: Session, key: str, model: type[M]) -> M:
    raw = session.execute(select(AppSetting.value).where(AppSetting.key == key)).scalar_one_or_none()
    if raw is None:
        return model()  # type: ignore[call-arg]
    return model.model_validate(raw)


def put_setting(session: Session, key: str, value: BaseModel | dict[str, Any], updated_by: str = "system") -> None:
    data = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    stmt = insert(AppSetting).values(key=key, value=data, updated_by=updated_by)
    stmt = stmt.on_conflict_do_update(
        index_elements=[AppSetting.key], set_={"value": data, "updated_by": updated_by, "updated_at": func.now()}
    )
    session.execute(stmt)


def get_raw(session: Session, key: str) -> Any | None:
    return session.execute(select(AppSetting.value).where(AppSetting.key == key)).scalar_one_or_none()


def beat(session: Session, service: str, ok: bool = True, **detail: Any) -> None:
    """Record a service heartbeat under key ``heartbeat:<service>``."""
    from climate.timeutil import utcnow

    put_setting(session, f"heartbeat:{service}", Heartbeat(at=utcnow(), ok=ok, detail=detail), updated_by=service)


def get_heartbeat(session: Session, service: str) -> Heartbeat | None:
    raw = get_raw(session, f"heartbeat:{service}")
    return None if raw is None else Heartbeat.model_validate(raw)
