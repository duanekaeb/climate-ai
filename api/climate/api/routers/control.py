"""Control routes: owner settings, controller mode, the plan, the action log, owner holds,
"Resume schedule" and "Back to automatic", presence, "Hand back to ecobee", and the policy
change gates (``/changes``).

Owner holds and resumes are only *queued* here (``control_actions.status = 'queued'``); the
worker runs them through the guardrails, writes, reads back and records the result. The
hand-back is a ``jobs`` row the worker runs (``controller.hand_back``). Nothing in the API
process talks to a thermostat.

This module also holds the few helpers the other routers share (``safe``, ``unprocessable``,
``actor_for``, ``house_tz``, ``active_policy``, ``controller_info``, ``job_out``, the
settings checks and the control-token attribution ``token_ref`` / ``by_caller`` /
``audit_token``), so the import direction stays one-way: other routers import from here,
never the reverse.

A control token acts with the owner's everyday controls but is recorded as itself: its
holds, resumes and presence changes carry ``request["by_token"] = {"id", "name"}`` and a
reason that starts ``Control token "<name>":``, and each one writes an audit row
(``control.hold`` / ``control.resume`` / ``control.automatic`` / ``control.presence``).
Settings read by a viewer or control token leave out the house's coordinates and the
household's schedule (``settings_out(full=False)``). The owner and the agent (which reasons
about sleep windows and presence) see them in full.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Literal, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi import status as http
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from climate import auth_service, events
from climate.api.auth import ControlCallerDep, OwnerDep, Principal, ReaderDep, Role, WriterDep, client_ip
from climate.api.schemas import (
    ChangeOut,
    ControlActionOut,
    ControllerInfo,
    DecisionBody,
    HandbackInfo,
    HandbackStep,
    JobOut,
    ManualHoldBody,
    ModeBody,
    PlanOut,
    PresenceBody,
    ProposePolicyBody,
    SettingsOut,
    SettingsUpdate,
    UnitBody,
)
from climate.control import changes, controller
from climate.control.guardrails import _deadband as thermostat_deadband
from climate.control.guardrails import round_setpoint
from climate.control.handback import load_originals
from climate.control.policy import CLAUDE_SIGNOFF_RANGES, OWNER_ONLY_PARAMS, PolicyParams
from climate.house import ROOM_BY_KEY, UNIT_KEYS
from climate.sources.base import HoldRequest
from climate.store.app_settings import (
    AgentSettings,
    ControlSettings,
    LocationSettings,
    OccupancySettings,
    SourceSettings,
    UtilityEventSettings,
    get_heartbeat,
    get_setting,
    put_setting,
)
from climate.store.db import get_session
from climate.store.orm import AppSetting, Change, ControlAction, Job, LiveUnit, PolicyVersion, Unit
from climate.timeutil import utcnow

log = logging.getLogger(__name__)
router = APIRouter(tags=["control"])
T = TypeVar("T")
SessionDep = Depends(get_session)  # module-level, like auth.OwnerDep

ChangeStatus = Literal[
    "rejected", "backtest", "shadow", "awaiting_signoff", "held", "trial", "active", "retired", "cancelled"
]
# control_actions.request.kind of the owner's two resumes (climate.state reads them):
# "Resume schedule" starts the resume back-off; "Back to automatic" cancels it.
RESUME_SCHEDULE = "resume_schedule"
AUTOMATIC = "automatic"
HANDBACK_JOB = "handback"
FINISHED = ("done", "failed")  # jobs.status of a job the worker is through with

# ---------------------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------------------


def safe(session: Session, label: str, fn: Callable[[], T], default: T) -> T:
    """Run ``fn`` inside a SAVEPOINT; on any error log it and return ``default``.

    Readers that assemble a page from several services use this so one failing service
    degrades its part instead of failing the request (and a DB error inside it does not
    poison the rest of the transaction)."""
    try:
        with session.begin_nested():
            return fn()
    except Exception:
        log.warning("%s failed; serving without it", label, exc_info=True)
        return default


def unprocessable(violations: list[tuple[str, str]]) -> HTTPException:
    """422 in FastAPI's own validation-error shape, so clients show every message."""
    detail = [
        {"loc": ["body", *loc.split(".")] if loc else ["body"], "msg": msg, "type": "value_error"}
        for loc, msg in violations
    ]
    return HTTPException(http.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)


