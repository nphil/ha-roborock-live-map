"""WebSocket contract: shapes, admin requirement, errors."""

from __future__ import annotations

import pytest
from pytest_homeassistant.common import MockConfigEntry

from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.roborock_live_map import schedules as sch
from custom_components.roborock_live_map import services
from custom_components.roborock_live_map.runtime import VacuumRuntime

VACUUM = "vacuum.robo"


def _schedule(**over):
    return {
        "name": "Morning",
        "time": "08:30",
        "days": [0, 1],
        "rooms": [16],
        "repeat": 2,
        "enabled": True,
        **over,
    }


@pytest.fixture
async def manager(hass):
    entry = MockConfigEntry(domain="roborock")
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("roborock", "duid1")}
    )
    er.async_get(hass).async_get_or_create(
        "vacuum", "roborock", "duid1", suggested_object_id="robo", device_id=device.id
    )
    mgr = sch.ScheduleManager(hass, "duid1", device.id)
    await mgr.async_load()
    mgr.async_start()
    hass.data["test_runtimes"] = {VACUUM: VacuumRuntime(mgr)}
    services.async_register_schedule_commands(hass)
    yield mgr
    await mgr.async_stop()


async def test_admin_save_list_update_delete(hass, hass_ws_client, manager):
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {"type": "roborock_live_map/schedules/save", "entity_id": VACUUM, "schedule": _schedule()}
    )
    saved = (await client.receive_json())["result"]
    assert saved["id"] and saved["name"] == "Morning" and saved["rooms"] == [16]

    await client.send_json_auto_id({"type": "roborock_live_map/schedules/list", "entity_id": VACUUM})
    listed = (await client.receive_json())["result"]
    assert [s["id"] for s in listed] == [saved["id"]]

    await client.send_json_auto_id(
        {
            "type": "roborock_live_map/schedules/save",
            "entity_id": VACUUM,
            "schedule": _schedule(**{"id": saved["id"], "name": "Renamed", "enabled": False}),
        }
    )
    updated = (await client.receive_json())["result"]
    assert updated["id"] == saved["id"] and updated["name"] == "Renamed" and not updated["enabled"]

    await client.send_json_auto_id(
        {"type": "roborock_live_map/schedules/delete", "entity_id": VACUUM, "schedule_id": saved["id"]}
    )
    resp = await client.receive_json()
    assert resp["success"] is True and resp["result"] == {"success": True}
    assert manager.async_list() == []


async def test_list_echo_can_be_saved_back(hass, hass_ws_client, manager):
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {"type": "roborock_live_map/schedules/save", "entity_id": VACUUM, "schedule": _schedule()}
    )
    saved = (await client.receive_json())["result"]
    await client.send_json_auto_id(
        {"type": "roborock_live_map/schedules/save", "entity_id": VACUUM, "schedule": saved}
    )
    assert (await client.receive_json())["success"] is True


async def test_non_admin_can_list_but_not_change(
    hass, hass_ws_client, hass_read_only_access_token, manager
):
    await manager.async_save(sch.validate_schedule_input(_schedule()))
    sid = manager.async_list()[0]["id"]
    client = await hass_ws_client(hass, hass_read_only_access_token)

    await client.send_json_auto_id({"type": "roborock_live_map/schedules/list", "entity_id": VACUUM})
    assert (await client.receive_json())["success"] is True

    await client.send_json_auto_id(
        {"type": "roborock_live_map/schedules/save", "entity_id": VACUUM, "schedule": _schedule(name="x")}
    )
    resp = await client.receive_json()
    assert resp["success"] is False and resp["error"]["code"] == "unauthorized"

    await client.send_json_auto_id(
        {"type": "roborock_live_map/schedules/delete", "entity_id": VACUUM, "schedule_id": sid}
    )
    resp = await client.receive_json()
    assert resp["success"] is False and resp["error"]["code"] == "unauthorized"
    assert len(manager.async_list()) == 1


@pytest.mark.parametrize(
    "schedule",
    [_schedule(time="25:00"), _schedule(repeat=0), _schedule(rooms=[], repeat=2), _schedule(days=[]), "no"],
)
async def test_invalid_schedule_rejected(hass, hass_ws_client, manager, schedule):
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {"type": "roborock_live_map/schedules/save", "entity_id": VACUUM, "schedule": schedule}
    )
    resp = await client.receive_json()
    assert resp["success"] is False and resp["error"]["code"] == "invalid_format"
    assert manager.async_list() == []


