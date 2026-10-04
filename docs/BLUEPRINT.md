# Climate AI blueprint

A whole-house optimizer for three ecobee thermostats. It monitors every sensor, separates weather
from strategy in every number, learns how the floors push heat into each other, keeps occupied
rooms comfortable first, and adjusts schedules to minimize total runtime. Statistical models do
the learning. Claude Opus 5.5 (through the Claude Agent SDK) is the analyst that steers them. A
deterministic controller does the controlling. Claude runs on the owner's own Claude
subscription, a few times a day, and is never in the control path.

The interactive version of this document, including a working mockup of the app, is
[`blueprint.html`](./blueprint.html). Open it in a browser.

---

## 1. The house

| Unit | Thermostat | Remote sensors | Notes |
|---|---|---|---|
| **Main floor** | ecobee in the Hallway (temperature, humidity, occupancy) | School Room, Living Room, Kitchen | Its heat rises into the upstairs through the floor and the open stairwell |
| **Upstairs** | ecobee Smart Thermostat Essential in the Toy Room (`attisRetail`; temperature, humidity) | Toy Room, Girls' Room | Under the roof. Loaded by the sun and by heat from the main floor |
| **Bed / Office** | ecobee in the Bedroom (temperature, humidity, occupancy) | Office | Opposite side of the house. No link to the upstairs; a weak link to the main floor at most |

There are nine temperature points: three thermostats and six SmartSensors. Every room reports
occupancy: the Hallway and Bedroom thermostats sense it themselves, and each SmartSensor reports it.
The Essential has no occupancy sensor, but the Toy Room's SmartSensor covers that room. No extra
hardware is needed for occupancy. Humidity comes from the thermostats only.

**Observed problem.** When the main floor is empty, ecobee Smart Away lets it float warm (about
80°F). If someone is upstairs holding 77°F, heat from the warmer main floor rises. That extra load is
the likely reason the upstairs unit hits 100% duty and runs all afternoon, so the house spends more
than the main floor saved. The history study measures how much is the floor and how much is sun. Each
ecobee reports only itself, so its reports can't show this. Two variants exist: classic Smart Away
switches to the Away comfort setting, while eco+ "Eco Away" applies a relative 1–4°F setback. The
backfill's calendar events show which one fired.

---

## 2. How the three units run together

### Controller rules

1. **Linked floors (buffer zone).** When the main floor is empty but anyone is upstairs (including
   asleep), the main floor's target is tied to the upstairs target plus a learned offset, starting
   at 1°F cooler. It never floats to its Away setting.
2. **Setback and recovery move together.** When the whole house is empty, both floors set back
   together with the main floor still the cooler one (for example main 80°F, upstairs 82°F). The
   setback gap is learned separately from the 1°F occupied offset. On recovery the main floor leads by a
   learned interval (0–45 min).
3. **Pre-cool with the forecast** on hot, sunny days; skip it on mild days.
4. **Sensor sets by time of day**, written into the ecobee comfort settings. Temperature holds use
   the **Home** comfort setting's sensors, so the current block's set goes into Home too.
   Follow Me is off.
5. **Bed / Office stays independent** unless the data shows coupling to the main floor.
6. **Protect the equipment and the air:** minimum run and off times, cycles per hour tracked,
   indoor humidity ≤ 58%, and back-off when someone changes a thermostat by hand.

### Objective

```
minimize    Σ over units  w_u · runtime_u        (w_u = unit power draw; 1.0 until measured)
subject to  occupied rooms inside their comfort band ≥ 97% of minutes
            every setpoint inside the hard limits
            ≤ 1 change per unit per 30 min, ≤ 2°F per change
            indoor humidity ≤ 58%
            no short cycling (runs < 5 min)
```

---

## 3. Occupancy

Occupied rooms come first. An occupied room's comfort is never traded for runtime.

