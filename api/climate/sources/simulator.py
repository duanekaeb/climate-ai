"""A simulated house so the whole app runs without credentials (dev, demo, tests).

Three coupled zones (main, up, bed) using the same RC structure as the blueprint's house
model, eleven rooms with fixed offsets/noise, occupancy schedules (school days, evenings,
sleep), synthetic or real weather, and ecobee-like thermostats (setpoints, Smart Away that
floats the main floor to ~80°F when empty -> reproduces the "upstairs maxed out" problem,
holdHours holds, equipment runtime per 5-minute slot, stage-1 only).
Deterministic for a given seed.

Physics (BTU/h flows, BTU/°F capacitances, 1-minute forward-Euler steps)::

    C_up·dT_up/dt     = UA_u(T_out−T_up) + UA_mu(T_main−T_up) + k_s·max(0, T_main−T_up)
                        + a_up·Sun_attic − Q_up + g_up
    C_main·dT_main/dt = UA_m(T_out−T_main) − [UA_mu(T_main−T_up) + k_s·max(0, ·)]
                        + UA_mb(T_bed−T_main) + a_m·Sun − Q_main + g_main
    C_bed·dT_bed/dt   = UA_b(T_out−T_bed) + UA_mb(T_main−T_bed) + a_b·Sun − Q_bed + g_bed

``Sun_attic`` is shortwave lagged ~1.5 h (the upstairs is under the roof). Each unit is an
AC (capacity derates on hot afternoons) plus a gas furnace: cooling seconds go to
``comp_cool1`` (``comp_cool2`` counts the part of those seconds after 10 min of continuous
run on hot afternoons; stage-2 time is INCLUDED in stage 1), furnace seconds go to
``aux_heat1`` (ecobee reports conventional furnace heat as auxHeat), fan = equipment-on.

Thermostats run an ecobee-like program per unit: 'sleep' while any of the unit's sleep
rooms is inside its sleep window (OccupancySettings), 'home' otherwise, setpoints from
ControlSettings.comfort (day / night / away). Smart Away: during 'home', when none of the
unit's occupancy sensors has seen motion for 30 minutes the unit switches to its away band
until motion returns. ``holdHours`` holds override the program until they end. Control is
on the average of the participating sensors (holds use the Home set) with ±0.5°F
hysteresis and a 5-minute minimum run.

Randomness (occupancy, sensor noise, synthetic weather) is a pure hash of (seed, stream,
time, sensor), so results are reproducible regardless of how the timeline is stepped
(live 1-minute polling, catch-up, or ``generate_history``).
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from climate.house import ROOMS, SENSORS, UNITS
from climate.sources.base import (
    HoldInfo,
    HoldRequest,
    RuntimeInterval,
    SensorReading,
    SourceHealth,
    UnitSnapshot,
    WriteResult,
)
from climate.sources.openmeteo import WeatherHourIn
from climate.store.app_settings import (
    DEFAULT_COMFORT,
    ControlSettings,
    LocationSettings,
    OccupancySettings,
)
from climate.timeutil import in_window, parse_hhmm, utcnow

log = logging.getLogger(__name__)

STATE_KEY = "simulator_state"
STATE_VERSION = 1

# --- timing ----------------------------------------------------------------------------
SLOT_MIN = 5
MAX_CATCHUP_MIN = 6 * 60  # beyond this gap, reset to steady state at `now`
WARMUP_MIN = 6 * 60  # spin-up before a reset point so state is settled
BUFFER_MIN = 24 * 60  # completed runtime slots kept in memory
PERSIST_SLOTS_MIN = 3 * 60  # completed slots carried in the saved state
PERSIST_EVERY_MIN = 10  # DB mode: save state at most this often (sim minutes)
SETTINGS_EVERY_MIN = 5  # DB mode: re-read settings this often (sim minutes)
WEATHER_EVERY_MIN = 15  # DB mode: re-read weather_hourly this often (sim minutes)
PUBLISH_AHEAD_H = 72  # DB mode: synthetic weather published this far ahead

# --- thermostat ------------------------------------------------------------------------
HYSTERESIS_F = 0.5
MIN_RUN_MIN = 5
MIN_OFF_COOL_MIN = 5  # compressor protection
MIN_OFF_HEAT_MIN = 3
AWAY_AFTER_MIN = 30  # Smart Away: no occupancy for this long during 'home'
OCCUPIED_WINDOW_MIN = 30  # ecobee occupancy = motion within the last 30 minutes
STAGE2_AFTER_MIN = 10
STAGE2_COOL_MIN_OUT_F = 88.0
STAGE2_HEAT_MAX_OUT_F = 20.0
HEAT_COOL_MIN_DELTA_F = 3.0
ECOBEE_HEAT_RANGE = (45.0, 79.0)
ECOBEE_COOL_RANGE = (65.0, 92.0)
SNAPSHOT_SETTINGS: dict[str, object] = {"autoAway": True, "followMeComfort": False, "heatCoolMinDelta": 3.0}

# --- physics ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ZoneParams:
    capacitance: float  # BTU/°F (air + furnishings + drywall)
    ua_out: float  # BTU/h per °F to outdoors
    solar: float  # BTU/h per W/m² of horizontal shortwave
    cool_cap: float  # sensible cooling BTU/h at 82°F outdoor
    heat_cap: float  # furnace output BTU/h
    base_gain: float  # BTU/h always present (appliances, electronics)
    model: str  # ecobee modelNumber reported in snapshots
    thermostat_name: str


ZONES: tuple[ZoneParams, ...] = (
    ZoneParams(4000.0, 420.0, 6.0, 27000.0, 60000.0, 600.0, "apolloSmart", "Hallway"),  # main
    ZoneParams(3000.0, 260.0, 8.5, 18000.0, 40000.0, 250.0, "attisRetail", "Toy Room"),  # up
    ZoneParams(3000.0, 280.0, 5.5, 18000.0, 40000.0, 250.0, "apolloSmart", "Bedroom"),  # bed
)
UA_MAIN_UP = 220.0  # floor + open stairwell conduction/mixing, BTU/h per °F
K_STACK = 900.0  # extra stairwell stack flow when the main floor is warmer, BTU/h per °F
UA_MAIN_BED = 40.0  # weak link to the wing
ATTIC_LAG_H = 1.5
COOL_DERATE_PER_F = 0.012  # capacity lost per °F above 82°F outdoor
DT_H = 1.0 / 60.0

# Humidity (dewpoint state per zone, °F)
DP_INFILTRATION_H = 8.0
DP_MOISTURE_F_PER_H = 1.2
DP_REMOVAL_F_PER_H = 4.0

UNIT_KEYS: tuple[str, ...] = tuple(u.key for u in UNITS)
UNIT_INDEX = {k: i for i, k in enumerate(UNIT_KEYS)}
MAIN, UP, BED = UNIT_INDEX["main"], UNIT_INDEX["up"], UNIT_INDEX["bed"]

# --- rooms, occupancy, sensor offsets --------------------------------------------------
NONE, ACTIVE, ASLEEP = 0, 1, 2
MOTION_P = {NONE: 0.0, ACTIVE: 0.85, ASLEEP: 0.06}  # chance of motion within a 5-min slot
HALLWAY_BUSY_P = 0.35  # someone active elsewhere on the main floor
HALLWAY_TRANSIT_P = 0.6  # a room on the main floor or upstairs just filled or emptied
HALLWAY_STRAY_P = 0.01

ROOM_GAIN_ACTIVE = {  # BTU/h while a room is in use (people, lights, screens, cooking)
    "school_room": 900.0,
    "living_room": 700.0,
    "kitchen": 1500.0,
    "toy_room": 600.0,
    "girls_room": 400.0,
    "bedroom": 400.0,
    "office": 700.0,
}
ROOM_GAIN_ASLEEP = {"twins_room": 400.0, "olive_room": 250.0, "girls_room": 400.0, "bedroom": 450.0}
MAIN_FLOOR_ACTIVITY_ROOMS = ("school_room", "living_room", "kitchen")
TRANSIT_ROOMS = ("school_room", "living_room", "kitchen", "toy_room", "girls_room")


@dataclass(frozen=True)
class OffsetSpec:
    """A sensor's offset from its zone's mass temperature (°F)."""

    base: float = 0.0
    morning: float = 0.0  # × east sun (peaks ~09:30 local)
    afternoon: float = 0.0  # × west sun (peaks ~16:00 local)
    evening: float = 0.0  # stored heat after a warm day (peaks ~19:30 local)
    occupied: float = 0.0  # while the room is in use
    night: float = 0.0  # 00:00-06:00 local
    noise: float = 0.12  # slow noise amplitude


OFFSETS: dict[str, OffsetSpec] = {
    "main.hallway_tstat": OffsetSpec(night=-0.2),
    "main.school_room": OffsetSpec(base=-0.3, morning=1.0, occupied=0.2),
    "main.living_room": OffsetSpec(base=0.2, afternoon=1.0, occupied=0.2),
    "main.kitchen": OffsetSpec(base=0.3, occupied=0.9),
    "up.toy_room_tstat": OffsetSpec(noise=0.08),
    "up.toy_room": OffsetSpec(base=0.3, afternoon=0.5, occupied=0.2),
    "up.girls_room": OffsetSpec(base=0.4, afternoon=0.3, evening=1.2),
    "bed.bedroom_tstat": OffsetSpec(base=-0.2, night=-0.2, noise=0.08),
    "bed.office": OffsetSpec(base=0.4, afternoon=1.5, occupied=0.6),
}

# hash streams
_S_MOTION, _S_MOTION_MIN, _S_NOISE, _S_WHITE, _S_PLAN = 1, 2, 3, 4, 5
_S_WX_PHASE, _S_WX_T, _S_WX_C, _S_WX_C2, _S_WX_D, _S_WX_W, _S_WX_P = 10, 11, 12, 13, 14, 15, 16

DEFAULT_LAT = 38.6  # mid-US when the owner has not set a location

# Static lookups derived from the house inventory.
SENSOR_KEYS: tuple[str, ...] = tuple(s.key for s in SENSORS)
SENSOR_UNIT: tuple[int, ...] = tuple(UNIT_INDEX[s.unit_key] for s in SENSORS)
UNIT_SENSORS: tuple[tuple[int, ...], ...] = tuple(
    tuple(i for i, s in enumerate(SENSORS) if s.unit_key == k) for k in UNIT_KEYS
)
OCC_SENSORS: tuple[int, ...] = tuple(i for i, s in enumerate(SENSORS) if s.has_occupancy)
THERMOSTAT_SENSOR: tuple[int, ...] = tuple(
    next(i for i in UNIT_SENSORS[u] if SENSORS[i].kind == "thermostat") for u in range(len(UNIT_KEYS))
)
UNIT_SLEEP_ROOMS: tuple[tuple[str, ...], ...] = tuple(
    tuple(r.key for r in ROOMS if r.unit_key == k and r.is_sleep_room) for k in UNIT_KEYS
)
SLEEP_ROOMS: tuple[str, ...] = tuple(r.key for r in ROOMS if r.is_sleep_room)
PRESENCE_ROOMS: tuple[str, ...] = tuple(dict.fromkeys([*ROOM_GAIN_ACTIVE, *ROOM_GAIN_ASLEEP]))
ROOM_UNIT = {r.key: UNIT_INDEX[r.unit_key] for r in ROOMS}


def _sensor_sets() -> tuple[dict[str, tuple[int, ...]], ...]:
    """Participating sensors per comfort setting: home/away = all of the unit's sensors;
    sleep = the sensors in the unit's sleep rooms (+ the Hallway thermostat on the main
    floor, whose bedrooms have no sensor)."""
    out = []
    for u, key in enumerate(UNIT_KEYS):
        all_s = UNIT_SENSORS[u]
        sleep = [i for i in all_s if SENSORS[i].room_key in UNIT_SLEEP_ROOMS[u]]
        if key == "main" or not sleep:
            sleep = [THERMOSTAT_SENSOR[u], *[i for i in sleep if i != THERMOSTAT_SENSOR[u]]]
        out.append({"home": all_s, "sleep": tuple(sleep), "away": all_s})
    return tuple(out)


SENSOR_SETS = _sensor_sets()

# --- deterministic hashing -------------------------------------------------------------
_M64 = 0xFFFFFFFFFFFFFFFF


def _mix64(x: int) -> int:
    x = (x + 0x9E3779B97F4A7C15) & _M64
    x = ((x ^ (x >> 30)) * 0xBF58476D1CE4E5B9) & _M64
    x = ((x ^ (x >> 27)) * 0x94D049BB133111EB) & _M64
    return x ^ (x >> 31)


def _u01(*keys: int) -> float:
    """Uniform [0, 1) from integer keys: multiply-add fold, then the splitmix64 finalizer."""
    h = 0x6A09E667F3BCC909
    for k in keys:
        h = (h * 0x100000001B3 + (k & _M64)) & _M64
    h = _mix64(h)
    return (h >> 11) * (1.0 / 9007199254740992.0)


def _vnoise(seed: int, stream: int, x: float) -> float:
    """Smooth value noise in [-1, 1] with knots at integer x."""
    i = math.floor(x)
    f = x - i
    a = 2.0 * _u01(seed, stream, i) - 1.0
    b = 2.0 * _u01(seed, stream, i + 1) - 1.0
    t = f * f * (3.0 - 2.0 * f)
    return a + (b - a) * t


def _rh_from(temp_f: float, dewpoint_f: float) -> float:
    """Relative humidity (%) from temperature and dewpoint (Magnus formula)."""
    t = (temp_f - 32.0) / 1.8
    d = (dewpoint_f - 32.0) / 1.8
    rh = 100.0 * math.exp(17.625 * d / (243.04 + d) - 17.625 * t / (243.04 + t))
    return max(1.0, min(100.0, rh))


def _dewpoint_from(temp_f: float, rh: float) -> float:
    t = (temp_f - 32.0) / 1.8
    g = math.log(max(1.0, min(100.0, rh)) / 100.0) + 17.625 * t / (243.04 + t)
    d = 243.04 * g / (17.625 - g)
    return d * 1.8 + 32.0


def _bump(x: float, center: float, width: float) -> float:
    """Raised-cosine bump: 1 at center, 0 beyond ±width."""
    d = abs(x - center)
    return 0.0 if d >= width else 0.5 * (1.0 + math.cos(math.pi * d / width))


def _minute_of(ts: datetime) -> int:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return math.floor(ts.timestamp() / 60.0)


def _dt(m: float) -> datetime:
    return datetime.fromtimestamp(m * 60.0, UTC)


def _iso(m: int | None) -> str | None:
    return None if m is None else _dt(m).isoformat()


# --- synthetic weather -----------------------------------------------------------------

WxTuple = tuple[float, float, float, float, float, float, float]  # temp, rh, dp, cloud, sw, wind, precip


class SyntheticClimate:
    """Deterministic hourly weather for a latitude/longitude: seasonal cycle by day of year,
    a diurnal swing peaking mid-afternoon (solar time), multi-day fronts, cloud cover and
    clear-sky shortwave (Haurwitz) dimmed by clouds (Kasten-Czeplak)."""

    def __init__(self, seed: int, lat: float, lon: float) -> None:
        self.seed = seed
        self.lat = lat
        self.lon = lon
        self._phase = [2.0 * math.pi * _u01(seed, _S_WX_PHASE, k) for k in range(4)]
        alat = abs(lat)
        self._mean = 57.0 - 1.1 * (alat - 38.0)
        self._amp = min(35.0, max(6.0, 22.0 + 0.5 * (alat - 38.0)))
        self._hemi = 1.0 if lat >= 0 else -1.0

    def hour(self, h: int) -> WxTuple:
        """Weather at the start of epoch hour ``h`` (UTC)."""
        seed, ph = self.seed, self._phase
        when = datetime.fromtimestamp(h * 3600, UTC)
        doy = when.timetuple().tm_yday + when.hour / 24.0
        day = h / 24.0
        season = self._hemi * math.cos(2.0 * math.pi * (doy - 200.0) / 365.25)
        front = (
            4.5 * math.sin(2.0 * math.pi * day / 6.3 + ph[0])
            + 2.5 * math.sin(2.0 * math.pi * day / 10.9 + ph[1])
            + 1.5 * math.sin(2.0 * math.pi * day / 3.7 + ph[2])
            + 3.0 * _vnoise(seed, _S_WX_T, day / 1.5)
        )
        cloud = (
            42.0
            + 30.0 * math.sin(2.0 * math.pi * day / 6.3 + ph[0] + 1.9)
            + 25.0 * _vnoise(seed, _S_WX_C, day * 1.3)
            + 12.0 * _vnoise(seed, _S_WX_C2, h / 5.0)
        )
        cloud = min(100.0, max(0.0, cloud))
        solar_hour = (when.hour + self.lon / 15.0) % 24.0
        swing = (19.0 + 4.0 * season) * (1.0 - 0.45 * cloud / 100.0)
        daily_mean = self._mean + self._amp * season + front
        temp = daily_mean + 0.5 * swing * math.cos(2.0 * math.pi * (solar_hour - 15.0) / 24.0)
        dp = (
            daily_mean
            - (10.0 - 2.0 * season)
            + 4.0 * _vnoise(seed, _S_WX_D, day)
            + 3.0 * (cloud / 50.0 - 1.0)
        )
        dp = min(dp, temp - 0.5)
        sin_e = self._sin_elevation(doy, solar_hour)
        if sin_e > 0.01:
            clear = 1098.0 * sin_e * math.exp(-0.057 / sin_e)
            sw = clear * (1.0 - 0.75 * (cloud / 100.0) ** 3.4)
        else:
            sw = 0.0
        wind = max(0.0, 7.0 + 4.0 * _vnoise(seed, _S_WX_W, h / 9.0) + 3.0 * abs(front) / 8.0)
        wet = _vnoise(seed, _S_WX_P, h / 3.0)
        precip = 0.08 * (cloud - 85.0) / 15.0 * wet if cloud > 85.0 and wet > 0.0 else 0.0
        return (
            round(temp, 1),
            round(_rh_from(temp, dp), 0),
            round(dp, 1),
            round(cloud, 0),
            round(sw, 0),
            round(wind, 1),
            round(precip, 3),
        )

    def _sin_elevation(self, doy: float, solar_hour: float) -> float:
        decl = math.radians(23.44) * math.sin(2.0 * math.pi * (284.0 + doy) / 365.0)
        lat = math.radians(self.lat)
        hour_angle = math.radians(15.0 * (solar_hour - 12.0))
        return math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(decl) * math.cos(hour_angle)


def default_longitude(tz: str) -> float:
    """A longitude whose solar noon matches the zone's standard time (UTC offset × 15°)."""
    try:
        offset = datetime(2026, 1, 15, 12, tzinfo=ZoneInfo(tz)).utcoffset()
    except (KeyError, ValueError):
        offset = None
    hours = offset.total_seconds() / 3600.0 if offset is not None else -6.0
    return max(-180.0, min(180.0, hours * 15.0))


