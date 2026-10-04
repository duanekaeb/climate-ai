"""Encrypted secrets at rest (Fernet), stored in the ``secrets`` table.

Keys in use:
- ``ecobee_refresh_token`` – the newest ecobee refresh token (rotated on every refresh).
- ``homekit_pairing:<alias>`` – JSON pairing_data for one thermostat (holds the controller's
  Ed25519 private key ``iOSDeviceLTSK``).

Passwords are never stored here (the ecobee password is used once during sign-in and dropped).
"""

from __future__ import annotations

import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from climate.config import get_settings
from climate.store.orm import SecretRow


class SecretsUnavailable(RuntimeError):
    """CLIMATE_SECRET_KEY is missing or wrong."""


def _fernet() -> Fernet:
    key = get_settings().secret_key
    if not key:
        raise SecretsUnavailable("CLIMATE_SECRET_KEY is not set; secrets cannot be stored.")
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise SecretsUnavailable("CLIMATE_SECRET_KEY is not a valid Fernet key.") from exc


def put_secret(session: Session, key: str, value: str) -> None:
    token = _fernet().encrypt(value.encode())
    stmt = insert(SecretRow).values(key=key, ciphertext=token)
    stmt = stmt.on_conflict_do_update(
        index_elements=[SecretRow.key], set_={"ciphertext": token, "updated_at": func.now()}
    )
    session.execute(stmt)


def get_secret(session: Session, key: str) -> str | None:
    row = session.execute(select(SecretRow.ciphertext).where(SecretRow.key == key)).scalar_one_or_none()
    if row is None:
        return None
    try:
        return _fernet().decrypt(bytes(row)).decode()
    except InvalidToken as exc:
        raise SecretsUnavailable(f"secret {key!r} cannot be decrypted with the current key") from exc


def put_secret_json(session: Session, key: str, value: Any) -> None:
    put_secret(session, key, json.dumps(value))


def get_secret_json(session: Session, key: str) -> Any | None:
    raw = get_secret(session, key)
    return None if raw is None else json.loads(raw)


def delete_secret(session: Session, key: str) -> None:
    session.execute(delete(SecretRow).where(SecretRow.key == key))


def list_secret_keys(session: Session, prefix: str = "") -> list[str]:
    rows = session.execute(select(SecretRow.key).where(SecretRow.key.startswith(prefix))).scalars()
    return sorted(rows)
