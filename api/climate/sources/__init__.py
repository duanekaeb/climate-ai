"""Data sources. ``get_source(kind)`` builds the worker's ThermostatSource."""

from __future__ import annotations

from climate.sources.base import ThermostatSource


def get_source(kind: str) -> ThermostatSource:
    """'simulator' -> climate.sources.simulator.SimulatedHouse (state restored from
    app_settings['simulator_state']); 'ecobee' -> climate.sources.ecobee.EcobeeCloud
    (refresh token from secrets). Imports lazily."""
    if kind == "simulator":
        from climate.sources.simulator import SimulatedHouse

        return SimulatedHouse.from_settings()
    if kind == "ecobee":
        from climate.sources.ecobee import EcobeeCloud

        return EcobeeCloud.from_settings()
    raise ValueError(f"unknown source {kind!r}")
