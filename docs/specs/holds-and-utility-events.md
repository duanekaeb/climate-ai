# Spec: people's holds, utility events, hand back to ecobee

Plan of record for the October 2026 change set. The shared contract is already in code:
`store/app_settings.py` (ControlSettings, UtilityEventSettings, EcobeeOriginal),
`sources/base.py` (HoldInfo event fields, ThermostatEvent, UtilityInfo, `opt_out_event`),
`store/migrations/003_utility_events_and_handback.sql` + `orm.UtilityEvent`,
`control/policy.py` (PersonHold, UtilityEventState, UnitStatus, HouseState, UnitTarget),
`utility/events.py` (`event_key`, `load_active`, `change_label`), `notify.raise_alert(push_info=)`
and `api/schemas.py`. Builders implement behaviour against those types and do not change them
without saying so in their report.

## 1. A hold a person set always wins

**Person hold** = a running plain hold (`hold_type` in `PLAIN_HOLD_TYPES`) or `quickSave` that the
controller did not write. That includes the owner's hold from this app's hold form (`by='app'`) and
anything set at the wall, in the ecobee app, Apple Home or Siri (`by='thermostat'`; ecobee doesn't
say which). A comfort setting picked by hand (`holdClimateRef`, e.g. Away) is a person hold too.

- While a person hold runs, the controller writes **nothing** to that unit: no hold, no renewal, no
  resume, no sensor-set write. No time limit. A timed hold runs until it ends; "until I change it"
  (`indefinite`) runs until someone presses Resume at the thermostat / in the ecobee app, or taps
  **Back to automatic** or **Resume schedule** in this app.
- When a timed person hold **ends on its own**, the controller takes over at the next tick (no wait).
- `UnitStatus.person_hold` carries it (`since` = hold.start else first seen; `until` = hold.end, None
  for indefinite; `detection_id` = the `control_actions` row that logged it).
- Guardrails blocks with: "On your hold since 2:10 PM (until you change it); the controller waits
  until it ends or you choose Back to automatic." (`until 4:00 PM` for a timed one; "your hold from
  the app" when `by='app'`). The owner's own queued actions are not blocked by it.
- Optional reminder: `control.manual_hold_reminder_hours` > 0 → once per person hold, after that long,
  `raise_alert(kind='person_hold', level='info', push_info=True, dedupe_key=f'person_hold:{unit}:{detection_id}')`
  ("Upstairs is still on your hold", body says since when, until when, and how to hand it back).
  Resolve it when that hold is gone. Reminders never change anything.

**Ours vs a person's (bug 3).** A hold is the controller's only if it matches our latest controller
write (verified/sent, or a failed attempt that landed) on setpoints (±0.1°F) **and** its window:
`hold.start >= write.ts - 10 min`, `hold.end` within `[write.ts - 10 min, write.ts + hours + 10 min]`,
and it is not `indefinite`. A hold at our setpoints with any other window is a person hold. The
ecobee adapter's own `set_by_us` (its record of our last hold, end within 5 min) still counts.

**Resume (bugs 1, 2, 5).** `control.resume_backoff_hours` (renamed from `manual_backoff_hours`;
the old key still loads) applies **only after a Resume**, counted from when it was **first seen**,
for its full length (no longer cut short when our old hold would have ended):
- our hold vanished before its end (existing `cancelled_write` logic; log once, `RESUME_KIND`);
- a person hold vanished before its `until` (or an indefinite one vanished): log once
  (`RESUME_KIND`, request `{"kind": ..., "person_hold_detection_id": id}`), reason "Someone pressed
  Resume at the upstairs thermostat; following the ecobee schedule until 6:10 PM.";
- the owner's **Resume schedule** in this app (owner `resume_program`, request kind
  `resume_schedule`): back-off from its `completed_at` when verified.
A person hold that vanished at/after its `until` (± 5 min) **ended on its own**: no back-off, no
"resumed" log (bug 5). Snapshots from HomeKit say nothing about holds and never start or end one.
`UnitStatus.resume_backoff_until` / `resume_seen_at` carry it. A later verified **Back to automatic**
(owner `resume_program`, request kind `automatic`) cancels any back-off. A first sighting long after
the hold started (server down, just switched to Act) changes nothing: person holds have no expiry
and back-offs count from first seen (bug 2).

