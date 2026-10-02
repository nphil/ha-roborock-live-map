"""Select entities for robot and dock settings.

Carpet-when-mopping mode and drying time read the shared RawSettings store;
mop wash mode and mop wash frequency ride python-roborock traits the core
coordinator already refreshes (wash_towel_mode, smart_wash_params); clean
history reads the CleanHistory store and drives the history map image.
"""

from __future__ import annotations

import logging
from typing import Any

from roborock.data.v1.v1_clean_modes import WashTowelModes

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import raw_commands as raw
from .entity import RoborockLiveMapEntity, RoborockSettingsEntity, run_command

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the selects for every vacuum."""
    async_add_entities(
        entity
        for runtime in config_entry.runtime_data
        for entity in (
            CarpetModeSelect(runtime),
            CleanHistorySelect(runtime),
            MopWashModeSelect(runtime),
            MopWashFrequencySelect(runtime),
            DryingTimeSelect(runtime),
        )
    )


class CarpetModeSelect(RoborockSettingsEntity, SelectEntity):
    """What Kevin does with carpets while mopping."""

    _attr_name = "Carpets when mopping"
    _attr_icon = "mdi:rug-outline"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = list(raw.CARPET_CLEAN_MODES)

    def __init__(self, runtime) -> None:
        """Initialize on the vacuum device."""
        super().__init__(
            runtime.coordinator,
            f"{runtime.coordinator.duid_slug}_carpet_mode",
            runtime.device,
            runtime.settings,
        )

    @property
    def current_option(self) -> str | None:
        """The current mode name, or None while unknown."""
        return raw.CARPET_CLEAN_MODE_NAMES.get(self._settings.carpet_clean_mode)

    async def async_select_option(self, option: str) -> None:
        """Set the carpet-when-mopping mode."""
        if option not in raw.CARPET_CLEAN_MODES:
            raise ServiceValidationError(f"Unknown carpet mode: {option}")
        code = await run_command(
            raw.set_carpet_clean_mode(self._command, option), "Carpet mode change"
        )
        self._settings.apply_carpet_clean_mode(code)


class DryingTimeSelect(RoborockSettingsEntity, SelectEntity):
    """How long the dock dries the mop after washing."""

    _attr_name = "Drying time"
    _attr_icon = "mdi:timer-outline"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = list(raw.DRYING_TIMES)

    def __init__(self, runtime) -> None:
        """Initialize on the dock device."""
        super().__init__(
            runtime.coordinator,
            f"{runtime.coordinator.duid_slug}_dock_drying_time",
            runtime.dock_device,
            runtime.settings,
        )

    @property
    def current_option(self) -> str | None:
        """The current auto-dry duration, or None while unknown."""
        dryer = self._settings.dryer or {}
        dry_time = (dryer.get("on") or {}).get("dry_time")
        return raw.DRYING_TIME_NAMES.get(dry_time)

    async def async_select_option(self, option: str) -> None:
        """Set the auto-dry duration."""
        if option not in raw.DRYING_TIMES:
            raise ServiceValidationError(f"Unknown drying time: {option}")
        await run_command(raw.set_drying_time(self._command, option), "Drying time change")
        self._settings.apply_dryer({"on": {"dry_time": raw.DRYING_TIMES[option]}})


class MopWashModeSelect(RoborockLiveMapEntity, SelectEntity):
    """How thoroughly the dock washes the mop (light / balanced / deep)."""

    _attr_name = "Mop wash mode"
    _attr_icon = "mdi:washing-machine"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, runtime) -> None:
        """Initialize on the dock device, backed by the wash-towel trait."""
        super().__init__(
            runtime.coordinator,
            f"{runtime.coordinator.duid_slug}_dock_mop_wash_mode",
            runtime.dock_device,
        )
        self._trait = runtime.coordinator.properties_api.wash_towel_mode

    @property
    def available(self) -> bool:
        """Available while the dock supports mop washing."""
        return super().available and self._trait is not None

    @property
    def options(self) -> list[str]:
        """The wash modes this dock supports."""
        if self._trait is None:
            return []
        return [mode.value for mode in self._trait.wash_towel_mode_options]

    @property
    def current_option(self) -> str | None:
        """The current wash mode name."""
        if self._trait is None or self._trait.wash_mode is None:
            return None
        mode = self._trait.wash_mode
        if isinstance(mode, WashTowelModes):
            return mode.value
        try:  # the converter may leave a raw code on some firmware replies
            return WashTowelModes.from_code(int(mode)).value
        except (TypeError, ValueError):
            _LOGGER.debug("Unknown wash_mode code: %r", mode)
            return None

    async def async_select_option(self, option: str) -> None:
        """Set the mop wash mode and re-read it from the dock."""
        try:
            mode = WashTowelModes(option)
        except ValueError as err:
            raise ServiceValidationError(f"Unknown mop wash mode: {option}") from err
        await run_command(self._trait.set_wash_towel_mode(mode), "Mop wash mode change")
        await self._trait.refresh()


class MopWashFrequencySelect(RoborockLiveMapEntity, SelectEntity):
    """How often the dock washes the mop during a clean.

    smart_wash=1 is the app's "Smart" (the dock adapts to how dirty the mop
    reads); smart_wash=0 washes on a fixed interval (10/15/20/25 minutes).
    """

    _attr_name = "Mop wash frequency"
    _attr_icon = "mdi:water-sync"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = list(raw.WASH_FREQUENCY_LABELS.values())

    def __init__(self, runtime) -> None:
        """Initialize on the dock device, backed by the smart-wash trait."""
        super().__init__(
            runtime.coordinator,
            f"{runtime.coordinator.duid_slug}_dock_mop_wash_frequency",
            runtime.dock_device,
        )
        self._trait = runtime.coordinator.properties_api.smart_wash_params

    @property
    def available(self) -> bool:
        """Available while the dock reports smart wash parameters."""
        return super().available and self._trait is not None

    @property
    def current_option(self) -> str | None:
        """The current frequency label."""
        if self._trait is None:
            return None
        key = raw.wash_frequency_key(self._trait.smart_wash, self._trait.wash_interval)
        return raw.WASH_FREQUENCY_LABELS.get(key) if key else None

    async def async_select_option(self, option: str) -> None:
        """Set the wash frequency and re-read it from the dock."""
        key = raw.WASH_FREQUENCY_KEYS.get(option)
        if key is None:
            raise ServiceValidationError(f"Unknown mop wash frequency: {option}")
        await run_command(raw.set_wash_frequency(self._command, key), "Mop wash frequency change")
        await self._trait.refresh()


class CleanHistorySelect(RoborockLiveMapEntity, SelectEntity):
    """Pick a past clean; the history map image follows the selection."""

    _attr_name = "Clean history"
    _attr_icon = "mdi:history"

    def __init__(self, runtime) -> None:
        """Initialize on the vacuum device, backed by the history store."""
        super().__init__(
            runtime.coordinator,
            f"{runtime.coordinator.duid_slug}_clean_history",
            runtime.device,
        )
        self._history = runtime.history

    async def async_added_to_hass(self) -> None:
        """Subscribe to history-store updates."""
        await super().async_added_to_hass()
        self.async_on_remove(self._history.add_listener(self._handle_history_update))

    @callback
    def _handle_history_update(self) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Available once at least one record has been read."""
        return super().available and self._history.loaded

    @property
    def options(self) -> list[str]:
        """Record labels, newest first."""
        return [record["label"] for record in self._history.records]

    @property
    def current_option(self) -> str | None:
        """The label of the selected record."""
        if self._history.selected is None:
            return None
        record = self._history.record_for_begin(self._history.selected)
        return record["label"] if record else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The full record list for cards, plus any map fetch problem."""
        attributes: dict[str, Any] = {"records": self._history.records}
        if self._history.selected is not None:
            attributes["selected"] = self._history.selected
        if self._history.map_error:
            attributes["map_error"] = self._history.map_error
        return attributes

    async def async_select_option(self, option: str) -> None:
        """Select a record and fetch its map."""
        begin = self._history.begin_for_label(option)
        if begin is None:
            raise ServiceValidationError(f"Unknown clean record: {option}")
        await self._history.async_select(begin)
