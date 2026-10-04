-- 001_init: the whole schema for Climate AI v1.
-- Runs inside one transaction owned by climate.store.migrate (no BEGIN/COMMIT here).
-- TimescaleDB is used when the extension is available (the docker image ships it); plain
-- Postgres works too, so tests can run against stock Postgres 16.
-- All timestamps are UTC timestamptz. Temperatures are degrees Fahrenheit. Runtime is seconds.

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb') THEN
        CREATE EXTENSION IF NOT EXISTS timescaledb;
    END IF;
END $$;

-- ---------------------------------------------------------------------------------------
-- House inventory
-- ---------------------------------------------------------------------------------------

CREATE TABLE units (
    key                 TEXT PRIMARY KEY,                 -- 'main' | 'up' | 'bed'
    name                TEXT NOT NULL,
    thermostat_room_key TEXT NOT NULL,
    thermostat_model    TEXT,                             -- ecobee modelNumber once known
    ecobee_identifier   TEXT UNIQUE,                      -- ecobee thermostat identifier
    homekit_device_id   TEXT UNIQUE,                      -- HAP device id (lower-case mac-like)
    equipment           JSONB NOT NULL DEFAULT '{}'::jsonb, -- {cooling, heating, stages, tonnage}
    power_weight        DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    sort                INT NOT NULL DEFAULT 0
);

CREATE TABLE rooms (
    key                 TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    unit_key            TEXT NOT NULL REFERENCES units(key),
    floor               TEXT NOT NULL,                    -- 'main' | 'upstairs' | 'wing'
    has_sensor          BOOLEAN NOT NULL,
    is_sleep_room       BOOLEAN NOT NULL DEFAULT FALSE,
    has_comfort_target  BOOLEAN NOT NULL DEFAULT TRUE,
    notes               TEXT,
    sort                INT NOT NULL DEFAULT 0
);
CREATE INDEX idx_rooms_unit ON rooms (unit_key);

CREATE TABLE sensors (
    key                 TEXT PRIMARY KEY,                 -- e.g. 'main.hallway_tstat'
    room_key            TEXT NOT NULL REFERENCES rooms(key),
    unit_key            TEXT NOT NULL REFERENCES units(key),
    kind                TEXT NOT NULL CHECK (kind IN ('thermostat', 'smartsensor', 'addon')),
    name                TEXT NOT NULL,
    has_temperature     BOOLEAN NOT NULL DEFAULT TRUE,
    has_humidity        BOOLEAN NOT NULL DEFAULT FALSE,
    has_occupancy       BOOLEAN NOT NULL DEFAULT FALSE,
    ecobee_sensor_id    TEXT,                             -- 'ei:0' (thermostat) / 'rs:100' (remote)
    homekit_aid         BIGINT,                           -- may exceed 2^32 on newer firmware
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    sort                INT NOT NULL DEFAULT 0,
    CONSTRAINT uq_sensors_unit_ecobee UNIQUE (unit_key, ecobee_sensor_id),
    CONSTRAINT uq_sensors_unit_homekit UNIQUE (unit_key, homekit_aid)
);
CREATE INDEX idx_sensors_room ON sensors (room_key);

-- ---------------------------------------------------------------------------------------
-- Live state (latest values only; one row per unit / sensor)
-- ---------------------------------------------------------------------------------------

CREATE TABLE live_units (
    unit_key            TEXT PRIMARY KEY REFERENCES units(key),
    ts                  TIMESTAMPTZ NOT NULL,
    source              TEXT NOT NULL,                    -- 'ecobee' | 'homekit' | 'simulator'
    revision            TEXT,
    snapshot            JSONB NOT NULL                    -- climate.sources.base.UnitSnapshot as JSON
);

CREATE TABLE live_sensors (
    sensor_key           TEXT PRIMARY KEY REFERENCES sensors(key),
    ts                   TIMESTAMPTZ NOT NULL,
    source               TEXT NOT NULL,
    temp_f               REAL,
    humidity             REAL,
    occupied             BOOLEAN,
    motion               BOOLEAN,
    seconds_since_motion INT,                             -- HomeKit vendor counter; -1 never
    seconds_since_occupancy INT,
    online               BOOLEAN NOT NULL DEFAULT TRUE,
    battery_low          BOOLEAN
);

-- ---------------------------------------------------------------------------------------
-- Time series
-- ---------------------------------------------------------------------------------------

-- Canonical 5-minute sensor series. Source precedence when upserting the same slot:
-- ecobee_report > ecobee_poll > homekit > simulator (see climate.collector.ingest).
CREATE TABLE readings_5m (
    ts                  TIMESTAMPTZ NOT NULL,             -- slot start, aligned to 5 minutes
    sensor_key          TEXT NOT NULL REFERENCES sensors(key),
    temp_f              REAL,
    humidity            REAL,
    occupied            BOOLEAN,
    source              TEXT NOT NULL,
    PRIMARY KEY (sensor_key, ts)
);
CREATE INDEX idx_readings_5m_ts ON readings_5m (ts);

