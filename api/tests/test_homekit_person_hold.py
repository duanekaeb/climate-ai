"""A person's change seen over HomeKit while the ecobee cloud is down (spec: "HomeKit fallback").

While the cloud circuit is open and HomeKit writes the unit's live snapshot, the homekit
service logs ONE control_actions row (request.kind MANUAL_KIND, source 'homekit') when the
thermostat shows a comfort setting other than our running HomeKit hold's, or setpoints that
are not the comfort setting's own; it never executes a hold the controller queued before
such a change. ingest_live_readings and the alert helpers are replaced with no-ops (they
belong to other modules).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

pytest.importorskip("aiohomekit")

from aiohomekit.exceptions import AccessoryDisconnectedError
from sqlalchemy import select

from climate import notify
from climate.collector import homekit_service as svc_mod
from climate.collector import ingest
from climate.collector.homekit_service import HomekitService
from climate.sources import homekit as hk
from climate.sources.base import HoldInfo, ThermostatEvent, UnitSnapshot, UtilityInfo
from climate.state import MANUAL_KIND, load_house_state
from climate.store.app_settings import ControlSettings, SourceSettings, put_setting
from climate.store.db import session_scope
from climate.store.orm import ControlAction, LiveUnit, UtilityEvent
from tests.test_homekit_fakes import FakeController, FakeDevice
from tests.test_homekit_service import Clock, action, paired_and_ready

MODE = "VENDOR_ECOBEE_CURRENT_MODE"
HEAT = "TEMPERATURE_HEATING_THRESHOLD"
COOL = "TEMPERATURE_COOLING_THRESHOLD"
# °C for the fake's comfort targets: home 68/76 °F, sleep 67/74 °F, away 62/80 °F
HOME_C, SLEEP_C = (20.0, 24.4), (19.4, 23.3)


@pytest.fixture
def env(db, tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "ingest_live_readings", lambda *a, **k: None)
    monkeypatch.setattr(notify, "raise_alert", lambda *a, **k: 1)
    monkeypatch.setattr(notify, "resolve_alert", lambda *a, **k: None)
    clock = Clock()
    put_setting(db, "source", SourceSettings(kind="ecobee", homekit_enabled=True,
                                             cloud_circuit_open_until=clock() + timedelta(minutes=15)))
    put_setting(db, "control", ControlSettings(mode="act"))  # the controller queues HomeKit holds only in act
    db.commit()
    dev = FakeDevice()
    ctl = FakeController([dev])
    bridge = hk.HomekitBridge(tmp_path / "hk", controller_factory=lambda _p: ctl)
    service = HomekitService(bridge, clock=clock)
    put_live(db, clock() - timedelta(minutes=11))  # the cloud's snapshot went stale
    return {"db": db, "dev": dev, "ctl": ctl, "clock": clock, "bridge": bridge, "svc": service}


def put_live(db, ts, hold: HoldInfo | None = None, **kw) -> None:
    snap = UnitSnapshot(unit_key="main", ts=ts, source="ecobee", name="Hallway", hvac_mode="auto", heat_sp_f=68.0,
                        cool_sp_f=76.0, climate_ref="home", hold=hold, **kw)
    db.merge(LiveUnit(unit_key="main", ts=ts, source="ecobee", revision="1|2", snapshot=snap.model_dump(mode="json")))
    db.commit()


def hand_rows(db) -> list[ControlAction]:
    db.expire_all()
    return list(db.execute(
        select(ControlAction).where(ControlAction.actor == "homekit_service").order_by(ControlAction.id)
    ).scalars())


def queue(db, request: dict, *, actor: str = "controller", ts=None) -> int:
    row = ControlAction(unit_key="main", actor=actor, mode="act", channel="homekit",
                        action="resume_program" if request["kind"] in svc_mod.CLEAR_HOLD_KINDS else "set_hold",
                        status="queued", reason="test", request=request)
    if ts is not None:
        row.ts = ts
    db.add(row)
    db.commit()
    return row.id


def away_hold(clock) -> dict:
    return {"kind": "climate_hold", "climate": "away", "until": (clock() + timedelta(minutes=90)).isoformat()}


async def poll(env, seconds: float = 61) -> None:
    env["clock"].advance(seconds)
    await env["svc"].poll_due()


def pick(dev: FakeDevice, mode: int, heat_c: float, cool_c: float) -> None:
    """What the thermostat shows after someone changes it at the wall."""
    dev.set(1, MODE, mode)
    dev.set(1, HEAT, heat_c)
    dev.set(1, COOL, cool_c)


# --- the service, against the test database --------------------------------------------


async def test_comfort_setting_picked_by_hand_is_logged_once(env):
    db, dev = env["db"], env["dev"]
    await paired_and_ready(env)
    hold_id = queue(db, away_hold(env["clock"]))
    await env["svc"].execute_actions()
    assert action(db, hold_id).status == "verified"
    await poll(env)  # our own away hold, as written: not a person's change
    assert hand_rows(db) == []
    assert db.get(LiveUnit, "main").source == "homekit"

    pick(dev, 0, *HOME_C)  # someone picks Home at the thermostat
    await poll(env)
    (row,) = hand_rows(db)
    assert (row.mode, row.channel, row.action, row.status, row.rule) == ("act", "homekit", "set_hold", "skipped",
                                                                          "hold_off")
    assert row.completed_at == env["clock"]()
    assert row.reason == (
        "Someone picked Home by hand on the main floor thermostat (seen over HomeKit while the ecobee cloud is "
        "down); the controller stands aside until the cloud is back."
    )
    assert row.request == {
        "kind": MANUAL_KIND, "source": "homekit", "climate_ref": "home", "heat_f": 68.0, "cool_f": 76.0,
        "hold_type": "homekit_manual", "start": env["clock"]().isoformat(), "until": None,
    }
    assert row.before["current_mode"] == 0 and row.before["comfort_targets"]["away"] == {"heat_f": 62.0,
                                                                                          "cool_f": 80.0}

    for _ in range(3):  # the same change, still showing: no duplicate
        await poll(env)
    assert len(hand_rows(db)) == 1

    pick(dev, 1, *SLEEP_C)  # a different change: a new row
    await poll(env)
    rows = hand_rows(db)
    assert [r.request["climate_ref"] for r in rows] == ["home", "sleep"]
    assert "picked Sleep by hand" in rows[-1].reason


async def test_our_own_writes_and_the_schedule_are_never_a_person(env):
    db, dev = env["db"], env["dev"]
    await paired_and_ready(env)
    await poll(env)  # the schedule's Home, at Home's own targets
    for climate in ("sleep", "away", "home"):
        aid = queue(db, {**away_hold(env["clock"]), "climate": climate})
        await env["svc"].execute_actions()
        assert action(db, aid).status == "verified"
        await poll(env)
    aid = queue(db, {"kind": "clear_hold"})  # and our resume
    await env["svc"].execute_actions()
    assert action(db, aid).status == "verified"
    await poll(env)
    assert dev.get(1, MODE) == 0
    assert hand_rows(db) == []


async def test_a_failed_write_that_went_out_may_have_landed_and_unsent_ones_never_did(env):
    db, dev, clock = env["db"], env["dev"], env["clock"]
    await paired_and_ready(env)
    aid = queue(db, away_hold(clock))
    await env["svc"].execute_actions()
    assert action(db, aid).status == "verified"
    # a newer Home hold whose read-back disagreed (it may have landed) ...
    db.add(ControlAction(unit_key="main", actor="controller", mode="act", channel="homekit", action="set_hold",
                         status="failed", reason="test", request={**away_hold(clock), "climate": "home"},
                         readback={"current_mode": 3}, error="current mode reads 3"))
    # ... then many rows that never reached the thermostat (expired, refused, not sent)
    for _ in range(25):
        db.add(ControlAction(unit_key="main", actor="controller", mode="act", channel="homekit",
                             action="set_hold", status="failed", reason="test", request=away_hold(clock),
                             readback=None, error="expired"))
    db.commit()
    pick(dev, 0, *HOME_C)  # Home: possibly our own failed write, not a person
    await poll(env)
    assert hand_rows(db) == []
    pick(dev, 1, *SLEEP_C)  # Sleep: neither our running away hold nor the home attempt
    await poll(env)
    assert [r.request["climate_ref"] for r in hand_rows(db)] == ["sleep"]


@pytest.mark.parametrize(("mode", "heat_c", "cool_c", "heat_f", "cool_f"), [
    (0, 21.1, 24.4, 70.0, 76.0),  # Home still shows, but someone set 70°F heat
    (3, 21.1, 25.0, 70.0, 77.0),  # a temperature hold at setpoints no comfort setting holds
])
async def test_temperature_set_by_hand_is_logged(env, mode, heat_c, cool_c, heat_f, cool_f):
    db, dev = env["db"], env["dev"]
    await paired_and_ready(env)
    pick(dev, mode, heat_c, cool_c)
    await poll(env)
    (row,) = hand_rows(db)
    assert row.request["climate_ref"] is None
    assert (row.request["heat_f"], row.request["cool_f"]) == (heat_f, cool_f)
    assert row.reason.startswith(
        f"Someone set a temperature hold (heat {heat_f:g}°F, cool {cool_f:g}°F) by hand on the main floor thermostat")
    await poll(env)
    assert len(hand_rows(db)) == 1


async def test_small_differences_and_unreadable_targets_are_not_a_change(env):
    db, dev = env["db"], env["dev"]
    await paired_and_ready(env)
    pick(dev, 0, 20.2, 24.4)  # 68.5°F: within 0.5°F of Home's 68°F
    await poll(env)
    pick(dev, 0, 21.1, 24.4)  # 70°F, but Home's heat target does not answer
    env["ctl"].aliases["hallway"].missing = {dev.k(1, "E4489BBC-5227-4569-93E5-B345E3E5508F")}
    await poll(env)
    assert hand_rows(db) == []


async def test_nothing_is_logged_unless_the_cloud_circuit_is_open_and_homekit_is_live(env):
    db, dev, clock = env["db"], env["dev"], env["clock"]
    await paired_and_ready(env)
    pick(dev, 3, 21.1, 25.0)
    put_setting(db, "source", SourceSettings(kind="ecobee", homekit_enabled=True))  # circuit closed
    db.commit()
    await poll(env)
    assert db.get(LiveUnit, "main").source == "homekit" and hand_rows(db) == []
    put_setting(db, "source", SourceSettings(kind="ecobee", homekit_enabled=True,
                                             cloud_circuit_open_until=clock() + timedelta(hours=1)))
    put_live(db, clock() + timedelta(seconds=30))  # circuit open, but the cloud's snapshot is current
    await poll(env)
    assert hand_rows(db) == []
    put_live(db, clock() - timedelta(minutes=20))  # now stale: HomeKit is the live path
    await poll(env)
    assert len(hand_rows(db)) == 1


async def test_a_new_cloud_outage_logs_a_still_showing_change_again(env):
    db, dev, clock = env["db"], env["dev"], env["clock"]
    await paired_and_ready(env)
    pick(dev, 3, 21.1, 25.0)
    await poll(env)
    await poll(env)
    assert len(hand_rows(db)) == 1
    put_live(db, clock() - timedelta(minutes=11))  # the cloud came back in between (no hold seen)
    await poll(env)
    assert len(hand_rows(db)) == 2


async def test_known_holds_and_events_are_not_a_person(env):
    db, dev, clock = env["db"], env["dev"], env["clock"]
    await paired_and_ready(env)
    # our cloud temperature hold from before the outage, still on the thermostat (mode 3)
    ours = HoldInfo(kind="temperature", heat_f=70.0, cool_f=77.0, start=clock() - timedelta(minutes=30),
                    end=clock() + timedelta(hours=1), hold_type="holdHours", set_by_us=True)
    put_live(db, clock() - timedelta(minutes=11), hold=ours)
    pick(dev, 3, 21.1, 25.0)
    await poll(env)
    await poll(env)
    assert hand_rows(db) == []
    # a utility event running by its clock window: its setpoints are not a person's
    put_live(db, clock() - timedelta(minutes=11))
    db.add(UtilityEvent(unit_key="main", event_key="link:abc", status="running", start_at=clock() - timedelta(hours=1),
                        end_at=clock() + timedelta(hours=2), first_seen_at=clock(), last_seen_at=clock(),
                        is_relative=True, cool_offset_f=2.0, detail={}))
    db.commit()
    pick(dev, 0, 20.0, 25.6)  # Home shows, cooling 2°F above Home's target
    await poll(env)
    assert hand_rows(db) == []


async def test_a_queued_hold_is_not_sent_after_a_change_by_hand(env):
    db, dev, clock = env["db"], env["dev"], env["clock"]
    await paired_and_ready(env)
    before = queue(db, away_hold(clock))  # the controller queued it ...
    before_resume = queue(db, {"kind": "clear_hold"})
    owner = queue(db, {"kind": "climate_hold", "climate": "sleep",
                       "until": (clock() + timedelta(hours=1)).isoformat()}, actor="owner")
    pick(dev, 3, 21.1, 25.0)  # ... then someone set a temperature at the thermostat
    await poll(env)
    (hand,) = hand_rows(db)
    await env["svc"].execute_actions()
    for aid in (before, before_resume):
        row = action(db, aid)
        assert row.status == "failed" and row.completed_at is not None
        assert row.error.startswith("Not sent: someone changed the thermostat by hand"), row.error
        assert f"action {hand.id}" in row.error
    assert action(db, owner).status == "verified"  # the owner's own action is not blocked
    hold_k = dev.k(1, "VENDOR_ECOBEE_SET_HOLD_SCHEDULE")
    assert [w for w in dev.writes if w[:2] == hold_k] == [(*hold_k, 1)]  # only the owner's sleep hold


async def test_a_hold_queued_after_the_change_is_the_controllers_decision(env):
    db, dev, clock = env["db"], env["dev"], env["clock"]
    await paired_and_ready(env)
    pick(dev, 3, 21.1, 25.0)
    await poll(env)
    assert len(hand_rows(db)) == 1
    later = queue(db, away_hold(clock), ts=clock() + timedelta(seconds=1))
    await env["svc"].execute_actions()
    assert action(db, later).status == "verified"
    # a tick that started BEFORE the change was logged (earlier ts, later id) is still blocked
    pick(dev, 3, 21.7, 25.0)
    await poll(env)
    raced = queue(db, away_hold(clock), ts=clock() - timedelta(seconds=1))
    await env["svc"].execute_actions()
    assert action(db, raced).status == "failed"


async def test_homekit_snapshots_never_carry_events_or_utility(env):
    db, clock = env["db"], env["clock"]
    event = ThermostatEvent(event_type="demandResponse", name="Peak", start=clock() + timedelta(hours=2),
                            end=clock() + timedelta(hours=4), is_relative=True, cool_offset_f=2.0)
    put_live(db, clock() - timedelta(minutes=11), events=[event], utility=UtilityInfo(name="Utility Co"))
    await paired_and_ready(env)
    await poll(env)
    db.expire_all()
    snap = UnitSnapshot.model_validate(db.get(LiveUnit, "main").snapshot)
    assert snap.source == "homekit" and snap.events == [] and snap.utility is None


# --- review findings: earlier outages, unverified writes, unpolled changes, events, owner ---


def _live_snapshot(db) -> UnitSnapshot:
    db.expire_all()
    return UnitSnapshot.model_validate(db.get(LiveUnit, "main").snapshot)


def _person_hold(db, clock):
    db.expire_all()
    return load_house_state(db, clock()).units["main"].person_hold


@pytest.mark.parametrize("circuit", ["open", "closed"])
async def test_an_earlier_outages_hand_change_never_comes_back(env, circuit):
    """Finding 7: a hand change logged in one outage is that outage's. Each HomeKit snapshot
    records when HomeKit took over (settings['homekit_since']); once the cloud has reported in
    between, a later HomeKit run starts afresh and the old row is not a person's hold again
    (with the cloud circuit open again, or merely a cloud snapshot more than 10 minutes old)."""
    db, dev, clock = env["db"], env["dev"], env["clock"]
    await paired_and_ready(env)
    hold_id = queue(db, away_hold(clock))
    await env["svc"].execute_actions()
    assert action(db, hold_id).status == "verified"
    await poll(env)
    started = _live_snapshot(db).settings["homekit_since"]  # this HomeKit run's first snapshot
    assert datetime.fromisoformat(started) <= clock()
    pick(dev, 0, *HOME_C)  # someone picks Home at the wall
    await poll(env)
    (row,) = hand_rows(db)
    assert _live_snapshot(db).settings["homekit_since"] == started  # the same run
    assert _person_hold(db, clock).detection_id == row.id

    # the cloud is back and shows no hold: that change has ended
    clock.advance(30 * 60)
    put_setting(db, "source", SourceSettings(kind="ecobee", homekit_enabled=True))
    put_live(db, clock())
    assert _person_hold(db, clock) is None

    # two days later the cloud's snapshot is stale again; nobody touched the thermostat
    clock.advance(2 * 24 * 3600)
    open_until = clock() + timedelta(minutes=15) if circuit == "open" else None
    put_setting(db, "source", SourceSettings(kind="ecobee", homekit_enabled=True, cloud_circuit_open_until=open_until))
    put_live(db, clock() - timedelta(minutes=11))
    await poll(env)
    snap = _live_snapshot(db)
    assert snap.source == "homekit" and snap.settings["homekit_since"] == clock().isoformat()
    assert len(hand_rows(db)) == 1  # nothing new was logged
    assert _person_hold(db, clock) is None


@pytest.mark.parametrize("on_put", [False, True], ids=["readback_get_failed", "put_answer_lost"])
async def test_our_hold_that_went_out_unverified_is_not_a_person(env, on_put):
    """Finding 19: our Sleep hold reached the thermostat but its read-back GET failed (or the
    put's answer was lost): it may have landed, so the Sleep the next poll shows is ours."""
    db, dev, clock = env["db"], env["dev"], env["clock"]
    await paired_and_ready(env)
    aid = queue(db, away_hold(clock))
    await env["svc"].execute_actions()
    assert action(db, aid).status == "verified"
    await poll(env)
    pairing = env["ctl"].aliases["hallway"]
    orig = pairing.put_characteristics
    hold_iid = dev.k(1, "VENDOR_ECOBEE_SET_HOLD_SCHEDULE")

    async def put_then_lose(rows):
        res = await orig(rows)
        if any((a, i) == hold_iid for a, i, _ in rows):
            if on_put:
                raise AccessoryDisconnectedError("connection lost")
            pairing.fail_next = [AccessoryDisconnectedError("connection lost")]
        return res

    pairing.put_characteristics = put_then_lose
    later = queue(db, {**away_hold(clock), "climate": "sleep"})
    await env["svc"].execute_actions()
    pairing.put_characteristics = orig
    row = action(db, later)
    assert row.status == "failed" and row.readback["sent"] is True and row.readback_ok is False
    assert dev.get(1, MODE) == 1  # Sleep is on the thermostat
    await poll(env)
    await poll(env)
    assert hand_rows(db) == []
    assert _person_hold(db, clock) is None


@pytest.mark.parametrize("lost", ["readback_get_failed", "readback_empty"])
async def test_our_resume_that_went_out_unverified_is_not_a_person(env, lost):
    """Finding 19: our resume reached the thermostat but was not verified; it is newer than
    our verified Away hold, so the schedule's Home the next poll shows may be its doing."""
    db, dev, clock = env["db"], env["dev"], env["clock"]
    await paired_and_ready(env)
    aid = queue(db, away_hold(clock))
    await env["svc"].execute_actions()
    assert action(db, aid).status == "verified"
    await poll(env)
    pairing = env["ctl"].aliases["hallway"]
    orig = pairing.put_characteristics
    clear_iid = dev.k(1, "VENDOR_ECOBEE_CLEAR_HOLD")

    async def put_then_lose(rows):
        res = await orig(rows)
        if any((a, i) == clear_iid for a, i, _ in rows):
            if lost == "readback_get_failed":
                pairing.fail_next = [AccessoryDisconnectedError("connection lost")]
            else:  # the read-back answers with statuses instead of values
                pairing.missing = {dev.k(1, "VENDOR_ECOBEE_TIMESTAMP"), dev.k(1, MODE)}
        return res

    pairing.put_characteristics = put_then_lose
    later = queue(db, {"kind": "clear_hold"})
    await env["svc"].execute_actions()
    pairing.put_characteristics, pairing.missing = orig, set()
    row = action(db, later)
    assert row.status == "failed" and isinstance(row.readback, dict)
    assert dev.get(1, MODE) == 0  # the schedule's Home
    await poll(env)
    assert hand_rows(db) == []
    assert _person_hold(db, clock) is None


async def test_our_verified_resume_after_our_away_hold_is_not_a_person(env):
    db, dev, clock = env["db"], env["dev"], env["clock"]
    await paired_and_ready(env)
    aid = queue(db, away_hold(clock))
    await env["svc"].execute_actions()
    await poll(env)
    rid = queue(db, {"kind": "clear_hold"})
    await env["svc"].execute_actions()
    assert action(db, aid).status == action(db, rid).status == "verified"
    await poll(env)
    assert (dev.get(1, MODE), dev.get(1, HEAT), dev.get(1, COOL)) == (0, *HOME_C)
    assert hand_rows(db) == []


@pytest.mark.parametrize("kind", ["sleep_hold", "renew_away", "clear_hold"])
async def test_a_change_since_the_last_poll_blocks_the_controllers_next_write(env, kind):
    """Finding 20: a person changed the thermostat 40 s after the last poll and the
    controller (which could not know) queued a write 5 s later. The service reads the
    thermostat right before claiming it: the change is logged first and the write is not
    sent, so the person's change stays."""
    db, dev, clock = env["db"], env["dev"], env["clock"]
    await paired_and_ready(env)
    aid = queue(db, away_hold(clock))
    await env["svc"].execute_actions()
    assert action(db, aid).status == "verified"
    await poll(env)
    clock.advance(40)
    pick(dev, 0, *HOME_C)
    clock.advance(5)
    req = {"sleep_hold": {**away_hold(clock), "climate": "sleep"}, "renew_away": away_hold(clock),
           "clear_hold": {"kind": "clear_hold"}}[kind]
    later = queue(db, req, ts=clock())
    writes = len(dev.writes)
    clock.advance(2)
    await env["svc"].step()  # a real service step: the 60 s poll is not due for another 14 s
    (hand,) = hand_rows(db)
    assert hand.request["climate_ref"] == "home"
    row = action(db, later)
    assert row.status == "failed" and row.error.startswith(svc_mod.NOT_SENT_MSG) and f"action {hand.id}" in row.error
    assert len(dev.writes) == writes and dev.get(1, MODE) == 0  # nothing written over the person's Home


async def test_an_owner_row_is_sent_without_waiting_for_a_fresh_read(env):
    db, clock = env["db"], env["clock"]
    await paired_and_ready(env)
    await poll(env)
    gets = sum(1 for kind, _ in env["ctl"].aliases["hallway"].calls if kind == "get")
    owner = queue(db, away_hold(clock), actor="owner")
    await env["svc"].execute_actions()
    assert action(db, owner).status == "verified"
    calls = env["ctl"].aliases["hallway"].calls
    # only the write's own reads: comfort targets, the GET before, the read-back
    assert sum(1 for kind, _ in calls if kind == "get") - gets == 3


async def test_an_unrecognised_ecobee_event_from_the_cloud_is_never_a_person(env):
    """Finding 21: the cloud's last snapshot showed an unrecognised event (relative, cooling
    2°F above Home's target, 3 more hours). Every HomeKit snapshot keeps it until its end, so
    no poll reads its setpoints as a person's change; after its end a real change is seen."""
    db, dev, clock = env["db"], env["dev"], env["clock"]
    await paired_and_ready(env)
    ev = HoldInfo(kind="temperature", hold_type="switchOccupancy", is_relative=True, cool_offset_f=2.0,
                  start=clock() - timedelta(hours=1), end=clock() + timedelta(hours=3))
    put_live(db, clock() - timedelta(minutes=11), hold=ev)
    pick(dev, 0, 20.0, 25.6)  # Home in force, cooling 2°F above Home's 76°F
    for _ in range(3):
        await poll(env)
        assert _live_snapshot(db).hold == ev
    assert hand_rows(db) == []
    clock.advance(3 * 3600)  # the event has ended; the cloud is still down
    put_setting(db, "source", SourceSettings(kind="ecobee", homekit_enabled=True,
                                             cloud_circuit_open_until=clock() + timedelta(minutes=15)))
    db.commit()
    await poll(env)
    assert _live_snapshot(db).hold is None
    assert [r.request["cool_f"] for r in hand_rows(db)] == [78.0]  # now a person's setpoint


@pytest.mark.parametrize("hold_type", ["switchOccupancy", "demandResponse"])
async def test_the_controller_stays_hands_off_an_event_the_cloud_reported(db, hold_type):
    """Finding 21: after the first HomeKit poll the controller still sees the running event
    (its own guard reads the snapshot's hold) and queues no HomeKit write over it."""
    from climate.control import controller
    from tests.test_controller_tick import NOW, FakeSource, refresh_sensors, setup_house

    setup_house(db, mode="act", act_units=["up"])
    t = NOW + timedelta(minutes=30)
    ev = HoldInfo(kind="temperature", hold_type=hold_type, is_relative=True, cool_offset_f=2.0,
                  start=t - timedelta(hours=1), end=t + timedelta(hours=3))
    cloud = UnitSnapshot(unit_key="up", ts=t - timedelta(minutes=11), source="ecobee", hvac_mode="cool",
                         heat_sp_f=60.0, cool_sp_f=85.0, climate_ref="home", hold=ev, zone_temp_f=76.0,
                         zone_humidity=45.0)
    db.merge(LiveUnit(unit_key="up", ts=cloud.ts, source="ecobee", revision="1|2",
                      snapshot=cloud.model_dump(mode="json")))
    put_setting(db, "source", SourceSettings(kind="ecobee", homekit_enabled=True,
                                             cloud_circuit_open_until=t + timedelta(minutes=15)))
    db.commit()
    values = {1: {MODE: 0, HEAT: 15.6, COOL: 29.4, "HEATING_COOLING_TARGET": 2, "TEMPERATURE_CURRENT": 24.4,
                  "RELATIVE_HUMIDITY_CURRENT": 45}}
    with session_scope() as s:
        assert svc_mod.write_homekit_snapshot(s, "up", values, {}, t)
    refresh_sensors(db, t)
    db.commit()
    assert UnitSnapshot.model_validate(db.get(LiveUnit, "up").snapshot).hold == ev
    await controller.tick(FakeSource(kind="ecobee"), t + timedelta(seconds=30))
    db.expire_all()
    queued = db.execute(select(ControlAction).where(ControlAction.unit_key == "up",
                                                    ControlAction.status == "queued")).scalars().all()
    assert queued == []


@pytest.mark.parametrize("kind", ["automatic", "resume_schedule"])
async def test_the_owners_resume_ends_a_hand_change_over_homekit(env, kind):
    """Finding 22: while the cloud is down the owner's Back to automatic / Resume schedule is
    queued on channel 'homekit' with its own kind: it runs as CLEAR_HOLD (never blocked by the
    hand change: the owner is the person), is verified, and ends that person's hold."""
    db, dev, clock = env["db"], env["dev"], env["clock"]
    await paired_and_ready(env)
    pick(dev, 3, 21.1, 25.0)  # someone set 70/77°F at the wall
    await poll(env)
    (hand,) = hand_rows(db)
    assert _person_hold(db, clock).detection_id == hand.id
    owner = queue(db, {"kind": kind, "unit_key": "main", "reason": "Owner: test"}, actor="owner", ts=clock())
    await env["svc"].execute_actions()
    row = action(db, owner)
    assert row.status == "verified", row.error
    assert row.request["kind"] == kind and row.action == "resume_program"  # the owner's kind is kept
    assert dev.writes[-1] == (*dev.k(1, "VENDOR_ECOBEE_CLEAR_HOLD"), True)
    assert _person_hold(db, clock) is None
    await poll(env)  # the schedule's Home now shows: not a new change
    assert len(hand_rows(db)) == 1 and _person_hold(db, clock) is None


async def test_the_owners_homekit_actions_fail_while_the_controller_is_off(env):
    db, dev = env["db"], env["dev"]
    await paired_and_ready(env)
    put_setting(db, "control", ControlSettings(mode="off"))
    db.commit()
    owner = queue(db, {"kind": "automatic", "unit_key": "main", "reason": "Owner: test"}, actor="owner")
    await env["svc"].execute_actions()
    row = action(db, owner)
    assert (row.status, row.error) == ("failed", "Not sent: the controller is off.")
    assert row.completed_at is not None and dev.writes == []


# --- the pure detector ----------------------------------------------------------------


def tstat(mode=0, heat=20.0, cool=24.4, hvac=3, **over) -> dict:
    vals = {MODE: mode, HEAT: heat, COOL: cool, "HEATING_COOLING_TARGET": hvac}
    for climate, (heat_t, cool_t) in {"home": HOME_C, "sleep": SLEEP_C, "away": (16.7, 26.7)}.items():
        heat_k, cool_k = hk.COMFORT_TARGET_KEYS[climate]
        vals[heat_k], vals[cool_k] = heat_t, cool_t
    vals.update(over)
    return {1: vals}


def detect(values, **known):
    return hk.detect_hand_change(values, hk.KnownHolds(**known))


def test_detector_rules():
    # the schedule (or a comfort setting picked by hand with no hold of ours): not visible
    assert detect(tstat(mode=2, heat=16.7, cool=26.7)) is None
    # (a) our away hold runs but Home shows / a temperature hold shows
    home = detect(tstat(), climate="away")
    assert home == hk.HandChange("comfort", "home", 68.0, 76.0, 0)
    assert detect(tstat(mode=3, heat=21.1), climate="away").kind == "temperature"
    # ... unless a newer hold of ours that may have landed set that climate
    assert detect(tstat(), climate="away", maybe_climates=frozenset({"home"})) is None
    # (b) only the setpoints the thermostat acts on: heating ignores the cooling one, off ignores both
    assert detect(tstat(cool=26.7, hvac=1)) is None
    assert detect(tstat(heat=21.1, hvac=1)).heat_f == 70.0
    assert detect(tstat(heat=21.1, cool=26.7, hvac=0)) is None
    # (c) a temperature hold at a comfort setting's own setpoints is not a change
    assert detect(tstat(mode=3, heat=16.7, cool=26.7)) is None
    # known temperature holds and events
    assert detect(tstat(mode=3, heat=21.1, cool=25.0), temps=((70.0, 77.0),)) is None
    assert detect(tstat(mode=3, heat=21.1, cool=25.0), event=True) is None
    # unreadable or unknown mode
    assert detect({1: {HEAT: 21.1, COOL: 25.0}}) is None
    assert detect(tstat(mode=7, heat=21.1)) is None


def test_same_change_compares_comfort_by_name_and_temperature_by_setpoints():
    comfort = hk.HandChange("comfort", "home", 68.0, 76.0, 0)
    assert comfort.same_as({"climate_ref": "home", "heat_f": 67.0, "cool_f": 75.0})
    assert not comfort.same_as({"climate_ref": "sleep", "heat_f": 68.0, "cool_f": 76.0})
    temp = hk.HandChange("temperature", None, 70.0, None, 3)
    assert temp.same_as({"climate_ref": None, "heat_f": 70.0, "cool_f": None})
    assert not temp.same_as({"climate_ref": None, "heat_f": 70.5, "cool_f": None})
    assert not temp.same_as({"climate_ref": None, "heat_f": 70.0, "cool_f": 76.0})
    assert not temp.same_as(None)


def test_reason_wording():
    temp = hk.HandChange("temperature", None, 70.0, None, 3)
    assert svc_mod.hand_change_reason(temp, "Upstairs") == (
        "Someone set a temperature hold (heat 70°F) by hand on the upstairs thermostat (seen over HomeKit while "
        "the ecobee cloud is down); the controller stands aside until the cloud is back."
    )
    away = hk.HandChange("comfort", "away", 62.0, 80.0, 2)
    assert svc_mod.hand_change_reason(away, "Upstairs").startswith("Someone picked Away by hand on the upstairs")


async def test_the_controllers_homekit_rows_fail_once_the_mode_changed(env):
    """A controller hold queued in 'act' is not sent after the owner switched to Suggest or
    handed back to ecobee (mode off) before the service claimed it."""
    db, dev = env["db"], env["dev"]
    await paired_and_ready(env)
    for mode in ("suggest", "off"):
        put_setting(db, "control", ControlSettings(mode=mode))
        db.commit()
        row_id = queue(db, {"kind": "climate_hold", "climate": "away", "unit_key": "main",
                            "until": (env["clock"]() + timedelta(hours=2)).isoformat()})
        await env["svc"].execute_actions()
        row = action(db, row_id)
        assert (row.status, row.error) == ("failed", f"Not sent: the controller was switched to {mode}.")
    assert dev.writes == []
