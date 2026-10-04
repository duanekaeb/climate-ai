"""collector.backfill: ecobee chunking (newest-first, stop beyond history), weather, simulator."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import ClassVar

import pytest
from sqlalchemy import func, select

from climate.collector import backfill as bf
from climate.collector import weather
from climate.sources.base import RuntimeInterval, SensorReading, UnitSnapshot
from climate.store.app_settings import LocationSettings, SourceSettings, put_setting
from climate.store.orm import LiveSensor, OccupancyEvent, Reading5m, Runtime5m

END = datetime(2026, 9, 30, 0, 0, tzinfo=UTC)


class HistorySource:
    """ecobee-like: hourly-ish data only for the last ``history_days``."""

    kind = "ecobee"

    def __init__(self, history_days: int) -> None:
        self.first = END - timedelta(days=history_days)
        self.calls: list[tuple[datetime, datetime]] = []

    async def fetch_runtime(self, start: datetime, end: datetime) -> list[RuntimeInterval]:
        self.calls.append((start, end))
        out = []
        t = max(start, self.first)
        while t < end:
            out.append(RuntimeInterval(unit_key="main", ts=t, comp_cool1=60, sensor_temps={"main.kitchen": 74.0}))
            t += timedelta(hours=6)
        return out


async def test_ecobee_chunks_newest_first_and_stop_beyond_history(db, monkeypatch):
    called = []

    async def fake_weather(start, end):
        called.append((start, end))
        return 24

    monkeypatch.setattr(weather, "backfill_weather", fake_weather)
    src = HistorySource(history_days=40)
    out = await bf.backfill(src, END - timedelta(days=400), END)

    assert len(src.calls) == 4  # 2 chunks with data, then 2 empty -> beyond history
    assert all(e - s <= timedelta(days=31) for s, e in src.calls)
    assert src.calls[0][1] == END and all(src.calls[i][0] == src.calls[i + 1][1] for i in range(3))
    assert out["beyond_history"] is True and out["chunks"] == 4
    assert out["runtime_rows"] == 40 * 4
    assert out["history_start"] == src.first.isoformat()
    assert out["weather_rows"] == 0 and called == []  # no location set
    db.expire_all()
    assert db.execute(select(func.count()).select_from(Runtime5m)).scalar() == 160
    assert db.execute(select(func.count()).select_from(Reading5m)).scalar() == 160


async def test_ecobee_full_span_and_weather_when_location_set(db, monkeypatch):
    put_setting(db, "location", LocationSettings(lat=41.9, lon=-87.6, tz="America/Chicago"))
    db.commit()
    called = []

    async def fake_weather(start, end):
        called.append((start, end))
        return 72

    monkeypatch.setattr(weather, "backfill_weather", fake_weather)
    src = HistorySource(history_days=400)
    out = await bf.backfill(src, END - timedelta(days=45), END)
    assert len(src.calls) == 2 and src.calls[-1][0] == END - timedelta(days=45)
    assert out["beyond_history"] is False and out["weather_rows"] == 72
    assert called == [(END - timedelta(days=45), END)]


async def test_weather_failure_keeps_runtime(db, monkeypatch):
    put_setting(db, "location", LocationSettings(lat=41.9, lon=-87.6))
    db.commit()

    async def broken(start, end):
        raise ConnectionError("archive down")

    monkeypatch.setattr(weather, "backfill_weather", broken)
    out = await bf.backfill(HistorySource(10), END - timedelta(days=5), END)
    assert out["runtime_rows"] == 20 and out["weather_rows"] == 0 and "ConnectionError" in out["weather_error"]
    with pytest.raises(ValueError):
        await bf.backfill(HistorySource(10), END, END)


class FakeSim:
    kind = "simulator"
    instances: ClassVar[list[FakeSim]] = []

    def __init__(self) -> None:
        self.closed = False
        FakeSim.instances.append(self)

    async def close(self) -> None:
        self.closed = True

    def generate_history(self, start: datetime, end: datetime):
        runtime, snaps = [], []
        t = start
        while t < end:
            occupied = 8 <= t.hour < 20
            for unit in ("main", "up"):
                sensor = "main.kitchen" if unit == "main" else "up.girls_room"
                runtime.append(RuntimeInterval(unit_key=unit, ts=t, comp_cool1=100, outdoor_temp_f=85.0,
                                               sensor_temps={sensor: 74.0}, sensor_occupancy={sensor: occupied}))
                if t.minute == 0:
                    snaps.append(UnitSnapshot(unit_key=unit, ts=t, source="simulator", zone_temp_f=74.0, sensors=[
                        SensorReading(sensor_key=sensor, ts=t, temp_f=74.0, occupied=occupied, motion=occupied)]))
            t += timedelta(minutes=5)
        return runtime, snaps


@pytest.fixture
def fake_sim(monkeypatch):
    from climate.sources import simulator

    weather_hours = []

    def synthetic_weather(start, end):
        return [object()] * 3

    def upsert_weather(session, hours, source):
        weather_hours.append((len(hours), source))
        return len(hours)

    monkeypatch.setattr(simulator.SimulatedHouse, "from_settings", classmethod(lambda cls: FakeSim()))
    monkeypatch.setattr(simulator, "synthetic_weather", synthetic_weather, raising=False)
    monkeypatch.setattr(weather, "upsert_weather", upsert_weather)
    return weather_hours


async def test_simulator_backfill_if_empty(db, monkeypatch, fake_sim):
    from climate.analytics import baseline
    from climate.models import thermal_rc
    from climate.occupancy import room_state as rs

    fits, persisted = [], []
    monkeypatch.setattr(baseline, "refit_all", lambda s, now, train_days=90: fits.append("baseline") or [1, 2])
    def broken_offsets(s, now, days=14):
        raise RuntimeError("not enough data")

    monkeypatch.setattr(thermal_rc, "fit_room_offsets", broken_offsets)

    def compute(now, tz, signals, settings, previous=None):
        kitchen = next(sig for sig in signals if sig.sensor_key == "main.kitchen")
        state = "occupied" if kitchen.occupied else "empty"
        return {"kitchen": rs.RoomStateResult("kitchen", state, 0.9, "test")}

    monkeypatch.setattr(rs, "compute_room_states", compute)
    monkeypatch.setattr(rs, "persist_room_states", lambda s, now, states: persisted.append((now, states["kitchen"].state)))

    out = await bf.backfill_simulator_if_empty(2)
    assert out is not None
    assert out["runtime_rows"] in (2 * 576, 2 * 577)  # two units, 2 days of 5-minute slots
    assert out["weather_rows"] == 3 and fake_sim == [(3, "simulator")]
    assert 48 <= out["room_state_hours"] <= 49 and len(persisted) == out["room_state_hours"]
    assert {st for _, st in persisted} == {"occupied", "empty"}
    assert out["fits"]["baselines"] == {"ok": True, "result": [1, 2]}
    assert out["fits"]["room_offsets"]["ok"] is False  # tolerated
    db.expire_all()
    assert db.execute(select(func.count()).select_from(Runtime5m).where(Runtime5m.source == "simulator")).scalar() > 0
    assert db.get(LiveSensor, "main.kitchen") is not None
    assert db.execute(select(func.count()).select_from(OccupancyEvent)).scalar() > 0

    assert FakeSim.instances[-1].closed  # end state persisted for the worker's live simulator

    # runtime_5m is no longer empty: nothing to do
    assert await bf.backfill_simulator_if_empty(2) is None


async def test_simulator_backfill_skips_for_ecobee_and_tolerates_missing_engine(db, monkeypatch, fake_sim):
    from climate.analytics import baseline
    from climate.models import thermal_rc
    from climate.occupancy import room_state as rs

    def unfinished(*args, **kwargs):
        raise NotImplementedError

    for mod, name in ((rs, "compute_room_states"), (baseline, "refit_all"), (thermal_rc, "fit_room_offsets")):
        monkeypatch.setattr(mod, name, unfinished)
    put_setting(db, "source", SourceSettings(kind="ecobee"))
    db.commit()
    assert await bf.backfill_simulator_if_empty(5) is None
    put_setting(db, "source", SourceSettings(kind="simulator"))
    db.commit()
    assert await bf.backfill_simulator_if_empty(0) is None
    # room-state engine and fits unfinished (NotImplementedError): history still lands
    out = await bf.backfill_simulator_if_empty(1)
    assert out["runtime_rows"] > 0 and out["room_state_hours"] == 0
    assert out["fits"]["baselines"]["ok"] is False


async def test_backfill_with_a_simulator_source_uses_generated_history(db, fake_sim):
    out = await bf.backfill(FakeSim(), END - timedelta(hours=3), END)
    assert out["source"] == "simulator" and out["runtime_rows"] == 3 * 12 * 2


async def test_backfill_on_the_live_source_holds_its_lock(db, fake_sim):
    import asyncio

    from climate.worker import LockedSource

    src = LockedSource(FakeSim())
    task = asyncio.create_task(bf.backfill(src, END - timedelta(hours=1), END))
    await asyncio.sleep(0)
    async with src.lock:  # waits until generation has released the lock
        pass
    out = await task
    assert out["runtime_rows"] == 12 * 2