-- Per-unit 5-minute equipment record (ecobee runtimeReport columns, seconds per slot 0..300).
-- Runtime metric = stage-1 seconds. comp_cool1 / comp_heat1 / aux_heat1 already include
-- stage-2 time; the *2 columns are stored for reference and never added on top.
CREATE TABLE runtime_5m (
    ts                  TIMESTAMPTZ NOT NULL,
    unit_key            TEXT NOT NULL REFERENCES units(key),
    comp_cool1          INT NOT NULL DEFAULT 0,
    comp_cool2          INT NOT NULL DEFAULT 0,
    comp_heat1          INT NOT NULL DEFAULT 0,
    comp_heat2          INT NOT NULL DEFAULT 0,
    aux_heat1           INT NOT NULL DEFAULT 0,
    aux_heat2           INT NOT NULL DEFAULT 0,
    fan                 INT NOT NULL DEFAULT 0,
    hvac_mode           TEXT,
    climate_ref         TEXT,                             -- comfort setting in force
    zone_temp_f         REAL,                             -- thermostat's averaged temperature
    zone_humidity       REAL,
    heat_sp_f           REAL,
    cool_sp_f           REAL,
    outdoor_temp_f      REAL,
    outdoor_humidity    REAL,
    source              TEXT NOT NULL,
    PRIMARY KEY (unit_key, ts)
);
CREATE INDEX idx_runtime_5m_ts ON runtime_5m (ts);

CREATE TABLE occupancy_events (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY,
    ts                  TIMESTAMPTZ NOT NULL,
    sensor_key          TEXT NOT NULL REFERENCES sensors(key),
    kind                TEXT NOT NULL CHECK (kind IN ('occupancy', 'motion')),
    value               BOOLEAN NOT NULL,
    source              TEXT NOT NULL,
    PRIMARY KEY (id, ts)
);
CREATE INDEX idx_occupancy_events_sensor_ts ON occupancy_events (sensor_key, ts DESC);

-- Room states written by the occupancy engine on every controller tick.
CREATE TABLE room_states (
    ts                  TIMESTAMPTZ NOT NULL,
    room_key            TEXT NOT NULL REFERENCES rooms(key),
    state               TEXT NOT NULL CHECK (state IN ('occupied', 'asleep', 'empty', 'unknown', 'no_target')),
    confidence          REAL NOT NULL,
    reason              TEXT NOT NULL,
    PRIMARY KEY (room_key, ts)
);
CREATE INDEX idx_room_states_ts ON room_states (ts);

CREATE TABLE weather_hourly (
    ts                  TIMESTAMPTZ NOT NULL,             -- hour start
    source              TEXT NOT NULL,                    -- 'open-meteo' | 'nws' | 'ecobee' | 'simulator'
    kind                TEXT NOT NULL CHECK (kind IN ('observed', 'forecast')),
    fetched_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    temp_f              REAL,
    rh                  REAL,
    dewpoint_f          REAL,
    cloud_cover         REAL,                             -- percent
    shortwave_wm2       REAL,                             -- global horizontal irradiance
    wind_mph            REAL,
    precip_in           REAL,
    PRIMARY KEY (source, kind, ts)
);
CREATE INDEX idx_weather_hourly_ts ON weather_hourly (ts);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
        PERFORM create_hypertable('readings_5m', 'ts', chunk_time_interval => INTERVAL '30 days', migrate_data => true);
        PERFORM create_hypertable('runtime_5m', 'ts', chunk_time_interval => INTERVAL '30 days', migrate_data => true);
        PERFORM create_hypertable('occupancy_events', 'ts', chunk_time_interval => INTERVAL '30 days', migrate_data => true);
        PERFORM create_hypertable('room_states', 'ts', chunk_time_interval => INTERVAL '30 days', migrate_data => true);
    END IF;
END $$;

-- ---------------------------------------------------------------------------------------
-- Control, policy and the change gates
-- ---------------------------------------------------------------------------------------

-- Versioned policy parameters the models and Claude may tune (through the change gates).
-- Owner settings (mode, comfort bands, hard limits, sleep windows) live in app_settings.
CREATE TABLE policy_versions (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by          TEXT NOT NULL,                    -- 'seed' | 'owner' | 'model' | 'claude'
    params              JSONB NOT NULL,                   -- climate.control.policy.PolicyParams
    status              TEXT NOT NULL CHECK (status IN ('active', 'shadow', 'trial', 'retired')),
    change_id           BIGINT,
    note                TEXT
);
CREATE INDEX idx_policy_versions_status ON policy_versions (status);

