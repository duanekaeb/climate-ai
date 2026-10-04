"""``utility_events`` rows: identity, loading for the house state, ingest from snapshots,
and the clock sweep (see docs/specs/holds-and-utility-events.md, "Utility events").

``event_key`` and ``load_active`` are the shared contract (state.py calls ``load_active``);
the ingest/sweep functions below them belong to the utility-events builder.

Lifecycle of a row (one per thermostat and event):

- ``ingest`` (every live ecobee / simulator poll, never HomeKit, which shows no events):
  a listed ``demandResponse`` event that runs is 'running' (``started_at`` = first seen
  running); one not running yet is 'announced'. A row the unit's snapshot no longer lists is
  over: a running one 'opted_out' when our skip went through, else 'ended'; an announced one
  'cancelled' while its start is still ahead (or unknown), else 'ended'. ``ended_at`` is the
  snapshot time. An opt-out is final; an event listed again after it ended or was cancelled
  (ecobee extended or re-listed it) is open again.
- ``sweep`` (every worker loop): the clock closes what no snapshot did: 'running' 10 minutes
  past its end, 'announced' past its end. An announced event whose start has passed stays
  announced until a snapshot or its end decides.
- ``climate.utility.skips``: the opt-out ('opted_out', ``skip`` 'done').

``detail`` holds the last ThermostatEvent seen, plus the app's own keys (``skip_attempts``,
``rule_requested_at``, written by ``skips``), which a snapshot update keeps.

Alerts (kind 'utility_event', dedupe ``utility_event:<event_key>:<phase>``) are grouped per
event: one alert names every thermostat with it, in house time. Each phase alerts at most once
per event (a resolved one is never raised again; an open one has its text kept current when
another thermostat joins). Phases: 'announced' (info, pushed; resolved when the event starts or
is over), 'running' (info, pushed; resolved when it is over), 'ended' (info, pushed) and
'cancelled' (info), both resolved by the sweep 12 hours after the event is over, like the skip
outcomes ``skips`` raises. Only raised while ``UtilityEventSettings.alerts`` is on; resolving
always happens. The 'unknown_event:<type>' warning (an ecobee event type the app does not
know is running) is not a utility-event alert and does not depend on that switch.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from climate import notify
from climate.control.policy import UtilityEventState
from climate.events import publish
from climate.house import UNIT_KEYS, UNITS
from climate.sources.base import KNOWN_HOLD_TYPES, ThermostatEvent, UnitSnapshot
from climate.store.app_settings import LocationSettings, UtilityEventSettings, get_setting
from climate.store.orm import Alert, LiveUnit, UtilityEvent
from climate.timeutil import to_local

log = logging.getLogger(__name__)

# Rows still worth carrying in the house state: open ones, plus ones that ended this recently
# (the Live card keeps showing "ended at 6:02 PM" / "skipped" for a while).
RECENT = timedelta(hours=2)


def event_key(ev: ThermostatEvent) -> str:
    """Stable identity of an event across polls and thermostats: ecobee's linkRef when it
    has one, else type + name + start (UTC, to the minute)."""
    if ev.link_ref:
        return f"link:{ev.link_ref}"
    start = ev.start.strftime("%Y-%m-%dT%H:%MZ") if ev.start is not None else ""
    return f"{ev.event_type}:{ev.name or ''}:{start}"


def to_state(row: UtilityEvent) -> UtilityEventState:
    return UtilityEventState(
        id=row.id, unit_key=row.unit_key, event_key=row.event_key, name=row.name, status=row.status,  # type: ignore[arg-type]
        start_at=row.start_at, end_at=row.end_at, heat_f=row.heat_f, cool_f=row.cool_f,
        is_relative=bool(row.is_relative), heat_offset_f=row.heat_offset_f, cool_offset_f=row.cool_offset_f,
        is_optional=row.is_optional, skip=row.skip, skip_by=row.skip_by,  # type: ignore[arg-type]
        is_cool_off=bool((row.detail or {}).get("is_cool_off")), is_heat_off=bool((row.detail or {}).get("is_heat_off")),
    )


def load_active(session: Session, now: datetime) -> list[UtilityEventState]:
    """Announced and running events, plus events over within the last 2 hours, oldest start
    first. Never raises on bad rows (the house state must always load)."""
    rows = session.execute(
        select(UtilityEvent)
        .where(
            or_(
                UtilityEvent.status.in_(("announced", "running")),
                UtilityEvent.ended_at >= now - RECENT,
            )
        )
        .order_by(UtilityEvent.start_at.asc().nulls_last(), UtilityEvent.id)
    ).scalars()
    out: list[UtilityEventState] = []
    for row in rows:
        try:
            out.append(to_state(row))
        except Exception:  # noqa: BLE001 - one odd row never breaks the house state
            continue
    return out


def change_label(
    *, heat_f: float | None = None, cool_f: float | None = None, is_relative: bool = False,
    heat_offset_f: float | None = None, cool_offset_f: float | None = None, is_cool_off: bool = False,
    is_heat_off: bool = False, duty_cycle_pct: int | None = None,
) -> str:
    """What the event does to the thermostat, in a few words: "cooling +2°F", "cooling set
    to 78°F", "AC off", "heating −2°F", "runtime capped at 50%". Shared by alerts and the API."""
    parts: list[str] = []
    if is_cool_off:
        parts.append("AC off")
    elif is_relative and cool_offset_f:
        parts.append(f"cooling {cool_offset_f:+g}°F")
    elif not is_relative and cool_f is not None:
        parts.append(f"cooling set to {cool_f:g}°F")
    if is_heat_off:
        parts.append("heat off")
    elif is_relative and heat_offset_f:
        parts.append(f"heating {heat_offset_f:+g}°F".replace("-", "−"))
    elif not is_relative and heat_f is not None:
        parts.append(f"heating set to {heat_f:g}°F")
    if duty_cycle_pct is not None and 0 <= duty_cycle_pct < 100:
        parts.append(f"runtime capped at {duty_cycle_pct}%")
    return ", ".join(parts) or "setpoint change not reported"


