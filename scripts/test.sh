#!/usr/bin/env bash
# Run the API and agent test suites natively, against the Docker database.
#
#   make test                                     # both suites
#   make test ARGS="tests/test_api_auth.py -x"    # pytest arguments for the API suite only
#   scripts/test.sh --agent-only
#
# The API suite creates its own throwaway database for every run (and drops it afterwards) on
# the compose Postgres, through the 127.0.0.1-only DB_PORT, so the db service must be up; this
# script starts it if needed. The suites use their own test secrets: nothing from .env except
# the database login reaches them. Python deps go into .venv (see scripts/lib.sh).
#
# Options (before any pytest arguments):
#   --api-only     only the API suite
#   --agent-only   only the agent suite (no database needed)

set -euo pipefail
# shellcheck source=scripts/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
cd "$ROOT"

RUN_API=1
RUN_AGENT=1
while [ $# -gt 0 ]; do
  case "$1" in
    --api-only) RUN_AGENT=0 ;;
    --agent-only) RUN_API=0 ;;
    -h | --help)
      sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) break ;;
  esac
  shift
done

# The suites set their own CLIMATE_* defaults (test tokens, test secrets); a value inherited
# from this shell would override them, so clear every one.
for var in $(env | sed -n 's/^\(CLIMATE_[A-Za-z0-9_]*\)=.*/\1/p'); do
  unset "$var"
done
unset ANTHROPIC_API_KEY 2>/dev/null || true

status=0
if [ "$RUN_API" -eq 1 ]; then
  [ -f "$ENV_FILE" ] || die "no .env yet: run 'make bootstrap' first (the API tests use its database)"
  need_docker --daemon
  step "Starting the database (if it is not running)"
  docker compose up -d --wait db >/dev/null
  ensure_venv api
  CLIMATE_TEST_ADMIN_URL="$(database_url_from_env postgres)"
  export CLIMATE_TEST_ADMIN_URL
  step "API tests"
  (cd "$ROOT/api" && "$VENV/bin/python" -m pytest -q -p no:cacheprovider ${1+"$@"}) || status=1
fi
if [ "$RUN_AGENT" -eq 1 ]; then
  ensure_venv agent
  step "Agent tests"
  (cd "$ROOT/agent" && "$VENV/bin/python" -m pytest -q -p no:cacheprovider) || status=1
fi
exit "$status"
