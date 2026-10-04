"""agent_queue: triggered limits + coalescing, claim order, finish, schedule_due."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from climate import agent_queue
from climate.agent_queue import TriggerLimitReached
from climate.store.app_settings import AgentSettings, LocationSettings, put_setting
from climate.store.orm import AgentRun

CHI = ZoneInfo("America/Chicago")


def local(y, mo, d, h, mi=0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=CHI).astimezone(UTC)


@pytest.fixture
def chicago(db):
    put_setting(db, "location", LocationSettings(tz="America/Chicago"))
    db.flush()
    return db


def runs(db, kind: str | None = None) -> list[AgentRun]:
    db.expire_all()
    q = select(AgentRun).order_by(AgentRun.id)
    if kind:
        q = q.where(AgentRun.kind == kind)
    return db.execute(q).scalars().all()


def test_triggered_coalesces_while_queued(chicago):
    db = chicago
    now = local(2026, 10, 4, 14)
    a = agent_queue.enqueue(db, "triggered", "anomaly", trigger={"kind": "drift"}, now=now)
    b = agent_queue.enqueue(db, "triggered", "anomaly", trigger={"kind": "sensor_offline"}, now=now)
    assert a.id == b.id
    assert [e["kind"] for e in b.trigger["events"]] == ["drift", "sensor_offline"]
    assert len(runs(db, "triggered")) == 1


def test_triggered_limit_per_local_day(chicago):
    db = chicago
    put_setting(db, "agent", AgentSettings(max_triggered_per_day=2))
    t = local(2026, 10, 4, 9)
    for i in range(2):
        run = agent_queue.enqueue(db, "triggered", "anomaly", trigger={"n": i}, now=t + timedelta(hours=i))
        claimed = agent_queue.claim_next(db, t + timedelta(hours=i, minutes=1))
        assert claimed.id == run.id
        agent_queue.finish(db, run.id, status="completed")
    with pytest.raises(TriggerLimitReached):
        agent_queue.enqueue(db, "triggered", "anomaly", trigger={"n": 3}, now=local(2026, 10, 4, 23, 59))
    # a new LOCAL day resets the count (23:59 Oct 4 local was already Oct 5 in UTC; this is Oct 5 local)
    run = agent_queue.enqueue(db, "triggered", "anomaly", trigger={"n": 4}, now=local(2026, 10, 5, 0, 30))
    assert run.status == "queued"
    assert agent_queue.triggered_today(db, local(2026, 10, 5, 12)) == 1


def test_triggered_cancelled_runs_do_not_count_and_disabled_agent_refuses(chicago):
    db = chicago
    put_setting(db, "agent", AgentSettings(max_triggered_per_day=1))
    now = local(2026, 10, 4, 10)
    run = agent_queue.enqueue(db, "triggered", "anomaly", now=now)
    run.status = "cancelled"
    db.flush()
    assert agent_queue.enqueue(db, "triggered", "anomaly", now=now).id != run.id
    put_setting(db, "agent", AgentSettings(enabled=False))
    agent_queue.claim_next(db, now)
    with pytest.raises(TriggerLimitReached):
        agent_queue.enqueue(db, "triggered", "anomaly", now=now)


def test_claim_order_chat_first_then_oldest_and_deferred(chicago):
    db = chicago
    t = local(2026, 10, 4, 12)
    nightly = agent_queue.enqueue(db, "nightly", "schedule", now=t)
    weekly = agent_queue.enqueue(db, "weekly", "schedule", now=t + timedelta(minutes=1))
    chat = agent_queue.enqueue(db, "chat", "owner", prompt="why?", now=t + timedelta(minutes=2))

    assert agent_queue.claim_next(db, t + timedelta(minutes=3)).id == chat.id
    first = agent_queue.claim_next(db, t + timedelta(minutes=3))
    assert first.id == nightly.id and first.status == "running" and first.started_at is not None

    # usage limit: the agent defers the nightly until the reset
    reset = t + timedelta(hours=2)
    agent_queue.finish(db, nightly.id, status="deferred", error="usage limit", not_before=reset)
    assert agent_queue.claim_next(db, t + timedelta(minutes=4)).id == weekly.id
    assert agent_queue.claim_next(db, t + timedelta(minutes=5)) is None
    again = agent_queue.claim_next(db, reset)
    assert again.id == nightly.id and again.finished_at is None and again.error is None


def test_finish_fields(chicago):
    db = chicago
    run = agent_queue.enqueue(db, "chat", "owner", prompt="hi")
    agent_queue.claim_next(db, datetime.now(UTC))
    done = agent_queue.finish(db, run.id, status="completed", result_text="All good.", model="claude-opus-5-5",
                              terminal_reason="completed", num_turns=4, usage={"input_tokens": 10})
    assert (done.status, done.result_text, done.num_turns) == ("completed", "All good.", 4)
    assert done.finished_at is not None
    with pytest.raises(ValueError):
        agent_queue.finish(db, run.id, status="running")
    with pytest.raises(ValueError):
        agent_queue.finish(db, run.id, bogus=1)
    with pytest.raises(LookupError):
        agent_queue.finish(db, 999999, status="failed")
    deferred = agent_queue.enqueue(db, "nightly", "schedule")
    out = agent_queue.finish(db, deferred.id, status="deferred")
    assert out.not_before is not None  # default wait


def test_stale_running_runs_are_failed(chicago):
    db = chicago
    t = local(2026, 10, 4, 1)
    run = agent_queue.enqueue(db, "nightly", "schedule", now=t)
    agent_queue.claim_next(db, t)
    assert agent_queue.claim_next(db, t + timedelta(hours=4)) is None
    db.expire_all()
    assert db.get(AgentRun, run.id).status == "failed"


def test_schedule_due_nightly_across_local_midnight(chicago):
    db = chicago
    put_setting(db, "agent", AgentSettings(weekly_day=2))  # nightly 03:30 local (default); weekly Wednesday
    assert agent_queue.schedule_due(db, local(2026, 10, 4, 3, 0)) == []
    ids = agent_queue.schedule_due(db, local(2026, 10, 4, 3, 31))
    assert len(ids) == 1 and runs(db, "nightly")[0].requested_by == "schedule"
    assert agent_queue.schedule_due(db, local(2026, 10, 4, 12)) == []
    assert agent_queue.schedule_due(db, local(2026, 10, 4, 23, 59)) == []
    # after local midnight but before 03:30: still nothing (UTC date already changed)
    assert agent_queue.schedule_due(db, local(2026, 10, 5, 0, 10)) == []
    assert len(agent_queue.schedule_due(db, local(2026, 10, 5, 3, 30))) == 1
    assert len(runs(db, "nightly")) == 2


def test_schedule_due_weekly_once_per_week(chicago):
    db = chicago
    put_setting(db, "agent", AgentSettings(nightly_time="23:00", weekly_day=6, weekly_time="05:00"))
    # Sunday 2026-10-04, 04:59 local: not yet
    assert agent_queue.schedule_due(db, local(2026, 10, 4, 4, 59)) == []
    assert len(agent_queue.schedule_due(db, local(2026, 10, 4, 5, 0))) == 1
    assert agent_queue.schedule_due(db, local(2026, 10, 4, 18)) == []
    # Monday after: the period's weekly run exists; nightly fires at 23:00 only
    assert agent_queue.schedule_due(db, local(2026, 10, 5, 12)) == []
    assert [r.kind for r in runs(db)] == ["weekly"]
    # next Sunday 05:00 -> the next weekly
    assert len(agent_queue.schedule_due(db, local(2026, 10, 11, 5, 1))) == 1
    assert len(runs(db, "weekly")) == 2


def test_schedule_due_weekly_catch_up_is_same_day_only_and_respects_disabled(chicago):
    db = chicago
    put_setting(db, "agent", AgentSettings(nightly_time="23:59", weekly_day=6, weekly_time="05:00"))
    # worker down all Sunday morning; it catches up at 21:00 the same day...
    assert len(agent_queue.schedule_due(db, local(2026, 10, 4, 21))) == 1
    # ...but a slot missed entirely is not run on Monday or Tuesday (no surprise run on install)
    assert agent_queue.schedule_due(db, local(2026, 10, 12, 9)) == []
    assert agent_queue.schedule_due(db, local(2026, 10, 13, 9)) == []
    put_setting(db, "agent", AgentSettings(enabled=False))
    assert agent_queue.schedule_due(db, local(2026, 10, 11, 23, 59)) == []
    assert agent_queue.next_nightly_at(db, local(2026, 10, 11, 12)) is None


def test_next_nightly_at(chicago):
    db = chicago
    assert agent_queue.next_nightly_at(db, local(2026, 10, 4, 1)) == local(2026, 10, 4, 3, 30)
    agent_queue.schedule_due(db, local(2026, 10, 4, 4))
    assert agent_queue.next_nightly_at(db, local(2026, 10, 4, 4)) == local(2026, 10, 5, 3, 30)


def test_waiting_runs_do_not_pile_up_and_stale_ones_expire(db):
    from datetime import UTC, datetime, timedelta

    from climate import agent_queue
    from climate.store.orm import AgentRun

    t0 = datetime(2026, 7, 1, 15, 0, tzinfo=UTC)  # 10:00 in Chicago, after 03:30
    ids = [agent_queue.schedule_due(db, t0 + timedelta(days=d))[0] for d in range(4)]
    # the agent never claimed any: each new nightly replaced the one still waiting
    statuses = [db.get(AgentRun, i).status for i in ids]
    assert statuses == ["cancelled", "cancelled", "cancelled", "queued"]
    # a run that waited more than 36 h is cancelled instead of being run late
    assert agent_queue.claim_next(db, t0 + timedelta(days=5)) is None
    assert db.get(AgentRun, ids[-1]).status == "cancelled"
    # owner chat questions never expire
    chat = agent_queue.enqueue(db, "chat", "owner", prompt="hi")
    chat.created_at = t0 - timedelta(days=10)
    db.flush()
    claimed = agent_queue.claim_next(db, t0 + timedelta(days=5))
    assert claimed is not None and claimed.id == chat.id