# ---------------------------------------------------------------------------------------
# ingest, sweep and alerts (the utility-events builder)
# ---------------------------------------------------------------------------------------

DR = "demandResponse"
OPEN = ("announced", "running")
OVER = ("ended", "cancelled", "opted_out")
RUNNING_GRACE = timedelta(minutes=10)  # the sweep closes a running event this long past its end
ALERT_KEEP = timedelta(hours=12)  # news about an event that is over stays open this long
ALERT_KIND = "utility_event"
ALERT_PREFIX = "utility_event:"
UNKNOWN_KIND = "unknown_event"
# Phases that describe the event as it stands (resolved as soon as it is over everywhere) and
# phases that are news about it (resolved ALERT_KEEP after that). ``skips`` raises the skip
# ones and 'rule:<unit>' (transient too).
TRANSIENT_PHASES = ("announced", "running", "skip_waiting_off", "skip_waiting_cloud")
NEWS_PHASES = ("ended", "cancelled", "skipped", "skip_refused", "skip_failed")
_RULE_PHASE = re.compile(r"^(?P<key>.+):(?P<phase>rule:[a-z_]+)$")
UNIT_NAME = {u.key: u.name for u in UNITS}
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _utc(ts: datetime) -> datetime:
    """Sources send aware UTC times; treat a naive one as UTC rather than crash."""
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def _opt_utc(ts: datetime | None) -> datetime | None:
    return None if ts is None else _utc(ts)


# --- wording (shared with climate.utility.skips) -----------------------------------------


def unit_names(unit_keys: Iterable[str], *, lead: bool = True) -> str:
    """'Main floor and upstairs' in house order; ``lead`` capitalizes the first name (for the
    start of a sentence), otherwise every name is lower-case."""
    order = {k: i for i, k in enumerate(UNIT_KEYS)}
    keys = sorted(set(unit_keys), key=lambda k: (order.get(k, len(order)), k))
    names = [UNIT_NAME.get(k, k) for k in keys]
    names = [n if i == 0 and lead else n.lower() for i, n in enumerate(names)]
    if len(names) <= 1:
        return names[0] if names else ""
    return ", ".join(names[:-1]) + " and " + names[-1]


