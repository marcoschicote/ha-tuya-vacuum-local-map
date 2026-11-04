"""Helpers shared by the Home Assistant tests."""

import base64
import json
import pathlib

from homeassistant.config_entries import ConfigEntry
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tuya_vacuum_local_map.const import (
    CONF_DEVICE_ID,
    CONF_HOST,
    CONF_LOCAL_KEY,
    CONF_VERSION,
    DOMAIN,
)

from ..fakes import DEVICE_ID, HOST, KEY

FRAMES = {
    f["name"]: f
    for f in json.loads(
        (pathlib.Path(__file__).parent.parent / "fixtures/frames.json").read_text()
    )
}
USER_INPUT = {
    CONF_HOST: HOST,
    CONF_DEVICE_ID: DEVICE_ID,
    CONF_LOCAL_KEY: KEY,
    CONF_VERSION: "3.3",
}
DATA = dict(USER_INPUT)
ENTRY_ID = "test_entry_id"


def envelope(info_type, data, **extra):
    """A DP value as the vacuum sends it: base64 of a JSON envelope."""
    body = {"data": data, "infoType": info_type, "message": "ok", **extra}
    return base64.b64encode(json.dumps(body).encode()).decode()


def chunk_dps(path_id, start, points, total=None):
    data = {
        "pathID": path_id,
        "pointCounts": len(points),
        "posArray": [list(p) for p in points],
        "startPos": start,
        "totalPoints": start + len(points) if total is None else total,
    }
    return {"123": envelope(21011, data, ts=path_id)}


def rooms_dps(map_id=1000000001, rooms=((1, "Hall"), (2, "Study"))):
    entries = [{"id": i, "name": n, "mode": "autolayer"} for i, n in rooms]
    data = {"autoAreaValue": entries, "mapId": map_id, "userId": "u", "sn": "s"}
    return {"127": envelope(21004, data, dInfo={"userId": "u"})}


def room_dps():
    return {FRAMES["room_list"]["dp"]: FRAMES["room_list"]["b64"]}


def make_entry(options=None) -> ConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data=DATA,
        unique_id=DEVICE_ID,
        options=options or {},
        entry_id=ENTRY_ID,
    )


async def setup_entry(hass, options=None):
    entry = make_entry(options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def serpentine(width=2000, height=1000, step=50, lane=250):
    points, y, forward = [], 0, True
    while y <= height:
        xs = range(0, width + 1, step)
        points += [(x, y) for x in (xs if forward else reversed(xs))]
        forward = not forward
        y += lane
    return points
