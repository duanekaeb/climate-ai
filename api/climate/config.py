"""Process configuration from environment variables (prefix ``CLIMATE_``).

Only deployment facts live here (database URL, keys, ports, which source to start with).
Anything the owner tunes at runtime (comfort bands, controller mode, sleep windows, location)
lives in the ``app_settings`` table; see ``climate.store.app_settings``.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CLIMATE_", env_file=None, extra="ignore")

    # --- storage -------------------------------------------------------------------------
    database_url: str = "postgresql+psycopg://climate:climate@127.0.0.1:5432/climate"

    # Fernet key (urlsafe base64, 32 bytes) that encrypts the ecobee refresh token and the
    # HomeKit pairing keys at rest. Generate with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    secret_key: str = ""
    # Signs the owner's session cookie.
    session_secret: str = ""
    # Optional: if set, this is the owner password (otherwise the first-run screen sets one).
    owner_password: str = ""
    # Bearer tokens for service callers. The agent token gets the "agent" role: read access
    # plus the gated tools (propose / sign off). It can never reach a thermostat write.
    agent_token: str = ""
    mcp_token: str = ""

    # --- runtime -------------------------------------------------------------------------
    # Initial data source on first start; afterwards app_settings["source"] wins.
    source: Literal["simulator", "ecobee"] = "simulator"
    # ecobee summary polling. ecobee asks for no faster than every 3 minutes.
    poll_seconds: int = Field(default=180, ge=180)
    # The simulator may poll faster (it is local); used only when source == simulator.
    sim_poll_seconds: int = Field(default=60, ge=5)
    # Days of synthetic history the simulator generates on first start.
    sim_backfill_days: int = Field(default=60, ge=0, le=400)
    sim_seed: int = 7

    # ecobee account sign-in uses ecobee's own web client. Configurable because the
    # unofficial account route has changed before.
    ecobee_web_client_id: str = "183eORFPlXyz9BbDZwqexHPBQoVjgadh"

    # HomeKit service state directory (charmap cache; pairing keys are stored encrypted in
    # the database, never here).
    homekit_state_dir: str = "/var/lib/climate/homekit"

    # Push notifications through ntfy (self-hosted or ntfy.sh). Empty URL disables.
    ntfy_url: str = ""
    # No default: a guessable topic on a public server leaks alerts. Push stays off until set.
    ntfy_topic: str = ""
    ntfy_token: str = ""

    # Public base URL (used in notification links), e.g. https://climate.example.home
    public_url: str = ""

    # Cookies: set Secure when served over HTTPS by the reverse proxy.
    cookie_secure: bool = False

    # --- sign-in and tokens (docs/specs/users-and-tokens.md) --------------------------------
    # Signs the 15-minute access tokens (HS256). Required outside tests: sign-in refuses to
    # run without it (scripts/bootstrap.sh generates it).
    jwt_secret: str = ""
    # HMAC key for every stored token hash (refresh tokens, API tokens). Required.
    token_pepper: str = ""
    access_ttl_minutes: int = Field(default=15, ge=1, le=60)
    refresh_ttl_days: int = Field(default=30, ge=1, le=90)
    session_max_days: int = Field(default=90, ge=1, le=365)
    reauth_window_minutes: int = Field(default=10, ge=1, le=60)
    password_min_length: int = Field(default=10, ge=8, le=128)
    # Failed sign-ins from PUBLIC addresses: after this many in the window, sign-in from the
    # internet pauses for login_pause_minutes (from home / Tailscale it always works, so
    # nobody can lock the owner out of their own house).
    public_login_max_failures: int = Field(default=20, ge=5, le=500)
    public_login_window_minutes: int = Field(default=60, ge=5, le=1440)
    login_pause_minutes: int = Field(default=15, ge=1, le=1440)
    # First-run setup (choosing the owner password from the browser) is only accepted from a
    # private address (LAN, Docker, Tailscale) unless this is true, so nobody on the internet
    # can claim the house before the owner does.
    allow_remote_setup: bool = False
    # Extra host names (comma-separated) on which first-run setup is accepted, besides IP
    # addresses, localhost, .local/.lan/.home/.home.arpa/.internal names and the public URL's host.
    setup_hosts: str = ""

    log_level: str = "INFO"
    version: str = "0.1.0"


@lru_cache
def get_settings() -> Settings:
    if os.environ.get("ANTHROPIC_API_KEY"):
        # Claude must run on the owner's subscription. The app itself never calls Claude,
        # but a stray key in this environment is a sign the agent container is misconfigured.
        import logging

        logging.getLogger("climate").warning(
            "ANTHROPIC_API_KEY is set in the app environment; remove it. Claude runs on the "
            "owner's subscription via CLAUDE_CODE_OAUTH_TOKEN in the agent service only."
        )
    return Settings()
