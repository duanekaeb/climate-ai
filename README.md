# Climate AI

One brain for three ecobees. Climate AI monitors every thermostat and room sensor in the house
(ecobee cloud plus local HomeKit), separates weather from strategy with weather-normalized
baselines, learns how the floors push heat into each other, and adjusts schedules to minimize
total HVAC runtime while occupied rooms stay comfortable. A deterministic controller enforces
every limit; Claude Opus 5.5, running on the owner's own Claude subscription through the Claude
Agent SDK, reviews the results a few times a day, explains them and proposes experiments, but
is never in the control path.

<!-- Screenshots: add images under docs/img/ and link them here (Live, Rooms, Did it work?). -->
_Screenshots: coming soon._

## Quickstart (simulator, no credentials needed)

On a Linux machine with Docker and Compose v2:

```bash
git clone <your repo URL> climate-ai && cd climate-ai
cp .env.example .env && chmod 600 .env
sed -i \
  -e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(openssl rand -hex 24)|" \
  -e "s|^CLIMATE_SECRET_KEY=.*|CLIMATE_SECRET_KEY=$(openssl rand -base64 32 | tr '+/' '-_')|" \
  -e "s|^CLIMATE_SESSION_SECRET=.*|CLIMATE_SESSION_SECRET=$(openssl rand -hex 32)|" \
  -e "s|^CLIMATE_AGENT_TOKEN=.*|CLIMATE_AGENT_TOKEN=$(openssl rand -hex 32)|" \
  -e "s|^CLIMATE_MCP_TOKEN=.*|CLIMATE_MCP_TOKEN=$(openssl rand -hex 32)|" \
  .env
sed -i 's|^CLIMATE_COOKIE_SECURE=.*|CLIMATE_COOKIE_SECURE=false|' .env   # plain http while trying it out
docker compose up -d --build
```

Open <http://localhost:8470> on the server (or `ssh -N -L 8470:127.0.0.1:8470 you@server` and
open it on your laptop), choose the owner password, and explore the simulated house. Then
follow **[docs/DEPLOY.md](docs/DEPLOY.md)** to put it behind your nginx with HTTPS (and switch
`CLIMATE_COOKIE_SECURE` back to `true`), connect ecobee and HomeKit, and sign Claude in.

## Architecture

Docker Compose on one home server; every port binds to `127.0.0.1` and the owner's nginx is
the front door.

| Service | Command | Role |
|---|---|---|
| `db` | TimescaleDB 2.30.2 on PostgreSQL 16 | Readings, runtime, occupancy, weather, policies, experiments, control log, reports |
| `app` | `uvicorn climate.api.app:app` | REST API under `/api`, websocket `/api/ws`, and the built Vue 3 PWA at `/`; migrates on start |
| `worker` | `python -m climate.worker` | ecobee cloud (or simulator), Open-Meteo weather, analytics, the controller, nightly jobs |
| `homekit` | `python -m climate.collector.homekit_service` | Local HomeKit controller for the thermostats and SmartSensors (host network; profile `homekit`) |
| `agent` | `python -m climate_agent.scheduler` | Claude analyst on the owner's subscription; talks to the API with a bearer token, no database access |
| `mcp` | `python -m climate_agent.mcp_server --http 0.0.0.0:8471` | The same tools for Claude Code / Desktop (profile `mcp`) |
| `ntfy` | `binwiederhier/ntfy` | Optional self-hosted push notifications (profile `ntfy`) |

The iPhone app in [`ios/`](ios/README.md) is a thin WKWebView wrapper around the same web app;
"Add to Home Screen" from Safari works too, without Xcode.

## Documentation

- [`docs/BLUEPRINT.md`](docs/BLUEPRINT.md): the plan of record (house, control rules, occupancy,
  learning loop, weather normalization, Claude integration, architecture, roadmap)
- [`docs/blueprint.html`](docs/blueprint.html): the same plan with a working mockup of the app
  (open it in a browser)
- [`docs/BUILD.md`](docs/BUILD.md): code layout, processes and the API contract
- [`docs/DEPLOY.md`](docs/DEPLOY.md): installing, nginx + HTTPS, Tailscale, ecobee, Claude, MCP,
  backups, updating, troubleshooting
- [`docs/HOMEKIT.md`](docs/HOMEKIT.md): pairing the thermostats locally, with what is verified
  and what is not
- [`ios/README.md`](ios/README.md): building the iPhone wrapper

## Safety principles

- **Claude is never in the control path.** Models and the controller make every real-time
  decision; Claude verifies, explains and proposes on a schedule (nightly, weekly, at most three
  triggered runs a day, on request). If Claude is unavailable, changes wait and the house keeps
  running.
- **Claude never writes to a thermostat.** Its tools can only queue proposals or sign off / hold
  model-queued changes inside pre-approved ranges, and every change passes backtest, simulation,
  shadow days and a trial window first.
- **Guardrails in code.** Every write goes through hard limits (setpoint range, at most one
  change per unit per 30 minutes and 2 °F per change, humidity, no short cycling) before it is
  sent.
- **Read back every write.** Only timed holds of 1-2 hours, renewed while healthy; every write is
  read back and logged in `control_actions` with the channel, reason, before/after values and
  read-back result. If the server dies, holds expire and each ecobee runs its own schedule.
- **Occupied rooms first; uncertain means occupied.** No savings claim without weather
  normalization and a 90% interval. No invented temperatures for rooms without a sensor.
- **Your subscription, not an API key.** The agent runs on `CLAUDE_CODE_OAUTH_TOKEN` and refuses
  to start if `ANTHROPIC_API_KEY` is set.
- **Private by default.** LAN + Tailscale only; secrets encrypted at rest; nothing exposed to the
  internet.

Weather data by [Open-Meteo.com](https://open-meteo.com/).

## License

Private. All rights reserved.
