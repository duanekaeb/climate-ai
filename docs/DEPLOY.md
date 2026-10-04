# Deploying Climate AI on a home server

Climate AI runs as a small Docker Compose stack on one always-on machine at home, behind the
nginx you already run for your GrowWise services. It works with **no credentials at all**
(simulator mode) so you can try everything before connecting the real thermostats.

| Service | What it is | Port (host side) | Default |
|---|---|---|---|
| `db` | PostgreSQL 16 + TimescaleDB 2.30.2 | `127.0.0.1:5433` (`DB_PORT`) | on |
| `app` | API + websocket + the web app | `127.0.0.1:8470` (`APP_PORT`) | on |
| `worker` | data source, weather, controller, nightly jobs | none | on |
| `agent` | Claude Agent SDK on your Claude subscription | none | on (idle without a token) |
| `homekit` | local HomeKit controller (host network, Linux only) | host network | profile `homekit` |
| `mcp` | MCP server for Claude Code / Desktop | `127.0.0.1:8471` (`MCP_BIND`, `MCP_PORT`) | profile `mcp` |
| `ntfy` | self-hosted push notifications | `127.0.0.1:8472` (`NTFY_PORT`) | profile `ntfy` |

Everything binds to `127.0.0.1`. Your nginx (or Tailscale) is the only way in. **Never
port-forward this to the internet**: it can change your thermostats.

---

## 1. Prerequisites

- **A Linux host is strongly recommended** (any x86-64 or 64-bit ARM box: mini PC, NUC, NAS
  with Docker, Raspberry Pi 4/5 with a 64-bit OS). The HomeKit service needs host networking
  and multicast DNS, which only Linux provides; Docker Desktop on macOS/Windows runs everything
  else but not HomeKit.
- Docker Engine 24+ with the Compose v2 plugin (`docker compose version`).
- About 2 GB of free RAM and 5 GB of disk (the agent image alone is ~400 MB because it bundles
  the Claude Code CLI).
- For HomeKit: the server on the same LAN/VLAN as the thermostats (docs/HOMEKIT.md).
- `openssl` and `git` on the host.

## 2. Get the code

```bash
git clone <your repo URL> climate-ai
cd climate-ai
```

## 3. Configure

```bash
cp .env.example .env
chmod 600 .env
# generate every secret in one go (Linux sed; on macOS use `sed -i ''`)
sed -i \
  -e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(openssl rand -hex 24)|" \
  -e "s|^CLIMATE_SECRET_KEY=.*|CLIMATE_SECRET_KEY=$(openssl rand -base64 32 | tr '+/' '-_')|" \
  -e "s|^CLIMATE_SESSION_SECRET=.*|CLIMATE_SESSION_SECRET=$(openssl rand -hex 32)|" \
  -e "s|^CLIMATE_AGENT_TOKEN=.*|CLIMATE_AGENT_TOKEN=$(openssl rand -hex 32)|" \
  -e "s|^CLIMATE_MCP_TOKEN=.*|CLIMATE_MCP_TOKEN=$(openssl rand -hex 32)|" \
  .env
```

`CLIMATE_SECRET_KEY` is a Fernet key; `openssl rand -base64 32 | tr '+/' '-_'` produces a valid
one. Once the image is built you can also use the app's own generator, which prints a secret
key and a session secret:

```bash
docker compose run --rm --no-deps app python -m climate.cli gen-key
```

Then open `.env` and review the rest. Every variable is commented. The important ones:

