#!/usr/bin/env bash
# Climate AI: one command from a fresh clone to a running stack (macOS with Docker Desktop, or
# Linux with Docker Engine). Safe to run again at any time, including after `git pull`.
#
#   scripts/bootstrap.sh            # or: make bootstrap
#
# 1. Checks Docker and Compose v2.
# 2. Creates .env from .env.example if it is missing (mode 600 either way).
# 3. Generates every empty secret, never replacing one that is set, and adds any an older .env
#    lacks: POSTGRES_PASSWORD, CLIMATE_SECRET_KEY (Fernet), CLIMATE_JWT_SECRET,
#    CLIMATE_TOKEN_PEPPER, CLIMATE_AGENT_TOKEN, CLIMATE_MCP_TOKEN. Secrets are never printed.
# 4. New .env only: local defaults (plain-http cookies, this computer's time zone).
# 5. Moves APP_PORT / DB_PORT (and the MCP / ntfy ports when those profiles are on) to a free
#    port when another program holds them.
# 6. docker compose up -d --build, waits until the app is healthy, prints the URL and what the
#    first screen will ask for (choosing the owner password, from this computer or your home
#    network only).
#
# Options:
#   --no-start     only create / complete .env (no build, no start)
#   --lan          also open the app to your home network over plain http (APP_BIND=0.0.0.0);
#                  phones on your Wi-Fi then use http://<this computer's address>:<APP_PORT>
#   --homekit      Linux: also run the HomeKit service (compose profile homekit). On a Mac use
#                  `make homekit-native` instead: Docker Desktop cannot do mDNS on your LAN.
#   --timeout SEC  how long to wait for the app to become healthy (default 600)
#   -h, --help     this help

set -euo pipefail
# shellcheck source=scripts/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
cd "$ROOT"

START=1
LAN=0
HOMEKIT=0
TIMEOUT=600
while [ $# -gt 0 ]; do
  case "$1" in
    --no-start) START=0 ;;
    --lan) LAN=1 ;;
    --homekit) HOMEKIT=1 ;;
    --timeout)
      [ $# -ge 2 ] || die "--timeout needs a number of seconds"
      TIMEOUT="$2"
      shift
      ;;
    -h | --help)
      sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) die "unknown option: $1 (see scripts/bootstrap.sh --help)" ;;
  esac
  shift
done
case "$TIMEOUT" in '' | *[!0-9]*) die "--timeout must be a number of seconds" ;; esac

# ---------------------------------------------------------------------------------------
# 1. prerequisites
# ---------------------------------------------------------------------------------------
step "Checking prerequisites"
if [ "$START" -eq 1 ]; then
  need_docker --daemon
  ok "$(docker compose version 2>/dev/null | head -n 1), engine $(docker version --format '{{.Server.Version}} ({{.Server.Os}}/{{.Server.Arch}})' 2>/dev/null || echo '?')"
else
  need_docker
  ok "Docker CLI and Compose v2 found"
fi
[ -f "$ROOT/.env.example" ] || die ".env.example is missing; run this from a complete checkout"

# ---------------------------------------------------------------------------------------
# 2. .env
# ---------------------------------------------------------------------------------------
CREATED=0
if [ ! -f "$ENV_FILE" ]; then
  # A database volume from an earlier install would keep its old password and its data would
  # be encrypted with the old CLIMATE_SECRET_KEY: new random values would lock it out.
  if [ "$START" -eq 1 ] && docker volume inspect "$(compose_project)_pgdata" >/dev/null 2>&1; then
    project="$(compose_project)"
    die "there is no .env, but the database volume '${project}_pgdata' from an earlier install exists.
       Put the old .env back (its POSTGRES_PASSWORD and CLIMATE_SECRET_KEY match that data), or
       delete the old data first (this cannot be undone):
         docker rm -f \$(docker ps -aq --filter label=com.docker.compose.project=$project)
         docker volume rm ${project}_pgdata"
  fi
  step "Creating .env from .env.example"
  (umask 077 && cp "$ROOT/.env.example" "$ENV_FILE")
  CREATED=1
fi
chmod 600 "$ENV_FILE"

