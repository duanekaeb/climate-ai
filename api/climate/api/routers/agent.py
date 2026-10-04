"""Claude agent routes: status, the run queue (owner asks / runs now; the agent service claims,
finishes and heartbeats), all over ``climate.agent_queue``.

The agent service has no database credentials; this API is its only door into the house, and
none of these routes reaches a thermostat.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query
from fastapi import status as http
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from climate import agent_queue, events, notify
from climate.api.auth import AgentDep, OwnerDep, ReaderDep, Role
from climate.api.routers.control import SessionDep, house_tz, safe, unprocessable
from climate.api.schemas import (
    AgentFinishBody,
    AgentHeartbeatBody,
    AgentInfo,
    AgentRunOut,
    AskBody,
    RunBody,
)
from climate.store.app_settings import AgentSettings, beat, get_heartbeat, get_setting
from climate.store.orm import AgentRun
from climate.timeutil import day_bounds_utc, local_date, parse_hhmm, utcnow

router = APIRouter(tags=["agent"])

TOKEN_WARN_DAYS = 30
SIGNIN_ALERT = "claude_signin"
EXPIRY_ALERT = "claude_token_expiry"


# ---------------------------------------------------------------------------------------
# AgentInfo (also used by /status)
# ---------------------------------------------------------------------------------------


def _as_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def token_warning(signed_in: bool | None, expires: date | None, today: date) -> str | None:
    if signed_in is False:
        return ("Claude sign-in failed. Run `claude setup-token` on a computer with a browser and put the "
                "new token in CLAUDE_CODE_OAUTH_TOKEN for the agent service.")
    if expires is None:
        return None
    days_left = (expires - today).days
    if days_left < 0:
        return f"The Claude token expired on {expires.isoformat()}. Run `claude setup-token` to make a new one."
    if days_left <= TOKEN_WARN_DAYS:
        when = "today" if days_left == 0 else f"in {days_left} day{'s' if days_left != 1 else ''}"
        return (f"The Claude token expires {when} ({expires.isoformat()}). Run `claude setup-token` and "
                "update CLAUDE_CODE_OAUTH_TOKEN before then.")
    return None


def next_local_time(now: datetime, tz: str, hhmm: str) -> datetime:
    """The next occurrence (strictly after ``now``) of a local wall-clock time, as UTC."""
    z = ZoneInfo(tz)
    local_now = now.astimezone(z)
    at = parse_hhmm(hhmm)
    candidate = datetime.combine(local_now.date(), at, z)
    if candidate <= local_now:
        candidate = datetime.combine(local_now.date() + timedelta(days=1), at, z)
    return candidate.astimezone(ZoneInfo("UTC"))


def run_out(run: AgentRun) -> AgentRunOut:
    return AgentRunOut.model_validate(run, from_attributes=True)


def agent_info(session: Session, now: datetime | None = None) -> AgentInfo:
    now = now or utcnow()
    settings = get_setting(session, "agent", AgentSettings)
    tz = house_tz(session)
    today = local_date(now, tz)
    hb = get_heartbeat(session, "agent")
    detail = hb.detail if hb else {}
    signed_in = detail.get("signed_in") if isinstance(detail.get("signed_in"), bool) else None
    sdk_version = detail.get("sdk_version") if isinstance(detail.get("sdk_version"), str) else None
    expires = _as_date(detail.get("token_expires_at"))
    last = session.execute(
        select(AgentRun).order_by(AgentRun.created_at.desc(), AgentRun.id.desc()).limit(1)
    ).scalar_one_or_none()
    start, end = day_bounds_utc(today, tz)
    triggered = session.execute(
        select(func.count()).select_from(AgentRun).where(
            AgentRun.kind == "triggered", AgentRun.created_at >= start, AgentRun.created_at < end
        )
    ).scalar_one()
    return AgentInfo(
        enabled=settings.enabled,
        signed_in=signed_in,
        last_beat_at=hb.at if hb else None,
        sdk_version=sdk_version,
        token_expires_at=expires,
        token_warning=token_warning(signed_in, expires, today),
        last_run=run_out(last) if last else None,
        next_nightly_at=next_local_time(now, tz, settings.nightly_time) if settings.enabled else None,
        triggered_today=int(triggered),
        settings=settings,
    )


def _sync_alerts(session: Session, body: AgentHeartbeatBody, today: date) -> None:
    """Raise/resolve the sign-in and token-expiry alerts (deduped; the push goes out once)."""
    if body.signed_in is False:
        notify.raise_alert(
            session, SIGNIN_ALERT, "error", "Claude sign-in failed",
            "The agent service could not sign in to your Claude plan. Run `claude setup-token` and update "
            "CLAUDE_CODE_OAUTH_TOKEN. Pending sign-offs wait; the house keeps running.",
            dedupe_key=SIGNIN_ALERT,
        )
    elif body.signed_in is True:
        notify.resolve_alert(session, SIGNIN_ALERT)
    if body.token_expires_at is not None:
        days_left = (body.token_expires_at - today).days
        if days_left <= TOKEN_WARN_DAYS:
            notify.raise_alert(
                session, EXPIRY_ALERT, "warn", "Claude token expires soon",
                token_warning(None, body.token_expires_at, today) or "", dedupe_key=EXPIRY_ALERT,
            )
        else:
            notify.resolve_alert(session, EXPIRY_ALERT)


# ---------------------------------------------------------------------------------------
# readers
# ---------------------------------------------------------------------------------------


@router.get("/agent/status", response_model=AgentInfo)
def agent_status(_: Role = ReaderDep, session: Session = SessionDep) -> AgentInfo:
    return agent_info(session)


@router.get("/agent/runs", response_model=list[AgentRunOut])
def list_runs(
    limit: Annotated[int, Query(ge=1, le=200)] = 20, _: Role = ReaderDep, session: Session = SessionDep
) -> list[AgentRunOut]:
    rows = session.execute(select(AgentRun).order_by(AgentRun.created_at.desc(), AgentRun.id.desc()).limit(limit))
    return [run_out(r) for r in rows.scalars()]


@router.get("/agent/runs/{run_id}", response_model=AgentRunOut)
def get_run(run_id: int, _: Role = ReaderDep, session: Session = SessionDep) -> AgentRunOut:
    run = session.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(http.HTTP_404_NOT_FOUND, f"Unknown agent run {run_id}.")
    return run_out(run)


# ---------------------------------------------------------------------------------------
# owner: ask / run now
# ---------------------------------------------------------------------------------------


def _require_enabled(session: Session) -> None:
    if not get_setting(session, "agent", AgentSettings).enabled:
        raise HTTPException(http.HTTP_409_CONFLICT, "Claude runs are disabled in the agent settings.")


@router.post("/agent/ask", response_model=AgentRunOut)
def ask(body: AskBody, _: Role = OwnerDep, session: Session = SessionDep) -> AgentRunOut:
    _require_enabled(session)
    question = body.question.strip()
    if len(question) < 2:
        raise unprocessable([("question", "Ask a question.")])
    run = agent_queue.enqueue(session, "chat", "owner", prompt=question)
    session.flush()
    events.publish(session, "agent_run", run.id)
    out = run_out(run)
    session.commit()
    return out


@router.post("/agent/run", response_model=AgentRunOut)
def run_now(body: RunBody, _: Role = OwnerDep, session: Session = SessionDep) -> AgentRunOut:
    _require_enabled(session)
    queued = session.execute(
        select(AgentRun).where(AgentRun.kind == body.kind, AgentRun.status == "queued")
        .order_by(AgentRun.id.desc()).limit(1)
    ).scalar_one_or_none()
    if queued is not None:
        return run_out(queued)  # already waiting for the agent; don't stack duplicates
    try:
        run = agent_queue.enqueue(session, body.kind, "owner", trigger={"reason": "owner asked to run now"})
    except agent_queue.TriggerLimitReached as exc:
        raise HTTPException(http.HTTP_409_CONFLICT, str(exc) or "Run limit reached for today.") from exc
    session.flush()
    events.publish(session, "agent_run", run.id)
    out = run_out(run)
    session.commit()
    return out


# ---------------------------------------------------------------------------------------
# agent service only
# ---------------------------------------------------------------------------------------


@router.post("/agent/claim", response_model=AgentRunOut | None)
def claim(_: Role = AgentDep, session: Session = SessionDep) -> AgentRunOut | None:
    run = agent_queue.claim_next(session, utcnow())
    if run is None:
        session.commit()
        return None
    session.flush()
    events.publish(session, "agent_run", run.id)
    out = run_out(run)
    session.commit()
    return out


@router.post("/agent/runs/{run_id}/finish", response_model=AgentRunOut)
def finish(
    run_id: int, body: AgentFinishBody, _: Role = AgentDep, session: Session = SessionDep
) -> AgentRunOut:
    run = session.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(http.HTTP_404_NOT_FOUND, f"Unknown agent run {run_id}.")
    if run.status != "running":
        raise HTTPException(http.HTTP_409_CONFLICT, f"Run {run_id} is {run.status}, not running.")
    if body.status == "deferred" and body.not_before is None:
        raise unprocessable([("not_before", "A deferred run needs not_before (when the usage limit resets).")])
    try:
        run = agent_queue.finish(session, run_id, **body.model_dump(exclude_none=True))
    except LookupError as exc:
        raise HTTPException(http.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(http.HTTP_409_CONFLICT, str(exc)) from exc
    session.flush()
    events.publish(session, "agent_run", run.id)
    out = run_out(run)
    session.commit()
    return out


@router.post("/agent/heartbeat", response_model=AgentInfo)
def heartbeat(body: AgentHeartbeatBody, _: Role = AgentDep, session: Session = SessionDep) -> AgentInfo:
    now = utcnow()
    previous = get_heartbeat(session, "agent")
    beat(session, "agent", ok=body.signed_in is not False, **body.model_dump(mode="json"))
    today = local_date(now, house_tz(session))
    safe(session, "agent heartbeat alerts", lambda: _sync_alerts(session, body, today), None)
    if previous is None or previous.detail.get("signed_in") != body.signed_in:
        events.publish(session, "status")
    out = agent_info(session, now)
    session.commit()
    return out
