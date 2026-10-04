"""Control-path safety regressions: manual changes the controller must respect (Resume at the
thermostat, refused resumes, Quick Save), ecobee's own events (Smart Away may be overridden,
vacation / demand response never), our own write whose read-back failed, the HomeKit fallback,
owner-queued actions that expire, and the Home sensor set following the block."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text, update
from sqlalchemy.dialects.postgresql import insert

from climate.control import controller
from climate.sources.base import HoldInfo, UnitSnapshot, WriteResult
from climate.state import MANUAL_KIND, RESUME_KIND, load_house_state
from climate.store.app_settings import SourceSettings, put_setting
from climate.store.orm import ControlAction, LiveUnit
from tests.test_controller_tick import (
    NOW,
    TZ,
    FakeSource,
    actions,
    put_snapshot,
    refresh_sensors,
    setup_house,
)


class SetsSource(FakeSource):
    """A fake cloud source that can also write sensor sets (the simulator cannot)."""

    def __init__(self, ok: bool = True) -> None:
        super().__init__(kind="ecobee")
        self.set_writes: list[tuple[str, dict[str, list[str]]]] = []
        self.sets_ok = ok

    async def update_sensor_sets(self, unit_key: str, sets: dict[str, list[str]], reason: str) -> WriteResult:
        self.set_writes.append((unit_key, {k: list(v) for k, v in sets.items()}))
        return WriteResult(ok=self.sets_ok, channel="ecobee", request={"unit_key": unit_key, "sets": sets},
                           readback={"sets": sets} if self.sets_ok else None,
                           error=None if self.sets_ok else "sensor set read-back differs for home")


def our_hold(db, unit_key: str, ts: datetime, heat: float, cool: float, *, status: str = "verified",
             completed: datetime | None = None, channel: str = "simulator", hours: int = 2) -> ControlAction:
    row = ControlAction(ts=ts, completed_at=completed if completed is not None else (ts if status != "sent" else None),
                        unit_key=unit_key, actor="controller", mode="act", channel=channel, action="set_hold",
                        status=status, reason="earlier", request={"unit_key": unit_key, "heat_f": heat, "cool_f": cool,
                                                                  "hours": hours})
    db.add(row)
    db.flush()
    return row


def put_homekit_snapshot(db, unit_key: str, ts: datetime, heat: float = 67.0, cool: float = 78.0) -> None:
    snap = UnitSnapshot(unit_key=unit_key, ts=ts, source="homekit", hvac_mode="cool", heat_sp_f=heat, cool_sp_f=cool,
                        zone_temp_f=76.0, zone_humidity=45.0)
    data = snap.model_dump(mode="json")
    db.execute(insert(LiveUnit).values(unit_key=unit_key, ts=ts, source="homekit", snapshot=data)
               .on_conflict_do_update(index_elements=[LiveUnit.unit_key],
                                      set_={"ts": ts, "source": "homekit", "snapshot": data}))


def circuit_open(db, open_: bool = True) -> None:
    put_setting(db, "source", SourceSettings(kind="ecobee", homekit_enabled=True,
                                             cloud_circuit_open_until=NOW + timedelta(hours=3) if open_ else None))
    db.commit()


# ---------------------------------------------------------------------------------------
# 1. someone cancels our hold at the thermostat
# ---------------------------------------------------------------------------------------


async def test_resume_at_thermostat_backs_off_instead_of_rewriting(db):
    setup_house(db, act_units=["up"])
    our_hold(db, "up", NOW - timedelta(minutes=60), 68.0, 77.0)  # ends at NOW + 60 min
    put_snapshot(db, "up", NOW - timedelta(minutes=1), heat=67.0, cool=78.0, hold=None)  # Resume pressed
    db.commit()
    src = FakeSource()
    await controller.tick(src, NOW)
    assert src.holds == []
    (row,) = actions(db, unit_key="up", status="skipped")
    assert row.reason == "Someone resumed the schedule at the upstairs thermostat; backing off until 6:00 PM."
    assert row.request["kind"] == RESUME_KIND and row.rule == "hold_off"
    assert load_house_state(db, NOW + timedelta(minutes=1)).units["up"].manual_override_until == NOW + timedelta(hours=4)

    later = NOW + timedelta(minutes=6)  # logged once, the back-off keeps its first-seen start
    put_snapshot(db, "up", later - timedelta(minutes=1), heat=67.0, cool=78.0)
    refresh_sensors(db, later)
    await controller.tick(src, later)
    assert src.holds == [] and len(actions(db, unit_key="up", status="skipped")) == 1
    assert load_house_state(db, later).units["up"].manual_override_until == NOW + timedelta(hours=4)

    after = NOW + timedelta(hours=4, minutes=5)  # back-off over (and our old hold long gone)
    put_snapshot(db, "up", after - timedelta(minutes=1), heat=67.0, cool=78.0)
    refresh_sensors(db, after)
    await controller.tick(src, after)
    assert [(h.heat_f, h.cool_f) for h in src.holds] == [(68.0, 77.0)]


async def test_hold_that_ran_out_is_not_a_manual_change(db):
    setup_house(db, act_units=["up"])
    our_hold(db, "up", NOW - timedelta(minutes=125), 68.0, 77.0)  # ended 5 minutes ago
    db.commit()
    src = FakeSource()
    await controller.tick(src, NOW)
    assert actions(db, status="skipped") == [] and len(src.holds) == 1


def test_resume_detection_needs_a_snapshot_newer_than_the_write(db):
    setup_house(db, act_units=["up"])
    our_hold(db, "up", NOW - timedelta(minutes=2), 68.0, 77.0)
    put_snapshot(db, "up", NOW - timedelta(minutes=3), heat=67.0, cool=78.0)  # taken before our write
    our_hold(db, "main", NOW - timedelta(minutes=10), 67.0, 76.0, status="sent")  # in flight / crashed
    put_snapshot(db, "main", NOW - timedelta(minutes=8))  # only 2 min after a write that never finished
    put_homekit_snapshot(db, "bed", NOW - timedelta(minutes=1))  # HomeKit shows no ecobee holds
    our_hold(db, "bed", NOW - timedelta(minutes=30), 68.0, 76.0)
    db.commit()
    units = load_house_state(db, NOW).units
    assert all(units[k].manual_override_until is None for k in ("up", "main", "bed"))


# ---------------------------------------------------------------------------------------
# 8. a resume the source refuses (the hold is not ours)
# ---------------------------------------------------------------------------------------


async def test_refused_resume_is_skipped_and_starts_the_backoff(db):
    setup_house(db, act_units=["up"])
    our_hold(db, "up", NOW - timedelta(hours=1), 67.0, 76.0)
    put_snapshot(db, "up", NOW - timedelta(minutes=1), heat=67.0, cool=76.0,
                 hold=HoldInfo(heat_f=67.0, cool_f=76.0, end=NOW + timedelta(hours=1)),
                 settings={"program_heat_f": 68.0, "program_cool_f": 77.0})
    db.commit()
    src = FakeSource()
    src.refuse_resume = True
    await controller.tick(src, NOW)
    assert src.resumes == ["up"] and src.forced == [False]  # the controller never forces
    (row,) = actions(db, unit_key="up", action="resume_program")
    assert row.status == "skipped" and row.rule == "hold_off"
    assert "not written by the controller" in row.reason and "backing off until 6:00 PM" in row.reason
    assert row.request["kind"] == MANUAL_KIND and row.request["refused"] is True
    assert (row.request["heat_f"], row.request["cool_f"]) == (67.0, 76.0)
    state = load_house_state(db, NOW + timedelta(minutes=1))
    assert state.units["up"].manual_override_until == NOW + timedelta(hours=4)
    assert state.units["up"].snapshot.hold.set_by_us is False

    later = NOW + timedelta(minutes=5)
    put_snapshot(db, "up", later - timedelta(minutes=1), heat=67.0, cool=76.0,
                 hold=HoldInfo(heat_f=67.0, cool_f=76.0, end=NOW + timedelta(hours=1)),
                 settings={"program_heat_f": 68.0, "program_cool_f": 77.0})
    refresh_sensors(db, later)
    await controller.tick(src, later)
    assert src.resumes == ["up"] and src.holds == []  # not retried, nothing written over it
    assert len(actions(db, unit_key="up", status="skipped")) == 1


# ---------------------------------------------------------------------------------------
# 2. ecobee's own events
# ---------------------------------------------------------------------------------------


async def test_smart_away_is_not_a_manual_change_and_is_overridden(db):
    setup_house(db, act_units=["main"], occupied=("toy_room",))
    put_snapshot(db, "main", NOW - timedelta(minutes=1), heat=62.0, cool=80.0,
                 hold=HoldInfo(kind="climate", climate_ref="away", hold_type="autoAway",
                               start=NOW - timedelta(minutes=5)))
    db.commit()
    assert load_house_state(db, NOW).units["main"].manual_override_until is None
    src = FakeSource()
    await controller.tick(src, NOW)
    assert actions(db, status="skipped") == []
    assert [h.unit_key for h in src.holds] == ["main"]  # linked floors takes the main floor back


async def test_smart_away_with_the_schedule_wanted_writes_a_hold_not_a_resume(db):
    setup_house(db, act_units=["up"])
    put_snapshot(db, "up", NOW - timedelta(minutes=1), heat=62.0, cool=82.0,
                 hold=HoldInfo(kind="climate", climate_ref="away", heat_f=62.0, cool_f=82.0, hold_type="autoAway"),
                 settings={"program_heat_f": 68.0, "program_cool_f": 77.0})
    db.commit()
    src = FakeSource()
    await controller.tick(src, NOW)
    assert src.resumes == [] and [(h.heat_f, h.cool_f) for h in src.holds] == [(64.0, 80.0)]  # stepping
    row = actions(db, unit_key="up", status="verified")[0]
    assert "autoAway" in row.reason


@pytest.mark.parametrize(("event", "words"), [("vacation", "vacation event"),
                                              ("demandResponse", "demand-response event")])
async def test_vacation_and_demand_response_are_never_overridden(db, event, words):
    setup_house(db, act_units=["up"])
    put_snapshot(db, "up", NOW - timedelta(minutes=1), heat=60.0, cool=84.0,
                 hold=HoldInfo(heat_f=60.0, cool_f=84.0, hold_type=event, start=NOW - timedelta(hours=1)))
    db.add(ControlAction(ts=NOW - timedelta(minutes=1), unit_key="up", actor="owner", mode="act", channel="simulator",
                         action="set_hold", status="queued", rule="manual", reason="Owner hold",
                         request={"unit_key": "up", "heat_f": 68.0, "cool_f": 77.0, "hours": 2}))
    db.commit()
    assert load_house_state(db, NOW).units["up"].manual_override_until is None
    by = {r.target.unit_key: r for r in controller.current_plan(db, NOW)}
    assert words in by["up"].guard.blocked_reason and not by["up"].would_write
    src = FakeSource()
    await controller.tick(src, NOW)
    await controller.execute_queued(src, NOW)
    assert src.holds == [] and src.resumes == []
    assert actions(db, status="skipped") == []
    owner = actions(db, actor="owner")[0]
    assert owner.status == "failed" and words in owner.error


async def test_quick_save_is_a_person(db):
    setup_house(db, act_units=["up"])
    put_snapshot(db, "up", NOW - timedelta(minutes=1), heat=66.0, cool=80.0,
                 hold=HoldInfo(heat_f=66.0, cool_f=80.0, hold_type="quickSave"))
    db.commit()
    src = FakeSource()
    await controller.tick(src, NOW)
    assert src.holds == []
    (row,) = actions(db, unit_key="up", status="skipped")
    assert "by hand" in row.reason


# ---------------------------------------------------------------------------------------
# 6. our own write whose read-back failed
# ---------------------------------------------------------------------------------------


async def test_failed_readback_that_landed_is_ours_not_manual(db):
    setup_house(db, act_units=["up"])
    our_hold(db, "up", NOW - timedelta(minutes=3), 68.0, 77.0, status="failed", channel="ecobee")
    put_snapshot(db, "up", NOW - timedelta(minutes=1), heat=68.0, cool=77.0,
                 hold=HoldInfo(heat_f=68.0, cool_f=77.0, start=NOW - timedelta(minutes=3),
                               end=NOW + timedelta(minutes=117)))
    db.commit()
    state = load_house_state(db, NOW)
    assert state.units["up"].manual_override_until is None and state.units["up"].snapshot.hold.set_by_us
    src = FakeSource()
    await controller.tick(src, NOW)
    assert actions(db, status="skipped") == [] and src.holds == []


async def test_same_setpoints_but_a_permanent_hold_is_still_manual(db):
    setup_house(db, act_units=["up"])
    our_hold(db, "up", NOW - timedelta(minutes=3), 68.0, 77.0, status="failed", channel="ecobee")
    put_snapshot(db, "up", NOW - timedelta(minutes=1), heat=68.0, cool=77.0,
                 hold=HoldInfo(heat_f=68.0, cool_f=77.0, hold_type="indefinite"))
    db.commit()
    await controller.tick(FakeSource(), NOW)
    (row,) = actions(db, unit_key="up", status="skipped")
    assert "by hand" in row.reason


# ---------------------------------------------------------------------------------------
# 3. HomeKit fallback
# ---------------------------------------------------------------------------------------


async def test_failed_homekit_hold_is_not_retried_for_30_minutes(db):
    setup_house(db, act_units=["up"])
    circuit_open(db)
    src = FakeSource(kind="ecobee")
    await controller.tick(src, NOW)
    (row,) = actions(db, channel="homekit")
    db.execute(update(ControlAction).where(ControlAction.id == row.id).values(status="failed", completed_at=NOW))
    db.commit()
    for minutes in (3, 6, 27):
        t = NOW + timedelta(minutes=minutes)
        for u in ("main", "up", "bed"):
            put_snapshot(db, u, t - timedelta(minutes=1))
        refresh_sensors(db, t)
        await controller.tick(src, t)
        assert len(actions(db, channel="homekit")) == 1
    t = NOW + timedelta(minutes=31)
    for u in ("main", "up", "bed"):
        put_snapshot(db, u, t - timedelta(minutes=1))
    refresh_sensors(db, t)
    await controller.tick(src, t)
    assert len(actions(db, channel="homekit")) == 2


async def test_homekit_resume_queues_clear_hold(db):
    setup_house(db, act_units=["up"])
    our_hold(db, "up", NOW - timedelta(hours=1), 67.0, 76.0, channel="ecobee")
    put_snapshot(db, "up", NOW - timedelta(minutes=1), heat=67.0, cool=76.0,
                 hold=HoldInfo(heat_f=67.0, cool_f=76.0, end=NOW + timedelta(hours=1)),
                 settings={"program_heat_f": 68.0, "program_cool_f": 77.0})
    circuit_open(db)
    src = FakeSource(kind="ecobee")
    await controller.tick(src, NOW)
    (row,) = actions(db, channel="homekit")
    assert row.action == "resume_program" and row.request == {"kind": "clear_hold", "unit_key": "up"}
    assert src.resumes == []


@pytest.mark.parametrize("age", [timedelta(minutes=16), timedelta(days=2)])
async def test_stuck_homekit_row_stops_blocking_after_15_minutes(db, age):
    setup_house(db, act_units=["up"])
    control = load_house_state(db, NOW).control
    control.limits.min_minutes_between_changes = 10  # so only the pending window can block
    put_setting(db, "control", control)
    db.add(ControlAction(ts=NOW - age, unit_key="up", actor="controller", mode="act",
                         channel="homekit", action="resume_program", status="sent", reason="claimed, never finished",
                         request={"kind": "clear_hold", "unit_key": "up"}))
    db.commit()
    circuit_open(db)
    await controller.tick(FakeSource(kind="ecobee"), NOW)
    assert [r.status for r in actions(db, channel="homekit")] == ["sent", "queued"]


async def test_recent_homekit_row_still_blocks(db):
    setup_house(db, act_units=["up"])
    control = load_house_state(db, NOW).control
    control.limits.min_minutes_between_changes = 10
    put_setting(db, "control", control)
    db.add(ControlAction(ts=NOW - timedelta(minutes=12), unit_key="up", actor="controller", mode="act",
                         channel="homekit", action="set_hold", status="sent", reason="being written",
                         request={"kind": "climate_hold", "climate": "home"}))
    db.commit()
    circuit_open(db)
    await controller.tick(FakeSource(kind="ecobee"), NOW)
    assert [r.status for r in actions(db, channel="homekit")] == ["sent"]


async def test_homekit_snapshot_is_used_while_the_circuit_is_open(db):
    setup_house(db, act_units=["up"])
    for u in ("main", "bed"):
        put_snapshot(db, u, NOW - timedelta(minutes=20))  # last cloud data
    put_homekit_snapshot(db, "up", NOW - timedelta(minutes=1))  # HomeKit keeps the unit fresh
    db.commit()
    assert load_house_state(db, NOW).units["up"].age_s == 60
    circuit_open(db)
    src = FakeSource(kind="ecobee")
    await controller.tick(src, NOW)
    (row,) = actions(db, channel="homekit")
    assert row.unit_key == "up" and row.status == "queued" and row.request["kind"] == "climate_hold"

    # with the cloud circuit closed, HomeKit-only data never drives a cloud write
    db.execute(text("DELETE FROM control_actions"))
    db.commit()
    circuit_open(db, open_=False)
    put_homekit_snapshot(db, "up", NOW + timedelta(minutes=2))
    refresh_sensors(db, NOW + timedelta(minutes=3))
    await controller.tick(src, NOW + timedelta(minutes=3))
    assert src.holds == [] and actions(db, unit_key="up", status="sent") == []
    plan = {r.target.unit_key: r for r in controller.current_plan(db, NOW + timedelta(minutes=3))}
    assert "HomeKit" in plan["up"].guard.blocked_reason


def test_queued_rows_count_toward_the_rate_limit_but_program_writes_do_not(db):
    setup_house(db)
    db.add_all([
        ControlAction(ts=NOW - timedelta(minutes=4), unit_key="up", actor="controller", mode="act",
                      channel="homekit", action="set_hold", status="queued", reason="on its way",
                      request={"kind": "climate_hold", "climate": "home"}),
        ControlAction(ts=NOW - timedelta(minutes=2), unit_key="main", actor="controller", mode="act",
                      channel="ecobee", action="update_program", status="verified", reason="sensor set",
                      request={"kind": "sensor_sets", "sets": {"home": ["main.hallway_tstat"]}}),
    ])
    db.commit()
    units = load_house_state(db, NOW).units
    assert units["up"].last_change_at == NOW - timedelta(minutes=4)
    assert units["main"].last_change_at is None


# ---------------------------------------------------------------------------------------
# 5. owner-queued actions
# ---------------------------------------------------------------------------------------


async def test_owner_action_expires_after_10_minutes(db):
    setup_house(db, mode="suggest")
    db.add(ControlAction(ts=NOW - timedelta(minutes=11), unit_key="up", actor="owner", mode="act", channel="simulator",
                         action="set_hold", status="queued", rule="manual", reason="Owner hold",
                         request={"unit_key": "up", "heat_f": 68.0, "cool_f": 77.0, "hours": 2}))
    db.commit()
    src = FakeSource()
    await controller.execute_queued(src, NOW)
    row = actions(db, actor="owner")[0]
    assert row.status == "failed" and "expired before the worker could run it" in row.error
    assert src.holds == []


async def test_owner_actions_fail_while_the_controller_is_off(db):
    setup_house(db, mode="off")
    db.add(ControlAction(ts=NOW - timedelta(minutes=1), unit_key="up", actor="owner", mode="act", channel="simulator",
                         action="set_hold", status="queued", rule="manual", reason="Owner hold",
                         request={"unit_key": "up", "heat_f": 68.0, "cool_f": 77.0, "hours": 2}))
    db.commit()
    src = FakeSource()
    await controller.execute_queued(src, NOW)
    row = actions(db, actor="owner")[0]
    assert row.status == "failed" and "controller is off" in row.error and src.holds == []


async def test_owner_resume_forces_and_owner_hold_obeys_the_hard_limits(db):
    setup_house(db, mode="suggest")
    put_snapshot(db, "up", NOW - timedelta(minutes=1), heat=50.0, cool=78.0)  # far below the 60°F minimum
    db.add_all([
        ControlAction(ts=NOW - timedelta(minutes=1), unit_key="up", actor="owner", mode="act", channel="simulator",
                      action="set_hold", status="queued", rule="manual", reason="Owner hold",
                      request={"unit_key": "up", "heat_f": 68.0, "cool_f": 78.0, "hours": 1}),
        ControlAction(ts=NOW - timedelta(minutes=1), unit_key="bed", actor="owner", mode="act", channel="simulator",
                      action="resume_program", status="queued", rule="manual", reason="Owner resume",
                      request={"unit_key": "bed"}),
    ])
    db.commit()
    src = FakeSource()
    await controller.execute_queued(src, NOW)
    assert [h.heat_f for h in src.holds] == [60.0]  # the hard minimum, not the 52°F step
    assert src.resumes == ["bed"] and src.forced == [True]


# ---------------------------------------------------------------------------------------
# 2. sensor sets follow the block (act mode only)
# ---------------------------------------------------------------------------------------

NIGHT = datetime(2026, 7, 15, 21, 0, tzinfo=ZoneInfo(TZ)).astimezone(UTC)  # main + up in sleep windows
DAY_SETS = {
    "main": ["main.hallway_tstat", "main.school_room", "main.living_room", "main.kitchen"],
    "up": ["up.toy_room_tstat", "up.toy_room", "up.girls_room"],
    "bed": ["bed.bedroom_tstat", "bed.office"],
}


def snapshots_with_sets(db, ts: datetime, sets: dict[str, list[str]]) -> None:
    for u in ("main", "up", "bed"):
        put_snapshot(db, u, ts - timedelta(minutes=1), sensor_sets={"home": sets[u]} if u in sets else {})
    db.commit()


async def test_night_block_writes_the_night_sensor_sets_once(db):
    setup_house(db, NIGHT)
    snapshots_with_sets(db, NIGHT, DAY_SETS)
    src = SetsSource()
    await controller.tick(src, NIGHT)
    assert src.set_writes == [("main", {"home": ["main.hallway_tstat"]}), ("up", {"home": ["up.girls_room"]})]
    rows = actions(db, action="update_program")
    assert [(r.unit_key, r.status, r.channel, r.mode) for r in rows] == [("main", "verified", "ecobee", "act"),
                                                                        ("up", "verified", "ecobee", "act")]
    assert rows[0].readback == {"sets": {"home": ["main.hallway_tstat"]}} and "Night block" in rows[0].reason
    assert rows[1].request["sets"] == {"home": ["up.girls_room"]} and rows[1].before["sensor_sets"]["home"]
    assert len(src.holds) == 3  # program writes do not use up the hold rate limit

    t = NIGHT + timedelta(minutes=5)  # the snapshot still shows the old sets: at most one write per 30 min
    snapshots_with_sets(db, t, DAY_SETS)
    refresh_sensors(db, t)
    await controller.tick(src, t)
    assert len(src.set_writes) == 2
    t = NIGHT + timedelta(minutes=31)
    snapshots_with_sets(db, t, DAY_SETS)
    refresh_sensors(db, t)
    await controller.tick(src, t)
    assert len(src.set_writes) == 4


async def test_upstairs_night_set_adds_its_thermostat_while_the_girls_room_sensor_is_silent(db):
    setup_house(db, NIGHT, act_units=["up"])
    db.execute(text("UPDATE live_sensors SET online = false WHERE sensor_key = 'up.girls_room'"))
    snapshots_with_sets(db, NIGHT, DAY_SETS)
    src = SetsSource()
    await controller.tick(src, NIGHT)
    assert src.set_writes == [("up", {"home": ["up.girls_room", "up.toy_room_tstat"]})]


async def test_day_block_restores_all_sensors_and_skips_units_without_reported_sets(db):
    setup_house(db)
    snapshots_with_sets(db, NOW, {"main": ["main.hallway_tstat"]})  # up / bed report no sets
    src = SetsSource()
    await controller.tick(src, NOW)
    assert src.set_writes == [("main", {"home": DAY_SETS["main"]})]
    assert "Day block" in actions(db, action="update_program")[0].reason


async def test_sensor_sets_are_not_written_outside_act_mode_or_by_the_simulator(db):
    setup_house(db, NIGHT, mode="suggest")
    snapshots_with_sets(db, NIGHT, DAY_SETS)
    src = SetsSource()
    await controller.tick(src, NIGHT)
    assert src.set_writes == [] and actions(db, action="update_program") == []

    put_setting(db, "control", load_house_state(db, NIGHT).control.model_copy(update={"mode": "act"}))
    db.commit()
    sim = FakeSource()  # no update_sensor_sets: skipped silently
    await controller.tick(sim, NIGHT + timedelta(minutes=1))
    assert actions(db, action="update_program") == [] and len(sim.holds) == 3


async def test_failed_sensor_set_write_is_logged_failed(db):
    setup_house(db, NIGHT, act_units=["main"])
    snapshots_with_sets(db, NIGHT, DAY_SETS)
    src = SetsSource(ok=False)
    await controller.tick(src, NIGHT)
    (row,) = actions(db, action="update_program")
    assert row.status == "failed" and "read-back differs" in row.error
    assert len(src.holds) == 1  # a failed program write does not stop the hold
