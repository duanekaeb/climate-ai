"""Experiments (randomized switchbacks), model fits, refit/backtest/simulate and jobs.

Experiments are proposed by the owner or Claude and approved only by the owner. Refits are
queued as ``jobs`` rows for the worker; backtests and simulations run synchronously and are
bounded by the request bodies' limits.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from fastapi import status as http
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from climate import events
from climate.api.auth import OwnerDep, ReaderDep, Role, WriterDep
from climate.api.routers.control import SessionDep, active_policy, actor_for, unprocessable
from climate.api.schemas import (
    ArmIn,
    BacktestBody,
    BacktestOut,
    Checkpoint,
    ExperimentAnalysis,
    ExperimentDayOut,
    ExperimentDecisionBody,
    ExperimentDetail,
    ExperimentOut,
    JobOut,
    ModelFitOut,
    PowerOut,
    ProposeExperimentBody,
    SimulateBody,
    SimulateOut,
)
from climate.control.policy import PolicyParams
from climate.experiments import analysis, switchback
from climate.models import backtest
from climate.store.orm import Experiment, ExperimentDay, Job, ModelFit

router = APIRouter(tags=["experiments"])


def _min(seconds: float | None) -> float | None:
    return None if seconds is None else round(seconds / 60.0, 1)


def experiment_out(e: Experiment) -> ExperimentOut:
    design: dict[str, Any] = e.design or {}
    return ExperimentOut(
        id=e.id,
        created_at=e.created_at,
        name=e.name,
        hypothesis=e.hypothesis,
        metric=e.metric,
        arms=[ArmIn.model_validate(a) for a in (e.arms or [])],
        n_days=int(design.get("n_days") or 0),
        block_days=int(design.get("block_days") or 1),
        checkpoints=[Checkpoint.model_validate(c) for c in design.get("checkpoints") or []],
        alpha=float(design.get("alpha") or 0.10),
        status=e.status,
        proposed_by=e.proposed_by,
        start_date=e.start_date,
        end_date=e.end_date,
    )


def _require_experiment(session: Session, experiment_id: int) -> Experiment:
    exp = session.get(Experiment, experiment_id)
    if exp is None:
        raise HTTPException(http.HTTP_404_NOT_FOUND, f"Unknown experiment {experiment_id}.")
    return exp


def _check_params(session: Session, params: dict[str, Any]) -> None:
    """A partial PolicyParams must name known keys and give in-range values over the active ones."""
    unknown = sorted(set(params) - set(PolicyParams.model_fields))
    if unknown:
        raise unprocessable([("params", f"Unknown policy parameters: {', '.join(unknown)}.")])
    current, _ = active_policy(session)
    try:
        PolicyParams.model_validate({**current.model_dump(), **params})
    except ValidationError as exc:
        raise unprocessable(
            [("params." + ".".join(str(p) for p in err["loc"]), err["msg"]) for err in exc.errors()]
        ) from exc


# ---------------------------------------------------------------------------------------
# experiments (power BEFORE {experiment_id}, or "power" would hit the id route)
# ---------------------------------------------------------------------------------------


@router.get("/experiments", response_model=list[ExperimentOut])
def list_experiments(_: Role = ReaderDep, session: Session = SessionDep) -> list[ExperimentOut]:
    rows = session.execute(select(Experiment).order_by(Experiment.created_at.desc(), Experiment.id.desc()))
    return [experiment_out(e) for e in rows.scalars()]


@router.get("/experiments/power", response_model=PowerOut)
def power(
    effect_pct: Annotated[float, Query(gt=0, le=100)] = 10.0,
    alpha: Annotated[float, Query(gt=0, lt=0.5)] = 0.10,
    power: Annotated[float, Query(ge=0.5, lt=1.0)] = 0.8,
    _: Role = ReaderDep,
    session: Session = SessionDep,
) -> PowerOut:
    return analysis.power(session, effect_pct, alpha, power)


@router.get("/experiments/{experiment_id}", response_model=ExperimentDetail)
def experiment_detail(
    experiment_id: int, _: Role = ReaderDep, session: Session = SessionDep
) -> ExperimentDetail:
    exp = _require_experiment(session, experiment_id)
    days = session.execute(
        select(ExperimentDay).where(ExperimentDay.experiment_id == experiment_id).order_by(ExperimentDay.day)
    ).scalars()
    schedule = [
        ExperimentDayOut(
            day=d.day, arm=d.arm, actual_min=_min(d.actual_s), expected_min=_min(d.expected_s),
            residual_min=_min(d.residual_s), included=d.included,
        )
        for d in days
    ]
    result: ExperimentAnalysis = analysis.analyze(session, exp)
    return ExperimentDetail(experiment=experiment_out(exp), schedule=schedule, analysis=result)


@router.post("/experiments", response_model=ExperimentOut)
def propose_experiment(
    body: ProposeExperimentBody, role: Role = WriterDep, session: Session = SessionDep
) -> ExperimentOut:
    keys = [a.key for a in body.arms]
    if len(set(keys)) != len(keys):
        raise unprocessable([("arms", "Arm keys must be unique.")])
    for i, arm in enumerate(body.arms):
        unknown = sorted(set(arm.params) - set(PolicyParams.model_fields))
        if unknown:
            raise unprocessable([(f"arms.{i}.params", f"Unknown policy parameters: {', '.join(unknown)}.")])
    try:
        exp = switchback.create_experiment(session, body, actor_for(role))
    except ValueError as exc:
        raise HTTPException(http.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    session.flush()
    events.publish(session, "change")
    out = experiment_out(exp)
    session.commit()
    return out


@router.post("/experiments/{experiment_id}/decision", response_model=ExperimentOut)
def decide_experiment(
    experiment_id: int, body: ExperimentDecisionBody, _: Role = OwnerDep, session: Session = SessionDep
) -> ExperimentOut:
    _require_experiment(session, experiment_id)
    try:
        exp = switchback.decide_experiment(session, experiment_id, body.decision, body.reason)
    except ValueError as exc:
        raise HTTPException(http.HTTP_409_CONFLICT, str(exc) or "The experiment is not in a state for that.") from exc
    session.flush()
    events.publish(session, "change")
    events.publish(session, "status")
    out = experiment_out(exp)
    session.commit()
    return out


# ---------------------------------------------------------------------------------------
# models / jobs
# ---------------------------------------------------------------------------------------


@router.get("/models", response_model=list[ModelFitOut])
def list_models(_: Role = ReaderDep, session: Session = SessionDep) -> list[ModelFitOut]:
    """The latest fit per (kind, unit, mode)."""
    q = (
        select(ModelFit)
        .distinct(ModelFit.kind, ModelFit.unit_key, ModelFit.mode)
        .order_by(ModelFit.kind, ModelFit.unit_key, ModelFit.mode, ModelFit.created_at.desc(), ModelFit.id.desc())
    )
    return [ModelFitOut.model_validate(f, from_attributes=True) for f in session.execute(q).scalars()]


def job_out(job: Job) -> JobOut:
    return JobOut.model_validate(job, from_attributes=True)


@router.post("/models/refit", response_model=JobOut)
def refit(role: Role = WriterDep, session: Session = SessionDep) -> JobOut:
    pending = session.execute(
        select(Job).where(Job.kind == "refit", Job.status.in_(("queued", "running"))).order_by(Job.id.desc()).limit(1)
    ).scalar_one_or_none()
    if pending is not None:
        return job_out(pending)  # one refit at a time; the queued one covers this request
    job = Job(kind="refit", status="queued", requested_by=actor_for(role), params={})
    session.add(job)
    session.flush()
    out = job_out(job)
    session.commit()
    return out


@router.post("/models/backtest", response_model=BacktestOut)
def run_backtest(body: BacktestBody, _: Role = WriterDep, session: Session = SessionDep) -> BacktestOut:
    _check_params(session, body.params)
    try:
        return backtest.backtest(session, body.params, body.days)
    except ValueError as exc:
        raise HTTPException(http.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


@router.post("/models/simulate", response_model=SimulateOut)
def run_simulate(body: SimulateBody, _: Role = WriterDep, session: Session = SessionDep) -> SimulateOut:
    _check_params(session, body.params)
    try:
        return backtest.simulate(session, body.params, body.date)
    except ValueError as exc:
        raise HTTPException(http.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: int, _: Role = ReaderDep, session: Session = SessionDep) -> JobOut:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(http.HTTP_404_NOT_FOUND, f"Unknown job {job_id}.")
    return job_out(job)
