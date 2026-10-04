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
    sim = FakeSource(kind="simulator")  # resumes, but cannot write sensor sets or settings
    steps = await controller.hand_back(sim, NOW)
    by = {(st["unit_key"], st["what"]): st for st in steps}
    assert by[("main", "Home sensors restored")]["detail"] == "Skipped: this thermostat source cannot write sensor sets."
    assert by[("main", "Smart Away and Follow Me restored")]["detail"].startswith(
        "Skipped: this thermostat source cannot write settings (Smart Away on, Follow Me on)")
    assert all(st["ok"] for st in steps) and sim.resumes == ["up"]

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
