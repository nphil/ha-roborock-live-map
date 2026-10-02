"""Services and the low-latency WebSocket remote command.

Services take the vacuum's entity_id (it belongs to the core Roborock
integration; we resolve it to our runtime through the entity registry's
device link). Every map-affecting service re-reads the map afterwards so
cards show what the robot actually stored.

The WebSocket command roborock_live_map/remote drives RemoteSession and
always answers with the documented {"ok": bool, "latency_ms": float} shape
(plus "error" on failure); send_error is reserved for protocol problems
like an unknown entity.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from . import raw_commands as raw
from .const import DOMAIN
from .entity import run_command
from .runtime import VacuumRuntime, all_runtimes, runtime_for_entity


_AFTER_EDIT_TIMEOUT = 15  # seconds per re-read after a split/merge
_LOGGER = logging.getLogger(__name__)


def _runtime_for_call(hass: HomeAssistant, call: ServiceCall) -> VacuumRuntime:
    """Resolve a service call's entity_id to a vacuum runtime."""
    entity_id = call.data["entity_id"]
    runtime = runtime_for_entity(hass, entity_id)
    if runtime is None:
        raise ServiceValidationError(f"No Roborock vacuum found behind {entity_id}")
    return runtime


def _code_by_name(mapping: dict[int, str], name: str, kind: str) -> int:
    """Invert a {code: name} mapping, with a helpful error on unknown names."""
    for code, value in mapping.items():
        if value == name:
            return code
    valid = ", ".join(sorted(set(mapping.values())))
    raise ServiceValidationError(f"Unknown {kind} {name!r}; expected one of: {valid}")


async def _set_room_order(hass: HomeAssistant, call: ServiceCall) -> None:
    runtime = _runtime_for_call(hass, call)
    rooms = [int(segment) for segment in call.data["rooms"]]
    await run_command(raw.set_room_order(runtime.coordinator.properties_api.command, rooms), "Set room order")
    runtime.settings.apply_room_order(rooms)
    await runtime.fast_map.async_refresh()


async def _set_room_settings(hass: HomeAssistant, call: ServiceCall) -> None:
    runtime = _runtime_for_call(hass, call)
    status = runtime.coordinator.properties_api.status
    rows: list[dict[str, Any]] = []
    for room in call.data["rooms"]:
        rows.append(
            {
                "segment": int(room["segment"]),
                "fan_power": _code_by_name(
                    dict(status.fan_speed_mapping or {}), room["fan_speed"], "fan speed"
                ),
                "water_box_mode": _code_by_name(
                    dict(status.water_mode_mapping or {}), room["water"], "water level"
                ),
                "mop_mode": _code_by_name(
                    dict(status.mop_route_mapping or {}), room["route"], "mop route"
                ),
                "repeat": int(room.get("repeat", 1)),
            }
        )
    await run_command(
        raw.set_room_settings_raw(runtime.coordinator.properties_api.command, rows),
        "Set room settings",
    )
    runtime.settings.apply_room_settings(rows)
    await runtime.fast_map.async_refresh()


async def _split_room(hass: HomeAssistant, call: ServiceCall) -> None:
    runtime = _runtime_for_call(hass, call)
    data = call.data
    await run_command(
        raw.split_room(
            runtime.coordinator.properties_api.command,
            int(data["segment"]),
            round(data["x0"]),
            round(data["y0"]),
            round(data["x1"]),
            round(data["y1"]),
        ),
        "Split room",
    )
    await _after_map_edit(runtime)


async def _merge_rooms(hass: HomeAssistant, call: ServiceCall) -> None:
    runtime = _runtime_for_call(hass, call)
    segments = [int(segment) for segment in call.data["segments"]]
    if len(segments) != 2:
        raise ServiceValidationError("merge_rooms needs exactly two segments")
    await run_command(
        raw.merge_rooms(runtime.coordinator.properties_api.command, segments[0], segments[1]),
        "Merge rooms",
    )
    await _after_map_edit(runtime)


async def _after_map_edit(runtime: VacuumRuntime) -> None:
    """Re-read everything a room edit can change: rooms, map, raw settings.

    Each step is bounded: the edit has already succeeded, and a slow re-read
    must not leave the dashboard waiting (a merge once stalled here for >90 s).
    """
    steps = (
        ("rooms", runtime.coordinator.properties_api.rooms.refresh()),
        ("map", runtime.fast_map.async_refresh()),
        ("settings", runtime.settings.async_refresh()),
    )
    for label, step in steps:
        try:
            async with asyncio.timeout(_AFTER_EDIT_TIMEOUT):
                await step
        except Exception as err:  # noqa: BLE001 - the edit itself succeeded
            _LOGGER.debug("%s refresh after map edit failed: %s", label, err)
    await runtime.coordinator.async_request_refresh()


