# Deploying Climate AI on a home server

Climate AI runs as a small Docker Compose stack on one always-on machine at home, behind the
nginx you already run for your GrowWise services. It works with **no credentials at all**
(simulator mode) so you can try everything before connecting the real thermostats. Trying it on
a Mac or a laptop first? [LOCAL_DEVELOPMENT.md](LOCAL_DEVELOPMENT.md) covers that; the start is
the same `make bootstrap`.

| Service | What it is | Port (host side) | Default |
|---|---|---|---|
| `db` | PostgreSQL 16 + TimescaleDB 2.30.2 | `127.0.0.1:5433` (`DB_PORT`) | on |
| `app` | API + websocket + the web app | `127.0.0.1:8470` (`APP_BIND`, `APP_PORT`) | on |
| `worker` | data source, weather, controller, nightly jobs | none | on |
| `agent` | Claude Agent SDK on your Claude subscription | none | on (idle without a token) |
| `homekit` | local HomeKit controller (host network, Linux only) | host network | profile `homekit` |
| `mcp` | MCP server for Claude Code / Desktop | `127.0.0.1:8471` (`MCP_BIND`, `MCP_PORT`) | profile `mcp` |
| `ntfy` | self-hosted push notifications | `127.0.0.1:8472` (`NTFY_PORT`) | profile `ntfy` |

Everything binds to `127.0.0.1`. The ways in are, from most to least private: your home network
(section 6, or `APP_BIND=0.0.0.0` for plain http), Tailscale (section 9), and a public name
through the GrowWise AppRelay gateway (section 8). **Never port-forward any of these ports on
your router**: a public name goes only through the gateway, with HTTPS and the checks in
section 8, because the app can change your thermostats. The database, the MCP server and ntfy
never leave the machine.

---

## 1. Prerequisites

- **A Linux host is strongly recommended** (any x86-64 or 64-bit ARM box: mini PC, NUC, NAS
  with Docker, Raspberry Pi 4/5 with a 64-bit OS). The HomeKit container needs host networking
  and multicast DNS, which only Docker Engine on Linux provides. A Mac runs everything else in
  Docker Desktop and HomeKit natively ([LOCAL_DEVELOPMENT.md](LOCAL_DEVELOPMENT.md), section 5).
- Docker Engine 24+ with the Compose v2 plugin (`docker compose version`); for HomeKit, Docker
  Engine itself (docker-ce), not Docker Desktop for Linux or rootless Docker.
- About 2 GB of free RAM and 10 GB of disk (images: TimescaleDB about 2.5 GB, the app and the
  agent about 650 MB each, plus build cache).
- For HomeKit: the server on the same LAN/VLAN as the thermostats ([HOMEKIT.md](HOMEKIT.md)).
- `git`, `make` and `curl` on the host (`openssl` is used when present).

## 2. Get the code

```bash
git clone <your repo URL> climate-ai
cd climate-ai
```

## 3. Configure and start

```bash
make bootstrap                    # = scripts/bootstrap.sh; on Linux with HomeKit: ARGS=--homekit
```

It creates `.env` from `.env.example` (mode 600), generates every empty secret without printing
it, keeps every value you already set, moves the app / database port if another program holds
it, runs `docker compose up -d --build`, waits until the app is healthy and prints the address.
Run it again whenever you like; after an update it adds any secret your `.env` lacks.
([LOCAL_DEVELOPMENT.md](LOCAL_DEVELOPMENT.md), section 2, has the details and options.)

What the secrets do:

| Variable | Used for | If it changes or is lost |
|---|---|---|
| `POSTGRES_PASSWORD` | the database login | the app cannot reach the existing database (the volume keeps the old one) |
| `CLIMATE_SECRET_KEY` | Fernet key: encrypts the ecobee sign-in and the HomeKit pairing keys in the database | **unrecoverable**: sign in to ecobee again and reset + re-pair every thermostat. Back it up now, separately from the database backups (password manager). |
| `CLIMATE_JWT_SECRET` | signs the 15-minute access tokens | current access tokens end; signed-in devices renew silently |
| `CLIMATE_TOKEN_PEPPER` | keys the hashes of signed-in devices' refresh tokens and of API tokens | every device signs in again; every API token stops working |
| `CLIMATE_AGENT_TOKEN`, `CLIMATE_MCP_TOKEN` | the agent's and the MCP server's service tokens (role agent, home addresses only) | change both sides (the same `.env`), then `docker compose up -d` |

