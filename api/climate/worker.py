"""The worker process: ``python -m climate.worker``.

Owns the ThermostatSource (simulator or ecobee) and runs, with independent cadences:
poll (summary/detail), runtime pulls, weather sync (hourly), controller tick (every 3 min),
queued owner actions (every 10 s), change gates (every 15 min), nightly jobs (baselines,
room offsets, RC fit, experiment days, daily report, agent schedule), the jobs table (refit /
backtest / backfill requests), anomaly triggers for Claude (drift, sensor offline, upstairs
maxed out), and a heartbeat. Switching app_settings['source'] swaps the source live.
"""
