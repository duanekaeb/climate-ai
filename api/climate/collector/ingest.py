"""Write source data into the store. Used by the worker poller, backfill and the homekit service.

Source precedence for the same 5-minute slot in readings_5m / runtime_5m:
ecobee_report > ecobee_poll > homekit > simulator (a lower-precedence write never
overwrites a higher one). live_sensors keeps the NEWEST value whatever its source.

Store source names are 'ecobee_report' (runtimeReport), 'ecobee_poll' (/thermostat detail),
'homekit' and 'simulator'. Snapshots from source kind 'ecobee' are stored as 'ecobee_poll';
runtime from 'ecobee' as 'ecobee_report' (see ``snapshot_source_name`` /
``runtime_source_name``).

Merge rules on an upsert that is allowed to win (same or higher precedence / newer):
- readings_5m: a value the new write does not carry (``None``) keeps the stored value, so a
  partial reading (e.g. a HomeKit motion push) never erases a temperature.
- runtime_5m: the whole row is replaced (a re-pull of the same slot is authoritative).
- live_sensors: newest ``ts`` wins. Missing values keep the stored ones while the sensor is
  online; a reading that says the sensor is offline clears them (no stale temperature
  presented as live).
- occupancy_events: one row per transition of ``occupied`` (kind 'occupancy') or ``motion``
  (kind 'motion') against the previous live value, only for readings that win live.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from climate.events import publish
from climate.sources.base import RuntimeInterval, SensorReading, UnitSnapshot
from climate.store.orm import LiveSensor, LiveUnit, OccupancyEvent, Reading5m, Runtime5m, Sensor, Unit
from climate.timeutil import floor_slot

log = logging.getLogger(__name__)

PRECEDENCE = {"ecobee_report": 4, "ecobee_poll": 3, "homekit": 2, "simulator": 1}

_SNAPSHOT_SOURCE = {"ecobee": "ecobee_poll", "simulator": "simulator", "homekit": "homekit"}
_RUNTIME_SOURCE = {"ecobee": "ecobee_report", "simulator": "simulator"}

# psycopg allows 65535 bind parameters per statement; stay under it.
_MAX_PARAMS = 60_000
_SLOT_MAX_S = 300
_ROWCOUNT = {"preserve_rowcount": True}  # SQLAlchemy 2.x reports INSERT rowcount only when asked

_READING_VALUES = ("temp_f", "humidity", "occupied")
_LIVE_VALUES = ("temp_f", "humidity", "occupied", "motion", "seconds_since_motion", "seconds_since_occupancy",
                "battery_low")
_RUNTIME_SECONDS = ("comp_cool1", "comp_cool2", "comp_heat1", "comp_heat2", "aux_heat1", "aux_heat2", "fan")
_RUNTIME_VALUES = ("hvac_mode", "climate_ref", "zone_temp_f", "zone_humidity", "heat_sp_f", "cool_sp_f",
                   "outdoor_temp_f", "outdoor_humidity")


def snapshot_source_name(kind: str) -> str:
    """Store source name for a UnitSnapshot / live reading from source kind ``kind``."""
    try:
        return _SNAPSHOT_SOURCE[kind]
    except KeyError:
        raise ValueError(f"unknown snapshot source kind {kind!r}") from None


def runtime_source_name(kind: str) -> str:
    """Store source name for runtime intervals from source kind ``kind``."""
    try:
        return _RUNTIME_SOURCE[kind]
    except KeyError:
        raise ValueError(f"unknown runtime source kind {kind!r}") from None


def _utc(ts: datetime) -> datetime:
    """Sources should send aware UTC times; treat a naive one as UTC rather than crash."""
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def _check_source(source: str) -> None:
    if source not in PRECEDENCE:
        raise ValueError(f"unknown store source {source!r}; expected one of {sorted(PRECEDENCE)}")


def _rank(col: Any) -> Any:
    return case(PRECEDENCE, value=col, else_=0)


def _chunks(rows: Sequence[dict[str, Any]]) -> Iterator[Sequence[dict[str, Any]]]:
    if not rows:
        return
    size = max(1, _MAX_PARAMS // max(1, len(rows[0])))
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


@dataclass(frozen=True)
class _SensorMeta:
    unit_key: str
    kind: str
    has_humidity: bool


def _sensor_meta(session: Session) -> dict[str, _SensorMeta]:
    rows = session.execute(select(Sensor.key, Sensor.unit_key, Sensor.kind, Sensor.has_humidity))
    return {k: _SensorMeta(u, kind, bool(h)) for k, u, kind, h in rows}


def _unit_keys(session: Session) -> set[str]:
    return set(session.execute(select(Unit.key)).scalars())


def _thermostat_humidity(meta: _SensorMeta, unit_key: str, zone_humidity: float | None) -> float | None:
    """ecobee thermostats carry the only humidity sensor: the zone humidity is theirs."""
    if meta.kind == "thermostat" and meta.has_humidity and meta.unit_key == unit_key:
        return zone_humidity
    return None


# ---------------------------------------------------------------------------------------
# readings_5m / runtime_5m upserts (precedence-guarded)
# ---------------------------------------------------------------------------------------


def _merge_reading(old: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, Any]:
    """In-batch twin of the SQL upsert: lower precedence loses; missing values keep the old."""
    if old is None:
        return new
    if PRECEDENCE[new["source"]] < PRECEDENCE[old["source"]]:
        return old
    merged = dict(new)
    for f in _READING_VALUES:
        if merged[f] is None:
            merged[f] = old[f]
    return merged


def _upsert_readings(session: Session, rows: list[dict[str, Any]]) -> int:
    merged: dict[tuple[str, datetime], dict[str, Any]] = {}
    for row in rows:
        key = (row["sensor_key"], row["ts"])
        merged[key] = _merge_reading(merged.get(key), row)
    written = 0
    for chunk in _chunks(list(merged.values())):
        stmt = insert(Reading5m).values(list(chunk))
        ex = stmt.excluded
        set_: dict[str, Any] = {f: func.coalesce(getattr(ex, f), getattr(Reading5m, f)) for f in _READING_VALUES}
        set_["source"] = ex.source
        stmt = stmt.on_conflict_do_update(
            index_elements=[Reading5m.sensor_key, Reading5m.ts],
            set_=set_,
            where=_rank(ex.source) >= _rank(Reading5m.source),
        )
        written += max(0, session.execute(stmt, execution_options=_ROWCOUNT).rowcount or 0)
    return written


def _upsert_runtime(session: Session, rows: list[dict[str, Any]]) -> int:
    written = 0
    for chunk in _chunks(rows):
        stmt = insert(Runtime5m).values(list(chunk))
        ex = stmt.excluded
        set_ = {f: getattr(ex, f) for f in (*_RUNTIME_SECONDS, *_RUNTIME_VALUES, "source")}
        stmt = stmt.on_conflict_do_update(
            index_elements=[Runtime5m.unit_key, Runtime5m.ts],
            set_=set_,
            where=_rank(ex.source) >= _rank(Runtime5m.source),
        )
        written += max(0, session.execute(stmt, execution_options=_ROWCOUNT).rowcount or 0)
    return written


def _reading_row(sensor_key: str, ts: datetime, temp_f: float | None, humidity: float | None,
                 occupied: bool | None, source: str) -> dict[str, Any] | None:
    if temp_f is None and humidity is None and occupied is None:
        return None
    return {"ts": floor_slot(ts), "sensor_key": sensor_key, "temp_f": temp_f, "humidity": humidity,
            "occupied": occupied, "source": source}


# ---------------------------------------------------------------------------------------
# live_sensors + occupancy_events
# ---------------------------------------------------------------------------------------


@dataclass
class _Live:
    ts: datetime
    source: str
    online: bool
    values: dict[str, Any] = field(default_factory=dict)


def _load_live(session: Session, keys: list[str]) -> dict[str, _Live]:
    """Current live rows for ``keys``, locked so concurrent writers (worker, homekit service)
    see each other's transitions instead of both emitting the same event."""
    if not keys:
        return {}
    rows = session.execute(
        select(LiveSensor).where(LiveSensor.sensor_key.in_(keys)).order_by(LiveSensor.sensor_key).with_for_update()
    ).scalars()
    out: dict[str, _Live] = {}
    for r in rows:
        out[r.sensor_key] = _Live(ts=r.ts, source=r.source, online=bool(r.online),
                                  values={f: getattr(r, f) for f in _LIVE_VALUES})
    return out


