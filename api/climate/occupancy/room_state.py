"""Per-room occupancy states (blueprint §3): occupied / asleep / empty / unknown / no_target.

Uncertain means occupied. Sleep windows count as occupied (state 'asleep'). Rooms without
a sensor are schedule-only: 'asleep' inside their sleep window, otherwise 'unknown' (no
temperature, not a comfort target). The Foyer has no comfort target: 'no_target'.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from climate.store.app_settings import OccupancySettings


@dataclass
class SensorSignal:
    sensor_key: str
    room_key: str
    ts: datetime | None  # newest reading time
    has_occupancy: bool
    occupied: bool | None
    motion: bool | None
    seconds_since_motion: int | None
    seconds_since_occupancy: int | None
    online: bool
    last_occupied_at: datetime | None  # newest occupancy_events/readings True for this sensor


@dataclass
class RoomStateResult:
    room_key: str
    state: str
    confidence: float
    reason: str
    since: datetime | None = None


def compute_room_states(
    now: datetime, tz: str, signals: list[SensorSignal], settings: OccupancySettings,
    previous: dict[str, RoomStateResult] | None = None,
) -> dict[str, RoomStateResult]:
    """One result per room in climate.house.ROOMS. Rules: inside a sleep window -> asleep
    (conf 0.9). Sensor reported occupied now, or last occupied within empty_after_min ->
    occupied. All occupancy-capable sensors online and fresh (<15 min) and nothing for
    empty_after_min -> empty. Anything stale/offline/unknown -> occupied with reason
    'uncertain: ...' (conf 0.5). Thermostat with no occupancy (the Toy Room Essential)
    contributes temperature only; its room's SmartSensor supplies occupancy.
    ``since`` carries over from ``previous`` when the state is unchanged."""
    raise NotImplementedError


def load_signals(session: Session, now: datetime) -> list[SensorSignal]:
    """Build SensorSignal for every active sensor from live_sensors + occupancy_events."""
    raise NotImplementedError


def house_empty(
    now: datetime, tz: str, states: dict[str, RoomStateResult], signals: list[SensorSignal],
    settings: OccupancySettings,
) -> tuple[bool, str]:
    """Empty only when phones_away is True AND no motion/occupancy anywhere for
    house_empty_after_min AND no room is asleep (outside every sleep window). Never from
    phones alone; unknown phones -> not empty."""
    raise NotImplementedError


def persist_room_states(session: Session, now: datetime, states: dict[str, RoomStateResult]) -> None:
    """Insert one room_states row per room at ``now`` (on conflict do nothing)."""
    raise NotImplementedError
