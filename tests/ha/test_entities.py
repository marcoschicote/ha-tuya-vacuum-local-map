"""Frames through to entity state, storage, rate limits and diagnostics."""

import json
import xml.etree.ElementTree as ET

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er

from custom_components.tuya_vacuum_local_map.const import RUN_POINTS_INTERVAL
from custom_components.tuya_vacuum_local_map.diagnostics import (
    async_get_config_entry_diagnostics,
)

from ..fakes import DEVICE_ID, HOST, KEY, FakeDevice, wait_for
from .common import (
    CLOCK,
    ENTRY_ID,
    NS,
    STORE_KEY,
    chunk_dps,
    end_run,
    entity_id,
    envelope,
    feed,
    rooms_dps,
    run_once,
    serpentine,
    setup_entry,
    state,
)

RUN = 1700000000


@pytest.fixture(autouse=True)
def _device(factory):
    # A connected, silent vacuum for tests that feed frames in directly.
    factory.devices.extend(FakeDevice() for _ in range(5))
    return factory


# --- frames to entities ---


async def test_the_entities_exist_on_one_device(hass, clock):
    entry = await setup_entry(hass)
    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, entry.entry_id)
    assert {e.unique_id for e in entries} == {
        f"{ENTRY_ID}_{k}"
        for k in (
            "coverage_map",
            "rooms",
            "current_room",
            "run_points",
            "covered_area",
            "connected",
            "reset_map",
        )
    }
    assert len({e.device_id for e in entries}) == 1
    reset = registry.async_get(entity_id(hass, "button", "reset_map"))
    assert reset.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_run_shows_up_in_the_sensors_and_the_image(hass, hass_client, clock):
    entry = await setup_entry(hass)
    points = serpentine()
    await feed(hass, entry, rooms_dps())
    await feed(hass, entry, chunk_dps(1700000000, 0, points[:20], total=len(points)))
    await feed(hass, entry, chunk_dps(1700000000, 20, points[20:]))
    # during a run the count is shown every 10 s at most: let that time pass
    CLOCK[0] += RUN_POINTS_INTERVAL
    entry.runtime_data._notify()
    assert state(hass, "sensor", "run_points").state == str(len(points))
    attrs = state(hass, "sensor", "run_points").attributes
    assert attrs["path_id"] == 1700000000 and attrs["started"].startswith("2023-11-14")
    rooms = state(hass, "sensor", "rooms")
    assert rooms.state == "2"
    assert rooms.attributes["rooms"] == [
        {"id": 1, "name": "Hall"},
        {"id": 2, "name": "Study"},
    ]
    assert float(state(hass, "sensor", "covered_area").state) == 0  # not counted yet

    await end_run(hass, entry, clock)
    area = float(state(hass, "sensor", "covered_area").state)
    assert 2 < area < 8  # a 2 x 1 m patch, plus the brush's border
    assert state(hass, "binary_sensor", "connected").state == "on"

    client = await hass_client()
    response = await client.get(
        f"/api/image_proxy/{entity_id(hass, 'image', 'coverage_map')}"
    )
    assert response.status == 200
    assert response.headers["Content-Type"].startswith("image/svg+xml")
    root = ET.fromstring(await response.read())
    assert root.find(f".//{NS}path") is not None
    assert root.find(f".//{NS}polyline") is not None
    assert "Hall" in root.find(f"{NS}desc").text
    await hass.config_entries.async_unload(entry.entry_id)


