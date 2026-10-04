# HomeKit: local readings from the three ecobees

Climate AI can talk to each ecobee directly over HomeKit (HAP over Wi-Fi) on your LAN, with
your home server acting as the HomeKit controller. That gives you room temperatures and
motion/occupancy within seconds instead of the cloud's 3-minute, 30-minute-window view, and it
keeps live readings and basic timed holds working when the ecobee cloud is down.

You need no extra hardware and nothing from Apple: no iPhone pairing, no Apple Home, no
Homebridge, no Home Assistant. The server uses [aiohomekit](https://github.com/Jc2k/aiohomekit)
4.0.1, the library Home Assistant uses, pinned exactly.

Plan on **three pairings, one per thermostat** (Hallway = `main`, Toy Room Essential = `up`,
Bedroom = `bed`). Each SmartSensor appears as an extra accessory inside the pairing of the
thermostat it is linked to in the ecobee app; it has no pairing of its own.

> **How much of this is verified.** Everything marked **verified** was checked against the
> aiohomekit 4.0.1 tag (byte-identical to the PyPI wheel) and run end to end with real mDNS
> against aiohomekit's test accessory server. **Nothing has been tested against a real ecobee
> yet.** ecobee-specific details come from aiohomekit's and Home Assistant's ecobee fixtures.
> Items marked **unverified** come from search-result summaries only (ecobee's support site
> could not be read); confirm them on your thermostat.

---

## 0. Before you start

**A household decision.** HomeKit allows a new pairing only on a thermostat that is not paired
to anything. If anyone in the house uses Apple Home or Siri for these thermostats, they lose
them there: Climate AI becomes the thermostat's HomeKit controller, and Apple Home offers no
way to share it back. Agree on this first.

**Network (verified).**
- The HomeKit service runs with `network_mode: host`, so the **server must run Linux**.
  Docker Desktop on macOS or Windows cannot do multicast DNS from a container.
- The server must see the thermostats' mDNS announcements (UDP 5353): same LAN/VLAN, or an
  mDNS reflector between VLANs. Discovery and pairing depend on it. If the host runs a
  firewall, allow UDP 5353 in (e.g. `sudo ufw allow 5353/udp`). Running avahi on the host is
  fine; both can listen.
- Give each thermostat a **DHCP reservation**. After pairing the service connects to the saved
  address and only follows an address change through mDNS while it is running; the saved
  address is never rewritten.

**Keys (by design of this app).** Pairing creates a long-term Ed25519 key pair; the
controller's private key (`iOSDeviceLTSK`) is what lets the server talk to the thermostat. Climate
AI stores each pairing **encrypted in the database** (`secrets` row `homekit_pairing:<alias>`,
Fernet with `CLIMATE_SECRET_KEY`), never in a `pairings.json` file. Back up the database
(`deploy/backup.sh`) and, separately, `CLIMATE_SECRET_KEY`. Lose either and every thermostat
has to be HomeKit-reset and paired again.

The `homekit-state` volume (`/var/lib/climate/homekit`, mode 0700) holds only aiohomekit's
`charmap.json` characteristic cache. It is safe to delete; it is rebuilt.

---

## 1. Start the HomeKit service

```bash
docker compose --profile homekit up -d          # or add homekit to COMPOSE_PROFILES in .env
docker compose logs -f homekit
```

Then in the web app open **Setup → Data source** and turn **HomeKit** on. The service idles
(re-checking every minute) until HomeKit is enabled there.

## 2. Discover the thermostats

**Setup → HomeKit** lists every HomeKit device the service sees over mDNS, with its device id
(e.g. `aa:bb:cc:dd:ee:ff`), model and whether it is **ready to pair**.

"Ready to pair" is the HomeKit status flag `sf` bit 0x01 (verified). A thermostat that is
**not** ready to pair is already paired to some controller (Apple Home, Homebridge, Home
Assistant...).

From the shell, the same scan (verified; it always takes 30 seconds):

```bash
docker compose run --rm homekit aiohomekitctl -f /tmp/none.json discover
```

Global flags go before the subcommand. **Do not use `-u`**: it is inverted in 4.0.1 and hides
the unpaired devices. Read the `Status Flags (sf)` line instead: `Status Flags (sf): 1` means
ready to pair; **no sf line at all** means sf=0, already paired.

## 3. Free any thermostat that is still paired

A paired thermostat rejects a new pairing (`UnavailableError`, verified). On the thermostat,
choose **Disconnect from HomeKit** (or **Reset HomeKit** on older models), then refresh Setup
until it shows ready to pair.

Menu paths (**unverified**; they differ by model, confirm on the device):
- Essential / Lite: Main Menu > Settings > Reset > Disconnect from HomeKit
- Premium / Enhanced: Main Menu > General > Settings > Reset > Disconnect from HomeKit
- Older models: a "Reset HomeKit" entry

## 4. Pair (one thermostat at a time)

**In the web app (normal way).** Setup → HomeKit → pick the device, give it an alias (lower
case, e.g. `hallway`, `toyroom`, `bedroom`) and its unit (`main`, `up`, `bed`), then **Pair**.
The service starts pair-setup; the thermostat then shows a **fresh 8-digit code** on its
screen. Type it as `XXX-XX-XXX` (dashes required) and submit. The pairing is saved encrypted
before the connection closes.

- If no code appears on the screen, open the thermostat's HomeKit menu and look there
  (**unverified** path; search summaries mention Main Menu > Settings > HomeKit).
- Wrong code: start again. The thermostat shows a new code for every attempt.
- `BusyError` / `MaxTriesError`: wait a few minutes, or reset HomeKit on the thermostat.

**From the shell (fallback).** Same result, same encrypted storage, run on the server:

```bash
docker compose run --rm homekit python scripts/pair_ecobee.py hallway aa:bb:cc:dd:ee:ff --unit main
docker compose run --rm homekit python scripts/pair_ecobee.py toyroom 11:22:33:44:55:66 --unit up
docker compose run --rm homekit python scripts/pair_ecobee.py bedroom 66:55:44:33:22:11 --unit bed
```

It prompts for the code shown on the thermostat (and asks again until it looks like
`123-45-678`). The running homekit service picks the new pairing up on its next pass.

> **Never use `aiohomekitctl pair`** (verified bug in 4.0.1 and 3.2.20): it prints
> `Pairing for "x" was established.` but throws the new keys away. The thermostat keeps the
> lost pairing and must then be HomeKit-reset before anything can pair with it again.

## 5. Map the SmartSensors

After pairing, Setup shows each pairing's accessories: the thermostat is `aid=1`; every
SmartSensor linked to that thermostat is its own accessory, named after its room. Map each one
to its room's sensor in **Setup → Sensors** (the service also pre-maps by name).

What to expect (from fixtures, **not yet seen on your units**):
- SmartSensor accessory ids are **not** necessarily 2..N. Home Assistant's ecobee3 lite fixture
  (firmware 4.8) uses aids like `4295608971`, above 2^32; the app stores them as 64-bit.
- Newer sensors (model `EBRSE4`) expose an OccupancySensor **and** a MotionSensor (each with a
  "seconds since last activation" counter) plus a battery level. The old ecobee3 4.2 sensors
  ("REMOTE SENSOR") expose motion only.
- Thermostats: the ecobee3 and ECB501 expose motion and occupancy; the ecobee3 lite exposes
  neither. **No fixture exists for the Smart Thermostat Essential** (the Toy Room unit), nor
  for the Premium or Enhanced: whether the Essential has any occupancy service is unknown until
  you look at its accessory list.
- Check the sensor count while you are here. The house notes say six SmartSensors; one reading
  of the owner's description gives seven (an extra one in the Toy Room). The inventory
  settles it, and `CLAUDE.md` / `docs/BLUEPRINT.md` should be updated to match.
- A sensor shows up only under the thermostat it is linked to in the ecobee app. If you re-link
  one, the accessory list changes; the service rebuilds its map when the thermostat's
  configuration number changes.

## 6. What is read locally

The service polls **every readable characteristic it uses every 60 seconds** (in batches of at
most 49, one request in flight per thermostat): polling is the source of truth and the
liveness check. It **also subscribes** to the characteristics that support events (`ev`) for
low latency.

| Characteristic | Where | How |
|---|---|---|
| Temperature (`TEMPERATURE_CURRENT`) | thermostat, every SmartSensor | pushed + polled |
| Humidity (`RELATIVE_HUMIDITY_CURRENT`) | thermostats | pushed + polled |
| Occupancy / motion (`OCCUPANCY_DETECTED`, `MOTION_DETECTED`) | sensors, some thermostats | pushed + polled |
| Heating/cooling state (`HEATING_COOLING_CURRENT`) | thermostats | pushed + polled |
| Sensor status, low battery, battery level | sensors | pushed + polled |
| Seconds since last occupancy / motion (`VENDOR_ECOBEE_*_LAST_ACTIVATION`, -1 = never) | sensors, thermostats | **polled only** |
| Equipment running, current mode, hold end time (`VENDOR_ECOBEE_*`) | thermostats | **polled only** |
| System mode, active heat/cool setpoints (`HEATING_COOLING_TARGET`, `TEMPERATURE_*_THRESHOLD`) | thermostats | **polled only**, never written |
| Each comfort setting's own targets (`VENDOR_ECOBEE_HOME/SLEEP/AWAY_TARGET_HEAT/COOL`) | thermostats | **polled only**, never written |

On current firmware (4.7 / 4.8 fixtures) **every ecobee vendor characteristic is readable but
has no `ev` permission**: it cannot be subscribed, it changes only by polling, and subscribing
to one returns an error. Only the old 4.2 firmware had `ev` on them.

Behaviour you may notice (verified in the library):
- After a thermostat restarts, its accessory list can carry stale values (e.g. 100 °C). The
  service always polls fresh values before trusting anything.
- While a thermostat is unreachable, each poll fails within about 10 s; the library reconnects
  by itself (waits of 0.75 s × 1.5, capped at 60 s; an mDNS sighting cuts the wait short). A
  thermostat is marked unavailable after **3 failed polls in a row**, or at once if it vanished
  from the network.
- If one disconnect hits while subscribing, push stays off for that pairing until the service
  restarts (`docker compose restart homekit`). Polling still covers it.
- **`AuthenticationError` means the thermostat no longer knows this pairing** (someone reset
  HomeKit on it). It repeats on every call; the app raises an alert. Re-pair (steps 3-4).
- Occupancy from HomeKit is evidence, not truth: unknown, stale or uncertain counts as occupied.
- Push events have **not** been observed from a real ecobee yet (the test server cannot send
  them). If they never arrive, the 60-second polls still drive everything.

## 7. Writes (controller only, from Phase 4)

Claude never writes to a thermostat; only the controller process does, after the guardrails.
Over HomeKit the controller writes only a **timed hold** of one comfort setting (and, to resume
the schedule, `VENDOR_ECOBEE_CLEAR_HOLD`):

1. Read `VENDOR_ECOBEE_TIMESTAMP` to learn this unit's suffix (`T`, `Q` and `R` have all been
   seen; none on 4.2 firmware).
