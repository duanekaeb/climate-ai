"""The thermostat-source contract shared by the simulator and the ecobee cloud adapter.

The worker owns exactly one ``ThermostatSource`` at a time (``climate.sources.get_source``).
HomeKit is not a ThermostatSource: it is a separate host-networked service that pushes live
sensor values into ``live_sensors`` / ``occupancy_events`` and executes queued HomeKit holds
(see ``climate.sources.homekit``).

Units are identified by our keys ('main', 'up', 'bed'); sensors by our sensor keys
(``climate.house.SENSORS``). Adapters translate vendor ids to these keys using the mapping
columns on ``units`` / ``sensors`` (and auto-map by name when unmapped).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

HvacMode = Literal["heat", "cool", "auto", "off", "auxHeatOnly"]


class SensorReading(BaseModel):
    sensor_key: str
    ts: datetime
    temp_f: float | None = None
    humidity: float | None = None
    occupied: bool | None = None
    motion: bool | None = None
    seconds_since_motion: int | None = None
    seconds_since_occupancy: int | None = None
    online: bool = True
    battery_low: bool | None = None


# HoldInfo.hold_type vocabulary (see climate.sources.ecobee_parse for how it is inferred):
# a person's or the controller's plain hold, by how it was set ...
PLAIN_HOLD_TYPES = ("holdHours", "nextTransition", "indefinite", "dateTime")
# ... ecobee's own automatic Smart Away / Smart Home (not a person; the controller may take it back) ...
AUTO_EVENTS = ("autoAway", "autoHome")
# ... a person pressed Quick Save (Smart Away's manual button): a person's hold ...
PERSON_EVENTS = ("quickSave",)
# ... and events the controller NEVER overrides (it stands down while one runs).
PROTECTED_EVENTS = ("vacation", "demandResponse")
KNOWN_HOLD_TYPES = (*PLAIN_HOLD_TYPES, *AUTO_EVENTS, *PERSON_EVENTS, *PROTECTED_EVENTS)
# Any other running ecobee event type ('sensor', 'switchOccupancy', 'today', or one ecobee adds
# later) arrives verbatim in hold_type: the controller treats the unit as hands-off and alerts.

# A source's refusal to cancel a hold it did not write (``resume_program`` without ``force``).
NOT_OURS_ERROR = "the running hold was not set by the controller; not cancelled"


class HoldInfo(BaseModel):
    """What overrides the thermostat's schedule right now (the top running ecobee event)."""

    kind: Literal["temperature", "climate"] = "temperature"
    heat_f: float | None = None  # absolute setpoints; None for a relative event
    cool_f: float | None = None
    climate_ref: str | None = None
    start: datetime | None = None
    end: datetime | None = None  # None = no end reported; indefinite holds report year 2035+
    hold_type: str | None = None  # PLAIN_HOLD_TYPES or the ecobee event type (see above)
    set_by_us: bool = False  # True when it matches a hold the controller wrote
    # Event details (ecobee events; None/False for plain holds)
    event_name: str | None = None  # e.g. the utility program's event name
    is_relative: bool = False  # setpoints are offsets from the scheduled comfort setting
    heat_offset_f: float | None = None  # relative heat change in °F (e.g. -2.0)
    cool_offset_f: float | None = None  # relative cool change in °F (e.g. +2.0)
    is_optional: bool | None = None  # demand response: True = the owner may opt out
    link_ref: str | None = None  # ecobee's id linking an event and its alerts (stable per event)


def write_refusal(hold: HoldInfo | None, *, by_owner: bool) -> tuple[dict[str, object], str] | None:
    """Why a hold or sensor-set write must not go out over ``hold`` (the running top override,
    read fresh just before the write): (extra ``request`` fields, error), or None when it may.

    Nothing running, Smart Away / Home and the controller's own plain hold never refuse. A
    plain hold the controller did not write, or a Quick Save, is a person's: refused for the
    controller (``not_ours``), allowed for the owner (``by_owner``: the owner is the person).
    Vacation, demand response and unknown events are refused for everyone (``event``)."""
    if hold is None or hold.hold_type in AUTO_EVENTS:
        return None
    etype = hold.hold_type or "unknown"
    if etype in PLAIN_HOLD_TYPES or etype in PERSON_EVENTS:
        if by_owner or (hold.set_by_us and etype in PLAIN_HOLD_TYPES):
            return None
        what = "Quick Save" if etype in PERSON_EVENTS else "hold"
        error = f"the running {what} was not set by the controller; not written"
        return {"refused": True, "not_ours": True}, error
    return {"refused": True, "event": etype}, f"a running {etype} event is in effect; not written"


