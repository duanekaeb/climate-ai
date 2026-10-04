"""History backfill: ecobee runtimeReport in 31-day chunks (as far back as ecobee keeps,
one request at a time), Open-Meteo archive for the same span; or simulator history.

ecobee: chunks are requested newest-first, so the run can stop where ecobee's history ends:
two empty chunks in a row mean the request went beyond what ecobee keeps (walking oldest-first
the empty chunks come first, and that stop rule would end the run before any data). Ingest is
an idempotent upsert, so the order changes nothing else, and an interrupted run keeps the
most recent (most useful) history.

Simulator: ``SimulatedHouse.generate_history`` supplies 5-minute runtime and hourly snapshots,
stored as source 'simulator', with synthetic weather; on first start
``backfill_simulator_if_empty`` also writes hourly room states and fits the baselines and
room offsets once, so every screen has data.
"""

from __future__ import annotations

import asyncio
import logging
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from climate.collector.ingest import ingest_runtime, ingest_snapshots, runtime_source_name
from climate.collector.poller import safe_error
from climate.events import publish
from climate.sources.base import RuntimeInterval, ThermostatSource, UnitSnapshot
from climate.store.app_settings import LocationSettings, OccupancySettings, SourceSettings, get_setting
from climate.store.db import session_scope
from climate.store.orm import Runtime5m, Sensor
from climate.timeutil import floor_slot, utcnow

log = logging.getLogger(__name__)

CHUNK = timedelta(days=31)
EMPTY_CHUNKS_TO_STOP = 2
_INGEST_BATCH = 5000  # runtime intervals / snapshots per transaction


# ---------------------------------------------------------------------------------------
# small shared helpers (the worker uses run_db_step too)
# ---------------------------------------------------------------------------------------