**Owner actions.**
- `POST /api/control/resume` → kind `resume_schedule` ("Resume schedule": cancel the running plain
  hold, any owner; the ecobee schedule runs for `resume_backoff_hours`).
- `POST /api/control/automatic` → kind `automatic` ("Back to automatic": cancel the running plain
  hold; the controller steers again at once). With no hold running it is a verified no-op that still
  clears a back-off. Rows of kind `automatic` do not count toward the 30-minute rate limit
  (`last_change_at`), so the controller's next write is not delayed by the owner's request.
- Owner holds from the hold form (bug 4) get exactly what was typed within the hard envelope only:
  min/max heat and cool, the deadband (and heatCoolMinDelta), the 0.5°F grid. **No step limit, no
  humidity guard, no rate limit, no back-off.** `guardrails.check(..., enforce_step=False,
  enforce_humidity=False)` (new keyword options, default True).
- All owner actions still fail in mode `off` and expire after 10 minutes in the queue.

**Messages (bug 5).** `_evaluate` must never say "the schedule already matches" while a person hold
or event runs; it says why it waits (person hold / resume back-off / event).

**Sensor sets (bug 6).** `_sensor_set_write` writes only when the running hold is none, ours, or
Smart Away/Home; never during a person hold, a resume back-off, vacation, a utility event (by
snapshot or by the event's clock window), or an unknown event.

**HomeKit fallback (bug 7).** While the cloud circuit is open, HomeKit shows the current comfort
setting (`VENDOR_ECOBEE_CURRENT_MODE`) but no ecobee holds. If it differs from what our last
HomeKit write set (before that write's `until`), or the HomeKit target temperatures change away
from ours, someone picked it by hand: the homekit service records a person hold for the unit
(same `MANUAL_KIND` detection row, `by='thermostat'`, `until` = None) so the controller stands
aside; it ends when the cloud returns and shows the real hold state, and never comes back in a
later outage (HomeKit snapshots carry `settings['homekit_since']`; older rows are ignored). The
service reads a unit fresh before claiming a controller row for it, fails controller rows once the
mode is no longer `act`, and runs the owner's Back to automatic / Resume schedule (queued on
channel `homekit` during the fallback) as a clear-hold. Limits: `docs/HOMEKIT.md`.

**Pre-cooled holds never run into an event.** Besides `event_prep`, the hot-day pre-cool rule
(rule 4) is capped to end before the unit's next announced utility event, and the controller
pulls back such a hold of its own (a resume, allowed inside the rate limit) when an event is
announced or moved to start before it ends.

## 2. Utility events (demand response)

ecobee reports an utility event as event type `demandResponse`, running or listed ahead of its
start (announced). Fields used: `name`, `running`, start/end (thermostat-local → UTC),
`isTemperatureAbsolute` + `coolHoldTemp`/`heatHoldTemp`, `isTemperatureRelative` +
`coolRelativeTemp`/`heatRelativeTemp` (tenths of °F), `isOptional` (False = mandatory),
`isCoolOff`/`isHeatOff`, `dutyCyclePercentage`, `linkRef`. Enrollment: `includeUtility` (Utility:
name/phone/email/web) and `settings.drAccept` (always / askMe / customerSelect / defaultAccept /
defaultDecline / never). `resumeProgram` "removes the currently running event providing the event
is not a mandatory demand response event" (ecobee API docs): that is the documented, recorded
opt-out we use. Never a counter-offset, never a hidden one: while an event runs the controller
stands down; the only ways out are opt-outs ecobee records.

**Source (ecobee adapter, simulator).**
- `HoldInfo` for the top running event carries `event_name`, `is_relative`, `heat_offset_f` /
  `cool_offset_f`, `is_optional`, `link_ref`; absolute setpoints only when absolute.
- `running_override` takes the first running event of **any** type (event list order is ecobee's
  priority); an unknown type arrives verbatim in `hold_type` (never `set_by_us`).
- `UnitSnapshot.events`: every `demandResponse` and `vacation` event, running or future (not
  templates or past ones). `UnitSnapshot.utility` from `includeUtility` (None when the name is
  empty). `settings['drAccept']` in the curated settings. `ecobee_thermostats.settings` also stores
  `utility` (dict or null) and `drAccept` for Setup.
- `opt_out_event(unit_key, reason, *, link_ref, name, start)`: fresh GET; refuse unless the top
  running event is `demandResponse`; refuse (`other_event`) when it is a different event than the
  one being skipped (linkRef, else name + start); refuse (request `refused=True`, error says
  mandatory) when `isOptional` is False; post `resumeProgram` `resumeAll=false`; read back that
  THIS event no longer runs (twice, retrying a failed read). Never touches a plain hold
  underneath. `resume_program` refuses events, except that the owner's forced resume (Back to
  automatic / Resume schedule) also clears a Quick Save or Smart Away/Home.