2. Write the hold end time alone: ISO-8601 local time with UTC offset plus that suffix.
3. In a **separate** request, write `VENDOR_ECOBEE_SET_HOLD_SCHEDULE` (0 home, 1 sleep, 2 away).
   Sent together in one request, ecobee falls back to a permanent hold.
4. Read back the end time and `VENDOR_ECOBEE_CURRENT_MODE` (3 = temp/hold). A write counts as
   failed only when a result row has `status != 0` (4.0.1 returns rows for successes too), and
   the optimistic echo is never taken as proof.

The Home/Sleep/Away target setpoints are **never written**: they permanently edit the
schedule. Every write is logged in `control_actions` with the channel `homekit`, the reason,
before/after values and the read-back.

When HomeKit is used: only as the fallback while the ecobee cloud is failing (the circuit
breaker opens after three failed polls and stays open 15 minutes), and never in simulator mode.
Because a HomeKit hold can only pick a comfort setting, the service first **reads** that
setting's Home/Sleep/Away targets and refuses the hold if they fall outside your hard limits.
A failed fallback write is not retried for 30 minutes; queued requests expire after 10
minutes and a request stuck "sent" (service restarted mid-write) is failed after 15. While the
cloud is down the service also writes a HomeKit-based snapshot of each unit, so the Live page
and the controller keep current temperatures.