**Why ecobee's flag isn't enough.** The API reports occupancy as "motion in the past 30 minutes",
updated about every 3 minutes in the cloud. PIR motion sensors miss people sitting still or
sleeping. Every room has a signal; the Toy Room's comes from its SmartSensor.

**Signals the app uses.**
- Live motion from each SmartSensor over local HomeKit (pushed in seconds), plus polled "seconds
  since last motion".
- Learned per-room patterns from the 5-minute sensor history (school days, weekends, holidays
  separately), for every room with a SmartSensor.
- Sleep windows for the Girls' Room and Bedroom: they count as occupied all night.
- Optional add-on presence sensors (mmWave), only for a room that keeps getting marked empty while
  someone sits still (likely the Office). Door contacts on bedrooms (closed after motion means
  someone is probably inside).
- Phones only for whole-house "adults away"; the kids carry no phones.

**States.** Start with three; add Arriving once predictions prove out.

| State | When | Effect |
|---|---|---|
| Occupied | Recent motion/presence, closed-door signal, or uncertainty | Must stay in band; ≤ 1°F over for ≤ 15 min |
| Asleep | Girls' Room / Bedroom inside sleep windows | ≤ 0.5°F over for ≤ 10 min; priority all night |
| Empty | No signal for the room's learned window | Drops out of the comfort targets, subject to linked floors |
| Arriving (later) | Learned pattern says the room is likely in use soon | Recovery starts early, timed by the house model |

A missed arrival costs comfort, while a false "occupied" only costs runtime, so uncertain means
occupied. The house counts as empty only when adults' phones are away, there has been no motion
anywhere for 45 minutes, and it's outside the sleep windows.

**Control.** ecobee averages the participating sensors equally, with no weights. Slow loop: the
sensor set per time block is written into the comfort settings (including Home). Every 3 minutes:
the app learns each room's typical offset from the thermostat's average and picks the setpoint
that keeps every occupied room in band. Conflicts on the same thermostat are resolved by priority:
upstairs, the Girls' Room at night and the Toy Room by day; in the wing, the Bedroom at night and the
Office in work hours; on the main floor, the School Room on school days and the Living Room in the
evening.

**Expectations.** Within one floor, occupancy mostly buys comfort. Runtime savings come from empty
floors and the linked-floors rule. Studies of multi-zone buildings (residence halls, zone-level
tests in larger buildings) find setbacks save less than predicted, because an empty zone pulls heat
from its neighbors, so savings are always measured on the whole house.

---

## 4. Machine learning and Claude

### The loop

Data (always on: ecobee every 3 min, HomeKit within seconds, weather hourly, add-on sensors) →
analytics and reporting (always on) → machine learning (always on: house model, plan, replans,
experiments) → controller (every 3 min, inside your limits). Claude reads the analytics and the
models' results a few times a day, verifies yesterday's decisions, signs off or holds the changes
the models queued, proposes better features and settings, and writes the reports.

### Who decides what

| Layer | Cadence | Does | Can change |
|---|---|---|---|
| **Claude Opus 5.5** (judgment, on your subscription) | nightly, weekly, minutes after an anomaly (≤ 3/day), on request | verify yesterday's decisions, sign off or hold model changes, diagnose, design and audit experiments, explain, propose model improvements | proposes features, model settings, plan parameters inside a pre-approved range, and experiments (you approve); can veto the optimizer's next pick; all gated |
| **Models & optimizer** (skill) | refit nightly; replan 6 AM, noon, 3 PM and on forecast shifts | baselines, house model, daily plan, experiment statistics, next-test choice | today's plan within policy and limits |
| **Controller** (reflexes) | every 3 min; seconds on local occupancy changes | runs the plan; writes 1–2 h timed holds renewed only while healthy | never waits on Claude |

Gates for every change: backtest → simulation → 3–7 shadow days (logs only) → Claude sign-off →
trial window (acts for a limited window, such as afternoons) → your limits.

