"""Local cleaning schedules, kept and fired by Home Assistant.

The self-hosted Roborock server cannot manage the robot's native schedules,
so the schedules live here instead: persisted in Home Assistant's .storage
(one store per vacuum) and fired by one timer per vacuum.

Guarantees:

* Times are wall-clock times in Home Assistant's configured time zone and
  follow DST. A time that does not exist (spring-forward gap) runs once,
  shifted forward by the gap; a time that happens twice (fall-back) runs once,
  at its first occurrence.
* A schedule only fires for an occurrence that arrives while Home Assistant is
  running. Occurrences missed while it was down, or during startup, are never
  replayed: a restart must not start a cleaning nobody expects. Creating,
  re-enabling or editing the time or days of a schedule likewise never fires
  for a moment that already passed.
* Each occurrence makes exactly one attempt to start the robot. There are no
  retries: after an uncertain failure a repeated command could start an
  unintended clean. The outcome is recorded in ``last_result``.
* Every timer callback and finished run checks it still belongs to the current
  schedule revision; edits, deletes, time-zone changes and unload make older
  callbacks harmless.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta, tzinfo
import logging
import re
from typing import Any
import uuid

from homeassistant.const import (
    EVENT_CORE_CONFIG_UPDATE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_KEY = "roborock_live_map.schedules.{slug}"

MAX_SCHEDULES = 50
MAX_NAME_LENGTH = 64
MAX_ROOMS = 64
MAX_SEGMENT = 65535
MAX_REPEAT = 3
MAX_ID_LENGTH = 64

MAX_TIMER_SLEEP = timedelta(hours=1)  # re-check at least hourly (clock jumps)

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

# Fields a client may send back unchanged from a list result; ignored on save.
READ_ONLY_FIELDS = ("last_run", "last_result", "next_run")


class ScheduleError(Exception):
    """A schedule operation failed; ``code`` is the WebSocket error code."""

    def __init__(self, code: str, message: str) -> None:
        """Store the WebSocket error code with its message."""
        super().__init__(message)
        self.code = code


def _strict_int(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{what} must be an integer")
    return value


def validate_schedule_input(raw: Any) -> dict[str, Any]:
    """Validate and normalise a client schedule. Raises ValueError."""
    if not isinstance(raw, dict):
        raise ValueError("schedule must be an object")
    allowed = {"id", "name", "time", "days", "rooms", "repeat", "enabled", *READ_ONLY_FIELDS}
    if unknown := sorted(set(raw) - allowed):
        raise ValueError(f"unknown schedule field(s): {', '.join(map(str, unknown))}")
    for required in ("name", "time", "days", "rooms", "repeat", "enabled"):
        if required not in raw:
            raise ValueError(f"schedule.{required} is required")

    out: dict[str, Any] = {}

    if (sid := raw.get("id")) is not None:
        if not isinstance(sid, str) or not sid or len(sid) > MAX_ID_LENGTH:
            raise ValueError("id must be a non-empty string")
        out["id"] = sid

    name = raw["name"]
    if not isinstance(name, str) or not (name := name.strip()):
        raise ValueError("name must be a non-empty string")
    if len(name) > MAX_NAME_LENGTH:
        raise ValueError(f"name must be at most {MAX_NAME_LENGTH} characters")
    out["name"] = name

    time = raw["time"]
    if not isinstance(time, str) or not _TIME_RE.match(time):
        raise ValueError("time must be HH:MM (24 hour)")
    out["time"] = time

    days = raw["days"]
    if not isinstance(days, list) or not days:
        raise ValueError("days must be a non-empty list")
    day_set = {_strict_int(d, "day") for d in days}
    if len(day_set) != len(days) or not day_set <= set(range(7)):
        raise ValueError("days must be unique integers 0 (Monday) to 6 (Sunday)")
    out["days"] = sorted(day_set)

    rooms = raw["rooms"]
    if not isinstance(rooms, list) or len(rooms) > MAX_ROOMS:
        raise ValueError(f"rooms must be a list of at most {MAX_ROOMS} room ids")
    room_ints = [_strict_int(r, "room") for r in rooms]
    if len(set(room_ints)) != len(room_ints) or any(
        not 0 <= r <= MAX_SEGMENT for r in room_ints
    ):
        raise ValueError(f"rooms must be unique integers 0 to {MAX_SEGMENT}")
    out["rooms"] = room_ints

    repeat = _strict_int(raw["repeat"], "repeat")
    if not 1 <= repeat <= MAX_REPEAT:
        raise ValueError(f"repeat must be 1 to {MAX_REPEAT}")
    if not room_ints and repeat != 1:
        raise ValueError("repeat must be 1 when cleaning the whole home (no rooms)")
    out["repeat"] = repeat

    if not isinstance(raw["enabled"], bool):
        raise ValueError("enabled must be true or false")
    out["enabled"] = raw["enabled"]
    return out


# --- time maths ---------------------------------------------------------------


def _occurrence(tz: tzinfo, day: date, time: str) -> datetime:
    """UTC instant of HH:MM local on ``day`` (gap -> shifted forward, fold -> first)."""
    hour, minute = int(time[:2]), int(time[3:])
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz).astimezone(UTC)


def latest_due(
    item: dict[str, Any], after: datetime, upto: datetime, tz: tzinfo
) -> datetime | None:
    """The latest occurrence with ``after < occurrence <= upto``, if any."""
    if upto <= after:
        return None
    day = upto.astimezone(tz).date() + timedelta(days=1)  # tz offset slack
    for _ in range(400):
        if day.weekday() in item["days"]:
            candidate = _occurrence(tz, day, item["time"])
            if candidate <= after:
                return None
            if candidate <= upto:
                return candidate
        day -= timedelta(days=1)
    return None


def next_after(item: dict[str, Any], after: datetime, tz: tzinfo) -> datetime | None:
    """The first occurrence strictly after ``after``."""
    day = after.astimezone(tz).date() - timedelta(days=1)
    for _ in range(10):
        if day.weekday() in item["days"]:
            candidate = _occurrence(tz, day, item["time"])
            if candidate > after:
                return candidate
        day += timedelta(days=1)
    return None


def _signature(item: dict[str, Any]) -> tuple:
    return (
        item["time"],
        tuple(item["days"]),
        tuple(item["rooms"]),
        item["repeat"],
        item["enabled"],
    )


def _clean_record(raw: Any) -> dict[str, Any] | None:
    """Rebuild one stored record, or None when it is unusable."""
    try:
        item = validate_schedule_input({k: raw[k] for k in raw if k in (
            "id", "name", "time", "days", "rooms", "repeat", "enabled")})
        if "id" not in item:
            raise ValueError("missing id")
    except (ValueError, TypeError, KeyError, AttributeError) as err:
        _LOGGER.warning("Dropping invalid stored schedule %r: %s", raw, err)
        return None
    item["last_run"] = raw.get("last_run") if isinstance(raw.get("last_run"), str) else None
    item["last_result"] = (
        raw.get("last_result") if isinstance(raw.get("last_result"), str) else None
    )
    item["rev"] = 0
    return item


class ScheduleManager:
    """Schedules, persistence and firing for one vacuum."""

    def __init__(self, hass: HomeAssistant, slug: str, device_id: str | None) -> None:
        """Create an unloaded manager for the vacuum behind ``device_id``."""
        self._hass = hass
        self._device_id = device_id
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, STORAGE_KEY.format(slug=slug), atomic_writes=True
        )
        self._items: dict[str, dict[str, Any]] = {}
        self._cursor: dict[str, datetime] = {}  # id -> fire only after this instant
        self._lock = asyncio.Lock()  # guards mutations + persistence
        self._run_lock = asyncio.Lock()  # one robot command sequence at a time
        self._tasks: set[asyncio.Task] = set()
        self._timer: CALLBACK_TYPE | None = None
        self._unsubs: list[CALLBACK_TYPE] = []
        self._generation = 0
        self._rev = 0
        self.loaded = False
        self._started = False  # HA fully started and start() called
        self._stopped = False

    # --- lifecycle ---------------------------------------------------------

    async def async_load(self) -> None:
        """Read the persisted schedules (call before anyone can query)."""
        data = await self._store.async_load()
        items = (data or {}).get("schedules", []) if isinstance(data, dict) else []
        for raw in items if isinstance(items, list) else []:
            if (item := _clean_record(raw)) is not None and item["id"] not in self._items:
                self._items[item["id"]] = item
        self.loaded = True

    @callback
    def async_start(self) -> None:
        """Begin firing once Home Assistant has finished starting."""
        self._unsubs.append(
            self._hass.bus.async_listen(EVENT_CORE_CONFIG_UPDATE, self._on_config_update)
        )
        self._unsubs.append(async_at_started(self._hass, self._on_ha_started))

    async def async_stop(self) -> None:
        """Stop timers and in-flight runs for good. Idempotent."""
        self._stopped = True
        self._generation += 1
        self._cancel_timer()
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    @callback
    def _on_ha_started(self, _hass: HomeAssistant) -> None:
        if self._stopped:
            return
        self._started = True
        now = dt_util.utcnow()
        for sid in self._items:
            self._cursor[sid] = now  # never replay anything missed while down
        self._evaluate()

    @callback
    def _on_config_update(self, event) -> None:
        """A time-zone change must not retroactively fire anything."""
        if self._stopped or "time_zone" not in (event.data or {}):
            return
        now = dt_util.utcnow()
        for sid in self._items:
            self._cursor[sid] = now
        self._evaluate()

    # --- queries ----------------------------------------------------------------

    def _tz(self) -> tzinfo:
        return dt_util.get_time_zone(self._hass.config.time_zone) or dt_util.get_default_time_zone()

    def _public(self, item: dict[str, Any], now: datetime, tz: tzinfo) -> dict[str, Any]:
        next_run = next_after(item, now, tz) if item["enabled"] else None
        return {
            "id": item["id"],
            "name": item["name"],
            "time": item["time"],
            "days": list(item["days"]),
            "rooms": list(item["rooms"]),
            "repeat": item["repeat"],
            "enabled": item["enabled"],
            "last_run": item["last_run"],
            "last_result": item["last_result"],
            "next_run": next_run.isoformat() if next_run else None,
        }

    def async_list(self) -> list[dict[str, Any]]:
        """All schedules, ordered by time then name."""
        now, tz = dt_util.utcnow(), self._tz()
        return [
            self._public(item, now, tz)
            for item in sorted(self._items.values(), key=lambda i: (i["time"], i["name"], i["id"]))
        ]

    # --- mutations ----------------------------------------------------------

    def _check_running(self) -> None:
        if self._stopped or not self.loaded:
            raise ScheduleError("unavailable", "Schedules are not running (integration unloading)")

    async def _persist(self) -> None:
        await self._store.async_save(
            {
                "schedules": [
                    {k: v for k, v in item.items() if k != "rev"}
                    for item in self._items.values()
                ]
            }
        )

    async def async_save(self, data: dict[str, Any]) -> dict[str, Any]:
        """Create (no id) or update (known id) a validated schedule."""
        return await self._async_save(lambda: data)

    async def async_set_enabled(self, sid: str, enabled: bool) -> dict[str, Any]:
        """Flip only ``enabled`` on the stored schedule (read under the lock)."""

        def build() -> dict[str, Any]:
            current = self._items.get(sid)
            if current is None:
                raise ScheduleError("not_found", f"No schedule {sid!r}")
            return {
                **{k: current[k] for k in ("id", "name", "time", "days", "rooms", "repeat")},
                "enabled": enabled,
            }

        return await self._async_save(build)

    async def _async_save(self, build) -> dict[str, Any]:
        now = dt_util.utcnow()
        async with self._lock:
            self._check_running()
            data = build()
            sid = data.get("id")
            fields = {k: data[k] for k in ("name", "time", "days", "rooms", "repeat", "enabled")}
            old = self._items.get(sid) if sid is not None else None
            if sid is None:
                if len(self._items) >= MAX_SCHEDULES:
                    raise ScheduleError(
                        "limit_reached", f"At most {MAX_SCHEDULES} schedules per vacuum"
                    )
                sid = uuid.uuid4().hex
                item = {**fields, "id": sid, "last_run": None, "last_result": None}
                reset = True
            elif old is None:
                raise ScheduleError("not_found", f"No schedule {sid!r}")
            else:
                item = {**old, **fields}
                reset = (
                    item["time"] != old["time"]
                    or item["days"] != old["days"]
                    or (item["enabled"] and not old["enabled"])
                )
            self._rev += 1
            item["rev"] = (
                self._rev if old is None or _signature(item) != _signature(old) else old["rev"]
            )
            self._items[sid] = item
            try:
                await self._persist()
            except Exception as err:
                if old is None:
                    del self._items[sid]
                else:
                    self._items[sid] = old
                _LOGGER.error("Could not save schedules: %s", err)
                raise ScheduleError("save_failed", f"Could not save schedule: {err}") from err
            if reset:
                self._cursor[sid] = now
            result = self._public(item, now, self._tz())
        self._evaluate()
        return result

    async def async_delete(self, sid: str) -> None:
        """Delete a schedule."""
        async with self._lock:
            self._check_running()
            old = self._items.pop(sid, None)
            if old is None:
                raise ScheduleError("not_found", f"No schedule {sid!r}")
            try:
                await self._persist()
            except Exception as err:
                self._items[sid] = old
                _LOGGER.error("Could not save schedules: %s", err)
                raise ScheduleError("save_failed", f"Could not delete schedule: {err}") from err
            self._cursor.pop(sid, None)
        self._evaluate()

    # --- firing -----------------------------------------------------------------

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer()
            self._timer = None

    @callback
    def _evaluate(self) -> None:
        """Claim every due occurrence, then arm the timer for the next one."""
        self._cancel_timer()
        self._generation += 1
        if self._stopped or not self._started:
            return
        now, tz = dt_util.utcnow(), self._tz()
        for item in list(self._items.values()):
            if not item["enabled"]:
                continue
            cursor = self._cursor.get(item["id"])
            if cursor is None:  # loaded but not yet seen: nothing is due
                self._cursor[item["id"]] = cursor = now
            if (due := latest_due(item, cursor, now, tz)) is not None:
                self._cursor[item["id"]] = now
                task = self._hass.async_create_background_task(
                    self._async_run(item["id"], item["rev"], due),
                    f"roborock_live_map_schedule_{item['id']}",
                )
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

        upcoming = [
            n
            for item in self._items.values()
            if item["enabled"] and (n := next_after(item, now, tz)) is not None
        ]
        target = min([*upcoming, now + MAX_TIMER_SLEEP])
        generation = self._generation

        @callback
        def _fire(_when: datetime) -> None:
            if generation != self._generation:
                return  # superseded by a newer evaluation
            self._timer = None
            self._evaluate()

        self._timer = async_track_point_in_utc_time(self._hass, _fire, target)

    def _vacuum_entity_id(self) -> str | None:
        if self._device_id is None:
            return None
        registry = er.async_get(self._hass)
        for entry in er.async_entries_for_device(registry, self._device_id):
            if entry.domain == "vacuum" and entry.platform == "roborock":
                return entry.entity_id
        return None

    async def _async_start_clean(self, item: dict[str, Any]) -> str:
        entity_id = self._vacuum_entity_id()
        if entity_id is None:
            raise HomeAssistantError("vacuum entity not found")
        state = self._hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            raise HomeAssistantError(f"{entity_id} is unavailable")
        if state.state == "cleaning":
            return "skipped: already cleaning"
        if item["rooms"]:
            await self._hass.services.async_call(
                "vacuum",
                "send_command",
                {
                    "entity_id": entity_id,
                    "command": "app_segment_clean",
                    "params": [{"segments": list(item["rooms"]), "repeat": item["repeat"]}],
                },
                blocking=True,
            )
        else:
            await self._hass.services.async_call(
                "vacuum", "start", {"entity_id": entity_id}, blocking=True
            )
        return "ok"

    async def _async_run(self, sid: str, rev: int, due: datetime) -> None:
        """Make one attempt to start the clean, then record the outcome."""
        async with self._run_lock:
            item = self._items.get(sid)
            if self._stopped or item is None or item["rev"] != rev:
                return  # deleted, edited or unloaded since it was claimed
            try:
                result = await self._async_start_clean(item)
            except Exception as err:  # noqa: BLE001 - recorded, never retried
                _LOGGER.warning("Schedule %r failed to start: %s", item["name"], err)
                result = f"error: {err}"
            async with self._lock:
                item = self._items.get(sid)
                if self._stopped or item is None or item["rev"] != rev:
                    return
                item["last_run"] = due.isoformat()
                item["last_result"] = result
                try:
                    await self._persist()
                except Exception as err:  # noqa: BLE001 - in-memory state stays right
                    _LOGGER.error("Could not save schedule result: %s", err)
