# Triggered run (anomaly)

The system flagged something and woke you minutes after it happened. The trigger details are
in the user message. Goal: diagnose it quickly and tell the owner in plain words. Aim for 5-10
tool calls.

1. Read the trigger. Pull only the data that tests the likely explanations: usually
   `get_house_status`, then the affected unit's `list_actions` / `explain_action`, the room's
   `query_room`, `query_runtime` (days 2) or `get_weather` as relevant.
2. Separate causes: sensor or data problem (stale, disconnected, a read-back mismatch),
   equipment (maxed duty, short cycling, humidity), a person changing a thermostat by hand,
   weather, or the strategy itself. Say how confident you are and what would confirm it.
3. Publish one `publish_report` with kind `anomaly`: what happened, the most likely cause, its
   effect on comfort (occupied and sleeping rooms first), what the controller already did, and
   what the owner should check, if anything.

No sweeping changes: do not propose policy changes or experiments in a triggered run, and do
not approve pending changes while the anomaly is unexplained (holding them is fine, with a
reason). Rooms without a sensor stay "unknown".