CREATE TABLE changes (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind                TEXT NOT NULL CHECK (kind IN ('policy', 'model', 'experiment')),
    title               TEXT NOT NULL,
    rationale           TEXT NOT NULL DEFAULT '',
    payload             JSONB NOT NULL,
    proposed_by         TEXT NOT NULL CHECK (proposed_by IN ('model', 'claude', 'owner')),
    status              TEXT NOT NULL CHECK (status IN (
                            'rejected', 'backtest', 'shadow', 'awaiting_signoff', 'held',
                            'trial', 'active', 'retired', 'cancelled')),
    gates               JSONB NOT NULL DEFAULT '{}'::jsonb, -- per-gate results
    decided_by          TEXT,                             -- 'claude' | 'owner'
    decided_at          TIMESTAMPTZ,
    decision_reason     TEXT,
    shadow_start        TIMESTAMPTZ,
    trial_start         TIMESTAMPTZ,
    trial_end           TIMESTAMPTZ,
    policy_version_id   BIGINT REFERENCES policy_versions(id),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_changes_status ON changes (status);

CREATE TABLE control_actions (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    unit_key            TEXT NOT NULL REFERENCES units(key),
    actor               TEXT NOT NULL CHECK (actor IN ('controller', 'owner', 'homekit_service')),
    mode                TEXT NOT NULL CHECK (mode IN ('suggest', 'act')),
    channel             TEXT NOT NULL CHECK (channel IN ('ecobee', 'homekit', 'simulator', 'none')),
    action              TEXT NOT NULL CHECK (action IN ('set_hold', 'resume_program', 'update_program', 'update_settings')),
    status              TEXT NOT NULL CHECK (status IN ('suggested', 'queued', 'sent', 'verified', 'failed', 'skipped')),
    rule                TEXT,                             -- policy rule that produced it
    reason              TEXT NOT NULL,
    before              JSONB,
    request             JSONB,
    readback            JSONB,
    readback_ok         BOOLEAN,
    error               TEXT,
    policy_version_id   BIGINT REFERENCES policy_versions(id),
    completed_at        TIMESTAMPTZ
);
CREATE INDEX idx_control_actions_unit_ts ON control_actions (unit_key, ts DESC);
CREATE INDEX idx_control_actions_status ON control_actions (status) WHERE status IN ('queued', 'sent');

-- ---------------------------------------------------------------------------------------
-- Experiments
-- ---------------------------------------------------------------------------------------

CREATE TABLE experiments (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    name                TEXT NOT NULL,
    hypothesis          TEXT NOT NULL,
    metric              TEXT NOT NULL DEFAULT 'house_runtime_residual',
    arms                JSONB NOT NULL,                   -- [{key, label, params}]
    design              JSONB NOT NULL,                   -- {block_days, seed, n_days, checkpoints[], alpha}
    status              TEXT NOT NULL CHECK (status IN ('proposed', 'approved', 'running', 'stopped', 'completed', 'rejected')),
    proposed_by         TEXT NOT NULL CHECK (proposed_by IN ('model', 'claude', 'owner')),
    start_date          DATE,
    end_date            DATE,
    result              JSONB,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE experiment_days (
    experiment_id       BIGINT NOT NULL REFERENCES experiments(id),
    day                 DATE NOT NULL,
    arm                 TEXT NOT NULL,
    actual_s            DOUBLE PRECISION,
    expected_s          DOUBLE PRECISION,
    residual_s          DOUBLE PRECISION,
    included            BOOLEAN NOT NULL DEFAULT TRUE,
    note                TEXT,
    PRIMARY KEY (experiment_id, day)
);

-- ---------------------------------------------------------------------------------------
-- Models, jobs, Claude, reports, alerts
-- ---------------------------------------------------------------------------------------

CREATE TABLE model_fits (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind                TEXT NOT NULL CHECK (kind IN ('baseline', 'rc', 'room_offsets', 'occupancy_priors')),
    unit_key            TEXT REFERENCES units(key),
    mode                TEXT CHECK (mode IN ('cool', 'heat')),
    train_start         DATE,
    train_end           DATE,
    params              JSONB NOT NULL,
    metrics             JSONB NOT NULL DEFAULT '{}'::jsonb,
    status              TEXT NOT NULL CHECK (status IN ('candidate', 'active', 'retired', 'failed')),
    notes               TEXT
);
CREATE INDEX idx_model_fits_kind_status ON model_fits (kind, status);

CREATE TABLE jobs (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind                TEXT NOT NULL,                    -- 'refit' | 'backtest' | 'backfill' | 'report'
    status              TEXT NOT NULL CHECK (status IN ('queued', 'running', 'done', 'failed')),
    requested_by        TEXT NOT NULL,
    params              JSONB NOT NULL DEFAULT '{}'::jsonb,
    result              JSONB,
    error               TEXT,
    started_at          TIMESTAMPTZ,
    finished_at         TIMESTAMPTZ
);
CREATE INDEX idx_jobs_status ON jobs (status) WHERE status IN ('queued', 'running');

CREATE TABLE agent_runs (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind                TEXT NOT NULL CHECK (kind IN ('nightly', 'weekly', 'triggered', 'chat', 'signin_check')),
    status              TEXT NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'failed', 'deferred', 'cancelled')),
    requested_by        TEXT NOT NULL,                    -- 'schedule' | 'owner' | 'anomaly'
    prompt              TEXT NOT NULL DEFAULT '',
    trigger             JSONB,
    not_before          TIMESTAMPTZ,                      -- deferred runs wait until here
    started_at          TIMESTAMPTZ,
    finished_at         TIMESTAMPTZ,
    model               TEXT,
    terminal_reason     TEXT,
    num_turns           INT,
    result_text         TEXT,
    usage               JSONB,
    error               TEXT,
    session_id          TEXT
);
CREATE INDEX idx_agent_runs_status ON agent_runs (status, created_at);

