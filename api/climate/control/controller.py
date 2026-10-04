"""The controller: every tick, plan -> guardrails -> suggest or act -> read back -> log.

Modes (app_settings control.mode): 'off' does nothing; 'suggest' logs control_actions with
status 'suggested' and never writes; 'act' writes timed holds (holdHours, 1-2 h, renewed
while healthy) through the active source and records the read-back. Every write is logged
to control_actions with channel, reason, before/after and read-back (CLAUDE.md).
Claude is never in this path.

Besides holds, in 'act' mode the controller keeps the Home comfort setting's sensor set on the
current block's set (blueprint rule 4: temperature holds use Home's sensors) and, once a day
from the worker, keeps ecobee's Smart Away and Follow Me off (``keep_settings``). Before its
first such change it records each unit's original ecobee settings, and ``hand_back`` (from
``climate.control.handback``) restores them.

A hold a person set always wins: while one runs (``UnitStatus.person_hold``) the controller
writes nothing to that unit, however long it lasts; after a Resume it follows the ecobee
schedule for ``resume_backoff_hours``; during a utility, vacation or unrecognised ecobee
event it stands aside. Each person's hold and each Resume is logged once ('skipped' rows).

A write is logged as status 'sent' (committed) BEFORE the source is called, then updated to
'verified' or 'failed' from the read-back, so a crash mid-write still leaves a record and
still counts toward the one-change-per-30-min limit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from climate.api.schemas import PlanRow
from climate.control.guardrails import PROTECTED_EVENTS, STALE_AFTER, GuardResult, check, person_hold_sentence
from climate.control.handback import capture_original, hand_back
from climate.control.policy import (
    HouseState,
    PersonHold,
    UnitStatus,
    UnitTarget,
    UtilityEventState,
    _in_sleep_window,
    plan,
)
from climate.events import publish
from climate.house import ROOMS, SENSOR_BY_KEY, UNIT_KEYS
from climate.notify import raise_alert, resolve_alert
from climate.occupancy.room_state import persist_room_states
from climate.sources.base import (
    KNOWN_HOLD_TYPES,
    PERSON_EVENTS,
    PLAIN_HOLD_TYPES,
    HoldInfo,
    HoldRequest,
    ThermostatSource,
    UnitSnapshot,
    WriteResult,
)
from climate.state import (
    AUTO_EVENTS,
    HOLD_ACTIONS,
    MANUAL_KIND,
    hold_signature,
    latest_write,
    load_house_state,
    pending_resume,
    room_results,
)
from climate.store.app_settings import ControlSettings, SourceSettings, UnitComfort, get_setting
from climate.store.db import session_scope
from climate.store.orm import Alert, ControlAction, LiveUnit
from climate.timeutil import to_local, utcnow

log = logging.getLogger(__name__)

# The public entry points. hand_back lives in climate.control.handback and is re-exported
# here: the worker's hand-back job calls controller.hand_back(source, now).
__all__ = [
    "SettingsCheck", "capture_original", "closest_climate", "current_plan", "execute_queued", "hand_back",
    "home_sensor_set", "keep_settings", "tick",
]

RENEW_WITHIN = timedelta(minutes=20)  # renew our own hold when it ends this soon
SUGGEST_REPEAT = timedelta(minutes=30)  # don't repeat an identical suggestion sooner
MATCH_F = 0.25  # setpoints within this are already where we want them
PENDING_MAX_AGE = timedelta(minutes=15)  # an older queued/sent HomeKit row no longer blocks the unit
OWNER_QUEUE_MAX_AGE = timedelta(minutes=10)  # an owner action not run by then fails instead
PROGRAM_WRITE_EVERY = timedelta(minutes=30)  # at most one program (sensor set) write per unit
WriteKind = Literal["none", "set_hold", "renew", "resume"]
# Home comfort setting's sensors at night (blueprint rule 4; the main floor steers on the Hallway).
NIGHT_SENSOR_SETS: dict[str, list[str]] = {
    "main": ["main.hallway_tstat"],
    "up": ["up.girls_room"],
    "bed": ["bed.bedroom_tstat"],
}
# Joins the upstairs night set only while the Girls' Room SmartSensor has no reading.
UP_NIGHT_FALLBACK = "up.toy_room_tstat"
# A source's WriteResult saying it refused to cancel a hold the controller did not write.
_REFUSAL_PHRASES = ("refus", "not ours", "not written by", "not set by", "was not written", "did not write",
                    "not one the controller")


@dataclass
class _Eval:
    kind: WriteKind
    guard: GuardResult
    note: str
    hours: int = 2  # holdHours for a set_hold / renew (an event_prep hold ends by its hold_end_by)


@dataclass
class _Write:
    action_id: int
    unit_key: str
    kind: Literal["set_hold", "resume_program", "update_program"]
    channel: str
    hold: HoldRequest | None
    reason: str
    force: bool = False  # resume_program only: an owner's explicit resume
    sets: dict[str, list[str]] | None = None  # update_program only
    refused_reason: str | None = None  # resume_program: logged when the source refuses


@dataclass
class SettingsCheck:
    """What ``keep_settings`` did: the mode it saw, the rows it logged, and whether every
    unit is (or was suggested to be) as wanted. ``ok`` False means retry later."""

    mode: str
    ids: list[int]
    ok: bool


# ---------------------------------------------------------------------------------------
# public entry points
# ---------------------------------------------------------------------------------------


def current_plan(session: Session, now: datetime | None = None) -> list[PlanRow]:
    """Plan + guard for every unit without writing anything (GET /api/control/plan)."""
    now = now or utcnow()
    state = load_house_state(session, now)
    mode = state.control.mode
    homekit = _homekit_fallback(_source_settings(session), None, now)
    rows: list[PlanRow] = []
    for target in plan(state):
        unit = _unit(state, target.unit_key)
        ev = _evaluate(state, unit, target, now, mode, _hold_end(session, unit, state.control), homekit)
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
    """One controller pass. Persists room states, captures each unit's original ecobee
    settings (first sight, ``capture_original``), plans, guards, then per mode logs or
    writes. Skips a unit whose current hold already matches the target (renews a hold of
    ours that ends within 20 minutes). Logs each person's hold and each Resume once
    (``_log_hold_changes``), and keeps the optional "still on your hold" reminders. Returns
    the control_action ids created. Never raises on a single unit's failure; logs it and
    continues.

    In 'act' mode a unit outside ``act_units`` (or any unit when no source is available)
    gets suggestions instead of writes. While the ecobee cloud circuit is open and HomeKit
    is enabled, writes become QUEUED 'homekit' climate holds for the homekit service. After
    a failed write (either channel) the unit is not retried for min_minutes_between_changes.
    In 'act' mode the Home comfort setting's sensor set is also kept on the current block's
    set when the source can write it (``update_sensor_sets``; the simulator cannot)."""
    now = now or utcnow()
    created: list[int] = []
    writes: list[_Write] = []
    source_kind = getattr(source, "kind", None) if source is not None else None
    can_write_sets = callable(getattr(source, "update_sensor_sets", None)) if source is not None else False

    with session_scope() as s:
        state = load_house_state(s, now)
        persist_room_states(s, now, room_results(state))
        publish(s, "status")
        try:
            with s.begin_nested():  # before any write, so hand-back knows what to restore
                capture_original(s, [u.snapshot for u in state.units.values()], now)
        except Exception:
            log.exception("could not capture the original ecobee settings")
        mode = state.control.mode
        if mode == "off":
            _hold_reminders(s, state, now, remind=False)
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
        _hold_reminders(s, state, now, remind=True)
        if mode == "act" and can_write_sets and source_kind is not None and not homekit:
            for unit_key in UNIT_KEYS:
                try:
                    with s.begin_nested():
                        write = _sensor_set_write(s, state, unit_key, now, str(source_kind))
                    if write is not None:
                        created.append(write.action_id)
                        writes.append(write)
                except Exception:
                    log.exception("sensor set check failed for unit %s", unit_key)

    for w in writes:
        await _perform(source, w)
    return created


async def execute_queued(source: ThermostatSource | None, now: datetime | None = None) -> list[int]:
    """Execute owner-queued actions (POST /api/control/hold|resume|automatic insert
    status='queued', channel matching the source). HomeKit-channel rows are left for the
    homekit service.

    An owner hold (bug 4) gets exactly what was typed within the hard envelope only: the
    hard min/max, the deadband (and heatCoolMinDelta) and the 0.5°F grid. No step limit, no
    humidity guard, no rate limit, no back-off and no person-hold block (the owner is the
    person). Stale data and running events still block it (vacation, utility, unrecognised;
    "skip the event first"). Owner actions run in 'suggest' and 'act' mode, but fail with
    "the controller is off" in 'off' mode. A row still queued 10 minutes after it was made
    fails as expired instead of reaching a thermostat late. An owner resume ("Back to
    automatic", request kind 'automatic', or "Resume schedule", kind 'resume_schedule')
    cancels any plain hold (``force``); what follows (steer again at once, or the resume
    back-off) is read from the kind by ``climate.state``. Returns the ids handled."""
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
                    if row.ts is not None and now - row.ts > OWNER_QUEUE_MAX_AGE:
                        write = _fail(row, now, f"Not sent: expired before the worker could run it (queued "
                                                f"{int((now - row.ts).total_seconds() // 60)} min ago).")
                    elif state.control.mode == "off":
                        write = _fail(row, now, "Not sent: the controller is off.")
                    else:
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
    ours_until: datetime | None, homekit: bool = False,
) -> _Eval:
    """What this unit needs right now: nothing, a new hold, a renewal or a resume.

    While a person's hold, the back-off after a Resume, or an event (vacation, utility,
    unrecognised) runs, nothing, and the note says why it waits (never "the schedule already
    matches", bug 5). ecobee's own Smart Away / Smart Home event is not the schedule: when the
    plan wants the schedule's setpoints a timed hold takes the unit back instead (the source
    would refuse to resume over it). A target with ``hold_end_by`` (event_prep) gets the
    largest of {hold_hours, 1} hours that ends by then, renewals included; when none fits,
    nothing is written."""
    control = state.control
    events = _unit_events(state, unit.unit_key)
    guard = check(target, unit, control.limits, now, mode=mode, act_units=control.act_units, events=events,
                  homekit_data_ok=homekit, tz=state.tz)
    hours = control.hold_hours
    if target.hold_end_by is not None:
        fit = fit_hours(now, target.hold_end_by, control.hold_hours)
        too_close = fit is None
        hours = fit or hours
    else:
        too_close = False
    if _stands_aside(unit, events, now):
        return _Eval("none", guard, guard.blocked_reason or "Waiting.", hours)
    snap = unit.snapshot
    hold = snap.hold if snap is not None else None
    ours = hold is not None and hold.set_by_us
    auto_event = hold is not None and hold.hold_type in AUTO_EVENTS
    close = _too_close_note(target)

    if target.desired == "program" and not auto_event:
        if not ours:
            return _Eval("none", guard, "The thermostat's own schedule already matches the plan.", hours)
        if not guard.ok:
            return _Eval("none", guard, guard.blocked_reason or "Blocked.", hours)
        if guard.clamped:
            if too_close:
                return _Eval("none", guard, close, hours)
            return _Eval("set_hold", guard, "Stepping toward the schedule's setpoints within the step limit.", hours)
        return _Eval("resume", guard, "The schedule matches the plan again, so our hold is released.", hours)

    cur_heat = snap.heat_sp_f if snap is not None else None
    cur_cool = snap.cool_sp_f if snap is not None else None
    same = (
        cur_heat is not None and cur_cool is not None
        and abs(guard.heat_f - cur_heat) <= MATCH_F and abs(guard.cool_f - cur_cool) <= MATCH_F
    )
    if same:
        # A renewal obeys the rate limit too: it comes long after the write it renews, and the
        # limit stops a second renewal while the snapshot still shows the old end time.
        if ours and ours_until is not None and ours_until - now <= RENEW_WITHIN:
            if not guard.ok:
                return _Eval("none", guard, guard.blocked_reason or "Blocked.", hours)
            if too_close:
                return _Eval("none", guard, close, hours)
            return _Eval("renew", guard, "Our hold ends soon and the target is unchanged, so it is renewed.", hours)
        return _Eval("none", guard, "The thermostat already holds the planned setpoints.", hours)
    if not guard.ok:
        return _Eval("none", guard, guard.blocked_reason or "Blocked.", hours)
    if too_close:
        return _Eval("none", guard, close, hours)
    if auto_event:
        return _Eval("set_hold", guard, f"ecobee's {hold.hold_type} event is running; a timed hold takes it back.",  # type: ignore[union-attr]
                     hours)
    return _Eval("set_hold", guard, "", hours)


def fit_hours(now: datetime, end_by: datetime, hold_hours: int) -> int | None:
    """The largest of {hold_hours, 1} hours such that a hold written now ends by ``end_by``."""
    for hours in sorted({int(hold_hours), 1}, reverse=True):
        if now + timedelta(hours=hours) <= end_by:
            return hours
    return None


def _too_close_note(target: UnitTarget) -> str:
    what = "pre-heating" if target.reason.startswith("Pre-heating") else "pre-cooling"
    return f"Too close to the utility event for another {what} hold; it ends on its own before the event."


def _unit_events(state: HouseState, unit_key: str) -> list[UtilityEventState]:
    return [e for e in state.utility_events if e.unit_key == unit_key]


def _stands_aside(unit: UnitStatus, events: list[UtilityEventState], now: datetime) -> bool:
    """A person's hold, the back-off after a Resume, or an event (vacation, utility,
    unrecognised) is running on the unit: the controller writes nothing to it."""
    if unit.person_hold is not None and (unit.person_hold.until is None or unit.person_hold.until > now):
        return True
    if unit.resume_backoff_until is not None and unit.resume_backoff_until > now:
        return True
    return _event_running(unit, events, now)


def _event_running(unit: UnitStatus, events: list[UtilityEventState], now: datetime) -> bool:
    """A vacation, utility or unrecognised ecobee event runs on the unit, by its snapshot or
    by a utility event's clock window."""
    snap = unit.snapshot
    hold = snap.hold if snap is not None else None
    if hold is not None and hold.hold_type is not None and (
        hold.hold_type in PROTECTED_EVENTS or hold.hold_type not in KNOWN_HOLD_TYPES
    ):
        return True
    return any(e.unit_key == unit.unit_key and e.covers(now) for e in events)


