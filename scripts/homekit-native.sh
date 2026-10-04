#!/usr/bin/env bash
# Run the HomeKit service natively on this computer, against the Docker database.
#
#   make homekit-native             # or: scripts/homekit-native.sh
#
# Why: HomeKit finds the thermostats with multicast DNS on your LAN. Docker Desktop (macOS,
# Windows, Docker Desktop for Linux) runs containers in a VM that cannot see that traffic, so
# the `homekit` compose profile only works with Docker Engine on Linux. On a Mac, run this
# instead: the rest of the stack stays in Docker, and this process reaches Postgres on the
# 127.0.0.1-only port the db service publishes (DB_PORT in .env).
#
# What it does:
# - creates .venv once (Python 3.12 preferred: `brew install python@3.12`) with the api
#   package and its HomeKit extra (aiohomekit), and refreshes it when api/pyproject.toml
#   changes;
# - refuses to run while the Docker homekit container is running (two controllers would poll
#   and pair the same thermostats);
# - waits for the database port, then runs `python -m climate.collector.homekit_service` in
#   the foreground with the CLIMATE_* settings from .env (read, never sourced), the database
#   URL for 127.0.0.1:DB_PORT, and a writable state directory
#   (CLIMATE_HOMEKIT_STATE_DIR, default ~/.local/state/climate-ai/homekit: the charmap cache
#   only; pairing keys are stored encrypted in the database). Ctrl-C stops it.
#
# macOS: run it from Terminal.app the first time. macOS 15+ asks whether the app may find
# devices on your local network; allow it, or discovery finds nothing. To keep it running
# without a terminal, see docs/LOCAL_DEVELOPMENT.md ("Keep HomeKit running on a Mac").
#
# Options:
#   --setup-only   create / update .venv and check the settings, then exit
#   -h, --help     this help

set -euo pipefail
# shellcheck source=scripts/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
cd "$ROOT"

SETUP_ONLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --setup-only) SETUP_ONLY=1 ;;
    -h | --help)
      sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) die "unknown option: $1 (see scripts/homekit-native.sh --help)" ;;
  esac
  shift
done

[ -f "$ENV_FILE" ] || die "no .env yet: run 'make bootstrap' first"
if is_linux; then
  note "On a Linux server with Docker Engine the 'homekit' compose profile is simpler"
  note "(scripts/bootstrap.sh --homekit). Running natively anyway."
fi

# One HomeKit controller only.
if have docker && docker info >/dev/null 2>&1; then
  if [ -n "$(docker compose --profile homekit ps -q --status running homekit 2>/dev/null || true)" ]; then
    die "the Docker homekit service is running; two controllers would fight over the thermostats. Stop it first: docker compose stop homekit (and remove 'homekit' from COMPOSE_PROFILES in .env)"
  fi
else
  warn "cannot reach Docker to check for a homekit container; make sure only one HomeKit service runs"
fi

ensure_venv api

# The settings the compose homekit service gets from .env, minus anything Claude-related.
# Every active CLIMATE_* line is exported literally (no shell expansion).
while IFS= read -r key; do
  case "$key" in
    CLIMATE_DATABASE_URL | CLIMATE_HOMEKIT_STATE_DIR | CLIMATE_MIGRATE_ON_START | CLIMATE_WEB_DIR) continue ;;
  esac
  export "$key=$(env_get "$key")"
done <<EOF
$(grep -E '^CLIMATE_[A-Z0-9_]+=' "$ENV_FILE" | cut -d= -f1 | sort -u)
EOF
tz="$(env_get TZ)"
[ -z "$tz" ] || export TZ="$tz"
CLIMATE_DATABASE_URL="$(database_url_from_env)"
export CLIMATE_DATABASE_URL
export CLIMATE_HOMEKIT_STATE_DIR="${CLIMATE_HOMEKIT_STATE_DIR_NATIVE:-$HOME/.local/state/climate-ai/homekit}"
export CLIMATE_MIGRATE_ON_START=0
unset CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN 2>/dev/null || true
mkdir -p "$CLIMATE_HOMEKIT_STATE_DIR"
chmod 700 "$CLIMATE_HOMEKIT_STATE_DIR"
[ -n "${CLIMATE_SECRET_KEY:-}" ] || die "CLIMATE_SECRET_KEY is empty in .env (pairing keys are stored encrypted with it); run 'make bootstrap'"

db_port="$(env_get DB_PORT)"
db_port="${db_port:-5433}"
step "Waiting for the database on 127.0.0.1:$db_port"
wait_for_tcp 127.0.0.1 "$db_port" 120 \
  || die "nothing listens on 127.0.0.1:$db_port. Start the stack first (make up), and check DB_PORT in .env"
ok "database port is open"

if [ "$SETUP_ONLY" -eq 1 ]; then
  ok ".venv is ready; state directory: $CLIMATE_HOMEKIT_STATE_DIR"
  exit 0
fi

step "Starting the HomeKit service (Ctrl-C stops it). Then: Setup > HomeKit in the app."
exec "$VENV/bin/python" -m climate.collector.homekit_service
