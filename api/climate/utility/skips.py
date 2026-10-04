"""Skipping a utility (demand-response) event: the owner's "Skip this event", the owner's skip
rules, and the opt-out itself (docs/specs/holds-and-utility-events.md, "Skips").

A skip is an honest opt-out: ``source.opt_out_event`` (ecobee's ``resumeProgram`` with the
event on top), which ecobee records and reports to the utility. Nothing here counteracts an
event any other way. ``utility_events.skip`` goes requested -> done | failed | refused.

- The owner requests one from the app (``POST /api/utility-events/{id}/skip``, ``skip_by``
  'owner').
- ``apply_rules`` requests one when a skip rule (``UtilityEventSettings.auto_skip``) matches
  the house state for an event running or starting within 5 minutes: in 'act' mode for a unit
  in ``act_units`` (``skip_by`` 'rule', ``skip_reason`` the sentence, e.g. "Girls' Room
  reached 79.5°F"), at most once per event and unit (``detail.rule_requested_at``), so the
  owner's undo sticks. Otherwise (suggest mode, or a suggest-only unit) it only alerts, once
  per event and unit. Mandatory events and mode 'off' are never considered.
- ``run`` (every worker loop, after the controller tick) evaluates the rules, then sends each
  requested skip whose unit's live snapshot shows that event running on top: a
  ``control_actions`` row (action 'opt_out_event', rule 'utility_opt_out', mode 'act', actor
  'owner', or 'controller' for a rule, channel = the source kind) is committed as 'sent'
  BEFORE the call, then marked verified / failed with the read-back. The call names the event
  (``link_ref`` / ``name`` / ``start`` from the row), so the source refuses rather than opt
  out of a different event that is on top by then. Owner skips run in suggest and act mode;
  rule skips only in act mode for units in act_units. Nothing is sent before the event is due
  (running, or its start has passed) or once it is over by the clock. Mode 'off', and an open
  cloud circuit (HomeKit can neither see nor cancel events), make a due skip wait, with an
  alert saying so that is resolved as soon as the cause clears (raised again if it comes
  back). A failed call is retried on later loops, at most 3 attempts in all
  (``detail.skip_attempts``, counted before each call), then the skip is 'failed'. A refusal
  showing the event is no longer on top (another utility event is, or none runs: the
  snapshot the skip was decided on is older than the thermostat) is not an attempt: the skip
  waits for the next snapshot. A refusal because the event is mandatory makes it 'refused'.
- An opt-out whose call went out (``request.sent``) but could not be confirmed (the
  read-back failed or lagged) is settled 'done' once the event is seen gone: by ``_record``
  when the row closed meanwhile, and by ``events`` ingest / sweep through ``landed`` when
  the event disappears later (the row is 'opted_out', not 'ended').

Alerts are grouped per event (kind 'utility_event', ``events.phase_alert``: once per event
and phase, text kept current while open): 'skipped' (info, pushed; only while
``UtilityEventSettings.alerts`` is on), 'skip_refused' (warn), 'skip_failed' (error),
'skip_waiting_off' (info, pushed) and 'skip_waiting_cloud' (warn) (these two once per wait:
resolved when the cause clears, raised again if it returns), and 'rule:<unit>' (info,
pushed). The sweep resolves them once the event is over (``events.sweep``).
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import ValidationError
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from climate.collector.poller import safe_error
from climate.control.policy import HouseState
from climate.events import publish
from climate.house import ROOMS
from climate.sources.base import ThermostatEvent, ThermostatSource, UnitSnapshot, WriteResult
from climate.store.app_settings import ControlSettings, SourceSettings, UtilityEventSettings, get_setting
from climate.store.db import session_scope
from climate.store.orm import ControlAction, LiveUnit, UtilityEvent
from climate.timeutil import utcnow
from climate.utility import events

log = logging.getLogger(__name__)

ACTION = "opt_out_event"
RULE = "utility_opt_out"
REQUEST_KIND = "utility_opt_out"
OWNER_REASON = "Skipped from the app"
MAX_ATTEMPTS = 3
RULE_LOOKAHEAD = timedelta(minutes=5)  # rules consider events running or starting this soon
LANDED_WITHIN = timedelta(minutes=15)  # an unconfirmed opt-out this recent explains an event that vanished
OUTDOOR_COOLING_F = 65.0  # 'auto' mode with nothing else to go on: cooling at or above this
_ROOM_ORDER = {r.key: i for i, r in enumerate(ROOMS)}
Direction = Literal["cool", "heat"]


@dataclass
class _OptOut:
    """A requested skip whose control_actions row is committed as 'sent'."""

    action_id: int
    event_id: int
    unit_key: str
    attempt: int
    reason: str
    channel: str
    link_ref: str | None = None  # the event's identity, so the source opts out of this one only
    name: str | None = None
    start: datetime | None = None


# ---------------------------------------------------------------------------------------
# rules
# ---------------------------------------------------------------------------------------


def _acts(control: ControlSettings, unit_key: str) -> bool:
    return control.mode == "act" and unit_key in control.act_units


def _rule_key(row: UtilityEvent) -> str:
    return events.alert_key(row.event_key, f"rule:{row.unit_key}")


def _deg(temp_f: float) -> str:
    return f"{round(temp_f, 1):g}°F"


def _join(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _quoted(name: str | None) -> str:
    return f" “{name}”" if name else ""


def _event_direction(row: UtilityEvent) -> Direction | None:
    """The way the event itself pushes, when it says only one way: a cooling offset up, a cool
    setpoint alone or AC off -> cooling; a heating offset down, a heat setpoint alone or heat
    off -> heating."""
    d = row.detail or {}
    if row.is_relative:
        cool = (row.cool_offset_f or 0.0) > 0 or bool(d.get("is_cool_off"))
        heat = (row.heat_offset_f or 0.0) < 0 or bool(d.get("is_heat_off"))
    else:
        cool = (row.cool_f is not None and row.heat_f is None) or bool(d.get("is_cool_off"))
        heat = (row.heat_f is not None and row.cool_f is None) or bool(d.get("is_heat_off"))
    if cool != heat:
        return "cool" if cool else "heat"
    return None


def direction(state: HouseState, row: UtilityEvent) -> Direction | None:
    """Whether the unit is cooling or heating for the temperature rules: its hvac_mode ('cool';
    'heat' / 'auxHeatOnly'); in 'auto' what it is calling for, else the event's own direction,
    else the outdoor temperature (cooling at 65°F or above). 'off' or no snapshot -> None."""
    unit = state.units.get(row.unit_key)
    snap = unit.snapshot if unit is not None else None
    if snap is None:
        return None
    if snap.hvac_mode == "cool":
        return "cool"
    if snap.hvac_mode in ("heat", "auxHeatOnly"):
        return "heat"
    if snap.hvac_mode != "auto":
        return None
    if unit is not None and unit.call in ("cool", "heat"):
        return unit.call  # type: ignore[return-value]
    own = _event_direction(row)
    if own is not None:
        return own
    outdoor = state.outdoor_temp_f if state.outdoor_temp_f is not None else snap.outdoor_temp_f
    if outdoor is None:
        return None
    return "cool" if outdoor >= OUTDOOR_COOLING_F else "heat"


def rule_match(state: HouseState, row: UtilityEvent, cfg: UtilityEventSettings) -> str | None:
    """The first skip rule the house state matches for the event's unit, as the sentence that
    becomes ``skip_reason``, else None. Temperature rules first: an occupied or asleep room
    with a fresh sensor reading at or above ``skip_above_f`` while cooling ("Girls' Room
    reached 79.5°F"; the warmest such room), at or below ``skip_below_f`` while heating; rooms
    without a sensor never count (no invented temperature). Then ``skip_when_asleep``: a room
    on the unit is asleep ("Someone is asleep in the Girls' Room")."""
    rooms = sorted((r for r in state.rooms.values() if r.unit_key == row.unit_key),
                   key=lambda r: _ROOM_ORDER.get(r.room_key, len(_ROOM_ORDER)))
    way = direction(state, row)
    with_people = [r for r in rooms if r.state in ("occupied", "asleep") and r.temp_f is not None and not r.stale]
    if way == "cool" and cfg.skip_above_f is not None:
        hot = [r for r in with_people if r.temp_f is not None and r.temp_f >= cfg.skip_above_f]
        if hot:
            room = max(hot, key=lambda r: r.temp_f or 0.0)
            return f"{room.name} reached {_deg(room.temp_f or 0.0)}"
    if way == "heat" and cfg.skip_below_f is not None:
        cold = [r for r in with_people if r.temp_f is not None and r.temp_f <= cfg.skip_below_f]
        if cold:
            room = min(cold, key=lambda r: r.temp_f or 0.0)
            return f"{room.name} fell to {_deg(room.temp_f or 0.0)}"
    if cfg.skip_when_asleep:
        asleep = [r.name for r in rooms if r.state == "asleep"]
        if asleep:
            return f"Someone is asleep in the {_join(asleep)}"
    return None


def apply_rules(session: Session, now: datetime) -> list[int]:
    """Evaluate the skip rules (module docstring). Returns the ids of rows it requested a skip
    for. Cheap when there is nothing to decide: the house state is loaded only when an
    optional, unskipped event is running or starts within 5 minutes and is not settled yet
    (in suggest mode: not alerted yet)."""
    cfg = events.event_settings(session)
    if not cfg.auto_skip or not (cfg.skip_when_asleep or cfg.skip_above_f is not None or cfg.skip_below_f is not None):
        return []
    control = _control(session)
    if control.mode == "off":
        return []
    rows = list(session.execute(
        select(UtilityEvent).where(
            UtilityEvent.skip.is_(None),
            UtilityEvent.status.in_(events.OPEN),
            UtilityEvent.is_optional.is_not(False),
            or_(UtilityEvent.status == "running", UtilityEvent.start_at <= now + RULE_LOOKAHEAD),
            or_(UtilityEvent.end_at.is_(None), UtilityEvent.end_at > now),
        ).order_by(UtilityEvent.id).with_for_update(skip_locked=True)
    ).scalars())
    rows = [r for r in rows if not (r.detail or {}).get("rule_requested_at")]  # a rule asks once
    pending = [r for r in rows if _acts(control, r.unit_key) or not events.alert_exists(session, _rule_key(r))]
    if not pending:
        return []
    from climate.state import load_house_state  # local: state pulls in the occupancy and control packages

    state = load_house_state(session, now)
    requested: list[int] = []
    for row in pending:
        why = rule_match(state, row, cfg)
        if why is None:
            continue
        if _acts(control, row.unit_key):
            row.skip, row.skip_by, row.skip_reason, row.skip_requested_at = "requested", "rule", why, now
            row.detail = {**(row.detail or {}), "rule_requested_at": now.isoformat()}
            requested.append(row.id)
            log.info("skip rule matched on %s (%s): requesting an opt-out of event %s", row.unit_key, why, row.id)
            continue
        unit = events.unit_names([row.unit_key])
        why_not = ("In Suggest mode the app doesn't skip on its own" if control.mode == "suggest"
                   else f"{unit} is suggest-only, so the app doesn't skip there on its own")
        events.phase_alert(
            session, _rule_key(row), "info", f"Your skip rule matched on {unit}",
            f"{why} during the utility event{_quoted(row.name)} ({events.window_label(row.start_at, row.end_at, state.tz)}). "
            f"{why_not}; tap Skip on Live.", push_info=True,
        )
    if requested:
        session.flush()
        publish(session, "status")
    return requested


# ---------------------------------------------------------------------------------------
# sending the opt-out
# ---------------------------------------------------------------------------------------


def _control(session: Session) -> ControlSettings:
    try:
        return get_setting(session, "control", ControlSettings)
    except Exception:  # noqa: BLE001 - unreadable settings: treat as off (nothing is sent)
        log.warning("control settings unreadable; skips wait")
        return ControlSettings(mode="off")


def _circuit_open(session: Session, now: datetime) -> bool:
    try:
        src = get_setting(session, "source", SourceSettings)
    except Exception:  # noqa: BLE001
        return False
    return src.cloud_circuit_open_until is not None and src.cloud_circuit_open_until > now


def _live(session: Session, unit_keys: set[str]) -> dict[str, UnitSnapshot]:
    out: dict[str, UnitSnapshot] = {}
    for row in session.execute(select(LiveUnit).where(LiveUnit.unit_key.in_(sorted(unit_keys)))).scalars():
        try:
            out[row.unit_key] = UnitSnapshot.model_validate(row.snapshot)
        except ValidationError:
            log.warning("live_units snapshot for %s does not parse; its skip waits", row.unit_key)
    return out


def on_top(snap: UnitSnapshot, key: str) -> bool:
    """The snapshot shows this event running AND on top (the event an opt-out would cancel):
    the top running event is demandResponse, this event is listed as running, and the top one
    is this one (by linkRef / name + start when the source reports them, else it is the only
    running utility event)."""
    hold = snap.hold
    if hold is None or hold.hold_type != events.DR:
        return False
    running = [events.event_key(ev) for ev in snap.events if ev.event_type == events.DR and ev.running]
    if key not in running:
        return False
    if hold.link_ref or hold.event_name:
        top = events.event_key(ThermostatEvent(event_type=events.DR, name=hold.event_name, start=hold.start,
                                               link_ref=hold.link_ref))
        return top == key
    return len(running) == 1


def _before(snap: UnitSnapshot) -> dict[str, Any]:
    return {
        "heat_f": snap.heat_sp_f, "cool_f": snap.cool_sp_f, "hvac_mode": snap.hvac_mode, "climate_ref": snap.climate_ref,
        "zone_temp_f": snap.zone_temp_f, "hold": snap.hold.model_dump(mode="json") if snap.hold is not None else None,
    }


def _rows_of(session: Session, key: str) -> list[UtilityEvent]:
    return list(session.execute(
        select(UtilityEvent).where(UtilityEvent.event_key == key).order_by(UtilityEvent.id)
    ).scalars())


def _waiting(session: Session, row: UtilityEvent, why: Literal["off", "cloud"]) -> None:
    """Say why a due skip is not being sent: one open alert per event and cause, resolved by
    ``_prepare`` once the cause clears, and raised again if it comes back."""
    units = [r.unit_key for r in _rows_of(session, row.event_key) if r.skip == "requested"] or [row.unit_key]
    where = events.unit_names(units, lead=False)
    if why == "off":
        events.phase_alert(
            session, events.alert_key(row.event_key, "skip_waiting_off"), "info", "Skip waiting: the controller is off",
            f"The skip of the utility event on {where} waits until the controller is switched to Suggest or Act. "
            "The event runs as planned meanwhile.", push_info=True, again=True,
        )
    else:
        events.phase_alert(
            session, events.alert_key(row.event_key, "skip_waiting_cloud"), "warn", "Skip waiting for the ecobee cloud",
            "The ecobee cloud isn't answering, and HomeKit can neither see nor cancel utility events. The skip of the "
            f"event on {where} is sent once the cloud is back; the event runs as planned meanwhile.", again=True,
        )


def _settle(
    session: Session, row: UtilityEvent, outcome: Literal["done", "refused", "failed"], action_id: int | None,
    now: datetime, *, error: str | None = None,
) -> None:
    """Record a skip's outcome on the row and alert (grouped per event)."""
    row.skip = outcome
    if action_id is not None:
        row.skip_action_id = action_id
    if outcome == "done":
        row.skip_done_at = now
        row.status = "opted_out"
        row.ended_at = row.ended_at or now
    elif outcome == "refused":
        row.is_optional = False
    session.flush()
    key = row.event_key
    rows = _rows_of(session, key)
    if not any(r.skip == "requested" for r in rows):
        events.resolve_keys(session, [events.alert_key(key, "skip_waiting_off"), events.alert_key(key, "skip_waiting_cloud")])
    where = events.unit_names(r.unit_key for r in rows if r.skip == outcome)
    if outcome == "done":
        cfg = events.event_settings(session)
        if cfg.alerts:
            events.phase_alert(
                session, events.alert_key(key, "skipped"), "info", f"Skipped the utility event on {where}",
                "ecobee recorded the opt-out, and the normal settings are back.", push_info=True,
            )
        events.reconcile_alerts(session, key, cfg=cfg)
    elif outcome == "refused":
        events.phase_alert(
            session, events.alert_key(key, "skip_refused"), "warn", f"Can't skip the utility event on {where}",
            "This event is mandatory: ecobee doesn't allow opting out of it. The app keeps standing aside until it ends.",
        )
    else:
        last = f" Last error: {error[:300]}." if error else ""
        events.phase_alert(
            session, events.alert_key(key, "skip_failed"), "error", f"Couldn't skip the utility event on {where}",
            f"The opt-out failed {MAX_ATTEMPTS} times.{last} The app keeps standing aside until the event ends; you can "
            "still opt out in the ecobee app.",
        )
    publish(session, "status")


def _prepare_row(
    session: Session, row: UtilityEvent, control: ControlSettings, kind: str | None, circuit_open: bool,
    snap: UnitSnapshot | None, now: datetime,
) -> _OptOut | None:
    by = row.skip_by or "owner"
    if row.is_optional is False:
        _settle(session, row, "refused", None, now)  # known mandatory: nothing to send
        return None
    if not _due(row, now):
        return None  # not started yet, or over by the clock (ingest / the sweep close it): nothing to say
    if control.mode == "off":
        _waiting(session, row, "off")
        return None
    if by == "rule" and not _acts(control, row.unit_key):
        return None  # a rule's skip runs only in act mode, for an act unit
    if kind is None:
        return None  # no thermostat source yet
    if circuit_open:
        _waiting(session, row, "cloud")
        return None
    if snap is None or snap.source == "homekit" or not on_top(snap, row.event_key):
        return None  # not running (yet), or not the top event: nothing to opt out of now
    attempt = int((row.detail or {}).get("skip_attempts") or 0) + 1
    row.detail = {**(row.detail or {}), "skip_attempts": attempt}
    why = row.skip_reason or OWNER_REASON
    if by == "rule":
        why += " (your skip rule)"
    unit = events.unit_names([row.unit_key])
    reason = f"{why}: opting out of the utility event{_quoted(row.name)} on {unit}; ecobee records the opt-out."
    action = ControlAction(
        ts=now, unit_key=row.unit_key, actor="owner" if by == "owner" else "controller", mode="act", channel=kind,
        action=ACTION, status="sent", rule=RULE, reason=reason, before=_before(snap),
        request={"kind": REQUEST_KIND, "event_id": row.id, "by": by},
    )
    session.add(action)
    session.flush()
    publish(session, "action", action.id)
    detail = row.detail if isinstance(row.detail, dict) else {}
    return _OptOut(action.id, row.id, row.unit_key, attempt, reason, kind,
                   link_ref=detail.get("link_ref") or None, name=row.name or detail.get("name") or None,
                   start=row.start_at)


def _due(row: UtilityEvent, now: datetime) -> bool:
    """The skip could go now: the event runs (or its start has passed; the snapshot may lag) and
    it is not over by the clock."""
    if row.end_at is not None and row.end_at <= now:
        return False
    return row.status == "running" or (row.start_at is not None and row.start_at <= now)


def _prepare(session: Session, source: ThermostatSource | None, now: datetime) -> list[_OptOut]:
    """Log a 'sent' row for each requested skip that can go now (committed by the caller
    before any call); the others wait (a due one says why, ``_waiting``). Resolves the "skip
    waiting" alerts of these events whose cause has cleared."""
    rows = list(session.execute(
        select(UtilityEvent).where(UtilityEvent.skip == "requested", UtilityEvent.status.in_(events.OPEN))
        .order_by(UtilityEvent.id).with_for_update(skip_locked=True)
    ).scalars())
    if not rows:
        return []
    control = _control(session)
    kind = getattr(source, "kind", None) if source is not None else None
    circuit_open = kind == "ecobee" and _circuit_open(session, now)
    live = _live(session, {r.unit_key for r in rows})
    out: list[_OptOut] = []
    for row in rows:
        try:
            with session.begin_nested():
                p = _prepare_row(session, row, control, kind, circuit_open, live.get(row.unit_key), now)
            if p is not None:
                out.append(p)
        except Exception:  # one row's failure never stops the others
            log.exception("could not prepare the skip of utility event %s", row.id)
    # a wait whose cause has cleared (the cloud is back, the controller is on again) is over
    cleared = [phase for phase, over in (("skip_waiting_cloud", not circuit_open),
                                         ("skip_waiting_off", control.mode != "off")) if over]
    events.resolve_keys(session, [events.alert_key(k, phase) for k in {r.event_key for r in rows} for phase in cleared])
    return out


def _mandatory(result: WriteResult) -> bool:
    req = result.request if isinstance(result.request, dict) else {}
    if "mandatory" in req:
        return req.get("mandatory") is True
    return bool(req.get("refused")) and "mandatory" in (result.error or "").lower()


async def _send(source: ThermostatSource | None, p: _OptOut) -> WriteResult:
    try:
        if source is None:
            raise RuntimeError("no thermostat source is running")
        if not _takes_identity(source.opt_out_event):  # an older source: it cannot check the event anyway
            return await source.opt_out_event(p.unit_key, p.reason[:500])
        return await source.opt_out_event(p.unit_key, p.reason[:500], link_ref=p.link_ref, name=p.name,
                                          start=p.start)
    except Exception as exc:  # noqa: BLE001 - the error is the record
        log.warning("opt-out on %s failed: %s", p.unit_key, safe_error(exc))
        channel = p.channel if p.channel in ("ecobee", "simulator") else "none"
        return WriteResult(ok=False, channel=channel, error=safe_error(exc, 1000))  # type: ignore[arg-type]


def _takes_identity(fn: Any) -> bool:
    """The source's ``opt_out_event`` accepts the event identity (``link_ref`` / ``name`` /
    ``start``, or ``**kwargs``, like the worker's locked wrapper). Decided from the signature,
    never by retrying a call that failed: the first call may have reached ecobee."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return True
    return "link_ref" in params or any(v.kind is inspect.Parameter.VAR_KEYWORD for v in params.values())


def _record(session: Session, p: _OptOut, result: WriteResult, now: datetime) -> None:
    """The call's outcome onto its control_actions row and the event row."""
    action = session.get(ControlAction, p.action_id)
    if action is not None:
        action.status = "verified" if result.ok else "failed"
        action.readback = dict(result.readback) if result.readback is not None else None
        action.readback_ok = bool(result.ok) if result.readback is not None else (None if result.ok else False)
        if result.before:
            action.before = {**(action.before or {}), "source": dict(result.before)}
        if result.request:
            action.request = {**(action.request or {}), "sent": dict(result.request)}
        action.error = None if result.ok else (result.error or "The read-back still shows the event running.")
        action.completed_at = now
        publish(session, "action", action.id)
    row = session.execute(
        select(UtilityEvent).where(UtilityEvent.id == p.event_id).with_for_update()
    ).scalar_one_or_none()
    if row is None:
        return
    req = result.request if isinstance(result.request, dict) else {}
    if result.ok:
        _settle(session, row, "done", p.action_id, now)
    elif row.skip != "requested":
        return  # the owner took the skip back meanwhile
    elif req.get("sent") is True and row.status not in events.OPEN:
        # The opt-out went out, the read-back could not confirm it, and a snapshot has shown
        # the event gone meanwhile: it landed.
        settle_landed(session, row, p.action_id, now)
    elif _mandatory(result):
        _settle(session, row, "refused", p.action_id, now)
    elif _gone(req, result.before if isinstance(result.before, dict) else {}):
        # Another event is on top, or none runs: not this skip's failure, and not an attempt.
        # The next snapshot closes the row (settled 'done' when our earlier opt-out landed).
        row.detail = {**(row.detail or {}), "skip_attempts": max(0, p.attempt - 1)}
        log.info("opt-out of utility event %s on %s not sent: %s; waiting for the next snapshot",
                 row.id, row.unit_key, result.error or "the event is not on top")
    elif p.attempt >= MAX_ATTEMPTS:
        _settle(session, row, "failed", p.action_id, now, error=result.error)
    else:
        log.info("opt-out of utility event %s on %s failed (attempt %d of %d); retrying on a later loop",
                 row.id, row.unit_key, p.attempt, MAX_ATTEMPTS)


def _gone(sent: dict[str, Any], before: dict[str, Any]) -> bool:
    """A refusal showing the event the skip is for is no longer on top: a different utility
    event is (``other_event``), or no utility event runs at all (the source's fresh read)."""
    if not sent.get("refused") or sent.get("mandatory") is True:
        return False
    return bool(sent.get("other_event")) or before.get("demand_response_running") is False


def landed(session: Session, row: UtilityEvent, at: datetime) -> int | None:
    """The id of the opt-out that explains why this event, with its skip still requested, is
    no longer listed at ``at``: the newest finished attempt from the last 15 minutes that
    tells anything (refusals showing the event already gone are skipped) went out
    (``request.sent``) without a confirming read-back. None otherwise (it ran to its end, or no
    opt-out went out). An attempt still in flight is left to ``_record``."""
    if row.skip != "requested":
        return None
    attempts = session.execute(
        select(ControlAction).where(
            ControlAction.action == ACTION,
            ControlAction.status.in_(("verified", "failed")),
            ControlAction.ts >= at - LANDED_WITHIN,
            ControlAction.request["event_id"].as_integer() == row.id,
        ).order_by(ControlAction.ts.desc(), ControlAction.id.desc())
    ).scalars()
    for action in attempts:
        req = action.request if isinstance(action.request, dict) else {}
        sent = req.get("sent") if isinstance(req.get("sent"), dict) else {}
        before = action.before if isinstance(action.before, dict) else {}
        source_before = before.get("source") if isinstance(before.get("source"), dict) else {}
        if _gone(sent, source_before):
            continue
        return action.id if sent.get("sent") is True else None
    return None


def settle_landed(session: Session, row: UtilityEvent, action_id: int, at: datetime) -> None:
    """Record an opt-out found to have landed (``landed``): the skip 'done', the row
    'opted_out', and the "Skipped" alert, as for a confirmed one."""
    action = session.get(ControlAction, action_id)
    if action is not None and action.status == "failed":
        note = "the event was gone at the next snapshot, so the opt-out landed"
        action.error = f"{action.error or 'Not confirmed'}; {note}."[:1000]
        publish(session, "action", action.id)
    _settle(session, row, "done", action_id, at)


async def run(source: ThermostatSource | None, now: datetime | None = None) -> list[int]:
    """Skip rules, then the requested opt-outs (module docstring). Returns the ids of the
    control_actions rows it created. A failure in the rules or in one row is logged and never
    stops the rest."""
    now = now or utcnow()
    try:
        with session_scope() as s:
            apply_rules(s, now)
    except Exception:  # a broken rule never blocks the owner's own skips
        log.exception("utility skip rules failed")
    with session_scope() as s:
        prepared = _prepare(s, source, now)
    ids: list[int] = []
    for p in prepared:
        result = await _send(source, p)
        try:
            with session_scope() as s:
                _record(s, p, result, now)
        except Exception:  # the 'sent' row stays as the record
            log.exception("could not record the result of opt-out action %s", p.action_id)
        ids.append(p.action_id)
    return ids
