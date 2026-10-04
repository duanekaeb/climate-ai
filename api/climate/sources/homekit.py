"""HomeKit (HAP over IP) adapter: the server is the HomeKit controller for each ecobee
(aiohomekit 4.0.1; verified notes in docs/HOMEKIT.md). No Apple device, Apple Home,
Homebridge or Home Assistant. Runs ONLY inside the host-networked homekit service
(``climate.collector.homekit_service``) because discovery and pairing need mDNS.

Rules: one AsyncZeroconf + one AsyncServiceBrowser for BOTH '_hap._tcp.local.' and
'_hap._udp.local.' kept for the process lifetime; ``controller.load_pairing`` only after the
controller started; never use ``aiohomekitctl pair`` (it loses the keys); pairing data is
saved encrypted in the DB (secrets 'homekit_pairing:<alias>'), never to a plain file.
Vendor characteristics on current firmware are 'pr' without 'ev': poll them. Subscribe only
'ev' characteristics; check ``supports_subscribe``. put_characteristics: an entry is a
failure only when status != 0. Writes: TIMESTAMP (reuse the unit's suffix) in its own put,
then SET_HOLD_SCHEDULE in a separate put, then read back. Never write HOME/SLEEP/AWAY targets.
"""