def _tick_unit(
    s: Session, state: HouseState, target: UnitTarget, now: datetime, mode: str,
    source_kind: str | None, homekit: bool,
) -> tuple[list[int], _Write | None]:
    control = state.control
    unit = _unit(state, target.unit_key)
    ids = _log_hold_changes(s, state, unit, now, mode)

    eff_mode = "act" if mode == "act" and target.unit_key in control.act_units else "suggest"
    if eff_mode == "act" and source_kind is None and not homekit:
        eff_mode = "suggest"  # nothing to write through: show what would happen instead
    ev = _evaluate(state, unit, target, now, eff_mode, _hold_end(s, unit, control), homekit)
    if ev.kind == "none":
        return ids, None

    before = _before(unit)
    if eff_mode == "suggest":
        request = _request(ev, target)
        last = s.execute(
            select(ControlAction)
            .where(ControlAction.unit_key == target.unit_key, ControlAction.status == "suggested",
                   ControlAction.action.in_(HOLD_ACTIONS))
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

    # no retry for a while after a failed write, whichever channel it went through
    if _recent_failure(s, target.unit_key, now, control.limits.min_minutes_between_changes):
        return ids, None
    if homekit:
        row_id = _queue_homekit(s, state, unit, target, ev, now, before)
        if row_id is not None:
            ids.append(row_id)
        return ids, None

    request = _request(ev, target)
    action = "resume_program" if ev.kind == "resume" else "set_hold"
    reason = _reason(ev, target)
    row = _insert(s, state, target.unit_key, mode="act", channel=str(source_kind), action=action, status="sent",
                  rule=target.rule, reason=reason, before=before, request=request, now=now)
    ids.append(row.id)
    hold = None
    refused = None
    if action == "set_hold":
        hold = HoldRequest(unit_key=target.unit_key, heat_f=ev.guard.heat_f, cool_f=ev.guard.cool_f,
                           hours=ev.hours, reason=reason[:500])
    else:
        refused = (f"The {unit.name.lower()} thermostat's hold was not written by the controller, so it was left "
                   "alone; the controller waits until it ends.")
    return ids, _Write(row.id, target.unit_key, action, str(source_kind), hold, reason,  # type: ignore[arg-type]
                       refused_reason=refused)


def _prepare_owner_action(state: HouseState, row: ControlAction, now: datetime, channel: str | None) -> _Write | None:
    control = state.control
    unit = _unit(state, row.unit_key)
    req: dict[str, Any] = dict(row.request or {})
    row.before = _before(unit)
    if row.action == "resume_program":
        if unit.snapshot is None:
            return _fail(row, now, "Not sent: there is no live data from this thermostat yet.")
        row.status = "sent"
        # The owner's explicit resume ("Back to automatic" or "Resume schedule": request.kind)
        # may cancel any plain hold, including one someone set by hand.
        return _Write(row.id, row.unit_key, "resume_program", str(channel), None, row.reason, force=True)
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
    # Exactly what was typed within the hard envelope (min/max, deadband, grid); events still block.
    guard = check(target, unit, lim, now, enforce_rate_limit=False, enforce_manual_backoff=False,
                  enforce_step=False, enforce_humidity=False, events=_unit_events(state, row.unit_key), tz=state.tz)
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
        elif w.kind == "update_program":
            result = await source.update_sensor_sets(w.unit_key, dict(w.sets or {}), w.reason[:500])  # type: ignore[attr-defined]
        elif w.force:
            result = await source.resume_program(w.unit_key, w.reason[:500], force=True)
        else:
            # the controller never forces: the source refuses to cancel a hold it did not write
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
            if w.kind == "resume_program" and not w.force and _refused(result):
                _mark_refused(row, result, w)
                publish(s, "action", row.id)
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


def _log_hold_changes(s: Session, state: HouseState, unit: UnitStatus, now: datetime, mode: str) -> list[int]:
    """Log, once each: a person's hold seen on a cloud snapshot (MANUAL_KIND row with its
    signature, ``by`` and ``until``; its id becomes ``person_hold.detection_id``), or a Resume
    someone pressed on a running hold (RESUME_KIND row: our hold vanished before its end, or a
    person's hold before its ``until``). A hold that ended on its own logs nothing (bug 5).
    HomeKit-only snapshots log nothing here (the homekit service logs what it sees)."""
    snap = unit.snapshot
    if snap is None or snap.source == "homekit":
        return []
    row_mode = "act" if mode == "act" else "suggest"
    ph = unit.person_hold
    if ph is not None:
        if ph.detection_id is not None or snap.hold is None:
            return []
        row = _insert(
            s, state, unit.unit_key, mode=row_mode, channel="none", action="set_hold", status="skipped",
            rule="hold_off", reason=_person_hold_reason(unit, ph, state.tz, now), before=_before(unit),
            request=hold_signature(snap.hold, by=ph.by), now=now,
        )
        unit.person_hold = ph.model_copy(update={"detection_id": row.id})
        return [row.id]
    request = pending_resume(s, unit.unit_key, snap, state.control, now)
    if request is None:
        return []
    name = unit.name.lower()
    if unit.resume_backoff_until is not None and unit.resume_backoff_until > now:
        reason = (f"Someone pressed Resume at the {name} thermostat; following the ecobee schedule until "
                  f"{_when(unit.resume_backoff_until, state.tz, now)}.")
    else:
        reason = f"Someone pressed Resume at the {name} thermostat; the controller steers it again now."
    row = _insert(s, state, unit.unit_key, mode=row_mode, channel="none", action="set_hold", status="skipped",
                  rule="hold_off", reason=reason, before=_before(unit), request=request, now=now)
    return [row.id]


def _person_hold_reason(unit: UnitStatus, ph: PersonHold, tz: str, now: datetime) -> str:
    """'Someone set a hold on the upstairs thermostat (70–74°F, until 4:00 PM); ...'"""
    name = unit.name.lower()
    if ph.heat_f is not None and ph.cool_f is not None:
        what = f"{ph.heat_f:g}–{ph.cool_f:g}°F"
    elif ph.climate_ref:
        what = ph.climate_ref.capitalize()
    else:
        what = "a hold"
    until = f"until {_when(ph.until, tz, now)}" if ph.until is not None else "until you change it"
    tail = "the controller waits until it ends or you choose Back to automatic."
    if ph.by == "app":
        return f"Your hold from the app is running on the {name} thermostat ({what}, {until}); {tail}"
    if ph.hold_type == "quickSave":
        return f"Someone pressed Quick Save on the {name} thermostat ({what}, {until}); {tail}"
    if ph.heat_f is None and ph.climate_ref:
        return f"Someone picked {what} on the {name} thermostat ({until}); {tail}"
    return f"Someone set a hold on the {name} thermostat ({what}, {until}); {tail}"


def _hold_reminders(s: Session, state: HouseState, now: datetime, remind: bool) -> None:
    """The optional "still on your hold" reminder (``control.manual_hold_reminder_hours`` >
    0): once per person's hold, after it has run that long, an info alert that is pushed
    (dedupe ``person_hold:{unit}:{detection_id}``); resolved when that hold is gone. It only
    reminds; it changes nothing. Never raises."""
    hours = state.control.manual_hold_reminder_hours
    for unit in state.units.values():
        try:
            with s.begin_nested():
                ph = unit.person_hold
                prefix = f"person_hold:{unit.unit_key}:"
                current = f"{prefix}{ph.detection_id}" if ph is not None and ph.detection_id is not None else None
                open_keys = s.execute(
                    select(Alert.dedupe_key).where(Alert.dedupe_key.like(f"{prefix}%"), Alert.resolved_at.is_(None))
                ).scalars().all()
                for key in open_keys:
                    if key is not None and key != current:
                        resolve_alert(s, key)
                if not remind or hours <= 0 or ph is None or current is None:
                    continue
                if now - ph.since < timedelta(hours=hours):
                    continue
                if s.execute(select(Alert.id).where(Alert.dedupe_key == current).limit(1)).scalar_one_or_none():
                    continue  # once per hold, even if the owner closed the reminder
                back = state.control.resume_backoff_hours
                body = (f"{person_hold_sentence(ph, state.tz, now)} Nothing changes until then. To hand it back now: "
                        f"Back to automatic on Live (the controller steers again at once), or Resume schedule (the "
                        f"ecobee schedule runs for {back:g} h).")
                raise_alert(s, kind="person_hold", level="info", title=f"{unit.name} is still on your hold",
                            body=body, dedupe_key=current, push_info=True)
        except Exception:  # a reminder never breaks the tick
            log.exception("person-hold reminder failed for unit %s", unit.unit_key)


def _queue_homekit(
    s: Session, state: HouseState, unit: UnitStatus, target: UnitTarget, ev: _Eval, now: datetime,
    before: dict[str, Any],
) -> int | None:
    """Cloud circuit open: queue a HomeKit climate hold (or clear_hold) for the homekit service.
    A queued/sent row younger than 15 minutes means one is already on its way; an older one
    (a crashed or stopped homekit service) no longer blocks the unit. An event_prep hold is
    queued only when its ``until`` is no later than the target's ``hold_end_by``."""
    pending = s.execute(
        select(ControlAction.id).where(
            ControlAction.unit_key == target.unit_key, ControlAction.channel == "homekit",
            ControlAction.status.in_(("queued", "sent")), ControlAction.ts > now - PENDING_MAX_AGE,
        ).limit(1)
    ).scalar_one_or_none()
    if pending is not None:
        return None
    until_at = now + timedelta(hours=ev.hours)
    if ev.kind == "resume":
        request: dict[str, Any] = {"kind": "clear_hold", "unit_key": target.unit_key}
        action = "resume_program"
    else:
        if target.hold_end_by is not None and until_at > target.hold_end_by:
            return None  # an event_prep hold must end before the utility event
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
            "kind": "climate_hold", "climate": climate, "until": until_at.isoformat(),
            "unit_key": target.unit_key, "target": {"heat_f": ev.guard.heat_f, "cool_f": ev.guard.cool_f},
        }
        action = "set_hold"
    reason = f"ecobee cloud unavailable, so this goes through HomeKit: {_reason(ev, target)}"
    row = _insert(s, state, target.unit_key, mode="act", channel="homekit", action=action, status="queued",
                  rule=target.rule, reason=reason, before=before, request=request, now=now)
    return row.id


