"""End to end: the real tinytuya, over a real socket, to a fake vacuum on localhost.

Nothing in the connection is mocked here: the integration's thread opens a TCP
connection, decrypts real Tuya 3.3 frames, and the coordinator turns them into
entities. Only the clock that decides when a run has gone quiet is moved forward.
"""

import asyncio
import time
import xml.etree.ElementTree as ET

import pytest
import tinytuya
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tuya_vacuum_local_map import connection
from custom_components.tuya_vacuum_local_map import coordinator as coordinator_module
from custom_components.tuya_vacuum_local_map.const import (
    CONF_DEVICE_ID,
    CONF_HOST,
    CONF_LOCAL_KEY,
    CONF_VERSION,
    DOMAIN,
    ISSUE_STALLED,
)

from ..fake_vacuum import (
    DEVICE_ID,
    KEY,
    WRONG_KEY,
    FakeVacuum,
    chunk_dps,
    command_dps,
    rooms_dps,
    run_messages,
    serpentine,
)
from .common import ENTRY_ID, IDLE, NS, state

ENTRY_DATA = {
    CONF_HOST: "127.0.0.1",
    CONF_DEVICE_ID: DEVICE_ID,
    CONF_LOCAL_KEY: KEY,
    CONF_VERSION: "3.3",
}
RUN = 1700000000


@pytest.fixture(autouse=True)
def _real_sockets(socket_enabled):
    """The HA test plugin blocks sockets; these tests exist to use a real one."""


@pytest.fixture
def vacuum(socket_enabled):
    with FakeVacuum() as fake:
        yield fake


@pytest.fixture(autouse=True)
def _real_library(monkeypatch, request):
    """Make the integration's own device factory talk to the fake's port."""
    if "vacuum" not in request.fixturenames:
        return

    fake = request.getfixturevalue("vacuum")

    def device(device_id, host, key, version, persist):
        return tinytuya.Device(
            device_id, host, key, version=version, persist=persist, port=fake.port
        )

    monkeypatch.setattr(connection, "_tinytuya_device", device)


@pytest.fixture
def later(monkeypatch):
    """The coordinator's clock, with a way to jump forward without waiting."""
    offset = [0.0]
    monkeypatch.setattr(
        coordinator_module, "_monotonic", lambda: time.monotonic() + offset[0]
    )
    return offset


