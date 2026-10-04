"""SimulatedHouse utility events, opt-out and Smart Away / Follow Me settings, plus the
``climate sim-event`` command that injects events into a database-backed simulator."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from climate import cli
from climate.sources.base import HoldRequest, ThermostatEvent
from climate.sources.simulator import (
    SIM_EVENTS_KEY,
    SimulatedHouse,
    load_sim_events,
    normalize_event,
)
from climate.store.app_settings import SourceSettings, get_raw, put_setting
from climate.timeutil import utcnow

T0 = datetime(2026, 7, 15, 17, 2, tzinfo=UTC)  # 12:02 local (CDT), the 'home' program


class Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


def peak(start: datetime, hours: float = 2.0, **kw) -> ThermostatEvent:
    fields = {"event_type": "demandResponse", "name": "Peak Saver", "start": start,
              "end": start + timedelta(hours=hours), "is_relative": True, "cool_offset_f": 2.0,
              "is_optional": True}
    fields.update(kw)
    return ThermostatEvent(**fields)


async def snap(house: SimulatedHouse, unit: str):
    (s,) = await house.fetch_snapshots([unit])
    return s


def house_at(t: datetime = T0) -> tuple[SimulatedHouse, Clock]:
    clock = Clock(t)
    house = SimulatedHouse(seed=7, clock=clock)
    house.advance_to(t)
    return house, clock


# --- lifecycle -------------------------------------------------------------------------


async def test_injected_event_is_announced_runs_with_offsets_then_disappears():
    house, clock = house_at()
    await house.apply_settings("up", {"autoAway": False}, "steady program")  # no Smart Away in the way
    start = T0 + timedelta(minutes=30)
    stored = house.inject_event("up", peak(start, link_ref="dr-1"))
    assert stored.link_ref == "dr-1" and stored.running is False

    before = await snap(house, "up")
    assert before.hold is None
    assert [(e.event_type, e.running, e.link_ref) for e in before.events] == [
        ("demandResponse", False, "dr-1")]
    assert before.events[0].start == start and before.events[0].end == start + timedelta(hours=2)
    rev_before = (await house.poll_revisions())["up"]

    clock.t = start + timedelta(minutes=1)
    during = await snap(house, "up")
    hold = during.hold
    assert hold is not None
    assert (hold.hold_type, hold.set_by_us, hold.event_name, hold.link_ref) == ("demandResponse", False,
                                                                               "Peak Saver", "dr-1")
    assert (hold.is_relative, hold.cool_offset_f, hold.heat_offset_f) == (True, 2.0, None)
    assert hold.is_optional is True
    assert (hold.heat_f, hold.cool_f) == (None, None)  # a relative event reports offsets, not setpoints
    assert (hold.start, hold.end) == (start, start + timedelta(hours=2))
    # the offset applies to the scheduled comfort setting; heating stays on the schedule
    assert during.cool_sp_f == during.settings["program_cool_f"] + 2.0
    assert during.heat_sp_f == during.settings["program_heat_f"]
    assert during.climate_ref == "home"
    assert [(e.running, e.link_ref) for e in during.events] == [(True, "dr-1")]
    assert (await house.poll_revisions())["up"] != rev_before
    # the other units are not part of it
    assert (await snap(house, "main")).events == [] and (await snap(house, "main")).hold is None

    clock.t = start + timedelta(hours=2)
    after = await snap(house, "up")
    assert after.hold is None and after.events == []
    assert after.cool_sp_f == after.settings["program_cool_f"]
    # the runtime record carries the event's setpoint while it ran
    rows = [r for r in await house.fetch_runtime(start + timedelta(minutes=5), start + timedelta(hours=2))
            if r.unit_key == "up"]
    assert rows and {r.cool_sp_f for r in rows} == {during.settings["program_cool_f"] + 2.0}


async def test_absolute_event_setpoints_and_heating_offsets():
    house, clock = house_at()
    house.inject_event("bed", peak(T0, is_relative=False, cool_offset_f=None, cool_f=79.0))
    house.inject_event("main", peak(T0, cool_offset_f=None, heat_offset_f=-2.0, name="Winter Peak"))
    clock.t = T0 + timedelta(minutes=1)
    bed = await snap(house, "bed")
    assert (bed.cool_sp_f, bed.heat_sp_f) == (79.0, bed.settings["program_heat_f"])
    assert (bed.hold.cool_f, bed.hold.heat_f) == (79.0, bed.settings["program_heat_f"])
    assert bed.hold.is_relative is False
    main = await snap(house, "main")
    assert main.heat_sp_f == main.settings["program_heat_f"] - 2.0
    assert main.hold.heat_offset_f == -2.0 and main.hold.event_name == "Winter Peak"


async def test_event_above_our_hold_and_resume_never_cancels_it():
    house, clock = house_at()
    held = await house.set_hold(HoldRequest(unit_key="up", heat_f=68, cool_f=75, hours=2, reason="ours"))
    assert held.ok
    house.inject_event("up", peak(T0 + timedelta(minutes=10), hours=1.0))
    clock.t = T0 + timedelta(minutes=11)
    s = await snap(house, "up")
    assert s.hold.hold_type == "demandResponse" and s.cool_sp_f == s.settings["program_cool_f"] + 2.0

    resumed = await house.resume_program("up", "controller resume", force=True)
    assert resumed.ok is False and "demandResponse event is in effect" in resumed.error
    assert resumed.readback is None
    # like the ecobee adapter: no hold is written while the event runs, by the controller or the owner
    for by_owner in (False, True):
        rewrite = await house.set_hold(HoldRequest(unit_key="up", heat_f=67, cool_f=74, hours=2, reason="x",
                                                   by_owner=by_owner))
        assert rewrite.ok is False and rewrite.readback is None
        assert rewrite.request["refused"] is True and rewrite.request["event"] == "demandResponse"
        assert rewrite.error == "a running demandResponse event is in effect; not written"
    assert (await snap(house, "up")).hold.hold_type == "demandResponse"
    clock.t = T0 + timedelta(minutes=71)  # the event is over: our hold runs again, untouched
    s = await snap(house, "up")
    assert (s.hold.hold_type, s.hold.cool_f, s.hold.set_by_us) == ("holdHours", 75, True)


# --- opt-out ---------------------------------------------------------------------------


async def test_opt_out_removes_an_optional_event_and_our_hold_runs_again():
    house, clock = house_at()
    await house.set_hold(HoldRequest(unit_key="up", heat_f=68, cool_f=75, hours=2, reason="ours"))
    house.inject_event("up", peak(T0 + timedelta(minutes=10), link_ref="dr-2"))
    house.inject_event("main", peak(T0 + timedelta(minutes=10), link_ref="dr-2"))
    clock.t = T0 + timedelta(minutes=11)
    result = await house.opt_out_event("up", "Skipped from the app")
    assert result.ok is True, result.error
    assert result.channel == "simulator"
    assert (result.request["event_name"], result.request["link_ref"]) == ("Peak Saver", "dr-2")
    assert result.before["demand_response_running"] is True
    assert result.before["hold"]["hold_type"] == "demandResponse"
    assert result.readback["demand_response_running"] is False
    assert result.readback["hold"]["hold_type"] == "holdHours" and result.readback["cool_f"] == 75
    s = await snap(house, "up")
    assert s.events == [] and s.hold.hold_type == "holdHours" and s.hold.set_by_us is True
    # the same event on another unit keeps running (opt-outs are per thermostat)
    assert (await snap(house, "main")).hold.hold_type == "demandResponse"


async def test_opt_out_refusals():
    house, clock = house_at()
    nothing = await house.opt_out_event("up", "x")
    assert nothing.ok is False
    assert nothing.request["refused"] is True and nothing.request["mandatory"] is False
    assert "nothing is running" in nothing.error

    house.inject_event("up", peak(T0, is_optional=False, name="Grid Emergency"))
    clock.t = T0 + timedelta(minutes=1)
    mandatory = await house.opt_out_event("up", "x")
    assert mandatory.ok is False
    assert mandatory.request["refused"] is True and mandatory.request["mandatory"] is True
    assert "mandatory" in mandatory.error and "Grid Emergency" in mandatory.error
    assert (await snap(house, "up")).hold.hold_type == "demandResponse"

    beach = ThermostatEvent(event_type="vacation", name="Beach", start=T0, end=T0 + timedelta(days=2),
                            heat_f=60.0, cool_f=85.0)
    house.inject_event("bed", beach)
    vacation = await house.opt_out_event("bed", "x")
    assert vacation.ok is False and "a vacation event is on top" in vacation.error
    assert (await snap(house, "bed")).hold.hold_type == "vacation"
    assert (await house.resume_program("bed", "x")).ok is False

    house.inject_event("main", peak(T0, hours=1.0))
    clock.t = T0 + timedelta(minutes=59)
    ending = await house.opt_out_event("main", "x")
    assert ending.ok is False and "ends within 2 minutes" in ending.error


async def test_opt_out_of_a_different_event_is_refused():
    """Finding 4 (simulator side): the skip names event dr-A, but dr-B runs on top now."""
    house, clock = house_at()
    house.inject_event("up", peak(T0, link_ref="dr-B", name="Peak Saver Extended"))
    clock.t = T0 + timedelta(minutes=1)
    other = await house.opt_out_event("up", "Skipped from the app", link_ref="dr-A", name="Peak Saver",
                                      start=T0 - timedelta(hours=1))
    assert other.ok is False and other.request["other_event"] is True and other.request["mandatory"] is False
    assert other.error == "a different utility event is on top; nothing sent"
    assert (await snap(house, "up")).hold.link_ref == "dr-B"
    same = await house.opt_out_event("up", "Skipped from the app", link_ref="dr-B")
    assert same.ok is True, same.error
    assert same.request["sent"] is True and same.readback["event_running"] is False


async def test_two_running_events_opt_out_of_the_top_one_only():
    house, clock = house_at()
    house.inject_event("up", peak(T0, link_ref="dr-1"))
    house.inject_event("up", peak(T0 + timedelta(minutes=5), link_ref="dr-2", name="Other program"))
    clock.t = T0 + timedelta(minutes=6)
    result = await house.opt_out_event("up", "x", link_ref="dr-1")
    assert result.ok is True, result.error
    assert result.readback["event_running"] is False and result.readback["demand_response_running"] is True
    assert (await snap(house, "up")).hold.link_ref == "dr-2"


async def test_owner_hold_is_a_persons_hold():
    """The owner's hold from the app is a person's: never ``set_by_us``; the controller cannot
    replace it or cancel it; the owner's own forced resume can."""
    house, _ = house_at()
    owner = await house.set_hold(HoldRequest(unit_key="up", heat_f=69, cool_f=74, hours=2, reason="Owner hold",
                                             by_owner=True))
    assert owner.ok is True, owner.error
    assert owner.readback["hold"]["set_by_us"] is False
    s = await snap(house, "up")
    assert (s.hold.hold_type, s.hold.set_by_us, s.cool_sp_f) == ("holdHours", False, 74)
    ctrl = await house.set_hold(HoldRequest(unit_key="up", heat_f=68, cool_f=76, hours=2, reason="tick"))
    assert ctrl.ok is False and ctrl.request["not_ours"] is True and "not set by the controller" in ctrl.error
    assert ctrl.before["hold"]["set_by_us"] is False
    resume = await house.resume_program("up", "controller resume")
    assert resume.ok is False and resume.error == "the running hold was not set by the controller; not cancelled"
    assert (await snap(house, "up")).cool_sp_f == 74
    # the owner may write over their own hold, and their forced resume ends it
    again = await house.set_hold(HoldRequest(unit_key="up", heat_f=69, cool_f=75, hours=1, reason="Owner",
                                             by_owner=True))
    assert again.ok is True, again.error
    back = await house.resume_program("up", "Owner: back to automatic", force=True)
    assert back.ok is True and back.readback["hold"] is None
    # the controller's own hold is ours: it renews and cancels it freely
    ours = await house.set_hold(HoldRequest(unit_key="up", heat_f=68, cool_f=76, hours=2, reason="tick"))
    renew = await house.set_hold(HoldRequest(unit_key="up", heat_f=68, cool_f=76, hours=2, reason="renew"))
    assert ours.ok and renew.ok and renew.before["hold"]["set_by_us"] is True
    assert (await house.resume_program("up", "controller resume")).ok is True


