"""HTTP API contract (request and response bodies).

This file is the single source of truth for the JSON the web app and the agent's tools
consume. ``web/src/api/types.ts`` mirrors it by hand; keep both in step. Times are ISO-8601
UTC strings on the wire; dates are ``YYYY-MM-DD`` local calendar days of the house.
Runtime on the wire is in MINUTES (stage-1 seconds / 60) unless a field says ``_s``.
"""

from __future__ import annotations

from datetime import date as Date
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from climate.control.guardrails import GuardResult
from climate.control.policy import PolicyParams, RoomStatus, UnitTarget
from climate.sources.base import HoldInfo
from climate.store.app_settings import (
    AgentSettings,
    ControlSettings,
    LocationSettings,
    OccupancySettings,
    SourceSettings,
)

OPEN_METEO_ATTRIBUTION = "Weather data by Open-Meteo.com"
Role = Literal["owner", "agent"]

# ---------------------------------------------------------------------------------------
# auth / health
# ---------------------------------------------------------------------------------------


class AuthState(BaseModel):
    authenticated: bool
    role: Role | None = None
    password_set: bool  # False -> show the first-run "choose a password" screen


class PasswordBody(BaseModel):
    password: str = Field(min_length=8, max_length=200)


class Health(BaseModel):
    ok: bool
    version: str
    db: bool


# ---------------------------------------------------------------------------------------
# status (Live tab)
# ---------------------------------------------------------------------------------------


class SourceInfo(BaseModel):
    kind: Literal["simulator", "ecobee"]
    ok: bool
    detail: str = ""
    signed_in: bool | None = None
    last_success_at: datetime | None = None


class HomekitInfo(BaseModel):
    enabled: bool
    online: bool  # heartbeat within 3 minutes
    last_beat_at: datetime | None = None
    paired: int = 0  # thermostats paired


class ControllerInfo(BaseModel):
    mode: Literal["off", "suggest", "act"]
    last_tick_at: datetime | None = None
    policy_version_id: int | None = None
    policy: PolicyParams


class UnitLive(BaseModel):
    unit_key: str
    name: str
    hvac_mode: str | None = None
    call: Literal["cool", "heat", "fan", "idle", "unknown"] = "unknown"
    running: list[str] = Field(default_factory=list)
    zone_temp_f: float | None = None
    zone_humidity: float | None = None
    heat_sp_f: float | None = None
    cool_sp_f: float | None = None
    climate_ref: str | None = None
    hold: HoldInfo | None = None
    target: UnitTarget | None = None  # what the policy wants right now
    age_s: float | None = None
    connected: bool = False
    today_runtime_min: float = 0.0  # stage-1 minutes since local midnight
    duty_last_hour_pct: float | None = None
    maxed_minutes_today: float = 0.0  # minutes in hours with duty >= 95%


class WeatherNow(BaseModel):
    temp_f: float | None = None
    rh: float | None = None
    cloud_cover: float | None = None
    shortwave_wm2: float | None = None
    forecast_high_f: float | None = None
    forecast_low_f: float | None = None
    source: str
    attribution: str = OPEN_METEO_ATTRIBUTION


class AlertOut(BaseModel):
    id: int
    ts: datetime
    level: Literal["info", "warn", "error"]
    kind: str
    title: str
    body: str
    resolved_at: datetime | None = None


class AgentRunOut(BaseModel):
    id: int
    created_at: datetime
    kind: Literal["nightly", "weekly", "triggered", "chat", "signin_check"]
    status: Literal["queued", "running", "completed", "failed", "deferred", "cancelled"]
    requested_by: str
    prompt: str = ""
    trigger: dict[str, Any] | None = None
    not_before: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    model: str | None = None
    terminal_reason: str | None = None
    num_turns: int | None = None
    result_text: str | None = None
    error: str | None = None


class AgentInfo(BaseModel):
    enabled: bool
    signed_in: bool | None = None  # from the agent service heartbeat
    last_beat_at: datetime | None = None
    sdk_version: str | None = None
    token_expires_at: Date | None = None
    token_warning: str | None = None  # set within 30 days of expiry or on sign-in failure
    last_run: AgentRunOut | None = None
    next_nightly_at: datetime | None = None
    triggered_today: int = 0
    settings: AgentSettings