async def eventually(condition, timeout=5.0, what="condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


async def setup(hass, data=None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=data or ENTRY_DATA,
        unique_id=(data or ENTRY_DATA)[CONF_DEVICE_ID],
        entry_id=ENTRY_ID,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def stream(hass, vacuum, messages):
    """Push messages from a thread, as the vacuum would, in order."""

    def send():
        for dps in messages:
            vacuum.push(dps)

    await hass.async_add_executor_job(send)


async def finish_run(hass, entry, later):
    later[0] += IDLE
    entry.runtime_data._tick(None)
    await hass.async_block_till_done(wait_background_tasks=True)


# --- connecting ---


async def test_it_connects_and_only_ever_reads(hass, vacuum):
    entry = await setup(hass)
    coordinator = entry.runtime_data
    await eventually(lambda: coordinator.connected, what="a connection")
    await eventually(lambda: vacuum.heartbeats >= 3, what="heartbeats")
    assert vacuum.commands[0] in (10, 16)  # the first thing it did was ask for status
    assert vacuum.only_reads, vacuum.commands
    assert vacuum.connections == 1 and vacuum.connected_clients == 1
    await hass.config_entries.async_unload(entry.entry_id)
    assert vacuum.only_reads


async def test_unload_is_quick_and_closes_the_connection(hass, vacuum):
    entry = await setup(hass)
    coordinator = entry.runtime_data
    await eventually(lambda: coordinator.connected)
    started = time.monotonic()
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert time.monotonic() - started < 3
    await eventually(lambda: vacuum.connected_clients == 0, what="the socket to close")
    assert not coordinator.connection._thread.is_alive()


async def test_the_first_status_is_delivered(hass, vacuum):
    vacuum.status = {"101": True, "105": "selectroom", **rooms_dps()}
    entry = await setup(hass)
    await eventually(lambda: entry.runtime_data.rooms, what="the room list")
    assert entry.runtime_data.rooms == {1: "Hall", 2: "Study"}
    await hass.config_entries.async_unload(entry.entry_id)


# --- a whole run ---


async def test_a_whole_run_reaches_the_map(hass, hass_client, vacuum, later):
    entry = await setup(hass)
    coordinator = entry.runtime_data
    await eventually(lambda: coordinator.connected)
    points = serpentine()

    await stream(hass, vacuum, [rooms_dps(), command_dps([1])])
    await stream(hass, vacuum, run_messages(RUN, points))
    await eventually(
        lambda: coordinator.tracker.point_count == len(points), what="the whole path"
    )
    assert coordinator.position == points[-1]  # while it runs
    assert coordinator.run_rooms == [1]

    await finish_run(hass, entry, later)
    assert coordinator.grid.covered_cells() > 0
    assert coordinator.layer.known_rooms() == {1}
    assert state(hass, "sensor", "run_points").state == str(len(points))
    assert float(state(hass, "sensor", "covered_area").state) > 2
    assert state(hass, "binary_sensor", "connected").state == "on"
    assert vacuum.only_reads

    client = await hass_client()
    entity_id = state(hass, "image", "coverage_map").entity_id
    response = await client.get(f"/api/image_proxy/{entity_id}")
    assert response.status == 200
    assert response.headers["Content-Type"].startswith("image/svg+xml")
    root = ET.fromstring(await response.read())
    assert root.find(f".//{NS}polyline") is not None
    assert [t.text for t in root.iter(f"{NS}text")] == ["Hall"]
    await hass.config_entries.async_unload(entry.entry_id)


async def test_the_app_resending_the_last_run_does_not_count_it_twice(
    hass, vacuum, later
):
    entry = await setup(hass)
    coordinator = entry.runtime_data
    await eventually(lambda: coordinator.connected)
    points = serpentine(1000, 500)
    messages = list(run_messages(RUN, points))
    await stream(hass, vacuum, messages)
    await eventually(lambda: coordinator.tracker.point_count == len(points))
    await finish_run(hass, entry, later)
    cells = coordinator.grid.cells()
    # opening the map screen: the previous run, whole, in one chunk
    await stream(hass, vacuum, [chunk_dps(RUN, 0, points)])
    await eventually(lambda: coordinator.frame_counts[21011] == len(messages) + 1)
    await finish_run(hass, entry, later)
    assert coordinator.grid.cells() == cells
    await hass.config_entries.async_unload(entry.entry_id)


# --- big frames and bad ones ---


async def test_a_frame_bigger_than_tinytuyas_own_limit_gets_through(hass, vacuum):
    entry = await setup(hass)
    coordinator = entry.runtime_data
    await eventually(lambda: coordinator.connected)
    big = serpentine(8000, 4000)[:600]
    message = chunk_dps(RUN, 0, big)
    assert len(message["123"]) > 5 * 1440
    await stream(hass, vacuum, [message])
    await eventually(lambda: coordinator.tracker.point_count == 600, what="the frame")
    assert coordinator.largest_frame > 1440
    await hass.config_entries.async_unload(entry.entry_id)


async def test_garbage_and_unknown_messages_do_not_hurt(hass, vacuum):
    entry = await setup(hass)
    coordinator = entry.runtime_data
    await eventually(lambda: coordinator.connected)
    await stream(
        hass,
        vacuum,
        [
            {"123": "!!! not base64 !!!", "9": 5, "10": None, "11": ["x"]},
            {"123": "e30="},  # base64 of {}
            {"123": "W10="},  # base64 of []
            chunk_dps(RUN, 0, [(1, 2)]),
            {"127": "A" * 5000},
        ],
    )
    await eventually(lambda: coordinator.tracker.point_count == 1)
    assert coordinator.connected and coordinator.connection._thread.is_alive()
    await hass.config_entries.async_unload(entry.entry_id)


# --- the connection drops ---


async def test_it_reconnects_after_a_drop_and_keeps_going(hass, vacuum):
    entry = await setup(hass)
    coordinator = entry.runtime_data
    await eventually(lambda: coordinator.connected)
    points = serpentine(1000, 500)
    messages = list(run_messages(RUN, points))
    half = len(messages) // 2
    await stream(hass, vacuum, messages[:half])
    await eventually(lambda: coordinator.frame_counts[21011] >= half)

    vacuum.drop_clients()
    await eventually(lambda: vacuum.connections >= 2, what="a second connection")
    await eventually(lambda: coordinator.connected, what="to be connected again")
    assert coordinator.connection.reconnects >= 1

    await stream(hass, vacuum, messages[half:])
    await eventually(lambda: coordinator.tracker.point_count == len(points))
    assert vacuum.only_reads
    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_vacuum_that_allows_one_connection_raises_the_repair_issue(
    hass, monkeypatch
):
    monkeypatch.setattr(connection, "STALLED_AFTER", 0.3)
    with FakeVacuum(one_connection_only=True) as fake:

        def device(device_id, host, key, version, persist):
            return tinytuya.Device(
                device_id, host, key, version=version, persist=persist, port=fake.port
            )

        monkeypatch.setattr(connection, "_tinytuya_device", device)
        # another app already holds the one connection
        holder = tinytuya.Device(
            DEVICE_ID, "127.0.0.1", KEY, version=3.3, persist=True, port=fake.port
        )
        holder.set_socketTimeout(2)
        assert holder.status()["dps"]

        entry = await setup(hass)
        coordinator = entry.runtime_data
        registry = ir.async_get(hass)
        issue_id = f"{ISSUE_STALLED}_{entry.entry_id}"
        await eventually(
            lambda: registry.async_get_issue(DOMAIN, issue_id) is not None,
            timeout=15,
            what="the repair issue",
        )
        assert not coordinator.connected and fake.refused >= 1
        assert entry.state is ConfigEntryState.LOADED

        holder.close()  # the other app lets go
        await eventually(lambda: coordinator.connected, timeout=15, what="to connect")
        await eventually(
            lambda: registry.async_get_issue(DOMAIN, issue_id) is None,
            what="the issue to clear",
        )
        await hass.config_entries.async_unload(entry.entry_id)


async def test_a_wrong_key_never_connects_and_is_not_fatal(hass, vacuum):
    entry = await setup(hass, {**ENTRY_DATA, CONF_LOCAL_KEY: WRONG_KEY})
    coordinator = entry.runtime_data
    await eventually(
        lambda: coordinator.connection.last_error is not None,
        timeout=15,
        what="a failure",
    )
    assert coordinator.connection.last_error in ("904", "914"), (
        coordinator.connection.last_error
    )
    assert not coordinator.connected and entry.state is ConfigEntryState.LOADED
    await hass.config_entries.async_unload(entry.entry_id)


# --- setting it up, against the real library ---


async def start_flow(hass, user_input):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    return await hass.config_entries.flow.async_configure(result["flow_id"], user_input)


async def test_the_setup_form_works_against_a_real_connection(hass, vacuum):
    result = await start_flow(
        hass,
        {
            "host": "127.0.0.1",
            "device_id": DEVICE_ID,
            "local_key": KEY,
            "version": "3.3",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    await eventually(lambda: vacuum.connections >= 2)  # the check, then the real one
    await hass.config_entries.async_unload(result["result"].entry_id)


async def test_the_setup_form_says_invalid_auth_for_a_wrong_key(hass, vacuum):
    result = await start_flow(
        hass,
        {
            "host": "127.0.0.1",
            "device_id": DEVICE_ID,
            "local_key": WRONG_KEY,
            "version": "3.3",
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_the_setup_form_says_cannot_connect_for_nothing_listening(hass, vacuum):
    port = vacuum.port
    vacuum.stop()  # now nothing listens there
    started = time.monotonic()
    result = await start_flow(
        hass,
        {
            "host": "127.0.0.1",
            "device_id": DEVICE_ID,
            "local_key": KEY,
            "version": "3.3",
        },
    )
    took = time.monotonic() - started
    assert result["errors"] == {"base": "cannot_connect"}, port
    assert took < 10, f"the form took {took:.1f} s to give up"