# ---------------------------------------------------------------------------------------
# sensor sets (blueprint rule 4) and ecobee settings
# ---------------------------------------------------------------------------------------


def home_sensor_set(state: HouseState, unit_key: str) -> tuple[str, list[str]]:
    """('night' | 'day', sensor keys) the Home comfort setting should average right now.

    Night = inside a sleep window of one of the unit's sleep rooms (the schedule, so the set
    does not flap with motion): main -> the Hallway thermostat; upstairs -> the Girls' Room
    SmartSensor (plus the Toy Room thermostat while that sensor has no reading); wing -> the
    Bedroom thermostat. Day: every active sensor of the unit."""
    local = to_local(state.now, state.tz)
    night = any(
        _in_sleep_window(r.key, state.occupancy, local) for r in ROOMS if r.unit_key == unit_key and r.is_sleep_room
    )
    if night and unit_key in NIGHT_SENSOR_SETS:
        keys = list(NIGHT_SENSOR_SETS[unit_key])
        if unit_key == "up":
            girls = state.rooms.get("girls_room")
            if girls is None or girls.temp_f is None:
                keys.append(UP_NIGHT_FALLBACK)
        return "night", keys
    keys = [k for r in state.rooms.values() if r.unit_key == unit_key for k in r.sensor_keys]
    return "day", list(dict.fromkeys(keys))


