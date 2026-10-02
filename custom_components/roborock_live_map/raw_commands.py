"""Raw V1 protocol commands not (yet) wrapped by python-roborock traits.

Every format here was confirmed against Kevin (S7 MaxV Ultra, python-roborock
7.4.2, served by the self-hosted local_roborock_server) with a live
read -> set -> read-back -> restore round trip via tools/rbq.py before this
module was written. Where the device echoes a value back differently than it
was set, that is called out so callers never treat such a response as the
source of truth for fields it silently drops.
"""

from __future__ import annotations

import logging
from typing import Any

from roborock.devices.traits.v1.command import CommandTrait
from roborock.roborock_typing import RoborockCommand

_LOGGER = logging.getLogger(__name__)

# --- Carpet boost (GET/SET_CARPET_MODE) --------------------------------------
# Kevin's baseline: {"enable": 0, "stall_time": 10, "current_low": 400,
# "current_high": 500, "current_integral": 450}. The device wants every field
# resent on every write, so callers must always read-modify-write.


async def get_carpet_boost(command: CommandTrait) -> dict[str, Any]:
    """Return the carpet boost (carpet detection / motor boost) settings."""
    response = await command.send(RoborockCommand.GET_CARPET_MODE)
    if isinstance(response, list):
        response = response[0]
    if not isinstance(response, dict):
        raise ValueError(f"Unexpected get_carpet_mode response: {response!r}")
    return response


async def set_carpet_boost_enable(command: CommandTrait, enable: bool) -> dict[str, Any]:
    """Enable/disable carpet boost, preserving the other tuned fields."""
    current = await get_carpet_boost(command)
    updated = {**current, "enable": 1 if enable else 0}
    await command.send(RoborockCommand.SET_CARPET_MODE, [updated])
    return updated


# --- Carpet mode when mopping (GET/SET_CARPET_CLEAN_MODE) --------------------
# 0 = avoid carpets, 1 = lift the mop over carpets, 2 = ignore (mop anyway).

CARPET_CLEAN_MODES = {"avoid": 0, "lift_mop": 1, "ignore": 2}
CARPET_CLEAN_MODE_NAMES = {v: k for k, v in CARPET_CLEAN_MODES.items()}
CARPET_CLEAN_MODE_LABELS = {"avoid": "Avoid", "lift_mop": "Lift mop", "ignore": "Ignore"}


async def get_carpet_clean_mode(command: CommandTrait) -> int | None:
    """Return the current carpet-when-mopping mode code."""
    response = await command.send(RoborockCommand.GET_CARPET_CLEAN_MODE)
    if isinstance(response, list):
        response = response[0]
    if not isinstance(response, dict):
        raise ValueError(f"Unexpected get_carpet_clean_mode response: {response!r}")
    return response.get("carpet_clean_mode")


async def set_carpet_clean_mode(command: CommandTrait, mode_name: str) -> int:
    """Set the carpet-when-mopping mode by name (avoid/lift_mop/ignore)."""
    code = CARPET_CLEAN_MODES[mode_name]
    await command.send(RoborockCommand.SET_CARPET_CLEAN_MODE, {"carpet_clean_mode": code})
    return code


# --- Dryer (APP_GET/SET_DRYER_SETTING, APP_SET_DRYER_STATUS) ----------------
# app_get_dryer_setting -> {"status": 0|1, "on": {"dry_time": seconds, ...},
# "off": {...}}. status is the auto-dry-after-mopping preference; dry_time is
# only meaningful while status == 1. app_set_dryer_status{status} starts/stops
# a drying cycle immediately, independent of the auto-dry preference.
# Kevin's baseline: status 1, on.dry_time 14400 (4 h).

DRYING_TIMES = {"2h": 7200, "3h": 10800, "4h": 14400}
DRYING_TIME_NAMES = {v: k for k, v in DRYING_TIMES.items()}
DRYING_TIME_LABELS = {"2h": "2 hours", "3h": "3 hours", "4h": "4 hours"}


async def get_dryer_setting(command: CommandTrait) -> dict[str, Any]:
    """Return the dryer configuration (auto-dry preference + timings)."""
    response = await command.send(RoborockCommand.APP_GET_DRYER_SETTING)
    if isinstance(response, list):
        response = response[0]
    if not isinstance(response, dict):
        raise ValueError(f"Unexpected app_get_dryer_setting response: {response!r}")
    return response


