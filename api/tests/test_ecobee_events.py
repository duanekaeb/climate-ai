"""ecobee events: utility (demand-response) events, vacations and unknown event types, from
the JSON (parsing) through the adapter (snapshots, opt-out, Smart Away / Follow Me writes)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from climate.sources import ecobee_parse as P
from climate.sources.base import KNOWN_HOLD_TYPES, HoldRequest, UtilityInfo
from climate.sources.ecobee import EcobeeApiError, EcobeeCloud
from climate.store.orm import EcobeeThermostat
from climate.store.secrets import put_secret
from tests.test_ecobee_cloud import CLIENT_ID, MAIN, UP, FakeEcobee

OFF = timedelta(hours=-5)  # the fixture thermostats run on CDT (14:32 local = 19:32Z)
T_NOW = datetime(2026, 10, 4, 19, 32, 10, tzinfo=UTC)


def dr(**overrides: Any) -> dict[str, Any]:
    """A demand-response event as ecobee lists it (absolute, optional, running 14:00-18:00 local)."""
    ev: dict[str, Any] = {
        "type": "demandResponse", "name": "Peak Saver", "running": True,
        "startDate": "2026-10-04", "startTime": "14:00:00", "endDate": "2026-10-04", "endTime": "18:00:00",
        "isOccupied": False, "isCoolOff": False, "isHeatOff": False, "coolHoldTemp": 780, "heatHoldTemp": 640,
        "fan": "auto", "vent": "off", "ventilatorMinOnTime": 5, "isOptional": True,
        "isTemperatureRelative": False, "coolRelativeTemp": 0, "heatRelativeTemp": 0,
        "isTemperatureAbsolute": True, "dutyCyclePercentage": 100, "fanMinOnTime": 0,
        "occupiedSensorActive": False, "unoccupiedSensorActive": False, "drRampUpTemp": 0,
        "drRampUpTime": 3600,
        "linkRef": "dr-7731", "holdClimateRef": "",
    }
    ev.update(overrides)
    return ev


def relative_dr(**overrides: Any) -> dict[str, Any]:
    """A relative event: cooling +2°F, heating -2°F (setback magnitudes in tenths)."""
    return dr(**{"isTemperatureRelative": True, "isTemperatureAbsolute": False, "coolRelativeTemp": 20,
                 "heatRelativeTemp": 20, "coolHoldTemp": 0, "heatHoldTemp": 0, **overrides})


def plain_hold(**overrides: Any) -> dict[str, Any]:
    return dr(**{"type": "hold", "name": "auto", "linkRef": "", "coolHoldTemp": 750, "heatHoldTemp": 680,
                 "dutyCyclePercentage": 255, "startTime": "13:00:00", "endTime": "15:00:00", **overrides})


# --- parsing: the running top event ----------------------------------------------------


def test_running_absolute_demand_response_carries_the_event_details():
    hold = P.parse_hold(dr(), OFF, {}, None)
    assert (hold.hold_type, hold.set_by_us, hold.kind) == ("demandResponse", False, "temperature")
    assert (hold.heat_f, hold.cool_f) == (64.0, 78.0)
    assert hold.start == datetime(2026, 10, 4, 19, 0, tzinfo=UTC)
    assert hold.end == datetime(2026, 10, 4, 23, 0, tzinfo=UTC)
    assert (hold.event_name, hold.is_relative) == ("Peak Saver", False)
    assert (hold.heat_offset_f, hold.cool_offset_f) == (None, None)
    assert (hold.is_optional, hold.link_ref) == (True, "dr-7731")
    assert P.parse_hold(dr(isOptional=False), OFF, {}, None).is_optional is False


def test_running_relative_demand_response_has_offsets_and_no_setpoints():
    hold = P.parse_hold(relative_dr(), OFF, {}, None)
    assert hold.is_relative is True
    assert (hold.heat_f, hold.cool_f) == (None, None)  # the hold temps of a relative event are not setpoints
    assert (hold.cool_offset_f, hold.heat_offset_f) == (2.0, -2.0)
    # the relative temps are setback magnitudes: a signed value reads the same way
    signed = P.parse_hold(relative_dr(heatRelativeTemp=-30, coolRelativeTemp=-15), OFF, {}, None)
    assert (signed.cool_offset_f, signed.heat_offset_f) == (1.5, -3.0)
    # a side the event leaves alone has no offset
    cool_only = P.parse_hold(relative_dr(heatRelativeTemp=0), OFF, {}, None)
    assert (cool_only.cool_offset_f, cool_only.heat_offset_f) == (2.0, None)
    # relative with no isTemperatureAbsolute flag at all: still no absolute setpoints
    no_flag = relative_dr(coolHoldTemp=790, heatHoldTemp=600)
    del no_flag["isTemperatureAbsolute"]
    no_flag_hold = P.parse_hold(no_flag, OFF, {}, None)
    assert (no_flag_hold.heat_f, no_flag_hold.cool_f) == (None, None)


def test_plain_holds_carry_no_event_details():
    hold = P.parse_hold(plain_hold(), OFF, {}, None)
    assert hold.hold_type in P.PLAIN_HOLD_TYPES
    assert (hold.event_name, hold.is_optional, hold.link_ref, hold.is_relative) == (None, None, None, False)


def test_first_running_event_of_any_type_is_in_effect():
    hold_ev = plain_hold()
    # a utility event above a hand-set hold: the event is in effect
    assert P.running_override([dr(), hold_ev]) is not None
    assert P.running_override([dr(), hold_ev])["type"] == "demandResponse"
    # announced (not yet running) events and templates are skipped
    announced = dr(running=False, startTime="16:00:00")
    template = dr(type="template", name="template", running=True)
    assert P.running_override([template, announced, hold_ev]) is hold_ev
    assert P.running_override([announced, template]) is None


def test_unknown_running_event_keeps_its_type_and_is_never_ours():
    ours = {"heat_f": 68.0, "cool_f": 75.0, "end": "2026-10-04T20:00:00+00:00"}  # same setpoints and end
    today = plain_hold(type="today", name="today")
    ev = P.running_override([dr(running=False, startTime="16:00:00"), today, plain_hold()])
    assert ev is today
    hold = P.parse_hold(ev, OFF, {}, ours)
    assert (hold.hold_type, hold.set_by_us) == ("today", False)
    assert hold.hold_type not in KNOWN_HOLD_TYPES
    assert P.is_event_hold(hold) is True
    assert P.is_event_hold(P.parse_hold(plain_hold(), OFF, {}, ours)) is False
    assert P.is_event_hold(None) is False


# --- parsing: listed events, utility ---------------------------------------------------


def test_parse_events_lists_running_and_announced_events_only():
    events = [
        plain_hold(),
        dr(running=False, name="Evening Peak", startTime="16:00:00", endTime="19:00:00", linkRef="dr-8000",
           isTemperatureRelative=True, isTemperatureAbsolute=False, coolRelativeTemp=30, heatRelativeTemp=0,
           dutyCyclePercentage=50),
        relative_dr(),
        dr(type="vacation", name="Beach", running=False, startDate="2026-12-20", startTime="08:00:00",
           endDate="2026-12-27", endTime="18:00:00", coolHoldTemp=820, heatHoldTemp=620, linkRef=""),
        dr(type="vacation", name="Past trip", running=False, startDate="2026-09-01", endDate="2026-09-03"),
        dr(running=False, name="Earlier today", startTime="09:00:00", endTime="11:00:00"),
        dr(type="template", name="template", running=False, startDate="2027-01-01", endDate="2027-01-02"),
        dr(type="today", running=True),
    ]
    out = P.parse_events(events, OFF, now=T_NOW)
    assert [(e.event_type, e.name, e.running) for e in out] == [
        ("demandResponse", "Evening Peak", False),
        ("demandResponse", "Peak Saver", True),
        ("vacation", "Beach", False),
    ]
    announced, running, vacation = out
    assert announced.start == datetime(2026, 10, 4, 21, 0, tzinfo=UTC)
    assert announced.end == datetime(2026, 10, 5, 0, 0, tzinfo=UTC)
    assert (announced.is_relative, announced.cool_offset_f, announced.heat_offset_f) == (True, 3.0, None)
    assert (announced.duty_cycle_pct, announced.link_ref, announced.is_optional) == (50, "dr-8000", True)
    assert (running.cool_offset_f, running.heat_offset_f) == (2.0, -2.0)
    assert (running.heat_f, running.cool_f) == (None, None)
    assert running.duty_cycle_pct == 100
    assert (vacation.heat_f, vacation.cool_f) == (62.0, 82.0)
    assert (vacation.link_ref, vacation.is_relative) == (None, False)
    # without the zone, the current (CDT) offset; with it, the event's own (CST) offset
    assert vacation.start == datetime(2026, 12, 20, 13, 0, tzinfo=UTC)
    with_zone = P.parse_events(events, OFF, now=T_NOW, tz=ZoneInfo("America/Chicago"))
    assert with_zone[2].start == datetime(2026, 12, 20, 14, 0, tzinfo=UTC)
    assert with_zone[0].start == announced.start  # today's events convert the same either way
    assert P.parse_events([], OFF) == [] and P.parse_events(None, None) == []


def test_parse_events_flags_and_unused_duty_cycle():
    (ev,) = P.parse_events([dr(isCoolOff=True, dutyCyclePercentage=255, isOptional=False)], OFF, now=T_NOW)
    assert ev.is_cool_off is True and ev.is_heat_off is False
    assert ev.duty_cycle_pct is None  # 255 = not used
    assert ev.is_optional is False
    # a running event whose end has passed on our clock is still listed while ecobee says it runs
    assert len(P.parse_events([dr()], OFF, now=T_NOW + timedelta(hours=6))) == 1


def test_parse_utility():
    assert P.parse_utility({"name": "", "phone": "", "email": "", "web": ""}) is None
    assert P.parse_utility({"name": "   "}) is None
    assert P.parse_utility(None) is None
    info = P.parse_utility({"name": "Example Electric Co-op", "phone": "800-555-0100", "email": "",
                            "web": "https://example.com/peak-saver"})
    assert info == UtilityInfo(name="Example Electric Co-op", phone="800-555-0100", email=None,
                               web="https://example.com/peak-saver")


# --- adapter ---------------------------------------------------------------------------


@pytest.fixture
def fake() -> FakeEcobee:
    return FakeEcobee()


@pytest.fixture
async def cloud(db, fake):
    put_secret(db, "ecobee_refresh_token", "rt-1")
    db.commit()
    c = EcobeeCloud(CLIENT_ID, transport=fake.transport(), readback_delay_s=0)
    await c.poll_revisions()  # maps the three thermostats to their units
    yield c
    await c.close()


def thermostat_gets(fake: FakeEcobee) -> int:
    return sum(1 for m, path, _ in fake.calls if m == "GET" and path == "/1/thermostat")


async def test_snapshot_events_utility_and_stored_enrollment(cloud, fake, db):
    fake.tstats[MAIN]["events"].insert(1, dr(running=False, startTime="16:00:00", endTime="19:00:00"))
    fake.tstats[UP]["events"] = [relative_dr(linkRef="dr-9")]
    main, up, bed = await cloud.fetch_snapshots()
    assert main.utility == UtilityInfo(name="Example Electric Co-op", phone="800-555-0100",
                                       web="https://example.com/peak-saver")
    assert up.utility is None and bed.utility is None  # empty utility / none returned
    assert main.settings["drAccept"] == "customerSelect" and up.settings["drAccept"] == "askMe"
    assert [(e.event_type, e.running) for e in main.events] == [("demandResponse", False),
                                                                ("vacation", False)]
    assert main.hold.hold_type == "holdHours"  # the announced event does not override anything yet
    assert [(e.event_type, e.running, e.link_ref) for e in up.events] == [("demandResponse", True, "dr-9")]
    assert (up.hold.hold_type, up.hold.cool_offset_f, up.hold.set_by_us) == ("demandResponse", 2.0, False)
    assert bed.events == []
    db.rollback()
    rows = {r.identifier: r.settings for r in db.execute(select(EcobeeThermostat)).scalars()}
    assert rows[MAIN]["utility"] == {"name": "Example Electric Co-op", "phone": "800-555-0100", "email": None,
                                     "web": "https://example.com/peak-saver"}
    assert rows[MAIN]["drAccept"] == "customerSelect"
    assert rows[UP]["utility"] is None and rows[UP]["drAccept"] == "askMe"
    assert "utility" in rows[MAIN] and rows[MAIN]["timeZone"] == "America/Chicago"


async def test_opt_out_cancels_an_optional_event_and_leaves_the_hold_underneath(cloud, fake):
    ours = await cloud.set_hold(HoldRequest(unit_key="up", heat_f=68.0, cool_f=77.0, hours=2, reason="ours"))
    assert ours.ok, ours.error
    held = fake.tstats[UP]["events"][0]
    fake.tstats[UP]["events"] = [dr(), held]
    fake.posts.clear()
    result = await cloud.opt_out_event("up", "Skipped from the app")
    assert result.ok is True, result.error
    (post,) = fake.posts
    assert post["selection"] == {"selectionType": "thermostats", "selectionMatch": UP}
    assert post["functions"] == [{"type": "resumeProgram", "params": {"resumeAll": False}}]
    assert result.request["function"] == post["functions"][0]
    assert (result.request["event_name"], result.request["link_ref"]) == ("Peak Saver", "dr-7731")
    assert "refused" not in result.request
    assert result.before["hold"]["hold_type"] == "demandResponse" and result.before["demand_response_running"]
    assert result.readback["demand_response_running"] is False
    # the controller's hold underneath was not touched: it is on top again, still ours
    after = result.readback["hold"]
    assert after["hold_type"] == "holdHours" and after["set_by_us"] is True
    assert fake.tstats[UP]["events"] == [held]


async def test_opt_out_refuses_a_mandatory_event(cloud, fake):
    fake.tstats[UP]["events"] = [dr(isOptional=False)]
    result = await cloud.opt_out_event("up", "Skipped from the app")
    assert result.ok is False
    assert result.request["refused"] is True and result.request["mandatory"] is True
    assert "mandatory" in result.error and "Peak Saver" in result.error
    assert fake.posts == [] and result.readback is None
    assert result.before["hold"]["is_optional"] is False


@pytest.mark.parametrize("events, on_top", [
    ([], "nothing is running"),
    ([plain_hold()], "a hold is on top"),
    ([plain_hold(), dr()], "a hold is on top"),  # a person's hold above the utility event: never cancelled
    ([dr(type="vacation", name="Beach")], "a vacation event is on top"),
    ([dr(running=False)], "nothing is running"),  # announced, not running
])
async def test_opt_out_refuses_when_no_utility_event_is_on_top(cloud, fake, events, on_top):
    fake.tstats[UP]["events"] = events
    result = await cloud.opt_out_event("up", "Skipped from the app")
    assert result.ok is False and on_top in result.error
    assert result.request["refused"] is True and result.request["mandatory"] is False
    assert fake.posts == []


async def test_opt_out_refuses_right_before_the_event_ends(cloud, fake):
    # the fixture's thermostat clock reads 14:32:10 local; this event ends at 14:34:00
    fake.tstats[UP]["events"] = [dr(endTime="14:34:00")]
    result = await cloud.opt_out_event("up", "Skipped from the app")
    assert result.ok is False and result.request["refused"] is True
    assert "ends within 2 minutes" in result.error
    assert fake.posts == []


async def test_opt_out_read_back_failure_is_not_ok(cloud, fake):
    fake.tstats[UP]["events"] = [dr()]
    fake.dr_sticky = True
    gets = thermostat_gets(fake)
    result = await cloud.opt_out_event("up", "Skipped from the app")
    assert result.ok is False and "still running" in result.error
    assert len(fake.posts) == 1
    assert thermostat_gets(fake) - gets == 3  # the fresh GET, then two read-backs
    assert result.readback["demand_response_running"] is True


async def test_opt_out_after_a_transport_error_lets_the_read_back_decide(cloud, fake, monkeypatch):
    fake.tstats[UP]["events"] = [dr()]
    real_post = cloud._post

    async def landed_then_dropped(ident, payload):
        await real_post(ident, payload)
        raise EcobeeApiError("ecobee thermostat request failed (ReadTimeout)", transport=True)

    monkeypatch.setattr(cloud, "_post", landed_then_dropped)
    result = await cloud.opt_out_event("up", "rule")
    assert result.ok is True, result.error

    fake.tstats[UP]["events"] = [dr()]

    async def rejected(ident, payload):
        raise EcobeeApiError("ecobee thermostat: HTTP 500, status 3: Processing error")

    monkeypatch.setattr(cloud, "_post", rejected)
    failed = await cloud.opt_out_event("up", "rule")
    assert failed.ok is False and "Processing error" in failed.error and failed.readback is None


async def test_opt_out_unmapped_unit(cloud, fake):
    result = await cloud.opt_out_event("garage", "x")
    assert result.ok is False and "not mapped" in result.error and fake.posts == []


async def test_set_hold_under_a_running_event_is_not_verified(cloud, fake):
    """An unknown event already on top: nothing is written. One that starts between the read
    and the write stays above the new hold, which then does not read back as this write."""
    fake.tstats[UP]["events"] = [plain_hold(type="today", name="today")]
    refused = await cloud.set_hold(HoldRequest(unit_key="up", heat_f=68.0, cool_f=77.0, hours=2, reason="x"))
    assert refused.ok is False and refused.request["event"] == "today" and fake.posts == []

    fake.tstats[UP]["events"] = []
    real_apply = fake._apply

    def event_starts_on_top(t, body):
        real_apply(t, body)
        t["events"].insert(0, plain_hold(type="today", name="today"))  # it stays above the new hold

    fake._apply = event_starts_on_top
    result = await cloud.set_hold(HoldRequest(unit_key="up", heat_f=68.0, cool_f=77.0, hours=2, reason="x"))
    assert result.ok is False and "a running today event overrides the hold" in result.error


# --- opt-out: which event, and the read-back ---------------------------------------------


async def test_opt_out_refuses_a_different_event_on_top(cloud, fake):
    """Finding 4: the skip was asked for event L1 (the snapshot it was decided on still showed
    it), but at the thermostat L1 is over and L2 runs. Nothing is sent: an opt-out is recorded
    and reported to the utility, so it must be of the event the owner skipped."""
    fake.tstats[UP]["events"] = [dr(linkRef="L2", name="Peak Saver Extended", startTime="14:30:00")]
    result = await cloud.opt_out_event("up", "Skipped from the app", link_ref="L1", name="Peak Saver",
                                       start=datetime(2026, 10, 4, 19, 0, tzinfo=UTC))
    assert result.ok is False and fake.posts == [] and result.readback is None
    assert result.request["refused"] is True and result.request["other_event"] is True
    assert result.request["mandatory"] is False  # never settles the skip as "mandatory"
    assert result.error == "a different utility event is on top; nothing sent"
    assert result.request["link_ref"] == "L2"
    # a mandatory different event is still "a different event", not a mandatory refusal
    fake.tstats[UP]["events"] = [dr(linkRef="L2", isOptional=False)]
    other = await cloud.opt_out_event("up", "x", link_ref="L1")
    assert other.request["other_event"] is True and other.request["mandatory"] is False
    # an event with a linkRef never matches one asked for without (and the other way round)
    fake.tstats[UP]["events"] = [dr(linkRef="")]
    no_ref = await cloud.opt_out_event("up", "x", link_ref="dr-7731")
    assert no_ref.request.get("other_event") is True and fake.posts == []
    # the event asked for: sent
    fake.tstats[UP]["events"] = [dr(linkRef="L1")]
    same = await cloud.opt_out_event("up", "x", link_ref="L1", name="whatever", start=None)
    assert same.ok is True, same.error
    assert len(fake.posts) == 1


async def test_opt_out_identity_without_a_link_ref_is_name_and_start(cloud, fake):
    """No linkRef: the name and the start (UTC, to the minute) identify the event, converted
    through the thermostat's zone exactly like the snapshot that listed it. Here the event
    started at 12:30 AM CDT and it is now 2:30 AM CST (the night the clocks went back): the
    current offset alone would put its start an hour late and refuse the right event."""
    t = fake.tstats[UP]
    t["thermostatTime"], t["utcTime"] = "2026-11-01 02:30:00", "2026-11-01 08:30:00"
    t["events"] = [dr(linkRef="", name="Night Saver", startDate="2026-11-01", startTime="00:30:00",
                      endDate="2026-11-01", endTime="04:00:00")]
    (listed,) = P.parse_events(t["events"], timedelta(hours=-6), now=datetime(2026, 11, 1, 8, 30, tzinfo=UTC),
                               tz=ZoneInfo("America/Chicago"))
    assert listed.start == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)  # what the snapshot (and the row) say
    late = await cloud.opt_out_event("up", "x", name="Night Saver", start=listed.start + timedelta(minutes=60))
    assert late.ok is False and late.request["other_event"] is True
    renamed = await cloud.opt_out_event("up", "x", name="Night Saver Plus", start=listed.start)
    assert renamed.ok is False and renamed.request["other_event"] is True
    assert fake.posts == []
    fake.calls.clear()
    same = await cloud.opt_out_event("up", "x", name="Night Saver", start=listed.start + timedelta(seconds=40))
    assert same.ok is True, same.error
    assert fake.calls[0][2]["selection"]["includeLocation"] is True  # the fresh read carries the zone


async def test_opt_out_without_an_identity_still_takes_the_top_event(cloud, fake):
    fake.tstats[UP]["events"] = [dr(linkRef="L9")]
    result = await cloud.opt_out_event("up", "rule")
    assert result.ok is True and "other_event" not in result.request


async def test_opt_out_read_back_retries_a_failed_get(cloud, fake, monkeypatch):
    """Finding 14: the POST landed and the first read-back GET timed out. The second read-back
    decides (it used to abort with a bare transport error)."""
    fake.tstats[UP]["events"] = [dr()]
    real_get = cloud._get_one
    calls = {"n": 0}

    async def first_read_back_drops(ident, includes):
        calls["n"] += 1
        if calls["n"] == 2:  # 1 = the fresh GET before the POST, 2 = the first read-back
            raise EcobeeApiError("ecobee thermostat request failed (ReadTimeout)", transport=True)
        return await real_get(ident, includes)

    monkeypatch.setattr(cloud, "_get_one", first_read_back_drops)
    result = await cloud.opt_out_event("up", "Skipped from the app", link_ref="dr-7731")
    assert result.ok is True, result.error
    assert calls["n"] == 3 and len(fake.posts) == 1 and result.request["sent"] is True
    assert result.readback["event_running"] is False


async def test_opt_out_both_read_backs_failing_says_it_was_sent(cloud, fake, monkeypatch):
    fake.tstats[UP]["events"] = [dr()]
    real_get = cloud._get_one
    calls = {"n": 0}

    async def read_backs_drop(ident, includes):
        calls["n"] += 1
        if calls["n"] > 1:
            raise EcobeeApiError("ecobee thermostat request failed (ReadTimeout)", transport=True)
        return await real_get(ident, includes)

    monkeypatch.setattr(cloud, "_get_one", read_backs_drop)
    result = await cloud.opt_out_event("up", "Skipped from the app")
    assert result.ok is False and result.readback is None and calls["n"] == 3
    assert result.error.startswith("resumeProgram sent; read-back failed") and "ReadTimeout" in result.error
    assert result.request["sent"] is True and "refused" not in result.request


async def test_opt_out_read_back_checks_this_event_not_any(cloud, fake):
    """Finding 14: two utility events run at once. Opting out of the top one is a success even
    though the other one still runs."""
    fake.tstats[UP]["events"] = [dr(), dr(name="Other program", linkRef="dr-2")]
    result = await cloud.opt_out_event("up", "Skipped from the app", link_ref="dr-7731")
    assert result.ok is True, result.error
    assert result.readback["event_running"] is False and result.readback["demand_response_running"] is True
    assert [e["linkRef"] for e in fake.tstats[UP]["events"]] == ["dr-2"]


# --- event times through the thermostat's zone (finding 15) ------------------------------


def test_event_hold_times_convert_through_the_zone_plain_holds_do_not():
    chicago = ZoneInfo("America/Chicago")
    trip = dr(type="vacation", name="Trip", linkRef="", startDate="2026-10-25", startTime="08:00:00",
              endDate="2026-11-05", endTime="09:00:00")
    # polled on Oct 30 (CDT, -5 h); the trip ends after the change back to CST (-6 h)
    assert P.parse_hold(trip, OFF, {}, None).end == datetime(2026, 11, 5, 14, 0, tzinfo=UTC)  # offset only
    hold = P.parse_hold(trip, OFF, {}, None, tz=chicago)
    assert hold.end == datetime(2026, 11, 5, 15, 0, tzinfo=UTC)  # 9:00 AM CST
    assert hold.start == datetime(2026, 10, 25, 13, 0, tzinfo=UTC)
    (listed,) = P.parse_events([trip], OFF, now=datetime(2026, 10, 30, tzinfo=UTC), tz=chicago)
    assert (hold.start, hold.end) == (listed.start, listed.end)
    # a plain hold keeps the current offset, zone or not (its end is what set_by_us compares)
    held = plain_hold(endDate="2026-11-02", endTime="15:00:00")
    assert P.parse_hold(held, OFF, {}, None, tz=chicago).end == P.parse_hold(held, OFF, {}, None).end


async def test_snapshot_vacation_across_dst_ends_when_its_listed_event_ends(cloud, fake):
    t = fake.tstats[UP]
    t["thermostatTime"], t["utcTime"] = "2026-10-30 14:32:10", "2026-10-30 19:32:10"  # CDT
    t["events"] = [dr(type="vacation", name="Trip", linkRef="", startDate="2026-10-25", startTime="08:00:00",
                      endDate="2026-11-05", endTime="09:00:00")]
    (snap,) = await cloud.fetch_snapshots(["up"])
    (ev,) = snap.events
    assert ev.end == datetime(2026, 11, 5, 15, 0, tzinfo=UTC)
    assert snap.hold.hold_type == "vacation" and (snap.hold.start, snap.hold.end) == (ev.start, ev.end)


# --- Smart Away / Follow Me ------------------------------------------------------------


async def test_apply_settings_writes_one_thermostat_and_reads_back(cloud, fake):
    result = await cloud.apply_settings("main", {"autoAway": False}, "hand back")
    assert result.ok is True, result.error
    (post,) = fake.posts
    assert post == {"selection": {"selectionType": "thermostats", "selectionMatch": MAIN},
                    "thermostat": {"settings": {"autoAway": False}}}
    assert result.request == {"unit_key": "main", "settings": {"autoAway": False}, "reason": "hand back",
                              "identifier": MAIN}
    assert result.before == {"autoAway": True, "followMeComfort": False}
    assert result.readback == {"autoAway": False, "followMeComfort": False}
    # both, back on
    both = await cloud.apply_settings("main", {"autoAway": True, "followMeComfort": True}, "hand back")
    assert both.ok and both.readback == {"autoAway": True, "followMeComfort": True}
    assert fake.tstats[MAIN]["settings"]["followMeComfort"] is True
    # already as asked: nothing sent
    again = await cloud.apply_settings("main", {"followMeComfort": True}, "hand back")
    assert again.ok and again.request["noop"] is True and len(fake.posts) == 2


async def test_apply_settings_refuses_other_settings_and_reports_a_mismatch(cloud, fake, monkeypatch):
    for bad in ({"hvacMode": "off"}, {}, {"autoAway": "false"},
                {"autoAway": False, "holdAction": "indefinite"}):
        result = await cloud.apply_settings("main", bad, "x")
        assert result.ok is False and "nothing sent" in result.error, bad
    unmapped = await cloud.apply_settings("garage", {"autoAway": False}, "x")
    assert unmapped.ok is False and "not mapped" in unmapped.error
    assert fake.posts == []

    real_apply = fake._apply

    def ignore_settings(t, body):
        real_apply(t, {k: v for k, v in body.items() if k != "thermostat"})

    fake._apply = ignore_settings
    result = await cloud.apply_settings("main", {"autoAway": False}, "x")
    assert result.ok is False and "autoAway=false" in result.error
    assert result.readback == {"autoAway": True, "followMeComfort": False}
