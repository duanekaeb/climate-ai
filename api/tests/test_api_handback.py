"""Hand back to ecobee: GET shows what would be restored and the last result; POST queues one
'handback' job for the worker (409 while one is queued or running). Rows are inserted
directly; the worker is not involved."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from climate.store.app_settings import ECOBEE_ORIGINAL_KEY, ControlSettings, put_setting
from climate.store.db import session_scope
from climate.store.orm import Job
from climate.timeutil import utcnow


def handback_jobs() -> list[Job]:
    with session_scope() as s:
        rows = list(s.execute(select(Job).where(Job.kind == "handback").order_by(Job.id)).scalars())
        s.expunge_all()
        return rows


def set_job(job_id: int, status: str, result: dict | None = None) -> None:
    with session_scope() as s:
        job = s.get(Job, job_id)
        job.status, job.result = status, result
        job.finished_at = utcnow() if status in ("done", "failed") else None


def test_get_before_anything_was_captured(owner, agent):
    r = agent.get("/api/control/handback")  # a reader route
    assert r.status_code == 200, r.text
    assert r.json() == {"original": {}, "mode": "suggest", "last_job": None, "steps": []}


def test_get_shows_the_captured_originals(owner):
    captured = (utcnow() - timedelta(days=3)).isoformat()
    with session_scope() as s:
        put_setting(s, ECOBEE_ORIGINAL_KEY, {
            "up": {"captured_at": captured, "auto_away": True, "follow_me": False,
                   "home_sensors": ["up.toy_room_tstat", "up.toy_room"]},
            "bogus": {"auto_away": "not a bool and no captured_at"},
        })
        put_setting(s, "control", ControlSettings(mode="act"))
    body = owner.get("/api/control/handback").json()
    assert set(body["original"]) == {"up"}  # an entry that does not parse is left out
    assert body["original"]["up"]["auto_away"] is True and body["original"]["up"]["follow_me"] is False
    assert body["original"]["up"]["home_sensors"] == ["up.toy_room_tstat", "up.toy_room"]
    assert body["mode"] == "act"


def test_post_queues_one_job_at_a_time(owner, agent):
    assert agent.post("/api/control/handback", json={}).status_code == 403
    assert handback_jobs() == []

    r = owner.post("/api/control/handback")
    assert r.status_code == 200, r.text
    job = r.json()
    assert (job["kind"], job["status"], job["params"], job["result"]) == ("handback", "queued", {}, None)
    assert handback_jobs()[0].requested_by == "owner"

    again = owner.post("/api/control/handback")
    assert again.status_code == 409 and "already queued" in again.json()["detail"]
    set_job(job["id"], "running")
    assert owner.post("/api/control/handback").status_code == 409
    assert len(handback_jobs()) == 1

    info = owner.get("/api/control/handback").json()
    assert info["last_job"]["id"] == job["id"] and info["last_job"]["status"] == "running"
    assert info["steps"] == []  # nothing finished yet


def test_steps_come_from_the_last_finished_job(owner):
    first = owner.post("/api/control/handback").json()
    steps = [
        {"unit_key": None, "what": "Controller off", "ok": True, "detail": ""},
        {"unit_key": "up", "what": "Resumed our hold", "ok": True, "detail": "read back: no hold"},
        {"unit_key": "main", "what": "Smart Away and Follow Me restored", "ok": False, "detail": "read-back mismatch"},
        {"what": "a step without the ok flag does not parse"},
    ]
    set_job(first["id"], "done", {"steps": steps})
    body = owner.get("/api/control/handback").json()
    assert body["last_job"]["status"] == "done"
    assert [(s["unit_key"], s["what"], s["ok"]) for s in body["steps"]] == [
        (None, "Controller off", True), ("up", "Resumed our hold", True),
        ("main", "Smart Away and Follow Me restored", False),
    ]

    # a newer one queued: last_job is the new one, the steps still the finished one's
    second = owner.post("/api/control/handback")
    assert second.status_code == 200, second.text
    body = owner.get("/api/control/handback").json()
    assert body["last_job"]["id"] == second.json()["id"] and body["last_job"]["status"] == "queued"
    assert len(body["steps"]) == 3

    # a failed job carries no steps
    set_job(second.json()["id"], "failed")
    assert owner.get("/api/control/handback").json()["steps"] == []