def _sensor_set_write(s: Session, state: HouseState, unit_key: str, now: datetime, channel: str) -> _Write | None:
    """In 'act' mode: when the unit's Home sensor set (as the snapshot reports it) is not the
    current block's set, log a 'sent' update_program row and return the write. At most one
    program write per unit per 30 minutes; never on stale, HomeKit-only or disconnected data,
    or outside ``act_units``. Only while the running hold is none, ours or Smart Away / Home
    (bug 6): never during a person's hold, the back-off after a Resume, a vacation, utility
    (by the snapshot or by the event's clock window) or unrecognised event. The unit's
    original ecobee settings are captured first (hand-back restores them)."""
    control = state.control
    unit = state.units.get(unit_key)
    snap = unit.snapshot if unit is not None else None
    if unit is None or snap is None or unit_key not in control.act_units:
        return None
    if snap.source == "homekit" or not snap.connected:
        return None
    if unit.age_s is None or unit.age_s > STALE_AFTER.total_seconds():
        return None
    hold = snap.hold
    if hold is not None and not hold.set_by_us and hold.hold_type not in AUTO_EVENTS:
        return None  # a person's hold or an ecobee event: hands off
    if _stands_aside(unit, _unit_events(state, unit_key), now):
        return None
    current = snap.sensor_sets.get("home")
    if current is None:
        return None  # the source does not report the set: nothing to compare against
    block, keys = home_sensor_set(state, unit_key)
    if not keys or set(current) == set(keys):
        return None
    recent = s.execute(
        select(ControlAction.id).where(
            ControlAction.unit_key == unit_key, ControlAction.actor == "controller",
            ControlAction.action == "update_program", ControlAction.status.in_(("sent", "verified", "failed")),
            ControlAction.ts > now - PROGRAM_WRITE_EVERY,
        ).limit(1)
    ).scalar_one_or_none()
    if recent is not None:
        return None
    capture_original(s, [snap], now)  # before our first change to the set
    names = ", ".join(SENSOR_BY_KEY[k].name if k in SENSOR_BY_KEY else k for k in keys)
    name = unit.name.lower()
    if block == "night":
        reason = (f"Night block (sleep window): the {name} Home comfort setting now averages {names}, because "
                  "temperature holds use Home's sensors.")
    else:
        reason = f"Day block: the {name} Home comfort setting averages all its sensors again ({names})."
    sets = {"home": keys}
    row = _insert(s, state, unit_key, mode="act", channel=channel, action="update_program", status="sent",
                  rule="sensor_set", reason=reason, before={"sensor_sets": dict(snap.sensor_sets)},
                  request={"kind": "sensor_sets", "unit_key": unit_key, "block": block, "sets": sets}, now=now)
    return _Write(row.id, unit_key, "update_program", channel, None, reason, sets=sets)


