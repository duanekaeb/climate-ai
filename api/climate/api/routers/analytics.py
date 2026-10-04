"""Analytics routes ("Did it work?" and "Floor coupling"): savings, the weekly waterfall,
baselines, coupling, comfort, drift and natural experiments.

Days are local calendar days of the house. Defaults: savings over the last 14 complete days;
the waterfall for the last full Monday-start week.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from climate.analytics import attribution, baseline, coupling, metrics
from climate.api.auth import ReaderDep, Role
from climate.api.routers.control import SessionDep, house_tz, safe, unprocessable
from climate.api.schemas import (
    BaselineOut,
    ComfortRow,
    Coupling,
    DriftReport,
    NaturalExperiments,
    Savings,
    Waterfall,
)
from climate.house import UNIT_KEYS
from climate.store.orm import ModelFit
from climate.timeutil import local_date, utcnow

log = logging.getLogger(__name__)
router = APIRouter(tags=["analytics"])

SAVINGS_DEFAULT_DAYS = 14
MAX_RANGE_DAYS = 400


def _today(session: Session) -> date:
    return local_date(utcnow(), house_tz(session))


@router.get("/analytics/savings", response_model=Savings)
def savings(
    start: date | None = None,
    end: date | None = None,
    _: Role = ReaderDep,
    session: Session = SessionDep,
) -> Savings:
    if end is None:
        end = _today(session) - timedelta(days=1)  # last complete day
    if start is None:
        start = end - timedelta(days=SAVINGS_DEFAULT_DAYS - 1)
    if start > end:
        raise unprocessable([("start", "start must be on or before end.")])
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise unprocessable([("start", f"The range is limited to {MAX_RANGE_DAYS} days.")])
    return attribution.savings(session, start, end)


@router.get("/analytics/waterfall", response_model=Waterfall)
def waterfall(
    week_start: date | None = None, _: Role = ReaderDep, session: Session = SessionDep
) -> Waterfall:
    if week_start is None:
        today = _today(session)
        week_start = today - timedelta(days=today.weekday() + 7)  # Monday of the last full week
    else:
        week_start = week_start - timedelta(days=week_start.weekday())  # snap to its Monday
    return attribution.waterfall(session, week_start)


@router.get("/analytics/baselines", response_model=list[BaselineOut])
def baselines(_: Role = ReaderDep, session: Session = SessionDep) -> list[BaselineOut]:
    rows = session.execute(
        select(ModelFit)
        .where(ModelFit.kind == "baseline", ModelFit.status == "active")
        .order_by(ModelFit.created_at.desc(), ModelFit.id.desc())
    ).scalars().all()
    fits = safe(session, "baseline.active_fits", lambda: baseline.active_fits(session), {})
    out: list[BaselineOut] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if row.unit_key is None or row.mode not in ("cool", "heat") or (row.unit_key, row.mode) in seen:
            continue
        seen.add((row.unit_key, row.mode))
        fit = fits.get((row.unit_key, row.mode))
        item = _from_fit(row, fit) if fit is not None else _from_row(row)
        if item is not None:
            out.append(item)
    order = {k: i for i, k in enumerate(UNIT_KEYS)}
    return sorted(out, key=lambda b: (order.get(b.unit_key, 99), b.mode))


def _from_fit(row: ModelFit, fit: baseline.BaselineFit) -> BaselineOut:
    return BaselineOut(
        unit_key=fit.unit_key,
        mode=fit.mode,
        balance_point_f=fit.balance_point_f,
        intercept_min=round(fit.intercept_s / 60.0, 2),
        slope_min_per_dd=round(fit.slope_s_per_dd / 60.0, 2),
        n_days=fit.n_days,
        r2=round(fit.r2, 4),
        cvrmse=round(fit.cvrmse, 4),
        nmbe=round(fit.nmbe, 5),
        passes=fit.passes,
        fitted_at=row.created_at,
        train_start=row.train_start or fit.train_start,
        train_end=row.train_end or fit.train_end,
    )


def _from_row(row: ModelFit) -> BaselineOut | None:
    """Read a stored fit straight from its params/metrics JSON (BaselineFit field names)."""
    merged = {**(row.metrics or {}), **(row.params or {})}

    def num(*names: str) -> float | None:
        for n in names:
            v = merged.get(n)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return float(v)
        return None

    bp, icpt, slope = num("balance_point_f"), num("intercept_s"), num("slope_s_per_dd")
    cvrmse, nmbe = num("cvrmse"), num("nmbe")
    if None in (bp, icpt, slope, cvrmse, nmbe) or row.train_start is None or row.train_end is None:
        log.warning("baseline model_fits %s lacks the fields to report; skipped", row.id)
        return None
    return BaselineOut(
        unit_key=row.unit_key or "",
        mode=row.mode,  # type: ignore[arg-type]
        balance_point_f=bp,
        intercept_min=round(icpt / 60.0, 2),
        slope_min_per_dd=round(slope / 60.0, 2),
        n_days=int(num("n_days") or 0),
        r2=round(num("r2") or 0.0, 4),
        cvrmse=round(cvrmse, 4),
        nmbe=round(nmbe, 5),
        passes=cvrmse <= 0.20 and abs(nmbe) <= 0.005,
        fitted_at=row.created_at,
        train_start=row.train_start,
        train_end=row.train_end,
    )


@router.get("/analytics/coupling", response_model=Coupling)
def coupling_route(
    days: Annotated[int, Query(ge=3, le=365)] = 30, _: Role = ReaderDep, session: Session = SessionDep
) -> Coupling:
    return coupling.coupling(session, days)


@router.get("/analytics/comfort", response_model=list[ComfortRow])
def comfort(
    days: Annotated[int, Query(ge=1, le=90)] = 7, _: Role = ReaderDep, session: Session = SessionDep
) -> list[ComfortRow]:
    return metrics.comfort(session, days)


@router.get("/analytics/drift", response_model=DriftReport)
def drift(_: Role = ReaderDep, session: Session = SessionDep) -> DriftReport:
    return metrics.drift(session)


@router.get("/analytics/natural-experiments", response_model=NaturalExperiments)
def natural_experiments(
    days: Annotated[int, Query(ge=7, le=730)] = 90, _: Role = ReaderDep, session: Session = SessionDep
) -> NaturalExperiments:
    return coupling.natural_experiments(session, days)
