"""The cloud/simulator poller loop body (the worker schedules it).

ecobee: /thermostatSummary every poll_seconds (>= 180); /thermostat detail only for units
whose revision changed; runtimeReport hourly for the last 2 h and nightly for the last 2 days
(one request at a time). Simulator: advance, snapshot, runtime every sim_poll_seconds.
"""

from __future__ import annotations

from datetime import datetime

from climate.sources.base import ThermostatSource


async def poll_once(source: ThermostatSource, now: datetime, last_revisions: dict[str, str]) -> dict[str, str]:
    """One summary/detail pass. Returns the new revisions map. Updates heartbeat 'worker'
    detail.source_*; raises nothing on transient errors (records an alert after 3 failures)."""
    raise NotImplementedError


async def pull_runtime(source: ThermostatSource, now: datetime, hours_back: int = 2) -> int:
    raise NotImplementedError
