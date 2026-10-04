"""collector.ingest: source precedence, live newest-wins, occupancy transitions, runtime."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from climate.collector import ingest
from climate.sources.base import RuntimeInterval, SensorReading, UnitSnapshot
from climate.store.orm import LiveSensor, LiveUnit, OccupancyEvent, Reading5m, Runtime5m

T0 = datetime(2026, 7, 1, 15, 0, tzinfo=UTC)
KITCHEN = "main.kitchen"
HALL = "main.hallway_tstat"


def reading(key: str = KITCHEN, ts: datetime = T0, **kw) -> SensorReading:
    return SensorReading(sensor_key=key, ts=ts, **kw)


def snap(ts: datetime = T0, source: str = "ecobee", sensors: list[SensorReading] | None = None, **kw) -> UnitSnapshot:
    return UnitSnapshot(unit_key="main", ts=ts, source=source, revision=kw.pop("revision", "r1"), hvac_mode="cool",
                        zone_temp_f=75.0, zone_humidity=kw.pop("zone_humidity", 51.0), sensors=sensors or [], **kw)


def interval(ts: datetime = T0, unit: str = "main", **kw) -> RuntimeInterval:
    return RuntimeInterval(unit_key=unit, ts=ts, **kw)


def slot_row(db, key: str, ts: datetime) -> Reading5m:
    db.expire_all()
    return db.execute(select(Reading5m).where(Reading5m.sensor_key == key, Reading5m.ts == ts)).scalar_one()


def events(db, key: str = KITCHEN) -> list[tuple[str, bool]]:
    db.expire_all()
    rows = db.execute(select(OccupancyEvent).where(OccupancyEvent.sensor_key == key).order_by(OccupancyEvent.ts,
                                                                                               OccupancyEvent.id))
    return [(e.kind, e.value) for e in rows.scalars()]


def test_source_names():
    assert ingest.snapshot_source_name("ecobee") == "ecobee_poll"
    assert ingest.snapshot_source_name("simulator") == "simulator"
    assert ingest.runtime_source_name("ecobee") == "ecobee_report"
    with pytest.raises(ValueError):
        ingest.ingest_runtime(None, [interval()], "bogus")  # type: ignore[arg-type]


def test_report_overwrites_poll_and_poll_cannot_overwrite_report(db):
    ingest.ingest_snapshot(db, snap(sensors=[reading(temp_f=74.0, occupied=True)]))
    row = slot_row(db, KITCHEN, T0)
    assert (row.temp_f, row.source) == (74.0, "ecobee_poll")

    ingest.ingest_runtime(db, [interval(sensor_temps={KITCHEN: 73.5}, sensor_occupancy={KITCHEN: False})], "ecobee_report")
    row = slot_row(db, KITCHEN, T0)
    assert (row.temp_f, row.occupied, row.source) == (73.5, False, "ecobee_report")

    # a later poll in the same slot loses to the report
    ingest.ingest_snapshot(db, snap(ts=T0 + timedelta(minutes=2), sensors=[reading(ts=T0 + timedelta(minutes=2), temp_f=80.0)]))
    row = slot_row(db, KITCHEN, T0)
    assert (row.temp_f, row.source) == (73.5, "ecobee_report")


def test_homekit_cannot_overwrite_poll_but_fills_new_slots(db):
    ingest.ingest_snapshot(db, snap(sensors=[reading(temp_f=74.0)]))
    ingest.ingest_live_readings(db, [reading(ts=T0 + timedelta(minutes=1), temp_f=71.0)], source="homekit")
    row = slot_row(db, KITCHEN, T0)
    assert (row.temp_f, row.source) == (74.0, "ecobee_poll")

    later = T0 + timedelta(minutes=6)
    ingest.ingest_live_readings(db, [reading(ts=later, temp_f=72.0)], source="homekit")
    row = slot_row(db, KITCHEN, ingest.floor_slot(later))
    assert (row.temp_f, row.source) == (72.0, "homekit")

    # simulator is the lowest precedence of all
    ingest.ingest_live_readings(db, [reading(ts=later, temp_f=60.0)], source="simulator")
    assert slot_row(db, KITCHEN, ingest.floor_slot(later)).temp_f == 72.0


def test_partial_reading_keeps_existing_slot_values(db):
    ingest.ingest_live_readings(db, [reading(temp_f=70.0)], source="homekit")
    ingest.ingest_live_readings(db, [reading(ts=T0 + timedelta(seconds=30), occupied=True)], source="homekit")
    row = slot_row(db, KITCHEN, T0)
    assert (row.temp_f, row.occupied) == (70.0, True)


def test_live_newest_wins_whatever_the_source(db):
    newer = T0 + timedelta(minutes=2)
    ingest.ingest_live_readings(db, [reading(ts=newer, temp_f=71.0)], source="homekit")
    # an older ecobee poll arriving later must not replace the newer HomeKit value
    ingest.ingest_snapshot(db, snap(sensors=[reading(temp_f=74.0)]))
    db.expire_all()
    live = db.get(LiveSensor, KITCHEN)
    assert (live.temp_f, live.source, live.ts) == (71.0, "homekit", newer)
    # ...but its slot still lands in readings_5m at poll precedence
    assert slot_row(db, KITCHEN, T0).temp_f == 74.0

    # a newer reading of lower precedence still wins live
    newest = T0 + timedelta(minutes=3)
    ingest.ingest_live_readings(db, [reading(ts=newest, temp_f=70.5)], source="simulator")
    db.expire_all()
    assert db.get(LiveSensor, KITCHEN).temp_f == 70.5


def test_live_keeps_values_a_push_does_not_carry_and_clears_when_offline(db):
    ingest.ingest_live_readings(db, [reading(temp_f=70.0, occupied=False)])
    ingest.ingest_live_readings(db, [reading(ts=T0 + timedelta(seconds=10), motion=True)])
    db.expire_all()
    live = db.get(LiveSensor, KITCHEN)
    assert (live.temp_f, live.motion, live.online) == (70.0, True, True)

    ingest.ingest_live_readings(db, [reading(ts=T0 + timedelta(seconds=20), online=False)])
    db.expire_all()
    live = db.get(LiveSensor, KITCHEN)
    assert live.online is False and live.temp_f is None


def test_occupancy_events_only_on_transitions(db):
    seq = [True, True, False, False, True]
    for i, occ in enumerate(seq):
        ingest.ingest_live_readings(db, [reading(ts=T0 + timedelta(minutes=i), occupied=occ)])
    assert events(db) == [("occupancy", True), ("occupancy", False), ("occupancy", True)]


def test_motion_events_and_batch_order(db):
    batch = [reading(ts=T0 + timedelta(seconds=s), motion=m) for s, m in ((30, False), (0, True), (10, True))]
    ingest.ingest_live_readings(db, batch)  # sorted by ts inside
    assert events(db) == [("motion", True), ("motion", False)]
    # an older reading arriving late is not a transition
    ingest.ingest_live_readings(db, [reading(ts=T0 - timedelta(minutes=5), motion=True)])
    assert events(db) == [("motion", True), ("motion", False)]


def test_snapshot_writes_live_unit_and_thermostat_humidity(db):
    ingest.ingest_snapshot(db, snap(sensors=[reading(HALL, temp_f=75.2, occupied=True)], revision="abc"))
    db.expire_all()
    lu = db.get(LiveUnit, "main")
    assert (lu.source, lu.revision, lu.snapshot["zone_temp_f"]) == ("ecobee", "abc", 75.0)
    assert db.get(LiveSensor, HALL).humidity == 51.0
    assert slot_row(db, HALL, T0).humidity == 51.0
    # older snapshot does not replace live_units
    ingest.ingest_snapshot(db, snap(ts=T0 - timedelta(minutes=3), revision="old"))
    db.expire_all()
    assert db.get(LiveUnit, "main").revision == "abc"


def test_unknown_sensor_is_ignored(db):
    ingest.ingest_live_readings(db, [reading("nope.sensor", temp_f=70.0), reading(temp_f=71.0)])
    db.expire_all()
    assert db.get(LiveSensor, KITCHEN).temp_f == 71.0
    assert db.get(LiveSensor, "nope.sensor") is None


def test_runtime_ingest_and_precedence(db):
    ivs = [interval(ts=T0 + timedelta(minutes=5 * i), comp_cool1=120 + i, comp_cool2=30, fan=150, hvac_mode="cool",
                    zone_temp_f=76.0, zone_humidity=50.0, sensor_temps={HALL: 76.1, "up.girls_room": 77.0})
           for i in range(3)]
    assert ingest.ingest_runtime(db, ivs, "ecobee_report") == 3
    db.expire_all()
    rows = db.execute(select(Runtime5m).order_by(Runtime5m.ts)).scalars().all()
    assert [r.comp_cool1 for r in rows] == [120, 121, 122]
    assert all(r.source == "ecobee_report" for r in rows)
    assert slot_row(db, HALL, T0).humidity == 50.0  # thermostat humidity from the zone
    assert slot_row(db, "up.girls_room", T0).humidity is None  # other unit's sensor: no zone humidity
    assert ingest.latest_runtime_ts(db) == T0 + timedelta(minutes=10)
    assert ingest.latest_runtime_ts(db, "up") is None

    # the simulator cannot overwrite a report; a re-pull of the report can
    assert ingest.ingest_runtime(db, [interval(comp_cool1=5)], "simulator") == 0
    assert ingest.ingest_runtime(db, [interval(comp_cool1=200, comp_cool2=999)], "ecobee_report") == 1
    db.expire_all()
    row = db.execute(select(Runtime5m).where(Runtime5m.ts == T0)).scalar_one()
    assert (row.comp_cool1, row.comp_cool2) == (200, 300)  # clamped to the 300 s slot


def test_runtime_dedupes_and_aligns_slots(db):
    ivs = [interval(ts=T0 + timedelta(minutes=2), comp_cool1=10), interval(ts=T0, comp_cool1=20)]
    assert ingest.ingest_runtime(db, ivs, "simulator") == 1
    db.expire_all()
    assert db.execute(select(Runtime5m.comp_cool1)).scalar_one() == 20


def test_bulk_snapshots_history(db):
    snaps = [snap(ts=T0 + timedelta(hours=h), source="simulator",
                  sensors=[reading(ts=T0 + timedelta(hours=h), temp_f=70 + h, occupied=h % 2 == 0)]) for h in range(6)]
    out = ingest.ingest_snapshots(db, list(reversed(snaps)), publish_event=False)
    assert out["units"] == 1 and out["readings"] == 6
    db.expire_all()
    assert db.get(LiveSensor, KITCHEN).temp_f == 75.0
    assert [v for _, v in events(db)] == [True, False, True, False, True, False]
