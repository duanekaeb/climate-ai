"""Integration smoke test: every GET endpoint, for real (no monkeypatching), against 30 days of
``tests.factories.make_history``. It exercises the other builders' services end to end, so it
only passes once state, control, analytics, experiments and models are all implemented."""

from __future__ import annotations

from datetime import timedelta

import pytest

from climate.analytics import baseline
from climate.store.db import session_scope
from climate.timeutil import local_date, utcnow
from tests.factories import make_history

GETS = [
    "/api/health",
    "/api/auth/state",
    "/api/status",
    "/api/rooms/hallway/history?hours=24",
    "/api/rooms/twins_room/history",
    "/api/runtime/daily?days=30",
    "/api/runtime/intraday",
    "/api/weather?hours_back=48&hours_ahead=48",
    "/api/analytics/savings",
    "/api/analytics/waterfall",
    "/api/analytics/baselines",
    "/api/analytics/coupling?days=30",
    "/api/analytics/comfort?days=7",
    "/api/analytics/drift",
    "/api/analytics/natural-experiments?days=30",
    "/api/control/settings",
    "/api/control/plan",
    "/api/control/actions?limit=100",
    "/api/changes",
    "/api/experiments",
    "/api/experiments/power?effect_pct=10&alpha=0.1&power=0.8",
    "/api/models",
    "/api/reports?limit=20",
    "/api/alerts?open=true",
    "/api/agent/status",
    "/api/agent/runs?limit=20",
    "/api/setup",
]


@pytest.fixture
def history_owner(owner):
    # make_history inserts 5000-row batches, which exceed Postgres's 65535 bind parameters for
    # runtime_5m beyond ~4 days per call; build the 30 days from contiguous 3-day chunks.
    end = utcnow().replace(minute=0, second=0, microsecond=0)
    with session_scope() as s:
        for i in range(10):
            make_history(s, days=3, end=end - timedelta(days=3 * i), seed=i + 1)
    with session_scope() as s:
        baseline.refit_all(s, utcnow())  # the nightly fit, so expected runtime and baselines exist
    return owner


def _get(client, path: str, failures: dict, **params):
    """GET, recording a non-200 (or a server exception, which TestClient re-raises) as a failure."""
    try:
        r = client.get(path, params=params or None)
    except Exception as exc:  # noqa: BLE001 - report every broken endpoint at once
        failures[path] = f"{type(exc).__name__}: {exc}"[:200]
        return None
    if r.status_code != 200:
        failures[path] = f"{r.status_code} {r.text[:200]}"
        return None
    return r.json()


def test_every_get_endpoint(history_owner):
    failures: dict[str, str] = {}
    for path in GETS:
        _get(history_owner, path, failures)
    yesterday = local_date(utcnow(), "America/Chicago") - timedelta(days=1)
    intraday = _get(history_owner, "/api/runtime/intraday", failures, date=yesterday.isoformat())
    if intraday is not None and not all(u["points"] for u in intraday["units"]):
        failures["/api/runtime/intraday?date=yesterday"] = "a unit has no points"

    status = _get(history_owner, "/api/status", failures)
    if status is not None:
        rooms = {r["room_key"]: r for r in status["rooms"]}
        if len(status["units"]) != 3 or len(rooms) != 11:
            failures["/api/status shape"] = f"{len(status['units'])} units, {len(rooms)} rooms"
        if any(rooms[k]["temp_f"] is not None for k in ("twins_room", "olive_room", "foyer")):
            failures["/api/status rooms"] = "a room without a sensor has a temperature"
        if any(u["target"] is None for u in status["units"]):
            failures["/api/status targets"] = "a unit has no policy target"
        if status["weather"] is None:
            failures["/api/status weather"] = "no weather"
    daily = _get(history_owner, "/api/runtime/daily", failures, days=30)
    if daily is not None and not any(d["cool_min"] > 0 and d["expected_min"] is not None for d in daily):
        failures["/api/runtime/daily values"] = "no cooling minutes with an expected baseline"
    assert not failures, "\n".join(f"{k}: {v}" for k, v in failures.items())
