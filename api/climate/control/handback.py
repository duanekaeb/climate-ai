"""Hand back to ecobee: put the thermostats back the way they were before the controller.

``capture_original`` records, once per unit, the ecobee settings the controller may change
(Smart Away, Follow Me, the Home comfort setting's sensors) in
``app_settings['ecobee_original']`` = ``{unit_key: EcobeeOriginal}``. The controller calls it
every tick and just before its own first settings or sensor-set write; an entry is never
overwritten once captured, so it keeps the values from before the controller's first change.

``hand_back`` (the worker's ``handback`` job, ``POST /api/control/handback``) then, in order:
1. switches the controller off (and commits that first), so nothing writes again and the
   worker's ``keep_settings`` does not switch Smart Away off again;
2. resumes each unit's schedule where the running hold is the controller's (never forced:
   a person's hold is left alone);
3. restores each unit's Home sensor set to the captured one, when it differs;
4. restores Smart Away / Follow Me to the captured values, where they differ.
Person holds, vacations and utility events are left alone. Every write is logged to
``control_actions`` (actor 'owner', rule 'handback') with before / request / read-back, logged
'sent' before the source is called. A step that cannot run (no thermostat source, a source
that cannot write that setting, data only from HomeKit, nothing captured) is reported as
skipped, never failed silently. The result is a list of ``HandbackStep``-shaped dicts, and an
info alert (pushed) says what was done and lists anything that failed.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from climate.api.schemas import HandbackStep
from climate.events import publish
from climate.house import UNIT_KEYS
from climate.notify import raise_alert
from climate.sources.base import ThermostatSource, UnitSnapshot, WriteResult
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
NO_SOURCE = "no thermostat source is running."


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
            publish(s, "status")
        steps.append(HandbackStep(what=CONTROLLER_OFF, ok=True,
                                  detail=f"Was {was}; it writes nothing until you switch it back on."))
    except Exception as exc:  # without this first step nothing else may run
        log.exception("hand back: could not switch the controller off")
        steps.append(HandbackStep(what=CONTROLLER_OFF, ok=False, detail=f"{type(exc).__name__}: {exc}"[:500]))
        _finish(steps)
        return [st.model_dump() for st in steps]

    try:
        with session_scope() as s:
            state = load_house_state(s, now)
            originals = load_originals(s)
    except Exception as exc:  # the controller is off already; nothing else can be judged
        log.exception("hand back: could not read the house state")
        steps.append(HandbackStep(what="Read the thermostats", ok=False, detail=f"{type(exc).__name__}: {exc}"[:500]))
        _finish(steps)
        return [st.model_dump() for st in steps]
    kind = getattr(source, "kind", None) if source is not None else None
    channel = kind if kind in ("ecobee", "simulator") else "none"

    for unit_key in UNIT_KEYS:
        unit = state.units.get(unit_key)
        snap = unit.snapshot if unit is not None else None
        name = unit.name if unit is not None else unit_key
        original = originals.get(unit_key)
        for what in (RESUMED, SENSORS_RESTORED, SETTINGS_RESTORED):
            try:
                if what == RESUMED:
                    steps += await _resume_ours(source, channel, unit_key, name, snap, state.tz, now)
                elif what == SENSORS_RESTORED:
                    steps += await _restore_sensors(source, channel, unit_key, name, snap, original, now)
                else:
                    steps += await _restore_settings(source, channel, unit_key, name, snap, original, now)
            except Exception as exc:  # report the step, carry on with the others
                log.exception("hand back: %r failed for %s", what, unit_key)
                steps.append(HandbackStep(unit_key=unit_key, what=what, ok=False,
                                          detail=f"{type(exc).__name__}: {exc}"[:500]))
    _finish(steps)
    return [st.model_dump() for st in steps]


async def _resume_ours(
    source: ThermostatSource | None, channel: str, unit_key: str, name: str, snap: UnitSnapshot | None, tz: str,
    now: datetime,
) -> list[HandbackStep]:
    """Step 2: cancel the controller's own running hold (not forced)."""
    hold = snap.hold if snap is not None else None
    if hold is None or not hold.set_by_us:
        return []  # no hold of ours (a person's hold, an event or the schedule): left alone
    what = RESUMED
    ends = f" Our hold ends on its own at {_clock(hold.end, tz)}." if hold.end is not None else ""
    if snap is not None and snap.source == "homekit":
        return [HandbackStep(unit_key=unit_key, what=what, ok=True,
                             detail=f"Skipped: only HomeKit data, the ecobee cloud is unavailable.{ends}")]
    if source is None:
        return [HandbackStep(unit_key=unit_key, what=what, ok=True, detail=f"Skipped: {NO_SOURCE}{ends}")]
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
    return [HandbackStep(unit_key=unit_key, what=what, ok=result.ok,
                         detail="Back on the ecobee schedule." if result.ok else _error(result))]


