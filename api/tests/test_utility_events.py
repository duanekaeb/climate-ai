"""climate.utility.events: ingest (the row state machine), the clock sweep, grouped alerts,
the unknown-event warning, and the poller wiring."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from climate.collector import poller
from climate.sources.base import HoldInfo, SensorReading, ThermostatEvent, UnitSnapshot
from climate.store.app_settings import UtilityEventSettings, put_setting
from climate.store.orm import Alert, LiveUnit, UtilityEvent
from climate.utility import events

# Tue 14 Jul 2026, 1:00 PM in Chicago (CDT, UTC-5); the event runs 3:00-6:00 PM.
T0 = datetime(2026, 7, 14, 18, 0, tzinfo=UTC)
START = datetime(2026, 7, 14, 20, 0, tzinfo=UTC)
END = datetime(2026, 7, 14, 23, 0, tzinfo=UTC)


def dr(*, running: bool = False, link: str | None = "L1", name: str | None = "Peak Saver", start=START, end=END,
       cool_offset: float | None = 2.0, optional: bool | None = True) -> ThermostatEvent:
    return ThermostatEvent(event_type="demandResponse", name=name, running=running, start=start, end=end,
                           is_relative=cool_offset is not None, cool_offset_f=cool_offset, is_optional=optional,
                           link_ref=link)


def snap(unit: str, ts: datetime, evs: list[ThermostatEvent] | None = None, *, hold: HoldInfo | None = None,
         source: str = "ecobee") -> UnitSnapshot:
    return UnitSnapshot(unit_key=unit, ts=ts, source=source, hvac_mode="cool", heat_sp_f=68.0, cool_sp_f=76.0,
                        hold=hold, events=evs or [])


def dr_hold() -> HoldInfo:
    return HoldInfo(hold_type="demandResponse", start=START, end=END, event_name="Peak Saver", is_relative=True,
                    cool_offset_f=2.0, is_optional=True, link_ref="L1")


def ingest(db, *snaps: UnitSnapshot, now: datetime | None = None) -> list[int]:
    ids = events.ingest(db, list(snaps), now or max(s.ts for s in snaps))
    db.commit()
    return ids


def rows(db) -> list[UtilityEvent]:
    db.expire_all()
    return list(db.execute(select(UtilityEvent).order_by(UtilityEvent.unit_key)).scalars())


def alert(db, phase: str, key: str = "link:L1") -> Alert | None:
    db.expire_all()
    return db.execute(
        select(Alert).where(Alert.dedupe_key == events.alert_key(key, phase)).order_by(Alert.id.desc())
    ).scalars().first()


def alert_count(db, dedupe_key: str) -> int:
    db.expire_all()
    return len(db.execute(select(Alert.id).where(Alert.dedupe_key == dedupe_key)).all())


# --- the state machine ---------------------------------------------------------------------


def test_announced_running_ended_with_one_alert_per_phase(db):
    ingest(db, snap("up", T0, [dr()]))
    (row,) = rows(db)
    assert row.status == "announced" and row.event_key == "link:L1" and row.started_at is None
    assert row.start_at == START and row.end_at == END and row.cool_offset_f == 2.0 and row.is_relative
    assert row.first_seen_at == T0 and row.detail["link_ref"] == "L1"
    a = alert(db, "announced")
    assert a is not None and a.resolved_at is None and a.level == "info" and a.kind == "utility_event"
    assert a.title == "Utility event announced: Peak Saver"
    assert a.body.startswith("Upstairs, Tue 3:00–6:00 PM, cooling +2°F. The app stands aside during it")
    assert "you can skip it from Live" in a.body

    ingest(db, snap("up", T0 + timedelta(minutes=3), [dr()]))  # nothing new: no second alert
    assert alert_count(db, events.alert_key("link:L1", "announced")) == 1

    t1 = START + timedelta(minutes=4)
    ingest(db, snap("up", t1, [dr(running=True)], hold=dr_hold()))
    (row,) = rows(db)
    assert row.status == "running" and row.started_at == t1 and row.last_seen_at == t1
    assert alert(db, "announced").resolved_at is not None
    started = alert(db, "running")
    assert started.resolved_at is None and started.title == "Utility event started: Peak Saver"
    assert started.body.startswith("Upstairs, until 6:00 PM, cooling +2°F.")

    t2 = END + timedelta(minutes=2)
    ingest(db, snap("up", t2, []))
    (row,) = rows(db)
    assert row.status == "ended" and row.ended_at == t2 and row.started_at == t1
    assert alert(db, "running").resolved_at is not None
    ended = alert(db, "ended")
    assert ended.resolved_at is None and ended.body == "The utility event on upstairs ended at 6:00 PM."

    # The sweep keeps the 'ended' news for 12 hours, then resolves it; it never comes back.
    events.sweep(db, t2 + timedelta(hours=11))
    db.commit()
    assert alert(db, "ended").resolved_at is None
    events.sweep(db, t2 + timedelta(hours=13))
    db.commit()
    assert alert(db, "ended").resolved_at is not None
    assert alert_count(db, events.alert_key("link:L1", "ended")) == 1


def test_same_event_on_two_units_is_one_alert_naming_both(db):
    ingest(db, snap("up", T0, [dr()]), snap("main", T0, [dr()]))
    assert [r.unit_key for r in rows(db)] == ["main", "up"]
    a = alert(db, "announced")
    assert a.body.startswith("Main floor and upstairs, Tue 3:00–6:00 PM")
    assert alert_count(db, events.alert_key("link:L1", "announced")) == 1


def test_a_unit_that_joins_later_is_added_to_the_open_alert(db):
    ingest(db, snap("up", T0, [dr()]))
    assert alert(db, "announced").body.startswith("Upstairs, ")
    ingest(db, snap("bed", T0 + timedelta(minutes=3), [dr()]))
    a = alert(db, "announced")
    assert a.body.startswith("Upstairs and bed / office wing, ")
    assert alert_count(db, events.alert_key("link:L1", "announced")) == 1


def test_running_row_gone_after_our_skip_is_opted_out(db):
    ingest(db, snap("up", START + timedelta(minutes=1), [dr(running=True)], hold=dr_hold()))
    (row,) = rows(db)
    row.skip = "done"
    db.commit()
    t = START + timedelta(minutes=40)
    ingest(db, snap("up", t, []))
    (row,) = rows(db)
    assert row.status == "opted_out" and row.ended_at == t
    assert alert(db, "running").resolved_at is not None
    assert alert(db, "ended") is None  # the skip alert says so; no 'ended' for an opt-out


def test_an_opt_out_is_final_even_if_a_late_snapshot_still_lists_it(db):
    ingest(db, snap("up", START + timedelta(minutes=1), [dr(running=True)], hold=dr_hold()))
    (row,) = rows(db)
    row.status, row.skip, row.ended_at = "opted_out", "done", START + timedelta(minutes=30)
    db.commit()
    ingest(db, snap("up", START + timedelta(minutes=33), [dr(running=True)], hold=dr_hold()))
    assert rows(db)[0].status == "opted_out"


def test_announced_row_gone_before_its_start_is_cancelled_else_ended(db):
    ingest(db, snap("up", T0, [dr()]), snap("main", T0, [dr(link="L2", name="Later", start=END, end=END + timedelta(hours=2))]))
    ingest(db, snap("up", T0 + timedelta(minutes=5), []))
    up = next(r for r in rows(db) if r.unit_key == "up")
    assert up.status == "cancelled" and up.ended_at == T0 + timedelta(minutes=5)
    assert alert(db, "announced").resolved_at is not None
    c = alert(db, "cancelled")
    assert c.level == "info" and c.title == "Utility event cancelled: Peak Saver"
    assert "Tue 3:00–6:00 PM on upstairs" in c.body

    # announced, its start passed without a running sighting, then gone: it ended
    after = END + timedelta(minutes=5)
    ingest(db, snap("main", after, []))
    main = next(r for r in rows(db) if r.unit_key == "main")
    assert main.status == "ended" and main.started_at is None


def test_a_cancelled_event_listed_again_is_announced_again(db):
    ingest(db, snap("up", T0, [dr()]))
    ingest(db, snap("up", T0 + timedelta(minutes=3), []))
    assert rows(db)[0].status == "cancelled"
    ingest(db, snap("up", T0 + timedelta(minutes=6), [dr()]))
    row = rows(db)[0]
    assert row.status == "announced" and row.ended_at is None


def test_homekit_snapshots_are_ignored(db):
    ingest(db, snap("up", T0, [dr()], source="homekit"))
    assert rows(db) == []
    ingest(db, snap("up", T0, [dr()]))
    ingest(db, snap("up", T0 + timedelta(minutes=5), [], source="homekit"))  # shows no events, ends nothing
    assert rows(db)[0].status == "announced"


def test_an_older_snapshot_changes_nothing(db):
    ingest(db, snap("up", START + timedelta(minutes=5), [dr(running=True)], hold=dr_hold()))
    ingest(db, snap("up", START, []))
    assert rows(db)[0].status == "running"


def test_a_past_event_seen_for_the_first_time_is_not_stored(db):
    ingest(db, snap("up", END + timedelta(minutes=1), [dr()]))
    assert rows(db) == []


def test_skip_attempts_survive_a_snapshot_update(db):
    ingest(db, snap("up", T0, [dr()]))
    row = rows(db)[0]
    row.detail = {**row.detail, "skip_attempts": 2}
    db.commit()
    ingest(db, snap("up", T0 + timedelta(minutes=3), [dr(cool_offset=3.0)]))
    row = rows(db)[0]
    assert row.detail["skip_attempts"] == 2 and row.detail["cool_offset_f"] == 3.0 and row.cool_offset_f == 3.0


def test_mandatory_event_says_it_cannot_be_skipped(db):
    ingest(db, snap("up", T0, [dr(optional=False)]))
    body = alert(db, "announced").body
    assert "this event is mandatory, so it can't be skipped" in body and "skip it from Live" not in body


def test_events_without_a_link_ref_key_on_name_and_start(db):
    ingest(db, snap("up", T0, [dr(link=None)]))
    assert rows(db)[0].event_key == "demandResponse:Peak Saver:2026-07-14T20:00Z"


# --- the sweep -----------------------------------------------------------------------------


def test_sweep_closes_by_the_clock(db):
    ingest(
        db,
        snap("up", START + timedelta(minutes=1), [dr(running=True)], hold=dr_hold()),
        snap("main", T0, [dr(link="L2", name="Short", start=T0 + timedelta(hours=1), end=T0 + timedelta(hours=2))]),
        snap("bed", T0, [dr(link="L3", name="Late start", start=T0 + timedelta(minutes=30), end=END)]),
    )
    by = {r.unit_key: r for r in rows(db)}
    assert {k: r.status for k, r in by.items()} == {"up": "running", "main": "announced", "bed": "announced"}

    # Shortly after the ends: 'running' waits 10 minutes past its end; 'announced' doesn't.
    now = END + timedelta(minutes=5)
    closed = events.sweep(db, now)
    db.commit()
    by = {r.unit_key: r for r in rows(db)}
    assert sorted(closed) == sorted([by["main"].id, by["bed"].id])
    assert by["up"].status == "running"
    assert by["main"].status == "ended" and by["main"].ended_at == now
    now = END + timedelta(minutes=11)
    events.sweep(db, now)
    db.commit()
    assert next(r for r in rows(db) if r.unit_key == "up").status == "ended"
    assert alert(db, "running").resolved_at is not None and alert(db, "ended").resolved_at is None


def test_sweep_leaves_an_announced_event_past_its_start_alone(db):
    ingest(db, snap("up", T0, [dr()]))
    assert events.sweep(db, START + timedelta(minutes=30)) == []
    db.commit()
    assert rows(db)[0].status == "announced"


def test_sweep_resolves_alerts_whose_event_is_gone(db):
    from climate import notify

    notify.raise_alert(db, "utility_event", "info", "old", dedupe_key=events.alert_key("link:gone", "ended"))
    db.commit()
    events.sweep(db, T0)
    db.commit()
    assert alert(db, "ended", key="link:gone").resolved_at is not None


# --- the alerts switch ------------------------------------------------------------------------


def test_alerts_off_raises_nothing_but_still_tracks_and_resolves(db):
    ingest(db, snap("up", T0, [dr()]))
    assert alert(db, "announced").resolved_at is None
    put_setting(db, "utility_events", UtilityEventSettings(alerts=False))
    db.commit()
    ingest(db, snap("up", START + timedelta(minutes=1), [dr(running=True)], hold=dr_hold()))
    assert rows(db)[0].status == "running"
    assert alert(db, "announced").resolved_at is not None  # resolving still happens
    assert alert(db, "running") is None
    ingest(db, snap("up", END + timedelta(minutes=1), []))
    assert rows(db)[0].status == "ended" and alert(db, "ended") is None


# --- unknown events ---------------------------------------------------------------------------


def put_live(db, unit: str, ts: datetime, *, source: str = "ecobee", hold: HoldInfo | None = None) -> None:
    s = snap(unit, ts, hold=hold, source=source)
    db.merge(LiveUnit(unit_key=unit, ts=ts, source=source, revision="r", snapshot=s.model_dump(mode="json")))
    db.commit()


def test_unknown_event_alert_raised_while_it_runs_and_resolved_after(db):
    today = HoldInfo(hold_type="today", start=T0, end=END)
    ingest(db, snap("up", T0, hold=today))
    a = db.execute(select(Alert).where(Alert.dedupe_key == "unknown_event:today")).scalar_one()
    assert a.level == "warn" and a.resolved_at is None and a.title == "Unrecognised ecobee event: today"
    assert a.body == ("An unrecognised ecobee event (today) is running on the upstairs thermostat; the app is "
                      "hands-off on that unit until it ends.")

    put_live(db, "main", T0, hold=today)  # another unit with it: the body names both
    ingest(db, snap("up", T0 + timedelta(minutes=3), hold=today))
    db.expire_all()
    a = db.get(Alert, a.id)
    assert "main floor and upstairs thermostats" in a.body and a.resolved_at is None

    put_live(db, "main", T0 + timedelta(minutes=4))
    ingest(db, snap("up", T0 + timedelta(minutes=6)))
    db.expire_all()
    assert db.get(Alert, a.id).resolved_at is not None


def test_unknown_event_alert_is_not_resolved_while_a_unit_is_only_seen_over_homekit(db):
    ingest(db, snap("up", T0, hold=HoldInfo(hold_type="switchOccupancy")))
    put_live(db, "main", T0, source="homekit")
    ingest(db, snap("up", T0 + timedelta(minutes=3)))  # gone from 'up', but 'main' is unknown
    a = db.execute(select(Alert).where(Alert.dedupe_key == "unknown_event:switchOccupancy")).scalar_one()
    assert a.resolved_at is None


def test_known_hold_types_never_alert(db):
    for i, hold_type in enumerate(("holdHours", "autoAway", "quickSave", "vacation", "demandResponse")):
        ingest(db, snap("up", T0 + timedelta(minutes=i), hold=HoldInfo(hold_type=hold_type)))
    db.expire_all()
    assert db.execute(select(Alert).where(Alert.kind == "unknown_event")).first() is None


# --- poller wiring ------------------------------------------------------------------------------


class EventSource:
    kind = "ecobee"

    def __init__(self, evs: list[ThermostatEvent]) -> None:
        self.evs = evs

    async def poll_revisions(self) -> dict[str, str]:
        return {"up": "r1"}

    async def fetch_snapshots(self, unit_keys=None) -> list[UnitSnapshot]:
        return [UnitSnapshot(unit_key="up", ts=T0, source="ecobee", revision="r1", events=self.evs,
                             sensors=[SensorReading(sensor_key="up.girls_room", ts=T0, temp_f=76.0)])]


@pytest.fixture
def _fresh_poller():
    poller.reset_states()
    yield
    poller.reset_states()


async def test_live_polls_record_utility_events(db, _fresh_poller):
    await poller.poll_once(EventSource([dr()]), T0, {})
    assert [(r.unit_key, r.status) for r in rows(db)] == [("up", "announced")]


async def test_a_failing_events_step_never_fails_the_poll(db, _fresh_poller, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bad event")

    monkeypatch.setattr(events, "ingest", boom)
    revs = await poller.poll_once(EventSource([dr()]), T0, {})
    assert revs == {"up": "r1"}
    db.expire_all()
    assert db.get(LiveUnit, "up") is not None  # the snapshot itself was stored
    assert poller.source_state("ecobee").consecutive_failures == 0
    assert rows(db) == []


def test_backfill_style_ingest_does_not_touch_utility_events(db):
    from climate.collector.ingest import ingest_snapshots

    ingest_snapshots(db, [snap("up", T0, [dr()])])
    db.commit()
    assert rows(db) == []
