"""HomeKit when DHCP moves a thermostat: the HAP device id is its identity, never its IP.

Part 1 pins what the real aiohomekit 4.0.1 does (its IP transport, no network: the TCP connect
is replaced by a recorder): a saved pairing connects to the address stored in its pairing data
until mDNS describes its device id, then every attempt follows mDNS, and an update cuts the
reconnect wait short. Part 2 runs the bridge against the real library. Part 3 runs the homekit
service against the test database with the aiohomekit-shaped fakes, whose ``move`` gives a
thermostat a new address (announced, half-open or unheard) and whose ``FakeMdns`` answers mDNS
queries, and proves it reconnects and that Setup follows, without a restart.
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("aiohomekit")

from aiohomekit import Controller
from aiohomekit.characteristic_cache import CharacteristicCacheMemory
from aiohomekit.controller.abstract import TransportType
from aiohomekit.controller.ip import connection as ip_connection
from aiohomekit.controller.ip.controller import IpController
from aiohomekit.exceptions import AccessoryDisconnectedError, AuthenticationError
from zeroconf.asyncio import AsyncServiceInfo

from climate import notify
from climate.collector import ingest
from climate.collector.homekit_service import HomekitService, save_pairing
from climate.sources import homekit as hk
from climate.store.app_settings import SourceSettings, put_setting
from climate.store.db import session_scope
from tests.test_homekit_fakes import FakeController, FakeDevice, disconnected
from tests.test_homekit_service import Clock, device_row

OLD, NEW, OTHER = "192.168.1.50", "192.168.1.77", "192.168.1.60"
DEVICE_ID = "aa:bb:cc:dd:ee:01"
PAIRING_DATA = {
    "AccessoryPairingID": DEVICE_ID.upper(),
    "AccessoryLTPK": "aa" * 32,
    "iOSPairingId": "11111111-2222-3333-4444-555555555555",
    "iOSDeviceLTSK": "bb" * 32,
    "iOSDeviceLTPK": "cc" * 32,
    "AccessoryIP": OLD,  # what finish_pairing saved: the address at pairing time
    "AccessoryIPs": [OLD],
    "AccessoryPort": 51826,
    "Connection": "IP",
}


def hap_record(address: str, port: int = 51826, config_num: int = 3) -> AsyncServiceInfo:
    """A resolved mDNS record of the thermostat, as zeroconf hands it to aiohomekit."""
    return AsyncServiceInfo(
        hk.HAP_TYPES[0], f"Hallway.{hk.HAP_TYPES[0]}", port=port, server="ecobee-hallway.local.",
        addresses=[socket.inet_aton(address)],
        properties={"id": DEVICE_ID.upper(), "md": "ECB501", "c#": str(config_num), "s#": "1", "sf": "0",
                    "ci": "9", "ff": "0", "pv": "1.1"},
    )


@pytest.fixture
def connects(monkeypatch) -> list[tuple[list[str], int]]:
    """Every TCP connection attempt aiohomekit makes, as (hosts, port); all of them fail."""
    seen: list[tuple[list[str], int]] = []

    async def connect_once(self: Any) -> None:
        seen.append((list(self.hosts), self.port))
        raise AccessoryDisconnectedError("unreachable (test)")

    monkeypatch.setattr(ip_connection.HomeKitConnection, "_connect_once", connect_once)
    return seen


async def wait_for(cond, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        assert asyncio.get_running_loop().time() < deadline, "condition not met in time"
        await asyncio.sleep(0.005)


# --- 1. the real aiohomekit 4.0.1 --------------------------------------------------------


async def test_aiohomekit_uses_the_stored_address_only_until_mdns_names_the_device_id(connects):
    ctl = IpController(char_cache=CharacteristicCacheMemory(), zeroconf_instance=SimpleNamespace(zeroconf=None))
    pairing = ctl.load_pairing("hallway", dict(PAIRING_DATA))
    try:
        assert pairing.description is None  # nothing from mDNS yet
        pairing.connection.reconnect_soon()
        await wait_for(lambda: len(connects) == 1)
        assert connects == [([OLD], 51826)]  # the pairing-time address saved with the keys

        # the thermostat announces itself (same device id) at a new address and port
        ctl._async_handle_loaded_service_info(hap_record(NEW, 51830))
        assert pairing.description.address == NEW
        # the next attempt goes there at once: the 0.5 s back-off wait is cut short
        await wait_for(lambda: len(connects) == 2, timeout=0.3)
        assert connects[-1] == ([NEW], 51830)
    finally:
        await pairing.shutdown()


async def test_aiohomekit_loads_a_pairing_at_the_live_mdns_address(connects):
    ctl = IpController(char_cache=CharacteristicCacheMemory(), zeroconf_instance=SimpleNamespace(zeroconf=None))
    ctl._async_handle_loaded_service_info(hap_record(NEW))  # mDNS knew the device before the load
    pairing = ctl.load_pairing("hallway", dict(PAIRING_DATA))
    try:
        await wait_for(lambda: len(connects) == 1)
        assert connects == [([NEW], 51826)]
    finally:
        await pairing.shutdown()


# --- 2. the bridge with the real aiohomekit Controller ----------------------------------


@pytest.fixture
async def real_bridge(tmp_path):
    ctl_box: dict[str, Any] = {}

    def factory(_path):
        ctl_box["ctl"] = Controller(async_zeroconf_instance=SimpleNamespace(zeroconf=None),
                                    char_cache=CharacteristicCacheMemory())
        return ctl_box["ctl"]

    bridge = hk.HomekitBridge(tmp_path / "hk", controller_factory=factory, load_find_timeout=0.05,
                              pair_start_timeout=0.3)
    await bridge.start()
    yield bridge, ctl_box["ctl"]
    await bridge.stop()


async def test_bridge_loads_at_the_live_mdns_address_never_the_stale_one(real_bridge, connects):
    bridge, ctl = real_bridge
    ctl.transports[TransportType.IP]._async_handle_loaded_service_info(hap_record(NEW))
    data = dict(PAIRING_DATA)
    await bridge.load("hallway", data, address=OTHER, port=51826)  # an older stored address
    await wait_for(lambda: len(connects) >= 1)
    assert connects[0] == ([NEW], 51826)
    assert bridge.endpoint("hallway") == (NEW, 51826)
    assert data == PAIRING_DATA  # the saved pairing data is never rewritten


@pytest.mark.parametrize(("hint", "expected"), [((NEW, 51830), ([NEW], 51830)), ((None, None), ([OLD], 51826))],
                         ids=["last_mdns_sighting", "pairing_time_address"])
async def test_bridge_without_an_mdns_record_uses_the_last_sighting_then_the_pairing_address(
        real_bridge, connects, hint, expected):
    bridge, ctl = real_bridge
    await bridge.load("hallway", dict(PAIRING_DATA), address=hint[0], port=hint[1])
    pairing = ctl.aliases["hallway"]
    assert pairing.description is None
    assert (pairing.connection.hosts, pairing.connection.port) == expected
    # and the first mDNS record for the device id still redirects it
    ctl.transports[TransportType.IP]._async_handle_loaded_service_info(hap_record("192.168.1.99"))
    await wait_for(lambda: connects and connects[-1] == (["192.168.1.99"], 51826))


async def test_pair_setup_at_a_stale_mdns_address_gives_up_and_stops_retrying(real_bridge, connects):
    bridge, ctl = real_bridge
    # mDNS still holds a record of an address the thermostat has left (it moved and the
    # announcement was lost, or it lost power without a goodbye): nothing answers there, and
    # aiohomekit's pairing-time connection would retry it forever
    ctl.transports[TransportType.IP]._async_handle_loaded_service_info(hap_record(OLD))
    with pytest.raises(hk.HomekitPairingError, match=r"did not answer at 192\.168\.1\.50:51826 within 0\.3 s"):
        await asyncio.wait_for(bridge.begin_pairing(DEVICE_ID, "hallway"), timeout=5)
    assert connects and all(c == ([OLD], 51826) for c in connects)
    assert not bridge.has_pending(DEVICE_ID)
    tried = len(connects)
    await asyncio.sleep(1.0)  # its next retry was due 0.75 s after the first attempt
    assert len(connects) == tried, "the pairing-time connection must be closed, not left retrying"


# --- 3. the service, the database and a thermostat that moves ---------------------------


@pytest.fixture
def readings(monkeypatch) -> list[Any]:
    """Readings the service hands to ingest (ingest and alerts belong to other modules)."""
    got: list[Any] = []
    monkeypatch.setattr(ingest, "ingest_live_readings", lambda _s, rs, source="homekit": got.extend(rs))
    monkeypatch.setattr(notify, "raise_alert", lambda *_a, **_k: 1)
    monkeypatch.setattr(notify, "resolve_alert", lambda *_a, **_k: None)
    return got


@pytest.fixture
def dhcp(db, tmp_path, readings):
    put_setting(db, "source", SourceSettings(homekit_enabled=True))
    db.commit()
    dev = FakeDevice(status_flags=0)
    dev.paired = True
    ctl = FakeController([dev])
    clock = Clock()
    bridge = hk.HomekitBridge(tmp_path / "hk", controller_factory=lambda _p: ctl, zeroconf_factory=ctl.zeroconf,
                              mdns_settle=0.0, load_find_timeout=0.01, resolve_backoff=(0.01, 0.02, 0.04))
    svc = HomekitService(bridge, clock=clock)
    return SimpleNamespace(db=db, dev=dev, ctl=ctl, clock=clock, bridge=bridge, svc=svc, readings=readings)


async def paired(env: SimpleNamespace) -> Any:
    """Paired at the old address, loaded, inventoried, polled once; returns the pairing object."""
    with session_scope() as s:
        save_pairing(s, env.dev.id, "hallway", "main", env.dev.pairing_data(), None, env.clock())
    await env.bridge.start()
    await env.svc.step()
    assert env.svc._states["hallway"].ready
    assert env.readings, "the first poll should have read the thermostat"
    env.readings.clear()
    return env.ctl.aliases["hallway"]


async def test_an_announced_new_address_reconnects_at_once_and_setup_follows(dhcp):
    pairing = await paired(dhcp)
    assert device_row(dhcp.db, dhcp.dev.id).address == OLD

    dhcp.ctl.move(dhcp.dev, NEW)  # rebooted onto a new lease and announced it
    await dhcp.svc.step()  # no clock advance: well before the next 60 s poll

    assert pairing.connects[-1] == ([NEW], 51826)  # aiohomekit followed the device id
    assert dhcp.readings, "readings resume at once, without waiting for the next poll"
    row = device_row(dhcp.db, dhcp.dev.id)
    assert (row.address, row.port, row.online, row.pairing_error) == (NEW, 51826, True, None)
    assert dhcp.ctl.aliases["hallway"] is pairing  # nothing to replace: aiohomekit already reconnected it there
    assert dhcp.ctl.mdns.sent == []  # heard the announcement: no need to ask


async def test_a_connection_left_on_the_old_address_is_replaced_without_a_timeout(dhcp):
    old = await paired(dhcp)
    assert old.connected_host == OLD

    # new address without the old connection noticing (half-open): its next request would hang
    dhcp.ctl.move(dhcp.dev, NEW, reboot=False)
    assert old.connected_host == OLD
    await dhcp.svc.step()

    new = dhcp.ctl.aliases["hallway"]
    assert new is not old and old.shut_down and not old.listeners
    assert old.timeouts == 0  # no request was lost on the dead connection
    assert new.connected_host == NEW
    assert dhcp.readings
    assert device_row(dhcp.db, dhcp.dev.id).address == NEW
    assert dhcp.bridge.endpoint("hallway") == (NEW, 51826)


async def test_an_announcement_during_a_connection_attempt_is_followed_at_once(dhcp):
    old = await paired(dhcp)
    dhcp.dev.answers = False
    dhcp.ctl.move(dhcp.dev, NEW, announce=False)  # rebooted onto a new lease, not heard yet
    dhcp.clock.advance(61)
    await dhcp.svc.poll_due()  # fails: nothing answers at the old address any more
    assert dhcp.readings == []
    # aiohomekit is now busy trying the old address: an announcement cannot cut that attempt
    # short, and the library would wait its back-off (up to 60 s) before trying the new one
    old.stalled = True
    dhcp.ctl.announce(dhcp.dev)
    assert old.connected_host is None

    await dhcp.svc.step()  # no clock advance: well before the next 60 s poll
    new = dhcp.ctl.aliases["hallway"]
    assert new is not old and old.shut_down
    assert new.connected_host == NEW
    assert dhcp.readings, "readings resume at once, without waiting for the library's back-off"
    row = device_row(dhcp.db, dhcp.dev.id)
    assert (row.address, row.online, row.pairing_error) == (NEW, True, None)


async def test_an_unheard_move_is_found_by_asking_mdns_after_a_failure(dhcp):
    pairing = await paired(dhcp)
    dhcp.ctl.move(dhcp.dev, NEW, announce=False)  # the announcement never reached us

    dhcp.clock.advance(61)
    await dhcp.svc.poll_due()  # the poll fails: nothing answers at the old address
    assert dhcp.readings == []
    await wait_for(lambda: dhcp.ctl.mdns.sent and pairing.connected_host == NEW)

    # one targeted query, by the device id's mDNS names, without known answers
    service, host = dhcp.dev.service, dhcp.dev.host
    assert dhcp.ctl.mdns.sent[0] == [(service, 33), (service, 16), (host, 1), (host, 28)]
    await dhcp.svc.step()  # the answer was an announcement: reconnected, polled, stored
    assert dhcp.readings
    row = device_row(dhcp.db, dhcp.dev.id)
    assert (row.address, row.online) == (NEW, True)
    sent = len(dhcp.ctl.mdns.sent)
    await asyncio.sleep(0.1)
    assert len(dhcp.ctl.mdns.sent) == sent  # found: no more queries


async def test_started_before_mdns_answers_it_connects_as_soon_as_the_device_is_heard(dhcp):
    # the thermostat moved while the service was down, and mDNS has not answered yet
    with session_scope() as s:
        save_pairing(s, dhcp.dev.id, "hallway", "main", dhcp.dev.pairing_data(), None, dhcp.clock())
    dhcp.ctl.move(dhcp.dev, NEW, announce=False)
    dhcp.dev.visible, dhcp.dev.answers = False, False
    await dhcp.bridge.start()
    await dhcp.svc.step()
    st = dhcp.svc._states["hallway"]
    assert not st.ready  # the inventory failed at the pairing-time address
    assert dhcp.ctl.aliases["hallway"].connects == [([OLD], 51826)]

    dhcp.ctl.announce(dhcp.dev)  # heard: aiohomekit reconnects there and says so
    await dhcp.svc.step()  # no clock advance: not the 60 s inventory retry
    assert st.ready and dhcp.readings
    assert device_row(dhcp.db, dhcp.dev.id).address == NEW


async def test_a_first_inventory_follows_an_announcement_that_lands_during_an_attempt(dhcp):
    with session_scope() as s:
        save_pairing(s, dhcp.dev.id, "hallway", "main", dhcp.dev.pairing_data(), None, dhcp.clock())
    dhcp.ctl.move(dhcp.dev, NEW, announce=False)
    dhcp.dev.visible, dhcp.dev.answers = False, False
    await dhcp.bridge.start()
    await dhcp.svc.step()
    st = dhcp.svc._states["hallway"]
    assert not st.ready  # the inventory failed at the pairing-time address
    old = dhcp.ctl.aliases["hallway"]
    old.stalled = True  # and aiohomekit is busy retrying it when the announcement lands

    dhcp.ctl.announce(dhcp.dev)
    await dhcp.svc.step()  # no clock advance: not the 60 s inventory retry
    assert dhcp.ctl.aliases["hallway"] is not old and old.shut_down
    assert st.ready and dhcp.readings
    assert device_row(dhcp.db, dhcp.dev.id).address == NEW


async def test_discovery_follows_announcements_goodbyes_and_config_changes(dhcp):
    other = FakeDevice("aa:bb:cc:dd:ee:02", "Bedroom", status_flags=1)
    dhcp.ctl.devices[other.id] = other
    await dhcp.bridge.start()
    await dhcp.svc.step()
    row = device_row(dhcp.db, other.id)
    assert (row.address, row.port, row.config_num, row.online) == (OLD, 51826, 3, True)
    first_seen = row.last_seen_at

    other.config_num = 4
    dhcp.clock.advance(5)  # well inside the 60 s discovery interval
    dhcp.ctl.move(other, "192.168.1.88", port=51830)
    await dhcp.svc.step()
    row = device_row(dhcp.db, other.id)
    assert (row.address, row.port, row.config_num, row.online) == ("192.168.1.88", 51830, 4, True)
    assert row.last_seen_at > first_seen
    assert not dhcp.bridge.discovery_due()  # consumed

    dhcp.ctl.goodbye(other)  # aiohomekit keeps the discovery; the browser says it is gone
    await dhcp.svc.step()
    assert device_row(dhcp.db, other.id).online is False
    dhcp.ctl.announce(other)
    await dhcp.svc.step()
    assert device_row(dhcp.db, other.id).online is True

    await dhcp.bridge.stop()
    assert dhcp.ctl.mdns.browser.service_state_changed.handlers == []


# --- bridge only: the re-resolve back-off -------------------------------------------------


async def test_re_resolving_backs_off_while_the_thermostat_stays_silent(tmp_path):
    dev = FakeDevice(status_flags=0)
    ctl = FakeController([dev])
    bridge = hk.HomekitBridge(tmp_path / "hk", controller_factory=lambda _p: ctl, zeroconf_factory=ctl.zeroconf,
                              load_find_timeout=0.01, resolve_backoff=(5, 15, 60))
    waits: list[float] = []
    gate = asyncio.Event()

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) >= 5:
            await gate.wait()  # hold the loop here
        await asyncio.sleep(0)

    bridge._sleep = fake_sleep
    await bridge.start()
    await bridge.load("hallway", dev.pairing_data())
    await bridge.inventory("hallway")
    dev.answers = False
    ctl.move(dev, NEW, announce=False)

    res = await bridge.read("hallway")
    assert not res.ok
    await wait_for(lambda: len(waits) == 5)
    assert waits == [5, 15, 60, 60, 60]  # the last wait repeats
    assert len(ctl.mdns.sent) == 5  # one query per round
    res = await bridge.read("hallway")  # a second failure does not start a second loop
    await asyncio.sleep(0.01)
    assert len(ctl.mdns.sent) == 5

    dev.address = OLD  # back where it was: the next poll succeeds and ends the loop
    assert (await bridge.read("hallway")).ok
    task = bridge._paired["hallway"].resolve_task
    assert task is None
    gate.set()
    await bridge.stop()


async def test_a_pairing_the_thermostat_rejects_is_not_re_resolved(tmp_path):
    dev = FakeDevice(status_flags=0)
    ctl = FakeController([dev])
    bridge = hk.HomekitBridge(tmp_path / "hk", controller_factory=lambda _p: ctl, zeroconf_factory=ctl.zeroconf,
                              load_find_timeout=0.01)
    await bridge.start()
    await bridge.load("hallway", dev.pairing_data())
    ctl.aliases["hallway"].fail_always = AuthenticationError("unknown controller")
    res = await bridge.read("hallway")
    assert res.needs_repair
    await asyncio.sleep(0.01)
    assert ctl.mdns.sent == [] and bridge._paired["hallway"].resolve_task is None
    ctl.aliases["hallway"].fail_always = disconnected()
    await bridge.read("hallway")
    await bridge.stop()


async def test_pairing_refuses_a_thermostat_that_left_the_network(tmp_path):
    dev = FakeDevice()  # ready to pair
    ctl = FakeController([dev])
    bridge = hk.HomekitBridge(tmp_path / "hk", controller_factory=lambda _p: ctl, zeroconf_factory=ctl.zeroconf)
    await bridge.start()
    ctl.goodbye(dev)  # aiohomekit keeps its discovery; the browser saw it leave
    with pytest.raises(hk.HomekitPairingError, match="left the network"):
        await bridge.begin_pairing(dev.id, "hallway")
    assert dev.log == [] and not bridge.has_pending(dev.id)

    ctl.announce(dev)  # back
    await bridge.begin_pairing(dev.id, "hallway")
    assert dev.log == ["start:hallway"] and bridge.has_pending(dev.id)
    await bridge.stop()


async def test_unknown_instance_asks_for_every_hap_service(tmp_path):
    dev = FakeDevice(status_flags=0)
    dev.visible = False  # never seen on mDNS since the start
    ctl = FakeController([dev])
    bridge = hk.HomekitBridge(tmp_path / "hk", controller_factory=lambda _p: ctl, zeroconf_factory=ctl.zeroconf,
                              load_find_timeout=0.01)
    await bridge.start()
    assert bridge.query_device(dev.id)
    assert ctl.mdns.sent == [[(t, 12) for t in hk.HAP_TYPES]]
    await asyncio.sleep(0)  # the device answers the PTR question
    assert {d.id: d.address for d in await bridge.discover()} == {dev.id: OLD}
    await bridge.stop()


def test_endpoint_data_never_touches_the_keys():
    data = dict(PAIRING_DATA)
    moved = hk.endpoint_data(data, NEW, 51830, [NEW, "fd00::7"])
    assert data == PAIRING_DATA
    assert (moved["AccessoryIP"], moved["AccessoryIPs"], moved["AccessoryPort"]) == (NEW, [NEW, "fd00::7"], 51830)
    assert {k: v for k, v in moved.items() if not k.startswith("AccessoryIP") and k != "AccessoryPort"} == \
        {k: v for k, v in data.items() if not k.startswith("AccessoryIP") and k != "AccessoryPort"}
    assert hk.endpoint_data(data, None, None) == data


# --- real mDNS (opt-in: needs multicast on this host) --------------------------------------


@pytest.mark.skipif(os.environ.get("CLIMATE_TEST_REAL_MDNS") != "1",
                    reason="set CLIMATE_TEST_REAL_MDNS=1 to run against real multicast DNS on this host")
async def test_real_mdns_follows_a_service_that_moves(tmp_path):
    """One zeroconf announces a HAP-like service; the real bridge (its own zeroconf, browser and
    aiohomekit Controller) follows it through an announced move, an unannounced one (found by
    ``query_device``) and a goodbye. Verified on Linux with host networking."""
    from zeroconf import ServiceInfo
    from zeroconf.asyncio import AsyncZeroconf

    device_id, name = "aa:bb:cc:dd:ee:42", f"Climate DHCP Test.{hk.HAP_TYPES[0]}"

    def record(address: str, port: int = 51826, config_num: int = 3) -> ServiceInfo:
        return ServiceInfo(hk.HAP_TYPES[0], name, port=port, server="climate-dhcp-test.local.",
                           addresses=[socket.inet_aton(address)],
                           properties={"id": device_id.upper(), "md": "ECB501", "c#": str(config_num), "s#": "1",
                                       "sf": "1", "ci": "9", "ff": "0", "pv": "1.1"})

    async def at(address: str | None) -> bool:
        dev = {d.id: d for d in await bridge.discover()}.get(device_id)
        return (dev is None) if address is None else (dev is not None and dev.address == address)

    async def until(check, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        while not await check():
            assert time.monotonic() < deadline, "mDNS did not deliver in time"
            await asyncio.sleep(0.2)

    async def due() -> bool:
        return bridge.discovery_due()

    accessory = AsyncZeroconf()
    bridge = hk.HomekitBridge(tmp_path / "hk", mdns_settle=0.5)
    try:
        await accessory.async_register_service(record("192.0.2.50"))
        await bridge.start()
        await until(lambda: at("192.0.2.50"))

        bridge.discovery_due()
        await accessory.async_update_service(record("192.0.2.77", 51830, 4))  # announced
        await until(due)  # the browser hook saw it
        await until(lambda: at("192.0.2.77"))
        dev = {d.id: d for d in await bridge.discover()}[device_id]
        assert (dev.port, dev.config_num) == (51830, 4)

        silent = record("192.0.2.88", 51830, 4)
        accessory.zeroconf.registry.async_update(silent)  # moved without announcing
        await asyncio.sleep(1.0)
        assert await at("192.0.2.77")  # nobody told us
        assert bridge.query_device(device_id)  # so ask
        await until(lambda: at("192.0.2.88"))

        await accessory.async_unregister_service(silent)  # goodbye
        await until(lambda: at(None))
    finally:
        await bridge.stop()
        await accessory.async_close()
