"""Utility (demand-response) events: the list the web app shows, and the owner's "Skip this
event" with its undo (docs/specs/holds-and-utility-events.md, "Skips").

A skip is an honest opt-out that ecobee records and the utility sees. It is only *requested*
here (``utility_events.skip = 'requested'``, ``skip_by = 'owner'``); the worker's
``climate.utility.skips.run`` sends it once the event runs on top of that thermostat, reads it
back and records the outcome (done / failed / refused). Nothing in the API process talks to a
thermostat. A skip can be taken back until the worker starts sending it: once its
``opt_out_event`` row is 'sent' the opt-out may already have reached ecobee, so the undo
answers 409.

``event_out`` and ``prep_labels`` are shared with the status route (the Live unit cards).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi import status as http
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from climate import state
from climate.api.auth import OwnerDep, ReaderDep, Role
from climate.api.routers.control import SessionDep, safe
from climate.api.schemas import PlanRow, SkipEventBody, UtilityEventOut
from climate.control import controller
from climate.control.policy import HouseState, UnitStatus
from climate.events import publish
from climate.house import UNITS
from climate.store.app_settings import ControlSettings, get_setting
from climate.store.orm import ControlAction, UtilityEvent
from climate.timeutil import utcnow
from climate.utility import events as utility
from climate.utility import skips
from climate.utility.events import row_change_label

log = logging.getLogger(__name__)
router = APIRouter(tags=["utility events"])

OPEN = ("announced", "running")
OWNER_SKIP_REASON = "Skipped from the app"  # the spec's wording; skip rules write their own sentence
PENDING_OR_DONE = ("requested", "done")
UNIT_NAMES = {u.key: u.name for u in UNITS}
# An opt-out logged 'sent' this recently is being sent now (older: the worker died mid-call,
# and the stale row must not block the undo for the rest of the event).
SENDING_MAX_AGE = timedelta(minutes=15)
SENDING = "The opt-out is already being sent to ecobee; it can't be taken back."
WAITING_PHASES = ("skip_waiting_off", "skip_waiting_cloud")
# policy._apply_event_prep's sentence, reworded as a conditional one when nothing is written
_PREP = re.compile(
    r"^Pre-(?P<verb>cool|heat)ing (?P<what>.+?); this hold ends by (?P<end>.+?), before the event starts\.$"
)


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


def _prep_hold_running(session: Session, unit: UnitStatus, now: datetime) -> bool:
    """Our pre-conditioning hold is the one running on the unit: the running hold is ours
    (``set_by_us``), has not ended by the clock, and came from a controller write with rule
    'event_prep' (like ``controller._ours_rule``)."""
    snap = unit.snapshot
    hold = snap.hold if snap is not None else None
    if hold is None or not hold.set_by_us or (hold.end is not None and hold.end <= now):
        return False
    write = state.latest_write(session, unit.unit_key, "controller")
    return write is not None and write.action == "set_hold" and write.rule == "event_prep"


def _prep_effect(
    session: Session, plan_row: PlanRow, house: HouseState, now: datetime,
) -> tuple[Literal["doing", "would"], str] | None:
    """What the controller does about an 'event_prep' plan row: ("doing", "") when it is
    pre-conditioning that unit (act mode, the unit in act_units, and it is about to write the
    prep hold, ``would_write``, or our prep hold is running; the guard's rate-limit block in the
    first minutes of that hold does not count against it); ("would", lead) when it only plans it
    (Suggest mode, or a suggest-only unit: nothing is written); None when it writes nothing and
    plans nothing either: the controller is off, the unit stands aside (a person's hold, the
    back-off after a Resume, a running event), or the guard blocks the prep hold."""
    control = house.control
    unit = house.units.get(plan_row.target.unit_key)
    if control.mode == "off" or unit is None:
        return None
    if controller._stands_aside(unit, house.utility_events, now):
        return None
    if control.mode != "act":
        return "would", "Suggest mode"
    if unit.unit_key not in control.act_units:
        return "would", f"{unit.name} is suggest-only"
    if plan_row.would_write or _prep_hold_running(session, unit, now):
        return "doing", ""
    return None


def _would(reason: str, lead: str) -> str:
    """The plan's prep sentence as a conditional one: "Suggest mode: would pre-cool 2°F before
    the utility event at 3:00 PM, with a hold ending by 2:50 PM. Nothing is written." """
    m = _PREP.match(reason)
    if m is None:
        return f"{lead}, so nothing is written. The plan: {reason}"
    return f"{lead}: would pre-{m['verb']} {m['what']}, with a hold ending by {m['end']}. Nothing is written."


def prep_labels(
    session: Session, rows: Iterable[UtilityEvent], plan_rows: Sequence[PlanRow], house: HouseState | None,
    now: datetime,
) -> dict[int, str]:
    """{event id: label} for each event a unit is pre-cooling (pre-heating) for: the unit's
    plan target is rule 'event_prep', and the event is that unit's earliest announced one, not
    skipped, starting after the prep hold must end. The label is the plan's reason only while
    the controller is actually doing it (``_prep_effect``); in Suggest mode or on a suggest-only
    unit it is a conditional sentence that says nothing is written; otherwise (off, a person's
    hold or a back-off, a blocked guard) there is none. No house state, no labels: never claim
    a pre-cooling that cannot be checked."""
    out: dict[int, str] = {}
    if house is None:
        return out
    candidates = [r for r in rows if r.status == "announced" and r.start_at is not None
                  and r.skip not in PENDING_OR_DONE]
    for plan_row in plan_rows:
        target = plan_row.target
        if target.rule != "event_prep":
            continue
        effect = _prep_effect(session, plan_row, house, now)
        if effect is None:
            continue
        after = target.hold_end_by or now
        mine = [r for r in candidates if r.unit_key == target.unit_key and r.start_at > after]  # type: ignore[operator]
        mine.sort(key=lambda r: (r.start_at, r.id))
        if mine:
            kind, lead = effect
            out[mine[0].id] = target.reason if kind == "doing" else _would(target.reason, lead)
    return out


