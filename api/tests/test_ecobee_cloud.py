"""EcobeeCloud against a fake ecobee cloud (httpx.MockTransport) and the test database."""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest
from sqlalchemy import select

from climate.sources.base import HoldRequest
from climate.sources.ecobee import (
    HOLDS_KEY,
    REFRESH_SECRET,
    STATUS_KEY,
    EcobeeApiError,
    EcobeeAuthError,
    EcobeeCloud,
)
from climate.store.app_settings import get_raw
from climate.store.orm import EcobeeThermostat, Sensor, Unit
from climate.store.secrets import get_secret, put_secret

FIX = Path(__file__).parent / "fixtures" / "ecobee"
CLIENT_ID = "test-web-client"
MAIN, UP, BED = "411111111111", "422222222222", "433333333333"

_INCLUDE_KEYS = {
    "includeRuntime": "runtime",
    "includeExtendedRuntime": "extendedRuntime",
    "includeSensors": "remoteSensors",
    "includeProgram": "program",
    "includeEvents": "events",
    "includeSettings": "settings",
    "includeEquipmentStatus": "equipmentStatus",
    "includeWeather": "weather",
    "includeLocation": "location",
    "includeUtility": "utility",
}
_ALWAYS = ("identifier", "name", "thermostatRev", "isRegistered", "modelNumber", "brand", "features",
           "lastModified", "thermostatTime", "utcTime")