# ---------------------------------------------------------------------------------------
# 3. secrets (fill blanks only; never print them)
# ---------------------------------------------------------------------------------------
step "Checking secrets in .env"
GENERATED=""
fill_secret() { # fill_secret KEY GENERATOR...
  local key="$1" value
  shift
  if [ -n "$(env_get "$key")" ]; then
    return 0
  fi
  value="$("$@")"
  [ -n "$value" ] || die "could not generate $key"
  env_set "$key" "$value"
  GENERATED="$GENERATED $key"
}
fill_secret POSTGRES_PASSWORD rand_hex 24
fill_secret CLIMATE_SECRET_KEY fernet_key
fill_secret CLIMATE_JWT_SECRET rand_hex 32
fill_secret CLIMATE_TOKEN_PEPPER rand_hex 32
fill_secret CLIMATE_AGENT_TOKEN rand_hex 32
fill_secret CLIMATE_MCP_TOKEN rand_hex 32
if [ -n "$GENERATED" ]; then
  ok "generated:$GENERATED (written to .env only)"
else
  ok "all secrets already set"
fi
if [ -n "$(env_get CLIMATE_SESSION_SECRET)" ]; then
  note "CLIMATE_SESSION_SECRET is no longer used (sign-in now uses CLIMATE_JWT_SECRET and"
  note "CLIMATE_TOKEN_PEPPER); you may delete that line from .env."
fi

# ---------------------------------------------------------------------------------------
# 4. local defaults (new .env only) and options
# ---------------------------------------------------------------------------------------
host_tz() {
  local tz=""
  if is_linux && have timedatectl; then
    tz="$(timedatectl show -p Timezone --value 2>/dev/null || true)"
  fi
  if [ -z "$tz" ] && [ -L /etc/localtime ]; then
    tz="$(readlink /etc/localtime | sed -n 's|.*zoneinfo/||p')"
  fi
  if [ -z "$tz" ] && [ -f /etc/timezone ]; then
    tz="$(head -n 1 /etc/timezone)"
  fi
  case "$tz" in '' | *[!A-Za-z0-9_+/-]*) return 1 ;; esac
  printf '%s\n' "$tz"
}

if [ "$CREATED" -eq 1 ]; then
  # Plain http on this computer: a Secure cookie would be dropped (Safari does so even on
  # localhost) and sign-in would not stick. docs/DEPLOY.md turns it back on behind HTTPS.
  env_set CLIMATE_COOKIE_SECURE false
  if tz="$(host_tz)" && [ "$(env_get TZ)" = "UTC" ]; then
    env_set TZ "$tz"
  fi
  ok "local defaults: CLIMATE_COOKIE_SECURE=false (plain http), TZ=$(env_get TZ), simulator data source"
elif [ "$(env_get CLIMATE_COOKIE_SECURE)" = "true" ]; then
  case "$(env_get CLIMATE_PUBLIC_URL)" in
    https://*) ;;
    *)
      note "CLIMATE_COOKIE_SECURE=true: the sign-in cookie is Secure for internet visitors; plain"
      note "http from your home network or Tailscale still signs in."
      ;;
  esac
fi

if [ "$LAN" -eq 1 ]; then
  env_set APP_BIND 0.0.0.0
  case "$(env_get CLIMATE_PUBLIC_URL)" in
    https://*)
      # Served over HTTPS as well: keep the cookie Secure (make doctor insists). Plain http
      # from a home or Tailscale address still gets a cookie the browser keeps.
      if [ "$(env_get CLIMATE_COOKIE_SECURE)" != "false" ]; then
        note "CLIMATE_PUBLIC_URL is https, so CLIMATE_COOKIE_SECURE stays true (plain http at home still signs in)"
      fi
      ;;
    *)
      if [ "$(env_get CLIMATE_COOKIE_SECURE)" != "false" ]; then
        env_set CLIMATE_COOKIE_SECURE false
        note "CLIMATE_COOKIE_SECURE=false: the home-network address is plain http"
      fi
      ;;
  esac
  ok "APP_BIND=0.0.0.0: the app is reachable from your home network (never port-forward it)"
fi

# HomeKit: host networking + mDNS only work with Docker Engine on Linux.
profiles_has() { case ",$(env_get COMPOSE_PROFILES)," in *",$1,"*) return 0 ;; esac; return 1; }
if is_mac; then
  if profiles_has homekit; then
    env_set COMPOSE_PROFILES "$(env_get COMPOSE_PROFILES | tr ',' '\n' | grep -vx homekit | paste -sd, -)"
    warn "removed 'homekit' from COMPOSE_PROFILES: Docker Desktop cannot do mDNS on your LAN. Run 'make homekit-native' instead."
  fi
  [ "$HOMEKIT" -eq 0 ] || warn "--homekit is for Linux; on a Mac run 'make homekit-native' (docs/LOCAL_DEVELOPMENT.md)."