def _truthy(v: object) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    return isinstance(v, str) and v.strip().lower() in ("true", "1", "yes", "on")


async def keep_settings(source: ThermostatSource | None, now: datetime | None = None) -> SettingsCheck:
    """Keep ecobee's Smart Away (``autoAway``) and Follow Me (``followMeComfort``) off, so the
    thermostats don't fight the controller (blueprint §7). The worker calls this at start and
    once a day; sources without ``ensure_settings`` (the simulator) are skipped silently.

    Settings writes happen only in 'act' mode, and only when every unit is in ``act_units``
    (``ensure_settings`` writes all thermostats at once, and a unit outside the list stays
    suggest-only). Before writing, each unit's original ecobee settings are captured if they
    are not yet (hand-back restores them). Each write the source made is logged as an
    'update_settings' row (verified or failed, with before/after and read-back); units already
    off are not logged. In 'suggest' mode (or with some units suggest-only) a unit whose live
    snapshot shows either setting on gets a 'suggested' row instead. 'off' does nothing (so
    after a hand-back it never switches Smart Away off again)."""
    now = now or utcnow()
    ensure = getattr(source, "ensure_settings", None) if source is not None else None
    if ensure is None or not callable(ensure):
        return SettingsCheck(mode="n/a", ids=[], ok=True)
    channel = getattr(source, "kind", None)
    channel = channel if channel in ("ecobee", "simulator") else "none"
    with session_scope() as s:
        control = _control_settings(s)
        if control.mode == "off":
            return SettingsCheck(mode="off", ids=[], ok=True)
        if control.mode != "act" or not all(k in control.act_units for k in UNIT_KEYS):
            return SettingsCheck(mode=control.mode, ids=_suggest_settings(s, control, now), ok=True)
        capture_original(s, _live_snapshots(s), now)  # committed before the first write
    results = list(await ensure())
    ids: list[int] = []
    ok = True
    with session_scope() as s:
        for r in results:
            req = dict(r.request) if isinstance(r.request, dict) else {}
            unit_key = req.get("unit_key")
            if unit_key not in UNIT_KEYS:
                ok = ok and r.ok
                continue
            if req.get("noop") and r.ok:
                continue  # already off: nothing was written
            ok = ok and r.ok
            row = ControlAction(
                ts=now, unit_key=unit_key, actor="controller", mode="act", channel=channel, action="update_settings",
                status="verified" if r.ok else "failed", rule="settings",
                reason=("Turned ecobee's Smart Away and Follow Me off so the thermostat does not fight the "
                        "controller (they let an empty floor float warm)."),
                before=dict(r.before) if r.before else None, request=req,
                readback=dict(r.readback) if r.readback is not None else None,
                readback_ok=bool(r.ok) if r.readback is not None else (None if r.ok else False),
                error=None if r.ok else (r.error or "The read-back did not match the request."),
                completed_at=utcnow(),
            )
            s.add(row)
            s.flush()
            publish(s, "action", row.id)
            ids.append(row.id)
    return SettingsCheck(mode="act", ids=ids, ok=ok)


