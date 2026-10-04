"""notify: alert dedupe/resolve and the ntfy push (MockTransport, no network)."""

from __future__ import annotations

import base64

import httpx
import pytest
from sqlalchemy import select

from climate import notify
from climate.config import get_settings
from climate.store.orm import Alert


@pytest.fixture
def ntfy(monkeypatch):
    """Point push() at a MockTransport and capture the requests."""
    sent: list[httpx.Request] = []
    status = {"code": 200}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(status["code"], json={"id": "x"})

    monkeypatch.setenv("CLIMATE_NTFY_URL", "http://ntfy.local/")
    monkeypatch.setenv("CLIMATE_NTFY_TOPIC", "house")
    monkeypatch.setenv("CLIMATE_NTFY_TOKEN", "tk_secret")
    monkeypatch.setenv("CLIMATE_PUBLIC_URL", "https://climate.example.home")
    get_settings.cache_clear()
    monkeypatch.setattr(notify, "_TRANSPORT", httpx.MockTransport(handler))
    yield sent, status
    monkeypatch.delenv("CLIMATE_NTFY_URL")
    get_settings.cache_clear()


def open_alerts(db) -> list[Alert]:
    db.expire_all()
    return db.execute(select(Alert).where(Alert.resolved_at.is_(None)).order_by(Alert.id)).scalars().all()


def test_dedupe_and_resolve(db):
    first = notify.raise_alert(db, "source_down", "warn", "ecobee down", dedupe_key="source_down")
    assert first is not None
    assert notify.raise_alert(db, "source_down", "warn", "ecobee down again", dedupe_key="source_down") is None
    assert [a.id for a in open_alerts(db)] == [first]

    notify.resolve_alert(db, "source_down")
    assert open_alerts(db) == []
    notify.resolve_alert(db, "source_down")  # no-op

    again = notify.raise_alert(db, "source_down", "warn", "ecobee down", dedupe_key="source_down")
    assert again is not None and again != first


def test_alerts_without_dedupe_key_always_insert(db):
    a = notify.raise_alert(db, "note", "info", "one")
    b = notify.raise_alert(db, "note", "info", "two")
    assert a and b and a != b
    with pytest.raises(ValueError):
        notify.raise_alert(db, "note", "fatal", "bad level")


def test_push_disabled_without_url(db):
    assert get_settings().ntfy_url == ""
    assert notify.push("t", "b") is False
    alert_id = notify.raise_alert(db, "x", "error", "boom", dedupe_key="x")
    db.expire_all()
    assert db.get(Alert, alert_id).notified_at is None


def test_push_headers(ntfy):
    sent, _ = ntfy
    assert notify.push("Upstairs maxed out", "Body text", priority="high", tags=["warning", "maxed"]) is True
    req = sent[0]
    assert str(req.url) == "http://ntfy.local/house"
    assert req.headers["Title"] == "Upstairs maxed out"
    assert req.headers["Priority"] == "high"
    assert req.headers["Tags"] == "warning,maxed"
    assert req.headers["Click"] == "https://climate.example.home"
    assert req.headers["Authorization"] == "Bearer tk_secret"
    assert req.content == b"Body text"


def test_push_non_ascii_title_is_rfc2047(ntfy):
    sent, _ = ntfy
    assert notify.push("Girls’ Room 79°F", "") is True
    title = sent[0].headers["Title"]
    assert title.startswith("=?UTF-8?B?") and title.endswith("?=")
    assert base64.b64decode(title[10:-2]).decode() == "Girls’ Room 79°F"
    assert sent[0].content == "Girls’ Room 79°F".encode()


def test_push_failure_never_raises(ntfy, monkeypatch):
    _, status = ntfy
    status["code"] = 500
    assert notify.push("t", "b") is False

    def boom(request):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(notify, "_TRANSPORT", httpx.MockTransport(boom))
    assert notify.push("t", "b") is False


def test_raise_alert_pushes_warn_and_error_and_marks_notified(db, ntfy):
    sent, _ = ntfy
    info_id = notify.raise_alert(db, "note", "info", "fyi")
    warn_id = notify.raise_alert(db, "sensor_offline", "warn", "Kitchen sensor offline", "no reading",
                                 dedupe_key="sensor_offline:main.kitchen")
    assert notify.raise_alert(db, "sensor_offline", "warn", "dup", dedupe_key="sensor_offline:main.kitchen") is None
    assert len(sent) == 1
    assert sent[0].headers["Tags"] == "warning,sensor_offline"
    db.expire_all()
    assert db.get(Alert, info_id).notified_at is None
    assert db.get(Alert, warn_id).notified_at is not None