Without `CLIMATE_JWT_SECRET` and `CLIMATE_TOKEN_PEPPER` (at least 32 characters each) nobody can
sign in, so compose refuses to start until they are set. `CLIMATE_SESSION_SECRET` from older
versions is no longer used; delete it.

By hand instead of bootstrap (any shell; `sed -i.bak` works with both GNU and BSD sed):

```bash
cp .env.example .env && chmod 600 .env
for k in POSTGRES_PASSWORD CLIMATE_JWT_SECRET CLIMATE_TOKEN_PEPPER CLIMATE_AGENT_TOKEN CLIMATE_MCP_TOKEN; do
  sed -i.bak "s|^$k=\$|$k=$(openssl rand -hex 32)|" .env
done
sed -i.bak "s|^CLIMATE_SECRET_KEY=\$|CLIMATE_SECRET_KEY=$(openssl rand -base64 32 | tr '+/' '-_')|" .env
rm -f .env.bak
docker compose up -d --build
```

Then open `.env` and review the rest. Every variable is commented. The important ones:

- `TZ`: your time zone, for log timestamps (the house time zone is set in the app's Setup).
- `CLIMATE_COOKIE_SECURE`: `true` as soon as you browse through HTTPS (sections 6 and 8). Over
  plain http the browser drops the sign-in cookie and sign-in does not stick, so bootstrap
  writes `false` into a new `.env`. `make doctor` flags an https `CLIMATE_PUBLIC_URL` with
  `false`.
- `CLIMATE_PUBLIC_URL`: the address you open the app at, e.g. `https://climate.example.home`.
- **Never set `ANTHROPIC_API_KEY`** here or anywhere on the server (section 11).

Check it:

```bash
docker compose ps                 # db and app become "healthy"; worker and agent "running"
curl -s http://127.0.0.1:8470/api/health     # {"ok":true,...}
make doctor                       # every check; "owner password" fails until section 4
```

On the first start the app applies database migrations and seeds the house; the worker then
generates synthetic history for the simulator. Until you do section 11 the agent has no Claude
sign-in: it stays up and idle (Claude never runs) and reports "not signed in"; nothing else is
affected.

## 4. Choose the owner password, from home, first

There is one login: the owner password. No user accounts; every phone, tablet and laptop signs
in with it and becomes its own signed-in device. The password is stored only as an Argon2id hash
and is never logged.

**Choose it before the app is reachable from anywhere else.** The first screen asks for it (at
least 10 characters; a long passphrase from your password manager is best), and it accepts the
choice only from a private address: this machine, your home network, Docker or Tailscale. A
request from the internet gets "choose the password from your home network". The app listens on
the server's `127.0.0.1:8470` only, so before nginx is set up use an SSH tunnel from your laptop:

```bash
ssh -N -L 8470:127.0.0.1:8470 you@server
# then open http://localhost:8470 and choose the password
```

Or on the server itself: `make password` (= `docker compose exec app python -m
climate.cli set-password`). That is also the way back in if you forget it; it signs every
device out. (`CLIMATE_OWNER_PASSWORD` in `.env` also works: it is hashed into the database on
the first start and ignored afterwards, so delete it from `.env` once the app has started.)

**Upgrading from a version before the bearer-token sign-in:** every device signs in again once
(the old session cookie is gone); the password itself is kept and upgraded to Argon2id on the
next sign-in.

## 5. Signed-in devices and API tokens

**More > Security** in the app:

- **Change password**: needs the current one; signs out every other device.
- **Signed-in devices**: name, last seen, address; sign out any of them, or every device at
  once (also this one). A lost phone: sign it out here (or everywhere), then change the
  password.
- **API tokens** for services (a dashboard, a script, Home Assistant): pick a name, a role, an
  expiry and whether it is limited to your home network. The token (`cai_...`) is shown
  **once**; copy it then. Creating one asks for the password again if you have not entered it
  in the last 10 minutes. Revoke a token and it stops working at once. A token is never the
  owner: none can change settings, the controller mode or setup, or manage tokens.

  | Role | May do |
  |---|---|
  | `viewer` | read everything the app shows |
  | `control` | read, plus everyday controls: holds, resume, back to automatic, presence, skip a utility event |
  | `agent` | read, plus the gated agent tools (propose, sign off / hold): what the Claude agent and the MCP server need. Never a thermostat write. |

  "Home network only" (the default) refuses the token from internet addresses; leave it on
  unless the service really calls in from outside.
- **Recent activity**: the audit log (sign-ins, failed sign-ins, password changes, device and
  token changes). Never contains a password or a token.

The same from the server's shell: `make token NAME=dashboard ROLE=viewer` (`DAYS=90`,
`REMOTE=1`), `make tokens`, `docker compose exec app python -m climate.cli revoke-token --id N`,
`docker compose exec app python -m climate.cli sign-out-everywhere`.

