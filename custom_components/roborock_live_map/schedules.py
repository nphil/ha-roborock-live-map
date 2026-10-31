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
* Optional cleaning settings (``settings``: mode, vacuum power, mop intensity)
  are applied by the run itself, never at save time and never to a robot that
  is busy. A schedule without settings changes nothing on the robot. With
  settings, a run first checks that the vacuum is docked or idle and that
  every entity, option and service it needs is available; only then does it
  set the mode, then the optional vacuum power, then the optional mop
  intensity, and finally start. Any refusal or failure before the start means
  the robot is not started; the settings applied so far stay (nothing restores
  them) and the outcome says which ones. The settings stay on the robot after
  the run.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
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
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, State, callback
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

# Cleaning settings a schedule may carry. Selecting a mode on the robot also
# resets vacuum power, mop intensity and mop route to that mode's presets, so
# the mode is always applied first and the optional values after it. The
# vacuum powers and mop intensities differ between robot models, so a schedule
# stores any bounded option name (never the ``off``/``custom`` sentinels) and
# the run checks it against what the robot offers at that moment.
MODES = ("vacuum", "vac_and_mop", "mop", "custom")
UNSAFE_OPTIONS = ("off", "custom")  # sentinels: never a scheduled value
MAX_OPTION_LENGTH = 64
FAN_MODES = ("vacuum", "vac_and_mop")  # modes where vacuum power applies
MOP_MODES = ("vac_and_mop", "mop")  # modes where mop intensity applies
SETTINGS_KEYS = ("mode", "fan_speed", "mop_intensity")
IDLE_STATES = ("docked", "idle")  # the only states in which settings may change


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


def _usable_option(value: Any) -> bool:
    """True for a bounded option name that is not an ``off``/``custom`` sentinel."""
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_OPTION_LENGTH
        and value == value.strip()
        and value.isprintable()
        and value.casefold() not in UNSAFE_OPTIONS
    )


def validate_settings(raw: Any) -> dict[str, str]:
    """Validate a schedule's cleaning settings. ``{}`` keeps the robot's own."""
    if not isinstance(raw, dict):
        raise ValueError("settings must be an object")
    if unknown := sorted(map(str, set(raw) - set(SETTINGS_KEYS))):
        raise ValueError(f"unknown settings field(s): {', '.join(unknown)}")
    if not raw:
        return {}
    if "mode" not in raw:
        raise ValueError("settings.mode is required when settings are given")
    mode = raw["mode"]
    if not isinstance(mode, str) or mode not in MODES:
        raise ValueError(f"settings.mode must be one of: {', '.join(MODES)}")
    out = {"mode": mode}
    for key, modes, label in (
        ("fan_speed", FAN_MODES, "vacuum power"),
        ("mop_intensity", MOP_MODES, "mop intensity"),
    ):
        if key not in raw:
            continue
        value = raw[key]
        if not _usable_option(value):
            raise ValueError(
                f"settings.{key} must be an option name of 1 to {MAX_OPTION_LENGTH} "
                f"characters, not {' or '.join(UNSAFE_OPTIONS)}"
            )
        if mode not in modes:
            raise ValueError(f"settings.{key} ({label}) does not apply to mode {mode}")
        out[key] = value
    return out


def validate_schedule_input(raw: Any) -> dict[str, Any]:
    """Validate and normalise a client schedule. Raises ValueError.

    ``settings`` is only in the result when the client sent it: left out on an
    update it keeps the stored settings, on a create it means none ({}).
    """
    if not isinstance(raw, dict):
        raise ValueError("schedule must be an object")
    allowed = {
        "id", "name", "time", "days", "rooms", "repeat", "enabled", "settings", *READ_ONLY_FIELDS
    }
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

    if "settings" in raw:  # left out by clients that predate settings: not touched on update
        out["settings"] = validate_settings(raw["settings"])
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
        tuple(sorted(item["settings"].items())),
    )