def _live_snapshots(s: Session) -> list[UnitSnapshot]:
    out: list[UnitSnapshot] = []
    for row in s.execute(select(LiveUnit).order_by(LiveUnit.unit_key)).scalars():
        try:
            out.append(UnitSnapshot.model_validate(row.snapshot))
        except ValidationError:
            continue
    return out


def _suggest_settings(s: Session, control: ControlSettings, now: datetime) -> list[int]:
    ids: list[int] = []
    for row in s.execute(select(LiveUnit).order_by(LiveUnit.unit_key)).scalars():
        try:
            snap = UnitSnapshot.model_validate(row.snapshot)
        except ValidationError:
            continue
        on = [k for k in ("autoAway", "followMeComfort") if _truthy(snap.settings.get(k))]
        if not on or row.unit_key not in UNIT_KEYS:
            continue
        what = " and ".join({"autoAway": "Smart Away", "followMeComfort": "Follow Me"}[k] for k in on)
        name = {"main": "main floor", "up": "upstairs", "bed": "bed / office wing"}.get(row.unit_key, row.unit_key)
        act = ControlAction(
            ts=now, unit_key=row.unit_key, actor="controller", mode="suggest", channel="none",
            action="update_settings", status="suggested", rule="settings",
            reason=(f"Turn off ecobee's {what} on the {name} thermostat: it fights the controller (an empty floor "
                    "floats warm). In act mode the controller turns it off itself."),
            before={k: snap.settings.get(k) for k in ("autoAway", "followMeComfort")},
            request={"unit_key": row.unit_key, "settings": {"autoAway": False, "followMeComfort": False}},
        )
        s.add(act)
        s.flush()
        publish(s, "action", act.id)
        ids.append(act.id)
    return ids