def jsonable(value: Any) -> Any:
    """Make a step result storable in a JSONB column."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [jsonable(v) for v in value]
    return str(value)


def run_db_step(name: str, fn: Callable[[Session], Any]) -> dict[str, Any]:
    """Run ``fn(session)`` in its own transaction; never raises. -> {ok, result | error}."""
    try:
        with session_scope() as s:
            out = fn(s)
        return {"ok": True, "result": jsonable(out)}
    except Exception as exc:  # noqa: BLE001 - one failing step must not stop the others
        log.warning("%s failed: %s", name, safe_error(exc))
        return {"ok": False, "error": safe_error(exc, 1000)}


def _ingest_runtime_batches(intervals: Sequence[RuntimeInterval], source: str) -> int:
    ordered = sorted(intervals, key=lambda iv: (iv.ts, iv.unit_key))
    written = 0
    for i in range(0, len(ordered), _INGEST_BATCH):
        with session_scope() as s:
            written += ingest_runtime(s, list(ordered[i : i + _INGEST_BATCH]), source)
    return written


def _ingest_snapshot_batches(snaps: Sequence[UnitSnapshot]) -> int:
    ordered = sorted(snaps, key=lambda sn: (sn.ts, sn.unit_key))
    readings = 0
    for i in range(0, len(ordered), _INGEST_BATCH):
        with session_scope() as s:
            readings += ingest_snapshots(s, list(ordered[i : i + _INGEST_BATCH]), publish_event=False)["readings"]
    if ordered:
        with session_scope() as s:
            publish(s, "status")
    return readings


def _location() -> LocationSettings:
    with session_scope() as s:
        return get_setting(s, "location", LocationSettings)


async def _archive_weather(start: datetime, end: datetime) -> tuple[int, str | None]:
    loc = await asyncio.to_thread(_location)
    if loc.lat is None or loc.lon is None:
        return 0, None
    try:
        from climate.collector import weather

        return int(await weather.backfill_weather(start, end) or 0), None
    except Exception as exc:  # noqa: BLE001 - runtime history is still worth keeping
        log.warning("weather backfill failed: %s", safe_error(exc))
        return 0, safe_error(exc)


def _synthetic_weather(sim: Any, start: datetime, end: datetime) -> tuple[int, str | None]:
    """Store the simulator's synthetic weather (``climate.sources.simulator.synthetic_weather``,
    or the same-named method) as source 'simulator'."""
    try:
        from climate.sources import simulator as sim_mod

        fn = getattr(sim_mod, "synthetic_weather", None) or getattr(sim, "synthetic_weather", None)
        if fn is None:
            log.info("the simulator provides no synthetic_weather; skipping synthetic weather")
            return 0, "synthetic_weather unavailable"
        hours = list(fn(start, end))
        from climate.collector import weather

        with session_scope() as s:
            return int(weather.upsert_weather(s, hours, "simulator") or 0), None
    except Exception as exc:  # noqa: BLE001
        log.warning("synthetic weather failed: %s", safe_error(exc))
        return 0, safe_error(exc)


# ---------------------------------------------------------------------------------------
# room states for generated history
# ---------------------------------------------------------------------------------------


def _room_states_history(start: datetime, end: datetime, runtime: Sequence[RuntimeInterval],
                         snaps: Sequence[UnitSnapshot]) -> int:
    """Hourly room_states over [start, end) from the generated readings (the occupancy engine
    decides; this only rebuilds the signals it would have seen). Returns hours written."""
    try:
        from climate.occupancy import room_state as rs
    except Exception as exc:  # noqa: BLE001
        log.info("occupancy engine unavailable; skipping history room states (%s)", safe_error(exc))
        return 0

    obs: dict[str, dict[datetime, dict[str, Any]]] = defaultdict(dict)
    for iv in runtime:
        slot = floor_slot(iv.ts)
        for key, temp in iv.sensor_temps.items():
            obs[key].setdefault(slot, {})["temp"] = temp
        for key, occ in iv.sensor_occupancy.items():
            obs[key].setdefault(slot, {})["occupied"] = occ
    for snap in snaps:
        for r in snap.sensors:
            d = obs[r.sensor_key].setdefault(floor_slot(r.ts), {})
            if r.occupied is not None:
                d["occupied"] = r.occupied
            d.update(motion=r.motion, ssm=r.seconds_since_motion, sso=r.seconds_since_occupancy, online=r.online)
    slots = {k: sorted(v) for k, v in obs.items()}
    occupied_at = {k: sorted(t for t, d in v.items() if d.get("occupied")) for k, v in obs.items()}

    hours = 0
    try:
        with session_scope() as s:
            tz = get_setting(s, "location", LocationSettings).tz
            occ_settings = get_setting(s, "occupancy", OccupancySettings)
            sensors = list(s.execute(select(Sensor.key, Sensor.room_key, Sensor.has_occupancy).where(Sensor.is_active)))
            previous = None
            h = start.replace(minute=0, second=0, microsecond=0)
            if h < start:
                h += timedelta(hours=1)
            while h < end:
                signals = []
                for key, room_key, has_occ in sensors:
                    times = slots.get(key, [])
                    i = bisect_right(times, h) - 1
                    latest = times[i] if i >= 0 else None
                    d = obs[key][latest] if latest is not None else {}
                    occ_times = occupied_at.get(key, [])
                    j = bisect_right(occ_times, h) - 1
                    signals.append(rs.SensorSignal(
                        sensor_key=key, room_key=room_key, ts=latest, has_occupancy=bool(has_occ),
                        occupied=d.get("occupied"), motion=d.get("motion"), seconds_since_motion=d.get("ssm"),
                        seconds_since_occupancy=d.get("sso"), online=bool(d.get("online", latest is not None)),
                        last_occupied_at=occ_times[j] if j >= 0 else None,
                    ))
                try:
                    states = rs.compute_room_states(h, tz, signals, occ_settings, previous)
                    rs.persist_room_states(s, h, states)
                except Exception as exc:  # noqa: BLE001 - keep the hours already written
                    log.info("history room states stopped at %s: %s", h.isoformat(), safe_error(exc))
                    break
                previous = states
                hours += 1
                h += timedelta(hours=1)
    except Exception as exc:  # noqa: BLE001
        log.warning("history room states failed: %s", safe_error(exc))
    return hours


def _fit_once(now: datetime) -> dict[str, Any]:
    def baselines(s: Session) -> Any:
        from climate.analytics import baseline

        return baseline.refit_all(s, now)

    def offsets(s: Session) -> Any:
        from climate.models import thermal_rc

        return thermal_rc.fit_room_offsets(s, now)

    return {"baselines": run_db_step("baseline refit", baselines), "room_offsets": run_db_step("room offsets", offsets)}


# ---------------------------------------------------------------------------------------
# backfill
# ---------------------------------------------------------------------------------------


async def _backfill_simulated(sim: Any, start: datetime, end: datetime) -> tuple[dict[str, Any], list, list]:
    # generate_history resets and advances the simulator's live state: on the worker's live
    # source (worker.LockedSource) it must not interleave with advance_to / polls.
    lock = getattr(sim, "lock", None)
    if isinstance(lock, asyncio.Lock):
        async with lock:
            runtime, snaps = await asyncio.to_thread(sim.generate_history, start, end)
    else:
        runtime, snaps = await asyncio.to_thread(sim.generate_history, start, end)
    runtime, snaps = list(runtime), list(snaps)
    runtime_rows = await asyncio.to_thread(_ingest_runtime_batches, runtime, "simulator")
    reading_rows = await asyncio.to_thread(_ingest_snapshot_batches, snaps)
    weather_rows, weather_error = await asyncio.to_thread(_synthetic_weather, sim, start, end)
    result: dict[str, Any] = {
        "runtime_rows": runtime_rows, "weather_rows": weather_rows, "snapshot_readings": reading_rows,
        "start": start.isoformat(), "end": end.isoformat(), "source": "simulator",
    }
    if weather_error:
        result["weather_error"] = weather_error
    return result, runtime, snaps


async def backfill(source: ThermostatSource, start: datetime, end: datetime) -> dict:
    """Returns {'runtime_rows': n, 'weather_rows': n, 'start': ..., 'end': ...}.

    Plus 'chunks' (requests made), 'history_start' (oldest slot ecobee returned) and
    'beyond_history' (True when the run stopped on two empty chunks before ``start``)."""
    if end <= start:
        raise ValueError("backfill needs start < end")
    if source.kind == "simulator" and getattr(source, "generate_history", None) is not None:
        result, _, _ = await _backfill_simulated(source, start, end)
        return result

    runtime_name = runtime_source_name(source.kind)
    runtime_rows = chunks = empty_streak = 0
    earliest: datetime | None = None
    beyond_history = False
    chunk_end = end
    while chunk_end > start:
        chunk_start = max(start, chunk_end - CHUNK)
        intervals = list(await source.fetch_runtime(chunk_start, chunk_end))  # one request at a time
        chunks += 1
        if intervals:
            empty_streak = 0
            runtime_rows += await asyncio.to_thread(_ingest_runtime_batches, intervals, runtime_name)
            first = min(iv.ts for iv in intervals)
            earliest = first if earliest is None else min(earliest, first)
        else:
            empty_streak += 1
            if empty_streak >= EMPTY_CHUNKS_TO_STOP:
                beyond_history = chunk_start > start
                break
        chunk_end = chunk_start
        log.info("backfill: %d chunk(s), %d runtime rows so far", chunks, runtime_rows)

    weather_start = max(start, floor_slot(earliest, 60) - timedelta(days=1)) if beyond_history and earliest else start
    weather_rows, weather_error = await _archive_weather(weather_start, end)
    result: dict[str, Any] = {
        "runtime_rows": runtime_rows, "weather_rows": weather_rows, "start": start.isoformat(),
        "end": end.isoformat(), "chunks": chunks, "beyond_history": beyond_history,
        "history_start": earliest.isoformat() if earliest else None, "source": source.kind,
    }
    if weather_error:
        result["weather_error"] = weather_error
    return result


def _simulator_needs_history() -> bool:
    with session_scope() as s:
        if get_setting(s, "source", SourceSettings).kind != "simulator":
            return False
        return s.execute(select(Runtime5m.ts).limit(1)).first() is None


async def backfill_simulator_if_empty(days: int) -> dict | None:
    """On first start in simulator mode with an empty runtime_5m: generate ``days`` of history
    (runtime, readings, room states, synthetic weather) so every screen has data."""
    if days <= 0 or not await asyncio.to_thread(_simulator_needs_history):
        return None
    from climate.sources.simulator import SimulatedHouse

    now = utcnow()
    start = floor_slot(now - timedelta(days=days))
    log.info("simulator: generating %d days of history", days)
    sim = SimulatedHouse.from_settings()
    result, runtime, snaps = await _backfill_simulated(sim, start, now)
    try:
        # Persist the end state so the worker's simulator (built after this) continues from it.
        await sim.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("could not save the simulator state: %s", safe_error(exc))
    result["room_state_hours"] = await asyncio.to_thread(_room_states_history, start, now, runtime, snaps)
    result["fits"] = await asyncio.to_thread(_fit_once, now)
    log.info("simulator history: %d runtime rows, %d room-state hours", result["runtime_rows"], result["room_state_hours"])
    return result
