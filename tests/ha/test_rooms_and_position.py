"""Room outlines, the current room, the live position and calibration (phase 5)."""

import json
import xml.etree.ElementTree as ET

import pytest

from custom_components.tuya_vacuum_local_map.core.render import ROOM_OPACITY
from custom_components.tuya_vacuum_local_map.diagnostics import (
    async_get_config_entry_diagnostics,
)

from ..fakes import FakeDevice
from .common import (
    CLOCK,
    NS,
    STORE_KEY,
    chunk_dps,
    end_run,
    envelope,
    feed,
    rooms_dps,
    serpentine,
    setup_entry,
    state,
)

RUN_A = 1700000000
RUN_B = 1700001000


def command(rooms):
    data = {"cmds": [{"infoType": 21023, "data": {"segmentId": rooms}}]}
    return {"127": envelope(30000, data)}


async def clean_room(hass, entry, room, path_id, points=None):
    """What the vacuum sends for a one-room clean, then the run going quiet."""
    await feed(hass, entry, command([room]))
    await feed(hass, entry, chunk_dps(path_id, 0, points or serpentine()))
    await end_run(hass, entry, CLOCK)


@pytest.fixture(autouse=True)
def _device(factory):
    factory.devices.extend(FakeDevice() for _ in range(6))


# --- learning outlines ---


async def test_a_one_room_clean_teaches_that_rooms_outline(hass, clock):
    entry = await setup_entry(hass)
    coordinator = entry.runtime_data
    await feed(hass, entry, rooms_dps())
    await clean_room(hass, entry, 1, RUN_A)
    assert coordinator.layer.known_rooms() == {1}
    assert len(coordinator.layer.owners()) == coordinator.grid.covered_cells()
    root = ET.fromstring(coordinator.svg)
    assert [t.text for t in root.iter(f"{NS}text")] == ["Hall"]
    assert any(
        p.get("fill-opacity") == str(ROOM_OPACITY) for p in root.iter(f"{NS}path")
    )
    await hass.config_entries.async_unload(entry.entry_id)


async def test_each_run_goes_to_its_own_command_even_when_they_overlap_in_time(
    hass, clock
):
    entry = await setup_entry(hass)
    coordinator = entry.runtime_data
    await feed(hass, entry, rooms_dps())
    await feed(hass, entry, command([1]))
    await feed(hass, entry, chunk_dps(RUN_A, 0, serpentine(1000, 500)))
    # the next command arrives before the first run has gone quiet
    await feed(hass, entry, command([2]))
    await feed(hass, entry, chunk_dps(RUN_B, 0, serpentine(1000, 500, step=50)[:30]))
    await end_run(hass, entry, CLOCK)
    assert coordinator._rooms_by_run == {RUN_A: [1], RUN_B: [2]}
    assert coordinator.layer.known_rooms() == {1, 2}
    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_command_that_no_run_follows_expires(hass, clock):
    entry = await setup_entry(hass)
    coordinator = entry.runtime_data
    await feed(hass, entry, command([1]))
    assert coordinator.run_rooms == [1]
    await feed(hass, entry, chunk_dps(RUN_A, 0, serpentine(1000, 500)), tick=200)
    await end_run(hass, entry, CLOCK)
    assert coordinator.layer.known_rooms() == set()  # too late to be its command
    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_run_without_a_command_teaches_nothing(hass, clock):
    entry = await setup_entry(hass)
    await feed(hass, entry, chunk_dps(RUN_A, 0, serpentine()))
    await end_run(hass, entry, CLOCK)
    assert not entry.runtime_data.layer
    await hass.config_entries.async_unload(entry.entry_id)


