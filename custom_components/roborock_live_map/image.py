"""Map image for the Xiaomi Vacuum Map Card.

Replaces the Roborock Custom Map integration. The attribute contract is the
same one that card reads -- calibration_points, rooms and zones -- but the entity
is keyed on the vacuum rather than on the map's name, so renaming or re-creating
the map (which a factory reset does) no longer orphans it, and it attaches to the
core Roborock device instead of creating a duplicate one.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.image import ImageEntity
from homeassistant.components.roborock.coordinator import RoborockDataUpdateCoordinator
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .entity import RoborockLiveMapEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one map image per vacuum."""
    async_add_entities(
        entity
        for runtime in config_entry.runtime_data
        for entity in (
            RoborockLiveMapImage(hass, runtime.coordinator, runtime.device),
            CleanHistoryMapImage(hass, runtime),
        )
    )


class RoborockLiveMapImage(RoborockLiveMapEntity, ImageEntity):
    """The current map, with the calibration the map card needs."""

    _attr_name = "Map"
    _attr_content_type = "image/png"

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: RoborockDataUpdateCoordinator,
        device: dr.DeviceEntry | None,
    ) -> None:
        """Initialize the map image."""
        RoborockLiveMapEntity.__init__(
            self, coordinator, f"{coordinator.duid_slug}_live_map_image", device
        )
        ImageEntity.__init__(self, hass)
        self._cached_image: bytes | None = None
        self._attr_image_last_updated = self._last_update()

    def _last_update(self) -> datetime:
        return getattr(self.coordinator, "last_home_update", None) or dt_util.utcnow()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Bump the image timestamp only when the rendered map changes."""
        content = self._map_content
        image = content.image_content if content is not None else None
        if image is not None and image != self._cached_image:
            self._cached_image = image
            self._attr_image_last_updated = self._last_update()
        super()._handle_coordinator_update()

    @property
    def available(self) -> bool:
        """Available while the current map has rendered image data."""
        content = self._map_content
        return super().available and content is not None and content.image_content is not None

    async def async_image(self) -> bytes | None:
        """Return the rendered current map."""
        content = self._map_content
        return content.image_content if content is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Calibration, rooms and zones, in the shape the map card expects."""
        map_data = self._map_data
        if map_data is None:
            return {}
        rooms = {
            number: {
                "x0": room.x0,
                "y0": room.y0,
                "x1": room.x1,
                "y1": room.y1,
                "number": room.number,
                "name": getattr(room, "name", None),
                "pos_x": getattr(room, "pos_x", None),
                "pos_y": getattr(room, "pos_y", None),
            }
            for number, room in (map_data.rooms or {}).items()
        }
        zones = (
            [zone.as_dict() for zone in map_data.zones] if map_data.zones else None
        )
        return {
            "calibration_points": map_data.calibration(),
            "rooms": rooms,
            "zones": zones,
        }


class CleanHistoryMapImage(RoborockLiveMapEntity, ImageEntity):
    """The map of the clean-history record selected in the Clean history select.

    Sourcing note: the historical map rides get_clean_record_map over the
    dedicated map RPC channel. The self-hosted local_roborock_server does not
    currently answer that command (it times out while get_map_v1 on the same
    channel returns in ~1 s), so until it does, this entity stays unavailable
    and map_error explains why.
    """

    _attr_name = "Clean history map"
    _attr_content_type = "image/png"

    def __init__(self, hass: HomeAssistant, runtime) -> None:
        """Initialize the history map image."""
        RoborockLiveMapEntity.__init__(
            self,
            runtime.coordinator,
            f"{runtime.coordinator.duid_slug}_clean_history_map",
            runtime.device,
        )
        ImageEntity.__init__(self, hass)
        self._history = runtime.history
        self._cached_content: Any | None = None
        self._attr_image_last_updated = dt_util.utcnow()

    async def async_added_to_hass(self) -> None:
        """Subscribe to history-store updates."""
        await super().async_added_to_hass()
        self.async_on_remove(self._history.add_listener(self._handle_history_update))

    @callback
    def _handle_history_update(self) -> None:
        """Bump the image timestamp only when a different map arrives."""
        content = self._history.map_content
        if content is not self._cached_content:
            self._cached_content = content
            self._attr_image_last_updated = dt_util.utcnow()
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Available while the selected record has a rendered map."""
        return super().available and self._history.map_content is not None

    async def async_image(self) -> bytes | None:
        """Return the rendered map of the selected record."""
        content = self._history.map_content
        return content.image_content if content is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The selected record, any map fetch problem, and the HTTP endpoints."""
        slug = self.coordinator.duid_slug
        attributes: dict[str, Any] = {
            "history_url": f"/api/roborock_live_map/{slug}/history",
        }
        if self._history.selected is not None:
            attributes["selected"] = self._history.selected
            attributes["map_url"] = (
                f"/api/roborock_live_map/{slug}/history/{self._history.selected}"
            )
            record = self._history.record_for_begin(self._history.selected)
            if record is not None:
                attributes["record"] = record
        if self._history.map_error:
            attributes["map_error"] = self._history.map_error
        return attributes
