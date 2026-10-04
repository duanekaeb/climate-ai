# Climate AI: build guide

How the code is laid out, who calls what, and the API contract. `BLUEPRINT.md` is the plan
of record for *what* the system does; this file is *how* the code does it. When the two
disagree, fix this file.

## Processes

```
                    ┌──────────── owner's nginx (GrowWise) ────────────┐
 iPhone app (WKWebView) / browser ──HTTPS──▶  climate.example  ──▶ app:8000
                    └──────────────────────────────────────────────────┘
 ┌─────────┐   REST + /api/ws    ┌──────────────┐   LISTEN/NOTIFY   ┌────────────────────┐
 │  web    │ ◀─────────────────▶ │  app (API)   │ ◀───────────────▶ │  db (Postgres +    │
 │ (built  │                     │  FastAPI     │                   │  TimescaleDB)      │
 │  into   │                     └──────────────┘                   └────────────────────┘
 │  app)   │                           ▲   ▲ bearer token                ▲      ▲      ▲
 └─────────┘                           │   │                             │      │      │
                         ┌─────────────┘   └──────────────┐              │      │      │
                  ┌──────┴──────┐                  ┌──────┴──────┐ ┌─────┴────┐ │ ┌────┴─────┐
                  │ agent       │  Claude Agent    │ mcp (opt.)  │ │ worker   │ │ │ homekit  │
                  │ (subscription│  SDK, no DB      │ Claude Code │ │ source,  │ │ │ host net │
                  │  token only) │  access          │ / Desktop   │ │ control, │ │ │ mDNS/HAP │
                  └─────────────┘                  └─────────────┘ │ analytics│ │ └──────────┘
                                                                   └──────────┘ │
                                                       ecobee cloud ◀─┘   Open-Meteo
```

| Service | Command | Talks to | Notes |
|---|---|---|---|
| `db` | timescale/timescaledb (pg16) | — | Port bound to 127.0.0.1 only (the homekit service uses it). |
| `app` | `uvicorn climate.api.app:app` | db | Migrates + seeds on start. Serves the built web app at `/`. |
| `worker` | `python -m climate.worker` | db, ecobee cloud or simulator, Open-Meteo | Owns the ThermostatSource. Only process that refreshes ecobee tokens or writes holds. |
| `homekit` | `python -m climate.collector.homekit_service` | db, thermostats on the LAN | `network_mode: host` (mDNS). Optional profile. |
| `agent` | `python -m climate_agent.scheduler` | app (HTTP, agent token) | Claude Agent SDK on the owner's subscription. No DB credentials. |
| `mcp` | `python -m climate_agent.mcp_server` | app (HTTP, mcp token) | Optional; same tools for Claude Code / Desktop. |
| `ntfy` | binwiederhier/ntfy | — | Optional push notifications. |

Data flow: sources → `collector.ingest` → `live_*`, `readings_5m`, `runtime_5m`,
`occupancy_events`, `weather_hourly` (live snapshots also → `utility.events.ingest` →
`utility_events`) → `state.load_house_state` (occupancy, offsets, policy, people's holds,
resume back-offs, utility events) → `control.policy.plan` → `control.guardrails.check` →
`control.controller` (suggest/act, read-back, `control_actions`) → `utility.skips.run`
(opt-outs) and `utility.events.sweep`. Nightly: baselines, room offsets, RC fit (shadow),
experiment days, change gates, daily report, agent runs; analytics leave utility-event days
out (`analytics.exclusions.event_days`). Behaviour for people's holds, utility events and
hand-back: `docs/specs/holds-and-utility-events.md`.

## Conventions

- Python code must run on **3.11 and 3.12** (tests run on 3.11 here; Docker uses 3.12).
- SQLAlchemy 2.0 **sync** sessions (`climate.store.db.session_scope`). FastAPI endpoints are
  plain `def`. Async loops call DB work directly when it is quick, or via `asyncio.to_thread`.
