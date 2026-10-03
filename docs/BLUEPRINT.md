# Climate AI blueprint

A whole-house optimizer for three ecobee thermostats. It monitors every sensor, separates weather
from strategy in every number, learns how the floors push heat into each other, and adjusts
schedules to minimize total runtime while occupied rooms stay comfortable. Claude Opus 5.5
(through the Claude Agent SDK) is the analyst. A deterministic controller does the controlling.

The interactive version of this document, including a working mockup of the app, is
[`blueprint.html`](./blueprint.html). Open it in a browser.

---

## 1. The house

| Unit | Thermostat | Remote sensors | Notes |
|---|---|---|---|
| **Main floor** | ecobee in the Hallway | School Room, Living Room, Kitchen | Its heat rises into the upstairs through the floor and the open stairwell |
| **Upstairs** | ecobee Smart Thermostat Essential in the Toy Room | Girls' Room | Under the roof. Loaded by the sun and by heat from the main floor |
| **Bed / Office** | ecobee in the Bedroom (confirm) | Office | Opposite side of the house. No link to the upstairs; a weak link to the main floor at most |

That's seven temperature points, each with motion-based occupancy. Humidity is measured at the three
thermostats only; SmartSensors report temperature and occupancy. A sensor reads "occupied" for
30 minutes after it last saw motion.

**Observed problem.** When the main floor is empty, ecobee Smart Away lets it float to about 80°F. If
someone is upstairs holding 77°F, heat from the warmer main floor rises, the upstairs unit hits
100% duty and runs all afternoon, and the house spends more than the main floor saved. Each ecobee
reports only itself, so its reports can't show this.

**What to measure first:** how strongly each floor is coupled to the others, each unit's cooling
rate, sensitivity to sun, and balance points.

---

## 2. How the three units run together

### Controller rules

1. **Linked floors (buffer zone).** When the main floor is empty but the upstairs is occupied, the
   main floor's target is tied to the upstairs target plus a learned offset. The starting offset
   is 1°F cooler than upstairs. It never floats to 80°F.
2. **Setback and recovery move together.** When the whole house is empty, both floors set back and
   keep the offset (for example main 80°F, upstairs 82°F). On recovery the main floor leads by a
   learned interval (0–45 min), so the upstairs recovers with less heat coming up from below.
3. **Pre-cool with the forecast.** On hot, sunny days, cool the upstairs 1–2°F below target before
   noon while the outdoor air is mild, then coast through the solar peak. Skip it on mild days.
4. **Sensor sets by time of day.** Upstairs uses the Toy Room by day and the Girls' Room at night.
   The main floor uses the Living Room, Kitchen and School Room. The wing uses the Office 9–5 and
   the Bedroom at night. These are written into the ecobee comfort settings, and Follow Me is
   turned off.
5. **Bed / Office stays independent** unless the data shows coupling to the main floor.
6. **Protect the equipment and the air.** Minimum run and off times; cycles per hour tracked;
   indoor humidity ≤ 58%; a manual change at a thermostat makes the app back off.

### Objective

```
minimize    Σ over units  w_u · runtime_u        (w_u = unit power draw; 1.0 until measured)
subject to  occupied rooms inside their comfort band ≥ 97% of minutes
            every setpoint inside the hard limits
            ≤ 1 change per unit per 30 min, ≤ 2°F per change
            indoor humidity ≤ 58%
            no short cycling (runs < 5 min)
```

The right offset depends on outdoor temperature, sun and time of day, so it is learned per
condition rather than fixed. Example from the illustrative house model: on a 95°F afternoon with
the upstairs at 77°F, Smart Away (80/77) maxes out the upstairs. Keeping the main floor 2°F cooler
gives the lowest combined runtime, and the upstairs holds its target.

### A cooling day