# --- Smart Away / Follow Me ------------------------------------------------------------


async def test_apply_settings_switches_smart_away_and_reads_back():
    house, clock = house_at()
    s = await snap(house, "main")
    assert (s.settings["autoAway"], s.settings["followMeComfort"]) == (True, False)
    result = await house.apply_settings("main", {"autoAway": False, "followMeComfort": True}, "hand back")
    assert result.ok and result.before == {"autoAway": True, "followMeComfort": False}
    assert result.readback == {"autoAway": False, "followMeComfort": True}
    assert result.request["settings"] == {"autoAway": False, "followMeComfort": True}
    s = await snap(house, "main")
    assert (s.settings["autoAway"], s.settings["followMeComfort"]) == (False, True)
    again = await house.apply_settings("main", {"autoAway": False}, "x")
    assert again.ok and again.request["noop"] is True
    for bad in ({"hvacMode": "off"}, {}, {"autoAway": "no"}):
        assert (await house.apply_settings("main", bad, "x")).ok is False
    assert (await house.apply_settings("garage", {"autoAway": False}, "x")).ok is False
    # saved and restored with the rest of the state
    state = json.loads(json.dumps(house._to_state()))
    restored = SimulatedHouse(seed=7)
    assert restored._restore(state)
    assert (restored._auto_away, restored._follow_me) == (house._auto_away, house._follow_me)