Use a token with `Authorization: Bearer cai_...`. The agent and MCP containers use
`CLIMATE_AGENT_TOKEN` / `CLIMATE_MCP_TOKEN` from `.env` (generated by bootstrap; accepted from
home and Docker addresses only). You may replace either with an `agent`-role `cai_` token from
the Security page, which you can then revoke from there.

## 6. Put it behind your nginx (HTTPS at home)

### A. nginx runs on the host

```bash
sudo cp deploy/nginx/climate.conf /etc/nginx/conf.d/climate.conf
sudoedit /etc/nginx/conf.d/climate.conf      # server_name, TLS lines
sudo nginx -t && sudo systemctl reload nginx
```

### B. nginx runs in Docker

```bash
docker network ls                    # find your nginx container's network
# add to .env:
#   NGINX_NETWORK=<that network>
#   COMPOSE_FILE=docker-compose.yml:docker-compose.nginx.yml
docker compose up -d                 # attaches app to that network as climate-ai-app
# copy deploy/nginx/climate-docker-network.conf into the directory your nginx container
# includes (usually a bind-mounted conf.d), edit server_name and TLS, then:
docker exec <nginx-container> nginx -t && docker exec <nginx-container> nginx -s reload
```

Both files proxy the websocket at `/api/ws` with the HTTP/1.1 upgrade headers and a long read
timeout, set `X-Forwarded-For` to the real client address, and contain a commented
`allow`/`deny` block for LAN + Tailscale only.

**The client address matters for security, not only for logs.** The app decides who is "at
home" from it: choosing the first password, home-network-only API tokens and the service tokens
all depend on it, as do the sign-in throttle and the internet sign-in pause. So:

- Your proxy must send `X-Forwarded-For` (both shipped files do). A proxy that does not would
  make every visitor look like the proxy itself, a private address.
