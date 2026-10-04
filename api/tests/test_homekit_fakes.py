"""Fakes shaped like aiohomekit 4.0.1 (Controller, IpDiscovery, IpPairing) for the HomeKit tests.

No network, no mDNS. A ``FakeDevice`` is one ecobee thermostat with its SmartSensors; its
``values`` are the device state that gets/puts act on. Behaviour mirrors what the verified
notes describe: put_characteristics returns an entry for EVERY row (status 0 on success),
pairing objects echo writes optimistically to listeners, a paired device refuses pair-setup
with UnavailableError, a wrong code raises AuthenticationError.

(This module holds helpers only; pytest collects no tests from it.)
"""

from __future__ import annotations

import asyncio
import copy
import itertools
from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("aiohomekit")

from aiohomekit.exceptions import (
    AccessoryDisconnectedError,
    AccessoryNotFoundError,
    AuthenticationError,
    UnavailableError,
)

from climate.sources.homekit import CHAR_TYPES, FORBIDDEN_WRITE_TYPES

HOME_TARGET_HEAT = "E4489BBC-5227-4569-93E5-B345E3E5508F"
SVC_INFO = "3E"  # short form on purpose: the adapter must normalize it
SVC_THERMOSTAT = "0000004A-0000-1000-8000-0026BB765291"
SVC_TEMP = "0000008A-0000-1000-8000-0026BB765291"
SVC_OCC = "00000086-0000-1000-8000-0026BB765291"
SVC_MOTION = "00000085-0000-1000-8000-0026BB765291"
SVC_BATTERY = "00000096-0000-1000-8000-0026BB765291"
INFO_NAME, INFO_MODEL, INFO_FW = "23", "21", "52"
SECRET_LTSK = "f00dfacecafebeef" * 4  # stands in for iOSDeviceLTSK; tests assert it is never logged

PR, PW, EV = "pr", "pw", "ev"


class FakeDevice:
    """One thermostat (aid 1) plus remote sensors, with mutable characteristic values."""

    def __init__(
        self,
        device_id: str = "aa:bb:cc:dd:ee:01",
        name: str = "Hallway",
        model: str = "ECB501",
        sensors: list[tuple[int, str, str]] | None = None,
        *,
        suffix: str = "R",
        vendor_ev: bool = False,
        code: str = "123-45-678",
        status_flags: int = 1,
    ) -> None:
        self.id = device_id
        self.name = name
        self.model = model
        self.code = code
        self.status_flags = status_flags
        self.paired = not (status_flags & 1)
        self.visible = True
        self.suffix = suffix
        self.start_error: BaseException | None = None
        self.accessories: list[dict[str, Any]] = []
        self.values: dict[tuple[int, int], Any] = {}
        self.types: dict[tuple[int, int], str] = {}
        self.iids: dict[tuple[int, str], int] = {}
        self.writes: list[tuple[int, int, Any]] = []
        self.on_temp_close: Callable[[], None] | None = None
        self.log: list[str] = []
        vend = [PR, EV] if vendor_ev else [PR]
        self._add(1, name, model, "4.7.340214", [
            (SVC_THERMOSTAT, [
                ("TEMPERATURE_CURRENT", [PR, EV], 22.0),
                ("RELATIVE_HUMIDITY_CURRENT", [PR, EV], 45.0),
                ("HEATING_COOLING_CURRENT", [PR, EV], 2),
                ("VENDOR_ECOBEE_CURRENT_MODE", vend, 0),
                ("VENDOR_ECOBEE_TIMESTAMP", [PR, PW] + ([EV] if vendor_ev else []), f"2026-10-04T22:00:00-05:00{suffix}"),
                ("VENDOR_ECOBEE_SET_HOLD_SCHEDULE", [PW], None),
                ("VENDOR_ECOBEE_CLEAR_HOLD", [PW], None),
                ("VENDOR_ECOBEE_EQUIPMENT_RUNNING", vend, 2),
                (HOME_TARGET_HEAT, [PR, PW], 20.0),
            ]),
            (SVC_OCC, [
                ("OCCUPANCY_DETECTED", [PR, EV], 1),
                ("VENDOR_ECOBEE_OCCUPANCY_LAST_ACTIVATION", vend, 30),
            ]),
        ])
        for aid, sname, smodel in sensors if sensors is not None else [(4295608971, "Kitchen", "EBRSE4")]:
            self._add(aid, sname, smodel, "1.0", [
                (SVC_TEMP, [("TEMPERATURE_CURRENT", [PR, EV], 21.5), ("STATUS_ACTIVE", [PR, EV], True),
                            ("STATUS_LO_BATT", [PR, EV], 0)]),
                (SVC_OCC, [("OCCUPANCY_DETECTED", [PR, EV], 0),
                           ("VENDOR_ECOBEE_OCCUPANCY_LAST_ACTIVATION", vend, -1)]),
                (SVC_MOTION, [("MOTION_DETECTED", [PR, EV], False),
                              ("VENDOR_ECOBEE_MOTION_LAST_ACTIVATION", vend, 600)]),
                (SVC_BATTERY, [("BATTERY_LEVEL", [PR, EV], 90)]),
            ])

    def _add(self, aid: int, name: str, model: str, fw: str, services: list[tuple[str, list]]) -> None:
        counter = itertools.count(1)
        svcs: list[dict[str, Any]] = []
        info = {"type": SVC_INFO, "iid": next(counter), "characteristics": []}
        for t, v in ((INFO_NAME, name), (INFO_MODEL, model), (INFO_FW, fw)):
            info["characteristics"].append({"type": t, "iid": next(counter), "perms": [PR], "value": v})
        svcs.append(info)
        for stype, chars in services:
            svc = {"type": stype, "iid": next(counter), "characteristics": []}
            for key, perms, value in chars:
                ctype = CHAR_TYPES.get(key, key)
                iid = next(counter)
                svc["characteristics"].append({"type": ctype, "iid": iid, "perms": list(perms), "value": value})
                self.types[(aid, iid)] = ctype
                self.values[(aid, iid)] = value
                self.iids.setdefault((aid, key), iid)
            svcs.append(svc)
        self.accessories.append({"aid": aid, "services": svcs})

    def k(self, aid: int, key: str) -> tuple[int, int]:
        return (aid, self.iids[(aid, key)])

    def set(self, aid: int, key: str, value: Any) -> None:
        self.values[self.k(aid, key)] = value

    def get(self, aid: int, key: str) -> Any:
        return self.values[self.k(aid, key)]

    def apply_write(self, aid: int, iid: int, value: Any) -> None:
        self.writes.append((aid, iid, value))
        ctype = self.types[(aid, iid)]
        if ctype == CHAR_TYPES["VENDOR_ECOBEE_SET_HOLD_SCHEDULE"]:
            self.set(1, "VENDOR_ECOBEE_CURRENT_MODE", value)
        elif ctype == CHAR_TYPES["VENDOR_ECOBEE_CLEAR_HOLD"]:
            if value:
                self.set(1, "VENDOR_ECOBEE_CURRENT_MODE", 0)
                self.set(1, "VENDOR_ECOBEE_TIMESTAMP", f"2026-10-04T22:00:00-05:00{self.suffix}")
        else:
            self.values[(aid, iid)] = value

    def pairing_data(self) -> dict[str, Any]:
        return {
            "AccessoryPairingID": self.id.upper(),
            "AccessoryLTPK": "aa" * 32,
            "iOSPairingId": "11111111-2222-3333-4444-555555555555",
            "iOSDeviceLTSK": SECRET_LTSK,
            "iOSDeviceLTPK": "bb" * 32,
            "AccessoryIP": "192.168.1.50",
            "AccessoryIPs": ["192.168.1.50"],
            "AccessoryPort": 51826,
            "Connection": "IP",
        }

    def description(self) -> SimpleNamespace:
        return SimpleNamespace(id=self.id, name=self.name, model=self.model, category=9, address="192.168.1.50",
                               port=51826, status_flags=self.status_flags, config_num=3)


