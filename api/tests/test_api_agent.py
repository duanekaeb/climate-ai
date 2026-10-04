"""Agent routes: owner ask/run, the agent's claim/finish/heartbeat (agent_queue and notify are
monkeypatched), and the AgentInfo fields (token warning, next nightly, triggered today)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from climate import agent_queue, notify
from climate.api.routers.agent import next_local_time, token_warning
from climate.store.db import session_scope
from climate.store.orm import AgentRun
from climate.timeutil import local_date, utcnow


def _run(kind="nightly", status="queued", **kw) -> int:
    with session_scope() as s:
        r = AgentRun(kind=kind, status=status, requested_by=kw.pop("requested_by", "schedule"), **kw)
        s.add(r)
        s.flush()
        return r.id


@pytest.fixture
def fake_queue(monkeypatch):
    calls: list[tuple] = []

    def enqueue(session, kind, requested_by, prompt="", trigger=None):
        calls.append(("enqueue", kind, requested_by, prompt))
        r = AgentRun(kind=kind, status="queued", requested_by=requested_by, prompt=prompt, trigger=trigger)
        session.add(r)
        session.flush()
        return r

    def claim_next(session, now):
        calls.append(("claim",))
        r = session.execute(
            select(AgentRun).where(AgentRun.status == "queued").order_by(AgentRun.id).limit(1)
        ).scalar_one_or_none()
        if r is not None:
            r.status, r.started_at = "running", now
        return r

    def finish(session, run_id, **fields):
        calls.append(("finish", run_id, fields))
        r = session.get(AgentRun, run_id)
        for k, v in fields.items():
            setattr(r, k, v)
        r.finished_at = utcnow()
        return r

    monkeypatch.setattr(agent_queue, "enqueue", enqueue)
    monkeypatch.setattr(agent_queue, "claim_next", claim_next)
    monkeypatch.setattr(agent_queue, "finish", finish)
    return calls


@pytest.fixture
def alerts(monkeypatch):
    raised: list[tuple] = []
    monkeypatch.setattr(notify, "raise_alert", lambda session, kind, level, title, body="", dedupe_key=None:
                        raised.append(("raise", kind, level, dedupe_key)))
    monkeypatch.setattr(notify, "resolve_alert", lambda session, key: raised.append(("resolve", key)))
    return raised


def test_ask_and_run_now(owner, fake_queue):
    r = owner.post("/api/agent/ask", json={"question": "  Why did the upstairs run all afternoon?  "})
    assert r.status_code == 200, r.text
    assert (r.json()["kind"], r.json()["status"], r.json()["requested_by"]) == ("chat", "queued", "owner")
    assert fake_queue[0] == ("enqueue", "chat", "owner", "Why did the upstairs run all afternoon?")

    first = owner.post("/api/agent/run", json={"kind": "weekly"})
    assert first.status_code == 200 and first.json()["kind"] == "weekly"
    again = owner.post("/api/agent/run", json={"kind": "weekly"})
    assert again.json()["id"] == first.json()["id"]  # coalesced with the queued one
    assert [c for c in fake_queue if c[0] == "enqueue" and c[1] == "weekly"] == [("enqueue", "weekly", "owner", "")]
    assert owner.post("/api/agent/run", json={"kind": "triggered"}).status_code == 422

    runs = owner.get("/api/agent/runs").json()
    assert [x["kind"] for x in runs] == ["weekly", "chat"]
    assert owner.get(f"/api/agent/runs/{runs[1]['id']}").json()["prompt"].startswith("Why did")
    assert owner.get("/api/agent/runs/9999").status_code == 404


def test_run_now_trigger_limit(owner, monkeypatch):
    def limited(*a, **k):
        raise agent_queue.TriggerLimitReached("Three triggered runs today already.")

    monkeypatch.setattr(agent_queue, "enqueue", limited)
    r = owner.post("/api/agent/run", json={"kind": "nightly"})
    assert r.status_code == 409 and "Three triggered" in r.json()["detail"]


def test_claim_and_finish(agent, fake_queue):
    assert agent.post("/api/agent/claim").json() is None  # nothing queued
    rid = _run("nightly")
    claimed = agent.post("/api/agent/claim")
    assert claimed.status_code == 200
    assert (claimed.json()["id"], claimed.json()["status"]) == (rid, "running")

    r = agent.post(f"/api/agent/runs/{rid}/finish", json={
        "status": "completed", "result_text": "All good.", "model": "claude-opus-5-5",
        "terminal_reason": "completed", "num_turns": 12, "usage": {"output_tokens": 900}})
    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["result_text"], r.json()["num_turns"]) == ("completed", "All good.", 12)
    finish_call = [c for c in fake_queue if c[0] == "finish"][0]
    assert finish_call[1] == rid and "error" not in finish_call[2]  # unset fields are not passed

    assert agent.post(f"/api/agent/runs/{rid}/finish", json={"status": "completed"}).status_code == 409
    assert agent.post("/api/agent/runs/9999/finish", json={"status": "completed"}).status_code == 404


def test_finish_deferred_needs_not_before(agent, fake_queue):
    rid = _run("weekly", status="running")
    r = agent.post(f"/api/agent/runs/{rid}/finish", json={"status": "deferred", "error": "usage limit"})
    assert r.status_code == 422
    later = (utcnow() + timedelta(hours=3)).isoformat()
    r = agent.post(f"/api/agent/runs/{rid}/finish", json={"status": "deferred", "error": "usage limit", "not_before": later})
    assert r.status_code == 200 and r.json()["status"] == "deferred" and r.json()["not_before"] is not None


def test_heartbeat_records_sign_in_and_raises_alerts(agent, alerts):
    expires = local_date(utcnow(), "America/Chicago") + timedelta(days=12)
    r = agent.post("/api/agent/heartbeat", json={"signed_in": True, "sdk_version": "0.9.1", "cli_version": "3.1.0",
                                                 "token_expires_at": expires.isoformat()})
    assert r.status_code == 200, r.text
    info = r.json()
    assert info["signed_in"] is True and info["sdk_version"] == "0.9.1"
    assert info["token_expires_at"] == expires.isoformat()
    assert "expires in 12 days" in info["token_warning"]
    assert ("resolve", "claude_signin") in alerts
    assert ("raise", "claude_token_expiry", "warn", "claude_token_expiry") in alerts

    r = agent.post("/api/agent/heartbeat", json={"signed_in": False})
    assert r.json()["signed_in"] is False and "sign-in failed" in r.json()["token_warning"]
    assert ("raise", "claude_signin", "error", "claude_signin") in alerts
    status = agent.get("/api/agent/status").json()
    assert status["signed_in"] is False and status["last_beat_at"] is not None


def test_heartbeat_survives_notify_failures(agent, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("ntfy down")

    monkeypatch.setattr(notify, "raise_alert", broken)
    monkeypatch.setattr(notify, "resolve_alert", broken)
    r = agent.post("/api/agent/heartbeat", json={"signed_in": False})
    assert r.status_code == 200 and r.json()["signed_in"] is False


def test_agent_info_counts_and_schedule(owner):
    now = utcnow()
    _run("triggered", status="completed", requested_by="anomaly")
    _run("triggered", status="queued", requested_by="anomaly")
    last = _run("chat", status="completed", requested_by="owner", prompt="hi")
    info = owner.get("/api/agent/status").json()
    assert info["triggered_today"] == 2
    assert info["last_run"]["id"] == last
    nxt = datetime.fromisoformat(info["next_nightly_at"])
    local = nxt.astimezone(ZoneInfo("America/Chicago"))
    assert nxt > now and (local.hour, local.minute) == (3, 30) and nxt - now <= timedelta(days=1)


def test_token_warning_and_next_local_time():
    today = date(2026, 10, 4)
    assert token_warning(None, None, today) is None
    assert token_warning(True, today + timedelta(days=31), today) is None
    assert "in 30 days" in token_warning(True, today + timedelta(days=30), today)
    assert "today" in token_warning(True, today, today)
    assert "expired" in token_warning(True, today - timedelta(days=1), today)
    assert "sign-in failed" in token_warning(False, today + timedelta(days=200), today)
    # the night DST ends (2026-11-01): 07:00 UTC is 01:00 CST, so 03:30 local is still ahead today
    now = datetime(2026, 11, 1, 7, 0, tzinfo=UTC)
    nxt = next_local_time(now, "America/Chicago", "03:30")
    assert nxt.astimezone(ZoneInfo("America/Chicago")).strftime("%Y-%m-%d %H:%M") == "2026-11-01 03:30"
    late = datetime(2026, 10, 4, 10, 0, tzinfo=UTC)  # 05:00 CDT, after today's 03:30
    assert next_local_time(late, "America/Chicago", "03:30") == datetime(2026, 10, 5, 8, 30, tzinfo=UTC)
