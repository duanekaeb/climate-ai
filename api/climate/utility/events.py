"""``utility_events`` rows: identity, loading for the house state, ingest from snapshots,
and the clock sweep (see docs/specs/holds-and-utility-events.md, "Utility events").

``event_key`` and ``load_active`` are the shared contract (state.py calls ``load_active``);
the ingest/sweep functions below them belong to the utility-events builder.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from climate.control.policy import UtilityEventState
from climate.sources.base import ThermostatEvent
from climate.store.orm import UtilityEvent

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
