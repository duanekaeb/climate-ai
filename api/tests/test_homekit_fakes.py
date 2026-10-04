"""Fakes shaped like aiohomekit 4.0.1 (Controller, IpDiscovery, IpPairing) for the HomeKit tests.

No network, no mDNS. A ``FakeDevice`` is one ecobee thermostat with its SmartSensors; its
``values`` are the device state that gets/puts act on. Behaviour mirrors what the verified
notes describe: put_characteristics returns an entry for EVERY row (status 0 on success),
pairing objects echo writes optimistically to listeners, a paired device refuses pair-setup
with UnavailableError, a wrong code raises AuthenticationError.

Addresses mirror aiohomekit 4.0.1 (tests/test_homekit_dhcp.py checks the real library does
this): a ``FakeDevice`` sits at ``address``:``port``; the controller holds what mDNS last told it
(``known``, stale after a silent ``move``); a loaded pairing connects to the addresses of its
mDNS description, or to the pairing data's ``AccessoryIP(s)`` / ``AccessoryPort`` before it has
one; a description update redirects a pairing that is not connected and reconnects it at once
(listeners get ``{}``), while a pairing still connected to an address the device left only
fails when its next request times out. A ``stalled`` pairing is one whose reconnect loop is busy
trying an address the device left: an update cannot cut that short, and its requests time out
waiting for the connection. ``FakeMdns`` stands in for zeroconf: a browser signal
fired on announcements / goodbyes, and ``async_send`` that records queries and lets devices that
``answers`` announce themselves in reply.

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
from zeroconf import ServiceStateChange

from climate.sources.homekit import CHAR_TYPES, FORBIDDEN_WRITE_TYPES

HAP_TCP = "_hap._tcp.local."

HOME_TARGET_HEAT = "E4489BBC-5227-4569-93E5-B345E3E5508F"
HOME_TARGET_COOL = "7D381BAA-20F9-40E5-9BE9-AEB92D4BECEF"
SLEEP_TARGET_HEAT = "05B97374-6DC0-439B-A0FA-CA33F612D425"
SLEEP_TARGET_COOL = "A251F6E7-AC46-4190-9C5D-3D06277BDF9F"
AWAY_TARGET_HEAT = "73AAB542-892A-4439-879A-D2A883724B69"
AWAY_TARGET_COOL = "5DA985F0-898A-4850-B987-B76C6C78D670"
# comfort targets in °C: home 68/76 °F, sleep 67/74 °F, away 62/80 °F
COMFORT_TARGETS_C = {
    "home": (HOME_TARGET_HEAT, 20.0, HOME_TARGET_COOL, 24.4),
    "sleep": (SLEEP_TARGET_HEAT, 19.4, SLEEP_TARGET_COOL, 23.3),
    "away": (AWAY_TARGET_HEAT, 16.7, AWAY_TARGET_COOL, 26.7),
}
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
        address: str = "192.168.1.50",
        port: int = 51826,
    ) -> None:
        self.id = device_id
        self.name = name
        self.model = model
        self.code = code
        self.status_flags = status_flags
        self.paired = not (status_flags & 1)
        self.visible = True  # the controller has an mDNS record of it
        self.address = address  # where the device is now (DHCP may move it)
        self.port = port
        self.config_num = 3
        self.reachable = True  # accepts HAP connections at its address
        self.answers = True  # answers mDNS queries
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
                ("HEATING_COOLING_TARGET", [PR, PW, EV], 3),
                ("TEMPERATURE_HEATING_THRESHOLD", [PR, PW, EV], 20.0),
                ("TEMPERATURE_COOLING_THRESHOLD", [PR, PW, EV], 24.4),
                *[(t, [PR, PW], v) for heat_t, heat_c, cool_t, cool_c in COMFORT_TARGETS_C.values()
                  for t, v in ((heat_t, heat_c), (cool_t, cool_c))],
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
            # the climate's own targets become the active setpoints
            heat_t, _h, cool_t, _c = COMFORT_TARGETS_C[["home", "sleep", "away"][value]]
            self.set(1, "TEMPERATURE_HEATING_THRESHOLD", self.get(1, heat_t))
            self.set(1, "TEMPERATURE_COOLING_THRESHOLD", self.get(1, cool_t))
        elif ctype == CHAR_TYPES["VENDOR_ECOBEE_CLEAR_HOLD"]:
            if value:
                # back to the schedule, which runs Home here: Home's own targets become the
                # active setpoints (as with a hold of it)
                self.set(1, "VENDOR_ECOBEE_CURRENT_MODE", 0)
                self.set(1, "VENDOR_ECOBEE_TIMESTAMP", f"2026-10-04T22:00:00-05:00{self.suffix}")
                self.set(1, "TEMPERATURE_HEATING_THRESHOLD", self.get(1, HOME_TARGET_HEAT))
                self.set(1, "TEMPERATURE_COOLING_THRESHOLD", self.get(1, HOME_TARGET_COOL))
        else:
            self.values[(aid, iid)] = value

    @property
    def service(self) -> str:
        return f"{self.name}.{HAP_TCP}"

    @property
    def host(self) -> str:
        return f"ecobee-{self.id.replace(':', '')}.local."

    def pairing_data(self) -> dict[str, Any]:
        """What aiohomekit's finish_pairing returns: the keys plus the address of the moment."""
        return {
            "AccessoryPairingID": self.id.upper(),
            "AccessoryLTPK": "aa" * 32,
            "iOSPairingId": "11111111-2222-3333-4444-555555555555",
            "iOSDeviceLTSK": SECRET_LTSK,
            "iOSDeviceLTPK": "bb" * 32,
            "AccessoryIP": self.address,
            "AccessoryIPs": [self.address],
            "AccessoryPort": self.port,
            "Connection": "IP",
        }

    def description(self) -> SimpleNamespace:
        """What an mDNS announcement of the device says right now (aiohomekit's HomeKitService)."""
        return SimpleNamespace(id=self.id, name=self.name, model=self.model, category=9, address=self.address,
                               addresses=[self.address], port=self.port, status_flags=self.status_flags,
                               config_num=self.config_num, type=HAP_TCP)


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
        self.description: SimpleNamespace | None = None  # set by the controller from mDNS
        self.connected_host: str | None = None
        self.connection = self  # aiohomekit: pairing.connection.connected_host
        self.connects: list[tuple[list[str], int]] = []  # (hosts, port) of every connection attempt
        self.timeouts = 0  # requests lost on a connection to an address the device had left
        # aiohomekit's reconnect loop is mid-attempt at an address the device left, then backs off
        # (up to 60 s): reconnect_soon() is a no-op meanwhile, and requests time out waiting
        self.stalled = False

    @property
    def hosts(self) -> list[str]:
        """aiohomekit: the mDNS description's addresses once there is one, else the pairing data's."""
        if self.description is not None:
            return list(self.description.addresses)
        return list(self.pairing_data.get("AccessoryIPs") or [self.pairing_data["AccessoryIP"]])

    @property
    def port(self) -> int:
        return int(self.description.port if self.description is not None else self.pairing_data["AccessoryPort"])

    @property
    def is_connected(self) -> bool:
        return self.connected_host is not None

    def _connect(self) -> bool:
        hosts, port = self.hosts, self.port
        self.connects.append((hosts, port))
        dev = self.device
        if dev.reachable and dev.address in hosts and dev.port == port:
            self.connected_host = dev.address
            return True
        return False

    def _async_description_update(self, description: SimpleNamespace) -> None:
        """aiohomekit IpPairing: new mDNS data; reconnect_soon() when not connected, and
        connection_made() tells the listeners with an empty event."""
        self.description = description
        if self.shut_down or self.is_connected or self.stalled:
            return
        if self._connect():
            self.push({})

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
            if self.stalled:
                raise AccessoryDisconnectedError(f"Timeout while waiting for connection to device {self.hosts}")
            dev = self.device
            if self.connected_host is not None and (self.connected_host != dev.address or not dev.reachable):
                self.connected_host = None  # the device left that address: the request times out
                self.timeouts += 1
                raise AccessoryDisconnectedError("Timeout while waiting for response")
            if self.connected_host is None and not self._connect():
                raise AccessoryDisconnectedError(f"Error while connecting to device {self.hosts}:{self.port}")
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
        self.connected_host = None

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
        self.description = controller.description_of(device)
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