class HouseStatus(BaseModel):
    now: datetime
    tz: str
    source: SourceInfo
    homekit: HomekitInfo
    controller: ControllerInfo
    units: list[UnitLive]
    rooms: list[RoomStatus]
    house_empty: bool
    house_empty_reason: str
    weather: WeatherNow | None = None
    alerts: list[AlertOut] = Field(default_factory=list)
    agent: AgentInfo
    location_confirmed: bool


class WsEvent(BaseModel):
    """Pushed on /api/ws. Clients refetch what changed; payloads stay tiny."""

    type: Literal["status", "action", "agent_run", "alert", "homekit", "change", "report"]
    id: int | None = None


# ---------------------------------------------------------------------------------------
# rooms / runtime / weather
# ---------------------------------------------------------------------------------------


class RoomPoint(BaseModel):
    ts: datetime
    temp_f: float | None = None
    occupied: bool | None = None
    state: str | None = None


class SetpointPoint(BaseModel):
    ts: datetime
    heat_sp_f: float | None = None
    cool_sp_f: float | None = None


class RoomHistory(BaseModel):
    room_key: str
    hours: int
    points: list[RoomPoint]
    setpoints: list[SetpointPoint]


class DailyRuntime(BaseModel):
    Date: Date
    unit_key: str
    cool_min: float
    heat_min: float  # compHeat1 (heat pump) or auxHeat1 (furnace) per unit equipment
    aux_min: float
    fan_min: float
    expected_min: float | None = None  # weather-normalized baseline for the day's mode
    mode: Literal["cool", "heat"] | None = None
    outdoor_mean_f: float | None = None
    outdoor_max_f: float | None = None
    cdd65: float | None = None
    hdd65: float | None = None
    maxed_min: float = 0.0


class IntradayPoint(BaseModel):
    ts: datetime
    cool_s: int = 0
    heat_s: int = 0
    aux_s: int = 0
    fan_s: int = 0
    zone_temp_f: float | None = None
    heat_sp_f: float | None = None
    cool_sp_f: float | None = None


class IntradayUnit(BaseModel):
    unit_key: str
    points: list[IntradayPoint]


class OutdoorPoint(BaseModel):
    ts: datetime
    temp_f: float | None = None


class Intraday(BaseModel):
    Date: Date
    units: list[IntradayUnit]
    outdoor: list[OutdoorPoint]


class WeatherPoint(BaseModel):
    ts: datetime
    kind: Literal["observed", "forecast"]
    temp_f: float | None = None
    rh: float | None = None
    cloud_cover: float | None = None
    shortwave_wm2: float | None = None


class WeatherOut(BaseModel):
    source: str
    points: list[WeatherPoint]
    attribution: str = OPEN_METEO_ATTRIBUTION


# ---------------------------------------------------------------------------------------
# analytics ("Did it work?", "Floor coupling")
# ---------------------------------------------------------------------------------------


class BaselineOut(BaseModel):
    unit_key: str
    mode: Literal["cool", "heat"]
    balance_point_f: float
    intercept_min: float
    slope_min_per_dd: float
    n_days: int
    r2: float
    cvrmse: float  # fraction, e.g. 0.14
    nmbe: float  # fraction
    passes: bool  # cvrmse <= 0.20 and |nmbe| <= 0.005
    fitted_at: datetime
    train_start: Date
    train_end: Date


class SavingsDay(BaseModel):
    Date: Date
    expected_min: float | None
    actual_min: float
    outdoor_mean_f: float | None = None


class SavingsByUnit(BaseModel):
    unit_key: str
    expected_min: float | None
    actual_min: float
    savings_pct: float | None


class Savings(BaseModel):
    start: Date
    end: Date
    n_days: int
    expected_min: float | None
    actual_min: float
    savings_min: float | None
    savings_pct: float | None  # positive = less runtime than the weather predicts
    ci90_low_pct: float | None
    ci90_high_pct: float | None
    by_unit: list[SavingsByUnit]
    days: list[SavingsDay]
    baseline_ok: bool
    note: str  # plain-language caveat; never claim savings when baseline_ok is false


class WaterfallItem(BaseModel):
    label: str  # 'Last week', 'Weather', 'Strategy & other', 'This week'
    minutes: float
    kind: Literal["total", "delta"]