async def test_run_rooms_are_known_while_the_run_is_under_way(hass, clock):
    entry = await setup_entry(hass)
    coordinator = entry.runtime_data
    assert coordinator.run_rooms is None
    await feed(hass, entry, command([2, 5]))
    assert coordinator.run_rooms == [2, 5]  # waiting for its run
    await feed(hass, entry, chunk_dps(RUN_A, 0, serpentine(500, 250)))
    assert coordinator.run_rooms == [2, 5]  # now bound to it
    await end_run(hass, entry, CLOCK)
    assert coordinator.run_rooms is None
    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_multi_room_run_teaches_the_one_unknown_room(hass, clock):
    entry = await setup_entry(hass)
    coordinator = entry.runtime_data
    await feed(hass, entry, rooms_dps(rooms=[(1, "Hall"), (2, "Study")]))
    await clean_room(hass, entry, 1, RUN_A, serpentine(1000, 500))
    await feed(hass, entry, command([1, 2]))
    wide = serpentine(3000, 500)
    await feed(hass, entry, chunk_dps(RUN_B, 0, wide))
    await end_run(hass, entry, CLOCK)
    assert coordinator.layer.known_rooms() == {1, 2}
    owners = coordinator.layer.owners()
    assert owners[(40, 5)] == 2 and owners[(5, 5)] == 1
    await hass.config_entries.async_unload(entry.entry_id)


async def test_the_outlines_survive_a_restart(hass, clock, hass_storage):
    entry = await setup_entry(hass)
    await feed(hass, entry, rooms_dps())
    await clean_room(hass, entry, 1, RUN_A)
    owners = entry.runtime_data.layer.owners()
    await hass.config_entries.async_unload(entry.entry_id)
    assert hass_storage[STORE_KEY]["data"]["room_layer"]

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.runtime_data.layer.owners() == owners
    assert b"Hall" in entry.runtime_data.svg  # drawn at start
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("bad", ["not base64!", "AQ==", 5, ["x"]])
async def test_corrupt_outlines_lose_only_the_outlines(
    hass, clock, hass_storage, caplog, bad
):
    entry = await setup_entry(hass)
    await feed(hass, entry, rooms_dps())
    await clean_room(hass, entry, 1, RUN_A)
    cells = entry.runtime_data.grid.cells()
    await hass.config_entries.async_unload(entry.entry_id)
    hass_storage[STORE_KEY]["data"]["room_layer"] = bad

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.runtime_data.grid.cells() == cells
    assert not entry.runtime_data.layer
    assert "room outlines could not be read" in caplog.text
    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_stores_without_outlines_loads_fine(hass, clock, hass_storage):
    entry = await setup_entry(hass)
    await clean_room(hass, entry, 1, RUN_A)
    await hass.config_entries.async_unload(entry.entry_id)
    del hass_storage[STORE_KEY]["data"]["room_layer"]  # a phase 4 store
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.runtime_data.grid.covered_cells() > 0 and not entry.runtime_data.layer
    await hass.config_entries.async_unload(entry.entry_id)


async def test_reset_and_a_new_map_forget_the_outlines(hass, clock):
    entry = await setup_entry(hass)
    coordinator = entry.runtime_data
    await feed(hass, entry, rooms_dps(map_id=1000000001))
    await clean_room(hass, entry, 1, RUN_A)
    assert coordinator.layer
    await feed(hass, entry, rooms_dps(map_id=1000000002))
    assert not coordinator.layer
    await clean_room(hass, entry, 1, RUN_B)
    assert coordinator.layer
    await coordinator.async_reset()
    assert not coordinator.layer
    await hass.config_entries.async_unload(entry.entry_id)


# --- current room and live position ---


