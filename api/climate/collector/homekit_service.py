"""Entry point of the host-networked HomeKit service: ``python -m climate.collector.homekit_service``.

Loops: mDNS discovery -> homekit_devices; the pairing handshake driven by the web UI
(pairing_state requested -> start pair-setup -> awaiting_code; code_submitted -> finish ->
save pairing encrypted -> paired; unpair_requested -> remove pairing); for each pairing:
populate accessories, map aids to sensors (by name), subscribe 'ev' characteristics, poll
every 60 s in batches of <= 49, push readings via collector.ingest.ingest_live_readings;
execute queued control_actions with channel 'homekit'; heartbeat 'homekit' every loop.
"""
