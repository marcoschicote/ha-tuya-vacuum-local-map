"""Diagnostics: counts and field names, never keys, IDs, hosts, names or maps."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_DEVICE_ID, CONF_HOST, CONF_LOCAL_KEY
from .coordinator import TuyaVacuumConfigEntry

TO_REDACT = {CONF_LOCAL_KEY, CONF_DEVICE_ID, CONF_HOST, "mapId", "userId", "sn"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: TuyaVacuumConfigEntry
) -> dict[str, Any]:
    options = dict(entry.options)
    # Which rooms are hidden is a count here: IDs alone say nothing, but keep it short.
    options["hidden_rooms"] = len(options.get("hidden_rooms", []))
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": options,
        "state": entry.runtime_data.diagnostics(),
    }