async def test_the_current_room_follows_the_vacuum(hass, clock):
    entry = await setup_entry(hass)
    await feed(hass, entry, rooms_dps())
    await clean_room(hass, entry, 1, RUN_A)  # room 1 is the patch around the origin
    assert state(hass, "sensor", "current_room").state == "unknown"  # not running

    await feed(hass, entry, chunk_dps(RUN_B, 0, [(500, 500)]))
    assert state(hass, "sensor", "current_room").state == "Hall"
    await feed(hass, entry, chunk_dps(RUN_B, 1, [(9000, 9000)]))
    assert state(hass, "sensor", "current_room").state == "unknown"  # unlearned
    await feed(hass, entry, chunk_dps(RUN_B, 2, [(500, 500)]))
    assert state(hass, "sensor", "current_room").state == "Hall"
    await end_run(hass, entry, CLOCK)
    assert state(hass, "sensor", "current_room").state == "unknown"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_hidden_room_is_still_named_as_the_current_room(hass, clock):
    entry = await setup_entry(hass, options={"hidden_rooms": [1]})
    await feed(hass, entry, rooms_dps())
    await clean_room(hass, entry, 1, RUN_A)
    await feed(hass, entry, chunk_dps(RUN_B, 0, [(500, 500)]))
    assert state(hass, "sensor", "current_room").state == "Hall"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_the_live_position_is_drawn_during_a_run_and_gone_after(hass, clock):
    entry = await setup_entry(hass)
    coordinator = entry.runtime_data
    await feed(hass, entry, chunk_dps(RUN_A, 0, serpentine(1000, 500)))
    coordinator._request_render(immediate=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    root = ET.fromstring(coordinator.svg)
    assert root.find(f".//{NS}circle") is not None
    await end_run(hass, entry, CLOCK)
    assert ET.fromstring(coordinator.svg).find(f".//{NS}circle") is None
    await hass.config_entries.async_unload(entry.entry_id)


async def test_the_position_is_the_newest_point_not_the_last_to_arrive(hass, clock):
    entry = await setup_entry(hass)
    points = serpentine(1000, 500)
    await feed(hass, entry, chunk_dps(RUN_A, 10, points[10:20], total=40))
    await feed(hass, entry, chunk_dps(RUN_A, 0, points[:10], total=40))  # late
    assert entry.runtime_data.position == points[19]
    await hass.config_entries.async_unload(entry.entry_id)


# --- calibration ---


async def test_the_map_area_is_compared_with_the_reported_area(hass, clock):
    entry = await setup_entry(hass)
    coordinator = entry.runtime_data
    await feed(hass, entry, {"102": envelope(20001, {"cleanArea": 11, "elec": 80})})
    assert coordinator.reported_area == 11
    await feed(hass, entry, chunk_dps(RUN_A, 0, serpentine()))
    await end_run(hass, entry, CLOCK)
    (row,) = coordinator.calibration
    assert row["reported_area"] == 11
    assert row["map_area"] == coordinator.covered_area
    assert row["ratio"] == round(row["map_area"] / 11, 2)

    result = await async_get_config_entry_diagnostics(hass, entry)
    assert result["state"]["calibration"] == [row]
    assert result["state"]["reported_area"] == 11
    await hass.config_entries.async_unload(entry.entry_id)


async def test_no_calibration_without_a_reported_area(hass, clock):
    entry = await setup_entry(hass)
    await feed(hass, entry, {"102": envelope(20001, {"cleanArea": 0})})
    await feed(hass, entry, chunk_dps(RUN_A, 0, serpentine()))
    await end_run(hass, entry, CLOCK)
    assert entry.runtime_data.calibration == []
    await hass.config_entries.async_unload(entry.entry_id)


async def test_only_the_last_few_calibrations_are_kept(hass, clock):
    entry = await setup_entry(hass)
    await feed(hass, entry, {"102": envelope(20001, {"cleanArea": 5})})
    for i in range(8):
        await feed(hass, entry, chunk_dps(RUN_A + i * 1000, 0, serpentine(500, 250)))
        await end_run(hass, entry, CLOCK)
    assert len(entry.runtime_data.calibration) == 5
    await hass.config_entries.async_unload(entry.entry_id)


async def test_diagnostics_still_hold_nothing_private_with_outlines(hass, clock):
    entry = await setup_entry(hass)
    await feed(hass, entry, rooms_dps(rooms=[(1, "Sótano")]))
    await clean_room(hass, entry, 1, RUN_A)
    result = await async_get_config_entry_diagnostics(hass, entry)
    text = json.dumps(result)
    assert "Sótano" not in text and result["state"]["rooms_with_outlines"] == 1
    await hass.config_entries.async_unload(entry.entry_id)