### Why not Claude in the 3-minute loop

About 480 calls a day would hit a subscription's session and weekly limits within hours (on a paid
API key it would be roughly $300–1,300 a month). House temperatures respond over tens of minutes
to hours, so 480 quick decisions add no control. Opus 5.5 answers aren't guaranteed repeatable. The
house would have no driver during outages. And a 2026 review of 66 studies found no language-model
HVAC controller ready for real operation, recommending advisory roles. The layered design needs
only a handful of Claude runs a day, which fits a subscription.

### How Claude speeds up the learning

1. **Mine the history.** Treat past warm-main-floor afternoons as natural experiments, with checks
   against being fooled: the bed wing should show no effect, and fake event times on similar days
   should show none. Sun, weaker AC on hot afternoons and Smart Away all peak together, so a naive
   comparison overstates coupling.
2. **Kill bad ideas in simulation.** Only candidates that beat the model's own uncertainty get a
   real day.
3. **Size every test first.** A single house can usually confirm a 10–15% change within a season
   (roughly 1–7 weeks of test days per option, 2–14 weeks in all). A 5% change can take 50–200 days
   per option, so small refinements are judged mainly in the simulator.
4. **Fix the success measure and checkpoints in advance.** Total-house runtime, 2–3 pre-planned
   checkpoints, with stricter (wider) intervals at the early ones (alpha spending) so the looks
   together keep about a 10% false-win rate. Daily peeking manufactures false wins.
5. **Audit the next-test choice.** The optimizer's math picks the next setting to try; Claude
   explains it and can veto it with a reason.
6. **Fix the model.** Claude proposes features when errors show a pattern; the gates decide.

### Robustness, honestly

A public set of about 60,000 house models fitted to ecobee data has a median indoor-temperature
error of about 0.6°F one hour ahead and about 2.5°F a day ahead, and roughly 1 in 10 fit poorly.
Three coupled zones measured through runtime alone will do somewhat worse. That is good enough for
coarse decisions (offset band, pre-cool yes or no, recovery lead), not for trusting minute-by-minute
trajectories. Without a power monitor, heat flows are learned in runtime minutes, not watts.

**Build order:** weather baselines, the history study and the linked-floors rule (checked against
the weather baseline, then tuned with one planned switchback) come first. The full house model and automatic tuning join only if they beat that rule
in backtests.

### The house model (when it earns its place)

```
C_up·dT_up/dt     = (T_out−T_up)/R_up + (T_main−T_up)/R_mu + k_s·max(0, T_main−T_up)
                    + a_up·Sun − Q_up·on_up(t) + g_up
C_main·dT_main/dt = (T_out−T_main)/R_m + (T_up−T_main)/R_mu + (T_bed−T_main)/R_mb
                    + a_m·Sun − Q_main·on_main(t) + g_main
C_bed·dT_bed/dt   = (T_out−T_bed)/R_b + (T_main−T_bed)/R_mb + a_b·Sun − Q_bed·on_bed(t) + g_bed
```

Fitted from weeks 3–6 but run in shadow; its plans drive the house only once it beats the
linked-floors rule in backtests. Start simple; keep extra states (attic, envelope) only if they
predict better on held-out data.
Separate cooling and heating parameter sets. Publish nightly which parameters the data can't pin
down.

### Checks

Clean data; walk-forward backtests by season; both scatter (CV(RMSE) ≤ 20%) and bias; physical
sanity; shadow days; canary windows; drift watch that triggers a Claude investigation.

### Fallbacks

Failed model → last good model. No good model → linked-floors rules. Claude unavailable (plan limit,
expired sign-in) → changes awaiting sign-off stay pending, reports catch up later. Server down → holds expire
within 2 hours, and each ecobee runs its own schedule (Smart Away stays off in settings). Cloud
down → HomeKit live readings and basic timed holds.

### Claude and model code

