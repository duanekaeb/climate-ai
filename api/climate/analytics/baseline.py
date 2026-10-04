"""Weather baselines per unit and mode (CalTRACK-style).

runtime_s/day = intercept + slope * DD(bp), bp searched over 30-90°F in 1°F steps (only bps
leaving >= 10 days with DD > 0), least squares, keep the best adjusted R². Checks:
CV(RMSE) <= 20% and |NMBE| <= 0.5% (ASHRAE Guideline 14 daily). Needs >= 21 days.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy.orm import Session

from climate.analytics.daily import DayRow


@dataclass
class BaselineFit:
    unit_key: str
    mode: str
    balance_point_f: float
    intercept_s: float
    slope_s_per_dd: float
    n_days: int
    r2: float
    cvrmse: float
    nmbe: float
    resid_std_s: float
    resid_lag1: float  # autocorrelation of residuals, for the savings interval
    train_start: date
    train_end: date

    @property
    def passes(self) -> bool:
        return self.cvrmse <= 0.20 and abs(self.nmbe) <= 0.005


def fit_baseline(days: list[DayRow], mode: str) -> BaselineFit | None:
    raise NotImplementedError


def expected_seconds(fit: BaselineFit, day: DayRow) -> float:
    raise NotImplementedError


def refit_all(session: Session, now: datetime, train_days: int = 90) -> list[int]:
    """Fit every unit x mode with enough data on the last train_days (excluding days used by a
    running experiment's treatment arm), store model_fits (kind='baseline') as 'active',
    retiring the previous active fit. Returns new ids."""
    raise NotImplementedError


def active_fits(session: Session) -> dict[tuple[str, str], BaselineFit]:
    """(unit_key, mode) -> the active baseline fit."""
    raise NotImplementedError
