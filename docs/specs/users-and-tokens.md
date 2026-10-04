# Spec: the owner login, signed-in devices and API tokens

One login (the owner password), real bearer tokens, per-device sessions and API tokens for
services. No multiple user accounts (the owner's call: a household utility needs one login).
Patterns follow GrowWise AI's auth (`growwise-ai/apps/api/app/core/security.py`, `core/deps.py`,
`modules/auth/*`) minus its multi-user and family domain. Contract in code: migration
`004_users_and_tokens.sql` (`auth_sessions`, `api_tokens`, `audit_events`), ORM `AuthSession`,
`ApiToken`, `AuditEvent`, settings in `config.py` (`jwt_secret`, `token_pepper`,
`access_ttl_minutes`, `refresh_ttl_days`, `session_max_days`, `reauth_window_minutes`,
`password_min_length`, `public_login_max_failures`, `public_login_window_minutes`,
`login_pause_minutes`, `allow_remote_setup`), wire types in `api/schemas.py` (`Role`,
`TokenRole`, `AuthState` ... `AuthErrorDetail`).

## The password

- Stored only as an **Argon2id hash** (`argon2-cffi` `PasswordHasher()`) in
  `app_settings['owner'].password_hash`. Never stored or logged in plain text, never
  reversible. Existing `scrypt$...` hashes keep working and are rehashed to Argon2id on the next
  successful sign-in (`needs_rehash`). `CLIMATE_OWNER_PASSWORD` (env) still works as before:
  hashed into the setting on first start, then ignored.
- New passwords: at least `password_min_length` (10) characters; the first-run screen and
  change-password enforce it. An existing shorter password still signs in.
- First run (`POST /auth/setup`): atomic "no password yet" claim (the existing
  `claim_first_password` pattern); **only from a private address** (LAN, Docker, Tailscale:
  10/8, 172.16/12, 192.168/16, 127/8, 100.64/10, ::1, fc00::/7, fe80::/10, after trusted proxy
  headers) unless `allow_remote_setup`. Public visitors get 403 `SETUP_NOT_ALLOWED`.
- Change password: needs the current one; revokes every other signed-in device and writes an
  audit row. Break-glass: `python -m climate.cli set-password` (revokes all sessions).

## Tokens

- **Access token:** JWT HS256 signed with `CLIMATE_JWT_SECRET`, 15 min; claims `sub="owner"`,
  `role="owner"`, `sid` (session id), `iat`, `exp`, `jti`, `typ="access"`, `iss`/`aud`
  `"climate-ai"`. JSON only; the web keeps it in memory. Every request re-checks the session row
  (revoked/expired → 401 `SESSION_REVOKED`), so sign-out and password changes take effect at once.
- **Refresh token:** `"<session id hex>.<secret>"` in cookie `climate_refresh`: HttpOnly,
  `Path=/api/auth`, `SameSite=Lax`, `Secure=CLIMATE_COOKIE_SECURE`. Rotates on every refresh;
  presenting the previous secret again revokes the session family (`reuse_detected`, audit).
  Sliding `refresh_ttl_days` (30), hard cap `session_max_days` (90) from sign-in.
- **API tokens:** `"cai_<id hex>_<secret>"` (`secret = token_urlsafe(32)`), stored as
  HMAC-SHA256(`CLIMATE_TOKEN_PEPPER`), hint = last 4 chars, **shown once**. Roles:
  `agent` (read + the gated agent endpoints: what the Claude agent and MCP server need),
  `viewer` (read only), `control` (read + everyday controls: holds, resume, back to automatic,
  presence, skip utility events). Never the owner's full rights (no settings, mode, setup,
  tokens, hand-back). `local_only` (default true) refuses the token from public addresses.
  Live checks on every use (revoked, expired). `last_used_at` / `last_used_ip` written at most
  once a minute. Creating a token needs the password re-entered within `reauth_window_minutes`.
