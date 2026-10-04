-- 004: real user accounts, per-device sessions, API tokens and an audit trail.
-- Runs inside one transaction owned by climate.store.migrate (no BEGIN/COMMIT here).
-- Spec: docs/specs/users-and-tokens.md. Secrets are never stored raw: passwords are
-- Argon2id (or the legacy scrypt hash until the next sign-in), every token is an
-- HMAC-SHA256 with CLIMATE_TOKEN_PEPPER.

CREATE TABLE IF NOT EXISTS users (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username            TEXT NOT NULL,                    -- what people type to sign in
    username_normalized TEXT NOT NULL UNIQUE,             -- lower(trim(username))
    display_name        TEXT NOT NULL DEFAULT '',
    password_hash       TEXT,                             -- NULL until an invitation is accepted
    role                TEXT NOT NULL CHECK (role IN ('admin', 'member', 'viewer')),
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    failed_login_count  INT NOT NULL DEFAULT 0,
    last_failed_at      TIMESTAMPTZ,
    locked_until        TIMESTAMPTZ,
    last_login_at       TIMESTAMPTZ,
    password_changed_at TIMESTAMPTZ,
    created_by          BIGINT REFERENCES users(id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per signed-in device. The refresh token "<id hex>.<secret>" lives in an HttpOnly
-- cookie scoped to /api/auth; it rotates on every refresh and a reused old one revokes the
-- whole family (token theft).
CREATE TABLE IF NOT EXISTS auth_sessions (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id             BIGINT NOT NULL REFERENCES users(id),
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
CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions (user_id) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_auth_sessions_family ON auth_sessions (family_id);

CREATE TABLE IF NOT EXISTS user_invitations (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username_normalized TEXT NOT NULL,
    role                TEXT NOT NULL CHECK (role IN ('admin', 'member', 'viewer')),
    token_hash          TEXT NOT NULL UNIQUE,
    invited_by          BIGINT NOT NULL REFERENCES users(id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at          TIMESTAMPTZ NOT NULL,
    accepted_at         TIMESTAMPTZ,
    accepted_user_id    BIGINT REFERENCES users(id),
    revoked_at          TIMESTAMPTZ
);

-- No email in a home app: an admin issues a one-time reset link and hands it over.
CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id             BIGINT NOT NULL REFERENCES users(id),
    token_hash          TEXT NOT NULL UNIQUE,
    issued_by_user_id   BIGINT REFERENCES users(id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at          TIMESTAMPTZ NOT NULL,
    used_at             TIMESTAMPTZ
);

-- Bearer tokens for the Claude agent, the MCP server, scripts and devices:
-- "cai_<id hex>_<secret>", shown once, stored as an HMAC. Never the admin role.
CREATE TABLE IF NOT EXISTS api_tokens (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name                TEXT NOT NULL,
    token_hint          TEXT NOT NULL,                    -- last 4 characters, for recognising it
    token_hash          TEXT NOT NULL UNIQUE,
    role                TEXT NOT NULL CHECK (role IN ('agent', 'viewer', 'member')),
    local_only          BOOLEAN NOT NULL DEFAULT TRUE,    -- refused from public (internet) addresses
    created_by_user_id  BIGINT REFERENCES users(id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at          TIMESTAMPTZ,
    last_used_at        TIMESTAMPTZ,
    last_used_ip        TEXT,
    revoked_at          TIMESTAMPTZ,
    revoked_by          BIGINT REFERENCES users(id),
    revoked_reason      TEXT
);

CREATE TABLE IF NOT EXISTS audit_events (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor_type          TEXT NOT NULL CHECK (actor_type IN ('user', 'api_token', 'system')),
    actor_id            BIGINT,
    actor_label         TEXT NOT NULL DEFAULT '',         -- username / token name at the time
    actor_role          TEXT,
    event_type          TEXT NOT NULL,                    -- e.g. 'auth.login', 'user.create', 'token.revoke'
    target_type         TEXT,
    target_id           TEXT,
    payload             JSONB NOT NULL DEFAULT '{}'::jsonb, -- never secrets
    ip                  TEXT,
    request_id          TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_events_ts ON audit_events (ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_events_type_ts ON audit_events (event_type, ts DESC);

-- Who asked for an owner action from the app (NULL for older rows and for services).
ALTER TABLE control_actions ADD COLUMN IF NOT EXISTS requested_by_user_id BIGINT REFERENCES users(id);

/*
-- rollback (manual)
ALTER TABLE control_actions DROP COLUMN IF EXISTS requested_by_user_id;
DROP TABLE IF EXISTS audit_events, api_tokens, password_reset_tokens, user_invitations, auth_sessions, users;
DELETE FROM schema_migrations WHERE id = '004_users_and_tokens';
*/