class FakePairing:
    def __init__(self, controller: FakeController, device: FakeDevice, pairing_data: dict[str, Any]) -> None:
        self.controller = controller
        self.device = device
        self.pairing_data = pairing_data
        self.supports_subscribe = True
        self.listeners: set[Callable[[dict], None]] = set()
        self.config_listeners: set[Callable[[int], None]] = set()
        self.calls: list[tuple[str, Any]] = []
        self.fail_next: list[BaseException] = []
        self.fail_always: BaseException | None = None
        self.put_status: dict[tuple[int, int], int] = {}
        self.missing: set[tuple[int, int]] = set()
        self.reject_subscribe: set[tuple[int, int]] = set()
        self.disconnect_on_subscribe = False
        self.in_flight = 0
        self.max_in_flight = 0
        self.closed = False
        self.shut_down = False
        self.temporary = False

    async def _request(self, kind: str, payload: Any) -> None:
        self.calls.append((kind, payload))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0)
            if self.fail_always is not None:
                raise self.fail_always
            if self.fail_next:
                raise self.fail_next.pop(0)
        finally:
            self.in_flight -= 1

    async def list_accessories_and_characteristics(self) -> list[dict[str, Any]]:
        await self._request("list", None)
        return copy.deepcopy(self.device.accessories)

    async def get_characteristics(self, chars: Any) -> dict[tuple[int, int], dict[str, Any]]:
        chars = list(chars)
        assert len(chars) <= 49, "batches must be <= 49"
        await self._request("get", chars)
        out: dict[tuple[int, int], dict[str, Any]] = {}
        for k in chars:
            if k in self.missing:
                out[k] = {"status": -70402, "description": "Unable to communicate"}
            else:
                out[k] = {"value": self.device.values.get(k)}
        return out

    async def put_characteristics(self, rows: Any) -> dict[tuple[int, int], dict[str, Any]]:
        rows = list(rows)
        await self._request("put", rows)
        res: dict[tuple[int, int], dict[str, Any]] = {}
        echo: dict[tuple[int, int], dict[str, Any]] = {}
        for aid, iid, value in rows:
            status = self.put_status.get((aid, iid), 0)
            # aiohomekit 4.0.1 returns an entry for every row, successes included (status 0)
            res[(aid, iid)] = {"status": status, "description": "ok" if status == 0 else "rejected"}
            if status == 0:
                self.device.apply_write(aid, iid, value)
                if "pr" in self._perms(aid, iid):
                    echo[(aid, iid)] = {"value": value}
        if echo:
            self.push(echo)  # the optimistic echo the library sends to listeners
        return res

    def _perms(self, aid: int, iid: int) -> list[str]:
        for acc in self.device.accessories:
            if acc["aid"] != aid:
                continue
            for svc in acc["services"]:
                for ch in svc["characteristics"]:
                    if ch["iid"] == iid:
                        return ch["perms"]
        return []

    async def subscribe(self, chars: Any) -> dict[tuple[int, int], dict[str, Any]] | None:
        chars = list(chars)
        if not self.supports_subscribe:
            return None
        await self._request("subscribe", chars)
        if self.disconnect_on_subscribe:
            self.supports_subscribe = False
            return {}
        return {k: {"status": -70409, "description": "no events"} for k in chars if k in self.reject_subscribe}

    def dispatcher_connect(self, cb: Callable[[dict], None]) -> Callable[[], None]:
        self.listeners.add(cb)
        return lambda: self.listeners.discard(cb)

    def dispatcher_connect_config_changed(self, cb: Callable[[int], None]) -> Callable[[], None]:
        self.config_listeners.add(cb)
        return lambda: self.config_listeners.discard(cb)

    def push(self, event: dict) -> None:
        for cb in list(self.listeners):
            cb(event)

    async def close(self) -> None:
        if self.temporary:
            self.device.log.append("close_temporary")
            if self.device.on_temp_close is not None:
                self.device.on_temp_close()
        self.closed = True

    async def shutdown(self) -> None:
        self.shut_down = True
        await self.close()

    async def remove_pairing(self, pairing_id: str) -> bool:
        await self._request("remove_pairing", pairing_id)
        self.device.paired = False
        self.device.status_flags = 1
        self.device.log.append("removed_on_device")
        return True