class FakeEcobee:
    """Just enough of api.ecobee.com + auth.ecobee.com to exercise the adapter."""

    def __init__(self) -> None:
        data = json.loads((FIX / "thermostats.json").read_text())
        self.tstats: dict[str, dict[str, Any]] = {t["identifier"]: t for t in data["thermostatList"]}
        self.report = json.loads((FIX / "runtime_report.json").read_text())
        self.refresh_token = "rt-1"
        self.valid_access: set[str] = set()
        self.n_tokens = 0
        self.rotate = True
        self.token_posts: list[dict[str, str]] = []
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []  # (method, path, decoded body)
        self.posts: list[dict[str, Any]] = []
        self.hold_heat_override: int | None = None  # simulate ecobee storing something else
        self.bump_rev_on_summary = False
        self.fail_next: tuple[int, int, str] | None = None
        self.inflight = 0
        self.max_inflight = 0
        self.report_bodies: list[dict[str, Any]] = []
        self.dr_sticky = False  # resumeProgram leaves a running demandResponse in place

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    # --- helpers ---------------------------------------------------------------------
    @staticmethod
    def _ok(extra: dict[str, Any] | None = None) -> httpx.Response:
        return httpx.Response(200, json={**(extra or {}), "status": {"code": 0, "message": ""}})

    def _view(self, t: dict[str, Any], sel: dict[str, Any]) -> dict[str, Any]:
        out = {k: copy.deepcopy(t[k]) for k in _ALWAYS if k in t}
        for flag, key in _INCLUDE_KEYS.items():
            if sel.get(flag) and key in t:
                out[key] = copy.deepcopy(t[key])
        return out

    def _bump(self, t: dict[str, Any]) -> None:
        t["thermostatRev"] = str(int(t["thermostatRev"]) + 1)

    def _local_now(self, t: dict[str, Any]) -> datetime:
        return datetime.strptime(t["thermostatTime"], "%Y-%m-%d %H:%M:%S")  # noqa: DTZ007 - thermostat-local

    # --- handler ---------------------------------------------------------------------
    async def handler(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == "auth.ecobee.com":
            assert url.path == "/oauth/token" and request.method == "POST"
            form = dict(parse_qsl(request.content.decode()))
            self.token_posts.append(form)
            if form.get("grant_type") != "refresh_token" or form.get("refresh_token") != self.refresh_token:
                return httpx.Response(403 if form.get("refresh_token") else 400,
                                      json={"error": "invalid_grant", "error_description": "Unknown or invalid refresh token."})
            self.n_tokens += 1
            access = f"at-{self.n_tokens}"
            self.valid_access = {access}
            body: dict[str, Any] = {"access_token": access, "expires_in": 3600, "token_type": "Bearer"}
            if self.rotate:
                self.refresh_token = f"rt-{self.n_tokens + 1}"
                body["refresh_token"] = self.refresh_token
            return httpx.Response(200, json=body)

        assert url.host == "api.ecobee.com"
        bearer = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if bearer not in self.valid_access:
            return httpx.Response(500, json={"status": {"code": 14, "message": "Authentication token has expired."}})
        if self.fail_next:
            http, code, msg = self.fail_next
            self.fail_next = None
            return httpx.Response(http, json={"status": {"code": code, "message": msg}})
        path = url.path
        if request.method == "GET":
            raw = url.params.get("json") or url.params.get("body")
            body = json.loads(raw) if raw else None
            self.calls.append(("GET", path, body))
            if path == "/1/thermostatSummary":
                return self._summary(body["selection"])
            if path == "/1/thermostat":
                sel = body["selection"]
                ids = sel["selectionMatch"].split(",")
                return self._ok({"page": {"page": 1, "totalPages": 1, "pageSize": len(ids), "total": len(ids)},
                                 "thermostatList": [self._view(self.tstats[i], sel) for i in ids if i in self.tstats]})
            if path == "/1/runtimeReport":
                assert url.params.get("format") == "json"
                self.report_bodies.append(body)
                self.inflight += 1
                self.max_inflight = max(self.max_inflight, self.inflight)
                await asyncio.sleep(0.01)
                self.inflight -= 1
                return httpx.Response(200, json=self.report)
        if request.method == "POST" and path == "/1/thermostat":
            assert url.params.get("format") == "json"
            body = json.loads(request.content)
            self.calls.append(("POST", path, body))
            self.posts.append(body)
            for ident in body["selection"]["selectionMatch"].split(","):
                self._apply(self.tstats[ident], body)
            return self._ok()
        return httpx.Response(404, json={"status": {"code": 5, "message": "not found"}})

    def _summary(self, sel: dict[str, Any]) -> httpx.Response:
        if self.bump_rev_on_summary:
            for t in self.tstats.values():
                self._bump(t)
        ids = list(self.tstats) if sel["selectionType"] == "registered" else sel["selectionMatch"].split(",")
        revs, stat = [], []
        for i in ids:
            t = self.tstats[i]
            r = t["runtime"]
            revs.append(f"{i}:{t['name']}:{'true' if r['connected'] else 'false'}:{t['thermostatRev']}:"
                        f"261004120000:{r['runtimeRev']}:261004193000")
            stat.append(f"{i}:{t['equipmentStatus']}")
        return self._ok({"thermostatCount": len(ids), "revisionList": revs, "statusList": stat})

    def _apply(self, t: dict[str, Any], body: dict[str, Any]) -> None:
        for fn in body.get("functions", []):
            params = fn["params"]
            if fn["type"] == "setHold":
                start = self._local_now(t)
                end = start + timedelta(hours=int(params["holdHours"]))
                ev = {"type": "hold", "name": "auto", "running": True,
                      "startDate": start.strftime("%Y-%m-%d"), "startTime": start.strftime("%H:%M:%S"),
                      "endDate": end.strftime("%Y-%m-%d"), "endTime": end.strftime("%H:%M:%S"),
                      "heatHoldTemp": self.hold_heat_override or params["heatHoldTemp"],
                      "coolHoldTemp": params["coolHoldTemp"], "isTemperatureAbsolute": True, "holdClimateRef": ""}
                t["events"] = [ev] + [e for e in t["events"] if e["type"] != "hold"]
                t["runtime"]["desiredHeat"] = ev["heatHoldTemp"]
                t["runtime"]["desiredCool"] = ev["coolHoldTemp"]
            elif fn["type"] == "resumeProgram":
                # ecobee removes the top running event, except a mandatory demand response
                for i, e in enumerate(t["events"]):
                    if not e["running"] or e["type"] == "template":
                        continue
                    if e["type"] == "demandResponse" and (e.get("isOptional") is False or self.dr_sticky):
                        break
                    del t["events"][i]
                    break
            self._bump(t)
        th = body.get("thermostat") or {}
        if "settings" in th:
            t["settings"].update(th["settings"])
            self._bump(t)
        if "program" in th:
            current = t["program"].get("currentClimateRef")
            t["program"] = copy.deepcopy(th["program"])
            t["program"]["currentClimateRef"] = current
            self._bump(t)


@pytest.fixture
def fake() -> FakeEcobee:
    return FakeEcobee()


@pytest.fixture
async def cloud(db, fake):
    put_secret(db, REFRESH_SECRET, "rt-1")
    db.commit()
    c = EcobeeCloud(CLIENT_ID, transport=fake.transport(), readback_delay_s=0)
    yield c
    await c.close()


def _secret(db) -> str | None:
    db.rollback()
    return get_secret(db, REFRESH_SECRET)


def _running_event(etype: str, **overrides: Any) -> dict[str, Any]:
    """A running ecobee event of ``etype`` (14:00-17:00 thermostat time, absolute setpoints)."""
    ev: dict[str, Any] = {"type": etype, "name": etype, "running": True, "startDate": "2026-10-04",
                          "startTime": "14:00:00", "endDate": "2026-10-04", "endTime": "17:00:00",
                          "heatHoldTemp": 640, "coolHoldTemp": 800, "isTemperatureAbsolute": True,
                          "isOptional": True, "linkRef": "", "holdClimateRef": ""}
    ev.update(overrides)
    return ev


def _no_hand_set_hold(fake: FakeEcobee, ident: str) -> dict[str, Any]:
    """Take the fixture's running hand-set hold off a thermostat (the Hallway runs one, which
    a controller write must never replace); returns it so a test can put it back."""
    t = fake.tstats[ident]
    held = next(e for e in t["events"] if e["type"] == "hold" and e["running"])
    t["events"] = [e for e in t["events"] if e is not held]
    return held


# --- summary + mapping ---------------------------------------------------------------


async def test_poll_revisions_upserts_and_automaps(cloud, fake, db):
    revs = await cloud.poll_revisions()
    assert revs == {"main": "261004193016|261004193200", "up": "261004180000|261004193100",
                    "bed": "261004170000|261004190000"}
    db.rollback()
    units = {u.key: u.ecobee_identifier for u in db.execute(select(Unit)).scalars()}
    assert units == {"main": MAIN, "up": UP, "bed": BED}
    rows = {r.identifier: r for r in db.execute(select(EcobeeThermostat)).scalars()}
    assert rows[MAIN].unit_key == "main" and rows[MAIN].last_revision == revs["main"]
    assert rows[UP].name == "Toy Room" and rows[UP].last_seen_at is not None
    # only the summary was fetched (budget: details only on revision change)
    assert [c[1] for c in fake.calls] == ["/1/thermostatSummary"]
    assert fake.calls[0][2]["selection"] == {"selectionType": "registered", "selectionMatch": "",
                                            "includeEquipmentStatus": True}


async def test_automap_skips_ambiguous_and_taken_units(cloud, fake, db):
    fake.tstats[MAIN]["name"] = "Upstairs Bedroom"  # up AND bed -> ambiguous
    fake.tstats[UP]["name"] = "Upstairs"
    db.execute(select(Unit))
    db.get(Unit, "up").ecobee_identifier = "999999999999"  # already has a thermostat
    db.commit()
    revs = await cloud.poll_revisions()
    db.rollback()
    units = {u.key: u.ecobee_identifier for u in db.execute(select(Unit)).scalars()}
    assert units == {"main": None, "up": "999999999999", "bed": BED}
    assert revs == {"bed": "261004170000|261004190000"}
    rows = {r.identifier: r.unit_key for r in db.execute(select(EcobeeThermostat)).scalars()}
    assert rows == {MAIN: None, UP: None, BED: "bed"}
    # an owner's unmapping sticks: a known thermostat is never auto-mapped again
    db.get(Unit, "bed").ecobee_identifier = None
    db.commit()
    assert await cloud.poll_revisions() == {}


async def test_two_new_thermostats_claiming_one_unit_are_left_unmapped(cloud, fake, db):
    fake.tstats[UP]["name"] = "Hall Upper"  # 'hall' + 'upper' -> main and up: ambiguous
    fake.tstats[BED]["name"] = "Main"
    revs = await cloud.poll_revisions()
    assert set(revs) == set()  # MAIN 'Hallway' and BED 'Main' both claim main


# --- snapshots ------------------------------------------------------------------------


async def test_fetch_snapshots(cloud, fake, db):
    await cloud.poll_revisions()
    fake.calls.clear()
    snaps = await cloud.fetch_snapshots()
    assert [s.unit_key for s in snaps] == ["main", "up", "bed"]
    (get,) = fake.calls
    sel = get[2]["selection"]
    assert sel["selectionType"] == "thermostats" and set(sel["selectionMatch"].split(",")) == {MAIN, UP, BED}
    for flag in ("includeRuntime", "includeExtendedRuntime", "includeSensors", "includeProgram", "includeEvents",
                 "includeSettings", "includeEquipmentStatus", "includeWeather", "includeUtility"):
        assert sel[flag] is True

    main = snaps[0]
    assert main.source == "ecobee" and main.revision == "261004193016|261004193200"
    assert main.ts == datetime(2026, 10, 4, 19, 32, 10, tzinfo=UTC)
    assert (main.name, main.model, main.hvac_mode) == ("Hallway", "aresSmart", "cool")
    assert main.equipment_running == ["compCool1", "fan"]
    assert (main.zone_temp_f, main.zone_humidity, main.heat_sp_f, main.cool_sp_f) == (74.1, 48.0, 68.0, 75.0)
    assert (main.outdoor_temp_f, main.outdoor_humidity) == (84.2, 40.0)
    assert main.climate_ref == "home" and main.connected is True
    assert main.hold is not None and main.hold.kind == "temperature"
    assert (main.hold.heat_f, main.hold.cool_f, main.hold.hold_type) == (68.0, 75.0, "holdHours")
    assert main.hold.start == datetime(2026, 10, 4, 19, 0, tzinfo=UTC)
    assert main.hold.end == datetime(2026, 10, 4, 21, 0, tzinfo=UTC)
    assert main.hold.set_by_us is False
    readings = {r.sensor_key: r for r in main.sensors}
    assert set(readings) == {"main.hallway_tstat", "main.school_room", "main.living_room", "main.kitchen"}
    assert readings["main.hallway_tstat"].occupied is True and readings["main.hallway_tstat"].humidity == 48.0
    assert readings["main.school_room"].temp_f == 72.8 and readings["main.school_room"].occupied is False
    assert readings["main.kitchen"].temp_f is None and readings["main.kitchen"].online is False
    assert main.sensor_sets == {"home": ["main.hallway_tstat", "main.school_room", "main.living_room"],
                                "away": ["main.hallway_tstat"], "sleep": ["main.hallway_tstat"]}
    assert main.settings["autoAway"] is True and main.settings["followMeComfort"] is False
    assert main.settings["heatCoolMinDelta"] == 4.0 and main.settings["timeZone"] == "America/Chicago"

    up = snaps[1]
    assert up.model == "attisRetail" and up.hold is None and up.equipment_running == ["compCool1", "compCool2", "fan"]
    up_readings = {r.sensor_key: r for r in up.sensors}
    assert up_readings["up.toy_room_tstat"].occupied is None  # Essential: no occupancy
    assert up_readings["up.girls_room"].temp_f == 75.8
    assert up.sensor_sets["sleep"] == ["up.girls_room"]

    bed = snaps[2]
    assert bed.connected is False and bed.hvac_mode == "auto"
    assert bed.hold is not None and bed.hold.hold_type == "indefinite" and bed.hold.set_by_us is False

    db.rollback()
    ids = dict(db.execute(select(Sensor.key, Sensor.ecobee_sensor_id)).all())
    assert ids["main.hallway_tstat"] == "ei:0" and ids["main.school_room"] == "rs:100"
    assert ids["up.toy_room"] == "rs:100" and ids["up.girls_room"] == "rs:101" and ids["bed.office"] == "rs:100"
    assert db.get(Unit, "up").thermostat_model == "attisRetail"
    row = db.get(EcobeeThermostat, MAIN)
    assert row.model_number == "aresSmart" and row.settings["heatCoolMinDelta"] == 4.0
    meta = {s["id"]: s for s in row.sensors}
    assert meta["rs:103"]["name"] == "Garage" and meta["rs:103"]["sensor_key"] is None
    assert meta["ei:0"]["capabilities"] == ["humidity", "occupancy", "temperature"]
    assert meta["rs:100"]["type"] == "ecobee3_remote_sensor"

    only_up = await cloud.fetch_snapshots(["up"])
    assert [s.unit_key for s in only_up] == ["up"]


# --- holds ----------------------------------------------------------------------------


async def test_set_hold_sends_holdhours_with_rounded_tenths_and_reads_back(cloud, fake, db):
    await cloud.poll_revisions()
    result = await cloud.set_hold(HoldRequest(unit_key="up", heat_f=68.4, cool_f=75.9, hours=2, reason="linked floors"))
    assert result.ok is True, result.error
    (post,) = fake.posts
    assert post["selection"] == {"selectionType": "thermostats", "selectionMatch": UP}
    assert post["functions"] == [{"type": "setHold", "params": {"holdType": "holdHours", "holdHours": 2,
                                                               "heatHoldTemp": 685, "coolHoldTemp": 760}}]
    assert result.channel == "ecobee"
    assert result.before["hold"] is None and result.before["cool_sp_f"] == 77.0
    assert result.request["heat_f"] == 68.5 and result.request["cool_f"] == 76.0
    assert result.readback["hold"]["heat_f"] == 68.5 and result.readback["hold"]["set_by_us"] is True
    # the read-back was a fresh GET after the POST
    assert fake.calls[-1][0] == "GET" and "includeEvents" in fake.calls[-1][2]["selection"]
    db.rollback()
    assert get_raw(db, HOLDS_KEY)["up"]["heat_f"] == 68.5
    # the next snapshot recognizes the hold as ours
    (snap,) = await cloud.fetch_snapshots(["up"])
    assert snap.hold.set_by_us is True and snap.hold.hold_type == "holdHours"
    assert snap.hold.end - snap.hold.start == timedelta(hours=2)


async def test_smart_away_snapshot_is_distinguishable_and_not_ours(cloud, fake, db):
    """Finding 2: a running autoAway reaches the snapshot as hold_type 'autoAway', set_by_us
    False, even when its setpoints equal the hold the controller last wrote."""
    await cloud.poll_revisions()
    ours = await cloud.set_hold(HoldRequest(unit_key="up", heat_f=62.0, cool_f=80.0, hours=2, reason="ours"))
    assert ours.ok, ours.error
    held = fake.tstats[UP]["events"][0]
    fake.tstats[UP]["events"] = [dict(held, type="autoAway", name="smartAway", holdClimateRef="away"), held]
    (snap,) = await cloud.fetch_snapshots(["up"])
    assert snap.hold is not None
    assert (snap.hold.hold_type, snap.hold.set_by_us, snap.hold.climate_ref) == ("autoAway", False, "away")
    # and once Smart Away ends, our own hold is recognized again
    fake.tstats[UP]["events"] = [held]
    (snap,) = await cloud.fetch_snapshots(["up"])
    assert (snap.hold.hold_type, snap.hold.set_by_us) == ("holdHours", True)


async def test_set_hold_readback_mismatch_is_not_ok(cloud, fake):
    await cloud.poll_revisions()
    fake.hold_heat_override = 680
    result = await cloud.set_hold(HoldRequest(unit_key="up", heat_f=69.0, cool_f=75.0, hours=1, reason="x"))
    assert result.ok is False
    assert "does not match" in result.error
    assert result.readback["hold"]["heat_f"] == 68.0
    assert len(fake.posts) == 1


def _shift_clock(t: dict[str, Any], minutes: int) -> None:
    """Move a fake thermostat's clock (thermostatTime and utcTime) forward."""
    for k in ("thermostatTime", "utcTime"):
        dt = datetime.strptime(t[k], "%Y-%m-%d %H:%M:%S") + timedelta(minutes=minutes)  # noqa: DTZ007
        t[k] = dt.strftime("%Y-%m-%d %H:%M:%S")


async def test_renewal_that_never_landed_is_not_verified(cloud, fake, db, monkeypatch):
    """Finding 5: an older hold with the same setpoints and 10 minutes left must not verify a
    renewal whose POST was lost in transit."""
    await cloud.poll_revisions()
    first = await cloud.set_hold(HoldRequest(unit_key="up", heat_f=68.0, cool_f=77.0, hours=2, reason="first"))
    assert first.ok, first.error
    db.rollback()
    record = dict(get_raw(db, HOLDS_KEY)["up"])
    _shift_clock(fake.tstats[UP], 110)  # 10 minutes left on our hold: the controller renews it

    async def lost_post(ident, payload):
        raise EcobeeApiError("ecobee thermostat request failed (ConnectError)", transport=True)

    monkeypatch.setattr(cloud, "_post", lost_post)
    renew = await cloud.set_hold(HoldRequest(unit_key="up", heat_f=68.0, cool_f=77.0, hours=2, reason="renew"))
    assert renew.ok is False
    assert "older hold" in renew.error and "ConnectError" in renew.error
    assert renew.readback["hold"]["heat_f"] == 68.0  # same setpoints, but the old end time
    db.rollback()
    stored = dict(get_raw(db, HOLDS_KEY)["up"])
    attempt = stored.pop("attempt")
    assert stored == record  # the record of our verified hold is not moved
    # the unverified renewal is noted on its own; the older hold does not end in its window
    assert datetime.fromisoformat(attempt["end_from"]) > datetime.fromisoformat(record["end"])
    (snap,) = await cloud.fetch_snapshots(["up"])
    assert snap.hold.set_by_us is True  # still the verified older hold of ours


async def test_renewal_that_lands_is_verified(cloud, fake, db):
    await cloud.poll_revisions()
    first = await cloud.set_hold(HoldRequest(unit_key="up", heat_f=68.0, cool_f=77.0, hours=2, reason="first"))
    assert first.ok, first.error
    _shift_clock(fake.tstats[UP], 110)
    renew = await cloud.set_hold(HoldRequest(unit_key="up", heat_f=68.0, cool_f=77.0, hours=2, reason="renew"))
    assert renew.ok is True, renew.error
    assert len(fake.posts) == 2
    db.rollback()
    end = datetime.fromisoformat(get_raw(db, HOLDS_KEY)["up"]["end"])
    assert end == datetime(2026, 10, 4, 23, 22, 10, tzinfo=UTC)  # shifted clock (21:22:10Z) + 2 h


async def test_set_hold_rejected_before_sending(cloud, fake):
    await cloud.poll_revisions()
    # Toy Room heatCoolMinDelta is 5.0°F
    tight = await cloud.set_hold(HoldRequest(unit_key="up", heat_f=72.0, cool_f=76.5, hours=1, reason="x"))
    assert tight.ok is False and "heatCoolMinDelta" in tight.error
    bad_hours = await cloud.set_hold(HoldRequest.model_construct(unit_key="main", heat_f=68.0, cool_f=75.0, hours=3,
                                                                 reason="x"))
    assert bad_hours.ok is False and "holdHours" in bad_hours.error
    unmapped = await cloud.set_hold(HoldRequest(unit_key="nope", heat_f=68.0, cool_f=75.0, hours=1, reason="x"))
    assert unmapped.ok is False
    assert fake.posts == []


async def test_controller_set_hold_never_replaces_a_persons_hold(cloud, fake, db):
    """Finding 9: the Hallway runs a hold someone set by hand. A controller write (decided on
    a snapshot from before it) is refused before anything is sent, in a shape the controller
    reads as a person's hold; a Quick Save is refused the same way."""
    await cloud.poll_revisions()
    result = await cloud.set_hold(HoldRequest(unit_key="main", heat_f=68.0, cool_f=76.0, hours=2,
                                              reason="linked floors"))
    assert result.ok is False and fake.posts == [] and result.readback is None
    assert result.request["refused"] is True and result.request["not_ours"] is True
    assert "not set by the controller" in result.error and "not written" in result.error
    assert result.before["hold"]["set_by_us"] is False and result.before["hold"]["hold_type"] == "holdHours"

    fake.tstats[UP]["events"] = [_running_event("quickSave", isTemperatureAbsolute=False,
                                                isTemperatureRelative=True, coolRelativeTemp=40)]
    quick = await cloud.set_hold(HoldRequest(unit_key="up", heat_f=68.0, cool_f=77.0, hours=2, reason="renew"))
    assert quick.ok is False and quick.request["not_ours"] is True and "Quick Save" in quick.error
    assert quick.before["hold"]["hold_type"] == "quickSave" and fake.posts == []
    db.rollback()
    assert get_raw(db, HOLDS_KEY) in (None, {})  # nothing recorded for a write that never went out


async def test_set_hold_decides_on_a_fresh_read_not_the_last_poll(cloud, fake):
    """Finding 9: the poll fetched the Hallway with no hold; someone sets one at the wall; the
    controller's write right after must see it (a fresh GET, not the poll's detail)."""
    await cloud.poll_revisions()
    held = _no_hand_set_hold(fake, MAIN)
    (snap,) = await cloud.fetch_snapshots(["main"])
    assert snap.hold is None
    fake.tstats[MAIN]["events"].insert(0, held)  # 2:00:30 at the wall, before any poll
    fake._bump(fake.tstats[MAIN])
    fake.calls.clear()
    result = await cloud.set_hold(HoldRequest(unit_key="main", heat_f=68.0, cool_f=76.0, hours=2, reason="tick"))
    assert result.ok is False and result.request["not_ours"] is True and fake.posts == []
    assert [(m, path) for m, path, _ in fake.calls] == [("GET", "/1/thermostat")]
    # and the sensor-set write of the same tick is refused the same way
    sets = await cloud.update_sensor_sets("main", {"sleep": ["main.hallway_tstat", "main.school_room"]}, "tick")
    assert sets.ok is False and sets.request["refused"] is True and sets.request["not_ours"] is True
    assert sets.before["hold"]["set_by_us"] is False and fake.posts == []
    # a write that would change nothing is still a no-op
    same = await cloud.update_sensor_sets("main", {"sleep": ["main.hallway_tstat"]}, "tick")
    assert same.ok is True and same.request["noop"] is True and fake.posts == []


@pytest.mark.parametrize("etype", ["vacation", "demandResponse", "today"])
async def test_no_write_over_a_protected_or_unknown_event(cloud, fake, etype):
    """Vacation, a utility event or an unknown type on top: refused for the controller AND the
    owner, holds and sensor sets alike, with the event type in the request."""
    await cloud.poll_revisions()
    fake.tstats[MAIN]["events"] = [_running_event(etype)]
    for by_owner in (False, True):
        result = await cloud.set_hold(HoldRequest(unit_key="main", heat_f=68.0, cool_f=76.0, hours=2,
                                                  reason="x", by_owner=by_owner))
        assert result.ok is False and result.request["refused"] is True and result.request["event"] == etype
        assert result.error == f"a running {etype} event is in effect; not written"
        assert "not_ours" not in result.request
    sets = await cloud.update_sensor_sets("main", {"sleep": ["main.hallway_tstat", "main.school_room"]}, "x")
    assert sets.ok is False and sets.request["event"] == etype
    assert fake.posts == []


async def test_controller_takes_smart_away_back_and_renews_its_own_hold(cloud, fake):
    """Smart Away on top and the controller's own hold never refuse."""
    await cloud.poll_revisions()
    fake.tstats[UP]["events"] = [_running_event("autoAway", holdClimateRef="away")]
    over_away = await cloud.set_hold(HoldRequest(unit_key="up", heat_f=68.0, cool_f=77.0, hours=2, reason="x"))
    assert over_away.ok is True, over_away.error
    assert over_away.before["hold"]["hold_type"] == "autoAway"
    _shift_clock(fake.tstats[UP], 110)
    renew = await cloud.set_hold(HoldRequest(unit_key="up", heat_f=68.0, cool_f=77.0, hours=2, reason="renew"))
    assert renew.ok is True, renew.error
    assert renew.before["hold"]["set_by_us"] is True and len(fake.posts) == 2


async def test_owner_hold_is_written_over_a_persons_hold_and_is_never_ours(cloud, fake, db):
    """The owner IS the person: their hold goes over the hand-set one. Once written it is the
    owner's (a person's hold), so the controller can neither replace it nor cancel it."""
    await cloud.poll_revisions()
    owner = await cloud.set_hold(HoldRequest(unit_key="main", heat_f=69.0, cool_f=75.0, hours=2,
                                             reason="Owner hold", by_owner=True))
    assert owner.ok is True, owner.error
    assert owner.before["hold"]["set_by_us"] is False and owner.readback["hold"]["set_by_us"] is False
    db.rollback()
    assert get_raw(db, HOLDS_KEY)["main"]["by_owner"] is True
    (snap,) = await cloud.fetch_snapshots(["main"])
    assert snap.hold.heat_f == 69.0 and snap.hold.set_by_us is False
    n = len(fake.posts)
    ctrl = await cloud.set_hold(HoldRequest(unit_key="main", heat_f=68.0, cool_f=76.0, hours=2, reason="tick"))
    assert ctrl.ok is False and ctrl.request["not_ours"] is True
    resume = await cloud.resume_program("main", "controller resume")
    assert resume.ok is False and resume.error == "the running hold was not set by the controller; not cancelled"
    assert len(fake.posts) == n
    # the owner's own Back to automatic still cancels it
    back = await cloud.resume_program("main", "Owner: back to automatic", force=True)
    assert back.ok is True, back.error


async def test_our_write_that_landed_without_a_read_back_is_still_ours(cloud, fake, db, monkeypatch):
    """A controller write whose read-back GETs failed may have landed. If it did, it is ours:
    the next snapshot says so and the controller's next write over it is not refused."""
    await cloud.poll_revisions()
    real_get = cloud._get_one
    calls = {"n": 0}

    async def flaky(ident, includes):
        calls["n"] += 1
        if calls["n"] > 1:  # the GET before the write works; both read-backs time out
            raise EcobeeApiError("ecobee thermostat request failed (ReadTimeout)", transport=True)
        return await real_get(ident, includes)

    monkeypatch.setattr(cloud, "_get_one", flaky)
    result = await cloud.set_hold(HoldRequest(unit_key="up", heat_f=68.0, cool_f=77.0, hours=2, reason="x"))
    monkeypatch.setattr(cloud, "_get_one", real_get)
    assert result.ok is False and result.readback is None and calls["n"] == 3
    assert result.error.startswith("setHold sent; read-back failed") and result.request["sent"] is True
    assert len(fake.posts) == 1  # it landed at ecobee
    db.rollback()
    record = get_raw(db, HOLDS_KEY)["up"]
    assert set(record) == {"attempt"} and record["attempt"]["heat_f"] == 68.0
    (snap,) = await cloud.fetch_snapshots(["up"])
    assert snap.hold.set_by_us is True and snap.hold.heat_f == 68.0
    retry = await cloud.set_hold(HoldRequest(unit_key="up", heat_f=68.0, cool_f=76.5, hours=2, reason="x"))
    assert retry.ok is True, retry.error
    db.rollback()
    assert "attempt" not in get_raw(db, HOLDS_KEY)["up"]  # a verified write replaces the note


async def test_resume_program(cloud, fake):
    await cloud.poll_revisions()
    # the Hallway's running hold was set by hand: only an owner's forced resume cancels it
    result = await cloud.resume_program("main", "back to schedule", force=True)
    assert result.ok is True, result.error
    assert fake.posts[-1]["functions"] == [{"type": "resumeProgram", "params": {"resumeAll": False}}]
    assert result.before["hold"]["heat_f"] == 68.0 and result.readback["hold"] is None
    assert result.request["force"] is True
    # nothing running -> no write
    again = await cloud.resume_program("main", "again")
    assert again.ok is True and again.request.get("noop") is True and len(fake.posts) == 1


async def test_resume_program_refuses_a_hold_the_controller_did_not_set(cloud, fake):
    """Finding 8: the controller's resume must never erase a hand-set hold."""
    await cloud.poll_revisions()
    result = await cloud.resume_program("main", "controller resume")
    assert result.ok is False
    assert result.error == "the running hold was not set by the controller; not cancelled"
    assert result.before["hold"]["set_by_us"] is False and result.request["force"] is False
    assert fake.posts == []
    # an indefinite hand-set hold (Bedroom) is refused the same way
    bed = await cloud.resume_program("bed", "controller resume")
    assert bed.ok is False and "not set by the controller" in bed.error and fake.posts == []


async def test_resume_program_cancels_our_own_hold_without_force(cloud, fake, db):
    await cloud.poll_revisions()
    written = await cloud.set_hold(HoldRequest(unit_key="up", heat_f=68.0, cool_f=77.0, hours=2, reason="ours"))
    assert written.ok, written.error
    result = await cloud.resume_program("up", "back to schedule")
    assert result.ok is True, result.error
    assert result.before["hold"]["set_by_us"] is True and result.readback["hold"] is None
    assert fake.posts[-1]["functions"] == [{"type": "resumeProgram", "params": {"resumeAll": False}}]
    db.rollback()
    assert "up" not in get_raw(db, HOLDS_KEY)  # the record of our hold is cleared


@pytest.mark.parametrize("etype", ["vacation", "demandResponse", "today"])
async def test_resume_program_never_cancels_an_event_even_when_forced(cloud, fake, etype):
    """Vacation, a utility event and a type this app does not know: never, forced or not."""
    await cloud.poll_revisions()
    ev = dict(fake.tstats[MAIN]["events"][0], type=etype, running=True)
    fake.tstats[MAIN]["events"] = [ev]
    result = await cloud.resume_program("main", "owner resume", force=True)
    assert result.ok is False and etype in result.error and fake.posts == []


@pytest.mark.parametrize("etype", ["quickSave", "autoAway", "autoHome"])
async def test_owner_forced_resume_cancels_a_quick_save_or_smart_away(cloud, fake, etype):
    """Findings 3 / 10: the app shows a Quick Save as a person's hold and offers Back to
    automatic / Resume schedule (a forced resume); during a resume back-off it offers Back to
    automatic over Smart Away. Both must work: ecobee's resumeProgram removes the top event.
    The controller's own (unforced) resume still never touches them."""
    await cloud.poll_revisions()
    fake.tstats[UP]["events"] = [_running_event(etype)]
    unforced = await cloud.resume_program("up", "controller resume")
    assert unforced.ok is False and etype in unforced.error and fake.posts == []

    result = await cloud.resume_program("up", "Owner: back to automatic", force=True)
    assert result.ok is True, result.error
    assert fake.posts[-1]["functions"] == [{"type": "resumeProgram", "params": {"resumeAll": False}}]
    assert result.before["hold"]["hold_type"] == etype and result.readback["hold"] is None
    assert result.request["sent"] is True and fake.tstats[UP]["events"] == []


async def test_owner_forced_resume_fails_when_the_quick_save_stays(cloud, fake):
    """The read-back must see the Quick Save gone: one still on top is not a success."""
    await cloud.poll_revisions()
    fake.tstats[UP]["events"] = [_running_event("quickSave", endDate="2035-01-01", endTime="00:00:00")]
    fake._apply = lambda t, body: None  # ecobee accepts the POST but the Quick Save keeps running
    result = await cloud.resume_program("up", "Owner: back to automatic", force=True)
    assert result.ok is False and "quickSave event is still running" in result.error
    assert len(fake.posts) == 1 and result.readback["hold"]["hold_type"] == "quickSave"


async def test_resume_program_never_cancels_a_vacation(cloud, fake):
    await cloud.poll_revisions()
    vac = dict(fake.tstats[MAIN]["events"][1], running=True)
    fake.tstats[MAIN]["events"] = [vac]
    result = await cloud.resume_program("main", "x")
    assert result.ok is False and "vacation" in result.error and fake.posts == []


# --- settings + sensor sets -------------------------------------------------------------


async def test_ensure_settings(cloud, fake):
    await cloud.poll_revisions()
    results = await cloud.ensure_settings()
    assert [r.request["unit_key"] for r in results] == ["main", "up", "bed"]
    assert all(r.ok for r in results), [r.error for r in results]
    posted = {p["selection"]["selectionMatch"]: p["thermostat"]["settings"] for p in fake.posts}
    assert posted == {MAIN: {"autoAway": False, "followMeComfort": False},
                      BED: {"autoAway": False, "followMeComfort": False}}
    assert results[1].request.get("noop") is True
    assert results[0].before == {"autoAway": True, "followMeComfort": False}
    assert results[0].readback == {"autoAway": False, "followMeComfort": False}
    assert fake.tstats[BED]["settings"]["followMeComfort"] is False


async def test_ensure_settings_results_are_complete_and_idempotent(cloud, fake):
    """Every result carries before / request / readback for the worker's log; a second run
    writes nothing because everything already reads off."""
    await cloud.poll_revisions()
    first = await cloud.ensure_settings()
    for r in first:
        assert r.ok and r.channel == "ecobee", r.error
        assert set(r.request) >= {"unit_key", "identifier", "settings", "reason"}
        assert r.request["settings"] == {"autoAway": False, "followMeComfort": False}
        assert r.before and r.readback == {"autoAway": False, "followMeComfort": False}
    assert [bool(r.request.get("noop")) for r in first] == [False, True, False]
    n_posts = len(fake.posts)
    second = await cloud.ensure_settings("custom reason")
    assert len(fake.posts) == n_posts  # no write when already off
    assert all(r.ok and r.request.get("noop") is True for r in second)
    assert all(r.before == r.readback == {"autoAway": False, "followMeComfort": False} for r in second)
    assert second[0].request["reason"] == "custom reason"


async def test_ensure_settings_failed_readback_keeps_before_and_reports(cloud, fake, monkeypatch):
    await cloud.poll_revisions()
    real = cloud._get_thermostats
    calls = {"n": 0}

    async def flaky(identifiers, includes):
        calls["n"] += 1
        if calls["n"] == 2:  # the read-back after the writes
            raise EcobeeApiError("ecobee thermostat: HTTP 500, status 3: Processing error")
        return await real(identifiers, includes)

    monkeypatch.setattr(cloud, "_get_thermostats", flaky)
    results = {r.request["unit_key"]: r for r in await cloud.ensure_settings()}
    main = results["main"]
    assert main.ok is False and "read-back failed" in main.error and main.readback is None
    assert main.before == {"autoAway": True, "followMeComfort": False} and "noop" not in main.request
    assert results["up"].ok is True and results["up"].request["noop"] is True


async def test_update_sensor_sets_read_modify_write(cloud, fake):
    await cloud.poll_revisions()
    _no_hand_set_hold(fake, MAIN)
    result = await cloud.update_sensor_sets("main", {"sleep": ["main.hallway_tstat", "main.school_room"]}, "night set")
    assert result.ok is True, result.error
    (post,) = fake.posts
    program = post["thermostat"]["program"]
    assert "currentClimateRef" not in program and len(program["schedule"]) == 7
    climates = {c["climateRef"]: c for c in program["climates"]}
    assert climates["sleep"]["sensors"] == [{"id": "ei:0:1", "name": "Hallway"}, {"id": "rs:100:1", "name": "School Room"}]
    assert climates["home"]["sensors"] == fake.tstats[MAIN]["program"]["climates"][0]["sensors"]  # untouched
    assert climates["sleep"]["heatTemp"] == 670  # everything else preserved
    assert result.before["sets"]["sleep"] == ["main.hallway_tstat"]
    assert result.readback["sets"]["sleep"] == ["main.hallway_tstat", "main.school_room"]
    # the revision check went through the summary between the GET and the POST
    paths = [c[1] for c in fake.calls]
    assert paths[-4:] == ["/1/thermostat", "/1/thermostatSummary", "/1/thermostat", "/1/thermostat"]
    # same sets again -> no write
    again = await cloud.update_sensor_sets("main", {"sleep": ["main.school_room", "main.hallway_tstat"]}, "x")
    assert again.ok is True and again.request.get("noop") is True and len(fake.posts) == 1


async def test_update_sensor_sets_aborts_when_revision_moves(cloud, fake):
    await cloud.poll_revisions()
    _no_hand_set_hold(fake, MAIN)
    fake.bump_rev_on_summary = True
    result = await cloud.update_sensor_sets("main", {"home": ["main.living_room"]}, "x")
    assert result.ok is False and "revision moved" in result.error
    assert fake.posts == []


async def test_update_sensor_sets_validates_keys(cloud, fake):
    await cloud.poll_revisions()
    result = await cloud.update_sensor_sets("main", {"home": ["up.toy_room"], "party": ["main.kitchen"], "sleep": []}, "x")
    assert result.ok is False
    assert "up.toy_room" in result.error and "party" in result.error and "at least one" in result.error
    assert fake.posts == []


# --- runtime report ---------------------------------------------------------------------


async def test_fetch_runtime_parses_and_converts_time(cloud, fake, db):
    await cloud.poll_revisions()  # maps units; thermostat time zones are not known yet
    rows = await cloud.fetch_runtime(datetime(2026, 10, 4, 5, 0, tzinfo=UTC), datetime(2026, 10, 4, 5, 20, tzinfo=UTC))
    # one location lookup (time zones), then the report
    assert [c[1] for c in fake.calls][-2:] == ["/1/thermostat", "/1/runtimeReport"]
    assert fake.calls[-2][2]["selection"]["includeLocation"] is True
    (body,) = fake.report_bodies
    assert body["startDate"] == "2026-10-04" and body["startInterval"] == 60
    assert body["endDate"] == "2026-10-04" and body["endInterval"] == 63
    assert body["includeSensors"] is True and set(body["selection"]["selectionMatch"].split(",")) == {MAIN, UP, BED}
    assert "compCool1" in body["columns"] and "zoneClimate" in body["columns"]
    main = [r for r in rows if r.unit_key == "main"]
    assert [r.ts for r in main] == [datetime(2026, 10, 4, 5, m, tzinfo=UTC) for m in (0, 5, 15)]
    assert main[0].comp_cool1 == 300 and main[0].comp_cool2 == 120
    assert main[0].sensor_temps["main.school_room"] == 72.6
    db.rollback()
    assert db.get(EcobeeThermostat, MAIN).settings["timeZone"] == "America/Chicago"
    # second pull: the zone is remembered, so only the report is requested
    fake.calls.clear()
    await cloud.fetch_runtime(datetime(2026, 10, 4, 5, 0, tzinfo=UTC), datetime(2026, 10, 4, 5, 20, tzinfo=UTC))
    assert [c[1] for c in fake.calls] == ["/1/runtimeReport"]


async def test_fetch_runtime_chunks_one_request_at_a_time(cloud, fake):
    await cloud.poll_revisions()
    start = datetime(2026, 8, 20, tzinfo=UTC)
    end = datetime(2026, 10, 4, 6, 0, tzinfo=UTC)
    a, b = await asyncio.gather(cloud.fetch_runtime(start, end), cloud.fetch_runtime(start, end))
    assert fake.max_inflight == 1
    assert len(fake.report_bodies) == 4
    for body in fake.report_bodies:
        span = datetime.fromisoformat(body["endDate"]) - datetime.fromisoformat(body["startDate"])
        assert span <= timedelta(days=31)
    assert len(a) == len(b) > 0


# --- tokens -------------------------------------------------------------------------------


async def test_refresh_token_rotation_is_persisted(cloud, fake, db):
    await cloud.poll_revisions()
    assert fake.token_posts == [{"grant_type": "refresh_token", "refresh_token": "rt-1", "client_id": CLIENT_ID}]
    assert _secret(db) == "rt-2"
    # the access token expires server-side (status 14): refresh once, persist, retry
    fake.valid_access = set()
    await cloud.poll_revisions()
    assert [p["refresh_token"] for p in fake.token_posts] == ["rt-1", "rt-2"]
    assert _secret(db) == "rt-3"
    # no rotation -> the stored token stays
    fake.rotate = False
    fake.valid_access = set()
    await cloud.poll_revisions()
    assert _secret(db) == "rt-3"
    health = await cloud.health()
    assert health.ok is True and health.signed_in is True and health.consecutive_failures == 0
    assert health.last_success_at is not None


async def test_concurrent_calls_share_one_refresh(cloud, fake):
    await asyncio.gather(cloud.poll_revisions(), cloud.fetch_snapshots(), cloud.poll_revisions())
    assert len(fake.token_posts) == 1


async def test_invalid_grant_raises_auth_error(cloud, fake, db):
    fake.refresh_token = "something-else"  # our stored rt-1 was revoked
    with pytest.raises(EcobeeAuthError):
        await cloud.poll_revisions()
    health = await cloud.health()
    assert health.signed_in is False and health.ok is False and "invalid_grant" in health.detail
    db.rollback()
    assert "invalid_grant" in get_raw(db, STATUS_KEY)["last_error"]
    # after a fresh sign-in (new token stored by the API process) the worker recovers
    put_secret(db, REFRESH_SECRET, "something-else")
    db.commit()
    await cloud.poll_revisions()
    assert (await cloud.health()).signed_in is True
    db.rollback()
    assert get_raw(db, STATUS_KEY)["last_error"] is None


async def test_not_signed_in(db, fake):
    c = EcobeeCloud(CLIENT_ID, transport=fake.transport())
    try:
        with pytest.raises(EcobeeAuthError):
            await c.poll_revisions()
        h = await c.health()
        assert h.signed_in is False and fake.token_posts == []
    finally:
        await c.close()


async def test_ecobee_status_error_is_surfaced(cloud, fake):
    await cloud.poll_revisions()
    fake.fail_next = (500, 3, "Processing error. Error populating API thermostats.")
    with pytest.raises(EcobeeApiError) as err:
        await cloud.poll_revisions()
    assert err.value.ecobee_code == 3 and "Processing error" in str(err.value)
    health = await cloud.health()
    assert health.consecutive_failures == 1 and "status 3" in health.detail
    await cloud.poll_revisions()
    assert (await cloud.health()).consecutive_failures == 0


def test_from_settings_uses_configured_client_id(monkeypatch):
    from climate.config import get_settings

    monkeypatch.setenv("CLIMATE_ECOBEE_WEB_CLIENT_ID", "configured-id")
    get_settings.cache_clear()
    try:
        assert EcobeeCloud.from_settings()._client_id == "configured-id"
    finally:
        monkeypatch.delenv("CLIMATE_ECOBEE_WEB_CLIENT_ID")
        get_settings.cache_clear()