- The app trusts `X-Forwarded-For` only from `FORWARDED_ALLOW_IPS` (default `172.16.0.0/12`,
  Docker's usual bridge range). Set it in `.env` to exactly the address your nginx connects
  from (the nginx container's IP, or the bridge subnet: `docker network inspect <network>`).
  On a Docker network shared with other stacks, use the nginx container's IP so no other
  container can claim to be someone else.

### HTTPS

Reuse whatever your GrowWise server blocks already do (wildcard certificate, certbot, acme.sh,
or a local CA): copy their `listen 443 ssl`, `ssl_certificate*` and include lines into the
Climate AI server block. Then set `CLIMATE_COOKIE_SECURE=true` and `CLIMATE_PUBLIC_URL` in
`.env`, `docker compose up -d`, and make the name resolve on your LAN (router DNS, Pi-hole, or
Tailscale MagicDNS). HTTPS also matters for the phone: iOS installs the PWA ("Add to Home
Screen") and keeps sign-ins most reliably over HTTPS.

## 7. Phones on plain http at home (optional)

Without nginx, `APP_BIND=0.0.0.0` (or `scripts/bootstrap.sh --lan`) opens the app to your home
network at `http://<server address>:8470`. Keep `CLIMATE_COOKIE_SECURE=false` then. Your router
must not forward the port.

## 8. Public access through the GrowWise AppRelay gateway

A public name such as `https://climate.apprelay.net` goes through the same gateway as GrowWise:
Cloudflare Tunnel to the gateway's nginx, which reaches the app over the shared Docker network.
No router port is opened. The gateway side (the `apps.conf` line, the server block, rate
limits) and the exact steps are in **[PUBLIC_ACCESS.md](PUBLIC_ACCESS.md)**. Climate's side, in
`.env`:

```
COMPOSE_FILE=docker-compose.yml:docker-compose.nginx.yml
NGINX_NETWORK=apprelay_gateway
CLIMATE_PUBLIC_URL=https://climate.apprelay.net
CLIMATE_COOKIE_SECURE=true
FORWARDED_ALLOW_IPS=<the apprelay_gateway subnet: docker network inspect apprelay_gateway -f '{{(index .IPAM.Config 0).Subnet}}'>
```

then `docker compose up -d`.

**What makes public access safe, and must hold before the name goes live:**

1. **The owner password is already chosen, from home** (section 4), and it is long and unique.
   Choosing it from the internet is refused anyway (`CLIMATE_ALLOW_REMOTE_SETUP` stays off).
2. **HTTPS only.** TLS ends at Cloudflare; plain http must be redirected to https (Cloudflare's
   "Always Use HTTPS" or the gateway) and HSTS sent. `CLIMATE_COOKIE_SECURE=true`, so the refresh cookie (HttpOnly, `SameSite=Lax`, path
   `/api/auth` only) never travels over plain http. `make doctor` checks this.
3. **Real tokens, short-lived and revocable.** The web app holds a 15-minute access token in
   memory only; the refresh cookie rotates on every use, and replaying an old one signs that
   device out. Devices expire after 30 days unused (90 at most) and every sign-in, device and
   token is visible and revocable under More > Security. Signing a device out takes effect on
   its next request.
4. **Guessing the password does not work, and cannot lock you out.** Argon2id on every attempt,
   10 failures per address per 5 minutes, and failures from internet addresses are counted
   together: past 20 in an hour, sign-in from the internet pauses for 15 minutes (with an alert
   and an audit entry), while sign-in from home and Tailscale always works and signed-in
   devices are unaffected. The gateway adds its own per-address rate limit in front.
5. **The proxy headers are right** (section 6): the gateway appends the real client address
   (taken from Cloudflare's `CF-Connecting-IP`) to `X-Forwarded-For` and sets `X-Forwarded-Host`
   / `X-Forwarded-Proto`; `FORWARDED_ALLOW_IPS` is the `apprelay_gateway` subnet (not the nginx
   container's address, which changes when nginx is recreated and would then fail open). As a
   second line, any request carrying `CF-Connecting-IP` is always treated as coming from the
   internet, so a proxy mistake can never make the internet look like home. Verify from
   cellular: More > Security should list that device with a public address.
6. **Tokens stay home unless you say otherwise.** API tokens are home-network-only by default;
   the agent and MCP service tokens are accepted from private addresses only. Give each service
   its own token with the smallest role.
7. **Only the app is published.** The database, MCP and ntfy are not on the gateway network and
   their ports stay on `127.0.0.1`. [PUBLIC_ACCESS.md](PUBLIC_ACCESS.md) lists what the gateway
   blocks or limits in front of the app.
8. **Thermostat limits hold whoever is signed in.** Every write still goes through the hard
   limits in code (setpoint range, rate of change, timed holds only), and if the server dies
   the holds expire and each ecobee runs its own schedule.
9. `make doctor` passes, and the iPhone app's server address is the https name.

Keep an eye on More > Security > Recent activity for failed sign-ins and pauses; with
notifications on (section 13) a pause also sends an alert.

## 9. Away from home without a public name: Tailscale

Install Tailscale on the server and on your phones, and use the server's Tailscale name or IP.
Uncomment the `allow 100.64.0.0/10;` block in the nginx file to restrict the app to LAN +
tailnet. Optionally `tailscale serve` can put the app on your tailnet name with a valid HTTPS
certificate. Tailscale addresses count as home: first-run setup and home-network-only tokens
work over it.

## 10. Connect the real house

### ecobee (cloud)

In the app: **Setup → Data source → ecobee**, sign in with your ecobee account. ecobee's MFA
must be an **authenticator app (TOTP) or SMS**; push and email codes are not supported. Then
map each thermostat to its unit (Main floor = Hallway, Upstairs = Toy Room Essential,
Bed / Office = Bedroom) and each sensor to its room. The worker backfills history and polls
ecobee no faster than every 3 minutes. Only the newest refresh token is stored, encrypted; your
password is never stored.

### HomeKit (local, recommended)

Linux with Docker Engine:

```bash
scripts/bootstrap.sh --homekit       # adds homekit to COMPOSE_PROFILES in .env and starts it
```

(or `docker compose --profile homekit up -d`). On a Mac: `make homekit-native`
([LOCAL_DEVELOPMENT.md](LOCAL_DEVELOPMENT.md), section 5). Then **Setup → HomeKit**: enable it
and pair each thermostat with the code shown on its screen. Read **[HOMEKIT.md](HOMEKIT.md)**
first: pairing takes the thermostats out of Apple Home, and it lists what is verified and what
is not.

## 11. Claude (on your subscription)

Claude runs in the `agent` container through the Claude Agent SDK, signed in with **your Claude
plan**. There is no API key and no per-token bill. It is never in the control path: if it is
not signed in, reports and sign-offs wait and the house keeps running.

1. On any computer with a browser, install Claude Code (`curl -fsSL https://claude.ai/install.sh | bash`
   or `npm install -g @anthropic-ai/claude-code`) and run:
   ```bash
   claude setup-token
   ```
   Approve in the browser. It prints a token valid for one year.
2. In `.env` on the server:
   ```
   CLAUDE_CODE_OAUTH_TOKEN=<the token>
   CLAUDE_TOKEN_CREATED=2026-10-04        # today's date; the app warns 30 days before expiry
   ```
3. `docker compose up -d agent` (recreates it with the new environment).
4. In the app, **Ask Claude** shows the sign-in status and the token's expiry.

> **Never set `ANTHROPIC_API_KEY`** (nor `ANTHROPIC_AUTH_TOKEN` or a Bedrock/Vertex/Foundry
> switch) on this server, in `.env` or in your shell. It takes priority over the subscription
> and bills the API. Compose never passes it to the agent, the agent refuses to start if it
> sees one, and the app logs a warning if it finds one in its own environment. Never run
> Claude Code in `--bare` mode for this either.

When the year is up, repeat steps 1-3.

## 12. MCP: the same tools in Claude Code or Claude Desktop

```bash
docker compose --profile mcp up -d mcp
docker compose logs mcp     # "serving MCP over streamable HTTP at http://0.0.0.0:8471/mcp"
```

The endpoint is `http://<host>:8471/mcp` and every request needs
`Authorization: Bearer <CLIMATE_MCP_TOKEN>`. It is bound to `127.0.0.1` by default, so either
use it on the server itself, tunnel it (`ssh -N -L 8471:127.0.0.1:8471 you@server`), or set
`MCP_BIND` to the server's Tailscale IP in `.env` and `docker compose up -d mcp`. Never put it
on the gateway.

Claude Code:

```bash
claude mcp add --transport http climate http://127.0.0.1:8471/mcp \
  --header "Authorization: Bearer <CLIMATE_MCP_TOKEN>"
```

Claude Desktop reaches remote "custom connectors" from Anthropic's cloud, which cannot see your
LAN. Add it as a local server through a stdio bridge instead, for example in
`claude_desktop_config.json` (not tested here):

```json
{ "mcpServers": { "climate": { "command": "npx", "args": [
  "-y", "mcp-remote", "http://127.0.0.1:8471/mcp",
  "--header", "Authorization: Bearer <CLIMATE_MCP_TOKEN>" ] } } }
```

The MCP tools are the agent's tools: read, compute and gated proposals. None can reach a
thermostat.

## 13. Notifications (optional)

Set `CLIMATE_NTFY_URL` to enable push for warnings and errors (including "sign-in from the
internet paused").

- **Easiest:** the public server. `CLIMATE_NTFY_URL=https://ntfy.sh` and a long random
  `CLIMATE_NTFY_TOPIC`; subscribe to that topic in the ntfy app.
- **Self-hosted:** `docker compose --profile ntfy up -d`, `CLIMATE_NTFY_URL=http://ntfy`. Your
  phone must reach it too: give it its own nginx server block (proxy to `127.0.0.1:8472`, same
  websocket headers) or use Tailscale, and set `NTFY_BASE_URL` to that address. For instant
  delivery on iPhone, also set `NTFY_UPSTREAM_BASE_URL=https://ntfy.sh` (Apple push is relayed
  through ntfy.sh).

## 14. Backups

```bash
make backup                                    # = deploy/backup.sh: dump to ~/climate-ai-backups, keep 14
crontab -e
17 3 * * * /path/to/climate-ai/deploy/backup.sh >> "$HOME/climate-ai-backup.log" 2>&1
```

`BACKUP_DIR` and `KEEP` override the defaults. Copy the dumps off the machine (NAS, cloud
drive). The dumps hold the ecobee token and HomeKit keys **encrypted with
`CLIMATE_SECRET_KEY`**, and the password hash and token hashes; store `.env` (or at least
`CLIMATE_SECRET_KEY` and `CLIMATE_TOKEN_PEPPER`) separately, not next to them.

Restore (TimescaleDB needs its pre/post-restore calls; same TimescaleDB version as the dump):

```bash
docker compose stop app worker homekit agent mcp
docker compose exec -T db sh -c 'dropdb -U "$POSTGRES_USER" --if-exists "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" "$POSTGRES_DB"'
docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "CREATE EXTENSION IF NOT EXISTS timescaledb; SELECT timescaledb_pre_restore();"'
docker compose exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner' < ~/climate-ai-backups/climate-YYYYmmdd-HHMMSS.dump
docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT timescaledb_post_restore();"'
docker compose up -d
```

## 15. Updating

```bash
make update                           # = git pull --ff-only && scripts/bootstrap.sh
docker image prune -f                 # drop the old image layers
```

Bootstrap adds any new required secret to `.env`, rebuilds the app and agent images, restarts
what changed (the app migrates on start) and waits until it is healthy. A plain
`git pull && docker compose up -d --build` also works; compose then names any secret `.env` is
missing.

TimescaleDB minor upgrades: change the `db` image tag in `docker-compose.yml`, `docker compose
up -d db`, then run, as the first command of a fresh session:

```bash
docker compose exec db sh -c 'psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "ALTER EXTENSION timescaledb UPDATE;"'
```

Do not change the PostgreSQL major version (`-pg16`) without a dump and restore.

## 16. Troubleshooting

Start here; it checks the secrets, the cookie setting, the database, the owner password, the
worker / HomeKit / agent heartbeats and the data source, and exits non-zero on any problem:

```bash
make doctor                       # = docker compose exec app python -m climate.cli doctor
docker compose ps
docker compose logs -f app worker
```

| Symptom | Fix |
|---|---|
| `required variable ... is missing a value` (`POSTGRES_PASSWORD`, `CLIMATE_JWT_SECRET`, `CLIMATE_TOKEN_PEPPER`) | `make bootstrap` fills in what is missing (section 3). |
| "Sign-in is not configured" (503 `AUTH_NOT_CONFIGURED`) | `CLIMATE_JWT_SECRET` / `CLIMATE_TOKEN_PEPPER` empty or shorter than 32 characters: `make bootstrap`, `docker compose up -d`. |
| First screen says to choose the password from your home network | You are coming from an internet address, or your proxy hides the client address (section 6). Choose it over an SSH tunnel, or `make password`. |
| "Sign-in from the internet is paused" (429 `LOGIN_PAUSED`) | Many failed sign-ins from the internet. It lifts by itself after 15 minutes; from home or Tailscale you can sign in now. Check Recent activity. |
| Sign-in does not stick / login loops | Plain http with `CLIMATE_COOKIE_SECURE=true`: use HTTPS, or set it to `false` and `docker compose up -d`. |
| Every device has to sign in again | After the upgrade to bearer tokens (once), after `CLIMATE_TOKEN_PEPPER` changed, after a password change, or after "sign out everywhere". |
| An API token stopped working | Revoked, expired, home-network-only and used from outside, or `CLIMATE_TOKEN_PEPPER` changed. Create a new one. |
| nginx 502 | `docker compose ps app` (healthy?), the upstream port matches `APP_PORT`; in Docker, the app joined `NGINX_NETWORK`. |
| Live page never updates | The websocket location (`/api/ws`) is missing its upgrade headers in your nginx. |
| `port is already allocated` on 5433/8470 | `make bootstrap` picks free ports; or change `DB_PORT` / `APP_PORT` in `.env` (and the nginx upstream). |
| "secrets cannot be decrypted" | `CLIMATE_SECRET_KEY` changed. Restore the old key; otherwise sign in to ecobee again and re-pair HomeKit. |
| Agent "refusing to start" | Read the reason: a billing variable is set (remove it), or the token is missing/expired (section 11). |
| Agent cannot reach `http://app:8000` | An HTTP proxy configured for Docker: the internal names are exempt already; add more with `EXTRA_NO_PROXY` in `.env`. |
| Agent fails with read-only filesystem errors | Its root filesystem is read-only on purpose; report it. As a stopgap, comment out `read_only: true` for `agent` in `docker-compose.yml`. |
| HomeKit finds nothing | [HOMEKIT.md](HOMEKIT.md), Troubleshooting; on a Mac, [LOCAL_DEVELOPMENT.md](LOCAL_DEVELOPMENT.md), section 8. |
| App logs warn about `ANTHROPIC_API_KEY` | Remove it from `.env` and from the shell that runs `docker compose`. |

Logs are rotated by Docker (5 × 10 MB per container).