def _wx_hour_in(h: int, wx: WxTuple, kind: str) -> WeatherHourIn:
    return WeatherHourIn(
        ts=datetime.fromtimestamp(h * 3600, UTC),
        kind=kind,
        temp_f=wx[0],
        rh=wx[1],
        dewpoint_f=wx[2],
        cloud_cover=wx[3],
        shortwave_wm2=wx[4],
        wind_mph=wx[5],
        precip_in=wx[6],
    )


def synthetic_weather(
    start: datetime,
    end: datetime,
    *,
    seed: int | None = None,
    location: LocationSettings | None = None,
    now: datetime | None = None,
) -> list[WeatherHourIn]:
    """Synthetic hours in [start, end) (source 'simulator'): 'observed' before ``now``
    (default: the current time), 'forecast' after. Seed defaults to ``CLIMATE_SIM_SEED`` and
    location to app_settings['location'] (the default mid-US climate when unset or when the
    database is unreachable), so the hours match what ``SimulatedHouse.from_settings`` runs on."""
    if seed is None:
        from climate.config import get_settings

        seed = get_settings().sim_seed
    loc = location or _configured_location()
    lat = loc.lat if loc.lat is not None else DEFAULT_LAT
    lon = loc.lon if loc.lon is not None else default_longitude(loc.tz)
    return _synthetic_hours(SyntheticClimate(seed, lat, lon), start, end, now or utcnow())


