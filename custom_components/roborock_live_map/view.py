"""HTTP views serving live Roborock map data to the 3D card.

The parsed map carries a full cleaning path and an occupancy grid, which are far
too large to live in entity attributes without flooding the recorder. They are
served here instead; a small sensor entity carries the revision so the card
knows when to refetch.
"""

from __future__ import annotations

import logging
from functools import partial
from typing import Any

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .entity import apply_room_names, current_map_content
from .map_payload import serialize
from .runtime import all_runtimes

_LOGGER = logging.getLogger(__name__)

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _find_runtime(hass: HomeAssistant, duid: str) -> Any | None:
    """The runtime for one vacuum, matched by duid or slug."""
    for runtime in all_runtimes(hass):
        coordinator = runtime.coordinator
        if duid in (getattr(coordinator, "duid", None), coordinator.duid_slug):
            return runtime
    return None


def _find(hass: HomeAssistant, duid: str) -> Any | None:
    """The coordinator for one vacuum, matched by duid or slug."""
    runtime = _find_runtime(hass, duid)
    return runtime.coordinator if runtime is not None else None


class RoborockLiveMapDataView(HomeAssistantView):
    """Serve the parsed map for one vacuum as JSON."""

    url = "/api/roborock_live_map/{duid}"
    name = "api:roborock_live_map:data"

    async def get(self, request: web.Request, duid: str) -> web.Response:
        """Return the live map payload.

        Pass grid=0 to omit the occupancy grid, which only changes when the map
        itself is re-scanned. Clients cache it by the hash in grid.hash.
        """
        hass: HomeAssistant = request.app["hass"]
        coordinator = _find(hass, duid)
        if coordinator is None:
            return self.json_message("Unknown vacuum", web.HTTPNotFound.status_code)

        map_content = current_map_content(coordinator)
        if map_content is None or map_content.map_data is None:
            return self.json_message(
                "No map data available yet", web.HTTPServiceUnavailable.status_code
            )
        apply_room_names(coordinator, map_content.map_data)

        include_grid = request.query.get("grid", "1") != "0"
        payload = await hass.async_add_executor_job(
            partial(
                serialize,
                map_content.map_data,
                map_content.raw_api_response,
                include_grid=include_grid,
            )
        )
        payload["duid"] = coordinator.duid_slug
        last_update = getattr(coordinator, "last_home_update", None)
        payload["last_update"] = last_update.isoformat() if last_update else None
        return self.json(payload)


class RoborockLiveMapPhotoView(HomeAssistantView):
    """Serve an obstacle photo captured by the vacuum's camera."""

    url = "/api/roborock_live_map/{duid}/photo/{photo_id}"
    name = "api:roborock_live_map:photo"

    async def get(
        self, request: web.Request, duid: str, photo_id: str
    ) -> web.Response:
        """Fetch one obstacle photo by its map photo id."""
        hass: HomeAssistant = request.app["hass"]
        coordinator = _find(hass, duid)
        if coordinator is None:
            return self.json_message("Unknown vacuum", web.HTTPNotFound.status_code)

        trait = coordinator.properties_api.obstacle_photos
        if trait is None:
            return self.json_message(
                "Vacuum does not support obstacle photos",
                web.HTTPNotImplemented.status_code,
            )

        try:
            photo = await trait.get_photo(photo_id)
        except Exception as err:  # noqa: BLE001 - surfaced to the caller as 502
            _LOGGER.debug("Obstacle photo %s failed: %s", photo_id, err)
            return self.json_message(
                f"Could not fetch obstacle photo: {err}",
                web.HTTPBadGateway.status_code,
            )

        content = photo.image_content
        content_type = "image/png" if content.startswith(_PNG_MAGIC) else "image/jpeg"
        return web.Response(
            body=content,
            content_type=content_type,
            headers={"Cache-Control": "public, max-age=86400"},
        )


class RoborockLiveMapHistoryView(HomeAssistantView):
    """Serve the clean-history record list for one vacuum as JSON.

    Pass refresh=1 to force a re-read; by default a list younger than
    HISTORY_STALE is served straight from the store.
    """

    url = "/api/roborock_live_map/{duid}/history"
    name = "api:roborock_live_map:history"

    async def get(self, request: web.Request, duid: str) -> web.Response:
        """Return {"records": [{begin, end, duration, area_m2, complete, label}]}."""
        hass: HomeAssistant = request.app["hass"]
        runtime = _find_runtime(hass, duid)
        if runtime is None:
            return self.json_message("Unknown vacuum", web.HTTPNotFound.status_code)
        force = request.query.get("refresh", "0") == "1"
        try:
            await runtime.history.async_refresh(force=force)
        except Exception as err:  # noqa: BLE001 - serve whatever we have
            _LOGGER.debug("History refresh failed for %s: %s", duid, err)
        return self.json({"records": runtime.history.records})


class RoborockLiveMapHistoryMapView(HomeAssistantView):
    """Serve one historical record map in the live-map payload shape.

    Same JSON contract as the live data view (rooms, path, vacuum_position,
    charger, image/grid, ...) plus a "record" key. NOTE: the historical map
    blob comes from get_clean_record_map, which the self-hosted
    local_roborock_server does not currently answer -- until it does, this
    endpoint returns 502 with the timeout as the reason.
    """

    url = "/api/roborock_live_map/{duid}/history/{begin}"
    name = "api:roborock_live_map:history_map"

    async def get(self, request: web.Request, duid: str, begin: str) -> web.Response:
        """Return the map payload for the record that began at `begin`."""
        hass: HomeAssistant = request.app["hass"]
        runtime = _find_runtime(hass, duid)
        if runtime is None:
            return self.json_message("Unknown vacuum", web.HTTPNotFound.status_code)
        try:
            begin_ts = int(begin)
        except ValueError:
            return self.json_message(
                "begin must be epoch seconds", web.HTTPBadRequest.status_code
            )
        include_grid = request.query.get("grid", "1") != "0"
        try:
            payload = await runtime.history.async_map_payload(
                begin_ts, include_grid=include_grid
            )
        except Exception as err:  # noqa: BLE001 - surfaced to the caller as 502
            _LOGGER.debug("History map %s failed for %s: %s", begin_ts, duid, err)
            return self.json_message(
                f"Could not fetch the clean history map: {err}",
                web.HTTPBadGateway.status_code,
            )
        payload["duid"] = runtime.coordinator.duid_slug
        return self.json(payload)
