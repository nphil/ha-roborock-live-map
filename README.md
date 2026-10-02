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

No card is included in this repository.

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