def _configured_location() -> LocationSettings:
    from sqlalchemy.exc import SQLAlchemyError

    from climate.store.app_settings import get_setting
    from climate.store.db import session_scope

    try:
        with session_scope() as s:
            return get_setting(s, "location", LocationSettings)
    except SQLAlchemyError as exc:
        log.info("simulator: location unavailable (%s); using the default climate", type(exc).__name__)
        return LocationSettings()


def _synthetic_hours(
    climate: SyntheticClimate, start: datetime, end: datetime, now: datetime
) -> list[WeatherHourIn]:
    h0 = math.floor(_minute_of(start) / 60)
    h1 = math.ceil(_minute_of(end) / 60)
    now_h = _minute_of(now) / 60.0
    return [_wx_hour_in(h, climate.hour(h), "observed" if h < now_h else "forecast") for h in range(h0, h1)]


# --- per-slot context ------------------------------------------------------------------


@dataclass
class _Hold:
    heat_f: float
    cool_f: float
    start_m: int
    end_m: int
    reason: str = ""


@dataclass
class _SlotCtx:
    """Everything that is constant over one 5-minute slot (weather, occupancy, offsets)."""

    slot_m: int
    wx: WxTuple
    local: datetime
    program: tuple[str, ...]  # per unit: 'home' | 'sleep'
    gains: tuple[float, ...]  # per unit, BTU/h
    motion: tuple[tuple[int, int], ...]  # (sensor index, minute offset within slot)
    sensor_off: tuple[float, ...]  # per sensor, °F from zone mass temperature
    set_off: tuple[dict[str, float], ...]  # per unit: mean offset of each participating set
    room_level: dict[str, int]


Plan = dict[str, list[tuple[int, int]]]


