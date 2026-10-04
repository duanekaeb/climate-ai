"""Utility (demand-response) events: the list the web app shows, and the owner's "Skip this
event" with its undo (docs/specs/holds-and-utility-events.md, "Skips").

A skip is an honest opt-out that ecobee records and the utility sees. It is only *requested*
here (``utility_events.skip = 'requested'``, ``skip_by = 'owner'``); the worker's
``climate.utility.skips.run`` sends it once the event runs on top of that thermostat, reads it
back and records the outcome (done / failed / refused). Nothing in the API process talks to a
thermostat.

``event_out`` and ``prep_labels`` are shared with the status route (the Live unit cards).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from fastapi import status as http
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from climate.api.auth import OwnerDep, ReaderDep, Role
from climate.api.routers.control import SessionDep, safe
from climate.api.schemas import PlanRow, SkipEventBody, UtilityEventOut
from climate.control import controller
from climate.events import publish
from climate.house import UNITS
from climate.store.app_settings import ControlSettings, get_setting
from climate.store.orm import UtilityEvent
from climate.timeutil import utcnow
from climate.utility.events import row_change_label

log = logging.getLogger(__name__)
router = APIRouter(tags=["utility events"])

OPEN = ("announced", "running")
OWNER_SKIP_REASON = "Skipped from the app"  # the spec's wording; skip rules write their own sentence
PENDING_OR_DONE = ("requested", "done")
UNIT_NAMES = {u.key: u.name for u in UNITS}


# ---------------------------------------------------------------------------------------
# shaping (shared with the status route)
# ---------------------------------------------------------------------------------------


def skip_problem(row: UtilityEvent, now: datetime) -> str | None:
    """Why this event cannot be skipped now (the 409 message), or None when it can."""
    if row.status not in OPEN or (row.end_at is not None and row.end_at <= now):
        return "This utility event is over."
    if row.is_optional is False or row.skip == "refused":
        return "This utility event is mandatory: ecobee doesn't allow opting out of it."
    if row.skip == "done":
        return "This utility event was already skipped."
    if row.skip == "requested":
        return "A skip of this utility event is already waiting to be sent."
    return None


def event_out(row: UtilityEvent, now: datetime, prep_label: str | None = None) -> UtilityEventOut:
    return UtilityEventOut(
        id=row.id,
        unit_key=row.unit_key,
        unit_name=UNIT_NAMES.get(row.unit_key, row.unit_key),
        event_key=row.event_key,
        event_type=row.event_type or "demandResponse",
        name=row.name,
        status=row.status,  # type: ignore[arg-type]
        start_at=row.start_at,
        end_at=row.end_at,
        first_seen_at=row.first_seen_at,
        started_at=row.started_at,
        ended_at=row.ended_at,
        heat_f=row.heat_f,
        cool_f=row.cool_f,
        is_relative=bool(row.is_relative),
        heat_offset_f=row.heat_offset_f,
        cool_offset_f=row.cool_offset_f,
        is_optional=row.is_optional,
        duty_cycle_pct=row.duty_cycle_pct,
        change_label=row_change_label(row),
        skip=row.skip,  # type: ignore[arg-type]
        skip_by=row.skip_by,  # type: ignore[arg-type]
        skip_reason=row.skip_reason,
        skip_requested_at=row.skip_requested_at,
        skip_done_at=row.skip_done_at,
        can_skip=skip_problem(row, now) is None,
        prep_label=prep_label,
    )


def prep_labels(rows: Iterable[UtilityEvent], plan_rows: Sequence[PlanRow], now: datetime) -> dict[int, str]:
    """{event id: the plan's reason} for each event a unit is pre-cooling (pre-heating) for
    right now: the unit's plan target is rule 'event_prep', and the event is that unit's
    earliest announced one, not skipped, starting after the prep hold must end."""
    out: dict[int, str] = {}
    candidates = [r for r in rows if r.status == "announced" and r.start_at is not None
                  and r.skip not in PENDING_OR_DONE]
    for plan_row in plan_rows:
        target = plan_row.target
        if target.rule != "event_prep":
            continue
        after = target.hold_end_by or now
        mine = [r for r in candidates if r.unit_key == target.unit_key and r.start_at > after]  # type: ignore[operator]
        mine.sort(key=lambda r: (r.start_at, r.id))
        if mine:
            out[mine[0].id] = target.reason
    return out


def _current_plan(session: Session, rows: Sequence[UtilityEvent], now: datetime) -> list[PlanRow]:
    """The controller's current plan, only when an announced event could be prepped for."""
    if not any(r.status == "announced" and r.skip not in PENDING_OR_DONE for r in rows):
        return []
    return safe(session, "controller.current_plan", lambda: controller.current_plan(session, now), [])


# ---------------------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------------------


@router.get("/utility-events", response_model=list[UtilityEventOut])
def list_events(
    days: Annotated[int, Query(ge=1, le=400)] = 30, _: Role = ReaderDep, session: Session = SessionDep,
) -> list[UtilityEventOut]:
    """Every event still announced or running, plus those that started (or were first seen)
    in the last ``days`` days; newest first (by start)."""
    now = utcnow()
    when = func.coalesce(UtilityEvent.start_at, UtilityEvent.first_seen_at)
    rows = session.execute(
        select(UtilityEvent)
        .where(or_(UtilityEvent.status.in_(OPEN), when >= now - timedelta(days=days)))
        .order_by(when.desc(), UtilityEvent.id.desc())
    ).scalars().all()
    preps = prep_labels(rows, _current_plan(session, rows, now), now)
    return [event_out(r, now, preps.get(r.id)) for r in rows]


def _locked(session: Session, event_id: int) -> UtilityEvent:
    row = session.execute(
        select(UtilityEvent).where(UtilityEvent.id == event_id).with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(http.HTTP_404_NOT_FOUND, f"Unknown utility event {event_id}.")
    return row


def _siblings(session: Session, row: UtilityEvent) -> list[UtilityEvent]:
    """The same event on the other thermostats (same event_key), locked."""
    return list(session.execute(
        select(UtilityEvent)
        .where(UtilityEvent.event_key == row.event_key, UtilityEvent.id != row.id)
        .order_by(UtilityEvent.id)
        .with_for_update()
    ).scalars())


@router.post("/utility-events/{event_id}/skip", response_model=UtilityEventOut)
def skip_event(
    event_id: int, body: SkipEventBody | None = None, _: Role = OwnerDep, session: Session = SessionDep,
) -> UtilityEventOut:
    """Request an opt-out of this event: on every thermostat it reaches (``all_units``, the
    default; rows that can't be skipped are left as they are) or only this one. 409 when the
    controller is off, or this event is mandatory, over, or already skipped (or waiting to be).
    A skip that failed before may be requested again, with a fresh set of attempts."""
    body = body or SkipEventBody()
    now = utcnow()
    row = _locked(session, event_id)
    if get_setting(session, "control", ControlSettings).mode == "off":
        raise HTTPException(
            http.HTTP_409_CONFLICT,
            "The controller is off: switch it to Suggest or Act to skip a utility event "
            "(the worker sends the opt-out).",
        )
    problem = skip_problem(row, now)
    if problem is not None:
        raise HTTPException(http.HTTP_409_CONFLICT, problem)
    targets = [row] + ([r for r in _siblings(session, row) if skip_problem(r, now) is None] if body.all_units else [])
    for r in targets:
        if r.skip == "failed":  # a new request gets the full number of attempts again
            r.detail = {**(r.detail if isinstance(r.detail, dict) else {}), "skip_attempts": 0}
        r.skip, r.skip_by, r.skip_reason, r.skip_requested_at = "requested", "owner", OWNER_SKIP_REASON, now
    session.flush()
    publish(session, "status")
    out = event_out(row, now)  # a requested skip ends any pre-cooling for it
    log.info("owner requested a skip of utility event %s on %s", row.event_key,
             ", ".join(r.unit_key for r in targets))
    session.commit()
    return out


@router.post("/utility-events/{event_id}/unskip", response_model=UtilityEventOut)
def unskip_event(
    event_id: int, body: SkipEventBody | None = None, _: Role = OwnerDep, session: Session = SessionDep,
) -> UtilityEventOut:
    """Take back a skip that has not been sent yet (still 'requested'): on every thermostat
    of this event (``all_units``, the default) or only this one. 409 when nothing is waiting
    (a skip ecobee already recorded can't be undone from here)."""
    body = body or SkipEventBody()
    now = utcnow()
    row = _locked(session, event_id)
    if row.skip != "requested":
        msg = ("ecobee already recorded this skip; it can't be undone." if row.skip == "done"
               else "No skip of this utility event is waiting to be sent.")
        raise HTTPException(http.HTTP_409_CONFLICT, msg)
    targets = [row] + ([r for r in _siblings(session, row) if r.skip == "requested"] if body.all_units else [])
    for r in targets:
        r.skip = r.skip_by = r.skip_reason = r.skip_requested_at = None
    session.flush()
    publish(session, "status")
    announced = session.execute(  # the unit's other announced events decide which one a prep is for
        select(UtilityEvent).where(UtilityEvent.unit_key == row.unit_key, UtilityEvent.status == "announced")
    ).scalars().all()
    preps = prep_labels(announced, _current_plan(session, announced, now), now)
    out = event_out(row, now, preps.get(row.id))
    log.info("owner took back the skip of utility event %s on %s", row.event_key,
             ", ".join(r.unit_key for r in targets))
    session.commit()
    return out