def test_smart_away_only_floats_the_house_while_auto_away_is_on():
    start = datetime(2026, 7, 13, 5, tzinfo=UTC)  # a Monday, local midnight
    on = SimulatedHouse(seed=7)
    rows_on, _ = on.generate_history(start, start + timedelta(days=2))
    off = SimulatedHouse(seed=7)
    off._auto_away = [False] * 3
    rows_off, _ = off.generate_history(start, start + timedelta(days=2))
    assert any(r.climate_ref == "away" for r in rows_on if r.unit_key == "main")
    assert not any(r.climate_ref == "away" for r in rows_off)


def test_normalize_event_rejects_what_the_simulator_cannot_run():
    ok = normalize_event(peak(T0.replace(tzinfo=None)))  # naive times are taken as UTC
    assert ok.start == T0 and ok.link_ref == "sim-demandResponse-202607151702-202607151902"
    bad = [
        peak(T0, event_type="today"),
        peak(T0, end=T0),
        peak(T0, cool_offset_f=-2.0),
        peak(T0, cool_offset_f=None, heat_offset_f=2.0),
        peak(T0, cool_offset_f=None),
        peak(T0, cool_f=78.0),
        peak(T0, is_relative=False, cool_offset_f=None, cool_f=99.0),
        peak(T0, is_relative=False, cool_offset_f=None, cool_f=70.0, heat_f=69.0),
        peak(T0, hours=24 * 40),
    ]
    for ev in bad:
        with pytest.raises(ValueError):
            normalize_event(ev)
    house, _ = house_at()
    with pytest.raises(ValueError):
        house.inject_event("garage", peak(T0))


