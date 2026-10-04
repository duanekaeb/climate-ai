"""worker: the jobs runner, anomaly triggers, source lifecycle, LockedSource, loop resilience."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from climate import worker as wmod
from climate.collector import poller
from climate.sources.base import RuntimeInterval, UnitSnapshot
from climate.store.app_settings import AgentSettings, SourceSettings, get_heartbeat, put_setting
from climate.store.orm import AgentRun, Alert, Job, LiveSensor, Reading5m


@pytest.fixture(autouse=True)
def _fresh_poller_state():
    poller.reset_states()
    yield
    poller.reset_states()


def add_job(db, kind: str, **params) -> int:
    job = Job(kind=kind, status="queued", requested_by="owner", params=params)
    db.add(job)
    db.flush()
    db.commit()
    return job.id


def job(db, job_id: int) -> Job:
    db.expire_all()
    return db.get(Job, job_id)


@pytest.fixture
def fits(monkeypatch):
    from climate.analytics import baseline, reports
    from climate.models import thermal_rc

    calls: list[str] = []

    def rc(s, now, days=28):
        calls.append("rc")
        raise RuntimeError("rc data too short")

    monkeypatch.setattr(baseline, "refit_all", lambda s, now, train_days=90: calls.append("baseline") or [11, 12])
    monkeypatch.setattr(thermal_rc, "fit_room_offsets", lambda s, now, days=14: calls.append("offsets") or 13)
    monkeypatch.setattr(thermal_rc, "fit_rc", rc)
    monkeypatch.setattr(reports, "build_daily_report", lambda s, day: calls.append(f"report:{day}") or 42)
    monkeypatch.setattr(wmod, "_optional", lambda name: None)  # no occupancy priors module
    return calls


async def test_job_runner_refit_report_unknown(db, fits):
    refit = add_job(db, "refit")
    report = add_job(db, "report", date="2026-09-30")
    bogus = add_job(db, "teleport")
    w = wmod.Worker()
    handled = await w.process_jobs(datetime(2026, 10, 1, 12, tzinfo=UTC))
    assert handled == [refit, report, bogus]

    j = job(db, refit)
    assert j.status == "done" and j.started_at and j.finished_at
    steps = j.result["steps"]
    assert steps["baselines"] == {"ok": True, "result": [11, 12]}
    assert steps["room_offsets"] == {"ok": True, "result": 13}
    assert steps["rc"]["ok"] is False and "rc data too short" in steps["rc"]["error"]

    j = job(db, report)
    assert j.status == "done" and j.result == {"report_id": 42, "date": "2026-09-30"}
    assert "report:2026-09-30" in fits

    j = job(db, bogus)
    assert j.status == "failed" and "unknown job kind" in j.error
    assert await w.process_jobs() == []


async def test_refit_job_fails_when_every_fit_fails(db, monkeypatch):
    from climate.analytics import baseline
    from climate.models import thermal_rc

    def broken(*a, **k):
        raise NotImplementedError

    for mod, name in ((baseline, "refit_all"), (thermal_rc, "fit_room_offsets"), (thermal_rc, "fit_rc")):
        monkeypatch.setattr(mod, name, broken)
    monkeypatch.setattr(wmod, "_optional", lambda name: type("Priors", (), {"fit_priors": staticmethod(broken)}))
    jid = add_job(db, "refit")
    await wmod.Worker().process_jobs()
    j = job(db, jid)
    assert j.status == "failed" and "every fit failed" in j.error and "occupancy_priors" in j.error


async def test_backfill_job_uses_the_worker_source(db, monkeypatch):
    seen = {}

    async def fake_backfill(source, start, end):
        seen.update(kind=source.kind, days=(end - start).days)
        return {"runtime_rows": 5, "weather_rows": 0, "start": start.isoformat(), "end": end.isoformat()}

    monkeypatch.setattr(wmod.backfill_mod, "backfill", fake_backfill)
    w = wmod.Worker()
    w.source, w.source_kind = wmod.LockedSource(FakeSim()), "simulator"
    jid = add_job(db, "backfill", days=14)
    await w.process_jobs()
    j = job(db, jid)
    assert j.status == "done" and j.result["runtime_rows"] == 5
    assert seen == {"kind": "simulator", "days": 14}


def test_orphaned_running_jobs_are_failed(db):
    db.add(Job(kind="refit", status="running", requested_by="owner", params={}))
    db.commit()
    assert wmod._fail_orphaned_jobs() == 1
    db.expire_all()
    assert db.execute(select(Job.status)).scalar_one() == "failed"


# --- anomalies ---------------------------------------------------------------------------


def test_sensor_offline_alert_trigger_and_resolve(db):
    now = datetime(2026, 10, 4, 15, 0, tzinfo=UTC)
    db.add(Reading5m(ts=now - timedelta(minutes=50), sensor_key="main.kitchen", temp_f=72.0, source="ecobee_poll"))
    db.add(LiveSensor(sensor_key="main.kitchen", ts=now - timedelta(minutes=1), source="ecobee_poll", online=False))
    db.add(LiveSensor(sensor_key="main.school_room", ts=now - timedelta(minutes=1), source="ecobee_poll", temp_f=71.0,
                      online=True))
    db.commit()

    events = wmod.check_sensors(now)
    assert [e["sensor_key"] for e in events] == ["main.kitchen"]
    db.expire_all()
    alert = db.execute(select(Alert).where(Alert.dedupe_key == "sensor_offline:main.kitchen")).scalar_one()
    assert alert.kind == "sensor_offline" and alert.level == "warn" and alert.resolved_at is None
    assert wmod.check_sensors(now + timedelta(minutes=15)) == []  # still open: no new trigger

    live = db.get(LiveSensor, "main.kitchen")
    live.online, live.temp_f, live.ts = True, 72.5, now + timedelta(minutes=20)
    db.commit()
    assert wmod.check_sensors(now + timedelta(minutes=21)) == []
    db.expire_all()
    assert db.get(Alert, alert.id).resolved_at is not None


def test_offline_less_than_30_minutes_is_not_an_alert(db):
    now = datetime(2026, 10, 4, 15, 0, tzinfo=UTC)
    db.add(Reading5m(ts=now - timedelta(minutes=10), sensor_key="main.kitchen", temp_f=72.0, source="ecobee_poll"))
    db.add(LiveSensor(sensor_key="main.kitchen", ts=now, source="ecobee_poll", online=False))
    db.commit()
    assert wmod.check_sensors(now) == []


def test_trigger_agent_once_per_day_per_key_and_coalesced(db):
    now = datetime(2026, 10, 4, 18, 0, tzinfo=UTC)
    drift = {"kind": "drift", "key": "drift:up:cool", "unit_key": "up", "mode": "cool"}
    offline = {"kind": "sensor_offline", "sensor_key": "main.kitchen"}
    ids = wmod.trigger_agent(now, [drift, offline])
    assert len(ids) == 1
    db.expire_all()
    run = db.execute(select(AgentRun)).scalar_one()
    assert run.kind == "triggered" and run.requested_by == "anomaly"
    assert [e["kind"] for e in run.trigger["events"]] == ["drift", "sensor_offline"]
    assert "key" not in run.trigger["events"][0]

    # same drift again today: ignored; tomorrow it may fire again
    run.status = "completed"
    db.commit()
    assert wmod.trigger_agent(now + timedelta(hours=1), [drift]) == []
    assert len(wmod.trigger_agent(now + timedelta(days=1), [drift])) == 1


def test_trigger_agent_limit_and_disabled(db):
    put_setting(db, "agent", AgentSettings(max_triggered_per_day=0))
    db.commit()
    assert wmod.trigger_agent(datetime(2026, 10, 4, 18, tzinfo=UTC), [{"kind": "drift", "key": "d"}]) == []
    put_setting(db, "agent", AgentSettings(enabled=False))
    db.commit()
    assert wmod.trigger_agent(datetime(2026, 10, 4, 18, tzinfo=UTC), [{"kind": "x", "key": "y"}]) == []
    db.expire_all()
    assert db.execute(select(AgentRun)).first() is None


def test_detect_anomalies_upstairs_maxed(db, monkeypatch):
    from climate.analytics import metrics
    from climate.api.schemas import DriftReport

    monkeypatch.setattr(metrics, "drift", lambda s, recent_days=7: DriftReport(units=[], note=""))
    monkeypatch.setattr(metrics, "unit_today", lambda s, now, tz: {"up": {"maxed_minutes_today": 75.0}})
    events = wmod.detect_anomalies(datetime(2026, 10, 4, 22, tzinfo=UTC))
    assert events == [{"kind": "upstairs_maxed", "key": "maxed:up", "unit_key": "up", "maxed_minutes_today": 75}]


# --- source lifecycle --------------------------------------------------------------------


class FakeSim:
    kind = "simulator"

    def __init__(self) -> None:
        self.advanced: list[datetime] = []
        self.closed = False
        self.active = 0
        self.max_active = 0

    def advance_to(self, now: datetime) -> None:
        self.advanced.append(now)

    async def poll_revisions(self) -> dict[str, str]:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        return {"main": str(len(self.advanced))}

    async def fetch_snapshots(self, unit_keys=None):
        return [UnitSnapshot(unit_key="main", ts=datetime.now(UTC), source="simulator")]

    async def fetch_runtime(self, start, end):
        return [RuntimeInterval(unit_key="main", ts=start, comp_cool1=10)]

    async def extra(self, x: int) -> int:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        return x * 2

    async def close(self) -> None:
        self.closed = True


async def test_locked_source_serializes_calls():
    inner = FakeSim()
    src = wmod.LockedSource(inner)
    out = await asyncio.gather(src.poll_revisions(), src.extra(2), src.poll_revisions(), src.extra(3))
    assert out[1] == 4 and out[3] == 6
    assert inner.max_active == 1
    assert src.advance_to is not None and src.inner is inner


async def test_source_step_polls_and_switches_live(db, monkeypatch):
    built: list[FakeSim] = []

    def get_source(kind):
        if kind != "simulator":
            raise RuntimeError("not signed in")
        sim = FakeSim()
        built.append(sim)
        return sim

    import climate.sources

    monkeypatch.setattr(climate.sources, "get_source", get_source)
    w = wmod.Worker()
    await w.source_step()
    assert w.source_kind == "simulator" and len(built[0].advanced) == 1 and w.last_poll_at is not None
    await w.source_step()  # within sim_poll_seconds: no second poll
    assert len(built[0].advanced) == 1

    await w.heartbeat_step()
    db.expire_all()
    hb = get_heartbeat(db, "worker")
    assert hb.detail["source"] == "simulator" and hb.detail["source_ok"] is True and hb.detail["last_poll_at"]

    put_setting(db, "source", SourceSettings(kind="ecobee"))
    db.commit()
    await w.source_step()
    assert built[0].closed and w.source is None
    assert poller.source_state("ecobee").consecutive_failures == 1


async def test_loop_survives_failures(monkeypatch):
    monkeypatch.setattr(wmod, "MAX_BACKOFF_S", 0.001)
    w = wmod.Worker()
    calls = []

    async def body():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("boom token=abc")
        w.stop()

    await asyncio.wait_for(w._loop("test", lambda: 0.001, body), timeout=5)
    assert len(calls) == 3 and w._loops["test"].failures == 0


async def test_daily_step_runs_nightly_once_per_local_day(db, monkeypatch):
    ran = []
    monkeypatch.setattr(wmod, "run_nightly", lambda now: ran.append(now) or {"baselines": {"ok": True}})
    clock = {"now": datetime(2026, 10, 4, 6, 0, tzinfo=UTC)}  # 01:00 Chicago: too early
    monkeypatch.setattr(wmod, "utcnow", lambda: clock["now"])
    w = wmod.Worker()
    await w.daily_step()
    assert ran == []
    clock["now"] = datetime(2026, 10, 4, 7, 31, tzinfo=UTC)  # 02:31 Chicago
    await w.daily_step()
    await w.daily_step()
    assert len(ran) == 1
    # a restarted worker remembers (stored in app_settings)
    w2 = wmod.Worker()
    w2._daily = wmod._load_json(wmod.DAILY_KEY)
    await w2.daily_step()
    assert len(ran) == 1
    clock["now"] = datetime(2026, 10, 5, 7, 31, tzinfo=UTC)
    await w2.daily_step()
    assert len(ran) == 2
