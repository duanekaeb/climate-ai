-- 003: utility (demand-response) events, the opt-out action, and the hand-back job.
-- Runs inside one transaction owned by climate.store.migrate (no BEGIN/COMMIT here).

-- One row per (thermostat, utility event). ecobee lists announced events before they start,
-- so a row is usually born 'announced'; the same event on several thermostats shares
-- event_key. Written by climate.utility.events (ingest + clock sweep) and climate.utility.skips.
CREATE TABLE IF NOT EXISTS utility_events (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    unit_key            TEXT NOT NULL REFERENCES units(key),
    event_key           TEXT NOT NULL,                    -- 'link:<linkRef>' or 'demandResponse:<name>:<start iso>'
    event_type          TEXT NOT NULL DEFAULT 'demandResponse',
    name                TEXT,
    status              TEXT NOT NULL CHECK (status IN ('announced', 'running', 'ended', 'cancelled', 'opted_out')),
    start_at            TIMESTAMPTZ,
    end_at              TIMESTAMPTZ,
    first_seen_at       TIMESTAMPTZ NOT NULL,
    last_seen_at        TIMESTAMPTZ NOT NULL,
    started_at          TIMESTAMPTZ,                      -- first seen running
    ended_at            TIMESTAMPTZ,                      -- first seen over (ended, gone, opted out)
    heat_f              REAL,                             -- absolute setpoints, when the event sets them
    cool_f              REAL,
    is_relative         BOOLEAN NOT NULL DEFAULT FALSE,
    heat_offset_f       REAL,                             -- relative change, e.g. cool +2.0
    cool_offset_f       REAL,
    is_optional         BOOLEAN,                          -- FALSE = mandatory: ecobee refuses an opt-out
    duty_cycle_pct      INT,
    skip                TEXT CHECK (skip IN ('requested', 'done', 'failed', 'refused')),
    skip_by             TEXT CHECK (skip_by IN ('owner', 'rule')),
    skip_reason         TEXT,
    skip_requested_at   TIMESTAMPTZ,
    skip_done_at        TIMESTAMPTZ,
    skip_action_id      BIGINT REFERENCES control_actions(id),
    detail              JSONB NOT NULL DEFAULT '{}'::jsonb,  -- the last ThermostatEvent seen
    UNIQUE (unit_key, event_key)
);
CREATE INDEX IF NOT EXISTS idx_utility_events_open ON utility_events (unit_key, status)
    WHERE status IN ('announced', 'running');
CREATE INDEX IF NOT EXISTS idx_utility_events_window ON utility_events (start_at, end_at);

-- Opting out of a utility event is its own logged thermostat write.
ALTER TABLE control_actions DROP CONSTRAINT IF EXISTS control_actions_action_check;
ALTER TABLE control_actions ADD CONSTRAINT control_actions_action_check
    CHECK (action IN ('set_hold', 'resume_program', 'update_program', 'update_settings', 'opt_out_event'));

/*
-- rollback (manual)
ALTER TABLE control_actions DROP CONSTRAINT IF EXISTS control_actions_action_check;
ALTER TABLE control_actions ADD CONSTRAINT control_actions_action_check
    CHECK (action IN ('set_hold', 'resume_program', 'update_program', 'update_settings'));
DROP TABLE IF EXISTS utility_events;
DELETE FROM schema_migrations WHERE id = '003_utility_events_and_handback';
*/
