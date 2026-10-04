-- 004: per-device sign-in sessions, API tokens and an audit trail for the single owner login.
-- Runs inside one transaction owned by climate.store.migrate (no BEGIN/COMMIT here).
-- Spec: docs/specs/users-and-tokens.md. There is one login (the owner password, Argon2id in
-- app_settings['owner']). Secrets are never stored raw: every token is an HMAC-SHA256 with
-- CLIMATE_TOKEN_PEPPER.

-- One row per signed-in device. The refresh token "<id hex>.<secret>" lives in an HttpOnly
-- cookie scoped to /api/auth; it rotates on every refresh and a reused old one revokes the
-- whole family (token theft). Changing the password or "sign out everywhere" revokes all.
CREATE TABLE IF NOT EXISTS auth_sessions (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    refresh_token_hash  TEXT NOT NULL,
    previous_token_hash TEXT,                             -- the one before the last rotation (reuse detection)
    family_id           TEXT NOT NULL,
    rotation_counter    INT NOT NULL DEFAULT 0,
    device_name         TEXT NOT NULL DEFAULT '',
    user_agent          TEXT NOT NULL DEFAULT '',
    ip                  TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at          TIMESTAMPTZ NOT NULL,             -- sliding (CLIMATE_REFRESH_TTL_DAYS)
    absolute_expires_at TIMESTAMPTZ NOT NULL,             -- hard cap (CLIMATE_SESSION_MAX_DAYS)
    reauthenticated_at  TIMESTAMPTZ,
    revoked_at          TIMESTAMPTZ,
    revoked_reason      TEXT
);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_open ON auth_sessions (last_seen_at DESC) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_auth_sessions_family ON auth_sessions (family_id);

-- Bearer tokens for the Claude agent, the MCP server, scripts and devices:
-- "cai_<id hex>_<secret>", shown once, stored as an HMAC. Never the owner's full rights.
CREATE TABLE IF NOT EXISTS api_tokens (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name                TEXT NOT NULL,
    token_hint          TEXT NOT NULL,                    -- last 4 characters, for recognising it
    token_hash          TEXT NOT NULL UNIQUE,
    role                TEXT NOT NULL CHECK (role IN ('agent', 'viewer', 'control')),
    local_only          BOOLEAN NOT NULL DEFAULT TRUE,    -- refused from public (internet) addresses
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at          TIMESTAMPTZ,
    last_used_at        TIMESTAMPTZ,
    last_used_ip        TEXT,
    revoked_at          TIMESTAMPTZ,
    revoked_reason      TEXT
);

CREATE TABLE IF NOT EXISTS audit_events (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor_type          TEXT NOT NULL CHECK (actor_type IN ('owner', 'api_token', 'system')),
    actor_id            BIGINT,                           -- session id (owner) or token id
    actor_label         TEXT NOT NULL DEFAULT '',         -- device name / token name at the time
    event_type          TEXT NOT NULL,                    -- e.g. 'auth.login', 'token.create'
    target_type         TEXT,
    target_id           TEXT,
    payload             JSONB NOT NULL DEFAULT '{}'::jsonb, -- never secrets
    ip                  TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_events_ts ON audit_events (ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_events_type_ts ON audit_events (event_type, ts DESC);

/*
-- rollback (manual)
DROP TABLE IF EXISTS audit_events, api_tokens, auth_sessions;
DELETE FROM schema_migrations WHERE id = '004_users_and_tokens';
*/
