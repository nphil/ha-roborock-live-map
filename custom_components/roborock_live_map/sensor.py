"""Sensors: live map revision, room order, room settings, map backup.

The live map sensor stays deliberately tiny (map sequence + pose; paths and
grids are served over HTTP). Room order and room settings read the slow
RawSettings store; room names come from the core rooms trait, and the option
lists for per-room settings come from the status trait's own mappings so they
always match what this exact robot supports.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.roborock.coordinator import RoborockDataUpdateCoordinator
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .entity import RoborockLiveMapEntity, RoborockSettingsEntity

PARALLEL_UPDATES = 0

_STATE_MAX = 250


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensors for every vacuum."""
    async_add_entities(
        entity
        for runtime in config_entry.runtime_data
        for entity in (
            RoborockLiveMapSensor(runtime.coordinator, runtime.device),
            RoomOrderSensor(runtime),
            RoomSettingsSensor(runtime),
            MapBackupSensor(runtime),
        )
    )


class RoborockLiveMapSensor(RoborockLiveMapEntity, SensorEntity):
    """Tracks the live map revision so the 3D card knows when to refetch."""

    _attr_name = "Live map"
    _attr_icon = "mdi:cube-scan"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self, coordinator: RoborockDataUpdateCoordinator, device: dr.DeviceEntry | None
    ) -> None:
        """Initialize the live map sensor."""
        super().__init__(coordinator, f"{coordinator.duid_slug}_live_map", device)

    @property
    def native_value(self) -> int | None:
        """Map sequence number, which advances as the map is re-scanned."""
        map_data = self._map_data
        if map_data is None:
            return None
        return map_data.additional_parameters.get("map_sequence")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Robot pose and a pointer to the full payload."""
        attributes: dict[str, Any] = {
            "data_url": f"/api/roborock_live_map/{self.coordinator.duid_slug}",
            "duid": self.coordinator.duid_slug,
        }
        map_data = self._map_data
        if map_data is None:
            return attributes

        position = map_data.vacuum_position
        if position is not None:
            attributes["position_x"] = position.x
            attributes["position_y"] = position.y
            if position.a is not None:
                attributes["angle"] = position.a
        if map_data.vacuum_room_name:
            attributes["room"] = map_data.vacuum_room_name

        obstacles = (map_data.obstacles or []) + (map_data.obstacles_with_photo or [])
        attributes["obstacle_count"] = len(obstacles)
        return attributes


def _room_names(coordinator: RoborockDataUpdateCoordinator) -> dict[int, str]:
    """Segment id -> room name, from the core rooms trait."""
    rooms = coordinator.properties_api.rooms
    room_map = getattr(rooms, "room_map", None) or {}
    return {segment: room.name for segment, room in room_map.items()}


class RoomOrderSensor(RoborockSettingsEntity, SensorEntity):
    """The custom room cleaning order, or Automatic when there is none."""

    _attr_name = "Room order"
    _attr_icon = "mdi:route"

    def __init__(self, runtime) -> None:
        """Initialize on the vacuum device."""
        super().__init__(
            runtime.coordinator,
            f"{runtime.coordinator.duid_slug}_room_order",
            runtime.device,
            runtime.settings,
        )

    @property
    def native_value(self) -> str:
        """Human summary, e.g. "Kitchen → Hallway", or "Automatic"."""
        order = self._settings.room_order
        if not order:
            return "Automatic"
        names = _room_names(self.coordinator)
        parts = [names.get(segment, f"Room {segment}") for segment in order]
        summary = " → ".join(parts)
        if len(summary) > _STATE_MAX:
            summary = summary[: _STATE_MAX - 1].rsplit(" ", 1)[0] + "…"
        return summary

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The raw order plus every room on the map (id/name), for cards."""
        names = _room_names(self.coordinator)
        return {
            "order": list(self._settings.room_order),
            "rooms": [{"id": segment, "name": name} for segment, name in sorted(names.items())],
        }


class RoomSettingsSensor(RoborockSettingsEntity, SensorEntity):
    """How many rooms carry custom fan/water/route/repeat settings."""

    _attr_name = "Room settings"
    _attr_icon = "mdi:tune-variant"

    def __init__(self, runtime) -> None:
        """Initialize on the vacuum device."""
        super().__init__(
            runtime.coordinator,
            f"{runtime.coordinator.duid_slug}_room_settings",
            runtime.device,
            runtime.settings,
        )

    @property
    def native_value(self) -> int:
        """Number of rooms with custom settings."""
        return len(self._settings.room_settings_rows)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Per-room settings plus the option lists this robot supports."""
        status = self.coordinator.properties_api.status
        fan_names = dict(status.fan_speed_mapping or {})
        water_names = dict(status.water_mode_mapping or {})
        route_names = dict(status.mop_route_mapping or {})
        names = _room_names(self.coordinator)

        rooms: dict[str, Any] = {}
        for row in self._settings.room_settings_rows:
            segment = row.get("segment")
            if segment is None:
                continue
            remembered = self._settings.remembered.get(segment, {})
            rooms[str(segment)] = {
                "name": names.get(segment, f"Room {segment}"),
                "fan_speed": fan_names.get(row.get("fan_power"), row.get("fan_power")),
                "water": water_names.get(row.get("water_box_mode"), row.get("water_box_mode")),
                "route": route_names.get(row.get("mop_mode"), row.get("mop_mode")),
                # Kevin's firmware accepts repeat but never echoes it back;
                # show what we last wrote (default 1 after a restart).
                "repeat": remembered.get("repeat", 1),
            }
        return {
            "rooms": rooms,
            "options": {
                "fan_speed": _choices(fan_names),
                "water": _choices(water_names),
                "route": _choices(route_names),
                "repeat": [1, 2, 3],
            },
        }


def _choices(mapping: dict[int, str]) -> list[str]:
    """Option names in the robot's own code order (weakest to strongest).

    "custom" is dropped: it means "use per-room settings", which is meaningless
    as a setting FOR one room.
    """
    return [name for _code, name in sorted(mapping.items()) if name != "custom"]


class MapBackupSensor(RoborockSettingsEntity, SensorEntity):
    """When the single map backup slot was last written."""

    _attr_name = "Map backup"
    _attr_icon = "mdi:content-save"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, runtime) -> None:
        """Initialize on the vacuum device."""
        super().__init__(
            runtime.coordinator,
            f"{runtime.coordinator.duid_slug}_map_backup",
            runtime.device,
            runtime.settings,
        )

    @property
    def native_value(self) -> datetime | None:
        """Timestamp of the backup, or None when the slot is empty."""
        ts = self._settings.map_backup_ts
        if not ts:
            return None
        return datetime.fromtimestamp(int(ts), tz=dt_util.UTC)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The raw epoch seconds for cards that prefer numbers."""
        return {"backup_timestamp": self._settings.map_backup_ts}