elif [ "$HOMEKIT" -eq 1 ]; then
  if ! profiles_has homekit; then
    cur="$(env_get COMPOSE_PROFILES)"
    env_set COMPOSE_PROFILES "${cur:+$cur,}homekit"
  fi
  ok "COMPOSE_PROFILES includes homekit"
fi
if is_linux && profiles_has homekit && [ "$START" -eq 1 ]; then
  os_name="$(docker info --format '{{.OperatingSystem}}' 2>/dev/null || true)"
  sec_opts="$(docker info --format '{{json .SecurityOptions}}' 2>/dev/null || true)"
  case "$os_name$sec_opts" in
    *"Docker Desktop"* | *rootless*)
      warn "this Docker ($os_name) runs containers in a VM or user namespace, so the HomeKit container cannot see your LAN's mDNS. Use Docker Engine (docker-ce), or run 'make homekit-native'."
      ;;
  esac
fi
if [ -n "${COMPOSE_PROFILES:-}" ] && [ "${COMPOSE_PROFILES:-}" != "$(env_get COMPOSE_PROFILES)" ]; then
  note "COMPOSE_PROFILES is set in your shell ($COMPOSE_PROFILES); it overrides the one in .env."
fi

if [ "$START" -eq 0 ]; then
  say ""
  say ".env is ready (mode 600). Start with: docker compose up -d --build   (or: make up)"
  exit 0
fi

# ---------------------------------------------------------------------------------------
# 5. ports
# ---------------------------------------------------------------------------------------
port_busy() { # something on this machine listens on (or forwards) TCP $1
  if is_linux && have ss; then
    [ -n "$(ss -ltn "sport = :$1" 2>/dev/null | tail -n +2)" ] && return 0
  elif have lsof; then
    lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1 && return 0
  fi
  (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && return 0
  return 1
}
free_port_after() { # free_port_after START EXCLUDED...: first free port above START
  local p=$(($1 + 1)) end=$(($1 + 200)) x skip
  shift
  while [ "$p" -le "$end" ]; do
    skip=0
    for x in "$@"; do [ "$x" = "$p" ] && skip=1; done
    if [ "$skip" -eq 0 ] && ! port_busy "$p"; then
      printf '%s\n' "$p"
      return 0
    fi
    p=$((p + 1))
  done
  return 1
}
ensure_port() { # ensure_port VAR DEFAULT SERVICE CONTAINER_PORT EXCLUDED...
  local var="$1" def="$2" svc="$3" cport="$4" cur mapped new
  shift 4
  cur="$(env_get "$var")"
  cur="${cur:-$def}"
  case "$cur" in '' | *[!0-9]*) return 0 ;; esac
  port_busy "$cur" || return 0
  mapped="$(docker compose port "$svc" "$cport" 2>/dev/null || true)"
  case "$mapped" in *":$cur") return 0 ;; esac # held by this stack's own container
  new="$(free_port_after "$cur" "$@")" || die "port $cur ($var) is in use and no free port was found above it; set $var in .env"
  env_set "$var" "$new"
  warn "port $cur is used by another program; $var=$new saved in .env"
}
port_of() { local v; v="$(env_get "$1")"; printf '%s\n' "${v:-$2}"; }
step "Checking ports"
ensure_port APP_PORT 8470 app 8000 "$(port_of DB_PORT 5433)" "$(port_of MCP_PORT 8471)" "$(port_of NTFY_PORT 8472)"
ensure_port DB_PORT 5433 db 5432 "$(port_of APP_PORT 8470)" "$(port_of MCP_PORT 8471)" "$(port_of NTFY_PORT 8472)"
if profiles_has mcp; then
  ensure_port MCP_PORT 8471 mcp 8471 "$(port_of APP_PORT 8470)" "$(port_of DB_PORT 5433)" "$(port_of NTFY_PORT 8472)"
fi
if profiles_has ntfy; then
  ensure_port NTFY_PORT 8472 ntfy 80 "$(port_of APP_PORT 8470)" "$(port_of DB_PORT 5433)" "$(port_of MCP_PORT 8471)"