def _control_settings(s: Session) -> ControlSettings:
    try:
        return get_setting(s, "control", ControlSettings)
    except ValidationError:
        return ControlSettings()


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


def _request(ev: _Eval, target: UnitTarget) -> dict[str, Any]:
    """The request recorded in control_actions (stable, so identical suggestions dedupe)."""
    if ev.kind == "resume":
        return {"kind": "resume_program", "unit_key": target.unit_key}
    request: dict[str, Any] = {"unit_key": target.unit_key, "heat_f": ev.guard.heat_f, "cool_f": ev.guard.cool_f,
                               "hours": ev.hours}
    if target.hold_end_by is not None:
        request["hold_end_by"] = target.hold_end_by.isoformat()
    return request


def _reason(ev: _Eval, target: UnitTarget) -> str:
    extra = []
    if ev.kind == "renew":
        extra.append("Renewing our hold, which ends soon.")
    elif ev.kind == "resume":
        extra.append("Resuming the thermostat's schedule, which already matches.")
    elif ev.kind == "set_hold" and ev.note:
        extra.append(ev.note)
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
    """A hold write or resume of ours failed for this unit within ``minutes`` (any channel,
    HomeKit included)."""
    return s.execute(
        select(ControlAction.id).where(
            ControlAction.unit_key == unit_key, ControlAction.actor == "controller",
            ControlAction.status == "failed", ControlAction.action.in_(HOLD_ACTIONS),
            ControlAction.ts > now - timedelta(minutes=minutes),
        ).limit(1)
    ).scalar_one_or_none() is not None