def _clean_record(raw: Any) -> dict[str, Any] | None:
    """Rebuild one stored record, or None when it is unusable."""
    try:
        item = validate_schedule_input({k: raw[k] for k in raw if k in (
            "id", "name", "time", "days", "rooms", "repeat", "enabled", "settings")})
        if "id" not in item:
            raise ValueError("missing id")
    except (ValueError, TypeError, KeyError, AttributeError) as err:
        _LOGGER.warning("Dropping invalid stored schedule %r: %s", raw, err)
        return None
    item["last_run"] = raw.get("last_run") if isinstance(raw.get("last_run"), str) else None
    item["last_result"] = (
        raw.get("last_result") if isinstance(raw.get("last_result"), str) else None
    )
    item.setdefault("settings", {})
    item["rev"] = 0
    return item


def _options(state: State, attribute: str) -> list[str]:
    """The string options a control state reports (empty when it reports none)."""
    value = state.attributes.get(attribute)
    if not isinstance(value, (list, tuple)):
        return []
    return [option for option in value if isinstance(option, str)]


def _usable_options(options: list[str]) -> list[str]:
    """Options a schedule may use, in the robot's order, without duplicates."""
    return [option for option in dict.fromkeys(options) if _usable_option(option)]


@dataclass(frozen=True)
class _Controls:
    """The robot's cleaning controls as Home Assistant currently exposes them."""

    vacuum: str
    mode_select: str
    mop_select: str | None
    mop_problem: str | None
    capabilities: dict[str, list[str]]


