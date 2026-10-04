"""Utility (demand-response) events: ingest, alerts, honest opt-outs, pre-conditioning inputs.

See docs/specs/holds-and-utility-events.md. The controller never counteracts a running
event; the only ways out are opt-outs ecobee records (``skips``).
"""
