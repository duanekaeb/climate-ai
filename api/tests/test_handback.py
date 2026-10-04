"""Hand back to ecobee (spec §3): capturing each unit's original ecobee settings once, before
the controller's first change, and ``hand_back`` putting them back (controller off first,
resume only our holds, sensor sets and Smart Away / Follow Me restored, everything logged)."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from climate.control import controller, handback
from climate.sources.base import HoldInfo, UnitSnapshot, WriteResult
from climate.state import load_house_state
from climate.store.app_settings import ECOBEE_ORIGINAL_KEY, ControlSettings, get_raw, get_setting, put_setting
from climate.store.db import session_scope
from climate.store.orm import Alert, ControlAction, LiveUnit
from tests.test_controller_safety import DAY_SETS, NIGHT, our_hold
from tests.test_controller_tick import NOW, FakeSource, actions, setup_house

INDEFINITE_END = datetime(2035, 1, 1, tzinfo=NOW.tzinfo)


def put_ecobee_snapshot(db, unit_key: str, ts: datetime, *, heat: float = 67.0, cool: float = 78.0,
                        hold: HoldInfo | None = None, home: list[str] | None = None, auto_away: bool = False,
                        follow_me: bool = False) -> None:
    snap = UnitSnapshot(unit_key=unit_key, ts=ts, source="ecobee", hvac_mode="cool", heat_sp_f=heat, cool_sp_f=cool,
                        hold=hold, zone_humidity=45.0, zone_temp_f=76.0,
                        sensor_sets={"home": home} if home is not None else {},
                        settings={"autoAway": auto_away, "followMeComfort": follow_me, "heatCoolMinDelta": 3.0})
    data = snap.model_dump(mode="json")
    db.execute(insert(LiveUnit).values(unit_key=unit_key, ts=ts, source="ecobee", snapshot=data)
               .on_conflict_do_update(index_elements=[LiveUnit.unit_key],
                                      set_={"ts": ts, "source": "ecobee", "snapshot": data}))


def mode_now() -> str:
    with session_scope() as s:
        return get_setting(s, "control", ControlSettings).mode


class HandbackSource(FakeSource):
    """A cloud source that can also write sensor sets and settings, and records the
    controller mode (as committed) at every call."""

    def __init__(self, raise_on_settings: bool = False) -> None:
        super().__init__(kind="ecobee")
        self.modes: list[str] = []
        self.set_writes: list[tuple[str, dict[str, list[str]]]] = []
        self.settings_writes: list[tuple[str, dict[str, bool]]] = []
        self.raise_on_settings = raise_on_settings

    async def resume_program(self, unit_key: str, reason: str, force: bool = False) -> WriteResult:
        self.modes.append(mode_now())
        return await super().resume_program(unit_key, reason, force)

    async def update_sensor_sets(self, unit_key: str, sets: dict[str, list[str]], reason: str) -> WriteResult:
        self.modes.append(mode_now())
        self.set_writes.append((unit_key, sets))
        return WriteResult(ok=True, channel="ecobee", request={"unit_key": unit_key, "sets": sets},
                           readback={"sets": sets})

    async def apply_settings(self, unit_key: str, settings: dict[str, bool], reason: str) -> WriteResult:
        self.modes.append(mode_now())
        self.settings_writes.append((unit_key, dict(settings)))
        if self.raise_on_settings:
            raise RuntimeError("ecobee said no")
        return WriteResult(ok=True, channel="ecobee", before={"autoAway": False}, request={"settings": settings},
                           readback=dict(settings))


def house_to_hand_back(db) -> None:
    """Upstairs: our hold, sensors as captured, Smart Away was on. Main floor: a person's hold,
    sensors changed by the controller, Smart Away and Follow Me were on. Wing: never captured."""
    setup_house(db, mode="act")
    ts = NOW - timedelta(minutes=1)
    our_hold(db, "up", NOW - timedelta(minutes=30), 68.0, 77.0, channel="ecobee")
    put_ecobee_snapshot(db, "up", ts, heat=68.0, cool=77.0, home=DAY_SETS["up"],
                        hold=HoldInfo(heat_f=68.0, cool_f=77.0, start=NOW - timedelta(minutes=30),
                                      end=NOW + timedelta(minutes=90), hold_type="holdHours"))
    put_ecobee_snapshot(db, "main", ts, heat=70.0, cool=74.0, home=["main.hallway_tstat"],
                        hold=HoldInfo(heat_f=70.0, cool_f=74.0, start=NOW - timedelta(hours=1), end=INDEFINITE_END,
                                      hold_type="indefinite"))
    put_ecobee_snapshot(db, "bed", ts)
    put_setting(db, ECOBEE_ORIGINAL_KEY, {
        "up": {"captured_at": (NOW - timedelta(days=9)).isoformat(), "auto_away": True, "follow_me": False,
               "home_sensors": DAY_SETS["up"]},
        "main": {"captured_at": (NOW - timedelta(days=9)).isoformat(), "auto_away": True, "follow_me": True,
                 "home_sensors": DAY_SETS["main"]},
    })
    db.commit()


# ---------------------------------------------------------------------------------------
# capture
# ---------------------------------------------------------------------------------------


def test_capture_original_once_and_only_from_ecobee(db):
    ecobee = UnitSnapshot(unit_key="up", ts=NOW, source="ecobee", sensor_sets={"home": ["up.toy_room"]},
                          settings={"autoAway": True, "followMeComfort": "false"})
    sim = UnitSnapshot(unit_key="main", ts=NOW, source="simulator", settings={"autoAway": True})
    hk = UnitSnapshot(unit_key="bed", ts=NOW, source="homekit")
    assert handback.capture_original(db, [ecobee, sim, hk, None], NOW) == ["up"]
    stored = get_raw(db, ECOBEE_ORIGINAL_KEY)
    assert set(stored) == {"up"}
    assert stored["up"]["auto_away"] is True and stored["up"]["follow_me"] is False
    assert stored["up"]["home_sensors"] == ["up.toy_room"]
    # never overwritten: a later snapshot (after the controller switched Smart Away off) changes nothing
    later = ecobee.model_copy(update={"settings": {"autoAway": False}, "sensor_sets": {"home": ["up.girls_room"]}})
    assert handback.capture_original(db, [later], NOW + timedelta(days=1)) == []
    assert get_raw(db, ECOBEE_ORIGINAL_KEY) == stored
    no_sets = UnitSnapshot(unit_key="main", ts=NOW, source="ecobee", settings={})
    assert handback.capture_original(db, [no_sets], NOW) == ["main"]
    main = handback.load_originals(db)["main"]
    assert (main.auto_away, main.follow_me, main.home_sensors) == (None, None, None)


async def test_tick_captures_before_writing_even_when_off(db):
    setup_house(db, mode="off")
    put_ecobee_snapshot(db, "up", NOW - timedelta(minutes=1), home=DAY_SETS["up"], auto_away=True)
    db.commit()
    await controller.tick(FakeSource(kind="ecobee"), NOW)
    db.expire_all()
    up = handback.load_originals(db)["up"]
    assert up.auto_away is True and up.home_sensors == DAY_SETS["up"]
    assert set(handback.load_originals(db)) == {"up"}  # the simulator snapshots of main / bed say nothing


async def test_keep_settings_captures_before_its_first_write(db):
    setup_house(db, mode="act")
    for u in ("main", "up", "bed"):
        put_ecobee_snapshot(db, u, NOW - timedelta(minutes=1), auto_away=True, follow_me=True)
    db.commit()
    seen: list[object] = []

    class Ensures(FakeSource):
        async def ensure_settings(self) -> list[WriteResult]:
            with session_scope() as s:
                seen.append(get_raw(s, ECOBEE_ORIGINAL_KEY))
            return [WriteResult(ok=True, channel="ecobee", request={"unit_key": u, "noop": True}) for u in ("main",)]

    await controller.keep_settings(Ensures(kind="ecobee"), NOW)
    assert isinstance(seen[0], dict) and set(seen[0]) == {"main", "up", "bed"}
    assert all(v["auto_away"] is True and v["follow_me"] is True for v in seen[0].values())


def test_sensor_set_write_captures_before_its_first_write(db):
    setup_house(db, NIGHT, act_units=["main"])
    put_ecobee_snapshot(db, "main", NIGHT - timedelta(minutes=1), home=DAY_SETS["main"])
    db.commit()
    state = load_house_state(db, NIGHT)
    write = controller._sensor_set_write(db, state, "main", NIGHT, "ecobee")
    assert write is not None and write.sets == {"home": ["main.hallway_tstat"]}
    assert handback.load_originals(db)["main"].home_sensors == DAY_SETS["main"]


# ---------------------------------------------------------------------------------------
# hand back
# ---------------------------------------------------------------------------------------


async def test_hand_back_restores_what_the_controller_changed(db):
    house_to_hand_back(db)
    src = HandbackSource()
    steps = await controller.hand_back(src, NOW)
    assert controller.hand_back is handback.hand_back

    assert steps[0] == {"unit_key": None, "what": "Controller off", "ok": True,
                        "detail": "Was act; it writes nothing until you switch it back on."}
    assert mode_now() == "off" and set(src.modes) == {"off"}  # off, and committed, before any write
    # only our hold is resumed, never forced; the person's hold on the main floor is left alone
    assert src.resumes == ["up"] and src.forced == [False]
    assert src.set_writes == [("main", {"home": DAY_SETS["main"]})]
    assert src.settings_writes == [("main", {"autoAway": True, "followMeComfort": True}), ("up", {"autoAway": True})]

    by = {(st["unit_key"], st["what"]): st for st in steps}
    assert all(st["ok"] for st in steps)
    assert by[("up", "Resumed our hold")]["detail"] == "Back on the ecobee schedule."
    assert ("main", "Resumed our hold") not in by
    assert by[("up", "Home sensors restored")]["detail"] == "Already the captured set."
    assert by[("bed", "Home sensors restored")]["detail"].startswith("Skipped: no ecobee sensor set was captured")
    assert by[("bed", "Smart Away and Follow Me restored")]["detail"].startswith("Skipped")

    rows = actions(db, rule="handback")
    assert [(r.unit_key, r.action, r.status, r.actor, r.mode, r.channel) for r in rows] == [
        ("main", "update_program", "verified", "owner", "act", "ecobee"),
        ("main", "update_settings", "verified", "owner", "act", "ecobee"),
        ("up", "resume_program", "verified", "owner", "act", "ecobee"),
        ("up", "update_settings", "verified", "owner", "act", "ecobee"),
    ]
    assert rows[0].before == {"sensor_sets": {"home": ["main.hallway_tstat"]}}
    assert rows[0].readback == {"sets": {"home": DAY_SETS["main"]}} and rows[0].completed_at is not None
    assert rows[2].request == {"kind": "handback", "unit_key": "up"}
    assert rows[3].before["autoAway"] is False and rows[3].readback == {"autoAway": True}

    (alert,) = db.execute(select(Alert).where(Alert.kind == "handback")).scalars().all()
    assert alert.title == "Handed back to ecobee" and alert.level == "info" and "Failed" not in alert.body

    # the hand-back's resume is neither "Back to automatic" nor "Resume schedule": no back-off
    assert load_house_state(db, NOW + timedelta(minutes=5)).units["up"].resume_backoff_until is None


async def test_controller_is_off_first_even_when_a_later_step_fails(db):
    house_to_hand_back(db)
    src = HandbackSource(raise_on_settings=True)
    steps = await controller.hand_back(src, NOW)
    assert mode_now() == "off"
    failed = [st for st in steps if not st["ok"]]
    assert [(st["unit_key"], st["what"]) for st in failed] == [("main", "Smart Away and Follow Me restored"),
                                                              ("up", "Smart Away and Follow Me restored")]
    assert "ecobee said no" in failed[0]["detail"]
    assert src.resumes == ["up"] and len(src.set_writes) == 1  # the other steps still ran
    settings_rows = actions(db, rule="handback", action="update_settings")
    assert [r.status for r in settings_rows] == ["failed", "failed"] and "ecobee said no" in settings_rows[0].error
    (alert,) = db.execute(select(Alert).where(Alert.kind == "handback")).scalars().all()
    assert alert.body.startswith("Failed: main: Smart Away and Follow Me restored")

    # the controller stays off: a tick and the daily settings check write nothing
    class Ensures(FakeSource):
        ensured = 0

        async def ensure_settings(self) -> list[WriteResult]:
            self.ensured += 1
            return []

    later = Ensures(kind="ecobee")
    assert await controller.tick(later, NOW + timedelta(minutes=1)) == []
    check = await controller.keep_settings(later, NOW + timedelta(minutes=1))
    assert later.holds == [] and later.ensured == 0 and (check.mode, check.ids) == ("off", [])


async def test_steps_that_cannot_run_are_reported_as_skipped(db):
    house_to_hand_back(db)
    plain = FakeSource(kind="ecobee")  # resumes, but cannot write sensor sets or settings
    steps = await controller.hand_back(plain, NOW)
    by = {(st["unit_key"], st["what"]): st for st in steps}
    assert by[("main", "Home sensors restored")]["detail"] == "Skipped: this thermostat source cannot write sensor sets."
    assert by[("main", "Smart Away and Follow Me restored")]["detail"].startswith(
        "Skipped: this thermostat source cannot write settings (Smart Away on, Follow Me on)")
    assert all(st["ok"] for st in steps) and plain.resumes == ["up"]

    put_setting(db, "control", ControlSettings(mode="act"))
    db.commit()
    steps = await controller.hand_back(None, NOW)
    by = {(st["unit_key"], st["what"]): st for st in steps}
    assert by[("up", "Resumed our hold")]["detail"] == ("Skipped: no thermostat source is running. Our hold ends on "
                                                       "its own at 3:30 PM.")
    assert by[("main", "Home sensors restored")]["detail"] == "Skipped: no thermostat source is running."
    assert all(st["ok"] for st in steps) and mode_now() == "off"
    assert db.execute(select(ControlAction).where(ControlAction.rule == "handback",
                                                  ControlAction.channel == "none")).first() is None


async def test_skipped_restores_make_the_alert_say_not_done_yet(db):
    house_to_hand_back(db)
    steps = await controller.hand_back(None, NOW)  # the ecobee source could not be built (signed out, say)
    pending = handback.pending_steps([handback.HandbackStep(**st) for st in steps])
    assert [(st.unit_key, st.what) for st in pending] == [
        ("main", "Home sensors restored"), ("main", "Smart Away and Follow Me restored"),
        ("up", "Smart Away and Follow Me restored"),
    ]  # the wing had nothing captured, and our hold on up ends on its own: neither is pending
    (alert,) = db.execute(select(Alert).where(Alert.kind == "handback")).scalars().all()
    assert (alert.title, alert.level) == ("Hand-back incomplete", "warn")
    assert alert.body.startswith(
        "Not done yet: main: Home sensors (no thermostat source is running); main: Smart Away and Follow Me (Smart "
        "Away on, Follow Me on: no thermostat source is running); up: Smart Away and Follow Me (Smart Away on: no "
        "thermostat source is running). Hand back again to finish.")


async def test_the_simulator_never_gets_the_real_thermostats_originals(db):
    """The originals are the real ecobee thermostats' settings: with the simulator as the source
    they are not written onto the simulated house, nor reported as restored or "Already"."""
    from climate.collector.poller import poll_once
    from climate.sources.simulator import SimulatedHouse
    from climate.worker import LockedSource

    setup_house(db, mode="act")
    put_setting(db, ECOBEE_ORIGINAL_KEY, {
        u: {"captured_at": (NOW - timedelta(days=9)).isoformat(), "auto_away": True, "follow_me": True,
            "home_sensors": DAY_SETS[u]} for u in ("main", "up", "bed")})
    db.commit()
    house = SimulatedHouse(seed=7, clock=lambda: NOW)
    house.advance_to(NOW)
    src = LockedSource(house)
    await poll_once(src, NOW, {})
    steps = await controller.hand_back(src, NOW)
    restores = [st for st in steps if st["what"] in ("Home sensors restored", "Smart Away and Follow Me restored")]
    assert len(restores) == 6 and all(st["ok"] and st["detail"].startswith("Skipped (") for st in restores)
    assert all("captured from the real ecobee thermostats and the source is the simulator" in st["detail"]
               for st in restores)
    assert actions(db, rule="handback", action="update_settings") == []
    assert actions(db, rule="handback", action="update_program") == []
    (alert,) = db.execute(select(Alert).where(Alert.kind == "handback")).scalars().all()
    assert alert.title == "Hand-back incomplete" and "Not done yet" in alert.body


# ---------------------------------------------------------------------------------------
# hand back right after the controller wrote (stale live snapshot, a tick in flight)
# ---------------------------------------------------------------------------------------


class ReadingSource(HandbackSource):
    """A cloud source whose thermostats show the controller's own writes: fetch_snapshots
    returns a fresh snapshot per unit (our latest hold, the latest sensor set), or raises when
    ``fail``. The live_units table is left as the last poll wrote it."""

    def __init__(self, at: datetime, fail: bool = False) -> None:
        super().__init__()
        self.at = at
        self.fail = fail
        self.fetched = 0
        self.home: dict[str, list[str]] = dict(DAY_SETS)

    async def update_sensor_sets(self, unit_key, sets, reason):
        self.home[unit_key] = list(sets["home"])
        return await super().update_sensor_sets(unit_key, sets, reason)

    async def fetch_snapshots(self, unit_keys=None):
        self.fetched += 1
        if self.fail:
            raise RuntimeError("cloud unreachable")
        out = []
        for u in ("main", "up", "bed"):
            hold = next((h for h in reversed(self.holds) if h.unit_key == u), None)
            start = self.at - timedelta(seconds=20)  # written moments before this read
            info = None if hold is None else HoldInfo(heat_f=hold.heat_f, cool_f=hold.cool_f, start=start,
                                                      end=start + timedelta(hours=hold.hours), hold_type="holdHours")
            out.append(UnitSnapshot(unit_key=u, ts=self.at, source="ecobee", hvac_mode="cool",
                                    heat_sp_f=hold.heat_f if hold else 67.0, cool_sp_f=hold.cool_f if hold else 78.0,
                                    hold=info, zone_temp_f=76.0, zone_humidity=45.0, sensor_sets={"home": self.home[u]},
                                    settings={"autoAway": False, "followMeComfort": False}))
        return out


def _ecobee_house(db, now: datetime) -> None:
    setup_house(db, now)
    for u in ("main", "up", "bed"):
        put_ecobee_snapshot(db, u, now - timedelta(minutes=1), home=DAY_SETS[u])
    db.commit()


async def test_hand_back_reads_the_thermostats_again_before_judging(db, monkeypatch):
    _ecobee_house(db, NOW)
    src = ReadingSource(NOW + timedelta(minutes=1))
    monkeypatch.setattr(controller, "utcnow", lambda: NOW + timedelta(seconds=10))
    await controller.tick(src, NOW)
    held = sorted(h.unit_key for h in src.holds)
    assert held == ["bed", "main", "up"]
    steps = await controller.hand_back(src, NOW + timedelta(minutes=1))
    assert src.fetched == 1 and sorted(src.resumes) == held and src.forced == [False, False, False]
    assert "Read the thermostats" not in [st["what"] for st in steps]
    with session_scope() as s:  # what the re-read showed is stored, as a poll stores it
        assert all(row.ts == NOW + timedelta(minutes=1) for row in s.execute(select(LiveUnit)).scalars())


async def test_hand_back_without_a_fresh_read_undoes_writes_the_last_poll_cannot_show(db, monkeypatch):
    """The cloud cannot be re-read: the last poll is from before the controller's night sensor
    sets and holds, so those are not taken as "Already" / absent."""
    _ecobee_house(db, NIGHT)
    src = ReadingSource(NIGHT + timedelta(minutes=1), fail=True)
    monkeypatch.setattr(controller, "utcnow", lambda: NIGHT + timedelta(seconds=10))
    await controller.tick(src, NIGHT)
    night = sorted(u for u, _ in src.set_writes)
    held = sorted(h.unit_key for h in src.holds)
    assert night == ["main", "up"] and held
    src.set_writes.clear()
    steps = await controller.hand_back(src, NIGHT + timedelta(minutes=1))
    by = {(st["unit_key"], st["what"]): st for st in steps}
    assert by[(None, "Read the thermostats")]["detail"] == ("Could not re-read them (RuntimeError); judged from the "
                                                            "last poll.")
    assert src.set_writes == [(u, {"home": DAY_SETS[u]}) for u in night]
    assert sorted(src.resumes) == held and not any(src.forced)
    assert by[("bed", "Home sensors restored")]["detail"] == "Already the captured set."  # never written
    (alert,) = db.execute(select(Alert).where(Alert.kind == "handback")).scalars().all()
    assert "judged from the last poll" in alert.body


async def test_a_tick_writing_when_the_hand_back_job_runs_finishes_first(db, monkeypatch):
    """The worker's tick and jobs loops run side by side: the hand-back waits for the tick's
    writes (one control lock), so none lands after the controller is off, and it resumes them."""
    import asyncio

    from climate import worker as wmod
    from climate.store.orm import Job

    class Slow(ReadingSource):
        def __init__(self) -> None:
            super().__init__(NIGHT + timedelta(seconds=30))
            self.log: list[tuple[str, str, str, list[str]]] = []  # (call, unit, mode when it landed, sensors)

        async def set_hold(self, req):
            await asyncio.sleep(0.1)
            self.log.append(("set_hold", req.unit_key, mode_now(), []))
            return await super().set_hold(req)

        async def update_sensor_sets(self, unit_key, sets, reason):
            await asyncio.sleep(0.1)
            self.log.append(("update_sensor_sets", unit_key, mode_now(), list(sets["home"])))
            return await super().update_sensor_sets(unit_key, sets, reason)

    _ecobee_house(db, NIGHT)
    monkeypatch.setattr(wmod, "utcnow", lambda: NIGHT)
    monkeypatch.setattr(controller, "utcnow", lambda: NIGHT + timedelta(seconds=10))
    inner = Slow()
    w = wmod.Worker()
    w.source, w.source_kind = wmod.LockedSource(inner), "ecobee"
    tick = asyncio.create_task(w.tick_step())
    await asyncio.sleep(0.05)  # the jobs loop wakes while the tick's first write is in flight
    with session_scope() as s:
        s.add(Job(kind="handback", status="queued", requested_by="owner", params={}))
    (job_id,) = await w.process_jobs(now=NIGHT + timedelta(seconds=30))
    await tick
    holds = [c for c in inner.log if c[0] == "set_hold"]
    night_sets = [c for c in inner.log if c[0] == "update_sensor_sets" and c[3] != DAY_SETS[c[1]]]
    assert len(holds) == 3 and len(night_sets) == 2
    assert all(mode == "act" for _, _, mode, _ in holds + night_sets)  # every tick write landed before "off"
    assert sorted(inner.resumes) == ["bed", "main", "up"]
    restored = [(u, mode) for call, u, mode, sets in inner.log if call == "update_sensor_sets" and sets == DAY_SETS[u]]
    assert restored == [("main", "off"), ("up", "off")]  # the night sets undone by the hand-back
    with session_scope() as s:
        assert s.get(Job, job_id).status == "done"


async def test_a_write_decided_before_the_controller_was_switched_off_is_not_sent(db):
    """controller.tick reads the mode again before each write: once it is off (a hand-back
    started), the remaining decided writes fail as not sent."""

    class SwitchesOff(FakeSource):
        async def set_hold(self, req):
            with session_scope() as s:
                put_setting(s, "control", ControlSettings(mode="off"))
            return await super().set_hold(req)

    setup_house(db)
    src = SwitchesOff()
    await controller.tick(src, NOW)
    assert len(src.holds) == 1  # the first write was already on its way
    rows = actions(db, action="set_hold")
    assert [r.status for r in rows] == ["verified", "failed", "failed"]
    assert {r.error for r in rows[1:]} == {"Not sent: the controller was switched off."}


async def test_hand_back_drops_queued_controller_writes(db):
    house_to_hand_back(db)
    db.add(ControlAction(ts=NOW, unit_key="bed", actor="controller", mode="act", channel="homekit",
                         action="set_hold", status="queued", rule="comfort", reason="through HomeKit",
                         request={"kind": "climate_hold", "climate": "home", "until": (NOW + timedelta(hours=2)).isoformat()}))
    db.commit()
    steps = await controller.hand_back(HandbackSource(), NOW)
    assert steps[0]["detail"] == "Was act; it writes nothing until you switch it back on. 1 queued controller write(s) were dropped."
    (row,) = actions(db, channel="homekit")
    assert (row.status, row.error) == ("failed", "Not sent: handed back to ecobee.")


async def test_our_homekit_hold_seen_on_the_cloud_is_left_to_end_on_its_own(db):
    setup_house(db, mode="act")
    until = NOW + timedelta(minutes=90)
    db.add(ControlAction(ts=NOW - timedelta(minutes=30), completed_at=NOW - timedelta(minutes=29), unit_key="up",
                         actor="controller", mode="act", channel="homekit", action="set_hold", status="verified",
                         rule="comfort", reason="through HomeKit",
                         request={"kind": "climate_hold", "climate": "home", "until": until.isoformat(), "unit_key": "up"}))
    put_ecobee_snapshot(db, "up", NOW - timedelta(minutes=1),
                        hold=HoldInfo(kind="climate", climate_ref="home", start=NOW - timedelta(minutes=30), end=until,
                                      hold_type="dateTime"))
    db.commit()
    src = HandbackSource()
    steps = await controller.hand_back(src, NOW)
    by = {(st["unit_key"], st["what"]): st for st in steps}
    assert by[("up", "Resumed our hold")] == {
        "unit_key": "up", "what": "Resumed our hold", "ok": True,
        "detail": "Left to end on its own at 3:30 PM: the controller's HomeKit hold, which the ecobee cloud does not "
                  "let it cancel."}
    assert src.resumes == []


async def test_a_sensor_restore_refused_under_a_persons_hold_is_not_done_yet(db):
    house_to_hand_back(db)

    class Refuses(HandbackSource):
        async def update_sensor_sets(self, unit_key, sets, reason):
            self.set_writes.append((unit_key, sets))
            return WriteResult(ok=False, channel="ecobee", request={"unit_key": unit_key, "refused": True, "not_ours": True},
                               error="the running hold was not set by the controller; not written")

    steps = await controller.hand_back(Refuses(), NOW)
    by = {(st["unit_key"], st["what"]): st for st in steps}
    main = by[("main", "Home sensors restored")]
    assert main["ok"] and main["detail"].startswith("Skipped (main.hallway_tstat") and "a person's hold" in main["detail"]
    (row,) = actions(db, rule="handback", action="update_program")
    assert row.status == "skipped"
    (alert,) = db.execute(select(Alert).where(Alert.kind == "handback")).scalars().all()
    assert alert.title == "Hand-back incomplete" and "main: Home sensors" in alert.body
