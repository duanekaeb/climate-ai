"""Floor coupling: how much the main floor's temperature drives upstairs runtime."""

from __future__ import annotations

from sqlalchemy.orm import Session

from climate.api.schemas import Coupling, NaturalExperiments


def coupling(session: Session, days: int = 30) -> Coupling:
    """Hourly regression on cooling hours: up_runtime_min ~ a + b*(T_main - T_up)+ +
    c*(T_out - 65)+ + d*shortwave + hour-of-day dummies. Report b with a 90% interval
    (HAC/Newey-West SE, lag 3). Placebo: same model for the bed wing using (T_main - T_bed)+;
    should be ~0. Downsample points to <= 2000 for the scatter."""
    raise NotImplementedError


def natural_experiments(session: Session, days: int = 90) -> NaturalExperiments:
    """Past afternoons where the main floor floated warm (>= 2°F above its occupied cool
    setpoint for >= 2 h between 12:00 and 18:00 local) vs similar-weather afternoons without:
    difference in upstairs residual runtime (actual - baseline). Placebo with fake event days,
    and the bed wing as a negative control."""
    raise NotImplementedError
