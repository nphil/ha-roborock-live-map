"""Schedule validation, DST maths, firing, durability and lifecycle."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.roborock_live_map import schedules as sch

NY = ZoneInfo("America/New_York")
VACUUM = "vacuum.robo"


def _valid(**over):
    base = {
        "name": "Morning",
        "time": "08:30",
        "days": [0, 2, 4],
        "rooms": [],
        "repeat": 1,
        "enabled": True,
    }
    return {**base, **over}


# --- validation -------------------------------------------------------------


def test_validation_normalises():
    out = sch.validate_schedule_input(_valid(name="  Hall  ", days=[4, 0, 2], rooms=[16, 17], repeat=2))
    assert out["name"] == "Hall"
    assert out["days"] == [0, 2, 4]
    assert out["rooms"] == [16, 17]


def test_validation_accepts_read_only_echo_and_id():
    out = sch.validate_schedule_input(
        _valid(id="abc", last_run=None, last_result="ok", next_run="x")
    )
    assert out["id"] == "abc"
    assert "last_run" not in out and "next_run" not in out


@pytest.mark.parametrize(
    "over",
    [
        {"name": ""},
        {"name": "   "},
        {"name": "x" * 65},
        {"name": 5},
        {"time": "8:30"},
        {"time": "24:00"},
        {"time": "08:60"},
        {"time": "08:30:00"},
        {"days": []},
        {"days": [7]},
        {"days": [-1]},
        {"days": [1, 1]},
        {"days": [True]},
        {"days": "1"},
        {"rooms": [1, 1]},
        {"rooms": [-1]},
        {"rooms": [65536]},
        {"rooms": ["1"]},
        {"rooms": [True]},
        {"rooms": list(range(65))},
        {"repeat": 0},
        {"repeat": 4},
        {"repeat": True},
        {"repeat": 1.0},
        {"repeat": 2, "rooms": []},  # whole home cannot repeat
        {"enabled": 1},
        {"enabled": "yes"},
        {"bogus": 1},
        {"id": ""},
        {"id": 3},
    ],
)
def test_validation_rejects(over):
    with pytest.raises(ValueError):
        sch.validate_schedule_input(_valid(**over))


@pytest.mark.parametrize("missing", ["name", "time", "days", "rooms", "repeat", "enabled"])
def test_validation_requires_fields(missing):
    data = _valid()
    del data[missing]
    with pytest.raises(ValueError):
        sch.validate_schedule_input(data)


def test_validation_rejects_non_object():
    with pytest.raises(ValueError):
        sch.validate_schedule_input([])


# --- time maths -------------------------------------------------------------


def test_spring_forward_gap_runs_once_shifted():
    item = {"time": "02:30", "days": list(range(7))}  # 02:30 does not exist on 2026-03-08
    after = datetime(2026, 3, 7, 12, 0, tzinfo=UTC)
    first = sch.next_after(item, after, NY)
    assert first == datetime(2026, 3, 8, 7, 30, tzinfo=UTC)  # 03:30 EDT
    second = sch.next_after(item, first, NY)
    assert second == datetime(2026, 3, 9, 6, 30, tzinfo=UTC)  # 02:30 EDT


def test_fall_back_ambiguous_runs_once_at_first():
    item = {"time": "01:30", "days": list(range(7))}  # happens twice on 2026-11-01
    after = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    first = sch.next_after(item, after, NY)
    assert first == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)  # EDT
    assert sch.latest_due(item, first, datetime(2026, 11, 1, 7, 0, tzinfo=UTC), NY) is None
    # the next one is the following day in EST
    assert sch.next_after(item, first, NY) == datetime(2026, 11, 2, 6, 30, tzinfo=UTC)


def test_weekday_filter_monday_is_zero():
    item = {"time": "08:00", "days": [0]}
    after = datetime(2026, 10, 28, 0, 0, tzinfo=UTC)  # a Wednesday
    assert sch.next_after(item, after, NY) == datetime(2026, 11, 2, 13, 0, tzinfo=UTC)


def test_latest_due_returns_only_latest_missed():
    item = {"time": "08:00", "days": list(range(7))}
    after = datetime(2026, 10, 20, 0, 0, tzinfo=UTC)
    now = datetime(2026, 10, 28, 20, 0, tzinfo=UTC)
    assert sch.latest_due(item, after, now, NY) == datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


def test_latest_due_bounds():
    item = {"time": "08:00", "days": list(range(7))}
    t = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    assert sch.latest_due(item, t, t, NY) is None  # (after, upto] is half open
    assert sch.latest_due(item, t - timedelta(seconds=1), t, NY) == t


# --- manager ------------------------------------------------------------------


@pytest.fixture
async def env(hass: HomeAssistant, freezer):
    """A vacuum entity, mocked vacuum services and a time zone."""
    await hass.config.async_set_time_zone("America/New_York")
    freezer.move_to("2026-10-28 11:00:00+00:00")  # 07:00 EDT, Wednesday
    entry = MockConfigEntry(domain="roborock")
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("roborock", "duid1")}
    )
    er.async_get(hass).async_get_or_create(
        "vacuum", "roborock", "duid1", suggested_object_id="robo", device_id=device.id
    )
    hass.states.async_set(VACUUM, "docked")
    start = async_mock_service(hass, "vacuum", "start")
    send = async_mock_service(hass, "vacuum", "send_command")
    return types.SimpleNamespace(device=device, start=start, send=send, freezer=freezer)


import types  # noqa: E402


async def _manager(hass, env, slug="duid1"):
    mgr = sch.ScheduleManager(hass, slug, env.device.id)
    await mgr.async_load()
    mgr.async_start()
    await hass.async_block_till_done()
    return mgr


async def _settle():
    """Let pending tasks run (frozen time: real sleeps would never return)."""
    for _ in range(50):
        await asyncio.sleep(0)


async def _advance(hass, env, to: str):
    env.freezer.move_to(to)
    async_fire_time_changed(hass, datetime.fromisoformat(to))
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_whole_home_fires_at_local_time_via_vacuum_start(hass, env):
    mgr = await _manager(hass, env)
    saved = await mgr.async_save(sch.validate_schedule_input(_valid(days=list(range(7)))))
    assert saved["next_run"] == "2026-10-28T12:30:00+00:00"  # 08:30 EDT
    assert not env.start
    await _advance(hass, env, "2026-10-28 12:30:00+00:00")
    assert len(env.start) == 1
    assert env.start[0].data == {"entity_id": VACUUM}
    assert not env.send
    listed = mgr.async_list()[0]
    assert listed["last_result"] == "ok"
    assert listed["last_run"] == "2026-10-28T12:30:00+00:00"
    assert listed["next_run"] == "2026-10-29T12:30:00+00:00"
    await mgr.async_stop()


async def test_rooms_use_segment_clean(hass, env):
    mgr = await _manager(hass, env)
    await mgr.async_save(
        sch.validate_schedule_input(_valid(days=list(range(7)), rooms=[16, 18], repeat=2))
    )
    await _advance(hass, env, "2026-10-28 12:30:00+00:00")
    assert not env.start
    assert env.send[0].data == {
        "entity_id": VACUUM,
        "command": "app_segment_clean",
        "params": [{"segments": [16, 18], "repeat": 2}],
    }
    await mgr.async_stop()


async def test_not_triggered_on_create_enable_or_edit(hass, env):
    mgr = await _manager(hass, env)
    # 06:00 local today already passed (now is 07:00 local).
    saved = await mgr.async_save(
        sch.validate_schedule_input(_valid(time="06:00", days=list(range(7)), enabled=False))
    )
    await mgr.async_save(sch.validate_schedule_input(_valid(id=saved["id"], time="06:00", days=list(range(7)))))
    await mgr.async_save(sch.validate_schedule_input(_valid(id=saved["id"], time="06:30", days=list(range(7)))))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not env.start and not env.send
    await mgr.async_stop()


async def test_disabled_never_fires_and_has_no_next_run(hass, env):
    mgr = await _manager(hass, env)
    saved = await mgr.async_save(
        sch.validate_schedule_input(_valid(days=list(range(7)), enabled=False))
    )
    assert saved["next_run"] is None
    await _advance(hass, env, "2026-10-28 13:00:00+00:00")
    assert not env.start
    await mgr.async_stop()






async def test_persisted_schedule_round_trips(hass, env, hass_storage):
    mgr = await _manager(hass, env)
    saved = await mgr.async_save(sch.validate_schedule_input(_valid(rooms=[16])))
    await mgr.async_stop()
    stored = hass_storage["roborock_live_map.schedules.duid1"]["data"]["schedules"]
    assert stored[0]["id"] == saved["id"]
    assert "rev" not in stored[0]
    mgr2 = await _manager(hass, env)
    assert mgr2.async_list()[0]["rooms"] == [16]
    await mgr2.async_stop()


async def test_invalid_stored_records_dropped(hass, env, hass_storage):
    hass_storage["roborock_live_map.schedules.duid1"] = {
        "version": 1,
        "minor_version": 1,
        "key": "roborock_live_map.schedules.duid1",
        "data": {
            "schedules": [
                {"id": "good", **_valid()},
                {"id": "bad", **_valid(time="99:99")},
                "junk",
                {**_valid()},
            ]
        },
    }
    mgr = await _manager(hass, env)
    assert [s["id"] for s in mgr.async_list()] == ["good"]
    await mgr.async_stop()


async def test_skips_when_already_cleaning(hass, env):
    mgr = await _manager(hass, env)
    await mgr.async_save(sch.validate_schedule_input(_valid(days=list(range(7)))))
    hass.states.async_set(VACUUM, "cleaning")
    await _advance(hass, env, "2026-10-28 12:30:00+00:00")
    assert not env.start
    assert mgr.async_list()[0]["last_result"] == "skipped: already cleaning"
    await mgr.async_stop()








async def test_edit_moves_schedule_and_old_timer_is_harmless(hass, env):
    mgr = await _manager(hass, env)
    saved = await mgr.async_save(sch.validate_schedule_input(_valid(days=list(range(7)))))
    moved = await mgr.async_save(
        sch.validate_schedule_input(_valid(id=saved["id"], time="10:00", days=list(range(7))))
    )
    assert moved["next_run"] == "2026-10-28T14:00:00+00:00"
    await _advance(hass, env, "2026-10-28 12:30:00+00:00")  # the old time
    assert not env.start
    await _advance(hass, env, "2026-10-28 14:00:00+00:00")
    assert len(env.start) == 1
    await mgr.async_stop()








async def test_timezone_change_does_not_fire_retroactively(hass, env):
    mgr = await _manager(hass, env)
    await mgr.async_save(sch.validate_schedule_input(_valid(time="09:00", days=list(range(7)))))
    # 11:00Z. Pacific: 09:00 PDT = 16:00Z, still ahead; Berlin: 09:00 CET = 08:00Z, already past.
    await hass.config.async_update(time_zone="Europe/Berlin")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not env.start
    assert mgr.async_list()[0]["next_run"] == "2026-10-29T08:00:00+00:00"
    await mgr.async_stop()


async def test_unknown_id_limit_and_concurrent_saves(hass, env):
    mgr = await _manager(hass, env)
    with pytest.raises(sch.ScheduleError) as err:
        await mgr.async_save(sch.validate_schedule_input(_valid(id="nope")))
    assert err.value.code == "not_found"
    with pytest.raises(sch.ScheduleError) as err:
        await mgr.async_delete("nope")
    assert err.value.code == "not_found"

    results = await asyncio.gather(
        *(mgr.async_save(sch.validate_schedule_input(_valid(name=f"s{i}"))) for i in range(sch.MAX_SCHEDULES + 5)),
        return_exceptions=True,
    )
    assert sum(isinstance(r, dict) for r in results) == sch.MAX_SCHEDULES
    limited = [r for r in results if isinstance(r, sch.ScheduleError)]
    assert len(limited) == 5 and all(e.code == "limit_reached" for e in limited)
    assert len({s["id"] for s in mgr.async_list()}) == sch.MAX_SCHEDULES
    await mgr.async_stop()


async def test_failed_persist_rolls_back(hass, env, monkeypatch):
    mgr = await _manager(hass, env)
    saved = await mgr.async_save(sch.validate_schedule_input(_valid()))

    async def boom(_data):
        raise OSError("disk full")

    monkeypatch.setattr(mgr._store, "async_save", boom)
    with pytest.raises(sch.ScheduleError) as err:
        await mgr.async_save(sch.validate_schedule_input(_valid(id=saved["id"], name="New")))
    assert err.value.code == "save_failed"
    assert mgr.async_list()[0]["name"] == "Morning"
    with pytest.raises(sch.ScheduleError):
        await mgr.async_delete(saved["id"])
    assert len(mgr.async_list()) == 1
    await mgr.async_stop()


async def test_restart_never_replays_missed_occurrences(hass, env):
    mgr = await _manager(hass, env)
    await mgr.async_save(sch.validate_schedule_input(_valid(days=list(range(7)))))
    await mgr.async_stop()

    # Down across three 08:30 EDT occurrences; nothing may run on startup.
    env.freezer.move_to("2026-10-30 18:00:00+00:00")
    mgr2 = await _manager(hass, env)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not env.start and not env.send
    assert mgr2.async_list()[0]["last_run"] is None
    # ...but the next occurrence after startup does run.
    await _advance(hass, env, "2026-10-31 12:30:00+00:00")
    assert len(env.start) == 1
    await mgr2.async_stop()


async def test_failed_start_is_recorded_and_never_retried(hass, env):
    mgr = await _manager(hass, env)
    await mgr.async_save(sch.validate_schedule_input(_valid(days=list(range(7)))))
    hass.states.async_set(VACUUM, "unavailable")
    await _advance(hass, env, "2026-10-28 12:30:00+00:00")
    assert mgr.async_list()[0]["last_result"].startswith("error:")
    hass.states.async_set(VACUUM, "docked")
    await _advance(hass, env, "2026-10-28 12:45:00+00:00")
    await _settle()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not env.start and not env.send
    await mgr.async_stop()


async def test_service_error_is_one_attempt(hass, env):
    calls = []

    async def failing(call):
        calls.append(call)
        raise HomeAssistantError("robot unreachable")

    hass.services.async_register("vacuum", "start", failing)
    mgr = await _manager(hass, env)
    await mgr.async_save(sch.validate_schedule_input(_valid(days=list(range(7)))))
    await _advance(hass, env, "2026-10-28 12:30:00+00:00")
    await _settle()
    assert len(calls) == 1
    assert "robot unreachable" in mgr.async_list()[0]["last_result"]
    await mgr.async_stop()


async def test_delete_while_run_in_flight_discards_result(hass, env):
    gate = asyncio.Event()

    async def slow(call):
        await gate.wait()

    hass.services.async_register("vacuum", "start", slow)
    mgr = await _manager(hass, env)
    saved = await mgr.async_save(sch.validate_schedule_input(_valid(days=list(range(7)))))
    env.freezer.move_to("2026-10-28 12:30:00+00:00")
    async_fire_time_changed(hass, datetime.fromisoformat("2026-10-28 12:30:00+00:00"))
    await _settle()
    await mgr.async_delete(saved["id"])
    gate.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mgr.async_list() == []
    await mgr.async_stop()


async def test_stop_cancels_everything_and_rejects_changes(hass, env):
    gate = asyncio.Event()

    async def slow(call):
        await gate.wait()

    hass.services.async_register("vacuum", "start", slow)
    mgr = await _manager(hass, env)
    await mgr.async_save(sch.validate_schedule_input(_valid(days=list(range(7)))))
    env.freezer.move_to("2026-10-28 12:30:00+00:00")
    async_fire_time_changed(hass, datetime.fromisoformat("2026-10-28 12:30:00+00:00"))
    await _settle()
    assert mgr._tasks  # a run is waiting on the robot
    await mgr.async_stop()
    assert not mgr._tasks and mgr._timer is None
    with pytest.raises(sch.ScheduleError) as err:
        await mgr.async_save(sch.validate_schedule_input(_valid()))
    assert err.value.code == "unavailable"
    await _advance(hass, env, "2026-10-29 12:30:00+00:00")
    assert not env.send
    await mgr.async_stop()  # idempotent