def _upsert_live(session: Session, rows: list[dict[str, Any]]) -> None:
    for chunk in _chunks(rows):
        stmt = insert(LiveSensor).values(list(chunk))
        ex = stmt.excluded
        set_: dict[str, Any] = {"ts": ex.ts, "source": ex.source, "online": ex.online}
        for f in _LIVE_VALUES:
            set_[f] = case(
                (ex.online.is_(True), func.coalesce(getattr(ex, f), getattr(LiveSensor, f))),
                else_=getattr(ex, f),
            )
        stmt = stmt.on_conflict_do_update(index_elements=[LiveSensor.sensor_key], set_=set_, where=ex.ts >= LiveSensor.ts)
        session.execute(stmt)


def _insert_events(session: Session, rows: list[dict[str, Any]]) -> None:
    for chunk in _chunks(rows):
        session.execute(insert(OccupancyEvent).values(list(chunk)))


def _ingest_sensor_readings(session: Session, readings: list[SensorReading], source: str,
                            meta: dict[str, _SensorMeta]) -> dict[str, int]:
    """live_sensors (newest wins), occupancy_events on transitions, readings_5m per slot."""
    known = [r if r.ts.tzinfo is not None else r.model_copy(update={"ts": _utc(r.ts)})
             for r in readings if r.sensor_key in meta]
    skipped = len(readings) - len(known)
    if skipped:
        log.debug("ignored %d readings for unknown sensors", skipped)
    if not known:
        return {"readings": 0, "events": 0, "live": 0}
    known.sort(key=lambda r: (r.ts, r.sensor_key))

    state = _load_live(session, sorted({r.sensor_key for r in known}))
    touched: set[str] = set()
    events: list[dict[str, Any]] = []
    reading_rows: list[dict[str, Any]] = []

    for r in known:
        row = _reading_row(r.sensor_key, r.ts, r.temp_f, r.humidity, r.occupied, source)
        if row is not None:
            reading_rows.append(row)

        cur = state.get(r.sensor_key)
        if cur is not None and r.ts < cur.ts:
            continue  # older than what live already shows: slot data only
        prev = cur.values if cur is not None else {}
        for kind, attr in (("occupancy", "occupied"), ("motion", "motion")):
            value = getattr(r, attr)
            if value is not None and value != prev.get(attr):
                events.append({"ts": r.ts, "sensor_key": r.sensor_key, "kind": kind, "value": value, "source": source})
        values: dict[str, Any] = {}
        for f in _LIVE_VALUES:
            new = getattr(r, f)
            values[f] = (new if new is not None else prev.get(f)) if r.online else new
        state[r.sensor_key] = _Live(ts=r.ts, source=source, online=r.online, values=values)
        touched.add(r.sensor_key)

    live_rows = [
        {"sensor_key": k, "ts": state[k].ts, "source": state[k].source, "online": state[k].online, **state[k].values}
        for k in sorted(touched)
    ]
    _upsert_live(session, live_rows)
    if events:
        _insert_events(session, events)
    written = _upsert_readings(session, reading_rows)
    return {"readings": written, "events": len(events), "live": len(live_rows)}


