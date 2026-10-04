# Weekly run

Goal: an honest weekly account of whether the strategy is saving runtime, what the house is
teaching us, and at most one next step. Take the time to be right; you still have 30 turns.

1. **Results with intervals.** `savings_report` (last 14 days and, if useful, since the last
   policy change) and `waterfall_report` (last full week). State savings only with the 90%
   interval; if the baseline fails or the interval includes zero, say there is no detectable
   effect yet and why (too few days, weather unlike the training period, a failing baseline).
2. **Baselines and drift.** `baseline_report` and `drift_report`. A failing baseline blocks
   any claim for that unit.
3. **Coupling.** `coupling_report` (30 days) and, every few weeks, `natural_experiment_report`.
   Trust the main-to-upstairs coefficient only if the bed-wing placebo and the fake-event
   placebo are near zero. Say what that means for the linked-floors offset.
4. **Comfort.** `comfort_report` (days 7): any room under 97% comes first.
5. **Experiments.** `list_experiments`; for a running one, `experiment_detail`. Judge it only
   at a checkpoint it has reached, using its pre-planned interval. Between checkpoints report
   progress (days per arm), not a verdict.
6. **Models.** `model_fits`: does the RC model beat the linked-floors rule in walk-forward
   backtests yet? Name parameters the data cannot pin down. Do not promote complexity that has
   not earned it.
7. **One next step, at most.** Either one `propose_policy_change` or one `propose_experiment`,
   or none. Before proposing: `run_backtest` (it must beat the model's uncertainty) and, for an
   experiment, `estimate_power` to size it (a 5% effect needs far more days than 10-15%; send
   small refinements to the simulator, not the house). The rationale cites the numbers.
8. **Sign-off queue.** `review_pending_changes`, same rules as nightly.
9. **Publish** one `publish_report` with kind `weekly`, period = the week:
   - Headline: savings with 90% interval, or "no detectable effect yet" and why.
   - Waterfall: last week -> weather -> strategy & other -> this week (minutes).
   - Coupling and what it means for the offset.
   - Comfort, experiments (progress or checkpoint verdict), model status.
   - Decisions this week and the one proposal (if any), with its backtest.
   - "Weather data by Open-Meteo.com" if weather is discussed.
   Put the key numbers in `data` (savings_pct, ci90, minutes by unit) for charts.
