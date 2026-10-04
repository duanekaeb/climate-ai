"""Live status (the Live tab) and the time-series readers: room history, daily and intraday
runtime, weather.

``/status`` is assembled from several services; each part runs in its own SAVEPOINT through
``safe`` so a failing service degrades its part (a target of None, no weather, a fallback
room list) instead of failing the whole page. Rooms without a sensor never get a temperature.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from fastapi import status as http
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from climate import state
from climate.analytics import baseline, daily, metrics
from climate.api.auth import ReaderDep, Role
from climate.api.routers.agent import agent_info
from climate.api.routers.control import SessionDep, controller_info, house_tz, parse_dt, safe
from climate.api.schemas import (
    AgentInfo,
    AlertOut,
    ControllerInfo,
    DailyRuntime,
    HomekitInfo,
    HouseStatus,
    Intraday,
    IntradayPoint,
    IntradayUnit,
    OutdoorPoint,
    RoomHistory,
    RoomPoint,
    SetpointPoint,
    SourceInfo,
    UnitLive,
    WeatherNow,
    WeatherOut,
    WeatherPoint,
)
from climate.control import controller
from climate.control.policy import PolicyParams, RoomStatus, UnitStatus, UnitTarget
from climate.house import ROOMS, UNIT_KEYS
from climate.sources.base import UnitSnapshot
from climate.store.app_settings import (
    AgentSettings,
    ControlSettings,
    LocationSettings,
    SourceSettings,
    get_heartbeat,
    get_raw,
    get_setting,
)
from climate.store.orm import (
    Alert,
    HomekitDevice,
    LiveSensor,
    LiveUnit,
    Reading5m,
    Room,
    RoomStateRow,
    Runtime5m,
    SecretRow,
    Sensor,
    Unit,
    WeatherHour,
)
from climate.timeutil import day_bounds_utc, floor_slot, local_date, utcnow

log = logging.getLogger(__name__)
router = APIRouter(tags=["status"])

STALE_S = 15 * 60  # a snapshot or reading older than this is stale
HOMEKIT_ONLINE_S = 3 * 60  # homekit service heartbeat freshness
WORKER_STALE_S = 10 * 60  # worker heartbeat freshness
WEATHER_PREFERENCE = ("open-meteo", "simulator", "nws", "ecobee")
ROOM_SORT = {r.key: r.sort for r in ROOMS}
UNIT_SORT = {k: i for i, k in enumerate(UNIT_KEYS)}


# ---------------------------------------------------------------------------------------
# small shared readers (also used by setup)
# ---------------------------------------------------------------------------------------


def has_secret(session: Session, key: str) -> bool:
    """Whether an encrypted secret exists (never decrypts it)."""
    return session.execute(select(SecretRow.key).where(SecretRow.key == key)).first() is not None


def heartbeat_fresh(session: Session, service: str, now: datetime, max_age_s: float) -> tuple[bool, datetime | None]:
    hb = get_heartbeat(session, service)
    if hb is None:
        return False, None
    return (now - hb.at).total_seconds() <= max_age_s, hb.at


def _rank(source: str) -> int:
    return WEATHER_PREFERENCE.index(source) if source in WEATHER_PREFERENCE else len(WEATHER_PREFERENCE)


def pick_weather_source(session: Session, kind: str, start: datetime, end: datetime) -> str | None:
    """The preferred source that has ``kind`` rows in [start, end]."""
    sources = session.execute(
        select(WeatherHour.source).where(
            WeatherHour.kind == kind, WeatherHour.ts >= start, WeatherHour.ts <= end
        ).distinct()
    ).scalars().all()
    return min(sources, key=_rank) if sources else None


def _weather_rows(session: Session, kind: str, source: str, start: datetime, end: datetime) -> list[WeatherHour]:
    q = (
        select(WeatherHour)
        .where(WeatherHour.kind == kind, WeatherHour.source == source, WeatherHour.ts >= start, WeatherHour.ts <= end)
        .order_by(WeatherHour.ts)
    )
    return list(session.execute(q).scalars())


def _r(value: float | None, nd: int = 1) -> float | None:
    return None if value is None else round(float(value), nd)


# ---------------------------------------------------------------------------------------
# /status parts
# ---------------------------------------------------------------------------------------


def call_from_running(running: list[str]) -> str:
    names = [r.lower() for r in running]
    if any(n.startswith("compcool") for n in names):
        return "cool"
    if any(n.startswith(("compheat", "auxheat", "heatpump")) for n in names):
        return "heat"
    if any(n.startswith("fan") for n in names):
        return "fan"
    return "idle"


def fallback_units(session: Session, now: datetime) -> list[UnitStatus]:
    """Unit statuses straight from live_units when the state service is unavailable."""
    out: list[UnitStatus] = []
    rows = session.execute(
        select(Unit, LiveUnit).outerjoin(LiveUnit, LiveUnit.unit_key == Unit.key).order_by(Unit.sort)
    ).all()
    for unit, live in rows:
        snap = None
        if live is not None:
            try:
                snap = UnitSnapshot.model_validate(live.snapshot)
            except ValidationError:
                log.warning("live_units.%s snapshot does not parse", unit.key)
        out.append(
            UnitStatus(
                unit_key=unit.key,
                name=unit.name,
                snapshot=snap,
                age_s=(now - snap.ts).total_seconds() if snap else None,
                call=call_from_running(snap.equipment_running) if snap else "unknown",
            )
        )
    return out


def fallback_rooms(session: Session, now: datetime) -> list[RoomStatus]:
    """Rooms with measured temperatures from live_sensors and occupancy 'unknown'."""
    live = {
        s.sensor_key: s for s in session.execute(select(LiveSensor)).scalars()
    }
    sensors_by_room: dict[str, list[Sensor]] = defaultdict(list)
    for s in session.execute(select(Sensor).where(Sensor.is_active.is_(True)).order_by(Sensor.sort)).scalars():
        sensors_by_room[s.room_key].append(s)
    out: list[RoomStatus] = []
    for room in session.execute(select(Room).order_by(Room.sort)).scalars():
        keys = [s.key for s in sensors_by_room.get(room.key, [])]
        readings = [live[k] for k in keys if k in live]
        temps = [r.temp_f for r in readings if r.temp_f is not None and r.online]
        hums = [r.humidity for r in readings if r.humidity is not None and r.online]
        newest = max((r.ts for r in readings), default=None)
        measured = room.has_sensor and bool(temps)
        out.append(
            RoomStatus(
                room_key=room.key,
                name=room.name,
                unit_key=room.unit_key,
                floor=room.floor,
                has_sensor=room.has_sensor,
                is_sleep_room=room.is_sleep_room,
                has_comfort_target=room.has_comfort_target,
                temp_f=_r(sum(temps) / len(temps), 2) if measured else None,
                humidity=_r(sum(hums) / len(hums), 1) if room.has_sensor and hums else None,
                state="unknown" if room.has_comfort_target else "no_target",
                reason="Occupancy is unavailable right now.",
                sensor_keys=keys,
                stale=room.has_sensor and (newest is None or (now - newest).total_seconds() > STALE_S),
            )
        )
    return out


def unit_live(us: UnitStatus, target: UnitTarget | None, today: dict[str, float | None], now: datetime) -> UnitLive:
    snap = us.snapshot
    age = us.age_s if us.age_s is not None else ((now - snap.ts).total_seconds() if snap else None)
    return UnitLive(
        unit_key=us.unit_key,
        name=us.name,
        hvac_mode=snap.hvac_mode if snap else None,
        call=us.call,
        running=list(snap.equipment_running) if snap else [],
        zone_temp_f=snap.zone_temp_f if snap else None,
        zone_humidity=snap.zone_humidity if snap else None,
        heat_sp_f=snap.heat_sp_f if snap else None,
        cool_sp_f=snap.cool_sp_f if snap else None,
        climate_ref=snap.climate_ref if snap else None,
        hold=snap.hold if snap else None,
        target=target,
        age_s=_r(age, 0),
        connected=bool(snap and snap.connected and age is not None and age <= STALE_S),
        today_runtime_min=float(today.get("today_runtime_min") or 0.0),
        duty_last_hour_pct=today.get("duty_last_hour_pct"),
        maxed_minutes_today=float(today.get("maxed_minutes_today") or 0.0),
    )


def weather_now(session: Session, now: datetime, tz: str) -> WeatherNow | None:
    current: WeatherHour | None = None
    src = pick_weather_source(session, "observed", now - timedelta(hours=3), now)
    if src:
        current = session.execute(
            select(WeatherHour)
            .where(WeatherHour.kind == "observed", WeatherHour.source == src, WeatherHour.ts <= now)
            .order_by(WeatherHour.ts.desc())
            .limit(1)
        ).scalar_one_or_none()
    if current is None:  # no recent observation: the forecast hour containing now
        hour = now.replace(minute=0, second=0, microsecond=0)
        fsrc = pick_weather_source(session, "forecast", hour, hour)
        if fsrc:
            current = session.execute(
                select(WeatherHour).where(
                    WeatherHour.kind == "forecast", WeatherHour.source == fsrc, WeatherHour.ts == hour
                )
            ).scalar_one_or_none()
    # Today's high/low over the whole local day: every forecast hour plus the observed hours so
    # far (a source may keep no forecast rows for hours already past, e.g. the simulator).
    day_start, day_end = day_bounds_utc(local_date(now, tz), tz)
    day_end = day_end - timedelta(seconds=1)
    obs_src = pick_weather_source(session, "observed", day_start, now)
    fc_src = pick_weather_source(session, "forecast", day_start, day_end)
    temps: list[float] = []
    if obs_src:
        temps += session.execute(
            select(WeatherHour.temp_f).where(
                WeatherHour.kind == "observed", WeatherHour.source == obs_src,
                WeatherHour.ts >= day_start, WeatherHour.ts <= now, WeatherHour.temp_f.is_not(None),
            )
        ).scalars().all()
    if fc_src:
        temps += session.execute(
            select(WeatherHour.temp_f).where(
                WeatherHour.kind == "forecast", WeatherHour.source == fc_src,
                WeatherHour.ts >= day_start, WeatherHour.ts <= day_end, WeatherHour.temp_f.is_not(None),
            )
        ).scalars().all()
    if current is not None and current.temp_f is not None:
        temps.append(current.temp_f)
    high = max(temps) if temps else None
    low = min(temps) if temps else None
    day_src = obs_src or fc_src
    if current is None and high is None:
        return None
    return WeatherNow(
        temp_f=_r(current.temp_f) if current else None,
        rh=_r(current.rh, 0) if current else None,
        cloud_cover=_r(current.cloud_cover, 0) if current else None,
        shortwave_wm2=_r(current.shortwave_wm2, 0) if current else None,
        forecast_high_f=_r(high),
        forecast_low_f=_r(low),
        source=current.source if current else (day_src or "none"),
    )


def source_info(session: Session, now: datetime) -> SourceInfo:
    src = get_setting(session, "source", SourceSettings)
    hb = get_heartbeat(session, "worker")
    top = hb.detail if hb else {}
    nested = top.get("source") if isinstance(top.get("source"), dict) else {}

    def pick(nested_key: str, top_key: str) -> object:
        return nested[nested_key] if nested.get(nested_key) is not None else top.get(top_key)

    if hb is None:
        ok, detail = False, "The worker has not reported yet."
    else:
        age = (now - hb.at).total_seconds()
        source_ok = pick("ok", "source_ok")
        text = pick("detail", "source_detail") or top.get("source_error")
        detail = text if isinstance(text, str) else ""
        ok = hb.ok and source_ok is not False and age <= WORKER_STALE_S
        if age > WORKER_STALE_S:
            detail = f"The worker has not reported for {int(age // 60)} min."
    signed_in: bool | None = None
    if src.kind == "ecobee":
        status_raw = get_raw(session, "ecobee_status")
        hb_signed = pick("signed_in", "signed_in")
        if isinstance(status_raw, dict) and isinstance(status_raw.get("signed_in"), bool):
            signed_in = status_raw["signed_in"]
        elif isinstance(hb_signed, bool):
            signed_in = hb_signed
        else:
            signed_in = has_secret(session, "ecobee_refresh_token")
    return SourceInfo(
        kind=src.kind, ok=ok, detail=detail, signed_in=signed_in,
        last_success_at=parse_dt(pick("last_success_at", "last_success_at") or top.get("source_last_success_at")),
    )


def homekit_info(session: Session, now: datetime) -> HomekitInfo:
    enabled = get_setting(session, "source", SourceSettings).homekit_enabled
    online, last = heartbeat_fresh(session, "homekit", now, HOMEKIT_ONLINE_S)
    paired = session.execute(
        select(func.count()).select_from(HomekitDevice).where(HomekitDevice.pairing_state == "paired")
    ).scalar_one()
    return HomekitInfo(enabled=enabled, online=online, last_beat_at=last, paired=int(paired))


def open_alerts(session: Session, limit: int = 50) -> list[AlertOut]:
    rows = session.execute(
        select(Alert).where(Alert.resolved_at.is_(None)).order_by(Alert.ts.desc(), Alert.id.desc()).limit(limit)
    ).scalars()
    return [AlertOut.model_validate(a, from_attributes=True) for a in rows]


# ---------------------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------------------


@router.get("/status", response_model=HouseStatus)
def get_status(_: Role = ReaderDep, session: Session = SessionDep) -> HouseStatus:
    now = utcnow()
    tz = house_tz(session)
    location = get_setting(session, "location", LocationSettings)

    hs = safe(session, "state.load_house_state", lambda: state.load_house_state(session, now), None)
    if hs is not None:
        unit_statuses = sorted(hs.units.values(), key=lambda u: UNIT_SORT.get(u.unit_key, 99))
        rooms = sorted(hs.rooms.values(), key=lambda r: ROOM_SORT.get(r.room_key, 99))
        house_empty, empty_reason = hs.house_empty, hs.house_empty_reason
    else:
        unit_statuses = safe(session, "fallback units", lambda: fallback_units(session, now), [])
        rooms = safe(session, "fallback rooms", lambda: fallback_rooms(session, now), [])
        house_empty, empty_reason = False, "Occupancy is unavailable right now; treating the house as occupied."

    plan_rows = safe(session, "controller.current_plan", lambda: controller.current_plan(session, now), [])
    targets = {row.target.unit_key: row.target for row in plan_rows}
    today = safe(session, "metrics.unit_today", lambda: metrics.unit_today(session, now, tz), {})
    units = [unit_live(us, targets.get(us.unit_key), today.get(us.unit_key, {}), now) for us in unit_statuses]

    mode = get_setting(session, "control", ControlSettings).mode
    source_kind = get_setting(session, "source", SourceSettings).kind
    agent_settings = get_setting(session, "agent", AgentSettings)
    return HouseStatus(
        now=now,
        tz=tz,
        source=safe(session, "source info", lambda: source_info(session, now),
                    SourceInfo(kind=source_kind, ok=False, detail="Source status unavailable.")),
        homekit=safe(session, "homekit info", lambda: homekit_info(session, now),
                     HomekitInfo(enabled=False, online=False)),
        controller=safe(session, "controller info", lambda: controller_info(session),
                        ControllerInfo(mode=mode, policy=PolicyParams())),
        units=units,
        rooms=rooms,
        house_empty=house_empty,
        house_empty_reason=empty_reason,
        weather=safe(session, "weather now", lambda: weather_now(session, now, tz), None),
        alerts=safe(session, "open alerts", lambda: open_alerts(session), []),
        agent=safe(session, "agent info", lambda: agent_info(session, now),
                   AgentInfo(enabled=agent_settings.enabled, settings=agent_settings)),
        location_confirmed=location.confirmed,
    )


# ---------------------------------------------------------------------------------------
# rooms / runtime / weather
# ---------------------------------------------------------------------------------------


@router.get("/rooms/{room_key}/history", response_model=RoomHistory)
def room_history(
    room_key: str,
    hours: Annotated[int, Query(ge=1, le=24 * 14)] = 24,
    _: Role = ReaderDep,
    session: Session = SessionDep,
) -> RoomHistory:
    room = session.get(Room, room_key)
    if room is None:
        raise HTTPException(http.HTTP_404_NOT_FOUND, f"Unknown room {room_key!r}.")
    now = utcnow()
    start = now - timedelta(hours=hours)

    readings: dict[datetime, tuple[float | None, bool | None]] = {}
    if room.has_sensor:  # never a temperature for a room without a sensor
        keys = session.execute(
            select(Sensor.key).where(Sensor.room_key == room_key, Sensor.is_active.is_(True))
        ).scalars().all()
        if keys:
            rows = session.execute(
                select(Reading5m.ts, func.avg(Reading5m.temp_f), func.bool_or(Reading5m.occupied))
                .where(Reading5m.sensor_key.in_(keys), Reading5m.ts >= start, Reading5m.ts <= now)
                .group_by(Reading5m.ts)
                .order_by(Reading5m.ts)
            ).all()
            readings = {ts: (_r(t, 2), occ) for ts, t, occ in rows}

    states: dict[datetime, str] = {}
    for ts, st in session.execute(
        select(RoomStateRow.ts, RoomStateRow.state)
        .where(RoomStateRow.room_key == room_key, RoomStateRow.ts >= start, RoomStateRow.ts <= now)
        .order_by(RoomStateRow.ts)
    ).all():
        states[floor_slot(ts)] = st  # last state within each 5-minute slot

    points: list[RoomPoint] = []
    last_state: str | None = None
    last_state_at: datetime | None = None
    for slot in sorted(set(readings) | set(states)):
        if slot in states:
            last_state, last_state_at = states[slot], slot
        carried = last_state if last_state_at and slot - last_state_at <= timedelta(minutes=15) else None
        temp, occ = readings.get(slot, (None, None))
        points.append(RoomPoint(ts=slot, temp_f=temp, occupied=occ, state=carried))

    setpoints = [
        SetpointPoint(ts=ts, heat_sp_f=_r(h), cool_sp_f=_r(c))
        for ts, h, c in session.execute(
            select(Runtime5m.ts, Runtime5m.heat_sp_f, Runtime5m.cool_sp_f)
            .where(Runtime5m.unit_key == room.unit_key, Runtime5m.ts >= start, Runtime5m.ts <= now)
            .order_by(Runtime5m.ts)
        ).all()
    ]
    return RoomHistory(room_key=room_key, hours=hours, points=points, setpoints=setpoints)


def _expected_min(fit: baseline.BaselineFit | None, row: daily.DayRow, today: date) -> float | None:
    """The weather-expected runtime for a COMPLETE, finished day over the slots it has data for
    (a >= 90% day's cool_min / heat_min cover only those slots, so the full-day expectation
    would overstate it by up to 10%); None for today (still in progress: late in the evening it
    has 90% of its slots, but the missing ones are the evening) and other partial days (a
    partial day's expectation depends on which hours are missing). Same rule as
    baseline.daily_runtime."""
    if fit is None or row.day >= today or not daily.is_complete(row):
        return None
    try:
        e = baseline.expected_covered_seconds(fit, row)
        return None if not math.isfinite(e) else _r(e / 60.0)
    except Exception:
        log.warning("expected runtime failed for %s %s", row.unit_key, row.day, exc_info=True)
        return None


@router.get("/runtime/daily", response_model=list[DailyRuntime])
def runtime_daily(
    days: Annotated[int, Query(ge=1, le=400)] = 30, _: Role = ReaderDep, session: Session = SessionDep
) -> list[DailyRuntime]:
    tz = house_tz(session)
    today = local_date(utcnow(), tz)
    rows = daily.daily_rows(session, today - timedelta(days=days - 1), today, tz)
    fits = safe(session, "baseline.active_fits", lambda: baseline.active_fits(session), {})
    out: list[DailyRuntime] = []
    for r in sorted(rows, key=lambda r: (r.day, UNIT_SORT.get(r.unit_key, 99))):
        mode = r.mode if r.mode in ("cool", "heat") else None
        out.append(
            DailyRuntime(
                date=r.day,
                unit_key=r.unit_key,
                cool_min=round(r.cool_s / 60.0, 1),
                heat_min=round(r.heat_s / 60.0, 1),
                aux_min=round(r.aux_s / 60.0, 1),
                fan_min=round(r.fan_s / 60.0, 1),
                expected_min=_expected_min(fits.get((r.unit_key, mode)) if mode else None, r, today),
                mode=mode,
                outdoor_mean_f=_r(r.outdoor_mean_f),
                outdoor_max_f=_r(r.outdoor_max_f),
                cdd65=_r(r.cdd65, 2),
                hdd65=_r(r.hdd65, 2),
                maxed_min=round(float(r.maxed_min or 0.0), 1),
            )
        )
    return out


@router.get("/runtime/intraday", response_model=Intraday)
def runtime_intraday(
    day: Annotated[date | None, Query(alias="date")] = None, _: Role = ReaderDep, session: Session = SessionDep
) -> Intraday:
    tz = house_tz(session)
    d = day or local_date(utcnow(), tz)
    start, end = day_bounds_utc(d, tz)
    by_unit: dict[str, list[IntradayPoint]] = {k: [] for k in UNIT_KEYS}
    # Same heating metric as the daily totals: comp_heat1 for a heat pump (aux = backup heat),
    # aux_heat1 for a furnace (no separate backup). Stage-2 columns are never added.
    heat_pump = daily.heat_metrics(session)
    for r in session.execute(
        select(Runtime5m).where(Runtime5m.ts >= start, Runtime5m.ts < end).order_by(Runtime5m.unit_key, Runtime5m.ts)
    ).scalars():
        hp = heat_pump.get(r.unit_key, True)
        by_unit.setdefault(r.unit_key, []).append(
            IntradayPoint(
                ts=r.ts, cool_s=r.comp_cool1, heat_s=r.comp_heat1 if hp else r.aux_heat1,
                aux_s=r.aux_heat1 if hp else 0, fan_s=r.fan,
                zone_temp_f=_r(r.zone_temp_f, 2), heat_sp_f=_r(r.heat_sp_f), cool_sp_f=_r(r.cool_sp_f),
            )
        )
    last = end - timedelta(seconds=1)
    kind, src = "observed", pick_weather_source(session, "observed", start, last)
    if src is None:
        kind, src = "forecast", pick_weather_source(session, "forecast", start, last)
    outdoor = [OutdoorPoint(ts=w.ts, temp_f=_r(w.temp_f)) for w in _weather_rows(session, kind, src, start, last)] if src else []
    units = [IntradayUnit(unit_key=k, points=pts) for k, pts in sorted(by_unit.items(), key=lambda kv: UNIT_SORT.get(kv[0], 99))]
    return Intraday(date=d, units=units, outdoor=outdoor)


@router.get("/weather", response_model=WeatherOut)
def weather(
    hours_back: Annotated[int, Query(ge=0, le=24 * 31)] = 48,
    hours_ahead: Annotated[int, Query(ge=0, le=24 * 16)] = 48,
    _: Role = ReaderDep,
    session: Session = SessionDep,
) -> WeatherOut:
    now = utcnow()
    obs_start = now - timedelta(hours=hours_back)
    osrc = pick_weather_source(session, "observed", obs_start, now)
    observed = _weather_rows(session, "observed", osrc, obs_start, now) if osrc else []
    # The forecast continues where the observations stop.
    f_start = observed[-1].ts + timedelta(minutes=1) if observed else now - timedelta(hours=1)
    f_end = now + timedelta(hours=hours_ahead)
    fsrc = pick_weather_source(session, "forecast", f_start, f_end) if hours_ahead > 0 else None
    forecast = _weather_rows(session, "forecast", fsrc, f_start, f_end) if fsrc else []
    points = [
        WeatherPoint(ts=w.ts, kind=w.kind, temp_f=_r(w.temp_f), rh=_r(w.rh, 0),
                     cloud_cover=_r(w.cloud_cover, 0), shortwave_wm2=_r(w.shortwave_wm2, 0))
        for w in [*observed, *forecast]
    ]
    return WeatherOut(source=osrc or fsrc or "none", points=points)