class ThermostatEvent(BaseModel):
    """A demand-response or vacation event on a thermostat, running or scheduled ahead
    (ecobee lists announced utility events before they start). Times are UTC."""

    event_type: str  # 'demandResponse' | 'vacation'
    name: str | None = None
    running: bool = False
    start: datetime | None = None
    end: datetime | None = None
    heat_f: float | None = None
    cool_f: float | None = None
    is_relative: bool = False
    heat_offset_f: float | None = None
    cool_offset_f: float | None = None
    is_optional: bool | None = None
    is_cool_off: bool = False  # cooling turned off during the event
    is_heat_off: bool = False
    duty_cycle_pct: int | None = None  # % of scheduled runtime allowed (100 = no change)
    link_ref: str | None = None


def event_identity(link_ref: str | None, name: str | None, start: datetime | None) -> str:
    """Which utility event this is, the way ``climate.utility.events.event_key`` tells events
    apart: ecobee's linkRef when it has one, else the name and the start (UTC, to the minute).
    Two events are the same only when their identities are equal, so an event with a linkRef
    never matches one without."""
    if link_ref:
        return f"link:{link_ref}"
    when = start.astimezone(UTC).strftime("%Y-%m-%dT%H:%MZ") if start is not None else ""
    return f"{name or ''}:{when}"


class UtilityInfo(BaseModel):
    """The utility ecobee associates the thermostat with (includeUtility). Its presence, or a
    demand-response event, is how the app tells the owner a thermostat is enrolled."""

    name: str
    phone: str | None = None
    email: str | None = None
    web: str | None = None


class UnitSnapshot(BaseModel):
    unit_key: str
    ts: datetime
    source: Literal["ecobee", "simulator", "homekit"]
    revision: str | None = None
    name: str | None = None
    model: str | None = None
    hvac_mode: HvacMode = "off"
    equipment_running: list[str] = Field(default_factory=list)  # e.g. ['compCool1', 'fan']
    heat_sp_f: float | None = None
    cool_sp_f: float | None = None
    climate_ref: str | None = None  # 'home' | 'away' | 'sleep' | custom
    hold: HoldInfo | None = None
    zone_temp_f: float | None = None
    zone_humidity: float | None = None
    outdoor_temp_f: float | None = None
    outdoor_humidity: float | None = None
    sensors: list[SensorReading] = Field(default_factory=list)
    # Which sensors participate in each comfort setting: {'home': ['main.hallway_tstat', ...]}
    sensor_sets: dict[str, list[str]] = Field(default_factory=dict)
    settings: dict[str, object] = Field(default_factory=dict)  # autoAway, followMeComfort, heatCoolMinDelta, drAccept...
    connected: bool = True
    # Demand-response and vacation events, running or announced ahead (empty from HomeKit,
    # which shows no ecobee events). The running top event is also in ``hold``.
    events: list[ThermostatEvent] = Field(default_factory=list)
    utility: UtilityInfo | None = None  # ecobee only; None = no utility reported


class RuntimeInterval(BaseModel):
    """One 5-minute slot of the equipment record. Seconds 0..300."""

    unit_key: str
    ts: datetime  # slot start, UTC, aligned to 5 minutes
    comp_cool1: int = 0
    comp_cool2: int = 0
    comp_heat1: int = 0
    comp_heat2: int = 0
    aux_heat1: int = 0
    aux_heat2: int = 0
    fan: int = 0
    hvac_mode: str | None = None
    climate_ref: str | None = None
    zone_temp_f: float | None = None
    zone_humidity: float | None = None
    heat_sp_f: float | None = None
    cool_sp_f: float | None = None
    outdoor_temp_f: float | None = None
    outdoor_humidity: float | None = None
    sensor_temps: dict[str, float | None] = Field(default_factory=dict)  # sensor_key -> temp
    sensor_occupancy: dict[str, bool | None] = Field(default_factory=dict)


class HoldRequest(BaseModel):
    unit_key: str
    heat_f: float
    cool_f: float
    hours: int = Field(ge=1, le=2)  # ecobee holdType=holdHours, 1-2 h, renewed while healthy
    reason: str
    # True only for the owner's own hold from this app (the owner IS the person, so the source
    # skips the person check; see ``ThermostatSource.set_hold``). Every controller write is False.
    by_owner: bool = False