Settings change through tools with automatic checks. Code changes come only as pull requests from
Claude Code. The scoring harness and recent holdout weeks stay read-only and out of the agent's
reach (a 2026 study found AI coding agents tampering with unlocked evaluations in about half of
runs). CI runs the backtest, a person merges, and a merged model starts in shadow mode.

---

## 5. Weather and analytics

| Source | Role | Notes |
|---|---|---|
| Open-Meteo forecast | Primary | No API key; non-commercial (includes personal home automation); 10,000 calls/day; 16-day forecast; 15-minute data native in North America; CC BY 4.0, so show "Weather data by Open-Meteo.com" |
| Open-Meteo archive | Backfill | Hourly back to 1940. ERA5 lags about 5 days; IFS 9 km has no delay |
| Open-Meteo previous runs | Judging forecast-based decisions | What the forecast said at decision time |
| NWS api.weather.gov | Cross-check | No key; User-Agent identifying the app (contact email recommended) |
| ecobee runtime report | Cross-check | `outdoorTemp`, `outdoorHumidity` |

Separating weather from strategy: expected runtime per day from per-unit baselines (balance point
searched over 30–90°F, CalTRACK-style); savings = expected − actual (IPMVP avoided energy); a weekly
attribution waterfall; randomized switchbacks on total-house runtime; a 90% interval on every claim.

Runtime metric: `compCool1` when cooling; `compHeat1` for a heat pump, or `auxHeat1` for a furnace or
backup heat. Stage-1 columns already include stage-2 time, so stage-2 columns are stored but never
added on top.

---

## 6. Claude setup (on your subscription)

Claude runs through the Claude Agent SDK on the home server, signed in with the owner's Claude plan.
There is no API key and no per-token bill.

**Setup, once:**
1. Run `claude setup-token` (browser approval) to get a one-year token.
2. Set it as `CLAUDE_CODE_OAUTH_TOKEN` in the agent service.
3. Make sure `ANTHROPIC_API_KEY` is not set anywhere on the server. It would take priority and bill
   the API.
4. Never run in `--bare` mode, which ignores subscription sign-in. Anthropic recommends bare for
   scripts and plans to make it the default for `-p`.
5. Pin the SDK version and check sign-in at startup.

**Footprint:**
- About one run a night, one a week, ≤ 3 triggered runs a day (coalesced), plus questions.
- Runs happen at night. Plan session, weekly and Opus limits are shared with the owner's own use.
- Tools return summaries and turns are capped.
- On a usage-limit error, reschedule after the reset. If only the Opus limit is hit, rerun on
  Sonnet 5.5.
- Warn 30 days before the token expires and immediately on any sign-in failure.

**Terms:** Anthropic's support article "Use the Claude Agent SDK with your Claude plan" (updated
2026-06-16) says Agent SDK and `claude -p` usage "still draw from your subscription's usage limits".
It also says Anthropic is reworking how subscriptions cover SDK use, so re-check it periodically.
The legal page asks developers building products for other people to use API keys; this is a
personal tool signed in as the owner. Claude is never in the control path, so any change can only
pause reports and tuning. Switching to an API key later is one environment variable.

**Alternatives checked:** Claude Code routines run in Anthropic's cloud and can't reach the home
LAN. Claude Desktop scheduled tasks need the desktop app open on an awake computer.

Tools: read (`get_house_status`, `query_runtime`, `query_sensors`, `get_weather`, `baseline_report`,
`coupling_report`, `drift_report`, `natural_experiment_report`, `explain_action`), compute
(`refit_model`, `run_backtest`, `simulate_plan`, `estimate_power`), gated (`review_pending_changes`,
`sign_off_change`, `propose_policy_change`, `propose_experiment`, `publish_report`). No tool talks to a thermostat.