| Time | What happens | Who decides |
|---|---|---|
| 3:30 AM | Yesterday's ecobee data is final (it runs about an hour behind). Models refit; Claude reviews and writes the digest | Model jobs + Claude |
| 6:00 AM | Forecast pulled; candidate plans simulated; cheapest plan that meets comfort chosen | Controller |
| 7–11 AM | Pre-cool upstairs on hot days | Controller |
| Midday | Main floor empties; the linked rule holds it | Controller |
| 3–6 PM | Duty and room temperatures watched every 3 min; main floor lowered within limits if the upstairs is about to max out | Controller |
| 5:30 PM | Evening recovery, main floor leading | Controller |
| 10 PM | Night sensor sets; quiet hours | ecobee program, written by the app |

---

## 3. Learning loop

1. **Backfill, then watch.** Pull the 5-minute history ecobee still holds (roughly 12–18 months,
   in 31-day chunks) and the matching hourly weather from the Open-Meteo archive. No changes yet.
2. **Fit the weather baseline.** Per unit: grid-search the balance point (30–90°F, CalTRACK
   style), compute degree-days from hourly temperatures, and regress runtime on degree-days,
   sun and humidity. Gate: CV(RMSE) ≤ 20% on held-out days.
3. **Fit the thermal model.** A grey-box RC model per zone, refit nightly, with a Kalman filter
   between refits:

   ```
   C_up·dT_up/dt     = (T_out−T_up)/R_up + (T_main−T_up)/R_mu + k_s·max(0, T_main−T_up)
                       + a_up·Sun − Q_up·on_up(t) + g_up
   C_main·dT_main/dt = (T_out−T_main)/R_m + (T_up−T_main)/R_mu + (T_bed−T_main)/R_mb
                       + a_m·Sun − Q_main·on_main(t) + g_main
   C_bed·dT_bed/dt   = (T_out−T_bed)/R_b + (T_main−T_bed)/R_mb + a_b·Sun − Q_bed·on_bed(t) + g_bed
   ```

   A small `R_mu` means a strong main-to-upstairs link. `k_s` is the one-way stairwell term.
   A large `R_mb` confirms the wing is independent.
4. **Simulate tomorrow.** Run candidate plans (offset, pre-cool, recovery lead) against the forecast
   and keep the cheapest plan that meets comfort (simple model predictive control).
5. **Test in the real house.** Randomized switchback: days are randomly assigned to champion or
   challenger, the first few hours after each switch are dropped (thermal mass), and results are
   weather-normalized. A test stops when the 90% interval excludes zero or it hits its maximum length.
6. **Adopt, then keep tuning.** Settings become condition-specific, Bayesian optimization picks the
   next values to try, and drift detection flags season changes or equipment problems.

**Division of labor.** The math (statsmodels/scipy) fits models, runs the 3-minute control loop,
enforces limits and computes statistics. Claude reads results, explains them, spots anomalies,
proposes experiments and answers questions. Claude is never in the real-time loop and never
writes to a thermostat directly.

**Dialed in** means experiments stop finding differences larger than their own uncertainty. Heating
season needs a new learning pass, because heat rising from the main floor then helps the upstairs.

---

## 4. Weather and analytics

### Sources (all free)

| Source | Role | Notes |
|---|---|---|
| Open-Meteo forecast | Primary | No API key. Free for non-commercial use, which includes personal home automation. 10,000 calls/day. 16-day forecast; 15-minute data native in North America. CC BY 4.0: show "Weather data by Open-Meteo.com" next to displayed data |
| Open-Meteo archive | Backfill | Hourly back to 1940. ERA5 lags about 5 days; IFS 9 km has no delay |
| Open-Meteo previous runs | Judging forecast-based decisions | What the forecast said at decision time |
| NWS api.weather.gov | Cross-check | No key. Requires a User-Agent with a contact email. Hourly station observations |
| ecobee runtime report | Cross-check | `outdoorTemp`, `outdoorHumidity` every 5 minutes |
| Personal weather station | Optional | Tempest or Ambient for on-site sun and temperature |

