"""controller.tick / execute_queued / current_plan against the test database with a fake source."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from climate.control import controller
from climate.house import SENSORS
from climate.sources.base import HoldInfo, HoldRequest, SourceHealth, UnitSnapshot, WriteResult
from climate.state import load_house_state
from climate.store.app_settings import ControlSettings, OccupancySettings, SourceSettings, put_setting
from climate.store.orm import ControlAction, LiveSensor, LiveUnit

TZ = "America/Chicago"
NOW = datetime(2026, 7, 15, 14, 0, tzinfo=ZoneInfo(TZ)).astimezone(UTC)  # Wednesday afternoon


class FakeSource:
    def __init__(self, kind: str = "simulator", ok: bool = True) -> None:
        self.kind = kind
        self.ok = ok
        self.holds: list[HoldRequest] = []
        self.resumes: list[str] = []
        self.forced: list[bool] = []
        self.refuse_resume = False

    async def set_hold(self, req: HoldRequest) -> WriteResult:
        self.holds.append(req)
        if self.ok:
            return WriteResult(ok=True, channel=self.kind, request=req.model_dump(mode="json"),
                               readback={"heat_f": req.heat_f, "cool_f": req.cool_f})
        return WriteResult(ok=False, channel=self.kind, request=req.model_dump(mode="json"),
                           readback={"heat_f": 70.0, "cool_f": 80.0}, error="Read-back 70/80 does not match.")

    async def resume_program(self, unit_key: str, reason: str, force: bool = False) -> WriteResult:
        self.resumes.append(unit_key)
        self.forced.append(force)
        if self.refuse_resume and not force:  # what the ecobee adapter returns for a hold it did not write
            return WriteResult(ok=False, channel=self.kind, request={"unit_key": unit_key, "force": force},
                               error="the running hold was not set by the controller; not cancelled")
        return WriteResult(ok=True, channel=self.kind, readback={"hold": None})

    async def poll_revisions(self) -> dict[str, str]:
        return {}

    async def fetch_snapshots(self, unit_keys=None):
        return []

    async def fetch_runtime(self, start, end):
        return []

    async def health(self) -> SourceHealth:
        return SourceHealth(ok=True, kind=self.kind)  # type: ignore[arg-type]

    async def close(self) -> None:
        return None


def put_snapshot(db, unit_key: str, ts: datetime, heat: float = 67.0, cool: float = 78.0,
                 hold: HoldInfo | None = None, **kw) -> None:
    snap = UnitSnapshot(unit_key=unit_key, ts=ts, source="simulator", hvac_mode="cool", heat_sp_f=heat,
                        cool_sp_f=cool, hold=hold, zone_humidity=45.0, zone_temp_f=76.0, **kw)
    data = snap.model_dump(mode="json")
    db.execute(insert(LiveUnit).values(unit_key=unit_key, ts=ts, source="simulator", snapshot=data)
               .on_conflict_do_update(index_elements=[LiveUnit.unit_key],
                                      set_={"ts": ts, "snapshot": data}))


def setup_house(db, now: datetime = NOW, *, mode: str = "act", occupied: tuple[str, ...] = ("toy_room", "office"),
                act_units: list[str] | None = None, phones_away: bool | None = None) -> None:
    ctl = ControlSettings(mode=mode)  # type: ignore[arg-type]
    if act_units is not None:
        ctl.act_units = act_units
    put_setting(db, "control", ctl)
    put_setting(db, "occupancy", OccupancySettings(phones_away=phones_away))
    for s in SENSORS:
        occ = (s.room_key in occupied) if s.has_occupancy else None
        values = {"sensor_key": s.key, "ts": now - timedelta(minutes=1), "source": "simulator", "temp_f": 75.0,
                      "humidity": 45.0 if s.has_humidity else None, "occupied": occ, "motion": occ, "online": True}
        db.execute(insert(LiveSensor).values(**values).on_conflict_do_update(
            index_elements=[LiveSensor.sensor_key], set_=values))
    for u in ("main", "up", "bed"):
        put_snapshot(db, u, now - timedelta(minutes=1))
    db.commit()


def refresh_sensors(db, ts: datetime) -> None:
    db.execute(text("UPDATE live_sensors SET ts = :ts"), {"ts": ts - timedelta(minutes=1)})
    db.commit()


def actions(db, **where) -> list[ControlAction]:
    db.expire_all()
    q = select(ControlAction).order_by(ControlAction.id)
    for k, v in where.items():
        q = q.where(getattr(ControlAction, k) == v)
    return list(db.execute(q).scalars())


async def test_suggest_mode_logs_without_writing(db):
    setup_house(db, mode="suggest")
    src = FakeSource()
    ids = await controller.tick(src, NOW)
    rows = actions(db, status="suggested")
    assert {r.unit_key for r in rows} == {"main", "up", "bed"} and set(ids) == {r.id for r in rows}
    assert all(r.mode == "suggest" and r.channel == "none" and r.actor == "controller" for r in rows)
    up = next(r for r in rows if r.unit_key == "up")
    assert (up.request["heat_f"], up.request["cool_f"]) == (68.0, 77.0)
    assert up.before["cool_f"] == 78.0 and up.rule == "comfort" and "Toy Room" in up.reason
    main = next(r for r in rows if r.unit_key == "main")
    assert main.rule == "linked_floors"
    assert src.holds == [] and src.resumes == []
    # five minutes later the same suggestion is not repeated
    refresh_sensors(db, NOW + timedelta(minutes=5))
    assert await controller.tick(src, NOW + timedelta(minutes=5)) == []
    assert len(actions(db, status="suggested")) == 3
    n_states = db.execute(text("SELECT count(*) FROM room_states")).scalar()
    assert n_states == 22  # 11 rooms x 2 ticks


async def test_act_mode_writes_and_reads_back(db):
    setup_house(db)
    src = FakeSource()
    ids = await controller.tick(src, NOW)
    assert len(ids) == 3 and len(src.holds) == 3
    assert all(h.hours == 2 for h in src.holds)
    rows = actions(db, status="verified")
    assert {r.unit_key for r in rows} == {"main", "up", "bed"}
    for r in rows:
        assert r.channel == "simulator" and r.mode == "act" and r.action == "set_hold"
        assert r.readback_ok is True and r.readback is not None and r.completed_at is not None
        assert r.policy_version_id is not None
    # rate limit: nothing new five minutes later
    refresh_sensors(db, NOW + timedelta(minutes=5))
    assert await controller.tick(src, NOW + timedelta(minutes=5)) == []
    assert len(src.holds) == 3


async def test_readback_failure_is_logged_failed_and_not_hammered(db):
    setup_house(db, act_units=["up"])
    src = FakeSource(ok=False)
    await controller.tick(src, NOW)
    failed = actions(db, status="failed")
    assert [r.unit_key for r in failed] == ["up"]
    assert failed[0].readback_ok is False and "does not match" in failed[0].error
    # main and bed are not in act_units: suggestions only
    assert {r.unit_key for r in actions(db, status="suggested")} == {"main", "bed"}
    refresh_sensors(db, NOW + timedelta(minutes=6))
    await controller.tick(src, NOW + timedelta(minutes=6))
    assert len(src.holds) == 1  # no retry within 30 minutes of a failure


async def test_renews_our_hold_before_it_ends(db):
    setup_house(db, act_units=["up"])
    src = FakeSource()
    await controller.tick(src, NOW)
    assert [(h.heat_f, h.cool_f) for h in src.holds] == [(68.0, 77.0)]

    def show_hold(ts: datetime) -> None:
        put_snapshot(db, "up", ts, heat=68.0, cool=77.0,
                     hold=HoldInfo(heat_f=68.0, cool_f=77.0, end=NOW + timedelta(hours=2), hold_type="holdHours"))
        for u in ("main", "bed"):
            put_snapshot(db, u, ts)
        refresh_sensors(db, ts)

    t1 = NOW + timedelta(minutes=40)
    show_hold(t1)
    state = load_house_state(db, t1)
    assert state.units["up"].snapshot.hold.set_by_us is True
    assert state.units["up"].person_hold is None and state.units["up"].resume_backoff_until is None
    await controller.tick(src, t1)
    assert len(src.holds) == 1  # hold matches and ends in 80 min: nothing to do

    t2 = NOW + timedelta(minutes=105)
    show_hold(t2)
    await controller.tick(src, t2)
    assert len(src.holds) == 2 and (src.holds[1].heat_f, src.holds[1].cool_f) == (68.0, 77.0)
    renewed = actions(db, unit_key="up", status="verified")[-1]
    assert "Renewing" in renewed.reason
    # the snapshot still shows the old end time three minutes later: no second renewal
    t3 = t2 + timedelta(minutes=3)
    show_hold(t3)
    await controller.tick(src, t3)
    assert len(src.holds) == 2


async def test_resumes_program_when_schedule_matches(db):
    setup_house(db, act_units=["up"])
    db.add(ControlAction(ts=NOW - timedelta(hours=1), unit_key="up", actor="controller", mode="act",
                         channel="simulator", action="set_hold", status="verified", reason="earlier",
                         request={"unit_key": "up", "heat_f": 67.0, "cool_f": 76.0, "hours": 2}))
    put_snapshot(db, "up", NOW - timedelta(minutes=1), heat=67.0, cool=76.0,
                 hold=HoldInfo(heat_f=67.0, cool_f=76.0, end=NOW + timedelta(hours=1)),
                 settings={"program_heat_f": 68.0, "program_cool_f": 77.0})
    db.commit()
    src = FakeSource()
    await controller.tick(src, NOW)
    assert src.resumes == ["up"] and src.holds == []
    row = actions(db, unit_key="up", action="resume_program")[0]
    assert row.status == "verified" and "Resuming" in row.reason


async def test_a_persons_hold_wins_for_as_long_as_it_runs(db):
    setup_house(db, act_units=["up"])
    put_snapshot(db, "up", NOW - timedelta(minutes=1), heat=70.0, cool=74.0,
                 hold=HoldInfo(heat_f=70.0, cool_f=74.0, hold_type="indefinite"))
    db.commit()
    src = FakeSource()
    await controller.tick(src, NOW)
    assert src.holds == []
    (skipped,) = actions(db, unit_key="up", status="skipped")
    assert skipped.reason == ("Someone set a hold on the upstairs thermostat (70–74°F, until you change it); the "
                              "controller waits until it ends or you choose Back to automatic.")
    assert skipped.request["kind"] == "manual_hold_detected" and skipped.request["until"] is None
    state = load_house_state(db, NOW + timedelta(minutes=5))
    ph = state.units["up"].person_hold
    assert ph is not None and ph.until is None and ph.by == "thermostat" and ph.detection_id == skipped.id
    assert state.units["up"].resume_backoff_until is None

    for later in (NOW + timedelta(minutes=10), NOW + timedelta(hours=4, minutes=5)):  # no time limit
        put_snapshot(db, "up", later, heat=70.0, cool=74.0, hold=HoldInfo(heat_f=70.0, cool_f=74.0))
        refresh_sensors(db, later)
        await controller.tick(src, later)
        assert src.holds == [] and len(actions(db, unit_key="up", status="skipped")) == 1


async def test_circuit_open_queues_homekit_climate_hold(db):
    setup_house(db)
    put_setting(db, "source", SourceSettings(kind="ecobee", homekit_enabled=True,
                                             cloud_circuit_open_until=NOW + timedelta(hours=1)))
    db.commit()
    src = FakeSource(kind="ecobee")
    await controller.tick(src, NOW)
    assert src.holds == []
    queued = actions(db, status="queued")
    assert {r.unit_key for r in queued} == {"main", "up", "bed"}
    up = next(r for r in queued if r.unit_key == "up")
    assert up.channel == "homekit" and up.action == "set_hold" and up.actor == "controller"
    assert up.request["kind"] == "climate_hold" and up.request["climate"] == "home"
    assert datetime.fromisoformat(up.request["until"]) == NOW + timedelta(hours=2)
    # not queued twice while the first is pending
    refresh_sensors(db, NOW + timedelta(minutes=3))
    await controller.tick(src, NOW + timedelta(minutes=3))
    assert len(actions(db, status="queued")) == 3


async def test_no_homekit_fallback_in_simulator_mode(db):
    setup_house(db)
    put_setting(db, "source", SourceSettings(kind="simulator", homekit_enabled=True,
                                             cloud_circuit_open_until=NOW + timedelta(hours=1)))
    db.commit()
    src = FakeSource()
    await controller.tick(src, NOW)
    assert len(src.holds) == 3 and actions(db, channel="homekit") == []


async def test_execute_queued_owner_hold(db):
    setup_house(db, mode="suggest")
    put_snapshot(db, "up", NOW - timedelta(minutes=1), heat=68.0, cool=76.0)
    db.add_all([
        # a controller write 5 min ago: owner holds skip the rate limit
        ControlAction(ts=NOW - timedelta(minutes=5), unit_key="up", actor="controller", mode="act",
                      channel="simulator", action="set_hold", status="verified", reason="x",
                      request={"heat_f": 68.0, "cool_f": 76.0}),
        ControlAction(ts=NOW - timedelta(minutes=1), unit_key="up", actor="owner", mode="act", channel="simulator",
                      action="set_hold", status="queued", rule="manual", reason="Owner hold",
                      request={"unit_key": "up", "heat_f": 68.0, "cool_f": 72.0, "hours": 1, "reason": "Owner hold"}),
        ControlAction(ts=NOW - timedelta(minutes=1), unit_key="bed", actor="owner", mode="act", channel="simulator",
                      action="resume_program", status="queued", rule="manual", reason="Owner resume",
                      request={"unit_key": "bed"}),
        ControlAction(ts=NOW - timedelta(minutes=1), unit_key="main", actor="owner", mode="act", channel="homekit",
                      action="set_hold", status="queued", rule="manual", reason="for the homekit service",
                      request={"kind": "climate_hold", "climate": "home"}),
    ])
    db.commit()
    src = FakeSource()
    handled = await controller.execute_queued(src, NOW)
    assert len(handled) == 2
    # exactly what was typed: no 2°F step limit for the owner (bug 4)
    assert [(h.unit_key, h.heat_f, h.cool_f, h.hours) for h in src.holds] == [("up", 68.0, 72.0, 1)]
    assert src.resumes == ["bed"]
    hold = actions(db, actor="owner", unit_key="up")[0]
    assert hold.status == "verified" and hold.request["cool_f"] == 72.0 and "requested" not in hold.request
    assert actions(db, channel="homekit")[0].status == "queued"
    # the owner's hold is a person's hold (from the app): the controller waits until it ends
    put_snapshot(db, "up", NOW, heat=68.0, cool=72.0,
                 hold=HoldInfo(heat_f=68.0, cool_f=72.0, start=NOW, end=NOW + timedelta(hours=1)))
    db.commit()
    state = load_house_state(db, NOW + timedelta(minutes=1))
    ph = state.units["up"].person_hold
    assert ph is not None and ph.by == "app" and ph.until == NOW + timedelta(hours=1)
    assert state.units["up"].snapshot.hold.set_by_us is False


async def test_execute_queued_rejects_stale_unit(db):
    setup_house(db)
    put_snapshot(db, "up", NOW - timedelta(hours=1))
    db.add(ControlAction(ts=NOW, unit_key="up", actor="owner", mode="act", channel="simulator",
                         action="set_hold", status="queued", reason="Owner hold",
                         request={"heat_f": 68.0, "cool_f": 75.0, "hours": 2}))
    db.commit()
    src = FakeSource()
    await controller.execute_queued(src, NOW)
    row = actions(db, actor="owner")[0]
    assert row.status == "failed" and "min old" in row.error and src.holds == []


async def test_mode_off_only_records_room_states(db):
    setup_house(db, mode="off")
    src = FakeSource()
    assert await controller.tick(src, NOW) == []
    assert actions(db) == [] and src.holds == []
    assert db.execute(text("SELECT count(*) FROM room_states")).scalar() == 11


async def test_current_plan_has_no_side_effects(db):
    setup_house(db, act_units=["up", "main"])
    rows = controller.current_plan(db, NOW)
    assert [r.target.unit_key for r in rows] == ["main", "up", "bed"]
    by = {r.target.unit_key: r for r in rows}
    assert by["up"].would_write and by["main"].would_write
    assert not by["bed"].would_write and "suggest-only" in by["bed"].guard.blocked_reason
    assert by["up"].current_cool_f == 78.0
    assert actions(db) == []
