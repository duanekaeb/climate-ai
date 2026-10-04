# shellcheck shell=bash
# Shared helpers for scripts/*.sh. Sourced, never run.
#
# Written for bash 3.2 (the /bin/bash macOS ships) and GNU or BSD userlands: no associative
# arrays, no `sed -i`, no GNU-only flags. `.env` is read and written literally (awk), never
# sourced or eval'd, and nothing here prints a value from it.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/.env"
VENV="$ROOT/.venv"

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'; C_RED=$'\033[31m'; C_GREEN=$'\033[32m'
  C_YELLOW=$'\033[33m'; C_RESET=$'\033[0m'
else
  C_BOLD=""; C_DIM=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_RESET=""
fi

say()  { printf '%s\n' "$*"; }
step() { printf '%s==>%s %s\n' "$C_BOLD" "$C_RESET" "$*"; }
ok()   { printf '%s ok%s  %s\n' "$C_GREEN" "$C_RESET" "$*"; }
note() { printf '%s     %s%s\n' "$C_DIM" "$*" "$C_RESET"; }
warn() { printf '%swarning:%s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
die()  { printf '%serror:%s %s\n' "$C_RED" "$C_RESET" "$*" >&2; exit 1; }

is_mac()   { [ "$(uname -s)" = "Darwin" ]; }
is_linux() { [ "$(uname -s)" = "Linux" ]; }
have()     { command -v "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------------------------------
# .env
# ---------------------------------------------------------------------------------------

# env_get KEY: the value of the last active `KEY=` line (what compose uses), without
# surrounding quotes or a trailing ` # comment`; empty when the key is absent.
env_get() {
  [ -f "$ENV_FILE" ] || return 0
  K="$1" awk -v q="'" '
    BEGIN { k = ENVIRON["K"] }
    { sub(/\r$/, "") }
    index($0, k "=") == 1 { v = substr($0, length(k) + 2); found = 1 }
    END {
      if (!found) exit
      if (v ~ /^".*"$/ || v ~ ("^" q ".*" q "$")) v = substr(v, 2, length(v) - 2)
      else { sub(/[ \t]+#.*$/, "", v); sub(/[ \t]+$/, "", v) }
      printf "%s", v
    }' "$ENV_FILE"
}

# env_has KEY: true when an active (uncommented) `KEY=` line exists.
env_has() { [ -f "$ENV_FILE" ] && grep -q "^$1=" "$ENV_FILE"; }

# env_set KEY VALUE: replace every active `KEY=` line; otherwise turn the first commented
# `# KEY=` example into the setting; otherwise append it. The file keeps its inode and mode
# (600); the scratch copy lives in the system temp dir (never next to .env, where git could
# see it) and is removed on every path.
env_set() {
  local key="$1" value="$2" mode tmp
  case "$value" in
    *'
'* | *\\*) die "refusing to write a multi-line or backslash value for $key" ;;
  esac
  if grep -q "^${key}=" "$ENV_FILE"; then mode=active
  elif grep -Eq "^#[[:space:]]*${key}=" "$ENV_FILE"; then mode=comment
  else mode=append
  fi
  tmp="$(mktemp "${TMPDIR:-/tmp}/climate-env.XXXXXX")" || die "mktemp failed"
  if ! K="$key" V="$value" M="$mode" awk '
      BEGIN { k = ENVIRON["K"]; v = ENVIRON["V"]; m = ENVIRON["M"]; done = 0 }
      m == "active" && index($0, k "=") == 1 { print k "=" v; next }
      m == "comment" && !done && $0 ~ ("^#[ \t]*" k "=") { print k "=" v; done = 1; next }
      { print }
      END { if (m == "append") print k "=" v }
    ' "$ENV_FILE" >"$tmp"; then
    rm -f "$tmp"
    die "could not update $key in .env"
  fi
  cat "$tmp" >"$ENV_FILE"
  rm -f "$tmp"
}

# ---------------------------------------------------------------------------------------
# secrets (printed to a command substitution only, never to the terminal)
# ---------------------------------------------------------------------------------------

rand_hex() { # rand_hex BYTES -> 2*BYTES hex characters
  if have openssl; then
    openssl rand -hex "$1"
  else
    od -An -tx1 -N "$1" /dev/urandom | tr -d ' \n'
    printf '\n'
  fi
}

fernet_key() { # 32 random bytes, URL-safe base64 with padding: a valid Fernet key (44 chars)
  if have openssl; then
    openssl rand -base64 32 | tr '+/' '-_' | tr -d '\n'
  else
    head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '\n'
  fi
  printf '\n'
}

# ---------------------------------------------------------------------------------------
# docker
# ---------------------------------------------------------------------------------------

need_docker() { # need_docker [--daemon]
  have docker || {
    if is_mac; then
      die "Docker is not installed. Install Docker Desktop for Mac (Apple Silicon): https://docs.docker.com/desktop/setup/install/mac-install/"
    fi
    die "Docker is not installed. Install Docker Engine with the Compose plugin: https://docs.docker.com/engine/install/"
  }
  docker compose version >/dev/null 2>&1 \
    || die "the Docker Compose v2 plugin is missing ('docker compose version' fails). Install docker-compose-plugin, or update Docker Desktop."
  if [ "${1:-}" = "--daemon" ] && ! docker info >/dev/null 2>&1; then
    if is_mac; then
      die "Docker is not running. Start Docker Desktop (open -a Docker), wait for it to say it is running, then try again."
    fi
    die "cannot reach the Docker daemon. Start it (sudo systemctl start docker), and make sure your user may use it (sudo usermod -aG docker \$USER, then log out and in)."
  fi
}

compose_project() { # the compose project name: COMPOSE_PROJECT_NAME (shell, then .env), else climate-ai
  local p="${COMPOSE_PROJECT_NAME:-}"
  [ -n "$p" ] || p="$(env_get COMPOSE_PROJECT_NAME)"
  printf '%s\n' "${p:-climate-ai}"
}

# ---------------------------------------------------------------------------------------
# native Python (HomeKit on a Mac, tests)
# ---------------------------------------------------------------------------------------

find_python() { # a Python >= 3.11 (3.12 preferred, as in the images); prints its path
  local c
  for c in "${CLIMATE_PYTHON:-}" python3.12 python3.13 python3.11 python3; do
    [ -n "$c" ] || continue
    have "$c" || continue
    if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
      command -v "$c"
      return 0
    fi
  done
  return 1
}

# ensure_venv api [agent]: the repo's .venv with the api package (editable, HomeKit and dev
# extras) and optionally the agent package. Reinstalls a part only when its pyproject.toml is
# newer than the last install.
ensure_venv() {
  local py part stamp
  if [ ! -x "$VENV/bin/python" ]; then
    py="$(find_python)" || {
      if is_mac; then
        die "Python 3.11+ not found. Install it with Homebrew: brew install python@3.12"
      fi
      die "Python 3.11+ not found. Install python3.12 and python3.12-venv (or set CLIMATE_PYTHON)."
    }
    step "Creating .venv with $("$py" --version 2>&1)"
    "$py" -m venv "$VENV" || die "could not create $VENV (Debian/Ubuntu: apt install python3-venv)"
    "$VENV/bin/python" -m pip install --quiet --upgrade pip
  fi
  for part in "$@"; do
    stamp="$VENV/.installed-$part"
    if [ -f "$stamp" ] && [ ! "$ROOT/$part/pyproject.toml" -nt "$stamp" ]; then
      continue
    fi
    step "Installing $part into .venv (first time takes a minute)"
    case "$part" in
      api) "$VENV/bin/python" -m pip install --quiet --prefer-binary -e "$ROOT/api[homekit,dev]" ;;
      agent) "$VENV/bin/python" -m pip install --quiet --prefer-binary -e "$ROOT/agent[test]" ;;
      *) die "ensure_venv: unknown part $part" ;;
    esac || die "pip install of $part failed (see above)"
    touch "$stamp"
  done
}

# database_url_from_env [DBNAME]: the URL for a process on THIS machine reaching the compose
# database through its 127.0.0.1-only port (password URL-encoded); DBNAME defaults to
# POSTGRES_DB. Needs ensure_venv first.
database_url_from_env() {
  local user pass db port
  user="$(env_get POSTGRES_USER)"; user="${user:-climate}"
  pass="$(env_get POSTGRES_PASSWORD)"
  db="${1:-$(env_get POSTGRES_DB)}"; db="${db:-climate}"
  port="$(env_get DB_PORT)"; port="${port:-5433}"
  [ -n "$pass" ] || die "POSTGRES_PASSWORD is empty in .env; run scripts/bootstrap.sh first"
  U="$user" P="$pass" D="$db" PORT="$port" "$VENV/bin/python" -c '
import os
from urllib.parse import quote
e = os.environ
print("postgresql+psycopg://%s:%s@127.0.0.1:%s/%s" % (quote(e["U"], safe=""), quote(e["P"], safe=""), e["PORT"], quote(e["D"], safe="")))'
}

wait_for_tcp() { # wait_for_tcp HOST PORT SECONDS
  local i=0
  while [ "$i" -lt "$3" ]; do
    if (exec 3<>"/dev/tcp/$1/$2") 2>/dev/null; then return 0; fi
    sleep 1
    i=$((i + 1))
  done
  return 1
}
