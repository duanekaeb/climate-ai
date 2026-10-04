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

from datetime import datetime
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


class HoldInfo(BaseModel):
    kind: Literal["temperature", "climate"] = "temperature"
    heat_f: float | None = None
    cool_f: float | None = None
    climate_ref: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    hold_type: str | None = None  # ecobee: holdHours / nextTransition / indefinite / dateTime
    set_by_us: bool = False  # True when it matches a hold the controller wrote


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
    settings: dict[str, object] = Field(default_factory=dict)  # autoAway, followMeComfort, heatCoolMinDelta...
    connected: bool = True


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
        """Write a holdHours temperature hold, then read it back (the library swallows errors)."""
        ...

    async def resume_program(self, unit_key: str, reason: str) -> WriteResult:
        ...

    async def health(self) -> SourceHealth:
        ...

    async def close(self) -> None:
        ...
