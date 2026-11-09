"""Diagnostics: counts and field names, never keys, IDs, hosts, names or maps."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import (
    CONF_BRUSH,
    CONF_DEVICE_ID,
    CONF_HIDDEN_ROOMS,
    CONF_HOST,
    CONF_LOCAL_KEY,
    CONF_ROOM_NAMES,
    CONF_UNITS_PER_METRE,
)
from .coordinator import TuyaVacuumConfigEntry

OPTIONS_SHOWN = (CONF_BRUSH, CONF_UNITS_PER_METRE)
TO_REDACT = {CONF_LOCAL_KEY, CONF_DEVICE_ID, CONF_HOST, "mapId", "userId", "sn"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: TuyaVacuumConfigEntry
) -> dict[str, Any]:
    # An allowlist, so an option added later stays out until someone chooses it.
    # The room options are counts: the names are the user's own words.
    options = {key: entry.options[key] for key in OPTIONS_SHOWN if key in entry.options}
    options[CONF_HIDDEN_ROOMS] = len(entry.options.get(CONF_HIDDEN_ROOMS, []))
    options[CONF_ROOM_NAMES] = len(entry.options.get(CONF_ROOM_NAMES, {}))
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": options,
        "state": entry.runtime_data.diagnostics(),
    }
