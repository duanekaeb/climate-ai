"""Control routes: owner settings, controller mode, the plan, the action log, owner holds and
resumes, presence, and the policy change gates (``/changes``).

Owner holds are only *queued* here (``control_actions.status = 'queued'``); the worker runs
them through the guardrails, writes, reads back and records the result. Nothing in the API
process talks to a thermostat.

This module also holds the few helpers the other routers share (``safe``, ``unprocessable``,
``actor_for``, ``house_tz``, ``active_policy``, ``controller_info`` and the settings checks),
so the import direction stays one-way: other routers import from here, never the reverse.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Literal, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from climate import events
from climate.api.auth import OwnerDep, ReaderDep, Role, WriterDep
from climate.api.schemas import (
    ChangeOut,
    ControlActionOut,
    ControllerInfo,
    DecisionBody,
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
from climate.control.guardrails import round_setpoint
from climate.control.policy import CLAUDE_SIGNOFF_RANGES, OWNER_ONLY_PARAMS, PolicyParams
from climate.house import ROOM_BY_KEY, UNIT_KEYS
from climate.sources.base import HoldRequest
from climate.store.app_settings import (
    ControlSettings,
    LocationSettings,
    OccupancySettings,
    SourceSettings,
    get_heartbeat,
    get_setting,
    put_setting,
)
from climate.store.db import get_session
from climate.store.orm import Change, ControlAction, PolicyVersion, Unit
from climate.timeutil import utcnow

log = logging.getLogger(__name__)
router = APIRouter(tags=["control"])
T = TypeVar("T")
SessionDep = Depends(get_session)  # module-level, like auth.OwnerDep

ChangeStatus = Literal[
    "rejected", "backtest", "shadow", "awaiting_signoff", "held", "trial", "active", "retired", "cancelled"
]

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
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


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
                out.append((loc, f"{unit_key} {period}: heat {band.heat_f}°F is outside the hard limits "
                                 f"{lim.min_heat_f}-{lim.max_heat_f}°F."))
            if not lim.min_cool_f <= band.cool_f <= lim.max_cool_f:
                out.append((loc, f"{unit_key} {period}: cool {band.cool_f}°F is outside the hard limits "
                                 f"{lim.min_cool_f}-{lim.max_cool_f}°F."))
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


def settings_out(session: Session) -> SettingsOut:
    policy, policy_id = active_policy(session)
    return SettingsOut(
        control=get_setting(session, "control", ControlSettings),
        occupancy=get_setting(session, "occupancy", OccupancySettings),
        location=get_setting(session, "location", LocationSettings),
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
def get_settings_route(_: Role = ReaderDep, session: Session = SessionDep) -> SettingsOut:
    return settings_out(session)


@router.put("/control/settings", response_model=SettingsOut)
def put_settings_route(
    body: SettingsUpdate, _: Role = OwnerDep, session: Session = SessionDep
) -> SettingsOut:
    violations: list[tuple[str, str]] = []
    if body.control is not None:
        violations += control_violations(body.control)
    if body.occupancy is not None:
        violations += occupancy_violations(body.occupancy)
    if body.location is not None:
        violations += location_violations(body.location)
    if violations:
        raise unprocessable(violations)
    for key, value in (("control", body.control), ("occupancy", body.occupancy), ("location", body.location)):
        if value is not None:
            put_setting(session, key, value, updated_by="owner")
    if body.control or body.occupancy or body.location:
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


def _queue_action(session: Session, unit_key: str, action: str, reason: str, request: dict) -> ControlActionOut:
    kind = get_setting(session, "source", SourceSettings).kind
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
    events.publish(session, "action", row.id)
    out = action_out(row)
    session.commit()
    return out


@router.post("/control/hold", response_model=ControlActionOut)
def manual_hold(body: ManualHoldBody, _: Role = OwnerDep, session: Session = SessionDep) -> ControlActionOut:
    require_unit(session, body.unit_key)
    lim = get_setting(session, "control", ControlSettings).limits
    heat, cool = round_setpoint(body.heat_f), round_setpoint(body.cool_f)
    violations: list[tuple[str, str]] = []
    if not lim.min_heat_f <= heat <= lim.max_heat_f:
        violations.append(("heat_f", f"Heat {heat}°F is outside the hard limits {lim.min_heat_f}-{lim.max_heat_f}°F."))
    if not lim.min_cool_f <= cool <= lim.max_cool_f:
        violations.append(("cool_f", f"Cool {cool}°F is outside the hard limits {lim.min_cool_f}-{lim.max_cool_f}°F."))
    if cool - heat < lim.min_deadband_f:
        violations.append(("cool_f", f"Heat must be at least {lim.min_deadband_f}°F below cool."))
    if not lim.min_hold_hours <= body.hours <= lim.max_hold_hours:
        violations.append(("hours", f"Holds last {lim.min_hold_hours}-{lim.max_hold_hours} hours."))
    if violations:
        raise unprocessable(violations)
    reason = f"Owner hold: heat {heat:g}°F / cool {cool:g}°F for {body.hours} h"
    request = HoldRequest(unit_key=body.unit_key, heat_f=heat, cool_f=cool, hours=body.hours, reason=reason)
    return _queue_action(session, body.unit_key, "set_hold", reason, request.model_dump(mode="json"))


@router.post("/control/resume", response_model=ControlActionOut)
def resume(body: UnitBody, _: Role = OwnerDep, session: Session = SessionDep) -> ControlActionOut:
    require_unit(session, body.unit_key)
    reason = "Owner: resume the thermostat's schedule"
    return _queue_action(session, body.unit_key, "resume_program", reason, {"unit_key": body.unit_key, "reason": reason})


@router.post("/control/presence", response_model=SettingsOut)
def presence(body: PresenceBody, _: Role = OwnerDep, session: Session = SessionDep) -> SettingsOut:
    occ = get_setting(session, "occupancy", OccupancySettings)
    occ.phones_away = body.phones_away
    occ.phones_updated_at = utcnow()
    put_setting(session, "occupancy", occ, updated_by="owner")
    events.publish(session, "status")
    out = settings_out(session)
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
