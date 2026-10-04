"""The change gates (blueprint §4): backtest -> shadow -> sign-off -> trial -> active.

proposed_by 'model' -> Claude may approve/hold if every changed key is inside
CLAUDE_SIGNOFF_RANGES; otherwise, and for anything Claude proposes, the owner decides.
The owner can approve anything that passes the hard-limit validation.

Every gate writes its result into ``changes.gates``::

    {"validation": [violation sentences],            # empty = passed
     "backtest": {BacktestOut fields, "at": iso} | {"error": str, "at": iso},
     "shadow": {"start": iso, "days": n, "end": iso},
     "decision": {"by", "decision", "reason", "at", "from_status", "skipped_gates": [...]},
     "trial": {"start", "end", "days", "window", "policy_version_id", "result": {...}}}

During a trial the candidate policy (a ``policy_versions`` row with status 'trial') drives the
controller only inside the trial window, afternoons 12:00-20:00 local
(``policy_for``); the active policy runs the rest of the day. One trial at a time.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from climate.control.guardrails import validate_policy_params, within_signoff_ranges
from climate.control.policy import PolicyParams
from climate.events import publish
from climate.house import ROOM_BY_KEY
from climate.store.app_settings import DEFAULT_COMFORT, ControlSettings, LocationSettings, get_setting
from climate.store.orm import Change, PolicyVersion, RoomStateRow
from climate.timeutil import floor_slot, in_window, to_local, utcnow

log = logging.getLogger(__name__)

SHADOW_DAYS = 3
TRIAL_DAYS = 7
TRIAL_WINDOW = ("12:00", "20:00")  # local; the candidate acts only inside this window
BACKTEST_DAYS = 28
COMFORT_TARGET_PCT = 97.0  # occupied rooms in band >= 97% of minutes (blueprint §2 objective)
REGRESSION_MARGIN_PCT = 1.0  # a trial must not be worse than the days before it by more than this
COMFORT_TOLERANCE_F = 1.0  # occupied rooms may sit up to 1°F outside the band


class ChangeNotFound(LookupError, ValueError):
    """No change with that id (a ValueError too, so callers that only map ValueError still work)."""


# ---------------------------------------------------------------------------------------
# policy versions
# ---------------------------------------------------------------------------------------


def active_policy(session: Session) -> tuple[PolicyParams, int | None]:
    """Params and id of the active policy_versions row (defaults, None if none)."""
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
        log.warning("active policy_versions row %s does not parse; using the defaults", row.id)
        return PolicyParams(), row.id


def trial_policy(session: Session, now: datetime, tz: str) -> tuple[PolicyParams, int, int] | None:
    """(params, policy_version_id, change_id) of the change on trial, when ``now`` is inside
    its trial period and the daily trial window; else None."""
    if not in_window(to_local(now, tz), *TRIAL_WINDOW):
        return None
    change = session.execute(
        select(Change)
        .where(Change.status == "trial", Change.trial_start <= now)
        .order_by(Change.trial_start.desc(), Change.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if change is None or change.policy_version_id is None:
        return None
    if change.trial_end is not None and change.trial_end <= now:
        return None
    pv = session.get(PolicyVersion, change.policy_version_id)
    if pv is None or pv.status != "trial":
        return None
    try:
        return PolicyParams.model_validate(pv.params), pv.id, change.id
    except ValidationError:
        log.warning("trial policy_versions row %s does not parse; staying on the active policy", pv.id)
        return None


def policy_for(session: Session, now: datetime, tz: str) -> tuple[PolicyParams, int | None]:
    """The policy the controller runs right now: the trial candidate inside its window, else
    the active version."""
    trial = trial_policy(session, now, tz)
    if trial is not None:
        return trial[0], trial[1]
    return active_policy(session)


# ---------------------------------------------------------------------------------------
# propose / decide
# ---------------------------------------------------------------------------------------


def propose_policy_change(
    session: Session, proposed_by: Literal["model", "claude", "owner"], title: str, rationale: str, params: dict,
) -> Change:
    """Validate (guardrails.validate_policy_params with actor), insert a 'policy' change in
    status 'backtest' (or 'rejected' with gates.validation errors), publish 'change'.

    Also rejected: an empty proposal, and one whose every value already equals the active
    policy (it would change nothing)."""
    if proposed_by not in ("model", "claude", "owner"):
        raise ValueError(f"Unknown proposer {proposed_by!r}.")
    active, active_id = active_policy(session)
    violations = validate_policy_params(params, actor=proposed_by)
    if isinstance(params, dict) and not params:
        violations.append("Name at least one policy parameter to change.")
    current = active.model_dump()
    if not violations and all(current.get(k) == v for k, v in params.items()):
        violations.append("Every value already matches the active policy, so nothing would change.")
    now = utcnow()
    status = "rejected" if violations else "backtest"
    safe_params = params if isinstance(params, dict) else {}
    change = Change(
        kind="policy",
        title=title.strip()[:200] or "Policy change",
        rationale=rationale or "",
        payload={
            "params": safe_params,
            "base_policy_version_id": active_id,
            "current": {k: current[k] for k in safe_params if k in current},
        },
        proposed_by=proposed_by,
        status=status,
        gates={"validation": violations, "validated_at": now.isoformat()},
        decision_reason=("Failed validation: " + " ".join(violations)) if violations else None,
        updated_at=now,
    )
    session.add(change)
    session.flush()
    publish(session, "change", change.id)
    return change


def needs(change: Change) -> Literal["nothing", "claude", "owner"]:
    """Who must decide next. Claude only for a model-proposed change awaiting sign-off whose
    every parameter is inside CLAUDE_SIGNOFF_RANGES; held changes wait for the owner."""
    if change.status == "awaiting_signoff":
        if change.proposed_by == "model" and within_signoff_ranges(_params(change)):
            return "claude"
        return "owner"
    if change.status == "held":
        return "owner"
    return "nothing"


def decide(
    session: Session, change_id: int, actor: Literal["claude", "owner"],
    decision: Literal["approve", "hold", "reject"], reason: str,
) -> Change:
    """Apply a decision. Raises PermissionError when the actor may not decide this change,
    ValueError when the change is not awaiting a decision. Approve -> 'trial' (creates a
    policy_versions row status 'trial') ; owner may approve from 'backtest'/'shadow' too
    (skipping remaining gates is recorded in gates).

    Claude may approve or hold only model-proposed changes whose every key is inside
    CLAUDE_SIGNOFF_RANGES, from 'awaiting_signoff' or a hold of its own (never one the owner
    placed); it may never decide changes
    Claude or the owner proposed, and may not reject (holding with a reason is its veto).
    The owner may decide anything that passed validation, from 'backtest', 'shadow',
    'awaiting_signoff' or 'held'."""
    change = session.execute(select(Change).where(Change.id == change_id).with_for_update()).scalar_one_or_none()
    if change is None:
        raise ChangeNotFound(f"There is no change {change_id}.")
    if decision not in ("approve", "hold", "reject"):
        raise ValueError(f"Unknown decision {decision!r}.")
    params = _params(change)

    if actor == "claude":
        if change.proposed_by != "model":
            raise PermissionError(
                f"Claude may not decide a change proposed by {'Claude' if change.proposed_by == 'claude' else 'the owner'}; "
                "the owner decides it."
            )
        if not within_signoff_ranges(params):
            raise PermissionError(
                "This model change sets parameters outside Claude's sign-off ranges, so only the owner can decide it."
            )
        if decision == "reject":
            raise PermissionError(
                "Claude can approve or hold a model change, but only the owner can reject one; hold it with the reason."
            )
        if change.status == "held" and change.decided_by != "claude":
            raise PermissionError("The owner put this change on hold, so only the owner can decide it now.")
        allowed = {"awaiting_signoff", "held"}
    elif actor == "owner":
        allowed = {"backtest", "shadow", "awaiting_signoff", "held"}
    else:
        raise PermissionError(f"{actor!r} may not decide changes.")

    if change.status not in allowed:
        if change.status in ("backtest", "shadow"):
            raise ValueError(f"Change {change.id} is still in its {change.status} gate, not awaiting sign-off yet.")
        raise ValueError(f"Change {change.id} is {change.status}, not awaiting a decision.")
    if (change.gates or {}).get("validation"):
        raise ValueError(f"Change {change.id} failed validation and cannot be decided.")

    now = utcnow()
    from_status = change.status
    record: dict[str, Any] = {
        "by": actor, "decision": decision, "reason": reason, "at": now.isoformat(), "from_status": from_status,
    }
    gates = dict(change.gates or {})

    if decision == "approve":
        other = session.execute(
            select(Change.id).where(Change.status == "trial", Change.id != change.id).limit(1)
        ).scalar_one_or_none()
        if other is not None:
            raise ValueError(f"Change {other} is already in its trial; approve this one after that trial ends.")
        active, _ = active_policy(session)
        try:
            merged = PolicyParams.model_validate({**active.model_dump(), **params})
        except ValidationError as exc:
            raise ValueError(f"The change no longer applies to the active policy: {exc.errors()[0]['msg']}.") from exc
        skipped = {"backtest": ["backtest", "shadow"], "shadow": ["shadow"]}.get(from_status, [])
        record["skipped_gates"] = skipped
        pv = PolicyVersion(
            created_by=change.proposed_by,
            params=merged.model_dump(mode="json"),
            status="trial",
            change_id=change.id,
            note=f"Trial of change {change.id}: {change.title}"[:500],
        )
        session.add(pv)
        session.flush()
        change.status = "trial"
        change.trial_start = now
        change.trial_end = now + timedelta(days=TRIAL_DAYS)
        change.policy_version_id = pv.id
        gates["trial"] = {
            "start": now.isoformat(), "end": change.trial_end.isoformat(), "days": TRIAL_DAYS,
            "window": f"{TRIAL_WINDOW[0]}-{TRIAL_WINDOW[1]} local", "policy_version_id": pv.id,
        }
        if skipped:
            gates.setdefault("skipped", skipped)
    elif decision == "hold":
        change.status = "held"
    else:
        change.status = "rejected"

    gates["decision"] = record
    change.gates = gates
    change.decided_by = actor
    change.decided_at = now
    change.decision_reason = reason
    change.updated_at = now
    session.flush()
    publish(session, "change", change.id)
    return change


# ---------------------------------------------------------------------------------------
# the gate machine
# ---------------------------------------------------------------------------------------


def advance(session: Session, now: datetime) -> list[int]:
    """Move changes through the gates: run the backtest (climate.models.backtest.backtest)
    for 'backtest' -> 'shadow' (or 'rejected' if it does not beat model uncertainty);
    after SHADOW_DAYS -> 'awaiting_signoff' (+ enqueue nothing; Claude sees it nightly);
    'trial' for TRIAL_DAYS with no comfort regressions -> 'active' (retire the old
    policy version). Returns ids that changed.

    A backtest that errors leaves the change in 'backtest' (error recorded) to retry next
    pass. A trial with a comfort regression is rejected and its policy version retired."""
    changed: list[int] = []
    for change in _by_status(session, "backtest"):
        if _run_backtest(session, change, now):
            changed.append(change.id)
    for change in _by_status(session, "shadow"):
        start = change.shadow_start or change.updated_at
        if start is not None and now >= start + timedelta(days=SHADOW_DAYS):
            gates = dict(change.gates or {})
            shadow = dict(gates.get("shadow") or {})
            shadow.update({"end": now.isoformat(), "days": SHADOW_DAYS})
            gates["shadow"] = shadow
            change.gates = gates
            change.status = "awaiting_signoff"
            change.updated_at = now
            changed.append(change.id)
    for change in _by_status(session, "trial"):
        if change.trial_end is not None and now >= change.trial_end:
            _finish_trial(session, change, now)
            changed.append(change.id)
    session.flush()
    for change_id in changed:
        publish(session, "change", change_id)
    return changed


def _run_backtest(session: Session, change: Change, now: datetime) -> bool:
    from climate.models import backtest as backtest_mod

    gates = dict(change.gates or {})
    try:
        with session.begin_nested():
            out = backtest_mod.backtest(session, _params(change), days=BACKTEST_DAYS)
    except Exception as exc:  # noqa: BLE001 - a failing model must not stall the other gates
        log.warning("backtest for change %s failed: %s", change.id, exc)
        gates["backtest"] = {"error": f"{type(exc).__name__}: {exc}"[:500], "at": now.isoformat()}
        change.gates = gates
        change.updated_at = now
        return False
    result = out.model_dump(mode="json")
    result["at"] = now.isoformat()
    gates["backtest"] = result
    change.gates = gates
    change.updated_at = now
    if out.beats_model_uncertainty:
        change.status = "shadow"
        change.shadow_start = now
        gates["shadow"] = {"start": now.isoformat(), "days": SHADOW_DAYS}
        change.gates = gates
    else:
        change.status = "rejected"
        change.decision_reason = (
            "The backtest did not show an effect larger than the model's uncertainty"
            + (f": {out.note}" if out.note else ".")
        )
    return True


def _finish_trial(session: Session, change: Change, now: datetime) -> None:
    tz = _house_tz(session)
    control = get_setting(session, "control", ControlSettings)
    start = change.trial_start or (change.trial_end - timedelta(days=TRIAL_DAYS))  # type: ignore[operator]
    end = change.trial_end or now
    trial = comfort_in_windows(session, start, end, tz, control)
    before = comfort_in_windows(session, start - (end - start), start, tz, control)
    regression, verdict = _judge(trial, before)
    gates = dict(change.gates or {})
    trial_gate = dict(gates.get("trial") or {})
    trial_gate["result"] = {"trial": trial, "before": before, "regression": regression, "verdict": verdict,
                            "at": now.isoformat()}
    gates["trial"] = trial_gate
    change.gates = gates
    change.updated_at = now
    pv = session.get(PolicyVersion, change.policy_version_id) if change.policy_version_id else None
    if regression:
        change.status = "rejected"
        change.decision_reason = f"Trial ended with a comfort regression: {verdict}"
        if pv is not None:
            pv.status = "retired"
        return
    session.execute(
        update(PolicyVersion).where(PolicyVersion.status == "active").values(status="retired")
    )
    if pv is not None:
        pv.status = "active"
    change.status = "active"


def _judge(trial: dict[str, Any], before: dict[str, Any]) -> tuple[bool, str]:
    t, b = trial.get("in_band_pct"), before.get("in_band_pct")
    if t is None:
        return False, "No occupied minutes with temperatures inside the trial windows, so no regression could be seen."
    if t >= COMFORT_TARGET_PCT:
        return False, f"Occupied rooms were in band {t:.1f}% of trial minutes (target {COMFORT_TARGET_PCT:g}%)."
    if b is not None and t >= b - REGRESSION_MARGIN_PCT:
        return False, (
            f"Occupied rooms were in band {t:.1f}% of trial minutes, below the {COMFORT_TARGET_PCT:g}% target but no "
            f"worse than the {b:.1f}% before the trial."
        )
    tail = f" (vs {b:.1f}% before the trial)" if b is not None else ""
    return True, f"occupied rooms were in band only {t:.1f}% of trial minutes{tail}; the target is {COMFORT_TARGET_PCT:g}%."


def comfort_in_windows(
    session: Session, start: datetime, end: datetime, tz: str, control: ControlSettings,
) -> dict[str, Any]:
    """Share of occupied (or asleep) room-minutes inside the day comfort band (+/-1°F) within
    the daily trial window, for sensored comfort rooms over [start, end). Occupancy comes
    from room_states; when none were recorded in the period, from readings_5m.occupied."""
    temps = session.execute(
        text(
            """
            SELECT r.ts, s.room_key, avg(r.temp_f) AS temp_f, bool_or(r.occupied) AS occupied
            FROM readings_5m r JOIN sensors s ON s.key = r.sensor_key
            WHERE r.ts >= :start AND r.ts < :end AND r.temp_f IS NOT NULL
            GROUP BY r.ts, s.room_key
            """
        ),
        {"start": start, "end": end},
    ).all()
    states = session.execute(
        select(RoomStateRow.ts, RoomStateRow.room_key, RoomStateRow.state)
        .where(RoomStateRow.ts >= start, RoomStateRow.ts < end)
    ).all()
    occupied_slots: set[tuple[str, datetime]] | None = None
    if states:
        occupied_slots = {(rk, floor_slot(ts)) for ts, rk, st in states if st in ("occupied", "asleep")}

    total = inside = 0
    for ts, room_key, temp_f, occ in temps:
        room = ROOM_BY_KEY.get(room_key)
        if room is None or not room.has_sensor or not room.has_comfort_target:
            continue
        if not in_window(to_local(ts, tz), *TRIAL_WINDOW):
            continue
        is_occ = (room_key, floor_slot(ts)) in occupied_slots if occupied_slots is not None else bool(occ)
        if not is_occ:
            continue
        comfort = control.comfort.get(room.unit_key) or DEFAULT_COMFORT[room.unit_key]
        band = comfort.day
        total += 1
        if band.heat_f - COMFORT_TOLERANCE_F <= float(temp_f) <= band.cool_f + COMFORT_TOLERANCE_F:
            inside += 1
    return {
        "occupied_minutes": total * 5,
        "in_band_pct": round(100.0 * inside / total, 2) if total else None,
        "start": start.isoformat(),
        "end": end.isoformat(),
    }


def _house_tz(session: Session) -> str:
    tz = get_setting(session, "location", LocationSettings).tz
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        return "UTC"
    return tz


def _by_status(session: Session, status: str) -> list[Change]:
    return list(
        session.execute(
            select(Change).where(Change.status == status, Change.kind == "policy").order_by(Change.id)
        ).scalars()
    )


def _params(change: Change) -> dict[str, Any]:
    payload = change.payload or {}
    params = payload.get("params") if isinstance(payload, dict) else None
    return params if isinstance(params, dict) else {}
