"""Shared base for Roborock Live Map entities.

Entities here attach to the core Roborock integration's existing device rather
than declaring one. Device identifiers are scoped per config entry, so an entity
that declared the Roborock identifier through device_info would get a second
copy of the vacuum's device under this integration. Setting device_entry and
leaving device_info unset links to the real device instead.
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, TypeVar

from roborock.exceptions import RoborockException

from homeassistant.components.roborock.coordinator import RoborockDataUpdateCoordinator
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import CoordinatorEntity

_T = TypeVar("_T")


def roborock_device(
    hass: HomeAssistant, coordinator: RoborockDataUpdateCoordinator, roborock_entry_id: str
) -> dr.DeviceEntry | None:
    """The core Roborock integration's device for this coordinator's vacuum."""
    identifier = next(iter(coordinator.device_info["identifiers"]))
    return dr.async_get(hass).async_get_device_by_identifier(identifier, roborock_entry_id)


def roborock_dock_device(
    hass: HomeAssistant, coordinator: RoborockDataUpdateCoordinator, roborock_entry_id: str
) -> dr.DeviceEntry | None:
    """The core Roborock integration's device for this coordinator's dock.

    Mirrors roborock_device() but for the synthesized "<name> Dock" device the
    core coordinator creates alongside the vacuum (coordinator.dock_device_info).
    """
    identifier = next(iter(coordinator.dock_device_info["identifiers"]))
    return dr.async_get(hass).async_get_device_by_identifier(identifier, roborock_entry_id)

def current_map_content(coordinator: RoborockDataUpdateCoordinator) -> Any | None:
    """The MapContent for the map the vacuum is currently using."""
    home = coordinator.properties_api.home
    if home is None or not home.home_map_content:
        return None
    current = coordinator.properties_api.maps.current_map
    if current is not None and current in home.home_map_content:
        return home.home_map_content[current]
    # Fall back to the only map when the current flag is not yet known.
    contents = list(home.home_map_content.values())
    return contents[0] if len(contents) == 1 else None


def apply_room_names(coordinator: RoborockDataUpdateCoordinator, map_data: Any) -> None:
    """Name the parsed map's rooms from the vacuum's room mapping.

    The map blob carries segment ids only; names come from the rooms trait.
    Mutates map_data.rooms in place, the same way RoborockCustomMap did, so
    every consumer of the shared map data sees names.
    """
    if map_data is None or not map_data.rooms:
        return
    home = coordinator.properties_api.home
    rooms_trait = getattr(home, "_rooms_trait", None) if home is not None else None
    room_map = getattr(rooms_trait, "room_map", None) or {}
    for room in map_data.rooms.values():
        named = room_map.get(room.number)
        # A room made by split/merge has no account room behind it yet.
        room.name = named.name if named else f"Room {room.number}"


class RoborockLiveMapEntity(CoordinatorEntity[RoborockDataUpdateCoordinator]):
    """Coordinator entity linked to the core Roborock device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: RoborockDataUpdateCoordinator,
        unique_id: str,
        device: dr.DeviceEntry | None,
    ) -> None:
        """Initialize the entity and link it to the vacuum's existing device."""
        super().__init__(coordinator)
        self._attr_unique_id = unique_id
        self.device_entry = device

    @property
    def _command(self):
        """The raw V1 command trait for this vacuum."""
        return self.coordinator.properties_api.command

    @property
    def _map_content(self) -> Any | None:
        return current_map_content(self.coordinator)

    @property
    def _map_data(self) -> Any | None:
        content = self._map_content
        if content is None or content.map_data is None:
            return None
        apply_room_names(self.coordinator, content.map_data)
        return content.map_data


async def run_command(action: Awaitable[_T], description: str) -> _T:
    """Await a robot command, turning protocol failures into a readable error."""
    try:
        return await action
    except RoborockException as err:
        raise HomeAssistantError(f"{description} failed: {err}") from err


class RoborockSettingsEntity(RoborockLiveMapEntity):
    """Entity backed by the slow RawSettings store.

    Reads are synchronous off the store; the store's slow poll and every
    optimistic post-write update push state through the listener.
    """

    def __init__(
        self,
        coordinator: RoborockDataUpdateCoordinator,
        unique_id: str,
        device: dr.DeviceEntry | None,
        settings: Any,
    ) -> None:
        """Initialize the entity with its settings store."""
        super().__init__(coordinator, unique_id, device)
        self._settings = settings

    async def async_added_to_hass(self) -> None:
        """Subscribe to settings-store updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._settings.add_listener(self._handle_settings_update)
        )

    @callback
    def _handle_settings_update(self) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Available once the coordinator and the settings store have data."""
        return super().available and self._settings.loaded
