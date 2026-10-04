"""The house: three units, eleven rooms, nine temperature points (CLAUDE.md, blueprint §1).

``seed(session)`` is idempotent: it inserts missing inventory rows and default settings,
and creates the first active policy version. It never overwrites owner edits.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from climate.store.orm import PolicyVersion, Room, Sensor, Unit


@dataclass(frozen=True)
class UnitDef:
    key: str
    name: str
    thermostat_room_key: str
    sort: int


@dataclass(frozen=True)
class RoomDef:
    key: str
    name: str
    unit_key: str
    floor: str
    has_sensor: bool
    is_sleep_room: bool = False
    has_comfort_target: bool = True
    notes: str | None = None
    sort: int = 0


@dataclass(frozen=True)
class SensorDef:
    key: str
    room_key: str
    unit_key: str
    kind: str  # 'thermostat' | 'smartsensor'
    name: str  # matches the ecobee / HomeKit name for auto-mapping
    has_humidity: bool = False
    has_occupancy: bool = True
    sort: int = 0


UNITS: list[UnitDef] = [
    UnitDef("main", "Main floor", "hallway", 0),
    UnitDef("up", "Upstairs", "toy_room", 1),
    UnitDef("bed", "Bed / Office wing", "bedroom", 2),
]

ROOMS: list[RoomDef] = [
    RoomDef("hallway", "Hallway", "main", "main", True, sort=0,
            notes="Main-floor thermostat. Night sensor set for the main floor (nearest the Twins' and Olive's rooms)."),
    RoomDef("school_room", "School Room", "main", "main", True, sort=1),
    RoomDef("living_room", "Living Room", "main", "main", True, sort=2),
    RoomDef("kitchen", "Kitchen", "main", "main", True, sort=3),
    RoomDef("twins_room", "Twins' Room", "main", "main", False, is_sleep_room=True, sort=4,
            notes="No sensor: temperature unknown; bedtimes are the only occupancy signal."),
    RoomDef("olive_room", "Olive's Room", "main", "main", False, is_sleep_room=True, sort=5,
            notes="No sensor: temperature unknown; bedtimes are the only occupancy signal."),
    RoomDef("toy_room", "Toy Room", "up", "upstairs", True, sort=6,
            notes="ecobee Smart Thermostat Essential (no occupancy) plus a SmartSensor."),
    RoomDef("girls_room", "Girls' Room", "up", "upstairs", True, is_sleep_room=True, sort=7),
    RoomDef("bedroom", "Bedroom", "bed", "wing", True, is_sleep_room=True, sort=8),
    RoomDef("office", "Office", "bed", "wing", True, sort=9),
    RoomDef("foyer", "Foyer", "bed", "wing", False, has_comfort_target=False, sort=10,
            notes="No sensor; walk-through space with no comfort target. Probably fed by the bedroom unit (confirm)."),
]

SENSORS: list[SensorDef] = [
    SensorDef("main.hallway_tstat", "hallway", "main", "thermostat", "Hallway", has_humidity=True, sort=0),
    SensorDef("main.school_room", "school_room", "main", "smartsensor", "School Room", sort=1),
    SensorDef("main.living_room", "living_room", "main", "smartsensor", "Living Room", sort=2),
    SensorDef("main.kitchen", "kitchen", "main", "smartsensor", "Kitchen", sort=3),
    SensorDef("up.toy_room_tstat", "toy_room", "up", "thermostat", "Toy Room", has_humidity=True,
              has_occupancy=False, sort=4),
    SensorDef("up.toy_room", "toy_room", "up", "smartsensor", "Toy Room Sensor", sort=5),
    SensorDef("up.girls_room", "girls_room", "up", "smartsensor", "Girls' Room", sort=6),
    SensorDef("bed.bedroom_tstat", "bedroom", "bed", "thermostat", "Bedroom", has_humidity=True, sort=7),
    SensorDef("bed.office", "office", "bed", "smartsensor", "Office", sort=8),
]

UNIT_KEYS = [u.key for u in UNITS]
ROOM_BY_KEY = {r.key: r for r in ROOMS}
SENSOR_BY_KEY = {s.key: s for s in SENSORS}


def rooms_for_unit(unit_key: str) -> list[RoomDef]:
    return [r for r in ROOMS if r.unit_key == unit_key]


def sensors_for_room(room_key: str) -> list[SensorDef]:
    return [s for s in SENSORS if s.room_key == room_key]


def normalize_name(name: str) -> str:
    """For auto-mapping ecobee/HomeKit names to rooms: lowercase, letters and digits only."""
    return "".join(ch for ch in name.lower() if ch.isalnum())


def seed(session: Session) -> None:
    """Insert missing inventory, default settings and the first policy version."""
    from climate.control.policy import PolicyParams
    from climate.store.app_settings import SETTINGS_MODELS, get_raw, put_setting

    have_units = set(session.execute(select(Unit.key)).scalars())
    for u in UNITS:
        if u.key not in have_units:
            session.add(Unit(key=u.key, name=u.name, thermostat_room_key=u.thermostat_room_key, sort=u.sort))
    session.flush()

    have_rooms = set(session.execute(select(Room.key)).scalars())
    for r in ROOMS:
        if r.key not in have_rooms:
            session.add(
                Room(key=r.key, name=r.name, unit_key=r.unit_key, floor=r.floor, has_sensor=r.has_sensor,
                     is_sleep_room=r.is_sleep_room, has_comfort_target=r.has_comfort_target, notes=r.notes, sort=r.sort)
            )
    session.flush()

    have_sensors = set(session.execute(select(Sensor.key)).scalars())
    for s in SENSORS:
        if s.key not in have_sensors:
            session.add(
                Sensor(key=s.key, room_key=s.room_key, unit_key=s.unit_key, kind=s.kind, name=s.name,
                       has_temperature=True, has_humidity=s.has_humidity, has_occupancy=s.has_occupancy, sort=s.sort)
            )
    session.flush()

    for key, model in SETTINGS_MODELS.items():
        if get_raw(session, key) is None:
            initial = model()
            if key == "source":
                from climate.config import get_settings

                initial = model(kind=get_settings().source)  # type: ignore[call-arg]
            put_setting(session, key, initial, updated_by="seed")

    has_active = session.execute(select(PolicyVersion.id).where(PolicyVersion.status == "active")).first()
    if not has_active:
        session.add(PolicyVersion(created_by="seed", params=PolicyParams().model_dump(mode="json"), status="active",
                                  note="Initial linked-floors policy (blueprint defaults)."))
    session.flush()
