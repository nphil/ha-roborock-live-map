"""Action buttons: map backup/restore on the vacuum, dock actions on the dock.

Every press asks the core coordinator for a fresh status right away so the
dock-activity binary sensors and the dashboard react without waiting for the
next scheduled poll.
"""

from __future__ import annotations

import logging

from roborock.roborock_typing import RoborockCommand

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import raw_commands as raw
from .entity import RoborockLiveMapEntity, run_command

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the buttons for every vacuum."""
    async_add_entities(
        entity
        for runtime in config_entry.runtime_data
        for entity in (
            BackUpMapButton(runtime),
            RestoreMapBackupButton(runtime),
            EmptyDustbinButton(runtime),
            WashMopButton(runtime),
            StartDryingButton(runtime),
            StopDryingButton(runtime),
        )
    )


class RuntimeButton(RoborockLiveMapEntity, ButtonEntity):
    """A button that keeps a reference to its vacuum runtime."""

    def __init__(self, runtime, unique_id: str, device) -> None:
        """Initialize the button on the given device."""
        super().__init__(runtime.coordinator, unique_id, device)
        self._runtime = runtime

    async def _after_press(self, refresh_map: bool = False) -> None:
        """Pull fresh status (and optionally the map) after an action."""
        if refresh_map:
            await self._runtime.fast_map.async_refresh()
        await self.coordinator.async_request_refresh()


class BackUpMapButton(RuntimeButton):
    """Save the current map into the dock's single backup slot."""

    _attr_name = "Back up map"
    _attr_icon = "mdi:content-save-outline"

    def __init__(self, runtime) -> None:
        """Initialize on the vacuum device."""
        super().__init__(runtime, f"{runtime.coordinator.duid_slug}_back_up_map", runtime.device)

    async def async_press(self) -> None:
        """Back up the current map and record its timestamp."""
        await run_command(raw.backup_map(self._command), "Map backup")
        try:
            ts = await raw.get_backup_timestamp(self._command)
        except Exception as err:  # noqa: BLE001 - the backup itself succeeded
            _LOGGER.debug("Backup timestamp re-read failed: %s", err)
            ts = None
        self._runtime.settings.apply_map_backup(ts)


class RestoreMapBackupButton(RuntimeButton):
    """Restore the map from the single backup slot."""

    _attr_name = "Restore map backup"
    _attr_icon = "mdi:restore"

    def __init__(self, runtime) -> None:
        """Initialize on the vacuum device."""
        super().__init__(
            runtime, f"{runtime.coordinator.duid_slug}_restore_map_backup", runtime.device
        )

    async def async_press(self) -> None:
        """Restore the backup, then re-read the map and raw settings."""
        await run_command(raw.restore_map_backup(self._command), "Map restore")
        await self._after_press(refresh_map=True)
        await self._runtime.settings.async_refresh()


class EmptyDustbinButton(RuntimeButton):
    """Tell the dock to empty Kevin's dustbin now."""

    _attr_name = "Empty dustbin"
    _attr_icon = "mdi:trash-can-outline"

    def __init__(self, runtime) -> None:
        """Initialize on the dock device."""
        super().__init__(
            runtime, f"{runtime.coordinator.duid_slug}_dock_empty_dustbin", runtime.dock_device
        )

    async def async_press(self) -> None:
        """Start a dust-emptying cycle."""
        await run_command(
            self._command.send(RoborockCommand.APP_START_COLLECT_DUST, []), "Empty dustbin"
        )
        await self._after_press()


class WashMopButton(RuntimeButton):
    """Tell the dock to wash the mop now."""

    _attr_name = "Wash mop"
    _attr_icon = "mdi:washing-machine"

    def __init__(self, runtime) -> None:
        """Initialize on the dock device."""
        super().__init__(
            runtime, f"{runtime.coordinator.duid_slug}_dock_wash_mop", runtime.dock_device
        )

    async def async_press(self) -> None:
        """Start a mop-wash cycle."""
        await run_command(self._command.send(RoborockCommand.APP_START_WASH, []), "Wash mop")
        await self._after_press()


class StartDryingButton(RuntimeButton):
    """Start drying the mop now."""

    _attr_name = "Start drying"
    _attr_icon = "mdi:hair-dryer"

    def __init__(self, runtime) -> None:
        """Initialize on the dock device."""
        super().__init__(
            runtime, f"{runtime.coordinator.duid_slug}_dock_start_drying", runtime.dock_device
        )

    async def async_press(self) -> None:
        """Start a drying cycle."""
        await run_command(raw.start_drying(self._command), "Start drying")
        await self._after_press()


class StopDryingButton(RuntimeButton):
    """Stop drying the mop."""

    _attr_name = "Stop drying"
    _attr_icon = "mdi:stop-circle-outline"

    def __init__(self, runtime) -> None:
        """Initialize on the dock device."""
        super().__init__(
            runtime, f"{runtime.coordinator.duid_slug}_dock_stop_drying", runtime.dock_device
        )

    async def async_press(self) -> None:
        """Stop the drying cycle."""
        await run_command(raw.stop_drying(self._command), "Stop drying")
        await self._after_press()
