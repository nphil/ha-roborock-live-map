"""Clean-history store: record list, selection, and historical map fetch.

The record list comes from the core coordinator's CleanSummaryTrait (core
already refreshes get_clean_summary every poll; only the LAST record's detail
is fetched by core). Details for every other record are fetched here with
get_clean_record and cached forever -- a finished record never changes.

The historical map (get_clean_record_map) rides the same dedicated map RPC
channel the live map uses. NOTE: the self-hosted local_roborock_server does
not currently answer that command (it times out at the library's 10 s
deadline on every channel, while get_map_v1 on the same channel returns in
~1 s). The code path is complete and will light up if the server ever
implements it; until then map_error carries the reason and the image entity
reports unavailable.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
import logging
from typing import Any

from homeassistant.components.roborock.coordinator import RoborockDataUpdateCoordinator
from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.util import dt as dt_util
from roborock.roborock_typing import RoborockCommand

from .const import HISTORY_STALE
from .entity import apply_room_names
from .map_payload import serialize

_LOGGER = logging.getLogger(__name__)

MAX_RECORDS = 20


def _format_duration(seconds: int | None) -> str | None:
    if not seconds:
        return None
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60}h {minutes % 60:02d}m"


class CleanHistory:
    """Clean records + the selected record's map, for one vacuum."""

    def __init__(self, hass, coordinator: RoborockDataUpdateCoordinator) -> None:
        """Initialize an empty history store."""
        self._hass = hass
        self._coordinator = coordinator
        self._listeners: list[Callable[[], None]] = []
        self._details: dict[int, dict[str, Any]] = {}
        self._fetch_lock = asyncio.Lock()
        self._last_refresh: datetime | None = None

        self.records: list[dict[str, Any]] = []
        self.selected: int | None = None
        self.map_content: Any | None = None
        self.map_error: str | None = None
        self.loaded = False

    # --- listeners -----------------------------------------------------------

    def add_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        """Subscribe to store updates; returns the unsubscribe callback."""
        self._listeners.append(listener)

        @callback
        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove

    @callback
    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    # --- records -------------------------------------------------------------

    @property
    def stale(self) -> bool:
        """True when the record list should be refetched."""
        return (
            self._last_refresh is None
            or dt_util.utcnow() - self._last_refresh > HISTORY_STALE
        )

    async def async_refresh(self, force: bool = False) -> None:
        """Refresh the record list, fetching details for new records only."""
        if not force and not self.stale:
            return
        if self._fetch_lock.locked():
            return
        async with self._fetch_lock:
            trait = self._coordinator.properties_api.clean_summary
            begins = sorted({int(b) for b in (trait.records or [])}, reverse=True)[
                :MAX_RECORDS
            ]
            for begin in begins:
                if begin in self._details:
                    continue
                try:
                    record = await trait.get_clean_record(begin)
                except Exception as err:  # noqa: BLE001 - keep the list usable
                    _LOGGER.debug("get_clean_record %s failed: %s", begin, err)
                    continue
                self._details[begin] = {
                    "begin": record.begin,
                    "end": record.end,
                    "duration": record.duration,
                    "area_m2": record.square_meter_area,
                    "complete": bool(record.complete) if record.complete is not None else None,
                }
            self._last_refresh = dt_util.utcnow()
            self._rebuild_records(begins)
            self.loaded = True
            self._notify()

    def _rebuild_records(self, begins: list[int]) -> None:
        """Assemble the public record list with unique human labels."""
        records: list[dict[str, Any]] = []
        seen: dict[str, int] = {}
        for begin in begins:
            detail = self._details.get(begin)
            if detail is None:
                continue
            started = dt_util.as_local(datetime.fromtimestamp(begin, tz=dt_util.UTC))
            parts = [started.strftime("%a %d %b, %H:%M")]
            if (duration := _format_duration(detail.get("duration"))) is not None:
                parts.append(duration)
            if detail.get("area_m2") is not None:
                parts.append(f"{detail['area_m2']} m²")
            label = " · ".join(parts)
            if (count := seen.get(label)) is not None:
                seen[label] = count + 1
                label = f"{label} ({count + 1})"
            else:
                seen[label] = 1
            records.append({**detail, "label": label})
        self.records = records

    def record_for_begin(self, begin: int) -> dict[str, Any] | None:
        """The public record dict for one begin timestamp."""
        return next((r for r in self.records if r["begin"] == begin), None)

    def begin_for_label(self, label: str) -> int | None:
        """The begin timestamp behind one of the current option labels."""
        return next((r["begin"] for r in self.records if r["label"] == label), None)

    # --- selection + historical map -------------------------------------------

    async def async_select(self, begin: int | None) -> None:
        """Select a record (None clears) and fetch its map."""
        self.selected = begin
        self.map_content = None
        self.map_error = None
        if begin is not None:
            try:
                self.map_content = await self._fetch_record_map(begin)
            except Exception as err:  # noqa: BLE001 - surfaced via map_error
                self.map_error = f"{type(err).__name__}: {err}"
                _LOGGER.debug(
                    "get_clean_record_map %s failed for %s: %s",
                    begin,
                    self._coordinator.duid_slug,
                    err,
                )
        self._notify()

    async def _fetch_record_map(self, begin: int) -> Any:
        """Fetch and parse one historical record map.

        Uses the dedicated map RPC channel (the one GET_MAP_V1 rides), because
        the generic command channel cannot carry map blobs.
        """
        map_trait = self._coordinator.properties_api.map_content
        raw = await map_trait.rpc_channel.send_command(
            RoborockCommand.GET_CLEAN_RECORD_MAP, params=[begin]
        )
        if not isinstance(raw, bytes):
            raise ValueError(f"Unexpected get_clean_record_map response: {type(raw)}")
        return await self._hass.async_add_executor_job(
            map_trait.converter.parse_map_content, raw
        )

    async def async_map_payload(self, begin: int, include_grid: bool = True) -> dict[str, Any]:
        """The live-map-shaped JSON payload for one historical record."""
        if self.selected == begin and self.map_content is not None:
            content = self.map_content
        else:
            content = await self._fetch_record_map(begin)
        map_data = content.map_data
        if map_data is None:
            raise ValueError("Record map did not parse")
        apply_room_names(self._coordinator, map_data)
        payload = await self._hass.async_add_executor_job(
            lambda: serialize(map_data, content.raw_api_response, include_grid=include_grid)
        )
        record = self.record_for_begin(begin) or {"begin": begin}
        payload["record"] = record
        return payload
