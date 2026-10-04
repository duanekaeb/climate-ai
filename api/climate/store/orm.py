"""ORM mapping of the schema in ``migrations/001_init.sql``.

The SQL file is authoritative; ``tests/test_schema.py`` checks this mapping against it.
Identity columns are mapped with ``Identity()`` so inserts let Postgres assign ids.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, ClassVar

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    Float,
    ForeignKey,
    Identity,
    Integer,
    LargeBinary,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, REAL, TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

TS = TIMESTAMP(timezone=True)


class Base(DeclarativeBase):
    __mapper_args__: ClassVar[dict[str, Any]] = {"eager_defaults": True}
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSONB, list[Any]: JSONB, datetime: TS}


# --- house inventory -------------------------------------------------------------------


class Unit(Base):
    __tablename__ = "units"
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    thermostat_room_key: Mapped[str] = mapped_column(Text)
    thermostat_model: Mapped[str | None] = mapped_column(Text)
    ecobee_identifier: Mapped[str | None] = mapped_column(Text, unique=True)
    homekit_device_id: Mapped[str | None] = mapped_column(Text, unique=True)
    equipment: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    power_weight: Mapped[float] = mapped_column(Float, default=1.0)
    sort: Mapped[int] = mapped_column(Integer, default=0)


class Room(Base):
    __tablename__ = "rooms"
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    unit_key: Mapped[str] = mapped_column(Text, ForeignKey("units.key"))
    floor: Mapped[str] = mapped_column(Text)
    has_sensor: Mapped[bool] = mapped_column(Boolean)
    is_sleep_room: Mapped[bool] = mapped_column(Boolean, default=False)
    has_comfort_target: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)
    sort: Mapped[int] = mapped_column(Integer, default=0)


class Sensor(Base):
    __tablename__ = "sensors"
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    room_key: Mapped[str] = mapped_column(Text, ForeignKey("rooms.key"))
    unit_key: Mapped[str] = mapped_column(Text, ForeignKey("units.key"))
    kind: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    has_temperature: Mapped[bool] = mapped_column(Boolean, default=True)
    has_humidity: Mapped[bool] = mapped_column(Boolean, default=False)
    has_occupancy: Mapped[bool] = mapped_column(Boolean, default=False)
    ecobee_sensor_id: Mapped[str | None] = mapped_column(Text)
    homekit_aid: Mapped[int | None] = mapped_column(BigInteger)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort: Mapped[int] = mapped_column(Integer, default=0)


# --- live state ------------------------------------------------------------------------


class LiveUnit(Base):
    __tablename__ = "live_units"
    unit_key: Mapped[str] = mapped_column(Text, ForeignKey("units.key"), primary_key=True)
    ts: Mapped[datetime] = mapped_column(TS)
    source: Mapped[str] = mapped_column(Text)
    revision: Mapped[str | None] = mapped_column(Text)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)


class LiveSensor(Base):
    __tablename__ = "live_sensors"
    sensor_key: Mapped[str] = mapped_column(Text, ForeignKey("sensors.key"), primary_key=True)
    ts: Mapped[datetime] = mapped_column(TS)
    source: Mapped[str] = mapped_column(Text)
    temp_f: Mapped[float | None] = mapped_column(REAL)
    humidity: Mapped[float | None] = mapped_column(REAL)
    occupied: Mapped[bool | None] = mapped_column(Boolean)
    motion: Mapped[bool | None] = mapped_column(Boolean)
    seconds_since_motion: Mapped[int | None] = mapped_column(Integer)
    seconds_since_occupancy: Mapped[int | None] = mapped_column(Integer)
    online: Mapped[bool] = mapped_column(Boolean, default=True)
    battery_low: Mapped[bool | None] = mapped_column(Boolean)


# --- time series -----------------------------------------------------------------------


class Reading5m(Base):
    __tablename__ = "readings_5m"
    ts: Mapped[datetime] = mapped_column(TS, primary_key=True)
    sensor_key: Mapped[str] = mapped_column(Text, ForeignKey("sensors.key"), primary_key=True)
    temp_f: Mapped[float | None] = mapped_column(REAL)
    humidity: Mapped[float | None] = mapped_column(REAL)
    occupied: Mapped[bool | None] = mapped_column(Boolean)
    source: Mapped[str] = mapped_column(Text)


class Runtime5m(Base):
    __tablename__ = "runtime_5m"
    ts: Mapped[datetime] = mapped_column(TS, primary_key=True)
    unit_key: Mapped[str] = mapped_column(Text, ForeignKey("units.key"), primary_key=True)
    comp_cool1: Mapped[int] = mapped_column(Integer, default=0)
    comp_cool2: Mapped[int] = mapped_column(Integer, default=0)
    comp_heat1: Mapped[int] = mapped_column(Integer, default=0)
    comp_heat2: Mapped[int] = mapped_column(Integer, default=0)
    aux_heat1: Mapped[int] = mapped_column(Integer, default=0)
    aux_heat2: Mapped[int] = mapped_column(Integer, default=0)
    fan: Mapped[int] = mapped_column(Integer, default=0)
    hvac_mode: Mapped[str | None] = mapped_column(Text)
    climate_ref: Mapped[str | None] = mapped_column(Text)
    zone_temp_f: Mapped[float | None] = mapped_column(REAL)
    zone_humidity: Mapped[float | None] = mapped_column(REAL)
    heat_sp_f: Mapped[float | None] = mapped_column(REAL)
    cool_sp_f: Mapped[float | None] = mapped_column(REAL)
    outdoor_temp_f: Mapped[float | None] = mapped_column(REAL)
    outdoor_humidity: Mapped[float | None] = mapped_column(REAL)
    source: Mapped[str] = mapped_column(Text)


class OccupancyEvent(Base):
    __tablename__ = "occupancy_events"
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    ts: Mapped[datetime] = mapped_column(TS, primary_key=True)
    sensor_key: Mapped[str] = mapped_column(Text, ForeignKey("sensors.key"))
    kind: Mapped[str] = mapped_column(Text)
    value: Mapped[bool] = mapped_column(Boolean)
    source: Mapped[str] = mapped_column(Text)


class RoomStateRow(Base):
    __tablename__ = "room_states"
    ts: Mapped[datetime] = mapped_column(TS, primary_key=True)
    room_key: Mapped[str] = mapped_column(Text, ForeignKey("rooms.key"), primary_key=True)
    state: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(REAL)
    reason: Mapped[str] = mapped_column(Text)


class WeatherHour(Base):
    __tablename__ = "weather_hourly"
    ts: Mapped[datetime] = mapped_column(TS, primary_key=True)
    source: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, primary_key=True)
    fetched_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    temp_f: Mapped[float | None] = mapped_column(REAL)
    rh: Mapped[float | None] = mapped_column(REAL)
    dewpoint_f: Mapped[float | None] = mapped_column(REAL)
    cloud_cover: Mapped[float | None] = mapped_column(REAL)
    shortwave_wm2: Mapped[float | None] = mapped_column(REAL)
    wind_mph: Mapped[float | None] = mapped_column(REAL)
    precip_in: Mapped[float | None] = mapped_column(REAL)


# --- control, policy, change gates -----------------------------------------------------


class PolicyVersion(Base):
    __tablename__ = "policy_versions"
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    created_by: Mapped[str] = mapped_column(Text)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text)
    change_id: Mapped[int | None] = mapped_column(BigInteger)
    note: Mapped[str | None] = mapped_column(Text)


class Change(Base):
    __tablename__ = "changes"
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    kind: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    rationale: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    proposed_by: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    gates: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    decided_by: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(TS)
    decision_reason: Mapped[str | None] = mapped_column(Text)
    shadow_start: Mapped[datetime | None] = mapped_column(TS)
    trial_start: Mapped[datetime | None] = mapped_column(TS)
    trial_end: Mapped[datetime | None] = mapped_column(TS)
    policy_version_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("policy_versions.id"))
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class ControlAction(Base):
    __tablename__ = "control_actions"
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    ts: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    unit_key: Mapped[str] = mapped_column(Text, ForeignKey("units.key"))
    actor: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    rule: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    request: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    readback: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    readback_ok: Mapped[bool | None] = mapped_column(Boolean)
    error: Mapped[str | None] = mapped_column(Text)
    policy_version_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("policy_versions.id"))
    completed_at: Mapped[datetime | None] = mapped_column(TS)


# --- experiments -----------------------------------------------------------------------


class Experiment(Base):
    __tablename__ = "experiments"
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    name: Mapped[str] = mapped_column(Text)
    hypothesis: Mapped[str] = mapped_column(Text)
    metric: Mapped[str] = mapped_column(Text, default="house_runtime_residual")
    arms: Mapped[list[Any]] = mapped_column(JSONB)
    design: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text)
    proposed_by: Mapped[str] = mapped_column(Text)
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class ExperimentDay(Base):
    __tablename__ = "experiment_days"
    experiment_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("experiments.id"), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    arm: Mapped[str] = mapped_column(Text)
    actual_s: Mapped[float | None] = mapped_column(Float)
    expected_s: Mapped[float | None] = mapped_column(Float)
    residual_s: Mapped[float | None] = mapped_column(Float)
    included: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str | None] = mapped_column(Text)


# --- models, jobs, agent, reports, alerts ----------------------------------------------


class ModelFit(Base):
    __tablename__ = "model_fits"
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    kind: Mapped[str] = mapped_column(Text)
    unit_key: Mapped[str | None] = mapped_column(Text, ForeignKey("units.key"))
    mode: Mapped[str | None] = mapped_column(Text)
    train_start: Mapped[date | None] = mapped_column(Date)
    train_end: Mapped[date | None] = mapped_column(Date)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    kind: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    requested_by: Mapped[str] = mapped_column(Text)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(TS)
    finished_at: Mapped[datetime | None] = mapped_column(TS)


class AgentRun(Base):
    __tablename__ = "agent_runs"
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    kind: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    requested_by: Mapped[str] = mapped_column(Text)
    prompt: Mapped[str] = mapped_column(Text, default="")
    trigger: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    not_before: Mapped[datetime | None] = mapped_column(TS)
    started_at: Mapped[datetime | None] = mapped_column(TS)
    finished_at: Mapped[datetime | None] = mapped_column(TS)
    model: Mapped[str | None] = mapped_column(Text)
    terminal_reason: Mapped[str | None] = mapped_column(Text)
    num_turns: Mapped[int | None] = mapped_column(Integer)
    result_text: Mapped[str | None] = mapped_column(Text)
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    session_id: Mapped[str | None] = mapped_column(Text)


class Report(Base):
    __tablename__ = "reports"
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    kind: Mapped[str] = mapped_column(Text)
    author: Mapped[str] = mapped_column(Text)
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    title: Mapped[str] = mapped_column(Text)
    body_md: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    agent_run_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("agent_runs.id"))


class Alert(Base):
    __tablename__ = "alerts"
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    ts: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    level: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, default="")
    dedupe_key: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(TS)
    notified_at: Mapped[datetime | None] = mapped_column(TS)


# --- settings, secrets, discovery ------------------------------------------------------


class AppSetting(Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    updated_by: Mapped[str] = mapped_column(Text, default="system")


class SecretRow(Base):
    __tablename__ = "secrets"
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class EcobeeThermostat(Base):
    __tablename__ = "ecobee_thermostats"
    identifier: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    model_number: Mapped[str | None] = mapped_column(Text)
    unit_key: Mapped[str | None] = mapped_column(Text, ForeignKey("units.key"))
    sensors: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    last_revision: Mapped[str | None] = mapped_column(Text)
    last_seen_at: Mapped[datetime] = mapped_column(TS)


class HomekitDevice(Base):
    __tablename__ = "homekit_devices"
    device_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    category: Mapped[int | None] = mapped_column(Integer)
    address: Mapped[str | None] = mapped_column(Text)
    port: Mapped[int | None] = mapped_column(Integer)
    status_flags: Mapped[int | None] = mapped_column(Integer)
    config_num: Mapped[int | None] = mapped_column(Integer)
    alias: Mapped[str | None] = mapped_column(Text, unique=True)
    unit_key: Mapped[str | None] = mapped_column(Text, ForeignKey("units.key"))
    pairing_state: Mapped[str] = mapped_column(Text, default="none")
    pairing_code: Mapped[str | None] = mapped_column(Text)
    pairing_error: Mapped[str | None] = mapped_column(Text)
    accessories: Mapped[Any | None] = mapped_column(JSONB)
    online: Mapped[bool] = mapped_column(Boolean, default=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(TS)
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