def actor_for(role: Role) -> Literal["owner", "claude"]:
    """The agent bearer token acts as Claude; the signed-in browser acts as the owner."""
    return "claude" if role == "agent" else "owner"


def token_ref(principal: Principal) -> dict | None:
    """``{"id", "name"}`` of the API token behind a caller, or None for the owner."""
    if principal.kind not in ("api_token", "env_token"):
        return None
    return {"id": principal.token_id, "name": principal.label}


def by_caller(principal: Principal, owner_text: str, token_text: str) -> str:
    """The owner's sentence, or the token's one prefixed ``Control token "<name>": ``."""
    if token_ref(principal) is None:
        return owner_text
    return f'Control token "{principal.label}": {token_text}'


def audit_token(
    session: Session, principal: Principal, event_type: str, request: Request, *, target_type: str,
    target_id: str | int | None, payload: dict | None = None,
) -> None:
    """An audit row for an everyday control made with an API token (the owner's own controls
    are not audited), in the caller's transaction."""
    if token_ref(principal) is None:
        return
    auth_service.audit(session, principal, event_type, target_type=target_type, target_id=target_id,
                       payload=payload, ip=client_ip(request))


def valid_tz(tz: str) -> bool:
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def house_tz(session: Session) -> str:
    tz = get_setting(session, "location", LocationSettings).tz
    return tz if valid_tz(tz) else "UTC"