def clock(ts: datetime, tz: str, *, meridiem: bool = True) -> str:
    """'3:00 PM' in house time ('3:00' without the meridiem)."""
    local = to_local(_utc(ts), tz)
    text = f"{local.hour % 12 or 12}:{local.minute:02d}"
    return f"{text} {'AM' if local.hour < 12 else 'PM'}" if meridiem else text


def window_label(start: datetime | None, end: datetime | None, tz: str) -> str:
    """An event's window in house time: 'Tue 3:00–6:00 PM', 'Tue 11:00 AM–1:00 PM',
    'Tue 9:00 PM – Wed 1:00 AM', 'Tue from 3:00 PM', 'until Tue 6:00 PM'."""
    if start is None and end is None:
        return "time not reported"
    if start is None:
        assert end is not None
        return f"until {_DAYS[to_local(_utc(end), tz).weekday()]} {clock(end, tz)}"
    ls = to_local(_utc(start), tz)
    if end is None:
        return f"{_DAYS[ls.weekday()]} from {clock(start, tz)}"
    le = to_local(_utc(end), tz)
    if le.date() == ls.date():
        same_half = (ls.hour < 12) == (le.hour < 12)
        return f"{_DAYS[ls.weekday()]} {clock(start, tz, meridiem=not same_half)}–{clock(end, tz)}"
    return f"{_DAYS[ls.weekday()]} {clock(start, tz)} – {_DAYS[le.weekday()]} {clock(end, tz)}"


def row_change_label(row: UtilityEvent) -> str:
    """``change_label`` for a stored row (AC/heat off and the duty cycle come from ``detail``)."""
    d = row.detail or {}
    return change_label(
        heat_f=row.heat_f, cool_f=row.cool_f, is_relative=bool(row.is_relative), heat_offset_f=row.heat_offset_f,
        cool_offset_f=row.cool_offset_f, is_cool_off=bool(d.get("is_cool_off")), is_heat_off=bool(d.get("is_heat_off")),
        duty_cycle_pct=row.duty_cycle_pct,
    )


def event_name(rows: Iterable[UtilityEvent]) -> str:
    return next((r.name for r in rows if r.name), None) or "energy-saving event"


def over_at(row: UtilityEvent) -> datetime | None:
    """When the event stopped acting on this thermostat: the earlier of its scheduled end and
    when it was seen over (an opt-out or an early end comes before the scheduled end)."""
    times = [_utc(t) for t in (row.end_at, row.ended_at) if t is not None]
    return min(times) if times else None


# --- alerts ------------------------------------------------------------------------------


def alert_key(key: str, phase: str) -> str:
    return f"{ALERT_PREFIX}{key}:{phase}"


def parse_alert_key(dedupe_key: str | None) -> tuple[str, str] | None:
    """(event_key, phase) of a utility-event alert's dedupe key; None for any other key."""
    if not dedupe_key or not dedupe_key.startswith(ALERT_PREFIX):
        return None
    rest = dedupe_key[len(ALERT_PREFIX):]
    m = _RULE_PHASE.match(rest)
    if m:
        return m["key"], m["phase"]
    for phase in (*TRANSIENT_PHASES, *NEWS_PHASES):
        if rest.endswith(":" + phase) and len(rest) > len(phase) + 1:
            return rest[: -len(phase) - 1], phase
    return None


def alert_exists(session: Session, dedupe_key: str) -> bool:
    """An alert with this key was ever raised (open or resolved)."""
    return session.execute(select(Alert.id).where(Alert.dedupe_key == dedupe_key).limit(1)).first() is not None


