"""Live dock activity: emptying, washing, drying.

All three read the status trait the core coordinator already refreshes (and
which the robot pushes on change), so no extra polling is involved:

- emptying: state == emptying_the_bin, or the raw dust_collection_status flag
- washing:  the raw wash_status flag, or state in the mop-washing states
- drying:   the raw dry_status flag (the same source core's now-deprecated
            mop-drying binary sensor used)
"""

from __future__ import annotations

from collections.abc import Callable

from roborock.data.v1.v1_code_mappings import RoborockStateCode

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import RoborockLiveMapEntity

PARALLEL_UPDATES = 0

_WASHING_STATES = {
    RoborockStateCode.washing_the_mop,
    RoborockStateCode.washing_the_mop_2,
}


def _is_emptying(status) -> bool:
    return status.state == RoborockStateCode.emptying_the_bin or bool(
        status.dust_collection_status
    )


def _is_washing(status) -> bool:
    return bool(status.wash_status) or status.state in _WASHING_STATES


def _is_drying(status) -> bool:
    return bool(status.dry_status)


_DESCRIPTIONS: tuple[tuple[str, str, str, Callable[[object], bool]], ...] = (
    ("emptying", "Emptying", "mdi:delete-empty", _is_emptying),
    ("washing", "Washing", "mdi:waves", _is_washing),
    ("drying", "Drying", "mdi:hair-dryer", _is_drying),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the dock activity sensors for every vacuum."""
    async_add_entities(
        DockActivitySensor(runtime, key, name, icon, value_fn)
        for runtime in config_entry.runtime_data
        for key, name, icon, value_fn in _DESCRIPTIONS
    )


class DockActivitySensor(RoborockLiveMapEntity, BinarySensorEntity):
    """One dock activity, reported from the live status."""

    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(
        self,
        runtime,
        key: str,
        name: str,
        icon: str,
        value_fn: Callable[[object], bool],
    ) -> None:
        """Initialize on the dock device."""
        super().__init__(
            runtime.coordinator,
            f"{runtime.coordinator.duid_slug}_dock_{key}",
            runtime.dock_device,
        )
        self._attr_name = name
        self._attr_icon = icon
        self._value_fn = value_fn

    @property
    def is_on(self) -> bool | None:
        """True while the dock is performing this activity."""
        status = self.coordinator.properties_api.status
        if status is None or status.state is None:
            return None
        return self._value_fn(status)