CREATE TABLE reports (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind                TEXT NOT NULL CHECK (kind IN ('daily', 'weekly', 'nightly', 'anomaly', 'note')),
    author              TEXT NOT NULL CHECK (author IN ('system', 'claude')),
    period_start        DATE,
    period_end          DATE,
    title               TEXT NOT NULL,
    body_md             TEXT NOT NULL,
    data                JSONB NOT NULL DEFAULT '{}'::jsonb,
    agent_run_id        BIGINT REFERENCES agent_runs(id)
);
CREATE INDEX idx_reports_kind_created ON reports (kind, created_at DESC);

CREATE TABLE alerts (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    level               TEXT NOT NULL CHECK (level IN ('info', 'warn', 'error')),
    kind                TEXT NOT NULL,                    -- stable code, e.g. 'ecobee_auth', 'sensor_offline'
    title               TEXT NOT NULL,
    body                TEXT NOT NULL DEFAULT '',
    dedupe_key          TEXT,
    resolved_at         TIMESTAMPTZ,
    notified_at         TIMESTAMPTZ
);
CREATE UNIQUE INDEX uq_alerts_open_dedupe ON alerts (dedupe_key) WHERE resolved_at IS NULL AND dedupe_key IS NOT NULL;

-- ---------------------------------------------------------------------------------------
-- Settings, secrets and device discovery
-- ---------------------------------------------------------------------------------------

CREATE TABLE app_settings (
    key                 TEXT PRIMARY KEY,
    value               JSONB NOT NULL,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by          TEXT NOT NULL DEFAULT 'system'
);

-- Fernet-encrypted blobs: 'ecobee_refresh_token', 'homekit_pairing:<alias>'.
CREATE TABLE secrets (
    key                 TEXT PRIMARY KEY,
    ciphertext          BYTEA NOT NULL,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Thermostats seen through the ecobee cloud (written by the worker after sign-in).
CREATE TABLE ecobee_thermostats (
    identifier          TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    model_number        TEXT,
    unit_key            TEXT REFERENCES units(key),
    sensors             JSONB NOT NULL DEFAULT '[]'::jsonb, -- [{id, name, type, capabilities}]
    settings            JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_revision       TEXT,
    last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- HomeKit accessories seen over mDNS, plus the DB-mediated pairing handshake between the
-- web UI (writes pairing_state/pairing_code) and the host-networked homekit service.
CREATE TABLE homekit_devices (
    device_id           TEXT PRIMARY KEY,                 -- lower-case 'aa:bb:cc:dd:ee:ff'
    name                TEXT NOT NULL,
    model               TEXT,
    category            INT,
    address             TEXT,
    port                INT,
    status_flags        INT,                              -- bit 0x01 = unpaired, ready to pair
    config_num          INT,
    alias               TEXT UNIQUE,
    unit_key            TEXT REFERENCES units(key),
    pairing_state       TEXT NOT NULL DEFAULT 'none' CHECK (pairing_state IN (
                            'none', 'requested', 'awaiting_code', 'code_submitted', 'paired',
                            'unpair_requested', 'failed')),
    pairing_code        TEXT,                             -- transient; cleared once used
    pairing_error       TEXT,
    accessories         JSONB,                            -- inventory: [{aid, name, model, chars{}}]
    online              BOOLEAN NOT NULL DEFAULT FALSE,
    last_seen_at        TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
