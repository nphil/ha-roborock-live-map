"""Roborock Live Map integration.

Piggybacks on the core Roborock integration's coordinators, so the vacuum is
polled exactly once. It adds no fast polling of its own: one slow background
store re-reads the raw (trait-unwrapped) settings every few minutes, entities
update optimistically after every write, and its entities attach to the core
integration's existing vacuum/dock devices rather than creating their own.
"""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url, remove_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.loader import async_get_integration

from .const import DOMAIN
from .entity import roborock_device, roborock_dock_device
from .fast_map import FastMapRefresher
from .history import CleanHistory
from .remote import RemoteSession
from .schedules import ScheduleManager
from .runtime import VacuumRuntime
from .services import (
    async_register_remote_command,
    async_register_schedule_commands,
    async_setup_services,
)
from .settings import RawSettings
from .view import (
    RoborockLiveMapDataView,
    RoborockLiveMapHistoryMapView,
    RoborockLiveMapHistoryView,
    RoborockLiveMapPhotoView,
)

_LOGGER = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).parent / "frontend"
STATIC_URL_BASE = "/roborock_live_map_static"
CARD_FILE = "roborock-schedule-card.js"

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.IMAGE,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Roborock Live Map from a config entry."""
    runtimes: list[VacuumRuntime] = []

    for roborock_entry in hass.config_entries.async_entries("roborock"):
        if roborock_entry.state != ConfigEntryState.LOADED:
            continue
        for coordinator in roborock_entry.runtime_data.v1:
            device = roborock_device(hass, coordinator, roborock_entry.entry_id)
            schedules = ScheduleManager(
                hass, coordinator.duid_slug, device.id if device is not None else None
            )
            await schedules.async_load()
            runtimes.append(
                VacuumRuntime(
                    coordinator=coordinator,
                    device=device,
                    dock_device=roborock_dock_device(hass, coordinator, roborock_entry.entry_id),
                    fast_map=FastMapRefresher(hass, coordinator),
                    settings=RawSettings(hass, coordinator),
                    history=CleanHistory(hass, coordinator),
                    remote=RemoteSession(coordinator),
                    schedules=schedules,
                )
            )

    if not runtimes:
        raise ConfigEntryNotReady("No Roborock entries loaded. Cannot start.")

    entry.runtime_data = runtimes
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtimes

    for runtime in runtimes:
        slug = runtime.coordinator.duid_slug
        entry.async_on_unload(runtime.fast_map.async_start())
        entry.async_on_unload(runtime.settings.async_start())
        # First fetches run in the background: entities start unavailable and
        # light up as the stores fill.
        entry.async_create_task(
            hass, runtime.settings.async_refresh(), f"{DOMAIN}_settings_{slug}"
        )
        entry.async_create_task(
            hass, runtime.history.async_refresh(force=True), f"{DOMAIN}_history_{slug}"
        )

    entry.async_on_unload(async_setup_services(hass))

    # Views and the WS remote command are global; register them once.
    if not hass.data.get(f"{DOMAIN}_views_registered"):
        hass.http.register_view(RoborockLiveMapDataView())
        hass.http.register_view(RoborockLiveMapPhotoView())
        hass.http.register_view(RoborockLiveMapHistoryView())
        hass.http.register_view(RoborockLiveMapHistoryMapView())
        async_register_remote_command(hass)
        async_register_schedule_commands(hass)
        hass.data[f"{DOMAIN}_views_registered"] = True

    # Static paths cannot be unregistered, so register them once per HA run.
    if FRONTEND_DIR.is_dir() and not hass.data.get(f"{DOMAIN}_static_registered"):
        await hass.http.async_register_static_paths(
            [StaticPathConfig(STATIC_URL_BASE, str(FRONTEND_DIR), cache_headers=False)]
        )
        hass.data[f"{DOMAIN}_static_registered"] = True
    if FRONTEND_DIR.is_dir():
        # The module URL is per entry lifecycle: added on setup, removed on unload.
        version = (await async_get_integration(hass, DOMAIN)).version
        card_url = f"{STATIC_URL_BASE}/{CARD_FILE}?v={version}"
        _add_card_url(hass, card_url)
        entry.async_on_unload(lambda: _remove_card_url(hass, card_url))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Last, so a failed setup never leaves schedule timers running.
    for runtime in runtimes:
        runtime.schedules.async_start()
    return True


def _add_card_url(hass: HomeAssistant, url: str) -> None:
    """Load the card on every dashboard; reference-counted across entries."""
    refs: dict[str, int] = hass.data.setdefault(f"{DOMAIN}_card_refs", {})
    refs[url] = refs.get(url, 0) + 1
    add_extra_js_url(hass, url)


def _remove_card_url(hass: HomeAssistant, url: str) -> None:
    """Drop the card only when the last entry using it unloads."""
    refs: dict[str, int] = hass.data.setdefault(f"{DOMAIN}_card_refs", {})
    remaining = refs.get(url, 0) - 1
    if remaining > 0:
        refs[url] = remaining
        return
    refs.pop(url, None)
    try:
        remove_extra_js_url(hass, url)
    except KeyError:
        pass  # already removed


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry, stopping any remote-control session first."""
    for runtime in entry.runtime_data or []:
        await runtime.remote.async_shutdown()
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        for runtime in entry.runtime_data or []:
            await runtime.schedules.async_stop()
    return unloaded


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: ConfigEntry, device
) -> bool:
    """Allow detaching this entry from a device.

    This integration owns no devices of its own; any device still carrying this
    entry is a leftover from before entities attached to the core device.
    """
    return True
