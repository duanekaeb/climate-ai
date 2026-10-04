"""Learned occupancy patterns per room (blueprint §3): hourly occupancy probability by day type.

``fit_priors`` is a nightly job. It reads the 5-minute occupancy history of every room with
an occupancy-capable sensor and stores, per room, the share of 5-minute slots that were
occupied in each local hour, separately for school days (``Schedule.school_days``) and the
other days ("weekend"). Holidays are not modelled yet. Rooms without a sensor (the Twins'
and Olive's rooms, the Foyer) get no prior: bedtimes are their only signal.

The fit is stored as ``model_fits`` kind ``'occupancy_priors'`` (status 'active', the
previous active fit retired). It feeds the later "Arriving" state; nothing in the control
path depends on it yet.

params shape::

    {"tz": "America/Chicago", "day_types": ["school_day", "weekend"],
     "rooms": {room_key: {"school_day": [p_00, ..., p_23], "weekend": [...]}},   # None = no data
     "slots": {room_key: {"school_day": [n_00, ..., n_23], "weekend": [...]}}}
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from climate.house import ROOMS, sensors_for_room
from climate.store.app_settings import ControlSettings, LocationSettings, get_setting
from climate.store.orm import ModelFit
from climate.timeutil import local_date, to_local

DayType = Literal["school_day", "weekend"]
DAY_TYPES: tuple[DayType, DayType] = ("school_day", "weekend")
TRAIN_DAYS = 56
MIN_DAYS = 7  # fewer local days of occupancy data than this -> no fit
MIN_SLOTS_PER_HOUR = 3  # an hour bucket needs this many 5-minute slots to report a probability

_SLOTS_SQL = text(
    """
    WITH slots AS (
        SELECT s.room_key, r.ts, bool_or(r.occupied) AS occ
        FROM readings_5m r
        JOIN sensors s ON s.key = r.sensor_key
        WHERE s.has_occupancy AND s.is_active AND r.occupied IS NOT NULL
          AND r.ts >= :start AND r.ts < :end
        GROUP BY s.room_key, r.ts
    )
    SELECT room_key,
           (extract(isodow FROM ts AT TIME ZONE :tz)::int - 1) AS weekday,
           extract(hour FROM ts AT TIME ZONE :tz)::int AS hour,
           count(*) AS n,
           count(*) FILTER (WHERE occ) AS k
    FROM slots
    GROUP BY 1, 2, 3
    """
)


def day_type(weekday: int, school_days: list[int]) -> DayType:
    return "school_day" if weekday in school_days else "weekend"


def fit_priors(session: Session, now: datetime, train_days: int = TRAIN_DAYS) -> int | None:
    """Fit hourly occupancy probabilities per room and day type over the last ``train_days``
    complete local days; store them as the active 'occupancy_priors' fit and return its id.
    Returns None (and stores nothing) when fewer than MIN_DAYS days of data exist."""
    tz = get_setting(session, "location", LocationSettings).tz
    schedule = get_setting(session, "control", ControlSettings).schedule
    today = local_date(now, tz)
    end_local = to_local(now, tz).replace(hour=0, minute=0, second=0, microsecond=0)
    start_local = end_local - timedelta(days=train_days)
    start, end = start_local, end_local  # tz-aware; psycopg sends them as timestamptz

    rows = session.execute(_SLOTS_SQL, {"start": start, "end": end, "tz": tz}).all()
    if not rows:
        return None
    n_days = session.execute(
        text(
            """
            SELECT count(DISTINCT (r.ts AT TIME ZONE :tz)::date)
            FROM readings_5m r JOIN sensors s ON s.key = r.sensor_key
            WHERE s.has_occupancy AND r.occupied IS NOT NULL AND r.ts >= :start AND r.ts < :end
            """
        ),
        {"start": start, "end": end, "tz": tz},
    ).scalar_one()
    if n_days < MIN_DAYS:
        return None

    counts: dict[str, dict[str, list[list[int]]]] = {}
    for room_key, weekday, hour, n, k in rows:
        dt = day_type(int(weekday), schedule.school_days)
        per = counts.setdefault(room_key, {t: [[0, 0] for _ in range(24)] for t in DAY_TYPES})
        cell = per[dt][int(hour)]
        cell[0] += int(n)
        cell[1] += int(k)

    rooms: dict[str, dict[str, list[float | None]]] = {}
    slots: dict[str, dict[str, list[int]]] = {}
    for room in ROOMS:
        if not room.has_sensor or not any(s.has_occupancy for s in sensors_for_room(room.key)):
            continue
        per = counts.get(room.key)
        if per is None:
            continue
        rooms[room.key] = {
            t: [round(k / n, 3) if n >= MIN_SLOTS_PER_HOUR else None for n, k in per[t]] for t in DAY_TYPES
        }
        slots[room.key] = {t: [n for n, _ in per[t]] for t in DAY_TYPES}
    if not rooms:
        return None

    params: dict[str, Any] = {"tz": tz, "day_types": list(DAY_TYPES), "rooms": rooms, "slots": slots}
    metrics = {
        "days": int(n_days),
        "rooms": len(rooms),
        "slots": int(sum(sum(v) for per in slots.values() for v in per.values())),
    }
    session.execute(
        update(ModelFit)
        .where(ModelFit.kind == "occupancy_priors", ModelFit.status == "active")
        .values(status="retired")
    )
    fit = ModelFit(
        kind="occupancy_priors",
        unit_key=None,
        mode=None,
        train_start=start_local.date(),
        train_end=today - timedelta(days=1),
        params=params,
        metrics=metrics,
        status="active",
        notes=(
            f"Hourly occupancy probability for {len(rooms)} rooms from {n_days} days, school days vs "
            "other days; holidays not separated yet."
        ),
    )
    session.add(fit)
    session.flush()
    return fit.id


def active_priors(session: Session) -> dict[str, Any] | None:
    """Params of the active occupancy_priors fit, or None."""
    return session.execute(
        select(ModelFit.params)
        .where(ModelFit.kind == "occupancy_priors", ModelFit.status == "active")
        .order_by(ModelFit.created_at.desc(), ModelFit.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def occupancy_probability(
    params: dict[str, Any], room_key: str, local_ts: datetime, school_days: list[int],
) -> float | None:
    """Prior probability that ``room_key`` is occupied at ``local_ts`` (a house-local time),
    or None when the room or hour has no data. Reads ``params`` defensively."""
    try:
        hours = params["rooms"][room_key][day_type(local_ts.weekday(), school_days)]
        value = hours[local_ts.hour]
    except (KeyError, IndexError, TypeError):
        return None
    return float(value) if isinstance(value, (int, float)) else None