class FakeSignal:
    """zeroconf's SignalRegistrationInterface + fire (handlers get keyword arguments)."""

    def __init__(self) -> None:
        self.handlers: list[Callable[..., None]] = []

    def register_handler(self, handler: Callable[..., None]) -> FakeSignal:
        self.handlers.append(handler)
        return self

    def unregister_handler(self, handler: Callable[..., None]) -> FakeSignal:
        self.handlers.remove(handler)
        return self

    def fire(self, **kwargs: Any) -> None:
        for h in self.handlers[:]:
            h(**kwargs)


class FakeMdns:
    """Stands in for AsyncZeroconf (``.zeroconf`` is itself) and its HAP browser."""

    def __init__(self, controller: FakeController) -> None:
        self.controller = controller
        self.zeroconf = self
        self.cache = self
        self.browser = SimpleNamespace(types=[HAP_TCP, "_hap._udp.local."], service_state_changed=FakeSignal(),
                                       async_cancel=self._noop)
        self.sent: list[list[tuple[str, int]]] = []  # questions (name, type) of every query sent
        self.closed = False

    @staticmethod
    async def _noop() -> None:
        return None

    async def async_close(self) -> None:
        self.closed = True

    def get_by_details(self, name: str, type_: int, class_: int) -> SimpleNamespace | None:
        """zeroconf DNSCache: the SRV record of a device the controller has heard of."""
        for dev in self.controller.devices.values():
            if type_ == 33 and class_ == 1 and dev.visible and name.lower() == dev.service.lower():
                return SimpleNamespace(name=dev.service, server=dev.host)
        return None

    def async_send(self, out: Any, addr: str | None = None, port: int = 5353) -> None:
        """A query goes out; every device it asks about that ``answers`` replies a moment later
        (an announcement of where it is now)."""
        assert out.is_query() and out.packets(), "must be a query zeroconf can encode"
        questions = [(q.name, q.type) for q in out.questions]
        self.sent.append(questions)
        names = {n.lower() for n, _ in questions}
        loop = asyncio.get_running_loop()
        for dev in self.controller.devices.values():
            if dev.answers and names & {dev.service.lower(), dev.host.lower(), HAP_TCP}:
                loop.call_soon(self.controller.announce, dev)

    def fire(self, dev: FakeDevice, change: ServiceStateChange) -> None:
        self.browser.service_state_changed.fire(zeroconf=self, service_type=HAP_TCP, name=dev.service,
                                                state_change=change)


