"""Switches for robot/dock settings python-roborock has no trait for.

Carpet boost (get/set_carpet_mode) lives on the vacuum; auto-dry
(app_get/set_dryer_setting status) lives on the dock. Both read the shared
RawSettings store and update it optimistically after a write.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import raw_commands as raw
from .entity import RoborockSettingsEntity, run_command

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the switches for every vacuum."""
    async_add_entities(
        entity
        for runtime in config_entry.runtime_data
        for entity in (
            CarpetBoostSwitch(runtime),
            AutoDrySwitch(runtime),
        )
    )


class CarpetBoostSwitch(RoborockSettingsEntity, SwitchEntity):
    """Boost suction automatically when carpet is detected."""

    _attr_name = "Carpet boost"
    _attr_icon = "mdi:rug"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, runtime) -> None:
        """Initialize on the vacuum device."""
        super().__init__(
            runtime.coordinator,
            f"{runtime.coordinator.duid_slug}_carpet_boost",
            runtime.device,
            runtime.settings,
        )

    @property
    def is_on(self) -> bool | None:
        """On while carpet boost is enabled."""
        carpet = self._settings.carpet
        if carpet is None:
            return None
        return bool(carpet.get("enable"))

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable carpet boost, preserving the tuned motor fields."""
        merged = await run_command(
            raw.set_carpet_boost_enable(self._command, True), "Carpet boost on"
        )
        self._settings.apply_carpet_boost(merged)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable carpet boost, preserving the tuned motor fields."""
        merged = await run_command(
            raw.set_carpet_boost_enable(self._command, False), "Carpet boost off"
        )
        self._settings.apply_carpet_boost(merged)


class AutoDrySwitch(RoborockSettingsEntity, SwitchEntity):
    """Automatically dry the mop after it is washed."""

    _attr_name = "Auto dry"
    _attr_icon = "mdi:hair-dryer"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, runtime) -> None:
        """Initialize on the dock device."""
        super().__init__(
            runtime.coordinator,
            f"{runtime.coordinator.duid_slug}_dock_auto_dry",
            runtime.dock_device,
            runtime.settings,
        )

    @property
    def is_on(self) -> bool | None:
        """On while the auto-dry preference is enabled."""
        dryer = self._settings.dryer
        if dryer is None:
            return None
        return bool(dryer.get("status"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the configured drying duration alongside the preference."""
        dryer = self._settings.dryer or {}
        dry_time = (dryer.get("on") or {}).get("dry_time")
        attributes: dict[str, Any] = {}
        if dry_time is not None:
            attributes["dry_time_hours"] = round(dry_time / 3600, 1)
        return attributes

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable auto-dry after mop washing."""
        await run_command(raw.set_auto_dry(self._command, True), "Auto dry on")
        self._settings.apply_dryer({"status": 1})

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable auto-dry after mop washing."""
        await run_command(raw.set_auto_dry(self._command, False), "Auto dry off")
        self._settings.apply_dryer({"status": 0})
