"""Slow background poller for the settings the core coordinator does not track.

Carpet boost, carpet-when-mopping mode, the dryer configuration, the custom
room order, the per-room settings table and the map-backup timestamp are raw
V1 commands with no python-roborock trait, so nothing else keeps them fresh.
They only change when someone edits them (app or HA), so one serial fetch
every RAW_SETTINGS_INTERVAL plus an immediate optimistic update after every
write is enough. Entities read this store synchronously and subscribe as
listeners; the store never raises into an entity property.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
from typing import Any

from homeassistant.components.roborock.coordinator import RoborockDataUpdateCoordinator
from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.helpers.event import async_track_time_interval

from . import raw_commands as raw
from .const import RAW_SETTINGS_INTERVAL

_LOGGER = logging.getLogger(__name__)


class RawSettings:
    """Latest raw settings for one vacuum, refreshed sparingly."""

    def __init__(self, hass, coordinator: RoborockDataUpdateCoordinator) -> None:
        """Initialize an empty store bound to one coordinator."""
        self._hass = hass
        self._coordinator = coordinator
        self._listeners: list[Callable[[], None]] = []
        self._refresh_lock = asyncio.Lock()

        self.carpet: dict[str, Any] | None = None
        self.carpet_clean_mode: int | None = None
        self.dryer: dict[str, Any] | None = None
        self.room_order: list[int] = []
        self.room_settings_rows: list[dict[str, Any]] = []
        self.map_backup_ts: int | None = None
        self.loaded = False

        # Kevin's firmware accepts repeat/seq_type/mop_power in
        # set_customize_clean_mode but never echoes them back. Remember what we
        # wrote per segment so the room-settings sensor can still show repeat.
        self.remembered: dict[int, dict[str, Any]] = {}

    @property
    def _command(self):
        return self._coordinator.properties_api.command

    def add_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        """Subscribe to store updates; returns the unsubscribe callback."""
        self._listeners.append(listener)

        @callback
        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove

    @callback
    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    @callback
    def async_start(self) -> CALLBACK_TYPE:
        """Start the slow interval poll; returns the function that stops it."""
        return async_track_time_interval(self._hass, self._async_tick, RAW_SETTINGS_INTERVAL)

    async def _async_tick(self, _now) -> None:
        try:
            await self.async_refresh()
        except Exception as err:  # noqa: BLE001 - a missed tick is harmless
            _LOGGER.debug("Raw settings refresh failed for %s: %s", self._coordinator.duid_slug, err)

    async def async_refresh(self) -> None:
        """Re-read every raw setting. One failure never wipes the others."""
        if self._refresh_lock.locked():
            return
        async with self._refresh_lock:
            for field, fetch in (
                ("carpet", raw.get_carpet_boost),
                ("carpet_clean_mode", raw.get_carpet_clean_mode),
                ("dryer", raw.get_dryer_setting),
                ("room_order", raw.get_room_order),
                ("room_settings_rows", raw.get_room_settings_raw),
                ("map_backup_ts", raw.get_backup_timestamp),
            ):
                try:
                    setattr(self, field, await fetch(self._command))
                except Exception as err:  # noqa: BLE001 - keep the previous value
                    _LOGGER.debug(
                        "Could not read %s for %s: %s", field, self._coordinator.duid_slug, err
                    )
            self.loaded = True
            # Drop remembered repeats for rooms no longer in the table.
            echoed = {row.get("segment") for row in self.room_settings_rows}
            for segment in list(self.remembered):
                if segment not in echoed:
                    self.remembered.pop(segment)
            self._notify()

    # --- optimistic updates after a write -----------------------------------

    @callback
    def apply_carpet_boost(self, settings: dict[str, Any]) -> None:
        """Record the merged carpet-boost dict returned by the write."""
        self.carpet = settings
        self._notify()

    @callback
    def apply_carpet_clean_mode(self, code: int) -> None:
        """Record the carpet-when-mopping code that was just set."""
        self.carpet_clean_mode = code
        self._notify()

    @callback
    def apply_dryer(self, patch: dict[str, Any]) -> None:
        """Merge a partial dryer reply ({"status":..} or {"on": {...}})."""
        merged = dict(self.dryer or {})
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
        self.dryer = merged
        self._notify()

    @callback
    def apply_room_order(self, order: list[int]) -> None:
        """Record the room order that was just set."""
        self.room_order = list(order)
        self._notify()

    @callback
    def apply_room_settings(self, rows: list[dict[str, Any]]) -> None:
        """Record the full room-settings table that was just written."""
        self.room_settings_rows = [dict(row) for row in rows]
        self.remembered = {
            row["segment"]: {"repeat": row.get("repeat", 1)} for row in rows
        }
        self._notify()

    @callback
    def apply_map_backup(self, ts: int | None) -> None:
        """Record a new (or cleared) map backup timestamp."""
        self.map_backup_ts = ts
        self._notify()