_ROOMS_SERVICE_FIELDS = {
    vol.Required("entity_id"): cv.entity_id,
}

SET_ROOM_ORDER_SCHEMA = vol.Schema(
    {
        **_ROOMS_SERVICE_FIELDS,
        vol.Required("rooms"): vol.All(cv.ensure_list, [vol.Coerce(int)]),
    }
)

SET_ROOM_SETTINGS_SCHEMA = vol.Schema(
    {
        **_ROOMS_SERVICE_FIELDS,
        vol.Required("rooms"): vol.All(
            cv.ensure_list,
            [
                vol.Schema(
                    {
                        vol.Required("segment"): vol.Coerce(int),
                        vol.Required("fan_speed"): cv.string,
                        vol.Required("water"): cv.string,
                        vol.Required("route"): cv.string,
                        vol.Optional("repeat", default=1): vol.All(
                            vol.Coerce(int), vol.Range(min=1, max=3)
                        ),
                    }
                )
            ],
        ),
    }
)

SPLIT_ROOM_SCHEMA = vol.Schema(
    {
        **_ROOMS_SERVICE_FIELDS,
        vol.Required("segment"): vol.Coerce(int),
        vol.Required("x0"): vol.Coerce(float),
        vol.Required("y0"): vol.Coerce(float),
        vol.Required("x1"): vol.Coerce(float),
        vol.Required("y1"): vol.Coerce(float),
    }
)

MERGE_ROOMS_SCHEMA = vol.Schema(
    {
        **_ROOMS_SERVICE_FIELDS,
        vol.Required("segments"): vol.All(cv.ensure_list, [vol.Coerce(int)]),
    }
)

_SERVICES = {
    "set_room_order": (SET_ROOM_ORDER_SCHEMA, _set_room_order),
    "set_room_settings": (SET_ROOM_SETTINGS_SCHEMA, _set_room_settings),
    "split_room": (SPLIT_ROOM_SCHEMA, _split_room),
    "merge_rooms": (MERGE_ROOMS_SCHEMA, _merge_rooms),
}


@callback
def async_setup_services(hass: HomeAssistant) -> callback:
    """Register every service; returns the function that removes them."""

    async def refresh_map(_call: ServiceCall) -> None:
        # Map edits (no-go zones, walls) land on the robot at once, but while
        # it is docked the core integration re-reads the map only every few
        # minutes.
        for runtime in all_runtimes(hass):
            await runtime.fast_map.async_refresh()

    hass.services.async_register(DOMAIN, "refresh_map", refresh_map)
    for name, (schema, handler) in _SERVICES.items():
        # HA calls service handlers with the call only; the handlers also need hass.
        hass.services.async_register(DOMAIN, name, partial(handler, hass), schema=schema)

    @callback
    def _remove() -> None:
        hass.services.async_remove(DOMAIN, "refresh_map")
        for name in _SERVICES:
            hass.services.async_remove(DOMAIN, name)

    return _remove


@callback
def async_register_remote_command(hass: HomeAssistant) -> None:
    """Register the roborock_live_map/remote WebSocket command (once)."""

    @websocket_api.websocket_command(
        {
            vol.Required("type"): "roborock_live_map/remote",
            vol.Required("entity_id"): cv.entity_id,
            vol.Required("action"): vol.In(["start", "move", "stop"]),
            vol.Optional("velocity", default=0.0): vol.Coerce(float),
            vol.Optional("omega", default=0.0): vol.Coerce(float),
        }
    )
    @websocket_api.async_response
    async def handle_remote(
        hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
    ) -> None:
        """Drive one remote-control action and report its latency."""
        runtime = runtime_for_entity(hass, msg["entity_id"])
        if runtime is None:
            connection.send_error(
                msg["id"], "not_found", f"No Roborock vacuum behind {msg['entity_id']}"
            )
            return
        result = await runtime.remote.handle(msg["action"], msg["velocity"], msg["omega"])
        connection.send_result(msg["id"], result)

    websocket_api.async_register_command(hass, handle_remote)
