#!/usr/bin/env bash
# Vite dev server for the web app with hot reload, on http://localhost:5173.
#
#   make dev        # starts the API with live reload (docker-compose.dev.yml), then this
#   make web-dev    # this alone, when the dev API is already up
#
# Vite proxies /api and the /api/ws websocket to the API that docker-compose.dev.yml publishes
# on 127.0.0.1:${DEV_API_PORT:-8000} (CLIMATE_API_PROXY, read by web/vite.config.ts). Needs Node 22.12+ on this computer
# (macOS: brew install node@22). Sign-in works over plain http because bootstrap set
# CLIMATE_COOKIE_SECURE=false; the browser talks to one origin (localhost:5173), so the refresh
# cookie and the same-origin checks behave exactly as in production.

set -euo pipefail
# shellcheck source=scripts/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
cd "$ROOT/web"

have node || die "Node.js is not installed (macOS: brew install node@22; Linux: https://nodejs.org)"
node -e 'const [a,b]=process.versions.node.split(".").map(Number); process.exit(a>22||(a===22&&b>=12)?0:1)' \
  || die "Node $(node -v) is too old: Vite needs 22.12 or newer"

if [ ! -d node_modules ] || [ package-lock.json -nt node_modules/.package-lock.json ]; then
  step "Installing web dependencies (npm ci)"
  npm ci
fi

port="$(env_get DEV_API_PORT)"
port="${port:-8000}"
# web/vite.config.ts reads CLIMATE_API_PROXY for its /api (and websocket) proxy target.
export CLIMATE_API_PROXY="http://127.0.0.1:$port"
step "Vite on http://localhost:5173 (API: $CLIMATE_API_PROXY). Ctrl-C stops it."
exec npm run dev