- **Legacy env tokens:** `CLIMATE_AGENT_TOKEN` / `CLIMATE_MCP_TOKEN` keep working as role
  `agent`, constant-time compared, private addresses only. The agent container uses whichever
  is in `.env`; a `cai_` agent token from the app works the same.
- One `hash_token()` (HMAC-SHA256 with the pepper) for refresh and API tokens. With an empty
  `jwt_secret` or `token_pepper` (outside tests) sign-in and token checks return 503
  `AUTH_NOT_CONFIGURED` and `climate.cli doctor` fails.

## Brute force

- Per-IP: 10 failed sign-ins per 5 minutes (in memory), Argon2 cost on every attempt (a fixed
  dummy hash when no password is set, for constant timing), at most 2 hash checks at once.
- Distributed attempts: failed sign-ins **from public addresses** are counted globally; past
  `public_login_max_failures` in `public_login_window_minutes`, sign-in from the internet
  pauses for `login_pause_minutes` (429 `LOGIN_PAUSED`, audit + warn alert). Sign-in from home
  or Tailscale always works and already signed-in devices are unaffected, so nobody can lock
  the owner out. The gateway adds its own per-IP rate limit in front.
- The old global 30-attempt cap goes (it let anyone lock the owner out).

## Who may call what

| Dependency (`climate.api.auth`) | Allowed | Used for |
|---|---|---|
| `ReaderDep` | owner, agent, viewer, control | every GET except tokens / sessions / audit |
| `ControlDep` (new) | owner, control | `POST /control/hold`, `/control/resume`, `/control/automatic`, `/control/presence`, `/utility-events/{id}/skip`, `/unskip` |
| `OwnerDep` | owner | everything else that changes state: mode, settings, hand-back, setup, decisions, alerts resolve, agent ask/run, tokens, sessions, audit |
| `RecentAuthDep` | owner with the password re-entered in the last `reauth_window_minutes` | creating API tokens |
| `WriterDep` | owner, agent | the gated agent writes it covers today (propose, sign off / hold) |
| `AgentDep` | agent | `/agent/claim`, `/agent/heartbeat`, `/agent/runs/{id}/finish` |

The agent role can never reach a thermostat write, the controller mode, settings, setup or
tokens. **A guard test walks every route** and asserts this matrix for owner, agent / viewer /
control tokens, the legacy env token, and anonymous; the only public routes are `/api/health`,
`/api/auth/state`, `/api/auth/setup`, `/api/auth/login`, `/api/auth/refresh`, `/api/auth/logout`
and the static web app.

## Endpoints

- Public: `GET /auth/state` → `AuthState`; `POST /auth/setup` (`SetupBody`) → `AccessTokenOut`
  + cookie; `POST /auth/login` (`LoginBody`) → `AccessTokenOut` + cookie; `POST /auth/refresh`
  (cookie) → `AccessTokenOut` + rotated cookie; `POST /auth/logout` (cookie, optional bearer) →
  204 (revokes that device, clears cookie). `refresh` and `logout` keep the same-origin check
  (Origin vs Host / X-Forwarded-Host) because they ride a cookie.