class SimulatedHouse:
    """ThermostatSource for kind='simulator'. See the module docstring."""

    kind = "simulator"

    def __init__(
        self,
        *,
        seed: int = 7,
        control: ControlSettings | None = None,
        occupancy: OccupancySettings | None = None,
        location: LocationSettings | None = None,
        clock: Callable[[], datetime] = utcnow,
        use_db: bool = False,
        weather_hours: Iterable[WeatherHourIn] | None = None,
    ) -> None:
        self.seed = int(seed)
        self._clock = clock
        self._use_db = use_db
        self.control = ControlSettings()
        self.occupancy = OccupancySettings()
        self.location = LocationSettings()
        self._tz = ZoneInfo(self.location.tz)
        self._climate = SyntheticClimate(self.seed, DEFAULT_LAT, default_longitude(self.location.tz))
        self._bands: list[dict[str, tuple[float, float]]] = []
        self._plan_cache: dict[date, Plan] = {}
        self._synth_cache: dict[int, WxTuple] = {}
        self._real_wx: dict[int, tuple[float | None, ...]] = {}
        self._ctx: _SlotCtx | None = None
        self._apply_settings(
            control or ControlSettings(), occupancy or OccupancySettings(), location or LocationSettings()
        )
        if weather_hours is not None:
            self.set_weather(weather_hours)

        # dynamic state (initialised by _reset / _restore)
        self._m: int | None = None
        n = len(UNIT_KEYS)
        self._T = [74.0] * n
        self._dp = [55.0] * n
        self._sun_lag = 0.0
        self._call = [0] * n  # +1 cooling, -1 heating, 0 off
        self._run = [0] * n
        self._off = [999] * n
        self._stage2 = [False] * n
        self._away = [False] * n
        self._away_since: list[int | None] = [None] * n
        self._home_since = [0] * n
        self._program = ["home"] * n
        self._unit_motion: list[int | None] = [None] * n
        self._sensor_motion: list[int | None] = [None] * len(SENSORS)
        self._hold: list[_Hold | None] = [None] * n
        self._rev = [0] * n
        self._acc: list[list[Any]] = [self._new_acc() for _ in range(n)]
        self._slots: list[RuntimeInterval] = []
        self._ctx = None
        self._last_persist_m: int | None = None
        self._last_settings_m: int | None = None
        self._last_weather_m: int | None = None
        self._weather_range: tuple[int, int] | None = None
        self._last_publish_h: int | None = None

    # ===================================================================================
    # construction / persistence
    # ===================================================================================

    @classmethod
    def from_settings(cls, clock: Callable[[], datetime] = utcnow) -> SimulatedHouse:
        """Build from app_settings (control, occupancy, location) and restore
        app_settings['simulator_state'] when it matches the configured seed."""
        from climate.config import get_settings
        from climate.store.app_settings import get_raw, get_setting
        from climate.store.db import session_scope

        with session_scope() as s:
            control = get_setting(s, "control", ControlSettings)
            occupancy = get_setting(s, "occupancy", OccupancySettings)
            location = get_setting(s, "location", LocationSettings)
            raw = get_raw(s, STATE_KEY)
        house = cls(
            seed=get_settings().sim_seed,
            control=control,
            occupancy=occupancy,
            location=location,
            clock=clock,
            use_db=True,
        )
        if raw is not None and not house._restore(raw):
            log.info("simulator: saved state unusable (seed or version changed); starting fresh")
        return house

    @property
    def now(self) -> datetime | None:
        """The simulation clock (UTC, minute-aligned), or None before the first step."""
        return None if self._m is None else _dt(self._m)

    def set_weather(self, hours: Iterable[WeatherHourIn]) -> None:
        """Use these hours (real or test weather) in place of the synthetic weather.
        Observed rows win over forecast rows for the same hour."""
        best: dict[int, tuple[int, tuple[float | None, ...]]] = {}
        for w in hours:
            h = math.floor(_minute_of(w.ts) / 60)
            rank = 0 if w.kind == "observed" else 1
            vals = (w.temp_f, w.rh, w.dewpoint_f, w.cloud_cover, w.shortwave_wm2, w.wind_mph, w.precip_in)
            if h not in best or rank <= best[h][0]:
                best[h] = (rank, vals)
        self._real_wx.update({h: v for h, (_, v) in best.items()})
        self._ctx = None

    def _apply_settings(
        self, control: ControlSettings, occupancy: OccupancySettings, location: LocationSettings
    ) -> bool:
        """Install settings; returns True when anything that shapes the simulation changed."""
        changed = (
            control.model_dump() != self.control.model_dump()
            or occupancy.model_dump(exclude={"phones_away", "phones_updated_at"})
            != self.occupancy.model_dump(exclude={"phones_away", "phones_updated_at"})
            or not self._bands
        )
        loc_changed = (location.lat, location.lon, location.tz) != (
            self.location.lat,
            self.location.lon,
            self.location.tz,
        )
        self.control, self.occupancy, self.location = control, occupancy, location
        if loc_changed or not self._bands:
            try:
                self._tz = ZoneInfo(location.tz)
            except (KeyError, ValueError):
                self._tz = ZoneInfo("America/Chicago")
            lat = location.lat if location.lat is not None else DEFAULT_LAT
            lon = location.lon if location.lon is not None else default_longitude(location.tz)
            self._climate = SyntheticClimate(self.seed, lat, lon)
            self._synth_cache.clear()
        bands = []
        for key in UNIT_KEYS:
            c = control.comfort.get(key) or DEFAULT_COMFORT[key]
            bands.append(
                {
                    "home": (c.day.heat_f, c.day.cool_f),
                    "sleep": (c.night.heat_f, c.night.cool_f),
                    "away": (c.away.heat_f, c.away.cool_f),
                }
            )
        self._bands = bands
        if changed or loc_changed:
            self._plan_cache.clear()
            self._ctx = None
        return changed or loc_changed

    @staticmethod
    def _new_acc() -> list[Any]:
        # cool1, cool2, heat1, heat2, fan, sum(avg), sum(mass), minutes, heat_sp, cool_sp, climate
        return [0, 0, 0, 0, 0, 0.0, 0.0, 0, None, None, None]

    def _to_state(self) -> dict[str, Any]:
        cutoff = (self._m or 0) - PERSIST_SLOTS_MIN
        return {
            "version": STATE_VERSION,
            "seed": self.seed,
            "m": self._m,
            "ts": _iso(self._m),
            # full precision so a restored house continues exactly where it stopped
            "zones": {k: self._T[i] for i, k in enumerate(UNIT_KEYS)},
            "dewpoints": {k: self._dp[i] for i, k in enumerate(UNIT_KEYS)},
            "sun_lag": self._sun_lag,
            "units": {
                k: {
                    "call": self._call[i],
                    "run": self._run[i],
                    "off": self._off[i],
                    "stage2": self._stage2[i],
                    "away": self._away[i],
                    "away_since": self._away_since[i],
                    "home_since": self._home_since[i],
                    "program": self._program[i],
                    "unit_motion": self._unit_motion[i],
                    "rev": self._rev[i],
                    "hold": None if self._hold[i] is None else vars(self._hold[i]).copy(),
                    "acc": self._acc[i],
                }
                for i, k in enumerate(UNIT_KEYS)
            },
            "sensor_motion": {k: self._sensor_motion[i] for i, k in enumerate(SENSOR_KEYS)},
            "slots": [s.model_dump(mode="json") for s in self._slots if _minute_of(s.ts) >= cutoff],
        }

    def _restore(self, raw: Any) -> bool:
        """Load a saved state; returns False (and leaves a fresh house) if it does not fit."""
        try:
            if (
                not isinstance(raw, dict)
                or raw.get("version") != STATE_VERSION
                or raw.get("seed") != self.seed
            ):
                return False
            m = int(raw["m"])
            zones, dps, units = raw["zones"], raw["dewpoints"], raw["units"]
            T = [float(zones[k]) for k in UNIT_KEYS]
            dp = [float(dps[k]) for k in UNIT_KEYS]
            per = [units[k] for k in UNIT_KEYS]
            holds = [None if p.get("hold") is None else _Hold(**p["hold"]) for p in per]
            accs = [list(p["acc"]) for p in per]
            if any(len(a) != len(self._new_acc()) for a in accs):
                return False
            slots = [RuntimeInterval.model_validate(s) for s in raw.get("slots", [])]
            motion = raw.get("sensor_motion") or {}
            sensor_motion = [None if motion.get(k) is None else int(motion[k]) for k in SENSOR_KEYS]
            sun_lag = float(raw.get("sun_lag", 0.0))
            call = [int(p["call"]) for p in per]
            run = [int(p["run"]) for p in per]
            off = [int(p["off"]) for p in per]
            stage2 = [bool(p["stage2"]) for p in per]
            away = [bool(p["away"]) for p in per]
            away_since = [None if p.get("away_since") is None else int(p["away_since"]) for p in per]
            home_since = [int(p["home_since"]) for p in per]
            program = [str(p["program"]) for p in per]
            unit_motion = [None if p.get("unit_motion") is None else int(p["unit_motion"]) for p in per]
            rev = [int(p.get("rev", 0)) for p in per]
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            log.info("simulator: cannot restore state (%s)", type(exc).__name__)
            return False
        self._m = m
        self._T, self._dp, self._sun_lag = T, dp, sun_lag
        self._call, self._run, self._off, self._stage2 = call, run, off, stage2
        self._away, self._away_since, self._home_since = away, away_since, home_since
        self._program, self._unit_motion, self._rev = program, unit_motion, rev
        self._hold = holds
        self._acc = accs
        self._sensor_motion = sensor_motion
        self._slots = slots
        self._ctx = None
        self._last_persist_m = m
        return True

    # ===================================================================================
    # weather
    # ===================================================================================

    def _synth(self, h: int) -> WxTuple:
        wx = self._synth_cache.get(h)
        if wx is None:
            if len(self._synth_cache) > 20000:
                self._synth_cache.clear()
            wx = self._synth_cache[h] = self._climate.hour(h)
        return wx

    def _hour_wx(self, h: int) -> WxTuple:
        syn = self._synth(h)
        real = self._real_wx.get(h)
        if real is None:
            return syn
        vals = [r if r is not None else s for r, s in zip(real, syn, strict=True)]
        if real[1] is None and real[0] is not None and real[2] is not None:
            vals[1] = _rh_from(real[0], real[2])
        if real[2] is None and real[0] is not None and real[1] is not None:
            vals[2] = _dewpoint_from(real[0], real[1])
        return tuple(vals)  # type: ignore[return-value]

    def _wx_at(self, minute: float) -> WxTuple:
        """Weather at a minute, interpolated linearly between hourly values."""
        hf = minute / 60.0
        h0 = math.floor(hf)
        f = hf - h0
        a, b = self._hour_wx(h0), self._hour_wx(h0 + 1)
        return tuple(x + (y - x) * f for x, y in zip(a, b, strict=True))  # type: ignore[return-value]

    def synthetic_weather(
        self, start: datetime, end: datetime, now: datetime | None = None
    ) -> list[WeatherHourIn]:
        """This house's synthetic hours in [start, end) for storage with source 'simulator'
        ('observed' before ``now``, default the simulation clock or wall clock)."""
        ref = now or (self.now if self._m is not None else utcnow())
        return _synthetic_hours(self._climate, start, end, ref)

    # ===================================================================================
    # occupancy plan
    # ===================================================================================

    def _day_plan(self, d: date) -> Plan:
        """Seeded 'in use' blocks per room for one local day: [start_min, end_min)."""
        plan = self._plan_cache.get(d)
        if plan is not None:
            return plan
        if len(self._plan_cache) > 120:
            self._plan_cache.clear()
        sched = self.control.schedule
        wd, dn, seed = d.weekday(), d.toordinal(), self.seed

        def r(k: int) -> float:
            return _u01(seed, _S_PLAN, dn, k)

        def jit(k: int, amp: int = 15) -> int:
            return round((r(k) - 0.5) * 2 * amp)

        def hm(s: str) -> int:
            t = parse_hhmm(s)
            return t.hour * 60 + t.minute

        plan = {}

        def add(room: str, s: int, e: int) -> None:
            s, e = max(0, s), min(1440, e)
            if e > s:
                plan.setdefault(room, []).append((s, e))

        school = wd in sched.school_days and r(1) >= 0.04  # ~4% of school days are holidays
        office = wd in sched.office_days and r(2) >= 0.08
        add("kitchen", 420 + jit(10), 480 + jit(11))
        add("kitchen", 720 + jit(12, 10), 780 + jit(13, 10))
        add("kitchen", 1020 + jit(14), 1170 + jit(15))
        add("living_room", 1020 + jit(16), 1290 + jit(17))
        if school:
            end = hm(sched.school_end) + jit(21, 20)
            add("school_room", hm(sched.school_start) + jit(20, 10), 720)
            add("school_room", 765, end)  # lunch in the kitchen
            add("toy_room", end + 10 + jit(22, 10), 1140 + jit(23))
        else:
            if r(30) < 0.8:
                add("living_room", 570 + jit(31), 720 + jit(32))
            if r(33) < 0.5:
                add("living_room", 780 + jit(34), 960 + jit(35))
            add("toy_room", 540 + jit(36), 720)
            add("toy_room", 780 + jit(37), 1140 + jit(38))
        if office:
            add("office", hm(sched.office_start) + jit(40), 720)
            add("office", 750 + jit(41, 10), hm(sched.office_end) + jit(42, 30))
        for room in ("girls_room", "bedroom"):
            for w in self.occupancy.sleep_windows.get(room, []):
                s, e = hm(w.start), hm(w.end)
                if wd in w.days:
                    add(room, s - 30, s)  # getting ready for bed
                woke_today = ((wd - 1) % 7 in w.days) if e <= s else (wd in w.days)
                if woke_today:
                    add(room, e, e + 30)
        self._plan_cache[d] = plan
        return plan

    def _asleep(self, room: str, local: datetime) -> bool:
        return any(
            in_window(local, w.start, w.end, w.days) for w in self.occupancy.sleep_windows.get(room, [])
        )

    # ===================================================================================
    # slot context
    # ===================================================================================

    def _ensure_ctx(self) -> _SlotCtx:
        assert self._m is not None
        slot_m = self._m - self._m % SLOT_MIN
        ctx = self._ctx
        if ctx is None or ctx.slot_m != slot_m:
            ctx = self._ctx = self._slot_context(slot_m)
        return ctx

    def _slot_context(self, slot_m: int) -> _SlotCtx:
        seed = self.seed
        wx = self._wx_at(slot_m + SLOT_MIN / 2)
        local = _dt(slot_m).astimezone(self._tz)
        mod = local.hour * 60 + local.minute
        hour = mod / 60.0
        plan = self._day_plan(local.date())

        asleep = {room: self._asleep(room, local) for room in SLEEP_ROOMS}
        level: dict[str, int] = {}
        for room in PRESENCE_ROOMS:
            lv = ASLEEP if asleep.get(room) else NONE
            for s, e in plan.get(room, ()):
                if s <= mod < e:
                    lv = ACTIVE
                    break
            level[room] = lv

        gains = [z.base_gain for z in ZONES]
        for room, lv in level.items():
            if lv == ACTIVE:
                gains[ROOM_UNIT[room]] += ROOM_GAIN_ACTIVE.get(room, 0.0)
            elif lv == ASLEEP:
                gains[ROOM_UNIT[room]] += ROOM_GAIN_ASLEEP.get(room, 0.0)

        # motion
        main_busy = any(level.get(r) == ACTIVE for r in MAIN_FLOOR_ACTIVITY_ROOMS)
        transit = any(
            abs(s - mod) < 10 or abs(e - mod) < 10 for r in TRANSIT_ROOMS for s, e in plan.get(r, ())
        )
        motion = []
        for si in OCC_SENSORS:
            room = SENSORS[si].room_key
            if room == "hallway":
                p = HALLWAY_TRANSIT_P if transit else HALLWAY_BUSY_P if main_busy else HALLWAY_STRAY_P
            else:
                p = MOTION_P[level.get(room, NONE)]
            if p > 0.0 and _u01(seed, _S_MOTION, si, slot_m) < p:
                motion.append((si, int(_u01(seed, _S_MOTION_MIN, si, slot_m) * SLOT_MIN)))

        # sensor offsets
        sun = min(1.0, wx[4] / 800.0)
        warm_day = min(1.0, max(0.0, (wx[0] - 60.0) / 30.0))
        morning = _bump(hour, 9.5, 3.0) * sun
        afternoon = _bump(hour, 16.0, 3.5) * sun
        evening = _bump(hour, 19.5, 2.5) * warm_day
        night = 1.0 if hour < 6.0 else 0.0
        offs = []
        for si, key in enumerate(SENSOR_KEYS):
            spec = OFFSETS.get(key, OffsetSpec())
            room = SENSORS[si].room_key
            o = (
                spec.base
                + spec.morning * morning
                + spec.afternoon * afternoon
                + spec.evening * evening
                + spec.night * night
                + (spec.occupied if level.get(room) == ACTIVE else 0.0)
                + spec.noise * _vnoise(seed, _S_NOISE * 100 + si, slot_m / 30.0)
                + 0.04 * (_u01(seed, _S_WHITE, si, slot_m) - 0.5)
            )
            offs.append(o)
        set_off = tuple(
            {name: sum(offs[i] for i in members) / len(members) for name, members in SENSOR_SETS[u].items()}
            for u in range(len(UNIT_KEYS))
        )
        program = tuple(
            "sleep" if any(asleep[r] for r in UNIT_SLEEP_ROOMS[u]) else "home" for u in range(len(UNIT_KEYS))
        )
        return _SlotCtx(slot_m, wx, local, program, tuple(gains), tuple(motion), tuple(offs), set_off, level)

    # ===================================================================================
    # thermostat logic
    # ===================================================================================

    def _resolve(self, u: int, m: int, program: str) -> tuple[float, float, str, str, bool]:
        """(heat_sp, cool_sp, climate_ref, participating set, smart_away) for unit u at m."""
        hold = self._hold[u]
        if hold is not None and m < hold.end_m:
            return hold.heat_f, hold.cool_f, program, "home", False
        away = False
        if program == "home":
            last = self._home_since[u]
            lm = self._unit_motion[u]
            if lm is not None and lm > last:
                last = lm
            away = m - last >= AWAY_AFTER_MIN
        climate = "away" if away else program
        heat, cool = self._bands[u][climate]
        return heat, cool, climate, climate, away

    def _step(self) -> None:
        """Advance the house by one minute."""
        m = self._m
        assert m is not None
        ctx = self._ensure_ctx()
        offset = m - ctx.slot_m
        for si, mm in ctx.motion:
            if mm == offset:
                self._sensor_motion[si] = m
                self._unit_motion[SENSOR_UNIT[si]] = m
        t_out, sw = ctx.wx[0], ctx.wx[4]
        T = self._T
        q = [0.0, 0.0, 0.0]
        for u in range(3):
            hold = self._hold[u]
            if hold is not None and m >= hold.end_m:
                self._hold[u] = None
                self._home_since[u] = m
                self._rev[u] += 1
            prog = ctx.program[u]
            if prog != self._program[u]:
                self._program[u] = prog
                self._rev[u] += 1
                if prog == "home":
                    self._home_since[u] = m
            heat, cool, climate, sset, away = self._resolve(u, m, prog)
            if away != self._away[u]:
                self._away[u] = away
                self._away_since[u] = m if away else None
                self._rev[u] += 1
            avg = T[u] + ctx.set_off[u][sset]

            prev = self._call[u]
            call = prev
            if prev == 1:
                if self._run[u] >= MIN_RUN_MIN and avg <= cool - HYSTERESIS_F:
                    call = 0
            elif prev == -1:
                if self._run[u] >= MIN_RUN_MIN and avg >= heat + HYSTERESIS_F:
                    call = 0
            elif avg >= cool + HYSTERESIS_F and self._off[u] >= MIN_OFF_COOL_MIN:
                call = 1
            elif avg <= heat - HYSTERESIS_F and self._off[u] >= MIN_OFF_HEAT_MIN:
                call = -1
            if call != prev:
                self._call[u] = call
                if call == 0:
                    self._off[u] = 0
                else:
                    self._run[u] = 0
            acc = self._acc[u]
            if call == 0:
                self._off[u] += 1
                self._stage2[u] = False
            else:
                self._run[u] += 1
                z = ZONES[u]
                if call == 1:
                    cap = z.cool_cap * min(1.15, max(0.55, 1.0 - COOL_DERATE_PER_F * (t_out - 82.0)))
                    q[u] = cap
                    stage2 = self._run[u] > STAGE2_AFTER_MIN and t_out >= STAGE2_COOL_MIN_OUT_F
                    acc[0] += 60
                    if stage2:
                        acc[1] += 60
                else:
                    q[u] = -z.heat_cap
                    stage2 = self._run[u] > STAGE2_AFTER_MIN and t_out <= STAGE2_HEAT_MAX_OUT_F
                    acc[2] += 60
                    if stage2:
                        acc[3] += 60
                self._stage2[u] = stage2
                acc[4] += 60
            acc[5] += avg
            acc[6] += T[u]
            acc[7] += 1
            acc[8], acc[9], acc[10] = heat, cool, climate

        # physics
        self._sun_lag += (sw - self._sun_lag) * (DT_H / ATTIC_LAG_H)
        tm, tu, tb = T[MAIN], T[UP], T[BED]
        d_mu = tm - tu
        q_mu = UA_MAIN_UP * d_mu + (K_STACK * d_mu if d_mu > 0.0 else 0.0)
        q_mb = UA_MAIN_BED * (tm - tb)
        zm, zu, zb = ZONES[MAIN], ZONES[UP], ZONES[BED]
        g = ctx.gains
        f_main = zm.ua_out * (t_out - tm) - q_mu - q_mb + zm.solar * sw + g[MAIN] - q[MAIN]
        f_up = zu.ua_out * (t_out - tu) + q_mu + zu.solar * self._sun_lag + g[UP] - q[UP]
        f_bed = zb.ua_out * (t_out - tb) + q_mb + zb.solar * sw + g[BED] - q[BED]
        T[MAIN] = tm + f_main / zm.capacitance * DT_H
        T[UP] = tu + f_up / zu.capacitance * DT_H
        T[BED] = tb + f_bed / zb.capacitance * DT_H
        self._m = m + 1

    def _end_slot(self, record: bool) -> None:
        """Close the slot that just ended at self._m (append its intervals when recording)."""
        ctx = self._ctx
        assert ctx is not None and self._m is not None
        ts = _dt(ctx.slot_m)
        for u in range(3):
            acc = self._acc[u]
            n = acc[7]
            duty = acc[0] / (SLOT_MIN * 60.0) if n else 0.0
            # humidity: infiltration toward outdoor dewpoint, people add moisture, AC removes it
            occupied_rooms = sum(1 for r, lv in ctx.room_level.items() if lv and ROOM_UNIT[r] == u)
            dp = self._dp[u]
            ddp = (
                (ctx.wx[2] - dp) / DP_INFILTRATION_H
                + DP_MOISTURE_F_PER_H * min(1.0, 0.3 + 0.35 * occupied_rooms)
                - DP_REMOVAL_F_PER_H * duty * max(0.0, (dp - 38.0) / 20.0)
            )
            self._dp[u] = dp + ddp * (SLOT_MIN / 60.0)
            if record and n:
                mass = acc[6] / n
                zone = acc[5] / n
                interval = RuntimeInterval(
                    unit_key=UNIT_KEYS[u],
                    ts=ts,
                    comp_cool1=min(300, acc[0]),
                    comp_cool2=min(300, acc[1]),
                    aux_heat1=min(300, acc[2]),
                    aux_heat2=min(300, acc[3]),
                    fan=min(300, acc[4]),
                    hvac_mode="auto",
                    climate_ref=acc[10],
                    zone_temp_f=round(zone, 1),
                    zone_humidity=round(self._rh(u, zone)),
                    heat_sp_f=acc[8],
                    cool_sp_f=acc[9],
                    outdoor_temp_f=round(ctx.wx[0], 1),
                    outdoor_humidity=round(ctx.wx[1]),
                    sensor_temps={
                        SENSOR_KEYS[i]: round(mass + ctx.sensor_off[i], 1) for i in UNIT_SENSORS[u]
                    },
                    sensor_occupancy={SENSOR_KEYS[i]: self._occupied(i) for i in UNIT_SENSORS[u]},
                )
                self._slots.append(interval)
            self._acc[u] = self._new_acc()

    def _rh(self, u: int, temp_f: float) -> float:
        return max(15.0, min(85.0, _rh_from(temp_f, self._dp[u])))

    def _occupied(self, si: int) -> bool | None:
        if not SENSORS[si].has_occupancy:
            return None
        lm = self._sensor_motion[si]
        assert self._m is not None
        return lm is not None and self._m - lm < OCCUPIED_WINDOW_MIN

    # ===================================================================================
    # stepping
    # ===================================================================================

    def _run_until(self, end_m: int, record: bool, on_hour: Callable[[], None] | None = None) -> None:
        assert self._m is not None
        while self._m < end_m:
            self._step()
            if self._m % SLOT_MIN == 0:
                self._end_slot(record)
            if on_hour is not None and self._m % 60 == 0:
                on_hour()

    def _trim_slots(self) -> None:
        if self._m is None or not self._slots:
            return
        cutoff = _dt(self._m - BUFFER_MIN)
        if self._slots[0].ts < cutoff:
            self._slots = [s for s in self._slots if s.ts >= cutoff]

    def _reset(self, at_m: int, clear_holds: bool = False) -> None:
        """Settle the house into a plausible state at ``at_m`` (spin-up, nothing recorded)."""
        start = at_m - WARMUP_MIN
        if self._use_db:
            self._db_load_weather(start, at_m + 120)
        self._m = start
        self._ctx = None
        ctx = self._ensure_ctx()
        t_out = ctx.wx[0]
        for u in range(3):
            heat, cool = self._bands[u][ctx.program[u]]
            self._T[u] = min(cool - 0.5, max(heat + 0.5, t_out + 6.0))
            self._dp[u] = min(max(ctx.wx[2], 25.0), self._T[u] - 18.0)
            self._call[u], self._run[u], self._off[u] = 0, 0, 999
            self._stage2[u] = False
            self._away[u], self._away_since[u] = False, None
            self._home_since[u] = start
            self._program[u] = ctx.program[u]
            self._unit_motion[u] = None
            self._acc[u] = self._new_acc()
            hold = self._hold[u]
            if clear_holds or (hold is not None and hold.end_m <= at_m):
                self._hold[u] = None
        self._sensor_motion = [None] * len(SENSORS)
        self._sun_lag = ctx.wx[4]
        self._run_until(at_m, record=False)

    def advance_to(self, now: datetime) -> None:
        """Step the physics in 1-minute steps up to ``now`` (bounded catch-up).

        The first call (no saved state) or a gap over 6 hours resets the house to a settled
        state at ``now`` instead of simulating the whole gap."""
        now_m = _minute_of(now)
        if self._use_db:
            self._db_refresh(now_m)
        if self._m is None:
            self._reset(now_m)
        elif now_m <= self._m:
            return
        elif now_m - self._m > MAX_CATCHUP_MIN:
            log.info("simulator: %d-minute gap; resetting to steady state", now_m - self._m)
            self._reset(now_m)
        else:
            self._run_until(now_m, record=True)
            self._trim_slots()
        if self._use_db:
            self._db_after_advance()

    def generate_history(
        self, start: datetime, end: datetime
    ) -> tuple[list[RuntimeInterval], list[UnitSnapshot]]:
        """Fast synthetic history for backfill (5-minute slots; snapshots hourly).

        Resets the house at ``start`` (clearing holds), simulates to ``end`` and leaves the
        live state there, so later ``advance_to`` calls continue seamlessly (state saved in
        DB mode). Snapshots are taken at every top of the hour in [start, end]."""
        start_m = _minute_of(start)
        start_m -= start_m % SLOT_MIN
        end_m = _minute_of(end)
        if end_m <= start_m:
            return [], []
        if self._use_db:
            self._db_refresh(end_m, force=True)
            self._db_load_weather(start_m - WARMUP_MIN - 60, end_m + 120)
        self._slots = []
        self._reset(start_m, clear_holds=True)
        snaps: list[UnitSnapshot] = []
        if start_m % 60 == 0:
            snaps.extend(self._snapshots())
        intervals: list[RuntimeInterval] = []

        def on_hour() -> None:
            snaps.extend(self._snapshots())

        # step in day-sized chunks so the in-memory buffer stays bounded
        m = start_m
        while m < end_m:
            chunk_end = min(end_m, m + 24 * 60)
            first = len(self._slots)
            self._run_until(chunk_end, record=True, on_hour=on_hour)
            intervals.extend(self._slots[first:])
            self._trim_slots()
            m = chunk_end
        if self._use_db:
            self._db_persist()
        return intervals, snaps

    # ===================================================================================
    # snapshots and revisions
    # ===================================================================================

    def _hold_info(self, u: int) -> HoldInfo | None:
        hold = self._hold[u]
        if hold is None or self._m is None or self._m >= hold.end_m:
            return None
        return HoldInfo(
            kind="temperature",
            heat_f=hold.heat_f,
            cool_f=hold.cool_f,
            start=_dt(hold.start_m),
            end=_dt(hold.end_m),
            hold_type="holdHours",
            set_by_us=True,
        )

    def _running(self, u: int) -> list[str]:
        call = self._call[u]
        if call == 1:
            return ["compCool1", "compCool2", "fan"] if self._stage2[u] else ["compCool1", "fan"]
        if call == -1:
            return ["auxHeat1", "auxHeat2", "fan"] if self._stage2[u] else ["auxHeat1", "fan"]
        return []

    def _readings(self, u: int, ctx: _SlotCtx, ts: datetime) -> list[SensorReading]:
        assert self._m is not None
        out = []
        for si in UNIT_SENSORS[u]:
            s = SENSORS[si]
            temp = round(self._T[u] + ctx.sensor_off[si], 1)
            if s.has_occupancy:
                lm = self._sensor_motion[si]
                since = None if lm is None else (self._m - lm) * 60
                occupied = since is not None and since < OCCUPIED_WINDOW_MIN * 60
                motion: bool | None = since is not None and since < 120
                since_occ = None if since is None else (0 if occupied else since - OCCUPIED_WINDOW_MIN * 60)
            else:
                since = since_occ = None
                occupied = motion = None  # type: ignore[assignment]
            out.append(
                SensorReading(
                    sensor_key=s.key,
                    ts=ts,
                    temp_f=temp,
                    humidity=round(self._rh(u, temp)) if s.has_humidity else None,
                    occupied=occupied if s.has_occupancy else None,
                    motion=motion,
                    seconds_since_motion=since,
                    seconds_since_occupancy=since_occ,
                    online=True,
                    battery_low=False if s.kind == "smartsensor" else None,
                )
            )
        return out

    def _unit_snapshot(self, u: int) -> UnitSnapshot:
        assert self._m is not None
        ctx = self._ensure_ctx()
        heat, cool, climate, sset, _ = self._resolve(u, self._m, ctx.program[u])
        ts = _dt(self._m)
        zone = self._T[u] + ctx.set_off[u][sset]
        return UnitSnapshot(
            unit_key=UNIT_KEYS[u],
            ts=ts,
            source="simulator",
            revision=self._revision(u, ctx),
            name=ZONES[u].thermostat_name,
            model=ZONES[u].model,
            hvac_mode="auto",
            equipment_running=self._running(u),
            heat_sp_f=heat,
            cool_sp_f=cool,
            climate_ref=climate,
            hold=self._hold_info(u),
            zone_temp_f=round(zone, 1),
            zone_humidity=round(self._rh(u, zone)),
            outdoor_temp_f=round(ctx.wx[0], 1),
            outdoor_humidity=round(ctx.wx[1]),
            sensors=self._readings(u, ctx, ts),
            sensor_sets={name: [SENSOR_KEYS[i] for i in members] for name, members in SENSOR_SETS[u].items()},
            settings=dict(SNAPSHOT_SETTINGS),
            connected=True,
        )

    def _snapshots(self, unit_keys: list[str] | None = None) -> list[UnitSnapshot]:
        keys = UNIT_KEYS if unit_keys is None else [k for k in UNIT_KEYS if k in unit_keys]
        return [self._unit_snapshot(UNIT_INDEX[k]) for k in keys]

    def _revision(self, u: int, ctx: _SlotCtx) -> str:
        assert self._m is not None
        heat, cool, climate, _, _ = self._resolve(u, self._m, ctx.program[u])
        hold = self._hold[u]
        hold_end = hold.end_m if hold is not None and self._m < hold.end_m else 0
        return f"{ctx.slot_m}:{heat:.1f}:{cool:.1f}:{climate}:{hold_end}:{self._rev[u]}"

    def _brief(self, u: int) -> dict[str, object]:
        """JSON-safe description of a unit's control state (before / read-back)."""
        assert self._m is not None
        ctx = self._ensure_ctx()
        heat, cool, climate, _, away = self._resolve(u, self._m, ctx.program[u])
        hold = self._hold[u]
        active = hold is not None and self._m < hold.end_m
        return {
            "unit_key": UNIT_KEYS[u],
            "ts": _iso(self._m),
            "heat_f": heat,
            "cool_f": cool,
            "climate_ref": climate,
            "smart_away": away,
            "hold": (
                {
                    "heat_f": hold.heat_f,
                    "cool_f": hold.cool_f,
                    "start": _iso(hold.start_m),
                    "end": _iso(hold.end_m),
                    "hold_type": "holdHours",
                }
                if active and hold is not None
                else None
            ),
        }

    # ===================================================================================
    # ThermostatSource
    # ===================================================================================

    def _tick(self) -> None:
        self.advance_to(self._clock())

    async def poll_revisions(self) -> dict[str, str]:
        self._tick()
        ctx = self._ensure_ctx()
        return {k: self._revision(u, ctx) for u, k in enumerate(UNIT_KEYS)}

    async def fetch_snapshots(self, unit_keys: list[str] | None = None) -> list[UnitSnapshot]:
        self._tick()
        return self._snapshots(unit_keys)

    async def fetch_runtime(self, start: datetime, end: datetime) -> list[RuntimeInterval]:
        """Completed 5-minute slots in [start, end) (the last 24 h are kept)."""
        self._tick()
        start = start if start.tzinfo else start.replace(tzinfo=UTC)
        end = end if end.tzinfo else end.replace(tzinfo=UTC)
        return [s.model_copy(deep=True) for s in self._slots if start <= s.ts < end]

    async def set_hold(self, req: HoldRequest) -> WriteResult:
        """Apply a holdHours temperature hold now and read it back."""
        self._tick()
        request = {
            "unit_key": req.unit_key,
            "heat_f": req.heat_f,
            "cool_f": req.cool_f,
            "hours": req.hours,
            "hold_type": "holdHours",
            "reason": req.reason,
        }
        u = UNIT_INDEX.get(req.unit_key)
        if u is None:
            return WriteResult(
                ok=False, channel="simulator", request=request, error=f"unknown unit {req.unit_key!r}"
            )
        before = self._brief(u)
        error = self._hold_error(req)
        if error is not None:
            return WriteResult(
                ok=False,
                channel="simulator",
                before=before,
                request=request,
                readback=self._brief(u),
                error=error,
            )
        assert self._m is not None
        heat, cool = round(float(req.heat_f), 1), round(float(req.cool_f), 1)
        self._hold[u] = _Hold(heat, cool, self._m, self._m + 60 * int(req.hours), req.reason)
        self._rev[u] += 1
        readback = self._brief(u)
        hold = readback["hold"]
        ok = (
            isinstance(hold, dict)
            and hold["heat_f"] == heat
            and hold["cool_f"] == cool
            and hold["end"] == _iso(self._m + 60 * int(req.hours))
        )
        self._persist_if_db()
        return WriteResult(
            ok=ok,
            channel="simulator",
            before=before,
            request=request,
            readback=readback,
            error=None if ok else "read-back does not match the request",
        )

    @staticmethod
    def _hold_error(req: HoldRequest) -> str | None:
        hours = req.hours
        if not isinstance(hours, int) or isinstance(hours, bool) or not 1 <= hours <= 2:
            return "holdHours must be 1 or 2"
        try:
            heat, cool = float(req.heat_f), float(req.cool_f)
        except (TypeError, ValueError):
            return "setpoints must be numbers"
        if not ECOBEE_HEAT_RANGE[0] <= heat <= ECOBEE_HEAT_RANGE[1]:
            return f"heat setpoint {heat} outside {ECOBEE_HEAT_RANGE}"
        if not ECOBEE_COOL_RANGE[0] <= cool <= ECOBEE_COOL_RANGE[1]:
            return f"cool setpoint {cool} outside {ECOBEE_COOL_RANGE}"
        if cool - heat < HEAT_COOL_MIN_DELTA_F - 1e-9:
            return f"cool - heat must be at least {HEAT_COOL_MIN_DELTA_F}°F"
        return None

    async def resume_program(self, unit_key: str, reason: str) -> WriteResult:
        """Clear any hold so the unit follows its program again; read it back."""
        self._tick()
        request = {"unit_key": unit_key, "action": "resumeProgram", "reason": reason}
        u = UNIT_INDEX.get(unit_key)
        if u is None:
            return WriteResult(
                ok=False, channel="simulator", request=request, error=f"unknown unit {unit_key!r}"
            )
        assert self._m is not None
        before = self._brief(u)
        if self._hold[u] is not None:
            self._hold[u] = None
            self._home_since[u] = self._m
            self._rev[u] += 1
        readback = self._brief(u)
        ok = readback["hold"] is None
        self._persist_if_db()
        return WriteResult(
            ok=ok,
            channel="simulator",
            before=before,
            request=request,
            readback=readback,
            error=None if ok else "hold still present after resume",
        )

    async def health(self) -> SourceHealth:
        when = self.now
        detail = f"simulated house (seed {self.seed})" + (f" at {when:%Y-%m-%d %H:%M}Z" if when else "")
        return SourceHealth(
            ok=True,
            kind="simulator",
            detail=detail,
            signed_in=None,
            last_success_at=when,
            consecutive_failures=0,
        )

    async def close(self) -> None:
        """Persist state to app_settings['simulator_state']."""
        self._persist_if_db()

    # ===================================================================================
    # database hooks (only when built by from_settings / use_db=True)
    # ===================================================================================

    def _persist_if_db(self) -> None:
        if self._use_db and self._m is not None:
            self._db_persist()

    def _db_persist(self) -> None:
        from sqlalchemy.exc import SQLAlchemyError

        from climate.store.app_settings import put_setting
        from climate.store.db import session_scope

        try:
            with session_scope() as s:
                put_setting(s, STATE_KEY, self._to_state(), updated_by="simulator")
            self._last_persist_m = self._m
        except SQLAlchemyError as exc:
            log.warning("simulator: could not save state (%s)", type(exc).__name__)

    def _db_refresh(self, now_m: int, force: bool = False) -> None:
        """Re-read settings now and then, and keep real weather loaded around ``now``."""
        from sqlalchemy.exc import SQLAlchemyError

        from climate.store.app_settings import get_setting
        from climate.store.db import session_scope

        if force or self._last_settings_m is None or abs(now_m - self._last_settings_m) >= SETTINGS_EVERY_MIN:
            try:
                with session_scope() as s:
                    control = get_setting(s, "control", ControlSettings)
                    occupancy = get_setting(s, "occupancy", OccupancySettings)
                    location = get_setting(s, "location", LocationSettings)
                self._apply_settings(control, occupancy, location)
                self._last_settings_m = now_m
            except SQLAlchemyError as exc:
                log.warning("simulator: could not read settings (%s)", type(exc).__name__)
        lo = max(self._m if self._m is not None else now_m, now_m - MAX_CATCHUP_MIN - WARMUP_MIN) - 120
        hi = now_m + 120
        stale = self._last_weather_m is None or abs(now_m - self._last_weather_m) >= WEATHER_EVERY_MIN
        covered = (
            self._weather_range is not None and self._weather_range[0] <= lo and hi <= self._weather_range[1]
        )
        if force or stale or not covered:
            self._db_load_weather(lo - 60 * 6, hi + 60 * 6)
            self._last_weather_m = now_m

    def _db_load_weather(self, from_m: int, to_m: int) -> None:
        """Load real weather (any source but 'simulator') for [from_m, to_m)."""
        from sqlalchemy import select
        from sqlalchemy.exc import SQLAlchemyError

        from climate.store.db import session_scope
        from climate.store.orm import WeatherHour

        h0, h1 = math.floor(from_m / 60), math.ceil(to_m / 60)
        try:
            with session_scope() as s:
                rows = (
                    s.execute(
                        select(WeatherHour).where(
                            WeatherHour.source != "simulator",
                            WeatherHour.ts >= datetime.fromtimestamp(h0 * 3600, UTC),
                            WeatherHour.ts < datetime.fromtimestamp(h1 * 3600, UTC),
                        )
                    )
                    .scalars()
                    .all()
                )
        except SQLAlchemyError as exc:
            log.warning("simulator: could not read weather (%s)", type(exc).__name__)
            return
        best: dict[int, tuple[tuple[int, int, float], tuple[float | None, ...]]] = {}
        for r in rows:
            h = math.floor(r.ts.timestamp() / 3600)
            rank = (
                0 if r.kind == "observed" else 1,
                0 if r.source == "open-meteo" else 1,
                -(r.fetched_at.timestamp() if r.fetched_at else 0.0),
            )
            vals = (r.temp_f, r.rh, r.dewpoint_f, r.cloud_cover, r.shortwave_wm2, r.wind_mph, r.precip_in)
            if h not in best or rank < best[h][0]:
                best[h] = (rank, vals)
        for h in [h for h in self._real_wx if h0 <= h < h1]:
            del self._real_wx[h]
        self._real_wx.update({h: v for h, (_, v) in best.items()})
        if len(self._real_wx) > 24 * 400:
            keep_from = h0 - 24 * 30
            self._real_wx = {h: v for h, v in self._real_wx.items() if h >= keep_from}
        rng = self._weather_range
        self._weather_range = (
            (from_m, to_m)
            if rng is None or rng[1] < from_m or to_m < rng[0]
            else (min(rng[0], from_m), max(rng[1], to_m))
        )
        if best:
            self._ctx = None

    def _db_after_advance(self) -> None:
        assert self._m is not None
        if self._last_persist_m is None or self._m - self._last_persist_m >= PERSIST_EVERY_MIN:
            self._db_persist()
        hour = self._m // 60
        if self._last_publish_h != hour:
            self._db_publish_weather(hour)

    def _db_publish_weather(self, hour: int) -> None:
        """Store the synthetic hours the house is using (no real row for that hour) with
        source 'simulator', so the app has weather even without a location."""
        from sqlalchemy.exc import SQLAlchemyError

        from climate.collector.weather import upsert_weather
        from climate.store.db import session_scope

        assert self._m is not None
        hours = [
            _wx_hour_in(h, self._synth(h), "observed" if h * 60 < self._m else "forecast")
            for h in range(hour - 2, hour + PUBLISH_AHEAD_H + 1)
            if h not in self._real_wx
        ]
        try:
            with session_scope() as s:
                upsert_weather(s, hours, "simulator")
            self._last_publish_h = hour
        except SQLAlchemyError as exc:
            log.warning("simulator: could not store synthetic weather (%s)", type(exc).__name__)
