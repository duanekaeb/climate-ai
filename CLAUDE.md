# CLAUDE.md: Climate AI

Read `docs/BLUEPRINT.md` before building anything. It is the plan of record.

## The house

- **Main floor:** ecobee in the Hallway; SmartSensors in the School Room, Living Room and Kitchen.
- **Upstairs:** ecobee Smart Thermostat Essential in the Toy Room; SmartSensor in the Girls' Room.
- **Bed / Office wing:** ecobee in the Bedroom (to confirm); SmartSensor in the Office.
- The main floor drives heat into the upstairs (floor + open stairwell). The wing is independent
  of the upstairs and at most weakly linked to the main floor. Measure; don't assume.

## Ground rules

- **Claude never writes to a thermostat.** Only the controller writes to ecobee. Claude's write
  tools only queue proposals, which are validated against hard limits in code.
- **No savings claim without weather normalization.** Compare against the weather-expected
  baseline and show a 90% interval. Raw before/after numbers are never presented as savings.
- **Respect ecobee's limits:** poll `/thermostatSummary` no faster than every 3 minutes; fetch
  details only when a revision changes; at most one runtime report request open at a time,
  ≤ 31 days each. Always persist the newest refresh token.
- **Keep the ecobee auth path behind one adapter** (`api/climate/sources/ecobee.py`). The
  account-sign-in route is unofficial and may need replacing.
- **Every ecobee write is logged** to `control_actions` with the reason and before/after values.
- **Open-Meteo attribution** ("Weather data by Open-Meteo.com") must appear wherever its data is
  shown.
- Stack: Python 3.12, FastAPI, SQLAlchemy 2.0, Postgres + TimescaleDB, Vue 3 + TypeScript,
  Docker Compose.
