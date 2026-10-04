"""Utility-event and pre-cooling days are left out of every weather analysis, and the reports
say how many and why. One synthetic history per module (like test_analytics_history); every
test adds its events inside a savepoint and rolls them back."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from climate.analytics import exclusions
from climate.store.orm import ControlAction, Experiment, ExperimentDay, UtilityEvent
from tests.factories import make_history

TZ = "America/Chicago"
HISTORY_DAYS = 48


@pytest.fixture(scope="module")
def hist(database_url):
    from climate.store.db import _factory
    from tests.conftest import reset_db

    reset_db()
    s = _factory()()
    end = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    for i in range(0, HISTORY_DAYS, 4):
        make_history(s, days=4, end=end - timedelta(days=i), seed=i + 1)
    s.commit()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@contextmanager
def scratch(s) -> Iterator[None]:
    sp = s.begin_nested()
    try:
        yield
    finally:
        sp.rollback()


def _yesterday() -> date:
    from climate.timeutil import local_date, utcnow

    return local_date(utcnow(), TZ) - timedelta(days=1)


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), ZoneInfo(TZ)).astimezone(UTC)


_n = iter(range(1, 10_000))


def add_event(s, start: datetime, end: datetime | None, *, status: str = "ended", unit: str = "up",
              ended_at: datetime | None = None, started: bool | None = None) -> None:
    ran = status in ("running", "ended", "opted_out") if started is None else started
    s.add(UtilityEvent(
        unit_key=unit, event_key=f"link:test{next(_n)}", status=status, start_at=start, end_at=end,
        first_seen_at=start - timedelta(hours=20), last_seen_at=start, started_at=start if ran else None,
        ended_at=ended_at if ended_at is not None else (end if status in ("ended", "opted_out", "cancelled") else None),
        detail={},
    ))
    s.flush()


def add_prep(s, ts: datetime, *, status: str = "verified", reason: str = "Pre-cooling 2°F before the utility event",
             request: dict | None = None) -> None:
    s.add(ControlAction(ts=ts, unit_key="up", actor="controller", mode="act", channel="ecobee", action="set_hold",
                        status=status, rule="event_prep", reason=reason,
                        request=request if request is not None else {"heat_f": 68, "cool_f": 74, "hours": 2}))
    s.flush()


# --- which days ------------------------------------------------------------------------------


def test_event_days_labels_and_spans(hist):
    d = date(2026, 1, 12)  # a Monday, far from the history
    with scratch(hist):
        add_event(hist, at(d, 22), at(d + timedelta(days=1), 1))  # crosses midnight: two days
        add_event(hist, at(d + timedelta(days=3), 15), at(d + timedelta(days=4), 23), status="opted_out",
                  ended_at=at(d + timedelta(days=3), 15, 30))  # skipped after 30 min: one day
        add_event(hist, at(d + timedelta(days=5), 15), at(d + timedelta(days=5), 18), status="announced")
        add_event(hist, at(d + timedelta(days=6), 15), at(d + timedelta(days=6), 18), status="cancelled")
        add_event(hist, at(d + timedelta(days=8), 15), None, status="running")  # no end: through the window
        add_prep(hist, at(d + timedelta(days=2), 12))
        add_prep(hist, at(d + timedelta(days=7), 23), reason="Pre-heating 2°F before the utility event")  # into day 8
        add_prep(hist, at(d + timedelta(days=4), 12), status="failed")
        add_prep(hist, at(d, 12))  # the event wins on a day with both
        out = exclusions.event_days(hist, d, d + timedelta(days=9), TZ)
    E, C, H = exclusions.EVENT, exclusions.PREP_COOL, exclusions.PREP_HEAT
    assert out == {
        d: E, d + timedelta(days=1): E, d + timedelta(days=2): C, d + timedelta(days=3): E,
        d + timedelta(days=7): H, d + timedelta(days=8): E, d + timedelta(days=9): E,
    }
    assert exclusions.counts(out) == {E: 5, C: 1, H: 1}
    assert exclusions.left_out_note(out) == (
        " 5 days with a utility event, 1 day of pre-cooling before a utility event and 1 day of pre-heating before a "
        "utility event were left out.")
    assert exclusions.left_out_note({d: E}) == " 1 day with a utility event was left out."
    assert exclusions.left_out_note({}) == ""


def test_event_days_clip_to_the_window(hist):
    d = date(2026, 1, 12)
    with scratch(hist):
        add_event(hist, at(d - timedelta(days=1), 20), at(d, 2))
        add_event(hist, at(d - timedelta(days=3), 20), at(d - timedelta(days=2), 0))  # ends at midnight before
        assert exclusions.event_days(hist, d, d + timedelta(days=2), TZ) == {d: exclusions.EVENT}


# --- baselines and savings --------------------------------------------------------------------


def test_baselines_leave_event_days_out_and_say_so(hist):
    from climate.analytics.baseline import fit_window

    y = _yesterday()
    start, end = y - timedelta(days=29), y
    before = fit_window(hist, start, end, TZ)
    assert before and all("utility event" not in (f.notes or "") for f in before.values())
    with scratch(hist):
        for back in (5, 10, 15):
            add_event(hist, at(y - timedelta(days=back), 15), at(y - timedelta(days=back), 18))
        add_prep(hist, at(y - timedelta(days=20), 11))
        after = fit_window(hist, start, end, TZ)
    assert set(after) == set(before)
    for key, fit in after.items():
        assert fit.n_days == before[key].n_days - 4, key
        assert y - timedelta(days=5) not in (fit.daily or {})
        assert "3 days with a utility event and 1 day of pre-cooling before a utility event left out" in fit.notes


def test_savings_drop_event_days_in_both_periods(hist):
    from climate.analytics.attribution import savings

    y = _yesterday()
    start = y - timedelta(days=13)
    base = savings(hist, start, y)
    assert base.baseline_ok and base.n_days == 14, base.note
    event_day = y - timedelta(days=3)
    with scratch(hist):
        add_event(hist, at(event_day, 15), at(event_day, 18), unit="main")
        add_event(hist, at(start - timedelta(days=6), 15), at(start - timedelta(days=6), 18))
        s = savings(hist, start, y)
    assert s.baseline_ok, s.note
    assert s.n_days == 13 and event_day not in {d.date for d in s.days}
    assert "1 day with a utility event was left out." in s.note
    assert f"Baseline: the 90 days before {start.isoformat()} (1 day with a utility event left out)." in s.note


def test_waterfall_drops_the_weekday_from_both_weeks(hist):
    from climate.analytics.attribution import waterfall

    y = _yesterday()
    this_monday = y + timedelta(days=1) - timedelta(days=(y + timedelta(days=1)).weekday())
    ws = this_monday - timedelta(days=7)
    w0 = waterfall(hist, ws)
    with scratch(hist):
        add_event(hist, at(ws + timedelta(days=2), 15), at(ws + timedelta(days=2), 18))
        w = waterfall(hist, ws)
    assert "Compared on the 6 weekday(s)" in w.note and "1 day with a utility event was left out." in w.note
    assert w.items[0].minutes < w0.items[0].minutes and w.items[-1].minutes < w0.items[-1].minutes


def test_drift_coupling_and_natural_experiments_say_what_they_left_out(hist):
    from climate.analytics import coupling, metrics
    from climate.analytics.baseline import refit_all

    y = _yesterday()
    with scratch(hist):
        refit_all(hist, datetime.now(UTC))  # drift needs active baselines
        add_event(hist, at(y - timedelta(days=2), 15), at(y - timedelta(days=2), 18))
        drift = metrics.drift(hist)
        c = coupling.coupling(hist, days=30)
        ne = coupling.natural_experiments(hist, days=40)
    assert drift.note.endswith(" 1 day with a utility event was left out."), drift.note
    assert c.interpretation.endswith(" 1 day with a utility event was left out."), c.interpretation
    assert ne.note.endswith(" 1 day with a utility event was left out."), ne.note
    assert all(e.date != y - timedelta(days=2) for e in ne.events)


def test_daily_report_does_not_grade_an_event_day(hist):
    from climate.analytics.reports import build_daily_report
    from climate.store.orm import Report

    y = _yesterday()
    day = y - timedelta(days=1)
    with scratch(hist):
        add_event(hist, at(day, 15), at(day, 18))
        add_event(hist, at(day - timedelta(days=20), 15), at(day - timedelta(days=20), 18))
        rid = build_daily_report(hist, day)
        r = hist.get(Report, rid)
        body, data = r.body_md, dict(r.data)
    assert "not graded against the weather: this is a day with a utility event" in body
    assert "The baselines leave out 1 day with a utility event from their 90 training days." in body
    assert data["excluded"] == "utility event" and data["baseline_excluded"] == {"utility event": 1}


# --- experiments ------------------------------------------------------------------------------


def test_experiment_days_on_an_event_are_not_included(hist):
    from climate.experiments import analysis, switchback

    y = _yesterday()
    start = y - timedelta(days=9)
    with scratch(hist):
        cps = switchback.plan_checkpoints(20, 2, 0.10)
        exp = Experiment(
            name="t", hypothesis="h", arms=[{"key": "a", "label": "A", "params": {}}, {"key": "b", "label": "B", "params": {}}],
            design={"n_days": 20, "block_days": 1, "seed": 1, "alpha": 0.10, "checkpoints": [c.model_dump() for c in cps]},
            status="running", proposed_by="owner", start_date=start, end_date=start + timedelta(days=19),
        )
        hist.add(exp)
        hist.flush()
        for d, arm in switchback.assign_days(["a", "b"], start, 20, 1, 1):
            hist.add(ExperimentDay(experiment_id=exp.id, day=d, arm=arm))
        event_day = y - timedelta(days=4)
        add_event(hist, at(event_day, 15), at(event_day, 18), unit="bed")
        analysis.update_days(hist, datetime.now(UTC))
        days = {d.day: d for d in hist.query(ExperimentDay).filter(ExperimentDay.experiment_id == exp.id)}
        assert days[event_day].included is False and days[event_day].note.startswith("utility event")
        others = [d for k, d in days.items() if k < y + timedelta(days=1) and k != event_day]
        assert others and all("utility event" not in (d.note or "") for d in others)
        a = analysis.analyze(hist, exp)
    assert "1 day with a utility event was left out." in a.note