# ---------------------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------------------


def ingest_snapshots(session: Session, snaps: Sequence[UnitSnapshot], *, publish_event: bool = True) -> dict[str, int]:
    """Bulk form of ``ingest_snapshot`` (backfill writes hourly history through it):
    live_units keeps the newest snapshot per unit; sensor readings go through the same
    precedence / transition rules in time order. Publishes 'status' once."""
    if not snaps:
        return {"units": 0, "readings": 0, "events": 0, "live": 0}
    meta = _sensor_meta(session)
    units = _unit_keys(session)

    newest: dict[str, UnitSnapshot] = {}
    for s in snaps:
        if s.unit_key not in units:
            log.warning("snapshot for unknown unit %r ignored", s.unit_key)
            continue
        if s.unit_key not in newest or _utc(s.ts) >= _utc(newest[s.unit_key].ts):
            newest[s.unit_key] = s
    if newest:
        rows = [
            {"unit_key": s.unit_key, "ts": _utc(s.ts), "source": s.source, "revision": s.revision,
             "snapshot": s.model_dump(mode="json")}
            for s in newest.values()
        ]
        stmt = insert(LiveUnit).values(rows)
        ex = stmt.excluded
        stmt = stmt.on_conflict_do_update(
            index_elements=[LiveUnit.unit_key],
            set_={"ts": ex.ts, "source": ex.source, "revision": ex.revision, "snapshot": ex.snapshot},
            where=ex.ts >= LiveUnit.ts,
        )
        session.execute(stmt)

    by_source: dict[str, list[SensorReading]] = defaultdict(list)
    for s in snaps:
        if s.unit_key not in units:
            continue
        name = snapshot_source_name(s.source)
        for r in s.sensors:
            m = meta.get(r.sensor_key)
            if m is not None and r.humidity is None:
                hum = _thermostat_humidity(m, s.unit_key, s.zone_humidity)
                if hum is not None:
                    r = r.model_copy(update={"humidity": hum})
            by_source[name].append(r)

    totals = {"units": len(newest), "readings": 0, "events": 0, "live": 0}
    for name, readings in by_source.items():
        out = _ingest_sensor_readings(session, readings, name, meta)
        for k, v in out.items():
            totals[k] += v
    if publish_event:
        publish(session, "status")
    return totals


