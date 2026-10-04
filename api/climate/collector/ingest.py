"""Write source data into the store. Used by the worker poller, backfill and the homekit service.

Source precedence for the same 5-minute slot in readings_5m / runtime_5m:
ecobee_report > ecobee_poll > homekit > simulator (a lower-precedence write never
overwrites a higher one). live_sensors keeps the NEWEST value whatever its source.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from climate.sources.base import RuntimeInterval, SensorReading, UnitSnapshot

PRECEDENCE = {"ecobee_report": 4, "ecobee_poll": 3, "homekit": 2, "simulator": 1}


def ingest_snapshot(session: Session, snap: UnitSnapshot) -> None:
    """Upsert live_units + live_sensors, add readings_5m rows for the slot (source
    'ecobee_poll' or 'simulator'), add occupancy_events on occupancy transitions, publish 'status'."""
    raise NotImplementedError


def ingest_runtime(session: Session, intervals: list[RuntimeInterval], source: str) -> int:
    """Upsert runtime_5m and the per-sensor readings_5m carried in each interval. Returns rows."""
    raise NotImplementedError


def ingest_live_readings(session: Session, readings: list[SensorReading], source: str = "homekit") -> None:
    """HomeKit pushes/polls: upsert live_sensors (newest wins), occupancy_events on
    transitions, readings_5m for the slot at 'homekit' precedence."""
    raise NotImplementedError


def latest_runtime_ts(session: Session, unit_key: str | None = None) -> datetime | None:
    raise NotImplementedError
