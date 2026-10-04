"""Pair one ecobee thermostat with this server over HomeKit, from the server's shell.

Fallback for the web app's Setup > HomeKit flow. Run it on the HomeKit host (it needs mDNS on
the thermostats' LAN), with the same environment as the homekit service (CLIMATE_DATABASE_URL,
CLIMATE_SECRET_KEY, CLIMATE_HOMEKIT_STATE_DIR):

    python scripts/pair_ecobee.py hallway aa:bb:cc:dd:ee:ff --unit main

It finds the thermostat, starts pair-setup (the thermostat then shows a fresh 8-digit code),
asks for that code (again until it looks like 123-45-678), and saves the pairing ENCRYPTED in
the database (secret 'homekit_pairing:<alias>') before closing the connection. It never writes
a plain pairings.json and never uses ``aiohomekitctl pair`` (which loses the keys). The
homekit service picks the pairing up on its next pass.

If the thermostat is still paired to Apple Home (or anything else), choose Disconnect from
HomeKit on it first. If no code appears, open the thermostat's HomeKit menu.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import re
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

if importlib.util.find_spec("climate") is None:  # source checkout without the package installed
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from climate.collector.homekit_service import SessionFactory, save_pairing, secret_key
from climate.house import UNIT_KEYS
from climate.sources.homekit import (
    BAD_CODE_FORMAT_MSG,
    CODE_RE,
    HomekitBridge,
    HomekitError,
)
from climate.store import secrets
from climate.store.db import session_scope
from climate.store.orm import HomekitDevice, Unit
from climate.timeutil import utcnow

ALIAS_RE = re.compile(r"^[a-z][a-z0-9_]{1,23}$")  # same rule as the web API
DEVICE_ID_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
PROMPT = "Code shown on the thermostat (XXX-XX-XXX): "


class PreflightError(Exception):
    pass


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Pair an ecobee thermostat over HomeKit and save it encrypted.")
    p.add_argument("alias", help="short name for this pairing, e.g. hallway (lower case, digits, _)")
    p.add_argument("device_id", help="HomeKit device id from discovery, e.g. aa:bb:cc:dd:ee:ff")
    p.add_argument("--unit", required=True, choices=UNIT_KEYS, help="which unit this thermostat runs")
    args = p.parse_args(argv)
    args.device_id = args.device_id.strip().lower()
    if not ALIAS_RE.fullmatch(args.alias):
        p.error("alias must be 2-24 characters: a lower-case letter, then letters, digits or _")
    if not DEVICE_ID_RE.fullmatch(args.device_id):
        p.error("device id must look like aa:bb:cc:dd:ee:ff")
    return args


def preflight(session: Any, alias: str, device_id: str, unit_key: str) -> None:
    """Refuse before touching the thermostat if the result could not be saved."""
    if session.get(Unit, unit_key) is None:
        raise PreflightError(f"unit {unit_key!r} does not exist (has the app run its migrations?)")
    probe = session.begin_nested()
    try:
        secrets.put_secret(session, secret_key("__preflight__"), "probe")
    except secrets.SecretsUnavailable as exc:
        raise PreflightError(f"{exc} Set CLIMATE_SECRET_KEY to the app's key.") from exc
    finally:
        probe.rollback()
    if secret_key(alias) in secrets.list_secret_keys(session, secret_key(alias)):
        raise PreflightError(f"a pairing named {alias!r} is already saved; unpair it first or pick another alias")
    other = session.execute(
        select(HomekitDevice.device_id).where(HomekitDevice.alias == alias, HomekitDevice.device_id != device_id)
    ).scalar_one_or_none()
    if other is not None:
        raise PreflightError(f"alias {alias!r} is already used by device {other}")
    row = session.get(HomekitDevice, device_id)
    if row is not None and row.pairing_state == "paired":
        raise PreflightError(f"{device_id} is already paired here as {row.alias!r}; unpair it first")


async def run(
    args: argparse.Namespace,
    *,
    bridge: HomekitBridge | None = None,
    input_fn: Callable[[str], str] = input,
    session_factory: SessionFactory = session_scope,
    out: Callable[[str], None] = print,
) -> int:
    try:
        with session_factory() as s:
            preflight(s, args.alias, args.device_id, args.unit)
    except PreflightError as exc:
        out(f"error: {exc}")
        return 2

    bridge = bridge or HomekitBridge()
    await bridge.start()
    try:
        out(f"Looking for {args.device_id} on the network (up to 30 s)...")
        device = await bridge.find(args.device_id)
        out(f"Found {device.name!r} model={device.model} at {device.address}:{device.port}")
        if not device.unpaired:
            out("Note: it advertises as already paired; this fails unless it was disconnected from HomeKit.")
        await bridge.begin_pairing(args.device_id, args.alias)
        out("The thermostat should now show an 8-digit code (if not, open its HomeKit menu).")
        while True:
            code = (await asyncio.to_thread(input_fn, PROMPT)).strip()
            if CODE_RE.fullmatch(code):
                break
            out(BAD_CODE_FORMAT_MSG)

        def save_sync(data: dict[str, Any]) -> None:
            with session_factory() as s:
                save_pairing(s, args.device_id, args.alias, args.unit, data, device, utcnow())

        async def save(data: dict[str, Any]) -> None:
            await asyncio.to_thread(save_sync, data)

        await bridge.finish_pairing(args.device_id, code, save=save)
        out(f"Paired {args.alias!r} ({args.device_id}) for unit {args.unit}; the keys are saved encrypted in the database.")
        out("The homekit service loads it on its next pass.")
        return 0
    except HomekitError as exc:
        out(f"error: {exc}")
        return 1
    except (EOFError, KeyboardInterrupt):
        out("cancelled")
        return 130
    finally:
        await bridge.stop()


async def main(argv: Sequence[str] | None = None) -> int:
    return await run(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
