"""Cleaning settings on schedules: contract, migration, capabilities and run order.

The robot is stood in for by real Home Assistant ``vacuum`` and ``select``
entities registered under the ``roborock`` platform, so every command travels
through Home Assistant's real service layer (option validation, service
registration, entity registry lookups). They only record what they are asked,
in order; nothing here says how a physical robot behaves.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
import types

import pytest
from pytest_homeassistant.common import (
    MockConfigEntry,
    MockModule,
    MockPlatform,
    async_fire_time_changed,
    mock_config_flow,
    mock_integration,
    mock_platform,
)

from homeassistant.components.select import SelectEntity
from homeassistant.components.vacuum import StateVacuumEntity, VacuumActivity, VacuumEntityFeature
from homeassistant.config_entries import ConfigEntryState, ConfigFlow
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo

from custom_components.roborock_live_map import schedules as sch
from custom_components.roborock_live_map import services
from custom_components.roborock_live_map.runtime import VacuumRuntime

VACUUM = "vacuum.robo"
MODE_SELECT = "select.robo_cleaning_mode"
MOP_SELECT = "select.robo_mop_intensity"

MODE_OPTIONS = ["vacuum", "vac_and_mop", "mop", "custom"]
FAN_OPTIONS = ["quiet", "balanced", "turbo", "max", "max_plus", "off", "custom"]
MOP_OPTIONS = ["off", "mild", "standard", "intense", "custom"]

SLUG = "duid1"
STORE_KEY = f"roborock_live_map.schedules.{SLUG}"


class _RoborockFlow(ConfigFlow):
    """Lets the stand-in ``roborock`` config entry be set up."""

    VERSION = 1


@pytest.fixture(autouse=True)
def _roborock_flow():
    with mock_config_flow("roborock", _RoborockFlow):
        yield


# --- a recording robot made of real HA entities ---------------------------------


@dataclass
class Robot:
    """What the fake entities were asked, in order, plus failure/hook switches."""

    log: list[tuple] = field(default_factory=list)
    fail: dict[str, str] = field(default_factory=dict)
    hooks: dict[str, Callable[[], Awaitable[None]]] = field(default_factory=dict)
    vacuum: FakeVacuum | None = None
    mode: FakeSelect | None = None
    mop: FakeSelect | None = None
    device: dr.DeviceEntry | None = None
    freezer: object = None

    async def record(self, name: str, *args) -> None:
        """Log one command, run its hook, then fail it if asked to."""
        self.log.append((name, *args))
        if (hook := self.hooks.get(name)) is not None:
            await hook()
        if (message := self.fail.get(name)) is not None:
            raise HomeAssistantError(message)

    def set_activity(self, activity: VacuumActivity | str) -> None:
        self.vacuum._attr_activity = VacuumActivity(activity)
        self.vacuum.async_write_ha_state()

    def set_available(self, entity, available: bool) -> None:
        entity._attr_available = available
        entity.async_write_ha_state()


class FakeVacuum(StateVacuumEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False  # a poll would rewrite the state the test just set
    _attr_name = None
    _attr_unique_id = SLUG
    _attr_device_info = DeviceInfo(identifiers={("roborock", SLUG)}, name="Robo")
    _attr_supported_features = (
        VacuumEntityFeature.START | VacuumEntityFeature.FAN_SPEED | VacuumEntityFeature.SEND_COMMAND
    )
    _attr_activity = VacuumActivity.DOCKED
    _attr_fan_speed = "custom"

    def __init__(self, robot: Robot, fan_speeds: list[str]) -> None:
        self.robot = robot
        self._attr_fan_speed_list = fan_speeds

    async def async_start(self) -> None:
        await self.robot.record("vacuum.start")
        self._attr_activity = VacuumActivity.CLEANING
        self.async_write_ha_state()

    async def async_send_command(self, command, params=None, **kwargs) -> None:
        await self.robot.record("vacuum.send_command", command, params)
        self._attr_activity = VacuumActivity.CLEANING
        self.async_write_ha_state()

    async def async_set_fan_speed(self, fan_speed: str, **kwargs) -> None:
        await self.robot.record("vacuum.set_fan_speed", fan_speed)
        self._attr_fan_speed = fan_speed
        self.async_write_ha_state()


class FakeSelect(SelectEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_info = DeviceInfo(identifiers={("roborock", SLUG)}, name="Robo")

    def __init__(self, robot, key: str, name: str, translation_key: str, options: list[str]):
        self.robot = robot
        self._key = key
        self._attr_unique_id = f"{key}_{SLUG}"
        self._attr_name = name
        self._attr_translation_key = translation_key
        self._attr_options = options
        self._attr_current_option = "custom"

    async def async_select_option(self, option: str) -> None:
        await self.robot.record(f"select.{self._key}", option)
        self._attr_current_option = option
        self.async_write_ha_state()


async def build_robot(
    hass: HomeAssistant,
    *,
    fan_speeds: list[str] | None = None,
    mode_options: list[str] | None = None,
    mop_options: list[str] | None = None,
    with_mop: bool = True,
) -> Robot:
    """Set up the fake robot as a ``roborock`` config entry with real platforms."""
    robot = Robot()
    robot.vacuum = FakeVacuum(robot, list(FAN_OPTIONS if fan_speeds is None else fan_speeds))
    robot.mode = FakeSelect(
        robot,
        "cleaning_mode",
        "Cleaning mode",
        "cleaning_mode",
        list(MODE_OPTIONS if mode_options is None else mode_options),
    )
    if with_mop:
        robot.mop = FakeSelect(
            robot,
            "mop_intensity",
            "Mop intensity",
            "mop_intensity",
            list(MOP_OPTIONS if mop_options is None else mop_options),
        )

    async def entry_setup(hass, entry):
        # Core integrations return None (2026.11 reports a returned boolean as an error).
        await hass.config_entries.async_forward_entry_setups(entry, ["select", "vacuum"])

    async def select_setup(hass, entry, add):
        add([e for e in (robot.mode, robot.mop) if e is not None])

    async def vacuum_setup(hass, entry, add):
        add([robot.vacuum])

    mock_integration(hass, MockModule("roborock", async_setup_entry=entry_setup))
    mock_platform(hass, "roborock.select", MockPlatform(async_setup_entry=select_setup))
    mock_platform(hass, "roborock.vacuum", MockPlatform(async_setup_entry=vacuum_setup))
    mock_platform(hass, "roborock.config_flow", None)
    entry = MockConfigEntry(domain="roborock")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.LOADED
    await hass.async_block_till_done()
    robot.device = dr.async_get(hass).async_get_device_by_identifier(
        ("roborock", SLUG), entry.entry_id
    )
    assert robot.device is not None
    assert hass.states.get(VACUUM).state == "docked"
    return robot


# --- helpers ------------------------------------------------------------------------


def _valid(**over):
    base = {
        "name": "Morning",
        "time": "08:30",
        "days": list(range(7)),
        "rooms": [],
        "repeat": 1,
        "enabled": True,
    }
    return {**base, **over}


@pytest.fixture
async def clock(hass: HomeAssistant, freezer):
    """07:00 local on a Wednesday; the schedules below fire at 08:30 (12:30Z)."""
    await hass.config.async_set_time_zone("America/New_York")
    freezer.move_to("2026-10-28 11:00:00+00:00")
    return freezer


async def _manager(hass, robot) -> sch.ScheduleManager:
    mgr = sch.ScheduleManager(hass, SLUG, robot.device.id)
    await mgr.async_load()
    mgr.async_start()
    await hass.async_block_till_done()
    return mgr


async def _save(mgr, **over):
    return await mgr.async_save(sch.validate_schedule_input(_valid(**over)))


async def _settle():
    for _ in range(50):
        await asyncio.sleep(0)


async def _fire(hass, freezer, to: str = "2026-10-28 12:30:00+00:00") -> None:
    freezer.move_to(to)
    async_fire_time_changed(hass, datetime.fromisoformat(to))


async def _advance(hass, freezer, to: str = "2026-10-28 12:30:00+00:00") -> None:
    await _fire(hass, freezer, to)
    await hass.async_block_till_done(wait_background_tasks=True)


FULL = {"mode": "vac_and_mop", "fan_speed": "turbo", "mop_intensity": "intense"}


# --- validation: the contract -----------------------------------------------------


def test_settings_left_out_stays_distinct_from_an_explicit_empty_object():
    assert "settings" not in sch.validate_schedule_input(_valid())  # old clients: untouched
    assert sch.validate_schedule_input(_valid(settings={}))["settings"] == {}


@pytest.mark.parametrize(
    "settings",
    [
        None,
        [],
        "vacuum",
        {"fan_speed": "turbo"},  # mode is required
        {"mop_intensity": "mild"},
        {"mode": None},
        {"mode": "smart_mode"},
        {"mode": ["vacuum"]},
        {"mode": "vacuum", "unknown": 1},
        {"mode": "vacuum", "fan_speed": "off"},  # off/custom are never accepted
        {"mode": "vacuum", "fan_speed": "custom"},
        {"mode": "vacuum", "fan_speed": "Off"},
        {"mode": "vacuum", "fan_speed": "CUSTOM"},
        {"mode": "vacuum", "fan_speed": ""},
        {"mode": "vacuum", "fan_speed": " turbo"},
        {"mode": "vacuum", "fan_speed": "tur\nbo"},
        {"mode": "vacuum", "fan_speed": "x" * 65},
        {"mode": "vacuum", "fan_speed": 3},
        {"mode": "vacuum", "fan_speed": None},
        {"mode": "mop", "mop_intensity": "off"},
        {"mode": "mop", "mop_intensity": "custom"},
        {"mode": "mop", "mop_intensity": ""},
        {"mode": "mop", "mop_intensity": ["mild"]},
        {"mode": "vacuum", "mop_intensity": "mild"},  # water does not apply to vacuum
        {"mode": "mop", "fan_speed": "turbo"},  # suction does not apply to mop-only
        {"mode": "custom", "fan_speed": "turbo"},
        {"mode": "custom", "mop_intensity": "mild"},
    ],
)
def test_invalid_settings_rejected(settings):
    with pytest.raises(ValueError):
        sch.validate_schedule_input(_valid(settings=settings))


# --- migration and persistence ------------------------------------------------------


async def test_legacy_stored_schedule_loads_as_keep_current(hass, clock, hass_storage):
    robot = await build_robot(hass)
    hass_storage[STORE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORE_KEY,
        "data": {
            "schedules": [
                {
                    "id": "weekly",
                    "name": "Weekly",
                    "time": "19:00",
                    "days": [1, 3, 6],
                    "rooms": [],
                    "repeat": 1,
                    "enabled": True,
                    "last_run": None,
                    "last_result": None,
                }
            ]
        },
    }
    mgr = await _manager(hass, robot)
    listed = mgr.async_list()
    assert len(listed) == 1 and listed[0]["id"] == "weekly"
    assert listed[0]["settings"] == {}
    assert (listed[0]["name"], listed[0]["time"], listed[0]["days"]) == ("Weekly", "19:00", [1, 3, 6])
    rev = mgr._items["weekly"]["rev"]
    # An unrelated edit that echoes the list result keeps it a keep-current schedule.
    renamed = await mgr.async_save(sch.validate_schedule_input({**listed[0], "name": "Weekly 2"}))
    assert renamed["settings"] == {} and mgr._items["weekly"]["rev"] == rev
    assert hass_storage[STORE_KEY]["data"]["schedules"][0]["settings"] == {}
    await mgr.async_stop()


async def test_settings_survive_restart(hass, clock, hass_storage):
    robot = await build_robot(hass)
    mgr = await _manager(hass, robot)
    saved = await _save(mgr, settings=FULL)
    assert saved["settings"] == FULL
    await mgr.async_stop()
    stored = hass_storage[STORE_KEY]["data"]["schedules"][0]
    assert stored["settings"] == FULL and "rev" not in stored
    mgr2 = await _manager(hass, robot)
    assert mgr2.async_list()[0]["settings"] == FULL
    await mgr2.async_stop()


async def test_invalid_stored_settings_drop_only_that_schedule(hass, clock, hass_storage):
    robot = await build_robot(hass)
    hass_storage[STORE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORE_KEY,
        "data": {
            "schedules": [
                {"id": "ok", **_valid(), "settings": {"mode": "mop"}},
                {"id": "bad", **_valid(), "settings": {"mode": "mop", "fan_speed": "turbo"}},
                {"id": "worse", **_valid(), "settings": "mop"},
            ]
        },
    }
    mgr = await _manager(hass, robot)
    assert [s["id"] for s in mgr.async_list()] == ["ok"]
    await mgr.async_stop()


async def test_set_enabled_and_time_edits_preserve_settings(hass, clock):
    robot = await build_robot(hass)
    mgr = await _manager(hass, robot)
    saved = await _save(mgr, settings={"mode": "mop", "mop_intensity": "mild"})
    off = await mgr.async_set_enabled(saved["id"], False)
    assert off["settings"] == {"mode": "mop", "mop_intensity": "mild"} and off["enabled"] is False
    on = await mgr.async_set_enabled(saved["id"], True)
    assert on["settings"] == {"mode": "mop", "mop_intensity": "mild"}
    moved = await mgr.async_save(
        sch.validate_schedule_input({**on, "time": "09:00"})  # echo of the list result
    )
    assert moved["settings"] == {"mode": "mop", "mop_intensity": "mild"}
    cleared = await mgr.async_save(sch.validate_schedule_input({**moved, "settings": {}}))
    assert cleared["settings"] == {}
    await mgr.async_stop()


async def test_editing_only_settings_never_fires_a_past_time(hass, clock):
    robot = await build_robot(hass)
    mgr = await _manager(hass, robot)
    saved = await _save(mgr, time="06:00", settings={"mode": "vacuum"})  # 06:00 already passed
    await mgr.async_save(
        sch.validate_schedule_input(
            {**saved, "settings": {"mode": "vacuum", "fan_speed": "max"}}
        )
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert robot.log == []
    await mgr.async_stop()


# --- capabilities, read from the live entities ---------------------------------------


async def test_capabilities_are_the_robots_live_options_filtered(hass, clock):
    robot = await build_robot(hass, mode_options=[*MODE_OPTIONS, "smart_mode"])
    mgr = await _manager(hass, robot)
    assert mgr.async_capabilities() == {
        "modes": ["vacuum", "vac_and_mop", "mop", "custom"],  # smart_mode is not schedulable
        "fan_speeds": ["quiet", "balanced", "turbo", "max", "max_plus"],  # no off/custom
        "mop_intensities": ["mild", "standard", "intense"],  # no off/custom
    }
    await mgr.async_stop()


async def test_capabilities_follow_what_this_robot_offers(hass, clock):
    robot = await build_robot(
        hass,
        fan_speeds=["quiet", "balanced", "turbo", "max", "off", "custom"],
        mode_options=["vacuum", "custom"],
        mop_options=["off", "standard", "custom"],
    )
    mgr = await _manager(hass, robot)
    assert mgr.async_capabilities() == {
        "modes": ["vacuum", "custom"],
        "fan_speeds": ["quiet", "balanced", "turbo", "max"],
        "mop_intensities": ["standard"],
    }
    await mgr.async_stop()


async def test_capabilities_without_a_mop_select(hass, clock):
    robot = await build_robot(hass, with_mop=False)
    mgr = await _manager(hass, robot)
    assert mgr.async_capabilities()["mop_intensities"] == []
    await mgr.async_stop()


async def test_capabilities_do_not_depend_on_entity_ids(hass, clock):
    robot = await build_robot(hass)
    registry = er.async_get(hass)
    registry.async_update_entity(VACUUM, new_entity_id="vacuum.kevin")
    registry.async_update_entity(MODE_SELECT, new_entity_id="select.kevin_mode_x")
    registry.async_update_entity(MOP_SELECT, new_entity_id="select.kevin_water")
    await hass.async_block_till_done()
    mgr = await _manager(hass, robot)
    assert mgr.async_capabilities()["modes"] == MODE_OPTIONS
    await _save(mgr, settings={"mode": "vac_and_mop", "mop_intensity": "mild"})
    await _advance(hass, clock)
    assert robot.log == [
        ("select.cleaning_mode", "vac_and_mop"),
        ("select.mop_intensity", "mild"),
        ("vacuum.start",),
    ]
    await mgr.async_stop()


async def test_capabilities_fail_clearly_when_unreadable(hass, clock):
    robot = await build_robot(hass)
    mgr = await _manager(hass, robot)

    robot.set_available(robot.vacuum, False)
    with pytest.raises(sch.ScheduleError) as err:
        mgr.async_capabilities()
    assert err.value.code == "unavailable" and VACUUM in str(err.value)
    robot.set_available(robot.vacuum, True)

    robot.set_available(robot.mode, False)
    with pytest.raises(sch.ScheduleError, match="cleaning mode select"):
        mgr.async_capabilities()
    robot.set_available(robot.mode, True)

    # The mop intensity select only matters to schedules that ask for a mop intensity.
    robot.set_available(robot.mop, False)
    assert mgr.async_capabilities() == {
        "modes": MODE_OPTIONS,
        "fan_speeds": ["quiet", "balanced", "turbo", "max", "max_plus"],
        "mop_intensities": [],
    }
    robot.set_available(robot.mop, True)
    assert mgr.async_capabilities()["mop_intensities"] == ["mild", "standard", "intense"]
    await mgr.async_stop()


async def test_capabilities_fail_when_entity_disabled_or_no_device(hass, clock):
    robot = await build_robot(hass)
    mgr = await _manager(hass, robot)
    er.async_get(hass).async_update_entity(
        MODE_SELECT, disabled_by=er.RegistryEntryDisabler.USER
    )
    await hass.async_block_till_done()
    with pytest.raises(sch.ScheduleError, match="no enabled cleaning mode select"):
        mgr.async_capabilities()
    await mgr.async_stop()

    orphan = sch.ScheduleManager(hass, "other", None)
    await orphan.async_load()
    with pytest.raises(sch.ScheduleError) as err:
        orphan.async_capabilities()
    assert err.value.code == "unavailable"


# --- WebSocket ------------------------------------------------------------------------


@pytest.fixture
async def wired(hass):
    # No frozen clock here: the WebSocket client must authenticate in real time.
    await hass.config.async_set_time_zone("America/New_York")
    robot = await build_robot(hass)
    mgr = await _manager(hass, robot)
    hass.data["test_runtimes"] = {VACUUM: VacuumRuntime(mgr)}
    services.async_register_schedule_commands(hass)
    yield types.SimpleNamespace(robot=robot, mgr=mgr)
    await mgr.async_stop()


def _ws_schedule(**over):
    return {
        "name": "Morning",
        "time": "08:30",
        "days": [0, 1],
        "rooms": [16],
        "repeat": 2,
        "enabled": True,
        **over,
    }


async def test_ws_capabilities_exact_shape(hass, hass_ws_client, hass_read_only_access_token, wired):
    expected = {
        "modes": ["vacuum", "vac_and_mop", "mop", "custom"],
        "fan_speeds": ["quiet", "balanced", "turbo", "max", "max_plus"],
        "mop_intensities": ["mild", "standard", "intense"],
    }
    for client in (
        await hass_ws_client(hass),
        await hass_ws_client(hass, hass_read_only_access_token),  # readable by any user
    ):
        await client.send_json_auto_id(
            {"type": "roborock_live_map/schedules/capabilities", "entity_id": VACUUM}
        )
        resp = await client.receive_json()
        assert resp["success"] is True
        assert resp["result"] == expected and list(resp["result"]) == list(expected)


async def test_ws_capabilities_errors(hass, hass_ws_client, wired):
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {"type": "roborock_live_map/schedules/capabilities", "entity_id": "vacuum.other"}
    )
    assert (await client.receive_json())["error"]["code"] == "not_found"

    wired.robot.set_available(wired.robot.mode, False)
    await client.send_json_auto_id(
        {"type": "roborock_live_map/schedules/capabilities", "entity_id": VACUUM}
    )
    resp = await client.receive_json()
    assert resp["success"] is False and resp["error"]["code"] == "unavailable"
    assert "cleaning mode select" in resp["error"]["message"]
    assert wired.robot.log == []  # reading capabilities never commands the robot


async def test_ws_save_list_and_set_enabled_carry_settings(hass, hass_ws_client, wired):
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {
            "type": "roborock_live_map/schedules/save",
            "entity_id": VACUUM,
            "schedule": _ws_schedule(settings={"mode": "vacuum", "fan_speed": "max"}),
        }
    )
    saved = (await client.receive_json())["result"]
    assert saved["settings"] == {"mode": "vacuum", "fan_speed": "max"}

    await client.send_json_auto_id({"type": "roborock_live_map/schedules/list", "entity_id": VACUUM})
    listed = (await client.receive_json())["result"]
    assert listed[0]["settings"] == {"mode": "vacuum", "fan_speed": "max"}

    await client.send_json_auto_id(
        {
            "type": "roborock_live_map/schedules/set_enabled",
            "entity_id": VACUUM,
            "schedule_id": saved["id"],
            "enabled": False,
        }
    )
    toggled = (await client.receive_json())["result"]
    assert toggled["enabled"] is False and toggled["settings"] == {"mode": "vacuum", "fan_speed": "max"}

    await client.send_json_auto_id({"type": "roborock_live_map/schedules/list", "entity_id": VACUUM})
    after_toggle = (await client.receive_json())["result"]
    assert after_toggle[0]["enabled"] is False
    assert after_toggle[0]["settings"] == {"mode": "vacuum", "fan_speed": "max"}  # not wiped


async def test_ws_old_client_edit_keeps_settings_and_explicit_empty_clears(hass, hass_ws_client, wired):
    """1.1.0 clients never send ``settings``: editing must not wipe cleaning choices."""
    client = await hass_ws_client(hass)

    async def call(kind, **fields):
        await client.send_json_auto_id(
            {"type": f"roborock_live_map/schedules/{kind}", "entity_id": VACUUM, **fields}
        )
        return await client.receive_json()

    # A new schedule saved without settings is keep-current ({}).
    created = (await call("save", schedule=_ws_schedule()))["result"]
    assert created["settings"] == {}

    chosen = {"mode": "vac_and_mop", "fan_speed": "turbo", "mop_intensity": "mild"}
    await call("save", schedule=_ws_schedule(id=created["id"], settings=chosen))

    # An old client edits name, time and rooms; it has no settings field to send back.
    old_client = _ws_schedule(id=created["id"], name="Renamed", time="09:15", rooms=[17], repeat=1)
    assert "settings" not in old_client
    edited = await call("save", schedule=old_client)
    assert edited["success"] is True and edited["result"]["name"] == "Renamed"
    assert edited["result"]["settings"] == chosen
    listed = (await call("list"))["result"]
    assert listed[0]["name"] == "Renamed" and listed[0]["settings"] == chosen

    # An explicit empty object clears them; the result and later lists show {} (never null).
    cleared = await call("save", schedule=_ws_schedule(id=created["id"], settings={}))
    assert cleared["result"]["settings"] == {}
    assert (await call("list"))["result"][0]["settings"] == {}


@pytest.mark.parametrize(
    ("settings", "fragment"),
    [
        (None, "settings must be an object"),
        ({"fan_speed": "turbo"}, "settings.mode is required"),
        ({"mode": "mop", "fan_speed": "turbo"}, "does not apply to mode mop"),
        ({"mode": "vacuum", "fan_speed": "off"}, "settings.fan_speed must be an option name"),
        ({"mode": "custom", "mop_intensity": "mild"}, "does not apply to mode custom"),
        ({"mode": "vacuum", "x": 1}, "unknown settings field"),
    ],
)
async def test_ws_invalid_settings_rejected_with_message(
    hass, hass_ws_client, wired, settings, fragment
):
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {
            "type": "roborock_live_map/schedules/save",
            "entity_id": VACUUM,
            "schedule": _ws_schedule(settings=settings),
        }
    )
    resp = await client.receive_json()
    assert resp["success"] is False and resp["error"]["code"] == "invalid_format"
    assert fragment in resp["error"]["message"]
    assert wired.mgr.async_list() == []


async def test_ws_non_admin_cannot_save_settings(
    hass, hass_ws_client, hass_read_only_access_token, wired
):
    client = await hass_ws_client(hass, hass_read_only_access_token)
    await client.send_json_auto_id(
        {
            "type": "roborock_live_map/schedules/save",
            "entity_id": VACUUM,
            "schedule": _ws_schedule(settings={"mode": "vacuum"}),
        }
    )
    assert (await client.receive_json())["error"]["code"] == "unauthorized"
    assert wired.mgr.async_list() == []


# --- running: order, gating, preflight, failures ---------------------------------------


async def test_run_order_mode_then_power_then_mop_then_start(hass, clock):
    robot = await build_robot(hass)
    mgr = await _manager(hass, robot)
    await _save(mgr, settings=FULL)
    await _advance(hass, clock)
    assert robot.log == [
        ("select.cleaning_mode", "vac_and_mop"),
        ("vacuum.set_fan_speed", "turbo"),
        ("select.mop_intensity", "intense"),
        ("vacuum.start",),
    ]
    assert mgr.async_list()[0]["last_result"] == "ok"
    # The settings stay on the robot afterwards; nothing restores the old ones.
    assert robot.mode.current_option == "vac_and_mop"
    assert robot.vacuum.fan_speed == "turbo"
    assert robot.mop.current_option == "intense"
    await mgr.async_stop()


async def test_run_with_rooms_starts_with_segment_clean_after_settings(hass, clock):
    robot = await build_robot(hass)
    mgr = await _manager(hass, robot)
    await _save(mgr, rooms=[16, 18], repeat=2, settings={"mode": "vacuum", "fan_speed": "max"})
    await _advance(hass, clock)
    assert robot.log == [
        ("select.cleaning_mode", "vacuum"),
        ("vacuum.set_fan_speed", "max"),
        ("vacuum.send_command", "app_segment_clean", [{"segments": [16, 18], "repeat": 2}]),
    ]
    await mgr.async_stop()


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        ({"mode": "mop"}, [("select.cleaning_mode", "mop")]),
        (
            {"mode": "mop", "mop_intensity": "mild"},
            [("select.cleaning_mode", "mop"), ("select.mop_intensity", "mild")],
        ),
        ({"mode": "custom"}, [("select.cleaning_mode", "custom")]),
        (
            {"mode": "vac_and_mop", "mop_intensity": "standard"},
            [("select.cleaning_mode", "vac_and_mop"), ("select.mop_intensity", "standard")],
        ),
        (
            {"mode": "vacuum", "fan_speed": "quiet"},
            [("select.cleaning_mode", "vacuum"), ("vacuum.set_fan_speed", "quiet")],
        ),
    ],
)
async def test_only_requested_steps_run_always_mode_first(hass, clock, settings, expected):
    robot = await build_robot(hass)
    mgr = await _manager(hass, robot)
    await _save(mgr, settings=settings)
    await _advance(hass, clock)
    assert robot.log == [*expected, ("vacuum.start",)]
    await mgr.async_stop()


async def test_keep_current_schedule_changes_nothing_but_starts(hass, clock):
    robot = await build_robot(hass)
    mgr = await _manager(hass, robot)
    await _save(mgr)  # no settings at all: today's behaviour
    await _advance(hass, clock)
    assert robot.log == [("vacuum.start",)]
    assert mgr.async_list()[0]["last_result"] == "ok"
    assert robot.mode.current_option == "custom" and robot.vacuum.fan_speed == "custom"
    await mgr.async_stop()


async def test_keep_current_rooms_schedule_only_sends_the_segment_clean(hass, clock):
    robot = await build_robot(hass)
    mgr = await _manager(hass, robot)
    await _save(mgr, rooms=[16], repeat=3, settings={})
    await _advance(hass, clock)
    assert robot.log == [
        ("vacuum.send_command", "app_segment_clean", [{"segments": [16], "repeat": 3}])
    ]
    assert robot.mode.current_option == "custom" and robot.vacuum.fan_speed == "custom"
    await mgr.async_stop()


@pytest.mark.parametrize("settings", [{}, FULL])
async def test_already_cleaning_changes_nothing(hass, clock, settings):
    robot = await build_robot(hass)
    mgr = await _manager(hass, robot)
    await _save(mgr, settings=settings)
    robot.set_activity("cleaning")
    await _advance(hass, clock)
    assert robot.log == []
    assert mgr.async_list()[0]["last_result"] == "skipped: already cleaning"
    assert robot.mode.current_option == "custom"
    await mgr.async_stop()


@pytest.mark.parametrize("activity", ["paused", "returning", "error"])
async def test_busy_robot_legacy_schedule_keeps_old_behaviour_settings_skip(hass, clock, activity):
    """Only an explicit-settings schedule needs a docked/idle robot."""
    robot = await build_robot(hass)
    mgr = await _manager(hass, robot)
    await _save(mgr, name="Legacy")
    await _save(mgr, name="Settings", time="08:31", settings={"mode": "vacuum", "fan_speed": "max"})
    robot.set_activity(activity)

    await _advance(hass, clock, "2026-10-28 12:30:00+00:00")
    assert robot.log == [("vacuum.start",)]  # legacy: only 'cleaning' ever skipped a run
    assert next(s for s in mgr.async_list() if s["name"] == "Legacy")["last_result"] == "ok"

    robot.log.clear()
    robot.set_activity(activity)  # the legacy start flipped it to cleaning
    await _advance(hass, clock, "2026-10-28 12:31:00+00:00")
    assert robot.log == []  # nothing touched: no mode, no power, no start
    assert next(s for s in mgr.async_list() if s["name"] == "Settings")["last_result"] == (
        f"skipped: vacuum is {activity}; settings not changed"
    )
    assert robot.mode.current_option == "custom" and robot.vacuum.fan_speed == "custom"
    await mgr.async_stop()


@pytest.mark.parametrize("activity", ["docked", "idle"])
async def test_docked_and_idle_both_allow_settings(hass, clock, activity):
    robot = await build_robot(hass)
    mgr = await _manager(hass, robot)
    await _save(mgr, settings={"mode": "mop"})
    robot.set_activity(activity)
    await _advance(hass, clock)
    assert robot.log == [("select.cleaning_mode", "mop"), ("vacuum.start",)]
    await mgr.async_stop()


@pytest.mark.parametrize("state", ["unavailable", "unknown"])
async def test_unavailable_vacuum_is_an_error_without_changes(hass, clock, state):
    robot = await build_robot(hass)
    mgr = await _manager(hass, robot)
    await _save(mgr, settings=FULL)
    hass.states.async_set(VACUUM, state)
    await _advance(hass, clock)
    assert robot.log == []
    assert mgr.async_list()[0]["last_result"].startswith("error:")
    await mgr.async_stop()


@pytest.mark.parametrize(
    ("build", "settings", "fragment"),
    [
        (
            {"fan_speeds": ["quiet", "balanced", "turbo", "max", "off", "custom"]},
            {"mode": "vacuum", "fan_speed": "max_plus"},
            "vacuum power 'max_plus' (offers: quiet, balanced, turbo, max)",
        ),
        (
            {"mop_options": ["off", "mild", "standard", "custom"]},
            {"mode": "vac_and_mop", "fan_speed": "turbo", "mop_intensity": "intense"},
            "mop intensity 'intense' (offers: mild, standard)",
        ),
        (
            {"mode_options": ["vacuum", "custom"]},
            {"mode": "mop", "mop_intensity": "mild"},
            "cleaning mode 'mop' (offers: vacuum, custom)",
        ),
        (
            {"mop_options": ["off", "custom"]},  # selects offer no usable intensity at all
            {"mode": "mop", "mop_intensity": "standard"},
            "mop intensity 'standard' (offers: none)",
        ),
    ],
)
async def test_unsupported_value_is_found_before_any_command(hass, clock, build, settings, fragment):
    robot = await build_robot(hass, **build)
    mgr = await _manager(hass, robot)
    await _save(mgr, settings=settings)
    await _advance(hass, clock)
    assert robot.log == []  # not even the (supported) mode was sent
    result = mgr.async_list()[0]["last_result"]
    assert result.startswith("error: the robot does not offer ")
    assert fragment in result and result.endswith("nothing was changed")
    assert robot.vacuum.state == "docked"
    assert robot.mode.current_option == "custom" and robot.vacuum.fan_speed == "custom"
    await mgr.async_stop()


async def test_unavailable_control_or_missing_service_is_found_before_any_command(hass, clock):
    robot = await build_robot(hass)
    mgr = await _manager(hass, robot)
    await _save(mgr, settings=FULL)

    robot.set_available(robot.mop, False)
    await _advance(hass, clock, "2026-10-28 12:30:00+00:00")
    assert robot.log == []
    assert "mop intensity select) is unavailable" in mgr.async_list()[0]["last_result"]
    robot.set_available(robot.mop, True)

    hass.services.async_remove("vacuum", "set_fan_speed")
    await _save(mgr, name="Second", time="08:31", settings=FULL)
    await _advance(hass, clock, "2026-10-28 12:31:00+00:00")
    assert robot.log == []
    second = next(s for s in mgr.async_list() if s["name"] == "Second")
    assert second["last_result"] == (
        "error: Home Assistant has no vacuum.set_fan_speed service; nothing was changed"
    )
    await mgr.async_stop()


async def test_vacuum_only_schedule_runs_while_the_mop_select_is_unavailable(hass, clock):
    robot = await build_robot(hass)
    mgr = await _manager(hass, robot)
    robot.set_available(robot.mop, False)
    await _save(mgr, settings={"mode": "vacuum", "fan_speed": "max"})
    await _advance(hass, clock)
    assert robot.log == [
        ("select.cleaning_mode", "vacuum"),
        ("vacuum.set_fan_speed", "max"),
        ("vacuum.start",),
    ]
    assert mgr.async_list()[0]["last_result"] == "ok"
    await mgr.async_stop()


async def test_mop_intensity_needs_a_usable_mop_select(hass, clock):
    robot = await build_robot(hass, with_mop=False)
    mgr = await _manager(hass, robot)
    await _save(mgr, settings={"mode": "mop", "mop_intensity": "standard"})
    await _save(mgr, name="Mode only", time="08:31", settings={"mode": "mop"})
    await _advance(hass, clock, "2026-10-28 12:30:00+00:00")
    assert robot.log == []  # the mode is not sent when the intensity cannot be
    first = next(s for s in mgr.async_list() if s["name"] == "Morning")
    assert first["last_result"] == (
        "error: Home Assistant has no enabled mop intensity select for this robot; "
        "nothing was changed"
    )
    await _advance(hass, clock, "2026-10-28 12:31:00+00:00")
    assert robot.log == [("select.cleaning_mode", "mop"), ("vacuum.start",)]
    await mgr.async_stop()


@pytest.mark.parametrize(
    ("failing", "expected_log", "applied"),
    [
        (
            "select.cleaning_mode",
            [("select.cleaning_mode", "vac_and_mop")],
            "no earlier setting had been applied",
        ),
        (
            "vacuum.set_fan_speed",
            [("select.cleaning_mode", "vac_and_mop"), ("vacuum.set_fan_speed", "turbo")],
            "already applied and left in place: cleaning mode vac_and_mop",
        ),
        (
            "select.mop_intensity",
            [
                ("select.cleaning_mode", "vac_and_mop"),
                ("vacuum.set_fan_speed", "turbo"),
                ("select.mop_intensity", "intense"),
            ],
            "already applied and left in place: cleaning mode vac_and_mop, vacuum power turbo",
        ),
    ],
)
async def test_failed_intermediate_command_stops_and_reports(
    hass, clock, failing, expected_log, applied
):
    robot = await build_robot(hass)
    robot.fail[failing] = "robot unreachable"
    mgr = await _manager(hass, robot)
    await _save(mgr, settings=FULL)
    await _advance(hass, clock)
    assert robot.log == expected_log  # stopped at the failure, never started
    result = mgr.async_list()[0]["last_result"]
    assert result.startswith("error: setting ") and "robot unreachable" in result
    assert "may or may not have reached the robot" in result
    assert applied in result and result.endswith("the vacuum was not started")
    assert robot.vacuum.state == "docked"

    # One attempt only: nothing is retried later in the day.
    robot.log.clear()
    robot.fail.clear()
    await _advance(hass, clock, "2026-10-28 13:30:00+00:00")
    await _settle()
    assert robot.log == []
    await mgr.async_stop()


async def test_robot_becoming_busy_midway_stops_the_remaining_steps(hass, clock):
    robot = await build_robot(hass)

    async def started_from_the_app():
        robot.set_activity("cleaning")

    robot.hooks["select.cleaning_mode"] = started_from_the_app
    mgr = await _manager(hass, robot)
    await _save(mgr, settings=FULL)
    await _advance(hass, clock)
    assert robot.log == [("select.cleaning_mode", "vac_and_mop")]  # no power, no mop, no start
    result = mgr.async_list()[0]["last_result"]
    assert result.startswith("error: stopped before vacuum power turbo: vacuum is cleaning")
    assert "cleaning mode vac_and_mop" in result and result.endswith("the vacuum was not started")
    await mgr.async_stop()


@pytest.mark.parametrize("change", ["delete", "edit_settings", "disable"])
async def test_schedule_changed_midway_stops_the_remaining_steps(hass, clock, change):
    robot = await build_robot(hass)
    gate = asyncio.Event()

    async def hold():
        await gate.wait()

    robot.hooks["select.cleaning_mode"] = hold
    mgr = await _manager(hass, robot)
    saved = await _save(mgr, settings=FULL)
    await _fire(hass, clock)
    await _settle()
    assert robot.log == [("select.cleaning_mode", "vac_and_mop")]  # run is waiting on the robot
    if change == "delete":
        await mgr.async_delete(saved["id"])
    elif change == "edit_settings":
        await mgr.async_save(
            sch.validate_schedule_input({**saved, "settings": {"mode": "vacuum"}})
        )
    else:
        await mgr.async_set_enabled(saved["id"], False)
    gate.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert robot.log == [("select.cleaning_mode", "vac_and_mop")]  # nothing after the change
    assert robot.vacuum.state == "docked"
    if change == "delete":
        assert mgr.async_list() == []  # a deleted schedule is never resurrected
    else:
        survivor = mgr.async_list()[0]
        assert survivor["last_run"] == "2026-10-28T12:30:00+00:00"
        assert survivor["last_result"] == (
            "error: stopped before vacuum power turbo: the schedule was changed; "
            "already applied and left in place: cleaning mode vac_and_mop; "
            "the vacuum was not started"
        )
        assert survivor["settings"] == (
            FULL if change == "disable" else {"mode": "vacuum"}
        )  # the edit itself is untouched
    await mgr.async_stop()


async def test_interrupted_run_never_overwrites_a_newer_result(hass, clock):
    robot = await build_robot(hass)
    gate = asyncio.Event()

    async def hold():
        await gate.wait()

    robot.hooks["select.cleaning_mode"] = hold
    mgr = await _manager(hass, robot)
    saved = await _save(mgr, settings=FULL)
    await _fire(hass, clock)
    await _settle()
    await mgr.async_save(sch.validate_schedule_input({**saved, "settings": {"mode": "mop"}}))
    newer = mgr._items[saved["id"]]  # a later run of the edited schedule has been recorded
    newer["last_run"], newer["last_result"] = "2026-10-28T13:00:00+00:00", "ok"
    gate.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert (newer["last_run"], newer["last_result"]) == ("2026-10-28T13:00:00+00:00", "ok")
    await mgr.async_stop()


async def test_edit_after_a_finished_run_still_drops_the_old_result(hass, clock):
    """Only runs that left settings behind and then stopped are kept past an edit."""
    robot = await build_robot(hass)
    gate = asyncio.Event()

    async def hold():
        await gate.wait()

    robot.hooks["vacuum.start"] = hold
    mgr = await _manager(hass, robot)
    saved = await _save(mgr, settings={"mode": "vacuum"})
    await _fire(hass, clock)
    await _settle()
    await mgr.async_save(sch.validate_schedule_input({**saved, "name": "Renamed", "time": "09:00"}))
    gate.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mgr.async_list()[0]["last_result"] is None
    await mgr.async_stop()


async def test_model_specific_options_are_offered_and_applied(hass, clock):
    robot = await build_robot(
        hass,
        fan_speeds=["off", "gentle", "balanced", "extreme", "Off", "custom", "gentle"],
        mop_options=["off", "low", "medium", "high", "custom"],
    )
    mgr = await _manager(hass, robot)
    assert mgr.async_capabilities() == {
        "modes": MODE_OPTIONS,
        "fan_speeds": ["gentle", "balanced", "extreme"],  # no sentinels, no duplicates
        "mop_intensities": ["low", "medium", "high"],
    }
    await _save(mgr, settings={"mode": "vac_and_mop", "fan_speed": "extreme", "mop_intensity": "high"})
    await _advance(hass, clock)
    assert robot.log == [
        ("select.cleaning_mode", "vac_and_mop"),
        ("vacuum.set_fan_speed", "extreme"),
        ("select.mop_intensity", "high"),
        ("vacuum.start",),
    ]
    await mgr.async_stop()


async def test_saved_option_the_robot_does_not_offer_is_refused_at_run_time(hass, clock):
    robot = await build_robot(hass)  # offers quiet..max_plus, not "gentle"
    mgr = await _manager(hass, robot)
    saved = await _save(mgr, settings={"mode": "vacuum", "fan_speed": "gentle"})  # storable
    assert saved["settings"] == {"mode": "vacuum", "fan_speed": "gentle"}
    await _advance(hass, clock)
    assert robot.log == []
    assert mgr.async_list()[0]["last_result"] == (
        "error: the robot does not offer vacuum power 'gentle' "
        "(offers: quiet, balanced, turbo, max, max_plus); nothing was changed"
    )
    await mgr.async_stop()


async def test_overlapping_schedules_never_change_settings_of_a_running_clean(hass, clock):
    robot = await build_robot(hass)
    mgr = await _manager(hass, robot)
    await _save(mgr, name="First", settings={"mode": "vacuum", "fan_speed": "turbo"})
    await _save(mgr, name="Second", settings={"mode": "mop", "mop_intensity": "intense"})
    await _advance(hass, clock)
    assert robot.log == [
        ("select.cleaning_mode", "vacuum"),
        ("vacuum.set_fan_speed", "turbo"),
        ("vacuum.start",),
    ]
    results = {s["name"]: s["last_result"] for s in mgr.async_list()}
    assert results == {"First": "ok", "Second": "skipped: already cleaning"}
    assert robot.mode.current_option == "vacuum" and robot.mop.current_option == "custom"
    await mgr.async_stop()


async def test_stop_mid_sequence_does_not_start(hass, clock):
    robot = await build_robot(hass)
    gate = asyncio.Event()

    async def hold():
        await gate.wait()

    robot.hooks["select.cleaning_mode"] = hold
    mgr = await _manager(hass, robot)
    await _save(mgr, settings=FULL)
    await _fire(hass, clock)
    await _settle()
    assert mgr._tasks
    await mgr.async_stop()
    gate.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert robot.log == [("select.cleaning_mode", "vac_and_mop")]


async def test_unavailable_mode_select_is_an_error_without_any_command(hass, clock):
    robot = await build_robot(hass)
    mgr = await _manager(hass, robot)
    await _save(mgr, settings={"mode": "vacuum", "fan_speed": "max"})
    robot.set_available(robot.mode, False)
    await _advance(hass, clock)
    assert robot.log == []  # not even the vacuum power was sent
    assert mgr.async_list()[0]["last_result"] == (
        f"error: {MODE_SELECT} (cleaning mode select) is unavailable"
    )
    await mgr.async_stop()