class Waterfall(BaseModel):
    week_start: Date
    prev_week_start: Date
    items: list[WaterfallItem]
    strategy_ci90_min: tuple[float, float] | None = None
    note: str


class CouplingPoint(BaseModel):
    ts: datetime  # hour
    main_minus_up_f: float
    up_duty_pct: float
    outdoor_f: float | None = None
    shortwave_wm2: float | None = None


class Coupling(BaseModel):
    days: int
    n_hours: int
    points: list[CouplingPoint]  # downsampled for the scatter (<= 2000)
    coef_min_per_degf: float | None  # upstairs runtime minutes/hour per °F main above up
    ci90: tuple[float, float] | None
    placebo_coef: float | None  # same model on the bed wing; should be ~0
    placebo_ci90: tuple[float, float] | None
    interpretation: str


class ComfortRow(BaseModel):
    room_key: str
    occupied_min: float
    in_band_pct: float | None
    worst_excursion_f: float | None


class DriftUnit(BaseModel):
    unit_key: str
    mode: Literal["cool", "heat"]
    recent_days: int
    resid_mean_pct: float
    z: float
    drifting: bool


class DriftReport(BaseModel):
    units: list[DriftUnit]
    note: str


class NaturalEvent(BaseModel):
    Date: Date
    main_floor_float_f: float  # how far the main floor floated above its occupied setpoint
    up_runtime_min: float
    expected_up_runtime_min: float | None


class NaturalExperiments(BaseModel):
    days: int
    events: list[NaturalEvent]
    estimate_min_per_event: float | None
    ci90: tuple[float, float] | None
    placebo_estimate: float | None  # fake event times on similar days; should be ~0
    bed_wing_estimate: float | None  # the wing should show no effect
    note: str


# ---------------------------------------------------------------------------------------
# control / changes
# ---------------------------------------------------------------------------------------


class PlanRow(BaseModel):
    target: UnitTarget
    guard: GuardResult
    current_heat_f: float | None
    current_cool_f: float | None
    would_write: bool


class PlanOut(BaseModel):
    at: datetime
    mode: Literal["off", "suggest", "act"]
    rows: list[PlanRow]


class ControlActionOut(BaseModel):
    id: int
    ts: datetime
    unit_key: str
    actor: str
    mode: str
    channel: str
    action: str
    status: str
    rule: str | None = None
    reason: str
    before: dict[str, Any] | None = None
    request: dict[str, Any] | None = None
    readback: dict[str, Any] | None = None
    readback_ok: bool | None = None
    error: str | None = None
    completed_at: datetime | None = None


class SettingsOut(BaseModel):
    control: ControlSettings
    occupancy: OccupancySettings
    location: LocationSettings
    policy: PolicyParams
    policy_version_id: int | None
    signoff_ranges: dict[str, tuple[float, float]]
    owner_only_params: list[str]


class SettingsUpdate(BaseModel):
    control: ControlSettings | None = None
    occupancy: OccupancySettings | None = None
    location: LocationSettings | None = None


class ModeBody(BaseModel):
    mode: Literal["off", "suggest", "act"]


class ManualHoldBody(BaseModel):
    unit_key: str
    heat_f: float
    cool_f: float
    hours: int = Field(default=2, ge=1, le=2)


class UnitBody(BaseModel):
    unit_key: str


class PresenceBody(BaseModel):
    phones_away: bool | None


class ChangeOut(BaseModel):
    id: int
    created_at: datetime
    kind: Literal["policy", "model", "experiment"]
    title: str
    rationale: str
    payload: dict[str, Any]
    proposed_by: Literal["model", "claude", "owner"]
    status: str
    gates: dict[str, Any]
    decided_by: str | None = None
    decided_at: datetime | None = None
    decision_reason: str | None = None
    shadow_start: datetime | None = None
    trial_start: datetime | None = None
    trial_end: datetime | None = None
    needs: Literal["nothing", "claude", "owner"]  # who must decide next