- Controller writes never replace what a person did since the last poll: `set_hold` (with
  `HoldRequest.by_owner` false) and `update_sensor_sets` decide on a fresh read and post nothing
  over a person's hold or Quick Save (`refused`, `not_ours`; the controller logs it as that
  person's hold) or over vacation / utility / unknown events (`refused`, `event`). The owner's
  holds skip the person check.
- `apply_settings(unit_key, settings, reason)`: `updateThermostat` with only `autoAway` and/or
  `followMeComfort` for that one thermostat, read back. For hand-back.
- Simulator: events too. `Simulator.inject_event(unit_key, ThermostatEvent)` and CLI
  `climate sim-event --unit up --in-min 30 --hours 2 [--cool-offset 2 | --cool 78] [--name ...]
  [--mandatory]`; the event becomes running at its start (offsets apply to the schedule setpoint),
  ends at its end; `opt_out_event` and `resume_program` behave like ecobee; `apply_settings` works.

**Ingest (`climate.utility.events`).**
- `ingest(session, snapshots, now)`: for each ecobee/simulator snapshot (skip `source=='homekit'`),
  upsert `utility_events` per `(unit_key, event_key(ev))` for its `demandResponse` events:
  running → `running` (`started_at` = first seen running); not running and start ahead →
  `announced`. Rows of that unit still `announced`/`running` that the snapshot no longer lists:
  `running` → `opted_out` if `skip == 'done'` else `ended` (`ended_at` = snapshot ts);
  `announced` → `cancelled` if its start is still ahead, else `ended`. Also raises/resolves the
  unknown-event alert: `unknown_event:{type}` (warn) while any unit's running top event has a
  `hold_type` outside `KNOWN_HOLD_TYPES`; body says the app is hands-off on that unit.
- `sweep(session, now)` (every worker loop): `running` with `end_at < now - 10 min` → `ended`;
  `announced` with `end_at < now` → `ended`; `announced` with `start_at <= now` stays announced
  until a snapshot or the end decides. Resolve stale event alerts.
- Alerts when `utility_events.alerts` (grouped per `event_key`, unit names joined, house time):
  announced (info, push) "Utility event announced: <name>" body "Upstairs and main floor, Tue 3:00–
  6:00 PM, cooling +2°F. The app stands aside during it; you can skip it from Live." (dedupe
  `utility_event:{event_key}:announced`, resolved when it starts or is cancelled); started (info,
  push, `...:running`, resolved at end); ended (info, push, then resolved by the sweep after 12 h);
  cancelled (info). Skip outcomes below.

**Skips (`climate.utility.skips`).** `skip`: requested → done | failed | refused.
- Owner: `POST /api/utility-events/{id}/skip` (`SkipEventBody.all_units`, default every row with
  the same `event_key`) → `skip='requested'`, `skip_by='owner'`, `skip_reason='Skipped from the app'`.
  409 when the controller is off, the event is mandatory, over, or already skipped. `POST
  /api/utility-events/{id}/unskip` clears a still-`requested` skip and its attempt count (409
  once the opt-out is being sent: ecobee would record it anyway).