def phase_alert(
    session: Session, dedupe_key: str, level: str, title: str, body: str, *, push_info: bool = False,
    kind: str = ALERT_KIND,
) -> int | None:
    """Raise an alert at most once per key, ever: a resolved one is never raised again. While
    it is open its title and body are kept current (another thermostat joined the event)
    without pushing again. Returns the new alert's id, else None."""
    last = session.execute(
        select(Alert.id, Alert.resolved_at, Alert.title, Alert.body)
        .where(Alert.dedupe_key == dedupe_key).order_by(Alert.id.desc()).limit(1)
    ).first()
    if last is None:
        return notify.raise_alert(session, kind, level, title, body, dedupe_key=dedupe_key, push_info=push_info)
    if last.resolved_at is None and (last.title, last.body) != (title, body):
        session.execute(update(Alert).where(Alert.id == last.id).values(title=title, body=body))
        publish(session, "alert", last.id)
    return None


def resolve_keys(session: Session, dedupe_keys: Iterable[str]) -> list[int]:
    """Resolve every open alert with one of these keys in one statement; returns their ids."""
    keys = sorted(set(dedupe_keys))
    if not keys:
        return []
    ids = list(session.execute(
        update(Alert).where(Alert.dedupe_key.in_(keys), Alert.resolved_at.is_(None))
        .values(resolved_at=func.now()).returning(Alert.id)
    ).scalars())
    for alert_id in ids:
        publish(session, "alert", alert_id)
    return ids


def house_tz(session: Session) -> str:
    try:
        return get_setting(session, "location", LocationSettings).tz
    except Exception:  # noqa: BLE001 - unreadable settings: the default zone, never a crash
        log.warning("location settings unreadable; using the default time zone")
        return LocationSettings().tz


def event_settings(session: Session) -> UtilityEventSettings:
    try:
        return get_setting(session, "utility_events", UtilityEventSettings)
    except Exception:  # noqa: BLE001
        log.warning("utility_events settings unreadable; using the defaults")
        return UtilityEventSettings()


def reconcile_alerts(
    session: Session, key: str, *, cfg: UtilityEventSettings | None = None, tz: str | None = None,
) -> None:
    """Bring the grouped alerts of one event (every thermostat with ``key``) in line with its
    rows: 'running' anywhere -> the started alert (announced resolved); else 'announced'
    anywhere -> the announced alert; else over everywhere -> the transient alerts resolved and
    'ended' (thermostats where it ran to its end or vanished after its start) or, when every
    row was cancelled, 'cancelled'. A thermostat that only opted out gets no 'ended' alert:
    the skip alert already said so. Raising needs ``cfg.alerts``; resolving does not."""
    rows = list(session.execute(
        select(UtilityEvent).where(UtilityEvent.event_key == key).order_by(UtilityEvent.id)
    ).scalars())
    if not rows:
        return
    cfg = cfg or event_settings(session)
    tz = tz or house_tz(session)
    by: dict[str, list[UtilityEvent]] = defaultdict(list)
    for r in rows:
        by[r.status].append(r)
    name = event_name(rows)
    starts = [_utc(r.start_at) for r in rows if r.start_at is not None]
    ends = [_utc(r.end_at) for r in rows if r.end_at is not None]
    start, end = (min(starts) if starts else None), (max(ends) if ends else None)
    hint = ("this event is mandatory, so it can't be skipped" if any(r.is_optional is False for r in rows)
            else "you can skip it from Live")
    if by["running"]:
        resolve_keys(session, [alert_key(key, "announced")])
        if cfg.alerts:
            units = [r.unit_key for r in by["running"]]
            until = f"until {clock(end, tz)}" if end is not None else "end time not reported"
            phase_alert(
                session, alert_key(key, "running"), "info", f"Utility event started: {name}",
                f"{unit_names(units)}, {until}, {row_change_label(by['running'][0])}. The app stands aside until it "
                f"ends; {hint}.", push_info=True,
            )
        return
    if by["announced"]:
        if cfg.alerts:
            units = [r.unit_key for r in by["announced"]]
            phase_alert(
                session, alert_key(key, "announced"), "info", f"Utility event announced: {name}",
                f"{unit_names(units)}, {window_label(start, end, tz)}, {row_change_label(by['announced'][0])}. The app "
                f"stands aside during it; {hint}.", push_info=True,
            )
        return
    resolve_keys(session, [alert_key(key, p) for p in TRANSIENT_PHASES]
                 + [alert_key(key, f"rule:{r.unit_key}") for r in rows])
    if not cfg.alerts:
        return
    if by["ended"]:
        stops = [t for t in (over_at(r) for r in by["ended"]) if t is not None]
        at = f" at {clock(max(stops), tz)}" if stops else ""
        phase_alert(
            session, alert_key(key, "ended"), "info", f"Utility event ended: {name}",
            f"The utility event on {unit_names((r.unit_key for r in by['ended']), lead=False)} ended{at}.",
            push_info=True,
        )
    elif len(by["cancelled"]) == len(rows):
        phase_alert(
            session, alert_key(key, "cancelled"), "info", f"Utility event cancelled: {name}",
            f"The event planned for {window_label(start, end, tz)} on {unit_names((r.unit_key for r in rows), lead=False)} "
            "is no longer listed by ecobee, so it won't run.",
        )


