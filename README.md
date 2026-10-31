<img src="custom_components/roborock_live_map/brand/icon.png" width="96" align="right" alt="">

# Roborock Live Map

Extra map data and controls for Home Assistant's built-in **Roborock** integration: a live
map feed for a 3D map card, a map image for the Xiaomi Vacuum Map Card, faster map updates
while the robot is cleaning, and dock, carpet and room settings the built-in integration
does not offer.

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=nphil&repository=ha-roborock-live-map&category=integration)
[![release](https://img.shields.io/github/v/release/nphil/ha-roborock-live-map)](https://github.com/nphil/ha-roborock-live-map/releases)

It has no account to sign in to and makes no network connections of its own. It rides on the
connection the built-in Roborock integration already has, so the robot is still polled once,
not twice.

## What you get

Entity names below are shown as they appear next to your robot; your entity ids will start
with your robot's name.

**On the robot**

- **Map** (image): the current map with the `calibration_points`, `rooms` and `zones`
  attributes that the [Xiaomi Vacuum Map Card](https://github.com/PiotrMachowski/lovelace-xiaomi-vacuum-map-card)
  reads. It is tied to the vacuum, not to the map's name, so renaming the map or rebuilding
  it (a factory reset does that) does not orphan it.
- **Live map** (sensor): changes whenever the map is re-scanned and carries the robot's
  position, angle, room and obstacle count. The full map is served over HTTP instead (see
  [For card authors](#for-card-authors)), so paths and grids never reach the database.
- **Faster map while cleaning**: the map is re-read every 5 seconds while the robot cleans.
  The built-in integration re-reads it at most every 30 seconds.
- **Clean history** (select) and **Clean history map** (image): pick a past run and see its map.
- **Room order** and **Room settings** (sensors): the custom cleaning order and how many rooms
  carry their own suction, water, mop route and repeat settings.
- **Back up map** and **Restore map backup** (buttons) with a **Map backup** timestamp sensor.
- **Carpet boost** (switch) and **Carpets when mopping** (select: avoid, lift mop, ignore).

**On the dock**

- **Emptying**, **Washing** and **Drying** (binary sensors).
- **Empty dustbin**, **Wash mop**, **Start drying** and **Stop drying** (buttons).
- **Mop wash mode**, **Mop wash frequency** (smart or every 10, 15, 20 or 25 minutes) and
  **Drying time** (2, 3 or 4 hours) (selects), and **Auto dry** (switch).

All of these attach to the robot and dock devices the built-in integration already created,
so you will not get a second copy of your vacuum in the device list.

## Actions (services)

| Action | What it does |
| --- | --- |
| `roborock_live_map.refresh_map` | Re-read the map from every Roborock now, for example right after editing no-go zones. |
| `roborock_live_map.set_room_order` | Choose the order rooms are cleaned in. An empty list lets the robot decide. |
| `roborock_live_map.set_room_settings` | Replace every room's own suction, water, mop route and repeat count. An empty list clears them. Takes effect when the cleaning mode is Custom. |
| `roborock_live_map.split_room` | Split one room in two along a line (robot millimetres). |
| `roborock_live_map.merge_rooms` | Merge two neighbouring rooms into one. |

`split_room` and `merge_rooms` change the robot's saved map for good: press **Back up map**
first. Rooms made by a split have no name in your Roborock account and show up as
"Room N".

## Requirements

- Home Assistant **2026.9.2** or newer. That is the first release that ships python-roborock
  7.4.2, the version this was built and tested against.
- The built-in **Roborock** integration, set up and loaded. This integration waits for it and
  will not start without it.

## Install

1. In HACS open the three-dot menu, choose **Custom repositories**, add
   `https://github.com/nphil/ha-roborock-live-map` as an **Integration**, then download
   **Roborock Live Map**. (Or use the button at the top.)
2. Restart Home Assistant.
3. Go to **Settings, Devices & services, Add integration** and pick **Roborock Live Map**.
   It asks nothing. Add it once; a second entry adds nothing.

Manual install: copy `custom_components/roborock_live_map` into your `config/custom_components`
folder and restart.

## For card authors

The data a 3D map card needs is too big for entity attributes, so it is served over HTTP. All
of these need a signed-in Home Assistant session or a long-lived access token, like any other
Home Assistant API. `<id>` is the vacuum's id as shown in the `duid` attribute of the Live map
sensor.

- `GET /api/roborock_live_map/<id>`: the parsed map as JSON: `map_name`, `sequence`,
  `vacuum_position`, `charger`, `goto`, `vacuum_room`, `vacuum_room_name`, `path`,
  `predicted_path`, `mop_path`, `obstacles`, `ignored_obstacles`, `no_go_areas`,
  `no_mopping_areas`, `cleaned_rooms`, `rooms`, `virtual_walls`, `image`,
  `calibration_points`, `grid`, `duid` and `last_update`. Add `?grid=0` to leave the occupancy
  grid out; it only changes when the map is re-scanned, so cache it by `grid.hash`.
- `GET /api/roborock_live_map/<id>/photo/<photo_id>`: an obstacle photo taken by the robot.
- `GET /api/roborock_live_map/<id>/history`: the clean-history records (`?refresh=1` forces a re-read).
- `GET /api/roborock_live_map/<id>/history/<begin>`: one past run's map in the same shape as the live map.
- WebSocket command `roborock_live_map/remote` with `entity_id`, `action` (`start`, `move` or
  `stop`) and, for `move`, `velocity` and `omega`: drives the robot by hand. It always answers
  `{"ok": ..., "latency_ms": ...}` (plus `error` on failure). Speeds are clamped. If no `move`
  arrives for 0.6 seconds the robot is told to stop, and the session ends after 60 seconds
  without input.

The only card in this repository is the schedule card, described next. The 3D map, remote,
dock and rooms cards are not included.

## Cleaning schedules and the schedule card

The robot's own schedules cannot be managed with a self-hosted Roborock server, so this
integration keeps its own. They are stored in Home Assistant (one file per vacuum under
`.storage`) and Home Assistant starts the clean at the set time. How they behave:

- A schedule has a name, a time, the weekdays, the rooms (none means the whole home), a repeat
  count (rooms only, 1 to 3) and an on/off switch.
- Times are local time in Home Assistant's configured time zone and follow daylight saving.
- A schedule only runs while Home Assistant is running. A time that passes while Home
  Assistant is down or starting is skipped, never replayed, so a restart cannot start a clean
  nobody expects. Creating, enabling or editing a schedule never runs for a time that has
  already passed.
- Each run makes one attempt, with no retries. If the robot is already cleaning the run is
  skipped; if the vacuum is unavailable the run fails. The outcome is kept as the schedule's
  last result.
- Anyone signed in can see the schedules; only a Home Assistant administrator can create,
  edit, switch or delete them.
- They do not touch the robot's own schedule list, and the robot does not run them itself: if
  Home Assistant is off, nothing starts.

### Cleaning settings

A schedule can also say how the robot should clean: a mode, a vacuum power and a mop
intensity. The default, **Keep current settings**, changes nothing on the robot and behaves
exactly as schedules always did, so existing schedules are unaffected.

- Modes are Vacuum, Vacuum & mop, Mop and Custom (the robot's own custom settings; no power
  or intensity can be added). Vacuum power applies to Vacuum and Vacuum & mop; mop intensity
  to Mop and Vacuum & mop. Leaving either empty ("Mode default") keeps whatever the mode sets.
- The choices come from what the robot offers right now, read from its Home Assistant
  entities (the cleaning mode select, the mop intensity select and the vacuum's fan speed
  list). Models differ, so nothing is hard-coded; `off` and `custom` are never offered as a
  power or intensity. The card shows an error if the robot cannot be read.
- Choosing a mode on the robot also resets its vacuum power, mop intensity **and mop route**
  to that mode's defaults (read from the python-roborock source, not tried on a robot). So a
  run sets the mode first, then the vacuum power, then the mop intensity, then starts. All of
  these are ordinary Home Assistant service calls, and the settings stay on the robot
  afterwards: nothing restores the previous ones, and the app and Home Assistant show them.
- A schedule with settings only runs while the vacuum is docked or idle. If it is paused,
  returning or in error, the run is skipped and nothing is changed. If the robot is already
  cleaning, the run is skipped for every schedule. An unavailable or unknown vacuum is an
  error for every schedule, and nothing is sent. A schedule without settings keeps the old
  rule: it is skipped only while cleaning.
- Before the first command, the run checks that every control and service it needs exists
  and that the robot offers each requested value. If not, it fails with a message naming the
  problem and changes nothing.
- If a command fails part-way (a failed command may or may not have reached the robot), or
  the schedule is edited or the robot becomes busy part-way, the run stops and never starts
  the vacuum. The last result lists the settings already applied; they cannot be rolled back.
  If Home Assistant itself stops in the middle of a run, nothing is recorded.
- Saving only checks the shape of the settings. Whether the robot offers a value is checked
  when the schedule runs, so a value the robot stops offering fails the run rather than the
  save.

Everything above was exercised against Home Assistant's real service layer with stand-in
entities; how a physical robot reacts (for example whether a power change takes effect while
docked) has not been tested.

For card authors, the schedule WebSocket commands are `roborock_live_map/schedules/` plus
`list`, `capabilities` (anyone signed in), and `save`, `set_enabled`, `delete` (administrators
only), all with `entity_id`. A schedule is `{id, name, time, days, rooms, repeat, enabled,
settings}` plus read-only `last_run`, `last_result` and `next_run`. `settings` is `{}` (keep
current settings) or `{"mode": "vacuum" | "vac_and_mop" | "mop" | "custom", "fan_speed"?: str,
"mop_intensity"?: str}`; it is always present in results. A save that leaves it out (clients
from before this feature do) keeps an existing schedule's stored settings and gives a new
schedule `{}`; send `{}` to clear them.
`capabilities` answers `{"modes": [...], "fan_speeds": [...], "mop_intensities": [...]}` or
an `unavailable` error whose message says which control could not be read.

The card ships inside this integration (`custom_components/roborock_live_map/frontend/`) and
Home Assistant loads it for you, so HACS updates it together with the integration and no
dashboard resource needs adding. After an update, reload the browser page. Add it to a
dashboard with:

```yaml
type: custom:roborock-schedule-card
entity: vacuum.your_robot
order_entity: sensor.your_robot_room_order
```

`entity` is the vacuum and `order_entity` is the room order sensor, which supplies the room
names. If you previously added the card by hand as a `/local/...roborock-schedule-card.js`
dashboard resource, delete that resource and file once this version is installed.

## Good to know

- Built and used on a Roborock S7 MaxV Ultra with its dock, served by the self-hosted
  [local_roborock_server](https://github.com/Python-roborock/local_roborock_server). Other
  models, and robots that use the Roborock cloud, are untested. The 5-second map refresh is
  the first thing to look at if the cloud pushes back; it is `FAST_MAP_INTERVAL` in `const.py`.
- The past-run map (`Clean history map`) needs the robot's server to answer a request the
  self-hosted server does not answer yet. Until it does the entity is unavailable and its
  `map_error` attribute says why. The run list itself works.
- Room names come from internal data of the built-in integration. A Home Assistant update
  could change that.
- There is no map rotation. RoborockCustomMap's rotation picker is not included.

## Credits, provenance and licence

This project started from a fork of
[RoborockCustomMap](https://github.com/Python-roborock/RoborockCustomMap) by Luke Lashley
([@Lash-L](https://github.com/Lash-L)), with contributions from
[@TheHangMan97](https://github.com/TheHangMan97) and
[@PiotrMachowski](https://github.com/PiotrMachowski) (who also wrote the Xiaomi Vacuum Map
Card). It replaces that integration and keeps its approach: ride on the built-in Roborock
integration, and give the map card the same `calibration_points`, `rooms` and `zones`. The
config flow and the "is the Roborock integration loaded" check are adapted from it. Everything
else, which is nearly all of the code, is new.

RoborockCustomMap publishes no licence. That is why this repository is a GitHub fork (GitHub's
terms let anyone fork a public repository) and why its history still shows the original
authors, instead of a copy. The [MIT licence](LICENSE) covers the code written for this
project; it cannot relicense the few lines adapted from upstream.

Not affiliated with or endorsed by Roborock or the authors above. "Roborock" is a trademark
of its owner and is used here only to say what this works with.