```python
options = ClaudeAgentOptions(
    model="claude-opus-5-5",
    fallback_model="claude-sonnet-5-5",    # used if Opus is overloaded
    effort="medium",                       # "high" for the weekly report
    system_prompt=open("agent/prompts/nightly.md").read(),
    mcp_servers={"house": house},
    allowed_tools=["mcp__house__query_runtime", "mcp__house__propose_policy_change"],
    # dontAsk still runs tools that never prompt (file reads, Agent), so remove built-ins outright
    disallowed_tools=["Bash", "Read", "Write", "Edit", "Glob", "Grep", "Agent",
                      "NotebookEdit", "WebFetch", "WebSearch"],
    permission_mode="dontAsk",
    cwd="/srv/climate/agent-empty",        # empty directory: no secrets within reach
    max_turns=30,
)
# Signed in via CLAUDE_CODE_OAUTH_TOKEN; assert ANTHROPIC_API_KEY is not in the environment.
# Check ResultMessage.terminal_reason == "completed" before trusting a digest: an API failure on
# the final request can still report subtype "success". query() then raises ResultError after the
# error result (turn cap, usage limit, sign-in failure), so wrap the loop in try/except ResultError
# and, on a usage-limit error (429), reschedule after the reset.
```

---

## 7. ecobee connection

**Link 1, required: the ecobee cloud API, signed in with the ecobee account.** Use
`python-ecobee-api` 0.4.x (the library behind Home Assistant's ecobee integration, which added
account sign-in in 2026.3) for sign-in, MFA and refresh only. Make
every data call yourself:

| Need | Call | Cadence |
|---|---|---|
| Change detection | `GET /1/thermostatSummary` (`includeEquipmentStatus`) | every 3 min |
| Live detail | `GET /1/thermostat` (sensors, events, program, settings, extended runtime) | on revision change |
| 5-minute record | `GET /1/runtimeReport` (`includeSensors`) | hourly + nightly re-pull; lags up to ~1 h |
| Holds | `setHold` with `holdType: holdHours` (1–2 h), `resumeProgram` | controller only |
| Sensor sets | program update (read-modify-write) | a few times a day |
| Stop ecobee fighting | `settings.autoAway=false`, `settings.followMeComfort=false` | once, verified daily |

That's about 30,000 requests a month, well under the 85,000 ecobee set for developer-key apps.
Nothing is published for the account sign-in route, so the app stays inside that budget anyway.

**Link 2, strongly recommended: HomeKit, locally, with the server as controller** via
`aiohomekit` ≥ 4.0. Pushed: each SmartSensor's temperature, motion and occupancy; thermostat
temperature, humidity and heating/cooling state. Polled: seconds since last motion; equipment
running. Pair directly, not through Home Assistant: HA hides the ecobee vendor fields, and its mode
select creates permanent holds. One HomeKit controller per thermostat, so the ecobees leave Apple
Home.

**Not possible or not advised:** no local LAN API; no Matter support found (October 2026);
SmartSensors use a proprietary 915 MHz radio that pairs only with ecobee devices and has no public
decoder; firmware access only via a 2021 ecobee3 lite serial exploit. Replacing the ecobees with
ESP32 relays is possible but means building an uncertified thermostat. If cloud-free control
becomes a hard requirement, use a listed thermostat with a local API (Venstar ColorTouch, Honeywell
T6 Pro Z-Wave).

**Hardware to add (read-only), in priority order:**
None of this is needed for occupancy.
1. Stairwell top/bottom temperature sensors.
2. Per-unit power monitoring (Emporia Vue 3 or Shelly EM Gen3).
3. Optional mmWave for a room where people sit still and get marked empty.
4. Bedroom/Office door contacts.
5. A wired attic probe.
6. A 24V call monitor with supply/return probes.

**Adapter rules.**
- Read back every write; the library swallows HTTP errors.
- Program edits start from a fresh GET and a revision check.
- HomeKit holds: write the end time first, then the hold, then verify. Never write the
  Home/Sleep/Away setpoint fields, which permanently edit the schedule.