fi
APP_PORT_V="$(port_of APP_PORT 8470)"
ok "app on port $APP_PORT_V, database on 127.0.0.1:$(port_of DB_PORT 5433)"

# ---------------------------------------------------------------------------------------
# 6. build, start, wait
# ---------------------------------------------------------------------------------------
step "Building and starting (the first build takes a few minutes)"
docker compose up -d --build

step "Waiting for the app to become healthy (migrations run on the first start)"
app_id="$(docker compose ps -q app 2>/dev/null || true)"
[ -n "$app_id" ] || die "the app container did not start; see: docker compose ps -a && docker compose logs app"
waited=0
while :; do
  state="$(docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' "$app_id" 2>/dev/null || echo 'gone')"
  case "$state" in
    "running healthy") break ;;
    exited* | dead* | gone)
      docker compose logs --tail=60 app >&2 || true
      die "the app container stopped ($state); its last log lines are above"
      ;;
  esac
  if [ "$waited" -ge "$TIMEOUT" ]; then
    docker compose ps >&2 || true
    docker compose logs --tail=60 app >&2 || true
    die "the app is not healthy after ${TIMEOUT}s (state: $state); its last log lines are above"
  fi
  sleep 3
  waited=$((waited + 3))
  [ $((waited % 30)) -ne 0 ] || note "still starting (${waited}s, state: $state)"
done
ok "app is healthy"

# ---------------------------------------------------------------------------------------
# 7. what next
# ---------------------------------------------------------------------------------------
bind="$(env_get APP_BIND)"
case "$bind" in '' | 0.0.0.0 | 127.0.0.1 | '::' | '[::]') probe_host=127.0.0.1 ;; *) probe_host="$bind" ;; esac
http_get() {
  if have curl; then
    curl -fsS --noproxy '*' --max-time 5 "$1" 2>/dev/null
  else
    return 1
  fi
}
auth_state="$(http_get "http://$probe_host:$APP_PORT_V/api/auth/state" || true)"
compact_state="$(printf '%s' "$auth_state" | tr -d ' \n')"

lan_ip() {
  local ip=""
  if is_mac; then
    ip="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
  else
    ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
  fi
  printf '%s\n' "$ip"
}

say ""
say "${C_BOLD}Climate AI is running.${C_RESET}"
case "$bind" in
  '' | 0.0.0.0 | 127.0.0.1 | '::' | '[::]') say "  Open:        http://localhost:$APP_PORT_V" ;;
  *) say "  Open:        http://$bind:$APP_PORT_V" ;;
esac
if [ "$bind" = "0.0.0.0" ]; then
  ip="$(lan_ip)"
  [ -z "$ip" ] || say "  On Wi-Fi:    http://$ip:$APP_PORT_V   (phones and tablets at home)"
fi
case "$compact_state" in
  *'"password_set":false'*)
    say "  First run:   the first screen asks you to choose the owner password (at least 10"
    say "               characters). That works from this computer and your home network only;"
    say "               nobody on the internet can choose it. Do it now. (Or: make password)"
    ;;
  *'"password_set":true'*)
    say "  Sign in:     with your owner password. Forgot it? make password"
    ;;
  *)
    say "  First visit: choose the owner password on the first screen (or: make password)."
    ;;
esac
if [ "$CREATED" -eq 1 ]; then
  say "  Simulator:   a simulated house with 60 days of history appears within a few minutes;"
  say "               connect the real ecobees later in Setup (docs/DEPLOY.md)."
fi
say ""
say "  make logs | make ps | make doctor | make password | make down | make help"
say "  Secrets live in .env (mode 600). Back up CLIMATE_SECRET_KEY separately from database backups."
if is_mac; then
  say "  HomeKit on this Mac: make homekit-native (docs/LOCAL_DEVELOPMENT.md)."
elif ! profiles_has homekit; then
  say "  HomeKit: scripts/bootstrap.sh --homekit (Docker Engine on Linux; docs/HOMEKIT.md)."
fi
if [ -z "$(env_get CLAUDE_CODE_OAUTH_TOKEN)" ]; then
  say "  Claude:      idle until CLAUDE_CODE_OAUTH_TOKEN is set (docs/DEPLOY.md, section \"Claude\")."
fi