async def set_auto_dry(command: CommandTrait, enabled: bool) -> None:
    """Turn the auto-dry-after-mopping preference on or off."""
    await command.send(RoborockCommand.APP_SET_DRYER_SETTING, {"status": 1 if enabled else 0})


async def set_drying_time(command: CommandTrait, time_name: str) -> None:
    """Set the auto-dry duration (2h/3h/4h)."""
    seconds = DRYING_TIMES[time_name]
    await command.send(RoborockCommand.APP_SET_DRYER_SETTING, {"on": {"dry_time": seconds}})


async def start_drying(command: CommandTrait) -> None:
    """Start a drying cycle now."""
    await command.send(RoborockCommand.APP_SET_DRYER_STATUS, {"status": 1})


async def stop_drying(command: CommandTrait) -> None:
    """Stop the current drying cycle."""
    await command.send(RoborockCommand.APP_SET_DRYER_STATUS, {"status": 0})


# --- Mop wash frequency (GET/SET_SMART_WASH_PARAMS) --------------------------
# Confirmed with a live round trip plus the app's own terminology:
# smart_wash=1 is "Smart" (the dock adapts wash timing and water to how dirty
# the mop reads); smart_wash=0 uses a fixed wash_interval in seconds. The app
# offers 10/15/20/25 minute fixed intervals. Kevin's baseline: 0 / 1200 (20 min).

WASH_FREQUENCIES: dict[str, dict[str, int]] = {
    "smart": {"smart_wash": 1, "wash_interval": 1200},
    "10_min": {"smart_wash": 0, "wash_interval": 600},
    "15_min": {"smart_wash": 0, "wash_interval": 900},
    "20_min": {"smart_wash": 0, "wash_interval": 1200},
    "25_min": {"smart_wash": 0, "wash_interval": 1500},
}
WASH_FREQUENCY_LABELS = {
    "smart": "Smart",
    "10_min": "10 min",
    "15_min": "15 min",
    "20_min": "20 min",
    "25_min": "25 min",
}
WASH_FREQUENCY_KEYS = {label: key for key, label in WASH_FREQUENCY_LABELS.items()}


def wash_frequency_key(smart_wash: int | None, wash_interval: int | None) -> str | None:
    """Classify a (smart_wash, wash_interval) pair back into an option key."""
    if smart_wash:
        return "smart"
    for key, params in WASH_FREQUENCIES.items():
        if params["smart_wash"] == 0 and params["wash_interval"] == wash_interval:
            return key
    return None


async def set_wash_frequency(command: CommandTrait, key: str) -> dict[str, int]:
    """Set the mop wash frequency by option key."""
    params = WASH_FREQUENCIES[key]
    await command.send(RoborockCommand.SET_SMART_WASH_PARAMS, params)
    return params


# --- Room cleaning order (GET/SET_CLEAN_SEQUENCE) ----------------------------
# Confirmed via a live round trip: params is a flat list of segment ids in the
# desired cleaning order; an empty list restores the robot's automatic order.
# ("set_clean_sequence [23,20]" -> get echoes [23,20]; "[]" -> echoes [].)


async def get_room_order(command: CommandTrait) -> list[int]:
    """Return the current custom room cleaning order (empty = automatic)."""
    response = await command.send(RoborockCommand.GET_CLEAN_SEQUENCE)
    if response is None:
        return []
    if not isinstance(response, list):
        raise ValueError(f"Unexpected get_clean_sequence response: {response!r}")
    return [int(v) for v in response]


async def set_room_order(command: CommandTrait, segments: list[int]) -> None:
    """Set (or, with an empty list, clear) the custom room cleaning order."""
    await command.send(RoborockCommand.SET_CLEAN_SEQUENCE, list(segments))


# --- Per-room settings (GET/SET_CUSTOMIZE_CLEAN_MODE) ------------------------
# Confirmed via a live round trip against Kevin: every row needs all seven
# keys (segment, fan_power, water_box_mode, mop_mode, repeat, seq_type,
# mop_power); the write REPLACES the whole table, a partial row is rejected
# with -10005, and one bad row fails the entire write. Kevin's firmware only
# ever echoes segment/fan_power/water_box_mode/mop_mode back on a read --
# repeat, seq_type and mop_power are silently dropped even though they were
# accepted, so callers must remember what they set for those three.
#
# seq_type (vacuum-then-mop sequencing) and mop_power (sonic scrub intensity)
# are not part of the Kevin room-settings contract; every row is written with
# seq_type=0 (simultaneous vac+mop, the robot's normal global behaviour) and a
# fixed, harmless mop_power=2 (mid-range).

