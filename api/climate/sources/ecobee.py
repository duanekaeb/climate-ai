"""The ONE ecobee cloud adapter (CLAUDE.md). Everything ecobee-auth lives behind this file.

Sign-in uses python-ecobee-api 0.4.x account sign-in (Auth0 PKCE web flow, TOTP/SMS MFA)
only for login + token refresh; every data call is made here with the bearer token:
/1/thermostatSummary (every >= 3 min), /1/thermostat (on revision change),
/1/runtimeReport (<= 31 days per call, one at a time), and setHold/resumeProgram writes,
each read back (the library swallows HTTP errors). Rules:
- The web client id comes from settings (``CLIMATE_ECOBEE_WEB_CLIENT_ID``) and is patched into
  pyecobee before use; the account route is unofficial and has changed before.
- Persist the NEWEST refresh token (encrypted, secrets 'ecobee_refresh_token') after every
  refresh; never store the password; only the worker refreshes (the API process only signs in).
- Keep the 'pyecobee' logger at INFO or above (it logs tokens at DEBUG).
- holdType 'holdHours' with holdHours 1-2; temperatures rounded to 0.5°F then x10 ints.
- Program edits start from a fresh GET and a revision check.
"""

from __future__ import annotations

from datetime import datetime

from climate.sources.base import HoldRequest, RuntimeInterval, SourceHealth, UnitSnapshot, WriteResult


class EcobeeAuthError(RuntimeError):
    """Sign-in or refresh failed; the owner must sign in again."""


class EcobeeCloud:
    kind = "ecobee"

    @classmethod
    def from_settings(cls) -> EcobeeCloud:
        raise NotImplementedError

    async def poll_revisions(self) -> dict[str, str]:
        raise NotImplementedError

    async def fetch_snapshots(self, unit_keys: list[str] | None = None) -> list[UnitSnapshot]:
        raise NotImplementedError

    async def fetch_runtime(self, start: datetime, end: datetime) -> list[RuntimeInterval]:
        raise NotImplementedError

    async def set_hold(self, req: HoldRequest) -> WriteResult:
        raise NotImplementedError

    async def resume_program(self, unit_key: str, reason: str) -> WriteResult:
        raise NotImplementedError

    async def update_sensor_sets(self, unit_key: str, sets: dict[str, list[str]], reason: str) -> WriteResult:
        """Read-modify-write the program's comfort-setting sensor participation (fresh GET +
        revision check). Keep Home's set current: temperature holds use Home's sensors."""
        raise NotImplementedError

    async def ensure_settings(self) -> list[WriteResult]:
        """autoAway=false and followMeComfort=false on every unit (verified daily)."""
        raise NotImplementedError

    async def health(self) -> SourceHealth:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


# --- sign-in (runs in the API process; holds the MFA challenge in memory) -------------


async def start_login(email: str, password: str) -> dict:
    """Return {'status': 'signed_in'} after storing the refresh token, or
    {'status': 'mfa_required', 'mfa_type': 'otp'|'sms'} keeping the challenge in memory for
    10 minutes, or {'status': 'error', 'error': '...'}. The password is never stored."""
    raise NotImplementedError


async def submit_mfa(code: str) -> dict:
    raise NotImplementedError


def mfa_pending() -> tuple[bool, str | None]:
    raise NotImplementedError


async def sign_out() -> None:
    raise NotImplementedError