Hourly variables: `temperature_2m, relative_humidity_2m, dew_point_2m, apparent_temperature,
shortwave_radiation, direct_radiation, diffuse_radiation, global_tilted_irradiance (tilt/azimuth
set to the roof and west windows), cloud_cover, wind_speed_10m, wind_direction_10m, is_day`.

### Separating weather from strategy

- **Expected runtime for every day** from the per-unit baseline.
- **Savings = expected − actual** (IPMVP avoided energy). Runtime is metered per unit.
- **Weekly attribution waterfall**: weather, sun, strategy, noise.
- **Randomized switchbacks** for small effects (LBNL's randomized M&V work found them more accurate
  and faster than pre/post comparisons).
- Every savings claim carries a 90% interval. Unusual days are flagged. Adopted strategies get
  occasional control days.

### Metrics

Runtime, weather-expected runtime, duty cycle, maxed-out minutes (100% duty while above target),
comfort score (share of occupied-room minutes in band), coupling coefficient (°F/hr of upstairs
gain per °F the main floor sits above it), cooling rate (°F per run-minute, weather-adjusted;
falls when a filter clogs or refrigerant runs low), cycles per hour, indoor humidity.

---

## 5. Claude (Agent SDK, Opus 5.5)

### Authentication

- **Pro/Max subscription:** run `claude setup-token` to get a one-year token and set
  `CLAUDE_CODE_OAUTH_TOKEN` in the agent service. Anthropic documents this for scripts and CI.
  Usage counts against the plan's limits. Personal use only: Anthropic's Agent SDK docs don't
  allow offering claude.ai login in products for other people.
- **API key:** `ANTHROPIC_API_KEY` from platform.claude.com, billed per token (Opus 5.5: $4 / $20
  per million input/output tokens). Use `max_budget_usd` to cap each run.

### When Claude runs

Nightly review (3:30 AM), weekly report (Sunday), triggered investigations (upstairs maxed out
45+ min, cooling-rate drop, humidity high, sensor offline), and chat: in the app, and from Claude
Code or Claude Desktop through the app's MCP server.

### Tools

| Tool | Type |
|---|---|
| `get_house_status`, `query_runtime`, `query_sensors`, `get_weather`, `baseline_report`, `coupling_report`, `simulate_plan`, `experiment_results` | read |
| `propose_policy_change`, `propose_experiment`, `publish_report` | gated: validated against hard limits in code, then queued per the autonomy mode |

Claude has no tool that writes to a thermostat. Only the controller writes to ecobee, and it
enforces the limits itself.

```python
options = ClaudeAgentOptions(
    model="claude-opus-5-5",
    system_prompt=open("agent/prompts/nightly.md").read(),
    mcp_servers={"house": house},              # create_sdk_mcp_server(name="house", tools=[...])
    allowed_tools=["mcp__house__query_runtime", "mcp__house__propose_policy_change"],
    disallowed_tools=["Bash", "Write", "Edit", "WebFetch", "WebSearch"],
    permission_mode="dontAsk",
    max_turns=40,
    max_budget_usd=1.00,
)
```

---

## 6. Architecture

Docker Compose on one home server.

- **Sources:** ecobee cloud; HomeKit local (optional backup and real-time path); Open-Meteo + NWS;
  optional power monitor.
- **Ingest:** `collector` (3-min summary poll; detail fetch on revision change; hourly runtime
  reports), `weather`, one-time `backfill`.
- **Store & brains:** Postgres + TimescaleDB; `models` (baselines, RC model, simulation);
  `experiments`; `controller` (3-min loop, guardrails, audit log).
- **Surfaces:** FastAPI (REST + WebSocket); Vue 3 + TypeScript PWA over Tailscale; `agent` (Claude
  Agent SDK); `mcp` (the same tools for Claude Code and Desktop); push notifications (ntfy or
  Pushover).

### ecobee access

