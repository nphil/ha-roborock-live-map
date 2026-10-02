"""Per-vacuum runtime bundle shared by every platform, service and view.

One VacuumRuntime is built per core Roborock coordinator when this
integration's config entry loads. Platforms receive the list through
config_entry.runtime_data; services, the WebSocket remote command and the HTTP
views resolve theirs from hass.data[DOMAIN].
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.roborock.coordinator import RoborockDataUpdateCoordinator
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import DOMAIN
from .fast_map import FastMapRefresher
from .history import CleanHistory
from .remote import RemoteSession
from .settings import RawSettings


@dataclass
class VacuumRuntime:
    """Everything this integration keeps for one vacuum."""

    coordinator: RoborockDataUpdateCoordinator
    device: dr.DeviceEntry | None
    dock_device: dr.DeviceEntry | None
    fast_map: FastMapRefresher
    settings: RawSettings
    history: CleanHistory
    remote: RemoteSession


def all_runtimes(hass: HomeAssistant) -> list[VacuumRuntime]:
    """Every vacuum runtime across every loaded config entry."""
    runtimes: list[VacuumRuntime] = []
    for entry_runtimes in hass.data.get(DOMAIN, {}).values():
        runtimes.extend(entry_runtimes)
    return runtimes


def runtime_for_device_id(hass: HomeAssistant, device_id: str) -> VacuumRuntime | None:
    """The runtime owning a core Roborock device (vacuum or its dock)."""
    for runtime in all_runtimes(hass):
        if runtime.device is not None and runtime.device.id == device_id:
            return runtime
        if runtime.dock_device is not None and runtime.dock_device.id == device_id:
            return runtime
    return None


def runtime_for_entity(hass: HomeAssistant, entity_id: str) -> VacuumRuntime | None:
    """The runtime behind any entity attached to a tracked vacuum or dock."""
    entry = er.async_get(hass).async_get(entity_id)
    if entry is None or entry.device_id is None:
        return None
    return runtime_for_device_id(hass, entry.device_id)