- Round temperatures before converting to tenths of °F.
- Use TOTP or SMS MFA (push and email are unsupported).
- Store only the newest refresh token, encrypted, and never the password.
- Make the web client ID configurable.
- Keep the library's debug logging off (it logs tokens).
- A circuit breaker falls back to HomeKit when the cloud fails.

**Phase 0 tests:**
1. Compressor columns from `runtimeReport` with the account-login token.
2. History depth.
3. Model numbers.
4. Which sensors a night-time hold uses.
5. The Essential's HomeKit sensor exposure.
6. The Smart Away variant in the backfill.

---

## 8. Architecture

Docker Compose on one home server.

- **Sources:** ecobee cloud, HomeKit (local), add-on sensors (Zigbee/ESPHome), Open-Meteo + NWS,
  optional power monitor.
- **Ingest:** `collector`, `homekit`, `weather`, `backfill`.
- **Store & brains:** Postgres + TimescaleDB, `occupancy`, `models`, `experiments`, `controller`.
- **Surfaces:** FastAPI (REST + WebSocket), Vue 3 PWA over Tailscale, `agent` (Agent SDK), `mcp`,
  push notifications.

Core tables: `units`, `sensors`, `readings_5m`, `runtime_5m`, `occupancy_events`, `room_state_1m`,
`weather_hourly`, `policies`, `policy_versions`, `experiments`, `experiment_days`,
`control_actions` (who, why, channel, before, after, read-back), `model_fits`, `agent_runs`,
`reports`. `runtime_5m` stores compCool1/2, compHeat1/2, auxHeat1/2 and fan seconds.

```
climate-ai/
├── api/climate/{sources,occupancy,collector,models,control,experiments,analytics,store}/
├── agent/            Claude Agent SDK runners + prompts (tools shared with mcp/)
├── mcp/              MCP server for Claude Code / Desktop
├── web/              Vue 3 + TypeScript PWA
├── db/migrations/
├── docker-compose.yml
└── docs/
```

---

## 9. Roadmap

Apart from Phase 0's HomeKit pairing and a one-night test hold, nothing writes to a thermostat until
Phase 4.

| Phase | When | Deliverable | Done when |
|---|---|---|---|
| 0 | this week | ecobee sign-in, HomeKit pairing, Phase 0 tests, confirm all six SmartSensors appear on both links, order stairwell sensors | all 9 points stream live; tests answered |
| 1 | weeks 1–2 | backfill, Live/Rooms/Runtime screens, daily digest | runtime per unit matches ecobee within 1% |
| 2 | weeks 2–4 | baselines, attribution, history study, occupancy v1 (3 states) | baselines pass checks; a week of room states spot-checked |
| 3 | weeks 3–5 | Claude analyst on your subscription (read-only tools + sign-off of model changes), weekly report, MCP server | the weekly report says something new |
| 4 | weeks 5–8 | controller in Suggest mode, timed holds with read-back, Linked floors checked against the weather baseline then its offset tuned with one pre-planned switchback | upstairs maxed-out minutes near zero; total-house savings with a 90% interval |
| 5 | week 8 on | house model, pre-cooling, automatic tuning (only if they beat the rule), predicted arrivals, heating season | experiments stop finding gains larger than their uncertainty |

### Open questions

1. AC + furnace or heat pumps? Tonnage per unit?
2. Hallway and Bedroom thermostat models?
3. Are the ecobees in Apple Home, and does the family use Siri for them?
4. ecobee MFA: on, and which kind?
5. Do the upstairs ducts run through the attic?
6. Is the stairwell open, or is there a door?
7. What server runs this? Is Home Assistant already running?
8. Weather point (ZIP), and flat or time-of-use electricity rate?
9. Bedtimes, school hours, office hours.
10. Comfort bands per floor and time.
11. Which Claude plan (Pro or Max)? It sets how much room the nightly runs have.
