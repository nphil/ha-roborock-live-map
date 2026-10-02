"""Constants for the Roborock Live Map integration."""

from datetime import timedelta

DOMAIN = "roborock_live_map"

# How often the map is re-fetched while the vacuum is cleaning. The core
# integration only re-fetches every 30 s (IMAGE_CACHE_INTERVAL), which is a
# cloud rate-limit precaution. On the local stack there is no rate limit; 5 s
# is fast without the CPU cost upstream reports for 1 s (map parsing is heavy).
FAST_MAP_INTERVAL = timedelta(seconds=5)

DATA_URL = "/api/roborock_live_map/{duid}"
PHOTO_URL = "/api/roborock_live_map/{duid}/photo/{photo_id}"

# How often the raw (trait-unwrapped) settings are re-read: carpet boost/mode,
# dryer, room order, per-room settings, map backup timestamp. These only change
# when someone edits them (app or HA), so a slow poll plus an immediate
# optimistic update after every write is plenty.
RAW_SETTINGS_INTERVAL = timedelta(minutes=3)

# Clean history is refetched on demand (HTTP/service) and after this long.
HISTORY_STALE = timedelta(seconds=60)

# Remote control (app_rc_*) session parameters. The card sends "move" at up to
# 20 Hz plus a 250 ms heartbeat while held; the backend re-applies the last
# vector on its own 150 ms tick so motion stays smooth even if a browser tick
# is late. Measured app_rc_move round trip on the local stack: 5-45 ms
# (avg ~16 ms), so a 400 ms robot-side duration overlaps consecutive sends.
RC_TICK = 0.15
RC_MOVE_DURATION_MS = 400
RC_DEADMAN = 0.6
RC_IDLE_END = 60.0
RC_VELOCITY_LIMIT = 0.3
RC_OMEGA_LIMIT = 1.5