def _current_plan(session: Session, rows: Sequence[UtilityEvent], now: datetime) -> list[PlanRow]:
    """The controller's current plan, only when an announced event could be prepped for."""
    if not any(r.status == "announced" and r.skip not in PENDING_OR_DONE for r in rows):
        return []
    return safe(session, "controller.current_plan", lambda: controller.current_plan(session, now), [])


def _prep_labels(session: Session, rows: Sequence[UtilityEvent], now: datetime) -> dict[int, str]:
    """``prep_labels`` for the routes below; the house state is loaded only when the plan has a
    prep row."""
    plan_rows = _current_plan(session, rows, now)
    if not any(r.target.rule == "event_prep" for r in plan_rows):
        return {}
    house = safe(session, "state.load_house_state", lambda: state.load_house_state(session, now), None)
    return prep_labels(session, rows, plan_rows, house, now)


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
    preps = _prep_labels(session, rows, now)
    return [event_out(r, now, preps.get(r.id)) for r in rows]


def _locked(session: Session, event_id: int) -> UtilityEvent:
    """The row, with every row of its event (same event_key, the other thermostats) locked in id
    order: the order ingest and the skip runner lock them in, so two of them never deadlock."""
    key = session.execute(
        select(UtilityEvent.event_key).where(UtilityEvent.id == event_id)
    ).scalar_one_or_none()
    if key is None:
        raise HTTPException(http.HTTP_404_NOT_FOUND, f"Unknown utility event {event_id}.")
    rows = session.execute(
        select(UtilityEvent).where(UtilityEvent.event_key == key).order_by(UtilityEvent.id).with_for_update()
    ).scalars().all()
    row = next((r for r in rows if r.id == event_id), None)
    if row is None:  # deleted meanwhile
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


def _sending(session: Session, event_ids: Sequence[int], now: datetime) -> set[int]:
    """The ids among ``event_ids`` whose opt-out the worker is sending right now: an
    ``opt_out_event`` row still 'sent' from the last 15 minutes. The worker commits that row
    under the event rows' locks before it calls ecobee, so with the rows locked here this sees
    every send that has started."""
    if not event_ids:
        return set()
    target = ControlAction.request["event_id"].as_integer()
    return set(session.execute(
        select(target).where(
            ControlAction.action == skips.ACTION,
            ControlAction.status == "sent",
            ControlAction.ts > now - SENDING_MAX_AGE,
            target.in_(list(event_ids)),
        )
    ).scalars())


@router.post("/utility-events/{event_id}/skip", response_model=UtilityEventOut)
def skip_event(
    event_id: int, body: SkipEventBody | None = None, _: Role = OwnerDep, session: Session = SessionDep,
) -> UtilityEventOut:
    """Request an opt-out of this event: on every thermostat it reaches (``all_units``, the
    default; rows that can't be skipped are left as they are) or only this one. 409 when the
    controller is off, or this event is mandatory, over, or already skipped (or waiting to be).
    A skip that failed before (or was taken back after failed attempts) may be requested
    again, with a fresh set of attempts."""
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
        detail = r.detail if isinstance(r.detail, dict) else {}
        if detail.get("skip_attempts"):  # a new request gets the full number of attempts again
            r.detail = {**detail, "skip_attempts": 0}
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
    (a skip ecobee already recorded can't be undone from here), or when the worker is already
    sending this thermostat's opt-out (``_sending``): it may reach ecobee whatever the app
    says. A thermostat whose opt-out is being sent is left out of an ``all_units`` undo for
    the same reason. The undo also forgets the failed attempts (a new request starts afresh;
    a skip rule still asks only once), and resolves the "skip waiting" alerts once nothing of
    the event is waiting."""
    body = body or SkipEventBody()
    now = utcnow()
    row = _locked(session, event_id)
    if row.skip != "requested":
        msg = ("ecobee already recorded this skip; it can't be undone." if row.skip == "done"
               else "No skip of this utility event is waiting to be sent.")
        raise HTTPException(http.HTTP_409_CONFLICT, msg)
    siblings = _siblings(session, row)
    targets = [row] + ([r for r in siblings if r.skip == "requested"] if body.all_units else [])
    sending = _sending(session, [r.id for r in targets], now)
    if row.id in sending:
        raise HTTPException(http.HTTP_409_CONFLICT, SENDING)
    targets = [r for r in targets if r.id not in sending]
    for r in targets:
        r.skip = r.skip_by = r.skip_reason = r.skip_requested_at = None
        if isinstance(r.detail, dict) and "skip_attempts" in r.detail:
            r.detail = {k: v for k, v in r.detail.items() if k != "skip_attempts"}
    session.flush()
    if not any(r.skip == "requested" for r in [row, *siblings]):
        utility.resolve_keys(session, [utility.alert_key(row.event_key, p) for p in WAITING_PHASES])
    publish(session, "status")
    announced = session.execute(  # the unit's other announced events decide which one a prep is for
        select(UtilityEvent).where(UtilityEvent.unit_key == row.unit_key, UtilityEvent.status == "announced")
    ).scalars().all()
    preps = _prep_labels(session, announced, now)
    out = event_out(row, now, preps.get(row.id))
    log.info("owner took back the skip of utility event %s on %s", row.event_key,
             ", ".join(r.unit_key for r in targets))
    session.commit()
    return out
