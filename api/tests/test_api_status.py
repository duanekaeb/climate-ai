"""/status assembly and degradation, plus room history, runtime and weather readers."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from climate import state
from climate.analytics import baseline, daily, metrics
from climate.api.schemas import OPEN_METEO_ATTRIBUTION, PlanRow
from climate.control import controller
from climate.control.guardrails import GuardResult
from climate.control.policy import HouseState, PolicyParams, RoomStatus, UnitStatus, UnitTarget
from climate.house import ROOMS
from climate.sources.base import UnitSnapshot
from climate.store.app_settings import (
    ControlSettings,
    Heartbeat,
    LocationSettings,
    OccupancySettings,
    put_setting,
)
from climate.store.db import session_scope
from climate.store.orm import Alert, HomekitDevice, LiveSensor, LiveUnit, RoomStateRow, WeatherHour
from climate.timeutil import day_bounds_utc, local_date, utcnow
from tests.factories import make_history


def _boom(*args, **kwargs):
    raise RuntimeError("service exploded")


def _snapshot(unit_key: str, **kw) -> dict:
    snap = UnitSnapshot(unit_key=unit_key, ts=utcnow(), source="simulator", hvac_mode="cool",
                        equipment_running=["compCool1", "fan"], heat_sp_f=68, cool_sp_f=76, zone_temp_f=75.5,
                        zone_humidity=47, climate_ref="home", **kw)
    return snap.model_dump(mode="json")


@pytest.fixture
def broken_services(monkeypatch):
    monkeypatch.setattr(state, "load_house_state", _boom)
    monkeypatch.setattr(controller, "current_plan", _boom)
    monkeypatch.setattr(metrics, "unit_today", _boom)


def test_status_degrades_when_services_raise(owner, broken_services):
    now = utcnow()
    with session_scope() as s:
        s.add(LiveUnit(unit_key="main", ts=now, source="simulator", snapshot=_snapshot("main")))
        s.add(LiveSensor(sensor_key="main.hallway_tstat", ts=now, source="simulator", temp_f=75.0, humidity=48))
        s.add(LiveSensor(sensor_key="up.toy_room", ts=now, source="simulator", temp_f=77.0))
        s.add(LiveSensor(sensor_key="up.toy_room_tstat", ts=now, source="simulator", temp_f=78.0))
    r = owner.get("/api/status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [u["unit_key"] for u in body["units"]] == ["main", "up", "bed"]
    main = body["units"][0]
    assert main["zone_temp_f"] == 75.5 and main["call"] == "cool" and main["connected"] is True
    assert main["target"] is None and main["today_runtime_min"] == 0.0
    assert body["units"][1]["call"] == "unknown" and body["units"][1]["connected"] is False
    rooms = {r["room_key"]: r for r in body["rooms"]}
    assert len(rooms) == len(ROOMS)
    assert rooms["hallway"]["temp_f"] == 75.0
    assert rooms["toy_room"]["temp_f"] == 77.5  # mean of the room's two sensors
    for unsensored in ("twins_room", "olive_room", "foyer"):
        assert rooms[unsensored]["temp_f"] is None  # never invented
    assert rooms["foyer"]["state"] == "no_target"
    assert body["house_empty"] is False
    assert body["controller"]["mode"] == "suggest"


def _house_state(now) -> HouseState:
    rooms = {
        r.key: RoomStatus(room_key=r.key, name=r.name, unit_key=r.unit_key, floor=r.floor, has_sensor=r.has_sensor,
                          is_sleep_room=r.is_sleep_room, has_comfort_target=r.has_comfort_target,
                          temp_f=74.0 if r.has_sensor else None, state="occupied", reason="motion 2 min ago")
        for r in reversed(ROOMS)
    }
    snap = UnitSnapshot.model_validate(_snapshot("up"))
    units = {
        k: UnitStatus(unit_key=k, name=k.title(), snapshot=snap if k == "up" else None, age_s=30 if k == "up" else None,
                      call="cool" if k == "up" else "unknown")
        for k in ("bed", "up", "main")
    }
    return HouseState(now=now, tz="America/Chicago", units=units, rooms=rooms, house_empty=False,
                      house_empty_reason="Someone is home.", control=ControlSettings(), occupancy=OccupancySettings(),
                      policy=PolicyParams())


def test_status_uses_the_services(owner, monkeypatch):
    target = UnitTarget(unit_key="up", heat_f=68, cool_f=76, rule="comfort", reason="Toy Room occupied")
    row = PlanRow(target=target, guard=GuardResult(ok=True, heat_f=68, cool_f=76), current_heat_f=68,
                  current_cool_f=77, would_write=True)
    monkeypatch.setattr(state, "load_house_state", lambda session, now=None: _house_state(now))
    monkeypatch.setattr(controller, "current_plan", lambda session, now=None: [row])
    monkeypatch.setattr(metrics, "unit_today", lambda session, now, tz: {
        "up": {"today_runtime_min": 312.5, "duty_last_hour_pct": 96.0, "maxed_minutes_today": 120.0}})
    body = owner.get("/api/status").json()
    assert [u["unit_key"] for u in body["units"]] == ["main", "up", "bed"]
    up = body["units"][1]
    assert up["target"]["reason"] == "Toy Room occupied"
    assert (up["today_runtime_min"], up["duty_last_hour_pct"], up["maxed_minutes_today"]) == (312.5, 96.0, 120.0)
    assert up["running"] == ["compCool1", "fan"] and up["hold"] is None
    assert body["units"][0]["target"] is None
    assert [r["room_key"] for r in body["rooms"]] == [r.key for r in ROOMS]  # house order
    assert body["house_empty_reason"] == "Someone is home."


def test_status_parts_from_heartbeats_weather_and_alerts(owner, broken_services):
    now = utcnow()
    tz = "America/Chicago"
    today = local_date(now, tz)
    hour = now.replace(minute=0, second=0, microsecond=0)
    day_start, _ = day_bounds_utc(today, tz)
    with session_scope() as s:
        put_setting(s, "heartbeat:worker", Heartbeat(at=now, ok=True, detail={
            "last_tick_at": now.isoformat(), "source": {"ok": True, "detail": "simulator running",
                                                        "last_success_at": now.isoformat()}}))
        put_setting(s, "heartbeat:homekit", Heartbeat(at=now - timedelta(minutes=1)))
        put_setting(s, "heartbeat:agent", Heartbeat(at=now, ok=True, detail={
            "signed_in": True, "sdk_version": "0.9.1", "token_expires_at": (today + timedelta(days=10)).isoformat()}))
        put_setting(s, "location", LocationSettings(confirmed=True, lat=41.9, lon=-87.6))
        s.add(HomekitDevice(device_id="aa:bb:cc:dd:ee:01", name="Hallway", pairing_state="paired", online=True))
        s.add(HomekitDevice(device_id="aa:bb:cc:dd:ee:02", name="Bedroom", pairing_state="none", online=True))
        s.add(Alert(level="warn", kind="sensor_offline", title="Office sensor offline", body=""))
        s.add(Alert(level="info", kind="old", title="Resolved", body="", resolved_at=now))
        s.add(WeatherHour(ts=hour, source="open-meteo", kind="observed", temp_f=88.4, rh=41, cloud_cover=10,
                          shortwave_wm2=650))
        s.add(WeatherHour(ts=hour, source="simulator", kind="observed", temp_f=60.0))
        for i, t in enumerate((70.0, 95.2, 81.0)):
            s.add(WeatherHour(ts=day_start + timedelta(hours=6 * i + 1), source="open-meteo", kind="forecast", temp_f=t))
    body = owner.get("/api/status").json()
    assert body["source"] == {"kind": "simulator", "ok": True, "detail": "simulator running", "signed_in": None,
                              "last_success_at": body["source"]["last_success_at"]}
    assert body["source"]["last_success_at"] is not None
    assert body["controller"]["last_tick_at"] is not None
    assert body["homekit"]["online"] is True and body["homekit"]["paired"] == 1
    assert body["agent"]["signed_in"] is True and body["agent"]["sdk_version"] == "0.9.1"
    assert "expires in 10 days" in body["agent"]["token_warning"]
    assert body["agent"]["next_nightly_at"] is not None
    w = body["weather"]
    assert (w["temp_f"], w["source"], w["forecast_high_f"], w["forecast_low_f"]) == (88.4, "open-meteo", 95.2, 70.0)
    assert w["attribution"] == OPEN_METEO_ATTRIBUTION
    assert [a["title"] for a in body["alerts"]] == ["Office sensor offline"]
    assert body["location_confirmed"] is True


def test_status_reports_stale_worker_and_ecobee_sign_in(owner, broken_services):
    from climate.store.app_settings import SourceSettings

    now = utcnow()
    with session_scope() as s:
        put_setting(s, "source", SourceSettings(kind="ecobee"))
        put_setting(s, "ecobee_status", {"signed_in": False, "last_error": "refresh rejected"})
        put_setting(s, "heartbeat:worker", Heartbeat(at=now - timedelta(minutes=30), ok=True))
    src = owner.get("/api/status").json()["source"]
    assert src["kind"] == "ecobee" and src["ok"] is False and src["signed_in"] is False
    assert "has not reported for 30 min" in src["detail"]


# --- rooms / runtime / weather -------------------------------------------------------------


def test_room_history(owner):
    with session_scope() as s:
        hist = make_history(s, days=1)  # readings end at the top of the current hour
        at = hist["end"] - timedelta(minutes=17)
        s.add(RoomStateRow(ts=at, room_key="twins_room", state="asleep", confidence=0.9, reason="sleep window"))
        s.add(RoomStateRow(ts=at, room_key="hallway", state="occupied", confidence=0.9, reason="motion"))
    r = owner.get("/api/rooms/hallway/history", params={"hours": 6})
    assert r.status_code == 200
    body = r.json()
    assert body["hours"] == 6
    assert 60 <= len(body["points"]) <= 75
    assert all(p["temp_f"] is not None for p in body["points"])
    assert any(p["state"] == "occupied" for p in body["points"])
    assert len(body["setpoints"]) >= 60 and body["setpoints"][0]["cool_sp_f"] == 76.0

    twins = owner.get("/api/rooms/twins_room/history").json()
    assert twins["points"] and all(p["temp_f"] is None for p in twins["points"])  # no sensor, no temperature
    assert twins["points"][-1]["state"] == "asleep"
    assert owner.get("/api/rooms/attic/history").status_code == 404
    assert owner.get("/api/rooms/hallway/history", params={"hours": 0}).status_code == 422


def test_runtime_intraday_and_weather(owner):
    with session_scope() as s:
        make_history(s, days=2)
        tz = "America/Chicago"
        start = utcnow().replace(minute=0, second=0, microsecond=0)
        for h in range(1, 5):
            s.add(WeatherHour(ts=start + timedelta(hours=h), source="open-meteo", kind="forecast", temp_f=90.0 + h))
    yesterday = local_date(utcnow(), tz) - timedelta(days=1)
    r = owner.get("/api/runtime/intraday", params={"date": yesterday.isoformat()})
    assert r.status_code == 200
    body = r.json()
    assert body["date"] == yesterday.isoformat()
    assert [u["unit_key"] for u in body["units"]] == ["main", "up", "bed"]
    assert all(len(u["points"]) >= 276 for u in body["units"])  # a full local day of 5-minute slots
    assert len(body["outdoor"]) >= 23
    assert owner.get("/api/runtime/intraday", params={"date": "not-a-date"}).status_code == 422

    w = owner.get("/api/weather", params={"hours_back": 12, "hours_ahead": 6}).json()
    kinds = [p["kind"] for p in w["points"]]
    assert w["source"] == "open-meteo" and w["attribution"] == OPEN_METEO_ATTRIBUTION
    assert 11 <= kinds.count("observed") <= 13 and kinds.count("forecast") == 4
    assert kinds == sorted(kinds, key=lambda k: k == "forecast")  # observed first, then forecast


def test_runtime_daily_converts_to_minutes(owner, monkeypatch):
    d = date(2026, 7, 1)

    def fake_rows(session, start, end, tz, unit_keys=None):
        return [
            daily.DayRow(day=d, unit_key="up", cool_s=3600, heat_s=0, aux_s=0, fan_s=4000, slots=288, mode="cool",
                         outdoor_mean_f=81.04, outdoor_max_f=92.0, cdd65=16.0, hdd65=0.0, maxed_min=95.0,
                         hourly_outdoor_f=[81.0] * 24),  # a complete day: expectation shown
            daily.DayRow(day=d, unit_key="main", cool_s=1800, heat_s=0, aux_s=0, fan_s=1800, slots=288, mode=None,
                         outdoor_mean_f=81.0, outdoor_max_f=92.0),
        ]

    fit = baseline.BaselineFit(unit_key="up", mode="cool", balance_point_f=65, intercept_s=0, slope_s_per_dd=200,
                               n_days=60, r2=0.9, cvrmse=0.1, nmbe=0.0, resid_std_s=300, resid_lag1=0.2,
                               train_start=d, train_end=d)
    monkeypatch.setattr(daily, "daily_rows", fake_rows)
    monkeypatch.setattr(baseline, "active_fits", lambda session: {("up", "cool"): fit})
    monkeypatch.setattr(baseline, "expected_seconds", lambda f, row: 4500.0)
    r = owner.get("/api/runtime/daily", params={"days": 7})
    assert r.status_code == 200
    rows = r.json()
    assert [x["unit_key"] for x in rows] == ["main", "up"]
    up = rows[1]
    assert (up["cool_min"], up["fan_min"], up["expected_min"], up["mode"], up["maxed_min"]) == (60.0, 66.7, 75.0, "cool", 95.0)
    assert rows[0]["expected_min"] is None  # no mode, no baseline
    monkeypatch.setattr(baseline, "active_fits", _boom)
    assert owner.get("/api/runtime/daily").json()[1]["expected_min"] is None  # baseline failure degrades
    assert owner.get("/api/runtime/daily", params={"days": 0}).status_code == 422


def test_status_needs_no_writes(owner, broken_services):
    """Reading status twice leaves no rows behind (it never persists state)."""
    owner.get("/api/status")
    owner.get("/api/status")
    with session_scope() as s:
        assert s.execute(select(RoomStateRow)).first() is None