- Owner: `GET /auth/me` (any caller) → `MeOut`; `POST /auth/reauth` (`ReauthBody`) → `MeOut`;
  `POST /auth/change-password` → 204; `GET /auth/sessions` → `SessionOut[]`;
  `DELETE /auth/sessions/{id}` → 204; `POST /auth/logout-all` → 204 (every device, this one
  too); `POST /auth/ws-ticket` (any caller) → `WsTicketOut` (single use, 30 s, in memory,
  carries the caller's role).
- Tokens (owner): `GET /tokens` → `ApiTokenOut[]`; `POST /tokens` (`ApiTokenCreateBody`,
  RecentAuthDep) → `ApiTokenCreated`; `DELETE /tokens/{id}` → 204.
- Audit (owner): `GET /audit?event_type=&limit=` → `AuditEventOut[]`.
- Every auth action writes `audit_events` in the same transaction: `auth.setup`, `auth.login`,
  `auth.login_failed`, `auth.login_paused`, `auth.logout`, `auth.logout_all`,
  `auth.refresh_reuse`, `auth.reauth`, `password.change`, `session.revoke`, `token.create`,
  `token.revoke`; payloads never contain secrets.
- Errors from the auth layer: `HTTPException(status, detail={"code", "message"})`
  (`AuthErrorDetail`). Other errors keep FastAPI's plain `{"detail": "..."}`.
- WebSocket `/api/ws`: `?ticket=` (single use) or an `Authorization: Bearer` header (tokens /
  agent). The old cookie path goes. Origin check stays for ticket connections.
- The old `climate_session` cookie, `/auth/logout-everywhere` and the session epoch go;
  existing signed-in devices sign in again once after the upgrade (expected, say so in docs).

## CLI (`python -m climate.cli`)

`set-password [--password-stdin]` (break-glass; revokes all devices), `create-token --name N
[--role agent|viewer|control] [--expires-days D] [--allow-remote] [--print-only-token]` (prints
the token once), `list-tokens`, `revoke-token --id N`, `sign-out-everywhere`. Same service
functions as the API, audit actor `system`.

## Web

`web/src/api/client.ts`: bearer from memory; `credentials: 'include'` for `/api/auth/*`;
single-flight refresh on 401 `TOKEN_EXPIRED` + one replay; 401 `SESSION_REVOKED` /
`NOT_AUTHENTICATED` → clear and go to `/login`; 403 `REAUTHENTICATION_REQUIRED` → a password
dialog (`/auth/reauth`) then one replay. `bootstrapSession()` (refresh) runs before the router
guards. Login (password; device name prefilled), first-run (choose + confirm; "only from your
home network" when `setup_allowed` is false). A **Security** page (More → Security): change
password, signed-in devices (name, last seen, IP; revoke; "this device"), sign out everywhere,
API tokens (create: name, role with plain-words help, expiry, "home network only" switch;
one-time reveal with copy + warning; list with hint, role, last used, expiry; revoke), and the
recent audit log. Everything else as today for the owner. WebSocket uses a ticket. iOS
WKWebView keeps working (the refresh cookie persists; the access token is rebuilt on launch).
Light/dark, 375 px.

## Ownership (parallel builders)

| Builder | Files |
|---|---|
| auth-core | new `api/climate/api/security.py` (hashing, tokens, JWT, private-address check), new `api/climate/auth_service.py` (password, sessions, refresh rotation, brute force, API tokens, ws tickets, audit), `api/climate/cli.py` auth commands, `api/pyproject.toml` (argon2-cffi, PyJWT), tests `test_auth_service.py`, `test_cli_tokens.py` |
| auth-api | `api/climate/api/auth.py` (resolver + deps), `routers/auth.py`, new `routers/tokens.py` (tokens + audit), `routers/ws.py`, dependency lines in every `routers/*.py`, `api/app.py`, `tests/conftest.py`, `test_api_auth.py`, `test_api_roles.py`, new `test_api_tokens.py`, new `test_route_guard.py`, fixture updates in other `test_api_*.py` |
| web | `web/src/**` (except generated `api/types.ts` / `schema.json`) |
| agent-ios | `agent/**`, `ios/**` |
| local-run | `docker-compose.yml`, `.env.example`, `docker/*`, `scripts/*`, `Makefile`, `README.md`, `docs/DEPLOY.md`, new `docs/LOCAL_DEVELOPMENT.md`, the `doctor` command in `cli.py` only |
| homekit | `sources/homekit.py`, `collector/homekit_service.py`, `docs/HOMEKIT.md`, homekit tests |
| gateway | `/home/user/growwise-ai/apprelay/**` (PR), Climate's `docker-compose.nginx.yml`, `deploy/nginx/*`, new `docs/PUBLIC_ACCESS.md` |
