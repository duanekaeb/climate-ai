"""The controller: every tick, plan -> guardrails -> suggest or act -> read back -> log.

Modes (app_settings control.mode): 'off' does nothing; 'suggest' logs control_actions with
status 'suggested' and never writes; 'act' writes timed holds (holdHours, 1-2 h, renewed
while healthy) through the active source and records the read-back. Every write is logged
to control_actions with channel, reason, before/after and read-back (CLAUDE.md).
Claude is never in this path.

A write is logged as status 'sent' (committed) BEFORE the source is called, then updated to
'verified' or 'failed' from the read-back, so a crash mid-write still leaves a record and
still counts toward the one-change-per-30-min limit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from climate.api.schemas import PlanRow
from climate.control.guardrails import GuardResult, check
from climate.control.policy import HouseState, UnitStatus, UnitTarget, plan
from climate.events import publish
from climate.occupancy.room_state import persist_room_states
from climate.sources.base import HoldRequest, ThermostatSource, WriteResult
from climate.state import (
    hold_matches,
    hold_signature,
    latest_write,
    load_house_state,
    manual_detection,
    room_results,
)
from climate.store.app_settings import ControlSettings, SourceSettings, UnitComfort, get_setting
from climate.store.db import session_scope
from climate.store.orm import ControlAction
from climate.timeutil import to_local, utcnow

log = logging.getLogger(__name__)

RENEW_WITHIN = timedelta(minutes=20)  # renew our own hold when it ends this soon
SUGGEST_REPEAT = timedelta(minutes=30)  # don't repeat an identical suggestion sooner
MATCH_F = 0.25  # setpoints within this are already where we want them
WriteKind = Literal["none", "set_hold", "renew", "resume"]


@dataclass
class _Eval:
    kind: WriteKind
    guard: GuardResult
    note: str


@dataclass
class _Write:
    action_id: int
    unit_key: str
    kind: Literal["set_hold", "resume_program"]
    channel: str
    hold: HoldRequest | None
    reason: str


# ---------------------------------------------------------------------------------------
# public entry points
# ---------------------------------------------------------------------------------------


def current_plan(session: Session, now: datetime | None = None) -> list[PlanRow]:
    """Plan + guard for every unit without writing anything (GET /api/control/plan)."""
    now = now or utcnow()
    state = load_house_state(session, now)
    mode = state.control.mode
    rows: list[PlanRow] = []
    for target in plan(state):
        unit = _unit(state, target.unit_key)
        ev = _evaluate(state, unit, target, now, mode, _hold_end(session, unit, state.control))
        snap = unit.snapshot
        rows.append(
            PlanRow(
                target=target,
                guard=ev.guard,
                current_heat_f=snap.heat_sp_f if snap else None,
                current_cool_f=snap.cool_sp_f if snap else None,
                would_write=mode == "act" and target.unit_key in state.control.act_units and ev.kind != "none",
            )
        )
    return rows


async def tick(source: ThermostatSource | None, now: datetime | None = None) -> list[int]:
    """One controller pass. Persists room states, plans, guards, then per mode logs or
    writes. Skips a unit whose current hold already matches the target (renews a hold of
    ours that ends within 20 minutes). Detects manual changes (a hold not set_by_us) and
    starts the manual back-off. Returns the control_action ids created. Never raises on a
    single unit's failure; logs it and continues.

    In 'act' mode a unit outside ``act_units`` (or any unit when no source is available)
    gets suggestions instead of writes. While the ecobee cloud circuit is open and HomeKit
    is enabled, writes become QUEUED 'homekit' climate holds for the homekit service. After
    a failed write the unit is not retried for min_minutes_between_changes."""
    now = now or utcnow()
    created: list[int] = []
    writes: list[_Write] = []
    source_kind = getattr(source, "kind", None) if source is not None else None

    with session_scope() as s:
        state = load_house_state(s, now)
        persist_room_states(s, now, room_results(state))
        publish(s, "status")
        mode = state.control.mode
        if mode == "off":
            return []
        try:
            targets = plan(state)
        except Exception:  # room states are still saved; nothing is written this tick
            log.exception("policy.plan failed; no control actions this tick")
            return []
        src = _source_settings(s)
        homekit = _homekit_fallback(src, source_kind, now)
        for target in targets:
            try:
                with s.begin_nested():
                    ids, write = _tick_unit(s, state, target, now, mode, source_kind, homekit)
                created += ids
                if write is not None:
                    writes.append(write)
            except Exception:  # one unit's failure never stops the others
                log.exception("controller tick failed for unit %s", target.unit_key)

    for w in writes:
        await _perform(source, w)
    return created


async def execute_queued(source: ThermostatSource | None, now: datetime | None = None) -> list[int]:
    """Execute owner-queued actions (POST /api/control/hold|resume insert status='queued',
    channel matching the source). HomeKit-channel rows are left for the homekit service.

    Owner holds obey the hard limits (clamped; the written values replace the request's)
    and stale-data checks, but skip the rate limit and the manual back-off: the owner is
    the manual actor. They run whatever the controller mode is. Returns the ids handled."""
    if source is None:
        return []
    now = now or utcnow()
    channel = getattr(source, "kind", None)
    handled: list[int] = []
    writes: list[_Write] = []
    with session_scope() as s:
        rows = list(
            s.execute(
                select(ControlAction)
                .where(ControlAction.status == "queued", ControlAction.actor == "owner",
                       ControlAction.channel == channel)
                .order_by(ControlAction.ts, ControlAction.id)
                .with_for_update(skip_locked=True)
            ).scalars()
        )
        if not rows:
            return []
        state = load_house_state(s, now)
        for row in rows:
            try:
                with s.begin_nested():
                    write = _prepare_owner_action(state, row, now, channel)
                handled.append(row.id)
                publish(s, "action", row.id)
                if write is not None:
                    writes.append(write)
            except Exception as exc:  # fail the row visibly instead of retrying it forever
                log.exception("could not prepare queued action %s", row.id)
                row.status = "failed"
                row.error = f"Not sent: {type(exc).__name__}: {exc}"[:1000]
                row.completed_at = now
                handled.append(row.id)
                publish(s, "action", row.id)
    for w in writes:
        await _perform(source, w)
    return handled


# ---------------------------------------------------------------------------------------
# decisions
# ---------------------------------------------------------------------------------------


def _evaluate(
    state: HouseState, unit: UnitStatus, target: UnitTarget, now: datetime, mode: str,
    ours_until: datetime | None,
) -> _Eval:
    """What this unit needs right now: nothing, a new hold, a renewal or a resume."""
    limits = state.control.limits
    act_units = state.control.act_units
    guard = check(target, unit, limits, now, mode=mode, act_units=act_units, tz=state.tz)
    snap = unit.snapshot
    hold = snap.hold if snap is not None else None
    ours = hold is not None and hold.set_by_us

    if target.desired == "program":
        if not ours:
            return _Eval("none", guard, "The thermostat's own schedule already matches the plan.")
        if not guard.ok:
            return _Eval("none", guard, guard.blocked_reason or "Blocked.")
        if guard.clamped:
            return _Eval("set_hold", guard, "Stepping toward the schedule's setpoints within the step limit.")
        return _Eval("resume", guard, "The schedule matches the plan again, so our hold is released.")

    cur_heat = snap.heat_sp_f if snap is not None else None
    cur_cool = snap.cool_sp_f if snap is not None else None
    same = (
        cur_heat is not None and cur_cool is not None
        and abs(guard.heat_f - cur_heat) <= MATCH_F and abs(guard.cool_f - cur_cool) <= MATCH_F
    )
    if same:
        if ours and ours_until is not None and ours_until - now <= RENEW_WITHIN:
            renew = check(target, unit, limits, now, mode=mode, act_units=act_units,
                          enforce_rate_limit=False, tz=state.tz)
            if renew.ok:
                return _Eval("renew", renew, "Our hold ends soon and the target is unchanged, so it is renewed.")
            return _Eval("none", renew, renew.blocked_reason or "Blocked.")
        return _Eval("none", guard, "The thermostat already holds the planned setpoints.")
    if not guard.ok:
        return _Eval("none", guard, guard.blocked_reason or "Blocked.")
    return _Eval("set_hold", guard, "")


def _tick_unit(
    s: Session, state: HouseState, target: UnitTarget, now: datetime, mode: str,
    source_kind: str | None, homekit: bool,
) -> tuple[list[int], _Write | None]:
    control = state.control
    unit = _unit(state, target.unit_key)
    ids: list[int] = []

    detected = _log_manual_change(s, state, unit, now, mode)
    if detected is not None:
        ids.append(detected)

    eff_mode = "act" if mode == "act" and target.unit_key in control.act_units else "suggest"
    if eff_mode == "act" and source_kind is None and not homekit:
        eff_mode = "suggest"  # nothing to write through: show what would happen instead
    ev = _evaluate(state, unit, target, now, eff_mode, _hold_end(s, unit, control))
    if ev.kind == "none":
        return ids, None

    before = _before(unit)
    if eff_mode == "suggest":
        request = _request(ev, target, control)
        last = s.execute(
            select(ControlAction)
            .where(ControlAction.unit_key == target.unit_key, ControlAction.status == "suggested")
            .order_by(ControlAction.ts.desc(), ControlAction.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if last is not None and last.request == request and now - last.ts < SUGGEST_REPEAT:
            return ids, None
        row = _insert(s, state, target.unit_key, mode="suggest", channel="none",
                      action="resume_program" if ev.kind == "resume" else "set_hold", status="suggested",
                      rule=target.rule, reason=_reason(ev, target), before=before, request=request, now=now)
        ids.append(row.id)
        return ids, None

    if homekit:
        row_id = _queue_homekit(s, state, unit, target, ev, now, before)
        if row_id is not None:
            ids.append(row_id)
        return ids, None

    if _recent_failure(s, target.unit_key, now, control.limits.min_minutes_between_changes):
        return ids, None
    request = _request(ev, target, control)
    action = "resume_program" if ev.kind == "resume" else "set_hold"
    reason = _reason(ev, target)
    row = _insert(s, state, target.unit_key, mode="act", channel=str(source_kind), action=action, status="sent",
                  rule=target.rule, reason=reason, before=before, request=request, now=now)
    ids.append(row.id)
    hold = None
    if action == "set_hold":
        hold = HoldRequest(unit_key=target.unit_key, heat_f=ev.guard.heat_f, cool_f=ev.guard.cool_f,
                           hours=control.hold_hours, reason=reason[:500])
    return ids, _Write(row.id, target.unit_key, action, str(source_kind), hold, reason)  # type: ignore[arg-type]


def _prepare_owner_action(state: HouseState, row: ControlAction, now: datetime, channel: str | None) -> _Write | None:
    control = state.control
    unit = _unit(state, row.unit_key)
    req: dict[str, Any] = dict(row.request or {})
    row.before = _before(unit)
    if row.action == "resume_program":
        if unit.snapshot is None:
            return _fail(row, now, "Not sent: there is no live data from this thermostat yet.")
        row.status = "sent"
        return _Write(row.id, row.unit_key, "resume_program", str(channel), None, row.reason)
    if row.action != "set_hold":
        return _fail(row, now, f"The worker does not execute {row.action!r} actions.")

    heat, cool = req.get("heat_f"), req.get("cool_f")
    if not _number(heat) or not _number(cool):
        return _fail(row, now, "Not sent: the hold request has no valid heat_f / cool_f.")
    lim = control.limits
    hours = req.get("hours", control.hold_hours)
    hours = int(hours) if _number(hours) else control.hold_hours
    hours = max(lim.min_hold_hours, min(lim.max_hold_hours, hours, 2), 1)
    # The rule is irrelevant to the guard; 'comfort' only satisfies the UnitTarget type.
    target = UnitTarget(unit_key=row.unit_key, heat_f=float(heat), cool_f=float(cool), rule="comfort",
                        reason=row.reason)
    guard = check(target, unit, lim, now, enforce_rate_limit=False, enforce_manual_backoff=False, tz=state.tz)
    if not guard.ok:
        return _fail(row, now, f"Not sent: {guard.blocked_reason}")
    written = {**req, "heat_f": guard.heat_f, "cool_f": guard.cool_f, "hours": hours}
    if guard.clamped or guard.violations:
        written["requested"] = {"heat_f": float(heat), "cool_f": float(cool)}
        written["guard"] = guard.violations
    row.request = written
    row.status = "sent"
    hold = HoldRequest(unit_key=row.unit_key, heat_f=guard.heat_f, cool_f=guard.cool_f, hours=hours,
                       reason=row.reason[:500])
    return _Write(row.id, row.unit_key, "set_hold", str(channel), hold, row.reason)


async def _perform(source: ThermostatSource | None, w: _Write) -> None:
    """Call the source, then record verified/failed with the read-back. Never raises."""
    try:
        if source is None:
            raise RuntimeError("no thermostat source is running")
        if w.kind == "set_hold" and w.hold is not None:
            result = await source.set_hold(w.hold)
        else:
            result = await source.resume_program(w.unit_key, w.reason[:500])
    except Exception as exc:  # noqa: BLE001 - the error is the record
        log.warning("write to %s failed: %s", w.unit_key, exc)
        result = WriteResult(ok=False, channel=w.channel if w.channel in ("ecobee", "simulator") else "none",  # type: ignore[arg-type]
                             error=f"{type(exc).__name__}: {exc}"[:1000])
    try:
        with session_scope() as s:
            row = s.get(ControlAction, w.action_id)
            if row is None:
                return
            row.status = "verified" if result.ok else "failed"
            row.readback = dict(result.readback) if result.readback is not None else None
            row.readback_ok = bool(result.ok) if result.readback is not None else (None if result.ok else False)
            if result.before:
                row.before = {**(row.before or {}), "source": dict(result.before)}
            row.error = None if result.ok else (result.error or "The read-back did not match the request.")
            row.completed_at = utcnow()
            publish(s, "action", row.id)
    except Exception:
        log.exception("could not record the result of control action %s", w.action_id)


# ---------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------


def _unit(state: HouseState, unit_key: str) -> UnitStatus:
    return state.units.get(unit_key) or UnitStatus(unit_key=unit_key, name=unit_key)


def _hold_end(session: Session, unit: UnitStatus, control: ControlSettings) -> datetime | None:
    """When our current hold ends: the snapshot's hold end, else our write time + hours."""
    snap = unit.snapshot
    hold = snap.hold if snap is not None else None
    if hold is None or not hold.set_by_us:
        return None
    if hold.end is not None:
        return hold.end
    write = latest_write(session, unit.unit_key)
    if write is None or write.action != "set_hold":
        return None
    hours = (write.request or {}).get("hours", control.hold_hours)
    hours = hours if _number(hours) else control.hold_hours
    return (write.completed_at or write.ts) + timedelta(hours=float(hours))


def _log_manual_change(s: Session, state: HouseState, unit: UnitStatus, now: datetime, mode: str) -> int | None:
    """Log (once per hold) a hold someone made by hand, which starts the manual back-off."""
    snap = unit.snapshot
    hold = snap.hold if snap is not None else None
    if hold is None or hold.set_by_us or unit.manual_override_until is None:
        return None
    write = latest_write(s, unit.unit_key)
    if write is not None and write.action == "set_hold" and hold_matches(hold, write.request):
        return None  # the owner's own hold from the app: already logged as the owner's action
    if manual_detection(s, unit.unit_key, hold, write.ts if write is not None else None) is not None:
        return None
    what = (
        f"{hold.heat_f:g}–{hold.cool_f:g}°F" if hold.heat_f is not None and hold.cool_f is not None
        else (hold.climate_ref or "a hold")
    )
    until = _clock(unit.manual_override_until, state.tz)
    row = _insert(
        s, state, unit.unit_key, mode="act" if mode == "act" else "suggest", channel="none", action="set_hold",
        status="skipped", rule="hold_off",
        reason=f"Someone changed the {unit.name.lower()} thermostat by hand ({what}); the controller backs off until {until}.",
        before=_before(unit), request=hold_signature(hold), now=now,
    )
    return row.id


def _queue_homekit(
    s: Session, state: HouseState, unit: UnitStatus, target: UnitTarget, ev: _Eval, now: datetime,
    before: dict[str, Any],
) -> int | None:
    """Cloud circuit open: queue a HomeKit climate hold (or resume) for the homekit service."""
    pending = s.execute(
        select(ControlAction.id).where(
            ControlAction.unit_key == target.unit_key, ControlAction.channel == "homekit",
            ControlAction.status.in_(("queued", "sent")),
        ).limit(1)
    ).scalar_one_or_none()
    if pending is not None:
        return None
    hours = state.control.hold_hours
    if ev.kind == "resume":
        request: dict[str, Any] = {"kind": "resume_program", "unit_key": target.unit_key}
        action = "resume_program"
    else:
        climate = closest_climate(state.control.comfort.get(target.unit_key), ev.guard.heat_f, ev.guard.cool_f)
        last = s.execute(
            select(ControlAction).where(
                ControlAction.unit_key == target.unit_key, ControlAction.channel == "homekit",
                ControlAction.status == "verified", ControlAction.action == "set_hold",
            ).order_by(ControlAction.ts.desc(), ControlAction.id.desc()).limit(1)
        ).scalar_one_or_none()
        if last is not None and (last.request or {}).get("climate") == climate:
            until = _parse_dt((last.request or {}).get("until"))
            if until is not None and until - now > RENEW_WITHIN:
                return None
        request = {
            "kind": "climate_hold", "climate": climate, "until": (now + timedelta(hours=hours)).isoformat(),
            "unit_key": target.unit_key, "target": {"heat_f": ev.guard.heat_f, "cool_f": ev.guard.cool_f},
        }
        action = "set_hold"
    reason = f"ecobee cloud unavailable, so this goes through HomeKit: {_reason(ev, target)}"
    row = _insert(s, state, target.unit_key, mode="act", channel="homekit", action=action, status="queued",
                  rule=target.rule, reason=reason, before=before, request=request, now=now)
    return row.id


def closest_climate(comfort: UnitComfort | None, heat_f: float, cool_f: float) -> str:
    """The ecobee comfort setting ('home', 'sleep', 'away') nearest to the target band."""
    if comfort is None:
        return "home"
    options = {"home": comfort.day, "sleep": comfort.night, "away": comfort.away}
    return min(options, key=lambda k: abs(options[k].heat_f - heat_f) + abs(options[k].cool_f - cool_f))


def _insert(
    s: Session, state: HouseState, unit_key: str, *, mode: str, channel: str, action: str, status: str,
    rule: str | None, reason: str, before: dict[str, Any] | None, request: dict[str, Any] | None, now: datetime,
) -> ControlAction:
    row = ControlAction(
        ts=now, unit_key=unit_key, actor="controller", mode=mode, channel=channel, action=action, status=status,
        rule=rule, reason=reason, before=before, request=request, policy_version_id=state.policy_version_id,
        completed_at=now if status in ("skipped",) else None,
    )
    s.add(row)
    s.flush()
    publish(s, "action", row.id)
    return row


def _request(ev: _Eval, target: UnitTarget, control: ControlSettings) -> dict[str, Any]:
    """The request recorded in control_actions (stable, so identical suggestions dedupe)."""
    if ev.kind == "resume":
        return {"kind": "resume_program", "unit_key": target.unit_key}
    return {"unit_key": target.unit_key, "heat_f": ev.guard.heat_f, "cool_f": ev.guard.cool_f,
            "hours": control.hold_hours}


def _reason(ev: _Eval, target: UnitTarget) -> str:
    extra = []
    if ev.kind == "renew":
        extra.append("Renewing our hold, which ends soon.")
    elif ev.kind == "resume":
        extra.append("Resuming the thermostat's schedule, which already matches.")
    if ev.guard.violations:
        extra.append(" ".join(ev.guard.violations))
    return " ".join([target.reason, *extra])


def _before(unit: UnitStatus) -> dict[str, Any]:
    snap = unit.snapshot
    if snap is None:
        return {}
    return {
        "heat_f": snap.heat_sp_f, "cool_f": snap.cool_sp_f, "hvac_mode": snap.hvac_mode,
        "climate_ref": snap.climate_ref, "zone_temp_f": snap.zone_temp_f, "zone_humidity": snap.zone_humidity,
        "hold": snap.hold.model_dump(mode="json") if snap.hold is not None else None,
    }


def _recent_failure(s: Session, unit_key: str, now: datetime, minutes: int) -> bool:
    return s.execute(
        select(ControlAction.id).where(
            ControlAction.unit_key == unit_key, ControlAction.actor == "controller",
            ControlAction.status == "failed", ControlAction.ts > now - timedelta(minutes=minutes),
        ).limit(1)
    ).scalar_one_or_none() is not None


def _fail(row: ControlAction, now: datetime, error: str) -> None:
    row.status = "failed"
    row.error = error
    row.completed_at = now


def _source_settings(s: Session) -> SourceSettings:
    try:
        return get_setting(s, "source", SourceSettings)
    except Exception:  # noqa: BLE001
        return SourceSettings()


def _homekit_fallback(src: SourceSettings, source_kind: str | None, now: datetime) -> bool:
    """True while the ecobee cloud circuit is open and HomeKit can take over. Never in
    simulator mode: real thermostats must not be touched while the app simulates."""
    if not src.homekit_enabled or src.cloud_circuit_open_until is None or src.cloud_circuit_open_until <= now:
        return False
    return source_kind == "ecobee" or (source_kind is None and src.kind == "ecobee")


def _number(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _parse_dt(v: object) -> datetime | None:
    if isinstance(v, datetime):
        return v
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v)  # 3.11+ accepts a trailing Z
        except ValueError:
            return None
    return None


def _clock(ts: datetime, tz: str) -> str:
    local = to_local(ts, tz)
    return f"{local.hour % 12 or 12}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"