def ingest_snapshot(session: Session, snap: UnitSnapshot) -> None:
    """Upsert live_units + live_sensors, add readings_5m rows for the slot (source
    'ecobee_poll' or 'simulator'), add occupancy_events on occupancy transitions, publish 'status'."""
    ingest_snapshots(session, [snap])


def ingest_runtime(session: Session, intervals: list[RuntimeInterval], source: str) -> int:
    """Upsert runtime_5m and the per-sensor readings_5m carried in each interval. Returns rows."""
    _check_source(source)
    if not intervals:
        return 0
    meta = _sensor_meta(session)
    units = _unit_keys(session)
    runtime_rows: dict[tuple[str, datetime], dict[str, Any]] = {}
    reading_rows: list[dict[str, Any]] = []
    for iv in intervals:
        if iv.unit_key not in units:
            log.warning("runtime for unknown unit %r ignored", iv.unit_key)
            continue
        slot = floor_slot(_utc(iv.ts))
        row: dict[str, Any] = {"ts": slot, "unit_key": iv.unit_key, "source": source}
        for f in _RUNTIME_SECONDS:
            row[f] = max(0, min(_SLOT_MAX_S, int(getattr(iv, f) or 0)))
        for f in _RUNTIME_VALUES:
            row[f] = getattr(iv, f)
        runtime_rows[(iv.unit_key, slot)] = row  # same source: the later interval wins

        for sensor_key in sorted(set(iv.sensor_temps) | set(iv.sensor_occupancy)):
            m = meta.get(sensor_key)
            if m is None:
                continue
            reading = _reading_row(sensor_key, slot, iv.sensor_temps.get(sensor_key),
                                   _thermostat_humidity(m, iv.unit_key, iv.zone_humidity),
                                   iv.sensor_occupancy.get(sensor_key), source)
            if reading is not None:
                reading_rows.append(reading)

    written = _upsert_runtime(session, list(runtime_rows.values()))
    _upsert_readings(session, reading_rows)
    if runtime_rows:
        publish(session, "status")
    return written


def ingest_live_readings(session: Session, readings: list[SensorReading], source: str = "homekit") -> None:
    """HomeKit pushes/polls: upsert live_sensors (newest wins), occupancy_events on
    transitions, readings_5m for the slot at 'homekit' precedence."""
    _check_source(source)
    if not readings:
        return
    _ingest_sensor_readings(session, list(readings), source, _sensor_meta(session))
    publish(session, "status")


def latest_runtime_ts(session: Session, unit_key: str | None = None) -> datetime | None:
    q = select(func.max(Runtime5m.ts))
    if unit_key is not None:
        q = q.where(Runtime5m.unit_key == unit_key)
    return session.execute(q).scalar_one_or_none()