## 8. While the ecobee cloud is down: what HomeKit can and can't see

HomeKit is a narrower window onto the thermostat than the ecobee cloud.

**It shows:** room temperatures, humidity, occupancy and motion; whether the system is heating
or cooling; the system mode; the active heat/cool setpoints; which comfort setting is in force
(Home, Sleep, Away, or a temperature hold); each comfort setting's own targets; and the time of
the next schedule change (or the end of a hold).

**It does not show:**
- **ecobee holds as such.** No hold type ("2 hours", "until the next change", "until I change
  it"), no start time and no hint of who set it. A HomeKit snapshot reports a hold only when it
  is our own HomeKit hold, a hold the cloud reported before it went down that the thermostat
  still shows, or a temperature hold it can see.
- **Utility (demand-response) events.** None is announced, started or ended over HomeKit, and
  the HomeKit snapshot lists no events and no utility. An event the cloud announced before it
  went down still makes the controller stand aside during its scheduled window. An event
  announced while the cloud is down is unknown to the app until the cloud is back.
- **Vacations and the other ecobee events** (Smart Away/Home, Quick Save, and so on).
- Utility enrollment, alerts, settings and the schedule itself.

**A utility event can't be skipped over HomeKit.** Skipping is an opt-out ecobee records and
reports to the utility, and it goes through the ecobee cloud. HomeKit has no such command, so a
skip you ask for waits until the cloud is back (the app tells you once).

### Changes made by hand during an outage

While the cloud circuit is open and HomeKit is writing the unit's live snapshot (the cloud's is
more than 10 minutes old), the service compares every poll with what the app knows it put on
the thermostat. It treats either of these as someone's hand change:

