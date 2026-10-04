"""ecobee account sign-in (API process): client id patching, MFA, and never keeping the password."""

from __future__ import annotations

import json
import logging
from datetime import timedelta

import pytest
import pyecobee
import requests
from pyecobee import MfaChallenge
from pyecobee.errors import EcobeeAuthFailedError, EcobeeAuthMfaRequiredError, EcobeeAuthUnknownError
from sqlalchemy import select

from climate.sources import ecobee
from climate.store.app_settings import get_raw, put_setting
from climate.store.orm import AppSetting
from climate.store.secrets import get_secret, list_secret_keys, put_secret
from climate.timeutil import utcnow

EMAIL = "owner@example.com"
PASSWORD = "Pa55-w0rd-that-must-never-be-stored"
REFRESH = "v1.refresh-token-from-auth0"
ACCESS = "eyJ.access-token"
CLIENT_ID = "test-client-id-from-settings"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Configured client id, restored library global, clean MFA state."""
    from climate.config import get_settings

    monkeypatch.setenv("CLIMATE_ECOBEE_WEB_CLIENT_ID", CLIENT_ID)
    get_settings.cache_clear()
    monkeypatch.setattr(pyecobee, "ECOBEE_WEB_CLIENT_ID", pyecobee.ECOBEE_WEB_CLIENT_ID)
    monkeypatch.setattr(pyecobee.const, "ECOBEE_WEB_CLIENT_ID", pyecobee.const.ECOBEE_WEB_CLIENT_ID)
    ecobee._MFA.clear()
    yield
    ecobee._MFA.clear()
    monkeypatch.delenv("CLIMATE_ECOBEE_WEB_CLIENT_ID")
    get_settings.cache_clear()


def _everything_stored(db) -> str:
    """All secrets (decrypted) and settings rows as one string."""
    db.rollback()
    parts = [get_secret(db, k) or "" for k in list_secret_keys(db)]
    parts += [json.dumps(v) for v in db.execute(select(AppSetting.value)).scalars()]
    return "\n".join(parts)


def test_client_id_patch_reaches_the_library(monkeypatch):
    """pyecobee's methods read the module global at call time: patching it changes the requests."""
    assert ecobee._patch_client_id() == CLIENT_ID
    seen: dict[str, object] = {}

    class Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"access_token": ACCESS, "refresh_token": REFRESH, "expires_in": 3600}

    def fake_post(url, data=None, timeout=None, **kw):
        seen["url"], seen["data"] = url, data
        return Resp()

    monkeypatch.setattr(pyecobee.requests, "post", fake_post)
    client = pyecobee.Ecobee(config={"USERNAME": EMAIL})  # non-empty: an empty dict means file-based config
    assert client._exchange_code_for_tokens("auth-code", "verifier") is True
    assert seen["url"] == "https://auth.ecobee.com/oauth/token"
    assert seen["data"]["client_id"] == CLIENT_ID

    def fake_get(self, url, params=None, timeout=None, **kw):
        seen["authorize_client_id"] = params["client_id"]
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(requests.Session, "get", fake_get)
    with pytest.raises(EcobeeAuthUnknownError):
        pyecobee.Ecobee(config={"USERNAME": EMAIL, "PASSWORD": PASSWORD}).request_tokens_web()
    assert seen["authorize_client_id"] == CLIENT_ID


def _fake_library(monkeypatch, *, mfa_type: str | None = "otp", good_code: str = "123456"):
    """Replace the network parts of pyecobee.Ecobee; keep its real _write_config (which copies
    the password into ``self.config``), so wiping is actually exercised."""
    calls: dict[str, object] = {}

    def request_tokens_web(self):
        calls["client_id_at_login"] = pyecobee.ECOBEE_WEB_CLIENT_ID
        assert self.username == EMAIL and self.password == PASSWORD
        logging.getLogger("pyecobee").debug("Making request with password=%s", self.password)
        if mfa_type is None:
            self.access_token, self.refresh_token = ACCESS, REFRESH
            self._write_config()
            return True
        raise EcobeeAuthMfaRequiredError(
            MfaChallenge(challenge_url=f"https://auth.ecobee.com/u/mfa-{mfa_type}-challenge?state=abc",
                         state="abc", mfa_type=mfa_type, cookies={"auth0": "cookie"}, code_verifier="verifier"))

    def submit_mfa_code(self, challenge, code):
        calls["client_id_at_mfa"] = pyecobee.ECOBEE_WEB_CLIENT_ID
        calls["password_at_mfa"] = self.password
        assert challenge.state == "abc"
        if code != good_code:
            raise EcobeeAuthFailedError("The MFA code was not accepted by ecobee.")
        self.access_token, self.refresh_token = ACCESS, REFRESH
        self._write_config()
        return True

    monkeypatch.setattr(pyecobee.Ecobee, "request_tokens_web", request_tokens_web)
    monkeypatch.setattr(pyecobee.Ecobee, "submit_mfa_code", submit_mfa_code)
    return calls


