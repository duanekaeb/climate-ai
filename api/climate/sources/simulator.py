"""A simulated house so the whole app runs without credentials (dev, demo, tests).

Three coupled zones (main, up, bed) using the same RC structure as the blueprint's house
model, eleven rooms with fixed offsets/noise, occupancy schedules (school days, evenings,
sleep), synthetic or real weather, and ecobee-like thermostats (setpoints, Smart Away that
floats the main floor to ~80°F when empty -> reproduces the "upstairs maxed out" problem,
holdHours holds, equipment runtime per 5-minute slot, stage-1 only).
Deterministic for a given seed.
"""

from __future__ import annotations

from datetime import datetime

from climate.sources.base import (
    HoldRequest,
    RuntimeInterval,
    SourceHealth,
    UnitSnapshot,
    WriteResult,
)


class SimulatedHouse:
    kind = "simulator"

    @classmethod
    def from_settings(cls) -> SimulatedHouse:
        raise NotImplementedError

    def advance_to(self, now: datetime) -> None:
        """Step the physics in 1-minute steps up to ``now`` (bounded catch-up)."""
        raise NotImplementedError

    def generate_history(self, start: datetime, end: datetime) -> tuple[list[RuntimeInterval], list[UnitSnapshot]]:
        """Fast synthetic history for backfill (5-minute slots; snapshots hourly)."""
        raise NotImplementedError

    async def poll_revisions(self) -> dict[str, str]:
        raise NotImplementedError

    async def fetch_snapshots(self, unit_keys: list[str] | None = None) -> list[UnitSnapshot]:
        raise NotImplementedError

    async def fetch_runtime(self, start: datetime, end: datetime) -> list[RuntimeInterval]:
        raise NotImplementedError

    async def set_hold(self, req: HoldRequest) -> WriteResult:
        raise NotImplementedError

    async def resume_program(self, unit_key: str, reason: str) -> WriteResult:
        raise NotImplementedError

    async def health(self) -> SourceHealth:
        raise NotImplementedError

    async def close(self) -> None:
        """Persist state to app_settings['simulator_state']."""
        raise NotImplementedError
