"""The agent_runs queue shared by the API (enqueue, claim, finish) and the worker (schedule,
anomaly triggers). The agent service talks to it only through the API.

Rules (blueprint §6): about one nightly run a day, one weekly run, at most
``AgentSettings.max_triggered_per_day`` triggered runs per LOCAL day (coalesced: a new anomaly
joins a triggered run that has not started yet instead of queuing another), plus the owner's
questions, which are claimed before anything else.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, select, text
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from climate.events import publish
from climate.store.app_settings import AgentSettings, LocationSettings, get_setting
from climate.store.orm import AgentRun
from climate.timeutil import day_bounds_utc, local_date, parse_hhmm, to_local, utcnow

KINDS = {"nightly", "weekly", "triggered", "chat", "signin_check"}
WAITING = ("queued", "deferred")  # not started yet
FINISH_STATUSES = {"completed", "failed", "deferred", "cancelled"}
FINISH_FIELDS = {"status", "result_text", "model", "terminal_reason", "num_turns", "usage", "error", "session_id",
                 "not_before", "finished_at"}
# A run 'running' this long without a finish call is assumed dead (agent crashed mid-run).
STALE_RUNNING = timedelta(hours=3)
# A scheduled or anomaly run nobody claimed for this long is superseded by newer data; run it
# late and it spends the owner's subscription on a stale report.
STALE_WAITING = timedelta(hours=36)
EXPIRING_KINDS = ("nightly", "weekly", "triggered")
DEFAULT_DEFER = timedelta(minutes=30)
_TRIGGER_LOCK = 727_101  # advisory xact lock: serializes triggered enqueue (API + worker)


class TriggerLimitReached(RuntimeError):
    pass


class RunNotFound(LookupError):
    pass


def _tz(session: Session) -> str:
    return get_setting(session, "location", LocationSettings).tz


def _events(trigger: dict[str, Any] | None, now: datetime) -> list[dict[str, Any]]:
    if not trigger:
        return []
    if isinstance(trigger.get("events"), list):
        return [dict(e) if isinstance(e, dict) else {"detail": e} for e in trigger["events"]]
    event = dict(trigger)
    event.setdefault("at", now.isoformat())
    return [event]


def triggered_today(session: Session, now: datetime | None = None) -> int:
    """Triggered runs created in the house's current local day (cancelled ones don't count)."""
    now = now or utcnow()
    tz = _tz(session)
    start, end = day_bounds_utc(local_date(now, tz), tz)
    return int(
        session.execute(
            select(func.count(AgentRun.id)).where(
                AgentRun.kind == "triggered", AgentRun.status != "cancelled",
                AgentRun.created_at >= start, AgentRun.created_at < end,
            )
        ).scalar_one()
    )


def _insert(session: Session, kind: str, requested_by: str, prompt: str, trigger: dict[str, Any] | None,
            now: datetime | None) -> AgentRun:
    run = AgentRun(kind=kind, status="queued", requested_by=requested_by, prompt=prompt or "", trigger=trigger)
    if now is not None:
        run.created_at = now
    session.add(run)
    session.flush()
    publish(session, "agent_run", run.id)
    return run


def enqueue(session: Session, kind: str, requested_by: str, prompt: str = "", trigger: dict[str, Any] | None = None,
            *, now: datetime | None = None) -> AgentRun:
    """Insert a queued run. 'triggered' runs: at most AgentSettings.max_triggered_per_day per
    local day, and coalesced with an already queued triggered run (append the trigger).

    ``now`` (optional) stamps ``created_at`` and picks the local day; it defaults to the clock.
    Raises TriggerLimitReached when a triggered run cannot be queued (limit, or agent disabled)."""
    if kind not in KINDS:
        raise ValueError(f"unknown agent run kind {kind!r}")
    if kind != "triggered":
        return _insert(session, kind, requested_by, prompt, trigger, now)

    clock = now or utcnow()
    session.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": _TRIGGER_LOCK})
    events = _events(trigger, clock)
    waiting = session.execute(
        select(AgentRun)
        .where(AgentRun.kind == "triggered", AgentRun.status.in_(WAITING))
        .order_by(AgentRun.created_at, AgentRun.id)
        .limit(1)
        .with_for_update()
    ).scalar_one_or_none()
    if waiting is not None:
        data = dict(waiting.trigger or {})
        data["events"] = list(data.get("events") or []) + events
        waiting.trigger = data
        flag_modified(waiting, "trigger")
        if prompt and prompt not in (waiting.prompt or ""):
            waiting.prompt = f"{waiting.prompt}\n\n{prompt}".strip()
        session.flush()
        publish(session, "agent_run", waiting.id)
        return waiting

    settings = get_setting(session, "agent", AgentSettings)
    if not settings.enabled:
        raise TriggerLimitReached("the Claude agent is disabled")
    used = triggered_today(session, clock)
    if used >= settings.max_triggered_per_day:
        raise TriggerLimitReached(
            f"{used} triggered run(s) today; the limit is {settings.max_triggered_per_day} per day"
        )
    return _insert(session, kind, requested_by, prompt, {"events": events}, now)


def _reap_stale(session: Session, now: datetime) -> None:
    stale = session.execute(
        select(AgentRun)
        .where(AgentRun.status == "running", AgentRun.started_at < now - STALE_RUNNING)
        .with_for_update(skip_locked=True)
    ).scalars().all()
    for run in stale:
        run.status = "failed"
        run.finished_at = now
        run.error = run.error or "The agent never reported back (stale run)."
        session.flush()
        publish(session, "agent_run", run.id)


def _expire_waiting(session: Session, now: datetime) -> None:
    """Cancel nightly/weekly/triggered runs that waited longer than STALE_WAITING (no agent,
    signed out, stopped container). Owner chat questions never expire."""
    old = session.execute(
        select(AgentRun)
        .where(
            AgentRun.status.in_(WAITING),
            AgentRun.kind.in_(EXPIRING_KINDS),
            AgentRun.created_at < now - STALE_WAITING,
        )
        .with_for_update(skip_locked=True)
    ).scalars().all()
    for run in old:
        run.status = "cancelled"
        run.finished_at = now
        run.error = "Superseded: waited more than 36 h for the agent service; newer runs cover this period."
        session.flush()
        publish(session, "agent_run", run.id)


def claim_next(session: Session, now: datetime) -> AgentRun | None:
    """Oldest queued (or deferred with not_before <= now) run -> running (SELECT ... FOR UPDATE
    SKIP LOCKED). Chat runs first. Stale scheduled runs are cancelled first."""
    _reap_stale(session, now)
    _expire_waiting(session, now)
    run = session.execute(
        select(AgentRun)
        .where(
            AgentRun.status.in_(WAITING),
            (AgentRun.not_before.is_(None)) | (AgentRun.not_before <= now),
        )
        .order_by(case((AgentRun.kind == "chat", 0), else_=1), AgentRun.created_at, AgentRun.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()
    if run is None:
        return None
    run.status = "running"
    run.started_at = now
    run.finished_at = None
    run.error = None
    session.flush()
    publish(session, "agent_run", run.id)
    return run


def finish(session: Session, run_id: int, **fields: Any) -> AgentRun:
    """Record the outcome of a run (fields of ``AgentFinishBody``). Sets finished_at (unless
    given) and publishes 'agent_run'. A 'deferred' run (usage limit) waits until
    ``not_before`` (default 30 minutes) and is claimed again after it."""
    unknown = set(fields) - FINISH_FIELDS
    if unknown:
        raise ValueError(f"unknown run fields: {sorted(unknown)}")
    status = fields.get("status")
    if status is not None and status not in FINISH_STATUSES:
        raise ValueError(f"cannot finish a run with status {status!r}")
    run = session.execute(select(AgentRun).where(AgentRun.id == run_id).with_for_update()).scalar_one_or_none()
    if run is None:
        raise RunNotFound(f"agent run {run_id} not found")
    now = utcnow()
    for key, value in fields.items():
        setattr(run, key, value)
    if run.status == "deferred" and run.not_before is None:
        run.not_before = now + DEFAULT_DEFER
    if fields.get("finished_at") is None:
        run.finished_at = now
    session.flush()
    publish(session, "agent_run", run.id)
    return run


def _supersede_waiting(session: Session, kind: str, now: datetime) -> None:
    """A new period's scheduled run replaces an older one still waiting (agent down or signed
    out), so the backlog never grows past one run per kind."""
    for run in session.execute(
        select(AgentRun).where(AgentRun.kind == kind, AgentRun.status.in_(WAITING)).with_for_update(skip_locked=True)
    ).scalars():
        run.status = "cancelled"
        run.finished_at = now
        run.error = f"Superseded by a newer {kind} run before the agent service picked it up."
        session.flush()
        publish(session, "agent_run", run.id)


def _exists(session: Session, kind: str, start: datetime, end: datetime) -> bool:
    return session.execute(
        select(AgentRun.id).where(AgentRun.kind == kind, AgentRun.created_at >= start, AgentRun.created_at < end).limit(1)
    ).first() is not None


def schedule_due(session: Session, now: datetime) -> list[int]:
    """Enqueue the nightly / weekly runs when their local time has passed today and none
    exists yet for this period. Returns new ids.

    Nightly: once per local day, after ``nightly_time``. Weekly: on ``weekly_day`` after
    ``weekly_time``, once per local week (the seven days starting that weekday at local
    midnight). A slot missed while the worker was down is caught up later the SAME local day,
    never on a later day. Any run of that kind created in the period counts, whoever asked."""
    settings = get_setting(session, "agent", AgentSettings)
    if not settings.enabled:
        return []
    tz = _tz(session)
    local_now = to_local(now, tz)
    new: list[int] = []

    if local_now.time() >= parse_hhmm(settings.nightly_time):
        start, end = day_bounds_utc(local_now.date(), tz)
        if not _exists(session, "nightly", start, end):
            _supersede_waiting(session, "nightly", now)  # one waiting nightly at most
            new.append(_insert(session, "nightly", "schedule", "", None, now).id)

    if local_now.weekday() == settings.weekly_day and local_now.time() >= parse_hhmm(settings.weekly_time):
        start, _ = day_bounds_utc(local_now.date(), tz)
        _, end = day_bounds_utc(local_now.date() + timedelta(days=6), tz)
        if not _exists(session, "weekly", start, end):
            _supersede_waiting(session, "weekly", now)
            new.append(_insert(session, "weekly", "schedule", "", None, now).id)
    return new


def next_nightly_at(session: Session, now: datetime) -> datetime | None:
    """When the next scheduled nightly run will be queued (None when the agent is disabled)."""
    settings = get_setting(session, "agent", AgentSettings)
    if not settings.enabled:
        return None
    tz = _tz(session)
    local_now = to_local(now, tz)
    z = ZoneInfo(tz)
    today_due = datetime.combine(local_now.date(), parse_hhmm(settings.nightly_time), z)
    start, end = day_bounds_utc(local_now.date(), tz)
    if local_now < today_due or not _exists(session, "nightly", start, end):
        return max(today_due, local_now).astimezone(now.tzinfo)
    return datetime.combine(local_now.date() + timedelta(days=1), parse_hhmm(settings.nightly_time), z).astimezone(now.tzinfo)