def active_policy(session: Session) -> tuple[PolicyParams, int | None]:
    """The active policy via ``changes.active_policy``; falls back to reading the
    ``policy_versions`` row directly so settings and status never fail on it."""
    got = safe(session, "changes.active_policy", lambda: changes.active_policy(session), None)
    if got is not None:
        return got
    row = session.execute(
        select(PolicyVersion)
        .where(PolicyVersion.status == "active")
        .order_by(PolicyVersion.created_at.desc(), PolicyVersion.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        return PolicyParams(), None
    try:
        return PolicyParams.model_validate(row.params), row.id
    except ValidationError:
        log.warning("active policy_versions row %s does not parse; using defaults", row.id)
        return PolicyParams(), row.id


def parse_dt(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)  # 3.11+ accepts a trailing "Z"
        except ValueError:
            return None
    return None


def job_out(job: Job) -> JobOut:
    return JobOut.model_validate(job, from_attributes=True)


def controller_info(session: Session) -> ControllerInfo:
    mode = get_setting(session, "control", ControlSettings).mode
    hb = get_heartbeat(session, "worker")
    last_tick = parse_dt(hb.detail.get("last_tick_at")) if hb else None
    policy, policy_id = active_policy(session)
    return ControllerInfo(mode=mode, last_tick_at=last_tick, policy_version_id=policy_id, policy=policy)


# ---------------------------------------------------------------------------------------
# settings validation (owner edits)
# ---------------------------------------------------------------------------------------


def control_violations(c: ControlSettings) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    lim = c.limits
    if lim.min_heat_f < 45 or lim.max_heat_f > 80:
        out.append(("control.limits", "Heat limits must stay within 45-80°F."))
    if lim.min_cool_f < 65 or lim.max_cool_f > 92:
        out.append(("control.limits", "Cool limits must stay within 65-92°F."))
    if lim.min_heat_f > lim.max_heat_f:
        out.append(("control.limits.min_heat_f", "min_heat_f is above max_heat_f."))
    if lim.min_cool_f > lim.max_cool_f:
        out.append(("control.limits.min_cool_f", "min_cool_f is above max_cool_f."))
    if lim.min_deadband_f <= 0:
        out.append(("control.limits.min_deadband_f", "The deadband must be positive."))
    if lim.max_step_f <= 0:
        out.append(("control.limits.max_step_f", "max_step_f must be positive."))
    if lim.min_minutes_between_changes < 0 or lim.min_run_minutes < 0:
        out.append(("control.limits", "Minute limits cannot be negative."))
    if not 1 <= lim.min_hold_hours <= lim.max_hold_hours <= 2:
        out.append(("control.limits", "Holds are 1-2 hours: need 1 <= min_hold_hours <= max_hold_hours <= 2."))
    elif not lim.min_hold_hours <= c.hold_hours <= lim.max_hold_hours:
        out.append(("control.hold_hours", f"hold_hours must be within {lim.min_hold_hours}-{lim.max_hold_hours}."))
    if not 0 < lim.max_indoor_rh <= 100:
        out.append(("control.limits.max_indoor_rh", "max_indoor_rh must be a percentage."))

    missing = [u for u in UNIT_KEYS if u not in c.comfort]
    unknown = [u for u in c.comfort if u not in UNIT_KEYS]
    if missing:
        out.append(("control.comfort", f"Comfort bands missing for: {', '.join(missing)}."))
    if unknown:
        out.append(("control.comfort", f"Unknown units: {', '.join(unknown)}."))
    for unit_key, uc in c.comfort.items():
        if unit_key not in UNIT_KEYS:
            continue
        for period in ("day", "night", "away"):
            band = getattr(uc, period)
            loc = f"control.comfort.{unit_key}.{period}"
            if not lim.min_heat_f <= band.heat_f <= lim.max_heat_f:
                out.append((loc, (f"{unit_key} {period}: heat {band.heat_f}°F is outside the hard limits "
                                  f"{lim.min_heat_f}-{lim.max_heat_f}°F.")))
            if not lim.min_cool_f <= band.cool_f <= lim.max_cool_f:
                out.append((loc, (f"{unit_key} {period}: cool {band.cool_f}°F is outside the hard limits "
                                  f"{lim.min_cool_f}-{lim.max_cool_f}°F.")))
            if band.cool_f - band.heat_f < lim.min_deadband_f:
                out.append((loc, f"{unit_key} {period}: heat must be at least {lim.min_deadband_f}°F below cool."))
    bad_units = [u for u in c.act_units if u not in UNIT_KEYS]
    if bad_units:
        out.append(("control.act_units", f"Unknown units: {', '.join(bad_units)}."))
    s = c.schedule
    if any(d < 0 or d > 6 for d in [*s.school_days, *s.office_days]):
        out.append(("control.schedule", "Days must be 0..6 (Monday=0)."))
    return out


def occupancy_violations(o: OccupancySettings) -> list[tuple[str, str]]:
    unknown = [k for k in o.sleep_windows if k not in ROOM_BY_KEY]
    return [("occupancy.sleep_windows", f"Unknown rooms: {', '.join(unknown)}.")] if unknown else []


def location_violations(loc: LocationSettings, prefix: str = "location") -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    p = f"{prefix}." if prefix else ""
    if not valid_tz(loc.tz):
        out.append((f"{p}tz", f"Unknown time zone {loc.tz!r}."))
    if (loc.lat is None) != (loc.lon is None):
        out.append((f"{p}lat", "Give both latitude and longitude, or neither."))
    if loc.lat is not None and not -90 <= loc.lat <= 90:
        out.append((f"{p}lat", "Latitude must be within -90..90."))
    if loc.lon is not None and not -180 <= loc.lon <= 180:
        out.append((f"{p}lon", "Longitude must be within -180..180."))
    return out


# The agent verifies comfort against sleep windows (sleep counts as occupied) and presence.
FULL_SETTINGS_ROLES = frozenset({"owner", "agent"})


def settings_out(session: Session, *, full: bool = True) -> SettingsOut:
    """The settings page. ``full=False`` (viewer and control tokens) keeps the time zone but
    leaves out the house's coordinates, ZIP and label, the sleep windows (the household's
    schedule) and whether the adults' phones are away."""
    policy, policy_id = active_policy(session)
    location = get_setting(session, "location", LocationSettings)
    occupancy = get_setting(session, "occupancy", OccupancySettings)
    if not full:
        location = LocationSettings(tz=location.tz, confirmed=location.confirmed)
        occupancy = occupancy.model_copy(update={"sleep_windows": {}, "phones_away": None, "phones_updated_at": None})
    return SettingsOut(
        control=get_setting(session, "control", ControlSettings),
        occupancy=occupancy,
        location=location,
        agent=get_setting(session, "agent", AgentSettings),
        utility_events=get_setting(session, "utility_events", UtilityEventSettings),
        policy=policy,
        policy_version_id=policy_id,
        signoff_ranges={k: (float(lo), float(hi)) for k, (lo, hi) in CLAUDE_SIGNOFF_RANGES.items()},
        owner_only_params=sorted(OWNER_ONLY_PARAMS),
    )


def require_unit(session: Session, unit_key: str) -> Unit:
    unit = session.get(Unit, unit_key)
    if unit is None:
        raise HTTPException(http.HTTP_404_NOT_FOUND, f"Unknown unit {unit_key!r}.")
    return unit


def action_out(row: ControlAction) -> ControlActionOut:
    return ControlActionOut.model_validate(row, from_attributes=True)


# ---------------------------------------------------------------------------------------
# settings / mode / plan
# ---------------------------------------------------------------------------------------


@router.get("/control/settings", response_model=SettingsOut)
def get_settings_route(role: Role = ReaderDep, session: Session = SessionDep) -> SettingsOut:
    """Every role may read the settings; only the owner and the agent see the house's location
    beyond its time zone, the sleep windows and the phones (``settings_out``)."""
    return settings_out(session, full=role in FULL_SETTINGS_ROLES)


@router.put("/control/settings", response_model=SettingsOut)
def put_settings_route(
    body: SettingsUpdate, _: Role = OwnerDep, session: Session = SessionDep
) -> SettingsOut:
    """Save the sections given. ``control.mode`` is never changed here: the stored mode is
    kept whatever the body says (read under a row lock, so a hand-back committing meanwhile is
    not undone either). The mode changes only through ``POST /control/mode`` and the worker's
    hand-back, which switches it off, so a form loaded before a hand-back cannot switch the
    controller back on by saving its stale copy of the control settings."""
    violations: list[tuple[str, str]] = []
    if body.control is not None:
        session.execute(select(AppSetting.key).where(AppSetting.key == "control").with_for_update())
        stored = get_setting(session, "control", ControlSettings).mode
        if body.control.mode != stored:
            log.info("PUT /control/settings: ignoring control.mode %r (stored %r; use POST /control/mode)",
                     body.control.mode, stored)
        body.control = body.control.model_copy(update={"mode": stored})
        violations += control_violations(body.control)
    if body.occupancy is not None:
        violations += occupancy_violations(body.occupancy)
    if body.location is not None:
        violations += location_violations(body.location)
    if violations:
        raise unprocessable(violations)
    updates = (
        ("control", body.control), ("occupancy", body.occupancy), ("location", body.location), ("agent", body.agent),
        ("utility_events", body.utility_events),
    )
    for key, value in updates:
        if value is not None:
            put_setting(session, key, value, updated_by="owner")
    if any(value is not None for _, value in updates):
        events.publish(session, "status")
    out = settings_out(session)
    session.commit()
    return out


@router.post("/control/mode", response_model=ControllerInfo)
def set_mode(body: ModeBody, _: Role = OwnerDep, session: Session = SessionDep) -> ControllerInfo:
    current = get_setting(session, "control", ControlSettings)
    if current.mode != body.mode:
        current.mode = body.mode
        put_setting(session, "control", current, updated_by="owner")
        events.publish(session, "status")
    out = controller_info(session)
    session.commit()
    return out


@router.get("/control/plan", response_model=PlanOut)
def get_plan(_: Role = ReaderDep, session: Session = SessionDep) -> PlanOut:
    now = utcnow()
    rows = controller.current_plan(session, now)
    mode = get_setting(session, "control", ControlSettings).mode
    return PlanOut(at=now, mode=mode, rows=rows)


# ---------------------------------------------------------------------------------------
# action log, owner holds, presence
# ---------------------------------------------------------------------------------------


@router.get("/control/actions", response_model=list[ControlActionOut])
def list_actions(
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    unit_key: str | None = None,
    _: Role = ReaderDep,
    session: Session = SessionDep,
) -> list[ControlActionOut]:
    q = select(ControlAction).order_by(ControlAction.ts.desc(), ControlAction.id.desc()).limit(limit)
    if unit_key:
        require_unit(session, unit_key)
        q = q.where(ControlAction.unit_key == unit_key)
    return [action_out(r) for r in session.execute(q).scalars()]


def _queue_action(
    session: Session, unit_key: str, action: str, reason: str, request: dict, *, principal: Principal,
    http_request: Request, event_type: str,
) -> ControlActionOut:
    """Queue the owner's action for the worker, on the source's channel. While the ecobee cloud
    circuit is open and HomeKit has taken over (``controller._homekit_fallback``), a resume
    ("Resume schedule" / "Back to automatic") goes on channel 'homekit' instead, keeping its
    ``request.kind``: the homekit service clears the hold locally, so a person's hold seen
    during the outage can still be ended from the app (the cloud call would only fail).
    Owner holds stay on the source's channel. A control token's action keeps ``actor`` 'owner'
    (the worker runs it like the owner's) but names the token in ``request.by_token`` and is
    audited (``event_type``)."""
    token = token_ref(principal)
    if token is not None:
        request = {**request, "by_token": token}
    src = get_setting(session, "source", SourceSettings)
    kind = src.kind
    if action == "resume_program" and controller._homekit_fallback(src, None, utcnow()):
        kind = "homekit"
    row = ControlAction(
        unit_key=unit_key,
        actor="owner",
        mode="act",
        channel=kind,
        action=action,
        status="queued",
        rule="manual",
        reason=reason,
        request=request,
    )
    session.add(row)
    session.flush()
    audit_token(session, principal, event_type, http_request, target_type="control_action", target_id=row.id,
                payload={"unit_key": unit_key, "action": action, "reason": reason})
    events.publish(session, "action", row.id)
    out = action_out(row)
    session.commit()
    return out


def _live_settings(session: Session, unit_key: str) -> dict:
    """The unit's thermostat settings from its live snapshot (empty when there is none)."""
    live = session.get(LiveUnit, unit_key)
    snap = live.snapshot if live is not None and isinstance(live.snapshot, dict) else {}
    settings = snap.get("settings")
    return settings if isinstance(settings, dict) else {}


@router.post("/control/hold", response_model=ControlActionOut)
def manual_hold(
    body: ManualHoldBody, http_request: Request, principal: Principal = ControlCallerDep,
    session: Session = SessionDep,
) -> ControlActionOut:
    """Queue the owner's hold: exactly what was typed, checked here against the hard envelope
    only (min/max heat and cool, the deadband or the thermostat's own heatCoolMinDelta when
    larger, the 0.5°F grid). No step limit, humidity guard, rate limit or back-off applies to
    the owner; the worker still refuses it while a utility, vacation or unrecognised ecobee
    event runs, or with the controller off."""
    require_unit(session, body.unit_key)
    lim = get_setting(session, "control", ControlSettings).limits
    heat, cool = round_setpoint(body.heat_f), round_setpoint(body.cool_f)
    violations: list[tuple[str, str]] = []
    if not lim.min_heat_f <= heat <= lim.max_heat_f:
        violations.append(("heat_f", f"Heat {heat}°F is outside the hard limits {lim.min_heat_f}-{lim.max_heat_f}°F."))
    if not lim.min_cool_f <= cool <= lim.max_cool_f:
        violations.append(("cool_f", f"Cool {cool}°F is outside the hard limits {lim.min_cool_f}-{lim.max_cool_f}°F."))
    gap = thermostat_deadband(lim, _live_settings(session, body.unit_key))
    if cool - heat < lim.min_deadband_f:
        violations.append(("cool_f", f"Heat must be at least {lim.min_deadband_f}°F below cool."))
    elif cool - heat < gap:
        msg = f"Heat must be at least {gap:g}°F below cool (the thermostat's own minimum gap)."
        violations.append(("cool_f", msg))
    if not lim.min_hold_hours <= body.hours <= lim.max_hold_hours:
        violations.append(("hours", f"Holds last {lim.min_hold_hours}-{lim.max_hold_hours} hours."))
    if violations:
        raise unprocessable(violations)
    what = f"heat {heat:g}°F / cool {cool:g}°F for {body.hours} h"
    reason = by_caller(principal, f"Owner hold: {what}", f"hold {what}")
    request = HoldRequest(unit_key=body.unit_key, heat_f=heat, cool_f=cool, hours=body.hours, reason=reason)
    return _queue_action(session, body.unit_key, "set_hold", reason, request.model_dump(mode="json"),
                         principal=principal, http_request=http_request, event_type="control.hold")


def _hours(h: float) -> str:
    return f"{h:g} h"


@router.post("/control/resume", response_model=ControlActionOut)
def resume(
    body: UnitBody, http_request: Request, principal: Principal = ControlCallerDep, session: Session = SessionDep,
) -> ControlActionOut:
    """The owner's "Resume schedule": cancel the running plain hold, whoever set it, and let
    the ecobee schedule run; the controller waits ``resume_backoff_hours`` after the resume is
    verified."""
    require_unit(session, body.unit_key)
    wait = get_setting(session, "control", ControlSettings).resume_backoff_hours
    if wait > 0:
        what = f"resume schedule (the ecobee schedule runs; the controller waits {_hours(wait)})"
    else:
        what = "resume schedule (no wait after a resume is set, so the controller steers again at once)"
    reason = by_caller(principal, f"Owner: {what}", what)
    request = {"kind": RESUME_SCHEDULE, "unit_key": body.unit_key, "reason": reason}
    return _queue_action(session, body.unit_key, "resume_program", reason, request,
                         principal=principal, http_request=http_request, event_type="control.resume")


@router.post("/control/automatic", response_model=ControlActionOut)
def automatic(
    body: UnitBody, http_request: Request, principal: Principal = ControlCallerDep, session: Session = SessionDep,
) -> ControlActionOut:
    """The owner's "Back to automatic": cancel the running plain hold, whoever set it, and let
    the controller steer again at once (it also ends a resume back-off). With no hold running
    it is a verified no-op that still ends the back-off. It does not delay the controller's
    next write (it is not counted toward the rate limit)."""
    require_unit(session, body.unit_key)
    what = "back to automatic (the controller steers again now)"
    reason = by_caller(principal, f"Owner: {what}", what)
    request = {"kind": AUTOMATIC, "unit_key": body.unit_key, "reason": reason}
    return _queue_action(session, body.unit_key, "resume_program", reason, request,
                         principal=principal, http_request=http_request, event_type="control.automatic")


@router.post("/control/presence", response_model=SettingsOut)
def presence(
    body: PresenceBody, http_request: Request, principal: Principal = ControlCallerDep,
    session: Session = SessionDep,
) -> SettingsOut:
    """Set whether the adults' phones are away. A control token's change is recorded as the
    token's (``updated_by`` 'token:<id>', audit ``control.presence``) and answered with the
    settings as a non-owner reads them, plus the phones value it just wrote."""
    occ = get_setting(session, "occupancy", OccupancySettings)
    occ.phones_away = body.phones_away
    occ.phones_updated_at = utcnow()
    token = token_ref(principal)
    put_setting(session, "occupancy", occ, updated_by="owner" if token is None else f"token:{token['id']}")
    audit_token(session, principal, "control.presence", http_request, target_type="setting", target_id="occupancy",
                payload={"phones_away": body.phones_away})
    events.publish(session, "status")
    out = settings_out(session, full=principal.role in FULL_SETTINGS_ROLES)
    if principal.role != "owner":  # its own answer, nothing it did not just write
        out.occupancy = out.occupancy.model_copy(
            update={"phones_away": occ.phones_away, "phones_updated_at": occ.phones_updated_at})
    session.commit()
    return out


# ---------------------------------------------------------------------------------------
# hand back to ecobee
# ---------------------------------------------------------------------------------------


def _handback_steps(job: Job | None) -> list[HandbackStep]:
    raw = job.result.get("steps") if job is not None and isinstance(job.result, dict) else None
    steps: list[HandbackStep] = []
    for item in raw if isinstance(raw, list) else []:
        try:
            steps.append(HandbackStep.model_validate(item))
        except ValidationError:
            log.warning("handback job %s has a step that does not parse; leaving it out", job.id if job else None)
    return steps


def _latest_handback(session: Session, statuses: tuple[str, ...] | None = None) -> Job | None:
    q = select(Job).where(Job.kind == HANDBACK_JOB)
    if statuses is not None:
        q = q.where(Job.status.in_(statuses))
    return session.execute(q.order_by(Job.created_at.desc(), Job.id.desc()).limit(1)).scalar_one_or_none()


@router.get("/control/handback", response_model=HandbackInfo)
def get_handback(_: Role = ReaderDep, session: Session = SessionDep) -> HandbackInfo:
    """What "Hand back to ecobee" would restore (each unit's ecobee settings as captured before
    the controller's first change), the controller mode, the latest hand-back job, and the
    steps of the last one that finished."""
    last = _latest_handback(session)
    finished = last if last is not None and last.status in FINISHED else _latest_handback(session, FINISHED)
    return HandbackInfo(
        original=load_originals(session),
        mode=get_setting(session, "control", ControlSettings).mode,
        last_job=job_out(last) if last is not None else None,
        steps=_handback_steps(finished),
    )


@router.post("/control/handback", response_model=JobOut)
def post_handback(_: Role = OwnerDep, session: Session = SessionDep) -> JobOut:
    """Queue the hand-back for the worker: the controller is switched off first, then our holds
    are resumed and the Home sensors, Smart Away and Follow Me are put back as captured.
    People's holds, vacations and utility events are left alone. 409 while one is already
    queued or running."""
    pending = _latest_handback(session, ("queued", "running"))
    if pending is not None:
        raise HTTPException(http.HTTP_409_CONFLICT, f"A hand-back is already {pending.status} (job {pending.id}).")
    job = Job(kind=HANDBACK_JOB, status="queued", requested_by="owner", params={})
    session.add(job)
    session.flush()
    out = job_out(job)
    session.commit()
    return out


# ---------------------------------------------------------------------------------------
# change gates
# ---------------------------------------------------------------------------------------


def change_out(c: Change) -> ChangeOut:
    return ChangeOut(
        id=c.id,
        created_at=c.created_at,
        kind=c.kind,
        title=c.title,
        rationale=c.rationale or "",
        payload=c.payload or {},
        proposed_by=c.proposed_by,
        status=c.status,
        gates=c.gates or {},
        decided_by=c.decided_by,
        decided_at=c.decided_at,
        decision_reason=c.decision_reason,
        shadow_start=c.shadow_start,
        trial_start=c.trial_start,
        trial_end=c.trial_end,
        needs=changes.needs(c),
    )


@router.get("/changes", response_model=list[ChangeOut])
def list_changes(
    status: ChangeStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    _: Role = ReaderDep,
    session: Session = SessionDep,
) -> list[ChangeOut]:
    q = select(Change).order_by(Change.created_at.desc(), Change.id.desc()).limit(limit)
    if status is not None:
        q = q.where(Change.status == status)
    return [change_out(c) for c in session.execute(q).scalars()]


@router.post("/changes", response_model=ChangeOut)
def propose_change(
    body: ProposePolicyBody, role: Role = WriterDep, session: Session = SessionDep
) -> ChangeOut:
    if not body.params:
        raise unprocessable([("params", "Name at least one policy parameter to change.")])
    try:
        change = changes.propose_policy_change(session, actor_for(role), body.title, body.rationale, body.params)
    except ValueError as exc:
        raise HTTPException(http.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    session.flush()
    events.publish(session, "change", change.id)
    out = change_out(change)
    session.commit()
    return out


@router.post("/changes/{change_id}/decision", response_model=ChangeOut)
def decide_change(
    change_id: int, body: DecisionBody, role: Role = WriterDep, session: Session = SessionDep
) -> ChangeOut:
    if session.get(Change, change_id) is None:
        raise HTTPException(http.HTTP_404_NOT_FOUND, f"Unknown change {change_id}.")
    try:
        change = changes.decide(session, change_id, actor_for(role), body.decision, body.reason)
    except PermissionError as exc:
        raise HTTPException(http.HTTP_403_FORBIDDEN, str(exc) or "You may not decide this change.") from exc
    except ValueError as exc:
        raise HTTPException(http.HTTP_409_CONFLICT, str(exc) or "This change is not awaiting a decision.") from exc
    session.flush()
    events.publish(session, "change", change.id)
    out = change_out(change)
    session.commit()
    return out
