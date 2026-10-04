"""Pure parsing of ecobee JSON (no network, no database)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from climate.house import SENSORS
from climate.sources import ecobee_parse as P

FIX = Path(__file__).parent / "fixtures" / "ecobee"
CHI = ZoneInfo("America/Chicago")


def load(name: str) -> dict:
    return json.loads((FIX / name).read_text())


def tstat(identifier: str) -> dict:
    return next(t for t in load("thermostats.json")["thermostatList"] if t["identifier"] == identifier)


def sensor_refs() -> list[P.SensorRef]:
    return [P.SensorRef(s.key, s.unit_key, s.kind, s.name, None) for s in SENSORS]


def test_scalars_handle_unknown_values():
    assert P.tenths_to_f("741") == 74.1
    assert P.tenths_to_f(680) == 68.0
    assert P.tenths_to_f("unknown") is None
    assert P.tenths_to_f(-5002) is None
    assert P.tenths_to_f(None) is None
    assert P.num("") is None and P.num("48") == 48.0
    assert P.parse_bool("true") is True and P.parse_bool("false") is False
    assert P.parse_bool("unknown") is None
    assert P.hvac_mode("auxHeatOnly") == "auxHeatOnly" and P.hvac_mode("cool") == "cool"


def test_summary_parsing_tokens_and_equipment():
    entries = P.parse_summary(load("summary.json"))
    assert [e.identifier for e in entries] == ["411111111111", "422222222222", "433333333333"]
    hall = entries[0]
    assert hall.name == "Hallway" and hall.connected is True
    assert hall.thermostat_rev == "261004193016" and hall.runtime_rev == "261004193200"
    assert hall.token == "261004193016|261004193200"
    assert hall.equipment == ("compCool1", "fan")
    assert entries[1].equipment == ("compCool1", "compCool2", "fan")
    assert entries[2].connected is False and entries[2].equipment == ()


def test_summary_name_with_colon_is_rejoined():
    payload = {"revisionList": ["5:Den: West:true:1:2:3:4"], "statusList": ["5:heatPump"]}
    (e,) = P.parse_summary(payload)
    assert e.name == "Den: West" and e.thermostat_rev == "1" and e.runtime_rev == "3" and e.interval_rev == "4"
    assert e.equipment == ("heatPump",)


def test_automap_candidates():
    assert P.automap_candidates("Hallway") == {"main"}
    assert P.automap_candidates("Main Floor") == {"main"}
    assert P.automap_candidates("MainFloor") == {"main"}
    assert P.automap_candidates("Downstairs") == {"main"}
    assert P.automap_candidates("Toy Room") == {"up"}
    assert P.automap_candidates("Upstairs") == {"up"}
    assert P.automap_candidates("Master Bedroom") == {"bed"}
    assert P.automap_candidates("Upstairs Bedroom") == {"up", "bed"}  # ambiguous
    assert P.automap_candidates("Garage") == set()
    assert P.automap_candidates("Supply Closet") == set()  # 'up' only counts at a word start


def test_thermostat_offset_and_event_times():
    t = tstat("411111111111")
    off = P.thermostat_utc_offset(t)
    assert off == timedelta(hours=-5)
    assert P.local_to_utc_offset("2026-10-04", "14:00:00", off) == datetime(2026, 10, 4, 19, 0, tzinfo=UTC)
    # a few seconds of skew between the two stamps still rounds to the zone offset
    assert P.thermostat_utc_offset({"thermostatTime": "2026-10-04 14:32:10", "utcTime": "2026-10-04 19:32:13"}) == off


def test_report_timestamp_conversion_handles_dst():
    # ordinary CDT row
    assert P.report_local_to_utc("2026-10-04", "00:00:00", CHI) == datetime(2026, 10, 4, 5, 0, tzinfo=UTC)
    # CST in winter
    assert P.report_local_to_utc("2026-12-01", "00:00:00", CHI) == datetime(2026, 12, 1, 6, 0, tzinfo=UTC)
    # fall back: 01:30 happens twice; the first occurrence (CDT) is used
    assert P.report_local_to_utc("2026-11-01", "01:30:00", CHI) == datetime(2026, 11, 1, 6, 30, tzinfo=UTC)
    # spring forward: 02:30 does not exist -> dropped
    assert P.report_local_to_utc("2026-03-08", "02:30:00", CHI) is None
    assert P.report_local_to_utc("2026-03-08", "03:00:00", CHI) == datetime(2026, 3, 8, 8, 0, tzinfo=UTC)
    # fixed-offset fallback zone
    assert P.report_local_to_utc("2026-10-04", "00:00:00", timezone(timedelta(hours=-5))) == datetime(
        2026, 10, 4, 5, 0, tzinfo=UTC)
    assert P.report_local_to_utc("2026-10-04", "bad", CHI) is None


def test_remote_sensors_mapping_and_unknown_values():
    t = tstat("411111111111")
    ts = datetime(2026, 10, 4, 19, 32, 10, tzinfo=UTC)
    sp = P.parse_remote_sensors("main", t["remoteSensors"], sensor_refs(), ts)
    by_key = {r.sensor_key: r for r in sp.readings}
    assert set(by_key) == {"main.hallway_tstat", "main.school_room", "main.living_room", "main.kitchen"}
    hall = by_key["main.hallway_tstat"]
    assert (hall.temp_f, hall.humidity, hall.occupied, hall.online) == (74.1, 48.0, True, True)
    school = by_key["main.school_room"]
    assert (school.temp_f, school.humidity, school.occupied) == (72.8, None, False)
    kitchen = by_key["main.kitchen"]
    assert kitchen.temp_f is None and kitchen.occupied is None and kitchen.online is False
    assert sp.new_mappings == {"main.hallway_tstat": "ei:0", "main.school_room": "rs:100",
                               "main.living_room": "rs:101", "main.kitchen": "rs:102"}
    garage = next(m for m in sp.meta if m["id"] == "rs:103")
    assert garage["sensor_key"] is None and garage["capabilities"] == ["occupancy", "temperature"]


def test_sensor_ids_are_scoped_per_thermostat_and_explicit_mapping_wins():
    refs = [P.SensorRef(s.key, s.unit_key, s.kind, s.name, "rs:100" if s.key == "up.girls_room" else None)
            for s in SENSORS]
    t = tstat("422222222222")
    sp = P.parse_remote_sensors("up", t["remoteSensors"], refs, datetime.now(UTC))
    # rs:100 is explicitly mapped to the Girls' Room (owner's choice) even though it is named "Toy Room Sensor"
    assert sp.id_to_key["rs:100"] == "up.girls_room"
    assert sp.id_to_key["ei:0"] == "up.toy_room_tstat"
    tstat_reading = next(r for r in sp.readings if r.sensor_key == "up.toy_room_tstat")
    assert tstat_reading.occupied is None  # the Essential has no occupancy capability
    # 'rs:100' on the main thermostat is a different sensor
    key, by_name = P.match_sensor("main", "rs:100", "School Room", None, refs)
    assert (key, by_name) == ("main.school_room", True)


def test_sensor_sets_by_climate_ref():
    t = tstat("422222222222")
    sp = P.parse_remote_sensors("up", t["remoteSensors"], sensor_refs(), datetime.now(UTC))
    assert sp.id_to_key == {"ei:0": "up.toy_room_tstat", "rs:100": "up.toy_room", "rs:101": "up.girls_room"}
    sets = P.parse_sensor_sets(t["program"], sp.id_to_key)
    assert sets == {"home": ["up.toy_room", "up.toy_room_tstat"], "away": ["up.toy_room_tstat"],
                    "sleep": ["up.girls_room"]}


def test_settings_and_outdoor():
    t = tstat("411111111111")
    s = P.curated_settings(t["settings"])
    assert s["autoAway"] is True and s["followMeComfort"] is False
    assert s["heatCoolMinDelta"] == 4.0 and s["coolRangeLow"] == 65.0
    assert P.outdoor_now(t["weather"]) == (84.2, 40.0)
    assert P.outdoor_now({"forecasts": [{"temperature": -5002}]}) == (None, None)


def test_hold_parsing_types():
    hall = tstat("411111111111")
    off = P.thermostat_utc_offset(hall)
    ev = P.running_override(hall["events"])
    assert ev is not None and ev["type"] == "hold"
    hold = P.parse_hold(ev, off, hall["program"], None)
    assert hold.kind == "temperature" and (hold.heat_f, hold.cool_f) == (68.0, 75.0)
    assert hold.start == datetime(2026, 10, 4, 19, 0, tzinfo=UTC)
    assert hold.end == datetime(2026, 10, 4, 21, 0, tzinfo=UTC)
    assert hold.hold_type == "holdHours" and hold.set_by_us is False

    ours = {"heat_f": 68.0, "cool_f": 75.0, "end": "2026-10-04T21:00:00+00:00"}
    assert P.parse_hold(ev, off, hall["program"], ours).set_by_us is True
    assert P.parse_hold(ev, off, hall["program"], {**ours, "heat_f": 67.5}).set_by_us is False

    bed = tstat("433333333333")
    indefinite = P.parse_hold(P.running_override(bed["events"]), off, bed["program"], None)
    assert indefinite.hold_type == "indefinite" and indefinite.end.year == 2035

    # ends at the next program transition (22:00 local: home -> sleep)
    nt = dict(ev, startTime="14:07:31", endTime="22:00:00")
    assert P.parse_hold(nt, off, hall["program"], None).hold_type == "nextTransition"
    odd = dict(ev, startTime="14:07:31", endTime="17:45:00")
    assert P.parse_hold(odd, off, hall["program"], None).hold_type == "dateTime"
    climate = dict(ev, holdClimateRef="away")
    assert P.parse_hold(climate, off, hall["program"], None).kind == "climate"
    vac = dict(ev, type="vacation")
    assert P.parse_hold(vac, off, hall["program"], None).hold_type == "vacation"
    assert P.running_override([dict(ev, running=False)]) is None


def test_event_overrides_keep_their_type_and_are_never_ours():
    """Finding 2: Smart Home/Away, vacation, demand response and quick save are told apart
    from plain holds by hold_type, and never count as the controller's hold."""
    hall = tstat("411111111111")
    off = P.thermostat_utc_offset(hall)
    ev = P.running_override(hall["events"])
    ours = {"heat_f": 68.0, "cool_f": 75.0, "end": "2026-10-04T21:00:00+00:00"}  # same setpoints and end
    assert P.EVENT_HOLD_TYPES == ("vacation", "autoAway", "autoHome", "quickSave", "demandResponse")
    for etype in P.EVENT_HOLD_TYPES:
        event = dict(ev, type=etype, holdClimateRef="away" if etype == "autoAway" else "")
        assert P.running_override([event]) is event
        hold = P.parse_hold(event, off, hall["program"], ours)
        assert hold.hold_type == etype and hold.set_by_us is False, etype
    smart_away = P.parse_hold(dict(ev, type="autoAway", holdClimateRef="away"), off, hall["program"], ours)
    assert smart_away.kind == "climate" and smart_away.climate_ref == "away"
    # plain holds only ever carry an inferred plain type
    plain = [P.parse_hold(dict(ev, endTime=t), off, hall["program"], None).hold_type
             for t in ("16:00:00", "22:00:00", "17:45:00")]
    plain.append(P.parse_hold(P.running_override(tstat("433333333333")["events"]), off, {}, None).hold_type)
    assert plain == ["holdHours", "nextTransition", "dateTime", "indefinite"]
    assert set(plain) == set(P.PLAIN_HOLD_TYPES)
    # the first RUNNING override is the one in effect (a Smart Away above a hand-set hold)
    stacked = [dict(ev, type="autoAway", holdClimateRef="away"), ev]
    assert P.parse_hold(P.running_override(stacked), off, hall["program"], ours).hold_type == "autoAway"