_SEQ_TYPE_DEFAULT = 0
_MOP_POWER_DEFAULT = 2


async def get_room_settings_raw(command: CommandTrait) -> list[dict[str, Any]]:
    """Return the device-echoed rows of the customize-clean-mode table."""
    response = await command.send(RoborockCommand.GET_CUSTOMIZE_CLEAN_MODE)
    if response is None:
        return []
    if not isinstance(response, list):
        raise ValueError(f"Unexpected get_customize_clean_mode response: {response!r}")
    return response


async def set_room_settings_raw(command: CommandTrait, rows: list[dict[str, Any]]) -> None:
    """Write the full customize-clean-mode table (empty list clears it)."""
    payload = [
        {
            "segment": row["segment"],
            "fan_power": row["fan_power"],
            "water_box_mode": row["water_box_mode"],
            "mop_mode": row["mop_mode"],
            "repeat": row.get("repeat", 1),
            "seq_type": _SEQ_TYPE_DEFAULT,
            "mop_power": _MOP_POWER_DEFAULT,
        }
        for row in rows
    ]
    await command.send(RoborockCommand.SET_CUSTOMIZE_CLEAN_MODE, payload)


# --- Map backup / restore (MANUAL_BAK_MAP, RECOVER_MULTI_MAP) ----------------
# Confirmed via a live round trip: manual_bak_map {"map_flag": 0} backed the
# current map into the single backup slot (bak add_time advanced to ~now);
# recover_multi_map {"map_flag": 0} restored it and afterwards the room
# mapping (segments 16-24 + names), the 1 no-go zone and the 1 invisible wall
# were all unchanged. Both commands key off the LIVE map's flag, not the
# backup slot's flag (passing the backup's mapFlag 4 is rejected: -10005).

LIVE_MAP_FLAG = 0


async def backup_map(command: CommandTrait) -> None:
    """Back up the current map into the single backup slot."""
    await command.send(RoborockCommand.MANUAL_BAK_MAP, {"map_flag": LIVE_MAP_FLAG})


async def restore_map_backup(command: CommandTrait) -> None:
    """Restore the current map from the single backup slot."""
    await command.send(RoborockCommand.RECOVER_MULTI_MAP, {"map_flag": LIVE_MAP_FLAG})


async def get_backup_timestamp(command: CommandTrait) -> int | None:
    """Return the epoch-seconds timestamp of the single map backup, if any."""
    response = await command.send(RoborockCommand.GET_MULTI_MAPS_LIST)
    if isinstance(response, list):
        response = response[0]
    if not isinstance(response, dict):
        raise ValueError(f"Unexpected get_multi_maps_list response: {response!r}")
    for map_info in response.get("map_info") or []:
        if map_info.get("mapFlag") != LIVE_MAP_FLAG:
            continue
        for bak in map_info.get("bak_maps") or []:
            return bak.get("add_time")
    return None


# --- Room split / merge (SPLIT_SEGMENT, MERGE_SEGMENT) -----------------------
# Both confirmed live on Kevin (2026-09-26) and undone with restore_map_backup.
# split_segment needs INTEGER millimetres: floats are rejected with -10006
# "Split map failed". New segments come back unnamed (see below).
#
# Room NAMES are not stored on the robot. get_room_mapping returns
# [segment, "<account room id>", <room type>]; the account (home data) maps
# that id to the name. name_segment overwrites the account id with whatever
# string it is given and zeroes the room type, so sending names through it
# destroys every room's name. There is deliberately no rename here: renaming
# needs the account's room API, which python-roborock only reads.


async def split_room(command: CommandTrait, segment: int, x0: int, y0: int, x1: int, y1: int) -> None:
    """Split a room along the line (x0,y0)-(x1,y1), robot millimetres."""
    await command.send(RoborockCommand.SPLIT_SEGMENT, [segment, *(round(v) for v in (x0, y0, x1, y1))])


async def merge_rooms(command: CommandTrait, segment_a: int, segment_b: int) -> None:
    """Merge two room segments into one."""
    await command.send(RoborockCommand.MERGE_SEGMENT, [segment_a, segment_b])