class FakeDiscovery:
    def __init__(self, controller: FakeController, device: FakeDevice) -> None:
        self.controller = controller
        self.device = device
        self.description = device.description()
        self.closed = False

    async def async_start_pairing(self, alias: str) -> Callable[[str], Any]:
        dev = self.device
        if dev.start_error is not None:
            raise dev.start_error
        if dev.paired:
            raise UnavailableError("Unavailable")
        dev.log.append(f"start:{alias}")

        async def finish(pin: str) -> FakePairing:
            if pin != dev.code:
                raise AuthenticationError("Step #4: wrong pin")
            dev.paired = True
            dev.status_flags = 0
            pairing = FakePairing(self.controller, dev, dev.pairing_data())
            pairing.temporary = True
            self.controller.pairings[alias] = pairing  # like IpDiscovery: keyed by ALIAS
            dev.log.append("finished")
            return pairing

        return finish

    async def close(self) -> None:
        self.closed = True


class FakeController:
    """Shaped like aiohomekit.Controller (top level, with the IP transport's dict folded in)."""

    def __init__(self, devices: list[FakeDevice]) -> None:
        self.devices = {d.id: d for d in devices}
        self.pairings: dict[str, FakePairing] = {}
        self.aliases: dict[str, FakePairing] = {}
        self.loaded: list[FakePairing] = []
        self.started = False
        self.stopped = False

    async def async_start(self) -> None:
        self.started = True

    async def async_stop(self) -> None:
        self.stopped = True

    async def async_find(self, device_id: str, timeout: float = 10.0) -> FakeDiscovery:
        dev = self.devices.get(device_id.lower())
        if dev is None or not dev.visible:
            raise AccessoryNotFoundError(f"Accessory with device id {device_id} not found")
        return FakeDiscovery(self, dev)

    async def async_discover(self) -> AsyncIterator[FakeDiscovery]:
        for dev in self.devices.values():
            if dev.visible:
                yield FakeDiscovery(self, dev)

    def load_pairing(self, alias: str, pairing_data: dict[str, Any]) -> FakePairing:
        dev = self.devices[str(pairing_data["AccessoryPairingID"]).lower()]
        pairing = FakePairing(self, dev, pairing_data)
        self.pairings[dev.id] = pairing
        self.aliases[alias] = pairing
        self.loaded.append(pairing)
        return pairing

    async def remove_pairing(self, alias: str) -> None:
        pairing = self.aliases.pop(alias)
        self.pairings.pop(pairing.device.id, None)
        try:
            await pairing.remove_pairing(pairing.pairing_data["iOSPairingId"])
        finally:
            await pairing.shutdown()


def disconnected() -> AccessoryDisconnectedError:
    return AccessoryDisconnectedError("Timeout while waiting for connection to device")


__all__ = [
    "FORBIDDEN_WRITE_TYPES",
    "HOME_TARGET_HEAT",
    "SECRET_LTSK",
    "FakeController",
    "FakeDevice",
    "FakeDiscovery",
    "FakePairing",
    "disconnected",
]