- Store UTC `timestamptz`; reason about schedules in `LocationSettings.tz`
  (`climate.timeutil`). Wire format: ISO-8601 UTC; dates are local calendar days.
- Temperatures °F. Runtime: **stage-1 seconds** in the DB; **minutes** on the wire.
- Never invent a temperature for a room without a sensor (`None`, shown as "unknown").
- Uncertain occupancy means occupied.
- Every thermostat write goes through `guardrails.check`, is read back, and is logged in
  `control_actions`. Claude never writes to a thermostat.
- Secrets: `climate.store.secrets` (Fernet, `CLIMATE_SECRET_KEY`). Never log tokens; keep the
  `pyecobee` logger at INFO or above.
- Open-Meteo data shown anywhere carries "Weather data by Open-Meteo.com".
- Tests: `api/tests` (pytest). Each session gets a throwaway database (see `conftest.py`);
  `tests/factories.make_history` writes deterministic synthetic history. Don't make a test
  depend on a module someone else owns being finished; insert rows directly instead.
- The app must run with **no credentials at all** (`source = simulator`).

## File ownership

| Area | Files |
|---|---|
| Contract (shared, change with care) | `config.py`, `timeutil.py`, `house.py`, `events.py`, `store/*`, `sources/base.py`, `control/policy.py` (types + `PolicyParams`), `api/schemas.py`, `api/auth.py`, `api/app.py`, `tests/conftest.py`, `tests/factories.py`, `web/src/api/*`, `web/src/router.ts`, `web/src/App.vue` |
| Simulator + weather | `sources/simulator.py`, `sources/openmeteo.py`, `sources/nws.py`, `collector/weather.py` |
| ecobee cloud | `sources/ecobee.py` |
| HomeKit | `sources/homekit.py`, `collector/homekit_service.py`, `api/scripts/pair_ecobee.py` |
| Collector + worker | `collector/ingest.py`, `collector/poller.py`, `collector/backfill.py`, `worker.py`, `notify.py`, `agent_queue.py`, `cli.py`, `__main__` shims |
| Occupancy + control | `state.py`, `occupancy/*`, `control/policy.plan`, `control/guardrails.py`, `control/controller.py`, `control/handback.py`, `control/changes.py` |
| Utility events | `utility/events.py` (ingest, sweep, alerts; `event_key` / `load_active` / `change_label` are contract), `utility/skips.py`, `analytics/exclusions.py` |
| Analytics | `analytics/*` |
| Experiments + models | `experiments/*`, `models/*` |
| API routers | `api/routers/*`, `api/labels.py` |
| Claude agent + MCP | `agent/` |
| Web views | `web/src/views/*`, `web/src/components/*` (new ones), `web/src/stores/*` (new ones) |
| Deploy | `docker-compose.yml`, `docker/`, `.env.example`, `deploy/`, `ios/`, `docs/DEPLOY.md`, `docs/HOMEKIT.md`, `README.md` |

## API

All under `/api`. Roles: **public** (no auth), **reader** (owner or agent), **owner**,
**writer** (owner or agent; gated rules inside), **agent** (agent token only).
Bodies and responses are the classes in `climate/api/schemas.py`; the web app's
`src/api/types.ts` is generated from them (`python api/scripts/export_schema.py >
web/src/api/schema.json && cd web && npm run gen:types`).