def _refused(result: WriteResult) -> bool:
    """The source refused to resume because the running hold is not one the controller wrote."""
    if result.ok:
        return False
    req = result.request if isinstance(result.request, dict) else {}
    if req.get("refused") or req.get("not_ours"):
        return True
    err = (result.error or "").lower()
    if any(p in err for p in _REFUSAL_PHRASES):
        return True
    hold = result.before.get("hold") if isinstance(result.before, dict) else None
    return (
        isinstance(hold, dict) and hold.get("set_by_us") is False and result.readback is None
        and hold.get("hold_type") in (*PLAIN_HOLD_TYPES, *PERSON_EVENTS)  # never an event, known or not
    )


def _parse_hold(raw: object) -> HoldInfo | None:
    if not isinstance(raw, dict):
        return None
    try:
        return HoldInfo.model_validate(raw)
    except ValidationError:
        return None


def _mark_refused(row: ControlAction, result: WriteResult, w: _Write) -> None:
    """The source would not cancel the running hold (not ours): nothing was written. Log it
    as 'skipped' with the hold's signature: ``climate.state`` then treats that hold as a
    person's hold (the controller waits until it ends)."""
    hold = _parse_hold((result.before or {}).get("hold")) or _parse_hold((row.before or {}).get("hold"))
    request: dict[str, Any] = hold_signature(hold) if hold is not None else {"kind": MANUAL_KIND}
    request.update(refused=True, attempted=row.request)
    row.status = "skipped"
    row.rule = "hold_off"
    row.request = request
    row.reason = w.refused_reason or "The thermostat's hold was not written by the controller, so it was left alone."
    row.readback = dict(result.readback) if result.readback is not None else None
    row.readback_ok = None
    if result.before:
        row.before = {**(row.before or {}), "source": dict(result.before)}
    row.error = result.error
    row.completed_at = utcnow()


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


def _when(ts: datetime, tz: str, now: datetime) -> str:
    """'4:00 PM' today, 'Tue 4:00 PM' on another day (house time)."""
    text = _clock(ts, tz)
    return text if to_local(ts, tz).date() == to_local(now, tz).date() else f"{to_local(ts, tz):%a} {text}"