# --- ingest ------------------------------------------------------------------------------


def _copy(row: UtilityEvent, ev: ThermostatEvent) -> None:
    """The event's fields onto its row; ``detail`` = the event, keeping the app's own keys."""
    row.event_type = ev.event_type
    row.name = ev.name
    row.start_at, row.end_at = _opt_utc(ev.start), _opt_utc(ev.end)
    row.heat_f, row.cool_f = ev.heat_f, ev.cool_f
    row.is_relative = bool(ev.is_relative)
    row.heat_offset_f, row.cool_offset_f = ev.heat_offset_f, ev.cool_offset_f
    row.is_optional = ev.is_optional
    row.duty_cycle_pct = ev.duty_cycle_pct
    own = {k: v for k, v in (row.detail or {}).items() if k not in ThermostatEvent.model_fields}
    row.detail = {**ev.model_dump(mode="json"), **own}


def _apply(session: Session, newest: dict[str, UnitSnapshot], now: datetime) -> tuple[list[int], set[str]]:
    """Upsert the units' rows from their snapshots, each seen at its snapshot's time (never
    later than ``now``: a thermostat clock running ahead puts no sighting in the future).
    Returns (ids of rows created or whose status changed, event keys whose alerts need a look)."""
    listed: dict[str, dict[str, ThermostatEvent]] = {
        unit: {event_key(ev): ev for ev in snap.events if ev.event_type == DR} for unit, snap in newest.items()
    }
    keys = sorted({k for evs in listed.values() for k in evs})
    cond = UtilityEvent.status.in_(OPEN)
    if keys:
        cond = or_(cond, UtilityEvent.event_key.in_(keys))
    have: dict[str, dict[str, UtilityEvent]] = defaultdict(dict)
    for row in session.execute(
        select(UtilityEvent).where(UtilityEvent.unit_key.in_(sorted(newest)), cond)
        .order_by(UtilityEvent.id).with_for_update()
    ).scalars():
        have[row.unit_key][row.event_key] = row

    changed: list[UtilityEvent] = []
    touched: set[str] = set()
    for unit, snap in newest.items():
        ts = min(_utc(snap.ts), _utc(now))
        rows = have.get(unit, {})
        for key, ev in listed[unit].items():
            row = rows.get(key)
            if row is None:
                if not ev.running and ev.end is not None and _utc(ev.end) <= ts:
                    continue  # listed, but already over before we ever saw it
                row = UtilityEvent(
                    unit_key=unit, event_key=key, status="running" if ev.running else "announced",
                    first_seen_at=ts, last_seen_at=ts, started_at=ts if ev.running else None, detail={},
                )
                _copy(row, ev)
                session.add(row)
                changed.append(row)
                touched.add(key)
                continue
            if row.last_seen_at is not None and ts < _utc(row.last_seen_at):
                continue  # an older snapshot than one already applied
            before = (row.status, row.start_at, row.end_at, row.name)
            _copy(row, ev)
            row.last_seen_at = ts
            if ev.running:
                # An opt-out ecobee recorded is final; an event the clock already closed reopens
                # only while it is not long past its (possibly extended) end.
                if row.status != "opted_out" and (
                    row.status != "ended" or row.end_at is None or row.end_at > ts - RUNNING_GRACE
                ):
                    row.status = "running"
                    row.started_at = row.started_at or ts
                    row.ended_at = None
            elif row.status == "running":
                row.status = "opted_out" if row.skip == "done" else "ended"
                row.ended_at = ts
            elif row.status == "cancelled" and (row.end_at is None or row.end_at > ts):
                row.status = "announced"  # listed again ahead of its end
                row.ended_at = None
            if row.status != before[0]:
                changed.append(row)
            if (row.status, row.start_at, row.end_at, row.name) != before:
                touched.add(key)
        for key, row in rows.items():
            if key in listed[unit] or row.status not in OPEN:
                continue
            if row.last_seen_at is not None and ts < _utc(row.last_seen_at):
                continue
            if row.status == "running":
                row.status = "opted_out" if row.skip == "done" else "ended"
            else:
                row.status = "cancelled" if row.start_at is None or _utc(row.start_at) > ts else "ended"
            row.ended_at = ts
            changed.append(row)
            touched.add(key)
    if changed:
        session.flush()
    return sorted({r.id for r in changed}), touched


