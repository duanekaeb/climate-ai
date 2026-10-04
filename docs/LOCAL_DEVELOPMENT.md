# Running Climate AI locally (Mac and Linux)

From a fresh clone to a running app is one command, `make bootstrap`, on a Mac with Docker
Desktop or a Linux machine with Docker Engine. It runs the whole stack in the simulator (no
ecobee, HomeKit or Claude credentials needed). For the always-on home server (HTTPS, ecobee,
Claude, backups) continue with [DEPLOY.md](DEPLOY.md) afterwards; it starts the same way.

## 1. What you need

| | Mac (Apple Silicon or Intel) | Linux (x86-64 or 64-bit ARM) |
|---|---|---|
| Docker | [Docker Desktop](https://docs.docker.com/desktop/setup/install/mac-install/), running | Docker Engine with the Compose v2 plugin (`docker compose version`); your user in the `docker` group |
| Tools | `git` and `make`: `xcode-select --install` | `git`, `make`, `curl` |
| Disk / memory | 10 GB free for images and build cache; Docker Desktop's default memory is enough | the same; 2 GB of free RAM |
| HomeKit (optional) | Python 3.12: `brew install python@3.12` (section 5) | nothing extra (the `homekit` container) |
| Live-reload web development (optional) | Node 22.12+: `brew install node@22` | Node 22.12+ |
| Tests (optional) | Python 3.12 | Python 3.12 with `venv` (`apt install python3.12-venv`) |

Every image and every Python dependency exists for both amd64 and arm64, so an Apple Silicon Mac
or a Raspberry Pi 4/5 (64-bit OS) builds natively; nothing is emulated and nothing needs a
compiler. The image builds do not use `apt-get` at all.

## 2. Start it

```bash
git clone <your repo URL> climate-ai && cd climate-ai
make bootstrap
```

`make bootstrap` runs [`scripts/bootstrap.sh`](../scripts/bootstrap.sh). It is safe to run again
at any time (also after `git pull`); it never replaces a value you set:

1. Checks that Docker is installed and running and that Compose v2 is there.
2. Creates `.env` from `.env.example` if it is missing, and keeps it at mode 600.
3. Generates every empty secret: `POSTGRES_PASSWORD`, `CLIMATE_SECRET_KEY` (a Fernet key),
   `CLIMATE_JWT_SECRET`, `CLIMATE_TOKEN_PEPPER`, `CLIMATE_AGENT_TOKEN`, `CLIMATE_MCP_TOKEN`.
   Secrets are written to `.env` only and never printed. An `.env` from an older version gets
   the ones it lacks.
4. For a new `.env`: `CLIMATE_COOKIE_SECURE=false` (you browse over plain http; a Secure cookie
   would be dropped and sign-in would not stick) and your computer's time zone.
5. If another program holds the app port (8470) or the database port (5433; GrowWise's native
   Postgres uses it too), it picks the next free one and saves it in `.env`.
6. `docker compose up -d --build`, then waits until the app reports healthy (the first build
   takes a few minutes; database migrations run on the first start).
7. Prints the address and what the first screen will ask.

Open the address (normally <http://localhost:8470>). **The first screen asks you to choose the
owner password** (at least 10 characters; a passphrase from your password manager is ideal).
Choosing it works only from this computer, your home network and Tailscale; a request that
arrives from the internet is refused. The name in the address bar matters too: open the app by
its IP address, `localhost` or a `.local` name. Over Tailscale, use the IP address (or add the
MagicDNS name to `CLIMATE_SETUP_HOSTS` in `.env`); the same goes for a LAN DNS name. `make
password` on the server always works. There is one login and no user accounts: every phone,
tablet and laptop signs in with the same owner password and shows up as its own signed-in
device under **More > Security**.

The simulated house fills in 60 days of history within a few minutes of the first start.

Options (`scripts/bootstrap.sh --help`, or `make bootstrap ARGS="..."`):

| Option | What it does |
|---|---|
| `--lan` | also open the app to your home network over plain http (`APP_BIND=0.0.0.0`), so phones on your Wi-Fi can use `http://<this computer's address>:8470`. Choose the password right away: until then anyone on your network could. macOS asks once whether Docker may accept incoming connections. |
| `--homekit` | Linux: also run the HomeKit service (compose profile `homekit`). On a Mac use section 5. |
| `--no-start` | only create / complete `.env` |
| `--timeout SEC` | how long to wait for the app (default 600) |

## 3. Everyday commands

`make help` lists them all.

| Command | What it does |
|---|---|
| `make up` / `make down` | build and start / stop (data is kept) |
| `make ps`, `make logs` (`SVC=worker`) | status, follow the logs |
| `make doctor` | checks the secrets, the database, the owner password, the cookie setting, the worker / HomeKit / agent heartbeats and the data source |
| `make password` | set the owner password from this computer (break-glass: signs every device out) |
| `make token NAME=dashboard ROLE=viewer` | an API token for a service (`agent`, `viewer` or `control`; `DAYS=90` to expire, `REMOTE=1` to also accept it from the internet); shown once |
| `make tokens` | list API tokens (never the secrets) |

The agent and MCP containers sign in with `CLIMATE_AGENT_TOKEN` / `CLIMATE_MCP_TOKEN` from `.env`.
To manage them like any other token, create an `agent`-role `cai_...` token in the app (More >
Security > API tokens, or `make token NAME=agent ROLE=agent`), put it in `.env` as
`CLIMATE_AGENT_TOKEN` and run `make up`: it stays listed and revocable in the app.
| `make shell`, `make psql` | a shell in the app container, psql on the database |
| `make reset` | **deletes all data** (history, settings, the owner password, the ecobee sign-in, HomeKit pairings) and starts fresh with the same `.env` |
| `make update` | `git pull --ff-only`, then bootstrap |
| `make test` | API and agent tests (section 6) |

The simulator can also play a utility energy-saving event:
`docker compose exec app python -m climate.cli sim-event --unit up,main --in-min 30 --hours 2`.

## 4. Phones and the iPhone app at home

With `--lan`, phones on the same Wi-Fi open `http://<computer's address>:<APP_PORT>`. The iPhone
wrapper ([ios/README.md](../ios/README.md)) accepts plain `http://` only for local addresses,
which this is. Over plain http, `CLIMATE_COOKIE_SECURE` must stay `false`. For HTTPS on your LAN,
Tailscale, or a public name, see [DEPLOY.md](DEPLOY.md).

## 5. HomeKit on a Mac: `make homekit-native`

HomeKit finds the thermostats with multicast DNS on your LAN. Docker Desktop runs containers in
a VM that cannot see that traffic (its host networking works at the TCP/UDP level only), so the
`homekit` container cannot work on a Mac. Run the HomeKit service natively instead; everything
else stays in Docker:

```bash
brew install python@3.12        # once
make homekit-native             # = scripts/homekit-native.sh, runs in the foreground
```

[`scripts/homekit-native.sh`](../scripts/homekit-native.sh):

- creates `.venv` once with the API package and its HomeKit extra, and refreshes it when
  `api/pyproject.toml` changes;
- refuses to start while the Docker `homekit` container runs (two controllers would poll and
  pair the same thermostats);
- reads the `CLIMATE_*` settings from `.env` (literally, it never sources the file), points the
  database URL at the 127.0.0.1-only port the `db` service publishes (`DB_PORT`), keeps its
  cache in `~/.local/state/climate-ai/homekit` (pairing keys are stored encrypted in the
  database, not there), and keeps your Claude token out of its environment.

Then in the app: **Setup > HomeKit**, enable it and pair each thermostat ([HOMEKIT.md](HOMEKIT.md)).

- **Run it from Terminal.app the first time.** macOS 15 and later ask whether the program may
  find devices on your local network. Allow it, or discovery finds nothing. (Apple's TN3179:
  tools started from Terminal or SSH are allowed; a terminal inside another app, such as an
  editor, asks on that app's behalf.)
- **The thermostats can change addresses.** HomeKit identifies each one by its HomeKit device
  id, not its IP, and follows the address it announces over mDNS, so a DHCP change is picked up
  automatically ([HOMEKIT.md](HOMEKIT.md)).

### Keep HomeKit running on a Mac

A terminal window works while you try it. For an always-on Mac, install a LaunchDaemon (system
daemons need no Local Network prompt per TN3179; not tested on every macOS version). Replace
`YOU` and the path, then save it as `/Library/LaunchDaemons/ai.climate.homekit.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>ai.climate.homekit</string>
  <key>UserName</key><string>YOU</string>
  <key>WorkingDirectory</key><string>/Users/YOU/climate-ai</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>/Users/YOU/climate-ai/scripts/homekit-native.sh</string></array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key><string>/Users/YOU</string>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>/Users/YOU/Library/Logs/climate-ai-homekit.log</string>
  <key>StandardErrorPath</key><string>/Users/YOU/Library/Logs/climate-ai-homekit.log</string>
</dict>
</plist>
```

```bash
scripts/homekit-native.sh --setup-only     # once, from Terminal.app: creates .venv
sudo chown root:wheel /Library/LaunchDaemons/ai.climate.homekit.plist
sudo chmod 644 /Library/LaunchDaemons/ai.climate.homekit.plist
sudo launchctl bootstrap system /Library/LaunchDaemons/ai.climate.homekit.plist
sudo launchctl bootout system/ai.climate.homekit     # to stop it
```

If Docker is not up yet at boot, the script waits two minutes for the database and exits;
launchd starts it again 30 seconds later. Also: Docker Desktop must start at login (Settings >
General), and a Mac that sleeps stops collecting data (holds on the thermostats expire safely;
System Settings > Energy > "Prevent automatic sleeping", or `sudo pmset -c sleep 0`).

## 6. Development

**Live reload.** `make dev` starts the stack with
[`docker-compose.dev.yml`](../docker-compose.dev.yml) (the API imports the Python package from
your checkout and reloads on every change; it is also published on `127.0.0.1:8000`), then the
Vite dev server with hot reload on <http://localhost:5173>, which proxies `/api` and the
websocket to it. Sign-in works the same way as in production: one origin, the refresh cookie on
`/api/auth`. The worker does not reload by itself: `docker compose restart worker`. `make up`
returns to the normal stack. Port 8000 taken? Set `DEV_API_PORT` in `.env`.

**Tests.** `make test` runs the API suite and the agent suite natively in `.venv`:

```bash
make test                                   # both suites
make test ARGS="tests/test_api_auth.py -x"  # pytest arguments for the API suite
scripts/test.sh --agent-only                # no database needed
```

The API suite creates a throwaway database for each run on the compose Postgres (through
`127.0.0.1:DB_PORT`, starting the `db` service if needed) and drops it afterwards. The suites use
their own test secrets; nothing from `.env` except the database login reaches them.

**Web type check.** `make typecheck-web` (`vue-tsc --noEmit`, emits nothing). The image build
runs the same check, so a type error also fails `make up`.

## 7. A Linux home server

The same `make bootstrap`, plus `scripts/bootstrap.sh --homekit` for HomeKit. The HomeKit
container uses host networking for mDNS, which needs **Docker Engine (docker-ce)**: Docker
Desktop for Linux and rootless Docker run containers in a VM or user namespace and cannot see
the LAN's multicast (bootstrap warns). With a firewall: `sudo ufw allow 5353/udp`. Then
[DEPLOY.md](DEPLOY.md) for HTTPS, the real thermostats, Claude and backups.

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| `Docker is not running` / `cannot reach the Docker daemon` | Start Docker Desktop (`open -a Docker`). Linux: `sudo systemctl start docker`, and `sudo usermod -aG docker $USER`, then log out and in. |
| `required variable CLIMATE_JWT_SECRET is missing a value` (or `CLIMATE_TOKEN_PEPPER`, `POSTGRES_PASSWORD`) | `.env` predates a new secret or is incomplete: `make bootstrap` adds it. |
| `there is no .env, but the database volume ... exists` | The data was created with another `.env`. Put that one back (its `POSTGRES_PASSWORD` and `CLIMATE_SECRET_KEY` match the data), or delete the old data with the two commands the message prints. |
| `port is already allocated` | bootstrap moves busy ports; if you started by hand, change `APP_PORT` / `DB_PORT` in `.env`. |
| Sign-in does not stick, or loops back to the sign-in screen | Plain http with `CLIMATE_COOKIE_SECURE=true` (Safari drops Secure cookies even on localhost; every browser does on a LAN address). Set it to `false` and `make up`, or use HTTPS. |
| "Choose the password from your home network" on the first screen | The request looked like it came from the internet. Behind a reverse proxy, the proxy must send `X-Forwarded-For` and `FORWARDED_ALLOW_IPS` must be the proxy network's subnet ([DEPLOY.md](DEPLOY.md)); or set the password with `make password`. |
| "Open the app by its address on your home network" when choosing the password | You opened it by a name setup does not accept (a Tailscale MagicDNS name, a LAN DNS name). Use the IP address, `localhost` or a `.local` name, add the name to `CLIMATE_SETUP_HOSTS` in `.env` and `docker compose up -d`, or run `make password`. |
| "Sign-in is not configured" (503) | `CLIMATE_JWT_SECRET` / `CLIMATE_TOKEN_PEPPER` missing or shorter than 32 characters: `make bootstrap`, then `make up`. |
| Forgot the owner password | `make password` on the machine itself (signs every device out). |
| After updating, every device asks to sign in again | Expected once after the switch to bearer tokens: the old session cookie no longer exists. |
| Build or agent errors mentioning a proxy, or the agent cannot reach `http://app:8000` | Your Docker has an HTTP proxy configured (Docker Desktop > Settings > Resources > Proxies, or `~/.docker/config.json`). Compose already exempts the internal names; add others with `EXTRA_NO_PROXY` in `.env`. |
| The first build is slow or runs out of space | It downloads about 3 GB of images. Docker Desktop > Settings > Resources: give the disk 10 GB or more of headroom; `docker system prune` clears old build cache. |
| HomeKit on the Mac finds nothing | Run `make homekit-native` from Terminal.app and allow "find devices on your local network" (System Settings > Privacy & Security > Local Network); the Mac on the same network (not a guest Wi-Fi or another VLAN) as the thermostats; the macOS firewall not set to "Block all incoming connections". Then [HOMEKIT.md](HOMEKIT.md). |
| `the Docker homekit service is running` | Only one HomeKit controller may run: `docker compose stop homekit` and remove `homekit` from `COMPOSE_PROFILES` in `.env`. |
| Raspberry Pi: the agent crashes at start | Untested: the bundled Claude Code CLI on a 16K-page kernel (Pi 5 default). Use the 4K-page kernel (`kernel=kernel8.img` in `config.txt`) or run the agent elsewhere. |
| Start over completely | `make reset` (deletes all data, keeps `.env`). |