async def test_frames_arrive_from_the_device_thread_end_to_end(hass, factory, clock):
    factory.devices.clear()
    points = serpentine(1000, 500)
    factory.devices.append(
        FakeDevice(
            {"dps": {"101": True, "105": "selectroom", **rooms_dps()}},
            inbox=[{"dps": chunk_dps(1700000000, 0, points)}],
        )
    )
    entry = await setup_entry(hass)
    coordinator = entry.runtime_data
    assert await hass.async_add_executor_job(
        wait_for, lambda: coordinator.frame_counts[21011] >= 1
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    clock[0] += 2  # past the once-a-second limit on entity updates
    coordinator._notify()
    assert state(hass, "sensor", "run_points").state == str(len(points))
    assert state(hass, "sensor", "rooms").state == "2"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_the_connected_sensor_follows_the_connection(hass, clock):
    entry = await setup_entry(hass)
    coordinator = entry.runtime_data
    await hass.async_add_executor_job(wait_for, lambda: coordinator.connected)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert state(hass, "binary_sensor", "connected").state == "on"
    coordinator._handle_connected(False)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert state(hass, "binary_sensor", "connected").state == "off"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_resent_run_is_not_counted_twice(hass, clock):
    entry = await setup_entry(hass)
    points = await run_once(hass, entry, clock)
    cells = entry.runtime_data.grid.cells()
    assert set(cells.values()) == {1}
    # opening the app's map screen re-sends the run, whole, hours later
    await feed(hass, entry, chunk_dps(1700000000, 0, points))
    await end_run(hass, entry, clock)
    assert entry.runtime_data.grid.cells() == cells
    assert state(hass, "sensor", "run_points").state == str(len(points))
    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_second_run_darkens_the_overlap(hass, clock):
    entry = await setup_entry(hass)
    await run_once(hass, entry, clock, path_id=1700000000)
    await run_once(hass, entry, clock, path_id=1700001000)
    assert set(entry.runtime_data.grid.cells().values()) == {2}
    await hass.config_entries.async_unload(entry.entry_id)


async def test_ordinary_data_points_and_unknown_types_are_handled(hass, clock):
    entry = await setup_entry(hass)
    await feed(hass, entry, {"101": True, "105": "chargring", "106": 80, "9": "hi"})
    await feed(hass, entry, {"1": envelope(40000, {"zeta": 1, "alpha": "secret"})})
    coordinator = entry.runtime_data
    assert coordinator.unknown_types == {40000: ["alpha", "zeta"]}
    assert "secret" not in json.dumps(coordinator.diagnostics())
    await hass.config_entries.async_unload(entry.entry_id)


async def test_the_room_command_is_kept_for_the_run_and_cleared_at_its_end(hass, clock):
    entry = await setup_entry(hass)
    command = {"cmds": [{"infoType": 21023, "data": {"segmentId": [2]}}]}
    await feed(hass, entry, {"127": envelope(30000, command)})
    assert entry.runtime_data.run_rooms == [2]
    await run_once(hass, entry, clock)
    assert entry.runtime_data.run_rooms is None
    await hass.config_entries.async_unload(entry.entry_id)


# --- storage ---


async def test_a_restart_restores_the_map(hass, clock, hass_storage):
    entry = await setup_entry(hass)
    await feed(hass, entry, rooms_dps())
    await run_once(hass, entry, clock)
    before = entry.runtime_data.grid.cells()
    area = state(hass, "sensor", "covered_area").state
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert STORE_KEY in hass_storage

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    coordinator = entry.runtime_data
    assert coordinator.grid.cells() == before
    assert state(hass, "sensor", "covered_area").state == area
    assert state(hass, "sensor", "rooms").state == "2"
    assert state(hass, "sensor", "run_points").state == str(len(serpentine()))
    assert coordinator.svg and b"<polyline" in coordinator.svg  # drawn at start
    # and the run the app re-sends after a restart is still not counted twice
    await feed(hass, entry, chunk_dps(1700000000, 0, serpentine()))
    await end_run(hass, entry, clock)
    assert coordinator.grid.cells() == before
    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_run_in_progress_is_saved_when_the_entry_unloads(
    hass, clock, hass_storage
):
    entry = await setup_entry(hass)
    await feed(hass, entry, chunk_dps(1700000000, 0, serpentine(1000, 500)))
    assert entry.runtime_data.grid.covered_cells() == 0  # still open
    await hass.config_entries.async_unload(entry.entry_id)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.runtime_data.grid.covered_cells() > 0
    await hass.config_entries.async_unload(entry.entry_id)


async def test_the_save_is_delayed_during_a_run_and_immediate_at_its_end(
    hass, clock, hass_storage
):
    entry = await setup_entry(hass)
    coordinator = entry.runtime_data
    saves = coordinator.save_count
    await feed(hass, entry, rooms_dps())  # a change: saved later, not now
    assert coordinator.save_count == saves
    await run_once(hass, entry, clock)  # a run ending saves at once
    assert coordinator.save_count > saves
    stored = hass_storage[STORE_KEY]["data"]
    assert stored["finished_ids"] == [1700000000]
    await hass.config_entries.async_unload(entry.entry_id)


async def test_the_stored_data_holds_no_map_id_or_user_data(hass, clock, hass_storage):
    entry = await setup_entry(hass)
    await feed(hass, entry, rooms_dps(map_id=1000000001))
    await run_once(hass, entry, clock)
    await hass.config_entries.async_unload(entry.entry_id)
    text = json.dumps(hass_storage[STORE_KEY])
    for private in ("1000000001", '"u"', '"s"', KEY, DEVICE_ID, HOST):
        assert private not in text


@pytest.mark.parametrize(
    "stored",
    [
        {"grid": "not base64!", "finished_ids": [], "rooms": {}, "last_run": []},
        {"grid": "AQ==", "finished_ids": [], "rooms": {}, "last_run": []},
        {"finished_ids": []},
        {"grid": 5, "finished_ids": 5, "rooms": 5, "last_run": 5},
        {},
        [],
    ],
)
async def test_a_corrupt_store_starts_empty_with_a_warning(
    hass, clock, hass_storage, caplog, stored
):
    hass_storage[STORE_KEY] = {"version": 1, "key": STORE_KEY, "data": stored}
    entry = await setup_entry(hass)
    assert entry.state is ConfigEntryState.LOADED
    coordinator = entry.runtime_data
    assert coordinator.grid.covered_cells() == 0 and coordinator.rooms == {}
    assert coordinator.tracker.finished_ids == []
    assert float(state(hass, "sensor", "covered_area").state) == 0
    assert "could not be read" in caplog.text or stored in ({}, [])
    # and it works from there
    await run_once(hass, entry, clock)
    assert coordinator.grid.covered_cells() > 0
    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_missing_store_is_not_a_problem(hass, clock, caplog):
    entry = await setup_entry(hass)
    assert "could not be read" not in caplog.text
    await hass.config_entries.async_unload(entry.entry_id)


# --- the map id ---


async def test_a_new_map_id_starts_a_new_map_and_keeps_the_old_one_for_a_generation(
    hass, clock, hass_storage
):
    entry = await setup_entry(hass)
    coordinator = entry.runtime_data
    await feed(hass, entry, rooms_dps(map_id=1000000001))
    await run_once(hass, entry, clock)
    cells = coordinator.grid.covered_cells()
    assert cells

    # the same map id again changes nothing
    await feed(hass, entry, rooms_dps(map_id=1000000001))
    assert coordinator.grid.covered_cells() == cells

    await feed(hass, entry, rooms_dps(map_id=1000000002, rooms=[(1, "Loft")]))
    assert coordinator.grid.covered_cells() == 0
    assert coordinator.rooms == {1: "Loft"}  # the old rooms went with the old map
    assert coordinator._previous_grid is not None
    assert float(state(hass, "sensor", "covered_area").state) == 0
    assert state(hass, "sensor", "run_points").state == "0"
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass_storage[STORE_KEY]["data"]["previous_grid"]
    await hass.config_entries.async_unload(entry.entry_id)


async def test_the_first_map_id_does_not_wipe_coverage(hass, clock):
    entry = await setup_entry(hass)
    await run_once(hass, entry, clock)  # a run before any room list arrived
    cells = entry.runtime_data.grid.covered_cells()
    await feed(hass, entry, rooms_dps())
    assert entry.runtime_data.grid.covered_cells() == cells
    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_new_map_id_after_a_restart_still_resets(hass, clock, hass_storage):
    entry = await setup_entry(hass)
    await feed(hass, entry, rooms_dps(map_id=1000000001))
    await run_once(hass, entry, clock)
    await hass.config_entries.async_unload(entry.entry_id)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.runtime_data.grid.covered_cells() > 0
    await feed(hass, entry, rooms_dps(map_id=1000000009))
    assert entry.runtime_data.grid.covered_cells() == 0
    await hass.config_entries.async_unload(entry.entry_id)


# --- the reset button ---


async def test_reset_clears_local_data_and_sends_nothing(hass, clock, factory):
    entry = await setup_entry(hass)
    coordinator = entry.runtime_data
    points = await run_once(hass, entry, clock)
    await coordinator.async_reset()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert coordinator.grid.covered_cells() == 0
    assert float(state(hass, "sensor", "covered_area").state) == 0
    assert state(hass, "sensor", "run_points").state == "0"
    assert coordinator._previous_grid is not None
    # the app re-sending the run we just wiped is not counted again
    await feed(hass, entry, chunk_dps(1700000000, 0, points))
    await end_run(hass, entry, clock)
    assert coordinator.grid.covered_cells() == 0
    await hass.config_entries.async_unload(entry.entry_id)
    allowed = {
        "status",
        "receive",
        "heartbeat",
        "set_socketTimeout",
        "set_socketRetryLimit",
        "set_socketRetryDelay",
    }
    for device in factory.made:
        assert set(device.calls) <= allowed


async def test_the_reset_button_press_resets(hass, clock):
    entry = await setup_entry(hass)
    await run_once(hass, entry, clock)
    registry = er.async_get(hass)
    button = entity_id(hass, "button", "reset_map")
    registry.async_update_entity(button, disabled_by=None)
    await hass.async_block_till_done(
        wait_background_tasks=True
    )  # reloads the entry with the button enabled
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.runtime_data.grid.covered_cells() > 0
    await hass.services.async_call(
        "button", "press", {"entity_id": button}, blocking=True
    )
    assert entry.runtime_data.grid.covered_cells() == 0
    await hass.config_entries.async_unload(entry.entry_id)


# --- options ---


async def test_hidden_rooms_leave_the_sensor_and_the_image(hass, clock):
    entry = await setup_entry(hass, options={"hidden_rooms": [2]})
    await feed(hass, entry, rooms_dps())
    assert state(hass, "sensor", "rooms").state == "1"
    await run_once(hass, entry, clock)
    desc = ET.fromstring(entry.runtime_data.svg).find(f"{NS}desc").text
    assert "Hall" in desc and "Study" not in desc
    await hass.config_entries.async_unload(entry.entry_id)


async def test_units_per_metre_scales_the_covered_area(hass, clock):
    first = await setup_entry(hass)
    await run_once(hass, first, clock)
    mm = float(state(hass, "sensor", "covered_area").state)
    await hass.config_entries.async_unload(first.entry_id)
    await hass.config_entries.async_remove(first.entry_id)

    second = await setup_entry(hass, options={"units_per_metre": 100})
    await run_once(hass, second, clock)
    assert float(state(hass, "sensor", "covered_area").state) == pytest.approx(
        mm * 100, rel=0.01
    )
    await hass.config_entries.async_unload(second.entry_id)


async def test_a_wider_brush_covers_more(hass, clock):
    first = await setup_entry(hass, options={"brush": 100})
    await run_once(hass, first, clock)
    narrow = first.runtime_data.grid.covered_cells()
    await hass.config_entries.async_unload(first.entry_id)
    await hass.config_entries.async_remove(first.entry_id)
    second = await setup_entry(hass, options={"brush": 500})
    await run_once(hass, second, clock)
    assert second.runtime_data.grid.covered_cells() > narrow * 2
    await hass.config_entries.async_unload(second.entry_id)


# --- rate limits ---


async def test_a_burst_of_frames_updates_entities_and_the_image_rarely(hass, clock):
    entry = await setup_entry(hass)
    coordinator = entry.runtime_data
    renders, notifies = coordinator.render_count, coordinator.notify_count
    points = serpentine(8000, 4000)
    for i, point in enumerate(points[:300]):  # 300 one-point chunks, no time passes
        coordinator._handle_dps(chunk_dps(1700000000, i, [point], total=len(points)))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert coordinator.tracker.point_count == 300
    assert coordinator.render_count - renders <= 1
    assert coordinator.notify_count - notifies <= 1
    # a second later: one more entity update, still no new drawing
    clock[0] += 1.5
    coordinator._handle_dps(chunk_dps(1700000000, 300, [points[300]]))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert coordinator.notify_count - notifies <= 2
    assert coordinator.render_count - renders <= 1
    await hass.config_entries.async_unload(entry.entry_id)


async def test_the_image_is_redrawn_every_30_seconds_during_a_run(hass, clock):
    entry = await setup_entry(hass)
    coordinator = entry.runtime_data
    points = serpentine(8000, 4000)
    await feed(hass, entry, chunk_dps(1700000000, 0, points[:10], total=500))
    first = coordinator.render_count
    for i in range(10, 200, 10):
        clock[0] += 1
        await feed(
            hass,
            entry,
            chunk_dps(1700000000, i, points[i : i + 10], total=500),
            tick=0,
        )
    assert coordinator.render_count == first  # 20 s in: not yet
    clock[0] += 15
    coordinator._request_render()  # what the 30 s timer does
    await hass.async_block_till_done(wait_background_tasks=True)
    assert coordinator.render_count == first + 1
    await hass.config_entries.async_unload(entry.entry_id)


async def test_the_image_is_drawn_at_once_when_a_run_ends(hass, clock):
    entry = await setup_entry(hass)
    coordinator = entry.runtime_data
    await feed(hass, entry, chunk_dps(1700000000, 0, serpentine(1000, 500)))
    before = coordinator.render_count
    await end_run(hass, entry, clock)
    assert coordinator.render_count == before + 1
    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_burst_of_frames_changes_the_recorder_state_rarely(hass, clock):
    entry = await setup_entry(hass)
    coordinator = entry.runtime_data
    events = []
    hass.bus.async_listen("state_changed", lambda e: events.append(e))
    for i in range(200):
        coordinator._handle_dps(chunk_dps(1700000000, i, [(i * 50, 0)], total=200))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(events) < 12  # a handful of entities, once or twice each
    await hass.config_entries.async_unload(entry.entry_id)


# --- diagnostics ---


async def test_diagnostics_hold_counts_and_nothing_private(hass, clock):
    entry = await setup_entry(hass)
    await feed(hass, entry, rooms_dps(map_id=1000000001, rooms=[(1, "Sótano")]))
    await feed(hass, entry, {"1": envelope(40000, {"zeta": 1, "alpha": "secret"})})
    await run_once(hass, entry, clock)
    result = await async_get_config_entry_diagnostics(hass, entry)
    text = json.dumps(result)
    for private in (KEY, DEVICE_ID, HOST, "1000000001", "Sótano", "secret"):
        assert private not in text
    assert result["entry"]["local_key"] == "**REDACTED**"
    state_ = result["state"]
    assert state_["frames_by_info_type"]["21011"] == 1
    assert state_["frames_by_info_type"]["21004"] == 1
    assert state_["unknown_info_types"] == {"40000": ["alpha", "zeta"]}
    assert state_["largest_frame"] > 100 and state_["rooms"] == 1
    assert state_["covered_cells"] > 0 and state_["finished_runs"] == 1
    assert "reconnects" in state_
    # no coordinates: none of the path's values appear as a list
    assert "posArray" not in text
    await hass.config_entries.async_unload(entry.entry_id)


async def test_removing_the_integration_deletes_its_stored_map(
    hass, clock, hass_storage
):
    entry = await setup_entry(hass)
    await run_once(hass, entry, clock)
    await hass.config_entries.async_unload(entry.entry_id)
    assert STORE_KEY in hass_storage
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert STORE_KEY not in hass_storage


async def test_a_change_is_saved_by_the_30_second_timer(hass, clock, hass_storage):
    entry = await setup_entry(hass)
    coordinator = entry.runtime_data
    await feed(hass, entry, rooms_dps())
    assert coordinator._save_timer is not None  # waiting
    saves = coordinator.save_count
    coordinator._save_due(None)  # what the timer does
    await hass.async_block_till_done(wait_background_tasks=True)
    assert coordinator.save_count == saves + 1
    assert hass_storage[STORE_KEY]["data"]["rooms"] == {"1": "Hall", "2": "Study"}
    await feed(hass, entry, rooms_dps(rooms=[(3, "Loft")]))
    await feed(hass, entry, rooms_dps(rooms=[(4, "Attic")]))
    assert coordinator._save_timer is not None  # one waiting save, not two
    await hass.config_entries.async_unload(entry.entry_id)
    assert coordinator._save_timer is None
    assert set(hass_storage[STORE_KEY]["data"]["rooms"]) == {"1", "2", "3", "4"}


# --- the run points sensor is not a firehose ---


async def test_run_points_changes_at_most_every_10_seconds_during_a_run(hass, clock):
    entry = await setup_entry(hass)
    entity = entity_id(hass, "sensor", "run_points")
    writes = []
    hass.bus.async_listen(
        "state_changed",
        lambda e: writes.append(e) if e.data["entity_id"] == entity else None,
    )
    points = serpentine(8000, 4000)
    for i in range(60):  # a point a second for a minute
        await feed(hass, entry, chunk_dps(RUN, i, [points[i]], total=500), tick=1.0)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert 3 <= len(writes) <= 8, len(writes)  # not 60
    assert int(state(hass, "sensor", "run_points").state) <= 60

    await end_run(hass, entry, clock)  # the run is over: the count is exact
    assert state(hass, "sensor", "run_points").state == "60"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_run_points_is_exact_when_a_new_run_starts(hass, clock):
    entry = await setup_entry(hass)
    points = serpentine()
    await feed(hass, entry, chunk_dps(RUN, 0, points[:50], total=100))
    await end_run(hass, entry, clock)
    assert state(hass, "sensor", "run_points").state == "50"
    await feed(hass, entry, chunk_dps(RUN + 1000, 0, points[:3], total=100))
    assert state(hass, "sensor", "run_points").state == "3"  # at once, not in 10 s
    assert state(hass, "sensor", "run_points").attributes["path_id"] == RUN + 1000
    await hass.config_entries.async_unload(entry.entry_id)


async def test_an_out_of_range_path_id_does_not_crash_run_points(hass, clock):
    # pathID comes from the device and is only checked to be an integer. A value too
    # large for a datetime must not make the sensor raise every time it is read.
    entry = await setup_entry(hass)
    points = serpentine()
    bad = 99_999_999_999_999  # would be the year 3170843
    await feed(hass, entry, chunk_dps(bad, 0, points, total=len(points)))
    sensor = state(hass, "sensor", "run_points")
    assert sensor.state == str(len(points))  # the state was written, not an error
    assert sensor.attributes["path_id"] == bad  # the raw id is still there
    assert "started" not in sensor.attributes  # left out, since it can't be a datetime
    await hass.config_entries.async_unload(entry.entry_id)
