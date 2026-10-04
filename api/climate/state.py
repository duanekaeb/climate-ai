"""Assemble the current ``HouseState`` from the database (live tables, settings, weather,
room states, policy). Used by the controller, the status endpoint and the agent's tools."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from climate.control.policy import HouseState


def load_house_state(session: Session, now: datetime | None = None) -> HouseState:
    """Read live_units/live_sensors, compute room states with
    ``climate.occupancy.room_state.compute_room_states`` (does NOT persist them), attach
    learned room offsets (latest active model_fits kind='room_offsets'), today's forecast
    high/sunniness, the house-empty decision, settings and the active policy version
    (with the running experiment arm's params overlaid, see ``climate.experiments.switchback.active_arm``).
    Missing data never raises: units without a snapshot have snapshot=None, call='unknown'."""
    raise NotImplementedError
