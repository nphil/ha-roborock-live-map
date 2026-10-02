"""Faster map refresh while the vacuum is cleaning.

The core coordinator re-fetches the map at most every IMAGE_CACHE_INTERVAL
(30 s) while cleaning -- a cloud rate-limit precaution. On the local stack there
is no rate limit, so this re-fetches the current map on a shorter interval, only
while the vacuum is cleaning. It drives the same HomeTrait.refresh() the core
integration uses, so the refreshed map lands in the shared cache and the core
coordinator's own trait listener pushes it to every entity.

Done here rather than by editing the core integration's constants, which would
be overwritten on every Home Assistant update.
"""

from __future__ import annotations

from datetime import datetime
import logging

from homeassistant.components.roborock.coordinator import RoborockDataUpdateCoordinator
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import FAST_MAP_INTERVAL

_LOGGER = logging.getLogger(__name__)


class FastMapRefresher:
    """Re-fetches one vacuum's map on a short interval while it cleans."""

    def __init__(self, hass: HomeAssistant, coordinator: RoborockDataUpdateCoordinator) -> None:
        """Initialize the refresher."""
        self._hass = hass
        self._coordinator = coordinator
        self._running = False

    @callback
    def async_start(self) -> CALLBACK_TYPE:
        """Start the interval; returns the function that stops it."""
        return async_track_time_interval(self._hass, self._async_tick, FAST_MAP_INTERVAL)

    async def _async_tick(self, _now: datetime) -> None:
        # Skip rather than queue: a slow fetch must not stack up behind itself.
        status = self._coordinator.properties_api.status
        if self._running or status is None or not status.in_cleaning:
            return
        try:
            await self.async_refresh()
        except Exception as err:  # noqa: BLE001 - a missed tick is harmless; the next one retries
            _LOGGER.debug("Fast map refresh failed for %s: %s", self._coordinator.duid_slug, err)

    async def async_refresh(self) -> None:
        """Re-fetch the map now (also used after map edits, whatever the robot is doing)."""
        home = self._coordinator.properties_api.home
        if home is None or self._running:
            return
        self._running = True
        try:
            await home.refresh()
            self._coordinator.last_home_update = dt_util.utcnow()
        finally:
            self._running = False