async def test_invalid_schedule_keeps_specific_message(hass, hass_ws_client, manager):
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {
            "type": "roborock_live_map/schedules/save",
            "entity_id": VACUUM,
            "schedule": _schedule(rooms=[], repeat=2),
        }
    )
    resp = await client.receive_json()
    assert "whole home" in resp["error"]["message"]


async def test_unknown_entity_and_unknown_id(hass, hass_ws_client, manager):
    client = await hass_ws_client(hass)
    for msg in (
        {"type": "roborock_live_map/schedules/list", "entity_id": "vacuum.other"},
        {"type": "roborock_live_map/schedules/delete", "entity_id": "vacuum.other", "schedule_id": "a"},
    ):
        await client.send_json_auto_id(msg)
        resp = await client.receive_json()
        assert resp["error"]["code"] == "not_found"

    await client.send_json_auto_id(
        {"type": "roborock_live_map/schedules/delete", "entity_id": VACUUM, "schedule_id": "missing"}
    )
    assert (await client.receive_json())["error"]["code"] == "not_found"
    await client.send_json_auto_id(
        {"type": "roborock_live_map/schedules/save", "entity_id": VACUUM, "schedule": _schedule(id="missing")}
    )
    assert (await client.receive_json())["error"]["code"] == "not_found"


async def test_after_unload_changes_report_unavailable(hass, hass_ws_client, manager):
    await manager.async_stop()
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {"type": "roborock_live_map/schedules/save", "entity_id": VACUUM, "schedule": _schedule()}
    )
    assert (await client.receive_json())["error"]["code"] == "unavailable"



async def test_ws_saved_room_schedule_fires_segment_clean(hass, hass_ws_client, manager, freezer):
    from pytest_homeassistant.common import (
        async_fire_time_changed,
        async_mock_service,
    )
    from datetime import datetime

    client = await hass_ws_client(hass)  # authenticate before time is frozen
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to("2026-10-28 07:00:00+00:00")
    hass.states.async_set(VACUUM, "docked")
    send = async_mock_service(hass, "vacuum", "send_command")
    await client.send_json_auto_id(
        {
            "type": "roborock_live_map/schedules/save",
            "entity_id": VACUUM,
            "schedule": _schedule(days=list(range(7)), rooms=[16, 17], repeat=2),
        }
    )
    assert (await client.receive_json())["success"] is True
    freezer.move_to("2026-10-28 08:30:00+00:00")
    async_fire_time_changed(hass, datetime.fromisoformat("2026-10-28 08:30:00+00:00"))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert send[0].data["params"] == [{"segments": [16, 17], "repeat": 2}]


async def test_set_enabled_preserves_concurrent_edits_and_needs_admin(
    hass, hass_ws_client, hass_read_only_access_token, manager
):
    saved = await manager.async_save(sch.validate_schedule_input(_schedule(enabled=True)))
    # Another device edits the schedule after the card cached it.
    await manager.async_save(
        sch.validate_schedule_input(
            _schedule(id=saved["id"], name="Edited elsewhere", time="09:15", rooms=[18], repeat=3)
        )
    )
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {
            "type": "roborock_live_map/schedules/set_enabled",
            "entity_id": VACUUM,
            "schedule_id": saved["id"],
            "enabled": False,
        }
    )
    resp = await client.receive_json()
    result = resp["result"]
    assert resp["success"] and result["enabled"] is False
    assert (result["name"], result["time"], result["rooms"], result["repeat"]) == (
        "Edited elsewhere", "09:15", [18], 3,
    )
    assert manager.async_list()[0]["name"] == "Edited elsewhere"

    await client.send_json_auto_id(
        {"type": "roborock_live_map/schedules/set_enabled", "entity_id": VACUUM, "schedule_id": "zzz", "enabled": True}
    )
    assert (await client.receive_json())["error"]["code"] == "not_found"
    await client.send_json_auto_id(
        {"type": "roborock_live_map/schedules/set_enabled", "entity_id": VACUUM, "schedule_id": saved["id"], "enabled": "yes"}
    )
    assert (await client.receive_json())["error"]["code"] == "invalid_format"

    ro = await hass_ws_client(hass, hass_read_only_access_token)
    await ro.send_json_auto_id(
        {"type": "roborock_live_map/schedules/set_enabled", "entity_id": VACUUM, "schedule_id": saved["id"], "enabled": True}
    )
    resp = await ro.receive_json()
    assert resp["error"]["code"] == "unauthorized"
    assert manager.async_list()[0]["enabled"] is False