class ProposePolicyBody(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    rationale: str = Field(default="", max_length=4000)
    params: dict[str, Any]  # partial PolicyParams: only the keys being changed


class DecisionBody(BaseModel):
    decision: Literal["approve", "hold", "reject"]
    reason: str = Field(min_length=3, max_length=2000)


# ---------------------------------------------------------------------------------------
# experiments / models / jobs
# ---------------------------------------------------------------------------------------


class ArmIn(BaseModel):
    key: str = Field(pattern=r"^[a-z0-9_]{1,24}$")
    label: str
    params: dict[str, Any] = Field(default_factory=dict)  # partial PolicyParams for the arm


class ProposeExperimentBody(BaseModel):
    name: str = Field(min_length=3, max_length=120)
    hypothesis: str = Field(min_length=3, max_length=2000)
    arms: list[ArmIn] = Field(min_length=2, max_length=3)
    n_days: int = Field(default=28, ge=6, le=180)
    block_days: int = Field(default=2, ge=1, le=7)
    n_checkpoints: int = Field(default=3, ge=1, le=3)
    alpha: float = Field(default=0.10, gt=0, lt=0.5)


class Checkpoint(BaseModel):
    day: int
    info_fraction: float
    alpha_spent: float
    z_crit: float


class ExperimentOut(BaseModel):
    id: int
    created_at: datetime
    name: str
    hypothesis: str
    metric: str
    arms: list[ArmIn]
    n_days: int
    block_days: int
    checkpoints: list[Checkpoint]
    alpha: float
    status: Literal["proposed", "approved", "running", "stopped", "completed", "rejected"]
    proposed_by: str
    start_date: Date | None = None
    end_date: Date | None = None


class ExperimentDayOut(BaseModel):
    day: Date
    arm: str
    actual_min: float | None
    expected_min: float | None
    residual_min: float | None
    included: bool


class ExperimentAnalysis(BaseModel):
    days_observed: int
    effect_pct: float | None  # arm B vs arm A on weather-normalized house runtime
    ci_low_pct: float | None
    ci_high_pct: float | None
    checkpoint_reached: int | None
    decision: Literal["continue", "stop_win", "stop_futile", "inconclusive", "not_started"]
    note: str


class ExperimentDetail(BaseModel):
    experiment: ExperimentOut
    schedule: list[ExperimentDayOut]
    analysis: ExperimentAnalysis


class ExperimentDecisionBody(BaseModel):
    decision: Literal["approve", "reject", "stop"]
    reason: str = Field(min_length=3, max_length=2000)


class PowerOut(BaseModel):
    effect_pct: float
    alpha: float
    power: float
    resid_cv: float | None
    days_per_arm: int | None
    total_days: int | None
    note: str


class ModelFitOut(BaseModel):
    id: int
    created_at: datetime
    kind: Literal["baseline", "rc", "room_offsets", "occupancy_priors"]
    unit_key: str | None
    mode: str | None
    train_start: Date | None
    train_end: Date | None
    params: dict[str, Any]
    metrics: dict[str, Any]
    status: str
    notes: str | None = None


class JobOut(BaseModel):
    id: int
    created_at: datetime
    kind: str
    status: Literal["queued", "running", "done", "failed"]
    params: dict[str, Any]
    result: dict[str, Any] | None = None
    error: str | None = None
    finished_at: datetime | None = None


class BacktestBody(BaseModel):
    params: dict[str, Any] = Field(default_factory=dict)  # partial PolicyParams
    days: int = Field(default=28, ge=7, le=120)


class BacktestOut(BaseModel):
    days: int
    model: Literal["rc", "rule_of_thumb"]  # 'rule_of_thumb' until an RC fit passes its checks
    current_runtime_min: float | None
    candidate_runtime_min: float | None
    delta_pct: float | None
    ci90_pct: tuple[float, float] | None
    comfort_violation_min_current: float | None
    comfort_violation_min_candidate: float | None
    beats_model_uncertainty: bool
    note: str


class SimulateBody(BaseModel):
    params: dict[str, Any] = Field(default_factory=dict)
    date: Date | None = None  # default: tomorrow with the forecast


class SimPoint(BaseModel):
    ts: datetime
    temp_f: float
    runtime_s: float


class SimulateOut(BaseModel):
    Date: Date
    model: Literal["rc", "rule_of_thumb"]
    units: dict[str, list[SimPoint]]
    total_runtime_min: float
    note: str


# ---------------------------------------------------------------------------------------
# reports / agent
# ---------------------------------------------------------------------------------------


class ReportOut(BaseModel):
    id: int
    created_at: datetime
    kind: Literal["daily", "weekly", "nightly", "anomaly", "note"]
    author: Literal["system", "claude"]
    period_start: Date | None = None
    period_end: Date | None = None
    title: str
    body_md: str
    data: dict[str, Any] = Field(default_factory=dict)
    agent_run_id: int | None = None


class PublishReportBody(BaseModel):
    kind: Literal["daily", "weekly", "nightly", "anomaly", "note"]
    title: str = Field(min_length=3, max_length=200)
    body_md: str = Field(min_length=1, max_length=40000)
    data: dict[str, Any] = Field(default_factory=dict)
    period_start: Date | None = None
    period_end: Date | None = None
    agent_run_id: int | None = None


class AskBody(BaseModel):
    question: str = Field(min_length=2, max_length=4000)


class RunBody(BaseModel):
    kind: Literal["nightly", "weekly"]


class AgentFinishBody(BaseModel):
    status: Literal["completed", "failed", "deferred"]
    result_text: str | None = None
    model: str | None = None
    terminal_reason: str | None = None
    num_turns: int | None = None
    usage: dict[str, Any] | None = None
    error: str | None = None
    session_id: str | None = None
    not_before: datetime | None = None  # for deferred (usage limit) runs


class AgentHeartbeatBody(BaseModel):
    signed_in: bool | None = None
    sdk_version: str | None = None
    cli_version: str | None = None
    token_expires_at: Date | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------------------
# setup (onboarding)
# ---------------------------------------------------------------------------------------


class UnitOut(BaseModel):
    key: str
    name: str
    thermostat_room_key: str
    thermostat_model: str | None
    ecobee_identifier: str | None
    homekit_device_id: str | None
    equipment: dict[str, Any]


class RoomOut(BaseModel):
    key: str
    name: str
    unit_key: str
    floor: str
    has_sensor: bool
    is_sleep_room: bool
    has_comfort_target: bool
    notes: str | None


class SensorOut(BaseModel):
    key: str
    room_key: str
    unit_key: str
    kind: str
    name: str
    has_humidity: bool
    has_occupancy: bool
    ecobee_sensor_id: str | None
    homekit_aid: int | None


class EcobeeThermostatOut(BaseModel):
    identifier: str
    name: str
    model_number: str | None
    unit_key: str | None
    sensors: list[dict[str, Any]]
    last_seen_at: datetime


class EcobeeSetup(BaseModel):
    signed_in: bool
    mfa_pending: bool
    mfa_type: str | None = None
    last_error: str | None = None
    web_client_id: str
    thermostats: list[EcobeeThermostatOut]


class HomekitDeviceOut(BaseModel):
    device_id: str
    name: str
    model: str | None
    address: str | None
    unpaired: bool  # status_flags & 0x01
    alias: str | None
    unit_key: str | None
    pairing_state: str
    pairing_error: str | None
    online: bool
    last_seen_at: datetime | None
    accessories: list[dict[str, Any]] | None = None


class HomekitSetup(BaseModel):
    enabled: bool
    service_online: bool
    devices: list[HomekitDeviceOut]


class SetupState(BaseModel):
    source: SourceSettings
    location: LocationSettings
    ecobee: EcobeeSetup
    homekit: HomekitSetup
    units: list[UnitOut]
    rooms: list[RoomOut]
    sensors: list[SensorOut]
    secrets_ok: bool  # CLIMATE_SECRET_KEY usable


class SourceBody(BaseModel):
    kind: Literal["simulator", "ecobee"]
    homekit_enabled: bool = False


class EcobeeLoginBody(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=500)


class EcobeeMfaBody(BaseModel):
    code: str = Field(pattern=r"^\d{4,8}$")


class EcobeeLoginResult(BaseModel):
    status: Literal["signed_in", "mfa_required", "error"]
    mfa_type: str | None = None
    error: str | None = None


class EcobeeMapBody(BaseModel):
    identifier: str
    unit_key: str | None  # None unmaps


class SensorMapBody(BaseModel):
    sensor_key: str
    ecobee_sensor_id: str | None = None
    homekit_aid: int | None = None


class HomekitPairBody(BaseModel):
    device_id: str
    alias: str = Field(pattern=r"^[a-z][a-z0-9_]{1,23}$")
    unit_key: str


class HomekitCodeBody(BaseModel):
    device_id: str
    code: str = Field(pattern=r"^\d{3}-\d{2}-\d{3}$")


class DeviceBody(BaseModel):
    device_id: str