class WriteResult(BaseModel):
    ok: bool  # True only if the read-back matches the request
    channel: Literal["ecobee", "homekit", "simulator", "none"]
    before: dict[str, object] = Field(default_factory=dict)
    request: dict[str, object] = Field(default_factory=dict)
    readback: dict[str, object] | None = None
    error: str | None = None


class SourceHealth(BaseModel):
    ok: bool
    kind: Literal["ecobee", "simulator"]
    detail: str = ""
    signed_in: bool | None = None
    last_success_at: datetime | None = None
    consecutive_failures: int = 0


@runtime_checkable
class ThermostatSource(Protocol):
    kind: Literal["ecobee", "simulator"]

    async def poll_revisions(self) -> dict[str, str]:
        """Cheap change detection: unit_key -> revision token (ecobee /thermostatSummary).

        Call no faster than every 3 minutes for ecobee."""
        ...

    async def fetch_snapshots(self, unit_keys: list[str] | None = None) -> list[UnitSnapshot]:
        """Full detail for the given units (all when None). ecobee: only on revision change."""
        ...

    async def fetch_runtime(self, start: datetime, end: datetime) -> list[RuntimeInterval]:
        """5-minute equipment + sensor record for [start, end). ecobee: <= 31 days per call,
        one request at a time; data lags up to ~1 h."""
        ...

    async def set_hold(self, req: HoldRequest) -> WriteResult:
        """Write a holdHours temperature hold, then read it back (the library swallows errors).

        Decided on a fresh read of what runs on top (never a cached one), and refused before
        anything is sent (ok=False, ``readback`` None, ``before['hold']`` = that hold as
        HoldInfo JSON):
        - a controller write (``req.by_owner`` False) over a plain hold the controller did not
          write, or a Quick Save: someone set it since the snapshot the controller decided on,
          and a person's hold always wins. ``request['refused']`` and ``request['not_ours']``
          True; the error says the hold "was not set by the controller".
        - any write over a running vacation, demand-response or unknown event: ``request
          ['refused']`` True, ``request['event']`` = its type, error "a running <type> event is
          in effect; not written".
        Smart Away / Home (``AUTO_EVENTS``) and the controller's own hold never refuse. The
        owner's hold (``by_owner``) skips the person check, and is not "ours" afterwards: a
        later controller write over it is refused like any person's hold."""
        ...

    async def resume_program(self, unit_key: str, reason: str, force: bool = False) -> WriteResult:
        """Cancel the running plain hold and return to the schedule. Unless ``force`` (an
        owner's explicit resume), refuse when the running hold is not one the controller
        wrote, so a hand-set hold is never erased by the controller. ``force`` (the owner only)
        also cancels a Quick Save (``PERSON_EVENTS``) and Smart Away / Home (``AUTO_EVENTS``).
        Never cancels vacation, demand response or an unknown event, forced or not: opting out
        of a utility event is ``opt_out_event``'s job. ok only when the read-back shows no
        plain hold, and not the person or auto event it cancelled, on top."""
        ...

    async def opt_out_event(self, unit_key: str, reason: str, *, link_ref: str | None = None,
                            name: str | None = None, start: datetime | None = None) -> WriteResult:
        """Opt out of the RUNNING demand-response event (the owner's "Skip this event" or the
        owner's skip rule): ecobee's documented cancel, ``resumeProgram`` with the event on
        top, which ecobee records and reports to the utility. Decided on a fresh read.

        Refuses before anything is sent (ok=False, ``request['refused']`` True) when the top
        running event is not demandResponse or is mandatory (``isOptional`` false;
        ``request['mandatory']`` True), and, when an identity is given (``link_ref`` / ``name``
        + ``start``, the event the skip was asked for), when the top running event is a
        different one (``request['other_event']`` True, ``mandatory`` False, error "a different
        utility event is on top; nothing sent"). Identity is ``event_identity``: the linkRef
        when either side has one, else the name and the start to the minute.

        Then reads back that THIS event no longer runs (another utility event still running
        does not fail it). Once the POST went out ``request['sent']`` is True; when no read-back
        GET succeeds the error says "resumeProgram sent; read-back failed"."""
        ...

    async def health(self) -> SourceHealth:
        ...

    async def close(self) -> None:
        ...