class FakeController:
    """Shaped like aiohomekit.Controller (top level, with the IP transport's dict folded in)."""

    def __init__(self, devices: list[FakeDevice]) -> None:
        self.devices = {d.id: d for d in devices}
        self.pairings: dict[str, FakePairing] = {}
        self.aliases: dict[str, FakePairing] = {}
        self.loaded: list[FakePairing] = []
        self.started = False
        self.stopped = False
        self.known: dict[str, SimpleNamespace] = {}  # mDNS descriptions frozen by a silent move
        self.mdns = FakeMdns(self)

    def zeroconf(self) -> tuple[FakeMdns, Any]:
        """For HomekitBridge(zeroconf_factory=...)."""
        return self.mdns, self.mdns.browser

    def description_of(self, dev: FakeDevice) -> SimpleNamespace:
        """What the controller believes (aiohomekit's discovery): the last announcement heard."""
        return self.known.get(dev.id) or dev.description()

    def announce(self, dev: FakeDevice) -> None:
        """The device announces itself and the browser hears it: the discovery and the loaded
        pairing get the new description (aiohomekit), then the browser's handlers fire."""
        dev.visible = True
        desc = self.known[dev.id] = dev.description()
        for pairing in list(self.aliases.values()):
            if pairing.device is dev:
                pairing._async_description_update(desc)
        self.mdns.fire(dev, ServiceStateChange.Updated)

    def move(self, dev: FakeDevice, address: str, *, port: int | None = None, announce: bool = True,
             reboot: bool = True) -> None:
        """DHCP gives the device a new address. ``reboot``: it dropped off the network to get it,
        so connections to the old address are dead (refused). ``announce=False``: nobody hears
        the announcement (lost multicast); the controller keeps the old description."""
        self.known[dev.id] = self.description_of(dev)
        dev.address = address
        if port is not None:
            dev.port = port
        if reboot:
            for pairing in self.aliases.values():
                if pairing.device is dev:
                    pairing.connected_host = None
        if announce:
            self.announce(dev)

    def goodbye(self, dev: FakeDevice) -> None:
        """The device's service goes away (goodbye, or its records expired). Like aiohomekit,
        the controller keeps its discovery; only the browser reports the removal."""
        self.mdns.fire(dev, ServiceStateChange.Removed)

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
        if dev.visible:  # IpController.load_pairing applies the discovery it already has
            pairing.description = self.description_of(dev)
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
    "AWAY_TARGET_COOL",
    "AWAY_TARGET_HEAT",
    "COMFORT_TARGETS_C",
    "FORBIDDEN_WRITE_TYPES",
    "HOME_TARGET_COOL",
    "HOME_TARGET_HEAT",
    "SECRET_LTSK",
    "FakeController",
    "FakeDevice",
    "FakeDiscovery",
    "FakeMdns",
    "FakePairing",
    "disconnected",
]