- **A comfort setting picked by hand.** One of our HomeKit holds is running, but the thermostat
  shows another comfort setting or a temperature hold.
- **A temperature set by hand.** The setpoints the thermostat acts on (heat, cool, or both in
  auto) are more than 0.5°F from the targets of the comfort setting in force. When the
  thermostat shows a temperature hold, they match none of the comfort settings' targets.

It logs **one** action-log entry per change, for example "Someone picked Away by hand on the
upstairs thermostat (seen over HomeKit while the ecobee cloud is down); the controller stands
aside until the cloud is back." That entry is your hold for that unit, "until you change it",
because HomeKit cannot show when it ends. The controller writes nothing to that unit until
the cloud is back. Then the real ecobee hold state decides: a hold still running stays your
hold, and one that has ended frees the unit. A HomeKit hold the controller had queued before
the change is not sent; it is logged as "Not sent: someone changed the thermostat by hand".
The same change is not logged again while it stays on the thermostat. A different change gets a
new entry.

What it cannot catch, or may get wrong:
- With no hold of ours running, a comfort setting picked by hand looks exactly like the
  schedule, so it is invisible. Picking the comfort setting our hold already holds is invisible
  too.
- A temperature set by hand is visible only while the thermostat answers for the comfort
  settings' targets, and only when it is more than 0.5°F from them. A temperature hold at
  exactly one comfort setting's setpoints looks like that setting.
- Pressing Resume during our hold looks like picking whatever the schedule shows, so it is
  logged as a hand change as well. The controller then stands aside, which is the safe
  direction.
- Nothing is read as a hand change while a utility event is in its window or the cloud's last
  snapshot showed a vacation or an unrecognised ecobee event: their setpoints are not a
  person's. A vacation or utility event that **starts** during the outage without the app
  knowing about it can be mistaken for a hand change.
- **Not checked on a real ecobee.** Two things are unverified: whether a temperature set at the
  wall reads as "temperature hold" in the current-mode characteristic, and whether the active
  setpoints ever drift from a comfort setting's targets on their own (Smart Recovery, eco+).
  If they do drift, the service reads it as a hand change and the controller stands aside until
  the cloud is back. That is the safe direction, at the cost of less automation during an
  outage.

## 9. Unpair or reset

**Normal way:** Setup → HomeKit → **Unpair**. The thermostat must be reachable: the service
removes the pairing on the thermostat itself, then deletes the stored keys. If the thermostat
cannot be reached, nothing is deleted (the keys stay valid) and Setup shows the error; bring it
back online and try again.

**Already reset on the thermostat** (Disconnect from HomeKit / Reset HomeKit, paths in step 3,
**unverified**): Unpair in Setup still works. The service sees the thermostat no longer accepts
the keys, treats the pairing as already removed and deletes the stored keys.

If the stored keys can no longer be decrypted (wrong or lost `CLIMATE_SECRET_KEY`), Unpair only
forgets the pairing here; choose Disconnect from HomeKit on the thermostat before pairing again.

To pair it again later, the thermostat must show ready to pair (step 2).

## 10. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Setup shows no devices | Not Linux / not host networking; firewall blocking UDP 5353; server on another VLAN without an mDNS reflector; HomeKit not enabled in Setup. Try the `discover` command in step 2. |
| Device listed but never "ready to pair" | Still paired elsewhere: step 3. |
| Pairing fails with "already paired" | Same as above (`UnavailableError`). |
| Readings stopped after a router change | The thermostat got a new address while the service was down; set DHCP reservations and `docker compose restart homekit`. |
| Alert "re-pair needed" (`AuthenticationError`) | The pairing was removed on the thermostat: steps 3-4. |
| Values update only once a minute | Normal for vendor fields; for temperatures it means push is off (see step 6): `docker compose restart homekit`. |
| HomeKit service offline in Setup | `docker compose --profile homekit ps`, `docker compose logs homekit`, and `docker compose exec app python -m climate.cli doctor`. |

## Library notes (pinned on purpose)

- `aiohomekit==4.0.1`, the version Home Assistant pins. 4.0.0 was the first release with the
  corrected ecobee Sleep/Away characteristic UUIDs (PR #548); 3.2.x has them swapped.
- aiohomekit promises no API stability; all of it sits behind one adapter
  (`api/climate/sources/homekit.py`), separate from the ecobee cloud adapter.
- The CLI's `accessories`, `remove-pairing` and `unpair` rewrite their pairing file in place, and
  `put` exits 0 on failure. Climate AI does not use the CLI for any of these.