def unknown_event_alerts(session: Session, newest: dict[str, UnitSnapshot]) -> None:
    """Raise 'unknown_event:<type>' (warn) while any unit's running top event is a type the app
    does not know (the controller is hands-off on that unit); resolve it once none runs. Units
    not in ``newest`` count by their live snapshot. One whose live snapshot is HomeKit's (no
    events visible) leaves what was known alone: new sightings still alert, nothing resolves."""
    hold_types: dict[str, str | None] = {u: s.hold.hold_type if s.hold is not None else None for u, s in newest.items()}
    complete = True
    others = [u for u in UNIT_KEYS if u not in newest]
    if others:
        for unit, source, hold_type in session.execute(
            select(LiveUnit.unit_key, LiveUnit.source, LiveUnit.snapshot["hold"]["hold_type"].astext)
            .where(LiveUnit.unit_key.in_(others))
        ):
            if source == "homekit":
                complete = False
            else:
                hold_types[unit] = hold_type
    by_type: dict[str, list[str]] = defaultdict(list)
    for unit, hold_type in hold_types.items():
        if hold_type and hold_type not in KNOWN_HOLD_TYPES:
            by_type[hold_type].append(unit)
    prefix = f"{UNKNOWN_KIND}:"
    open_alerts = {
        key: (alert_id, title, body)
        for alert_id, key, title, body in session.execute(
            select(Alert.id, Alert.dedupe_key, Alert.title, Alert.body)
            .where(Alert.resolved_at.is_(None), Alert.kind == UNKNOWN_KIND)
        )
    }
    for hold_type, units in sorted(by_type.items()):
        many = len(units) > 1
        title = f"Unrecognised ecobee event: {hold_type}"
        body = (f"An unrecognised ecobee event ({hold_type}) is running on the {unit_names(units, lead=False)} "
                f"thermostat{'s' if many else ''}; the app is hands-off on {'those units' if many else 'that unit'} "
                "until it ends.")
        key = prefix + hold_type
        current = open_alerts.get(key)
        if current is None:
            notify.raise_alert(session, UNKNOWN_KIND, "warn", title, body, dedupe_key=key)
        elif complete and (current[1], current[2]) != (title, body):
            session.execute(update(Alert).where(Alert.id == current[0]).values(title=title, body=body))
            publish(session, "alert", current[0])
    if complete:
        resolve_keys(session, [k for k in open_alerts if k and k[len(prefix):] not in by_type])


