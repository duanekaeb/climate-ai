"""Grey-box 3-zone RC house model (blueprint §4) and per-room offsets.

Runs in shadow: its plans drive nothing until it beats the linked-floors rule in
walk-forward backtests. Separate cooling and heating parameter sets.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session


def fit_rc(session: Session, now: datetime, days: int = 28) -> list[int]:
    """Fit C, R_out, R_mu, R_mb, k_s, a_sun, Q per zone (main, up, bed) by least squares on
    15-minute data (zone temps = thermostat averaged temps, on fraction = stage-1 runtime / slot,
    T_out and shortwave from weather). Walk-forward: train on all but the last 7 days, score
    1 h and 24 h ahead RMSE on the held-out week. Report parameters the data cannot pin down
    (relative CI half-width > 50%). Store model_fits kind='rc' as 'candidate' (status
    'active' only if 1 h RMSE <= 1.0°F and it beats persistence). Returns ids."""
    raise NotImplementedError


def fit_room_offsets(session: Session, now: datetime, days: int = 14) -> int | None:
    """Per sensored room: median (room temp - its unit's zone temp) by period (day/night)
    and mode. Store model_fits kind='room_offsets' active. Returns id."""
    raise NotImplementedError
