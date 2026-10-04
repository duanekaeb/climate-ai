# CLAUDE.md: Climate AI

Read `docs/BLUEPRINT.md` before building anything. It is the plan of record.

## The house

- **Main floor:** ecobee in the Hallway (model to confirm); SmartSensors in the School Room,
  Living Room and Kitchen.
- **Upstairs:** ecobee Smart Thermostat Essential (`attisRetail`) in the Toy Room, which has no
  built-in occupancy sensor; SmartSensor in the Girls' Room. Confirm whether one of the five
  SmartSensors is also in the Toy Room; if not, that room has no occupancy signal.
- **Bed / Office wing:** ecobee in the Bedroom (model to confirm); SmartSensor in the Office.
- Five SmartSensors in total (eight temperature points with the thermostats). Every SmartSensor
  reports temperature and occupancy; they are the primary occupancy source.
- The main floor drives heat into the upstairs (floor + open stairwell). The wing is independent
  of the upstairs and at most weakly linked to the main floor. Measure; don't assume.

## Ground rules

- **The machine learning and controller make every real-time decision; Claude is never in the
  control path.** Claude verifies and tunes on a schedule (nightly, weekly, ≤ 3 triggered runs a
  day, on request). If Claude can't run, pending changes wait and the house keeps running.
- **Claude never writes to a thermostat.** Only the controller writes to ecobee. Claude's write
  tools only queue proposals or sign off / hold changes the models queued, validated against hard
  limits in code, then backtested, shadowed and trial-windowed.
- **Claude runs on the owner's Claude subscription through the Agent SDK**: authenticate with
  `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`. `ANTHROPIC_API_KEY` must never be set on
  the server (it would take priority and bill the API). Never use `--bare` mode (it ignores the
  subscription). Pin the SDK version and check sign-in at startup. Keep tool outputs summarized
  and turns capped. On a usage-limit error, reschedule after the reset. Warn 30 days before the
  one-year token expires. Built-in tools are disallowed and the agent runs in an empty `cwd`.
- **No savings claim without weather normalization.** Compare total-house runtime against the
  weather-expected baseline and show a 90% interval. Fix the success measure and 2–3
  checkpoints before an experiment starts, with stricter intervals at the early ones; never stop on
  a daily peek.
- **Runtime = stage-1 seconds.** `compCool1` / `compHeat1` / `auxHeat1` already include stage-2
  time; never add the stage-2 columns on top.
- **Earn complexity.** The linked-floors rule ships first. The RC house model and automatic
  tuning replace it only if they beat it in walk-forward backtests.
- **Model code changes arrive as pull requests**, with the scoring harness and holdout data
  read-only and outside the agent's writable tree.
- **Occupancy: uncertain means occupied.** Sleep windows count as occupied. Never declare the
  house empty from phones alone.
- **ecobee writes:**
  - Only `holdType: holdHours` holds of 1–2 h, renewed while healthy.
  - Read back every write; the library swallows HTTP errors.
  - Program edits start from a fresh GET with a revision check.
  - Round temperatures before converting to tenths.
  - Temperature holds use the Home comfort setting's sensors, so keep Home's set current.
- **HomeKit writes:**
  - Use aiohomekit ≥ 4.0.
  - Write the hold end time before the hold, then verify.
  - Never write the Home/Sleep/Away setpoint fields.
- **Respect ecobee's limits:**
  - Poll `/thermostatSummary` no faster than every 3 minutes.
  - Fetch details only when a revision changes.
  - Keep at most one runtime report request open at a time.
  - Always persist the newest refresh token.
  - Keep the `pyecobee` logger above DEBUG, because it logs tokens.
- **Keep the ecobee auth path behind one adapter** (`api/climate/sources/ecobee.py`), with the web
  client ID configurable. The account-sign-in route is unofficial and has broken before.
- **Every ecobee write is logged** to `control_actions` with the channel, reason, before/after
  values and read-back result.
- **Open-Meteo attribution** ("Weather data by Open-Meteo.com") must appear wherever its data is
  shown.
- Stack: Python 3.12, FastAPI, SQLAlchemy 2.0, Postgres + TimescaleDB, Vue 3 + TypeScript,
  Docker Compose.
