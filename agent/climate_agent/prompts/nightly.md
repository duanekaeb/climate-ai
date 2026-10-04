# Nightly run

Goal: verify yesterday, clear the sign-off queue, flag drift, and publish a short nightly
report. Aim for about 10-15 tool calls.

1. **State of the house.** `get_house_status`. Note disconnected units, stale sensors, open
   alerts, people's holds and resume back-offs (the controller stands aside for them), and
   utility events (announced, running or skipped).
2. **Yesterday's decisions vs plan.** `list_actions` (about 50): did writes read back
   correctly, were any blocked by guardrails, did anything happen that the rule does not
   explain? Use `explain_action` only for the odd ones. `query_runtime` (days 3) for
   yesterday's runtime against the weather-expected minutes and maxed-out minutes (especially
   upstairs).
3. **Comfort.** `comfort_report` (days 1-2). Any occupied or sleeping room under 97% in band
   is the first thing in the report, with the room, minutes and worst excursion. Look at that
   room with `query_room` only if the cause is unclear.
4. **Sign-off queue.** `review_pending_changes`. For each change: approve or hold per the
   sign-off rules, with a reason that cites the gate numbers. When in doubt, hold. You cannot
   reject: if the evidence shows harm, hold it with that evidence in the reason for the owner.
5. **Drift.** `drift_report`. If a unit is drifting, say so and suggest what to check (sensor,
   equipment, a schedule edit, weather the baseline has not seen). Request a refit
   (`request_refit`) only if drift is clear and no refit ran in the last day.
6. **Publish** one `publish_report` with kind `nightly`, period = yesterday:
   - Headline (one line): comfort OK or not, anything needing the owner.
   - Yesterday: runtime per unit vs expected (stage-1 minutes), maxed minutes, notable actions.
   - Comfort: rooms below target, or "all occupied rooms in band".
   - Decisions: what you approved or held, and why (one line each); flag any hold that
     you think the owner should reject.
   - Watch list: drift, alerts, data gaps.
   Do not claim savings from one day; mention the latest `savings_report` interval only if
   you fetched it. Cite Open-Meteo if you discuss weather.

Do not propose new policy changes or experiments in a nightly run; note ideas for the weekly.