- `TZ`: your time zone, for log timestamps (the house time zone is set in the app's Setup).
- `CLIMATE_COOKIE_SECURE=true` (default) once you browse through HTTPS. Over plain http the
  browser drops the session cookie and sign-in will not stick: set `false` in that case.
- `CLIMATE_PUBLIC_URL`: the address you will open the app at, e.g. `https://climate.example.home`.
- **Never set `ANTHROPIC_API_KEY`** here or anywhere on the server (see section 9).

**Back up `CLIMATE_SECRET_KEY` now** (password manager). It encrypts the ecobee sign-in and the
HomeKit pairing keys stored in the database; without it they are unrecoverable.

## 4. Start (simulator mode)

```bash
docker compose up -d --build
docker compose ps                 # db and app become "healthy"; worker and agent "running"
curl -s http://127.0.0.1:8470/api/health     # {"ok":true,...}
```

The first build takes a few minutes (it builds the web app and installs the Python packages).
On start the app applies database migrations and seeds the house; the worker then generates
synthetic history for the simulator.

Until you do section 9 the agent has no Claude sign-in. It stays up and idle (Claude never
runs) and reports "not signed in" on the Live and Ask Claude pages; nothing else is affected.

## 5. First visit: choose the owner password

The app listens on the server's `127.0.0.1:8470` only. Until nginx is set up, reach it with an
SSH tunnel from your laptop:

```bash
ssh -N -L 8470:127.0.0.1:8470 you@server
# then open http://localhost:8470
```

The first screen asks you to **choose the owner password** (unless you set
`CLIMATE_OWNER_PASSWORD` in `.env`). Forgot it later?
`docker compose exec app python -m climate.cli set-password`.

## 6. Put it behind your nginx

### A. nginx runs on the host

```bash
sudo cp deploy/nginx/climate.conf /etc/nginx/conf.d/climate.conf
sudoedit /etc/nginx/conf.d/climate.conf      # server_name, TLS lines
sudo nginx -t && sudo systemctl reload nginx
```

### B. nginx runs in Docker

```bash
docker network ls                    # find your nginx container's network, e.g. growwise_default
# add to .env:
#   NGINX_NETWORK=growwise_default
#   COMPOSE_FILE=docker-compose.yml:docker-compose.nginx.yml
docker compose up -d                 # attaches app to that network as climate-ai-app
# copy deploy/nginx/climate-docker-network.conf into the directory your nginx container
# includes (usually a bind-mounted conf.d), edit server_name and TLS, then:
docker exec <nginx-container> nginx -t && docker exec <nginx-container> nginx -s reload
```

Both files proxy the websocket at `/api/ws` with the HTTP/1.1 upgrade headers and a long read
timeout, overwrite `X-Forwarded-For` with the real client address (the login throttle counts
attempts per client IP), and contain a commented `allow`/`deny` block for LAN + Tailscale only.

The app trusts `X-Forwarded-For` only from `FORWARDED_ALLOW_IPS` (default `172.16.0.0/12`, Docker's
usual bridge range). If your Docker networks live elsewhere (some hosts with many stacks hand
out `192.168.x.x` bridges), set it in `.env` to the address your nginx connects from, e.g. the
nginx container's IP or the bridge subnet (`docker network inspect <network>`). If it is wrong,
every visitor shares one login-throttle bucket and a single wrong-password streak locks out the
whole house for five minutes.

### HTTPS

Reuse whatever your GrowWise server blocks already do (wildcard certificate, certbot, acme.sh,
or a local CA): copy their `listen 443 ssl`, `ssl_certificate*` and include lines into the
Climate AI server block. Then keep `CLIMATE_COOKIE_SECURE=true`, set `CLIMATE_PUBLIC_URL`, and
make the name resolve on your LAN (router DNS, Pi-hole, or Tailscale MagicDNS).

HTTPS also matters for the phone: iOS installs the PWA ("Add to Home Screen") and keeps
sessions most reliably over HTTPS.

## 7. Away from home: Tailscale

Install Tailscale on the server and on your phones, and use the server's Tailscale name or IP.
Uncomment the `allow 100.64.0.0/10;` block in the nginx file to restrict the app to LAN +
tailnet. Optionally `tailscale serve` can put the app on your tailnet name with a valid HTTPS
certificate. **Do not** expose the app, the MCP port or the database to the internet.

## 8. Connect the real house

### ecobee (cloud)

In the app: **Setup → Data source → ecobee**, sign in with your ecobee account. ecobee's MFA
must be an **authenticator app (TOTP) or SMS**; push and email codes are not supported. Then
map each thermostat to its unit (Main floor = Hallway, Upstairs = Toy Room Essential,
Bed / Office = Bedroom) and each sensor to its room. The worker backfills history and polls
ecobee no faster than every 3 minutes. Only the newest refresh token is stored, encrypted; your
password is never stored.

### HomeKit (local, recommended)

```bash
docker compose --profile homekit up -d
```

(or add `homekit` to `COMPOSE_PROFILES` in `.env` so plain `docker compose up -d` includes it),
then **Setup → HomeKit**: enable it and pair each thermostat with the code shown on its screen.
Read **[docs/HOMEKIT.md](HOMEKIT.md)** first: pairing takes the thermostats out of Apple Home,
and it lists what is verified and what is not.

## 9. Claude (on your subscription)

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

### MCP: the same tools in Claude Code or Claude Desktop

```bash
docker compose --profile mcp up -d mcp
docker compose logs mcp     # "serving MCP over streamable HTTP at http://0.0.0.0:8471/mcp"
```

The endpoint is `http://<host>:8471/mcp` and every request needs
`Authorization: Bearer <CLIMATE_MCP_TOKEN>`. It is bound to `127.0.0.1` by default, so either
use it on the server itself, tunnel it (`ssh -N -L 8471:127.0.0.1:8471 you@server`), or set
`MCP_BIND` to the server's Tailscale IP in `.env` and `docker compose up -d mcp`.

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

## 10. Notifications (optional)

Set `CLIMATE_NTFY_URL` to enable push for warnings and errors.

- **Easiest:** the public server. `CLIMATE_NTFY_URL=https://ntfy.sh` and a long random
  `CLIMATE_NTFY_TOPIC`; subscribe to that topic in the ntfy app.
- **Self-hosted:** `docker compose --profile ntfy up -d`, `CLIMATE_NTFY_URL=http://ntfy`. Your
  phone must reach it too: give it its own nginx server block (proxy to `127.0.0.1:8472`, same
  websocket headers) or use Tailscale, and set `NTFY_BASE_URL` to that address. For instant
  delivery on iPhone, also set `NTFY_UPSTREAM_BASE_URL=https://ntfy.sh` (Apple push is relayed
  through ntfy.sh).

## 11. Backups

```bash
deploy/backup.sh                               # dump to ~/climate-ai-backups, keep 14
crontab -e
17 3 * * * /path/to/climate-ai/deploy/backup.sh >> "$HOME/climate-ai-backup.log" 2>&1
```

`BACKUP_DIR` and `KEEP` override the defaults. Copy the dumps off the machine (NAS, cloud
drive). The dumps hold the ecobee token and HomeKit keys **encrypted with
`CLIMATE_SECRET_KEY`**; store that key (or the whole `.env`) separately, not next to them.

Restore (TimescaleDB needs its pre/post-restore calls; same TimescaleDB version as the dump):

```bash
docker compose stop app worker homekit agent mcp
docker compose exec -T db sh -c 'dropdb -U "$POSTGRES_USER" --if-exists "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" "$POSTGRES_DB"'
docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "CREATE EXTENSION IF NOT EXISTS timescaledb; SELECT timescaledb_pre_restore();"'
docker compose exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner' < ~/climate-ai-backups/climate-YYYYmmdd-HHMMSS.dump
docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT timescaledb_post_restore();"'
docker compose up -d
```

## 12. Updating

```bash
git pull
docker compose up -d --build          # rebuilds app + agent images; the app migrates on start
docker image prune -f                 # drop the old image layers
```

TimescaleDB minor upgrades: change the `db` image tag in `docker-compose.yml`, `docker compose
up -d db`, then run, as the first command of a fresh session:

```bash
docker compose exec db sh -c 'psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "ALTER EXTENSION timescaledb UPDATE;"'
```

Do not change the PostgreSQL major version (`-pg16`) without a dump and restore.

## 13. Troubleshooting

Start here; it checks the database, the secret key, the data source sign-in and the HomeKit and
agent heartbeats, and exits non-zero on any problem:

```bash
docker compose exec app python -m climate.cli doctor
docker compose ps
docker compose logs -f app worker
```

| Symptom | Fix |
|---|---|
| `required variable POSTGRES_PASSWORD is missing` | Section 3: fill in `.env`. |
| Sign-in does not stick / login loops | Browsing over plain http with `CLIMATE_COOKIE_SECURE=true`: use HTTPS, or set it to `false` and `docker compose up -d`. |
| nginx 502 | `docker compose ps app` (healthy?), the upstream port matches `APP_PORT`; in Docker, the app joined `NGINX_NETWORK`. |
| Live page never updates | The websocket location (`/api/ws`) is missing its upgrade headers in your nginx. |
| `port is already allocated` on 5433/8470 | Change `DB_PORT` / `APP_PORT` in `.env` (and the nginx upstream). |
| "secrets cannot be decrypted" | `CLIMATE_SECRET_KEY` changed. Restore the old key; otherwise sign in to ecobee again and re-pair HomeKit. |
| Agent "refusing to start" | Read the reason: a billing variable is set (remove it), or the token is missing/expired (section 9). |
| Agent fails with read-only filesystem errors | Its root filesystem is read-only on purpose; report it. As a stopgap, comment out `read_only: true` for `agent` in `docker-compose.yml`. |
| HomeKit finds nothing | docs/HOMEKIT.md, Troubleshooting. |
| App logs warn about `ANTHROPIC_API_KEY` | Remove it from `.env` and from the shell that runs `docker compose`. |

Logs are rotated by Docker (5 × 10 MB per container).