# --- database mode and the CLI ---------------------------------------------------------


async def test_cli_injects_into_a_database_backed_simulator(db, capsys):
    now = utcnow().replace(second=0, microsecond=0)
    clock = Clock(now)
    house = SimulatedHouse(seed=7, clock=clock, use_db=True)
    assert (await snap(house, "up")).events == []

    assert cli.main(["sim-event", "--unit", "up,main", "--in-min", "30", "--hours", "2", "--cool-offset", "2",
                     "--name", "Peak Saver"]) == 0
    out = capsys.readouterr().out
    assert "Injected optional utility event 'Peak Saver' on up, main" in out and "cooling +2°F" in out
    db.rollback()
    stored = load_sim_events(db)
    assert set(stored) == {"up", "main"} and stored["up"][0].link_ref == stored["main"][0].link_ref
    ev = stored["up"][0]
    assert ev.end - ev.start == timedelta(hours=2) and ev.is_optional is True

    clock.t = now + timedelta(minutes=1)  # the simulator re-reads the row every simulated minute
    up = await snap(house, "up")
    assert [(e.running, e.link_ref) for e in up.events] == [(False, ev.link_ref)]

    clock.t = ev.start + timedelta(minutes=1)
    assert (await snap(house, "up")).hold.hold_type == "demandResponse"
    result = await house.opt_out_event("up", "Skipped from the app")
    assert result.ok, result.error
    db.rollback()
    assert set(load_sim_events(db)) == {"main"}  # the opt-out is recorded in the shared row

    # a restarted simulator still has the main floor's event
    clock.t += timedelta(minutes=1)
    restarted = SimulatedHouse(seed=7, clock=clock, use_db=True)
    assert (await snap(restarted, "main")).hold.hold_type == "demandResponse"
    assert (await snap(restarted, "up")).hold is None

    assert cli.main(["sim-event", "--clear"]) == 0
    assert "Removed 1 injected event(s)" in capsys.readouterr().out
    db.rollback()
    assert get_raw(db, SIM_EVENTS_KEY) == {}
    clock.t += timedelta(minutes=1)
    assert (await snap(restarted, "main")).hold is None


