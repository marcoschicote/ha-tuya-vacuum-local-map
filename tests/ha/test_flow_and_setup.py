"""Config flow, options flow, setup and unload, against a fake tinytuya Device."""

import base64
import json

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tuya_vacuum_local_map import connection
from custom_components.tuya_vacuum_local_map.const import (
    CONF_BRUSH,
    CONF_DEVICE_ID,
    CONF_HIDDEN_ROOMS,
    CONF_HOST,
    CONF_LOCAL_KEY,
    CONF_UNITS_PER_METRE,
    CONF_VERSION,
    DOMAIN,
    ISSUE_STALLED,
)

from ..fakes import DEVICE_ID, HOST, KEY, FakeDevice, wait_for
from .common import DATA, FRAMES, USER_INPUT, room_dps, setup_entry


async def start_flow(hass, user_input):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(result["flow_id"], user_input)


# --- config flow ---


async def test_flow_success_creates_an_entry(hass, factory):
    device = FakeDevice()
    factory.devices.append(device)
    result = await start_flow(hass, USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Tuya vacuum"
    assert DEVICE_ID not in result["title"]
    assert result["data"] == DATA
    assert result["result"].unique_id == DEVICE_ID
    assert factory.args[0] == (DEVICE_ID, HOST, KEY, 3.3, False)  # the probe
    assert device.closed.is_set()
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(result["result"].entry_id)


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        ({"Err": "914"}, "invalid_auth"),
        ({"Err": "904"}, "invalid_auth"),
        ({"Err": "905"}, "cannot_connect"),
        ({"Err": "901"}, "cannot_connect"),
        ({"Err": "902"}, "cannot_connect"),
        (None, "cannot_connect"),
        (OSError("refused"), "cannot_connect"),
    ],
)
async def test_flow_errors_then_success(hass, factory, reply, error):
    bad = FakeDevice()
    bad.status = (
        (lambda nowait=False: (_ for _ in ()).throw(reply))
        if isinstance(reply, Exception)
        else (lambda nowait=False: reply)
    )
    factory.devices.append(bad)
    result = await start_flow(hass, USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}
    # the form comes back with what was typed, except the key
    factory.devices.append(FakeDevice())
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(result["result"].entry_id)


async def test_a_key_of_the_wrong_length_is_rejected_without_connecting(hass, factory):
    result = await start_flow(hass, {**USER_INPUT, CONF_LOCAL_KEY: "tooshort"})
    assert result["errors"] == {"base": "invalid_auth"}
    assert factory.args == []


async def test_any_key_length_is_fine_for_protocol_3_1(hass, factory):
    factory.devices.append(FakeDevice())
    result = await start_flow(
        hass, {**USER_INPUT, CONF_LOCAL_KEY: "", CONF_VERSION: "3.1"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(result["result"].entry_id)


async def test_whitespace_around_the_input_is_trimmed(hass, factory):
    factory.devices.append(FakeDevice())
    result = await start_flow(
        hass,
        {
            **USER_INPUT,
            CONF_HOST: f"  {HOST} ",
            CONF_DEVICE_ID: f" {DEVICE_ID}\n",
            CONF_LOCAL_KEY: f" {KEY} ",
        },
    )
    assert result["data"] == DATA
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(result["result"].entry_id)


async def test_the_same_device_cannot_be_added_twice(hass, factory):
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, unique_id=DEVICE_ID)
    entry.add_to_hass(hass)
    result = await start_flow(hass, USER_INPUT)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert factory.args == []  # nothing tried


async def test_unknown_protocol_version_is_not_accepted(hass, factory):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, CONF_VERSION: "9.9"}
        )


# --- setup and unload ---


async def test_setup_connects_and_unload_stops_the_thread(hass, factory):
    factory.devices.append(FakeDevice({"dps": room_dps()}))
    entry = await setup_entry(hass)
    hub = entry.runtime_data
    assert entry.state is ConfigEntryState.LOADED
    assert await hass.async_add_executor_job(
        wait_for, lambda: hub.connected and hub.rooms
    )
    await hass.async_block_till_done()
    assert hub.frame_counts[21004] >= 1
    thread = hub.connection._thread
    assert thread.is_alive() and thread.daemon
    assert factory.args[0] == (DEVICE_ID, HOST, KEY, 3.3, True)  # persistent

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert not thread.is_alive()
    assert factory.made[0].closed.is_set()


async def test_rooms_from_the_room_list_are_decoded_and_merged(hass, factory):
    # a later list missing a room must not drop it
    full = {"dps": room_dps()}
    partial = json.loads(base64.b64decode(FRAMES["room_list"]["b64"]))
    partial["data"]["autoAreaValue"] = partial["data"]["autoAreaValue"][:2]
    partial_b64 = base64.b64encode(json.dumps(partial).encode()).decode()
    factory.devices.append(
        FakeDevice(full, inbox=[{"dps": {"127": partial_b64}}, {"dps": {"105": "x"}}])
    )
    entry = await setup_entry(hass)
    hub = entry.runtime_data
    assert await hass.async_add_executor_job(
        wait_for, lambda: hub.frame_counts[21004] >= 2
    )
    await hass.async_block_till_done()
    assert sorted(hub.rooms) == [1, 2, 3, 4, 5, 6]
    assert "Sótano" in hub.rooms.values()
    await hass.config_entries.async_unload(entry.entry_id)