ecobee stopped issuing new developer API keys on 2024-03-28. Existing keys still work.

| Path | Status |
|---|---|
| **ecobee account sign-in** via `python-ecobee-api` 0.4.x (username, password, MFA; refresh token; regular `api.ecobee.com/1` endpoints) | **Recommended.** Home Assistant's ecobee integration has worked this way since 2026.3. It borrows ecobee's web-app login rather than an approved developer key, so it can break if ecobee changes its login. Keep it behind one adapter |
| Developer key from before March 2024 | Most stable, if you have one |
| HomeKit via Home Assistant HomeKit Controller | Backup and real-time: pushed temperatures, sensors, occupancy, cooling/idle state, targets. No runtime history and no sensor-set control. A thermostat pairs with only one HomeKit controller |

| Need | Call | Cadence | Notes |
|---|---|---|---|
| Change detection | `GET /thermostatSummary` (`includeEquipmentStatus`) | every 3 min | ecobee asks clients not to poll faster |
| Live detail | `GET /thermostat` (runtime, sensors, settings, program, events) | on revision change | up to 25 thermostats per call |
| 5-minute truth | `GET /runtimeReport` (`includeSensors`) | hourly + nightly re-pull | compressor seconds per 5-min interval; lags up to ~1 h; ≤ 31 days per request |
| Change a target | `setHold` / `resumeProgram` | controller decisions | tenths of °F (77°F = 770); timed holds |
| Sensor sets | program update | rarely | replaces the whole program, so read, edit, write back |
| Stop ecobee fighting | `settings.autoAway`, `settings.followMeComfort` | once, verified daily | demand-response shows up in `dmOffset` |

Access tokens last about an hour; refresh tokens rotate, so always store the newest one.

### Core tables

`units`, `sensors`, `readings_5m`, `runtime_5m`, `weather_hourly`, `policies`, `policy_versions`,
`experiments`, `experiment_days`, `control_actions` (every ecobee write: who, why, before, after),
`model_fits`, `agent_runs`, `reports`.

### Repo layout

```
climate-ai/
├── api/climate/{sources,collector,models,control,experiments,analytics,store}/
├── agent/            Claude Agent SDK runners + prompts (tools shared with mcp/)
├── mcp/              MCP server for Claude Code / Desktop
├── web/              Vue 3 + TypeScript PWA
├── db/migrations/
├── docker-compose.yml
└── docs/
```

---

## 7. Roadmap

| Phase | When | Deliverable | Done when |
|---|---|---|---|
| 0 | this week | ecobee sign-in, Compose skeleton, schema, Open-Meteo | all 7 sensors stored every 3 min |
| 1 | weeks 1–2 | backfill, Live + Runtime screens, daily digest | runtime per unit matches ecobee's reports within 1% |
| 2 | weeks 2–4 | baselines, weather-expected runtime, attribution, first coupling estimate | baselines within 20% CV(RMSE) on held-out days |
| 3 | weeks 3–5 | Claude analyst (read-only), weekly report, MCP server | the weekly report says something new |
| 4 | weeks 5–7 | controller in Suggest mode, guardrails, Linked floors | upstairs maxed-out minutes near zero; savings with a 90% interval |
| 5 | week 7 on | RC model, switchbacks, pre-cooling, Auto mode, heating season | experiments stop finding gains larger than their uncertainty |

### Open questions

1. AC + furnace or heat pumps? Tonnage per unit?
2. Do the upstairs ducts run through the attic?
3. Is the stairwell open, or is there a door?
4. Is there an ecobee developer key from before March 2024?
5. What server runs this? Is Home Assistant already running?
6. Weather point (address or ZIP), kept on the server.
7. Flat or time-of-use electricity rate?
8. Are Smart Home/Away, Follow Me or eco+ features on today?
9. Comfort bands per floor and time; when is the main floor usually empty?
10. Claude via subscription token or Console API key?