def test_cli_mandatory_and_absolute_event(db, capsys):
    assert cli.main(["sim-event", "--unit", "bed", "--in-min", "0", "--hours", "1", "--cool", "78",
                     "--mandatory"]) == 0
    assert "Injected mandatory utility event" in capsys.readouterr().out
    db.rollback()
    (ev,) = load_sim_events(db)["bed"]
    assert (ev.is_optional, ev.is_relative, ev.cool_f, ev.heat_f) == (False, False, 78.0, None)


@pytest.mark.parametrize("argv, message", [
    (["sim-event", "--unit", "up", "--cool-offset", "2", "--cool", "78"], "not both"),
    (["sim-event", "--unit", "up"], "not both and not neither"),
    (["sim-event", "--cool-offset", "2"], "--unit is required"),
    (["sim-event", "--unit", "garage", "--cool-offset", "2"], "Unknown unit"),
    (["sim-event", "--unit", "up", "--cool-offset", "-2"], "Not injected"),
    (["sim-event", "--unit", "up", "--heat-offset", "-2", "--hours", "0"], "--hours must be above 0"),
])
def test_cli_rejects_bad_requests(db, capsys, argv, message):
    assert cli.main(argv) == 1
    assert message in capsys.readouterr().out
    db.rollback()
    assert get_raw(db, SIM_EVENTS_KEY) is None


def test_cli_refuses_when_the_source_is_ecobee(db, capsys):
    put_setting(db, "source", SourceSettings(kind="ecobee"))
    db.commit()
    assert cli.main(["sim-event", "--unit", "up", "--cool-offset", "2"]) == 1
    assert "not the simulator" in capsys.readouterr().out