async def test_ordinary_data_points_are_ignored_quietly(hass, factory):
    factory.devices.append(
        FakeDevice({"dps": {"101": True, "105": "chargring", "106": 80}})
    )
    entry = await setup_entry(hass)
    hub = entry.runtime_data
    assert await hass.async_add_executor_job(wait_for, lambda: hub.connected)
    await hass.async_block_till_done()
    assert not hub.frame_counts and hub.rooms == {}
    await hass.config_entries.async_unload(entry.entry_id)


async def test_connected_follows_the_socket_and_reconnects(hass, factory):
    factory.devices.append(FakeDevice(inbox=[{"Err": "905"}]))
    factory.devices.append(FakeDevice())
    entry = await setup_entry(hass)
    hub = entry.runtime_data
    assert await hass.async_add_executor_job(
        wait_for, lambda: len(factory.made) >= 2 and hub.connected
    )
    await hass.async_block_till_done()
    assert hub.connected and hub.connection.reconnects >= 1
    await hass.config_entries.async_unload(entry.entry_id)


async def test_only_reading_calls_are_made_through_setup(hass, factory):
    factory.devices.append(FakeDevice(inbox=[{"dps": {"1": 1}}]))
    entry = await setup_entry(hass)
    assert await hass.async_add_executor_job(
        wait_for, lambda: "heartbeat" in factory.made[0].calls
    )
    await hass.config_entries.async_unload(entry.entry_id)
    allowed = {
        "status",
        "receive",
        "heartbeat",
        "set_socketTimeout",
        "set_socketRetryLimit",
        "set_socketRetryDelay",
    }
    assert set(factory.made[0].calls) <= allowed


async def test_an_unreachable_vacuum_leaves_the_entry_loaded_and_retrying(
    hass, factory
):
    factory.devices.extend(FakeDevice({"Err": "905"}) for _ in range(5))
    entry = await setup_entry(hass)
    hub = entry.runtime_data
    assert await hass.async_add_executor_job(wait_for, lambda: len(factory.made) >= 3)
    assert entry.state is ConfigEntryState.LOADED and not hub.connected
    await hass.config_entries.async_unload(entry.entry_id)


# --- the repair issue ---


async def test_a_long_outage_raises_a_repair_issue_and_recovery_removes_it(
    hass, factory
):
    factory.devices.append(FakeDevice())
    entry = await setup_entry(hass)
    hub = entry.runtime_data
    registry = ir.async_get(hass)
    issue_id = f"{ISSUE_STALLED}_{entry.entry_id}"

    hub._handle_stalled(True)
    issue = registry.async_get_issue(DOMAIN, issue_id)
    assert issue is not None and issue.translation_key == ISSUE_STALLED
    assert not issue.is_fixable
    hub._handle_stalled(False)
    assert registry.async_get_issue(DOMAIN, issue_id) is None

    hub._handle_stalled(True)
    await hass.config_entries.async_unload(entry.entry_id)
    assert registry.async_get_issue(DOMAIN, issue_id) is None  # gone on unload


async def test_the_outage_reaches_the_repair_issue_end_to_end(
    hass, factory, monkeypatch
):
    monkeypatch.setattr(connection, "STALLED_AFTER", 0.05)
    factory.devices.extend(FakeDevice({"Err": "901"}) for _ in range(100))
    entry = await setup_entry(hass)
    registry = ir.async_get(hass)
    issue_id = f"{ISSUE_STALLED}_{entry.entry_id}"

    async def raised():
        await hass.async_block_till_done()
        return registry.async_get_issue(DOMAIN, issue_id) is not None

    for _ in range(100):
        if await raised():
            break
        await hass.async_add_executor_job(wait_for, lambda: False, 0.02)
    assert registry.async_get_issue(DOMAIN, issue_id) is not None
    await hass.config_entries.async_unload(entry.entry_id)


# --- options flow ---


async def test_options_flow_offers_the_known_rooms_and_saves_numbers(hass, factory):
    factory.devices.append(FakeDevice({"dps": room_dps()}))
    entry = await setup_entry(hass)
    hub = entry.runtime_data
    assert await hass.async_add_executor_job(wait_for, lambda: len(hub.rooms) == 6)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "init"
    schema = result["data_schema"].schema
    hidden = next(k for k in schema if k == CONF_HIDDEN_ROOMS)
    values = {o["value"] for o in schema[hidden].config["options"]}
    assert values == {"1", "2", "3", "4", "5", "6"}
    defaults = {str(k): k.default() for k in schema if hasattr(k, "default")}
    assert defaults[CONF_BRUSH] == 300 and defaults[CONF_UNITS_PER_METRE] == 1000

    factory.devices.append(FakeDevice())
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_HIDDEN_ROOMS: ["5", "2"], CONF_BRUSH: 250.0, CONF_UNITS_PER_METRE: 100.0},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {
        CONF_HIDDEN_ROOMS: [2, 5],
        CONF_BRUSH: 250,
        CONF_UNITS_PER_METRE: 100,
    }
    await hass.async_block_till_done()
    # changing options reloads the entry, which opens a fresh connection
    assert entry.state is ConfigEntryState.LOADED
    assert len(factory.made) >= 2
    await hass.config_entries.async_unload(entry.entry_id)


async def test_options_flow_works_before_any_room_is_known(hass, factory):
    factory.devices.append(FakeDevice())
    entry = await setup_entry(hass, options={CONF_HIDDEN_ROOMS: [7]})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    hidden = next(k for k in result["data_schema"].schema if k == CONF_HIDDEN_ROOMS)
    options = result["data_schema"].schema[hidden].config["options"]
    assert [o["value"] for o in options] == ["7"]  # a hidden room stays selectable
    hass.config_entries.options.async_abort(result["flow_id"])
    await hass.config_entries.async_unload(entry.entry_id)