def test_report_chunks_are_at_most_31_days_and_inclusive():
    start = datetime(2026, 8, 1, 0, 3, tzinfo=UTC)
    end = datetime(2026, 9, 15, 0, 0, tzinfo=UTC)
    chunks = P.report_chunks(start, end)
    assert len(chunks) == 2
    assert chunks[0][0] == datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    assert chunks[-1][1] == datetime(2026, 9, 14, 23, 55, tzinfo=UTC)
    for first, last in chunks:
        assert (last - first) <= timedelta(days=31)
    assert chunks[1][0] == chunks[0][1] + timedelta(minutes=5)
    body = P.report_request(["1", "2"], *chunks[0])
    assert body["startDate"] == "2026-08-01" and body["startInterval"] == 0
    assert body["endDate"] == "2026-08-30" and body["endInterval"] == 287
    assert body["includeSensors"] is True and body["columns"].split(",")[2] == "compCool1"
    assert P.report_chunks(end, end) == []


def test_runtime_report_parsing():
    payload = load("runtime_report.json")
    start = datetime(2026, 10, 4, 5, 0, tzinfo=UTC)
    end = datetime(2026, 10, 4, 5, 20, tzinfo=UTC)
    units = {"411111111111": "main", "422222222222": "up"}
    rows = P.parse_runtime_report(payload, units, sensor_refs(), {k: CHI for k in units}, start, end)
    by = {(r.unit_key, r.ts): r for r in rows}
    # 23:55 local (04:55Z) is outside the window; the blank 00:10 row is skipped (no data != zero runtime)
    assert sorted({r.ts.minute for r in rows if r.unit_key == "main"}) == [0, 5, 15]
    first = by[("main", datetime(2026, 10, 4, 5, 0, tzinfo=UTC))]
    assert first.comp_cool1 == 300 and first.comp_cool2 == 120  # stage 2 kept separate, never summed
    assert first.fan == 300 and first.aux_heat1 == 0
    assert first.hvac_mode == "cool" and first.climate_ref == "sleep"
    assert (first.zone_temp_f, first.zone_humidity, first.heat_sp_f, first.cool_sp_f) == (74.1, 48.0, 67.0, 74.0)
    assert (first.outdoor_temp_f, first.outdoor_humidity) == (71.2, 80.0)
    assert first.sensor_temps == {"main.hallway_tstat": 74.1, "main.school_room": 72.6}
    assert first.sensor_occupancy == {"main.hallway_tstat": True, "main.school_room": False}
    second = by[("main", datetime(2026, 10, 4, 5, 5, tzinfo=UTC))]
    assert second.comp_cool2 == 0  # blank -> 0
    assert second.sensor_occupancy["main.school_room"] is None  # blank -> None
    last = by[("main", datetime(2026, 10, 4, 5, 15, tzinfo=UTC))]
    assert last.comp_cool1 == 0 and last.zone_humidity is None and last.outdoor_temp_f is None
    assert last.sensor_temps["main.school_room"] is None
    up = by[("up", datetime(2026, 10, 4, 5, 0, tzinfo=UTC))]
    assert up.comp_cool1 == 300 and up.comp_cool2 == 300
    assert up.sensor_temps == {"up.toy_room_tstat": 76.9, "up.toy_room": 76.2, "up.girls_room": 75.8}
    assert up.sensor_occupancy == {"up.toy_room": True, "up.girls_room": True}
    assert all(r.ts.tzinfo is not None and r.ts.minute % 5 == 0 for r in rows)
    # climate names resolve through the program map when one is known
    rows2 = P.parse_runtime_report(payload, units, sensor_refs(), {k: CHI for k in units}, start, end,
                                   {"411111111111": {"Sleep": "smart1"}})
    assert next(r for r in rows2 if r.unit_key == "main").climate_ref == "smart1"
