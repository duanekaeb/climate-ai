"""climate.utility.skips with a fake source: owner skips, skip rules, refusals, retries, the
waits (controller off, cloud circuit open: only once the event is due, resolved when the cause
clears), the event's identity on the call, refusals that are not attempts (another event on
top, nothing running), and opt-outs that landed without a confirming read-back."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from climate.control.policy import HouseState, PolicyParams, RoomStatus, UnitStatus
from climate.sources.base import HoldInfo, ThermostatEvent, UnitSnapshot, WriteResult
from climate.store.app_settings import (
    ControlSettings,
    OccupancySettings,
    SourceSettings,
    UtilityEventSettings,
    put_setting,
)
from climate.store.orm import Alert, ControlAction, LiveUnit, UtilityEvent
from climate.utility import events, skips

START = datetime(2026, 7, 14, 20, 0, tzinfo=UTC)  # Tue 3:00 PM in Chicago
END = datetime(2026, 7, 14, 23, 0, tzinfo=UTC)
NOW = START + timedelta(minutes=20)
KEY = "link:L1"


def dr(*, running: bool = True, optional: bool | None = True) -> ThermostatEvent:
    return ThermostatEvent(event_type="demandResponse", name="Peak Saver", running=running, start=START, end=END,
                           is_relative=True, cool_offset_f=2.0, is_optional=optional, link_ref="L1")


def live_snapshot(unit: str = "up", *, running: bool = True, hold_type: str | None = "demandResponse",
                  source: str = "ecobee", hvac_mode: str = "cool") -> UnitSnapshot:
    hold = None
    if hold_type == "demandResponse":
        hold = HoldInfo(hold_type="demandResponse", start=START, end=END, event_name="Peak Saver", is_relative=True,
                        cool_offset_f=2.0, is_optional=True, link_ref="L1")
    elif hold_type is not None:
        hold = HoldInfo(hold_type=hold_type, start=START, end=END)
    return UnitSnapshot(unit_key=unit, ts=NOW, source=source, hvac_mode=hvac_mode, heat_sp_f=68.0, cool_sp_f=78.0,
                        hold=hold, events=[dr(running=running)] if source != "homekit" else [])


def setup_event(db, unit: str = "up", *, skip: str | None = "requested", by: str = "owner", running: bool = True,
                optional: bool | None = True, mode: str = "suggest", live: UnitSnapshot | None = None,
                act_units: list[str] | None = None) -> int:
    put_setting(db, "control", ControlSettings(mode=mode, act_units=act_units or ["main", "up", "bed"]))
    row = UtilityEvent(
        unit_key=unit, event_key=KEY, name="Peak Saver", status="running" if running else "announced",
        start_at=START, end_at=END, first_seen_at=START - timedelta(hours=3), last_seen_at=NOW,
        started_at=START if running else None, is_relative=True, cool_offset_f=2.0, is_optional=optional,
        skip=skip, skip_by=by if skip else None, skip_reason="Skipped from the app" if by == "owner" and skip else None,
        skip_requested_at=NOW if skip else None, detail=dr(running=running, optional=optional).model_dump(mode="json"),
    )
    db.add(row)
    snap = live or live_snapshot(unit, running=running)
    db.merge(LiveUnit(unit_key=unit, ts=snap.ts, source=snap.source, revision="r", snapshot=snap.model_dump(mode="json")))
    db.commit()
    return row.id


class FakeSource:
    def __init__(self, *results: WriteResult | Exception, kind: str = "ecobee") -> None:
        self.kind = kind
        self.results = list(results)
        self.calls: list[tuple[str, str]] = []
        self.identities: list[dict] = []  # the event each call named

    async def opt_out_event(self, unit_key: str, reason: str, *, link_ref: str | None = None,
                            name: str | None = None, start: datetime | None = None) -> WriteResult:
        self.calls.append((unit_key, reason))
        self.identities.append({"link_ref": link_ref, "name": name, "start": start})
        r = self.results.pop(0) if self.results else ok()
        if isinstance(r, Exception):
            raise r
        return r


def ok() -> WriteResult:
    return WriteResult(ok=True, channel="ecobee", before={"demand_response_running": True},
                       request={"action": "resumeProgram"}, readback={"demand_response_running": False})


def failed(error: str = "HTTP 500 from ecobee") -> WriteResult:
    return WriteResult(ok=False, channel="ecobee", request={"action": "resumeProgram"}, error=error)


def mandatory() -> WriteResult:
    return WriteResult(ok=False, channel="ecobee", request={"refused": True, "mandatory": True},
                       error="the utility event 'Peak Saver' is mandatory: ecobee does not allow opting out of it")


def other_event() -> WriteResult:
    """The fresh read shows a different utility event on top (the snapshot was older)."""
    return WriteResult(ok=False, channel="ecobee", before={"demand_response_running": True},
                       request={"refused": True, "mandatory": False, "other_event": True, "link_ref": "L2"},
                       error="a different utility event is on top; nothing sent")


def nothing_running() -> WriteResult:
    return WriteResult(ok=False, channel="ecobee", before={"demand_response_running": False},
                       request={"refused": True, "mandatory": False},
                       error="no utility event to opt out of (nothing is running); nothing sent")


def unconfirmed() -> WriteResult:
    """The resumeProgram went out, but no read-back GET answered."""
    return WriteResult(ok=False, channel="ecobee", before={"demand_response_running": True},
                       request={"function": {"type": "resumeProgram"}, "sent": True},
                       error="resumeProgram sent; read-back failed (ecobee thermostat request failed (ReadTimeout))")


def gone_snapshot(at: datetime, unit: str = "up") -> UnitSnapshot:
    """The next poll: the event is no longer listed, nothing on top."""
    return live_snapshot(unit, hold_type=None).model_copy(update={"events": [], "ts": at})


def event(db, event_id: int) -> UtilityEvent:
    db.expire_all()
    return db.get(UtilityEvent, event_id)


def actions(db) -> list[ControlAction]:
    db.expire_all()
    return list(db.execute(select(ControlAction).order_by(ControlAction.id)).scalars())


def alert(db, phase: str) -> Alert | None:
    db.expire_all()
    return db.execute(select(Alert).where(Alert.dedupe_key == events.alert_key(KEY, phase))).scalars().first()


def n_alerts(db, phase: str) -> int:
    db.expire_all()
    return len(db.execute(select(Alert.id).where(Alert.dedupe_key == events.alert_key(KEY, phase))).all())


# --- owner skips ---------------------------------------------------------------------------


async def test_owner_skip_runs_in_suggest_mode_and_is_logged(db):
    eid = setup_event(db, mode="suggest")
    src = FakeSource(ok())
    ids = await skips.run(src, NOW)
    assert len(src.calls) == 1 and src.calls[0][0] == "up"
    (act,) = actions(db)
    assert ids == [act.id]
    assert (act.actor, act.mode, act.channel, act.action, act.rule, act.status) == (
        "owner", "act", "ecobee", "opt_out_event", "utility_opt_out", "verified")
    assert act.request["kind"] == "utility_opt_out" and act.request["event_id"] == eid and act.request["by"] == "owner"
    assert act.readback == {"demand_response_running": False} and act.readback_ok is True
    assert act.before["hold"]["hold_type"] == "demandResponse" and act.completed_at == NOW
    assert act.reason.startswith("Skipped from the app: opting out of the utility event “Peak Saver” on Upstairs")
    row = event(db, eid)
    assert (row.skip, row.status, row.skip_done_at, row.skip_action_id, row.ended_at) == (
        "done", "opted_out", NOW, act.id, NOW)
    assert row.detail["skip_attempts"] == 1
    a = alert(db, "skipped")
    assert a.level == "info" and a.title == "Skipped the utility event on Upstairs"
    assert "ecobee recorded the opt-out" in a.body
    assert await skips.run(src, NOW + timedelta(minutes=3)) == [] and len(src.calls) == 1


async def test_the_sent_row_is_committed_before_the_call(db):
    setup_event(db, mode="act")
    seen: list[str] = []

    class Peek(FakeSource):
        async def opt_out_event(self, unit_key, reason, **kw):
            seen.extend(a.status for a in actions(db))
            return await super().opt_out_event(unit_key, reason, **kw)

    await skips.run(Peek(), NOW)
    assert seen == ["sent"]


async def test_an_announced_event_waits_until_it_runs(db):
    eid = setup_event(db, running=False, live=live_snapshot(running=False, hold_type=None))
    src = FakeSource()
    assert await skips.run(src, START - timedelta(minutes=30)) == []
    assert src.calls == [] and event(db, eid).skip == "requested"


async def test_waits_when_another_event_is_on_top(db):
    eid = setup_event(db, live=live_snapshot(hold_type="vacation"))
    src = FakeSource()
    assert await skips.run(src, NOW) == []
    assert src.calls == [] and event(db, eid).skip == "requested"


async def test_waits_on_a_homekit_snapshot(db):
    eid = setup_event(db, live=live_snapshot(source="homekit", hold_type=None))
    src = FakeSource()
    assert await skips.run(src, NOW) == [] and src.calls == [] and event(db, eid).skip == "requested"


# --- refusals, retries, waits ----------------------------------------------------------------


async def test_mandatory_refusal(db):
    eid = setup_event(db)
    await skips.run(FakeSource(mandatory()), NOW)
    row = event(db, eid)
    (act,) = actions(db)
    assert row.skip == "refused" and row.status == "running" and row.skip_action_id == act.id and row.is_optional is False
    assert act.status == "failed" and "mandatory" in act.error
    a = alert(db, "skip_refused")
    assert a.level == "warn" and "mandatory" in a.body


async def test_a_known_mandatory_event_is_refused_without_a_call(db):
    eid = setup_event(db, optional=False)
    src = FakeSource()
    assert await skips.run(src, NOW) == []
    assert src.calls == [] and actions(db) == [] and event(db, eid).skip == "refused"
    assert alert(db, "skip_refused") is not None


async def test_retries_three_times_then_fails(db):
    eid = setup_event(db)
    src = FakeSource(failed(), RuntimeError("connection reset token=abc123"), failed("HTTP 503"))
    for i in range(1, 3):
        await skips.run(src, NOW + timedelta(minutes=3 * i))
        row = event(db, eid)
        assert row.skip == "requested" and row.detail["skip_attempts"] == i
        assert alert(db, "skip_failed") is None
    await skips.run(src, NOW + timedelta(minutes=9))
    row = event(db, eid)
    acts = actions(db)
    assert row.skip == "failed" and row.skip_action_id == acts[-1].id and row.status == "running"
    assert [a.status for a in acts] == ["failed"] * 3
    assert "token=[redacted]" in acts[1].error and "abc123" not in acts[1].error
    a = alert(db, "skip_failed")
    assert a.level == "error" and "3 times" in a.body and "HTTP 503" in a.body
    assert await skips.run(src, NOW + timedelta(minutes=12)) == [] and len(src.calls) == 3


async def test_mode_off_waits_and_says_so_once(db):
    eid = setup_event(db, mode="off")
    src = FakeSource()
    for i in range(2):
        assert await skips.run(src, NOW + timedelta(minutes=3 * i)) == []
    assert src.calls == [] and event(db, eid).skip == "requested"
    assert n_alerts(db, "skip_waiting_off") == 1 and alert(db, "skip_waiting_off").level == "info"

    put_setting(db, "control", ControlSettings(mode="suggest"))
    db.commit()
    await skips.run(src, NOW + timedelta(minutes=9))
    assert event(db, eid).skip == "done" and alert(db, "skip_waiting_off").resolved_at is not None


async def test_cloud_circuit_open_waits_and_warns_once(db):
    eid = setup_event(db)
    put_setting(db, "source", SourceSettings(kind="ecobee", homekit_enabled=True,
                                             cloud_circuit_open_until=NOW + timedelta(minutes=15)))
    db.commit()
    src = FakeSource()
    await skips.run(src, NOW)
    await skips.run(src, NOW + timedelta(minutes=3))
    assert src.calls == [] and event(db, eid).skip == "requested"
    assert n_alerts(db, "skip_waiting_cloud") == 1 and alert(db, "skip_waiting_cloud").level == "warn"
    await skips.run(src, NOW + timedelta(minutes=16))  # the circuit has closed
    assert event(db, eid).skip == "done"


async def test_no_source_waits(db):
    eid = setup_event(db)
    assert await skips.run(None, NOW) == [] and event(db, eid).skip == "requested"


async def test_owner_unskip_during_a_failed_call_is_kept(db):
    eid = setup_event(db)

    class Unskip(FakeSource):
        async def opt_out_event(self, unit_key, reason, **kw):
            row = event(db, eid)
            row.skip, row.skip_by, row.skip_reason = None, None, None
            db.commit()
            return failed()

    await skips.run(Unskip(), NOW)
    assert event(db, eid).skip is None


def open_alerts(db, phase: str) -> int:
    db.expire_all()
    return len(db.execute(select(Alert.id).where(Alert.dedupe_key == events.alert_key(KEY, phase),
                                                 Alert.resolved_at.is_(None))).all())


def circuit(db, until: datetime | None) -> None:
    put_setting(db, "source", SourceSettings(kind="ecobee", homekit_enabled=True, cloud_circuit_open_until=until))
    db.commit()


async def test_waits_say_nothing_before_the_event_is_due(db):
    """A skip requested ahead of the event: the controller being off or a cloud outage is not
    worth an alert yet (nothing could be sent anyway). Once its start has passed it is."""
    eid = setup_event(db, running=False, mode="off", live=live_snapshot(running=False, hold_type=None))
    circuit(db, START + timedelta(hours=1))
    await skips.run(FakeSource(), START - timedelta(hours=2))
    assert alert(db, "skip_waiting_off") is None and alert(db, "skip_waiting_cloud") is None
    put_setting(db, "control", ControlSettings(mode="act"))
    db.commit()
    await skips.run(FakeSource(), START - timedelta(hours=1))
    assert alert(db, "skip_waiting_cloud") is None and event(db, eid).skip == "requested"
    await skips.run(FakeSource(), START + timedelta(minutes=1))  # the snapshot still says announced
    assert open_alerts(db, "skip_waiting_cloud") == 1


async def test_a_wait_is_resolved_when_its_cause_clears_and_raised_again_when_it_returns(db):
    eid = setup_event(db, live=live_snapshot(hold_type=None))  # running, not on top yet: nothing to send
    src = FakeSource()
    circuit(db, NOW + timedelta(minutes=15))
    await skips.run(src, NOW)
    assert open_alerts(db, "skip_waiting_cloud") == 1
    circuit(db, None)  # the cloud is back; the skip still waits for the event to be on top
    await skips.run(src, NOW + timedelta(minutes=3))
    assert open_alerts(db, "skip_waiting_cloud") == 0 and n_alerts(db, "skip_waiting_cloud") == 1
    circuit(db, NOW + timedelta(minutes=21))  # down again: said again
    await skips.run(src, NOW + timedelta(minutes=6))
    assert open_alerts(db, "skip_waiting_cloud") == 1 and n_alerts(db, "skip_waiting_cloud") == 2
    circuit(db, None)

    put_setting(db, "control", ControlSettings(mode="off"))
    db.commit()
    await skips.run(src, NOW + timedelta(minutes=9))
    assert open_alerts(db, "skip_waiting_off") == 1 and open_alerts(db, "skip_waiting_cloud") == 0
    put_setting(db, "control", ControlSettings(mode="act"))
    db.commit()
    await skips.run(src, NOW + timedelta(minutes=12))
    assert open_alerts(db, "skip_waiting_off") == 0
    assert src.calls == [] and event(db, eid).skip == "requested"


async def test_nothing_is_sent_once_the_event_is_over_by_the_clock(db):
    eid = setup_event(db)  # the snapshot still shows it on top (it lags)
    src = FakeSource()
    assert await skips.run(src, END + timedelta(minutes=1)) == [] and src.calls == []
    assert event(db, eid).skip == "requested"


# --- the event's identity, refusals that are no attempt, opt-outs that landed -----------------


async def test_the_call_names_the_event(db):
    setup_event(db)
    src = FakeSource()
    await skips.run(src, NOW)
    assert src.identities == [{"link_ref": "L1", "name": "Peak Saver", "start": START}]


async def test_a_source_without_the_identity_is_called_once_without_it(db):
    eid = setup_event(db)

    class Old(FakeSource):
        async def opt_out_event(self, unit_key, reason):  # the contract before the identity
            self.calls.append((unit_key, reason))
            return ok()

    src = Old()
    await skips.run(src, NOW)
    assert len(src.calls) == 1 and event(db, eid).skip == "done"


async def test_another_event_on_top_is_not_an_attempt(db):
    """The snapshot showed L1 on top; on the thermostat L2 is on top by now. The source refuses
    and sends nothing: no failed attempt, however often it happens; the skip waits for the next
    snapshot. When that shows L1 gone with no opt-out of it sent, it simply ended."""
    eid = setup_event(db)
    tries = skips.MAX_ATTEMPTS + 1
    src = FakeSource(*(other_event() for _ in range(tries)))
    for i in range(tries):
        await skips.run(src, NOW + timedelta(minutes=3 * i))
    row = event(db, eid)
    assert len(src.calls) == tries and {a.status for a in actions(db)} == {"failed"}
    assert (row.skip, row.status, row.detail["skip_attempts"]) == ("requested", "running", 0)
    assert alert(db, "skip_failed") is None and alert(db, "skipped") is None
    later = NOW + timedelta(minutes=13)
    events.ingest(db, [gone_snapshot(later)], later)
    db.commit()
    assert (event(db, eid).status, event(db, eid).skip) == ("ended", "requested")


async def test_an_unconfirmed_opt_out_is_settled_when_the_event_vanishes(db):
    """resumeProgram went out but no read-back answered; the next poll no longer lists the
    event: it landed. The row is 'opted_out' with the skip 'done' and the "Skipped" alert, not
    'ended' with the skip stuck 'requested'."""
    eid = setup_event(db)
    await skips.run(FakeSource(unconfirmed()), NOW)
    row = event(db, eid)
    (act,) = actions(db)
    assert (row.skip, row.status, row.detail["skip_attempts"], act.status) == ("requested", "running", 1, "failed")
    later = NOW + timedelta(minutes=3)
    events.ingest(db, [gone_snapshot(later)], later)
    db.commit()
    row = event(db, eid)
    assert (row.status, row.skip, row.skip_action_id, row.skip_done_at) == ("opted_out", "done", act.id, later)
    assert alert(db, "skipped") is not None and alert(db, "ended") is None
    assert "the opt-out landed" in actions(db)[0].error


async def test_ticks_before_the_poll_do_not_count_against_a_landed_opt_out(db):
    """The ticks run before the poll catches up: each fresh read finds nothing running (our
    opt-out landed). No attempt is counted, so no false "failed 3 times"; the poll settles it."""
    eid = setup_event(db)
    src = FakeSource(unconfirmed(), nothing_running(), nothing_running(), nothing_running())
    for i in range(4):
        await skips.run(src, NOW + timedelta(minutes=3 * i))
    row = event(db, eid)
    assert (row.skip, row.detail["skip_attempts"]) == ("requested", 1) and alert(db, "skip_failed") is None
    later = NOW + timedelta(minutes=10)
    events.ingest(db, [gone_snapshot(later)], later)
    db.commit()
    row = event(db, eid)
    assert (row.status, row.skip, row.skip_action_id) == ("opted_out", "done", actions(db)[0].id)


async def test_an_event_closed_during_the_call_is_settled_by_its_result(db):
    """The poll runs while the opt-out is in flight and no longer lists the event: ingest closes
    the row as 'ended' (nothing confirmed yet); the unconfirmed result then settles it 'done'."""
    eid = setup_event(db)
    later = NOW + timedelta(minutes=1)

    class PollMidCall(FakeSource):
        async def opt_out_event(self, unit_key, reason, **kw):
            events.ingest(db, [gone_snapshot(later)], later)
            db.commit()
            assert event(db, eid).status == "ended"
            return unconfirmed()

    await skips.run(PollMidCall(), NOW)
    assert (event(db, eid).status, event(db, eid).skip) == ("opted_out", "done")


async def test_an_old_or_overruled_opt_out_does_not_explain_a_vanished_event(db):
    end_guard = WriteResult(ok=False, channel="ecobee", before={"demand_response_running": True},
                            request={"refused": True, "mandatory": False, "link_ref": "L1"},
                            error="the utility event 'Peak Saver' ends within 2 minutes; nothing sent")
    eid = setup_event(db)  # a later attempt still found the event on top: the first did not land
    await skips.run(FakeSource(unconfirmed()), NOW)
    await skips.run(FakeSource(end_guard), NOW + timedelta(minutes=3))
    later = NOW + timedelta(minutes=5)
    events.ingest(db, [gone_snapshot(later)], later)
    db.commit()
    assert (event(db, eid).status, event(db, eid).skip) == ("ended", "requested")

    db.query(UtilityEvent).delete()
    db.query(ControlAction).delete()
    db.commit()
    eid = setup_event(db)  # the opt-out was too long ago to explain it
    await skips.run(FakeSource(unconfirmed()), NOW)
    later = NOW + skips.LANDED_WITHIN + timedelta(minutes=1)
    events.ingest(db, [gone_snapshot(later)], later)
    db.commit()
    assert (event(db, eid).status, event(db, eid).skip) == ("ended", "requested")


async def test_the_sweep_settles_a_landed_opt_out_too(db):
    eid = setup_event(db)  # no poll after the opt-out: the clock closes the row
    late = END - timedelta(minutes=3)
    await skips.run(FakeSource(unconfirmed()), late)
    events.sweep(db, END + events.RUNNING_GRACE + timedelta(seconds=30))
    db.commit()
    row = event(db, eid)
    assert (row.status, row.skip) == ("opted_out", "done")


# --- rules ------------------------------------------------------------------------------------


def house(unit: str = "up", *, temp: float | None = 79.5, state: str = "occupied", hvac_mode: str = "cool",
          call: str = "cool", mode: str = "act") -> HouseState:
    snap = live_snapshot(unit, hvac_mode=hvac_mode)
    room = RoomStatus(room_key="girls_room", name="Girls' Room", unit_key="up", floor="upstairs", has_sensor=True,
                      is_sleep_room=True, has_comfort_target=True, temp_f=temp, state=state)  # type: ignore[arg-type]
    toy = RoomStatus(room_key="toy_room", name="Toy Room", unit_key="up", floor="upstairs", has_sensor=True,
                     is_sleep_room=False, has_comfort_target=True, temp_f=77.0, state="empty")
    return HouseState(
        now=NOW, tz="America/Chicago",
        units={unit: UnitStatus(unit_key=unit, name="Upstairs", snapshot=snap, call=call)},  # type: ignore[arg-type]
        rooms={"girls_room": room, "toy_room": toy}, control=ControlSettings(mode=mode),  # type: ignore[arg-type]
        occupancy=OccupancySettings(), policy=PolicyParams(),
    )


@pytest.fixture
def state(monkeypatch):
    import climate.state

    holder = {"state": house(), "calls": 0}

    def load(session, now=None):
        holder["calls"] += 1
        return holder["state"]

    monkeypatch.setattr(climate.state, "load_house_state", load)
    return holder


def rules(db, **kw) -> None:
    put_setting(db, "utility_events", UtilityEventSettings(auto_skip=True, skip_when_asleep=False, **kw))
    db.commit()


async def test_rule_skip_in_act_mode_requests_and_sends(db, state):
    eid = setup_event(db, skip=None, mode="act")
    rules(db, skip_above_f=79.0)
    src = FakeSource(ok())
    ids = await skips.run(src, NOW)
    row = event(db, eid)
    assert (row.skip, row.skip_by, row.skip_reason, row.skip_requested_at) == (
        "done", "rule", "Girls' Room reached 79.5°F", NOW)
    (act,) = actions(db)
    assert ids == [act.id] and act.actor == "controller" and act.request["by"] == "rule"
    assert act.reason.startswith("Girls' Room reached 79.5°F (your skip rule): opting out")


async def test_a_rule_asks_once_so_the_owners_undo_sticks(db, state):
    eid = setup_event(db, skip=None, mode="act", live=live_snapshot(hold_type=None))  # not on top: waits
    rules(db, skip_above_f=79.0)
    await skips.run(FakeSource(), NOW)
    row = event(db, eid)
    assert row.skip == "requested" and row.detail["rule_requested_at"] == NOW.isoformat()
    row.skip = row.skip_by = row.skip_reason = row.skip_requested_at = None  # the owner's "undo"
    db.commit()
    await skips.run(FakeSource(), NOW + timedelta(minutes=3))
    assert event(db, eid).skip is None and state["calls"] == 1


async def test_rule_skip_in_suggest_mode_only_alerts_once(db, state):
    eid = setup_event(db, skip=None, mode="suggest")
    rules(db, skip_above_f=79.0)
    state["state"] = house(mode="suggest")
    src = FakeSource()
    await skips.run(src, NOW)
    await skips.run(src, NOW + timedelta(minutes=3))
    assert src.calls == [] and event(db, eid).skip is None
    assert n_alerts(db, "rule:up") == 1 and state["calls"] == 1  # settled: the house state isn't reloaded
    a = alert(db, "rule:up")
    assert a.title == "Your skip rule matched on Upstairs" and a.level == "info"
    assert a.body.startswith("Girls' Room reached 79.5°F during the utility event “Peak Saver” (Tue 3:00–6:00 PM).")
    assert "In Suggest mode the app doesn't skip on its own; tap Skip on Live." in a.body


async def test_rule_on_a_suggest_only_unit_alerts(db, state):
    eid = setup_event(db, skip=None, mode="act", act_units=["main"])
    rules(db, skip_above_f=79.0)
    src = FakeSource()
    await skips.run(src, NOW)
    assert src.calls == [] and event(db, eid).skip is None
    assert "Upstairs is suggest-only" in alert(db, "rule:up").body


async def test_rule_requested_skip_waits_in_suggest_mode(db, state):
    eid = setup_event(db, by="rule", mode="suggest")
    src = FakeSource()
    assert await skips.run(src, NOW) == [] and src.calls == [] and event(db, eid).skip == "requested"


async def test_rules_need_cooling_for_the_heat_rule_and_never_invent_a_temperature(db, state):
    eid = setup_event(db, skip=None, mode="act")
    rules(db, skip_above_f=79.0)
    state["state"] = house(hvac_mode="heat", call="heat")
    await skips.run(FakeSource(), NOW)
    assert event(db, eid).skip is None
    state["state"] = house(temp=None)  # no reading: nothing to compare
    await skips.run(FakeSource(), NOW)
    assert event(db, eid).skip is None
    state["state"] = house(state="empty")  # nobody there
    await skips.run(FakeSource(), NOW)
    assert event(db, eid).skip is None


async def test_heating_rule_and_asleep_rule(db, state):
    eid = setup_event(db, skip=None, mode="act")
    rules(db, skip_below_f=62.0)
    state["state"] = house(temp=61.5, hvac_mode="heat", call="idle")
    await skips.run(FakeSource(ok()), NOW)
    assert event(db, eid).skip_reason == "Girls' Room fell to 61.5°F"

    db.query(UtilityEvent).delete()
    db.query(ControlAction).delete()
    db.commit()
    eid = setup_event(db, skip=None, mode="act")
    put_setting(db, "utility_events", UtilityEventSettings(auto_skip=True, skip_when_asleep=True))
    db.commit()
    state["state"] = house(temp=72.0, state="asleep")
    await skips.run(FakeSource(ok()), NOW)
    assert event(db, eid).skip_reason == "Someone is asleep in the Girls' Room"


async def test_rules_skip_mandatory_events_and_mode_off(db, state):
    eid = setup_event(db, skip=None, mode="act", optional=False)
    rules(db, skip_above_f=79.0)
    await skips.run(FakeSource(), NOW)
    assert event(db, eid).skip is None and state["calls"] == 0
    db.query(UtilityEvent).delete()
    db.commit()
    eid = setup_event(db, skip=None, mode="off")
    await skips.run(FakeSource(), NOW)
    assert event(db, eid).skip is None and state["calls"] == 0


async def test_rules_look_at_events_starting_within_five_minutes(db, state):
    eid = setup_event(db, skip=None, mode="act", running=False, live=live_snapshot(running=False, hold_type=None))
    rules(db, skip_above_f=79.0)
    await skips.run(FakeSource(), START - timedelta(minutes=10))
    assert event(db, eid).skip is None and state["calls"] == 0
    await skips.run(FakeSource(), START - timedelta(minutes=4))
    row = event(db, eid)
    assert row.skip == "requested" and row.skip_by == "rule"  # sent once the event runs


def test_direction_in_auto_mode():
    st = house(hvac_mode="auto", call="idle")
    row = UtilityEvent(unit_key="up", is_relative=True, cool_offset_f=2.0, detail={})
    assert skips.direction(st, row) == "cool"
    row = UtilityEvent(unit_key="up", is_relative=True, heat_offset_f=-2.0, detail={})
    assert skips.direction(st, row) == "heat"
    row = UtilityEvent(unit_key="up", is_relative=False, cool_f=78.0, heat_f=64.0, detail={})
    st.outdoor_temp_f = 90.0
    assert skips.direction(st, row) == "cool"
    st.outdoor_temp_f = 40.0
    assert skips.direction(st, row) == "heat"
    assert skips.direction(house(hvac_mode="off"), row) is None


# --- end to end with the simulator ------------------------------------------------------------


async def test_simulator_event_ingested_skipped_and_opted_out(db):
    """Announced on the simulator -> ingested -> the owner skips -> it starts -> ``run`` opts
    out through the simulator's ``opt_out_event`` -> the next poll shows it gone."""
    from climate.collector.ingest import ingest_snapshots
    from climate.sources.simulator import SimulatedHouse

    t0 = datetime(2026, 7, 15, 17, 2, tzinfo=UTC)
    clock = {"t": t0}
    house = SimulatedHouse(seed=7, clock=lambda: clock["t"])
    house.advance_to(t0)
    await house.apply_settings("up", {"autoAway": False}, "steady program")
    start = t0 + timedelta(minutes=30)
    house.inject_event("up", ThermostatEvent(event_type="demandResponse", name="Peak Saver", start=start,
                                             end=start + timedelta(hours=2), is_relative=True, cool_offset_f=2.0,
                                             is_optional=True, link_ref="sim-1"))
    put_setting(db, "control", ControlSettings(mode="suggest"))
    db.commit()

    async def poll(t: datetime) -> list[UnitSnapshot]:
        clock["t"] = t
        house.advance_to(t)
        snaps = await house.fetch_snapshots(["up"])
        ingest_snapshots(db, snaps)
        events.ingest(db, snaps, t)
        db.commit()
        return snaps

    await poll(t0 + timedelta(minutes=3))
    row = db.execute(select(UtilityEvent)).scalar_one()
    assert row.status == "announced" and row.event_key == "link:sim-1"
    row.skip, row.skip_by, row.skip_reason, row.skip_requested_at = "requested", "owner", skips.OWNER_REASON, t0
    db.commit()

    assert await skips.run(house, t0 + timedelta(minutes=4)) == []  # not running yet: waits
    await poll(start + timedelta(minutes=2))
    assert event(db, row.id).status == "running"
    ids = await skips.run(house, start + timedelta(minutes=3))
    assert len(ids) == 1
    act = db.get(ControlAction, ids[0])
    db.refresh(act)
    assert act.status == "verified" and act.channel == "simulator"
    assert event(db, row.id).status == "opted_out" and event(db, row.id).skip == "done"
    (s,) = await poll(start + timedelta(minutes=6))
    assert not any(ev.running for ev in s.events) and (s.hold is None or s.hold.hold_type != "demandResponse")
    assert event(db, row.id).status == "opted_out"
