"""Reports (system digests, Claude's nightly/weekly reports, owner notes) and alerts."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi import status as http
from sqlalchemy import select
from sqlalchemy.orm import Session

from climate import events
from climate.api.auth import OwnerDep, ReaderDep, Role, WriterDep
from climate.api.routers.control import SessionDep, unprocessable
from climate.api.schemas import AlertOut, PublishReportBody, ReportOut
from climate.store.orm import AgentRun, Alert, Report
from climate.timeutil import utcnow

router = APIRouter(tags=["reports"])

ReportKind = Literal["daily", "weekly", "nightly", "anomaly", "note"]


def report_out(r: Report) -> ReportOut:
    return ReportOut.model_validate(r, from_attributes=True)


def alert_out(a: Alert) -> AlertOut:
    return AlertOut.model_validate(a, from_attributes=True)


@router.get("/reports", response_model=list[ReportOut])
def list_reports(
    kind: ReportKind | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
    _: Role = ReaderDep,
    session: Session = SessionDep,
) -> list[ReportOut]:
    q = select(Report).order_by(Report.created_at.desc(), Report.id.desc()).limit(limit)
    if kind is not None:
        q = q.where(Report.kind == kind)
    return [report_out(r) for r in session.execute(q).scalars()]


@router.get("/reports/{report_id}", response_model=ReportOut)
def get_report(report_id: int, _: Role = ReaderDep, session: Session = SessionDep) -> ReportOut:
    report = session.get(Report, report_id)
    if report is None:
        raise HTTPException(http.HTTP_404_NOT_FOUND, f"Unknown report {report_id}.")
    return report_out(report)


@router.post("/reports", response_model=ReportOut)
def publish_report(body: PublishReportBody, role: Role = WriterDep, session: Session = SessionDep) -> ReportOut:
    """Claude publishes its reports (author 'claude'); the owner may add notes (author 'system')."""
    violations: list[tuple[str, str]] = []
    if role == "owner" and body.kind != "note":
        violations.append(("kind", "The owner can publish notes only; reports come from the system or Claude."))
    if body.period_start and body.period_end and body.period_start > body.period_end:
        violations.append(("period_start", "period_start must be on or before period_end."))
    if body.agent_run_id is not None and session.get(AgentRun, body.agent_run_id) is None:
        violations.append(("agent_run_id", f"Unknown agent run {body.agent_run_id}."))
    if violations:
        raise unprocessable(violations)
    report = Report(
        kind=body.kind,
        author="claude" if role == "agent" else "system",
        period_start=body.period_start,
        period_end=body.period_end,
        title=body.title.strip(),
        body_md=body.body_md,
        data=body.data,
        agent_run_id=body.agent_run_id,
    )
    session.add(report)
    session.flush()
    events.publish(session, "report", report.id)
    out = report_out(report)
    session.commit()
    return out


@router.get("/alerts", response_model=list[AlertOut])
def list_alerts(
    open: bool = True,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    _: Role = ReaderDep,
    session: Session = SessionDep,
) -> list[AlertOut]:
    """open=true: unresolved alerts; open=false: all recent alerts, resolved included."""
    q = select(Alert).order_by(Alert.ts.desc(), Alert.id.desc()).limit(limit)
    if open:
        q = q.where(Alert.resolved_at.is_(None))
    return [alert_out(a) for a in session.execute(q).scalars()]


@router.post("/alerts/{alert_id}/resolve", response_model=AlertOut)
def resolve_alert(alert_id: int, _: Role = OwnerDep, session: Session = SessionDep) -> AlertOut:
    alert = session.get(Alert, alert_id)
    if alert is None:
        raise HTTPException(http.HTTP_404_NOT_FOUND, f"Unknown alert {alert_id}.")
    if alert.resolved_at is not None:
        raise HTTPException(http.HTTP_409_CONFLICT, "This alert is already resolved.")
    alert.resolved_at = utcnow()
    session.flush()
    events.publish(session, "alert", alert.id)
    out = alert_out(alert)
    session.commit()
    return out