- Rules (`utility_events.auto_skip`), evaluated from the house state for events running or
  starting within 5 minutes: `skip_when_asleep` (a room on that unit is `asleep`), `skip_above_f`
  (an occupied/asleep room's temperature ≥ it while cooling), `skip_below_f` (≤ it while heating).
  Mode `act` and the unit in `act_units` → `skip='requested'`, `skip_by='rule'`, `skip_reason` =
  the sentence ("Girls' Room reached 79.5°F"). Mode `suggest` → only an alert (info, push) "Your
  skip rule matched … In Suggest mode the app doesn't skip on its own; tap Skip on Live." Once per
  event and unit.
- `run(source, now)` (every worker loop, after the controller tick): for `requested` rows whose unit
  snapshot shows a running `demandResponse` → log a `control_actions` row (action `opt_out_event`,
  actor `owner` or `controller` for rules, mode `act`, channel = source kind, rule
  `utility_opt_out`, request `{"kind": "utility_opt_out", "event_id": id, "by": ...}`, status
  `sent` before the call) → `opt_out_event` → `verified`/`failed` with read-back. Done →
  `skip='done'`, `skip_done_at`, `skip_action_id`, `status='opted_out'`, `ended_at`; alert (info,
  push) "Skipped the utility event on Upstairs — ecobee recorded the opt-out; normal temperatures
  are back." Refused (mandatory) → `skip='refused'` + warn alert. Failed → retry on the next loops,
  at most 3 attempts (count in `detail.skip_attempts`), then `skip='failed'` + error alert. Mode
  `off` → requested skips wait (and say so in the alert once). Cloud circuit open (HomeKit can't
  see or cancel events) → wait, warn once.

**Controller during events.** Guardrails blocks a unit while (a) its snapshot's top event is
`vacation`/`demandResponse` (as today) or any type outside `KNOWN_HOLD_TYPES` ("An unrecognised
ecobee event (today) is running on the … thermostat; the app is hands-off until it ends."), or
(b) any `HouseState.utility_events` row for the unit `covers(now)` (the clock window, in case the
snapshot lags). Owner holds are blocked during (a)/(b) too, with "Skip the event first" in the
message — an owner hold over a running event would be an opt-out ecobee may not record as one.

**Pre-cool / pre-heat (`utility_events.precondition`).** In `policy.plan`, after the normal rules,
for a unit with an `announced` event `e` (no skip requested/done) and `start_at` set:
`end_by = e.start_at - precondition_end_gap_min`; window `[end_by - precondition_hours h - 15 min,
end_by)`. Inside it the target becomes rule `event_prep`, `desired='hold'`, `hold_end_by=end_by`,
cooling: `cool_f -= precondition_degrees_f`; heating: `heat_f += precondition_degrees_f`. Direction:
the event's own (cool offset > 0 / cool setpoint / AC off → cooling; heat offset < 0 / heat
setpoint / heat off → heating), else the unit's `hvac_mode` (cool → cooling; heat/auxHeatOnly →
heating; auto → cooling when outdoor ≥ 65°F). Reason: "Pre-cooling 2°F before the utility event at
3:00 PM; this hold ends by 2:50 PM, before the event starts."
The controller writes an `event_prep` hold only with `hours` = the largest of {hold_hours, 1} such
that `now + hours <= hold_end_by`; when none fits it writes nothing ("Too close to the utility
event for another pre-cooling hold; it ends on its own before the event."). Renewals obey the same
fit. The hold lapses on its own; it is never relied on to be resumed. Guardrails (step, rate,
humidity, limits) apply as usual. HomeKit fallback: queue only if `until <= hold_end_by`.

**Analytics.** `climate.analytics.exclusions.event_days(session, start, end, tz) -> dict[date, str]`:
local days overlapped by a utility event that ran (`started_at` set, or status running / ended /
opted_out) on any unit, or by a verified/sent `event_prep` hold → "utility event" / "pre-cooling
before a utility event". Excluded from baseline fitting, savings (both periods), experiments
(`experiment_days.included = false`, note), natural experiments and the drift/attribution reports
wherever days are chosen. Reports say how many days were excluded and why.

## 3. Hand back to ecobee

- Capture: `app_settings['ecobee_original']` = `{unit_key: EcobeeOriginal}`. The controller captures
  a unit the first time it sees an ecobee snapshot for it (autoAway, followMeComfort, Home sensor
  keys), and `keep_settings` / `_sensor_set_write` capture any missing unit from the snapshot
  BEFORE their first write. Never overwritten once captured.
- `GET /api/control/handback` → `HandbackInfo`. `POST /api/control/handback` (owner) → queues a
  `jobs` row `kind='handback'` (409 while one is queued/running) → worker runs
  `controller.hand_back(source, now)`:
  1. `control.mode = 'off'` first (updated_by owner), publish status. The worker's
     `keep_settings` does nothing in `off`, so it will not switch Smart Away off again. The job
     holds the worker's control lock (shared with the tick, queued owner actions and the settings
     check), and the tick re-reads the mode before each write, so nothing the controller decided
     earlier lands afterwards. Then the thermostats are read fresh (falling back to the last poll,
     and saying so) before steps 2-4.
  2. Each unit whose running hold is ours: `resume_program` (not forced) → logged, actor `owner`,
     rule `handback`.
  3. Home sensor set back to the original (when captured and different) via `update_sensor_sets`.
  4. Smart Away / Follow Me back to the original values via `apply_settings` (only those that
     differ).
  5. Person holds, vacations and utility events are left alone. Every write is logged to
     `control_actions` (rule `handback`) with before/after/read-back. Job result
     `{"steps": [HandbackStep...]}`; info alert (push) "Handed back to ecobee" listing anything
     that failed.
  Simulator and HomeKit-only: steps that can't run are reported as skipped, not failed silently.