async def test_login_with_mfa_never_stores_or_logs_the_password(db, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    calls = _fake_library(monkeypatch, mfa_type="otp")

    res = await ecobee.start_login(f"  {EMAIL} ", PASSWORD)
    assert res == {"status": "mfa_required", "mfa_type": "otp"}
    assert calls["client_id_at_login"] == CLIENT_ID
    assert ecobee.mfa_pending() == (True, "otp")
    held = ecobee._MFA["client"]
    assert held.password is None and held.config is None  # wiped while waiting for the code
    assert ecobee._MFA["expires_at"] - utcnow() <= timedelta(minutes=10)
    assert PASSWORD not in _everything_stored(db)
    assert get_secret(db, ecobee.REFRESH_SECRET) is None

    wrong = await ecobee.submit_mfa("000000")
    assert wrong["status"] == "error" and "code" in wrong["error"]
    assert ecobee.mfa_pending() == (True, "otp")  # may retry until it expires

    ok = await ecobee.submit_mfa("123456")
    assert ok == {"status": "signed_in"}
    assert calls["client_id_at_mfa"] == CLIENT_ID and calls["password_at_mfa"] is None
    assert ecobee.mfa_pending() == (False, None)
    db.rollback()
    assert get_secret(db, ecobee.REFRESH_SECRET) == REFRESH
    status = get_raw(db, ecobee.STATUS_KEY)
    assert status["signed_in_at"] and status["last_error"] is None
    stored = _everything_stored(db)
    assert PASSWORD not in stored and ACCESS not in stored  # only the refresh token, encrypted
    assert held.password is None and held.config is None and held.refresh_token is None
    # pyecobee was kept at INFO, so its DEBUG line with the password never reached the logs
    assert logging.getLogger("pyecobee").getEffectiveLevel() >= logging.INFO
    assert PASSWORD not in caplog.text and REFRESH not in caplog.text and ACCESS not in caplog.text


async def test_login_without_mfa(db, monkeypatch):
    _fake_library(monkeypatch, mfa_type=None)
    assert await ecobee.start_login(EMAIL, PASSWORD) == {"status": "signed_in"}
    db.rollback()
    assert get_secret(db, ecobee.REFRESH_SECRET) == REFRESH
    assert PASSWORD not in _everything_stored(db)
    status = ecobee.login_status(db)
    assert status["signed_in"] is True and status["last_error"] is None


async def test_expired_challenge(db, monkeypatch):
    _fake_library(monkeypatch, mfa_type="sms")
    assert (await ecobee.start_login(EMAIL, PASSWORD))["mfa_type"] == "sms"
    ecobee._MFA["expires_at"] = utcnow() - timedelta(seconds=1)
    res = await ecobee.submit_mfa("123456")
    assert res["status"] == "error" and "expired" in res["error"]
    assert ecobee.mfa_pending() == (False, None)
    assert get_secret(db, ecobee.REFRESH_SECRET) is None
    # expiry is also noticed by mfa_pending() on its own
    assert (await ecobee.start_login(EMAIL, PASSWORD))["status"] == "mfa_required"
    ecobee._MFA["expires_at"] = utcnow() - timedelta(seconds=1)
    assert ecobee.mfa_pending() == (False, None)
    assert (await ecobee.submit_mfa("123456"))["status"] == "error"


async def test_submit_without_pending_login():
    res = await ecobee.submit_mfa("123456")
    assert res["status"] == "error" and "Sign in again" in res["error"]


async def test_push_or_email_mfa_is_reported_as_unsupported(db, monkeypatch):
    def push(self):
        raise EcobeeAuthUnknownError(
            "ecobee account requires an MFA type that is not yet supported by this library "
            "(challenge URL: https://auth.ecobee.com/u/mfa-push-challenge?state=secretstate). TOTP and SMS are supported.")

    monkeypatch.setattr(pyecobee.Ecobee, "request_tokens_web", push)
    res = await ecobee.start_login(EMAIL, PASSWORD)
    assert res["status"] == "error"
    assert "push or email" in res["error"] and "TOTP" in res["error"] and "SMS" in res["error"]
    assert "secretstate" not in res["error"]
    assert ecobee.mfa_pending() == (False, None)


async def test_bad_password_and_network_errors(db, monkeypatch):
    def rejected(self):
        raise EcobeeAuthFailedError("ecobee rejected the supplied password.")

    monkeypatch.setattr(pyecobee.Ecobee, "request_tokens_web", rejected)
    res = await ecobee.start_login(EMAIL, PASSWORD)
    assert res == {"status": "error", "error": "ecobee did not accept that email and password."}

    def offline(self):
        raise EcobeeAuthUnknownError("Failed to start ecobee Auth0 login: connection refused")

    monkeypatch.setattr(pyecobee.Ecobee, "request_tokens_web", offline)
    res = await ecobee.start_login(EMAIL, PASSWORD)
    assert res["status"] == "error" and "CLIMATE_ECOBEE_WEB_CLIENT_ID" in res["error"]
    assert get_secret(db, ecobee.REFRESH_SECRET) is None
    assert (await ecobee.start_login("", PASSWORD))["status"] == "error"


async def test_missing_secret_key_fails_before_contacting_ecobee(db, monkeypatch):
    from climate.config import get_settings

    called = []
    monkeypatch.setattr(pyecobee.Ecobee, "request_tokens_web", lambda self: called.append(1))
    monkeypatch.setenv("CLIMATE_SECRET_KEY", "")
    get_settings.cache_clear()
    res = await ecobee.start_login(EMAIL, PASSWORD)
    assert res["status"] == "error" and "CLIMATE_SECRET_KEY" in res["error"]
    assert called == []


async def test_sign_out(db, monkeypatch):
    put_secret(db, ecobee.REFRESH_SECRET, REFRESH)
    put_setting(db, ecobee.STATUS_KEY, {"signed_in_at": utcnow().isoformat(), "last_error": None})
    db.commit()
    _fake_library(monkeypatch, mfa_type="otp")
    await ecobee.start_login(EMAIL, PASSWORD)
    await ecobee.sign_out()
    db.rollback()
    assert get_secret(db, ecobee.REFRESH_SECRET) is None
    assert get_raw(db, ecobee.STATUS_KEY) is None
    assert ecobee.mfa_pending() == (False, None)
    assert ecobee.login_status(db)["signed_in"] is False