async def _restore_sensors(
    source: ThermostatSource | None, channel: str, unit_key: str, name: str, snap: UnitSnapshot | None,
    original: EcobeeOriginal | None, now: datetime,
) -> list[HandbackStep]:
    """Step 3: the Home comfort setting's sensors back to the captured set."""
    what = SENSORS_RESTORED
    if original is None or original.home_sensors is None:
        return [HandbackStep(unit_key=unit_key, what=what, ok=True,
                             detail="Skipped: no ecobee sensor set was captured for this thermostat.")]
    wanted = list(original.home_sensors)
    current = snap.sensor_sets.get("home") if snap is not None else None
    if current is not None and set(current) == set(wanted):
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
    _log_result(row_id, result)
    return [HandbackStep(unit_key=unit_key, what=what, ok=result.ok,
                         detail=", ".join(wanted) if result.ok else _error(result))]


async def _restore_settings(
    source: ThermostatSource | None, channel: str, unit_key: str, name: str, snap: UnitSnapshot | None,
    original: EcobeeOriginal | None, now: datetime,
) -> list[HandbackStep]:
    """Step 4: Smart Away / Follow Me back to the captured values (only those that differ)."""
    what = SETTINGS_RESTORED
    if original is None:
        return [HandbackStep(unit_key=unit_key, what=what, ok=True,
                             detail="Skipped: no ecobee settings were captured for this thermostat.")]
    captured = {"autoAway": original.auto_away, "followMeComfort": original.follow_me}
    current = snap.settings if snap is not None else {}
    wanted = {k: v for k, v in captured.items() if v is not None and _bool(current.get(k)) is not v}
    if not wanted:
        return [HandbackStep(unit_key=unit_key, what=what, ok=True, detail="Already as captured.")]
    label = ", ".join(f"{SETTING_NAMES[k]} {'on' if v else 'off'}" for k, v in wanted.items())
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
    return [HandbackStep(unit_key=unit_key, what=what, ok=result.ok, detail=label if result.ok else _error(result))]


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


def _finish(steps: list[HandbackStep]) -> None:
    """The closing alert: what was handed back and anything that failed. Best effort."""
    failed = [st for st in steps if not st.ok]
    done = [st for st in steps if st.ok and not st.detail.startswith(("Skipped", "Already", "Left alone"))]
    lines = []
    if failed:
        lines.append("Failed: " + "; ".join(f"{_where(st)}{st.what} ({st.detail})" for st in failed) + ".")
    if done:
        lines.append("Done: " + "; ".join(f"{_where(st)}{st.what}" for st in done) + ".")
    lines.append("The controller is off; person holds, vacations and utility events were left alone.")
    try:
        with session_scope() as s:
            raise_alert(s, kind="handback", level="info", title="Handed back to ecobee", body=" ".join(lines),
                        push_info=True)
    except Exception:  # the steps are the record; the alert is a courtesy
        log.exception("hand back: could not raise the closing alert")


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