| Method | Path | Role | Body → Response |
|---|---|---|---|
| GET | `/health` | public | → `Health` |
| GET | `/auth/state` | public | → `AuthState` |
| POST | `/auth/setup` | public, only while no password is set | `PasswordBody` → `AuthState` (sets cookie) |
| POST | `/auth/login` | public (throttled) | `PasswordBody` → `AuthState` |
| POST | `/auth/logout` | public | → `AuthState` |
| POST | `/auth/logout-everywhere` | owner | → `AuthState` (invalidates every owner session; so does a password change) |
| GET | `/status` | reader | → `HouseStatus` (each `UnitLive` says whose hold runs: `hold_owner` + `hold_label` in house time, from `api/labels.py`; `person_hold`, `resume_backoff_until` while in the future, `utility_event` = running, else next announced, else one over in the last 2 h; `upcoming_events`) |
| WS | `/ws` | owner cookie or agent token | server → `WsEvent` |
| GET | `/rooms/{room_key}/history?hours=24` | reader | → `RoomHistory` |
| GET | `/runtime/daily?days=30` | reader | → `DailyRuntime[]` |
| GET | `/runtime/intraday?date=YYYY-MM-DD` | reader | → `Intraday` |
| GET | `/weather?hours_back=48&hours_ahead=48` | reader | → `WeatherOut` |
| GET | `/analytics/savings?start=&end=` | reader | → `Savings` (default last 14 days) |
| GET | `/analytics/waterfall?week_start=` | reader | → `Waterfall` (default last full week, Monday start) |
| GET | `/analytics/baselines` | reader | → `BaselineOut[]` |
| GET | `/analytics/coupling?days=30` | reader | → `Coupling` |
| GET | `/analytics/comfort?days=7` | reader | → `ComfortRow[]` |
| GET | `/analytics/drift` | reader | → `DriftReport` |
| GET | `/analytics/natural-experiments?days=90` | reader | → `NaturalExperiments` |
| GET | `/control/settings` | reader | → `SettingsOut` (includes `utility_events`) |
| PUT | `/control/settings` | owner | `SettingsUpdate` → `SettingsOut` (sections given are validated and stored; `utility_events` under key `utility_events`; an old client's `control.manual_backoff_hours` is read as `resume_backoff_hours`) |
| POST | `/control/mode` | owner | `ModeBody` → `ControllerInfo` |
| GET | `/control/plan` | reader | → `PlanOut` |
| GET | `/control/actions?limit=100&unit_key=` | reader | → `ControlActionOut[]` |
| POST | `/control/hold` | owner | `ManualHoldBody` → `ControlActionOut` (queued; worker executes). Exactly what was typed within the hard envelope: min/max, the deadband (or the thermostat's `heatCoolMinDelta` when larger), the 0.5°F grid; no step limit, humidity guard, rate limit or back-off |
| POST | `/control/resume` | owner | `UnitBody` → `ControlActionOut` ("Resume schedule": queued `resume_program`, request kind `resume_schedule`; the ecobee schedule runs and the controller waits `resume_backoff_hours`) |
| POST | `/control/automatic` | owner | `UnitBody` → `ControlActionOut` ("Back to automatic": queued `resume_program`, request kind `automatic`; the controller steers again at once and any resume back-off ends) |
| GET | `/control/handback` | reader | → `HandbackInfo` (captured originals, mode, latest `handback` job, steps of the last finished one) |
| POST | `/control/handback` | owner | → `JobOut` (queues a `handback` job; 409 while one is queued or running) |
| GET | `/utility-events?days=30` | reader | → `UtilityEventOut[]` (open events plus those that started in the last `days`, newest first; `prep_label` from the current plan) |
| POST | `/utility-events/{id}/skip` | owner | `SkipEventBody` (optional; `all_units` default true) → `UtilityEventOut` (`skip='requested'` on that event's rows that can be skipped; the worker opts out; 409 when the controller is off or the event is mandatory, over, or already skipped) |
| POST | `/utility-events/{id}/unskip` | owner | `SkipEventBody` (optional) → `UtilityEventOut` (clears a still-`requested` skip; 409 otherwise) |
| POST | `/control/presence` | owner | `PresenceBody` → `SettingsOut` |
| GET | `/changes?status=` | reader | → `ChangeOut[]` |
| POST | `/changes` | writer | `ProposePolicyBody` → `ChangeOut` (proposed_by = owner or claude) |
| POST | `/changes/{id}/decision` | writer | `DecisionBody` → `ChangeOut` (403 if this role may not decide it) |
| GET | `/experiments` | reader | → `ExperimentOut[]` |
| GET | `/experiments/power?effect_pct=10&alpha=0.1&power=0.8&block_days=2` | reader | → `PowerOut` |
| GET | `/experiments/{id}` | reader | → `ExperimentDetail` |
| POST | `/experiments` | writer | `ProposeExperimentBody` → `ExperimentOut` |
| POST | `/experiments/{id}/decision` | owner | `ExperimentDecisionBody` → `ExperimentOut` |
| GET | `/models` | reader | → `ModelFitOut[]` (latest per kind/unit/mode) |
| POST | `/models/refit` | writer | → `JobOut` (worker runs it) |
| POST | `/models/backtest` | writer | `BacktestBody` → `BacktestOut` (synchronous, bounded) |
| POST | `/models/simulate` | writer | `SimulateBody` → `SimulateOut` |
| GET | `/jobs/{id}` | reader | → `JobOut` |
| GET | `/reports?kind=&limit=20` | reader | → `ReportOut[]` |
| GET | `/reports/{id}` | reader | → `ReportOut` |
| POST | `/reports` | writer | `PublishReportBody` → `ReportOut` (author claude for the agent) |
| GET | `/alerts?open=true` | reader | → `AlertOut[]` |
| POST | `/alerts/{id}/resolve` | owner | → `AlertOut` |
| GET | `/agent/status` | reader | → `AgentInfo` |
| GET | `/agent/runs?limit=20` | reader | → `AgentRunOut[]` |
| GET | `/agent/runs/{id}` | reader | → `AgentRunOut` |
| POST | `/agent/ask` | owner | `AskBody` → `AgentRunOut` (kind chat) |
| POST | `/agent/run` | owner | `RunBody` → `AgentRunOut` |
| POST | `/agent/claim` | agent | → `AgentRunOut \| null` |
| POST | `/agent/runs/{id}/finish` | agent | `AgentFinishBody` → `AgentRunOut` |
| POST | `/agent/heartbeat` | agent | `AgentHeartbeatBody` → `AgentInfo` |
| GET | `/setup` | owner | → `SetupState` |
| POST | `/setup/source` | owner | `SourceBody` → `SetupState` |
| PUT | `/setup/location` | owner | `LocationSettings` → `SetupState` |
| POST | `/setup/ecobee/login` | owner | `EcobeeLoginBody` → `EcobeeLoginResult` |
| POST | `/setup/ecobee/mfa` | owner | `EcobeeMfaBody` → `EcobeeLoginResult` |
| POST | `/setup/ecobee/signout` | owner | → `SetupState` |
| POST | `/setup/ecobee/map` | owner | `EcobeeMapBody` → `SetupState` |
| POST | `/setup/sensors/map` | owner | `SensorMapBody` → `SetupState` |
| POST | `/setup/homekit/pair` | owner | `HomekitPairBody` → `SetupState` |
| POST | `/setup/homekit/code` | owner | `HomekitCodeBody` → `SetupState` |
| POST | `/setup/homekit/unpair` | owner | `DeviceBody` → `SetupState` |

The agent role may never call `/control/*` writes, `/utility-events/*` skips, `/setup/*`,
`/agent/ask|run`, `/experiments/{id}/decision` or `/alerts/*/resolve`.

## Claude's tools (agent and MCP)

All tools call the API with the agent token and return short text summaries (numbers rounded,
lists capped) so turns stay cheap. None can reach a thermostat.

| Kind | Tools |
|---|---|
| Read | `get_house_status`, `query_runtime` (daily, N days), `query_room` (history summary), `get_weather`, `baseline_report`, `savings_report`, `waterfall_report`, `coupling_report`, `comfort_report`, `drift_report`, `natural_experiment_report`, `list_actions` / `explain_action`, `list_reports`, `get_settings` |
| Compute | `run_backtest`, `simulate_plan`, `estimate_power`, `request_refit` |
| Gated | `review_pending_changes`, `sign_off_change` (approve or hold, with a reason; only the owner rejects), `propose_policy_change`, `propose_experiment`, `publish_report` |

## Web

Vue 3 + TypeScript + Tailwind v4, Pinia, ECharts (SVG renderer), built as a PWA and served by
the app. Mobile-first: bottom tab bar on phones (Live, Rooms, Runtime, Results, More), sidebar
on wider screens. Dark mode follows the system and can be toggled. Use the tokens in
`src/style.css` (`bg-surface`, `text-muted`, `border-line`, unit colors `--color-unit-*`,
state colors `--color-st-*`) and the shared components (`Card`, `EChart`, `AsyncState`, `Icon`,
`OpenMeteoAttribution`). Every view handles loading, error and empty states, works at 375 px
wide without horizontal scrolling, and labels rooms without sensors as "no sensor".

| View | Shows |
|---|---|
| Live | per-unit card (temp, setpoints, call, whose hold and until when, Back to automatic / Resume schedule on a person's hold, the resume back-off, today's runtime, duty, maxed minutes, policy target + reason), utility event card (announced / running / skipped, Skip with confirm, undo, pre-cooling line), house occupancy, weather now, alerts, controller mode, agent status |
| Rooms | 11 rooms grouped by floor with temp, occupancy state and reason, priority marker; tap a room → 24 h chart (temp, occupancy band, unit setpoints) |
| Runtime | daily stacked runtime by unit vs expected (weather-normalized) and outdoor temp; intraday 5-min chart for a picked day |
| Did it work? | savings with 90% interval (or why not yet), weekly waterfall, baselines table with pass/fail |
| Floor coupling | scatter of upstairs duty vs main-minus-upstairs temperature, coefficient with interval, bed-wing placebo, natural experiments |
| Experiments | list, detail with schedule calendar and analysis at checkpoints, power calculator, propose form, approve/stop for the owner |
| Model | baseline + RC fits, identifiability notes, backtest and simulate forms |
| Ask Claude | ask a question, see runs (status, result markdown), trigger nightly/weekly, sign-in status and token expiry |
| Guardrails | controller mode switch, wait after Resume, hold reminder, hard limits, comfort bands, sleep windows, utility events (alerts, skip rules, pre-cooling), policy params with Claude's sign-off ranges, pending changes with approve/hold/reject, action log with read-back |
| Reports | daily / nightly / weekly reports (markdown) |
| Setup | location, data source (simulator ↔ ecobee), ecobee sign-in with MFA, thermostat → unit mapping and utility enrollment, sensor mapping, HomeKit devices and pairing (enter the code shown on the thermostat), Hand back to ecobee |

## Runtime contracts (JSON stored in the database)

| Where | Shape |
|---|---|
| `app_settings['heartbeat:worker'].detail` | `source`, `source_ok`, `source_error`, `source_last_success_at`, `source_consecutive_failures`, `last_poll_at`, `last_tick_at` |
| `app_settings['heartbeat:homekit'].detail` | `paired`, `online` (counts) |
| `app_settings['heartbeat:agent'].detail` | `AgentHeartbeatBody` fields (`signed_in`, `sdk_version`, `cli_version`, `token_expires_at`) plus `detail` (`idle`, `last_error`, `busy_run_id`, ...) |
| `app_settings['ecobee_status']` | `{signed_in_at, last_error, error_at}` (written by the sign-in path; signed-in = the encrypted refresh token exists) |
| `app_settings['ecobee_holds']` | `{unit_key: {heat_f, cool_f, end, hours, written_at}}`: the last hold we wrote, used to tell our holds from manual ones |
| `app_settings['simulator_state']` | simulator physics state (only in simulator mode) |
| `app_settings['sim_events']` | `{unit_key: [ThermostatEvent JSON]}`: utility / vacation events injected with `python -m climate.cli sim-event`; the worker's simulator re-reads it every simulated minute |
| `changes.payload` (policy) | `{"params": {partial PolicyParams}, "base_policy_version_id": int, "current": {...}}` |
| `changes.gates` | `validation` (list of violations), `validated_at`, `backtest` (BacktestOut or `{error}`), `shadow` `{start, days}`, `decision` `{actor, decision, reason, at}`, `trial` `{start, end, verdict...}` |
| `control_actions.request` (HomeKit channel) | `{"kind": "climate_hold", "climate": "home"\|"sleep"\|"away", "until": ISO-8601}` or `{"kind": "clear_hold"}` |
| `control_actions.request` (owner resumes, action `resume_program`) | `{"kind": "resume_schedule"\|"automatic", "unit_key", "reason"}` (`/control/resume` and `/control/automatic`) |
| `control_actions.request` (person's hold seen, `status='skipped'`, rule `hold_off`) | `{"kind": "manual_hold_detected", "by": "thermostat"\|"app", "heat_f", "cool_f", "climate_ref", "hold_type", "start", "end", "until"}` (one per hold; a refused controller resume adds `refused`, `attempted`). The homekit service's rows (actor `homekit_service`) carry `"source": "homekit"`, `"hold_type": "homekit_manual"`, `"until": null` |
| `control_actions.request` (Resume seen) | `{"kind": "manual_resume_detected", "person_hold_detection_id"}` for a person's hold, or `{"kind", "cancelled_action_id", "hold_end"}` for the controller's own |
| `control_actions` (utility opt-out) | action `opt_out_event`, rule `utility_opt_out`, request `{"kind": "utility_opt_out", "event_id", "by": "owner"\|"rule", "sent": {source request}}`; `event_prep` holds add `hold_end_by` |
| `alerts.dedupe_key` (utility) | `utility_event:<event_key>:<phase>`, phases `announced`, `running`, `ended`, `cancelled`, `skipped`, `skip_refused`, `skip_failed`, `skip_waiting_off`, `skip_waiting_cloud`, `rule:<unit>`; `unknown_event:<type>`; `person_hold:<unit>:<detection id>` (hold reminders) |
| `utility_events.detail` | the last `ThermostatEvent` seen plus the app's keys `skip_attempts` and `rule_requested_at` |
| `app_settings['utility_events']` | `UtilityEventSettings` (alerts, skip rules, pre-cooling); edited through `PUT /control/settings` |
| `app_settings['ecobee_original']` | `{unit_key: EcobeeOriginal}`: each unit's Smart Away, Follow Me and Home sensors before the controller's first change; read by `GET /control/handback` |
| `jobs` kind `handback` | `params` `{}`, `requested_by` `owner`; `result` `{"steps": [HandbackStep...]}` |
| `utility_events.skip*` (API writes) | owner skip: `skip='requested'`, `skip_by='owner'`, `skip_reason='Skipped from the app'`, `skip_requested_at`; a new request after `failed` resets `detail.skip_attempts` to 0; unskip clears all four |
| `ecobee_thermostats.settings` | the curated settings plus `utility` (`UtilityInfo` dict or null) and `drAccept`; Setup's `EcobeeThermostatOut.utility` / `dr_accept` / `enrolled` |
| `model_fits` kind `rc` | `metrics`: `rmse_1h`, `rmse_24h`, `persistence_rmse_1h/24h`, `bias_*`, `n_points`; `params.unidentified`: `["zone.param", ...]` |
| `model_fits` kind `room_offsets` | `params.offsets`: `{room_key: {"day": °F, "night": °F}}` |
| `UnitSnapshot.settings` | `autoAway`, `followMeComfort`, `heatCoolMinDelta` (°F), `program_heat_f`/`program_cool_f` (what the schedule holds now), `timeZone`, `drAccept` (ecobee) |

Compose networks: `db` sits on a private `data` network shared only with `app` and `worker`; the
agent and MCP containers reach the API over the default network and never see the database.