class _SettingsIncomplete(HomeAssistantError):
    """A run sent settings to the robot and then stopped before starting it."""


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
            "settings": dict(item["settings"]),
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
                **{
                    k: current[k]
                    for k in ("id", "name", "time", "days", "rooms", "repeat", "settings")
                },
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
            if "settings" in data:
                fields["settings"] = data["settings"]
            old = self._items.get(sid) if sid is not None else None
            if sid is None:
                if len(self._items) >= MAX_SCHEDULES:
                    raise ScheduleError(
                        "limit_reached", f"At most {MAX_SCHEDULES} schedules per vacuum"
                    )
                sid = uuid.uuid4().hex
                item = {"settings": {}, **fields, "id": sid, "last_run": None, "last_result": None}
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

    # --- the robot's controls ---------------------------------------------------

    def _device_entity_id(self, domain: str, translation_key: str | None = None) -> str | None:
        """The enabled core Roborock entity of ``domain`` on this vacuum's device."""
        if self._device_id is None:
            return None
        registry = er.async_get(self._hass)
        for entry in er.async_entries_for_device(registry, self._device_id):
            if (
                entry.domain == domain
                and entry.platform == "roborock"
                and (translation_key is None or entry.translation_key == translation_key)
            ):
                return entry.entity_id
        return None

    def _vacuum_entity_id(self) -> str | None:
        return self._device_entity_id("vacuum")

    def _live_state(self, entity_id: str | None, label: str, *, strict: bool = False) -> State:
        """The state of a control, or ScheduleError("unavailable") saying what is wrong.

        ``strict`` also rejects ``unknown``; selects stay usable while unknown
        because their options do not depend on the current value.
        """
        if entity_id is None:
            raise ScheduleError(
                "unavailable", f"Home Assistant has no enabled {label} for this robot"
            )
        state = self._hass.states.get(entity_id)
        bad = (STATE_UNAVAILABLE, STATE_UNKNOWN) if strict else (STATE_UNAVAILABLE,)
        if state is None or state.state in bad:
            raise ScheduleError("unavailable", f"{entity_id} ({label}) is unavailable")
        return state

    def _controls(self) -> _Controls:
        """Resolve and read the controls a settings run needs.

        The vacuum and the cleaning mode select are always needed; without
        them this raises ScheduleError("unavailable"). The mop intensity select
        is only needed for a mop intensity: when it is missing or unavailable
        no mop intensity is offered and ``mop_problem`` says why.
        """
        vacuum_id = self._vacuum_entity_id()
        vacuum = self._live_state(vacuum_id, "vacuum", strict=True)
        mode_id = self._device_entity_id("select", "cleaning_mode")
        mode = self._live_state(mode_id, "cleaning mode select")
        mop_id = self._device_entity_id("select", "mop_intensity")
        mop: State | None = None
        mop_problem: str | None = None
        if mop_id is None:
            mop_problem = "Home Assistant has no enabled mop intensity select for this robot"
        else:
            try:
                mop = self._live_state(mop_id, "mop intensity select")
            except ScheduleError as err:
                mop_problem = str(err)
        return _Controls(
            vacuum=vacuum_id,
            mode_select=mode_id,
            mop_select=mop_id,
            mop_problem=mop_problem,
            capabilities={
                "modes": [m for m in MODES if m in _options(mode, "options")],
                "fan_speeds": _usable_options(_options(vacuum, "fan_speed_list")),
                "mop_intensities": _usable_options(
                    _options(mop, "options") if mop is not None else []
                ),
            },
        )

    def async_capabilities(self) -> dict[str, list[str]]:
        """Modes, vacuum powers and mop intensities this robot offers right now.

        Read from the robot's live Home Assistant entities and filtered to the
        values a schedule may use (never ``off`` or ``custom`` intensities).
        The mop intensities are empty when the mop intensity select is missing
        or unavailable. Raises ScheduleError("unavailable") when the vacuum or
        the cleaning mode select cannot be read.
        """
        return {key: list(values) for key, values in self._controls().capabilities.items()}

    def _plan_settings(self, item: dict[str, Any]) -> list[tuple[str, str, str, dict[str, Any]]]:
        """Preflight a settings run; return its ordered service calls.

        Everything that can be known before touching the robot is checked
        here: entities, their states, the offered options and the services.
        Raises when anything is missing, so nothing has been changed yet.
        """
        settings = item["settings"]
        controls = self._controls()
        offered = controls.capabilities
        mode = settings["mode"]
        fan = settings.get("fan_speed")
        mop = settings.get("mop_intensity")

        missing = []
        if mode not in offered["modes"]:
            missing.append(
                f"cleaning mode {mode!r} (offers: {', '.join(offered['modes']) or 'none'})"
            )
        if fan is not None and fan not in offered["fan_speeds"]:
            missing.append(
                f"vacuum power {fan!r} (offers: {', '.join(offered['fan_speeds']) or 'none'})"
            )
        if mop is not None and controls.mop_problem is not None:
            raise HomeAssistantError(f"{controls.mop_problem}; nothing was changed")
        if mop is not None and mop not in offered["mop_intensities"]:
            missing.append(
                f"mop intensity {mop!r} (offers: {', '.join(offered['mop_intensities']) or 'none'})"
            )
        if missing:
            raise HomeAssistantError(
                f"the robot does not offer {'; '.join(missing)}; nothing was changed"
            )

        steps = [
            (
                f"cleaning mode {mode}",
                "select",
                "select_option",
                {"entity_id": controls.mode_select, "option": mode},
            )
        ]
        if fan is not None:
            steps.append(
                (
                    f"vacuum power {fan}",
                    "vacuum",
                    "set_fan_speed",
                    {"entity_id": controls.vacuum, "fan_speed": fan},
                )
            )
        if mop is not None:
            steps.append(
                (
                    f"mop intensity {mop}",
                    "select",
                    "select_option",
                    {"entity_id": controls.mop_select, "option": mop},
                )
            )
        needed = {(domain, service) for _label, domain, service, _data in steps}
        needed.add(("vacuum", "send_command" if item["rooms"] else "start"))
        for domain, service in sorted(needed):
            if not self._hass.services.has_service(domain, service):
                raise HomeAssistantError(
                    f"Home Assistant has no {domain}.{service} service; nothing was changed"
                )
        return steps

    def _interrupted(self, item: dict[str, Any], entity_id: str) -> str | None:
        """Why a run with settings must stop now, or None to carry on."""
        if self._stopped:
            return "the schedules are stopping"
        current = self._items.get(item["id"])
        if current is None:
            return "the schedule was deleted"
        if current["rev"] != item["rev"]:
            return "the schedule was changed"
        state = self._hass.states.get(entity_id)
        if state is None or state.state not in IDLE_STATES:
            return f"vacuum is {state.state if state is not None else 'unavailable'}"
        return None

    @staticmethod
    def _progress(applied: list[str]) -> str:
        if not applied:
            return "no earlier setting had been applied"
        return f"already applied and left in place: {', '.join(applied)}"

    async def _async_apply_settings(
        self, item: dict[str, Any], entity_id: str, state: str
    ) -> str | None:
        """Set the schedule's settings in order; a result string means do not start.

        Order: mode (resets vacuum power, mop intensity and mop route to its
        presets), then vacuum power, then mop intensity. Preflight failures
        raise before the robot is touched. A failure or an interruption after
        that raises and names the steps already applied; they are never rolled
        back.
        """
        if state not in IDLE_STATES:
            return f"skipped: vacuum is {state}; settings not changed"
        steps = self._plan_settings(item)
        applied: list[str] = []
        for label, domain, service, data in steps:
            if (reason := self._interrupted(item, entity_id)) is not None:
                if not applied:
                    return f"skipped: {reason}; settings not changed"
                raise _SettingsIncomplete(
                    f"stopped before {label}: {reason}; {self._progress(applied)}; "
                    "the vacuum was not started"
                )
            try:
                await self._hass.services.async_call(domain, service, data, blocking=True)
            except Exception as err:  # noqa: BLE001 - reported, never retried
                raise _SettingsIncomplete(
                    f"setting {label} failed ({err}); it may or may not have reached the "
                    f"robot; {self._progress(applied)}; the vacuum was not started"
                ) from err
            applied.append(label)
        if (reason := self._interrupted(item, entity_id)) is not None:
            raise _SettingsIncomplete(
                f"stopped before starting: {reason}; {self._progress(applied)}; "
                "the vacuum was not started"
            )
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
        if item["settings"] and (
            skipped := await self._async_apply_settings(item, entity_id, state.state)
        ):
            return skipped
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

    @staticmethod
    def _is_older(item: dict[str, Any], due: datetime) -> bool:
        """True when no run of this schedule is recorded at or after ``due``."""
        try:
            return item["last_run"] is None or datetime.fromisoformat(item["last_run"]) < due
        except (ValueError, TypeError):
            return True

    async def _async_run(self, sid: str, rev: int, due: datetime) -> None:
        """Make one attempt to start the clean, then record the outcome.

        The outcome of a schedule that was edited meanwhile is dropped, with
        one exception: a run that had already sent settings to the robot and
        then stopped is recorded on the surviving schedule (never over a newer
        result, never on a deleted one), because the robot was left changed.
        """
        async with self._run_lock:
            item = self._items.get(sid)
            if self._stopped or item is None or item["rev"] != rev:
                return  # deleted, edited or unloaded since it was claimed
            sent_settings = False
            try:
                result = await self._async_start_clean(item)
            except _SettingsIncomplete as err:
                _LOGGER.warning(
                    "Schedule %r stopped after changing settings: %s", item["name"], err
                )
                result, sent_settings = f"error: {err}", True
            except Exception as err:  # noqa: BLE001 - recorded, never retried
                _LOGGER.warning("Schedule %r failed to start: %s", item["name"], err)
                result = f"error: {err}"
            async with self._lock:
                item = self._items.get(sid)
                if self._stopped or item is None:
                    return
                if item["rev"] != rev and not (sent_settings and self._is_older(item, due)):
                    return
                item["last_run"] = due.isoformat()
                item["last_result"] = result
                try:
                    await self._persist()
                except Exception as err:  # noqa: BLE001 - in-memory state stays right
                    _LOGGER.error("Could not save schedule result: %s", err)