## 4. API and web

- `UnitLive`: `hold_owner`, `hold_label`, `person_hold`, `resume_backoff_until`, `utility_event`
  (running, else next announced), `upcoming_events` (vacations/events ahead from the snapshot).
  Labels in house time: "On your hold since 2:10 PM, until 4:00 PM" / "On your hold since 2:10 PM
  (until you change it)" / "Your hold from the app until 4:00 PM" / "Utility event until 6:00 PM
  (cooling +2°F)" / "Vacation until Oct 12, 9:00 AM" / "Smart Away" / "Quick Save" / "Our hold until
  3:40 PM" / "Unrecognised ecobee event: today".
- `GET /api/utility-events?days=30`, skip/unskip as above; `UtilityEventOut.prep_label` is the
  plan's pre-cooling sentence only while the controller is doing it (a conditional "Nothing is
  written." sentence in Suggest mode). `PUT /api/control/settings` never changes the mode.
- `SettingsOut/SettingsUpdate.utility_events`; Setup's `EcobeeThermostatOut.utility`, `dr_accept`,
  `enrolled`.
- Web: Live unit cards show the label and, on a person hold, **Back to automatic** and **Resume
  schedule** with one line on the difference; during a resume back-off, "Following the ecobee
  schedule until 6:10 PM" + Back to automatic. A utility-event card (announced/running/skipped;
  change, window, thermostats; Skip with a confirm that says it is an opt-out the utility sees and
  may cost that event's credit; mandatory events say they can't be skipped; pending skip + undo;
  prep label). Guardrails: "Wait after Resume", hold reminder, a Utility events section (alerts,
  skip rules, pre-cooling with the "ends before the event" rule spelled out). Setup: enrollment per
  thermostat and the Hand back to ecobee card (what it restores, confirm, last result). Action log
  and plan know `event_prep`, `utility_opt_out`, `handback`, `opt_out_event`.

## 5. Ownership (parallel builders)

| Builder | Files |
|---|---|
| sources | `sources/ecobee_parse.py`, `sources/ecobee.py`, `sources/simulator.py`, `cli.py` (sim-event) + their tests |
| homekit | `sources/homekit.py`, `collector/homekit_service.py`, `docs/HOMEKIT.md` + tests |
| control | `state.py`, `control/policy.py` (plan only), `control/guardrails.py`, `control/controller.py`, new `control/handback.py` + tests |
| utility | `utility/events.py` (ingest/sweep), new `utility/skips.py`, `analytics/*` (+ new `exclusions.py`), `experiments/*`, `collector/ingest.py`/`poller.py` wiring, `worker.py` (loop + `handback` job) + tests |
| api | `api/routers/*`, new `api/labels.py`, `api/schemas.py` (additions only), `web/src/api/schema.json` + `types.ts` regen + tests |
| web | `web/src/**` except the generated types |

Interfaces between builders are the contract files above plus: `controller.hand_back(source, now)
-> list[HandbackStep-shaped dicts]` (control) called by the worker job (utility);
`climate.utility.skips.run(source, now) -> list[int]` and `climate.utility.events.ingest/sweep`
(utility) called by the worker; `source.opt_out_event` / `apply_settings` (sources).

Tests: `cd api && /tmp/claude-0/-home-user/289a7eda-1f53-572c-8dd1-961fc133a4d7/scratchpad/venv/bin/python -m pytest -q`
(each session makes its own database on the local Postgres). Web: `cd web && npx vue-tsc --noEmit`.