def ingest(session: Session, snapshots: Sequence[UnitSnapshot], now: datetime) -> list[int]:
    """Bring ``utility_events`` up to date with live snapshots (the module docstring has the
    rules), raise or resolve the grouped event alerts, and the unknown-event warning. HomeKit
    snapshots are skipped. ``now`` is the poll time (no sighting is recorded later than it).
    Returns the ids of rows created or whose status changed.

    Runs on every poll, so it stays cheap: with nothing new it is one indexed read of the
    units' open rows, one of live_units and one of the open unknown-event alerts. The poller
    runs it in a savepoint and logs a failure; it never fails a poll."""
    newest: dict[str, UnitSnapshot] = {}
    for snap in snapshots:
        if snap.source == "homekit" or snap.unit_key not in UNIT_KEYS:
            continue
        current = newest.get(snap.unit_key)
        if current is None or _utc(snap.ts) >= _utc(current.ts):
            newest[snap.unit_key] = snap
    if not newest:
        return []
    changed, touched = _apply(session, newest, now)
    if touched:
        publish(session, "status")
        cfg, tz = event_settings(session), house_tz(session)
        for key in sorted(touched):
            reconcile_alerts(session, key, cfg=cfg, tz=tz)
    unknown_event_alerts(session, newest)
    return changed


# --- sweep -------------------------------------------------------------------------------


def _resolve_stale_alerts(session: Session, now: datetime) -> list[int]:
    """Resolve open utility-event alerts of events that are over everywhere: the transient
    ones at once, news (ended, cancelled, skip outcomes) ALERT_KEEP after the event was over,
    and any whose event has no rows at all."""
    open_alerts = [
        (alert_id, parsed)
        for alert_id, key in session.execute(
            select(Alert.id, Alert.dedupe_key).where(Alert.resolved_at.is_(None), Alert.kind == ALERT_KIND)
        )
        if (parsed := parse_alert_key(key)) is not None
    ]
    if not open_alerts:
        return []
    keys = sorted({p[0] for _, p in open_alerts})
    still_open: set[str] = set()
    last_over: dict[str, datetime | None] = {}
    for row in session.execute(select(UtilityEvent).where(UtilityEvent.event_key.in_(keys))).scalars():
        if row.status in OPEN:
            still_open.add(row.event_key)
            continue
        t = over_at(row)
        prev = last_over.get(row.event_key)
        last_over[row.event_key] = t if prev is None else (prev if t is None else max(prev, t))
    stale: list[int] = []
    for alert_id, (key, phase) in open_alerts:
        if key in still_open:
            continue
        if key not in last_over:
            stale.append(alert_id)  # no rows for it any more
        elif phase not in NEWS_PHASES:
            stale.append(alert_id)
        else:
            at = last_over[key]
            if at is None or at <= now - ALERT_KEEP:
                stale.append(alert_id)
    if not stale:
        return []
    ids = list(session.execute(
        update(Alert).where(Alert.id.in_(stale), Alert.resolved_at.is_(None))
        .values(resolved_at=func.now()).returning(Alert.id)
    ).scalars())
    for alert_id in ids:
        publish(session, "alert", alert_id)
    return ids


def sweep(session: Session, now: datetime) -> list[int]:
    """The clock closes what no snapshot did (a snapshot can lag, or the cloud be down): a
    'running' event 10 minutes past its end and an 'announced' one past its end become 'ended'
    (``ended_at`` = now). An announced event whose start has passed stays announced until a
    snapshot or its end decides. Then resolves stale event alerts. Returns the ids closed.
    Cheap when nothing is due: one indexed read of open rows and one of open event alerts."""
    closed: list[UtilityEvent] = []
    for row in session.execute(
        select(UtilityEvent).where(UtilityEvent.status.in_(OPEN), UtilityEvent.end_at < now)
        .order_by(UtilityEvent.id).with_for_update(skip_locked=True)
    ).scalars():
        if row.status == "running":
            if row.end_at is not None and _utc(row.end_at) >= now - RUNNING_GRACE:
                continue
            row.status = "opted_out" if row.skip == "done" else "ended"
        else:
            row.status = "ended"
        row.ended_at = now
        closed.append(row)
    if closed:
        session.flush()
        publish(session, "status")
        cfg, tz = event_settings(session), house_tz(session)
        for key in sorted({r.event_key for r in closed}):
            reconcile_alerts(session, key, cfg=cfg, tz=tz)
    _resolve_stale_alerts(session, now)
    return [r.id for r in closed]
