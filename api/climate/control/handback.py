"""Hand back to ecobee: put the thermostats back the way they were before the controller.

``capture_original`` records, once per unit, the ecobee settings the controller may change
(Smart Away, Follow Me, the Home comfort setting's sensors) in
``app_settings['ecobee_original']`` = ``{unit_key: EcobeeOriginal}``. The controller calls it
every tick and just before its own first settings or sensor-set write; an entry is never
overwritten once captured, so it keeps the values from before the controller's first change.

``hand_back`` (the worker's ``handback`` job, ``POST /api/control/handback``) then, in order:
1. switches the controller off (and commits that first, failing any controller write still
   queued for the HomeKit fallback), so nothing writes again and the worker's
   ``keep_settings`` does not switch Smart Away off again. The worker runs the hand-back
   under the same lock as the controller's tick, queued owner actions and settings check, so
   a write already in flight finishes first, and none starts after;
2. re-reads the thermostats through the source and stores what they show (the last poll can
   be minutes older than the controller's latest writes); when that fails it judges from the
   last poll and says so, and a controller write newer than that poll is not taken as undone;
3. resumes each unit's schedule where the running hold is the controller's, or where the
   controller wrote a hold the last poll cannot show yet (never forced: the source decides
   on its own fresh read and leaves a person's hold alone). The controller's HomeKit
   comfort-setting hold is left to end on its own (the cloud would not cancel it);
4. restores each unit's Home sensor set to the captured one, when it differs;
5. restores Smart Away / Follow Me to the captured values, where they differ.
Person holds, vacations and utility events are left alone. Every write is logged to
``control_actions`` (actor 'owner', rule 'handback') with before / request / read-back, logged
'sent' before the source is called. A step that cannot run (no thermostat source, a source
that cannot write that setting, the simulator (the originals are the real ecobee
thermostats'), data only from HomeKit, a person's hold or an event in the way, nothing
captured) is reported as skipped, never failed silently. The result is a list of
``HandbackStep``-shaped dicts, and an alert (pushed) says what was done: "Handed back to
ecobee" (info) when everything is back, else "Hand-back incomplete" (warn) listing what failed
and what is not done yet (hand back again to finish).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from climate.api.schemas import HandbackStep
from climate.control.policy import UnitStatus
from climate.events import publish
from climate.house import UNIT_KEYS
from climate.notify import raise_alert
from climate.sources.base import PLAIN_HOLD_TYPES, ThermostatSource, UnitSnapshot, WriteResult
from climate.store.app_settings import (
    ECOBEE_ORIGINAL_KEY,
    ControlSettings,
    EcobeeOriginal,
    get_raw,
    get_setting,
    put_setting,
)
from climate.store.db import session_scope
from climate.store.orm import ControlAction
from climate.timeutil import to_local, utcnow

log = logging.getLogger(__name__)

RULE = "handback"
REQUEST_KIND = "handback"  # request.kind of its resume rows (neither 'automatic' nor 'resume_schedule')
SETTING_NAMES = {"autoAway": "Smart Away", "followMeComfort": "Follow Me"}
# HandbackStep.what per step
CONTROLLER_OFF = "Controller off"
RESUMED = "Resumed our hold"
SENSORS_RESTORED = "Home sensors restored"
SETTINGS_RESTORED = "Smart Away and Follow Me restored"
READ = "Read the thermostats"
NO_SOURCE = "no thermostat source is running."
# The restores the owner still has to finish when skipped (hand back again), as the alert names them.
PENDING_NAMES = {SENSORS_RESTORED: "Home sensors", SETTINGS_RESTORED: "Smart Away and Follow Me"}
NOTHING_CAPTURED = "Skipped: no ecobee"  # starts a skip that leaves nothing to do (nothing was captured)
SIMULATOR = ("these were captured from the real ecobee thermostats and the source is the simulator. Switch the "
             "source to ecobee and hand back again.")


# ---------------------------------------------------------------------------------------
# capture
# ---------------------------------------------------------------------------------------


def load_originals(session: Session) -> dict[str, EcobeeOriginal]:
    """The captured originals per unit (entries that do not parse are left out)."""
    raw = get_raw(session, ECOBEE_ORIGINAL_KEY)
    out: dict[str, EcobeeOriginal] = {}
    if not isinstance(raw, dict):
        return out
    for unit_key, value in raw.items():
        try:
            out[str(unit_key)] = EcobeeOriginal.model_validate(value)
        except ValidationError:
            log.warning("ecobee_original[%s] does not parse; ignoring it", unit_key)
    return out


def capture_original(session: Session, snapshots: Iterable[UnitSnapshot | None], now: datetime | None = None) -> list[str]:
    """Capture each ecobee snapshot's unit not captured yet (Smart Away, Follow Me, the Home
    sensor set). Only snapshots from the ecobee cloud count (the simulator and HomeKit don't
    show the real thermostat's settings). An existing entry is never overwritten. Returns the
    unit keys captured now."""
    fresh = [s for s in snapshots if s is not None and s.source == "ecobee"]
    if not fresh:
        return []
    raw = get_raw(session, ECOBEE_ORIGINAL_KEY)
    stored: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
    added: list[str] = []
    for snap in fresh:
        if snap.unit_key in stored:
            continue
        home = snap.sensor_sets.get("home")
        stored[snap.unit_key] = EcobeeOriginal(
            captured_at=now or utcnow(),
            auto_away=_bool(snap.settings.get("autoAway")),
            follow_me=_bool(snap.settings.get("followMeComfort")),
            home_sensors=list(home) if home is not None else None,
        ).model_dump(mode="json")
        added.append(snap.unit_key)
    if added:
        put_setting(session, ECOBEE_ORIGINAL_KEY, stored, updated_by="controller")
        log.info("captured the original ecobee settings of %s", ", ".join(added))
    return added


# ---------------------------------------------------------------------------------------
# hand back
# ---------------------------------------------------------------------------------------


async def hand_back(source: ThermostatSource | None, now: datetime | None = None) -> list[dict[str, Any]]:
    """Switch the controller off, then undo what it changed on the thermostats (module
    docstring). Never raises for a single step; returns the steps as dicts."""
    from climate.state import load_house_state  # local: state imports the control package

    now = now or utcnow()
    steps: list[HandbackStep] = []
    try:
        with session_scope() as s:
            control = _control(s)
            was = control.mode
            put_setting(s, "control", control.model_copy(update={"mode": "off"}), updated_by="owner")
            dropped = _fail_queued_controller_writes(s, now)
            publish(s, "status")
        tail = f" {dropped} queued controller write(s) were dropped." if dropped else ""
        steps.append(HandbackStep(what=CONTROLLER_OFF, ok=True,
                                  detail=f"Was {was}; it writes nothing until you switch it back on.{tail}"))
    except Exception as exc:  # without this first step nothing else may run
        log.exception("hand back: could not switch the controller off")
        steps.append(HandbackStep(what=CONTROLLER_OFF, ok=False, detail=f"{type(exc).__name__}: {exc}"[:500]))
        _finish(steps)
        return [st.model_dump() for st in steps]

    # Without a fresh read (no source, or it failed) the snapshots are the last poll's, which
    # can predate the controller's latest writes: those writes then count as not undone yet.
    stale = True
    if source is not None:
        read = await _reread(source, now)
        if read is not None:
            steps.append(read)
        stale = read is not None
    try:
        with session_scope() as s:
            state = load_house_state(s, now)
            originals = load_originals(s)
            recent = {u: _recent_writes(s, u) if stale else {} for u in UNIT_KEYS}
    except Exception as exc:  # the controller is off already; nothing else can be judged
        log.exception("hand back: could not read the house state")
        steps.append(HandbackStep(what=READ, ok=False, detail=f"{type(exc).__name__}: {exc}"[:500]))
        _finish(steps)
        return [st.model_dump() for st in steps]
    kind = getattr(source, "kind", None) if source is not None else None
    channel = kind if kind in ("ecobee", "simulator") else "none"

    for unit_key in UNIT_KEYS:
        unit = state.units.get(unit_key)
        snap = unit.snapshot if unit is not None else None
        name = unit.name if unit is not None else unit_key
        original = originals.get(unit_key)
        writes = recent[unit_key]
        for what in (RESUMED, SENSORS_RESTORED, SETTINGS_RESTORED):
            try:
                if what == RESUMED:
                    steps += await _resume_ours(source, channel, unit, unit_key, name, writes, state.tz,
                                                state.control.hold_hours, now)
                elif what == SENSORS_RESTORED:
                    steps += await _restore_sensors(source, channel, unit_key, name, snap, original, writes, now)
                else:
                    steps += await _restore_settings(source, channel, unit_key, name, snap, original, writes, now)
            except Exception as exc:  # report the step, carry on with the others
                log.exception("hand back: %r failed for %s", what, unit_key)
                steps.append(HandbackStep(unit_key=unit_key, what=what, ok=False,
                                          detail=f"{type(exc).__name__}: {exc}"[:500]))
    _finish(steps)
    return [st.model_dump() for st in steps]


def _fail_queued_controller_writes(s: Session, now: datetime) -> int:
    """Fail every controller write still queued (the HomeKit fallback's), in the transaction
    that switches the controller off, so none reaches a thermostat after the hand-back."""
    ids = list(s.execute(
        update(ControlAction)
        .where(ControlAction.status == "queued", ControlAction.actor == "controller")
        .values(status="failed", error="Not sent: handed back to ecobee.", completed_at=now)
        .returning(ControlAction.id)
        .execution_options(synchronize_session=False)
    ).scalars())
    for row_id in ids:
        publish(s, "action", row_id)
    return len(ids)


async def _reread(source: ThermostatSource, now: datetime) -> HandbackStep | None:
    """Read every thermostat through the source and store the snapshots (as a poll does), so
    the steps judge what the thermostats show now, the controller's latest writes included.
    None when that worked; else a step saying the last poll is used instead."""
    from climate.collector.poller import _ingest_snapshots_tx  # local: the collector imports control

    try:
        snaps = list(await source.fetch_snapshots(None))
        if not snaps:
            return HandbackStep(what=READ, ok=True,
                                detail="The source returned no thermostats; judged from the last poll.")
        await asyncio.to_thread(_ingest_snapshots_tx, snaps, now)
        return None
    except Exception as exc:  # the cloud is down, say: the last poll and the write log decide
        log.warning("hand back: could not re-read the thermostats (%s); judging from the last poll",
                    type(exc).__name__)
        return HandbackStep(what=READ, ok=True,
                            detail=f"Could not re-read them ({type(exc).__name__}); judged from the last poll.")


def _recent_writes(s: Session, unit_key: str) -> dict[str, ControlAction | None]:
    """The controller's newest hold write or resume that went out ('hold'), sensor-set write
    ('sensors') and settings write ('settings') for the unit (verified, sent or failed: a
    failed one may have landed). Only read when the thermostats could not be re-read."""
    out: dict[str, ControlAction | None] = {}
    for key, actions in (("hold", ("set_hold", "resume_program")), ("sensors", ("update_program",)),
                         ("settings", ("update_settings",))):
        out[key] = s.execute(
            select(ControlAction)
            .where(ControlAction.unit_key == unit_key, ControlAction.actor == "controller",
                   ControlAction.action.in_(actions), ControlAction.status.in_(("verified", "sent", "failed")),
                   ControlAction.channel != "none")
            .order_by(ControlAction.ts.desc(), ControlAction.id.desc())
            .limit(1)
        ).scalar_one_or_none()
    return out


def _unshown(write: ControlAction | None, snap: UnitSnapshot | None) -> bool:
    """``write`` is too recent for the snapshot to show it (``state.snapshot_predates``)."""
    from climate.state import snapshot_predates  # local: state imports the control package

    return write is not None and snapshot_predates(snap, write)


async def _resume_ours(
    source: ThermostatSource | None, channel: str, unit: UnitStatus | None, unit_key: str, name: str,
    writes: dict[str, ControlAction | None], tz: str, hold_hours: int, now: datetime,
) -> list[HandbackStep]:
    """Step 3: cancel the controller's own running hold (not forced). That is the snapshot's
    hold of ours, or (judging from the last poll, ``writes``), while no person's hold or event
    is known, the controller's latest hold write when the snapshot is too old to show it and
    its window is not over: the source decides on its own fresh read, and a hold that is not
    the controller's is refused and left alone."""
    from climate.state import write_end  # local: state imports the control package

    snap = unit.snapshot if unit is not None else None
    hold = snap.hold if snap is not None else None
    ends: datetime | None = None
    if hold is not None and hold.set_by_us:
        ends = hold.end
        if hold.kind == "climate" and snap is not None and snap.source != "homekit":
            until = f" at {_clock(ends, tz)}" if ends is not None else ""
            return [HandbackStep(unit_key=unit_key, what=RESUMED, ok=True,
                                 detail=f"Left to end on its own{until}: the controller's HomeKit hold, which the "
                                        "ecobee cloud does not let it cancel.")]
    else:
        write = writes.get("hold")
        person = unit.person_hold if unit is not None else None
        event = hold is not None and hold.hold_type is not None and hold.hold_type not in PLAIN_HOLD_TYPES
        if write is None or write.action != "set_hold" or write.channel == "homekit" or person is not None \
                or event or not _unshown(write, snap):
            return []  # no hold of ours (a person's hold, an event or the schedule): left alone
        ends = write_end(write, hold_hours)
        if ends is None or ends <= now:
            return []  # it has ended on its own
    what = RESUMED
    tail = f" Our hold ends on its own at {_clock(ends, tz)}." if ends is not None else ""
    if snap is not None and snap.source == "homekit":
        return [HandbackStep(unit_key=unit_key, what=what, ok=True,
                             detail=f"Skipped: only HomeKit data, the ecobee cloud is unavailable.{tail}")]
    if source is None:
        return [HandbackStep(unit_key=unit_key, what=what, ok=True, detail=f"Skipped: {NO_SOURCE}{tail}")]
    reason = f"Hand back to ecobee: cancel the controller's hold on the {name.lower()} thermostat."
    row_id = _log_sent(now, unit_key, channel, "resume_program", reason, snap,
                       {"kind": REQUEST_KIND, "unit_key": unit_key})
    result = await _call(lambda: source.resume_program(unit_key, reason[:500]), channel)
    from climate.control.controller import _refused  # local: the controller imports this module

    if _refused(result):
        _log_result(row_id, result, status="skipped")
        return [HandbackStep(unit_key=unit_key, what=what, ok=True,
                             detail="Left alone: the thermostat's hold was not the controller's.")]
    _log_result(row_id, result)
    if result.ok and isinstance(result.request, dict) and result.request.get("noop"):
        return [HandbackStep(unit_key=unit_key, what=what, ok=True, detail="Already on the ecobee schedule.")]
    return [HandbackStep(unit_key=unit_key, what=what, ok=result.ok,
                         detail="Back on the ecobee schedule." if result.ok else _error(result))]


async def _restore_sensors(
    source: ThermostatSource | None, channel: str, unit_key: str, name: str, snap: UnitSnapshot | None,
    original: EcobeeOriginal | None, writes: dict[str, ControlAction | None], now: datetime,
) -> list[HandbackStep]:
    """Step 4: the Home comfort setting's sensors back to the captured set. "Already" only when
    the snapshot shows the captured set and is newer than the controller's latest sensor-set
    write (else the source compares on its own fresh read)."""
    what = SENSORS_RESTORED
    if original is None or original.home_sensors is None:
        return [HandbackStep(unit_key=unit_key, what=what, ok=True,
                             detail="Skipped: no ecobee sensor set was captured for this thermostat.")]
    wanted = list(original.home_sensors)
    if channel == "simulator" or (snap is not None and snap.source == "simulator"):
        return [HandbackStep(unit_key=unit_key, what=what, ok=True,
                             detail=f"Skipped ({', '.join(wanted)}): {SIMULATOR}")]
    current = snap.sensor_sets.get("home") if snap is not None else None
    if current is not None and set(current) == set(wanted) and not _unshown(writes.get("sensors"), snap):
        return [HandbackStep(unit_key=unit_key, what=what, ok=True, detail="Already the captured set.")]
    if source is None:
        return [HandbackStep(unit_key=unit_key, what=what, ok=True, detail=f"Skipped: {NO_SOURCE}")]
    write = getattr(source, "update_sensor_sets", None)
    if not callable(write):
        return [HandbackStep(unit_key=unit_key, what=what, ok=True,
                             detail="Skipped: this thermostat source cannot write sensor sets.")]
    if snap is not None and snap.source == "homekit":
        return [HandbackStep(unit_key=unit_key, what=what, ok=True,
                             detail="Skipped: only HomeKit data, the ecobee cloud is unavailable. Hand back again "
                                    "when the cloud is back.")]
    reason = f"Hand back to ecobee: the {name.lower()} Home comfort setting averages its original sensors again."
    sets = {"home": wanted}
    row_id = _log_sent(now, unit_key, channel, "update_program", reason, snap,
                       {"kind": "sensor_sets", "unit_key": unit_key, "sets": sets},
                       before={"sensor_sets": dict(snap.sensor_sets)} if snap is not None else None)
    result = await _call(lambda: write(unit_key, sets, reason[:500]), channel)
    refused = _in_the_way(result)
    if refused is not None:
        _log_result(row_id, result, status="skipped")
        return [HandbackStep(unit_key=unit_key, what=what, ok=True,
                             detail=f"Skipped ({', '.join(wanted)}): {refused}")]
    _log_result(row_id, result)
    if result.ok and isinstance(result.request, dict) and result.request.get("noop"):
        return [HandbackStep(unit_key=unit_key, what=what, ok=True, detail="Already the captured set.")]
    return [HandbackStep(unit_key=unit_key, what=what, ok=result.ok,
                         detail=", ".join(wanted) if result.ok else _error(result))]


async def _restore_settings(
    source: ThermostatSource | None, channel: str, unit_key: str, name: str, snap: UnitSnapshot | None,
    original: EcobeeOriginal | None, writes: dict[str, ControlAction | None], now: datetime,
) -> list[HandbackStep]:
    """Step 5: Smart Away / Follow Me back to the captured values (only those that differ; all
    of them when the controller changed settings since the snapshot, and the source compares
    on its own fresh read)."""
    what = SETTINGS_RESTORED
    if original is None:
        return [HandbackStep(unit_key=unit_key, what=what, ok=True,
                             detail="Skipped: no ecobee settings were captured for this thermostat.")]
    captured = {k: v for k, v in {"autoAway": original.auto_away, "followMeComfort": original.follow_me}.items()
                if v is not None}
    if channel == "simulator" or (snap is not None and snap.source == "simulator"):
        return [HandbackStep(unit_key=unit_key, what=what, ok=True,
                             detail=f"Skipped ({_label(captured) or 'nothing captured'}): {SIMULATOR}")]
    current = snap.settings if snap is not None else {}
    if _unshown(writes.get("settings"), snap):
        wanted = dict(captured)  # the snapshot predates the controller's latest settings write
    else:
        wanted = {k: v for k, v in captured.items() if _bool(current.get(k)) is not v}
    if not wanted:
        return [HandbackStep(unit_key=unit_key, what=what, ok=True, detail="Already as captured.")]
    label = _label(wanted)
    if source is None:
        return [HandbackStep(unit_key=unit_key, what=what, ok=True, detail=f"Skipped ({label}): {NO_SOURCE}")]
    apply = getattr(source, "apply_settings", None)
    if not callable(apply):
        return [HandbackStep(unit_key=unit_key, what=what, ok=True,
                             detail=f"Skipped: this thermostat source cannot write settings ({label}).")]
    if snap is not None and snap.source == "homekit":
        return [HandbackStep(unit_key=unit_key, what=what, ok=True,
                             detail=f"Skipped ({label}): only HomeKit data, the ecobee cloud is unavailable. Hand "
                                    "back again when the cloud is back.")]
    reason = f"Hand back to ecobee: {label} on the {name.lower()} thermostat, as before the controller."
    row_id = _log_sent(now, unit_key, channel, "update_settings", reason, snap,
                       {"kind": REQUEST_KIND, "unit_key": unit_key, "settings": wanted},
                       before={k: current.get(k) for k in SETTING_NAMES})
    result = await _call(lambda: apply(unit_key, wanted, reason[:500]), channel)
    _log_result(row_id, result)
    if result.ok and isinstance(result.request, dict) and result.request.get("noop"):
        return [HandbackStep(unit_key=unit_key, what=what, ok=True, detail="Already as captured.")]
    return [HandbackStep(unit_key=unit_key, what=what, ok=result.ok, detail=label if result.ok else _error(result))]


def _label(settings: dict[str, bool]) -> str:
    """'Smart Away on, Follow Me off'."""
    return ", ".join(f"{SETTING_NAMES[k]} {'on' if v else 'off'}" for k, v in settings.items())


def _in_the_way(result: WriteResult) -> str | None:
    """Why the source refused to change the sensor set (nothing written: a person's hold or an
    event runs on the thermostat), as the rest of a skipped step's detail; else None."""
    req = result.request if isinstance(result.request, dict) else {}
    if result.ok or not req.get("refused"):
        return None
    what = "an ecobee event" if req.get("event") else "a person's hold"
    return (f"{what} is running on this thermostat and the source does not change its sensors under it. Hand back "
            "again when it has ended.")


# ---------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------


def _log_sent(
    now: datetime, unit_key: str, channel: str, action: str, reason: str, snap: UnitSnapshot | None,
    request: dict[str, Any], before: dict[str, Any] | None = None,
) -> int:
    """Log the write as 'sent' and commit it before the source is called."""
    if before is None and snap is not None:
        before = {"heat_f": snap.heat_sp_f, "cool_f": snap.cool_sp_f, "hvac_mode": snap.hvac_mode,
                  "hold": snap.hold.model_dump(mode="json") if snap.hold is not None else None}
    with session_scope() as s:
        row = ControlAction(ts=now, unit_key=unit_key, actor="owner", mode="act", channel=channel, action=action,
                            status="sent", rule=RULE, reason=reason, before=before, request=request)
        s.add(row)
        s.flush()
        publish(s, "action", row.id)
        return row.id


def _log_result(row_id: int, result: WriteResult, status: str | None = None) -> None:
    """Record the source's answer: verified / failed (or ``status``) with the read-back."""
    with session_scope() as s:
        row = s.get(ControlAction, row_id)
        if row is None:
            return
        row.status = status or ("verified" if result.ok else "failed")
        row.readback = dict(result.readback) if result.readback is not None else None
        row.readback_ok = bool(result.ok) if result.readback is not None else (None if result.ok else False)
        if result.before:
            row.before = {**(row.before or {}), "source": dict(result.before)}
        row.error = None if result.ok else (result.error or "The read-back did not match the request.")
        row.completed_at = utcnow()
        publish(s, "action", row.id)


async def _call(fn: Any, channel: str) -> WriteResult:
    try:
        return await fn()
    except Exception as exc:  # noqa: BLE001 - the error is the record
        log.warning("hand back write failed: %s", type(exc).__name__)
        return WriteResult(ok=False, channel=channel if channel in ("ecobee", "simulator") else "none",  # type: ignore[arg-type]
                           error=f"{type(exc).__name__}: {exc}"[:1000])


def pending_steps(steps: Iterable[HandbackStep]) -> list[HandbackStep]:
    """The restores that did not run and still need doing (a skipped sensor-set or Smart Away /
    Follow Me restore: no source, the simulator, only HomeKit data, a person's hold or an
    event in the way; not one with nothing captured). Hand back again to finish them."""
    return [st for st in steps if st.ok and st.what in PENDING_NAMES and st.detail.startswith("Skipped")
            and not st.detail.startswith(NOTHING_CAPTURED)]


def _finish(steps: list[HandbackStep]) -> None:
    """The closing alert. "Handed back to ecobee" (info, pushed) when everything is back; else
    "Hand-back incomplete" (warn) listing what failed and what is not done yet (skipped
    restores still to do: hand back again). Best effort."""
    failed = [st for st in steps if not st.ok]
    pending = pending_steps(steps)
    done = [st for st in steps if st.ok and st.what != READ and not st.detail.startswith(("Skipped", "Already", "Left"))]
    read = [st for st in steps if st.ok and st.what == READ]
    lines = []
    if failed:
        lines.append("Failed: " + "; ".join(f"{_where(st)}{st.what} ({st.detail})" for st in failed) + ".")
    if pending:
        lines.append("Not done yet: " + "; ".join(f"{_where(st)}{PENDING_NAMES[st.what]} ({_why(st.detail)})"
                                                    for st in pending) + ". Hand back again to finish.")
    if done:
        lines.append("Done: " + "; ".join(f"{_where(st)}{st.what}" for st in done) + ".")
    if read:
        lines.append(read[0].detail)
    lines.append("The controller is off; person holds, vacations and utility events were left alone.")
    incomplete = bool(failed or pending)
    try:
        with session_scope() as s:
            raise_alert(s, kind="handback", level="warn" if incomplete else "info",
                        title="Hand-back incomplete" if incomplete else "Handed back to ecobee",
                        body=" ".join(lines), push_info=True)
    except Exception:  # the steps are the record; the alert is a courtesy
        log.exception("hand back: could not raise the closing alert")


def _why(detail: str) -> str:
    """A skipped step's reason without the "Skipped" word: 'no thermostat source is running'."""
    rest = detail[len("Skipped"):].lstrip()
    if rest.startswith("("):  # "(Smart Away on): ..." keeps what it would have restored
        label, _, rest = rest[1:].partition("):")
        return f"{label}: {rest.strip().rstrip('.')}"
    return rest.lstrip(":").strip().rstrip(".")


def _clock(ts: datetime, tz: str) -> str:
    local = to_local(ts, tz)
    return f"{local.hour % 12 or 12}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"


def _where(st: HandbackStep) -> str:
    return f"{st.unit_key}: " if st.unit_key else ""


def _error(result: WriteResult) -> str:
    return result.error or "The read-back did not match the request."


def _control(s: Session) -> ControlSettings:
    try:
        return get_setting(s, "control", ControlSettings)
    except ValidationError:
        return ControlSettings()


def _bool(v: object) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        low = v.strip().lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off"):
            return False
    return None
